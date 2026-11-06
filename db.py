"""SQLite: схема и доступ.

journal_mode=DELETE, а не WAL: файл -wal хранил бы копии удалённых данных,
а удаление чата должно быть безвозвратным. secure_delete затирает
освобождённые страницы нулями вместо того, чтобы просто пометить их свободными.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS persona (
    id               INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL,
    system_prompt    TEXT    NOT NULL DEFAULT '',
    thinking_enabled INTEGER NOT NULL DEFAULT 0,
    next_chat_number INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT    NOT NULL,
    deleted_at       TEXT
);

CREATE TABLE IF NOT EXISTS chat (
    id                  INTEGER PRIMARY KEY,
    persona_id          INTEGER NOT NULL REFERENCES persona(id),
    number              INTEGER NOT NULL,
    title               TEXT    NOT NULL,
    system_prompt       TEXT    NOT NULL DEFAULT '',
    pinned              INTEGER NOT NULL DEFAULT 0,
    forked_from_chat_id INTEGER,
    forked_at_position  INTEGER,
    created_at          TEXT    NOT NULL,
    deleted_at          TEXT
);

CREATE TABLE IF NOT EXISTS message (
    id                INTEGER PRIMARY KEY,
    chat_id           INTEGER NOT NULL REFERENCES chat(id),
    position          INTEGER NOT NULL,
    role              TEXT    NOT NULL,
    content           TEXT    NOT NULL,
    reasoning_content TEXT,
    created_at        TEXT    NOT NULL
);

-- Журнал расходов. Только дозапись: строки не удаляются и не меняются
-- никогда, даже когда удалён чат или личность — деньги потрачены взаправду.
-- Внешних ключей сознательно нет, иначе удаление чата утянуло бы их за собой.
CREATE TABLE IF NOT EXISTS api_call (
    id                INTEGER PRIMARY KEY,
    created_at        TEXT    NOT NULL,
    persona_id        INTEGER,
    chat_id           INTEGER,
    message_id        INTEGER,
    model             TEXT    NOT NULL,
    cache_hit_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    tariff            TEXT    NOT NULL,
    cost_usd          REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_persona    ON chat(persona_id);
CREATE INDEX IF NOT EXISTS idx_message_chat    ON message(chat_id, position);
CREATE INDEX IF NOT EXISTS idx_call_chat       ON api_call(chat_id);
CREATE INDEX IF NOT EXISTS idx_call_persona    ON api_call(persona_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session():
    """Соединение, которое закрывается.

    У sqlite3 `with conn` только фиксирует транзакцию, но НЕ закрывает
    соединение — на каждый запрос оставался бы открытый файловый дескриптор.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)


def vacuum() -> None:
    """Пересобирает файл базы после удаления.

    secure_delete уже затирает освобождённые страницы нулями, VACUUM вдобавок
    выбрасывает их из файла. Запускается на отдельном соединении без
    транзакции: внутри транзакции SQLite VACUUM не выполняет.
    """
    conn = sqlite3.connect(config.DB_PATH, isolation_level=None)
    try:
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("VACUUM")
    finally:
        conn.close()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
