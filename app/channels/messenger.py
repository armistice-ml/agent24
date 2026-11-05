"""Adaptateur Messenger : vérification de signature + traduction vers NormalizedMessage."""
import hashlib
import hmac
import logging
from datetime import UTC, datetime

from app.schemas import EchoEvent, NormalizedMessage

log = logging.getLogger(__name__)

_ATTACHMENT_TYPES = {"image", "audio", "video", "file"}


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Contrôle l'en-tête X-Hub-Signature-256 sur le corps BRUT de la requête."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_payload(payload: dict) -> list[NormalizedMessage]:
    """Extrait les messages entrants. Ignore échos, accusés de lecture/livraison, postbacks."""
    messages: list[NormalizedMessage] = []
    for entry in payload.get("entry", []):
        page_id = str(entry.get("id", ""))
        for event in entry.get("messaging", []):
            msg = event.get("message")
            sender = (event.get("sender") or {}).get("id")
            if not msg or not sender or not page_id:
                log.debug("événement Messenger ignoré (pas un message entrant)")
                continue
            if msg.get("is_echo"):
                continue  # message envoyé par la page elle-même (plus tard : détection de reprise humaine)
            mid = msg.get("mid")
            if not mid:
                continue

            attachments = msg.get("attachments") or []
            kind, media_ref = "text", None
            if attachments:
                first = attachments[0]
                kind = first.get("type") if first.get("type") in _ATTACHMENT_TYPES else "other"
                media_ref = (first.get("payload") or {}).get("url")

            ts = event.get("timestamp")
            received_at = (
                datetime.fromtimestamp(ts / 1000, tz=UTC) if isinstance(ts, (int, float)) else datetime.now(UTC)
            )
            messages.append(
                NormalizedMessage(
                    channel_type="messenger",
                    channel_external_id=page_id,
                    channel_user_id=str(sender),
                    type=kind,
                    text=msg.get("text"),
                    media_ref=media_ref,
                    received_at=received_at,
                    idempotency_key=f"messenger:{mid}",
                    raw=event,
                )
            )
    return messages


def parse_echoes(payload: dict) -> list[EchoEvent]:
    """Extrait les échos (messages envoyés par la page). Ne contient jamais de pièce jointe téléchargée."""
    echoes: list[EchoEvent] = []
    for entry in payload.get("entry", []):
        page_id = str(entry.get("id", ""))
        for event in entry.get("messaging", []):
            msg = event.get("message") or {}
            if not msg.get("is_echo") or not msg.get("mid"):
                continue
            customer = (event.get("recipient") or {}).get("id")
            if not customer or not page_id:
                continue
            ts = event.get("timestamp")
            received_at = (
                datetime.fromtimestamp(ts / 1000, tz=UTC) if isinstance(ts, (int, float)) else datetime.now(UTC)
            )
            app_id = msg.get("app_id")
            echoes.append(
                EchoEvent(
                    channel_external_id=page_id,
                    customer_id=str(customer),
                    mid=msg["mid"],
                    app_id=str(app_id) if app_id else None,
                    type="text" if msg.get("text") else "other",
                    text=msg.get("text"),
                    received_at=received_at,
                )
            )
    return echoes
