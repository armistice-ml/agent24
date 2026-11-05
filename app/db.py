"""Accès base de données.

Règle d'or : toute lecture/écriture de données d'un client passe par `tenant_session`,
qui fixe `app.tenant_id` pour la transaction. La RLS PostgreSQL fait le reste.
Le tenant_id vient TOUJOURS du serveur (routage du canal), jamais du modèle ni du client.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


@contextmanager
def tenant_session(tenant_id: UUID) -> Iterator[Session]:
    """Transaction dans laquelle seules les lignes de `tenant_id` sont visibles."""
    with get_sessionmaker()() as session, session.begin():
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield session


@contextmanager
def routing_session() -> Iterator[Session]:
    """Session SANS tenant : uniquement pour lire `channel_routes` (routage des webhooks)."""
    with get_sessionmaker()() as session, session.begin():
        yield session


def assert_safe_role() -> None:
    """Refuse de démarrer si le rôle applicatif contourne la RLS."""
    with get_engine().connect() as conn:
        rolsuper, bypass = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    if rolsuper or bypass:
        raise RuntimeError(
            "Le rôle de base de données de l'application est superuser ou BYPASSRLS : "
            "l'isolation entre clients ne serait pas garantie. Utilisez agent24_app."
        )
