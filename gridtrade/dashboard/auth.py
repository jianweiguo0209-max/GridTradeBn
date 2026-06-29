"""登录鉴权：pbkdf2 密码哈希 + HMAC 签名会话 + 失败计数锁定。仅标准库。"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional


def hash_password(password: str, *, iterations: int = 200_000) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
    return 'pbkdf2$%d$%s$%s' % (iterations, salt.hex(), dk.hex())


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iters, salt_hex, hash_hex = encoded.split('$')
        if scheme != 'pbkdf2':
            return False
        dk = hashlib.pbkdf2_hmac('sha256', password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def make_session(username: str, secret: str, *, ttl_sec: int = 86400,
                 now_fn=time.time) -> str:
    payload = json.dumps({'u': username, 'exp': int(now_fn()) + ttl_sec}).encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return '%s.%s' % (_b64(payload), sig)


def verify_session(token: str, secret: str, *, now_fn=time.time) -> Optional[str]:
    try:
        payload_b64, sig = token.split('.')
        payload = _unb64(payload_b64)
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        data = json.loads(payload)
        if int(now_fn()) >= int(data['exp']):
            return None
        return data['u']
    except Exception:
        return None


class LoginThrottle:
    """按 key（用户名/IP）记失败次数；达 max_attempts 锁定 lockout_sec。内存态。"""

    def __init__(self, max_attempts: int = 5, lockout_sec: int = 3600,
                 now_fn=time.time):
        self.max_attempts = max_attempts
        self.lockout_sec = lockout_sec
        self._now = now_fn
        self._fails = {}          # key -> count
        self._locked_until = {}   # key -> ts

    def is_locked(self, key: str) -> bool:
        until = self._locked_until.get(key)
        if until is None:
            return False
        if self._now() >= until:
            self._locked_until.pop(key, None)
            self._fails.pop(key, None)
            return False
        return True

    def record_failure(self, key: str) -> None:
        self._fails[key] = self._fails.get(key, 0) + 1
        if self._fails[key] >= self.max_attempts:
            self._locked_until[key] = self._now() + self.lockout_sec

    def record_success(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked_until.pop(key, None)
