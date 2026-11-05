"""Envoi d'emails (notification d'escalade). Mode console en développement."""
import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from app.config import get_settings

log = logging.getLogger(__name__)


class EmailError(Exception):
    pass


class EmailSender(Protocol):
    def send(self, to: list[str], subject: str, body: str) -> None: ...


def _clean_header(value: str) -> str:
    return " ".join(value.split())  # supprime retours à la ligne (injection d'en-têtes)


class ConsoleEmailSender:
    def send(self, to: list[str], subject: str, body: str) -> None:
        print(f"\n[email console] à: {', '.join(to)}\nobjet: {subject}\n{body}\n")


class SmtpEmailSender:
    def send(self, to: list[str], subject: str, body: str) -> None:
        s = get_settings()
        if not (s.smtp_host and s.email_from):
            raise EmailError("SMTP non configuré (SMTP_HOST, EMAIL_FROM)")
        msg = EmailMessage()
        msg["From"] = _clean_header(s.email_from)
        msg["To"] = ", ".join(_clean_header(t) for t in to)
        msg["Subject"] = _clean_header(subject)
        msg.set_content(body)
        try:
            if s.smtp_security == "ssl":
                server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20)
            else:
                server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
            with server:
                if s.smtp_security == "starttls":
                    server.starttls()
                if s.smtp_user:
                    server.login(s.smtp_user, s.smtp_password)
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            raise EmailError(f"échec SMTP: {type(e).__name__}") from e


def get_email_sender() -> EmailSender:
    return SmtpEmailSender() if get_settings().email_mode == "smtp" else ConsoleEmailSender()
