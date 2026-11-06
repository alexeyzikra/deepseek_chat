"""Локальный веб-интерфейс к DeepSeek."""

import json
from collections.abc import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import db
import deepseek_client
import pricing

app = FastAPI(title="DeepSeek Chat")
db.init()

CURRENT_PERSONA = "current_persona_id"


# ---------------------------------------------------------------- модели тела

class PersonaIn(BaseModel):
    name: str
    system_prompt: str = ""
    thinking_enabled: bool = False


class CurrentPersonaIn(BaseModel):
    persona_id: int


class PersonaPatchIn(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    thinking_enabled: bool | None = None


class SettingsIn(BaseModel):
    theme: str | None = None


class SendIn(BaseModel):
    content: str


class AppendIn(BaseModel):
    role: str
    content: str


class ChatPatchIn(BaseModel):
    system_prompt: str | None = None
    title: str | None = None
    pinned: bool | None = None


# ------------------------------------------------------------------ состояние

@app.get("/api/bootstrap")
def bootstrap():
    with db.session() as conn:
        personas = [
            dict(r)
            for r in conn.execute(
                "SELECT id, name, thinking_enabled FROM persona "
                "WHERE deleted_at IS NULL ORDER BY id"
            )
        ]
        current = db.get_state(conn, CURRENT_PERSONA)
        current_id = int(current) if current else None
        if current_id and not any(p["id"] == current_id for p in personas):
            current_id = None
        if current_id is None and personas:
            current_id = personas[0]["id"]
            db.set_state(conn, CURRENT_PERSONA, str(current_id))
        theme = db.get_state(conn, "theme") or "dark"
    return {
        "personas": personas,
        "current_persona_id": current_id,
        "theme": theme,
    }


@app.post("/api/personas")
def create_persona(body: PersonaIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "У личности должно быть имя")
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO persona (name, system_prompt, thinking_enabled, created_at) "
            "VALUES (?, ?, ?, ?)",
            (name, body.system_prompt, int(body.thinking_enabled), db.now()),
        )
        persona_id = cur.lastrowid
        db.set_state(conn, CURRENT_PERSONA, str(persona_id))
    return {"id": persona_id}


