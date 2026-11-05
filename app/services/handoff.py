"""Passage de relais agent <-> humain : escalades, prise en charge, réponses humaines, échos de la page."""
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text

from app.channels.sender import SendError, Sender
from app.config import get_settings
from app.crypto import decrypt
from app.db import tenant_session
from app.schemas import EchoEvent
from app.services.events import emit

log = logging.getLogger(__name__)

# Fenêtre de réponse standard de Messenger : à vérifier dans la documentation Meta.
REPLY_WINDOW = timedelta(hours=24)


class HandoffError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def channel_info(s, conversation_id: UUID):
    row = s.execute(
        text(
            "SELECT ch.id, ch.type, ch.credentials_encrypted, ct.channel_user_id "
            "FROM conversations cv JOIN channels ch ON ch.id = cv.channel_id "
            "JOIN contacts ct ON ct.id = cv.contact_id WHERE cv.id = :c"
        ),
        {"c": conversation_id},
    ).one()
    return row.id, row.type, decrypt(row.credentials_encrypted or ""), row.channel_user_id


def _conversation(s, conversation_id: UUID):
    row = s.execute(
        text("SELECT id, status, assigned_user_id FROM conversations WHERE id = :c"), {"c": conversation_id}
    ).first()
    if row is None:  # inexistante, ou appartenant à un autre client (la RLS la rend invisible)
        raise HandoffError("not_found", "Conversation introuvable.", 404)
    return row


# --- escalade par l'agent ou le système --------------------------------------------------

def create_escalation(
    s, *, tenant_id: UUID, conversation_id: UUID, category: str, reason: str | None, summary: str | None,
    source: str, run_id: UUID | None = None,
) -> tuple[UUID, bool]:
    """Passe la conversation à l'humain, crée l'escalade, l'événement et le job d'email (même transaction)."""
    s.execute(
        text("UPDATE conversations SET status = 'human', handoff_reason = 'escalation' WHERE id = :c AND status = 'agent'"),
        {"c": conversation_id},
    )
    esc_id = s.execute(
        text(
            "INSERT INTO escalations (tenant_id, conversation_id, run_id, source, category, reason, summary) "
            "VALUES (:t, :c, :r, :src, :cat, :reason, :summary) "
            "ON CONFLICT (tenant_id, conversation_id) WHERE status <> 'resolved' DO NOTHING RETURNING id"
        ),
        {"t": tenant_id, "c": conversation_id, "r": run_id, "src": source, "cat": category,
         "reason": (reason or "")[:500] or None, "summary": (summary or "")[:1000] or None},
    ).scalar()
    if esc_id is None:  # déjà escaladée : idempotent
        existing = s.execute(
            text("SELECT id FROM escalations WHERE conversation_id = :c AND status <> 'resolved'"), {"c": conversation_id}
        ).scalar_one()
        return existing, False
    s.execute(
        text("INSERT INTO jobs (tenant_id, conversation_id, kind, escalation_id) VALUES (:t, :c, 'notify_escalation', :e)"),
        {"t": tenant_id, "c": conversation_id, "e": esc_id},
    )
    emit(s, tenant_id, "escalation.created", conversation_id, esc_id)
    emit(s, tenant_id, "conversation.updated", conversation_id)
    return esc_id, True


# --- actions humaines depuis le tableau de bord -------------------------------------------

def take_conversation(user, conversation_id: UUID) -> dict:
    with tenant_session(user.tenant_id) as s:
        conv = _conversation(s, conversation_id)
        if conv.status == "closed":
            raise HandoffError("closed", "Cette conversation est clôturée.")
        s.execute(
            text(
                "UPDATE conversations SET handoff_reason = CASE WHEN status = 'agent' THEN 'dashboard' ELSE handoff_reason END, "
                "status = 'human', assigned_user_id = :u WHERE id = :c"
            ),
            {"c": conversation_id, "u": user.id},
        )
        esc = s.execute(
            text(
                "UPDATE escalations SET status = 'taken', taken_by = :u, taken_at = now() "
                "WHERE conversation_id = :c AND status = 'open' RETURNING id"
            ),
            {"c": conversation_id, "u": user.id},
        ).scalar()
        emit(s, user.tenant_id, "conversation.updated", conversation_id)
        if esc:
            emit(s, user.tenant_id, "escalation.updated", conversation_id, esc)
    return {"status": "human"}


