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
    """一个走位动作及其时间参数（未写的字段取 moves.default）。"""

    name: str
    min_dist: float = 60
    max_dist: float = 240
    margin: float = 20
    lead_sec: float = 2.0
    tail_sec: float = 2.0


@dataclass
class AnimationPools:
    idle: list[str] = field(default_factory=lambda: ["待机呼吸休闲"])
    clicks: list[str] = field(default_factory=list)
    drag: list[str] = field(default_factory=list)
    turn: list[str] = field(default_factory=list)
    moves: list[MoveAction] = field(default_factory=list)
    categories: list[dict[str, Any]] = field(default_factory=list)
    # 事件动画（余额/碎碎念/工作状态）——独立模式没有 DSH 事件源，但右键菜单照样能点播预览
    events: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class PetSpec:
    """pets[] 里的一只宠物（多开 = 数组里多条）。"""

    id: str = "main"
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
    pets: list[PetSpec] = field(default_factory=lambda: [PetSpec()])
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "PetConfig":
        raw = json.loads(strip_jsonc(Path(path).read_text(encoding="utf-8")))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PetConfig":
        pets = raw.get("pets") or [{}]
        animations = raw.get("animations") or {}

        moves_block = animations.get("moves") or {}
        base = moves_block.get("default") or {}
        move_actions: list[MoveAction] = []
        for item in moves_block.get("actions") or []:
            if "name" not in item:
                continue
            merged = {**base, **(item.get("params") or {})}
            move_actions.append(
                MoveAction(
                    name=item["name"],
                    min_dist=float(merged.get("minDist", 60)),
                    max_dist=float(merged.get("maxDist", 240)),
                    margin=float(merged.get("margin", 20)),
                    lead_sec=float(merged.get("leadSec", 2)),
                    tail_sec=float(merged.get("tailSec", 2)),
                )
            )

        pools = AnimationPools(
            idle=list(animations.get("idle") or ["待机呼吸休闲"]),
            clicks=list(animations.get("clicks") or []),
            drag=list(animations.get("drag") or []),
            turn=list(animations.get("turn") or []),
            moves=move_actions,
            categories=list(animations.get("categories") or []),
            events=_parse_events(animations.get("events") or {}),
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
            pets=specs,
            raw=raw,
        )


def _parse_events(events: dict[str, Any]) -> dict[str, list[str]]:
    """事件池可能是「名字数组」或「档位数组」，统一展平成名字数组。"""
    out: dict[str, list[str]] = {}
    for key, slots in events.items():
        names: list[str] = []
        for slot in slots or []:
            if isinstance(slot, str):
                names.append(slot)
            elif isinstance(slot, (list, tuple)):
                names.extend(str(x) for x in slot)
        if names:
            out[str(key)] = names
    return out
