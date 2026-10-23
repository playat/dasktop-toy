"""动画池解析 + 播放器（切换时交叉淡入）。"""

from __future__ import annotations

import random
import time
from pathlib import Path

import numpy as np
from PySide6.QtGui import QImage

from config import PetConfig
from video import AlphaVideo

FADE_SEC = 0.18


def to_qimage(frame: np.ndarray) -> QImage:
    height, width = frame.shape[:2]
    return QImage(frame.data, width, height, frame.strides[0], QImage.Format_RGBA8888).copy()


class AnimationSet:
    """把配置里的动画名映射到 webm 文件，并负责随机抽取。"""

    def __init__(self, webm_dir: str | Path, cfg: PetConfig) -> None:
        self.dir = Path(webm_dir)
        self.pools = cfg.pools
        self.weights = cfg.weights
        self._missing: set[str] = set()

    def path_of(self, name: str | None) -> Path | None:
        if not name:
            return None
        path = self.dir / f"{name}.webm"
        if path.exists():
            return path
        if name not in self._missing:
            self._missing.add(name)
            print(f"[anim] 缺素材: {path}", flush=True)
        return None

    def pick(self, pool: list[str], exclude: str | None = None) -> tuple[str, Path] | None:
        """从池里随机抽一个；exclude 用于避免连续重复（排除后为空才允许重复）。"""
        candidates = [(n, p) for n in pool if (p := self.path_of(n)) is not None]
        if not candidates:
            return None
        if exclude and len(candidates) > 1:
            filtered = [c for c in candidates if c[0] != exclude]
            if filtered:
                candidates = filtered
        return random.choice(candidates)

    def pick_category(self, facing: str, exclude: str | None = None) -> tuple[str, Path] | None:
        """按 weight 抽一个分类，再从分类里抽动作。noMirror 的类在面向右时跳过。"""
        cats = []
        for cat in self.pools.categories:
            if cat.get("noMirror") and facing == "right":
                continue
            actions = cat.get("actions") or []
            if actions and any(self.path_of(n) for n in actions):
                cats.append(cat)
        if not cats:
            return None
        total = sum(float(c.get("weight") or 0) for c in cats) or float(len(cats))
        roll = random.uniform(0, total)
        acc = 0.0
        chosen = cats[-1]
        for cat in cats:
            acc += float(cat.get("weight") or 0) or 1.0
            if roll <= acc:
                chosen = cat
                break
        return self.pick(list(chosen.get("actions") or []), exclude)

    def idle(self):
        return self.pick(self.pools.idle)

    def click(self):
        return self.pick(self.pools.clicks)

    def drag(self):
        return self.pick(self.pools.drag)

    def turn(self):
        return self.pick(self.pools.turn)

    def move(self, exclude: str | None = None):
        if not self.pools.moves:
            return None
        usable = [m for m in self.pools.moves if self.path_of(m.name)]
        if not usable:
            return None
        if exclude and len(usable) > 1:
            usable = [m for m in usable if m.name != exclude] or usable
        action = random.choice(usable)
        return action, self.path_of(action.name)


class AnimationPlayer:
    """同一时刻只持有一个解码器；切换时把上一帧留作淡出底图。"""

    def __init__(self) -> None:
        self._clip: AlphaVideo | None = None
        self._name: str | None = None
        self._prev_image: QImage | None = None
        self._fade_at = 0.0
        self._on_finish = None
        self._cache_src: np.ndarray | None = None
        self._cache_img: QImage | None = None

    @property
    def current(self) -> str | None:
        return self._name

    @property
    def duration(self) -> float:
        return self._clip.duration if self._clip is not None else 10.0

    def play(self, name: str, path: Path, loop: bool = True, on_finish=None) -> bool:
        if name == self._name and self._clip is not None and not self._clip.finished:
            return False

        if self._clip is not None:
            if self._clip.frame is not None:
                self._prev_image = self._image_of(self._clip.frame)
            self._clip.stop()

        try:
            clip = AlphaVideo(path, loop=loop)
        except Exception as exc:  # noqa: BLE001
            print(f"[anim] 打开失败 {path.name}: {exc}", flush=True)
            return False

        clip.start()
        self._clip = clip
        self._name = name
        self._on_finish = on_finish
        self._fade_at = time.monotonic()
        self._cache_src = None
        self._cache_img = None
        return True

    def _image_of(self, frame: np.ndarray) -> QImage:
        if frame is self._cache_src and self._cache_img is not None:
            return self._cache_img
        self._cache_src = frame
        self._cache_img = to_qimage(frame)
        return self._cache_img

    def tick(self) -> tuple[QImage | None, QImage | None, float]:
        """返回 (当前帧, 淡出底图, 淡入进度 0~1)。"""
        if self._clip is None:
            return None, None, 1.0

        if self._clip.finished and self._on_finish is not None:
            callback = self._on_finish
            self._on_finish = None
            callback()

        clip = self._clip
        if clip is None:
            return None, None, 1.0

        frame = clip.frame
        current = self._image_of(frame) if frame is not None else None

        progress = (time.monotonic() - self._fade_at) / FADE_SEC
        if progress >= 1.0 or self._prev_image is None:
            self._prev_image = None
            return current, None, 1.0
        return current, self._prev_image, progress

    def stop(self) -> None:
        if self._clip is not None:
            self._clip.stop()
            self._clip = None
