"""说话气泡：宠物头上那个白底圆角小气泡（原 DSH 版 client/bubble.ts 的 PySide 版）。

独立顶层窗口（宠物窗口是分层的，子控件会被裁在窗口矩形内，气泡要探出头顶就必须自己开窗）：

    Qt.Tool | FramelessWindowHint | WindowStaysOnTopHint | WindowTransparentForInput
    WA_TranslucentBackground

`WindowTransparentForInput` 保证「气泡不吃鼠标」——点气泡等价于点穿到下面的东西，
跟原版 `pointer-events:none` 一致。气泡自己不做定位决策，只负责：
  · 按字号算尺寸（圆角/内边距/尾巴/最大宽度全部锚字号，与原版 CSS 同一套比例）
  · 把「文字 + 可选配图」画出来
  · 跟着宠物窗口走（anchor 变化就重新贴到头顶），到点自动收
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QWidget

# 原版 CSS 的比例（单位都是「字号 f」）
_PAD_H = 0.66          # padding 左右
_PAD_V = 0.48          # padding 上下
_RADIUS = 0.77         # border-radius
_TAIL = 0.37           # 尾巴高度
_MAX_W = 16.0          # 普通气泡最大宽度
_MAX_W_WHISPER = 11.0  # 碎碎念变体最大宽度
_WHISPER_SCALE = 0.75  # 碎碎念字号缩放
_IMG_MAX = 11.0        # 配图最大宽度（×f）
_ICON = 1.7            # 通知小图标边长（×f）
_GAP = 0.30            # 图文之间

BG = QColor(255, 255, 255, 235)
FG = QColor(43, 43, 43)


class SpeechBubble(QWidget):
    """一只宠物一个气泡窗口（多开时各说各的，互不覆盖）。"""

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)

        self._font_family = "Microsoft YaHei UI"
        self._font_px = 21
        self._text = ""
        self._image: QPixmap | None = None
        self._icon: QPixmap | None = None
        self._image_scaled: QPixmap | None = None
        self._icon_scaled: QPixmap | None = None
        self._anchor = QRect()
        self._body = QRect()
        self._text_rect = QRect()
        self._img_pos = QPointF(0, 0)
        self._icon_pos = QPointF(0, 0)
        self._whisper = True

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    # ---------- 外观参数 ----------

    def configure(self, *, pet_size: int, family: str, size: int, follow: bool) -> None:
        """字号跟着宠物宽度走（原版 --f0 = size*0.0455）；配了固定字号就用固定值。"""
        family = family or "Microsoft YaHei UI"
        if size > 0:
            px = round(size * pet_size / 462) if follow else size
        else:
            px = round(pet_size * 0.0455)
        self._font_px = max(12, min(px, 64))
        self._font_family = family
        if self.isVisible():
            self._relayout()
            self._place()
            self.update()

    def _font(self, scale: float = 1.0) -> QFont:
        font = QFont(self._font_family)
        font.setPixelSize(max(10, round(self._font_px * scale)))
        return font

    @property
    def font_px(self) -> int:
        return round(self._font_px * (_WHISPER_SCALE if self._whisper else 1.0))

    # ---------- 说话 ----------

    def say(
        self,
        text: str,
        *,
        anchor: QRect,
        image: Path | str | None = None,
        icon: Path | str | None = None,
        duration_ms: int = 10_000,
        whisper: bool = True,
    ) -> bool:
        """弹一条气泡。`duration_ms<=0` = 一直显示到下次说话（或手动收起）。"""
        text = (text or "").strip()
        pix = _load(image)
        icn = _load(icon)
        if not text and pix is None and icn is None:
            return False

        self._text = text
        self._image = pix
        self._icon = icn
        self._whisper = whisper
        self._anchor = QRect(anchor)

        self._relayout()
        self._place()
        self.show()
        self.raise_()

        if duration_ms > 0:
            self._timer.start(int(duration_ms))
        else:
            self._timer.stop()
        return True

    def clear(self) -> None:
        self._timer.stop()
        self.hide()

    def follow(self, anchor: QRect) -> None:
        """宠物移动/缩放后重新贴到头顶；位置没变就什么都不做。"""
        if anchor == self._anchor and self.isVisible():
            return
        self._anchor = QRect(anchor)
        if self.isVisible():
            self._place()

    def reflow(self, anchor: QRect) -> None:
        """字号/尺寸变了：重算内容再贴位。"""
        self._anchor = QRect(anchor)
        if self.isVisible():
            self._relayout()
            self._place()
            self.update()

    # ---------- 布局 ----------

    def _relayout(self) -> None:
        scale = _WHISPER_SCALE if self._whisper else 1.0
        font = self._font(scale)
        px = font.pixelSize()
        max_text_w = round(px * (_MAX_W_WHISPER if self._whisper else _MAX_W))

        text_w = text_h = 0
        if self._text:
            fm = QFontMetrics(font)
            longest = max(
                (fm.horizontalAdvance(line) for line in self._text.split("\n")), default=0
            )
            if longest <= max_text_w:
                # 一行放得下：按自然宽度，别为了凑满最大宽把泡撑胖
                text_w = max(1, longest)
                text_h = max(px, fm.lineSpacing() * self._text.count("\n") + fm.lineSpacing())
            else:
                text_w = max_text_w
                box = fm.boundingRect(
                    QRect(0, 0, max_text_w, 10_000),
                    int(Qt.TextWordWrap) | int(Qt.AlignLeft) | int(Qt.AlignTop),
                    self._text,
                )
                text_h = max(px, box.height())

        img_w = img_h = 0
        self._image_scaled = None
        if self._image is not None:
            limit = round(px * _IMG_MAX)
            scaled = self._image.scaled(
                limit, round(limit * 1.4), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self._image_scaled = scaled
            img_w, img_h = scaled.width(), scaled.height()

        icon_sz = 0
        self._icon_scaled = None
        if self._icon is not None:
            icon_sz = round(px * _ICON)
            self._icon_scaled = self._icon.scaled(
                icon_sz, icon_sz, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )

        pad_x = round(px * _PAD_H)
        pad_y = round(px * _PAD_V)
        gap = round(px * _GAP) if (img_h or icon_sz) and text_h else 0
        inner_w = max(img_w, icon_sz, text_w)

        w = inner_w + pad_x * 2
        body_h = pad_y * 2 + img_h + icon_sz + gap + text_h
        tail = round(px * _TAIL)

        self._body = QRect(0, 0, w, body_h)
        self._text_rect = QRect(
            pad_x + (inner_w - text_w) // 2, pad_y + img_h + icon_sz + gap, text_w, text_h
        )
        self._img_pos = QPointF(pad_x + (inner_w - img_w) / 2, pad_y)
        self._icon_pos = QPointF(pad_x + (inner_w - icon_sz) / 2, pad_y + img_h)
        self._tail = tail
        self.resize(w, body_h + tail)

    def _place(self) -> None:
        """贴到宠物头顶：横居中，气泡底（含尾巴）落在宠物画面顶端那条线上。"""
        a = self._anchor
        head_y = a.top() + round(a.width() * 0.108)
        x = a.center().x() - self.width() // 2
        y = head_y - self.height()

        screen = QGuiApplication.screenAt(a.center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 2, min(x, area.right() - self.width() - 2))
            y = max(area.top() + 2, min(y, area.bottom() - self.height() - 2))
        self.move(int(x), int(y))

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._text and self._image_scaled is None and self._icon_scaled is None:
            return
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(BG)

        body = QRectF(self._body)
        painter.drawRoundedRect(body, round(self._font_px * _RADIUS), round(self._font_px * _RADIUS))

        # 底部小三角指向宠物
        tail = self._tail
        cx = body.center().x()
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx - tail * 0.8, body.bottom() - 1),
                    QPointF(cx, body.bottom() + tail - 1),
                    QPointF(cx + tail * 0.8, body.bottom() - 1),
                ]
            )
        )

        if self._image_scaled is not None:
            painter.drawPixmap(self._img_pos, self._image_scaled)
        if self._icon_scaled is not None:
            painter.drawPixmap(self._icon_pos, self._icon_scaled)
        if self._text:
            painter.setPen(FG)
            painter.setFont(self._font(_WHISPER_SCALE if self._whisper else 1.0))
            painter.drawText(
                self._text_rect,
                int(Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop),
                self._text,
            )
        painter.end()

    # ---------- 生命周期 ----------

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)


def _load(path: Path | str | None) -> QPixmap | None:
    """读图；路径不存在/读不了就当没有这张图（气泡只显示文字）。"""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    pix = QPixmap(str(p))
    return None if pix.isNull() else pix
