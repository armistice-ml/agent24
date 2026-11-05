"""Crée un client (tenant) et son canal Messenger. À lancer avec le rôle PROPRIÉTAIRE.

Exemple :
  PAGE_ACCESS_TOKEN=EAAB... python -m scripts.create_tenant \
      --name "Vigo Services" --slug vigo-services --sector services --page-id 123456789
"""
import argparse
import getpass
import os
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, text

from app.config import get_settings
from app.crypto import encrypt


def create_tenant(
    engine: Engine,
    *,
    name: str,
    slug: str,
    sector: str | None,
    page_id: str,
    page_token: str,
    channel_type: str = "messenger",
) -> tuple[UUID, UUID]:
    tenant_id, channel_id = uuid4(), uuid4()
    with engine.begin() as conn:
        # La RLS est forcée même pour le propriétaire : on se place dans le tenant à créer.
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text("INSERT INTO tenants (id, name, slug, sector) VALUES (:id, :name, :slug, :sector)"),
            {"id": tenant_id, "name": name, "slug": slug, "sector": sector},
        )
        conn.execute(
            text(
                "INSERT INTO channels (id, tenant_id, type, external_id, name, credentials_encrypted) "
                "VALUES (:id, :tenant_id, :type, :ext, :name, :cred)"
            ),
            {
                "id": channel_id,
                "tenant_id": tenant_id,
                "type": channel_type,
                "ext": page_id,
                "name": name,
                "cred": encrypt(page_token),
            },
        )
    return tenant_id, channel_id


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--sector")
    p.add_argument("--page-id", required=True, help="Identifiant de la page Facebook")
    args = p.parse_args()
    token = os.environ.get("PAGE_ACCESS_TOKEN") or getpass.getpass("Jeton d'accès de la page : ")

    engine = create_engine(get_settings().migrations_database_url)
    tenant_id, channel_id = create_tenant(
        engine, name=args.name, slug=args.slug, sector=args.sector, page_id=args.page_id, page_token=token
    )
    print(f"tenant_id={tenant_id}\nchannel_id={channel_id}")


if __name__ == "__main__":
    main()
