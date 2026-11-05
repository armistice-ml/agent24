"""API du tableau de bord. Toutes les routes exigent une session ; le client vient de la session (jamais de l'URL)."""
from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth import AuthUser, check_origin, current_user, require_role
from app.channels.sender import get_sender
from app.db import tenant_session
from app.services import handoff
from app.services.events import emit
from app.services.handoff import HandoffError

router = APIRouter(prefix="/api", tags=["dashboard"], dependencies=[Depends(check_origin)])

DEFAULT_TZ = "Africa/Porto-Novo"  # fuseau par défaut ; surchargeable par client (config.timezone)


def sender_factory_dep():
    """Dépendance remplaçable dans les tests."""
    return get_sender


def _err(e: HandoffError) -> HTTPException:
    return HTTPException(e.status, detail={"code": e.code, "message": e.message})


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# --- conversations ----------------------------------------------------------------------

@router.get("/conversations")
def list_conversations(
    status: str = Query("open", pattern="^(open|all|agent|human|closed)$"),
    channel: str | None = Query(None, max_length=20),
    q: str | None = Query(None, max_length=100),
    before: datetime | None = None,
    limit: int = Query(30, ge=1, le=100),
    user: AuthUser = Depends(current_user),
):
    where, params = ["1=1"], {"limit": limit + 1}
    if status == "open":
        where.append("cv.status <> 'closed'")
    elif status != "all":
        where.append("cv.status = :status")
        params["status"] = status
    if channel:
        where.append("ch.type = :channel")
        params["channel"] = channel
    if q:
        where.append(
            "(ct.name ILIKE :q ESCAPE '\\' OR EXISTS (SELECT 1 FROM messages m2 "
            "WHERE m2.conversation_id = cv.id AND m2.text ILIKE :q ESCAPE '\\'))"
        )
        params["q"] = _like(q)
    if before:
        where.append("cv.last_message_at < :before")
        params["before"] = before
    sql = f"""
        SELECT cv.id, cv.status, cv.handoff_reason, cv.last_message_at, cv.assigned_user_id,
               ct.name AS contact_name, ct.channel_user_id, ch.type AS channel_type,
               lm.text AS last_text, lm.type AS last_type, lm.author AS last_author,
               (SELECT count(*) FROM messages m WHERE m.conversation_id = cv.id AND m.direction = 'in'
                  AND (cv.team_read_at IS NULL OR m.received_at > cv.team_read_at)) AS unread,
               es.id AS escalation_id, es.status AS escalation_status, es.category AS escalation_category
        FROM conversations cv
        JOIN contacts ct ON ct.id = cv.contact_id
        JOIN channels ch ON ch.id = cv.channel_id
        LEFT JOIN LATERAL (SELECT text, type, author FROM messages m WHERE m.conversation_id = cv.id
                           ORDER BY created_at DESC, id DESC LIMIT 1) lm ON true
        LEFT JOIN escalations es ON es.conversation_id = cv.id AND es.status <> 'resolved'
        WHERE {' AND '.join(where)}
        ORDER BY cv.last_message_at DESC NULLS LAST, cv.id
        LIMIT :limit
    """
    with tenant_session(user.tenant_id) as s:
        rows = s.execute(text(sql), params).mappings().all()
    items = [dict(r) for r in rows[:limit]]
    for it in items:
        it["contact_display"] = it["contact_name"] or f"Client ••{str(it['channel_user_id'])[-4:]}"
        del it["channel_user_id"]
    return {"items": items, "next_before": items[-1]["last_message_at"] if len(rows) > limit and items else None}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: UUID, user: AuthUser = Depends(current_user)):
    with tenant_session(user.tenant_id) as s:
        row = s.execute(
            text(
                """
                SELECT cv.id, cv.status, cv.handoff_reason, cv.last_message_at, cv.assigned_user_id,
                       au.name AS assigned_user_name, ct.name AS contact_name, ct.channel_user_id,
                       ch.type AS channel_type,
                       (SELECT max(received_at) FROM messages m WHERE m.conversation_id = cv.id AND m.direction = 'in') AS last_inbound_at,
                       es.id AS escalation_id, es.status AS escalation_status, es.category AS escalation_category,
                       es.reason AS escalation_reason, es.summary AS escalation_summary
                FROM conversations cv
                JOIN contacts ct ON ct.id = cv.contact_id
                JOIN channels ch ON ch.id = cv.channel_id
                LEFT JOIN users au ON au.id = cv.assigned_user_id
                LEFT JOIN escalations es ON es.conversation_id = cv.id AND es.status <> 'resolved'
                WHERE cv.id = :c
                """
            ),
            {"c": conversation_id},
        ).mappings().first()
    if row is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "Conversation introuvable."})
    out = dict(row)
    out["contact_display"] = out["contact_name"] or f"Client ••{str(out['channel_user_id'])[-4:]}"
    del out["channel_user_id"]
    out["reply_window"] = handoff.reply_window(out.pop("last_inbound_at"))
    return out


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: UUID,
    before: UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    user: AuthUser = Depends(current_user),
):
    params: dict = {"c": conversation_id, "limit": limit + 1}
    cursor = ""
    if before:
        cursor = "AND (m.created_at, m.id) < (SELECT created_at, id FROM messages WHERE id = :b)"
        params["b"] = before
    with tenant_session(user.tenant_id) as s:
        exists = s.execute(text("SELECT 1 FROM conversations WHERE id = :c"), {"c": conversation_id}).first()
        if exists is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "Conversation introuvable."})
        rows = s.execute(
            text(
                f"""
                SELECT m.id, m.direction, m.author, m.type, m.text, m.delivery_status,
                       CASE m.direction WHEN 'in' THEN m.received_at ELSE COALESCE(m.sent_at, m.received_at) END AS at,
                       u.name AS sent_by_name
                FROM messages m LEFT JOIN users u ON u.id = m.sent_by_user_id
                WHERE m.conversation_id = :c {cursor}
                ORDER BY m.created_at DESC, m.id DESC LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
    has_more = len(rows) > limit
    page = [dict(r) for r in rows[:limit]][::-1]  # ordre chronologique
    return {"items": page, "has_more": has_more, "next_before": page[0]["id"] if has_more and page else None}


@router.post("/conversations/{conversation_id}/read", status_code=204)
def mark_read(conversation_id: UUID, user: AuthUser = Depends(current_user)):
    with tenant_session(user.tenant_id) as s:
        n = s.execute(
            text("UPDATE conversations SET team_read_at = now() WHERE id = :c"), {"c": conversation_id}
        ).rowcount
    if n == 0:
        raise HTTPException(404, detail={"code": "not_found", "message": "Conversation introuvable."})


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def reply(
    conversation_id: UUID,
    body: ReplyIn,
    user: AuthUser = Depends(require_role("employee")),
    sender_factory=Depends(sender_factory_dep),
):
    try:
        return await run_in_threadpool(handoff.send_human_reply, user, conversation_id, body.text, sender_factory)
    except HandoffError as e:
        raise _err(e) from e


def _action(fn):
    def endpoint(conversation_id: UUID, user: AuthUser = Depends(require_role("employee"))):
        try:
            return fn(user, conversation_id)
        except HandoffError as e:
            raise _err(e) from e

    return endpoint


router.post("/conversations/{conversation_id}/take")(_action(handoff.take_conversation))
router.post("/conversations/{conversation_id}/handback")(_action(handoff.hand_back_to_agent))
router.post("/conversations/{conversation_id}/close")(_action(handoff.close_conversation))


# --- escalades --------------------------------------------------------------------------

@router.get("/escalations")
def list_escalations(
    status: str = Query("open", pattern="^(open|taken|resolved|handled|all)$"),
    limit: int = Query(50, ge=1, le=200),
    user: AuthUser = Depends(current_user),
):
    cond = {
        "open": "es.status = 'open'",
        "taken": "es.status = 'taken'",
        "resolved": "es.status = 'resolved'",
        "handled": "es.status IN ('taken', 'resolved')",  # onglet « Prises en charge et traitées »
        "all": "true",
    }[status]
    with tenant_session(user.tenant_id) as s:
        rows = s.execute(
            text(
                f"""
                SELECT es.id, es.conversation_id, es.status, es.category, es.reason, es.summary, es.source,
                       es.resolution, es.created_at, es.taken_at, es.resolved_at, tu.name AS taken_by_name,
                       ct.name AS contact_name, ct.channel_user_id, ch.type AS channel_type
                FROM escalations es
                JOIN conversations cv ON cv.id = es.conversation_id
                JOIN contacts ct ON ct.id = cv.contact_id
                JOIN channels ch ON ch.id = cv.channel_id
                LEFT JOIN users tu ON tu.id = es.taken_by
                WHERE {cond}
                ORDER BY es.created_at DESC LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
        counts = dict(
            s.execute(text("SELECT status, count(*) FROM escalations GROUP BY status")).all()
        )
    items = [dict(r) for r in rows]
    for it in items:
        it["contact_display"] = it["contact_name"] or f"Client ••{str(it['channel_user_id'])[-4:]}"
        del it["channel_user_id"]
    return {"items": items, "counts": {k: counts.get(k, 0) for k in ("open", "taken", "resolved")}}


# --- agent : état et interrupteur d'urgence ---------------------------------------------

@router.get("/agent/status")
def agent_status(user: AuthUser = Depends(current_user)):
    with tenant_session(user.tenant_id) as s:
        row = s.execute(text("SELECT agent_paused, paused_at FROM tenants")).one()
    return {"paused": row.agent_paused, "paused_at": row.paused_at}


def _set_pause(user: AuthUser, paused: bool) -> dict:
    with tenant_session(user.tenant_id) as s:
        s.execute(
            text(
                "UPDATE tenants SET agent_paused = :p, paused_at = CASE WHEN :p THEN now() END, "
                "paused_by = CASE WHEN :p THEN CAST(:u AS uuid) END"
            ),
            {"p": paused, "u": str(user.id)},
        )
        emit(s, user.tenant_id, "agent.paused" if paused else "agent.resumed")
    return {"paused": paused}


@router.post("/agent/pause")
def pause_agent(user: AuthUser = Depends(require_role("manager"))):
    return _set_pause(user, True)


@router.post("/agent/resume")
def resume_agent(user: AuthUser = Depends(require_role("manager"))):
    return _set_pause(user, False)


# --- statistiques -----------------------------------------------------------------------

def _tz_and_day(s, day: date | None) -> tuple[str, date]:
    tz = s.execute(text("SELECT COALESCE(config->>'timezone', :d) FROM tenants"), {"d": DEFAULT_TZ}).scalar()
    if day is None:
        day = s.execute(text("SELECT (now() AT TIME ZONE :tz)::date"), {"tz": tz}).scalar()
    return tz, day


@router.get("/stats/overview")
def stats_overview(day: date | None = None, user: AuthUser = Depends(current_user)):
    with tenant_session(user.tenant_id) as s:
        tz, day = _tz_and_day(s, day)
        bounds = {
            "tz": tz,
            "d0": day,
        }

        def window(offset: int) -> str:
            return (
                f"((CAST(:d0 AS date) + {offset})::timestamp AT TIME ZONE :tz)",
                f"((CAST(:d0 AS date) + {offset + 1})::timestamp AT TIME ZONE :tz)",
            )

        out = {"day": day, "timezone": tz}
        for label, off in (("today", 0), ("yesterday", -1)):
            a, b = window(off)
            out[label] = s.execute(
                text(
                    f"""
                    SELECT
                      (SELECT count(*) FROM messages WHERE direction = 'in' AND received_at >= {a} AND received_at < {b}) AS messages_in,
                      (SELECT count(*) FROM messages WHERE author = 'agent' AND delivery_status = 'sent' AND sent_at >= {a} AND sent_at < {b}) AS agent_replies,
                      (SELECT count(*) FROM escalations WHERE created_at >= {a} AND created_at < {b}) AS escalations,
                      (SELECT round(avg(extract(epoch FROM (o.sent_at - i.received_at)))::numeric, 1)
                         FROM messages o JOIN messages i ON i.id::text = substring(o.idempotency_key from 7)
                          AND o.conversation_id = i.conversation_id
                        WHERE o.author = 'agent' AND o.delivery_status = 'sent' AND o.idempotency_key LIKE 'reply:%'
                          AND o.sent_at >= {a} AND o.sent_at < {b}) AS avg_response_seconds,
                      (SELECT coalesce(sum(input_tokens), 0) FROM agent_runs WHERE started_at >= {a} AND started_at < {b}) AS input_tokens,
                      (SELECT coalesce(sum(output_tokens), 0) FROM agent_runs WHERE started_at >= {a} AND started_at < {b}) AS output_tokens
                    """
                ),
                bounds,
            ).mappings().one()
            out[label] = dict(out[label])
        out["escalations_by_status"] = dict(s.execute(text("SELECT status, count(*) FROM escalations GROUP BY status")).all())
        out["conversations_by_status"] = dict(s.execute(text("SELECT status, count(*) FROM conversations GROUP BY status")).all())
    return out


@router.get("/stats/activity")
def stats_activity(day: date | None = None, user: AuthUser = Depends(current_user)):
    """Messages reçus par heure (heure locale du client), aujourd'hui et la veille."""
    with tenant_session(user.tenant_id) as s:
        tz, day = _tz_and_day(s, day)
        result = {"day": day, "timezone": tz}
        for label, off in (("today", 0), ("yesterday", -1)):
            rows = s.execute(
                text(
                    "SELECT extract(hour FROM received_at AT TIME ZONE :tz)::int AS h, count(*) AS n FROM messages "
                    "WHERE direction = 'in' AND received_at >= ((CAST(:d0 AS date) + :off)::timestamp AT TIME ZONE :tz) "
                    "AND received_at < ((CAST(:d0 AS date) + :off + 1)::timestamp AT TIME ZONE :tz) GROUP BY h"
                ),
                {"tz": tz, "d0": day, "off": off},
            ).all()
            by_hour = [0] * 24
            for h, n in rows:
                by_hour[h] = n
            result[label] = by_hour
    return result
