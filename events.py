"""事件编排：把「谁在什么时候说什么、播什么」收在一处。

三个数据源，优先级从高到低：

1. **DSH**（`dsh.py`）—— 余额、工作状态、通知、点播动画、用 DSH 自己的模型说话。装了 DSH
   且插件在跑才有；这是原插件的原生链路。
2. **本机大模型**（`ai.py`）—— 只覆盖「说话」这一类（碎碎念 / 对话 / 挑表情包配图）。
   没 DSH 但有 API 时用它，端上和原插件的「独立模式」一个思路。
3. **纯本地** —— 既没 DSH 也没配模型：只播动画（余额/工作状态/碎碎念的素材本来就在），
   不弹文字气泡。所有依赖网络的入口自动隐藏。

素材按**名字**找（和上游 config.jsonc 的池子同名，仓库里就是这套名字）：
    余额-钱袋满溢 / 金袋叮当 / 钱袋如常 / 数金皱眉 / 袋空如洗 / 分文不剩   ← 0~100% 六档
    工作状态-思考冒泡 / 忙碌点按 / 清点归档 / 原地踱步张望 / 雀跃庆祝 / 垂头叹气冒汗  ← 六档
    碎碎念-擦桌碎碎念 / 发呆碎碎念 / 对屏碎碎念
名字找不到就跳过那一档（删了素材也不会报错）。
"""

from __future__ import annotations

import random
import threading
from collections import deque
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QInputDialog

import ai
from bubble import SpeechBubble
from config import PetConfig
from dsh import DshBridge
from memes import MemePool

# ---------------- 名字表（与上游 config.jsonc 的 events 池一致） ----------------

BALANCE_ANIMS = [
    "余额-钱袋满溢",
    "余额-金袋叮当",
    "余额-钱袋如常",
    "余额-数金皱眉",
    "余额-袋空如洗",
    "余额-分文不剩",
]
WORK_STAGES = ["thinking", "working", "result", "waiting", "success", "error"]
WORK_ANIMS = [
    "工作状态-思考冒泡",
    "工作状态-忙碌点按",
    "工作状态-清点归档",
    "工作状态-原地踱步张望",
    "工作状态-雀跃庆祝",
    "工作状态-垂头叹气冒汗",
]
WHISPER_PREFIX = "碎碎念-"

NOTIFY_ICONS = {
    "done": "notify-done.png",
    "error": "notify-error.png",
    "truncated": "notify-truncated.png",
    "approval": "notify-approval.png",
    "question": "notify-question.png",
    "test": "notify-test.png",
}

MAX_BODY = 80


