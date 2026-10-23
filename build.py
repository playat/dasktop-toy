"""把桌宠打包成当前系统的可执行产物（PyInstaller）。

    .venv\\Scripts\\python.exe build.py              # 目录版 dist/DshPet/DshPet.exe
    .venv\\Scripts\\python.exe build.py --onefile    # 单文件 dist/DshPet.exe
    .venv\\Scripts\\python.exe build.py --console    # 保留控制台窗口（看实时日志）
    .venv\\Scripts\\python.exe build.py --clean      # 先删 build/ 和 dist/

产物结构（目录版，默认）：

    dist/DshPet/
      DshPet.exe
      _internal/          运行时依赖（Qt / av / numpy）
      config.jsonc        ← 外置，直接改，改完右键「重载配置」
      assets/webm/        ← 外置，换素材不用重新编译
      dsh-pet.log         首次运行后生成（打包版没有控制台，日志在这）

为什么不把素材和配置塞进 exe：53MB，塞进去每次启动都要解压到临时目录，
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
NAME = "DshPet"
MIRROR = "https://mirrors.cloud.tencent.com/pypi/simple"


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
    """从待机动画首帧生成 build/pet.ico；任何一步失败都只是没有图标，不阻断构建。"""
    try:
        import numpy as np
        from PIL import Image

        from config import PetConfig

        cfg = PetConfig.load(ROOT / "config.jsonc")
        name = cfg.pools.idle[0] if cfg.pools.idle else None
        if name is None:
            return None
        src = ROOT / "assets" / "webm" / f"{name}.webm"
        if not src.exists():
            return None

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


# ---------------------------------------------------------------- 构建

def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ModuleNotFoundError:
        print("[1/4] 没装 PyInstaller，先装一个 ...")
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install", "-q",
                "--disable-pip-version-check", "-i", MIRROR, "pyinstaller",
            ],
            check=True,
        )
        print("      ok")


def stage(out_root: Path) -> None:
    """把配置和素材摆到 exe 旁边（外置，可改可换）。"""
    shutil.copy2(ROOT / "config.jsonc", out_root / "config.jsonc")
    dst = out_root / "assets"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(ROOT / "assets", dst)


def main() -> int:
    ap = argparse.ArgumentParser(description="打包桌宠为可执行文件")
    ap.add_argument("--onefile", action="store_true", help="单文件 exe（启动慢，体积集中）")
    ap.add_argument("--console", action="store_true", help="保留控制台窗口，方便看日志")
    ap.add_argument("--clean", action="store_true", help="先删掉 build/ 与 dist/")
    ap.add_argument("--no-icon", action="store_true", help="不生成图标")
    args = ap.parse_args()

    if args.clean:
        for d in (BUILD_DIR, DIST_DIR):
            if d.exists():
                shutil.rmtree(d)
        print("[0/4] 已清理 build/ 与 dist/")

    ensure_pyinstaller()

    print("[2/4] 生成图标 ...")
    icon = None if args.no_icon else make_icon()
    print(f"      {icon.name if icon else '无（用默认图标）'}")

    print("[3/4] PyInstaller 打包中（首次约 1~2 分钟）...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        # 注意：不要加 --clean。它会让 PyInstaller 去清 %LOCALAPPDATA%\pyinstaller 缓存，
        # 一次删上百个文件，在受限环境里会被安全策略拦下导致构建直接失败。
        # 要干净构建就用本脚本的 --clean（只删项目内的 build/ 和 dist/）。
        "--onefile" if args.onefile else "--onedir",
        "--windowed" if not args.console else "--console",
        "--name", NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        # av 自带 ffmpeg 的 dll，不整包收进来运行时会找不到 libvpx
        "--collect-all", "av",
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

    out_root = DIST_DIR if args.onefile else DIST_DIR / NAME
    exe = out_root / (NAME + (".exe" if sys.platform == "win32" else ""))
    if not exe.exists():
        print(f"\n[失败] 没找到产物 {exe}")
        return 1

    print("[4/4] 摆配置和素材 ...")
    stage(out_root)

    size_mb = sum(f.stat().st_size for f in out_root.rglob("*") if f.is_file()) / 1024 / 1024
    print(f"\n完成 → {exe}")
    print(f"      整个目录 {size_mb:.0f} MB（素材占 53MB）")
    print(f"      双击 {exe.name} 启动；想改配置/换素材直接动旁边的文件，不用重编译")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
