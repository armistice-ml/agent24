"""Envoie un faux événement Messenger SIGNÉ au serveur local (sans passer par Meta).

  python -m scripts.send_test_webhook --page-id 123456789 --psid 5551234 --text "Bonjour"
"""
import argparse
import hashlib
import hmac
import json
import time

import httpx

from app.config import get_settings


def build_payload(page_id: str, psid: str, text: str, mid: str | None = None) -> dict:
    return {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": int(time.time() * 1000),
                "messaging": [
                    {
                        "sender": {"id": psid},
                        "recipient": {"id": page_id},
                        "timestamp": int(time.time() * 1000),
                        "message": {"mid": mid or f"m_{time.time_ns()}", "text": text},
                    }
                ],
            }
        ],
    }


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000/webhooks/messenger")
    p.add_argument("--page-id", required=True)
    p.add_argument("--psid", default="test_user_1")
    p.add_argument("--text", default="Bonjour, quels sont vos horaires ?")
    p.add_argument("--mid")
    a = p.parse_args()

    body = json.dumps(build_payload(a.page_id, a.psid, a.text, a.mid)).encode()
    r = httpx.post(
        a.url,
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(get_settings().meta_app_secret, body)},
    )
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
