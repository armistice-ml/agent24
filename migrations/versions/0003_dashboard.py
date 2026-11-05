"""Tableau de bord : comptes, sessions, escalades, événements temps réel, auteur des messages.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = "agent24_app"
TENANT_POLICY = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("""
    CREATE TABLE users (
      id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      email              text NOT NULL,
      name               text NOT NULL,
      role               text NOT NULL CHECK (role IN ('owner', 'manager', 'employee', 'readonly')),
      password_hash      text NOT NULL,
      status             text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
      notify_escalations boolean NOT NULL DEFAULT true,
      last_login_at      timestamptz,
      created_at         timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id)
    );
    -- La connexion se fait sans connaître le client : l'email est donc unique sur toute la plateforme.
    CREATE UNIQUE INDEX users_email_unique ON users (lower(email));

    -- Index de connexion (email -> client). Sans RLS, aucune donnée sensible, tenu à jour par trigger.
    CREATE TABLE login_index (
      email     text PRIMARY KEY,
      user_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      tenant_id uuid NOT NULL
    );

    CREATE FUNCTION sync_login_index() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $fn$
    BEGIN
      IF TG_OP IN ('UPDATE', 'DELETE') THEN
        DELETE FROM login_index WHERE user_id = OLD.id;
      END IF;
      IF TG_OP IN ('INSERT', 'UPDATE') AND NEW.status = 'active' THEN
        INSERT INTO login_index (email, user_id, tenant_id) VALUES (lower(NEW.email), NEW.id, NEW.tenant_id);
      END IF;
      RETURN COALESCE(NEW, OLD);
    END
    $fn$;
    CREATE TRIGGER users_sync_login_index AFTER INSERT OR UPDATE OR DELETE ON users
      FOR EACH ROW EXECUTE FUNCTION sync_login_index();

    -- Sessions : seul le HASH du jeton est stocké. Sans RLS (on retrouve le client à partir du jeton).
    CREATE TABLE sessions (
      token_hash   text PRIMARY KEY,
      user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      tenant_id    uuid NOT NULL,
      created_at   timestamptz NOT NULL DEFAULT now(),
      last_seen_at timestamptz NOT NULL DEFAULT now(),
      expires_at   timestamptz NOT NULL
    );
    CREATE INDEX sessions_user ON sessions (user_id);

    -- Conversations : prise en charge humaine, lecture par l'équipe
    ALTER TABLE conversations
      ADD COLUMN assigned_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
      ADD COLUMN handoff_reason text,      -- escalation | dashboard | page_inbox
      ADD COLUMN team_read_at timestamptz;

    -- Messages : auteur (agent, humain du tableau de bord, humain depuis la boîte de la page)
    ALTER TABLE messages
      ADD COLUMN author text CHECK (author IN ('customer', 'agent', 'user', 'page_inbox')),
      ADD COLUMN sent_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL;
    UPDATE messages SET author = CASE direction WHEN 'in' THEN 'customer' ELSE 'agent' END;

    ALTER TABLE contacts ADD COLUMN profile_fetched_at timestamptz;

    -- Escalades
    CREATE TABLE escalations (
      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      conversation_id uuid NOT NULL,
      run_id          uuid,
      source          text NOT NULL CHECK (source IN ('agent', 'system')),
      category        text NOT NULL DEFAULT 'autre' CHECK (category IN
                        ('plainte', 'litige', 'paiement', 'information_manquante', 'sensible', 'technique', 'autre')),
      reason          text,
      summary         text,
      status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'taken', 'resolved')),
      resolution      text CHECK (resolution IN ('handed_back', 'closed')),
      taken_by        uuid REFERENCES users(id) ON DELETE SET NULL,
      taken_at        timestamptz,
      resolved_at     timestamptz,
      created_at      timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, id) ON DELETE CASCADE
    );
    -- Une seule escalade active par conversation
    CREATE UNIQUE INDEX one_active_escalation_per_conversation
      ON escalations (tenant_id, conversation_id) WHERE status <> 'resolved';
    CREATE INDEX escalations_status ON escalations (tenant_id, status, created_at DESC);

    -- Événements temps réel : des IDENTIFIANTS seulement (jamais de contenu de message).
    CREATE TABLE events (
      id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      type            text NOT NULL,
      conversation_id uuid,
      escalation_id   uuid,
      created_at      timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX events_tenant_id ON events (tenant_id, id);

    CREATE FUNCTION notify_event() RETURNS trigger LANGUAGE plpgsql AS $fn$
    BEGIN
      PERFORM pg_notify('agent24_events', json_build_object(
        'id', NEW.id, 't', NEW.tenant_id, 'type', NEW.type,
        'c', NEW.conversation_id, 'e', NEW.escalation_id)::text);
      RETURN NEW;
    END
    $fn$;
    CREATE TRIGGER events_notify AFTER INSERT ON events FOR EACH ROW EXECUTE FUNCTION notify_event();

    -- Pause de l'agent (interrupteur d'urgence)
    ALTER TABLE tenants
      ADD COLUMN agent_paused boolean NOT NULL DEFAULT false,
      ADD COLUMN paused_at timestamptz,
      ADD COLUMN paused_by uuid REFERENCES users(id) ON DELETE SET NULL;

    -- File de jobs : types supplémentaires (notification d'escalade par email)
    ALTER TABLE jobs
      ALTER COLUMN message_id DROP NOT NULL,
      ADD COLUMN escalation_id uuid REFERENCES escalations(id) ON DELETE CASCADE,
      ADD CONSTRAINT jobs_kind_shape CHECK (
        (kind = 'reply' AND message_id IS NOT NULL) OR (kind = 'notify_escalation' AND escalation_id IS NOT NULL));
    """)

    for table in ("users", "escalations", "events"):
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
          USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY});
        """)

    op.execute(f"""
    GRANT SELECT ON login_index TO {APP_ROLE};
    GRANT SELECT, INSERT, UPDATE, DELETE ON sessions TO {APP_ROLE};
    GRANT SELECT ON users TO {APP_ROLE};
    GRANT UPDATE (last_login_at) ON users TO {APP_ROLE};
    GRANT SELECT, INSERT, UPDATE ON escalations TO {APP_ROLE};
    GRANT SELECT, INSERT ON events TO {APP_ROLE};
    GRANT UPDATE (agent_paused, paused_at, paused_by) ON tenants TO {APP_ROLE};
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_shape;
    DELETE FROM jobs WHERE message_id IS NULL;
    ALTER TABLE jobs DROP COLUMN IF EXISTS escalation_id, ALTER COLUMN message_id SET NOT NULL;
    ALTER TABLE tenants DROP COLUMN IF EXISTS paused_by, DROP COLUMN IF EXISTS paused_at, DROP COLUMN IF EXISTS agent_paused;
    DROP TABLE IF EXISTS events, escalations CASCADE;
    DROP FUNCTION IF EXISTS notify_event();
    ALTER TABLE contacts DROP COLUMN IF EXISTS profile_fetched_at;
    ALTER TABLE messages DROP COLUMN IF EXISTS sent_by_user_id, DROP COLUMN IF EXISTS author;
    ALTER TABLE conversations DROP COLUMN IF EXISTS team_read_at, DROP COLUMN IF EXISTS handoff_reason,
      DROP COLUMN IF EXISTS assigned_user_id;
    DROP TABLE IF EXISTS sessions, login_index, users CASCADE;
    DROP FUNCTION IF EXISTS sync_login_index();
    """)
