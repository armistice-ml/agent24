"""File de jobs dans PostgreSQL (SKIP LOCKED). Ordre garanti PAR CONVERSATION pour les réponses.

Règle : un job n'est réclamable que s'il n'existe, dans la même conversation, ni job en cours
ni job en attente plus ancien. Deux workers ne traitent donc jamais la même conversation
en parallèle, et les messages d'un client sont traités dans l'ordre.
"""
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from app.db import routing_session

BACKOFF_SECONDS = [30, 120, 600, 1800]  # 30 s, 2 min, 10 min, 30 min
STALE_AFTER_MINUTES = 5


@dataclass
class Job:
    id: int
    tenant_id: UUID
    conversation_id: UUID
    message_id: UUID | None
    attempts: int
    max_attempts: int
    kind: str = "reply"
    escalation_id: UUID | None = None


def claim_job() -> Job | None:
    with routing_session() as s:
        row = s.execute(
            text(
                """
                WITH next AS (
                  SELECT j.id FROM jobs j
                  WHERE j.status = 'pending' AND j.run_after <= now()
                    AND NOT EXISTS (
                      SELECT 1 FROM jobs p
                      WHERE j.kind = 'reply' AND p.kind = 'reply'
                        AND p.conversation_id = j.conversation_id AND p.id <> j.id
                        AND (p.status = 'processing' OR (p.status = 'pending' AND p.id < j.id))
                    )
                  ORDER BY j.id
                  LIMIT 1
                  FOR UPDATE OF j SKIP LOCKED
                )
                UPDATE jobs SET status = 'processing', locked_at = now(), attempts = jobs.attempts + 1
                FROM next WHERE jobs.id = next.id
                RETURNING jobs.id, jobs.tenant_id, jobs.conversation_id, jobs.message_id,
                          jobs.attempts, jobs.max_attempts, jobs.kind, jobs.escalation_id
                """
            )
        ).first()
    return Job(*row) if row else None


def complete_job(job_id: int) -> None:
    with routing_session() as s:
        s.execute(text("UPDATE jobs SET status='done', finished_at=now(), locked_at=NULL WHERE id=:i"), {"i": job_id})


def fail_job(job: Job, error: str, *, permanent: bool = False) -> bool:
    """Replanifie avec délai croissant. Retourne True si l'échec est définitif."""
    terminal = permanent or job.attempts >= job.max_attempts
    delay = BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)]
    with routing_session() as s:
        if terminal:
            s.execute(
                text("UPDATE jobs SET status='failed', finished_at=now(), locked_at=NULL, last_error=:e WHERE id=:i"),
                {"i": job.id, "e": error[:500]},
            )
        else:
            s.execute(
                text(
                    "UPDATE jobs SET status='pending', locked_at=NULL, last_error=:e, "
                    "run_after = now() + make_interval(secs => :d) WHERE id=:i"
                ),
                {"i": job.id, "e": error[:500], "d": delay},
            )
    return terminal


def requeue_stale() -> int:
    """Remet en file les jobs bloqués en 'processing' (worker tombé en cours de route)."""
    with routing_session() as s:
        return s.execute(
            text(
                "UPDATE jobs SET status='pending', locked_at=NULL "
                "WHERE status='processing' AND locked_at < now() - make_interval(mins => :m)"
            ),
            {"m": STALE_AFTER_MINUTES},
        ).rowcount
