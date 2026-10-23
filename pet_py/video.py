"""VP9-alpha 视频解码（PyAV）。

关键陷阱：PyAV 默认会选 native vp9 解码器，它不保留 alpha 平面——
解出来的 pix_fmt 是 yuv420p、alpha 全 255，看上去就像「素材根本没有透明通道」。
必须显式创建 libvpx-vp9 解码器并声明 yuva420p。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import av
import numpy as np

# 素材背景的 alpha 落在 1~15（VP9 alpha 有损编码的噪声），低于此值视为完全透明。
ALPHA_NOISE_FLOOR = 16


class AlphaVideo:
    """后台线程按素材帧率解码，主线程随时读 `frame` 拿最新 RGBA 帧。

    loop=True  无限循环（待机、拖拽这类持续动作）
    loop=False 只播一遍，播完 `finished` 置 True（点击回应这类一次性动作）
    """

    def __init__(self, path: str | Path, loop: bool = True) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)

        self.loop = loop
        self.width, self.height, self.fps, self.duration = self._probe()
        self._frame: np.ndarray | None = None
        self._finished = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def frame(self) -> np.ndarray | None:
        return self._frame

    @property
    def finished(self) -> bool:
        return self._finished

    def _probe(self) -> tuple[int, int, float, float]:
        container = av.open(str(self.path))
        stream = container.streams.video[0]
        ctx = stream.codec_context
        fps = float(stream.average_rate) if stream.average_rate else 24.0
        width, height = ctx.width, ctx.height
        # 时长：优先用流自带的 duration，否则按帧数/帧率推算（本项目素材都是 ~10s）
        duration = float(stream.duration * stream.time_base) if stream.duration else 0.0
        if duration <= 0 and stream.frames:
            duration = stream.frames / fps
        container.close()
        return width, height, fps, duration or 10.0

    def _extradata(self) -> bytes:
        container = av.open(str(self.path))
        extradata = bytes(container.streams.video[0].codec_context.extradata or b"")
        container.close()
        return extradata

    def _new_decoder(self) -> av.CodecContext:
        decoder = av.CodecContext.create("libvpx-vp9", "r")
        decoder.width = self.width
        decoder.height = self.height
        decoder.pix_fmt = "yuva420p"
        decoder.extradata = self._extradata()
        decoder.open()
        return decoder

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="alpha-video", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        interval = 1.0 / self.fps
        while not self._stop.is_set():
            try:
                decoder = self._new_decoder()
            except Exception as exc:  # noqa: BLE001
                print(f"[video] 解码器创建失败: {exc}")
                self._finished = True
                return

            container = av.open(str(self.path))
            stream = container.streams.video[0]
            deadline = time.monotonic()
            try:
                for packet in container.demux(stream):
                    if self._stop.is_set():
                        return
                    if not packet.size:
                        continue
                    for decoded in decoder.decode(packet):
                        rgba = decoded.to_ndarray(format="rgba")
                        if not rgba.flags.writeable:
                            rgba = rgba.copy()
                        alpha = rgba[:, :, 3]
                        alpha[alpha < ALPHA_NOISE_FLOOR] = 0
                        self._frame = rgba

                        deadline += interval
                        gap = deadline - time.monotonic()
                        if gap > 0:
                            time.sleep(gap)
                        else:
                            deadline = time.monotonic()
            finally:
                container.close()

            if not self.loop:
                self._finished = True
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
