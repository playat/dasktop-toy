"""右侧输入框：右键「说句话…」时在宠物**旁边**弹出的一条输入框。

外观跟头顶那个说话气泡是同一套比例（`bubble.py` 的白色半透明圆角 + 深灰字 + 指向宠物的小尾巴），
只是尾巴改到左侧/右侧、里面换成一个无边框 `QLineEdit`：

    回车发送 · Esc 取消 · 点到别处（焦点跑到本程序别的窗口）自动收

两个和气泡不一样的地方：

1. 气泡是 `WindowTransparentForInput`（不吃鼠标），这里**必须吃鼠标和键盘**，
   否则输不进去；所以打开时要把窗口激活、把焦点塞给输入框。
2. 气泡只贴头顶，这个要贴**侧面**：优先宠物右边（宠物在屏幕右边缘时自动翻到左边，尾巴跟着换边）。

位置不自己算：`main`/`EventDirector` 把它挂进 `window.followers`，宠物每帧把 `geometry()`
喂过来（拖拽、抛掷时也跟得上），和计分徽章用的是同一套机制。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

# 比例全部锚字号 f（跟 bubble.py 同一套观感）
_PAD_X = 0.78          # 左右内边距
_PAD_Y = 0.40          # 上下内边距
_RADIUS = 0.77         # 圆角（气泡同款）
_TAIL = 0.42           # 尾巴长度
_BODY_W = 12.5         # 输入条宽度 ×f
_LINE_H = 1.55         # 输入行高度 ×f
_GAP = 5               # 和宠物之间的缝
_MARGIN = 4            # 给投影留的边

BG = QColor(255, 255, 255, 243)
EDGE = QColor(0, 0, 0, 30)
FG = QColor(43, 43, 43)
HINT = QColor(152, 152, 152)


class SayBox(QWidget):
    """一只宠物一个（多开时各说各的；同一时刻只开一个）。"""

    submitted = Signal(str)   # 回车：把文字交出去
    dismissed = Signal()      # 收起（回车/Esc/失焦），宿主据此摘掉跟随回调

    def __init__(self) -> None:
        super().__init__(
            None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setWindowTitle("对桌宠说")

        self._font_family = "Microsoft YaHei UI"
        self._font_px = 20
        self._anchor = QRect()
        self._body = QRect()
        self._tail_left = True
        self._tail = 8
        self._radius = 14
        self._pad_x = 15
        self._pad_y = 8
        self._body_w = 240
        self._body_h = 42
        self._hint = "对桌宠说点什么…"

        self._edit = QLineEdit(self)
        self._edit.setFrame(False)
        self._edit.setStyleSheet(
            "QLineEdit {"
            "  background: transparent;"
            "  border: none;"
            "  padding: 0;"
            f" color: rgb({FG.red()},{FG.green()},{FG.blue()});"
            "  selection-background-color: #cfe4ff;"
            "  selection-color: #101010;"
            "}"
        )
        self._edit.installEventFilter(self)
        self._edit.returnPressed.connect(self._submit)
        self._edit.textChanged.connect(self.update)  # 打字后把灰色提示语擦掉

        self._relayout()
        self.hide()

    # ---------- 外观 / 布局 ----------

    def configure(self, *, pet_size: int, family: str, size: int, follow: bool) -> None:
        """字号跟宠物宽度走（和气泡同一套：`--f0 = size*0.0455`）。"""
        family = family or "Microsoft YaHei UI"
        if size > 0:
            px = round(size * pet_size / 462) if follow else size
        else:
            px = round(pet_size * 0.0455)
        self._font_px = max(12, min(px, 64))
        self._font_family = family
        self._relayout()
        if self.isVisible():
            self._place()

    def _relayout(self) -> None:
        f = self._font_px
        self._pad_x = round(f * _PAD_X)
        self._pad_y = round(f * _PAD_Y)
        self._tail = max(6, round(f * _TAIL))
        self._body_w = round(f * _BODY_W)
        self._body_h = round(f * _LINE_H) + self._pad_y * 2
        self._radius = max(6, round(f * _RADIUS))

        font = QFont(self._font_family)
        font.setPixelSize(max(10, round(f * 0.98)))
        self._edit.setFont(font)
        pal = self._edit.palette()
        pal.setColor(QPalette.Text, FG)
        self._edit.setPalette(pal)

    # ---------- 打开 / 跟随 / 收起 ----------

    def open_for(self, anchor: QRect, *, placeholder: str = "对桌宠说点什么…") -> None:
        self._anchor = QRect(anchor)
        self._hint = placeholder or ""
        self._place()
        self.show()
        self.raise_()
        self.activateWindow()
        self._edit.setFocus(Qt.PopupFocusReason)

    def follow(self, anchor: QRect) -> None:
        """宠物每帧把 geometry 喂过来（挂进 `window.followers`）。"""
        if anchor == self._anchor:
            return
        self._anchor = QRect(anchor)
        if self.isVisible():
            self._place()

    def close_box(self) -> None:
        if not self.isVisible():
            return
        self.hide()
        self._edit.clear()
        self.dismissed.emit()

    @property
    def text(self) -> str:
        return self._edit.text()

    # ---------- 贴位 ----------

    def _place(self) -> None:
        """优先贴宠物右边；右边放不下就整条翻到左边，尾巴跟着换到另一侧。"""
        a = self._anchor
        if a.isNull():
            return
        m = _MARGIN
        total_w = m * 2 + self._tail + self._body_w
        total_h = m * 2 + self._body_h

        # 竖直：和气泡的「头顶线」对齐（气泡底落在这一条线上，这里则居中对齐）
        head_y = a.top() + round(a.width() * 0.108)

        screen = QGuiApplication.screenAt(a.center()) or QGuiApplication.primaryScreen()
        area = (
            screen.availableGeometry()
            if screen is not None
            else QRect(a.left(), a.top(), 1280, 720)
        )

        if a.right() + _GAP + total_w <= area.right() - 2:
            tail_left, x = True, a.right() + _GAP
        else:
            tail_left, x = False, a.left() - _GAP - total_w
        if x < area.left() + 2:  # 两侧都塞不下（窄屏）：退回来贴边
            tail_left, x = True, a.right() + _GAP
        x = max(area.left() + 2, min(x, area.right() - total_w - 2))
        y = max(area.top() + 2, min(head_y - total_h // 2, area.bottom() - total_h - 2))

        self._tail_left = tail_left
        self._body = QRect(m + (self._tail if tail_left else 0), m, self._body_w, self._body_h)
        if (total_w, total_h) != (self.width(), self.height()):
            self.resize(total_w, total_h)
        self._edit.setGeometry(
            self._body.x() + self._pad_x,
            self._body.y() + self._pad_y,
            self._body_w - self._pad_x * 2,
            self._body_h - self._pad_y * 2,
        )
        self.move(int(x), int(y))
        self.update()

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform
        )

        body = QRectF(self._body)
        radius = float(self._radius)
        t = float(self._tail)
        cy = body.center().y()

        tri = QPolygonF()
        if self._tail_left:
            tri.append(QPointF(body.left(), cy - t * 0.78))
            tri.append(QPointF(body.left() - t, cy))
            tri.append(QPointF(body.left(), cy + t * 0.78))
        else:
            tri.append(QPointF(body.right(), cy - t * 0.78))
            tri.append(QPointF(body.right() + t, cy))
            tri.append(QPointF(body.right(), cy + t * 0.78))

        card = QPainterPath()
        card.addRoundedRect(body, radius, radius)
        tail = QPainterPath()
        tail.addPolygon(tri)
        shape = card.united(tail)

        # 极淡的投影：往右下叠三层，浅色桌面上也有点"厚度"（气泡没有，这里是为了像输入框）
        painter.setPen(Qt.NoPen)
        for offset, alpha in ((3.0, 10), (2.0, 16), (1.0, 22)):
            painter.save()
            painter.translate(0.0, offset)
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawPath(shape)
            painter.restore()

        painter.setBrush(BG)
        painter.setPen(QPen(EDGE, 1.0))
        painter.drawPath(shape)

        # 占位提示自己画：走样式表时 QPalette 的 PlaceholderText 会被 sheet 的 color 吃掉，
        # 提示语就跟正文一个色了（黑乎乎一片）。这里直接用浅灰画，空白时才出现。
        if self._hint and not self._edit.text():
            painter.setPen(HINT)
            painter.setFont(self._edit.font())
            painter.drawText(
                self._edit.geometry().adjusted(1, 0, 0, 0),
                int(Qt.AlignLeft | Qt.AlignVCenter),
                self._hint,
            )
        painter.end()

    # ---------- 交互 ----------

    def _submit(self) -> None:
        text = self._edit.text().strip()
        if text:
            self.hide()
            self._edit.clear()
            self.submitted.emit(text)
            self.dismissed.emit()
        else:
            self.close_box()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._edit and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.close_box()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close_box()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        # 焦点跑到本程序别的窗口（比如点了宠物）就收起来；整个程序被切走（Alt+Tab）
        # 就先留着，回来还能接着写完
        QTimer.singleShot(120, self._dismiss_if_focus_left)

    def _dismiss_if_focus_left(self) -> None:
        if not self.isVisible():
            return
        if QApplication.activeWindow() is None:
            return  # 切到别的程序去了，别把写了一半的字丢掉
        if not self.isActiveWindow():
            self.close_box()
