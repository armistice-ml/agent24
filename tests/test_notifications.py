import pytest
from sqlalchemy import text

from app.worker import run_once
from scripts.set_tenant_config import set_tenant_config
from tests.fakes import FakeEmail, FakeLLM, FakeSender, text_reply, tool_reply
from tests.helpers import drain, inbound, sql

CONFIG = {"tools_enabled": ["get_business_info", "escalate_to_human"]}
ESCALATE = tool_reply(
    "escalate_to_human",
    {"category": "plainte", "reason": "Client mécontent", "summary": "Son dossier est déposé depuis une semaine sans nouvelles."},
)


@pytest.fixture
def vigo(make_tenant, make_user, owner_engine):
    t, _ = make_tenant("vigo", "P1")
    set_tenant_config(owner_engine, t, CONFIG)
    make_user(t, "boss@vigo.bj", "owner")
    make_user(t, "gestionnaire@vigo.bj", "manager")
    make_user(t, "employe@vigo.bj", "employee")
    make_user(t, "lecteur@vigo.bj", "readonly")
    make_user(t, "muet@vigo.bj", "manager", notify=False)
    return t


def _escalate(email=None):
    inbound("P1", "U1", "Toujours rien ?", "m1")
    return drain(FakeLLM(ESCALATE, text_reply("Je transmets à l'équipe.")), FakeSender(), email)


def test_managers_are_emailed_when_the_agent_escalates(vigo):
    email = _escalate()
    [mail] = email.sent
    assert set(mail["to"]) == {"boss@vigo.bj", "gestionnaire@vigo.bj"}        # ni employé, ni lecteur, ni notifications coupées
    assert "[vigo]" in mail["subject"] and "Plainte" in mail["subject"]
    assert "Son dossier est déposé" in mail["body"] and "Client mécontent" in mail["body"]
    assert "https://dashboard.test/conversations/" in mail["body"]


def test_the_email_does_not_leak_the_conversation_itself(vigo):
    email = _escalate()
    assert "Toujours rien" not in email.sent[0]["body"]      # résumé uniquement, pas le contenu des messages


def test_no_recipient_makes_the_failure_visible(make_tenant, make_user, owner_engine):
    t, _ = make_tenant("vigo", "P1")
    set_tenant_config(owner_engine, t, CONFIG)
    make_user(t, "employe@vigo.bj", "employee")               # personne à prévenir
    email = _escalate()
    assert email.sent == []
    rows = sql(owner_engine, "SELECT status, last_error FROM jobs WHERE kind = 'notify_escalation'")
    assert rows[0][0] == "failed" and "aucun responsable" in rows[0][1]


def test_an_already_resolved_escalation_sends_nothing(vigo, owner_engine):
    inbound("P1", "U1", "Toujours rien ?", "m1")
    email = FakeEmail()
    # l'agent escalade, mais le dirigeant traite le dossier avant que l'email parte
    from app.worker import run_once as step
    llm, sender = FakeLLM(ESCALATE, text_reply("Je transmets à l'équipe.")), FakeSender()
    assert step(llm, sender.factory(), email)                                   # job « reply »
    sql(owner_engine, "UPDATE escalations SET status = 'resolved', resolution = 'closed', resolved_at = now()")
    assert step(llm, sender.factory(), email)                                   # job « notify »
    assert email.sent == []


def test_a_transient_email_failure_is_retried(vigo, owner_engine):
    email = _escalate(FakeEmail(fail_times=1))
    assert email.sent == []
    assert sql(owner_engine, "SELECT status FROM jobs WHERE kind = 'notify_escalation'")[0][0] == "pending"
    sql(owner_engine, "UPDATE jobs SET run_after = now()")
    while run_once(FakeLLM(), FakeSender().factory(), email):
        pass
    assert len(email.sent) == 1
    assert sql(owner_engine, "SELECT status FROM jobs WHERE kind = 'notify_escalation'")[0][0] == "done"


def test_a_slow_llm_does_not_delay_the_email_of_another_conversation(vigo, owner_engine):
    """Les jobs d'email ne sont pas bloqués derrière les jobs de réponse d'une conversation en échec."""
    inbound("P1", "U1", "Bonjour", "m1")
    from app.services.jobs import claim_job, fail_job

    job = claim_job()
    fail_job(job, "LLM indisponible")                         # le job reply est replanifié (en attente, plus tard)
    sql(
        owner_engine,
        "INSERT INTO escalations (tenant_id, conversation_id, source, category, summary) "
        "SELECT tenant_id, id, 'system', 'technique', 'test' FROM conversations",
    )
    sql(
        owner_engine,
        "INSERT INTO jobs (tenant_id, conversation_id, kind, escalation_id) "
        "SELECT tenant_id, conversation_id, 'notify_escalation', id FROM escalations",
    )
    email = FakeEmail()
    assert run_once(FakeLLM(), FakeSender().factory(), email)
    assert len(email.sent) == 1
