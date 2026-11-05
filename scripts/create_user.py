"""Crée un compte du tableau de bord. À lancer avec le rôle PROPRIÉTAIRE.

  USER_PASSWORD='...' python -m scripts.create_user --tenant-id <UUID> --email dirigeant@exemple.com \
      --name "Prénom Nom" --role owner
Rôles : owner, manager, employee, readonly. Mot de passe : 10 caractères minimum.
"""
import argparse
import getpass
import os
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, text

from app.config import get_settings
from app.security import hash_password

ROLES = ("owner", "manager", "employee", "readonly")


def create_user(engine: Engine, *, tenant_id: UUID, email: str, name: str, role: str, password: str,
                notify_escalations: bool | None = None) -> UUID:
    if role not in ROLES:
        raise ValueError(f"rôle invalide: {role}")
    if len(password) < 10:
        raise ValueError("mot de passe trop court (10 caractères minimum)")
    user_id = uuid4()
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, name, role, password_hash, notify_escalations) "
                "VALUES (:id, :t, :email, :name, :role, :ph, :n)"
            ),
            {"id": user_id, "t": tenant_id, "email": email.strip().lower(), "name": name, "role": role,
             "ph": hash_password(password), "n": role in ("owner", "manager") if notify_escalations is None else notify_escalations},
        )
    return user_id


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tenant-id", required=True, type=UUID)
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--role", required=True, choices=ROLES)
    a = p.parse_args()
    password = os.environ.get("USER_PASSWORD") or getpass.getpass("Mot de passe : ")
    uid = create_user(
        create_engine(get_settings().migrations_database_url),
        tenant_id=a.tenant_id, email=a.email, name=a.name, role=a.role, password=password,
    )
    print(f"user_id={uid}")


if __name__ == "__main__":
    main()
