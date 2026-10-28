"""dsh-pet Python 桌面端 —— M3/M4：菜单 + 托盘 + 多开。

用法（在工作区根目录）：
    run-pet.cmd                          双击/命令行都行，会先自检 .venv 与依赖
    .venv\\Scripts\\python.exe main.py     直接用项目自己的解释器跑
    python main.py                       自己的环境里装齐依赖也能跑

能力：透明窗口 + VP9-alpha 播放 + 动画链状态机（待机/随机动作/转向/走位）
      + 点击回应 + 拖拽 + 抛掷物理（重力/反弹/落地挤压）
      + 右键菜单（动作点播 / 大小 / 固定原地 / 回到初始位置 / 重载配置）
      + 系统托盘 + 多开（配置 pets[] 写几只就是几只，托盘也能「再来一只」）。
退出：Ctrl+C、托盘退出、右键菜单退出。

注意 1：窗口是 WS_EX_LAYERED 分层窗口，不少截图工具（PIL ImageGrab 的 BitBlt 路径）
        抓不到它的内容，验证时以肉眼为准，别拿截图当证据。
注意 2：坐标统一走 Qt 的逻辑像素（availableGeometry / move）。混用 Win32 的物理像素
        会在 150% 缩放下把窗口推到屏幕外——看起来像"宠物没启动"。
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import Any


def _app_dir() -> Path:
    """可写目录：打包后是 exe 所在目录，源码模式是脚本目录。

    配置、素材、日志都相对这里找 —— 所以换素材/改配置不需要重新编译。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


HERE = _app_dir()
sys.path.insert(0, str(HERE))

