"""抛掷 / 落地回弹 / 走位几何 —— `shared/physics.ts` + `shared/motion.ts` 的直译。

只放纯函数，不碰 Qt。所有坐标单位是「逻辑像素」，与 QWidget.move() 同一套。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# ---- 抛掷 ----
GRAVITY = 1400.0
RESTITUTION = 0.78
GROUND_FRICTION = 2.5
REST_VY = 40.0
REST_VX = 15.0
MAX_STEP_DT = 0.05

# ---- 松手初速估算 ----
RELEASE_WINDOW_MS = 150.0
RELEASE_STALE_MS = 150.0
MIN_SPAN_MS = 20.0
SEG_MIN_DT_MS = 8.0
DEAD_ZONE_SPEED = 500.0
MAX_THROW_SPEED = 3600.0
PEAK_WEIGHT = 0.5
ACCEL_REF = 8000.0
ACCEL_GAIN_MAX = 0.6

# ---- 多宠互撞（petCollision） ----
# 画布 640×360 内的身体命中矩形，与 shared/constants.ts 的 HIT_BOX 同一套（守卫测试钉住二者同步）
HIT_BOX = (200, 50, 440, 335)
PET_BOUNCE_E = 0.995


def body_box(x: float, y: float, width: float, height: float) -> tuple[float, float, float, float]:
    """窗口左上角 → 身体像素包围盒 (left, top, right, bottom)。"""
    x0, y0, x1, y1 = HIT_BOX
    return (
        x + x0 / 640.0 * width,
        y + y0 / 360.0 * height,
        x + x1 / 640.0 * width,
        y + y1 / 360.0 * height,
    )


def rects_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def collide_pet(
    fly: tuple[float, float, float, float, float],
    hit: tuple[float, float, float, float, float],
) -> tuple[float, float, float, float] | None:
    """飞行宠物撞静止/飞行宠物：动量守恒 + 恢复系数。返回 (飞行方 vx, vy, 被撞方 vx, vy)。

    参数统一为 (x, y, vx, vy, 宽)。质量 ∝ 宽²（与 shared/physics.ts 的 collidePet 一致）。
    中心重合或正在分离时返回 None —— 避免重叠帧反复触发抖动。
    """
    fx, fy, fvx, fvy, fs = fly
    hx, hy, hvx, hvy, hs = hit
    hf = fs * 9.0 / 16.0 / 2.0
    hh = hs * 9.0 / 16.0 / 2.0
    cx = hx + hs / 2.0 - (fx + fs / 2.0)
    cy = hy + hh - (fy + hf)
    dist = math.hypot(cx, cy)
    if dist < 1e-6:
        return None
    nx, ny = cx / dist, cy / dist
    vrel = (fvx - hvx) * nx + (fvy - hvy) * ny
    if vrel <= 0:
        return None

    e = PET_BOUNCE_E
    m1, m2 = fs * fs, hs * hs
    v1n = fvx * nx + fvy * ny
    v2n = hvx * nx + hvy * ny
    v1n2 = ((m1 - e * m2) * v1n + (1 + e) * m2 * v2n) / (m1 + m2)
    v2n2 = ((m2 - e * m1) * v2n + (1 + e) * m1 * v1n) / (m1 + m2)
    return (
        fvx - v1n * nx + v1n2 * nx,
        fvy - v1n * ny + v1n2 * ny,
        hvx - v2n * nx + v2n2 * nx,
        hvy - v2n * ny + v2n2 * ny,
    )


# ---- 落地 Q 弹 ----
# 皮球式：短促、克制的压扁，回弹略有过冲，宽度几乎不动。
# 原值 0.55（压到 45%）配 35% 的横向耦合，是把角色压成"饼"的主因。
SQ_SQUASH = 0.72
SQ_DURATION_MS = 190.0
SQ_SOFT_SPEED = 300.0
SQ_HARD_SPEED = 1500.0
SQ_MAX_SQUASH = 0.72
# 轻落（速度低于 SOFT）也吃的压幅下限：0.9 = 只压 10%
SQ_LIGHT_SQUASH = 0.9
# 回弹过冲上限（1.0 = 不拉长）
SQ_MAX_STRETCH = 1.06
# 横向耦合系数：压扁时宽度只放大 (1-sy)*gain，0.12 → 最深也只宽 ~3.4%
SQ_WIDTH_GAIN = 0.12


@dataclass
class PhysicsParams:
    gravity: float = GRAVITY
    restitution: float = RESTITUTION
    ground_friction: float = GROUND_FRICTION
    ceiling_bounce: bool = True
    throw_power: float = 1.0
    pet_collision: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> "PhysicsParams":
        return cls(
            gravity=float(raw.get("gravity", GRAVITY)),
            restitution=float(raw.get("restitution", RESTITUTION)),
            ground_friction=float(raw.get("groundFriction", GROUND_FRICTION)),
            ceiling_bounce=bool(raw.get("ceilingBounce", True)),
            throw_power=float(raw.get("throwPower", 1.0)),
            pet_collision=bool(raw.get("petCollision", False)),
        )


@dataclass
class Bounds:
    """包围盒左上角的允许范围。"""

    min_x: float
    min_y: float
    max_x: float
    max_y: float


@dataclass
class ThrowState:
    x: float
    y: float
    vx: float
    vy: float


@dataclass
class MovePlan:
    target_x: float
    distance: float


def soft_clamp_speed(speed: float) -> float:
    """软钳速：渐近逼近 MAX_THROW_SPEED，任意输入都单调可区分。"""
    if speed <= 0:
        return 0.0
    return MAX_THROW_SPEED * (1.0 - math.exp(-speed / MAX_THROW_SPEED))


def estimate_release_velocity(
    trail: list[tuple[float, float, float]],
    now_ms: float,
    params: PhysicsParams,
) -> tuple[float, float] | None:
    """由拖拽轨迹估算松手初速 (vx, vy) px/s；None = 温柔放下。

    trail 每项为 (t_ms, x, y)，按时间追加。
    """
    if not trail:
        return None
    last_t = trail[-1][0]
    if now_ms - last_t > RELEASE_STALE_MS:
        return None

    win = [s for s in trail if now_ms - s[0] <= RELEASE_WINDOW_MS]
    if len(win) < 2:
        return None

    t0, x0, y0 = win[0]
    t1, x1, y1 = win[-1]
    span = t1 - t0
    if span < MIN_SPAN_MS:
        return None

    base_vx = (x1 - x0) / span * 1000.0
    base_vy = (y1 - y0) / span * 1000.0
    base_speed = math.hypot(base_vx, base_vy)
    if base_speed < 1e-6:
        return None

    # 分段速度（过密采样向前合并）
    seg_speeds: list[tuple[float, float]] = []
    px, py, pt = x0, y0, t0
    for t, x, y in win[1:]:
        dt = t - pt
        if dt >= SEG_MIN_DT_MS:
            seg_speeds.append((math.hypot(x - px, y - py) / dt * 1000.0, t))
            px, py, pt = x, y, t

    peak = max((s for s, _ in seg_speeds), default=base_speed)

    accel = 0.0
    if len(seg_speeds) >= 2:
        last_seg, first_seg = seg_speeds[-1], seg_speeds[0]
        accel = (last_seg[0] - first_seg[0]) / max((last_seg[1] - first_seg[1]) / 1000.0, MIN_SPAN_MS / 1000.0)

    speed_before = ((1 - PEAK_WEIGHT) * base_speed + PEAK_WEIGHT * peak) * (
        1 + min(max(accel, 0.0) / ACCEL_REF, 1.0) * ACCEL_GAIN_MAX
    )
    speed = soft_clamp_speed(speed_before) * params.throw_power
    if speed < DEAD_ZONE_SPEED:
        return None
    return base_vx / base_speed * speed, base_vy / base_speed * speed


def throw_step(
    state: ThrowState,
    dt_raw: float,
    bounds: Bounds,
    params: PhysicsParams,
) -> tuple[ThrowState, bool, bool]:
    """单步积分 + 边缘反弹。返回 (新状态, 本步是否碰边, 是否静止)。"""
    dt = min(max(dt_raw, 0.0), MAX_STEP_DT)
    x, y, vx, vy = state.x, state.y, state.vx, state.vy

    vy += params.gravity * dt
    x += vx * dt
    y += vy * dt
    bounced = False

    if x < bounds.min_x:
        x = bounds.min_x
        vx = abs(vx) * params.restitution
        bounced = True
    elif x > bounds.max_x:
        x = bounds.max_x
        vx = -abs(vx) * params.restitution
        bounced = True

    if y < bounds.min_y:
        if params.ceiling_bounce:
            y = bounds.min_y
            vy = abs(vy) * params.restitution
            bounced = True
    elif y >= bounds.max_y:
        y = bounds.max_y
        vx *= max(0.0, 1.0 - params.ground_friction * dt)
        if abs(vy) < REST_VY:
            vy = 0.0
        else:
            vy = -abs(vy) * params.restitution
        bounced = True

    speed = math.hypot(vx, vy)
    at_rest = (y >= bounds.max_y - 1 and abs(vy) < 1 and abs(vx) < REST_VX) or (
        bounced and speed < REST_VY and abs(vy) < 1
    )
    return ThrowState(x, y, vx, vy), bounced, at_rest


def landing_squash(impact_speed: float) -> float:
    """落地冲击速度 → 下压幅度（越小压得越狠）。轻落只压 SQ_LIGHT_SQUASH。"""
    t = min(max((abs(impact_speed) - SQ_SOFT_SPEED) / (SQ_HARD_SPEED - SQ_SOFT_SPEED), 0.0), 1.0)
    return min(SQ_LIGHT_SQUASH, 1.0 - t * (1.0 - SQ_MAX_SQUASH))


def squash_scale(u: float, squash: float = SQ_SQUASH) -> float:
    """Q 弹挤压曲线：u∈[0,1] → scaleY。下压 ease-in，回弹 easeOutBack 带轻微过冲。"""
    if u < 0.45:
        p = u / 0.45
        return 1.0 - (1.0 - squash) * p * p
    p = (u - 0.45) / 0.55
    c1 = 1.70158
    c3 = c1 + 1.0
    f = 1.0 + c3 * (p - 1.0) ** 3 + c1 * (p - 1.0) ** 2
    return min(SQ_MAX_STRETCH, squash + (1.0 - squash) * max(f, 0.0))


def plan_move(
    *,
    x: float,
    area_left: float,
    area_width: float,
    half_w: float,
    direction: int,
    min_dist: float,
    max_dist: float,
    margin: float,
    side_allow: float = 0.0,
) -> MovePlan | None:
    """规划一次左右走位：返回目标窗口左上角 x；越界返回 None（本次不走）。

    距离按配置的 px 值直接使用（已由调用方按 size/基准 缩放）。
    """
    distance = random.uniform(min_dist, max_dist)
    cx = x + half_w
    target_cx = cx + direction * distance

    body_half = half_w - side_allow
    left_bound = area_left + margin + body_half
    right_bound = area_left + area_width - margin - body_half
    if target_cx < left_bound or target_cx > right_bound:
        return None
    return MovePlan(target_x=target_cx - half_w, distance=distance)
