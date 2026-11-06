"""Обёртка над DeepSeek. API совместим с OpenAI, поэтому берём их SDK."""

from collections.abc import Iterator

from openai import OpenAI

import config

_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        if not config.API_KEY:
            raise RuntimeError(
                "Нет ключа. Создай файл .env рядом с проектом и положи в него "
                "строку DEEPSEEK_API_KEY_CHAT=<твой ключ>."
            )
        _client = OpenAI(api_key=config.API_KEY, base_url=config.BASE_URL)
    return _client


def _extra(obj, field: str):
    """Поля DeepSeek, которых нет в типах OpenAI SDK."""
    if obj is None:
        return None
    value = getattr(obj, field, None)
    if value is not None:
        return value
    extra = getattr(obj, "model_extra", None) or {}
    return extra.get(field)


def stream_reply(messages: list[dict], thinking_enabled: bool) -> Iterator[dict]:
    """Отдаёт куски ответа, последним — словарь со статистикой.

    Ключи: {"reasoning": str} | {"content": str} | {"usage": dict}
    """
    kwargs: dict = {
        "model": config.MODEL,
        "messages": messages,
        "stream": True,
        "extra_body": {
            "thinking": {"type": "enabled" if thinking_enabled else "disabled"}
        },
    }
    # В режиме размышления температура игнорируется, поэтому не шлём её.
    if not thinking_enabled:
        kwargs["temperature"] = config.TEMPERATURE
    if config.MAX_TOKENS:
        kwargs["max_tokens"] = config.MAX_TOKENS

    usage = None
    for chunk in client().chat.completions.create(**kwargs):
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        reasoning = _extra(delta, "reasoning_content")
        if reasoning:
            yield {"reasoning": reasoning}
        if delta.content:
            yield {"content": delta.content}

    yield {"usage": _usage_dict(usage)}


def _usage_dict(usage) -> dict:
    if usage is None:
        return {
            "prompt_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
        }
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    hit = _extra(usage, "prompt_cache_hit_tokens") or 0
    miss = _extra(usage, "prompt_cache_miss_tokens")
    if miss is None:
        miss = max(prompt - hit, 0)
    details = getattr(usage, "completion_tokens_details", None)
    return {
        "prompt_tokens": prompt,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "reasoning_tokens": (_extra(details, "reasoning_tokens") or 0),
    }


def balance() -> dict:
    """Реальный остаток на счету — для сверки с нашим подсчётом.

    Обычный GET на стандартной библиотеке: ради одного запроса не стоит
    тащить зависимость, тем более что openai здесь работает на httpx2.
    """
    import json
    import urllib.request

    request = urllib.request.Request(
        f"{config.BASE_URL}/user/balance",
        headers={"Authorization": f"Bearer {config.API_KEY}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())
