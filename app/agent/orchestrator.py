"""Orchestrateur : traite un job (un message client) de bout en bout.

Étapes : charger le contexte -> boucle d'agent (Claude + outils) -> enregistrer la réponse -> l'envoyer.
Aucune transaction n'est ouverte pendant les appels réseau (Claude, Meta).
"""
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent.llm import LLMClient
from app.agent.prompt import build_system_prompt
from app.agent.tools import ToolContext, enabled_tools, run_tool
from app.channels.sender import SendError, Sender, get_sender
from app.config import get_settings
from app.db import tenant_session
from app.models import AgentRun, Conversation, Message
from app.services.events import emit
from app.services.handoff import channel_info as _channel_info
from app.services.handoff import create_escalation
from app.services.jobs import Job

log = logging.getLogger(__name__)

HISTORY_LIMIT = 30
FALLBACK_ESCALATION = "Je transmets votre demande à un membre de l'équipe, qui vous répondra dès que possible."
FALLBACK_TECHNICAL = "Nous rencontrons un souci technique. Un membre de l'équipe va vous répondre dès que possible."

PLACEHOLDERS = {
    "image": "[Le client a envoyé une image que je ne peux pas encore voir]",
    "audio": "[Le client a envoyé un message vocal que je ne peux pas encore écouter]",
    "video": "[Le client a envoyé une vidéo que je ne peux pas encore voir]",
    "file": "[Le client a envoyé un fichier que je ne peux pas encore lire]",
    "other": "[Le client a envoyé un contenu que je ne peux pas traiter]",
}


@dataclass
class Loaded:
    skip: str | None = None
    resume_message_id: UUID | None = None   # réponse déjà enregistrée mais non envoyée
    resume_text: str | None = None
    tenant_name: str = ""
    sector: str | None = None
    config: dict | None = None
    channel_id: UUID | None = None
    channel_type: str = ""
    token: str = ""
    recipient_id: str = ""
    history: list[dict] | None = None
    covered_until: datetime | None = None
    contact_id: UUID | None = None
    profile_fetched: bool = True


def to_llm_messages(rows: list[tuple[str, str, str | None]]) -> list[dict]:
    """(direction, type, texte) -> messages Claude. Fusionne les tours consécutifs du même rôle."""
    out: list[dict] = []
    for direction, kind, body in rows:
        role = "user" if direction == "in" else "assistant"
        content = body or (PLACEHOLDERS.get(kind, PLACEHOLDERS["other"]) if direction == "in" else "")
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] == "assistant":  # la conversation doit commencer par le client
        out.pop(0)
    return out


def _load(job: Job) -> Loaded:
    with tenant_session(job.tenant_id) as s:
        trigger = s.get(Message, job.message_id)
        conv = s.get(Conversation, job.conversation_id)
        if trigger is None or conv is None:
            return Loaded(skip="missing")

        # 1. Réponse déjà enregistrée pour ce message ? (reprise après un échec d'envoi)
        existing = s.execute(
            select(Message.id, Message.text, Message.delivery_status).where(
                Message.conversation_id == conv.id, Message.idempotency_key == f"reply:{job.message_id}"
            )
        ).first()
        base = Loaded()
        base.channel_id, base.channel_type, base.token, base.recipient_id = _channel_info(s, conv.id)
        if existing:
            if existing.delivery_status == "pending":
                base.resume_message_id, base.resume_text = existing.id, existing.text
                return base
            return Loaded(skip="already_answered")

        # 2. L'agent est-il toujours en charge ?
        if conv.status != "agent":
            return Loaded(skip="not_agent")

        tenant = s.execute(
            text("SELECT name, sector, config, agent_paused FROM tenants WHERE id = :t"), {"t": job.tenant_id}
        ).one()
        if tenant.agent_paused:  # interrupteur d'urgence : les messages restent enregistrés, l'agent se tait
            return Loaded(skip="paused")
        contact = s.execute(
            text("SELECT id, profile_fetched_at FROM contacts WHERE id = (SELECT contact_id FROM conversations WHERE id = :c)"),
            {"c": conv.id},
        ).one()
        base.contact_id, base.profile_fetched = contact.id, contact.profile_fetched_at is not None
        rows = s.execute(
            select(Message.direction, Message.type, Message.text, Message.created_at)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(HISTORY_LIMIT)
        ).all()[::-1]

    base.tenant_name, base.sector, base.config = tenant.name, tenant.sector, tenant.config or {}
    base.history = to_llm_messages([(r.direction, r.type, r.text) for r in rows])
    inbound_times = [r.created_at for r in rows if r.direction == "in"]
    base.covered_until = max(inbound_times) if inbound_times else trigger.created_at
    if not base.history or base.history[-1]["role"] != "user":
        return Loaded(skip="no_user_turn")
    return base


