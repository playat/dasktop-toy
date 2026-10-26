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
class FontConfig:
    """气泡字体（原 DSH 版的 `font` 字段，现在真的会用了）。"""

    family: str = "ShangshouSoftCandy"
    size: int = 21          # 0 = 按宠物尺寸自动算（size*0.0455）
    follow_pet: bool = True  # 缩放宠物时字号跟着变


@dataclass
class DshConfig:
    """DSH 对接（原插件挂在 DSH Web 服务上的 `/dsh-pet-7340` 契约）。

    `base_url` 留空 = 自动探测：环境变量 DSH_PET_URL / DSH_WEB_URL → 默认 3080 端口。
    探测不到就整块功能静默关闭，桌宠照常跑（纯本地模式）。
    """

    enabled: bool = True
    base_url: str = ""
    pet_id: str = ""       # 上报/读取哪只宠物的槽位；空 = pets[0].id
    report_pose: bool = True


@dataclass
class AiConfig:
    """本机大模型（可选）：没有 DSH 时也能碎碎念/对话/挑表情包。

    走 OpenAI 兼容的 /chat/completions，只说 `baseUrl`（到 /v1 为止）+ `apiKey` + `model`。
    不填 = 不启用，相关菜单项自动隐藏。
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    whisper_prompt: str = (
        "你是主人桌面上的Q版蓝发小女仆，会时不时碎碎念一句。说话要自然随意、短短一句"
        "（20字以内），温柔乖巧带点俏皮，说人话不啰嗦，不要解释你自己，不要提你是AI。"
    )
    timeout: float = 25.0

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.model)


@dataclass
class EventsConfig:
    """事件联动开关（原 DSH 版的 events / 通知 / 气泡那一套）。

    素材按**名字**找（余额-* / 工作状态-* / 碎碎念-*），原来就长这样，不用改目录名。
    """

    work_status: bool = True
    balance: bool = True
    notify: bool = True
    whisper: bool = True
    chat: bool = True
    click_sound: bool = False
    balance_sec: int = 1800
    whisper_sec: int = 300
    work_status_texts: list[list[str]] = field(default_factory=list)


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
    font: FontConfig = field(default_factory=FontConfig)
    dsh: DshConfig = field(default_factory=DshConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    # 表情包：名字 → 一句话描述（AI 选图用，也当菜单悬停提示）
    memes: dict[str, str] = field(default_factory=dict)
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
            font=_font(raw.get("font") or {}),
            dsh=_dsh(raw.get("dsh") or {}),
            ai=_ai(raw.get("ai") or {}),
            events=_events(raw),
            memes={str(k): str(v) for k, v in (raw.get("memes") or {}).items() if isinstance(k, str)},
            raw=raw,
        )


# ---------------------------------------------------------------- 子块解析


def _num(value: Any, fallback: float) -> float:
    try:
        got = float(value)
    except (TypeError, ValueError):
        return fallback
    return got if got > 0 else fallback


def _font(raw: dict[str, Any]) -> FontConfig:
    cfg = FontConfig()
    if isinstance(raw.get("family"), str) and raw["family"].strip():
        cfg.family = raw["family"].strip()
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    cfg.size = size if 0 <= size <= 96 else 21
    cfg.follow_pet = bool(raw.get("followPet", True))
    return cfg


def _dsh(raw: dict[str, Any]) -> DshConfig:
    return DshConfig(
        enabled=bool(raw.get("enabled", True)),
        base_url=str(raw.get("baseUrl") or "").strip(),
        pet_id=str(raw.get("petId") or "").strip(),
        report_pose=bool(raw.get("reportPose", True)),
    )


def _ai(raw: dict[str, Any]) -> AiConfig:
    cfg = AiConfig()
    cfg.base_url = str(raw.get("baseUrl") or "").strip().rstrip("/")
    cfg.api_key = str(raw.get("apiKey") or "").strip()
    cfg.model = str(raw.get("model") or "").strip()
    if isinstance(raw.get("whisperPrompt"), str) and raw["whisperPrompt"].strip():
        cfg.whisper_prompt = raw["whisperPrompt"].strip()
    cfg.timeout = _num(raw.get("timeout"), 25.0)
    return cfg


# 与上游 config.jsonc 的 workStatusTexts 一致：外层索引 = 档位
DEFAULT_WORK_TEXTS: list[list[str]] = [
    ["正在认真想下一步呢", "让我整理一下思路~", "正在梳理接下来要做什么"],
    ["正在处理任务呢", "这一步正在进行中哦", "正在继续干活呢，别打扰我~"],
    ["这一步处理好啦，接着看下一步", "搞定一步，继续看看哦~"],
    ["需要你确认一下呢", "这里要等你看一下哦", "轮到你来决定啦，等你~"],
    ["这一轮搞定啦，真棒！", "任务完成咯，可以摸鱼啦~"],
    ["这一步好像没跑通呢", "刚才的操作遇到点问题哦", "这里卡了一下，再等等你呢"],
]


def _events(raw: dict[str, Any]) -> EventsConfig:
    """事件开关：可以写在顶层，也可以收在 `events` 对象里（两种都认）。"""
    block = raw.get("events") if isinstance(raw.get("events"), dict) else {}

    def flag(key: str, fallback: bool = True) -> bool:
        if key in block:
            return bool(block[key])
        return bool(raw.get(key, fallback))

    cfg = EventsConfig(
        work_status=flag("workStatusEnabled"),
        balance=flag("balanceEnabled"),
        notify=flag("notificationsEnabled"),
        whisper=flag("whisperEnabled"),
        chat=flag("chatEnabled"),
        click_sound=bool(raw.get("clickSoundEnabled", block.get("clickSoundEnabled", False))),
    )
    refresh = raw.get("eventsRefreshSec") or {}
    cfg.balance_sec = int(_num(refresh.get("balance"), 1800))
    cfg.whisper_sec = int(_num(refresh.get("whisper"), 300))

    texts = raw.get("workStatusTexts")
    if isinstance(texts, list) and texts:
        cleaned = [
            [str(x) for x in row if isinstance(x, str)] if isinstance(row, list) else []
            for row in texts
        ]
        cfg.work_status_texts = cleaned
    else:
        cfg.work_status_texts = [list(row) for row in DEFAULT_WORK_TEXTS]
    return cfg
