"""Envoi des réponses vers les canaux."""
import logging
from typing import Protocol

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)
MAX_CHARS = 1900  # Messenger limite un message à 2000 caractères


class SendError(Exception):
    def __init__(self, message: str, *, permanent: bool):
        super().__init__(message)
        self.permanent = permanent


class Sender(Protocol):
    def send_text(self, recipient_id: str, text: str) -> str | None:
        """Envoie le texte ; retourne l'identifiant du message côté plateforme."""

    def get_profile(self, recipient_id: str) -> dict | None:
        """Profil public du client (ex. {'name': ...}) ou None s'il n'est pas disponible."""


def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Découpe en morceaux <= limit, de préférence aux fins de paragraphe, de ligne, puis d'espace."""
    text, chunks = text.strip(), []
    while len(text) > limit:
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = text.rfind(sep, 0, limit)
            if cut >= limit // 2:
                break
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


class ConsoleSender:
    """Développement : affiche la réponse au lieu de l'envoyer."""

    def send_text(self, recipient_id: str, text: str) -> str | None:
        print(f"\n[console] -> {recipient_id}:\n{text}\n")
        return "console"

    def get_profile(self, recipient_id: str) -> dict | None:
        return None


class MessengerSender:
    def __init__(self, page_token: str, client: httpx.Client | None = None):
        self._token = page_token
        self._client = client or httpx.Client(timeout=15)

    def send_text(self, recipient_id: str, text: str) -> str | None:
        s = get_settings()
        url = f"https://graph.facebook.com/{s.graph_api_version}/me/messages"
        last_id = None
        for chunk in split_text(text):
            try:
                r = self._client.post(
                    url,
                    headers={"Authorization": f"Bearer {self._token}"},  # jeton hors URL (donc hors logs)
                    json={
                        "recipient": {"id": recipient_id},
                        "messaging_type": "RESPONSE",
                        "message": {"text": chunk},
                    },
                )
            except httpx.TransportError as e:
                raise SendError(f"réseau: {type(e).__name__}", permanent=False) from e
            if r.status_code >= 500 or r.status_code == 429:
                raise SendError(f"meta {r.status_code}", permanent=False)
            if r.status_code >= 400:
                # 4xx : jeton invalide, fenêtre de 24 h dépassée, utilisateur bloqué... inutile de réessayer.
                raise SendError(f"meta {r.status_code}: {r.text[:200]}", permanent=True)
            last_id = r.json().get("message_id")
        return last_id

    def get_profile(self, recipient_id: str) -> dict | None:
        s = get_settings()
        try:
            r = self._client.get(
                f"https://graph.facebook.com/{s.graph_api_version}/{recipient_id}",
                headers={"Authorization": f"Bearer {self._token}"},
                params={"fields": "name"},
            )
        except httpx.TransportError as e:
            raise SendError(f"réseau: {type(e).__name__}", permanent=False) from e
        if r.status_code >= 500 or r.status_code == 429:
            raise SendError(f"meta {r.status_code}", permanent=False)
        if r.status_code >= 400:
            return None  # permission absente ou profil indisponible : on n'insiste pas
        return r.json()


def get_sender(channel_type: str, token: str) -> Sender:
    if get_settings().send_mode == "console":
        return ConsoleSender()
    if channel_type == "messenger":
        return MessengerSender(token)
    raise SendError(f"canal non supporté pour l'envoi: {channel_type}", permanent=True)
