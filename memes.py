"""表情包池：`assets/memes/*.png`（老布局放 `assets/pic/memes/` 也认）。

名字 = 文件名 stem，和 config.jsonc 里 `memes` 的键一一对应：

    "可爱": "标准正面卖萌立绘，适合开心问候、卖萌、无梗纯可爱"

那句描述两个用途：AI 选图时当候选清单（模型只能从这堆名字里挑），
右键菜单里当悬停提示。图片本身换掉不用改配置，**加图**才需要补一行描述。
"""

from __future__ import annotations

import random
import re
from pathlib import Path

IMG_EXTS = (".png", ".webp", ".gif", ".jpg", ".jpeg")

# 相对 assets 根的几个候选目录（前面优先）
CANDIDATE_DIRS = ("memes", "pic/memes")


class MemePool:
    def __init__(self, assets: Path, descriptions: dict[str, str] | None = None) -> None:
        self._assets = Path(assets)
        self._desc = {str(k): str(v) for k, v in (descriptions or {}).items()}
        self._index: dict[str, Path] = {}
        self.scan()

    # ---------- 扫描 ----------

    def scan(self) -> None:
        found: dict[str, Path] = {}
        for rel in CANDIDATE_DIRS:
            folder = self._assets / rel
            if not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                if path.is_file() and path.suffix.lower() in IMG_EXTS:
                    found.setdefault(path.stem, path)
        self._index = found

    # ---------- 查询 ----------

    @property
    def names(self) -> list[str]:
        return list(self._index)

    def __len__(self) -> int:
        return len(self._index)

    def path_of(self, name: str | None) -> Path | None:
        if not name:
            return None
        key = str(name).strip()
        if key in self._index:
            return self._index[key]
        # 兼容带扩展名 / 带目录的写法（DSH 那边只给池内名字）
        key = Path(key).stem
        return self._index.get(key)

    def describe(self, name: str) -> str:
        return self._desc.get(name, "")

    def random(self) -> str | None:
        if not self._index:
            return None
        return random.choice(list(self._index))

    def brief(self, limit: int = 40) -> str:
        """给模型看的候选清单：名字 + 一句适用场景。"""
        lines = []
        for name in list(self._index)[:limit]:
            desc = self.describe(name)
            lines.append(f"- {name}：{desc}" if desc else f"- {name}")
        return "\n".join(lines)

    def pick_from(self, reply: str) -> str | None:
        """从模型回复里认出一个池内名字（宽松匹配，认不出来就当没配图）。"""
        text = (reply or "").strip()
        if not text:
            return None
        for name in self._index:
            if name and name in text:
                return name
        # 去掉引号/标点后再试一次，返回第一个命中的合法名字
        cleaned = re.sub(r"[\s\"'“”‘’。，,、:：!！?？`*]+", "", text)
        for name in self._index:
            key = re.sub(r"[\s\"'“”‘’。，,、:：!！?？`*]+", "", name)
            if key and key in cleaned:
                return name
        return None
