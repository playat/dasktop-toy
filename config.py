"""配置读取：解析 jsonc（带行注释 / 块注释 / 尾逗号），抽出动画池与物理参数。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from physics import PhysicsParams


def strip_jsonc(text: str) -> str:
    """去掉注释和尾逗号，得到合法 JSON。字符串内的 // 与 /* 不动。"""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False

    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue

        out.append(ch)
        i += 1

    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


@dataclass
class MoveAction:
    """一个走位动作的完整参数（未写的字段取 moves.default）。

    素材本身来自 `assets/webm/走位/`，这里只管手感 —— 名字是文件名 stem。
    """

    name: str = ""
    min_dist: float = 60
    max_dist: float = 240
    margin: float = 20
    lead_sec: float = 2.0
    tail_sec: float = 2.0


@dataclass
class AnimationPools:
    """只装「参数」和「例外清单」—— 播什么素材完全由 assets/webm 的目录结构决定。

    目录名即用途（任意层级都认，见 player.ROLE_DIRS）：
        待机/ 转向/ 拖拽/ 走位/ 点击回应/   +   其余素材 = 随机小动作池
    """

    move_default: MoveAction = field(default_factory=MoveAction)
    # 文件名 stem → 只写了要覆盖的那几个键（minDist/maxDist/margin/leadSec/tailSec）
    move_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 带文字、镜像会颠倒的素材：面向右时改用朝左，不然字是反的
    no_mirror: set[str] = field(default_factory=set)


@dataclass
class PetSpec:
    """pets[] 里的一只宠物（多开 = 数组里多条）。"""

    id: str = "main"
    name: str = ""
    size: int = 462
    fixed: bool = False
    position: dict[str, Any] = field(default_factory=dict)


@dataclass
class PetConfig:
    size: int = 462
    pools: AnimationPools = field(default_factory=AnimationPools)
    weights: dict[str, float] = field(default_factory=lambda: {"idle": 10, "turn": 5, "move": 5})
    physics: PhysicsParams = field(default_factory=PhysicsParams)
    fixed: bool = False
    # 根字段（不属于 physics）：抛掷时是否把宠物锁在松手所在那块屏内。
    # True = 屏缝当墙（不会飞到隔壁屏）；False = 允许跨屏飞行，落到隔壁屏。
    confine_to_screen: bool = False
    pets: list[PetSpec] = field(default_factory=lambda: [PetSpec()])
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "PetConfig":
        raw = json.loads(strip_jsonc(Path(path).read_text(encoding="utf-8")))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PetConfig":
        pets = raw.get("pets") or [{}]

        moves_block = raw.get("moves") or {}
        base = moves_block.get("default") or {}
        move_default = MoveAction(
            min_dist=float(base.get("minDist", 60)),
            max_dist=float(base.get("maxDist", 240)),
            margin=float(base.get("margin", 20)),
            lead_sec=float(base.get("leadSec", 2)),
            tail_sec=float(base.get("tailSec", 2)),
        )
        # actions 只作「按文件名覆盖参数」用；素材本身来自 走位/ 目录，列不列都行
        move_overrides: dict[str, dict[str, Any]] = {}
        for item in moves_block.get("actions") or []:
            if isinstance(item, dict) and item.get("name"):
                params = item.get("params") or {}
                if isinstance(params, dict) and params:
                    move_overrides[str(item["name"])] = dict(params)

        pools = AnimationPools(
            move_default=move_default,
            move_overrides=move_overrides,
            no_mirror={str(x) for x in (raw.get("noMirror") or [])},
        )

        weights = {"idle": 10.0, "turn": 5.0, "move": 5.0}
        for key, value in (raw.get("animationWeights") or {}).items():
            try:
                weights[key] = float(value)
            except (TypeError, ValueError):
                pass

        specs = [
            PetSpec(
                id=str(p.get("id") or f"pet{i + 1}"),
                name=str(p.get("name") or ""),
                size=int(p.get("size") or 462),
                fixed=bool(p.get("fixedEnabled", False)),
                position=dict(p.get("position") or {}),
            )
            for i, p in enumerate(pets)
            if isinstance(p, dict)
        ] or [PetSpec()]

        return cls(
            size=specs[0].size,
            pools=pools,
            weights=weights,
            physics=PhysicsParams.from_dict(raw.get("physics") or {}),
            fixed=specs[0].fixed,
            confine_to_screen=bool(raw.get("confineToScreen", False)),
            pets=specs,
            raw=raw,
        )
