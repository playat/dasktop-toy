"""DSH 对接：消费 DSH 上由 dsh-pet 插件注册的 `/dsh-pet-7340` HTTP 契约。

上游那份插件（TypeScript，跑在 DSH 宿主进程里）把「余额 / 工作状态 / 通知 / 碎碎念 / 点播动画」
这些能力挂在 **DSH 自己的 Web 服务**上（`dsh web`，默认 `127.0.0.1:3080`），契约见上游
`openapi.yaml`，**只监听本机且没有鉴权**，所以这个独立版不必装 Node / Electron，直接轮询就行：

    GET  /dsh-pet-7340/state?since=&timeout=   长轮询：所有状态位的唯一出口（只读、纯内存）
    POST /dsh-pet-7340/whisper?pet=            让 DSH 用它配的模型生成一句碎碎念
    POST /dsh-pet-7340/chat?pet=               对桌宠说一句话（带记忆）
    POST /dsh-pet-7340/balance                 立即刷新一次余额
    POST /dsh-pet-7340/pose/report?pet=        上报桌面宠物位置（网页端/第三方能读到）

**探测不到就整块关掉**。顺序：`config.dsh.baseUrl` → 环境变量 `DSH_PET_URL` / `DSH_WEB_URL`
→ 默认 3080。找不到就退化成纯本地模式，所有依赖 DSH 的入口（余额、工作状态、通知、AI 说话）
自动隐藏，桌宠照常跑 —— 用户要的就是「没 DSH 环境就别开这套」。

叶子比对沿用上游口径：每个叶子 `{counter, data}`，**counter 变了才算更新**；首次拉到只记基线
不渲染，否则启东瞬间会把上一次运行留下的旧状态全播一遍。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from PySide6.QtCore import QObject, Signal

PREFIX = "/dsh-pet-7340"
DEFAULT_BASE = "http://127.0.0.1:3080"
ENV_KEYS = ("DSH_PET_URL", "DSH_WEB_URL", "DSH_URL")
PROBE_TIMEOUT = 1.5
POLL_TIMEOUT = 2000          # 长轮询挂起上限（ms）：越小越跟手，越大越省
MIN_POLL_GAP = 0.15          # 两拍之间至少隔这么久（对面一拍就回时不至于空转）
OFFLINE_RETRY_SEC = 3.0


# ---------------------------------------------------------------- HTTP


def _request(
    url: str, *, method: str = "GET", body: dict[str, Any] | None = None, timeout: float = 6.0
) -> dict[str, Any] | None:
    """发一个请求并把响应解成 dict；任何失败（连不上 / 非 2xx / 不是 JSON）都返回 None。"""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_base(url: str) -> str:
    """把 `127.0.0.1:3080` / `http://x/dsh-pet-7340` 这类写法归一成 `http://host:port`。"""
    text = (url or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "http://" + text
    parts = urllib.parse.urlsplit(text)
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    if path.endswith(PREFIX):
        path = path[: -len(PREFIX)]
    return base + path


def detect(configured: str = "") -> tuple[str | None, str]:
    """找 DSH：返回 (base_url 或 None, 一行说明)。"""
    candidates: list[str] = []
    for raw in (configured, *(os.environ.get(key, "") for key in ENV_KEYS)):
        base = normalize_base(raw)
        if base and base not in candidates:
            candidates.append(base)
    if not candidates and DEFAULT_BASE not in candidates:
        candidates.append(DEFAULT_BASE)

    for base in candidates:
        state = _request(f"{base}{PREFIX}/state", timeout=PROBE_TIMEOUT)
        if isinstance(state, dict) and "pets" in state:
            return base, f"已连上 {base}（DSH 桌宠插件在跑）"
    return None, "没找到 DSH（跳过余额/工作状态/通知/DSH 说话，纯本地模式）"


# ---------------------------------------------------------------- 桥


class DshBridge(QObject):
    """DSH 状态轮询 + 主动调用。所有网络都在工作线程里，UI 只收信号。"""

    online = Signal(bool, str)          # (在线?, 说明)
    say = Signal(str, str, str, int)    # pet, text, image(池内名或空), duration_ms
    anim = Signal(str, str)             # pet, 动画名
    work_status = Signal(object)        # dict|None → WorkStatusSnapshot
    balance = Signal(object)            # dict|None → BalanceState
    notify = Signal(object)             # dict → NotifyFrame
    locked = Signal(str, bool)          # pet, 是否被第三方占用
    spoken = Signal(str, str, str)      # pet, text, image —— whisper/chat 的回复

    def __init__(self, base_url: str, pet_ids: list[str], *, report_pose: bool = True) -> None:
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.pet_ids = [p for p in pet_ids if p] or ["main"]
        self.report_pose = report_pose
        self.is_online = False

        self._stop = threading.Event()
        self._since = 0
        self._baseline: dict[str, int] | None = None
        self._poses: dict[str, tuple[float, float, str]] = {}
        self._pose_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dsh-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---------- 主动调用（主线程可直接调，网络在线程里做） ----------

    def whisper(self, pet: str | None = None) -> None:
        self._fire(lambda: self._post_command("/whisper", pet, "spoken_or_say"))

    def chat(self, text: str, pet: str | None = None) -> None:
        self._fire(lambda: self._post_command("/chat", pet, "spoken_or_say", {"text": text}))

    def refresh_balance(self) -> None:
        self._fire(lambda: self._request("/balance", method="POST", body={}))

    def set_pose(self, pet: str, x: float, y: float, facing: str) -> None:
        """记录最新位置，由轮询线程顺手上报（不额外开线程）。"""
        with self._pose_lock:
            self._poses[pet or self.pet_ids[0]] = (
                max(0.0, min(1.0, x)),
                max(0.0, min(1.0, y)),
                "left" if facing == "left" else "right",
            )

    # ---------- 内部 ----------

    def _fire(self, job) -> None:
        if not self.is_online:
            return
        threading.Thread(target=job, name="dsh-cmd", daemon=True).start()

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any] | None:
        url = f"{self.base_url}{PREFIX}{path}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        return _request(url, method=method, body=body, timeout=timeout)

    def _pet(self, pet: str | None) -> str:
        return pet or self.pet_ids[0]

    def _post_command(self, path: str, pet: str | None, emit: str, extra: dict | None = None) -> None:
        name = self._pet(pet)
        body = {"pet": name, **(extra or {})}
        got = self._request(path, method="POST", query={"pet": name}, body=body, timeout=30.0)
        if not isinstance(got, dict):
            return
        text = str(got.get("text") or "").strip()
        image = str(got.get("image") or "").strip()
        if text or image:
            self.spoken.emit(name, text, image)

    def _report_poses(self) -> None:
        if not self.report_pose:
            return
        with self._pose_lock:
            poses, self._poses = self._poses, {}
        for pet, (x, y, facing) in poses.items():
            self._request(
                "/pose/report",
                method="POST",
                query={"pet": pet},
                body={"x": round(x, 4), "y": round(y, 4), "facing": facing},
                timeout=2.0,
            )

    # ---------- 轮询 ----------

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            state = self._request(
                "/state",
                query={"since": str(self._since), "timeout": str(POLL_TIMEOUT)},
                timeout=POLL_TIMEOUT / 1000.0 + 4.0,
            )
            if state is None:
                if self.is_online:
                    self.is_online = False
                    self.online.emit(False, "DSH 连接断了（重试中）")
                self._since = 0
                self._baseline = None
                self._stop.wait(OFFLINE_RETRY_SEC)
                continue

            if not self.is_online:
                self.is_online = True
                self.online.emit(True, f"已连上 {self.base_url}")

            try:
                self._since = int(state.get("counter") or 0)
            except (TypeError, ValueError):
                self._since = 0
            self._dispatch(state)
            self._report_poses()

            # 兜底：对面一拍就回（counter 一直在变）时别把 CPU 打满
            spent = time.monotonic() - started
            if spent < MIN_POLL_GAP:
                self._stop.wait(MIN_POLL_GAP - spent)

    def _dispatch(self, state: dict[str, Any]) -> None:
        sections = state.get("sections") or {}
        pets = state.get("pets") or {}

        # 首拉只记基线：旧叶子不补播（用户开程序不该看到上次的余额动画）
        if self._baseline is None:
            self._baseline = {}
            for key, leaf in sections.items():
                self._baseline[f"s:{key}"] = _counter(leaf)
            for pet, slots in pets.items():
                if not isinstance(slots, dict):
                    continue
                for slot, leaf in slots.items():
                    self._baseline[f"p:{pet}:{slot}"] = _counter(leaf)
            return

        for key, leaf in sections.items():
            if _counter(leaf) == self._baseline.get(f"s:{key}"):
                continue
            self._baseline[f"s:{key}"] = _counter(leaf)
            data = leaf.get("data") if isinstance(leaf, dict) else None
            if key == "workStatus":
                self.work_status.emit(data if isinstance(data, dict) else None)
            elif key == "balance":
                self.balance.emit(data if isinstance(data, dict) else None)
            elif key == "notify":
                if isinstance(data, dict):
                    self.notify.emit(data)

        for pet, slots in pets.items():
            if not isinstance(slots, dict):
                continue
            for slot, leaf in slots.items():
                mark = f"p:{pet}:{slot}"
                if _counter(leaf) == self._baseline.get(mark):
                    continue
                self._baseline[mark] = _counter(leaf)
                data = leaf.get("data") if isinstance(leaf, dict) else None
                if slot == "say" and isinstance(data, dict):
                    if data.get("clear"):
                        self.say.emit(pet, "", "", 0)
                        continue
                    text = str(data.get("text") or "").strip()
                    image = str(data.get("image") or data.get("url") or "").strip()
                    try:
                        duration = int(data.get("duration") or 10_000)
                    except (TypeError, ValueError):
                        duration = 10_000
                    if text or image:
                        self.say.emit(pet, text, image, duration)
                elif slot == "anim" and isinstance(data, dict):
                    name = str(data.get("name") or "").strip()
                    if name:
                        self.anim.emit(pet, name)
                elif slot == "lock" and isinstance(data, dict):
                    expires = data.get("expiresAt")
                    locked = bool(data.get("locked"))
                    if locked and isinstance(expires, (int, float)):
                        locked = expires / 1000.0 > time.time()
                    self.locked.emit(pet, locked)


def _counter(leaf: Any) -> int:
    if not isinstance(leaf, dict):
        return 0
    try:
        return int(leaf.get("counter") or 0)
    except (TypeError, ValueError):
        return 0
