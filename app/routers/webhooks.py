import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from app.channels import messenger
from app.config import get_settings
from app.services.handoff import register_page_reply
from app.services.ingest import ingest

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks")


@router.get("/messenger")
def verify_subscription(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    """Appelé une fois par Meta quand on configure le webhook."""
    expected = get_settings().messenger_verify_token
    if mode == "subscribe" and hmac.compare_digest(token, expected):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/messenger")
async def receive(request: Request):
    raw = await request.body()
    # 1. Signature AVANT tout traitement
    if not messenger.verify_signature(
        get_settings().meta_app_secret, raw, request.headers.get("X-Hub-Signature-256")
    ):
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")
    if payload.get("object") != "page":
        raise HTTPException(status_code=404, detail="not a page event")

    # 2. Normalisation + enregistrement idempotent. Une erreur => 500 => Meta renverra
    #    l'événement, et l'idempotence évitera les doublons.
    for msg in messenger.parse_payload(payload):
        result = await run_in_threadpool(ingest, msg)
        log.info("messenger %s -> %s", msg.idempotency_key, result.status)  # jamais le contenu

    # 2 bis. Réponses faites par un humain depuis la boîte de la page : l'agent doit se taire.
    for echo in messenger.parse_echoes(payload):
        outcome = await run_in_threadpool(register_page_reply, echo)
        log.info("messenger écho %s -> %s", echo.mid, outcome)

    # 3. Toujours 200 si tout s'est bien passé (même canal inconnu), pour éviter les renvois en boucle.
    return {"status": "ok"}
