"""AI 绿幕视频 → dsh-pet 能直接播放的透明 webm（一条命令跑完整条素材链）。

原来是 4 个脚本 + step01~04 一堆中间目录 + 外部 ffmpeg.exe：
    video/*.mp4 → 去水印 → 抠像 → 归一化 2160×1215 → 转码 640×360 → webm

这里把**同一套算法**合并成「解码 → 处理 → 编码」的单遍管线，不落中间文件。
四步一一对应原脚本，没有改判据：

  1. 去水印（watermark_step01 / fill_nn + 自动识别平台水印）
     mask 路线：mask 里标白的像素，用周围非水印像素的颜色填掉。
     原版用 scipy 取「第 k 近非水印像素」；这里改成：按行段切成小块 →
     洞先膨胀几 px → 再沿「明显偏离底色」的像素自适应长开 → 洞内做上下左右
     最近像素的距离加权插值。纯 numpy，不需要 scipy。
     关键点：遮罩只盖住笔画实心部分，边缘那圈半透明像素不一起挖掉就会留一圈淡字影
     （就是「去不干净」）。也不能整帧取并集 —— 遮罩底部有整行白线，并集等于整个画面。
     自动路线（_strip_watermark）：mask 只盖住部分水印时（常见于左上角那串字），
     按「亮 + 无彩 + 扁长 + 小 + 不在角色身上」认出白字块，直接判透明。
  2. 抠像（chroma_step02）
     只把「色相 70~170° + 饱和度 ≥0.07 + 明度 ≥0.15 + 绿通道占优」判为背景 → alpha 0，
     色相两端 6° 羽化。饱和度下限比上游（0.15）低，否则绿幕被提亮处、水印填充处
     会留下灰白边/灰白点；「绿通道占优」保证白/浅灰角色不被误抠。
     刻意不用 chromakey 的 YUV 距离：蓝色/白色角色会被误抠。
  3. 归一化（normalize_step03）
     首尾各 5 帧当站立姿态，量出站立高度 / 水平中位中心 / 脚底 y，再整帧缩放到
     「640×360 画布 + 站立 266.7px + 脚底 y=330」。这与原链路
     （站立 900 @ 2160×1215 → 缩到 640×360）算出来的数值完全一致，只是省掉 2160 中间层。
  4. 转码（encode_thumbs）
     libvpx-vp9 / yuva420p / CRF 40 / 24fps，直接出插件播放的 640×360。

依赖：av（PyAV）、numpy、Pillow —— 都用工作区的 .venv，**不需要装 ffmpeg**。

用法（在工作区根目录跑，**不带参数就行**）：
    python make_pet_webm.py                处理 video\\src 下所有新视频（已存在的跳过）
    .venv\\Scripts\\python.exe make_pet_webm.py

    # 源目录默认 video/src（原始 mp4 就放这里）
    # 输出写工程根的 assets/webm —— 那是项目文件（进 git），源码模式运行时直接读它
    # 打包时 assets/ 会被整个镜像进 dist/assets/（build.py 负责），那一份才是分发用的产物
    # 文件名 = 源文件名（= 动画名）
    # --force   已有成品也全部重做
    # --only 关键字 / --jobs 4 / --out / --mask / --no-watermark / --no-sync

增量规则：递归扫 assets/webm（含所有子目录）里已有的 webm，按文件名（stem）对上就跳过；
源文件比成品新时才重做，且**写回原来那个位置**（你把成品挪进分类子目录也不会被拉回根目录）。
所以反复运行是安全的——只补没做过的。

素材要求（跟提示词里的「通用前缀」对应）：纯绿幕、16:9、角色全程居中、首尾是标准站立。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

try:
    import av
    import numpy as np
    from PIL import Image
except ModuleNotFoundError as exc:  # 用对解释器就不该走到这
    sys.stderr.write(
        f"\n[错误] 缺少依赖 {exc.name}。用工作区的虚拟环境跑：\n"
        "    .venv\\Scripts\\python.exe make_pet_webm.py --src <视频目录>\n\n"
    )
    raise SystemExit(1)

ROOT = Path(__file__).resolve().parent
# 两个目录分工明确，别混：
#   assets/webm      项目文件（进 git）—— 生成工具写这里，源码模式运行时读这里
#   dist/assets/webm 打包产物（不进 git）—— build.py 按 assets/ 整个镜像出来，可整包搬走
ASSET_WEBM = ROOT / "assets" / "webm"
DIST_WEBM = ROOT / "dist" / "assets" / "webm"

# ---- 目标几何（与运行时常量对齐：FEET_Y_RATIO = 330/360） ----
CANVAS_W, CANVAS_H = 640, 360
FEET_Y = 330.0
STANDING_H = 900.0 * CANVAS_W / 2160.0  # 266.67 = 原链路 2160 画布站立 900 折算到 640
FPS = 24
CRF = 40

# ---- 抠像判据 ----
# 上游 chroma_step02.py 用「色相 + 饱和度 ≥0.15 + 明度 ≥0.15」。饱和度下限太松了：
# 绿幕被角色光晕提亮、或水印被 4 向插值填成淡绿之后，饱和度会掉到 0.08~0.10，
# 原判据把这些像素留成灰白边/灰白点（角色轮廓一圈、水印位置一片）。
# 这里降到 0.07，并补一条「绿通道确实占优」——低饱和的白/浅灰角色绿不是最大通道，
# 不会被削，所以两者可以同时用。
GREEN_HUE_MIN, GREEN_HUE_MAX = 70.0, 170.0
SAT_MIN, VAL_MIN = 0.07, 0.15
GREEN_EXCESS_MIN = 2  # G 比 max(R,B) 至少高这么多才算「还是绿幕」
HUE_FEATHER = 6.0

# ---- 测量 ----
HEAD_FRAMES = 5
OPAQUE_FLOOR = 10  # alpha > 10 才算身体像素

DEFAULT_MASK = ROOT / "video" / "watermark_mask_v5.mkv"
DEFAULT_OUT = ASSET_WEBM  # 产出到工程素材目录（项目文件），dist 那份由 build.py 镜像
DEFAULT_SRC = ROOT / "video" / "src"

VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")
MIN_WEBM_BYTES = 512  # 小于这个数当作空文件/残片（正常成品都在几百 KB）


# ---------------------------------------------------------------- 去水印


class MaskFiller:
    """水印遮罩：每帧把白像素切块，逐块外扩后做「4 向插值」填充。

    两个关键点（都是实测踩出来的）：

    1. **洞要先膨胀再填**。遮罩只盖住笔画的实心部分，抗锯齿边缘那 1~3px 半透明像素
       落在遮罩外；不一起挖掉的话，填完会留下一圈淡淡的字影 —— 看着就是「去不干净」。
    2. **必须分块，不能取整帧并集**。遮罩底部常有一条整行白线（水印条），
       它和顶部文字差了几百行，并集 box 就是整个画面：既按 1280×720 算 N 圈（260s/视频），
       又只能从相邻像素一圈圈往里渗，把边缘的脏色一起拖进洞中央。

    现在按「连续有白像素的行段」切块，每块裁一个刚好带 pad 的小图，
    用上下左右最近的非洞像素做距离加权插值（等价于 scipy 的最近邻内填，但纯 numpy）。
    """

    def __init__(self, path: Path, grow: int = 4) -> None:
        self.grow = max(0, grow)
        self.fringe = 14  # 自适应补长的最大圈数（见 _grow_into_fringe）
        self.regions: list[list[tuple[int, int, int, int, np.ndarray]]] = []

        cont = av.open(str(path))
        try:
            for frame in cont.decode(video=0):
                m = frame.to_ndarray(format="gray") > 127
                if m.any() and self.grow:
                    m = _dilate(m, self.grow)
                # pad 要留出「干净」的一圈供取色：自适应补长最多再吃 fringe 圈
                self.regions.append(_split_regions(m, pad=self.grow + self.fringe) if m.any() else [])
        finally:
            cont.close()

        if not self.regions or not any(self.regions):
            raise RuntimeError(f"遮罩里没有白色像素：{path}")

        counts = [len(r) for r in self.regions]
        boxes = [b for regs in self.regions for b in regs]
        big = max(boxes, key=lambda t: (t[2] - t[0]) * (t[3] - t[1]))
        print(
            f"[mask] {path.name} 基础外扩 {self.grow}px + 自适应 {self.fringe}px · "
            f"每帧 {min(counts)}~{max(counts)} 块 · 最大块 {big[2] - big[0]}x{big[3] - big[1]} "
            f"@({big[0]},{big[1]}) · {len(self.regions)} 帧",
            flush=True,
        )

    def apply(self, rgb: np.ndarray, index: int) -> np.ndarray:
        if not self.regions:
            return rgb
        bg = _dominant_color(rgb)
        for x0, y0, x1, y1, m in self.regions[min(index, len(self.regions) - 1)]:
            sub = rgb[y0:y1, x0:x1]
            hole = _grow_into_fringe(m, sub, self.fringe, bg)
            rgb[y0:y1, x0:x1] = _fill_4dir(sub, hole)
        return rgb


def _dominant_color(rgb: np.ndarray) -> np.ndarray:
    """整帧主色 —— 绿幕视频里就是背景色。隔 8 取样取中位数，角色占不到一半所以稳。"""
    return np.median(rgb[::8, ::8].reshape(-1, 3).astype(np.float32), axis=0)


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """4 邻域膨胀 r 圈，用来吃掉水印边缘的抗锯齿残影。"""
    out = mask
    for _ in range(r):
        p = np.pad(out, 1, mode="constant", constant_values=False)
        out = p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
    return out


def _split_regions(
    mask: np.ndarray, pad: int, gap: int = 4
) -> list[tuple[int, int, int, int, np.ndarray]]:
    """按「连续有白像素的行段」切块（行间隔 > gap 就断开），每块裁一个带 pad 的 box。

    顶部文字 + 底部整行白线会被切成两块，各自只算自己那一小片 —— 这就是旧版
    整帧并集慢 260s 的根因。
    """
    h, w = mask.shape
    rows = np.nonzero(mask.any(axis=1))[0]
    if rows.size == 0:
        return []

    runs: list[tuple[int, int]] = []
    start = prev = int(rows[0])
    for r in rows[1:]:
        r = int(r)
        if r - prev > gap:
            runs.append((start, prev))
            start = r
        prev = r
    runs.append((start, prev))

    out = []
    for y0, y1 in runs:
        cols = np.nonzero(mask[y0: y1 + 1].any(axis=0))[0]
        x0, x1 = int(cols[0]), int(cols[-1])
        X0, Y0 = max(0, x0 - pad), max(0, y0 - pad)
        X1, Y1 = min(w, x1 + 1 + pad), min(h, y1 + 1 + pad)
        out.append((X0, Y0, X1, Y1, mask[Y0:Y1, X0:X1].copy()))
    return out


def _grow_into_fringe(
    hole: np.ndarray, rgb: np.ndarray, rounds: int, bg: np.ndarray, thresh: float = 12.0
) -> np.ndarray:
    """把洞沿着「明显偏离底色」的像素继续长开，吃掉水印的半透明发光边。

    遮罩是二值的，只盖住笔画实心部分；边缘那圈半透明像素颜色偏离底色却没被标进来。
    固定外扩治不了：扩少了留一圈淡字影，扩多了会把旁边的角色吃掉。
    这里让它自己长——只往「颜色还不是底色」的像素里长，颜色回到底色就停。

    bg 必须是**整帧主色**而不是裁剪框边缘的中位数：水印常贴着画面底部，
    框边会框到角色，底色估歪了就会把整条角色腿当成水印象素一路吃掉（实测过）。
    """
    if rounds <= 0 or hole.all():
        return hole
    cand = np.abs(rgb.astype(np.float32) - bg).sum(axis=2) > thresh

    out = hole
    for _ in range(rounds):
        p = np.pad(out, 1, mode="constant", constant_values=False)
        near = p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
        grown = near & cand & ~out
        if not grown.any():
            break
        out = out | grown
    return out


def _fill_4dir(img: np.ndarray, hole: np.ndarray) -> np.ndarray:
    """洞内每个像素取「上下左右最近的非洞像素」做距离加权平均。

    比逐圈膨胀好在两点：
    - 洞中央不会出现方向性拖影（膨胀是从四个边同时往里渗，中线会有接缝）；
    - 取色点离洞足够远（洞已膨胀过），不会把水印边缘的残色带进来。
    纯 numpy 的四向 accumulate，一次算完，跟洞的厚度无关。
    """
    h, w = hole.shape
    known = ~hole
    if not known.any() or not hole.any():
        return img

    src = img.astype(np.float32)
    yy = np.broadcast_to(np.arange(h, dtype=np.int32)[:, None], (h, w))
    xx = np.broadcast_to(np.arange(w, dtype=np.int32)[None, :], (h, w))

    # 沿四根轴找「最近的非洞像素」坐标（没有则越界值，靠 ok 掩码剔掉）
    top = np.maximum.accumulate(np.where(known, yy, -1), axis=0)
    bot = np.minimum.accumulate(np.where(known, yy, h)[::-1], axis=0)[::-1]
    left = np.maximum.accumulate(np.where(known, xx, -1), axis=1)
    right = np.minimum.accumulate(np.where(known, xx, w)[:, ::-1], axis=1)[:, ::-1]

    acc = np.zeros((h, w, 3), np.float32)
    wsum = np.zeros((h, w), np.float32)
    picks = (
        (np.clip(top, 0, h - 1), xx, yy - np.clip(top, 0, h - 1), top >= 0),
        (np.clip(bot, 0, h - 1), xx, np.clip(bot, 0, h - 1) - yy, bot < h),
        (yy, np.clip(left, 0, w - 1), xx - np.clip(left, 0, w - 1), left >= 0),
        (yy, np.clip(right, 0, w - 1), np.clip(right, 0, w - 1) - xx, right < w),
    )
    for cy, cx, dist, ok in picks:
        wt = np.where(ok, 1.0 / (dist + 1.0), 0.0)
        acc += src[cy, cx] * wt[..., None]
        wsum += wt

    out = src.copy()
    fill = hole & (wsum > 0)
    out[fill] = acc[fill] / wsum[fill][:, None]
    return np.clip(out, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- 抠像


def _rgb_to_hsv(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rgb = frame.astype(np.float32) / 255.0
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    delta = mx - mn
    nz = delta > 0
    d = np.where(nz, delta, 1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        hr = 60.0 * (((g - b) / d) % 6.0)
        hg = 60.0 * (((b - r) / d) + 2.0)
        hb = 60.0 * (((r - g) / d) + 4.0)
    hue = np.where((mx == r) & nz, hr, np.where((mx == g) & nz, hg, np.where((mx == b) & nz, hb, 0.0)))
    sat = np.where(mx > 0, delta / np.maximum(mx, 1e-6), 0.0)
    return hue, sat, mx


def _strip_watermark(alpha: np.ndarray, val: np.ndarray, sat: np.ndarray) -> np.ndarray:
    """把「平台水印」直接判成透明，返回新的 alpha。

    生成平台爱在画面上打一串半透明白字（豆包是左上、右下各一串，而且时有时无）。
    这类像素很亮却没有颜色，抠像判据不会把它当绿幕，于是整块留成前景 ——
    到桌面上就是一片灰白点；而去水印遮罩常常只盖住其中一处（本项目这份只盖了底部一条）。

    四个条件一起判，宁可漏掉也不碰角色和特效：
      1. 亮 + 低饱和（白字；绿幕是暗绿、角色是有颜色的实体）
      2. 落在角色外接框之外
      3. 单块扁长（宽 ≥ 2.5×高、高 ≤ 画面 12%）—— 横排文字的形状
      4. 单块面积够小（< 2% 画面）—— 排除大片光斑/烟花/泡泡
    """
    h, w = alpha.shape
    flat = (val > 0.70) & (sat < 0.13) & (alpha > 0)
    if not flat.any():
        return alpha

    body = (alpha > 200) & (sat >= 0.13)
    ys, xs = np.where(body)
    if len(ys):  # 角色身上一律不动：白裙子、白发同样是「亮 + 低饱和」
        flat[max(0, int(ys.min()) - 16): int(ys.max()) + 17,
             max(0, int(xs.min()) - 16): int(xs.max()) + 17] = False
    if not flat.any():
        return alpha

    out = alpha
    for bx0, by0, bx1, by1, m in _split_regions(flat, pad=0):
        bw, bh = bx1 - bx0, by1 - by0
        area = int(m.sum())
        if area < 150 or area > 0.02 * h * w:
            continue
        if bh > h * 0.12 or bw < bh * 2.5:
            continue
        if out is alpha:
            out = alpha.copy()
        out[by0:by1, bx0:bx1][m] = 0
    return out


def key_frame(rgb: np.ndarray) -> np.ndarray:
    """绿幕 → alpha。返回 RGBA（uint8）。

    判据 = 色相在绿区 + 明度够 + 饱和度够 + **绿通道确实占优**。

    两个改动都是为了灰边/灰点（见 SAT_MIN 那段注释）：
    - 饱和度下限 0.15 → 0.07：吃掉「绿幕被提亮」和「水印填充成淡绿」的残留；
    - 绿通道占优（G ≥ max(R,B)+2）：白/浅灰角色同样低饱和，但绿不是最大通道，
      不会被削 —— 没有这条，白裙子会被啃掉一片。
    """
    hue, sat, val = _rgb_to_hsv(rgb)
    lo, hi = GREEN_HUE_MIN - HUE_FEATHER, GREEN_HUE_MAX + HUE_FEATHER
    in_hue = np.clip((hue - lo) / HUE_FEATHER, 0, 1) * np.clip((hi - hue) / HUE_FEATHER, 0, 1)
    f = rgb.astype(np.int16)
    green = (f[..., 1] - np.maximum(f[..., 0], f[..., 2])) >= GREEN_EXCESS_MIN
    bg = (sat >= SAT_MIN) & (val >= VAL_MIN) & (hue >= lo) & (hue <= hi) & green
    alpha = np.where(bg, (1.0 - in_hue) * 255.0, 255.0).astype(np.uint8)
    alpha = _strip_watermark(alpha, val, sat)
    return np.dstack([rgb, alpha])


# ---------------------------------------------------------------- 归一化 + 合成


def measure(rgb_frames: list[np.ndarray], filler: MaskFiller | None) -> dict:
    """首尾站立帧 → 站立高度 / 水平中位中心 / 脚底 y。"""
    heights: list[float] = []
    med_xs: list[float] = []
    max_ys: list[float] = []
    for idx, rgb in rgb_frames:
        if filler is not None:
            rgb = filler.apply(rgb.copy(), idx)
        a = key_frame(rgb)[..., 3]
        ys, xs = np.where(a > OPAQUE_FLOOR)
        if len(xs) == 0:
            continue
        heights.append(float(ys.max() - ys.min() + 1))
        med_xs.append(float(np.median(xs)))
        max_ys.append(float(ys.max()))
    if not heights:
        raise RuntimeError("首尾帧里找不到角色（整帧都透明？检查绿幕是否被误判）")
    return {
        "height": sum(heights) / len(heights),
        "center_x": sum(med_xs) / len(med_xs),
        "max_y": sum(max_ys) / len(max_ys),
    }


def compose(rgba: np.ndarray, scale: float, dx: float, dy: float) -> np.ndarray:
    """等比缩放 + 贴到 640×360 透明画布。全程用预乘 alpha，避免透明边发黑。"""
    h, w = rgba.shape[:2]
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))

    a = rgba[..., 3].astype(np.float32) / 255.0
    prem = rgba[..., :3].astype(np.float32) * a[..., None]
    src = Image.fromarray(np.dstack([prem, a * 255.0]).astype(np.uint8), "RGBA")
    resized = np.asarray(src.resize((nw, nh), Image.LANCZOS), dtype=np.float32)
    ra = resized[..., 3]
    rgb = resized[..., :3] * 255.0 / np.maximum(ra, 1.0)[..., None]
    prem2 = rgb * (ra / 255.0)[..., None]

    left, top = int(round(dx)), int(round(dy))
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(CANVAS_W, left + nw), min(CANVAS_H, top + nh)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((CANVAS_H, CANVAS_W, 4), np.uint8)

    sx0, sy0 = x0 - left, y0 - top
    sub_rgb = prem2[sy0 : sy0 + (y1 - y0), sx0 : sx0 + (x1 - x0)]
    sub_a = ra[sy0 : sy0 + (y1 - y0), sx0 : sx0 + (x1 - x0)]

    canvas = np.zeros((CANVAS_H, CANVAS_W, 4), np.float32)
    canvas[y0:y1, x0:x1, :3] = sub_rgb
    canvas[y0:y1, x0:x1, 3] = sub_a
    out_a = canvas[..., 3]
    canvas[..., :3] = canvas[..., :3] * 255.0 / np.maximum(out_a, 1.0)[..., None]
    return np.clip(canvas, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- 编解码


def _open_video(path: Path):
    cont = av.open(str(path))
    stream = next(s for s in cont.streams if s.type == "video")
    return cont, stream


def measure_video(src: Path, filler: MaskFiller | None) -> dict:
    """扫全片：头 5 帧 + 尾 5 帧（索引对齐，方便水印按帧取遮罩）。"""
    cont, stream = _open_video(src)
    head: list[tuple[int, np.ndarray]] = []
    tail: list[tuple[int, np.ndarray]] = []
    try:
        for idx, frame in enumerate(cont.decode(stream)):
            rgb = frame.to_ndarray(format="rgb24")
            if len(head) < HEAD_FRAMES:
                head.append((idx, rgb))
            tail.append((idx, rgb))
            if len(tail) > HEAD_FRAMES:
                tail.pop(0)
    finally:
        cont.close()
    return measure(head + tail, filler)


def render(src: Path, dst: Path, stats: dict, filler: MaskFiller | None) -> int:
    scale = STANDING_H / stats["height"]
    dx = CANVAS_W / 2.0 - stats["center_x"] * scale
    dy = FEET_Y - stats["max_y"] * scale

    container = av.open(str(dst), "w", format="webm")
    stream = container.add_stream("libvpx-vp9", rate=FPS)
    stream.width, stream.height = CANVAS_W, CANVAS_H
    stream.pix_fmt = "yuva420p"
    stream.options = {
        "crf": str(CRF),
        "b:v": "0",
        "deadline": "good",
        "cpu-used": "4",
        "row-mt": "1",
        "auto-alt-ref": "0",
    }

    n = 0
    src_cont, src_stream = _open_video(src)
    try:
        for idx, frame in enumerate(src_cont.decode(src_stream)):
            rgb = frame.to_ndarray(format="rgb24")
            if filler is not None:
                rgb = filler.apply(rgb, idx)
            rgba = key_frame(rgb)
            out = compose(rgba, scale, dx, dy)

            vf = av.VideoFrame.from_ndarray(out, format="rgba").reformat(format="yuva420p")
            vf.pts = n
            n += 1
            for packet in stream.encode(vf):
                container.mux(packet)
    finally:
        src_cont.close()

    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return n


# ---------------------------------------------------------------- 单个任务


def convert_one(args: tuple[Path, Path, Path | None, int]) -> tuple[str, str]:
    """src 转到 dst（dst 可能是子目录里的已有成品，原地覆盖以保住用户的目录结构）。"""
    src, dst, mask_path, grow = args
    t0 = time.time()
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名：中途失败不会留下一个看起来"已生成"的残片。
        # 临时文件故意不带 .webm 后缀——否则它会被「递归扫目录」当成一个成品索引进去
        tmp = dst.with_name(dst.stem + ".part")
        filler = MaskFiller(mask_path, grow) if mask_path else None
        stats = measure_video(src, filler)
        frames = render(src, tmp, stats, filler)
        tmp.replace(dst)
        nbytes = dst.stat().st_size
        size = f"{nbytes / 1e6:.1f}MB" if nbytes >= 1_000_000 else f"{nbytes / 1024:.0f}KB"
        return (
            src.name,
            f"OK  {frames}帧  {size}  {time.time() - t0:.1f}s  "
            f"站立{stats['height']:.0f}px→{STANDING_H:.0f}px",
        )
    except Exception as exc:  # noqa: BLE001
        try:
            dst.with_name(dst.stem + ".part").unlink(missing_ok=True)
        except OSError:
            pass
        return src.name, f"FAIL {type(exc).__name__}: {exc}"


def existing_webm(root: Path) -> dict[str, Path]:
    """递归扫输出目录树里已有的成品：文件名（stem）→ 路径。

    既然成品住在 assets/webm，用户很可能把它拖进分类子目录；只认根目录会
    导致「明明生成过还再做一遍，根目录多出一份重复」。所以整棵树都算数，
    匹配顺序：层数最浅优先、同名同层按路径字典序。
    """
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    try:
        files = [p for p in root.rglob("*.webm") if p.is_file()]
    except OSError:
        return found
    for p in sorted(files, key=lambda q: (len(q.relative_to(root).parts), str(q))):
        try:
            if p.stat().st_size < MIN_WEBM_BYTES:  # 上次失败留下的残片，不算已生成
                continue
        except OSError:
            continue
        found.setdefault(p.stem, p)
    return found


# ---------------------------------------------------------------- 源目录 / 清单


def _clean_path(raw: str) -> Path:
    """容错：拖拽和手敲常带引号、首尾空白、末尾反斜杠，统统去掉。"""
    s = raw.strip().strip('"').strip("'").strip()
    while len(s) > 3 and s[-1] in ("\\", "/"):
        s = s[:-1]
    return Path(s).expanduser()


def _list_videos(folder: Path) -> list[Path]:
    """递归收集视频：支持任意深的子目录树，子目录会被原样镜像到输出目录。

    排序后同一目录的文件自然聚在一起（路径前缀相同），清单里看着是一组一组的。
    跳过隐藏文件和 .part 临时文件（上次中断留下的）。
    """
    return sorted(
        p
        for p in folder.rglob("*")
        if p.is_file()
        and p.suffix.lower() in VIDEO_EXTS
        and not p.name.startswith(".")
        and not p.name.endswith(".part")
    )


def _dedupe_by_stem(videos: list[Path]) -> tuple[list[Path], list[tuple[Path, Path]]]:
    """成品按文件名(stem)索引，所以源树里跨目录同名会互相覆盖 —— 只留第一个。

    返回 (保留的, [(被丢弃的, 先出现的那份)])，调用方负责告警。
    """
    seen: dict[str, Path] = {}
    keep: list[Path] = []
    dups: list[tuple[Path, Path]] = []
    for v in videos:
        first = seen.get(v.stem)
        if first is not None:
            dups.append((v, first))
            continue
        seen[v.stem] = v
        keep.append(v)
    return keep, dups


def _names_brief(items: list[Path], limit: int = 6) -> str:
    shown = [p.name for p in items[:limit]]
    rest = len(items) - len(shown)
    return "、".join(shown) + (f" …（还有 {rest} 个）" if rest > 0 else "")


# ---------------------------------------------------------------- 进度指示


_SPIN = "|/-\\"


def _mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


class Spinner:
    """等视频转码时原地转圈的 loading。转码在子进程里跑，主进程只能等着。

    几个约束：
    - 只在真终端（isatty）里画。输出被重定向到文件/管道时画 \\r 会把日志搅成一行，
      那种情况退回「每完成一个打一行」。
    - 帧只用纯 ASCII `|/-\\`。cmd.exe 默认 cp936，盲文字符（⠋⠙⠹…）不在 GBK 里，
      写出去会 UnicodeEncodeError，而且 .cmd 里救不回来。
    - 画行和打结果都在同一把锁里 —— 否则子进程结果一返回就会和转圈的 `\\r` 撞在一起，
      出现「[3/12] OK ...处理中 2/12」这种半截串行的乱码。
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.done = 0
        self.failed = 0
        self.t0 = time.time()
        self.enabled = self._is_tty()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thr = threading.Thread(target=self._loop, daemon=True)
        # 清行用的空白长度：别超过终端宽度，否则擦除本身就换行了
        try:
            self._width = max(shutil.get_terminal_size((80, 24)).columns - 1, 40)
        except OSError:
            self._width = 79

    @staticmethod
    def _is_tty() -> bool:
        try:
            return bool(sys.stdout and sys.stdout.isatty())
        except Exception:  # noqa: BLE001
            return False

    # ---------- 生命周期 ----------

    def __enter__(self) -> "Spinner":
        if self.enabled:
            self._thr.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self.enabled:
            self._thr.join(timeout=2)
            with self._lock:
                self._wipe()

    # ---------- 内部 ----------

    def _wipe(self) -> None:
        sys.stdout.write("\r" + " " * self._width + "\r")
        sys.stdout.flush()

    def _loop(self) -> None:
        i = 0
        while not self._stop.wait(0.1):  # 0.1s 一格，看着像在转
            i = (i + 1) % len(_SPIN)
            self._draw(_SPIN[i])

    def _draw(self, frame: str) -> None:
        text = f" {frame} 已完成 {self.done}/{self.total} · 已用 {_mmss(time.time() - self.t0)}"
        if self.failed:
            text += f" · 失败 {self.failed}"
        with self._lock:
            sys.stdout.write("\r" + text + " " * 4)
            sys.stdout.flush()

    # ---------- 给主循环用 ----------

    def report(self, idx: int, name: str, status: str) -> bool:
        """落一条结果。返回是否成功。会先把转圈那行擦掉再正常换行输出。"""
        ok = status.startswith("OK")
        self.done += 1
        if not ok:
            self.failed += 1
        label = name if len(name) <= 30 else name[:29] + "…"
        with self._lock:
            if self.enabled:
                self._wipe()
            print(f"[{idx}/{self.total}] {label}  {status}", flush=True)
        return ok


