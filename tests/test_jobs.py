from sqlalchemy import text

from app.channels.messenger import parse_payload
from app.channels.sender import split_text
from app.services.ingest import ingest
from app.services.jobs import claim_job, complete_job, fail_job, requeue_stale
from scripts.send_test_webhook import build_payload


def _in(page, psid, body, mid):
    return ingest(parse_payload(build_payload(page, psid, body, mid=mid))[0])


def test_ingest_enqueues_one_job_per_message(make_tenant, owner_engine):
    make_tenant("v", "P1")
    _in("P1", "U1", "a", "m1")
    _in("P1", "U1", "a", "m1")  # doublon : pas de second job
    _in("P1", "U1", "b", "m2")
    with owner_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM jobs")).scalar() == 2


def test_jobs_of_a_conversation_run_in_order_and_never_in_parallel(make_tenant):
    make_tenant("v", "P1")
    _in("P1", "U1", "a", "m1")
    _in("P1", "U1", "b", "m2")
    j1 = claim_job()
    assert j1 is not None
    assert claim_job() is None          # j1 en cours : j2 doit attendre
    complete_job(j1.id)
    j2 = claim_job()
    assert j2 is not None and j2.id > j1.id


def test_different_conversations_are_independent(make_tenant):
    make_tenant("v", "P1")
    _in("P1", "U1", "a", "m1")
    _in("P1", "U2", "b", "m2")
    assert claim_job() is not None
    assert claim_job() is not None


def test_failed_job_is_retried_later_then_gives_up(make_tenant, owner_engine):
    make_tenant("v", "P1")
    _in("P1", "U1", "a", "m1")
    j = claim_job()
    assert fail_job(j, "boom") is False          # replanifié avec délai
    assert claim_job() is None                    # pas avant le délai
    with owner_engine.begin() as c:
        c.execute(text("UPDATE jobs SET run_after = now(), max_attempts = 2"))
    j = claim_job()
    assert j.attempts == 2
    assert fail_job(j, "boom") is True            # tentatives épuisées
    with owner_engine.connect() as c:
        assert c.execute(text("SELECT status FROM jobs")).scalar() == "failed"


def test_stale_processing_job_is_requeued(make_tenant, owner_engine):
    make_tenant("v", "P1")
    _in("P1", "U1", "a", "m1")
    claim_job()
    with owner_engine.begin() as c:
        c.execute(text("UPDATE jobs SET locked_at = now() - interval '10 minutes'"))
    assert requeue_stale() == 1
    assert claim_job().attempts == 2


def test_no_job_when_a_human_has_taken_over(make_tenant, owner_engine):
    make_tenant("v", "P1")
    _in("P1", "U1", "a", "m1")
    with owner_engine.begin() as c:
        c.execute(text("UPDATE conversations SET status = 'human'"))
        c.execute(text("DELETE FROM jobs"))
    _in("P1", "U1", "b", "m2")
    with owner_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM jobs")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM messages")).scalar() == 2  # le message est quand même enregistré


def test_split_text_respects_limit():
    long = ("mot " * 1200).strip()
    parts = split_text(long, 1900)
    assert all(len(p) <= 1900 for p in parts) and " ".join(parts).split() == long.split()
    assert split_text("court") == ["court"]
