"""Локальный веб-интерфейс к DeepSeek."""

import json
import re
import time
import urllib.parse
from collections.abc import Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import db
import deepseek_client
import passwords
import pricing

app = FastAPI(title="DeepSeek Chat")
db.init()

CURRENT_PERSONA = "current_persona_id"

# Личности, открытые паролем в этом запуске. Живёт только в памяти: закрыл
# приложение — заперто снова. Это замок на интерфейсе, а не шифрование:
# тот, кто откроет сам chats.db, прочитает всё и без пароля. Так решено
# сознательно, и об этом сказано и в окне настроек, и в README.
UNLOCKED: set[int] = set()


def _is_locked(persona_id: int) -> bool:
    if persona_id in UNLOCKED:
        return False
    with db.session() as conn:
        row = conn.execute(
            "SELECT password_hash FROM persona WHERE id = ?", (persona_id,)
        ).fetchone()
    return bool(row and row["password_hash"])


def _persona_of_chat(chat_id: int) -> int | None:
    with db.session() as conn:
        row = conn.execute(
            "SELECT persona_id FROM chat WHERE id = ?", (chat_id,)
        ).fetchone()
    return row["persona_id"] if row else None


def _persona_of_message(message_id: int) -> int | None:
    with db.session() as conn:
        row = conn.execute(
            "SELECT c.persona_id FROM message m JOIN chat c ON c.id = m.chat_id "
            "WHERE m.id = ?",
            (message_id,),
        ).fetchone()
    return row["persona_id"] if row else None


def _persona_for_path(path: str) -> int | None:
    """Чью личность трогает этот адрес. None — проверять нечего."""
    parts = path.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "api":
        return None
    kind, ident = parts[1], parts[2]
    tail = parts[3] if len(parts) > 3 else ""
    if kind == "stats":  # /api/stats/chat/5 — то же самое, но на шаг глубже
        if len(parts) < 4:
            return None
        kind, ident, tail = parts[2] + "s", parts[3], ""
    if not ident.isdigit():
        return None
    ident = int(ident)
    if kind == "personas":
        return None if tail == "unlock" else ident
    if kind == "chats":
        return _persona_of_chat(ident)
    if kind == "messages":
        return _persona_of_message(ident)
    return None


