"""Schéma initial multi-tenant : tenants, channels, contacts, conversations, messages.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "agent24_app"

TENANT_POLICY = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    # Échoue clairement si le rôle applicatif n'existe pas (voir docker/initdb/01-roles.sql).
    op.execute(f"""
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        RAISE EXCEPTION 'Le rôle {APP_ROLE} doit exister avant la migration';
      END IF;
    END $$;
    """)

    op.execute("""
    CREATE TABLE tenants (
      id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      name       text NOT NULL,
      slug       text NOT NULL UNIQUE,
      sector     text,
      config     jsonb NOT NULL DEFAULT '{}'::jsonb,
      status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
      created_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE channels (
      id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      type                  text NOT NULL
                            CHECK (type IN ('messenger', 'instagram', 'whatsapp', 'telegram', 'email')),
      external_id           text NOT NULL,          -- ex. identifiant de la page Facebook
      name                  text,
      credentials_encrypted text,                   -- jeton d'accès chiffré (Fernet)
      status                text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
      created_at            timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      UNIQUE (tenant_id, type, external_id)
    );

    -- Table de routage : retrouver le tenant à partir de l'identifiant de page reçu dans
    -- un webhook, AVANT de connaître le tenant. Pas de RLS (aucune donnée personnelle),
    -- alimentée automatiquement par un trigger. Une page ne peut appartenir qu'à un seul tenant.
    CREATE TABLE channel_routes (
      type        text NOT NULL,
      external_id text NOT NULL,
      channel_id  uuid NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
      tenant_id   uuid NOT NULL,
      PRIMARY KEY (type, external_id)
    );

    CREATE FUNCTION sync_channel_route() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $fn$
    BEGIN
      IF TG_OP IN ('UPDATE', 'DELETE') THEN
        DELETE FROM channel_routes WHERE channel_id = OLD.id;
      END IF;
      IF TG_OP IN ('INSERT', 'UPDATE') AND NEW.status = 'active' THEN
        INSERT INTO channel_routes (type, external_id, channel_id, tenant_id)
        VALUES (NEW.type, NEW.external_id, NEW.id, NEW.tenant_id);
      END IF;
      RETURN COALESCE(NEW, OLD);
    END
    $fn$;

    CREATE TRIGGER channels_sync_route
      AFTER INSERT OR UPDATE OR DELETE ON channels
      FOR EACH ROW EXECUTE FUNCTION sync_channel_route();

    CREATE TABLE contacts (
      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      channel_id      uuid NOT NULL,
      channel_user_id text NOT NULL,                -- ex. PSID Messenger
      name            text,
      language        text,
      profile         jsonb NOT NULL DEFAULT '{}'::jsonb,
      consents        jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at      timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      UNIQUE (tenant_id, channel_id, channel_user_id),
      FOREIGN KEY (tenant_id, channel_id) REFERENCES channels (tenant_id, id) ON DELETE CASCADE
    );

    CREATE TABLE conversations (
      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      channel_id      uuid NOT NULL,
      contact_id      uuid NOT NULL,
      status          text NOT NULL DEFAULT 'agent' CHECK (status IN ('agent', 'human', 'closed')),
      last_message_at timestamptz,
      created_at      timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      FOREIGN KEY (tenant_id, channel_id) REFERENCES channels (tenant_id, id) ON DELETE CASCADE,
      FOREIGN KEY (tenant_id, contact_id) REFERENCES contacts (tenant_id, id) ON DELETE CASCADE
    );
    -- Une seule conversation ouverte par contact (protège contre les courses).
    CREATE UNIQUE INDEX one_open_conversation_per_contact
      ON conversations (tenant_id, contact_id) WHERE status <> 'closed';

    CREATE TABLE messages (
      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      conversation_id uuid NOT NULL,
      channel_id      uuid NOT NULL,
      direction       text NOT NULL CHECK (direction IN ('in', 'out')),
      type            text NOT NULL DEFAULT 'text',
      text            text,
      media_ref       text,
      language        text,
      raw             jsonb,
      idempotency_key text NOT NULL,
      received_at     timestamptz NOT NULL DEFAULT now(),
      created_at      timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, channel_id, idempotency_key),
      FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, id) ON DELETE CASCADE,
      FOREIGN KEY (tenant_id, channel_id) REFERENCES channels (tenant_id, id) ON DELETE CASCADE
    );
    CREATE INDEX messages_conversation_time ON messages (tenant_id, conversation_id, created_at);
    """)

    # Row-Level Security : activée ET forcée (s'applique aussi au propriétaire des tables).
    op.execute(f"""
    ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON tenants
      USING (id = nullif(current_setting('app.tenant_id', true), '')::uuid)
      WITH CHECK (id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    """)
    for table in ("channels", "contacts", "conversations", "messages"):
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
          USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY});
        """)

    # Moindre privilège pour le rôle applicatif.
    op.execute(f"""
    GRANT USAGE ON SCHEMA public TO {APP_ROLE};
    GRANT SELECT ON tenants, channels, channel_routes TO {APP_ROLE};
    GRANT SELECT, INSERT, UPDATE ON contacts, conversations, messages TO {APP_ROLE};
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS messages, conversations, contacts, channel_routes, channels, tenants CASCADE;
    DROP FUNCTION IF EXISTS sync_channel_route();
    """)
