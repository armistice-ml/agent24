"""Worker : lit la file de jobs.   python -m app.worker

Jobs : 'reply' (faire répondre l'agent à un message) et 'notify_escalation' (email aux responsables).
"""
import logging
import signal
import time

from app.agent.llm import LLMClient
from app.agent.orchestrator import handle_job, on_terminal_failure
from app.channels.sender import SendError, get_sender
from app.config import get_settings
from app.db import assert_safe_role
from app.notify.email import EmailError, EmailSender, get_email_sender
from app.services.jobs import Job, claim_job, complete_job, fail_job, requeue_stale
from app.services.notifications import NoRecipients, send_escalation_email
from app.agent.factory import build_llm

log = logging.getLogger("worker")
_stop = False


def process(job: Job, llm: LLMClient, sender_factory=get_sender, email_sender: EmailSender | None = None) -> None:
    if job.kind == "notify_escalation":
        try:
            status = send_escalation_email(job, email_sender or get_email_sender())
            complete_job(job.id)
            log.info("job %s (email d'escalade) -> %s", job.id, status)
        except NoRecipients as e:
            fail_job(job, str(e), permanent=True)
            log.error("job %s: %s", job.id, e)
        except EmailError as e:
            terminal = fail_job(job, str(e))
            log.warning("job %s: échec d'envoi de l'email (%s) terminal=%s", job.id, e, terminal)
        except Exception as e:
            log.exception("job %s: échec", job.id)
            fail_job(job, f"{type(e).__name__}: {e}")
        return

    try:
        status = handle_job(job, llm=llm, sender_factory=sender_factory)
        complete_job(job.id)
        log.info("job %s -> %s", job.id, status)
    except SendError as e:
        terminal = fail_job(job, str(e), permanent=e.permanent)
        log.warning("job %s: échec d'envoi (%s) terminal=%s", job.id, e, terminal)
        if terminal:
            on_terminal_failure(job, sender_factory=sender_factory)
    except Exception as e:
        log.exception("job %s: échec", job.id)
        terminal = fail_job(job, f"{type(e).__name__}: {e}")
        if terminal:
            on_terminal_failure(job, sender_factory=sender_factory)


def run_once(llm: LLMClient, sender_factory=get_sender, email_sender: EmailSender | None = None) -> bool:
    """Traite au plus un job. Retourne False si la file était vide."""
    requeue_stale()
    job = claim_job()
    if job is None:
        return False
    process(job, llm, sender_factory, email_sender)
    return True


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    assert_safe_role()
    llm = build_llm(settings)

    def _handle(*_):
        global _stop
        _stop = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    log.info(
        "worker démarré (modèle: %s, envoi: %s, email: %s)", settings.agent_model, settings.send_mode, settings.email_mode
    )
    while not _stop:
        if not run_once(llm):
            time.sleep(settings.worker_poll_seconds)
    log.info("worker arrêté proprement")


if __name__ == "__main__":
    main()