@dataclass
class AgentResult:
    text: str | None
    escalated: bool
    steps: int
    input_tokens: int
    output_tokens: int


def _agent_loop(llm: LLMClient, model: str, system: str, ctx: ToolContext, messages: list[dict], max_steps: int) -> AgentResult:
    tools = enabled_tools(ctx.config)
    schemas = [t.schema() for t in tools]
    steps = in_tok = out_tok = 0
    escalated = False
    for _ in range(max_steps):
        reply = llm.create(model=model, system=system, tools=schemas, messages=messages, max_tokens=1000)
        steps += 1
        in_tok += reply.input_tokens
        out_tok += reply.output_tokens
        if not reply.tool_calls:
            return AgentResult(reply.text or None, escalated, steps, in_tok, out_tok)
        messages.append({"role": "assistant", "content": reply.content_blocks})
        results = []
        for call in reply.tool_calls:
            res = run_tool(ctx, call.name, call.input)
            if call.name == "escalate_to_human" and res.ok:
                escalated = True
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(res.output, ensure_ascii=False),
                    "is_error": not res.ok,
                }
            )
        messages.append({"role": "user", "content": results})
    return AgentResult(None, escalated, steps, in_tok, out_tok)  # limite d'étapes atteinte


def _insert_outbound(
    s, job: Job, loaded: Loaded, key: str, body: str, after: datetime | None = None
) -> UUID | None:
    """`after` : la réponse est classée juste après le dernier message client qu'elle couvre.
    Ainsi un message arrivé PENDANT le traitement reste, dans l'historique, après cette réponse
    et obtient sa propre réponse ; les messages déjà pris en compte, eux, la précèdent."""
    values = dict(
        tenant_id=job.tenant_id,
        conversation_id=job.conversation_id,
        channel_id=loaded.channel_id,
        direction="out",
        author="agent",
        type="text",
        text=body,
        idempotency_key=key,
        delivery_status="pending",
        received_at=datetime.now().astimezone(),
    )
    if after is not None:
        values["created_at"] = after + timedelta(microseconds=1)
    return s.execute(
        pg_insert(Message)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[Message.tenant_id, Message.channel_id, Message.idempotency_key])
        .returning(Message.id)
    ).scalar()


def _deliver(job: Job, loaded: Loaded, message_id: UUID, body: str, sender: Sender) -> None:
    try:
        external_id = sender.send_text(loaded.recipient_id, body)
    except SendError as e:
        if e.permanent:
            with tenant_session(job.tenant_id) as s:
                s.execute(update(Message).where(Message.id == message_id).values(delivery_status="failed"))
                emit(s, job.tenant_id, "message.failed", job.conversation_id)
        raise
    with tenant_session(job.tenant_id) as s:
        s.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(delivery_status="sent", external_id=external_id, sent_at=datetime.now().astimezone())
        )
        emit(s, job.tenant_id, "message.sent", job.conversation_id)


def _ensure_contact_profile(job: Job, loaded: Loaded, sender_factory) -> None:
    """Récupère le nom du client une fois (au mieux : une erreur ne bloque jamais la réponse)."""
    if loaded.profile_fetched or loaded.contact_id is None:
        return
    try:
        profile = sender_factory(loaded.channel_type, loaded.token).get_profile(loaded.recipient_id)
    except Exception:
        log.debug("profil client indisponible pour le moment", exc_info=True)
        return
    name = (profile or {}).get("name")
    with tenant_session(job.tenant_id) as s:
        s.execute(
            text("UPDATE contacts SET name = COALESCE(:n, name), profile_fetched_at = now() WHERE id = :c"),
            {"n": name, "c": loaded.contact_id},
        )


