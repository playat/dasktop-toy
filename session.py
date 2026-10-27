"""会话状态：读 dsh 会话日志 → 折叠成状态 → **决定什么时候让桌宠做什么**。

（原来拆成 `pet_state.py` + `session.py` 两个文件，现在合并成一个：前者只负责
「读日志 → 折叠成状态」，它不知道桌宠该干什么；后者补上两头。两半在同一个文件里
按小节隔开，改的时候看小节标题就够了。）

只读磁盘，不需要启动 token / cookie / 正在运行的 dsh。日志是 append-only 的 zstd
多帧文件，本模块增量解码，因此可以高频 poll。

# 第一部分：读日志

最小用法：

    from session import PetWatcher

    watcher = PetWatcher()
    snap = watcher.poll()          # 每 1~2 秒调一次
    if snap.primary is not None:
        pet.set_animation(snap.primary.status)   # idle/thinking/tool/waiting_*
    for notice in snap.notices:                  # 首次 poll 为空
        pet.bubble(notice.message)

命令行：

    python session.py                 # 一行状态
    python session.py --json          # 完整快照
    python session.py --watch 2       # 每 2 秒检查，变化时输出
    python session.py --all           # 所有活跃会话

父进程不是 Python 时（Electron / Node）：

    python session.py --watch 2 --json --compact

每次状态变化输出一行 JSON，父进程逐行读即可，不必反复启进程（那样每次都要重新扫日志）。

依赖：Python 3.14+ 自带 compression.zstd；更早版本需要 `pip install zstandard`。
不需要联网，不需要 dsh 正在运行，也不需要任何令牌。

# 第二部分：什么时候触发（六条规则，按优先级）

**1. 只认「跃迁」，不认「状态本身」**
日志里 `step/start`、`tool/call`、`tool/result` 几秒一条，但 `status` 只在
「闲 → 想 → 做 → 等 → 闲」这些边界上变。所以按 `(会话, status)` 是否变化触发，
同一个状态持续多久都不会重复播 —— 这是不刷屏的根本。

**2. 忙碌内部抖动要合并**
一轮任务里 `thinking ↔ tool` 会随每一步来回切（一次工具调用切两下），
全按跃迁播会变成幻灯片。所以：**两个忙碌状态之间切换，要稳定 `minHoldMs` 才认**；
而 **从闲进入忙碌立即反应**（那是主人真的按了回车）。`waiting_*` 永远立即反应。

**3. 启动/换会话只对齐，不复读**
首次拿到快照时，状态是「当下正在发生的事」，值得立刻显示动画；但它是历史，
不该弹气泡（否则一开程序就被念一句）。同理，主会话被别的会话抢走时只静默换动画。

**4. 一次性事件走 notices**
`turn/end`（完成/出错/被中止/被截断）、`approval/asked`、后台任务结算、交付文件
—— 日志里这些是「发生了一次」的事件，读日志那一半已经做了首次静默 + 增量消费，
这里照单播一次，不会重播。多条同一批来的（比如一个 turn 结束顺便交付文件）合成一句。

**5. 停留太久才催，而且有上限**
等主人（`waiting_*`）超过 `waitNudgeSec` 开始催，之后每 `waitRepeatSec` 一次、
最多 `waitNudgeMax` 次；一直忙超过 `busyNudgeSec` 说一句，最多 `busyNudgeMax` 次。
**叫不动就不叫了** —— 桌宠的价值是「在场」，不是「催命」。

**6. 会挑主会话，并且粘住**
同时开几个会话时按「等主人 > 忙碌 > 空闲，再按最近活动」排序；但同档次时**粘住
当前那只**，免得两边一齐动、宠物在中间反复切状态。

# 什么时候不触发

- 日志目录不存在 / 没有 zstd → 整块关掉（`session_ready = False`），DSH 的工作状态照旧可用；
- 宠物正在被拖拽/抛掷/被第三方占用 → 动画层自己排后（`PetWindow.set_status` 记着，能播时补）；
- `session.bubble = false` → 只播动画不说话；
- 状态变回 `idle` 且日志里没有 `turn/end`（进程崩了、`staleSec` 兜底）→ 安静回待机。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from PySide6.QtCore import QObject, Signal

try:  # Python 3.14+
    from compression import zstd as _zstd

    _ZSTD_IMPL = "stdlib"
except ImportError:  # pragma: no cover - depends on interpreter
    try:
        import zstandard as _zstd  # type: ignore[no-redef]

        _ZSTD_IMPL = "zstandard"
    except ImportError:
        _zstd = None  # type: ignore[assignment]
        _ZSTD_IMPL = None

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

IDLE = "idle"
THINKING = "thinking"
TOOL = "tool"
WAITING_ANSWER = "waiting_answer"
WAITING_APPROVAL = "waiting_approval"

QUESTION_TOOL_HINTS = ("ask_user_question", "user_question", "askuserquestion")


# ============================================================================ #
# 第一部分：读日志（纯文件 IO，不依赖 Qt）
# ============================================================================ #


def _decompress(blob: bytes) -> bytes:
    """解一帧 zstd。

    dsh 写的是**流式帧**：帧头里不带 content size，所以模块级 `decompress()` 会
    直接报 `could not determine content size in frame header`（python-zstandard 的
    行为；stdlib 版本同样）。带 `max_output_size` 调用才能解这种帧，所以这里按
    「模块级 → 带上限的 decompress → 流式对象」三级降级。
    """
    direct = getattr(_zstd, "decompress", None)
    if direct is not None:
        try:
            return direct(blob)
        except Exception:
            pass  # 绝大多数 dsh 日志都会走到下面的分支

    decoder = _zstd.ZstdDecompressor()  # type: ignore[union-attr]
    try:
        return decoder.decompress(blob, max_output_size=64 << 20)
    except TypeError:  # 老版本没有 max_output_size 关键字
        pass
    try:
        stream = decoder.decompressobj()
        out = stream.decompress(blob)
        try:
            out += stream.flush()
        except Exception:
            pass
        return out
    except Exception:
        return decoder.decompress(blob)  # type: ignore[union-attr]


class LogTail:
    """按 zstd 帧（或纯文本行）增量读取一个会话日志文件。"""

    def __init__(self, path: str, first_read_limit: int = 4 << 20) -> None:
        self.path = path
        self.first_read_limit = first_read_limit
        self.offset = 0
        self._started = False
        self._compressed = path.endswith(".zstd")

    def _priming_offset(self) -> int:
        size = os.path.getsize(self.path)
        if size <= self.first_read_limit:
            return 0
        with open(self.path, "rb") as handle:
            handle.seek(size - self.first_read_limit)
            head = handle.read(1 << 16)
        at = head.find(ZSTD_MAGIC)
        return size - self.first_read_limit + (at if at >= 0 else 0)

    def read_new(self) -> list[dict[str, Any]]:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if not self._started:
            self.offset = self._priming_offset() if self._compressed else 0
            self._started = True
        elif size < self.offset:  # 被截断或替换
            self.offset = 0

        if size <= self.offset:
            return []

        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            blob = handle.read()

        if self._compressed:
            payload, consumed = _decode_frames(blob)
            if consumed == 0:
                return []
            self.offset += consumed
        else:
            payload = blob
            cut = payload.rfind(b"\n")
            if cut < 0:
                return []
            self.offset += cut + 1
            payload = payload[: cut + 1]

        return _parse_lines(payload)


def _decode_frames(buf: bytes) -> tuple[bytes, int]:
    """解出所有完整帧，返回（解码内容, 已消费字节数）。末尾半帧留到下次。"""
    starts: list[int] = []
    pos = 0
    while True:
        at = buf.find(ZSTD_MAGIC, pos)
        if at < 0:
            break
        starts.append(at)
        pos = at + 1

    chunks: list[bytes] = []
    consumed = 0
    index = 0
    while index < len(starts):
        start = starts[index]
        matched = False
        # 帧内数据偶尔可能含魔数，失败就尝试更远的边界。
        for probe in range(index + 1, min(index + 5, len(starts) + 1)):
            end = starts[probe] if probe < len(starts) else len(buf)
            try:
                chunk = _decompress(buf[start:end])
            except Exception:
                continue
            chunks.append(chunk)
            consumed = end
            index = probe
            matched = True
            break
        if not matched:
            if not chunks:
                # 首次读取可能从某个帧中间开始，跳过这个半帧再试。
                index += 1
                continue
            break
    return b"".join(chunks), consumed


def _parse_lines(payload: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in payload.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(record, dict):
            events.append(record)
    return events


# ---------------------------------------------------------------- 状态模型


@dataclass
class Notice:
    """一次性的提醒，供桌宠弹气泡。"""

    kind: str
    session_id: str
    at: int
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "session_id": self.session_id,
            "at": self.at,
            "message": self.message,
            "data": self.data,
        }


@dataclass
class SessionState:
    session_id: str = ""
    path: str = ""
    cwd: str = ""
    project: str = ""
    title: str = ""
    status: str = IDLE
    status_detail: str = ""
    tool_name: str | None = None
    tool_target: str | None = None
    turn: int = 0
    step: int = 0
    model: str | None = None
    provider: str | None = None
    plan_mode: bool = False
    sandbox_mode: str | None = None
    permission_preset: str | None = None
    compacting: bool = False
    todo: list[dict[str, Any]] = field(default_factory=list)
    pending_jobs: list[dict[str, Any]] = field(default_factory=list)
    last_files: list[str] = field(default_factory=list)
    last_end_kind: str | None = None
    last_error: str | None = None
    stats: dict[str, Any] = field(default_factory=lambda: _empty_stats())
    last_event_at: int = 0
    turn_started_at: int = 0
    last_prompt_at: int = 0
    created_at: int = 0
    notice_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["stats"] = dict(self.stats)
        out["todo"] = [dict(item) for item in self.todo]
        out["pending_jobs"] = [dict(item) for item in self.pending_jobs]
        out["last_files"] = list(self.last_files)
        return out

    def busy_seconds(self, now_ms: int) -> int:
        if self.status == IDLE:
            return 0
        started = self.turn_started_at or self.last_event_at
        return max(0, (now_ms - started) // 1000) if started else 0


@dataclass
class Snapshot:
    now: int
    primary: SessionState | None
    sessions: list[SessionState]
    notices: list[Notice]
    changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "now": self.now,
            "primary": self.primary.to_dict() if self.primary else None,
            "sessions": [s.to_dict() for s in self.sessions],
            "notices": [n.to_dict() for n in self.notices],
        }


# ---------------------------------------------------------------- 单会话折叠


class SessionFold:
    def __init__(self, path: str, session_id: str = "", first_read_limit: int = 4 << 20) -> None:
        self.tail = LogTail(path, first_read_limit=first_read_limit)
        self.state = SessionState(path=path, session_id=session_id)
        self._calls: dict[str, dict[str, Any]] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._turn_open = False
        self._primed = False

    # -- 事件入口 ---------------------------------------------------------- #

    def pull(self, now_ms: int, stale_seconds: int) -> tuple[SessionState, list[Notice]]:
        notices: list[Notice] = []
        for event in self.tail.read_new():
            self._apply(event, notices)
        self._settle_status(now_ms, stale_seconds)
        if not self._primed:
            # 首次读取是在回放历史，不要把这些当成实时提醒。
            notices = []
            self._primed = True
        return self.state, notices

    # -- 折叠 -------------------------------------------------------------- #

    def _apply(self, event: dict[str, Any], notices: list[Notice]) -> None:
        kind = event.get("type")
        data = event.get("data") or {}
        at = int(event.get("time") or 0)
        seq = int(event.get("seq") or 0)
        state = self.state
        silent = not self._primed

        if kind == "session":  # 文件头
            state.session_id = str(event.get("id") or state.session_id)
            state.cwd = str(event.get("cwd") or "")
            state.project = os.path.basename(state.cwd.rstrip("\\/")) if state.cwd else ""
            state.created_at = int(event.get("createdAt") or 0)
            state.stats = _empty_stats()
            return

        if at:
            state.last_event_at = at
        if seq:
            state.notice_seq = seq

        if kind == "turn/start":
            state.turn = int(data.get("turn") or 0)
            state.step = 0
            state.turn_started_at = at
            self._turn_open = True
            self._calls.clear()
            self._approvals.clear()
            state.todo = []
            state.last_end_kind = None
            state.compacting = False
            state.status = THINKING
            state.status_detail = ""
            state.tool_name = None
            state.tool_target = None
            state.stats["turns"] += 1

        elif kind == "step/start":
            state.step = int(data.get("step") or 0)
            state.stats["steps"] += 1
            state.compacting = False

        elif kind == "step/end":
            state.compacting = False

        elif kind == "tool/call":
            call_id = str(data.get("callId") or "")
            name = str(data.get("name") or "")
            self._calls[call_id] = {
                "name": name,
                "target": _target_of(name, data.get("arguments")),
                "at": at,
            }
            state.stats["tools"] += 1
            state.tool_name = name
            state.tool_target = self._calls[call_id]["target"]

        elif kind == "tool/result":
            message = data.get("message") or {}
            call_id = str((message.get("source") or {}).get("callId") or data.get("callId") or "")
            started = self._calls.pop(call_id, None)
            if started and at and started.get("at"):
                state.stats["tool_ms"] += max(0, at - int(started["at"]))
            if data.get("error"):
                state.stats["tool_errors"] += 1
            rest = self._calls
            if rest:
                latest = list(rest.values())[-1]
                state.tool_name = latest["name"]
                state.tool_target = latest["target"]
            else:
                state.tool_name = None
                state.tool_target = None

        elif kind == "approval/asked":
            approval_id = str(data.get("id") or "")
            self._approvals[approval_id] = {"tool": data.get("toolName"), "reason": data.get("reason")}
            if not silent:
                tool = data.get("toolName") or "某个工具"
                notices.append(self._notice("approval", at, f"要动 {tool}，等你点头", {"tool": tool}))

        elif kind == "approval/decided":
            self._approvals.pop(str(data.get("id") or ""), None)

        elif kind == "turn/end":
            reason = data.get("reason") or {}
            reason_kind = str(reason.get("kind") or "")
            state.last_end_kind = reason_kind
            self._turn_open = False
            self._calls.clear()
            self._approvals.clear()
            if reason_kind == "error":
                err = (reason.get("error") or {})
                state.last_error = str(err.get("message") or err.get("code") or "未知错误")
                state.stats["errors"] += 1
                if not silent:
                    notices.append(self._notice("error", at, _error_line(state.last_error), {"reason": reason}))
            elif reason_kind == "completed":
                if not silent:
                    notices.append(self._notice("turn_end", at, "干完了", {"reason": reason_kind}))
            elif reason_kind in ("aborted", "blocked"):
                if not silent:
                    notices.append(self._notice("turn_end", at, "被叫停了", {"reason": reason_kind}))
            elif reason_kind == "max-tokens":
                if not silent:
                    notices.append(self._notice("turn_end", at, "话没说完就被截断了", {"reason": reason_kind}))

        elif kind == "session/title":
            state.title = str(data.get("title") or "")

        elif kind == "model/selection":
            state.provider = data.get("provider")
            state.model = data.get("model")

        elif kind == "plan/mode":
            state.plan_mode = bool(data.get("active"))

        elif kind == "sandbox/mode":
            state.sandbox_mode = data.get("mode")

        elif kind == "permission/preset":
            state.permission_preset = data.get("preset")

        elif kind in ("compaction/start",):
            state.compacting = True

        elif kind in ("compaction/end", "compaction/prune"):
            state.compacting = False

        elif kind == "todo/write":
            todos = data.get("todos") or []
            state.todo = [
                {"content": str(item.get("content") or ""), "status": str(item.get("status") or "")}
                for item in todos
                if isinstance(item, dict)
            ]

        elif kind == "job/registered":
            job_id = str(data.get("jobId") or "")
            self._jobs[job_id] = {
                "id": job_id,
                "kind": data.get("kind"),
                "label": data.get("label"),
                "startedAt": data.get("startedAt") or at,
            }

        elif kind == "job/settled":
            job_id = str(data.get("jobId") or "")
            self._jobs.pop(job_id, None)
            status = str(data.get("status") or "")
            if not silent:
                label = data.get("label") or "后台任务"
                if status == "completed":
                    text = f"{label} 跑完了"
                elif status == "failed":
                    text = f"{label} 挂了"
                else:
                    text = f"{label} 被中止了"
                notices.append(self._notice("job", at, text, {"status": status, "detail": data.get("detail")}))

        elif kind == "deliverables/presented":
            files = []
            for item in data.get("files") or []:
                if isinstance(item, dict):
                    files.append(str(item.get("path") or item.get("name") or ""))
                else:
                    files.append(str(item))
            state.last_files = [f for f in files if f]
            if state.last_files and not silent:
                first = os.path.basename(state.last_files[0])
                notices.append(self._notice("deliverable", at, f"交付了 {first}", {"files": state.last_files}))

        elif kind == "assistant/attempt":
            _absorb_usage(state, data)

        elif kind == "user/message":
            state.last_prompt_at = at

        self._recompute_pending()

    def _recompute_pending(self) -> None:
        state = self.state
        state.pending_jobs = list(self._jobs.values())
        if self._approvals:
            state.status = WAITING_APPROVAL
            first = list(self._approvals.values())[0]
            state.status_detail = str(first.get("tool") or "")
            return
        if any(_is_question_call(call["name"]) for call in self._calls.values()):
            state.status = WAITING_ANSWER
            state.status_detail = ""
            state.tool_name = None
            state.tool_target = None
            return
        if self._calls:
            state.status = TOOL
            latest = list(self._calls.values())[-1]
            state.tool_name = latest["name"]
            state.tool_target = latest["target"]
            state.status_detail = ""
            return
        if self._turn_open:
            state.status = THINKING
            state.status_detail = "整理上下文" if state.compacting else ""
            return
        state.status = IDLE
        state.status_detail = ""
        state.tool_name = None
        state.tool_target = None

    def _settle_status(self, now_ms: int, stale_seconds: int) -> None:
        state = self.state
        if state.status == IDLE:
            return
        if stale_seconds <= 0 or state.last_event_at <= 0:
            return
        # 进程崩溃会留下没有 turn/end 的日志，别让桌宠永远卡在“工作中”。
        if now_ms - state.last_event_at > stale_seconds * 1000:
            self._turn_open = False
            self._calls.clear()
            self._approvals.clear()
            self._recompute_pending()
            state.status = IDLE
            state.last_end_kind = "stale"

    def _notice(self, kind: str, at: int, message: str, data: dict[str, Any]) -> Notice:
        return Notice(kind=kind, session_id=self.state.session_id, at=at or self.state.last_event_at, message=message, data=data)


# ---------------------------------------------------------------- 工具函数


def _empty_stats() -> dict[str, Any]:
    return {
        "turns": 0,
        "steps": 0,
        "tools": 0,
        "tool_errors": 0,
        "errors": 0,
        "tool_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def _is_question_call(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in QUESTION_TOOL_HINTS)


def _target_of(name: str | None, raw_arguments: Any) -> str | None:
    if not raw_arguments or not isinstance(raw_arguments, str):
        return None
    try:
        parsed = json.loads(raw_arguments)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return _short(value.strip())
    return None


def _short(text: str, limit: int = 48) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _error_line(message: str) -> str:
    lowered = message.lower()
    if "insufficient balance" in lowered or "insufficient_balance" in lowered:
        return "余额不够了，任务中断"
    return _short(message, 60)


def _absorb_usage(state: SessionState, data: dict[str, Any]) -> None:
    stream = data.get("stream") or []
    for record in stream:
        if not isinstance(record, dict):
            continue
        chunk = record.get("chunk")
        if not isinstance(chunk, dict):
            continue
        usage = chunk.get("usage")
        if not isinstance(usage, dict):
            continue
        state.stats["input_tokens"] += _int_field(
            usage, "inputTokens", "input_tokens", "promptTokens", "prompt_tokens", "uncachedInputTokens"
        )
        state.stats["output_tokens"] += _int_field(
            usage, "outputTokens", "output_tokens", "completionTokens", "completion_tokens"
        )


def _int_field(payload: dict[str, Any], *names: str) -> int:
    for name in names:
        value = payload.get(name)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


STATUS_TEXT = {
    IDLE: "闲",
    THINKING: "想事情",
    TOOL: "干活",
    WAITING_ANSWER: "等你回答",
    WAITING_APPROVAL: "等你点头",
}


def describe(state: SessionState, now_ms: int) -> str:
    parts = [STATUS_TEXT.get(state.status, state.status)]
    if state.status == TOOL and state.tool_name:
        parts.append(state.tool_name)
        if state.tool_target:
            parts.append(state.tool_target)
    elif state.status == WAITING_APPROVAL and state.status_detail:
        parts.append(state.status_detail)
    elif state.status == THINKING and state.status_detail:
        parts.append(state.status_detail)
    elif state.status == IDLE and state.last_end_kind == "error" and state.last_error:
        parts.append(_error_line(state.last_error))
    if state.status != IDLE:
        seconds = state.busy_seconds(now_ms)
        if seconds:
            parts.append(f"{seconds}s")
    if state.todo:
        done = sum(1 for item in state.todo if item["status"] == "completed")
        parts.append(f"待办 {done}/{len(state.todo)}")
    if state.project:
        parts.append(f"@{state.project}")
    return " · ".join(parts)


# ---------------------------------------------------------------- 观察者


class PetWatcher:
    def __init__(
        self,
        home: str | None = None,
        *,
        max_sessions: int = 8,
        max_age_hours: float = 72,
        stale_seconds: int = 600,
        first_read_limit: int = 4 << 20,
    ) -> None:
        self.home = home or os.environ.get("DSH_HOME") or os.path.expanduser("~/.dsh")
        self.max_sessions = max_sessions
        self.max_age_hours = max_age_hours
        self.stale_seconds = stale_seconds
        self.first_read_limit = first_read_limit
        self._folds: dict[str, SessionFold] = {}
        self._primed = False
        self._last_signature: tuple[Any, ...] | None = None
        self._paths: list[str] = []
        self._paths_at = 0.0
        self.discover_interval = 3.0

    def log_paths(self) -> list[str]:
        # 桌宠是高频 poll 的，没必要每次扫描整个 sessions 目录。
        if self._paths and time.time() - self._paths_at < self.discover_interval:
            return self._paths
        self._paths = self._discover()
        self._paths_at = time.time()
        return self._paths

    def _discover(self) -> list[str]:
        pattern = os.path.join(self.home, "sessions", "*", "session-*", "session.*.jsonl*")
        found = []
        for path in glob.glob(pattern):
            try:
                found.append((os.path.getmtime(path), path))
            except OSError:
                continue
        found.sort(reverse=True)
        cutoff = time.time() - self.max_age_hours * 3600
        keep = [path for mtime, path in found if mtime >= cutoff][: self.max_sessions]
        if not keep:  # 全都过期时至少给一个，方便离线调试
            keep = [path for _, path in found[:1]]
        return keep

    def poll(self, now_ms: int | None = None) -> Snapshot:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        notices: list[Notice] = []
        live: list[SessionState] = []
        paths = self.log_paths()

        for path in paths:
            fold = self._folds.get(path)
            if fold is None:
                fold = SessionFold(path, first_read_limit=self.first_read_limit)
                self._folds[path] = fold
            state, found = fold.pull(now, self.stale_seconds)
            live.append(state)
            notices.extend(found)

        for key in set(self._folds) - set(paths):
            self._folds.pop(key, None)

        live.sort(key=lambda s: (s.status != IDLE, s.last_event_at), reverse=True)
        primary = live[0] if live else None
        if not self._primed:
            notices = []
            self._primed = True

        signature = tuple((s.session_id, s.status, s.tool_name, s.last_event_at, len(s.todo)) for s in live)
        changed = signature != self._last_signature
        self._last_signature = signature
        return Snapshot(now=now, primary=primary, sessions=live, notices=notices, changed=changed)


# ============================================================================ #
# 第二部分：触发决策与轮询（session.py 原有）
# ============================================================================ #

# 状态 → 素材名（素材在 assets/webm/工作学习/ 下，AnimationSet.path_of 会按 stem 兜底）
ANIM_THINKING = "工作状态-思考冒泡"
ANIM_COMPACT = "工作状态-清点归档"
ANIM_TOOL = "工作状态-忙碌点按"
ANIM_WAIT = "工作状态-原地踱步张望"
ANIM_CHEER = "工作状态-雀跃庆祝"
ANIM_SIGH = "工作状态-垂头叹气冒汗"

BUSY = {THINKING, TOOL}
ATTENTION = {WAITING_APPROVAL, WAITING_ANSWER}

# notice 动画的抢夺优先级（大者胜）：出错压住庆祝，庆祝压住「等你」
_PRIO = {ANIM_SIGH: 3, ANIM_CHEER: 2, ANIM_WAIT: 1}


# ---------------------------------------------------------------- 环境探测


def home_of(cfg) -> str:
    """dsh home：配置 > $DSH_HOME > ~/.dsh。"""
    return (cfg.home or os.environ.get("DSH_HOME") or os.path.expanduser("~/.dsh")).strip()


def detect(cfg) -> tuple[bool, str]:
    """能不能用日志观察（只看文件在不在，不读内容）。"""
    if _ZSTD_IMPL is None:
        return False, "没有 zstd 支持（pip install zstandard），跳过状态观察"
    root = os.path.join(home_of(cfg), "sessions")
    if not os.path.isdir(root):
        return False, f"没找到会话日志目录 {root}，跳过状态观察"
    pattern = os.path.join(root, "*", "session-*", "session.*.jsonl*")
    if next(glob.iglob(pattern), None) is None:
        return False, f"{root} 里还没有会话日志，跳过状态观察"
    return True, f"观察 {root}（{_ZSTD_IMPL} 解压）"


def build_payload(snap: Snapshot) -> dict[str, Any]:
    """把 Snapshot 压成主线程能安全持有的普通 dict（快照对象下一次 poll 会被改写）。"""
    sessions: list[dict[str, Any]] = []
    primary: dict[str, Any] | None = None
    for state in snap.sessions:
        data = state.to_dict()
        data["busy_sec"] = state.busy_seconds(snap.now)
        sessions.append(data)
        if state is snap.primary:
            primary = data
    if primary is None and sessions:
        primary = sessions[0]
    return {
        "now": snap.now,
        "primary": primary,
        "sessions": sessions,
        "notices": [notice.to_dict() for notice in snap.notices],
        "brief": describe(snap.primary, snap.now) if snap.primary else "",
    }


# ---------------------------------------------------------------- 触发决策


def _sid_of(state: dict[str, Any]) -> str:
    return str(state.get("session_id") or state.get("path") or "")


def _anim_of(state: dict[str, Any]) -> str | None:
    status = str(state.get("status") or "")
    if status == THINKING:
        return ANIM_COMPACT if state.get("compacting") else ANIM_THINKING
    if status == TOOL:
        return ANIM_TOOL
    if status in ATTENTION:
        return ANIM_WAIT
    return None


class ReactionPolicy:
    """状态 → 反应，以及**什么时候**反应。纯逻辑（不碰 Qt、不读文件），可以单独测。

    `plan(snapshot_dict)` 返回一串动作，按执行顺序：

        ("hold", 素材名 | None)   把动画切成这个「持续状态」；None = 回随机链
        ("play", 素材名)          一次性特效（庆祝/叹气），播完自动回上面的 hold
        ("say", 文本)             弹气泡
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._sid = ""
        self._status = ""
        self._anim: str | None = None
        self._since = 0
        self._pending: tuple[str, int] | None = None
        self._nudges = 0
        self._last_nudge = 0

    # -- 对外 ------------------------------------------------------------ #

    def plan(self, snapshot: dict[str, Any]) -> list[tuple]:
        now = int(snapshot.get("now") or 0)
        sessions = [s for s in snapshot.get("sessions") or [] if isinstance(s, dict)]
        focus = self._focus(sessions)
        if focus is None:
            if self._status:
                self._reset()
                return [("hold", None)]
            return []

        sid = _sid_of(focus)
        status = str(focus.get("status") or IDLE)
        anim = _anim_of(focus)
        text, notice_anim = self._read_notices(snapshot.get("notices") or [])
        acts: list[tuple] = []

        # 规则 3：首次 / 换主会话 —— 对齐当下，不复读历史
        if sid != self._sid or not self._status:
            self._sid = sid
            self._status = status
            self._anim = anim
            self._since = now
            self._pending = None
            self._nudges = 0
            acts.append(("hold", anim))
            if notice_anim and notice_anim != anim:
                acts.append(("play", notice_anim))
            if text:
                acts.append(("say", text))
            return self._gate(acts)

        # 规则 1 + 2：只有一个状态真的变了才动
        if status != self._status:
            if self._status in BUSY and status in BUSY:
                # 抖动合并：还没记待确认 → 记下截止时间；够稳了 → 认这次切换
                if self._pending is None or self._pending[0] != status:
                    self._pending = (status, now + self.cfg.min_hold_ms)
                elif now >= self._pending[1]:
                    self._pending = None
                    self._status = status
                    self._anim = anim
                    self._since = now
                    self._nudges = 0
                    acts.append(("hold", anim))
            else:
                self._pending = None
                acts.extend(self._switch(status, anim, now, quiet=bool(text)))
        elif self._pending is not None:
            self._pending = None  # 又切回去了，当没发生
        elif anim != self._anim:
            # 同一状态里的子形态（比如 thinking 中开始「整理上下文」）
            self._anim = anim
            acts.append(("hold", anim))

        # 规则 4：一次性事件（在 hold 之后执行，形状是「状态底 + 特效盖一层」）
        if notice_anim:
            acts.append(("play", notice_anim))
        if text:
            acts.append(("say", text))

        # 规则 5：久等/久忙才催
        acts.extend(self._nudge(now))
        return self._gate(acts)

    # -- 状态跃迁 -------------------------------------------------------- #

    def _switch(self, status: str, anim: str | None, now: int, *, quiet: bool) -> list[tuple]:
        acts: list[tuple] = [("hold", anim)]
        prev = self._status
        self._status = status
        self._anim = anim
        self._since = now
        self._nudges = 0
        if quiet:
            return acts  # 同一批已经有 notice 要说了，不抢话
        if status in ATTENTION:
            line = self._pick("wait")
            if line:
                acts.append(("say", line))
        elif status in BUSY and prev not in BUSY:
            line = self._pick("start")
            if line:
                acts.append(("say", line))
        return acts

    # -- 催 -------------------------------------------------------------- #

    def _nudge(self, now: int) -> list[tuple]:
        """催：从「我们确认这个状态」算起，而不是从日志里的 turn 开始算。

        这样刚启动就接手一个跑了十分钟的会话不会立刻被念一句；而且忙碌内部
        一切换（想↔做）就把计时清零 —— 它在动，说明没卡住，没必要催。
        """
        if self._status in ATTENTION:
            limit = self.cfg.wait_nudge_max
            if self._nudges >= limit:
                return []
            if now - self._since < self.cfg.wait_nudge_sec * 1000:
                return []
            if self._nudges and now - self._last_nudge < self.cfg.wait_repeat_sec * 1000:
                return []
            return self._bump("wait", now)
        if self._status in BUSY:
            if self._nudges >= self.cfg.busy_nudge_max:
                return []
            if now - self._since < self.cfg.busy_nudge_sec * 1000:
                return []
            if self._nudges and now - self._last_nudge < self.cfg.busy_nudge_sec * 1000:
                return []
            return self._bump("busy", now)
        return []

    def _bump(self, kind: str, now: int) -> list[tuple]:
        line = self._pick(kind)
        self._nudges += 1
        self._last_nudge = now
        return [("say", line)] if line else []

    # -- 小工具 ---------------------------------------------------------- #

    @staticmethod
    def _rank(state: dict[str, Any]) -> int:
        status = str(state.get("status") or "")
        if status in ATTENTION:
            return 3
        if status in BUSY:
            return 2
        return 1

    def _focus(self, sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
        """挑一只会话当「主会话」：同档次时粘住当前这只，避免两只抢来抢去。"""
        if not sessions:
            return None
        ranked = sorted(
            sessions,
            key=lambda s: (self._rank(s), int(s.get("last_event_at") or 0)),
            reverse=True,
        )
        top = ranked[0]
        current = next((s for s in ranked if _sid_of(s) == self._sid), None)
        if current is not None and self._rank(current) >= self._rank(top):
            return current
        return top

    def _read_notices(self, notices: list[dict[str, Any]]) -> tuple[str | None, str | None]:
        texts: list[str] = []
        best: tuple[int, str] = (0, "")
        for notice in notices:
            if not isinstance(notice, dict):
                continue
            kind = str(notice.get("kind") or "")
            message = str(notice.get("message") or "").strip()
            data = notice.get("data") or {}
            anim = None
            if kind == "approval":
                anim = ANIM_WAIT
            elif kind == "error":
                anim = ANIM_SIGH
            elif kind == "turn_end":
                if str(data.get("reason") or "") == "completed":
                    anim = ANIM_CHEER
            elif kind == "job":
                status = str(data.get("status") or "")
                if status == "failed":
                    anim = ANIM_SIGH
                elif status == "completed":
                    anim = ANIM_CHEER
            elif kind == "deliverable":
                anim = ANIM_CHEER
            if anim and _PRIO.get(anim, 0) > best[0]:
                best = (_PRIO.get(anim, 0), anim)
            if message:
                texts.append(message)
        text = "；".join(dict.fromkeys(texts)) or None
        return text, (best[1] or None)

    def _pick(self, kind: str) -> str:
        rows = self.cfg.texts.get(kind) or []
        return random.choice(rows) if rows else ""

    def _gate(self, acts: list[tuple]) -> list[tuple]:
        """同一批里：hold 只留最后一个，say 只留最后一个（更具体的 notice 在最后）。"""
        if not self.cfg.bubble:
            acts = [a for a in acts if a[0] != "say"]
        holds = [i for i, a in enumerate(acts) if a[0] == "hold"]
        if len(holds) > 1:
            keep = holds[-1]
            acts = [a for i, a in enumerate(acts) if a[0] != "hold" or i == keep]
        says = [a for a in acts if a[0] == "say"]
        if len(says) > 1:
            acts = [a for a in acts if a[0] != "say"] + says[-1:]
        return acts

    def _reset(self) -> None:
        self._sid = ""
        self._status = ""
        self._anim = None
        self._since = 0
        self._pending = None
        self._nudges = 0
        self._last_nudge = 0


# ---------------------------------------------------------------- 轮询


class SessionWatcher(QObject):
    """工作线程里轮询会话日志；快照经信号回主线程（不在子线程碰 Qt 控件）。"""

    snapshot = Signal(object)   # build_payload() 的结果
    fault = Signal(str)         # 读日志出错（只报一次，恢复后重置）

    def __init__(self, cfg, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watcher = PetWatcher(
            home_of(cfg),
            max_sessions=cfg.max_sessions,
            stale_seconds=cfg.stale_sec,
        )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="session-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.5)

    def _loop(self) -> None:
        interval = max(0.3, self.cfg.poll_ms / 1000.0)
        warned = False
        while not self._stop.is_set():
            try:
                payload = build_payload(self._watcher.poll())
                self.snapshot.emit(payload)
                warned = False
            except Exception as exc:  # noqa: BLE001  读日志失败不该带走桌宠
                if not warned:
                    self.fault.emit(f"读日志出错（{type(exc).__name__}: {exc}）")
                    warned = True
            self._stop.wait(interval)


# ============================================================================ #
# 命令行（原 pet_state.py）
# ============================================================================ #


def _dump(payload: Any, compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def short_id(session_id: str) -> str:
    trimmed = session_id[8:] if session_id.startswith("session-") else session_id
    return trimmed[:8] or "-"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提取桌宠需要的 dsh 会话状态")
    parser.add_argument("--home", help="DSH 主目录，默认 $DSH_HOME 或 ~/.dsh")
    parser.add_argument("--json", action="store_true", help="输出完整快照 JSON")
    parser.add_argument("--compact", action="store_true", help="配合 --json 输出单行，便于父进程逐行读取")
    parser.add_argument("--all", action="store_true", help="列出所有会话而不只是主会话")
    parser.add_argument("--watch", type=float, metavar="SEC", help="轮询间隔，只打印变化")
    parser.add_argument("--stale", type=int, default=600, help="无事件多少秒后判为空闲")
    parser.add_argument("--events", action="store_true", help="只打印提醒事件")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if _zstd is None:
        print(
            "缺少 zstd 支持：Python 3.14+ 自带 compression.zstd；更早版本请 pip install zstandard",
            file=sys.stderr,
        )
        return 2

    watcher = PetWatcher(args.home, stale_seconds=args.stale)

    if args.watch:
        interval = max(0.2, args.watch)
        try:
            while True:
                snap = watcher.poll()
                if snap.changed or snap.notices:
                    _render(snap, args)
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0

    snap = watcher.poll()
    _render(snap, args)
    return 0 if snap.primary else 1


def _render(snap: Snapshot, args: argparse.Namespace) -> None:
    if args.json:
        _dump(snap.to_dict(), args.compact)
        return
    if args.events:
        for notice in snap.notices:
            stamp = time.strftime("%H:%M:%S", time.localtime(notice.at / 1000))
            print(f"[{stamp}] {notice.kind}: {notice.message}")
        return
    if args.all:
        for state in snap.sessions:
            print(f"{short_id(state.session_id)}  {describe(state, snap.now)}")
        return
    if snap.primary is None:
        print("没找到会话日志")
        return
    print(describe(snap.primary, snap.now))
    for notice in snap.notices:
        print(f"  -> {notice.message}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass
    raise SystemExit(main())
