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


class SendIn(BaseModel):
    content: str


# ------------------------------------------------------------------ состояние

@app.get("/api/bootstrap")
def bootstrap():
    with db.connect() as conn:
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
    return {"personas": personas, "current_persona_id": current_id}


@app.post("/api/personas")
def create_persona(body: PersonaIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "У личности должно быть имя")
    with db.connect() as conn:
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
    with db.connect() as conn:
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
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, name, system_prompt, thinking_enabled FROM persona "
            "WHERE id = ? AND deleted_at IS NULL",
            (persona_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Личность не найдена")
    return dict(row)


# ---------------------------------------------------------------------- чаты

@app.get("/api/personas/{persona_id}/chats")
def list_chats(persona_id: int):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, number, title, pinned, created_at FROM chat "
            "WHERE persona_id = ? AND deleted_at IS NULL "
            "ORDER BY pinned DESC, id DESC",
            (persona_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/personas/{persona_id}/chats")
def create_chat(persona_id: int):
    with db.connect() as conn:
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
        cur = conn.execute(
            "INSERT INTO chat (persona_id, number, title, system_prompt, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (persona_id, number, f"Чат {number}", persona["system_prompt"], db.now()),
        )
        chat_id = cur.lastrowid
    return {"id": chat_id, "number": number, "title": f"Чат {number}"}


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: int):
    with db.connect() as conn:
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


# ------------------------------------------------------------------ отправка

def _history(conn, chat) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM message WHERE chat_id = ? ORDER BY position",
        (chat["id"],),
    ).fetchall()
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
            raise HTTPException(
                409,
                "История сломана: два сообщения подряд с ролью "
                f"«{previous}». Запрос не отправлен, поправь историю.",
            )
        previous = item["role"]


@app.post("/api/chats/{chat_id}/send")
def send(chat_id: int, body: SendIn):
    text = body.content.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")

    with db.connect() as conn:
        chat = conn.execute(
            "SELECT * FROM chat WHERE id = ? AND deleted_at IS NULL", (chat_id,)
        ).fetchone()
        if not chat:
            raise HTTPException(404, "Чат не найден")
        persona = conn.execute(
            "SELECT * FROM persona WHERE id = ?", (chat["persona_id"],)
        ).fetchone()

        position = (
            conn.execute(
                "SELECT COALESCE(MAX(position), 0) AS p FROM message WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()["p"]
            + 1
        )
        cur = conn.execute(
            "INSERT INTO message (chat_id, position, role, content, created_at) "
            "VALUES (?, ?, 'user', ?, ?)",
            (chat_id, position, text, db.now()),
        )
        user_message_id = cur.lastrowid
        payload = _history(conn, chat)

    try:
        _check_alternation(payload)
    except HTTPException:
        with db.connect() as conn:
            conn.execute("DELETE FROM message WHERE id = ?", (user_message_id,))
        raise

    thinking = bool(persona["thinking_enabled"])
    persona_id = chat["persona_id"]

    def events() -> Iterator[str]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict | None = None
        try:
            yield _sse({"type": "user_saved", "message_id": user_message_id})
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
            # Запрос не удался — убираем сообщение, чтобы история осталась целой.
            with db.connect() as conn:
                conn.execute("DELETE FROM message WHERE id = ?", (user_message_id,))
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
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO message "
                "(chat_id, position, role, content, reasoning_content, created_at) "
                "VALUES (?, ?, 'assistant', ?, ?, ?)",
                (
                    chat_id,
                    position + 1,
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


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# -------------------------------------------------------------------- статика

app.mount("/static", StaticFiles(directory=config.BASE_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(config.BASE_DIR / "static" / "index.html")


if __name__ == "__main__":
    import threading
    import webbrowser

    import uvicorn

    url = f"http://{config.HOST}:{config.PORT}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"DeepSeek Chat: {url}")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
