"""AI 绿幕视频 → dsh-pet 能直接播放的透明 webm（一条命令跑完整条素材链）。

原来是 4 个脚本 + step01~04 一堆中间目录 + 外部 ffmpeg.exe：
    video/*.mp4 → 去水印 → 抠像 → 归一化 2160×1215 → 转码 640×360 → webm

这里把**同一套算法**合并成「解码 → 处理 → 编码」的单遍管线，不落中间文件。
四步一一对应原脚本，没有改判据：

  1. 去水印（watermark_step01 / fill_nn）
     原理：mask 里标白的像素，用周围非水印像素的颜色填掉。
     改动：原版用 scipy 取「第 k 近非水印像素」；这里改成在水印局部区域做逐圈膨胀内填
     （8 邻域求已填像素均值，一圈 1px）。水印是细笔画，几圈就收敛，效果等价且不需要 scipy。
  2. 抠像（chroma_step02）
     只把「色相 70~170° + 饱和度 ≥0.15 + 明度 ≥0.15」判为背景 → alpha 0，色相两端 6° 羽化。
     刻意不用 chromakey 的 YUV 距离：蓝色/白色角色会被误抠。
  3. 归一化（normalize_step03）
     首尾各 5 帧当站立姿态，量出站立高度 / 水平中位中心 / 脚底 y，再整帧缩放到
     「640×360 画布 + 站立 266.7px + 脚底 y=330」。这与原链路
     （站立 900 @ 2160×1215 → 缩到 640×360）算出来的数值完全一致，只是省掉 2160 中间层。
  4. 转码（encode_thumbs）
     libvpx-vp9 / yuva420p / CRF 40 / 24fps，直接出插件播放的 640×360。

依赖：av（PyAV）、numpy、Pillow —— 都用工作区的 .venv，**不需要装 ffmpeg**。

用法（在工作区根目录跑）：
    .venv\\Scripts\\python.exe make_pet_webm.py --src D:\\ai视频
    # 默认输出到 video/webm，文件名 = 源文件名（= 动画名）
    # --deploy 顺手同步到 assets/webm（运行时直接能播）
    # --out 换目录；--mask 换水印遮罩；--no-watermark 跳过去水印；--jobs 4 并行

素材要求（跟提示词里的「通用前缀」对应）：纯绿幕、16:9、角色全程居中、首尾是标准站立。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
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
RUNTIME_WEBM = ROOT / "assets" / "webm"

# ---- 目标几何（与运行时常量对齐：FEET_Y_RATIO = 330/360） ----
CANVAS_W, CANVAS_H = 640, 360
FEET_Y = 330.0
STANDING_H = 900.0 * CANVAS_W / 2160.0  # 266.67 = 原链路 2160 画布站立 900 折算到 640
FPS = 24
CRF = 40

# ---- 抠像判据（chroma_step02.py 原值） ----
GREEN_HUE_MIN, GREEN_HUE_MAX = 70.0, 170.0
SAT_MIN, VAL_MIN = 0.15, 0.15
HUE_FEATHER = 6.0

# ---- 测量 ----
HEAD_FRAMES = 5
OPAQUE_FLOOR = 10  # alpha > 10 才算身体像素

DEFAULT_MASK = ROOT / "video" / "watermark_mask_v5.mkv"
DEFAULT_OUT = ROOT / "video" / "webm"


# ---------------------------------------------------------------- 去水印


class MaskFiller:
    """水印遮罩：只保留「所有帧 mask 的并集」那一小块，逐帧膨胀内填。"""

    def __init__(self, path: Path, passes: int = 24) -> None:
        self.passes = passes
        cont = av.open(str(path))
        frames: list[np.ndarray] = []
        boxes: list[tuple[int, int, int, int]] = []
        for frame in cont.decode(video=0):
            gray = frame.to_ndarray(format="gray")
            m = gray > 127
            if m.any():
                ys, xs = np.where(m)
                boxes.append((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
            frames.append(m)
        cont.close()
        if not boxes:
            raise RuntimeError(f"遮罩里没有白色像素：{path}")

        pad = 4
        h, w = frames[0].shape
        x0 = max(0, min(b[0] for b in boxes) - pad)
        y0 = max(0, min(b[1] for b in boxes) - pad)
        x1 = min(w, max(b[2] for b in boxes) + pad)
        y1 = min(h, max(b[3] for b in boxes) + pad)
        self.box = (x0, y0, x1, y1)
        self.masks = np.stack([f[y0:y1, x0:x1] for f in frames]) if frames else None
        print(
            f"[mask] 水印区域 {x1 - x0}x{y1 - y0} @({x0},{y0})，{len(frames)} 帧",
            flush=True,
        )

    def apply(self, rgb: np.ndarray, index: int) -> np.ndarray:
        if self.masks is None:
            return rgb
        m = self.masks[min(index, len(self.masks) - 1)]
        if not m.any():
            return rgb
        x0, y0, x1, y1 = self.box
        sub = rgb[y0:y1, x0:x1].astype(np.float32)
        sub = _dilate_fill(sub, m, self.passes)
        rgb[y0:y1, x0:x1] = np.clip(sub, 0, 255).astype(np.uint8)
        return rgb


def _dilate_fill(img: np.ndarray, hole: np.ndarray, passes: int) -> np.ndarray:
    """洞内逐圈用 8 邻域已填像素的均值填充（等价于「从最近的外圈取色」）。"""
    known = ~hole
    todo = hole.copy()
    for _ in range(passes):
        if not todo.any():
            break
        acc = np.zeros_like(img)
        cnt = np.zeros(hole.shape, np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            si = np.roll(np.roll(img, dy, 0), dx, 1)
            sk = np.roll(np.roll(known, dy, 0), dx, 1)
            acc += si * sk[..., None]
            cnt += sk
        fill = todo & (cnt > 0)
        if not fill.any():
            break
        img[fill] = acc[fill] / cnt[fill][:, None]
        known |= fill
        todo &= ~fill
    return img


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


def key_frame(rgb: np.ndarray) -> np.ndarray:
    """绿幕 → alpha。返回 RGBA（uint8）。"""
    hue, sat, val = _rgb_to_hsv(rgb)
    lo, hi = GREEN_HUE_MIN - HUE_FEATHER, GREEN_HUE_MAX + HUE_FEATHER
    in_hue = np.clip((hue - lo) / HUE_FEATHER, 0, 1) * np.clip((hi - hue) / HUE_FEATHER, 0, 1)
    bg = (sat >= SAT_MIN) & (val >= VAL_MIN) & (hue >= lo) & (hue <= hi)
    alpha = np.where(bg, (1.0 - in_hue) * 255.0, 255.0).astype(np.uint8)
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
    src, out_dir, mask_path, passes = args
    dst = out_dir / (src.stem + ".webm")
    t0 = time.time()
    try:
        filler = MaskFiller(mask_path, passes) if mask_path else None
        stats = measure_video(src, filler)
        frames = render(src, dst, stats, filler)
        size_mb = dst.stat().st_size / 1e6
        return (
            src.name,
            f"OK  {frames}帧  {size_mb:.1f}MB  {time.time() - t0:.1f}s  "
            f"站立{stats['height']:.0f}px→{STANDING_H:.0f}px",
        )
    except Exception as exc:  # noqa: BLE001
        return src.name, f"FAIL {type(exc).__name__}: {exc}"


def deploy(out_dir: Path) -> int:
    """把成品 webm 同步到运行时素材目录（文件名即动画 ID，不能改）。"""
    src = out_dir.resolve()
    dst = RUNTIME_WEBM.resolve()
    if src == dst:
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(src.glob("*.webm")):
        shutil.copy2(f, dst / f.name)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 绿幕视频 → dsh-pet 可直接播放的透明 webm")
    ap.add_argument("--src", type=Path, required=True, help="AI 生成的绿幕视频目录（mp4/mov/webm）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"输出目录（默认 {DEFAULT_OUT}）")
    ap.add_argument("--mask", type=Path, default=DEFAULT_MASK, help="水印遮罩 mkv")
    ap.add_argument("--no-watermark", action="store_true", help="跳过去水印（素材无水印时用）")
    ap.add_argument("--passes", type=int, default=24, help="去水印内填圈数（默认 24）")
    ap.add_argument("--jobs", type=int, default=4, help="并行进程数（默认 4）")
    ap.add_argument("--only", default="", help="只处理文件名包含该子串的视频")
    ap.add_argument(
        "--deploy",
        action="store_true",
        help="生成后同步到 assets/webm（运行时直接读）",
    )
    args = ap.parse_args()

    if not args.src.is_dir():
        print(f"[错误] 源目录不存在：{args.src}")
        return 2

    videos = sorted(
        p for p in args.src.iterdir() if p.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv")
    )
    if args.only:
        videos = [p for p in videos if args.only in p.name]
    if not videos:
        print(f"[错误] {args.src} 里没有视频文件")
        return 2

    mask: Path | None = None
    if not args.no_watermark:
        if args.mask.exists():
            mask = args.mask
        else:
            print(f"[提示] 找不到遮罩 {args.mask}，本次跳过去水印（或者加 --no-watermark 明确跳过）")

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"输入 {len(videos)} 个 · 输出 {args.out} · 水印 {'开' if mask else '关'} · 并行 {args.jobs}")

    jobs = [(v, args.out, mask, args.passes) for v in videos]
    total = len(jobs)
    ok = 0
    if args.jobs <= 1:
        results = map(convert_one, jobs)
        for i, (name, status) in enumerate(results, 1):
            print(f"[{i}/{total}] {name}  {status}", flush=True)
            ok += status.startswith("OK")
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            for i, (name, status) in enumerate(ex.map(convert_one, jobs), 1):
                print(f"[{i}/{total}] {name}  {status}", flush=True)
                ok += status.startswith("OK")

    print(f"\n完成 {ok}/{total} → {args.out}")
    if args.deploy:
        copied = deploy(args.out)
        print(f"已同步 {copied} 个到 {RUNTIME_WEBM}")
    else:
        print("提示：成品在 video/webm。加 --deploy 可直接同步到 assets/webm 给运行时用。")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
