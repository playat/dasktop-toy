"""透明无边框置顶窗口：动画链状态机 + 拖拽 + 抛掷物理。

状态机（与 Electron 版语义一致）：
    idle  → 播一段待机，播完掷骰
    action→ 随机分类小动作，播完掷骰
    turn  → 转向动画，播完翻转朝向再掷骰
    move  → 走动动画，按 lead/tail 线性平移窗口，结束掷骰
    drag  → 按住拖动（循环播「被悬空」）
    throw → 松手后抛掷：重力 + 边缘反弹 + 落地摩擦，静止后回掷骰
    click → 点击回应，播完掷骰

掷骰权重来自 animationWeights（idle/turn/move），余量归随机分类动作。
右键弹菜单（见 menu.py），菜单里的点播走 play_named()，不受 fixedEnabled 限制。
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import QWidget

import physics
from config import PetConfig
from player import AnimationPlayer, AnimationSet

# 素材几何常量（与 shared/constants.ts 同一套 640×360 画布语义）
FEET_Y_RATIO = 330 / 360
SIDE_ALLOW_RATIO = 200 / 640
PET_REF_WIDTH = 462
SQ_DURATION_SEC = physics.SQ_DURATION_MS / 1000.0

DRAG_THRESHOLD = 5
TRAIL_MAX = 64


class PetWindow(QWidget):
    TICK_MS = 15

    def __init__(
        self,
        player: AnimationPlayer,
        anims: AnimationSet,
        cfg: PetConfig,
        width: int,
        height: int,
        area: QRect,
        fixed: bool = False,
    ) -> None:
        super().__init__()
        self._player = player
        self._anims = anims
        self._cfg = cfg
        self._phys = cfg.physics
        self._area = QRect(area)
        self._fixed = fixed  # 每只宠物自己的「固定原地」（pets[].fixedEnabled）
        self._confine = bool(cfg.confine_to_screen)  # 抛掷是否锁在松手那屏（根字段 confineToScreen）

        # 绘制
        self._cur: QImage | None = None
        self._prev: QImage | None = None
        self._fade = 1.0
        self._last_drawn: QImage | None = None
        self.facing = "left"
        self.state = "idle"

        # 交互
        self._press_global: QPoint | None = None
        self._drag_origin: QPoint | None = None
        self._dragging = False
        self._context_pos: QPoint | None = None
        self._trail: deque[tuple[float, float, float]] = deque(maxlen=TRAIL_MAX)

        # 走位
        self._move_t0 = 0.0
        self._move_dur = 10.0
        self._move_from = 0.0
        self._move_target = 0.0
        self._move_lead = 2.0
        self._move_tail = 2.0

        # 抛掷
        self._throw: physics.ThrowState | None = None
        self._throw_last = 0.0

        # 落地挤压
        self._squash_t0 = -99.0
        self._squash_amp = 1.0

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.resize(width, height)

        # 回调（由 main 接）：右键菜单 / 「回到初始位置」的目标坐标 / 同屏的其它宠物（互撞用）
        self.on_context_menu = None
        self.home_provider = None
        self.peers_provider = None

        # 说话气泡（由 EventDirector 挂上来；没挂就是纯动画）
        self.bubble = None
        # DSH 第三方占用锁：true = 不自己掷骰、不自己走（显式点播照常）
        self.external_lock = False
        # 点击音效 + 拖拽光标（素材在 assets/sound、assets/pic）
        self.click_sound: Path | None = None
        self._last_click_sound = 0.0
        self._cursor_idle: QCursor | None = None
        self._cursor_hold: QCursor | None = None

        self._recalc_geometry(width, height)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(self.TICK_MS)

    def _recalc_geometry(self, width: int, height: int) -> None:
        """尺寸变化后重算「身体贴边」边距、地面高度与抛掷边界。"""
        self._side_allow = width * SIDE_ALLOW_RATIO
        self._ground_y = self._area.top() + self._area.height() - height * FEET_Y_RATIO
        self._bounds = physics.Bounds(
            min_x=self._area.left() - self._side_allow,
            min_y=self._area.top() if self._phys.ceiling_bounce else self._area.top() - height,
            max_x=self._area.left() + self._area.width() - width + self._side_allow,
            max_y=self._ground_y,
        )

    def _sync_area(self) -> None:
        """把「宠物当前所在那块屏」的可视区同步给边界计算。

        不这么做的话，拖到副屏甩出去会被主屏的 max_x/min_x 钳回来，宠物直接瞬移到主屏边缘
        （多屏用户看到的就是"飞一半消失，然后贴回主屏"）。
        """
        self._adopt_area(self._screen_area_at_center())

    def _follow_screen(self) -> None:
        """抛掷途中跟着窗口中心换屏（confineToScreen = false 时每帧调）。

        中心落在某块屏上就换成那块屏的边界；落在屏缝 / 所有屏之外（screenAt 返回 None）
        就保持当前边界，让宠物先把这一步飞完 —— 不会因为"暂时不属于任何屏"被钳在半空。
        与 _sync_area 的区别：这个不 fallback 到主屏，纯粹跟着中心走。
        """
        if self._confine:
            return
        app = QGuiApplication.instance()
        if app is None:
            return
        screen = app.screenAt(self.frameGeometry().center())
        if screen is None:
            return
        self._adopt_area(screen.availableGeometry())

    def _screen_area_at_center(self) -> QRect:
        app = QGuiApplication.instance()
        if app is None:
            return self._area
        screen = app.screenAt(self.frameGeometry().center()) or app.primaryScreen()
        return screen.availableGeometry() if screen is not None else self._area

    def _adopt_area(self, area: QRect) -> None:
        if area == self._area:
            return
        self._area = QRect(area)
        self._recalc_geometry(self.width(), self.height())
        print(
            f"[pet] 所在屏 → {area.width()}x{area.height()} @({area.left()},{area.top()})",
            flush=True,
        )

    # ---------- 动画链 ----------

    def start(self) -> None:
        self._roll_next(force_idle=True)

    # ---------- 外部挂件（气泡 / 音效 / 光标 / 第三方占用） ----------

    def attach_bubble(self, bubble) -> None:
        """挂上说话气泡（EventDirector 建好传进来，这里只负责跟着窗口走）。"""
        self.bubble = bubble
        bubble.configure(
            pet_size=self.width(),
            family=getattr(self._cfg.font, "family", ""),
            size=getattr(self._cfg.font, "size", 0),
            follow=getattr(self._cfg.font, "follow_pet", True),
        )

    def set_external_lock(self, on: bool) -> None:
        """DSH 第三方占用锁：冻结自播（不掷骰、不走位），显式点播与拖拽照常。"""
        self.external_lock = bool(on)
        if self.external_lock:
            self._stop_move()
            self._throw = None

    def set_sound(self, path: Path | None) -> None:
        self.click_sound = path

    def set_cursors(self, idle: Path | None, hold: Path | None) -> None:
        """拖拽光标（原版 assets/pic/cursor-grab(.bing).png）；文件缺就保持系统箭头。"""
        self._cursor_idle = _cursor_from(idle)
        self._cursor_hold = _cursor_from(hold)
        if self._cursor_idle is not None:
            self.setCursor(self._cursor_idle)

    def _play_click_sound(self) -> None:
        """点一下「啾」一声（100ms 内的连点只响一次，多开也不叠音）。"""
        if self.click_sound is None or not self.click_sound.exists():
            return
        now = time.monotonic()
        if now - self._last_click_sound < 0.1:
            return
        self._last_click_sound = now
        path = str(self.click_sound)

        def job() -> None:
            try:
                import winsound  # 仅 Windows；其他平台静默跳过

                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=job, name="click-sound", daemon=True).start()

    def _play(self, name: str, path, *, loop: bool, state: str, on_finish=None) -> bool:
        if not self._player.play(name, path, loop=loop, on_finish=on_finish):
            return False
        self.state = state
        return True

    def _play_idle(self, force: bool = False) -> None:
        picked = self._anims.idle()
        if picked is None:
            return
        name, path = picked
        self._play(name, path, loop=False, state="idle", on_finish=self._after_oneshot)
        print(f"[anim] idle → {name}", flush=True)

    def _play_action(self) -> None:
        picked = self._anims.random_action(self.facing, exclude=self._player.current)
        if picked is None:
            self._play_idle()
            return
        name, path = picked
        if self._play(name, path, loop=False, state="action", on_finish=self._after_oneshot):
            print(f"[anim] action → {name}", flush=True)

    def _play_turn(self) -> None:
        picked = self._anims.turn()
        if picked is None:
            self._play_idle()
            return
        name, path = picked
        if self._play(name, path, loop=False, state="turn", on_finish=self._after_oneshot):
            print(f"[anim] turn → {name}", flush=True)

    def _roll_next(self, force_idle: bool = False) -> None:
        if self.state in ("drag", "throw"):
            return
        if self.external_lock:
            # 被第三方占用：只保持当前画面，不自己开新动作（显式点播/点击回应照常走别的入口）
            return
        w = self._cfg.weights
        idle_w = max(0.0, w.get("idle", 10.0))
        turn_w = 0.0 if self._fixed else max(0.0, w.get("turn", 5.0))
        move_w = 0.0 if self._fixed else max(0.0, w.get("move", 5.0))

        roll = random.uniform(0.0, 100.0)
        has_turn = self._anims.has_role("turn")
        has_move = self._anims.has_role("move")
        if force_idle or roll < idle_w or not (has_turn or has_move):
            self._play_idle(force=force_idle)
        elif roll < idle_w + turn_w:
            self._play_turn()
        elif roll < idle_w + turn_w + move_w:
            if not self._start_move():
                self._play_action()
        else:
            self._play_action()

    def _after_oneshot(self) -> None:
        """一次性动画播完后的分派（player.tick 内回调）。"""
        if self.state == "turn":
            self.facing = "right" if self.facing == "left" else "left"
        self._roll_next()

    # ---------- 走位 ----------

    def _start_move(self, preferred=None) -> bool:
        self._sync_area()
        if preferred is not None:
            action = preferred
            path = self._anims.path_of(action.name)
            if path is None:
                return False
        else:
            picked = self._anims.move(exclude=self._player.current)
            if picked is None:
                return False
            action, path = picked

        direction = -1 if self.facing == "left" else 1
        scale = self.width() / PET_REF_WIDTH
        plan = physics.plan_move(
            x=self.x(),
            area_left=self._area.left(),
            area_width=self._area.width(),
            half_w=self.width() / 2,
            direction=direction,
            min_dist=action.min_dist * scale,
            max_dist=action.max_dist * scale,
            margin=action.margin,
            side_allow=self._side_allow,
        )
        if plan is None:
            return False

        if not self._player.play(action.name, path, loop=False, on_finish=None):
            return False
        self.state = "move"
        self.facing = "left" if direction < 0 else "right"
        self._move_t0 = time.monotonic()
        self._move_dur = self._player.duration
        self._move_from = float(self.x())
        self._move_target = plan.target_x
        self._move_lead = action.lead_sec
        self._move_tail = action.tail_sec
        print(
            f"[anim] move → {action.name}  距离 {plan.distance:.0f}px "
            f"{'←' if direction < 0 else '→'}  时长 {self._move_dur:.1f}s",
            flush=True,
        )
        return True

    def _drive_move(self) -> None:
        t = time.monotonic() - self._move_t0
        dur = self._move_dur
        lead, tail = self._move_lead, self._move_tail
        if t >= dur:
            self.move(round(self._move_target), self.y())
            self.state = "idle"
            self._roll_next()
            return
        travel = max(0.1, dur - lead - tail)
        if t <= lead:
            ratio = 0.0
        elif t >= dur - tail:
            ratio = 1.0
        else:
            ratio = (t - lead) / travel
        x = self._move_from + (self._move_target - self._move_from) * ratio
        self.move(round(x), self.y())

    def _stop_move(self) -> None:
        if self.state == "move":
            self.state = "idle"

    # ---------- 抛掷 ----------

    def _start_throw(self, vx: float, vy: float) -> None:
        self._sync_area()
        self.state = "throw"
        self._throw = physics.ThrowState(float(self.x()), float(self.y()), vx, vy)
        self._throw_last = time.monotonic()

    def _drive_throw(self) -> None:
        if self._throw is None:
            self.state = "idle"
            return
        now = time.monotonic()
        dt = min(now - self._throw_last, 0.2)
        self._throw_last = now
        if dt <= 0:
            return

        # 跨屏飞行（confineToScreen = false）：中心进入隔壁屏就换成那块屏的边界继续飞。
        # 必须在积分前换，否则这一步还会被旧屏的 max_x/min_x 弹回来。
        self._follow_screen()

        before_vy = self._throw.vy
        state, bounced, at_rest = physics.throw_step(self._throw, dt, self._bounds, self._phys)

        # 宠物间碰撞（配置 physics.petCollision）：飞行中撞到别的宠物 → 双方按动量弹开
        if self._phys.pet_collision:
            state = self._bounce_off_peers(state)

        # 落地那一帧触发 Q 弹（冲击速度 = 积分前的下落速度）
        landed = bounced and state.y >= self._bounds.max_y - 0.5 and before_vy > 0
        if landed:
            self._squash_t0 = now
            self._squash_amp = physics.landing_squash(before_vy)

        self._throw = state
        self.move(round(state.x), round(state.y))

        if at_rest:
            self._throw = None
            self.state = "idle"
            self._roll_next()

    def _bounce_off_peers(self, state: physics.ThrowState) -> physics.ThrowState:
        """本帧自己被甩出去时撞到别的宠物 → 改写自己的速度 + 通知对方以新初速飞出去。

        一帧只处理一次（原版同样 break）：避免三只以上时连锁触发抖成筛子。
        """
        if self.peers_provider is None:
            return state
        mine = physics.body_box(state.x, state.y, self.width(), self.height())
        for other in self.peers_provider():
            if other is self or not other.isVisible():
                continue
            theirs = other.body_box()
            if not physics.rects_overlap(mine, theirs):
                continue
            ovx, ovy = other.throw_velocity()
            hit = physics.collide_pet(
                (state.x, state.y, state.vx, state.vy, float(self.width())),
                (float(other.x()), float(other.y()), ovx, ovy, float(other.width())),
            )
            if hit is None:
                continue
            fvx, fvy, hvx, hvy = hit
            other.on_hit(hvx, hvy)
            print(f"[pet] 撞上另一只宠物 → 自己 ({fvx:.0f},{fvy:.0f}) 对方 ({hvx:.0f},{hvy:.0f})", flush=True)
            return physics.ThrowState(state.x, state.y, fvx, fvy)
        return state

    def body_box(self) -> tuple[float, float, float, float]:
        return physics.body_box(
            float(self.x()), float(self.y()), float(self.width()), float(self.height())
        )

    def throw_velocity(self) -> tuple[float, float]:
        """当前速度；没在飞行就是 0（被撞时当作静止目标）。"""
        if self._throw is None or self.state != "throw":
            return 0.0, 0.0
        return self._throw.vx, self._throw.vy

    def on_hit(self, vx: float, vy: float) -> None:
        """被别的宠物撞到：停掉当前动作，从落点以新初速飞出去（复用同一套抛掷物理）。"""
        self._stop_move()
        self._start_throw(vx, vy)

    # ---------- 菜单 / 外部控制 ----------

    def play_named(self, key: str) -> None:
        """右键点播一个动作：播一次，播完回到随机链。

        key 一般是素材名；素材放进了子目录时是「子目录/名字」（见 AnimationSet.scan）。
        """
        path = self._anims.path_of(key)
        if path is None:
            return
        stem = key.rsplit("/", 1)[-1]

        # 走位目录里的动作走完整一次走位（不然只有原地踏步，看着像不动的贴图）
        for action, _path in self._anims.moves():
            if action.name in (key, stem):
                if self._start_move(preferred=action):
                    return
                break

        # 文字类（noMirror）朝右时先掰回朝左，否则镜像会让字反过来
        if self.facing == "right" and stem in self._anims.pools.no_mirror:
            self.facing = "left"

        if self._play(key, path, loop=False, state="action", on_finish=self._after_oneshot):
            print(f"[anim] menu → {key}", flush=True)

    @property
    def fixed(self) -> bool:
        return self._fixed

    def set_fixed(self, on: bool) -> None:
        """「固定原地」：只掐随机链里的转向/走位两档，用户主动点播照常。"""
        self._fixed = bool(on)
        if self._fixed:
            self._stop_move()
        print(f"[pet] 固定原地 = {self._fixed}", flush=True)

    def apply_size(self, size: int) -> None:
        """以脚底中心为锚点等比缩放，宠物不会忽然跳到别处。"""
        width = int(size)
        height = int(round(width * 9 / 16))
        feet_x = self.x() + self.width() / 2
        feet_y = self.y() + self.height() * FEET_Y_RATIO

        self._throw = None
        self._stop_move()
        self.resize(width, height)
        self._recalc_geometry(width, height)
        self.move(int(feet_x - width / 2), int(feet_y - height * FEET_Y_RATIO))
        if self.bubble is not None:
            self.bubble.configure(
                pet_size=width,
                family=getattr(self._cfg.font, "family", ""),
                size=getattr(self._cfg.font, "size", 0),
                follow=getattr(self._cfg.font, "follow_pet", True),
            )
            self.bubble.reflow(self.geometry())
        self.update()
        print(f"[pet] 尺寸 → {width}x{height}", flush=True)

    def reset_home(self) -> None:
        if self.home_provider is None:
            return
        x, y = self.home_provider(self.width(), self.height())
        self._throw = None
        self._stop_move()
        self.move(x, y)
        print(f"[pet] 回到初始位置 ({x}, {y})", flush=True)

    def reload_config(self, cfg: PetConfig, anims: AnimationSet) -> None:
        self._cfg = cfg
        self._phys = cfg.physics
        self._anims = anims
        self._player.stop()
        self._cur = self._prev = self._last_drawn = None
        self.state = "idle"
        self._roll_next(force_idle=True)

    def shutdown(self) -> None:
        self._timer.stop()
        self._player.stop()
        if self.bubble is not None:
            self.bubble.clear()
            self.bubble.close()
            self.bubble = None

    # ---------- 绘制 ----------

    def _tick(self) -> None:
        if self.state == "move":
            self._drive_move()
        elif self.state == "throw":
            self._drive_throw()

        current, previous, fade = self._player.tick()
        self._cur, self._prev, self._fade = current, previous, fade
        if self.bubble is not None:
            self.bubble.follow(self.geometry())
        needs = current is not None and (current is not self._last_drawn or fade < 1.0)
        # 挤压动画进行中要持续重绘
        if time.monotonic() - self._squash_t0 < SQ_DURATION_SEC:
            needs = True
        if needs:
            self._last_drawn = current
            self.update()

    def _squash_factors(self) -> tuple[float, float]:
        u = (time.monotonic() - self._squash_t0) / SQ_DURATION_SEC
        if u >= 1.0 or u < 0.0:
            return 1.0, 1.0
        sy = physics.squash_scale(u, self._squash_amp)
        sx = 1.0 + (1.0 - sy) * physics.SQ_WIDTH_GAIN
        return sx, sy

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._cur is None and self._prev is None:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        sx, sy = self._squash_factors()
        transform = QTransform()
        if sy != 1.0 or sx != 1.0:
            # 以「脚底 + 水平中心」为锚点缩放：只压高度时脚不离地，
            # 横向撑开也是左右对称，不会把角色往一边推着裁掉。
            feet = self.height() * FEET_Y_RATIO
            cx = self.width() / 2.0
            transform.translate(cx, feet)
            transform.scale(sx, sy)
            transform.translate(-cx, -feet)
        if self.facing == "right":
            transform.translate(self.width(), 0.0)
            transform.scale(-1.0, 1.0)
        painter.setTransform(transform)

        if self._prev is not None and self._cur is not None and self._fade < 1.0:
            painter.setOpacity(1.0 - self._fade)
            painter.drawImage(self.rect(), self._prev)
            painter.setOpacity(self._fade)
            painter.drawImage(self.rect(), self._cur)
        else:
            image = self._cur or self._prev
            if image is not None:
                painter.drawImage(self.rect(), image)
        painter.end()

    # ---------- 交互 ----------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            # 菜单留到 release 再弹：在 press 里直接 exec() 会撞上鼠标抓取，
            # 弹出来立刻被随后的 release 关掉。
            self._context_pos = event.globalPosition().toPoint()
            event.accept()
            return
        if event.button() != Qt.LeftButton:
            event.ignore()
            return

        # 抓住飞行中的宠物 / 打断走位
        self._throw = None
        self._stop_move()

        self._press_global = event.globalPosition().toPoint()
        self._drag_origin = self._press_global - self.frameGeometry().topLeft()
        self._dragging = False
        if self._cursor_hold is not None:
            self.setCursor(self._cursor_hold)
        self._trail.clear()
        self._trail.append((time.monotonic() * 1000.0, float(self._press_global.x()), float(self._press_global.y())))
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_global is None or not (event.buttons() & Qt.LeftButton):
            event.ignore()
            return

        position = event.globalPosition().toPoint()
        if not self._dragging:
            if (position - self._press_global).manhattanLength() < DRAG_THRESHOLD:
                event.accept()
                return
            self._dragging = True
            picked = self._anims.drag()
            if picked is not None:
                name, path = picked
                self._play(name, path, loop=True, state="drag")
                print(f"[anim] drag → {name}", flush=True)

        self.move(position - self._drag_origin)
        self._trail.append((time.monotonic() * 1000.0, float(position.x()), float(position.y())))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            pos, self._context_pos = self._context_pos, None
            if pos is not None and self.on_context_menu is not None:
                # 排到事件循环下一轮，等本次鼠标事件彻底处理完再弹
                QTimer.singleShot(0, lambda p=pos: self.on_context_menu(p))
            event.accept()
            return

        if event.button() != Qt.LeftButton:
            event.ignore()
            return

        was_dragging = self._dragging
        self._dragging = False
        self._press_global = None
        self._drag_origin = None
        if self._cursor_idle is not None:
            self.setCursor(self._cursor_idle)

        if was_dragging:
            now_ms = time.monotonic() * 1000.0
            velocity = physics.estimate_release_velocity(list(self._trail), now_ms, self._phys)
            if velocity is None:
                # 正常拖到某处松手：原地落住，不抛物也不自由落体
                print("[anim] release → 放在原地", flush=True)
                self.state = "idle"
                self._throw = None
                self._play_idle(force=True)
            else:
                vx, vy = velocity
                print(f"[anim] release → 甩出 v=({vx:.0f}, {vy:.0f})px/s", flush=True)
                self._start_throw(vx, vy)
        else:
            self._play_click_sound()
            picked = self._anims.click()
            if picked is not None:
                name, path = picked
                if self._play(name, path, loop=False, state="click", on_finish=self._after_oneshot):
                    print(f"[anim] click → {name}", flush=True)
            else:
                self._roll_next()
        self._trail.clear()
        event.accept()


def _cursor_from(path: Path | None) -> QCursor | None:
    """把 png 变成光标（缩到 32px）；读不到就返回 None（保持系统箭头）。"""
    if not path:
        return None
    target = Path(path)
    if not target.exists():
        return None
    pix = QPixmap(str(target))
    if pix.isNull():
        return None
    pix = pix.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return QCursor(pix, 4, 4)
