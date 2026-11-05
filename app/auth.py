"""Authentification du tableau de bord : sessions en base (jeton haché), rôles, protection CSRF.

Le client (tenant) vient TOUJOURS de la session, jamais de la requête.
"""
import time
from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text

from app.config import get_settings
from app.db import routing_session, tenant_session
from app.security import hash_token, new_session_token, verify_password

SESSION_COOKIE = "a24_session"
ROLE_RANK = {"readonly": 0, "employee": 1, "manager": 2, "owner": 3}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class AuthUser:
    id: UUID
    tenant_id: UUID
    email: str
    name: str
    role: str


class LoginLimiter:
    """Limite les tentatives par (IP, email). En mémoire : suffisant pour un processus, à externaliser ensuite."""

    def __init__(self, max_failures: int = 5, window_seconds: int = 900):
        self.max, self.window = max_failures, window_seconds
        self._fails: dict[str, list[float]] = defaultdict(list)

    def _recent(self, key: str) -> list[float]:
        now = time.monotonic()
        self._fails[key] = [t for t in self._fails[key] if now - t < self.window]
        return self._fails[key]

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.max

    def fail(self, key: str) -> None:
        self._recent(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


login_limiter = LoginLimiter()


def authenticate(email: str, password: str) -> AuthUser | None:
    email = email.strip().lower()
    with routing_session() as s:
        row = s.execute(text("SELECT user_id, tenant_id FROM login_index WHERE email = :e"), {"e": email}).first()
    if row is None:
        verify_password(None, password)  # égalise le temps de réponse
        return None
    with tenant_session(row.tenant_id) as s:
        u = s.execute(
            text("SELECT id, email, name, role, status, password_hash FROM users WHERE id = :u"), {"u": row.user_id}
        ).first()
        if u is None or u.status != "active" or not verify_password(u.password_hash, password):
            return None
        s.execute(text("UPDATE users SET last_login_at = now() WHERE id = :u"), {"u": u.id})
    return AuthUser(u.id, row.tenant_id, u.email, u.name, u.role)


def create_session(user: AuthUser) -> str:
    raw, hashed = new_session_token()
    with routing_session() as s:
        s.execute(
            text(
                "INSERT INTO sessions (token_hash, user_id, tenant_id, expires_at) "
                "VALUES (:h, :u, :t, now() + make_interval(hours => :hrs))"
            ),
            {"h": hashed, "u": user.id, "t": user.tenant_id, "hrs": get_settings().session_hours},
        )
    return raw


def delete_session(raw: str) -> None:
    with routing_session() as s:
        s.execute(text("DELETE FROM sessions WHERE token_hash = :h"), {"h": hash_token(raw)})


def check_origin(request: Request) -> None:
    """Défense CSRF en plus du cookie SameSite : une requête qui modifie des données doit venir du tableau de bord."""
    allowed = get_settings().dashboard_origin
    if allowed and request.method in UNSAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and origin != allowed:
            raise HTTPException(403, detail={"code": "bad_origin", "message": "Origine non autorisée."})


def current_user(request: Request) -> AuthUser:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise HTTPException(401, detail={"code": "unauthenticated", "message": "Connexion requise."})
    h = hash_token(raw)
    with routing_session() as s:
        sess = s.execute(
            text("SELECT user_id, tenant_id FROM sessions WHERE token_hash = :h AND expires_at > now()"), {"h": h}
        ).first()
        if sess is not None:  # session glissante : prolongée au plus toutes les 5 minutes
            s.execute(
                text(
                    "UPDATE sessions SET last_seen_at = now(), expires_at = now() + make_interval(hours => :hrs) "
                    "WHERE token_hash = :h AND last_seen_at < now() - interval '5 minutes'"
                ),
                {"h": h, "hrs": get_settings().session_hours},
            )
    if sess is None:
        raise HTTPException(401, detail={"code": "unauthenticated", "message": "Session expirée."})
    with tenant_session(sess.tenant_id) as s:
        u = s.execute(
            text("SELECT id, email, name, role, status FROM users WHERE id = :u"), {"u": sess.user_id}
        ).first()
    if u is None or u.status != "active":
        raise HTTPException(401, detail={"code": "unauthenticated", "message": "Compte désactivé."})
    return AuthUser(u.id, sess.tenant_id, u.email, u.name, u.role)


def require_role(minimum: str):
    def dependency(user: AuthUser = Depends(current_user)) -> AuthUser:
        if ROLE_RANK[user.role] < ROLE_RANK[minimum]:
            raise HTTPException(403, detail={"code": "forbidden", "message": "Droits insuffisants pour cette action."})
        return user

    return dependency