@app.post("/api/current-persona")
def set_current_persona(body: CurrentPersonaIn):
    with db.session() as conn:
        row = conn.execute(
            "SELECT id FROM persona WHERE id = ? AND deleted_at IS NULL",
            (body.persona_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Личность не найдена")
        db.set_state(conn, CURRENT_PERSONA, str(body.persona_id))
    return {"ok": True}


@app.get("/api/personas/{persona_id}")
def get_persona(persona_id: int):
    with db.session() as conn:
        row = conn.execute(
            "SELECT id, name, system_prompt, thinking_enabled FROM persona "
            "WHERE id = ? AND deleted_at IS NULL",
            (persona_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Личность не найдена")
    return dict(row)


@app.patch("/api/personas/{persona_id}")
def patch_persona(persona_id: int, body: PersonaPatchIn):
    """Имя, промпт и режим размышления личности.

    Промпт личности — образец для НОВЫХ чатов. Уже существующие чаты держат
    собственную копию и здесь не меняются.
    """
    with db.session() as conn:
        row = conn.execute(
            "SELECT id FROM persona WHERE id = ? AND deleted_at IS NULL",
            (persona_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Личность не найдена")
        if body.name is not None:
            name = body.name.strip()
            if not name:
                raise HTTPException(400, "У личности должно быть имя")
            conn.execute("UPDATE persona SET name = ? WHERE id = ?", (name, persona_id))
        if body.system_prompt is not None:
            conn.execute(
                "UPDATE persona SET system_prompt = ? WHERE id = ?",
                (body.system_prompt, persona_id),
            )
        if body.thinking_enabled is not None:
            conn.execute(
                "UPDATE persona SET thinking_enabled = ? WHERE id = ?",
                (int(body.thinking_enabled), persona_id),
            )
    return {"ok": True}


@app.delete("/api/personas/{persona_id}")
def delete_persona(persona_id: int):
    """Безвозвратно: имя, промпт и все чаты личности стираются.

    В статистике остаётся обезличенная строка «Удалённая личность №N» —
    только цифры, чтобы расходы не выпали из общей суммы.
    """
    next_id = None
    with db.session() as conn:
        persona = conn.execute(
            "SELECT * FROM persona WHERE id = ? AND deleted_at IS NULL",
            (persona_id,),
        ).fetchone()
        if not persona:
            raise HTTPException(404, "Личность не найдена")

        for chat in conn.execute(
            "SELECT id FROM chat WHERE persona_id = ?", (persona_id,)
        ).fetchall():
            conn.execute("DELETE FROM message WHERE chat_id = ?", (chat["id"],))
        conn.execute("DELETE FROM chat WHERE persona_id = ?", (persona_id,))

        counter = int(db.get_state(conn, "deleted_personas") or 0) + 1
        db.set_state(conn, "deleted_personas", str(counter))
        conn.execute(
            "UPDATE persona SET deleted_at = ?, name = ?, system_prompt = '' "
            "WHERE id = ?",
            (db.now(), f"Удалённая личность №{counter}", persona_id),
        )

        other = conn.execute(
            "SELECT id FROM persona WHERE deleted_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
        if other:
            next_id = other["id"]
            db.set_state(conn, CURRENT_PERSONA, str(next_id))
        else:
            conn.execute("DELETE FROM app_state WHERE key = ?", (CURRENT_PERSONA,))

    db.vacuum()
    return {"ok": True, "next_persona_id": next_id}


# ------------------------------------------------------------- настройки

@app.get("/api/settings")
def get_settings():
    with db.session() as conn:
        return {"theme": db.get_state(conn, "theme") or "dark"}


@app.patch("/api/settings")
def patch_settings(body: SettingsIn):
    if body.theme is not None:
        if body.theme not in ("dark", "light"):
            raise HTTPException(400, "Тема бывает только dark или light")
        with db.session() as conn:
            db.set_state(conn, "theme", body.theme)
    return {"ok": True}


# ---------------------------------------------------------------------- чаты

@app.get("/api/personas/{persona_id}/chats")
def list_chats(persona_id: int):
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, number, title, pinned, created_at FROM chat "
            "WHERE persona_id = ? AND deleted_at IS NULL "
            "ORDER BY pinned DESC, id DESC",
            (persona_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/personas/{persona_id}/chats")
def create_chat(persona_id: int):
    with db.session() as conn:
        persona = conn.execute(
            "SELECT * FROM persona WHERE id = ? AND deleted_at IS NULL", (persona_id,)
        ).fetchone()
        if not persona:
            raise HTTPException(404, "Личность не найдена")
        number = persona["next_chat_number"]
        conn.execute(
            "UPDATE persona SET next_chat_number = ? WHERE id = ?",
            (number + 1, persona_id),
        )
        title = f"{number:02d}"
        cur = conn.execute(
            "INSERT INTO chat (persona_id, number, title, system_prompt, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (persona_id, number, title, persona["system_prompt"], db.now()),
        )
        chat_id = cur.lastrowid
    return {"id": chat_id, "number": number, "title": title}


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: int):
    with db.session() as conn:
        chat = conn.execute(
            "SELECT * FROM chat WHERE id = ? AND deleted_at IS NULL", (chat_id,)
        ).fetchone()
        if not chat:
            raise HTTPException(404, "Чат не найден")
        messages = conn.execute(
            "SELECT id, position, role, content, reasoning_content FROM message "
            "WHERE chat_id = ? ORDER BY position",
            (chat_id,),
        ).fetchall()
        costs = {
            r["message_id"]: r["cost_usd"]
            for r in conn.execute(
                "SELECT message_id, cost_usd FROM api_call "
                "WHERE chat_id = ? AND message_id IS NOT NULL",
                (chat_id,),
            )
        }
        spent = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM api_call WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()["total"]
        last_prompt = conn.execute(
            "SELECT prompt_tokens FROM api_call WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()

    out = []
    for m in messages:
        item = dict(m)
        usd = costs.get(m["id"])
        item["cost_millicents"] = pricing.to_millicents(usd) if usd else None
        out.append(item)
    return {
        "chat": dict(chat),
        "messages": out,
        "spent_millicents": pricing.to_millicents(spent),
        "context_tokens": last_prompt["prompt_tokens"] if last_prompt else 0,
        "context_limit": config.CONTEXT_LIMIT,
    }


# ------------------------------------------------------- история и проверки

def _chat_or_404(conn, chat_id: int):
    chat = conn.execute(
        "SELECT * FROM chat WHERE id = ? AND deleted_at IS NULL", (chat_id,)
    ).fetchone()
    if not chat:
        raise HTTPException(404, "Чат не найден")
    return chat


def _history(conn, chat, upto_position: int | None = None) -> list[dict]:
    sql = "SELECT role, content FROM message WHERE chat_id = ?"
    args: list = [chat["id"]]
    if upto_position is not None:
        sql += " AND position <= ?"
        args.append(upto_position)
    rows = conn.execute(sql + " ORDER BY position", args).fetchall()

    payload = []
    if chat["system_prompt"].strip():
        payload.append({"role": "system", "content": chat["system_prompt"]})
    # reasoning_content обратно в API не уходит никогда — только сам ответ.
    payload += [{"role": r["role"], "content": r["content"]} for r in rows]
    return payload


def _check_alternation(payload: list[dict]) -> None:
    previous = None
    for item in payload:
        if item["role"] == "system":
            continue
        if item["role"] == previous:
            who = "твоих" if previous == "user" else "ответов бота"
            raise HTTPException(
                409,
                f"История сломана: два {who} сообщения подряд. "
                "Запрос не отправлен — поправь историю и попробуй снова.",
            )
        previous = item["role"]


# ------------------------------------------------------------- генерация

def _completion_stream(
    chat,
    persona,
    payload: list[dict],
    truncate_after: int,
    pending_user_text: str | None = None,
) -> StreamingResponse:
    """Стримит ответ и пишет в базу ТОЛЬКО после удачного запроса.

    До этого момента история не меняется вообще: оборвавшийся запрос не
    должен стирать хвост, который пользователь собирал руками.

    truncate_after — позиция, после которой хвост удаляется (перегенерация).
    pending_user_text — сообщение, которое надо сохранить вместе с ответом.
    """
    chat_id = chat["id"]
    persona_id = chat["persona_id"]
    thinking = bool(persona["thinking_enabled"])

    def events() -> Iterator[str]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict | None = None
        try:
            for piece in deepseek_client.stream_reply(payload, thinking):
                if "reasoning" in piece:
                    reasoning_parts.append(piece["reasoning"])
                    yield _sse({"type": "reasoning", "text": piece["reasoning"]})
                elif "content" in piece:
                    content_parts.append(piece["content"])
                    yield _sse({"type": "content", "text": piece["content"]})
                elif "usage" in piece:
                    usage = piece["usage"]
        except Exception as exc:
            yield _sse({"type": "error", "message": str(exc)})
            return

        usage = usage or {}
        tariff = pricing.tariff_at()
        usd = pricing.cost_usd(
            usage.get("cache_hit_tokens", 0),
            usage.get("cache_miss_tokens", 0),
            usage.get("output_tokens", 0),
            tariff,
        )

        with db.session() as conn:
            conn.execute(
                "DELETE FROM message WHERE chat_id = ? AND position > ?",
                (chat_id, truncate_after),
            )
            position = truncate_after
            user_message_id = None
            if pending_user_text is not None:
                position += 1
                cur = conn.execute(
                    "INSERT INTO message (chat_id, position, role, content, created_at) "
                    "VALUES (?, ?, 'user', ?, ?)",
                    (chat_id, position, pending_user_text, db.now()),
                )
                user_message_id = cur.lastrowid

            position += 1
            cur = conn.execute(
                "INSERT INTO message "
                "(chat_id, position, role, content, reasoning_content, created_at) "
                "VALUES (?, ?, 'assistant', ?, ?, ?)",
                (
                    chat_id,
                    position,
                    "".join(content_parts),
                    "".join(reasoning_parts) or None,
                    db.now(),
                ),
            )
            assistant_message_id = cur.lastrowid

            conn.execute(
                "INSERT INTO api_call (created_at, persona_id, chat_id, message_id, "
                "model, cache_hit_tokens, cache_miss_tokens, output_tokens, "
                "reasoning_tokens, prompt_tokens, tariff, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    db.now(),
                    persona_id,
                    chat_id,
                    assistant_message_id,
                    config.MODEL,
                    usage.get("cache_hit_tokens", 0),
                    usage.get("cache_miss_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("reasoning_tokens", 0),
                    usage.get("prompt_tokens", 0),
                    tariff,
                    usd,
                ),
            )
            spent = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS t FROM api_call WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()["t"]

        yield _sse(
            {
                "type": "done",
                "message_id": assistant_message_id,
                "user_message_id": user_message_id,
                "cost_millicents": pricing.to_millicents(usd),
                "chat_spent_millicents": pricing.to_millicents(spent),
                "context_tokens": usage.get("prompt_tokens", 0),
                "tariff": tariff,
                "usage": usage,
            }
        )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chats/{chat_id}/send")
def send(chat_id: int, body: SendIn):
    text = body.content.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    with db.session() as conn:
        chat = _chat_or_404(conn, chat_id)
        persona = conn.execute(
            "SELECT * FROM persona WHERE id = ?", (chat["persona_id"],)
        ).fetchone()
        last = conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS p FROM message WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()["p"]
        payload = _history(conn, chat) + [{"role": "user", "content": text}]
    _check_alternation(payload)
    return _completion_stream(chat, persona, payload, last, text)


@app.post("/api/messages/{message_id}/regenerate")
def regenerate(message_id: int):
    """Стирает всё после этого сообщения и получает ответ заново.

    Хвост удаляется только после удачного ответа — см. _completion_stream.
    """
    with db.session() as conn:
        message = conn.execute(
            "SELECT * FROM message WHERE id = ?", (message_id,)
        ).fetchone()
        if not message:
            raise HTTPException(404, "Сообщение не найдено")
        if message["role"] != "user":
            raise HTTPException(400, "Перегенерировать можно только своё сообщение")
        chat = _chat_or_404(conn, message["chat_id"])
        persona = conn.execute(
            "SELECT * FROM persona WHERE id = ?", (chat["persona_id"],)
        ).fetchone()
        payload = _history(conn, chat, upto_position=message["position"])
    _check_alternation(payload)
    return _completion_stream(chat, persona, payload, message["position"])


# --------------------------------------------------- правка без запроса

@app.patch("/api/messages/{message_id}")
def edit_message(message_id: int, body: SendIn):
    """Меняет текст в истории. Ничего не перегенерирует и не стоит денег."""
    if not body.content.strip():
        raise HTTPException(400, "Пустое сообщение. Чтобы убрать — удали его.")
    with db.session() as conn:
        cur = conn.execute(
            "UPDATE message SET content = ? WHERE id = ?", (body.content, message_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Сообщение не найдено")
    return {"ok": True}


@app.delete("/api/messages/{message_id}")
def delete_message(message_id: int):
    """Удаляет сообщение и весь хвост после него."""
    with db.session() as conn:
        message = conn.execute(
            "SELECT chat_id, position FROM message WHERE id = ?", (message_id,)
        ).fetchone()
        if not message:
            raise HTTPException(404, "Сообщение не найдено")
        cur = conn.execute(
            "DELETE FROM message WHERE chat_id = ? AND position >= ?",
            (message["chat_id"], message["position"]),
        )
    return {"deleted": cur.rowcount}


@app.post("/api/chats/{chat_id}/messages")
def append_message(chat_id: int, body: AppendIn):
    """Дописывает сообщение в историю, не обращаясь к API. Денег не стоит."""
    if body.role not in ("user", "assistant"):
        raise HTTPException(400, "Роль должна быть user или assistant")
    with db.session() as conn:
        _chat_or_404(conn, chat_id)
        last = conn.execute(
            "SELECT role, position FROM message WHERE chat_id = ? "
            "ORDER BY position DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if last and last["role"] == body.role:
            who = "твоих" if body.role == "user" else "ответов бота"
            raise HTTPException(409, f"Двух {who} сообщений подряд быть не должно")
        position = (last["position"] if last else 0) + 1
        cur = conn.execute(
            "INSERT INTO message (chat_id, position, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, position, body.role, body.content, db.now()),
        )
    return {"id": cur.lastrowid, "position": position}


@app.patch("/api/chats/{chat_id}")
def patch_chat(chat_id: int, body: ChatPatchIn):
    """Промпт, заголовок, закрепление. Промпт действует только на этот чат."""
    with db.session() as conn:
        _chat_or_404(conn, chat_id)
        if body.system_prompt is not None:
            conn.execute(
                "UPDATE chat SET system_prompt = ? WHERE id = ?",
                (body.system_prompt, chat_id),
            )
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise HTTPException(400, "Пустой заголовок")
            conn.execute("UPDATE chat SET title = ? WHERE id = ?", (title, chat_id))
        if body.pinned is not None:
            conn.execute(
                "UPDATE chat SET pinned = ? WHERE id = ?", (int(body.pinned), chat_id)
            )
    return {"ok": True}


# ------------------------------------------------------- форк и удаление

@app.post("/api/messages/{message_id}/fork")
def fork_from_message(message_id: int):
    """Новый чат с копией истории до этого сообщения включительно.

    Только от ответа бота: копия обязана заканчиваться его репликой, иначе
    следующее сообщение пользователя станет вторым подряд. Форк от последнего
    ответа — это просто дубликат чата.
    """
    with db.session() as conn:
        message = conn.execute(
            "SELECT * FROM message WHERE id = ?", (message_id,)
        ).fetchone()
        if not message:
            raise HTTPException(404, "Сообщение не найдено")
        if message["role"] != "assistant":
            raise HTTPException(
                400,
                "Форк делается от ответа бота: копия должна заканчиваться "
                "его репликой, иначе дальше пойдут два твоих сообщения подряд.",
            )
        source = _chat_or_404(conn, message["chat_id"])
        persona = conn.execute(
            "SELECT * FROM persona WHERE id = ?", (source["persona_id"],)
        ).fetchone()

        number = persona["next_chat_number"]
        conn.execute(
            "UPDATE persona SET next_chat_number = ? WHERE id = ?",
            (number + 1, persona["id"]),
        )
        cur = conn.execute(
            "INSERT INTO chat (persona_id, number, title, system_prompt, "
            "forked_from_chat_id, forked_at_position, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                persona["id"],
                number,
                f"{number:02d}",
                source["system_prompt"],
                source["id"],
                message["position"],
                db.now(),
            ),
        )
        new_id = cur.lastrowid
        conn.execute(
            "INSERT INTO message "
            "(chat_id, position, role, content, reasoning_content, created_at) "
            "SELECT ?, position, role, content, reasoning_content, ? "
            "FROM message WHERE chat_id = ? AND position <= ? ORDER BY position",
            (new_id, db.now(), source["id"], message["position"]),
        )
        copied = conn.execute(
            "SELECT COUNT(*) AS c FROM message WHERE chat_id = ?", (new_id,)
        ).fetchone()["c"]

    return {"id": new_id, "title": f"{number:02d}", "copied": copied}


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: int):
    """Безвозвратно: сообщения и промпт стираются, вернуть их нельзя.

    Остаётся обезличенная строка — только номер — чтобы расходы по этому чату
    не выпали из статистики. Журнал вызовов не трогаем никогда.
    """
    with db.session() as conn:
        chat = _chat_or_404(conn, chat_id)
        conn.execute("DELETE FROM message WHERE chat_id = ?", (chat_id,))
        conn.execute(
            "UPDATE chat SET deleted_at = ?, system_prompt = '', title = ? "
            "WHERE id = ?",
            (db.now(), f"Удалённый чат №{chat['number']:02d}", chat_id),
        )
    db.vacuum()
    return {"ok": True}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ------------------------------------------------------------ статистика

def _breakdown(rows) -> dict:
    """Разбор журнала вызовов.

    Цена по категориям пересчитывается из токенов по тарифу того вызова,
    а не делится задним числом по текущему прайсу.
    """
    tokens = {"cache_hit": 0, "cache_miss": 0, "output": 0, "reasoning": 0}
    cost = {"cache_hit": 0.0, "cache_miss": 0.0, "output": 0.0}
    by_tariff = {"peak": 0.0, "offpeak": 0.0}
    total = 0.0

    for r in rows:
        prices = config.PRICES.get(r["tariff"], config.PRICES["offpeak"])
        tokens["cache_hit"] += r["cache_hit_tokens"]
        tokens["cache_miss"] += r["cache_miss_tokens"]
        tokens["output"] += r["output_tokens"]
        tokens["reasoning"] += r["reasoning_tokens"]
        cost["cache_hit"] += r["cache_hit_tokens"] * prices["cache_hit"] / 1e6
        cost["cache_miss"] += r["cache_miss_tokens"] * prices["cache_miss"] / 1e6
        cost["output"] += r["output_tokens"] * prices["output"] / 1e6
        by_tariff[r["tariff"]] = by_tariff.get(r["tariff"], 0.0) + r["cost_usd"]
        total += r["cost_usd"]

    mc = pricing.to_millicents
    return {
        "calls": len(rows),
        "tokens": tokens,
        "cost": {k: mc(v) for k, v in cost.items()},
        "by_tariff": {k: mc(v) for k, v in by_tariff.items()},
        "total": mc(total),
    }


CALL_COLUMNS = (
    "SELECT cache_hit_tokens, cache_miss_tokens, output_tokens, "
    "reasoning_tokens, prompt_tokens, tariff, cost_usd, created_at FROM api_call"
)


@app.get("/api/stats/message/{message_id}")
def stats_message(message_id: int):
    with db.session() as conn:
        rows = conn.execute(
            CALL_COLUMNS + " WHERE message_id = ?", (message_id,)
        ).fetchall()
    if not rows:
        raise HTTPException(404, "Это сообщение написано вручную — запроса к API не было")
    data = _breakdown(rows)
    data["title"] = "Этот ответ"
    data["context_tokens"] = rows[-1]["prompt_tokens"]
    data["at"] = rows[-1]["created_at"]
    return data


@app.get("/api/stats/chat/{chat_id}")
def stats_chat(chat_id: int):
    with db.session() as conn:
        chat = conn.execute("SELECT * FROM chat WHERE id = ?", (chat_id,)).fetchone()
        if not chat:
            raise HTTPException(404, "Чат не найден")
        rows = conn.execute(CALL_COLUMNS + " WHERE chat_id = ?", (chat_id,)).fetchall()
        last = conn.execute(
            "SELECT prompt_tokens FROM api_call WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    data = _breakdown(rows)
    data["title"] = f"Чат {chat['title']}"
    data["context_tokens"] = last["prompt_tokens"] if last else 0
    data["context_limit"] = config.CONTEXT_LIMIT
    return data


@app.get("/api/stats/persona/{persona_id}")
def stats_persona(persona_id: int):
    with db.session() as conn:
        persona = conn.execute(
            "SELECT * FROM persona WHERE id = ?", (persona_id,)
        ).fetchone()
        if not persona:
            raise HTTPException(404, "Личность не найдена")
        rows = conn.execute(
            CALL_COLUMNS + " WHERE persona_id = ?", (persona_id,)
        ).fetchall()
        per_chat = conn.execute(
            "SELECT c.chat_id, COALESCE(ch.title, 'Удалённый чат') AS title, "
            "COUNT(*) AS calls, SUM(c.cost_usd) AS cost "
            "FROM api_call c LEFT JOIN chat ch ON ch.id = c.chat_id "
            "WHERE c.persona_id = ? GROUP BY c.chat_id ORDER BY cost DESC",
            (persona_id,),
        ).fetchall()
    data = _breakdown(rows)
    data["title"] = f"Личность «{persona['name']}»"
    data["rows"] = [
        {
            "label": r["title"],
            "calls": r["calls"],
            "total": pricing.to_millicents(r["cost"]),
        }
        for r in per_chat
    ]
    data["rows_header"] = "По чатам"
    return data


@app.get("/api/stats/all")
def stats_all():
    with db.session() as conn:
        rows = conn.execute(CALL_COLUMNS).fetchall()
        per_persona = conn.execute(
            "SELECT c.persona_id, COALESCE(p.name, 'Удалённая личность') AS name, "
            "COUNT(*) AS calls, SUM(c.cost_usd) AS cost "
            "FROM api_call c LEFT JOIN persona p ON p.id = c.persona_id "
            "GROUP BY c.persona_id ORDER BY cost DESC"
        ).fetchall()
        first = conn.execute(
            "SELECT created_at FROM api_call ORDER BY id LIMIT 1"
        ).fetchone()
    data = _breakdown(rows)
    data["title"] = "За всё время"
    data["since"] = first["created_at"] if first else None
    data["rows"] = [
        {
            "label": r["name"],
            "calls": r["calls"],
            "total": pricing.to_millicents(r["cost"]),
        }
        for r in per_persona
    ]
    data["rows_header"] = "По личностям"
    return data


@app.get("/api/balance")
def balance():
    """Настоящий остаток у DeepSeek — для сверки с нашим подсчётом."""
    try:
        return deepseek_client.balance()
    except Exception as exc:
        raise HTTPException(502, f"Не удалось спросить баланс: {exc}")


# -------------------------------------------------------------------- статика

class NoCacheStatic(StaticFiles):
    """Статика без кэширования.

    Без заголовка Cache-Control браузер вправе взять свою копию файла
    вообще не спрашивая сервер, и правки в CSS или JS до экрана не
    доезжают. Для локального инструмента кэшировать нечего: файлы лежат
    на том же диске, читаются мгновенно.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/static", NoCacheStatic(directory=config.BASE_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(
        config.BASE_DIR / "static" / "index.html",
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    import threading
    import webbrowser

    import uvicorn

    url = f"http://{config.HOST}:{config.PORT}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"DeepSeek Chat: {url}")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