def sync_to_dist(out: Path, targets: list[Path]) -> int:
    """把刚生成的成品顺带复制到 dist/assets/webm，返回复制个数。

    为什么要有这一步：dist/ 是构建产物，正常靠 `build.py` 从 assets/ 整个镜像出来。
    但「宠物开着（跑的是 dist 里那份 exe）+ 又往素材里加了动作」这种情况如果不同步，
    就得为了一个动作重跑一遍打包。所以默认顺手同步一次；它只是让产物跟上源，
    并不是第二份源——下次 build 照样按 assets/ 重建 dist/assets。
    """
    if not DIST_WEBM.is_dir():
        return 0  # 还没打过包，没这个目录就不用管
    try:
        base = ASSET_WEBM.resolve()
        out.resolve().relative_to(base)
    except (OSError, ValueError):
        return 0  # --out 指到别处去了，跟这套目录无关
    copied = 0
    for dst in targets:
        if not dst.exists():
            continue  # 生成失败的不会留下文件（convert_one 先写 .part 再改名）
        try:
            rel = dst.resolve().relative_to(base)
            to = DIST_WEBM / rel
            to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, to)
            copied += 1
        except (OSError, ValueError):
            continue  # 同步失败不影响生成结果，build 时会再对齐
    return copied


def main() -> int:
    ap = argparse.ArgumentParser(
        description="AI 绿幕视频 → dsh-pet 可直接播放的透明 webm（默认增量：做过的不再做）"
    )
    ap.add_argument(
        "--src",
        default=None,
        help=f"原始视频目录（默认 {DEFAULT_SRC}）。支持多深的子目录树，"
        "成品会镜像到输出目录的同名子目录里（缺的目录自动新建）",
    )
    ap.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help=f"输出目录（默认 {DEFAULT_OUT}，即工程素材目录）"
    )
    ap.add_argument("--mask", type=Path, default=DEFAULT_MASK, help="水印遮罩 mkv")
    ap.add_argument("--no-watermark", action="store_true", help="跳过去水印（素材无水印时用）")
    ap.add_argument(
        "--grow",
        type=int,
        default=4,
        help="水印遮罩的基础外扩像素（默认 4）；边缘那圈半透明部分会自动补长，一般不用调",
    )
    ap.add_argument("--passes", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--jobs", type=int, default=4, help="并行进程数（默认 4）")
    ap.add_argument("--only", default="", help="只处理相对路径包含该子串的视频（可写目录名或文件名片段）")
    ap.add_argument("--force", action="store_true", help="已有成品也重新生成")
    ap.add_argument(
        "--no-sync", action="store_true", help="不把成品同步进 dist/assets（默认会同步，前提是那边已有素材目录）"
    )
    # 老习惯的 --deploy：现在默认产出到 assets/webm 并自动同步 dist，收到当无事发生，别让人卡在这
    ap.add_argument("--deploy", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.passes is not None:
        print("[提示] --passes 已经没意义了（填充改成 4 向插值），已忽略；要调边缘用 --grow", flush=True)

    # ---- 1. 定位源目录 ----
    using_default = args.src is None
    src = DEFAULT_SRC if using_default else _clean_path(args.src)
    if using_default:
        src.mkdir(parents=True, exist_ok=True)

    if not src.exists():
        print(f"[错误] 源目录不存在：{src}")
        print("  检查这几点：")
        print("    1. 路径拼错，或带了多余的引号 / 末尾反斜杠（已自动清理过一次）")
        print('    2. 路径含中文或空格时要用引号包住：--src "C:\\我的 视频"')
        print("    3. 目录在受保护位置（如 C:\\Program Files、别的用户目录）→ 需要管理员权限")
        print(f"  最省事的做法：把视频复制到 {DEFAULT_SRC}，然后跑 python make_pet_webm.py（不带参数）")
        return 2
    if not src.is_dir():
        print(f"[错误] {src} 是个文件，--src 要给目录。它所在目录是：{src.parent}")
        return 2
    try:
        videos = _list_videos(src)
    except PermissionError as exc:
        print(f"[错误] 没有权限读取 {src}（{exc}）")
        print(f"  把视频复制到 {DEFAULT_SRC} 再跑，或者换一个你有权限的目录。")
        return 2

    if args.only:
        videos = [p for p in videos if args.only in str(p.relative_to(src))]
    videos, dups = _dedupe_by_stem(videos)
    for dup, first in dups:
        print(
            f"[提示] 同名视频只处理第一个：{first.relative_to(src)}"
            f"（跳过 {dup.relative_to(src)}）"
        )
    if not videos:
        print(f"[错误] {src} 里没有视频文件（支持 {' / '.join(VIDEO_EXTS)}）")
        if using_default:
            print(f"  把 AI 生成的绿幕 mp4 放进这个目录就行：{DEFAULT_SRC}")
        return 2

    # ---- 2. 清单：目标路径镜像源树，递归扫输出目录树判断哪些已有成品 ----
    #   源  video/src/吃吃喝喝/吃饺子.mp4  →  成品 assets/webm/吃吃喝喝/吃饺子.webm
    #   源放在根上（没有子目录）就照旧平铺到 assets/webm 根 —— 老用法完全不变。
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    existing = existing_webm(out)
    plan: list[tuple[Path, Path]] = []  # (源文件, 目标 webm)
    skipped: list[Path] = []
    stale = 0
    nested = 0
    for v in videos:
        mirrored = out / v.relative_to(src).with_suffix(".webm")
        if mirrored.exists():
            dst = mirrored  # 镜像位置已有成品（上次生成的 / 用户自己放的）
        else:
            # 用户可能把成品拖进了别的分类目录 —— 按文件名兜底，原地覆盖，保住他的结构
            dst = existing.get(v.stem) or mirrored
        if dst.parent != out:
            nested += 1
        if not dst.exists():
            plan.append((v, dst))
            continue
        try:
            newer = v.stat().st_mtime > dst.stat().st_mtime + 2
        except OSError:
            newer = True
        if args.force:
            plan.append((v, dst))
        elif newer:
            stale += 1
            plan.append((v, dst))
        else:
            skipped.append(v)

    new_dirs = sorted({d.parent for _, d in plan if not d.parent.exists()})

    # ---- 3. 水印 ----
    mask: Path | None = None
    if not args.no_watermark:
        if args.mask.exists():
            mask = args.mask
        else:
            print(f"[提示] 找不到遮罩 {args.mask}，本次跳过去水印")

    # ---- 4. 打印清单 ----
    line = "-" * 66
    tree_depth = max((len(v.relative_to(src).parts) - 1 for v in videos), default=0)
    print(line)
    print(f" 源目录   {src}" + (f"（递归 {tree_depth} 层子目录）" if tree_depth else "（无子目录）"))
    print(f" 输出到   {out}" + ("（工程素材目录 assets/，源码模式直接读）" if out.resolve() == ASSET_WEBM.resolve() else ""))
    print(f" 视频总数 {len(videos)}")
    print(f" 已生成   {len(skipped)}（跳过）")
    print(f" 待生成   {len(plan)}" + (f"（其中 {stale} 个源文件比成品新，会重做）" if stale else ""))
    if nested:
        print(f" 成品位置 {nested} 个落在子目录里（沿用源树的目录结构，缺的目录会新建）")
    if new_dirs:
        shown = "、".join(d.name for d in new_dirs[:4])
        more = f" …（还有 {len(new_dirs) - 4} 个）" if len(new_dirs) > 4 else ""
        print(f" 新建目录 {len(new_dirs)} 个：{shown}{more}")
    print(f" 去水印   {'开 · ' + mask.name if mask else '关'}")
    print(f" 并行     {args.jobs} 个进程")
    print(line)
    if skipped:
        print(f" 跳过：{_names_brief(skipped)}")

    # 成品落在哪决定它怎么被播（目录名即用途，见 README）
    loose = [d for _, d in plan if d.parent == out]
    if loose:
        print(f" 落点提醒：{len(loose)} 个成品直接落在根目录 → 会进「随机小动作池」")
        print("          想让它们归到待机 / 转向 / 拖拽 / 走位 / 点击回应，")
        print("          就在 video/src 里建同名子目录再放进去，成品会镜像过去。")
    print()

    if not plan:
        print("没有需要生成的视频 —— 全部已有成品。要强制重做加 --force。")
        print("（宠物正在运行的话，右键菜单会自己重扫素材目录，不用重启。）")
        return 0

    # ---- 5. 开跑 ----
    t0 = time.time()
    jobs = [(v, dst, mask, args.grow) for v, dst in plan]
    total = len(jobs)
    ok = 0
    failed: list[tuple[str, str]] = []

    with Spinner(total) as sp:
        if args.jobs <= 1:
            for i, (name, status) in enumerate(map(convert_one, jobs), 1):
                if sp.report(i, name, status):
                    ok += 1
                else:
                    failed.append((name, status))
        else:
            with ProcessPoolExecutor(max_workers=args.jobs) as ex:
                # 用 submit + as_completed，谁先做完先报谁。
                # ex.map 是按提交顺序返回的 —— 第一个视频慢的时候，后面早就做完的
                # 也要一起干等，屏幕上看着像卡死了。
                futs = [ex.submit(convert_one, job) for job in jobs]
                for i, fut in enumerate(as_completed(futs), 1):
                    try:
                        name, status = fut.result()
                    except Exception as exc:  # noqa: BLE001  子进程整体挂掉（不是单个视频失败）
                        name, status = "（子进程异常）", f"FAIL {type(exc).__name__}: {exc}"
                    if sp.report(i, name, status):
                        ok += 1
                    else:
                        failed.append((name, status))

    # ---- 6. 汇总 ----
    print()
    print(line)
    print(
        f" 本次生成 {ok}/{total}"
        + (f" · 失败 {len(failed)}" if failed else "")
        + f" · 用时 {time.time() - t0:.0f}s"
    )
    print(f" 素材目录累计 {len(existing_webm(out))} 个 webm → {out}")
    if failed:
        print(" 失败清单（常见原因：遮罩尺寸和视频不一致、绿幕不纯、首尾帧看不到角色）：")
        for name, status in failed:
            print(f"   - {name}  {status}")
    print(line)

    if ok:
        print("已写入工程素材目录 assets/webm（项目文件）。")
        print("  跑源码（run-pet.cmd 或 python main.py）：右键菜单会自动重扫，不用重启就能看到新动作。")
        synced = 0 if args.no_sync else sync_to_dist(out, [d for _, d in plan])
        if synced:
            print(f"  顺带同步了 {synced} 个到 dist/assets/webm（打包产物那侧也能直接用）。")
        elif not args.no_sync and out.resolve() == ASSET_WEBM.resolve():
            print("  想更新可分发版本：跑一次 python build.py 把 assets/ 整个镜像进 dist/。")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