def _resolve_escalations(s, tenant_id: UUID, conversation_id: UUID, resolution: str) -> None:
    for esc_id in s.execute(
        text(
            "UPDATE escalations SET status = 'resolved', resolution = :r, resolved_at = now() "
            "WHERE conversation_id = :c AND status <> 'resolved' RETURNING id"
        ),
        {"c": conversation_id, "r": resolution},
    ).scalars():
        emit(s, tenant_id, "escalation.updated", conversation_id, esc_id)


def hand_back_to_agent(user, conversation_id: UUID) -> dict:
    with tenant_session(user.tenant_id) as s:
        conv = _conversation(s, conversation_id)
        if conv.status != "human":
            raise HandoffError("not_human", "La conversation n'est pas en traitement humain.")
        s.execute(
            text("UPDATE conversations SET status = 'agent', assigned_user_id = NULL, handoff_reason = NULL WHERE id = :c"),
            {"c": conversation_id},
        )
        _resolve_escalations(s, user.tenant_id, conversation_id, "handed_back")
        emit(s, user.tenant_id, "conversation.updated", conversation_id)
    return {"status": "agent"}


def close_conversation(user, conversation_id: UUID) -> dict:
    with tenant_session(user.tenant_id) as s:
        conv = _conversation(s, conversation_id)
        if conv.status == "closed":
            raise HandoffError("closed", "Cette conversation est déjà clôturée.")
        s.execute(
            text("UPDATE conversations SET status = 'closed', assigned_user_id = NULL WHERE id = :c"), {"c": conversation_id}
        )
        _resolve_escalations(s, user.tenant_id, conversation_id, "closed")
        emit(s, user.tenant_id, "conversation.updated", conversation_id)
    return {"status": "closed"}


def reply_window(last_inbound_at: datetime | None) -> dict:
    if last_inbound_at is None:
        return {"open": False, "expires_at": None, "seconds_left": 0}
    expires = last_inbound_at + REPLY_WINDOW
    left = int((expires - datetime.now(UTC)).total_seconds())
    return {"open": left > 0, "expires_at": expires, "seconds_left": max(left, 0)}


def send_human_reply(user, conversation_id: UUID, body: str, sender_factory: Callable[[str, str], Sender]) -> dict:
    body = body.strip()
    if not body:
        raise HandoffError("empty", "Le message est vide.", 422)

    with tenant_session(user.tenant_id) as s:
        conv = _conversation(s, conversation_id)
        if conv.status != "human":
            raise HandoffError("not_human", "Prenez d'abord la main sur cette conversation.")
        last_in = s.execute(
            text("SELECT max(received_at) FROM messages WHERE conversation_id = :c AND direction = 'in'"),
            {"c": conversation_id},
        ).scalar()
        if not reply_window(last_in)["open"]:
            raise HandoffError(
                "window_expired",
                "Le dernier message du client date de plus de 24 h : la plateforme n'autorise plus la réponse.",
            )
        channel_id, ctype, token, recipient = channel_info(s, conversation_id)
        message_id = s.execute(
            text(
                "INSERT INTO messages (tenant_id, conversation_id, channel_id, direction, type, text, idempotency_key, "
                "delivery_status, received_at, author, sent_by_user_id) "
                "VALUES (:t, :c, :ch, 'out', 'text', :body, :key, 'pending', now(), 'user', :u) RETURNING id"
            ),
            {"t": user.tenant_id, "c": conversation_id, "ch": channel_id, "body": body, "key": f"user:{uuid4()}", "u": user.id},
        ).scalar_one()
        s.execute(
            text("UPDATE conversations SET assigned_user_id = COALESCE(assigned_user_id, :u) WHERE id = :c"),
            {"u": user.id, "c": conversation_id},
        )
        for esc_id in s.execute(
            text(
                "UPDATE escalations SET status = 'taken', taken_by = :u, taken_at = now() "
                "WHERE conversation_id = :c AND status = 'open' RETURNING id"
            ),
            {"u": user.id, "c": conversation_id},
        ).scalars():
            emit(s, user.tenant_id, "escalation.updated", conversation_id, esc_id)
        emit(s, user.tenant_id, "conversation.updated", conversation_id)

    try:
        external_id = sender_factory(ctype, token).send_text(recipient, body)
    except SendError as e:
        with tenant_session(user.tenant_id) as s:
            s.execute(text("UPDATE messages SET delivery_status = 'failed' WHERE id = :m"), {"m": message_id})
            emit(s, user.tenant_id, "message.failed", conversation_id)
        raise HandoffError("send_failed", f"Envoi impossible : {e}", 502) from e

    with tenant_session(user.tenant_id) as s:
        s.execute(
            text("UPDATE messages SET delivery_status = 'sent', external_id = :x, sent_at = now() WHERE id = :m"),
            {"x": external_id, "m": message_id},
        )
        emit(s, user.tenant_id, "message.sent", conversation_id)
    return {"id": message_id, "delivery_status": "sent"}


