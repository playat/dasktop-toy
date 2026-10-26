"""动画池解析 + 播放器（切换时交叉淡入）。"""

from __future__ import annotations

import random
import time
from pathlib import Path

import numpy as np
from PySide6.QtGui import QImage

from config import MoveAction, PetConfig
from video import AlphaVideo

FADE_SEC = 0.18


def to_qimage(frame: np.ndarray) -> QImage:
    height, width = frame.shape[:2]
    return QImage(frame.data, width, height, frame.strides[0], QImage.Format_RGBA8888).copy()


class AnimationSet:
    """把素材目录映射到「用途」，并负责随机抽取。

    播什么**完全由目录名决定**，配置里不再列素材清单：

        待机/     待机时循环播
        转向/     播完翻面
        拖拽/     被抓起时
        走位/     走位（手感参数在 config.jsonc 的 moves）
        点击回应/ 点一下宠物
        余额/ 碎碎念/  特定剧情，不参与随机
        其余全部素材  → 随机小动作池

    目录名在**任意层级**都认（`小动作/待机/x.webm` 也算待机），所以可以把特殊素材
    收进分类目录里而不影响归类。扫描用 rglob，往目录里丢文件不用重启：
    每次开右键菜单前都会重扫一遍。
    """

    # 目录名 → 用途键
    ROLE_DIRS = {
        "待机": "idle",
        "转向": "turn",
        "拖拽": "drag",
        "走位": "move",
        "点击回应": "click",
    }
    # 这些目录里的素材不参加随机小动作（特殊用途 + 特定剧情）
    NON_RANDOM = frozenset(ROLE_DIRS) | {"余额", "碎碎念"}

    def __init__(self, webm_dir: str | Path, cfg: PetConfig) -> None:
        self.dir = Path(webm_dir)
        self.pools = cfg.pools
        self.weights = cfg.weights
        self._missing: set[str] = set()
        self.index: dict[str, Path] = {}  # "子目录/名字" -> 文件
        self.by_stem: dict[str, list[str]] = {}  # "名字" -> [键…]（同名兜底）
        self.by_role: dict[str, list[str]] = {}  # "idle" -> [键…]
        self.random_keys: list[str] = []  # 随机小动作池
        self.scan()

    # ---------- 索引 ----------

    def scan(self) -> None:
        """重扫素材目录（递归）。键 = 相对路径去掉 .webm，用 / 分隔。"""
        self.index.clear()
        self.by_stem.clear()
        self.by_role = {role: [] for role in self.ROLE_DIRS.values()}
        self.random_keys = []
        self._missing.clear()
        try:
            files = sorted(self.dir.rglob("*.webm"))
        except OSError as exc:  # 目录被删 / 没权限
            print(f"[anim] 扫描素材失败 {self.dir}: {exc}", flush=True)
            return
        for path in files:
            if not path.is_file():
                continue
            key = path.relative_to(self.dir).with_suffix("").as_posix()
            self.index[key] = path
            self.by_stem.setdefault(path.stem, []).append(key)

            parts = key.split("/")[:-1]  # 去掉文件名，只剩目录
            role = next((self.ROLE_DIRS[d] for d in parts if d in self.ROLE_DIRS), None)
            if role is not None:
                self.by_role[role].append(key)
            if role is None and not (set(parts) & self.NON_RANDOM):
                self.random_keys.append(key)

    def has_role(self, role: str) -> bool:
        return bool(self.by_role.get(role))

    def role_keys(self, role: str) -> list[str]:
        return self.by_role.get(role) or []


    # 目录里的「告知文件」：不参与播放，只用来给目录写说明（特殊分类的备注）
    NOTE_SUFFIXES = (".md", ".markdown", ".txt")
    NOTE_PREFERRED = ("说明.md", "readme.md", "index.md", "说明.txt")

    def tree(self) -> dict:
        """把索引折成目录树 + 每个目录自己的说明。

        节点结构：{"dirs": {名字: 同结构}, "files": [(名字, 键, 路径)], "note": (标题, 全文) | None}
        分类完全由目录决定（不看文件名、不看配置）；note 来自目录里那个 .md。
        """
        root: dict = {"dirs": {}, "files": [], "note": None}
        for key, path in self.index.items():
            parts = key.split("/")
            node = root
            for part in parts[:-1]:
                node = node["dirs"].setdefault(part, {"dirs": {}, "files": [], "note": None})
            node["files"].append((parts[-1], key, path))
        self._fill_notes(root, self.dir)
        return root

    def _fill_notes(self, node: dict, folder: Path) -> None:
        node["note"] = self._read_note(folder)
        for name, child in node["dirs"].items():
            self._fill_notes(child, folder / name)

    @classmethod
    def _read_note(cls, folder: Path) -> tuple[str, str] | None:
        """读目录里的告知文件 → (标题, 全文)。第一行当标题，没文件返回 None。"""
        try:
            files = [
                p
                for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in cls.NOTE_SUFFIXES
            ]
        except OSError:
            return None
        if not files:
            return None
        pref = cls.NOTE_PREFERRED
        files.sort(
            key=lambda p: (pref.index(p.name) if p.name in pref else len(pref), p.name.lower())
        )
        try:
            text = files[0].read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        lines = [ln.strip().lstrip("#").strip() for ln in text.splitlines()]
        body = "\n".join(ln for ln in lines if ln)
        title = next((ln for ln in lines if ln), "") or files[0].stem
        if len(body) > 600:
            body = body[:600] + " …"
        return title, body

    def path_of(self, name: str | None) -> Path | None:
        if not name:
            return None
        # 1. 完整键精确命中（子目录里的素材）
        path = self.index.get(name)
        if path is not None:
            return path
        # 2. 配置里写的是名字，素材已被挪进子目录 → 按文件名兜底（同名多个时取第一个并告警）
        hits = self.by_stem.get(name) or []
        if hits:
            if len(hits) > 1 and name not in self._missing:
                self._missing.add(name)
                print(f"[anim] 同名素材 {len(hits)} 个，用第一个：{name} → {hits[0]}", flush=True)
            return self.index[hits[0]]
        if name not in self._missing:
            self._missing.add(name)
            print(f"[anim] 缺素材: {self.dir / (name + '.webm')}", flush=True)
        return None

    def _pick_role(self, role: str, exclude: str | None = None) -> tuple[str, Path] | None:
        """从某个用途目录里随机抽一个（排除刚播过的，避免连着重复）。"""
        keys = self.by_role.get(role) or []
        if not keys:
            return None
        if exclude and len(keys) > 1:
            keys = [k for k in keys if k != exclude] or keys
        key = random.choice(keys)
        return key, self.index[key]

    def idle(self):
        return self._pick_role("idle")

    def click(self):
        return self._pick_role("click")

    def drag(self):
        return self._pick_role("drag")

    def turn(self):
        return self._pick_role("turn")

    def random_action(self, facing: str, exclude: str | None = None) -> tuple[str, Path] | None:
        """随机小动作池：所有没被特殊目录收走的素材。

        带文字的素材（config.jsonc 的 noMirror）面向右时会被过滤掉，否则镜像后字是反的；
        全被过滤光就退回去用全部，总比什么都不播强。
        """
        keys = self.random_keys
        if not keys:
            return None
        blocked = self.pools.no_mirror
        if facing == "right" and blocked:
            keys = [k for k in keys if k.rsplit("/", 1)[-1] not in blocked] or keys
        if exclude and len(keys) > 1:
            keys = [k for k in keys if k != exclude] or keys
        key = random.choice(keys)
        return key, self.index[key]

    # ---------- 走位 ----------

    def tuning(self, stem: str) -> MoveAction:
        """把 moves.default 和该文件的 params 覆盖合成一份完整参数。"""
        d = self.pools.move_default
        p = self.pools.move_overrides.get(stem) or {}
        return MoveAction(
            name=stem,
            min_dist=float(p.get("minDist", d.min_dist)),
            max_dist=float(p.get("maxDist", d.max_dist)),
            margin=float(p.get("margin", d.margin)),
            lead_sec=float(p.get("leadSec", d.lead_sec)),
            tail_sec=float(p.get("tailSec", d.tail_sec)),
        )

    def moves(self) -> list[tuple[MoveAction, Path]]:
        """走位素材 = 走位/ 目录里的全部素材（参数按文件名覆盖）。"""
        return [(self.tuning(k.rsplit("/", 1)[-1]), self.index[k]) for k in self.role_keys("move")]

    def move(self, exclude: str | None = None):
        usable = self.moves()
        if not usable:
            return None
        if exclude and len(usable) > 1:
            usable = [(a, p) for a, p in usable if a.name != exclude] or usable
        return random.choice(usable)


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