@app.middleware("http")
async def lock_guard(request: Request, call_next):
    """Запертую личность не отдаём никому — ни одному маршруту.

    Проверка здесь, а не в каждом обработчике: маршрутов два десятка, и
    забытый обработчик означал бы, что чат запертой личности открывается
    по прямой ссылке.
    """
    persona_id = _persona_for_path(request.url.path)
    if persona_id is not None and _is_locked(persona_id):
        return JSONResponse(
            {"detail": "Личность заперта паролем"}, status_code=423
        )
    return await call_next(request)


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
            {
                "id": r["id"],
                "name": r["name"],
                "thinking_enabled": r["thinking_enabled"],
                "locked": bool(r["password_hash"]) and r["id"] not in UNLOCKED,
            }
            for r in conn.execute(
                "SELECT id, name, thinking_enabled, password_hash, hidden "
                "FROM persona WHERE deleted_at IS NULL ORDER BY id"
            )
            # Скрытая личность не упоминается вообще: ни строкой, ни числом.
            # Иначе по одному её присутствию было бы видно, что она есть.
            if not r["hidden"] or r["id"] in UNLOCKED
        ]
        current = db.get_state(conn, CURRENT_PERSONA)
        current_id = int(current) if current else None
        if current_id and not any(p["id"] == current_id for p in personas):
            current_id = None
        if current_id is None and personas:
            current_id = personas[0]["id"]
            db.set_state(conn, CURRENT_PERSONA, str(current_id))
        theme = db.get_state(conn, "theme") or "dark"
    current = next((p for p in personas if p["id"] == current_id), None)
    return {
        "personas": personas,
        "current_persona_id": current_id,
        "current_locked": bool(current and current["locked"]),
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


class PasswordIn(BaseModel):
    password: str = ""


class PasswordSetIn(BaseModel):
    password: str | None = None  # пусто или None — снять пароль
    hidden: bool | None = None


WRONG_PASSWORD_DELAY = 0.5  # не защита, а помеха перебору вслепую


MASTER_KEY = "master_password"


def _master_hash(conn) -> str | None:
    return db.get_state(conn, MASTER_KEY) or None


def _opens_anything(conn, password: str) -> bool:
    """Знает ли человек хоть один пароль в этом приложении.

    Нужно, чтобы поставить общий пароль было не проще, чем открыть личность.
    Иначе он был бы чёрным ходом: пришёл к незапертому компьютеру, завёл
    общий пароль, им же всё и открыл.
    """
    master = _master_hash(conn)
    if master and passwords.check_password(password, master):
        return True
    rows = conn.execute(
        "SELECT password_hash FROM persona "
        "WHERE password_hash IS NOT NULL AND deleted_at IS NULL"
    ).fetchall()
    return any(passwords.check_password(password, r["password_hash"]) for r in rows)


def _anything_locked(conn) -> bool:
    master = _master_hash(conn)
    has_persona_password = conn.execute(
        "SELECT 1 FROM persona WHERE password_hash IS NOT NULL "
        "AND deleted_at IS NULL LIMIT 1"
    ).fetchone()
    return bool(master or has_persona_password)


def _unlock_all(conn) -> int:
    rows = conn.execute(
        "SELECT id FROM persona WHERE password_hash IS NOT NULL "
        "AND deleted_at IS NULL"
    ).fetchall()
    UNLOCKED.update(r["id"] for r in rows)
    return len(rows)


@app.get("/api/master")
def master_state():
    with db.session() as conn:
        return {"has_master": bool(_master_hash(conn))}


class MasterIn(BaseModel):
    password: str = ""  # пусто — снять общий пароль
    proof: str = ""     # любой действующий пароль: общий или личности


@app.post("/api/master")
def set_master(body: MasterIn):
    with db.session() as conn:
        if _anything_locked(conn) and not _opens_anything(conn, body.proof):
            time.sleep(WRONG_PASSWORD_DELAY)
            raise HTTPException(
                403,
                "Этот пароль не подошёл. В поле «действующий» нужен пароль, "
                "который уже работает: общий или любой личности. Если поле "
                "заполнил браузер — сотри и впиши вручную.",
            )
        password = body.password.strip()
        if password:
            db.set_state(conn, MASTER_KEY, passwords.hash_password(password))
            has_master = True
        else:
            db.set_state(conn, MASTER_KEY, "")
            has_master = False
    return {"has_master": has_master}


@app.post("/api/personas/{persona_id}/unlock")
def unlock_persona(persona_id: int, body: PasswordIn):
    with db.session() as conn:
        row = conn.execute(
            "SELECT password_hash FROM persona WHERE id = ? AND deleted_at IS NULL",
            (persona_id,),
        ).fetchone()
        master = _master_hash(conn)
    own = row and passwords.check_password(body.password, row["password_hash"])
    if not own and not (master and passwords.check_password(body.password, master)):
        time.sleep(WRONG_PASSWORD_DELAY)
        raise HTTPException(403, "Пароль не подошёл")
    if own:
        UNLOCKED.add(persona_id)
        return {"ok": True, "opened": 1}
    # Общий пароль открывает всё разом — в том числе ту, куда стучались.
    with db.session() as conn:
        opened = _unlock_all(conn)
    return {"ok": True, "opened": opened}


@app.post("/api/unlock-hidden")
def unlock_hidden(body: PasswordIn):
    """Вход в скрытую личность — по одному паролю, без имени.

    Имя спрашивать нельзя: пришлось бы либо показывать список скрытых, либо
    отвечать «такой нет», а это уже ответ на вопрос, есть ли она. Ответ
    здесь один и тот же и когда пароль неверный, и когда скрытых нет вовсе.
    """
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, name, password_hash FROM persona "
            "WHERE hidden = 1 AND deleted_at IS NULL"
        ).fetchall()
    # Открываем ВСЕ скрытые личности с этим паролем, а не первую попавшуюся.
    # Одинаковые пароли мы разрешаем, и раньше вторая такая личность просто
    # не открывалась: перебор останавливался на первом совпадении.
    matched = [
        row
        for row in rows
        if passwords.check_password(body.password, row["password_hash"])
    ]
    if matched:
        UNLOCKED.update(row["id"] for row in matched)
        first = matched[0]
        return {
            "persona_id": first["id"],
            "name": first["name"],
            "opened": len(matched),
        }

    with db.session() as conn:
        master = _master_hash(conn)
        if master and passwords.check_password(body.password, master):
            opened = _unlock_all(conn)
            first = rows[0] if rows else None
            return {
                "persona_id": first["id"] if first else None,
                "name": first["name"] if first else None,
                "opened": opened,
            }
    time.sleep(WRONG_PASSWORD_DELAY)
    raise HTTPException(403, "Ничего не открылось")


