import uuid

from tests.conftest import PASSWORD, login
from tests.helpers import sql


def _setup(make_tenant, make_user, role="owner", email="boss@vigo.bj"):
    t, _ = make_tenant("vigo", "P1")
    make_user(t, email, role)
    return t


def test_login_sets_httponly_cookie_and_me_works(client, make_tenant, make_user):
    _setup(make_tenant, make_user)
    r = login(client, "boss@vigo.bj")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/auth/me").json()["user"]["email"] == "boss@vigo.bj"


def test_wrong_password_and_unknown_email_give_the_same_answer(client, make_tenant, make_user):
    _setup(make_tenant, make_user)
    bad_pw = client.post("/api/auth/login", json={"email": "boss@vigo.bj", "password": "faux-mot-de-passe"})
    unknown = client.post("/api/auth/login", json={"email": "inconnu@vigo.bj", "password": "faux-mot-de-passe"})
    assert bad_pw.status_code == unknown.status_code == 401
    assert bad_pw.json() == unknown.json()


def test_password_is_hashed_in_database(client, make_tenant, make_user, owner_engine):
    _setup(make_tenant, make_user)
    [(h,)] = sql(owner_engine, "SELECT password_hash FROM users")
    assert h.startswith("$argon2") and PASSWORD not in h


def test_disabled_user_cannot_log_in_or_keep_a_session(client, make_tenant, make_user, owner_engine):
    _setup(make_tenant, make_user)
    login(client, "boss@vigo.bj")
    sql(owner_engine, "UPDATE users SET status = 'disabled'")
    assert client.get("/api/auth/me").status_code == 401           # la session existante tombe aussi
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"email": "boss@vigo.bj", "password": PASSWORD}).status_code == 401


def test_logout_invalidates_the_session(client, make_tenant, make_user):
    _setup(make_tenant, make_user)
    login(client, "boss@vigo.bj")
    raw = client.cookies.get("a24_session")
    client.post("/api/auth/logout")
    client.cookies.set("a24_session", raw)            # rejoue l'ancien jeton
    assert client.get("/api/auth/me").status_code == 401


def test_no_cookie_and_expired_session_are_rejected(client, make_tenant, make_user, owner_engine):
    _setup(make_tenant, make_user)
    assert client.get("/api/conversations").status_code == 401
    login(client, "boss@vigo.bj")
    sql(owner_engine, "UPDATE sessions SET expires_at = now() - interval '1 minute'")
    assert client.get("/api/conversations").status_code == 401


def test_session_token_is_stored_hashed(client, make_tenant, make_user, owner_engine):
    _setup(make_tenant, make_user)
    login(client, "boss@vigo.bj")
    raw = client.cookies.get("a24_session")
    [(h,)] = sql(owner_engine, "SELECT token_hash FROM sessions")
    assert raw != h and len(h) == 64


def test_login_is_rate_limited(client, make_tenant, make_user):
    _setup(make_tenant, make_user)
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": "boss@vigo.bj", "password": "faux-mot-de-passe"}).status_code == 401
    r = client.post("/api/auth/login", json={"email": "boss@vigo.bj", "password": PASSWORD})  # même le bon est bloqué
    assert r.status_code == 429


def test_foreign_origin_is_rejected_on_state_changing_requests(client, make_tenant, make_user):
    _setup(make_tenant, make_user)
    r = client.post("/api/auth/login", json={"email": "boss@vigo.bj", "password": PASSWORD}, headers={"Origin": "https://evil.test"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "bad_origin"


def test_roles_are_enforced_server_side(client, make_tenant, make_user):
    t, _ = make_tenant("vigo", "P1")
    make_user(t, "ro@vigo.bj", "readonly")
    make_user(t, "emp@vigo.bj", "employee")
    make_user(t, "mgr@vigo.bj", "manager")
    some = uuid.uuid4()

    login(client, "ro@vigo.bj")
    assert client.get("/api/conversations").status_code == 200
    assert client.post(f"/api/conversations/{some}/messages", json={"text": "salut"}).status_code == 403
    assert client.post(f"/api/conversations/{some}/take").status_code == 403

    client.cookies.clear()
    login(client, "emp@vigo.bj")
    assert client.post("/api/agent/pause").status_code == 403           # réservé aux responsables
    assert client.post(f"/api/conversations/{some}/take").status_code == 404   # autorisé, mais introuvable

    client.cookies.clear()
    login(client, "mgr@vigo.bj")
    assert client.post("/api/agent/pause").status_code == 200
    assert client.get("/api/agent/status").json()["paused"] is True
