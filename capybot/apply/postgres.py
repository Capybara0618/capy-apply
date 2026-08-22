"""PostgreSQL schema, connection pool, and Alembic lifecycle."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import Engine

DEFAULT_DATABASE_URL = "postgresql+psycopg://capybot:capybot@127.0.0.1:15432/capybot_apply"
DEFAULT_REDIS_URL = "redis://127.0.0.1:16379/0"


def apply_database_url() -> str:
    return os.getenv("CAPYBOT_APPLY_DATABASE_URL", DEFAULT_DATABASE_URL)


def apply_redis_url() -> str:
    return os.getenv("CAPYBOT_APPLY_REDIS_URL", DEFAULT_REDIS_URL)


metadata = MetaData()


def _json_text(name: str = "raw_payload", default: str = "{}") -> Column[str]:
    return Column(name, Text, nullable=False, server_default=default)


boss_conversations = Table(
    "boss_conversations",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE"), nullable=False
    ),
    Column("boss_uid", String),
    Column("contact_name", String),
    Column("contact_role", String),
    Column("company", String),
    Column("last_message_preview", Text),
    Column("last_message_at", String),
    _json_text(),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_boss_conversations_account_updated", "account_id", "updated_at"),
)

apply_accounts = Table(
    "apply_accounts",
    metadata,
    Column("id", String, primary_key=True),
    Column("platform", String, nullable=False, server_default="boss"),
    Column("account_uid", String),
    Column("display_name", String),
    Column("profile_dir", String),
    Column("source", String),
    _json_text(),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
    Column("last_import_at", String),
)

boss_messages = Table(
    "boss_messages",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE"), nullable=False
    ),
    Column(
        "conversation_id",
        String,
        ForeignKey("boss_conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("message_id", String, nullable=False),
    Column("from_me", Integer, nullable=False, server_default="0"),
    Column("from_me_confidence", Float, nullable=False, server_default="0.5"),
    Column("sender_uid", String),
    Column("sender_name", String),
    Column("sender_role", String),
    Column("text", Text),
    Column("message_type", String, nullable=False, server_default="unknown"),
    Column("is_human_message", Integer, nullable=False, server_default="1"),
    Column("attachment_meta", Text, nullable=False, server_default="{}"),
    Column("content_fingerprint", String),
    Column("first_seen_import_run_id", String),
    Column("sent_at", String),
    _json_text(),
    Column("created_at", String, nullable=False),
    UniqueConstraint("account_id", "message_id", name="uq_boss_messages_account_message"),
    Index("ix_boss_messages_conversation_sent", "conversation_id", "sent_at"),
)

boss_job_cards = Table(
    "boss_job_cards",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE"), nullable=False
    ),
    Column(
        "conversation_id",
        String,
        ForeignKey("boss_conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("platform_job_id", String),
    Column("title", String, nullable=False),
    Column("company", String),
    Column("salary", String),
    Column("city", String),
    Column("experience", String),
    Column("education", String),
    Column("boss_uid", String),
    Column("boss_name", String),
    _json_text(),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_boss_job_cards_conversation", "conversation_id"),
)

boss_job_snapshots = Table(
    "boss_job_snapshots",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE"), nullable=False
    ),
    Column(
        "opportunity_id", String, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    ),
    Column("conversation_id", String, ForeignKey("boss_conversations.id", ondelete="CASCADE")),
    Column("platform_job_id", String),
    Column("version", Integer, nullable=False),
    Column("source", String, nullable=False),
    Column("content_hash", String, nullable=False),
    Column("payload", Text, nullable=False, server_default="{}"),
    Column("captured_at", String, nullable=False),
    UniqueConstraint("opportunity_id", "version", name="uq_boss_job_snapshots_version"),
    Index("ix_boss_job_snapshots_opportunity_captured", "opportunity_id", "captured_at"),
)

research_sources = Table(
    "research_sources",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE"), nullable=False
    ),
    Column(
        "opportunity_id", String, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    ),
    Column("query", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("title", Text),
    Column("source_domain", String),
    Column("excerpt", Text),
    Column("research_type", String, nullable=False, server_default="legacy"),
    Column("source_tier", String, nullable=False, server_default="general"),
    Column("quality_score", Float, nullable=False, server_default="0.4"),
    Column("verified", Boolean, nullable=False, server_default="false"),
    Column("published_at", String),
    Column("metadata", Text, nullable=False, server_default="{}"),
    Column("last_checked_at", String),
    Column("content_hash", String, nullable=False),
    Column("retrieved_at", String, nullable=False),
    UniqueConstraint("opportunity_id", "content_hash", name="uq_research_sources_content"),
    Index("ix_research_sources_opportunity_retrieved", "opportunity_id", "retrieved_at"),
)

sync_checkpoints = Table(
    "sync_checkpoints",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE"), nullable=False
    ),
    Column("conversation_id", String, ForeignKey("boss_conversations.id", ondelete="CASCADE")),
    Column("source", String, nullable=False),
    Column("cursor", Text),
    Column("last_message_id", String),
    Column("snapshot_hash", String),
    Column("synced_at", String, nullable=False),
    UniqueConstraint("account_id", "conversation_id", "source", name="uq_sync_checkpoint_scope"),
)

contacts = Table(
    "contacts",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE"), nullable=False
    ),
    Column("platform", String, nullable=False),
    Column("platform_uid", String),
    Column("name", String),
    Column("role", String),
    Column("company", String),
    Column("summary", Text),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint(
        "account_id", "platform", "platform_uid", name="uq_contacts_account_platform_uid"
    ),
)

opportunities = Table(
    "opportunities",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE"), nullable=False
    ),
    Column("platform", String, nullable=False),
    Column("platform_job_id", String),
    Column("title", String, nullable=False),
    Column("company", String),
    Column("stage", String, nullable=False, server_default="discovered"),
    Column("pursuit_recommendation", String),
    Column("job_fit_score", Integer),
    Column("opportunity_priority_score", Integer),
    Column("fit_status", String),
    Column("fit_confidence", Float),
    Column("fit_updated_at", String),
    Column("fit_reasons", Text, nullable=False, server_default="[]"),
    Column("risk_flags", Text, nullable=False, server_default="[]"),
    Column("open_questions", Text, nullable=False, server_default="[]"),
    Column("summary", Text),
    Column("confidence", Float),
    Column("next_action", Text),
    Column("last_progress_at", String),
    Column("source_quality", String),
    Column("user_note", Text),
    Column("manual_override_at", String),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_opportunities_account_updated", "account_id", "updated_at"),
    Index("ix_opportunities_account_stage_updated", "account_id", "stage", "updated_at"),
    Index("ix_opportunities_account_priority", "account_id", "opportunity_priority_score"),
)

conversation_opportunities = Table(
    "conversation_opportunities",
    metadata,
    Column(
        "conversation_id",
        String,
        ForeignKey("boss_conversations.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "opportunity_id",
        String,
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_conversation_opportunities_opportunity", "opportunity_id"),
)

apply_events = Table(
    "apply_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("conversation_id", String, ForeignKey("boss_conversations.id", ondelete="CASCADE")),
    Column("opportunity_id", String, ForeignKey("opportunities.id", ondelete="CASCADE")),
    Column("event_type", String, nullable=False),
    Column("title", String, nullable=False),
    Column("detail", Text),
    Column("evidence_message_ids", Text, nullable=False, server_default="[]"),
    Column("created_at", String, nullable=False),
    Index("ix_apply_events_opportunity_created", "opportunity_id", "created_at"),
)

suggestions = Table(
    "suggestions",
    metadata,
    Column("id", String, primary_key=True),
    Column("conversation_id", String, ForeignKey("boss_conversations.id", ondelete="CASCADE")),
    Column("opportunity_id", String, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("status", String, nullable=False, server_default="suggested"),
    Column("due_at", String),
    Column("priority", String),
    Column("reason", Text),
    Column("severity", String),
    Column("evidence_refs", Text, nullable=False, server_default="[]"),
    Column("fingerprint", String, nullable=False),
    Column("payload", Text, nullable=False, server_default="{}"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_suggestions_opportunity_status", "opportunity_id", "status"),
    Index("ix_suggestions_kind_status_due", "kind", "status", "due_at"),
    Index(
        "uq_suggestions_opportunity_kind_fingerprint",
        "opportunity_id",
        "kind",
        "fingerprint",
        unique=True,
    ),
)

for table_name, key_name, value_name in [
    ("contact_summaries", "contact_id", "summary"),
    ("opportunity_summaries", "opportunity_id", "summary"),
]:
    Table(
        table_name,
        metadata,
        Column(
            key_name,
            String,
            ForeignKey(
                "contacts.id" if table_name == "contact_summaries" else "opportunities.id",
                ondelete="CASCADE",
            ),
            primary_key=True,
        ),
        Column(value_name, Text, nullable=False),
        Column("updated_at", String, nullable=False),
    )

opportunity_fit_results = Table(
    "opportunity_fit_results",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "opportunity_id",
        String,
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("status", String, nullable=False),
    Column("job_fit_score", Integer),
    Column("opportunity_priority_score", Integer),
    Column("confidence", Float),
    Column("dimensions", Text, nullable=False, server_default="[]"),
    Column("matched_evidence", Text, nullable=False, server_default="[]"),
    Column("missing_requirements", Text, nullable=False, server_default="[]"),
    Column("hard_filter_caps", Text, nullable=False, server_default="[]"),
    Column("raw_model_output", Text, nullable=False, server_default="{}"),
    Column("resume_version_hash", String),
    Column("job_context_hash", String),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_fit_results_opportunity_updated", "opportunity_id", "updated_at"),
)

candidate_profile = Table(
    "candidate_profile",
    metadata,
    Column(
        "account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE"), primary_key=True
    ),
    Column("id", Integer, primary_key=True),
    Column("resume_markdown", Text),
    Column("profile_summary", Text),
    Column("skill_tags", Text, nullable=False, server_default="[]"),
    Column("project_tags", Text, nullable=False, server_default="[]"),
    Column("agent_tags", Text, nullable=False, server_default="[]"),
    Column("weaknesses", Text, nullable=False, server_default="[]"),
    Column("updated_at", String, nullable=False),
)

job_preferences = Table(
    "job_preferences",
    metadata,
    Column(
        "account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE"), primary_key=True
    ),
    Column("id", Integer, primary_key=True),
    Column("target_roles", Text),
    Column("cities", Text),
    Column("salary", Text),
    Column("internship_time", Text),
    Column("excluded", Text),
    Column("updated_at", String, nullable=False),
)

agent_runs = Table(
    "agent_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE")),
    Column("target_type", String, nullable=False),
    Column("target_id", String, nullable=False),
    Column("conversation_id", String, ForeignKey("boss_conversations.id", ondelete="SET NULL")),
    Column("opportunity_id", String, ForeignKey("opportunities.id", ondelete="SET NULL")),
    Column("started_at", String, nullable=False),
    Column("finished_at", String),
    Column("status", String, nullable=False),
    Column("model_provider", String),
    Column("model_name", String),
    Column("input_summary", Text),
    Column("output_summary", Text),
    Column("confidence", Float),
    Column("error", Text),
    Column("engine", String),
    Column("planner_mode", String),
    Column("degraded_reason", Text),
    Column("tool_call_count", Integer),
    Column("boss_tool_call_count", Integer),
    Column("llm_call_count", Integer),
    Column("prompt_tokens", Integer),
    Column("completion_tokens", Integer),
    Column("duration_ms", Integer),
    Column("created_at", String, nullable=False),
    Index("ix_agent_runs_account_started", "account_id", "started_at"),
    Index("ix_agent_runs_opportunity_started", "opportunity_id", "started_at"),
)

agent_trace_steps = Table(
    "agent_trace_steps",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "run_id",
        String,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("step_index", Integer, nullable=False),
    Column("step_type", String, nullable=False),
    Column("title", String, nullable=False),
    Column("summary", Text),
    Column("input_ref", Text),
    Column("output_ref", Text),
    Column("metadata", Text, nullable=False, server_default="{}"),
    Column("created_at", String, nullable=False),
    UniqueConstraint("run_id", "step_index", name="uq_agent_trace_run_step"),
)

tool_observations = Table(
    "tool_observations",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
    Column("tool_call_id", String, nullable=False),
    Column("tool_name", String, nullable=False),
    Column("server_name", String, nullable=False),
    Column("status", String, nullable=False),
    Column("arguments_summary", Text, nullable=False, server_default="{}"),
    Column("result_summary", Text),
    Column("evidence_refs", Text, nullable=False, server_default="[]"),
    Column("duration_ms", Integer),
    Column("fact_count", Integer, nullable=False, server_default="0"),
    Column("novel_evidence_count", Integer, nullable=False, server_default="0"),
    Column("used_evidence_count", Integer, nullable=False, server_default="0"),
    Column("empty_result", Boolean, nullable=False, server_default="false"),
    Column("utility", String, nullable=False, server_default="unknown"),
    Column("created_at", String, nullable=False),
    UniqueConstraint("run_id", "tool_call_id", name="uq_tool_observations_call"),
    Index("ix_tool_observations_run_created", "run_id", "created_at"),
)

import_runs = Table(
    "import_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE")),
    Column("started_at", String, nullable=False),
    Column("finished_at", String),
    Column("report", Text, nullable=False, server_default="{}"),
)

import_run_items = Table(
    "import_run_items",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "import_run_id",
        String,
        ForeignKey("import_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("conversation_id", String, ForeignKey("boss_conversations.id", ondelete="CASCADE")),
    Column("opportunity_id", String, ForeignKey("opportunities.id", ondelete="SET NULL")),
    Column("new_message_ids", Text, nullable=False, server_default="[]"),
    Column("new_message_count", Integer, nullable=False, server_default="0"),
    Column("analysis_mode", String),
    Column("skipped_reason", Text),
    Column("before_stage", String),
    Column("after_stage", String),
    Column("created_at", String, nullable=False),
    Index("ix_import_items_run_mode", "import_run_id", "analysis_mode"),
)

apply_meta = Table(
    "apply_meta",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", String, nullable=False),
)

apply_jobs = Table(
    "apply_jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("account_id", String, ForeignKey("apply_accounts.id", ondelete="CASCADE")),
    Column("job_type", String, nullable=False),
    Column("status", String, nullable=False),
    Column("progress_current", Integer, nullable=False, server_default="0"),
    Column("progress_total", Integer, nullable=False, server_default="0"),
    Column("progress_percent", Integer, nullable=False, server_default="0"),
    Column("message", Text),
    Column("target_type", String),
    Column("target_id", String),
    Column("idempotency_key", String),
    Column("celery_task_id", String),
    Column("error", Text),
    Column("payload", Text, nullable=False, server_default="{}"),
    Column("created_at", String, nullable=False),
    Column("started_at", String),
    Column("finished_at", String),
    Column("updated_at", String, nullable=False),
    Index(
        "uq_apply_jobs_active_key",
        "idempotency_key",
        unique=True,
        postgresql_where=text("idempotency_key IS NOT NULL AND status IN ('queued', 'running')"),
    ),
    Index("ix_apply_jobs_account_created", "account_id", "created_at"),
    Index("ix_apply_jobs_status_updated", "status", "updated_at"),
)


_engine_lock = threading.Lock()
_engines: dict[str, Engine] = {}


def engine(url: str | None = None) -> Engine:
    """Return one connection-pooled Engine per database URL and process."""

    database_url = url or apply_database_url()
    with _engine_lock:
        cached = _engines.get(database_url)
        if cached is None:
            cached = create_engine(
                database_url,
                pool_pre_ping=True,
                future=True,
                connect_args={"connect_timeout": 2},
            )
            _engines[database_url] = cached
        return cached


_upgrade_lock = threading.Lock()
_upgraded_urls: set[str] = set()


def upgrade_database(url: str | None = None) -> None:
    """Upgrade the Apply schema once per process through Alembic."""

    database_url = url or apply_database_url()
    with _upgrade_lock:
        if database_url in _upgraded_urls:
            return
        from alembic import command
        from alembic.config import Config

        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parent / "alembic"))
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        _upgraded_urls.add(database_url)


def database_ready(url: str | None = None) -> tuple[bool, str | None]:
    try:
        eng = engine(url)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        tables = set(inspect(eng).get_table_names())
        if "apply_jobs" not in tables:
            return False, "PostgreSQL 可连接，但 Apply 表尚未初始化，请运行 capybot db upgrade。"
        return True, None
    except Exception as exc:
        return False, str(exc)


@contextmanager
def begin(url: str | None = None) -> Iterator[Any]:
    eng = engine(url)
    with eng.begin() as conn:
        yield conn
