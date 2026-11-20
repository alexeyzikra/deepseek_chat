"""Пароли личностей.

Содержимое базы они не шифруют — это замок на интерфейсе, а не сейф.
Но сам пароль человек вполне мог взять тот же, что и где-то ещё, поэтому
в базе лежит не он, а медленный хеш с солью: даже укравший файл не получит
готовую строку для подстановки в чужие двери.
"""

import hashlib
import hmac
import os

ALGO = "pbkdf2_sha256"
ITERATIONS = 240_000
SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"{ALGO}${ITERATIONS}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != ALGO:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    # Сравнение постоянного времени: иначе по задержке можно подбирать побайтно.
    return hmac.compare_digest(digest.hex(), digest_hex)
