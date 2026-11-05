from alembic import context
from sqlalchemy import create_engine

from app.config import get_settings

config = context.config


def run_migrations_online() -> None:
    # Les migrations tournent avec le rôle PROPRIÉTAIRE, jamais avec le rôle applicatif.
    engine = create_engine(get_settings().migrations_database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
