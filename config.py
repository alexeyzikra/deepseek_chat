"""Настройки. Всё, что может понадобиться поменять, собрано здесь."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

API_KEY = os.getenv("DEEPSEEK_API_KEY_CHAT")
BASE_URL = "https://api.deepseek.com"

MODEL = "deepseek-flash"

# Температура не действует, когда у личности включено размышление.
# Умолчание самого DeepSeek — 1.0; их рекомендации: код 0.0, разговор 1.3.
TEMPERATURE = 0.7

# None — отдать на усмотрение API (8K без размышления, 64K с ним).
MAX_TOKENS = None

CONTEXT_LIMIT = 1_000_000

DB_PATH = BASE_DIR / "chats.db"

HOST = "127.0.0.1"
PORT = 8765

# Доллары за 1M токенов, проверено по документации 2026-11-06.
PRICES = {
    "peak": {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20},
    "offpeak": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60},
}
