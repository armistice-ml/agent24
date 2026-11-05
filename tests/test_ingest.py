from sqlalchemy import text

from app.db import tenant_session
from app.services.ingest import ingest
from scripts.send_test_webhook import build_payload
from app.channels.messenger import parse_payload


def _msg(page, psid, body, mid):
    [m] = parse_payload(build_payload(page, psid, body, mid=mid))
    return m


def _count(tenant_id, table):
    with tenant_session(tenant_id) as s:
        return s.execute(text(f"SELECT count(*) FROM {table}")).scalar()


def test_first_message_creates_contact_conversation_message(make_tenant):
    t, _ = make_tenant("vigo", "PAGE1")
    r = ingest(_msg("PAGE1", "U1", "Bonjour", "m_1"))
    assert r.status == "stored" and r.tenant_id == t
    assert [_count(t, x) for x in ("contacts", "conversations", "messages")] == [1, 1, 1]


def test_second_message_reuses_conversation(make_tenant):
    t, _ = make_tenant("vigo", "PAGE1")
    r1 = ingest(_msg("PAGE1", "U1", "Bonjour", "m_1"))
    r2 = ingest(_msg("PAGE1", "U1", "Merci", "m_2"))
    assert r1.conversation_id == r2.conversation_id
    assert [_count(t, x) for x in ("contacts", "conversations", "messages")] == [1, 1, 2]


def test_duplicate_delivery_is_idempotent(make_tenant):
    t, _ = make_tenant("vigo", "PAGE1")
    assert ingest(_msg("PAGE1", "U1", "Bonjour", "m_1")).status == "stored"
    assert ingest(_msg("PAGE1", "U1", "Bonjour", "m_1")).status == "duplicate"
    assert _count(t, "messages") == 1


def test_unknown_page_is_not_stored(make_tenant):
    make_tenant("vigo", "PAGE1")
    assert ingest(_msg("INCONNUE", "U1", "Bonjour", "m_1")).status == "unknown_channel"


def test_same_user_id_on_two_tenants_stays_separate(make_tenant):
    ta, _ = make_tenant("a", "PAGE_A")
    tb, _ = make_tenant("b", "PAGE_B")
    ingest(_msg("PAGE_A", "SAME", "pour A", "m_1"))
    ingest(_msg("PAGE_B", "SAME", "pour B", "m_1"))  # même mid, autre tenant : pas un doublon
    with tenant_session(ta) as s:
        assert s.execute(text("SELECT text FROM messages")).scalars().all() == ["pour A"]
    with tenant_session(tb) as s:
        assert s.execute(text("SELECT text FROM messages")).scalars().all() == ["pour B"]
