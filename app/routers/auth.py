from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth import (
    SESSION_COOKIE,
    AuthUser,
    authenticate,
    check_origin,
    create_session,
    current_user,
    delete_session,
    login_limiter,
)
from app.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"], dependencies=[Depends(check_origin)])


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


def _user_json(u: AuthUser) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    key = f"{request.client.host if request.client else '?'}|{body.email.strip().lower()}"
    if login_limiter.blocked(key):
        raise HTTPException(429, detail={"code": "too_many_attempts", "message": "Trop de tentatives, réessayez plus tard."})
    user = authenticate(body.email, body.password)
    if user is None:
        login_limiter.fail(key)
        # même message dans tous les cas : on ne révèle pas si le compte existe
        raise HTTPException(401, detail={"code": "invalid_credentials", "message": "Email ou mot de passe incorrect."})
    login_limiter.reset(key)
    s = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        create_session(user),
        max_age=s.session_hours * 3600,
        httponly=True,
        secure=s.cookie_secure,
        samesite=s.cookie_samesite,
        path="/",
    )
    return {"user": _user_json(user)}


@router.post("/logout")
def logout(request: Request, response: Response):
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        delete_session(raw)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: AuthUser = Depends(current_user)):
    return {"user": _user_json(user)}
