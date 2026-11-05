"""Modèles ORM (miroir du schéma créé par la migration 0001)."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, Text, Uuid
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=sql_text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    channel_id: Mapped[UUID] = mapped_column(Uuid)
    channel_user_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=sql_text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    channel_id: Mapped[UUID] = mapped_column(Uuid)
    contact_id: Mapped[UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=sql_text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    conversation_id: Mapped[UUID] = mapped_column(Uuid)
    channel_id: Mapped[UUID] = mapped_column(Uuid)
    direction: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    media_ref: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql_text("now()"))
    delivery_status: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    author: Mapped[str | None] = mapped_column(Text)
    sent_by_user_id: Mapped[UUID | None] = mapped_column(Uuid)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=sql_text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    conversation_id: Mapped[UUID] = mapped_column(Uuid)
    trigger_message_id: Mapped[UUID] = mapped_column(Uuid)
    model: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    covered_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=sql_text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    run_id: Mapped[UUID] = mapped_column(Uuid)
    tool_name: Mapped[str] = mapped_column(Text)
    input: Mapped[dict | None] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