def handle_job(job: Job, *, llm: LLMClient, sender_factory: Callable[[str, str], Sender] = get_sender) -> str:
    loaded = _load(job)
    if loaded.skip:
        return f"skipped:{loaded.skip}"

    # Reprise : la réponse existe déjà, il ne reste qu'à l'envoyer (pas de nouvel appel à Claude).
    if loaded.resume_message_id:
        _deliver(job, loaded, loaded.resume_message_id, loaded.resume_text or "", sender_factory(loaded.channel_type, loaded.token))
        return "resumed"

    _ensure_contact_profile(job, loaded, sender_factory)

    config = loaded.config or {}
    agent_cfg = config.get("agent") or {}
    model = agent_cfg.get("model") or get_settings().agent_model
    max_steps = int(agent_cfg.get("max_steps", 6))

    with tenant_session(job.tenant_id) as s:
        run = AgentRun(
            tenant_id=job.tenant_id,
            conversation_id=job.conversation_id,
            trigger_message_id=job.message_id,
            model=model,
        )
        s.add(run)
        s.flush()
        run_id = run.id

    ctx = ToolContext(tenant_id=job.tenant_id, conversation_id=job.conversation_id, run_id=run_id, config=config)
    system = build_system_prompt(loaded.tenant_name, loaded.sector, config)
    result = _agent_loop(llm, model, system, ctx, list(loaded.history or []), max_steps)

    # Aucune réponse exploitable ou trop d'étapes : on ne laisse pas le client sans réponse, on escalade.
    force_escalation = result.text is None
    body = result.text or FALLBACK_ESCALATION
    escalated = result.escalated or force_escalation

    with tenant_session(job.tenant_id) as s:
        if force_escalation:
            create_escalation(
                s,
                tenant_id=job.tenant_id,
                conversation_id=job.conversation_id,
                category="technique",
                reason="L'assistant n'a pas produit de réponse exploitable (limite d'étapes atteinte).",
                summary="L'assistant n'a pas pu répondre à ce client ; un message d'attente lui a été envoyé.",
                source="system",
                run_id=run_id,
            )
        message_id = _insert_outbound(s, job, loaded, f"reply:{job.message_id}", body, after=loaded.covered_until)
        s.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(
                outcome="escalated" if escalated else "replied",
                steps=result.steps,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                covered_until=loaded.covered_until,
                finished_at=datetime.now().astimezone(),
            )
        )
    if message_id is None:
        return "skipped:duplicate_reply"

    _deliver(job, loaded, message_id, body, sender_factory(loaded.channel_type, loaded.token))
    return "escalated" if escalated else "replied"


def on_terminal_failure(job: Job, *, sender_factory: Callable[[str, str], Sender] = get_sender) -> None:
    """Échec définitif : on passe la main à un humain et on prévient honnêtement le client (au mieux)."""
    try:
        with tenant_session(job.tenant_id) as s:
            create_escalation(
                s,
                tenant_id=job.tenant_id,
                conversation_id=job.conversation_id,
                category="technique",
                reason="Le traitement automatique a échoué définitivement (voir la file de jobs).",
                summary="Un message client n'a pas pu être traité automatiquement ; un message d'excuse lui a été envoyé.",
                source="system",
            )
            channel_id, ctype, token, recipient = _channel_info(s, job.conversation_id)
            loaded = Loaded(channel_id=channel_id, channel_type=ctype, token=token, recipient_id=recipient)
            message_id = _insert_outbound(s, job, loaded, f"fallback:{job.message_id}", FALLBACK_TECHNICAL)
        if message_id:
            _deliver(job, loaded, message_id, FALLBACK_TECHNICAL, sender_factory(ctype, token))
    except Exception:
        log.exception("impossible d'envoyer le message de secours (job %s)", job.id)
