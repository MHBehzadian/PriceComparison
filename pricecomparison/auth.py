"""Accounts: PBKDF2 password hashes and HMAC-signed session cookies.

Two roles. An admin manages everything (products, links, sites, imports,
settings, users); a user sees the panel and keeps their own folders.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from .store import replace

COOKIE = "pc_session"
SESSION_TTL = 7 * 24 * 3600
ADMIN, USER = "admin", "user"
ROLES = (ADMIN, USER)


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


class AuthError(Exception):
    pass


class Auth:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        self._mtime = 0.0
        self._reload()
        if not self.data.get("secret"):
            self.data["secret"] = secrets.token_hex(32)
            self._save()

    def _reload(self) -> None:
        """Pick up a password changed from the command line (`passwd`) while
        the panel is running."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self._mtime:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            self._mtime = mtime
            self._migrate()

    def _migrate(self) -> None:
        # v0.1 had one account at the top level: it becomes the first admin
        if "hash" in self.data:
            name = self.data.pop("username", "admin")
            self.data.setdefault("users", {})[name] = {
                "salt": self.data.pop("salt"), "hash": self.data.pop("hash"), "role": ADMIN, "ver": 0, "created": time.time(),
            }
            self.data.setdefault("users", {})
            self._save()
        self.data.setdefault("users", {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    # ---- accounts ------------------------------------------------------
    @property
    def users(self) -> dict[str, dict]:
        self._reload()
        return self.data.setdefault("users", {})

    @property
    def configured(self) -> bool:
        return any(u.get("role") == ADMIN for u in self.users.values())

    def role(self, username: str) -> str | None:
        u = self.users.get(username)
        return u.get("role") if u else None

    def list(self) -> list[dict]:
        return [{"username": n, "role": u.get("role"), "created": u.get("created", 0)} for n, u in sorted(self.users.items())]

    def set_password(self, username: str, password: str, role: str | None = None) -> None:
        """Create the account, or change its password (and role). Sessions
        of that account end; other people stay logged in."""
        username = username.strip()
        if not username:
            raise AuthError("نام کاربری خالی است")
        if len(password) < 6:
            raise AuthError("رمز باید حداقل ۶ حرف باشد")
        users = self.users
        u = users.get(username, {"role": role or USER, "ver": 0, "created": time.time()})
        salt = secrets.token_bytes(16)
        u.update(salt=salt.hex(), hash=_hash(password, salt), ver=u.get("ver", 0) + 1)
        if role:
            u["role"] = role
        users[username] = u
        self._save()

    def set_role(self, username: str, role: str) -> None:
        u = self.users.get(username)
        if u is None:
            raise AuthError("چنین کاربری نیست")
        if u.get("role") == ADMIN and role != ADMIN and self._admins() == 1:
            raise AuthError("حداقل یک مدیر باید بماند")
        u["role"] = role
        u["ver"] = u.get("ver", 0) + 1
        self._save()

    def delete(self, username: str) -> None:
        u = self.users.get(username)
        if u is None:
            raise AuthError("چنین کاربری نیست")
        if u.get("role") == ADMIN and self._admins() == 1:
            raise AuthError("آخرین مدیر را نمی‌شود حذف کرد")
        del self.users[username]
        self._save()

    def _admins(self) -> int:
        return sum(1 for u in self.users.values() if u.get("role") == ADMIN)

    def check(self, username: str, password: str) -> bool:
        u = self.users.get(username)
        if not u:
            _hash(password, b"0" * 16)  # same time whether or not the name exists
            return False
        return hmac.compare_digest(_hash(password, bytes.fromhex(u["salt"])), u["hash"])

    # ---- sessions ------------------------------------------------------
    def _sig(self, payload: str) -> str:
        return hmac.new(self.data["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, username: str) -> str:
        ver = self.users.get(username, {}).get("ver", 0)
        claims = {"u": username, "v": ver, "e": int(time.time()) + SESSION_TTL}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
        return f"{payload}.{self._sig(payload)}"

    def verify(self, token: str | None) -> dict | None:
        """The session's account as {"username", "role"}, or None. A token
        stops working when its account is deleted, its password or role
        changes, or it expires."""
        self._reload()
        if not token or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig(payload)):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload))
        except ValueError:
            return None
        u = self.users.get(data.get("u", ""))
        if not u or data.get("e", 0) <= time.time() or data.get("v") != u.get("ver", 0):
            return None
        return {"username": data["u"], "role": u.get("role", USER)}
