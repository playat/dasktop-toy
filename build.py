"""把桌宠打包成当前系统的可执行产物（PyInstaller）。

    .venv\\Scripts\\python.exe build.py              # 目录版 dist/DshPet.exe
    .venv\\Scripts\\python.exe build.py --onefile    # 单文件 dist/DshPet.exe
    .venv\\Scripts\\python.exe build.py --console    # 保留控制台窗口（看实时日志）
    .venv\\Scripts\\python.exe build.py --clean      # 先删 build/ 和 dist/ 里的旧产物

两个目录分工（别混）：

    assets/            项目文件：素材的「源」，进 git，源码模式运行时读它
    dist/              构建产物：一条命令就能从工程文件整个重建，可以整包搬走

产物结构（目录版，默认）—— **直接平铺在 dist/ 里，没有 DshPet 那一层**：

    dist/
      DshPet.exe
      _internal/          运行时依赖（Qt / av / numpy）
      config.jsonc        ← 从根目录复制一份，改完右键「重载配置」
      assets/             ← 按根 assets/ 整个镜像（多出来的会删掉），可移植
      dsh-pet.log         首次运行后生成（打包版没有控制台，日志在这）

镜像而不是「就地用 dist/assets」：dist 里的东西随时可以删干净重来，
素材的真身在工程根 assets/（有 git 历史）。改完素材跑一次 build，
dist/ 就是一个自洽、能直接拷给别人跑的目录。

为什么不把素材和配置塞进 exe：64MB，塞进去每次启动都要解压到临时目录，
启动慢一倍，而且换一套素材就得重编译 —— 外置才符合这东西的用法。

打包前会从素材首帧抠一张图标（alpha 噪声先清掉，否则裁切框会退化）。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
STAGE_DIST = BUILD_DIR / "pkg"          # PyInstaller 的临时产物目录，构建完整体搬进 dist/
SRC_ASSETS = ROOT / "assets"            # 项目文件：素材的源（进 git）
ASSETS_DIR = DIST_DIR / "assets"        # 构建产物：按 SRC_ASSETS 镜像出来，可整包搬走
NAME = "DshPet"
EXE_NAME = NAME + (".exe" if sys.platform == "win32" else "")
MIRROR = "https://mirrors.cloud.tencent.com/pypi/simple"


# ---------------------------------------------------------------- 小工具

def _rmtree(path: Path) -> None:
    """删目录，只读文件先放开权限再删（PyInstaller 产物偶尔带只读位）。"""
    if not path.exists():
        return
    if path.is_dir():
        def _retry(func, p, exc):
            try:
                Path(p).chmod(0o700)
                func(p)
            except OSError:
                raise exc  # 放开权限也删不掉（被占用）就往外报，交给调用方给人话

        # 3.12 起 onerror 改名成 onexc，两种都带上防版本差异
        try:
            shutil.rmtree(path, onexc=_retry)
        except TypeError:
            shutil.rmtree(path, onerror=_retry)
    else:
        path.unlink()


def _has_webm(folder: Path) -> bool:
    try:
        return folder.is_dir() and next(folder.rglob("*.webm"), None) is not None
    except OSError:
        return False


def _webm_root() -> Path:
    """素材目录：工程根 assets/webm（源）；没有才退到 dist/assets/webm（老工作区剩的）。"""
    for cand in (SRC_ASSETS / "webm", ASSETS_DIR / "webm"):
        if _has_webm(cand):
            return cand
    return SRC_ASSETS / "webm"


# ---------------------------------------------------------------- 图标

def _first_frame_rgba(path: Path):
    """解第一帧 RGBA。注意必须指定 libvpx-vp9，默认解码器会丢掉 alpha。"""
    import av

    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
        dec = av.CodecContext.create("libvpx-vp9", "r")
        dec.width = stream.codec_context.width
        dec.height = stream.codec_context.height
        dec.pix_fmt = "yuva420p"
        dec.extradata = bytes(stream.codec_context.extradata or b"")
        dec.open()
        for packet in container.demux(stream):
            if not packet.size:
                continue
            for frame in dec.decode(packet):
                return frame.to_ndarray(format="rgba")
    finally:
        container.close()
    return None


def make_icon() -> Path | None:
    """从待机动画首帧生成 build/pet.ico；任何一步失败都只是没有图标，不阻断构建。

    素材的「待机」是由目录决定的（`AnimationSet.role_keys("idle")` 读 assets/webm/待机/），
    不再从 config.jsonc 的池子里取 —— 配置里已经没有素材清单了。
    """
    try:
        import numpy as np
        from PIL import Image

        from config import PetConfig
        from player import AnimationSet

        cfg = PetConfig.load(ROOT / "config.jsonc")
        anims = AnimationSet(_webm_root(), cfg)
        # 优先待机（站着不动的姿态当图标最合适），没有就退到随机池、再退到任意素材
        keys = anims.role_keys("idle") or anims.random_keys or list(anims.index)
        if not keys:
            print("[图标] 跳过（素材目录里没有可用素材）")
            return None
        src = anims.path_of(keys[0])
        if src is None or not src.exists():
            return None
        print(f"      取首帧：{src.relative_to(ROOT)}")

        arr = _first_frame_rgba(src)
        if arr is None:
            return None

        alpha = arr[..., 3].copy()
        alpha[alpha < 16] = 0  # 背景是 1~15 的编码噪声，不清会算出一个满屏 bbox
        ys, xs = np.where(alpha > 0)
        if len(ys) == 0:
            return None

        body = Image.fromarray(arr[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1], "RGBA")
        body.thumbnail((240, 240), Image.LANCZOS)
        canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        canvas.paste(body, ((256 - body.width) // 2, (256 - body.height) // 2), body)

        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        out = BUILD_DIR / "pet.ico"
        canvas.save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"[图标] 跳过（{type(exc).__name__}: {exc}）")
        return None


# ---------------------------------------------------------------- 布局

def icon_state(exe: Path, ico: Path) -> tuple[int, int] | None:
    """检查 exe 里嵌的图标是不是我们这张 ico。返回 (命中数, ico 里的图数)，None = 读不了。

    为什么不能数资源个数：不给 --icon 时 PyInstaller 也会塞自己的默认图标（16/32/48/256），
    资源永远存在，看不出区别。而 RT_ICON 里存的就是 ico 每个尺寸的原始图像块，
    所以拿 ico 的图像数据去 exe 里做子串匹配是可靠的判据。
    """
    try:
        raw = ico.read_bytes()
        if len(raw) < 6 or raw[:4] != b"\x00\x00\x01\x00":  # ICONDIR
            return None
        count = int.from_bytes(raw[4:6], "little")
        payloads = []
        for i in range(count):
            e = 6 + i * 16
            size = int.from_bytes(raw[e + 8: e + 12], "little")
            off = int.from_bytes(raw[e + 12: e + 16], "little")
            if size and off + size <= len(raw):
                payloads.append(raw[off: off + size])
        if not payloads:
            return None
        data = exe.read_bytes()
        return sum(1 for p in payloads if len(p) >= 256 and p in data), len(payloads)
    except Exception:  # noqa: BLE001
        return None


def ensure_pyinstaller() -> None:
    """只检查，**不替用户装**：环境归用户自己管。

    以前这里是「没有就静默 pip install」，等于把装包副作用藏进打包流程；
    现在缺了就直接说清楚哪条命令要跑 —— 编译只做编译。
    """
    try:
        import PyInstaller  # noqa: F401
    except ModuleNotFoundError:
        print("\n[失败] 当前解释器里没有 PyInstaller：")
        print(f"       {sys.executable}")
        print("\n  装上再重跑本命令（只需一次）：")
        print(f'       "{sys.executable}" -m pip install -i {MIRROR} pyinstaller')
        raise SystemExit(1)


def _mirror(src: Path, dst: Path) -> tuple[int, int]:
    """把 src 镜像到 dst：缺的/变了的拷过去，dst 里多出来的删掉。返回 (写入数, 删除数)。

    为什么不用「删干净再 copytree」：中途被打断就会剩个半空的 dist/assets，
    而镜像是一步步对齐的，任何时刻中断最多是「新版没拷完」，旧文件还在。
    """
    written = deleted = 0
    keep: set[Path] = set()
    for f in sorted(src.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(src)
        keep.add(rel)
        target = dst / rel
        try:
            s_stat = f.stat()
            if target.exists():
                t_stat = target.stat()
                if t_stat.st_size == s_stat.st_size and t_stat.st_mtime_ns >= s_stat.st_mtime_ns:
                    continue  # 大小一样且不比源旧，当作已同步
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            written += 1
        except OSError as exc:
            print(f"      [警告] 复制失败 {rel}：{exc}")
    # 源里没有的（删掉的旧素材、改了名的）在产物里也不该留
    for f in list(dst.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(dst)
        if rel in keep:
            continue
        try:
            f.unlink()
            deleted += 1
        except OSError as exc:
            print(f"      [警告] 删不掉多余文件 {rel}：{exc}")
    # 删掉清空后的旧分类目录。**先确认真的空**：`rmdir` 按理说对非空目录会报错，
    # 但在某些环境里（实测过一个沙箱里的 F: 盘）它被实现成「递归删除」——
    # 真踩过：这一步把刚镜像好的 162 个素材全删了，返回值还是 0。
    # 有了 any(iterdir()) 这道闸，非空目录永远轮不到 rmdir，怎么都不会误删。
    for d in sorted((p for p in dst.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            if any(d.iterdir()):
                continue
            d.rmdir()
        except OSError:
            pass
    return written, deleted


def sync_assets() -> Path:
    """把工程 assets/ 整个镜像进 dist/assets/。

    dist 是构建产物：删掉它、换台机器重新 build，结果应该一模一样。
    所以每次打包都按 assets/ 对齐一遍，而不是「dist 里有了就沿用」——
    后者会让改了素材却忘了重打包的 dist 悄悄停在旧版本。
    """
    if not SRC_ASSETS.is_dir():
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        print(f"      [提示] 没有 {SRC_ASSETS.name}\\ 素材目录 —— 先跑 python make_pet_webm.py 生成")
        return ASSETS_DIR
    if not _has_webm(SRC_ASSETS):
        print(f"      [提示] {SRC_ASSETS.name}\\ 里还没有 webm 素材 —— 先跑 python make_pet_webm.py 生成")

    print("      镜像素材：assets/ → dist/assets/ ...")
    written, deleted = _mirror(SRC_ASSETS, ASSETS_DIR)
    total = sum(1 for f in ASSETS_DIR.rglob("*") if f.is_file())
    detail = ""
    if written or deleted:
        parts = []
        if written:
            parts.append(f"更新 {written}")
        if deleted:
            parts.append(f"清掉多余 {deleted}")
        detail = "（" + "，".join(parts) + "）"
    print(f"      {total} 个文件{detail or '（已是最新）'}")
    return ASSETS_DIR


def _covers(base: Path, other: Path) -> bool:
    """other 里每个文件，base 里都有同相对路径的版本吗（用来判断老产物能不能安全删）。"""
    try:
        for f in other.rglob("*"):
            if f.is_file() and not (base / f.relative_to(other)).exists():
                return False
    except OSError:
        return False
    return True


def drop_old_layout() -> None:
    """老布局的 dist/DshPet/ 这一层已经废弃；里面的素材在 assets/ 里都有就直接清掉。"""
    old = DIST_DIR / NAME
    if not old.is_dir():
        return
    legacy = old / "assets"
    if _has_webm(legacy) and not _covers(SRC_ASSETS, legacy):
        print(f"      [警告] dist/{NAME}/assets 里有 assets/ 没有的素材，本次保留（确认后可手删）")
        return
    try:
        _rmtree(old)
    except OSError as exc:
        print(f"      [警告] 删不掉老布局 dist/{NAME}/：{exc}")
        return
    print(f"      已移除老布局 dist/{NAME}/")


def flatten(onefile: bool) -> Path:
    """把 PyInstaller 的产物搬到 dist/ 根：不留 DshPet/ 这层，配置摆到 exe 旁边。

    PyInstaller 只往 STAGE_DIST 里写，素材（dist/assets）在打包前已经镜像好了，
    搬过来时碰不到它。
    """
    built = STAGE_DIST if onefile else STAGE_DIST / NAME
    exe = built / EXE_NAME
    if not exe.exists():
        print(f"\n[失败] 没找到 PyInstaller 产物：{exe}")
        return exe

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    # 上次的构建件先清（_internal 换版本时必须整个换掉）
    for name in (EXE_NAME, "_internal"):
        p = DIST_DIR / name
        if p.is_dir():
            _rmtree(p)
        elif p.exists():
            p.unlink()

    for item in sorted(built.iterdir()):
        dst = DIST_DIR / item.name
        if dst.exists():
            _rmtree(dst)
        shutil.move(str(item), str(dst))

    _rmtree(STAGE_DIST)  # 临时目录整个清掉
    shutil.copy2(ROOT / "config.jsonc", DIST_DIR / "config.jsonc")
    return DIST_DIR / EXE_NAME


def _busy_hint(exc: OSError) -> int:
    """dist 里的东西正被占用时给一句人话，别甩 traceback。"""
    print(f"\n[失败] 文件被占用：{exc}")
    print("      多半是桌宠还开着（dist\\DshPet.exe 正在运行）。")
    print("      先从托盘右键「退出」把它关掉，再重跑一次 build。")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="打包桌宠为可执行文件")
    ap.add_argument("--onefile", action="store_true", help="单文件 exe（启动慢，体积集中）")
    ap.add_argument("--console", action="store_true", help="保留控制台窗口，方便看日志")
    ap.add_argument("--clean", action="store_true", help="先删 build/ 和 dist/ 里的旧产物")
    ap.add_argument("--no-icon", action="store_true", help="不生成图标")
    args = ap.parse_args()

    if args.clean:
        # 只清构建件。dist/assets 留着不删：它下一步会被 assets/ 镜像对齐，
        # 删了只是白白多拷 60MB（真要删就直接删整个 dist/）。
        print("[0/5] 清理 build/ 与 dist/ 里的旧构建件 ...")
        try:
            _rmtree(BUILD_DIR)
            for name in (EXE_NAME, "_internal"):
                p = DIST_DIR / name
                if p.is_dir():
                    _rmtree(p)
                elif p.exists():
                    p.unlink()
        except OSError as exc:
            return _busy_hint(exc)

    print("[1/5] 检查 PyInstaller ...")
    ensure_pyinstaller()

    print("[2/5] 镜像素材到 dist/assets ...")
    try:
        sync_assets()
        drop_old_layout()
    except OSError as exc:
        return _busy_hint(exc)

    print("[3/5] 生成图标 ...")
    icon = None if args.no_icon else make_icon()
    print(f"      {icon.relative_to(ROOT) if icon else '无（exe 将用默认图标）'}")

    print("[4/5] PyInstaller 打包中（首次约 1~2 分钟）...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        # 注意：不要加 --clean。它会让 PyInstaller 去清 %LOCALAPPDATA%\pyinstaller 缓存，
        # 一次删上百个文件，在受限环境里会被安全策略拦下导致构建直接失败。
        # 要干净构建就用本脚本的 --clean（只删项目内的构建件）。
        "--onefile" if args.onefile else "--onedir",
        "--windowed" if not args.console else "--console",
        "--name", NAME,
        # 先落到 build/pkg，最后整体搬到 dist/ 根 —— 这样 dist/ 里就没有 DshPet/ 这一层
        "--distpath", str(STAGE_DIST),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        # av 自带 ffmpeg 的 dll，不整包收进来运行时会找不到 libvpx
        "--collect-all", "av",
        # zstandard 是编译扩展（还带 backend_c/backend_rust 子模块），整包收更保险：
        # 收不全的话「读 dsh 会话日志」在打包版里会静默降级
        "--collect-all", "zstandard",
        # 运行时不碰这两个，收进去纯浪费体积
        "--exclude-module", "PIL",
        "--exclude-module", "tkinter",
    ]
    if icon:
        cmd += ["--icon", str(icon)]
    cmd.append(str(ROOT / "main.py"))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("\n[失败] PyInstaller 出错，看上面的日志")
        return result.returncode

    print("[5/5] 搬到 dist/ 根并摆好配置 ...")
    try:
        exe = flatten(args.onefile)
    except OSError as exc:
        return _busy_hint(exc)
    if not exe.exists():
        return 1

    # 读回 exe 确认图标真的嵌进去了（别信「参数传了就等于生效」）
    hit = icon_state(exe, icon) if icon else None
    if hit is None:
        print("      图标校验跳过（读不了 exe / ico）")
    elif hit[0] == hit[1]:
        print(f"      图标已嵌入 exe（{hit[1]} 个尺寸全部命中）")
    elif hit[0]:
        print(f"      [警告] 图标只嵌进去一部分（{hit[0]}/{hit[1]} 个尺寸）")
    else:
        print("      [警告] exe 里没找到这张图标，用的是 PyInstaller 默认图标 —— 试试 build.py --clean")

    size_mb = sum(f.stat().st_size for f in DIST_DIR.rglob("*") if f.is_file()) / 1024 / 1024
    asset_mb = sum(f.stat().st_size for f in ASSETS_DIR.rglob("*") if f.is_file()) / 1024 / 1024
    print(f"\n完成 → {exe}")
    print(f"      整个 dist/ {size_mb:.0f} MB（其中素材 {asset_mb:.0f} MB）")
    print(f"      双击 {exe.name} 启动；想改配置/换素材直接动旁边的文件，不用重编译")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
