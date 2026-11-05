"""File de jobs (outbox), traces de l'agent, statut de livraison des messages sortants.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = "agent24_app"
TENANT_POLICY = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("""
    -- Messages sortants : état de livraison
    ALTER TABLE messages
      ADD COLUMN delivery_status text CHECK (delivery_status IN ('pending', 'sent', 'failed')),
      ADD COLUMN external_id text,
      ADD COLUMN sent_at timestamptz,
      ADD CONSTRAINT messages_tenant_id_id_key UNIQUE (tenant_id, id);

    -- File de traitement. Volontairement SANS RLS : le worker doit voir les jobs de tous les
    -- clients. Aucune donnée personnelle ici (uniquement des identifiants).
    -- Le job est inséré dans la MÊME transaction que le message (pattern « outbox »).
    CREATE TABLE jobs (
      id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      conversation_id uuid NOT NULL,
      message_id      uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
      kind            text NOT NULL DEFAULT 'reply',
      status          text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'processing', 'done', 'failed')),
      attempts        int  NOT NULL DEFAULT 0,
      max_attempts    int  NOT NULL DEFAULT 5,
      run_after       timestamptz NOT NULL DEFAULT now(),
      locked_at       timestamptz,
      last_error      text,
      created_at      timestamptz NOT NULL DEFAULT now(),
      finished_at     timestamptz
    );
    CREATE INDEX jobs_claim ON jobs (status, run_after, id);
    CREATE INDEX jobs_conversation ON jobs (conversation_id, status);

    CREATE TABLE agent_runs (
      id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      conversation_id    uuid NOT NULL,
      trigger_message_id uuid NOT NULL,
      model              text,
      outcome            text CHECK (outcome IN ('replied', 'escalated', 'failed')),
      steps              int NOT NULL DEFAULT 0,
      input_tokens       int NOT NULL DEFAULT 0,
      output_tokens      int NOT NULL DEFAULT 0,
      covered_until      timestamptz,   -- dernier message client pris en compte par cette exécution
      error              text,
      started_at         timestamptz NOT NULL DEFAULT now(),
      finished_at        timestamptz,
      UNIQUE (tenant_id, id),
      FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, id) ON DELETE CASCADE,
      FOREIGN KEY (tenant_id, trigger_message_id) REFERENCES messages (tenant_id, id) ON DELETE CASCADE
    );
    CREATE INDEX agent_runs_conversation ON agent_runs (tenant_id, conversation_id, covered_until);

    CREATE TABLE tool_calls (
      id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      run_id      uuid NOT NULL,
      tool_name   text NOT NULL,
      input       jsonb,
      output      jsonb,
      status      text NOT NULL CHECK (status IN ('ok', 'error')),
      error       text,
      duration_ms int,
      created_at  timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id) ON DELETE CASCADE
    );
    """)

    for table in ("agent_runs", "tool_calls"):
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
          USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY});
        """)

    op.execute(f"""
    GRANT SELECT, INSERT, UPDATE ON jobs, agent_runs, tool_calls TO {APP_ROLE};
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS tool_calls, agent_runs, jobs CASCADE;
    ALTER TABLE messages
      DROP CONSTRAINT IF EXISTS messages_tenant_id_id_key,
      DROP COLUMN IF EXISTS sent_at,
      DROP COLUMN IF EXISTS external_id,
      DROP COLUMN IF EXISTS delivery_status;
    """)
