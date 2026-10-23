"""系统托盘（M4）：图标直接从素材首帧裁出来，不额外维护图片文件。

托盘菜单：
    显示宠物 / 隐藏宠物（勾选）
    再来一只
    全部回到初始位置
    重载配置
    退出
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import av
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from menu import MENU_QSS
from video import ALPHA_NOISE_FLOOR


def first_frame_rgba(path: str | Path) -> tuple[np.ndarray, int, int] | None:
    """解出 webm 的第一帧 RGBA（必须用 libvpx-vp9，否则 alpha 全 255）。"""
    try:
        container = av.open(str(path))
        stream = container.streams.video[0]
        ctx = stream.codec_context
        width, height = ctx.width, ctx.height
        extradata = bytes(ctx.extradata or b"")

        decoder = av.CodecContext.create("libvpx-vp9", "r")
        decoder.width, decoder.height = width, height
        decoder.pix_fmt = "yuva420p"
        decoder.extradata = extradata
        decoder.open()

        for packet in container.demux(stream):
            if not packet.size:
                continue
            for frame in decoder.decode(packet):
                rgba = frame.to_ndarray(format="rgba")
                container.close()
                return rgba, width, height
        container.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[tray] 取首帧失败: {exc}", flush=True)
    return None


def icon_from_webm(path: str | Path, size: int = 64) -> QIcon:
    """素材首帧 → 裁掉透明边 → 缩放成正方形图标。"""
    result = first_frame_rgba(path)
    if result is None:
        return QIcon()

    rgba, width, height = result

    # 背景不是精确的 alpha=0，而是 1~15 的编码噪声（跟播放线程同样的处理）。
    # 不先清掉，下面的「裁透明边」会退化成整帧，图标里的宠物会小成一个点。
    if not rgba.flags.writeable:
        rgba = rgba.copy()
    alpha = rgba[:, :, 3]
    alpha[alpha < ALPHA_NOISE_FLOOR] = 0

    rows = np.where(alpha.max(axis=1) > 0)[0]
    cols = np.where(alpha.max(axis=0) > 0)[0]
    if rows.size and cols.size:
        top, bottom = int(rows[0]), int(rows[-1]) + 1
        left, right = int(cols[0]), int(cols[-1]) + 1
        rgba = rgba[top:bottom, left:right]
        height, width = rgba.shape[:2]

    rgba = np.ascontiguousarray(rgba)
    image = QImage(rgba.data, width, height, rgba.strides[0], QImage.Format_RGBA8888).copy()
    pixmap = QPixmap.fromImage(image).scaled(
        size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
    )
    return QIcon(pixmap)


class PetTray(QSystemTrayIcon):
    def __init__(
        self,
        parent,
        icon: QIcon,
        *,
        on_toggle: Callable[[bool], None],
        on_spawn: Callable[[], None],
        on_home: Callable[[], None],
        on_reload: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        super().__init__(icon, parent)
        self.setToolTip("dsh-pet 桌宠")

        menu = QMenu()
        menu.setStyleSheet(MENU_QSS)

        self.show_act = menu.addAction("显示宠物")
        self.show_act.setCheckable(True)
        self.show_act.setChecked(True)
        self.show_act.triggered.connect(on_toggle)

        spawn_act = menu.addAction("再来一只")
        spawn_act.triggered.connect(lambda _checked=False: on_spawn())

        menu.addSeparator()
        home_act = menu.addAction("全部回到初始位置")
        home_act.triggered.connect(lambda _checked=False: on_home())

        reload_act = menu.addAction("重载配置")
        reload_act.triggered.connect(lambda _checked=False: on_reload())

        menu.addSeparator()
        quit_act = menu.addAction("退出")
        quit_act.triggered.connect(lambda _checked=False: on_quit())

        self._menu = menu
        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason) -> None:
        # 左键点托盘 = 切换显示（Windows 上双击更容易触发，两个都接）
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_act.setChecked(not self.show_act.isChecked())
            self.show_act.triggered.emit(self.show_act.isChecked())
