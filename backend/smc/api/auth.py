"""Dashboard kimlik doğrulama: PBKDF2-SHA256 hash, session cookie, CSRF token, login rate limit."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

ITERATIONS = 390_000


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


class LoginRateLimiter:
    def __init__(self, max_attempts: int = 5, window_s: float = 900):
        self.max = max_attempts
        self.window = window_s
        self.attempts: dict[str, list[float]] = {}

    def allowed(self, key: str) -> bool:
        now = time.time()
        lst = [t for t in self.attempts.get(key, []) if now - t < self.window]
        self.attempts[key] = lst
        return len(lst) < self.max

    def fail(self, key: str) -> None:
        self.attempts.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        self.attempts.pop(key, None)
