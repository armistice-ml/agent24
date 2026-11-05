"""Remplace la configuration d'un client (tone, outils activés, informations officielles...).
À lancer avec le rôle PROPRIÉTAIRE.

  python -m scripts.set_tenant_config --tenant-id <UUID> --file tenants/vigo-services.json
"""
import argparse
import json
from uuid import UUID

from sqlalchemy import Engine, create_engine, text

from app.config import get_settings


def set_tenant_config(engine: Engine, tenant_id: UUID, config: dict) -> None:
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        n = conn.execute(
            text("UPDATE tenants SET config = CAST(:c AS jsonb) WHERE id = :t"),
            {"c": json.dumps(config, ensure_ascii=False), "t": tenant_id},
        ).rowcount
        if n != 1:
            raise SystemExit("client introuvable")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tenant-id", required=True, type=UUID)
    p.add_argument("--file", required=True)
    a = p.parse_args()
    with open(a.file, encoding="utf-8") as f:
        config = json.load(f)
    set_tenant_config(create_engine(get_settings().migrations_database_url), a.tenant_id, config)
    print("configuration enregistrée")


if __name__ == "__main__":
    main()