# --- réponse humaine faite depuis la boîte de la page (écho Meta) -------------------------

def register_page_reply(echo: EchoEvent) -> str:
    """Un écho est « à nous » (agent ou tableau de bord) ou « d'un humain ». Dans le doute on ne se tait pas."""
    from app.services.ingest import resolve_route  # import local : évite un cycle

    route = resolve_route("messenger", echo.channel_external_id)
    if route is None:
        return "unknown_channel"
    tenant_id, channel_id = route
    meta_app_id = get_settings().meta_app_id

    with tenant_session(tenant_id) as s:
        conv = s.execute(
            text(
                "SELECT cv.id, cv.status FROM conversations cv JOIN contacts ct ON ct.id = cv.contact_id "
                "WHERE ct.channel_id = :ch AND ct.channel_user_id = :u AND cv.status <> 'closed'"
            ),
            {"ch": channel_id, "u": echo.customer_id},
        ).first()
        if conv is None:
            return "no_conversation"

        # Écho de NOTRE propre envoi ? (app émettrice, identifiant du message, ou envoi très récent :
        # un long message est découpé en plusieurs morceaux dont un seul identifiant est mémorisé.)
        if echo.app_id and meta_app_id and echo.app_id == meta_app_id:
            return "own_echo"
        if s.execute(
            text("SELECT 1 FROM messages WHERE channel_id = :ch AND external_id = :m LIMIT 1"),
            {"ch": channel_id, "m": echo.mid},
        ).first():
            return "own_echo"
        if s.execute(
            text(
                "SELECT 1 FROM messages WHERE conversation_id = :c AND direction = 'out' AND author IN ('agent', 'user') "
                "AND (delivery_status = 'pending' OR sent_at > now() - interval '60 seconds') LIMIT 1"
            ),
            {"c": conv.id},
        ).first():
            return "own_echo"

        inserted = s.execute(
            text(
                "INSERT INTO messages (tenant_id, conversation_id, channel_id, direction, type, text, idempotency_key, "
                "delivery_status, external_id, received_at, sent_at, author) "
                "VALUES (:t, :c, :ch, 'out', :type, :body, :key, 'sent', :mid, :at, :at, 'page_inbox') "
                "ON CONFLICT (tenant_id, channel_id, idempotency_key) DO NOTHING RETURNING id"
            ),
            {"t": tenant_id, "c": conv.id, "ch": channel_id, "type": echo.type, "body": echo.text,
             "key": f"echo:{echo.mid}", "mid": echo.mid, "at": echo.received_at},
        ).scalar()
        if inserted is None:
            return "duplicate"
        outcome = "recorded"
        if conv.status == "agent":  # un humain parle : l'agent se tait
            s.execute(
                text("UPDATE conversations SET status = 'human', handoff_reason = 'page_inbox' WHERE id = :c"), {"c": conv.id}
            )
            emit(s, tenant_id, "conversation.updated", conv.id)
            outcome = "human_takeover"
        emit(s, tenant_id, "message.sent", conv.id)
    return outcome
