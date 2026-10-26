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

用法（在工作区根目录跑，**不带参数就行**）：
    make-webm.cmd                          双击：处理 video\\src 下所有新视频
    .venv\\Scripts\\python.exe make_pet_webm.py

    # 源目录默认 video/src（原始 mp4 就放这里）
    # 输出直接写 assets/webm —— 就是运行时读的素材目录，不用再复制/同步
    # 文件名 = 源文件名（= 动画名）
    # --force   已有成品也全部重做
    # --only 关键字 / --jobs 4 / --out / --mask / --no-watermark

增量规则：递归扫 assets/webm（含所有子目录）里已有的 webm，按文件名（stem）对上就跳过；
源文件比成品新时才重做，且**写回原来那个位置**（你把成品挪进分类子目录也不会被拉回根目录）。
所以反复运行是安全的——只补没做过的。

素材要求（跟提示词里的「通用前缀」对应）：纯绿幕、16:9、角色全程居中、首尾是标准站立。
"""

from __future__ import annotations

import argparse
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
DEFAULT_OUT = RUNTIME_WEBM  # 直接产出到运行时素材目录，省掉「再复制一遍」
DEFAULT_SRC = ROOT / "video" / "src"

VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")
MIN_WEBM_BYTES = 512  # 小于这个数当作空文件/残片（正常成品都在几百 KB）


# ---------------------------------------------------------------- 去水印


class MaskFiller:
    """水印遮罩：每帧裁到「该帧白像素的紧包围盒」，只在那块区域做膨胀内填。

    早先版本取「所有帧的并集」再统一裁，遇到遮罩底部有条整行白线就把整幅 1280×711
    都当成水印区——每个视频白算 90 秒，还平白去动大半张图。改成逐帧紧裁即可。
    """

    def __init__(self, path: Path, passes: int = 24) -> None:
        self.passes = passes
        pad = max(2, min(passes, 24) + 2)  # 留出已知像素环，供内填取色
        self.crops: list[tuple[int, int, int, int, np.ndarray] | None] = []

        cont = av.open(str(path))
        try:
            for frame in cont.decode(video=0):
                m = frame.to_ndarray(format="gray") > 127
                if not m.any():
                    self.crops.append(None)
                    continue
                h, w = m.shape
                ys, xs = np.where(m)
                x0, y0 = max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad)
                x1, y1 = min(w, int(xs.max()) + 1 + pad), min(h, int(ys.max()) + 1 + pad)
                self.crops.append((x0, y0, x1, y1, m[y0:y1, x0:x1].copy()))
        finally:
            cont.close()

        if not self.crops or not any(c is not None for c in self.crops):
            raise RuntimeError(f"遮罩里没有白色像素：{path}")

        boxes = [c[:4] for c in self.crops if c is not None]
        bx0 = min(b[0] for b in boxes)
        by0 = min(b[1] for b in boxes)
        bx1 = max(b[2] for b in boxes)
        by1 = max(b[3] for b in boxes)
        w1 = max(c[2] - c[0] for c in boxes if c is not None)
        h1 = max(c[3] - c[1] for c in boxes if c is not None)
        print(
            f"[mask] {path.name} 水印并集 {bx1 - bx0}x{by1 - by0} @({bx0},{by0})，"
            f"单帧最大 {w1}x{h1}，{len(self.crops)} 帧",
            flush=True,
        )

    def apply(self, rgb: np.ndarray, index: int) -> np.ndarray:
        if not self.crops:
            return rgb
        crop = self.crops[min(index, len(self.crops) - 1)]
        if crop is None:
            return rgb
        x0, y0, x1, y1, m = crop
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
    """src 转到 dst（dst 可能是子目录里的已有成品，原地覆盖以保住用户的目录结构）。"""
    src, dst, mask_path, passes = args
    t0 = time.time()
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名：中途失败不会留下一个看起来"已生成"的残片。
        # 临时文件故意不带 .webm 后缀——否则它会被「递归扫目录」当成一个成品索引进去
        tmp = dst.with_name(dst.stem + ".part")
        filler = MaskFiller(mask_path, passes) if mask_path else None
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

    既然成品直接住在 assets/webm，用户很可能把它拖进分类子目录；只认根目录会
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
        "--out", type=Path, default=DEFAULT_OUT, help=f"输出目录（默认 {DEFAULT_OUT}，即运行时素材目录）"
    )
    ap.add_argument("--mask", type=Path, default=DEFAULT_MASK, help="水印遮罩 mkv")
    ap.add_argument("--no-watermark", action="store_true", help="跳过去水印（素材无水印时用）")
    ap.add_argument("--passes", type=int, default=24, help="去水印内填圈数（默认 24）")
    ap.add_argument("--jobs", type=int, default=4, help="并行进程数（默认 4）")
    ap.add_argument("--only", default="", help="只处理相对路径包含该子串的视频（可写目录名或文件名片段）")
    ap.add_argument("--force", action="store_true", help="已有成品也重新生成")
    # 老习惯的 --deploy：现在默认就产出到 assets/webm，收到当无事发生，别让人卡在这
    ap.add_argument("--deploy", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

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
        print(f"  最省事的做法：把视频复制到 {DEFAULT_SRC}，然后直接跑 make-webm.cmd（不带参数）")
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
    print(f" 输出到   {out}" + ("（运行时素材目录，做完即可播）" if out.resolve() == RUNTIME_WEBM.resolve() else ""))
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
    jobs = [(v, dst, mask, args.passes) for v, dst in plan]
    total = len(jobs)
    ok = 0
    failed: list[tuple[str, str]] = []
    if args.jobs <= 1:
        results = map(convert_one, jobs)
        for i, (name, status) in enumerate(results, 1):
            print(f"[{i}/{total}] {name}  {status}", flush=True)
            if status.startswith("OK"):
                ok += 1
            else:
                failed.append((name, status))
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            for i, (name, status) in enumerate(ex.map(convert_one, jobs), 1):
                print(f"[{i}/{total}] {name}  {status}", flush=True)
                if status.startswith("OK"):
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
        print("已直接写入素材目录。宠物正在运行的话，右键菜单会自动看到新动作（无需重启）。")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