@app.post("/api/personas/{persona_id}/lock")
def lock_persona(persona_id: int):
    """Запереть обратно, не перезапуская приложение."""
    UNLOCKED.discard(persona_id)
    return {"ok": True}


@app.post("/api/personas/{persona_id}/password")
def set_password(persona_id: int, body: PasswordSetIn):
    """Поставить, сменить или снять пароль, и заодно скрытие.

    Скрытие без пароля запрещено: в переключателе такой личности нет, а
    открыть её нечем — она пропала бы насовсем.
    """
    with db.session() as conn:
        row = conn.execute(
            "SELECT password_hash, hidden FROM persona "
            "WHERE id = ? AND deleted_at IS NULL",
            (persona_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Личность не найдена")

        has_password = bool(row["password_hash"])
        hidden = bool(row["hidden"])
        if body.password is not None:
            password = body.password.strip()
            if password:
                conn.execute(
                    "UPDATE persona SET password_hash = ? WHERE id = ?",
                    (passwords.hash_password(password), persona_id),
                )
                has_password = True
                UNLOCKED.add(persona_id)  # кто поставил, тот уже внутри
            else:
                conn.execute(
                    "UPDATE persona SET password_hash = NULL, hidden = 0 "
                    "WHERE id = ?",
                    (persona_id,),
                )
                has_password = False
                hidden = False

        if body.hidden is not None:
            hidden = bool(body.hidden)
            if hidden and not has_password:
                raise HTTPException(
                    400, "Скрывать можно только личность с паролем — иначе её не открыть"
                )
            conn.execute(
                "UPDATE persona SET hidden = ? WHERE id = ?", (int(hidden), persona_id)
            )

    return {"has_password": has_password, "hidden": hidden}


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
            "SELECT id, name, system_prompt, thinking_enabled, password_hash, hidden "
            "FROM persona WHERE id = ? AND deleted_at IS NULL",
            (persona_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Личность не найдена")
    # Хеш наружу не отдаём никогда — интерфейсу достаточно знать, что пароль есть.
    return {
        "id": row["id"],
        "name": row["name"],
        "system_prompt": row["system_prompt"],
        "thinking_enabled": row["thinking_enabled"],
        "has_password": bool(row["password_hash"]),
        "hidden": bool(row["hidden"]),
    }


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


SEARCH_MIN = 2          # по одной букве искать бессмысленно
SEARCH_MAX_CHATS = 40
SEARCH_MAX_HITS = 3     # в боковом меню больше трёх строк на чат — каша
SNIPPET_PAD = 60        # символов вокруг совпадения


def _like_pattern(query: str) -> str:
    """Экранируем служебные символы LIKE, иначе «100%» найдёт что угодно."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.lower()}%"


def _snippet(text: str, query: str) -> dict:
    """Кусок вокруг совпадения плюс координаты, чтобы подсветить его в меню.

    Переводы строк меняем на пробелы один к одному: длина сохраняется,
    значит смещения остаются верными, а строка не рвётся посреди меню.
    """
    flat = text.replace("\n", " ").replace("\r", " ")
    at = flat.lower().find(query.lower())
    if at < 0:
        return {"text": flat[: SNIPPET_PAD * 2], "start": 0, "length": 0}
    start = max(0, at - SNIPPET_PAD)
    end = min(len(flat), at + len(query) + SNIPPET_PAD)
    head = "…" if start else ""
    tail = "…" if end < len(flat) else ""
    return {
        "text": head + flat[start:end] + tail,
        "start": at - start + len(head),
        "length": len(query),
    }


@app.get("/api/personas/{persona_id}/search")
def search_chats(persona_id: int, q: str = ""):
    """Поиск по названиям и тексту сообщений внутри одной личности.

    Только внутри: личности — изолированные пространства, и общий поиск
    вытаскивал бы в чужое окно то, что туда не клали. Ищем простым LIKE,
    без полнотекстового индекса: он потребовал бы менять схему базы, а
    выигрыш на личной переписке в тысячи сообщений не заметен.

    `lower` у SQLite умеет только латиницу, поэтому регистр снимаем своей
    функцией — иначе «Матан» не нашёлся бы по «матан».
    """
    q = q.strip()
    if len(q) < SEARCH_MIN:
        return {"query": q, "results": []}
    pattern = _like_pattern(q)

    with db.session() as conn:
        conn.create_function("lower_u", 1, lambda v: v.lower() if v else v)
        rows = conn.execute(
            "SELECT c.id AS chat_id, c.title, c.pinned, "
            "m.id AS message_id, m.position, m.role, m.content "
            "FROM chat c LEFT JOIN message m "
            "  ON m.chat_id = c.id AND lower_u(m.content) LIKE ? ESCAPE '\\' "
            "WHERE c.persona_id = ? AND c.deleted_at IS NULL "
            "  AND (m.id IS NOT NULL OR lower_u(c.title) LIKE ? ESCAPE '\\') "
            "ORDER BY c.pinned DESC, c.id DESC, m.position",
            (pattern, persona_id, pattern),
        ).fetchall()

    found: dict[int, dict] = {}
    for row in rows:
        chat = found.get(row["chat_id"])
        if chat is None:
            if len(found) >= SEARCH_MAX_CHATS:
                continue
            chat = found[row["chat_id"]] = {
                "chat_id": row["chat_id"],
                "title": row["title"],
                "pinned": bool(row["pinned"]),
                "title_match": q.lower() in (row["title"] or "").lower(),
                "total": 0,
                "hits": [],
            }
        if row["message_id"] is None:
            continue
        chat["total"] += 1
        if len(chat["hits"]) < SEARCH_MAX_HITS:
            chat["hits"].append(
                {
                    "message_id": row["message_id"],
                    "role": row["role"],
                    "position": row["position"],
                    **_snippet(row["content"], q),
                }
            )

    return {"query": q, "results": list(found.values())}


@app.post("/api/personas/{persona_id}/chats")
def create_chat(persona_id: int):
    with db.session() as conn:
        persona = conn.execute(
            "SELECT * FROM persona WHERE id = ? AND deleted_at IS NULL", (persona_id,)
        ).fetchone()
        if not persona:
            raise HTTPException(404, "Личность не найдена")
        # Пустой чат уже есть — отдаём его, а не плодим второй. Кнопка в
        # интерфейсе в этот момент выключена, но запрос всё равно может
        # прийти — из второго окна или от двух нажатий подряд.
        empty = conn.execute(
            "SELECT id, number, title FROM chat "
            "WHERE persona_id = ? AND deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM message WHERE chat_id = chat.id) "
            "ORDER BY id DESC LIMIT 1",
            (persona_id,),
        ).fetchone()
        if empty:
            return {
                "id": empty["id"],
                "number": empty["number"],
                "title": empty["title"],
                "reused": True,
            }
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
    return {"id": chat_id, "number": number, "title": title, "reused": False}


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
        title = _fork_title(conn, persona["id"], source["title"], number)
        cur = conn.execute(
            "INSERT INTO chat (persona_id, number, title, system_prompt, "
            "forked_from_chat_id, forked_at_position, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                persona["id"],
                number,
                title,
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

    return {"id": new_id, "title": title, "copied": copied}


FORK_SUFFIX = re.compile(r"\s*\(форк(?:\s+\d+)?\)$")


def _fork_title(conn, persona_id: int, source_title: str, number: int) -> str:
    """«матан» → «матан (форк)», а форк форка — снова от «матан».

    Номер остаётся в поле number и нужен для обезличенной строки в статистике,
    но в заголовок больше не идёт: по названию видно, из чего ветка выросла.
    Одинаковых автоимён не делаем — иначе в меню два «матан (форк)» подряд.
    """
    base = FORK_SUFFIX.sub("", source_title).strip() or f"{number:02d}"
    taken = {
        r["title"]
        for r in conn.execute(
            "SELECT title FROM chat WHERE persona_id = ? AND deleted_at IS NULL",
            (persona_id,),
        ).fetchall()
    }
    title = f"{base} (форк)"
    n = 2
    while title in taken:
        title = f"{base} (форк {n})"
        n += 1
    return title


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: int):
    """Безвозвратно: сообщения и промпт стираются, вернуть их нельзя.

    Остаётся обезличенная строка — только номер — чтобы расходы по этому чату
    не выпали из статистики. Журнал вызовов не трогаем никогда.
    """
    with db.session() as conn:
        _wipe_chat(conn, _chat_or_404(conn, chat_id))
    db.vacuum()
    return {"ok": True}


def _wipe_chat(conn, chat) -> None:
    """Сообщения и промпт стираются, остаётся обезличенная строка с номером."""
    conn.execute("DELETE FROM message WHERE chat_id = ?", (chat["id"],))
    conn.execute(
        "UPDATE chat SET deleted_at = ?, system_prompt = '', title = ? "
        "WHERE id = ?",
        (db.now(), f"Удалённый чат №{chat['number']:02d}", chat["id"]),
    )


@app.delete("/api/personas/{persona_id}/chats")
def clear_chats(persona_id: int):
    """Все чаты личности безвозвратно, но сама личность остаётся.

    Имя, системный промпт, нумерация и статистика не трогаются: расходы
    живут в отдельном журнале, из которого не удаляется ничего.
    """
    with db.session() as conn:
        persona = conn.execute(
            "SELECT id FROM persona WHERE id = ? AND deleted_at IS NULL",
            (persona_id,),
        ).fetchone()
        if not persona:
            raise HTTPException(404, "Личность не найдена")
        chats = conn.execute(
            "SELECT id, number FROM chat "
            "WHERE persona_id = ? AND deleted_at IS NULL",
            (persona_id,),
        ).fetchall()
        for chat in chats:
            _wipe_chat(conn, chat)
    db.vacuum()
    return {"cleared": len(chats)}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------- экспорт

EXPORT_MEDIA = {
    "md": "text/markdown; charset=utf-8",
    "json": "application/json; charset=utf-8",
}
ROLE_TITLE = {"user": "Ты", "assistant": "Бот", "system": "Система"}


def _day(stamp: str) -> str:
    """ISO с секундами и зоной — в человеческое «2026-11-15 22:10»."""
    return (stamp or "").replace("T", " ")[:16]


def _quoted(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def _collect(conn, persona, chat_rows: list) -> dict:
    """Всё, что знает база о выбранных чатах, одним словарём."""
    chats = []
    for chat in chat_rows:
        messages = conn.execute(
            "SELECT id, position, role, content, reasoning_content, created_at "
            "FROM message WHERE chat_id = ? ORDER BY position",
            (chat["id"],),
        ).fetchall()
        costs = {
            r["message_id"]: r["cost_usd"]
            for r in conn.execute(
                "SELECT message_id, cost_usd FROM api_call "
                "WHERE chat_id = ? AND message_id IS NOT NULL",
                (chat["id"],),
            )
        }
        source = None
        if chat["forked_from_chat_id"]:
            row = conn.execute(
                "SELECT title FROM chat WHERE id = ?", (chat["forked_from_chat_id"],)
            ).fetchone()
            source = row["title"] if row else None
        chats.append(
            {
                "title": chat["title"],
                "number": chat["number"],
                "created_at": chat["created_at"],
                "pinned": bool(chat["pinned"]),
                "system_prompt": chat["system_prompt"],
                "forked_from": source,
                "forked_at_position": chat["forked_at_position"],
                "messages": [
                    {
                        "position": m["position"],
                        "role": m["role"],
                        "content": m["content"],
                        "reasoning_content": m["reasoning_content"],
                        "created_at": m["created_at"],
                        "cost_millicents": (
                            pricing.to_millicents(costs[m["id"]])
                            if costs.get(m["id"])
                            else None
                        ),
                    }
                    for m in messages
                ],
            }
        )
    return {
        "app": "deepseek_chat",
        "format": 1,
        "exported_at": db.now(),
        "persona": {
            "name": persona["name"],
            "system_prompt": persona["system_prompt"],
            "thinking_enabled": bool(persona["thinking_enabled"]),
        },
        "chats": chats,
    }


def _markdown(payload: dict, single: bool) -> str:
    """Переписка так, как её читают. Размышления и цены остаются в JSON."""
    persona = payload["persona"]
    chats = payload["chats"]
    out: list[str] = []

    if single and chats:
        chat = chats[0]
        out += [
            f"# {chat['title']}",
            "",
            f"Личность «{persona['name']}» · создан {_day(chat['created_at'])}"
            f" · выгружено {_day(payload['exported_at'])}",
            "",
        ]
    else:
        out += [
            f"# Личность «{persona['name']}»",
            "",
            f"Чатов: {len(chats)} · выгружено {_day(payload['exported_at'])}",
            "",
        ]

    for chat in chats:
        if not single:
            out += ["---", "", f"## {chat['title']}", ""]
        if chat["forked_from"]:
            out += [f"Форк из «{chat['forked_from']}».", ""]
        prompt = (chat["system_prompt"] or "").strip()
        if prompt:
            out += ["**Системный промпт**", "", _quoted(prompt), ""]
        for message in chat["messages"]:
            who = ROLE_TITLE.get(message["role"], message["role"])
            out += [f"**{who}**", "", (message["content"] or "").strip(), ""]

    out += [
        "---",
        "",
        "Выгружено из локального DeepSeek Chat. Размышления модели и цены",
        "в этот формат не попадают — они есть в выгрузке JSON.",
        "",
    ]
    return "\n".join(out)


def _attachment(text: str, name: str, fmt: str) -> Response:
    """Имя файла отдаём дважды: латиницей и по RFC 5987, с кириллицей."""
    stem = re.sub(r"[^\w .-]+", "", name, flags=re.UNICODE).strip() or "export"
    filename = f"{stem} {_day(db.now())[:10]}.{fmt}"
    # Кириллица из запасного имени выпадает целиком, и остаётся одна дата —
    # поэтому подставляем осмысленное имя: это имя видят только браузеры,
    # не понимающие filename*, современные берут полное.
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-")
    if not fallback or fallback[0].isdigit():
        fallback = f"deepseek-chat-{fallback}" if fallback else f"export.{fmt}"
    return Response(
        content=text.encode("utf-8"),
        media_type=EXPORT_MEDIA[fmt],
        headers={
            "Content-Disposition": (
                f'attachment; filename="{fallback}"; '
                f"filename*=UTF-8''{urllib.parse.quote(filename)}"
            )
        },
    )


def _export(conn, persona, chat_rows, fmt: str, single: bool) -> Response:
    payload = _collect(conn, persona, chat_rows)
    if fmt == "json":
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = _markdown(payload, single)
    name = payload["chats"][0]["title"] if single else f"Личность {persona['name']}"
    return _attachment(text, name, fmt)


@app.get("/api/chats/{chat_id}/export")
def export_chat(chat_id: int, fmt: str = "md"):
    if fmt not in EXPORT_MEDIA:
        raise HTTPException(400, "Формат бывает md или json")
    with db.session() as conn:
        chat = _chat_or_404(conn, chat_id)
        persona = conn.execute(
            "SELECT * FROM persona WHERE id = ?", (chat["persona_id"],)
        ).fetchone()
        return _export(conn, persona, [chat], fmt, single=True)


@app.get("/api/personas/{persona_id}/export")
def export_persona(persona_id: int, fmt: str = "md"):
    if fmt not in EXPORT_MEDIA:
        raise HTTPException(400, "Формат бывает md или json")
    with db.session() as conn:
        persona = conn.execute(
            "SELECT * FROM persona WHERE id = ? AND deleted_at IS NULL", (persona_id,)
        ).fetchone()
        if not persona:
            raise HTTPException(404, "Личность не найдена")
        chats = conn.execute(
            "SELECT * FROM chat WHERE persona_id = ? AND deleted_at IS NULL "
            "ORDER BY pinned DESC, id",
            (persona_id,),
        ).fetchall()
        return _export(conn, persona, chats, fmt, single=False)


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
        # Скрытые личности считаются наравне со всеми, но без имени:
        # «Скрытая личность №1». Деньги потрачены по-настоящему, и сумма,
        # которая зависит от того, что сейчас открыто, не сумма, а фокус.
        # Цена честности: по такой строке видно, что скрытые есть.
        # Нумеруем только те, что сейчас и правда скрыты: иначе в списке
        # остались бы дыры (№1, №3), а дыра — это тоже сведения.
        still_hidden = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM persona WHERE hidden = 1 ORDER BY id"
            ).fetchall()
            if row["id"] not in UNLOCKED
        ]
        hidden_names = {
            persona_id: f"Скрытая личность №{number}"
            for number, persona_id in enumerate(still_hidden, start=1)
        }
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
            "label": hidden_names.get(r["persona_id"], r["name"]),
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
