"""Notification d'escalade par email aux responsables (owner/manager)."""
from sqlalchemy import text

from app.config import get_settings
from app.db import tenant_session
from app.notify.email import EmailSender, _clean_header
from app.services.jobs import Job

CATEGORY_LABELS = {
    "plainte": "Plainte",
    "litige": "Litige",
    "paiement": "Paiement",
    "information_manquante": "Information non renseignée",
    "sensible": "Sujet sensible",
    "technique": "Problème technique",
    "autre": "Autre demande",
}
CHANNEL_LABELS = {"messenger": "Messenger", "whatsapp": "WhatsApp", "instagram": "Instagram", "telegram": "Telegram", "email": "Email"}


class NoRecipients(Exception):
    """Aucun responsable à prévenir : visible dans la file (job en échec), jamais silencieux."""


def send_escalation_email(job: Job, email_sender: EmailSender) -> str:
    with tenant_session(job.tenant_id) as s:
        row = s.execute(
            text(
                "SELECT e.category, e.reason, e.summary, e.status, e.conversation_id, "
                "ct.name AS contact_name, ch.type AS channel_type, t.name AS tenant_name "
                "FROM escalations e JOIN conversations cv ON cv.id = e.conversation_id "
                "JOIN contacts ct ON ct.id = cv.contact_id JOIN channels ch ON ch.id = cv.channel_id "
                "JOIN tenants t ON t.id = e.tenant_id WHERE e.id = :e"
            ),
            {"e": job.escalation_id},
        ).first()
        recipients = (
            s.execute(
                text(
                    "SELECT email FROM users WHERE status = 'active' AND notify_escalations "
                    "AND role IN ('owner', 'manager') ORDER BY created_at"
                )
            )
            .scalars()
            .all()
        )
    if row is None:
        return "skipped:missing"
    if row.status == "resolved":
        return "skipped:already_resolved"  # traitée avant l'envoi de l'email
    if not recipients:
        raise NoRecipients("aucun responsable (owner/manager) avec les notifications activées")

    category = CATEGORY_LABELS.get(row.category, "Autre demande")
    channel = CHANNEL_LABELS.get(row.channel_type, row.channel_type)
    base = get_settings().dashboard_base_url.rstrip("/")
    link = f"{base}/conversations/{row.conversation_id}" if base else None
    lines = [
        "Bonjour,",
        "",
        f"Une conversation {channel} demande votre attention : l'assistant a transmis la main.",
        "",
        f"Client : {row.contact_name or 'client sans nom enregistré'}",
        f"Motif : {category}",
        f"Résumé : {row.summary or '(non précisé)'}",
    ]
    if row.reason:
        lines.append(f"Détail : {row.reason}")
    if link:
        lines += ["", f"Répondre depuis le tableau de bord : {link}"]
    lines += ["", "— Agent24"]
    subject = _clean_header(f"[{row.tenant_name}] Client à traiter : {category}")
    email_sender.send(list(recipients), subject, "\n".join(lines))
    return "sent"
