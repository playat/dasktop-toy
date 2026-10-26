"""本机大模型（可选）：没有 DSH 时也能碎碎念 / 对话 / 挑表情包。

只用一套约定：OpenAI 兼容的 `POST {baseUrl}/chat/completions`。
上游插件的独立模式（`standalone/ai-provider.ts`）也是这条路，配置项名字照抄：
`ai.baseUrl` / `ai.apiKey` / `ai.model`。

不填 `baseUrl` 或 `model` 就当作没启用 —— 调用方（events.py）据此隐藏相关菜单项。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from config import AiConfig


def _endpoint(base_url: str) -> str:
    """`https://api.x.com/v1` 和 `https://api.x.com` 都拼到同一个地方。"""
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def chat(messages: list[dict[str, str]], cfg: AiConfig) -> str | None:
    """一次非流式对话，返回正文；任何失败都返回 None（调用方退化成只播动画）。"""
    if not cfg.ready:
        return None
    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": 0.8,
        "stream": False,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    req = urllib.request.Request(
        _endpoint(cfg.base_url),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[ai] 请求失败：{type(exc).__name__}: {exc}", flush=True)
        return None

    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):  # 少数网关会返回分片数组
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    text = str(content or "").strip()
    return text or None


def whisper(cfg: AiConfig, pet_name: str = "") -> str | None:
    """碎碎念：一句短话。人设来自 `ai.whisperPrompt`。"""
    system = cfg.whisper_prompt
    if pet_name:
        system += f"你的名字是“{pet_name}”。"
    return chat(
        [{"role": "system", "content": system}, {"role": "user", "content": "现在随便说一句。"}],
        cfg,
    )


def reply(cfg: AiConfig, text: str, pet_name: str = "", history: list[dict[str, str]] | None = None) -> str | None:
    """对话：默认人设 +（可选）最近几轮记忆。"""
    system = cfg.whisper_prompt
    if pet_name:
        system += f"你的名字是“{pet_name}”。"
    messages = [{"role": "system", "content": system}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": text})
    return chat(messages, cfg)


def pick_meme(cfg: AiConfig, candidates: str, context: str) -> str | None:
    """让模型从表情包池里挑一张；只回一个名字，挑不出来就回「无」。"""
    if not cfg.ready or not candidates:
        return None
    system = (
        "你在为桌面宠物挑一张表情包配图。只能从候选清单里选一个名字原样回答；"
        "没有合适的就回答「无」。不要解释，不要加标点。"
    )
    user = f"候选：\n{candidates}\n\n当前情境：{context}\n\n选哪个？"
    return chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], cfg
    )