def _truncate(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_BODY else text[:MAX_BODY] + "…"


def frame_to_toast(frame: dict[str, Any]) -> tuple[str, str, str] | None:
    """通知帧 → (标题, 正文, 图标名)。照抄上游 shared/notify.ts 的映射。"""
    kind = str(frame.get("type") or "")
    if kind == "session/event":
        event = frame.get("event") or {}
        if str(event.get("type") or "") != "turn/end":
            return None
        data = event.get("data") or {}
        reason = data.get("reason") or {}
        how = str(reason.get("kind") or "")
        if how == "completed":
            return "对话完成", "", "done"
        if how == "error":
            msg = str((reason.get("error") or {}).get("message") or "")
            return "生成失败", msg, "error"
        if how == "max-tokens":
            return "输出被截断", "已达到输出 token 上限", "truncated"
        return None
    if kind == "approval/requested":
        tool = str(frame.get("toolName") or "")
        reason = str(frame.get("reason") or "")
        body = (f"工具「{tool}」" if tool else "") + (f"：{reason}" if reason else "")
        return "正在申请权限", body, "approval"
    if kind == "question/requested":
        questions = frame.get("questions") or []
        first = str((questions[0] or {}).get("question") or "") if questions else ""
        return "模型在等你回答", first, "question"
    if kind == "host/agent-error":
        return "生成失败", str(frame.get("message") or ""), "error"
    return None


# ---------------- 余额口径 ----------------


def balance_percent(state: dict[str, Any]) -> float | None:
    """已用百分比（0 = 没花，100 = 用光）；给不出语义就返回 None。

    口径照上游 percent.ts：windows 取三窗口最大；money/points 按可用/总量折算；
    custom 直接用服务商给的 percent。
    """
    if not state.get("ok"):
        return None
    shape = str(state.get("shape") or "")
    data = state.get("data") or {}

    if shape == "windows":
        values = [data.get(key) for key in ("rolling", "weekly", "monthly")]
        nums = [float(v) for v in values if isinstance(v, (int, float))]
        return max(nums) if nums else None
    if shape == "points":
        try:
            total = float(str(data.get("total")).replace(",", ""))
            full = float(str(data.get("full")).replace(",", ""))
        except (TypeError, ValueError):
            return None
        return max(0.0, min(100.0, (1 - total / full) * 100)) if full > 0 else None
    if shape == "money":
        best = None
        for wallet in data.get("wallets") or []:
            if not isinstance(wallet, dict):
                continue
            try:
                total = float(str(wallet.get("total")).replace(",", ""))
                full = float(str(wallet.get("fullBalance")).replace(",", ""))
            except (TypeError, ValueError):
                continue
            if full > 0:
                got = max(0.0, min(100.0, (1 - total / full) * 100))
                best = got if best is None else max(best, got)
        return best
    if shape == "custom":
        value = data.get("percent")
        return float(value) if isinstance(value, (int, float)) else None
    return None


def balance_event_index(percent: float) -> int:
    """20% 一档；恰好 100% 单独算最高档（与上游 balanceEventIndex 一致）。"""
    if percent >= 100:
        return 5
    return min(4, int(percent // 20))


def balance_text(state: dict[str, Any]) -> str:
    if not state.get("ok"):
        reason = str(state.get("error") or state.get("reason") or "取不到")
        return f"余额暂时取不到（{reason}）"
    shape = str(state.get("shape") or "")
    data = state.get("data") or {}
    if shape == "windows":
        pct = balance_percent(state)
        return f"额度已用 {round(pct):d}%" if pct is not None else "额度正常"
    if shape == "money":
        parts = []
        for wallet in data.get("wallets") or []:
            if isinstance(wallet, dict) and wallet.get("total") is not None:
                parts.append(f"{wallet.get('currency') or ''} {wallet.get('total')}".strip())
        return "余额 " + " / ".join(parts) if parts else "余额正常"
    if shape == "points":
        return f"{data.get('unit') or '积分'} {data.get('total')}" + (
            f" / {data.get('full')}" if data.get("full") is not None else ""
        )
    return "余额已刷新"


class EventDirector(QObject):
    """窗口 + 气泡 + 数据源的总调度。主线程持有，一切 UI 操作都在主线程。"""

    status = Signal(str)   # 一行状态，给启动日志用

    def __init__(self, cfg: PetConfig, assets: Path, anims) -> None:
        super().__init__()
        self.cfg = cfg
        self.assets = Path(assets)
        self.anims = anims
        self.memes = MemePool(self.assets, cfg.memes)
        self.bridge: DshBridge | None = None

        self._bubbles: dict[str, SpeechBubble] = {}
        self._windows: dict[str, Any] = {}
        self._locked: set[str] = set()
        self._history: dict[str, deque[dict[str, str]]] = {}

        self._whisper_timer = QTimer(self)
        self._whisper_timer.timeout.connect(self._auto_whisper)
        self._balance_timer = QTimer(self)
        self._balance_timer.timeout.connect(self.refresh_balance)

        self._ai_busy: set[str] = set()
        self._ai_done = _Signals()
        self._ai_done.done.connect(self._on_ai_reply)
        # 右键菜单里的子菜单要留引用：QMenu 没人引用就会被回收，
        # 返回的 menuAction() 跟着失效（PySide 报 "QAction already deleted"）
        self._keep_menus: list = []

    # ---------------- 装配 ----------------

    def attach(self, pet_id: str, win) -> None:
        """给一只宠物配气泡，并挂上位置跟随。"""
        bubble = SpeechBubble()
        bubble.configure(
            pet_size=win.width(),
            family=self.cfg.font.family,
            size=self.cfg.font.size,
            follow=self.cfg.font.follow_pet,
        )
        self._bubbles[pet_id] = bubble
        self._windows[pet_id] = win
        win.attach_bubble(bubble)

    def detach_all(self) -> None:
        for bubble in self._bubbles.values():
            bubble.close()
        self._bubbles.clear()
        self._windows.clear()
        self._locked.clear()

    def pet_name(self, pet_id: str) -> str:
        for spec in self.cfg.pets:
            if spec.id == pet_id:
                return spec.name or spec.id
        return pet_id

    def _win(self, pet_id: str | None):
        if pet_id and pet_id in self._windows:
            return self._windows[pet_id]
        return next(iter(self._windows.values()), None)

    def _bubble(self, pet_id: str | None) -> SpeechBubble | None:
        win = self._win(pet_id)
        if win is None:
            return None
        for key, bubble in self._bubbles.items():
            if self._windows.get(key) is win:
                return bubble
        return None

    # ---------------- 启动 / 停止 ----------------

    def start(self) -> None:
        cfg = self.cfg
        if cfg.dsh.enabled:
            from dsh import detect

            base, note = detect(cfg.dsh.base_url)
            self.status.emit(f"[dsh] {note}")
            if base:
                pet_ids = [spec.id for spec in cfg.pets]
                self.bridge = DshBridge(base, pet_ids, report_pose=cfg.dsh.report_pose)
                self.bridge.online.connect(self._on_online)
                self.bridge.say.connect(self._on_say)
                self.bridge.anim.connect(self._on_anim)
                self.bridge.work_status.connect(self.on_work_status)
                self.bridge.balance.connect(self.on_balance)
                self.bridge.notify.connect(self.on_notify)
                self.bridge.locked.connect(self.on_locked)
                self.bridge.spoken.connect(self._on_spoken)
                self.bridge.start()
        else:
            self.status.emit("[dsh] 配置里关掉了（dsh.enabled = false）")

        if cfg.ai.ready:
            self.status.emit(f"[ai] 本机模型 {cfg.ai.model}（说话类功能可用）")

        if cfg.events.whisper_sec > 0 and self.whisper_ready:
            self._whisper_timer.start(max(30, cfg.events.whisper_sec) * 1000)
        if cfg.events.balance_sec > 0 and self.bridge is not None:
            self._balance_timer.start(max(60, cfg.events.balance_sec) * 1000)

    def stop(self) -> None:
        self._whisper_timer.stop()
        self._balance_timer.stop()
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None
        self.detach_all()

    # ---------------- 能力开关（菜单据此显示/隐藏） ----------------

    @property
    def dsh_online(self) -> bool:
        return self.bridge is not None and self.bridge.is_online

    @property
    def whisper_ready(self) -> bool:
        """能不能「说出一句话」：DSH 的模型或本机模型，有一个就行。"""
        return self.dsh_online or self.cfg.ai.ready

    @property
    def balance_ready(self) -> bool:
        return self.bridge is not None

    # ---------------- 说话 ----------------

    def say(
        self,
        pet_id: str | None,
        text: str,
        *,
        image: str | Path | None = None,
        icon: str | Path | None = None,
        duration_ms: int = 10_000,
        whisper: bool = True,
    ) -> bool:
        bubble = self._bubble(pet_id)
        win = self._win(pet_id)
        if bubble is None or win is None:
            return False

        image_path: Path | None = None
        if isinstance(image, str) and image:
            image_path = Path(image) if Path(image).exists() else self.memes.path_of(image)
        elif isinstance(image, Path):
            image_path = image

        icon_path: Path | None = None
        if isinstance(icon, str) and icon:
            icon_path = Path(icon) if Path(icon).is_absolute() else self.assets / "pic" / icon
        elif isinstance(icon, Path):
            icon_path = icon

        bubble.configure(
            pet_size=win.width(),
            family=self.cfg.font.family,
            size=self.cfg.font.size,
            follow=self.cfg.font.follow_pet,
        )
        return bubble.say(
            text,
            anchor=win.geometry(),
            image=image_path,
            icon=icon_path,
            duration_ms=duration_ms,
            whisper=whisper,
        )

    def show_meme(self, pet_id: str | None, name: str | None = None) -> None:
        """直接把某张表情包甩出来（不带文字，或者带上配置里那句描述）。"""
        self.memes.scan()
        key = name or self.memes.random()
        if key is None:
            self.say(pet_id, "（还没有表情包：把图放进 assets/memes/ 就行）")
            return
        desc = self.memes.describe(key)
        self.say(pet_id, desc if len(desc) <= 24 else "", image=key, duration_ms=8000)

    def whisper(self, pet_id: str | None = None, *, silent: bool = False) -> None:
        """让它说一句话：DSH 有就用 DSH 的模型，否则用本机模型；都没有就只播动画。"""
        win = self._win(pet_id)
        if win is None:
            return
        key = self._play_whisper_anim(pet_id)
        if not silent:
            self.status.emit(f"[say] 碎碎念{f'（{key}）' if key else ''}")

        if self.dsh_online and self.cfg.events.whisper:
            self.bridge.whisper(self._pet_arg(pet_id))
            return
        if self.cfg.ai.ready:
            self._ai_say(pet_id, kind="whisper")
            return
        if not silent and not key:
            self.say(pet_id, "（没有 DSH、也没配本机模型，只能比划不能说话）", duration_ms=4000)

    def chat(self, pet_id: str | None, text: str) -> None:
        """对桌宠说一句话。"""
        text = (text or "").strip()
        if not text:
            return
        if self.dsh_online and self.cfg.events.chat:
            self.bridge.chat(text, self._pet_arg(pet_id))
            return
        if self.cfg.ai.ready:
            self._ai_say(pet_id, kind="chat", text=text)
            return
        self.say(pet_id, "（我还没接上模型，先去配 ai.baseUrl）", duration_ms=4000)

    def _pet_arg(self, pet_id: str | None) -> str:
        return pet_id or self.cfg.dsh.pet_id or self.cfg.pets[0].id

    def _play_whisper_anim(self, pet_id: str | None) -> str | None:
        """随机抽一段 碎碎念-* 播一遍（没有素材就返回 None）。"""
        win = self._win(pet_id)
        if win is None:
            return None
        names = [k for k in self.anims.index if Path(k).stem.startswith(WHISPER_PREFIX)]
        if not names:
            return None
        key = random.choice(names)
        win.play_named(key)
        return key

    # ---------------- AI（本机模型，异步） ----------------

    def _ai_say(self, pet_id: str | None, *, kind: str, text: str = "") -> None:
        key = pet_id or self._pet_arg(None)
        if key in self._ai_busy:
            return
        self._ai_busy.add(key)
        name = self.pet_name(key)
        cfg = self.cfg.ai
        history = list(self._history.get(key, ()))
        if kind == "whisper":
            history = []  # 碎碎念不带上下文（省 token，也更像自言自语）

        def job() -> None:
            try:
                if kind == "whisper":
                    reply_text = ai.whisper(cfg, name)
                else:
                    reply_text = ai.reply(cfg, text, name, history)
                image = ""
                if reply_text and self.cfg.events.whisper and len(self.memes):
                    picked = ai.pick_meme(cfg, self.memes.brief(), reply_text)
                    hit = self.memes.pick_from(picked or "")
                    image = hit or ""
            finally:
                self._ai_busy.discard(key)
            self._ai_done.done.emit(key, reply_text or "", image)

        threading.Thread(target=job, name=f"ai-{kind}", daemon=True).start()

    def _on_ai_reply(self, pet_id: str, text: str, image: str) -> None:
        if not text:
            return
        if image:
            self.say(pet_id, text, image=image, duration_ms=12_000)
        else:
            self.say(pet_id, text, duration_ms=12_000)

    def _on_spoken(self, pet_id: str, text: str, image: str) -> None:
        """DSH 的 whisper/chat 回复。"""
        if not text and not image:
            return
        if text:
            self.say(pet_id, text, image=image or None, duration_ms=12_000)
        else:
            self.show_meme(pet_id, image)

    # ---------------- 菜单入口 ----------------

    def menu_actions(self, pet_id: str | None = None) -> list[QAction]:
        """右键菜单里的「说话」一块；什么都没有时返回空列表（菜单不显示这一项）。"""
        from PySide6.QtWidgets import QMenu

        from menu import MENU_QSS

        say_menu = QMenu("说话", None)
        say_menu.setStyleSheet(MENU_QSS)
        say_menu.setToolTipsVisible(True)
        self._keep_menus = [say_menu, *self._keep_menus][:4]

        whisper_act = QAction("碎碎念", say_menu)
        whisper_act.setEnabled(self.whisper_ready)
        whisper_act.setToolTip(
            "让桌宠说一句（DSH 的模型 / 本机模型）" if self.whisper_ready else "没找到 DSH，也没配本机模型"
        )
        whisper_act.triggered.connect(lambda _c=False: self.whisper(pet_id))
        say_menu.addAction(whisper_act)

        chat_act = QAction("说句话…", say_menu)
        chat_act.setEnabled(self.whisper_ready)
        chat_act.triggered.connect(lambda _c=False: self._ask_and_chat(pet_id))
        say_menu.addAction(chat_act)

        if len(self.memes):
            meme_menu = QMenu("表情包", say_menu)
            meme_menu.setStyleSheet(MENU_QSS)
            meme_menu.setToolTipsVisible(True)
            random_act = QAction(f"随机一张（共 {len(self.memes)} 张）", meme_menu)
            random_act.triggered.connect(lambda _c=False: self.show_meme(pet_id))
            meme_menu.addAction(random_act)
            meme_menu.addSeparator()
            for name in self.memes.names:
                act = QAction(name, meme_menu)
                desc = self.memes.describe(name)
                if desc:
                    act.setToolTip(desc)
                act.triggered.connect(lambda _c=False, n=name: self.show_meme(pet_id, n))
                meme_menu.addAction(act)
            say_menu.addMenu(meme_menu)

        if self.balance_ready:
            say_menu.addSeparator()
            balance_act = QAction("刷新余额", say_menu)
            balance_act.triggered.connect(lambda _c=False: self.refresh_balance())
            say_menu.addAction(balance_act)

        return [say_menu.menuAction()]

    def _ask_and_chat(self, pet_id: str | None) -> None:
        text, ok = QInputDialog.getText(None, "对桌宠说", "说点什么：")
        if ok and text.strip():
            self.chat(pet_id, text)

    # ---------------- 数据源回调 ----------------

    def _on_online(self, online: bool, note: str) -> None:
        self.status.emit(f"[dsh] {note}")
        if online and self.cfg.events.balance_sec > 0 and not self._balance_timer.isActive():
            self._balance_timer.start(max(60, self.cfg.events.balance_sec) * 1000)
        if online and self.cfg.events.whisper_sec > 0 and not self._whisper_timer.isActive():
            self._whisper_timer.start(max(30, self.cfg.events.whisper_sec) * 1000)

    def _on_say(self, pet_id: str, text: str, image: str, duration: int) -> None:
        if not text and not image:
            bubble = self._bubble(pet_id)
            if bubble is not None:
                bubble.clear()
            return
        self.say(pet_id, text, image=image or None, duration_ms=duration or 10_000)

    def _on_anim(self, pet_id: str, name: str) -> None:
        win = self._win(pet_id)
        if win is not None:
            win.play_named(name)

    def on_work_status(self, snapshot: dict[str, Any] | None) -> None:
        if not self.cfg.events.work_status:
            return
        if not isinstance(snapshot, dict):
            return
        stage = str(snapshot.get("state") or "")
        if stage not in WORK_STAGES:
            return
        index = WORK_STAGES.index(stage)
        win = self._win(None)
        if win is None:
            return
        anim = WORK_ANIMS[index]
        if self.anims.path_of(anim) is not None:
            win.play_named(anim)
        texts = self.cfg.events.work_status_texts
        if 0 <= index < len(texts) and texts[index]:
            pet_id = self._pet_arg(None)
            task = str(snapshot.get("task") or "").strip()
            line = random.choice(texts[index])
            self.say(pet_id, f"{line}\n{_truncate(task)}" if task else line, duration_ms=6000)

    def on_balance(self, state: dict[str, Any] | None) -> None:
        if not self.cfg.events.balance:
            return
        if not isinstance(state, dict):
            return
        win = self._win(None)
        if win is None:
            return
        pct = balance_percent(state)
        if pct is not None:
            index = balance_event_index(pct)
            anim = BALANCE_ANIMS[index]
            if self.anims.path_of(anim) is not None:
                win.play_named(anim)
        self.say(self._pet_arg(None), balance_text(state), duration_ms=8000, whisper=False)

    def on_notify(self, frame: dict[str, Any]) -> None:
        if not self.cfg.events.notify:
            return
        toast = frame_to_toast(frame)
        if toast is None:
            return
        title, body, icon = toast
        text = title if not body else f"{title}\n{_truncate(body)}"
        icon_file = NOTIFY_ICONS.get(icon)
        path = self.assets / "pic" / icon_file if icon_file else None
        self.say(self._pet_arg(None), text, icon=path, duration_ms=8000)

    def on_locked(self, pet_id: str, locked: bool) -> None:
        win = self._win(pet_id)
        if win is None:
            return
        if locked:
            self._locked.add(pet_id)
        else:
            self._locked.discard(pet_id)
        win.set_external_lock(locked)
        self.status.emit(f"[dsh] {pet_id} {'被第三方占用，暂时静止' if locked else '解除占用'}")

    def refresh_balance(self) -> None:
        if self.bridge is not None and self.dsh_online:
            self.bridge.refresh_balance()

    def _auto_whisper(self) -> None:
        if self.whisper_ready:
            self.whisper(None, silent=True)

    # ---------------- 位置上报 ----------------

    def report_pose(self) -> None:
        """把每只宠物的位置/朝向报给 DSH（网页端与第三方据此知道桌面宠物在哪）。"""
        if self.bridge is None or not self.dsh_online:
            return
        from PySide6.QtGui import QGuiApplication

        for pet_id, win in self._windows.items():
            center = win.frameGeometry().center()
            screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
            if screen is None:
                continue
            area = screen.geometry()
            if area.width() <= 0 or area.height() <= 0:
                continue
            self.bridge.set_pose(
                pet_id,
                (center.x() - area.left()) / area.width(),
                (center.y() - area.top()) / area.height(),
                getattr(win, "facing", "left"),
            )


class _Signals(QObject):
    """跨线程回主线程的小信道（AI 请求在工作线程里跑）。"""

    done = Signal(str, str, str)
