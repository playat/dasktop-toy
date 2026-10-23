"""dsh-pet Python 桌面端 —— M3/M4：菜单 + 托盘 + 多开。

用法（在 pet_py 目录下）：
    run-pet.cmd                         双击或命令行都行
    .venv\\Scripts\\python.exe main.py    显式用自带 venv

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

import signal
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QAction, QIcon
    from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon
except ModuleNotFoundError:
    sys.stderr.write(
        "\n[错误] 当前 Python 解释器没有装 PySide6：\n"
        f"    {sys.executable}\n\n"
        "  依赖装在 pet_py/.venv 里，用下面任一方式启动：\n"
        "    run-pet.cmd                                   （双击也行）\n"
        "    .venv\\Scripts\\python.exe main.py\n\n"
        "  venv 没建过就先跑 setup-pet.cmd；\n"
        "  想在自己的 Python 里装：pip install -r requirements.txt\n\n"
    )
    raise SystemExit(1)

from config import PetConfig, PetSpec  # noqa: E402
from menu import build_menu  # noqa: E402
from player import AnimationPlayer, AnimationSet  # noqa: E402
from tray import PetTray, icon_from_webm  # noqa: E402
from window import FEET_Y_RATIO, PetWindow  # noqa: E402

ASSETS = HERE / "assets"
CONFIG_FILE = HERE / "config.jsonc"

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
        self.anims = AnimationSet(ASSETS / "webm", self.cfg)
        self.windows: list[PetWindow] = []
        self.tray: PetTray | None = None
        self._hidden = False

    # ---------- 装配 ----------

    def start(self) -> None:
        for i, spec in enumerate(self.cfg.pets):
            self.spawn(spec, slot=i)
        self._setup_tray()

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
        self.windows.append(win)
        return win

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[tray] 系统托盘不可用，跳过", flush=True)
            return

        idle_pool = self.cfg.pools.idle or []
        icon_path = ASSETS / "webm" / f"{idle_pool[0]}.webm" if idle_pool else None
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

    # ---------- 菜单 ----------

    def _area_for(self, win: PetWindow):
        """宠物当前所在那块屏的可视区（多屏时「回到初始位置」也回自己那块）。"""
        screen = self.app.screenAt(win.frameGeometry().center()) or self.app.primaryScreen()
        return screen.availableGeometry() if screen is not None else self.area

    def _show_menu(self, win: PetWindow, global_pos) -> None:
        spawn_act = QAction("再来一只", win)
        spawn_act.triggered.connect(lambda _checked=False: self.spawn())

        menu = build_menu(
            None,
            self.cfg,
            self.anims,
            size=win.width(),
            fixed=win.fixed,
            on_play=win.play_named,
            on_size=win.apply_size,
            on_fixed=win.set_fixed,
            on_home=win.reset_home,
            on_reload=self.reload,
            on_quit=self.quit,
            extra=[spawn_act],
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

        for win in self.windows:
            win.shutdown()
            win.close()
        self.windows.clear()

        self.cfg = cfg
        self.anims = AnimationSet(ASSETS / "webm", cfg)
        for i, spec in enumerate(cfg.pets):
            self.spawn(spec, slot=i)
        if self._hidden:
            for win in self.windows:
                win.setVisible(False)
        print(f"[pet] 配置已重载 · {len(self.windows)} 只", flush=True)

    def quit(self) -> None:
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
    print(
        f"[pet] 配置 {CONFIG_FILE.name} · 待机池 {len(controller.cfg.pools.idle)} · "
        f"点击池 {len(controller.cfg.pools.clicks)} · 转向池 {len(controller.cfg.pools.turn)} · "
        f"走位池 {len(controller.cfg.pools.moves)} · 分类 {len(controller.cfg.pools.categories)} · "
        f"宠物 {len(controller.windows)} 只",
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
