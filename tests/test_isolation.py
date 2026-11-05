import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import assert_safe_role, get_engine, tenant_session


def _add_contact(session, tenant_id, channel_id, psid):
    session.execute(
        text("INSERT INTO contacts (tenant_id, channel_id, channel_user_id) VALUES (:t, :c, :p)"),
        {"t": tenant_id, "c": channel_id, "p": psid},
    )


def test_app_role_cannot_bypass_rls():
    assert_safe_role()  # ne lève pas


def test_each_tenant_only_sees_its_own_rows(make_tenant):
    ta, ca = make_tenant("a", "page_a")
    tb, cb = make_tenant("b", "page_b")
    with tenant_session(ta) as s:
        _add_contact(s, ta, ca, "u1")
    with tenant_session(tb) as s:
        _add_contact(s, tb, cb, "u2")

    with tenant_session(ta) as s:
        rows = s.execute(text("SELECT channel_user_id FROM contacts")).scalars().all()
        assert rows == ["u1"]
        assert s.execute(text("SELECT count(*) FROM tenants")).scalar() == 1
        assert s.execute(text("SELECT count(*) FROM channels")).scalar() == 1


def test_no_tenant_set_means_no_rows(make_tenant):
    ta, ca = make_tenant("a", "page_a")
    with tenant_session(ta) as s:
        _add_contact(s, ta, ca, "u1")
    with get_engine().connect() as conn:  # connexion applicative sans tenant
        assert conn.execute(text("SELECT count(*) FROM contacts")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM channels")).scalar() == 0


def test_cannot_write_into_another_tenant(make_tenant):
    ta, _ = make_tenant("a", "page_a")
    tb, cb = make_tenant("b", "page_b")
    with pytest.raises(DBAPIError):  # refusé par la politique WITH CHECK de la RLS
        with tenant_session(ta) as s:
            _add_contact(s, tb, cb, "intrus")


def test_cannot_reference_another_tenants_channel(make_tenant):
    ta, _ = make_tenant("a", "page_a")
    _, cb = make_tenant("b", "page_b")
    with pytest.raises(DBAPIError):  # refusé par la clé étrangère composite (tenant_id, channel_id)
        with tenant_session(ta) as s:
            _add_contact(s, ta, cb, "u1")


def test_a_page_belongs_to_one_tenant_only(make_tenant):
    make_tenant("a", "page_x")
    with pytest.raises(DBAPIError):
        make_tenant("b", "page_x")
