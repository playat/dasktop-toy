"""投篮小游戏：可拖动的篮筐 + 宠物头顶计分 + 一投的判定。

三个部件（都是独立顶层窗口，宠物窗口是分层的，塞不进子控件）：

    BasketWindow  篮筐。有 assets/pic/basket.png 就画图，没有就画蓝框占位。
                  左键拖动、右键菜单（分数 / 回到宠物旁边 / 结束）。
                  图默认「板在左、篮口朝右」，窗口拖到屏幕右半边会左右镜像让篮口朝内。
    ScoreBadge    宠物头顶常驻分数，命中时上方飘一个 +N。**不吃鼠标**。
    BasketGame    控制器：开局收尾、把窗口钩子接上、判定计分、最高分写盘。

判定（纯函数在 physics.py，好单测）：
    投掷每一帧拿「上一帧身体中心 → 当前帧身体中心」的线段跟判定圆求最短距离 d，
    归一化 u = d / R 分档给分。**必须扫掠**：一帧能飞十几像素，只看当前帧在不在
    圈里会漏判高抛球。判定点用身体中心（physics.body_box 那套 640×360 比例）。

一投 = 从甩出（`_start_throw`）到落地静止（`throw_step` 的 at_rest）。计分**不按入口那一点**：
进圈后一路记「离筐心最近多远」，等它飞出圈（或落地）再按这个最近距离结算 ——
按入口算的话空心球会被判成擦边分。一投只计一次，命中后上锁。

最高分存成 `<exe 同级>/basket-score.json`（源码模式就是工程根目录），破了才写盘。
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QRegion,
    QTransform,
)
from PySide6.QtWidgets import QMenu, QWidget

import physics
from config import BasketballConfig
from menu import MENU_QSS

INK = QColor(24, 95, 165)      # 深蓝（线条/文字）
INSIDE = QColor(55, 138, 221)  # 主蓝
HALO = QColor(133, 183, 235)   # 浅蓝（判定圈）
GOLD = QColor(186, 117, 23)    # 满分圈 / 命中波纹


def load_basket_image(value: str, app_dir: Path, assets_dir: Path) -> QPixmap | None:
    """按配置找篮筐图：绝对路径 → 相对 exe/工程目录 → 相对 assets/。都没有就 None。"""
    if not value:
        return None
    raw = Path(value)
    candidates = [raw] if raw.is_absolute() else [app_dir / raw, assets_dir / raw]
    if not raw.is_absolute() and str(value).replace("\\", "/").startswith("assets/"):
        candidates.insert(0, app_dir / raw)
    for path in candidates:
        try:
            if path.is_file():
                pix = QPixmap(str(path))
                if not pix.isNull():
                    return pix
        except OSError:
            continue
    return None


def fit_square(pix: QPixmap, side: int) -> QPixmap:
    """把图缩成 side×side 的正方形（篮筐窗口恒为正方形，判定几何全按比例算）。

    篮筐图要求是正方形，方形图直接等比缩到 side 见方；万一给了张长方图也不拉伸——
    按比例缩进方框并居中，多出来的地方补透明（宁可小一圈，也不要被拉扁）。
    """
    if pix.width() == side and pix.height() == side:
        return pix
    scaled = pix.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if scaled.width() == side and scaled.height() == side:
        return scaled
    out = QPixmap(side, side)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.drawPixmap(
        (side - scaled.width()) // 2, (side - scaled.height()) // 2, scaled
    )
    painter.end()
    return out


# ---------------------------------------------------------------- 篮筐


class BasketWindow(QWidget):
    """篮筐：左键拖着走，右键出菜单，命中时筐口扩散一圈波纹。"""

    RIPPLE_SEC = 0.45

    def __init__(
        self, cfg: BasketballConfig, image: QPixmap | None, on_menu=None
    ) -> None:
        super().__init__(
            None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._cfg = cfg
        self.on_menu = on_menu
        self._press: QPointF | None = None
        self._drag_origin = None
        self._context_pos = None
        self._ripple_t0 = -99.0

        # 窗口恒为正方形、边长 = cfg.width（图片本身是方的，缩到 width 见方即可）。
        # 万一有人换了张非方图，也不拉伸：按比例缩进方形窗口居中，四周补透明。
        if image is not None and not image.isNull():
            image = fit_square(image, cfg.width)
            self._pix: QPixmap | None = image
            # 预先翻好一份镜像：原图的篮板在左、篮口朝右，放到屏幕右半边时要反过来（篮口朝屏内）
            self._pix_flip: QPixmap | None = image.transformed(
                QTransform().scale(-1.0, 1.0)
            )
            self.resize(cfg.width, cfg.width)
        else:
            self._pix = None
            self._pix_flip = None
            self.resize(cfg.width, cfg.width)

        # 三种点选区域（只有不透明的地方吃鼠标）：
        #   _mask_plain / _mask_flip  两个朝向各自的
        #   _mask_drag                两者的并集 —— 拖动中用它，翻转时区域不变
        if self._pix is not None and self._pix_flip is not None:
            self._mask_plain = QRegion(self._pix.mask())
            self._mask_flip = QRegion(self._pix_flip.mask())
            self._mask_drag = self._mask_plain.united(self._mask_flip)
        else:
            self._mask_plain = self._mask_flip = self._mask_drag = QRegion(self.rect())

        self._flipped = False
        self._mask_now: QRegion | None = None
        self._apply_mask()

        self._ripple = QTimer(self)
        self._ripple.setInterval(16)
        self._ripple.timeout.connect(self._tick_ripple)

    # ---------- 朝向（放在屏幕右半边就镜像） ----------

    def _current_pix(self) -> QPixmap | None:
        if self._pix is None:
            return None
        return self._pix_flip if self._flipped else self._pix

    def _apply_mask(self) -> None:
        """只有「画出来的部分」吃鼠标：透明区穿透，宠物停在筐后面照样能抓。

        拖动期间改用两个朝向的并集：翻转只换画面、不动区域。否则区域一变，
        Windows 上这次鼠标抓取会断（而且新区域没盖住光标就彻底不跟手了），
        表现就是「拖过屏幕中线就卡住」。
        """
        if self._press is not None:
            region = self._mask_drag
        elif self._flipped:
            region = self._mask_flip
        else:
            region = self._mask_plain
        if region == self._mask_now:
            return
        self._mask_now = region
        self.setMask(region)

    def set_flipped(self, on: bool) -> None:
        on = bool(on)
        if on == self._flipped:
            return
        self._flipped = on
        self._apply_mask()
        self.update()

    FLIP_DEADBAND = 6  # 压在中线上时留点余量，慢慢拖过去不会来回翻

    def _auto_flip(self) -> None:
        """窗口中心落在屏幕右半边就镜像（篮口朝屏幕内侧，宠物从左边抛过来才看得见网）。"""
        app = QGuiApplication.instance()
        if app is None:
            return
        center = self.frameGeometry().center()
        screen = app.screenAt(center)
        if screen is None:
            return
        dx = center.x() - screen.availableGeometry().center().x()
        if abs(dx) <= self.FLIP_DEADBAND:
            return  # 正好压在中线：维持现状
        self.set_flipped(dx > 0)

    def is_flipped(self) -> bool:
        return self._flipped

    def showEvent(self, event) -> None:  # noqa: N802
        self._auto_flip()
        super().showEvent(event)

    def moveEvent(self, event) -> None:  # noqa: N802
        self._auto_flip()  # 拖过屏幕中线时跟着翻
        super().moveEvent(event)

    # ---------- 几何（判定只看这两个） ----------

    def mouth_rel_x(self) -> float:
        """筐口中心在窗口里的横向位置：镜像后左右对调（mouthX 永远按原图写）。"""
        x = self._cfg.mouth_x
        return 1.0 - x if self._flipped else x

    def mouth_center(self) -> tuple[float, float]:
        """筐口的全局坐标（判定圆圆心）。"""
        return (
            self.x() + self.width() * self.mouth_rel_x(),
            self.y() + self.height() * self._cfg.mouth_y,
        )

    def hit_radius(self) -> float:
        """判定半径：筐口内径的一半 × hitRatio。u = d / 它，u>1 就是没进。"""
        mouth_d = self.width() * self._cfg.mouth_ratio
        return max(6.0, mouth_d * 0.5 * self._cfg.hit_ratio)

    def pop(self) -> None:
        """命中反馈：筐口扩一圈波纹（不缩窗口，免得 mask/位置跟着抖）。"""
        self._ripple_t0 = time.monotonic()
        self._ripple.start()
        self.update()

    def _tick_ripple(self) -> None:
        if time.monotonic() - self._ripple_t0 > self.RIPPLE_SEC + 0.05:
            self._ripple.stop()
            self._ripple_t0 = -99.0
        self.update()

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing
        )
        pix = self._current_pix()
        if pix is not None:
            painter.drawPixmap(self.rect(), pix)
            if self._cfg.show_rings:
                self._paint_rings(painter)
        else:
            self._paint_placeholder(painter)
        self._paint_ripple(painter)
        painter.end()

    def _paint_rings(self, p: QPainter) -> None:
        cx, cy = self.width() * self.mouth_rel_x(), self.height() * self._cfg.mouth_y
        r = self.hit_radius()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(HALO, 1, Qt.DashLine))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(QPen(GOLD, 1.4, Qt.DashLine))
        rp = r * self._cfg.perfect_ratio
        p.drawEllipse(QPointF(cx, cy), rp, rp)

    def _paint_placeholder(self, p: QPainter) -> None:
        """蓝框占位：虚线外框 + 筐口 + 网 + 判定圈，图到位后自动不再画。"""
        w, h = self.width(), self.height()
        cx, cy = w * self.mouth_rel_x(), h * self._cfg.mouth_y
        rx = w * self._cfg.mouth_ratio * 0.5
        ry = rx * 0.42

        p.setPen(QPen(QColor(55, 138, 221, 160), 1.2, Qt.DashLine))
        p.setBrush(QColor(55, 138, 221, 22))
        p.drawRoundedRect(QRectF(2, 2, w - 4, h - 4), 14, 14)

        self._paint_rings(p)

        # 网
        nh = rx * 1.05
        bw = rx * 0.54
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(24, 95, 165, 140), 1.0))
        for t in (-1.0, -0.6, -0.2, 0.2, 0.6, 1.0):
            ty = cy + ry * math.sqrt(max(0.0, 1.0 - t * t))
            p.drawLine(QPointF(cx - rx * t, ty), QPointF(cx - bw * t, cy + nh))
        bottom = [
            QPointF(cx - bw * t, cy + nh + (1.0 - abs(t)) * ry * 0.30)
            for t in (-1.0, -0.8, -0.4, 0.0, 0.4, 0.8, 1.0)
        ]
        p.drawPolyline(QPolygonF(bottom))

        # 筐口
        p.setPen(QPen(QColor(24, 95, 165, 220), 2.6))
        p.drawEllipse(QRectF(cx - rx, cy - ry, rx * 2, ry * 2))

        # 占位小字（图到位后不会再有）
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(max(11, int(h * 0.05)))
        p.setFont(f)
        p.setPen(QColor(24, 95, 165, 190))
        p.drawText(QRectF(0, 6, w, 18), int(Qt.AlignHCenter | Qt.AlignVCenter), "篮筐 · 占位")

    def _paint_ripple(self, p: QPainter) -> None:
        u = (time.monotonic() - self._ripple_t0) / self.RIPPLE_SEC
        if not (0.0 <= u < 1.0):
            return
        cx, cy = self.width() * self.mouth_rel_x(), self.height() * self._cfg.mouth_y
        r0 = self.hit_radius()
        p.setBrush(Qt.NoBrush)
        for delay in (0.0, 0.22):
            v = (u - delay) / (1.0 - delay)
            if not (0.0 < v < 1.0):
                continue
            alpha = int(200 * (1.0 - v))
            p.setPen(QPen(QColor(GOLD.red(), GOLD.green(), GOLD.blue(), alpha), 2.2))
            p.drawEllipse(QPointF(cx, cy), r0 * (1.0 + v * 0.9), r0 * (1.0 + v * 0.9))

    # ---------- 交互 ----------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            # 跟宠物窗口一样，留到 release 再弹菜单（press 里 exec 会被随后的 release 关掉）
            self._context_pos = event.globalPosition().toPoint()
            event.accept()
            return
        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        self._press = event.globalPosition().toPoint()
        self._drag_origin = self._press - self.frameGeometry().topLeft()
        self._apply_mask()  # 换成并集区域，之后翻转就不用改区域了
        self.setCursor(Qt.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press is None or not (event.buttons() & Qt.LeftButton):
            event.ignore()
            return
        pos = event.globalPosition().toPoint()
        self.move(pos - self._drag_origin)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            pos, self._context_pos = self._context_pos, None
            if pos is not None and self.on_menu is not None:
                QTimer.singleShot(0, lambda p=pos: self.on_menu(p))
            event.accept()
            return
        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        self._press = None
        self._drag_origin = None
        self._apply_mask()  # 松手：区域收回当前朝向的真实形状
        self.unsetCursor()
        event.accept()


# ---------------------------------------------------------------- 分数徽章


class ScoreBadge(QWidget):
    """宠物头顶的常驻分数 + 命中飘字。

    不吃鼠标（WindowTransparentForInput），也不复用 bubble.SpeechBubble ——
    那个被会话状态/碎碎念来回占用，游戏分数放进去会被随时打断。
    """

    FLASH_SEC = 0.82

    def __init__(self, family: str, pet_size: int, offset: int = 8) -> None:
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

        self._family = family or "Microsoft YaHei UI"
        self._px = max(12, min(40, round(pet_size * 0.0455)))
        self._offset = int(offset)
        self._score = 0
        self._flash_text = ""
        self._flash_t0 = -99.0
        self._anchor = QRect()

        self._badge = QRectF()
        self._float_h = 0

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick_flash)

        self._relayout()
        self.hide()

    # ---------- 数据 ----------

    def set_score(self, score: int) -> None:
        if score == self._score:
            return
        self._score = int(score)
        self._relayout()
        self._place()
        self.update()

    def flash(self, delta: int) -> None:
        self._flash_text = f"+{int(delta)}"
        self._flash_t0 = time.monotonic()
        self._relayout()
        self._place()
        self._timer.start()
        self.raise_()
        self.update()

    def _tick_flash(self) -> None:
        if time.monotonic() - self._flash_t0 > self.FLASH_SEC:
            self._timer.stop()
            self._flash_text = ""
            self._flash_t0 = -99.0
        self.update()

    # ---------- 布局 / 贴位 ----------

    def _fonts(self) -> tuple[QFont, QFont]:
        f = QFont(self._family)
        f.setPixelSize(self._px)
        big = QFont(self._family)
        big.setPixelSize(round(self._px * 1.15))
        big.setBold(True)
        return f, big

    def _relayout(self) -> None:
        f, big = self._fonts()
        fm = QFontMetrics(f)
        text = f"分数 {self._score}"
        pad_x = round(self._px * 0.72)
        pad_y = round(self._px * 0.34)
        bw = fm.horizontalAdvance(text) + pad_x * 2
        bh = fm.height() + pad_y * 2

        bfm = QFontMetrics(big)
        fw = bfm.horizontalAdvance("+10") + 6
        self._float_h = bfm.height() + 4

        w = int(max(bw, fw)) + 8
        h = self._float_h + 4 + int(bh)
        self._badge = QRectF((w - bw) / 2.0, h - bh, bw, bh)
        if (w, h) != (self.width(), self.height()):
            self.resize(w, h)

    def follow(self, anchor: QRect) -> None:
        self._anchor = QRect(anchor)
        if self.isVisible():
            self._place()

    def _place(self) -> None:
        a = self._anchor
        if a.isNull():
            return
        head = a.top() + round(a.width() * 0.055)
        x = a.center().x() - self.width() // 2
        y = head - self.height() - self._offset
        screen = QGuiApplication.screenAt(a.center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 2, min(x, area.right() - self.width() - 2))
            y = max(area.top() + 2, min(y, area.bottom() - self.height() - 2))
        self.move(int(x), int(y))

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform
        )
        radius = self._badge.height() / 2.0

        painter.setPen(QPen(QColor(55, 138, 221, 90), 1))
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawRoundedRect(self._badge, radius, radius)

        f, big = self._fonts()
        painter.setPen(INK)
        painter.setFont(f)
        painter.drawText(
            self._badge, int(Qt.AlignCenter), f"分数 {self._score}"
        )

        if self._flash_text:
            u = (time.monotonic() - self._flash_t0) / self.FLASH_SEC
            u = min(max(u, 0.0), 1.0)
            # 先弹一下（1.0→1.18→1.0），再整体上浮淡出
            scale = 1.0 + 0.18 * math.sin(min(u / 0.35, 1.0) * math.pi)
            alpha = int(255 * (1.0 - max(0.0, (u - 0.55) / 0.45)))
            painter.setPen(QColor(GOLD.red(), GOLD.green(), GOLD.blue(), max(0, alpha)))
            big2 = QFont(big)
            big2.setPixelSize(max(10, round(self._px * 1.15 * scale)))
            painter.setFont(big2)
            area = QRectF(0, -u * self._float_h * 0.9, self.width(), self._float_h + 6)
            painter.drawText(area, int(Qt.AlignCenter), self._flash_text)
        painter.end()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)


# ---------------------------------------------------------------- 控制器


def read_best(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    try:
        return max(0, int(data.get("best", 0)))
    except (TypeError, ValueError, AttributeError):
        return 0


def write_best(path: Path, best: int) -> None:
    """只存一个最高分，写失败就算了（下次再破纪录时还会试）。"""
    try:
        path.write_text(json.dumps({"best": int(best)}, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[game] 最高分写盘失败：{exc}", flush=True)


class BasketGame:
    """一局投篮：管篮筐窗口、计分徽章，以及窗口抛掷事件的判定。"""

    def __init__(
        self,
        win,
        cfg: BasketballConfig,
        image: QPixmap | None,
        score_path: Path,
        family: str,
    ) -> None:
        self.win = win
        self.cfg = cfg
        self.score_path = score_path
        self.score = 0
        self.best = read_best(score_path)
        self.on_quit = None  # 由 main 接：结束这一局（菜单「结束投篮游戏」）

        self.basket = BasketWindow(cfg, image, on_menu=self._show_menu)
        self.badge = ScoreBadge(family, win.width(), offset=cfg.badge_offset)

        self._shot = False      # 当前这一投还没落地
        self._scored = False    # 这一投已经计过分（一投只计一次）
        self._celebrate = False  # 落地后要播一段庆祝
        self._best_d: float | None = None  # 这一投到目前为止离筐心最近的距离
        self._home: tuple[int, int] | None = None  # 这一投的起点（命中后回这里）
        self._home_timer = QTimer()
        self._home_timer.setSingleShot(True)
        self._home_timer.timeout.connect(self._go_home)

    # ---------- 开局 / 收尾 ----------

    def start(self) -> bool:
        if not self.cfg.enabled:
            print("[game] basketball.enabled = false，不开局", flush=True)
            return False

        x, y = self._initial_pos()
        self.basket.move(x, y)
        self.basket.show()
        self.basket.raise_()

        self.badge.set_score(0)
        self.badge.show()
        # 先 show 再贴位：follow() 对隐藏窗口只记 anchor 不挪位置，顺序反了会闪在屏幕左上角
        self.badge.follow(self.win.geometry())

        self.win.on_shot_start = self._on_shot_start
        self.win.on_throw_step = self._on_throw_step
        self.win.on_shot_land = self._on_shot_land
        self.win.followers.append(self._follow)
        self.win.set_game_lock(True)

        print(
            f"[game] 开始投篮 · 筐 @({x},{y}) {self.basket.width()}x{self.basket.height()} "
            f"判定半径 {self.basket.hit_radius():.0f}px · 最高 {self.best} 分",
            flush=True,
        )
        return True

    def stop(self) -> None:
        self._home_timer.stop()
        self._home = None
        if self._follow in self.win.followers:
            self.win.followers.remove(self._follow)
        self.win.on_shot_start = None
        self.win.on_throw_step = None
        self.win.on_shot_land = None
        self.win.set_game_lock(False)

        for w in (self.badge, self.basket):
            try:
                w.hide()
                w.close()
                w.deleteLater()
            except RuntimeError:
                pass
        print(f"[game] 结束投篮 · 本局 {self.score} 分 · 最高 {self.best} 分", flush=True)

    def set_visible(self, on: bool) -> None:
        """托盘「隐藏全部」时跟着一起藏（不然篮筐和分数会留在桌面上）。"""
        for w in (self.basket, self.badge):
            try:
                w.setVisible(on)
            except RuntimeError:
                pass

    def _follow(self, rect: QRect) -> None:
        self.badge.follow(rect)

    def _initial_pos(self) -> tuple[int, int]:
        """开局把筐放在宠物的**另一边**。

        以屏幕中线为准：宠物在中线左边 → 筐优先放右边，宠物在右边 → 筐优先放左边，
        这样永远隔着宠物一段距离，不会跟宠物挤在同一侧。那一边塞不下（宠物贴着屏边）就退到另一侧，
        两边都塞不下（窄屏 + 大宠物）就挑溢出小的那边贴边放。
        """
        win = self.win
        bw, bh = self.basket.width(), self.basket.height()
        app = QGuiApplication.instance()
        screen = app.screenAt(win.frameGeometry().center()) if app else None
        area = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)

        gap = 40
        right = win.x() + win.width() + gap
        left = win.x() - bw - gap
        pet_cx = win.x() + win.width() / 2.0
        prefer_right = pet_cx <= area.center().x()

        lo, hi = area.left() + 8, area.right() - 8 - bw
        x = None
        for cand in ((right, left) if prefer_right else (left, right)):
            if lo <= cand <= hi:
                x = cand
                break
        if x is None:
            # 两边都放不下：挑溢出小的那边，再夹回屏内（宁可贴边也别整块跑到屏幕外）
            def overflow(c: float) -> float:
                return max(0.0, lo - c) + max(0.0, c - hi)

            x = max(lo, min(min((right, left), key=overflow), hi))

        y = win.y() + int(win.height() * 0.2)
        y = max(area.top() + 8, min(y, area.bottom() - bh - 8))
        return int(x), int(y)

    def _reset_pos(self) -> None:
        x, y = self._initial_pos()
        self.basket.move(x, y)
        self.basket.raise_()

    # ---------- 右键菜单 ----------

    def _show_menu(self, global_pos) -> None:
        menu = QMenu()
        menu.setStyleSheet(MENU_QSS)
        info = menu.addAction(f"分数 {self.score} · 最高 {self.best}")
        info.setEnabled(False)
        menu.addSeparator()
        home = menu.addAction("回到宠物旁边")
        home.triggered.connect(lambda _checked=False: self._reset_pos())
        quit_act = menu.addAction("结束投篮游戏")
        quit_act.triggered.connect(self._request_quit)
        menu.exec(global_pos)

    def _request_quit(self) -> None:
        # 排到下一轮再关自己：菜单还在 exec 里，这时候删窗口容易崩
        if self.on_quit is not None:
            QTimer.singleShot(0, self.on_quit)

    # ---------- 一投 ----------

    def _on_shot_start(self) -> None:
        self._shot = True
        self._scored = False
        self._celebrate = False
        self._best_d = None
        self._home_timer.stop()
        # 甩出去这一刻的窗口位置 = 这一投的起点（`_start_throw` 先记 ThrowState 再回调，
        # 所以这里读到的还是松手原地，宠物一像素都还没飞出去）
        try:
            self._home = (self.win.x(), self.win.y())
        except RuntimeError:
            self._home = None

    def _on_throw_step(self, p0: tuple[float, float], p1: tuple[float, float]) -> None:
        if not self._shot or self._scored:
            return
        cx, cy = self.basket.mouth_center()
        radius = self.basket.hit_radius()
        dist = physics.segment_point_distance(p0, p1, (cx, cy))
        if dist <= radius:
            # 还在圈里：先记「到目前为止离筐心最近多远」，别按入口距离急着算分。
            # 一进圈就结算的话，正穿筐心的球也是拿入口那一点的边缘距离（实测 3.7px 的
            # 空心球被判成 4 分），「越准分越高」就废了。
            self._best_d = dist if self._best_d is None else min(self._best_d, dist)
        elif self._best_d is not None:
            self._settle()  # 出圈了：用这一程的最近距离结算

    def _on_shot_land(self) -> None:
        was = self._shot
        self._shot = False
        if was and self._best_d is not None:
            self._settle()  # 停在筐里/被抓住：落地这一刻结算
        if not (was and self._celebrate):
            return
        self._celebrate = False
        if self._home_timer.isActive():
            return  # 回位的定时器已经排上了：庆祝统一在「回去」那一刻放，别播两遍
        # 排到下一轮：这一帧待机刚起播，立刻放庆祝会被待机的 _play() 掐掉
        QTimer.singleShot(0, self._celebrate_now)

    def _celebrate_now(self) -> None:
        if not self.cfg.celebrate_anim:
            return
        try:
            self.win.play_effect(self.cfg.celebrate_anim)
        except RuntimeError:  # 宠物窗口已经销毁（「关掉这只」）
            pass

    def _go_home(self) -> None:
        """命中后把宠物送回这一投的起点。

        · 它还在天上飞（撞墙弹跳能耗好几秒）→ 直接收掉这一投，别让「回去」等太久
        · 这几百毫秒里用户又把它抓起来了 → 不抢鼠标，让它跟着手走
        """
        home, self._home = self._home, None
        if home is None:
            return
        self._celebrate = False  # 回位时已经庆祝过，落地别再补一遍
        try:
            if self.win.state == "drag":
                return
            if self.win.state == "throw":
                self.win.abort_throw()
            self.win.move(int(home[0]), int(home[1]))
            print(f"[game] 命中回位 → ({home[0]},{home[1]})", flush=True)
            self._celebrate_now()
        except RuntimeError:
            return

    def _settle(self) -> None:
        """按这一投的最近距离结算分数（一投只算一次）。"""
        dist, self._best_d = self._best_d, None
        if dist is None or self._scored:
            return
        got = physics.score_for_distance(
            dist, self.basket.hit_radius(), self.cfg.perfect_ratio
        )
        if got <= 0:
            return

        self._scored = True
        self._celebrate = True
        self.score += got
        self.badge.set_score(self.score)
        self.badge.flash(got)
        self.basket.pop()

        new_best = self.score > self.best
        if new_best:
            self.best = self.score
            write_best(self.score_path, self.best)
        # 命中就排队回起点：不等落地 —— 撞墙/落地弹跳能耗好几秒，等它静下来再回去太拖
        if self.cfg.return_home and self._home is not None:
            self._home_timer.start(max(0, self.cfg.return_delay_ms))
        print(
            f"[game] 命中 +{got} · 距筐心 {dist:.0f}px · 本局 {self.score} 分"
            f"{' · 新纪录' if new_best else ''}",
            flush=True,
        )
