from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class NormalizedMessage(BaseModel):
    """Format unique vers lequel tous les canaux sont traduits avant l'orchestrateur."""

    channel_type: Literal["messenger", "instagram", "whatsapp", "telegram", "email"]
    channel_external_id: str      # identifiant du canal côté plateforme (ex. id de la page)
    channel_user_id: str          # identifiant du contact sur le canal (ex. PSID)
    type: Literal["text", "image", "audio", "video", "file", "other"]
    text: str | None = None
    media_ref: str | None = None  # URL temporaire fournie par la plateforme : à télécharger vite
    received_at: datetime
    idempotency_key: str          # ex. "messenger:<mid>"
    raw: dict                     # événement brut, pour le débogage (contient des données perso)


class EchoEvent(BaseModel):
    """Message envoyé PAR la page (écho). Peut venir de notre app ou d'un humain via la boîte de la page."""

    channel_external_id: str      # identifiant de la page
    customer_id: str              # identifiant du client (destinataire de l'écho)
    mid: str
    app_id: str | None = None     # app émettrice, si Meta la précise
    type: str = "text"
    text: str | None = None
    received_at: datetime