# PyInstaller --windowed 没有控制台，此时 sys.stdout/stderr 是 None，
# 而本文件到处都是 print(..., flush=True) —— 不兜住会直接 AttributeError。
# 打包版统一写 exe 同级的 dsh-pet.log，出问题能查。
if sys.stdout is None or sys.stderr is None:
    try:
        _stream = open(HERE / "dsh-pet.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        _stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _stream
    sys.stderr = sys.stderr or _stream

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QAction, QIcon
    from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon
except ModuleNotFoundError:
    sys.stderr.write(
        "\n[错误] 当前 Python 解释器没有装 PySide6：\n"
        f"    {sys.executable}\n\n"
        "  依赖装在项目自己的 .venv 里，用下面任一方式启动：\n"
        "    run-pet.cmd                                   （双击也行，会先自检环境）\n"
        "    .venv\\Scripts\\python.exe main.py\n\n"
        "  .venv 还没建过的话（在本目录执行）：\n"
        "    python -m venv .venv\n"
        "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n\n"
    )
    raise SystemExit(1)

from config import PetConfig, PetSpec  # noqa: E402
from events import EventDirector  # noqa: E402
from menu import build_menu  # noqa: E402
from player import AnimationPlayer, AnimationSet  # noqa: E402
from tray import PetTray, icon_from_webm  # noqa: E402
from window import FEET_Y_RATIO, PetWindow  # noqa: E402


def _assets_dir() -> Path:
    """素材目录：`assets/`（exe 或工程根旁边的那份）。

    两种运行形态用的是同一句判断：
      - 打包后：exe 平铺在 `dist/`，`dist/assets/` 就在旁边 → 命中第一个候选
      - 源码模式：工程根 `assets/`（项目文件、进 git）→ 命中第一个候选
    第二个候选只是兜底：源码模式下 `dist/assets/` 也常常有素材（打完包留下的），
    根目录那份被清掉时不至于起不来。

    判定顺序：`<app>/assets`（有 webm 才算）→ `<app>/dist/assets`（有 webm 才算）。
    """
    here = _app_dir()
    candidates = [here / "assets", here / "dist" / "assets"]
    for cand in candidates:
        try:
            if cand.is_dir() and next(cand.rglob("*.webm"), None) is not None:
                return cand
        except OSError:
            continue
    # 一个成品都还没有：给工程素材目录，菜单会提示「没有素材」
    return candidates[0]


ASSETS = _assets_dir()
CONFIG_FILE = HERE / "config.jsonc"


def load_font_family(assets: Path, cfg: PetConfig) -> str:
    """把 `assets/fonts/*.ttf` 注册进 Qt，返回气泡实际该用的字体族名。

    上游是网页里 `@font-face` 声明一个 CSS 名（ShangshouSoftCandy）；桌面端要走
    QFontDatabase 注册字体文件，再问它真正的族名 —— 配置里写 CSS 名是认不到的。
    """
    from PySide6.QtGui import QFontDatabase

    folder = assets / "fonts"
    families: list[str] = []
    if folder.is_dir():
        for font_file in sorted(folder.iterdir()):
            if font_file.suffix.lower() not in (".ttf", ".otf"):
                continue
            fid = QFontDatabase.addApplicationFont(str(font_file))
            if fid >= 0:
                families.extend(QFontDatabase.applicationFontFamilies(fid))
    if not families:
        return cfg.font.family or "Microsoft YaHei UI"
    if cfg.font.family in families:
        return cfg.font.family
    if cfg.font.family in ("", "ShangshouSoftCandy", "candy"):
        return families[0]
    return cfg.font.family

CANVAS_W, CANVAS_H = 640, 360

# 素材原生分辨率就是 640×360，把窗口开得比它还大属于纯放大（只会更糊）。
MIN_SIZE, MAX_SIZE = 160, CANVAS_W

# 多开时相邻两只之间的间隙
PET_GAP = 16


def clamp_size(size: float) -> int:
    return max(MIN_SIZE, min(int(round(size)), MAX_SIZE))


def window_size(size: float) -> tuple[int, int]:
    """size 的语义与 Electron 版一致：宠物本体（整块 16:9 画面）的宽度。

    高度按 16:9 推导，所以缩放是等比的，窗口内所有几何比例常量都不用改。
    """
    width = clamp_size(size)
    return width, int(round(width * CANVAS_H / CANVAS_W))


def anchor_position(
    position: dict[str, Any], width: int, height: int, area: Any, slot: int = 0
) -> tuple[int, int]:
    """按 position 的角落 + 边距换算窗口左上角（全部用 Qt 逻辑像素）。

    bottom-* 的 marginY 以「脚底线」为基准，所以宠物是站在屏幕底边上方 marginY 处，
    而不是包围盒底边（包围盒脚底以下还有一条透明留白）。
    slot 用于多开：从指定角落向外依次排开，避免几只叠在一起。
    """
    corner = str(position.get("corner") or "bottom-right")
    margin_x = int(position.get("marginX") or 48)
    margin_y = int(position.get("marginY") or 24)
    step = slot * (width + PET_GAP)

    if corner.endswith("left"):
        x = area.left() + margin_x + step
    else:
        x = area.left() + area.width() - width - margin_x - step

    if corner.startswith("top"):
        y = area.top() + margin_y
    else:
        y = area.top() + area.height() - height * FEET_Y_RATIO - margin_y

    # 兜底：保证整块窗口和主屏有交集（多开排到屏幕外时至少还露出一部分）
    x = max(area.left() - width // 3, min(x, area.left() + area.width() - width * 2 // 3))
    y = max(area.top(), min(y, area.top() + area.height() - height // 2))
    return int(x), int(y)


class PetApp:
    """窗口集合 + 托盘 + 菜单的装配层。"""

    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.area = app.primaryScreen().availableGeometry()
        self.cfg = PetConfig.load(CONFIG_FILE)
        self.cfg.font.family = load_font_family(ASSETS, self.cfg)
        self.anims = AnimationSet(ASSETS / "webm", self.cfg)
        self.events = EventDirector(self.cfg, ASSETS, self.anims)
        self.events.status.connect(lambda line: print(line, flush=True))
        self.windows: list[PetWindow] = []
        self.tray: PetTray | None = None
        self._hidden = False

        # 位置上报（只有连上 DSH 才有意义，events 内部自己判）
        self._pose_timer = QTimer()
        self._pose_timer.setInterval(2000)
        self._pose_timer.timeout.connect(self.events.report_pose)

    # ---------- 装配 ----------

    def start(self) -> None:
        for i, spec in enumerate(self.cfg.pets):
            self.spawn(spec, slot=i)
        self._setup_tray()
        self.events.start()
        self._pose_timer.start()

    def spawn(self, spec: PetSpec | None = None, slot: int | None = None) -> PetWindow:
        spec = spec or self.cfg.pets[0]
        slot = len(self.windows) if slot is None else slot

        width, height = window_size(spec.size)
        player = AnimationPlayer()
        win = PetWindow(
            player, self.anims, self.cfg, width, height, self.area, fixed=spec.fixed
        )
        win.home_provider = (
            lambda w, h, p=dict(spec.position), s=slot, ref=win: anchor_position(
                p, w, h, self._area_for(ref), s
            )
        )
        win.on_context_menu = lambda pos, w=win: self._show_menu(w, pos)
        win.peers_provider = lambda ref=win: self.windows

        x, y = anchor_position(spec.position, width, height, self.area, slot)
        win.move(x, y)
        win.show()
        win.start()
        win.pet_id = spec.id
        self.events.attach(spec.id, win)
        self._decorate(win)
        self.windows.append(win)
        self._refresh_tray_tip()
        return win

    def _decorate(self, win: PetWindow) -> None:
        """点击音效 + 拖拽光标（素材缺就自动跳过）。"""
        if self.cfg.events.click_sound:
            win.set_sound(ASSETS / "sound" / "click-duck.wav")
        win.set_cursors(
            ASSETS / "pic" / "cursor-grab.png", ASSETS / "pic" / "cursor-grabbing.png"
        )

    def _refresh_tray_tip(self) -> None:
        """托盘悬停提示：显示开了几只、都叫什么（pets[].name 的唯一用处）。"""
        if self.tray is None:
            return
        names = [spec.name or spec.id for spec in self.cfg.pets]
        tip = f"dsh-pet 桌宠 · {len(self.windows)} 只"
        if names:
            tip += "\n" + "、".join(names)
        self.tray.setToolTip(tip)

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[tray] 系统托盘不可用，跳过", flush=True)
            return

        idle_keys = self.anims.role_keys("idle")
        icon_path = self.anims.path_of(idle_keys[0]) if idle_keys else None
        icon = icon_from_webm(icon_path) if icon_path and icon_path.exists() else QIcon()
        if icon.isNull():
            icon = self.app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)

        self.tray = PetTray(
            self.app,
            icon,
            on_toggle=self.set_hidden,
            on_spawn=self.spawn,
            on_home=self.reset_all_home,
            on_reload=self.reload,
            on_quit=self.quit,
        )
        self.tray.show()
        self._refresh_tray_tip()

    # ---------- 菜单 ----------

    def _area_for(self, win: PetWindow):
        """宠物当前所在那块屏的可视区（多屏时「回到初始位置」也回自己那块）。"""
        screen = self.app.screenAt(win.frameGeometry().center()) or self.app.primaryScreen()
        return screen.availableGeometry() if screen is not None else self.area

    def _show_menu(self, win: PetWindow, global_pos) -> None:
        spawn_act = QAction("再来一只", win)
        spawn_act.triggered.connect(lambda _checked=False: self.spawn())

        pet_id = getattr(win, "pet_id", None)
        # 根菜单上的直接入口：碎碎念 / 说句话… / 刷新余额
        extra = [*self.events.menu_actions(pet_id), spawn_act]

        # 「动作」子菜单里多一块表情包（一张图都没有时返回 None，不占位置）
        meme_menu = self.events.meme_menu(pet_id)
        action_extra = [meme_menu] if meme_menu is not None else []

        menu = build_menu(
            None,
            self.anims,
            size=win.width(),
            fixed=win.fixed,
            on_play=win.play_named,
            on_size=win.apply_size,
            on_fixed=win.set_fixed,
            on_home=win.reset_home,
            on_reload=self.reload,
            on_quit=self.quit,
            extra=extra,
            action_extra=action_extra,
        )
        menu.exec(global_pos)

    # ---------- 全局操作 ----------

    def set_hidden(self, hidden: bool) -> None:
        self._hidden = hidden
        for win in self.windows:
            win.setVisible(not hidden)
        print(f"[pet] 宠物已{'隐藏' if hidden else '显示'}", flush=True)

    def reset_all_home(self) -> None:
        for win in self.windows:
            win.reset_home()

    def reload(self) -> None:
        try:
            cfg = PetConfig.load(CONFIG_FILE)
        except Exception as exc:  # noqa: BLE001
            print(f"[pet] 重载失败：{exc}", flush=True)
            return

        self.events.stop()
        for win in self.windows:
            win.shutdown()
            win.close()
        self.windows.clear()

        self.cfg = cfg
        self.cfg.font.family = load_font_family(ASSETS, cfg)
        self.anims = AnimationSet(ASSETS / "webm", cfg)
        self.events = EventDirector(self.cfg, ASSETS, self.anims)
        self.events.status.connect(lambda line: print(line, flush=True))
        for i, spec in enumerate(cfg.pets):
            self.spawn(spec, slot=i)
        self.events.start()
        if self._hidden:
            for win in self.windows:
                win.setVisible(False)
        print(f"[pet] 配置已重载 · {len(self.windows)} 只", flush=True)

    def quit(self) -> None:
        self._pose_timer.stop()
        self.events.stop()
        for win in self.windows:
            win.shutdown()
            win.close()
        self.windows.clear()
        if self.tray is not None:
            self.tray.hide()
        self.app.quit()


def main() -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("dsh-pet")
    app.setQuitOnLastWindowClosed(False)  # 托盘「隐藏全部」不算退出

    controller = PetApp(app)
    controller.start()

    # ---- Ctrl+C：Qt 事件循环不跑 Python 字节码，信号处理函数排不上队。
    # 用一个空转的 QTimer 定期把控制权交回解释器，Ctrl+C 才能真正生效。
    def on_signal(_signum, _frame) -> None:
        print("\n[pet] 收到中断，正在退出…", flush=True)
        controller.quit()

    signal.signal(signal.SIGINT, on_signal)
    try:
        signal.signal(signal.SIGTERM, on_signal)
    except (AttributeError, ValueError):  # 部分平台没有 SIGTERM
        pass
    signal_tick = QTimer()
    signal_tick.start(200)
    signal_tick.timeout.connect(lambda: None)

    first = controller.windows[0] if controller.windows else None
    a = controller.anims
    print(
        f"[pet] 配置 {CONFIG_FILE.name} · 待机 {len(a.role_keys('idle'))} · "
        f"转向 {len(a.role_keys('turn'))} · 拖拽 {len(a.role_keys('drag'))} · "
        f"走位 {len(a.role_keys('move'))} · 点击 {len(a.role_keys('click'))} · "
        f"随机 {len(a.random_keys)} · 宠物 {len(controller.windows)} 只",
        flush=True,
    )
    if first is not None:
        print(
            f"[pet] 窗口 {first.width()}x{first.height()}（config size={controller.cfg.size}） · "
            f"{first.geometry()} · 屏幕 {controller.area.width()}x{controller.area.height()} "
            f"@ {app.primaryScreen().devicePixelRatio():g}x",
            flush=True,
        )
    print("[pet] 左键拖动/甩飞 · 单击有回应 · 右键菜单 · 托盘 · Ctrl+C 退出", flush=True)

    try:
        return app.exec()
    finally:
        controller.quit()


if __name__ == "__main__":
    raise SystemExit(main())
