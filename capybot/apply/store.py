"""PostgreSQL persistence for Capybot Apply."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .conversation_signals import ConversationSignals
from .models import PIPELINE_STAGES, utc_now_iso
from .sql_session import PostgresRow, PostgresSession

DERIVED_TABLES = [
    "agent_trace_steps",
    "agent_runs",
    "opportunity_fit_results",
    "opportunity_summaries",
    "contact_summaries",
    "suggestions",
    "apply_events",
    "conversation_opportunities",
    "opportunities",
]

TRACE_PRIVACY_VERSION = 4
DRAFT_SAFETY_VERSION = 1
TRACE_PRIVATE_KEYS = {
    "analysis_messages",
    "candidate_profile",
    "contact_summaries",
    "context",
    "full_prompt",
    "invalid_output",
    "messages",
    "new_messages",
    "opportunity",
    "prompt",
    "raw_messages",
    "raw_payload",
    "recent_messages",
    "resume_markdown",
    "resume_text",
    "existing_summary",
    "system_prompt",
    "user_prompt",
}
_maintenance_bootstrapped_urls: set[str] = set()


class ApplyStore:
    """PostgreSQL store for raw BOSS data and derived Apply agent records."""

    def __init__(
        self,
        *,
        account_id: str | None = None,
        database_url: str | None = None,
    ):
        self._account_id = str(account_id) if account_id else None
        self._database_url = database_url
        self._init()

    def connect(self) -> PostgresSession:
        return PostgresSession(self._database_url)

    def _init(self) -> None:
        from .postgres import apply_database_url, upgrade_database

        database_url = self._database_url or apply_database_url()
        upgrade_database(database_url)
        if database_url not in _maintenance_bootstrapped_urls:
            self.scrub_trace_metadata()
            self.supersede_unsafe_drafts()
            _maintenance_bootstrapped_urls.add(database_url)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value if value is not None else {}, ensure_ascii=False)

    @staticmethod
    def _loads(value: Any, default: Any = None) -> Any:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except Exception:
            return default

    @classmethod
    def _sanitize_trace_metadata(cls, value: Any) -> Any:
        """Remove prompts and raw source content from durable Agent traces."""

        if isinstance(value, dict):
            return {
                key: cls._sanitize_trace_metadata(item)
                for key, item in value.items()
                if str(key).lower() not in TRACE_PRIVATE_KEYS
            }
        if isinstance(value, list):
            return [cls._sanitize_trace_metadata(item) for item in value]
        return value

    @staticmethod
    def _sanitize_trace_summary(summary: str | None) -> str | None:
        if summary is None:
            return None
        text = re.sub(r"1[3-9]\d{9}", "[手机号]", summary)
        text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱]", text)
        text = re.sub(r"https?://\S+", "[URL]", text)
        text = re.sub(r"\d{17}[\dXx]", "[身份证]", text)
        return text

    def scrub_trace_metadata(self, *, force: bool = False) -> dict[str, Any]:
        marker = {"version": TRACE_PRIVACY_VERSION}
        if not force and self.get_meta("trace_privacy_version") == marker:
            return {"ran": False, "changed": 0, "reason": "already_scrubbed"}

        changed = 0
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, step_type, title, summary, metadata FROM agent_trace_steps"
            ).fetchall()
            for row in rows:
                original = self._loads(row["metadata"], {})
                sanitized = self._sanitize_trace_metadata(original)
                summary = self._sanitize_trace_summary(row["summary"])
                if sanitized == original and summary == row["summary"]:
                    continue
                db.execute(
                    "UPDATE agent_trace_steps SET summary=?, metadata=? WHERE id=?",
                    (summary, self._json(sanitized), row["id"]),
                )
                changed += 1
        self.set_meta("trace_privacy_version", marker)
        return {"ran": True, "changed": changed}

    def supersede_unsafe_drafts(self, *, force: bool = False) -> dict[str, Any]:
        """Retire old generated drafts that still contain fill-in placeholders."""

        marker = {"version": DRAFT_SAFETY_VERSION}
        if not force and self.get_meta("draft_safety_version") == marker:
            return {"ran": False, "changed": 0, "reason": "already_scrubbed"}
        patterns = (
            "%[您的%",
            "%【您的%",
            "%<您的%",
            "%[项目名称]%",
            "%【项目名称】%",
            "%[公司名称]%",
            "%【公司名称】%",
            "%[职位名称]%",
            "%【职位名称】%",
            "%[请填写%",
            "%【请填写%",
        )
        clauses = " OR ".join("content LIKE ?" for _ in patterns)
        with self.connect() as db:
            rows = db.execute(
                f"""
                UPDATE suggestions
                SET status='superseded', updated_at=?
                WHERE kind='draft' AND status='suggested' AND ({clauses})
                RETURNING id
                """,
                (utc_now_iso(), *patterns),
            ).fetchall()
        self.set_meta("draft_safety_version", marker)
        return {"ran": True, "changed": len(rows)}

    @staticmethod
    def _id(prefix: str, *parts: object) -> str:
        raw = "|".join(str(p or "") for p in parts)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}_{digest}"

    def clear(self) -> None:
        with self.connect() as db:
            for table in [
                *DERIVED_TABLES,
                "contacts",
                "boss_job_cards",
                "boss_messages",
                "boss_conversations",
                "apply_accounts",
            ]:
                db.execute(f"DELETE FROM {table}")

    def delete_account(self, account_id: str) -> bool:
        """Delete one isolated account; PostgreSQL cascades all owned records."""

        with self.connect() as db:
            row = db.execute(
                "DELETE FROM apply_accounts WHERE id=? RETURNING id",
                (account_id,),
            ).fetchone()
        if self._account_id == account_id:
            self._account_id = None
        return row is not None

    def clear_derived(self) -> None:
        with self.connect() as db:
            for table in DERIVED_TABLES:
                db.execute(f"DELETE FROM {table}")

    def clear_current_account_derived(self, *, preserve_agent_runs: bool = True) -> None:
        """Clear derived records for one account without touching other accounts."""

        account_id = self.current_account_id()
        if not account_id:
            return
        with self.connect() as db:
            if not preserve_agent_runs:
                db.execute(
                    "DELETE FROM agent_trace_steps WHERE run_id IN (SELECT id FROM agent_runs WHERE account_id=?)",
                    (account_id,),
                )
                db.execute("DELETE FROM agent_runs WHERE account_id=?", (account_id,))
            for table in (
                "opportunity_fit_results",
                "opportunity_summaries",
                "suggestions",
                "apply_events",
            ):
                db.execute(
                    f"DELETE FROM {table} WHERE opportunity_id IN (SELECT id FROM opportunities WHERE account_id=?)",
                    (account_id,),
                )
            db.execute(
                "DELETE FROM contact_summaries WHERE contact_id IN (SELECT id FROM contacts WHERE account_id=?)",
                (account_id,),
            )
            db.execute(
                "DELETE FROM conversation_opportunities WHERE opportunity_id IN (SELECT id FROM opportunities WHERE account_id=?)",
                (account_id,),
            )
            db.execute("DELETE FROM opportunities WHERE account_id=?", (account_id,))

    def upsert_conversation(self, row: dict[str, Any]) -> str:
        now = utc_now_iso()
        account_id = str(row.get("account_id") or (self.current_account() or {}).get("id") or "")
        if not account_id:
            raise ValueError("写入 BOSS 会话前必须确定账号")
        boss_uid = str(row.get("boss_uid") or row.get("id") or "")
        source_id = str(
            row.get("conversation_id") or row.get("id") or boss_uid or row.get("contact_name") or ""
        )
        with self.connect() as db:
            existing = db.execute(
                "SELECT id FROM boss_conversations WHERE account_id=? AND (id=? OR boss_uid=?) LIMIT 1",
                (account_id, source_id, boss_uid),
            ).fetchone()
            cid = str(existing["id"]) if existing else self._id("conv", account_id, source_id)
            db.execute(
                """
                INSERT INTO boss_conversations
                (id, account_id, boss_uid, contact_name, contact_role, company, last_message_preview,
                 last_message_at, raw_payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  boss_uid=excluded.boss_uid,
                  contact_name=COALESCE(excluded.contact_name, boss_conversations.contact_name),
                  contact_role=COALESCE(excluded.contact_role, boss_conversations.contact_role),
                  company=COALESCE(excluded.company, boss_conversations.company),
                  last_message_preview=COALESCE(excluded.last_message_preview, boss_conversations.last_message_preview),
                  last_message_at=COALESCE(excluded.last_message_at, boss_conversations.last_message_at),
                  raw_payload=excluded.raw_payload,
                  updated_at=excluded.updated_at
                """,
                (
                    cid,
                    account_id,
                    boss_uid,
                    row.get("contact_name"),
                    row.get("contact_role"),
                    row.get("company"),
                    row.get("last_message_preview"),
                    row.get("last_message_at"),
                    self._json(row.get("raw_payload", row)),
                    now,
                    now,
                ),
            )
        return cid

    def upsert_account(self, row: dict[str, Any] | None, *, imported: bool = False) -> str:
        row = row or {}
        now = utc_now_iso()
        account_uid = str(row.get("account_uid") or row.get("uid") or row.get("id") or "")
        profile_dir = str(row.get("profile_dir") or "")
        proposed_account_id = str(
            row.get("id") or self._id("boss_account", account_uid or profile_dir or "local")
        )
        display_name = row.get("display_name") or row.get("name") or "BOSS 本地账号"
        with self.connect() as db:
            existing = None
            if account_uid and not account_uid.startswith("profile:"):
                existing = db.execute(
                    "SELECT id FROM apply_accounts WHERE platform='boss' AND account_uid=? LIMIT 1",
                    (account_uid,),
                ).fetchone()
            if (
                existing is None
                and profile_dir
                and account_uid
                and not account_uid.startswith("profile:")
            ):
                existing = db.execute(
                    """
                    SELECT id FROM apply_accounts
                    WHERE platform='boss' AND profile_dir=? AND account_uid LIKE ?
                    ORDER BY COALESCE(last_import_at, last_seen_at) DESC
                    LIMIT 1
                    """,
                    (profile_dir, "profile:%"),
                ).fetchone()
            account_id = str(existing["id"]) if existing else proposed_account_id
            db.execute(
                """
                INSERT INTO apply_accounts
                (id, platform, account_uid, display_name, profile_dir, source, raw_payload,
                 first_seen_at, last_seen_at, last_import_at)
                VALUES (?, 'boss', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  account_uid=COALESCE(excluded.account_uid, apply_accounts.account_uid),
                  display_name=COALESCE(excluded.display_name, apply_accounts.display_name),
                  profile_dir=COALESCE(excluded.profile_dir, apply_accounts.profile_dir),
                  source=COALESCE(excluded.source, apply_accounts.source),
                  raw_payload=excluded.raw_payload,
                  last_seen_at=excluded.last_seen_at,
                  last_import_at=COALESCE(excluded.last_import_at, apply_accounts.last_import_at)
                """,
                (
                    account_id,
                    account_uid or None,
                    display_name,
                    profile_dir or None,
                    row.get("source") or "boss_profile",
                    self._json(row),
                    now,
                    now,
                    now if imported else None,
                ),
            )
        self._account_id = account_id
        return account_id

    def current_account(self) -> dict[str, Any] | None:
        with self.connect() as db:
            if self._account_id:
                row = db.execute(
                    "SELECT * FROM apply_accounts WHERE id=?", (self._account_id,)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM apply_accounts ORDER BY COALESCE(last_import_at, last_seen_at) DESC LIMIT 1"
                ).fetchone()
                if row:
                    self._account_id = str(row["id"])
        return dict(row) if row else None

    def current_account_id(self) -> str | None:
        account = self.current_account()
        return str(account["id"]) if account else None

    def bind_opportunity_account(self, opportunity_id: str) -> str | None:
        """Bind a subprocess store to the account that owns a known opportunity."""

        with self.connect() as db:
            row = db.execute(
                "SELECT account_id FROM opportunities WHERE id=?",
                (opportunity_id,),
            ).fetchone()
        if not row:
            return None
        self._account_id = str(row["account_id"])
        return self._account_id

    def upsert_contact_from_conversation(self, conversation_id: str) -> str | None:
        with self.connect() as db:
            conv = db.execute(
                "SELECT * FROM boss_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if conv is None:
                return None
            uid = conv["boss_uid"] or conversation_id
            account_id = conv["account_id"]
            contact_id = self._id("contact", account_id, "boss", uid)
            now = utc_now_iso()
            db.execute(
                """
                INSERT INTO contacts
                (id, account_id, platform, platform_uid, name, role, company, created_at, updated_at)
                VALUES (?, ?, 'boss', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, platform, platform_uid) DO UPDATE SET
                  name=COALESCE(excluded.name, contacts.name),
                  role=COALESCE(excluded.role, contacts.role),
                  company=COALESCE(excluded.company, contacts.company),
                  updated_at=excluded.updated_at
                """,
                (
                    contact_id,
                    account_id,
                    uid,
                    conv["contact_name"],
                    conv["contact_role"],
                    conv["company"],
                    now,
                    now,
                ),
            )
            stored = db.execute(
                "SELECT id FROM contacts WHERE account_id=? AND platform='boss' AND platform_uid=?",
                (account_id, uid),
            ).fetchone()
        return str(stored["id"]) if stored else contact_id

    def upsert_message(self, row: dict[str, Any]) -> tuple[str, bool]:
        now = utc_now_iso()
        msg_id = str(row.get("message_id") or row.get("mid") or "")
        if not msg_id:
            msg_id = f"fp:{row.get('content_fingerprint') or self._id('hashmsg', row.get('conversation_id'), row.get('from_me'), row.get('text'), row.get('sent_at'))}"
        pk = self._id("msg", row.get("conversation_id"), msg_id)
        with self.connect() as db:
            conv = db.execute(
                "SELECT account_id FROM boss_conversations WHERE id=?", (row["conversation_id"],)
            ).fetchone()
            if conv is None:
                raise ValueError(f"消息关联的会话不存在: {row['conversation_id']}")
            account_id = conv["account_id"]
            before = db.total_changes
            db.execute(
                """
                INSERT INTO boss_messages
                (id, account_id, conversation_id, message_id, from_me, sender_uid, sender_name, sender_role,
                  text, message_type, from_me_confidence, is_human_message, attachment_meta,
                  content_fingerprint, first_seen_import_run_id, sent_at, raw_payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, message_id) DO NOTHING
                """,
                (
                    pk,
                    account_id,
                    row["conversation_id"],
                    msg_id,
                    1 if row.get("from_me") else 0,
                    row.get("sender_uid"),
                    row.get("sender_name"),
                    row.get("sender_role"),
                    row.get("text"),
                    row.get("message_type") or "unknown",
                    float(row.get("from_me_confidence") or 0.5),
                    1 if row.get("is_human_message", True) else 0,
                    self._json(row.get("attachment_meta") or {}),
                    row.get("content_fingerprint"),
                    row.get("first_seen_import_run_id"),
                    row.get("sent_at"),
                    self._json(row.get("raw_payload", row)),
                    now,
                ),
            )
            inserted = db.total_changes > before
        return pk, inserted

    def upsert_job_card(self, conversation_id: str, row: dict[str, Any]) -> tuple[str, bool]:
        now = utc_now_iso()
        platform_job_id = str(row.get("platform_job_id") or row.get("job_id") or "") or None
        title = row.get("title") or "待补全岗位"
        with self.connect() as db:
            conv = db.execute(
                "SELECT account_id FROM boss_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if conv is None:
                raise ValueError(f"岗位卡关联的会话不存在: {conversation_id}")
            identity_row = (
                db.execute(
                    """
                    SELECT id FROM boss_job_cards
                    WHERE conversation_id=? AND platform_job_id=?
                    ORDER BY created_at LIMIT 1
                    """,
                    (conversation_id, platform_job_id),
                ).fetchone()
                if platform_job_id
                else None
            )
            pk = (
                str(identity_row["id"])
                if identity_row
                else self._id(
                    "jobcard",
                    conversation_id,
                    platform_job_id,
                    title,
                    row.get("company"),
                )
            )
            existing = db.execute(
                "SELECT 1 FROM boss_job_cards WHERE id=?",
                (pk,),
            ).fetchone()
            db.execute(
                """
                INSERT INTO boss_job_cards
                (id, account_id, conversation_id, platform_job_id, title, company, salary, city,
                  experience, education, boss_uid, boss_name, raw_payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  platform_job_id=COALESCE(excluded.platform_job_id, boss_job_cards.platform_job_id),
                  title=COALESCE(excluded.title, boss_job_cards.title),
                  company=COALESCE(excluded.company, boss_job_cards.company),
                  salary=COALESCE(excluded.salary, boss_job_cards.salary),
                  city=COALESCE(excluded.city, boss_job_cards.city),
                  experience=COALESCE(excluded.experience, boss_job_cards.experience),
                  education=COALESCE(excluded.education, boss_job_cards.education),
                  boss_uid=COALESCE(excluded.boss_uid, boss_job_cards.boss_uid),
                  boss_name=COALESCE(excluded.boss_name, boss_job_cards.boss_name),
                  raw_payload=excluded.raw_payload,
                  updated_at=excluded.updated_at
                """,
                (
                    pk,
                    conv["account_id"],
                    conversation_id,
                    platform_job_id,
                    title,
                    row.get("company"),
                    row.get("salary"),
                    row.get("city"),
                    row.get("experience"),
                    row.get("education"),
                    row.get("boss_uid"),
                    row.get("boss_name"),
                    self._json(row.get("raw_payload", row)),
                    now,
                    now,
                ),
            )
            inserted = existing is None
        return pk, inserted

    def ensure_opportunities_for_conversation(self, conversation_id: str) -> list[str]:
        now = utc_now_iso()
        out: list[str] = []
        with self.connect() as db:
            conv = db.execute(
                "SELECT * FROM boss_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if conv is None:
                return []
            if ConversationSignals.is_platform_assistant_conversation(
                contact_name=str(conv["contact_name"] or ""),
                preview=str(conv["last_message_preview"] or ""),
            ):
                return []
            account_id = conv["account_id"]
            cards = db.execute(
                "SELECT * FROM boss_job_cards WHERE conversation_id=?", (conversation_id,)
            ).fetchall()
            if not cards:
                cards = [
                    {
                        "platform_job_id": None,
                        "title": "待补全岗位",
                        "company": conv["company"] if conv else None,
                        "source_quality": "missing_job_card",
                    }
                ]
            for card in cards:
                is_row = isinstance(card, PostgresRow)
                platform_job_id = card["platform_job_id"] if is_row else card.get("platform_job_id")
                title = (card["title"] if is_row else card.get("title")) or "待补全岗位"
                company = card["company"] if is_row else card.get("company")
                source_quality = "job_card" if platform_job_id else "missing_job_card"
                oid = self._id(
                    "opp", account_id, "boss", platform_job_id or conversation_id, title, company
                )
                db.execute(
                    """
                    INSERT INTO opportunities
                    (id, account_id, platform, platform_job_id, title, company, source_quality, created_at, updated_at)
                    VALUES (?, ?, 'boss', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      platform_job_id=COALESCE(excluded.platform_job_id, opportunities.platform_job_id),
                      title=excluded.title,
                      company=COALESCE(excluded.company, opportunities.company),
                      source_quality=excluded.source_quality,
                      updated_at=excluded.updated_at
                    """,
                    (oid, account_id, platform_job_id, title, company, source_quality, now, now),
                )
                db.execute(
                    """
                    INSERT INTO conversation_opportunities (conversation_id, opportunity_id)
                    VALUES (?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (conversation_id, oid),
                )
                out.append(oid)
        return out

    def opportunity_ids_for_conversation(self, conversation_id: str) -> list[str]:
        account_id = self.current_account_id()
        with self.connect() as db:
            rows = db.execute(
                """SELECT co.opportunity_id FROM conversation_opportunities co
                JOIN opportunities o ON o.id=co.opportunity_id
                WHERE co.conversation_id=? AND o.account_id=?""",
                (conversation_id, account_id),
            ).fetchall()
        return [str(row["opportunity_id"]) for row in rows]

    def opportunity_ids_for_analysis(
        self, limit: int = 50, only_unanalyzed: bool = False
    ) -> list[str]:
        account_id = self.current_account_id()
        with self.connect() as db:
            where = "WHERE o.account_id=?"
            if only_unanalyzed:
                where += " AND o.id NOT IN (SELECT opportunity_id FROM agent_runs WHERE status IN ('ok', 'needs_review'))"
            rows = db.execute(
                f"""
                SELECT o.id, MAX(m.sent_at) AS last_at, MAX(m.created_at) AS imported_at
                FROM opportunities o
                LEFT JOIN conversation_opportunities co ON co.opportunity_id=o.id
                LEFT JOIN boss_messages m ON m.conversation_id=co.conversation_id
                {where}
                GROUP BY o.id
                ORDER BY COALESCE(MAX(m.sent_at), MAX(m.created_at), o.updated_at) DESC
                LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        return [r["id"] for r in rows]

    def opportunity_context(self, opportunity_id: str) -> dict[str, Any]:
        account_id = self.current_account_id()
        with self.connect() as db:
            opp = db.execute(
                "SELECT * FROM opportunities WHERE id=? AND account_id=?",
                (opportunity_id, account_id),
            ).fetchone()
            convs = db.execute(
                """
                SELECT c.* FROM boss_conversations c
                JOIN conversation_opportunities co ON co.conversation_id=c.id
                WHERE co.opportunity_id=?
                ORDER BY c.updated_at DESC
                """,
                (opportunity_id,),
            ).fetchall()
            conv_ids = [r["id"] for r in convs]
            messages: list[Any] = []
            jobs: list[Any] = []
            if conv_ids:
                placeholders = ",".join("?" for _ in conv_ids)
                messages = db.execute(
                    f"""
                    SELECT * FROM boss_messages
                    WHERE conversation_id IN ({placeholders})
                    ORDER BY COALESCE(sent_at, created_at), message_id
                    """,
                    conv_ids,
                ).fetchall()
                jobs = db.execute(
                    f"SELECT * FROM boss_job_cards WHERE conversation_id IN ({placeholders})",
                    conv_ids,
                ).fetchall()
            events = db.execute(
                "SELECT * FROM apply_events WHERE opportunity_id=? ORDER BY created_at",
                (opportunity_id,),
            ).fetchall()
            opportunity_summary = db.execute(
                "SELECT * FROM opportunity_summaries WHERE opportunity_id=?",
                (opportunity_id,),
            ).fetchone()
            contact_summaries = []
            for conv in convs:
                contact_id = self._id(
                    "contact", conv["account_id"], "boss", conv["boss_uid"] or conv["id"]
                )
                row = db.execute(
                    "SELECT * FROM contact_summaries WHERE contact_id=?", (contact_id,)
                ).fetchone()
                if row:
                    contact_summaries.append(dict(row))
            profile = db.execute(
                "SELECT * FROM candidate_profile WHERE account_id=? AND id=1", (account_id,)
            ).fetchone()
            prefs = db.execute(
                "SELECT * FROM job_preferences WHERE account_id=? AND id=1", (account_id,)
            ).fetchone()
            last_run = db.execute(
                "SELECT * FROM agent_runs WHERE opportunity_id=? ORDER BY started_at DESC LIMIT 1",
                (opportunity_id,),
            ).fetchone()
            fit_row = db.execute(
                "SELECT * FROM opportunity_fit_results WHERE opportunity_id=? ORDER BY updated_at DESC LIMIT 1",
                (opportunity_id,),
            ).fetchone()
            job_snapshots = db.execute(
                """
                SELECT * FROM boss_job_snapshots
                WHERE opportunity_id=? ORDER BY captured_at DESC LIMIT 10
                """,
                (opportunity_id,),
            ).fetchall()
            research_sources = db.execute(
                """
                SELECT * FROM research_sources
                WHERE opportunity_id=? ORDER BY retrieved_at DESC LIMIT 20
                """,
                (opportunity_id,),
            ).fetchall()
        fit_analysis = dict(fit_row) if fit_row else None
        if fit_analysis:
            for key in [
                "dimensions",
                "matched_evidence",
                "missing_requirements",
                "hard_filter_caps",
            ]:
                fit_analysis[key] = self._loads(fit_analysis.get(key), [])
            fit_analysis["raw_model_output"] = self._loads(fit_analysis.get("raw_model_output"), {})
        return {
            "opportunity": self._row_to_payload(opp) if opp else None,
            "conversations": [dict(r) for r in convs],
            "messages": [dict(r) for r in messages],
            "jobs": [dict(r) for r in jobs],
            "events": [dict(r) for r in events],
            "opportunity_summary": dict(opportunity_summary) if opportunity_summary else None,
            "contact_summaries": contact_summaries,
            "candidate_profile": self._profile_payload(profile) if profile else None,
            "job_preferences": dict(prefs) if prefs else None,
            "last_run": dict(last_run) if last_run else None,
            "fit_analysis": fit_analysis,
            "job_snapshots": [dict(row) for row in job_snapshots],
            "research_sources": [dict(row) for row in research_sources],
        }

    def save_opportunity_analysis(
        self,
        opportunity_id: str,
        result: dict[str, Any],
        *,
        conversation_id: str | None = None,
    ) -> None:
        now = utc_now_iso()
        proposed_stage = result.get("pipeline_stage") or result.get("stage") or "communicating"
        commit_policy = result.get("_commit") if isinstance(result.get("_commit"), dict) else {}
        proposed_risk_flags = result.get("risk_flags", [])
        summary_update = result.get("summary_update") or {}
        proposed_summary = (
            summary_update.get("opportunity_summary")
            or result.get("summary")
            or result.get("stage_reason")
        )
        evidence = result.get("evidence_message_ids") or self._collect_evidence(result)
        confidence = result.get("confidence")
        proposed_next_action = result.get("next_action") or self._default_next_action(
            str(proposed_stage)
        )
        source_quality = result.get("source_quality")
        with self.connect() as db:
            existing = db.execute(
                """
                SELECT stage, pursuit_recommendation, summary, next_action,
                       risk_flags, open_questions, last_progress_at
                FROM opportunities WHERE id=?
                """,
                (opportunity_id,),
            ).fetchone()
            if existing is None:
                raise ValueError(f"机会不存在: {opportunity_id}")
            commit_stage = bool(commit_policy.get("stage", True))
            commit_summary = bool(commit_policy.get("summary", True))
            commit_next_action = bool(commit_policy.get("next_action", True))
            commit_events = bool(commit_policy.get("events", True))
            commit_risk_flags = bool(commit_policy.get("risk_flags", True))
            stage = proposed_stage if commit_stage else existing["stage"]
            stage_changed = commit_stage and stage != existing["stage"]
            summary = proposed_summary if commit_summary else existing["summary"]
            next_action = proposed_next_action if commit_next_action else existing["next_action"]
            risk_flags = (
                proposed_risk_flags
                if commit_risk_flags
                else self._loads(existing["risk_flags"], [])
            )
            pursuit_recommendation = (
                result.get("pursuit_recommendation") or existing["pursuit_recommendation"]
            )
            open_questions = result.get("open_questions")
            if not isinstance(open_questions, list):
                open_questions = self._loads(existing["open_questions"], [])
            has_progress = stage_changed or bool(result.get("events"))
            last_progress_at = now if has_progress else existing["last_progress_at"]
            conv_id = (
                conversation_id
                or db.execute(
                    "SELECT conversation_id FROM conversation_opportunities WHERE opportunity_id=? LIMIT 1",
                    (opportunity_id,),
                ).fetchone()
            )
            if isinstance(conv_id, PostgresRow):
                conv_id = conv_id["conversation_id"]
            db.execute(
                """
                UPDATE opportunities SET stage=?, pursuit_recommendation=?,
                  risk_flags=?, open_questions=?,
                  summary=?, confidence=?, next_action=?, source_quality=COALESCE(?, source_quality),
                  last_progress_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    stage,
                    pursuit_recommendation,
                    self._json(risk_flags),
                    self._json(open_questions),
                    summary,
                    confidence,
                    next_action,
                    source_quality,
                    last_progress_at,
                    now,
                    opportunity_id,
                ),
            )
            db.execute(
                """
                INSERT INTO opportunity_summaries (opportunity_id, summary, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(opportunity_id) DO UPDATE SET
                  summary=excluded.summary,
                  updated_at=excluded.updated_at
                """,
                (opportunity_id, summary or "", now),
            )
            if conv_id:
                conv = db.execute(
                    "SELECT * FROM boss_conversations WHERE id=?", (conv_id,)
                ).fetchone()
                if conv:
                    contact_id = self._id(
                        "contact",
                        conv["account_id"],
                        "boss",
                        conv["boss_uid"] or conv["id"],
                    )
                    db.execute(
                        """
                        INSERT INTO contacts
                        (id, account_id, platform, platform_uid, name, role, company, created_at, updated_at)
                        VALUES (?, ?, 'boss', ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(account_id, platform, platform_uid) DO UPDATE SET
                          name=COALESCE(excluded.name, contacts.name),
                          role=COALESCE(excluded.role, contacts.role),
                          company=COALESCE(excluded.company, contacts.company),
                          updated_at=excluded.updated_at
                        """,
                        (
                            contact_id,
                            conv["account_id"],
                            conv["boss_uid"] or conv["id"],
                            conv["contact_name"],
                            conv["contact_role"],
                            conv["company"],
                            now,
                            now,
                        ),
                    )
                    stored_contact = db.execute(
                        "SELECT id FROM contacts WHERE account_id=? AND platform='boss' AND platform_uid=?",
                        (conv["account_id"], conv["boss_uid"] or conv["id"]),
                    ).fetchone()
                    contact_id = str(stored_contact["id"]) if stored_contact else contact_id
                    contact_summary = (
                        summary_update.get("contact_summary")
                        or f"{conv['contact_name'] or 'BOSS 联系人'}：{summary or '暂无摘要'}"
                    )
                    db.execute(
                        """
                        INSERT INTO contact_summaries (contact_id, summary, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(contact_id) DO UPDATE SET
                          summary=excluded.summary,
                          updated_at=excluded.updated_at
                        """,
                        (contact_id, contact_summary, now),
                    )
            events = list(result.get("events", [])) if commit_events else []
            if (
                stage_changed
                and evidence
                and not any(event.get("event_type") == "stage_changed" for event in events)
            ):
                events.append(
                    {
                        "event_type": "stage_changed",
                        "title": "机会阶段更新",
                        "detail": f"{existing['stage']} -> {stage}",
                        "evidence_message_ids": evidence,
                    }
                )
            for event in events:
                event_evidence = event.get("evidence_message_ids") or evidence
                db.execute(
                    """
                    INSERT INTO apply_events
                    (id, conversation_id, opportunity_id, event_type, title, detail,
                     evidence_message_ids, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      detail=excluded.detail,
                      evidence_message_ids=excluded.evidence_message_ids
                    """,
                    (
                        self._id(
                            "event",
                            opportunity_id,
                            event.get("event_type"),
                            event.get("title"),
                            ",".join(event_evidence),
                        ),
                        conv_id,
                        opportunity_id,
                        event.get("event_type", "stage_changed"),
                        event.get("title", "进展更新"),
                        event.get("detail"),
                        self._json(event_evidence),
                        now,
                    ),
                )

            tasks = self._actionable_tasks(
                result.get("tasks") or [],
                proposed_stage,
                source_quality,
            )
            active: dict[str, set[str]] = {"task": set(), "draft": set(), "risk": set()}
            for task in tasks:
                task_evidence = task.get("evidence_message_ids") or evidence
                fingerprint = self._upsert_suggestion(
                    db,
                    opportunity_id=opportunity_id,
                    conversation_id=conv_id,
                    kind="task",
                    content=task.get("title") or "跟进任务",
                    evidence_refs=task_evidence,
                    due_at=task.get("due_at"),
                    priority=task.get("priority") or "medium",
                    reason=task.get("reason"),
                    payload=task,
                    now=now,
                )
                active["task"].add(fingerprint)

            draft = result.get("reply_draft")
            if isinstance(draft, str):
                draft = {
                    "content": draft,
                    "reason": "Agent 生成的回复草稿",
                    "evidence_message_ids": evidence,
                }
            if (
                isinstance(draft, dict)
                and draft.get("content")
                and self._draft_allowed(proposed_stage, source_quality)
            ):
                draft_evidence = draft.get("evidence_message_ids") or evidence
                fingerprint = self._upsert_suggestion(
                    db,
                    opportunity_id=opportunity_id,
                    conversation_id=conv_id,
                    kind="draft",
                    content=draft["content"],
                    evidence_refs=draft_evidence,
                    reason=draft.get("reason"),
                    payload=draft,
                    now=now,
                )
                active["draft"].add(fingerprint)

            for risk in proposed_risk_flags if commit_risk_flags else []:
                risk_evidence = risk.get("evidence_message_ids") or evidence
                fingerprint = self._upsert_suggestion(
                    db,
                    opportunity_id=opportunity_id,
                    conversation_id=conv_id,
                    kind="risk",
                    content=risk.get("reason") or "岗位存在待确认风险",
                    evidence_refs=risk_evidence,
                    severity=risk.get("severity") or "low",
                    payload=risk,
                    now=now,
                )
                active["risk"].add(fingerprint)

            for kind, fingerprints in active.items():
                if fingerprints:
                    placeholders = ",".join("?" for _ in fingerprints)
                    db.execute(
                        f"""
                        UPDATE suggestions SET status='superseded', updated_at=?
                        WHERE opportunity_id=? AND kind=? AND status='suggested'
                          AND fingerprint NOT IN ({placeholders})
                        """,
                        (now, opportunity_id, kind, *sorted(fingerprints)),
                    )
                else:
                    db.execute(
                        """
                        UPDATE suggestions SET status='superseded', updated_at=?
                        WHERE opportunity_id=? AND kind=? AND status='suggested'
                        """,
                        (now, opportunity_id, kind),
                    )

            stage_changed = bool(result.get("stage_changed", proposed_stage != existing["stage"]))
            if (
                stage_changed
                and proposed_stage in PIPELINE_STAGES
                and not commit_stage
                and evidence
            ):
                self._upsert_suggestion(
                    db,
                    opportunity_id=opportunity_id,
                    conversation_id=conv_id,
                    kind="stage",
                    content=f"阶段建议：{self.stage_label(proposed_stage)}",
                    evidence_refs=evidence,
                    reason=result.get("stage_reason"),
                    payload={
                        "stage": proposed_stage,
                        "stage_label": self.stage_label(proposed_stage),
                    },
                    now=now,
                )
            elif commit_stage:
                db.execute(
                    """
                    UPDATE suggestions SET status='superseded', updated_at=?
                    WHERE opportunity_id=? AND kind='stage' AND status='suggested'
                    """,
                    (now, opportunity_id),
                )

    def _upsert_suggestion(
        self,
        db: PostgresSession,
        *,
        opportunity_id: str,
        conversation_id: str | None,
        kind: str,
        content: str,
        evidence_refs: list[str],
        now: str,
        due_at: str | None = None,
        priority: str | None = None,
        reason: str | None = None,
        severity: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        normalized = " ".join(content.lower().split())
        fingerprint = hashlib.sha256(
            self._json(
                {
                    "kind": kind,
                    "content": normalized,
                    "evidence": sorted(evidence_refs),
                }
            ).encode("utf-8")
        ).hexdigest()
        db.execute(
            """
            INSERT INTO suggestions
              (id, conversation_id, opportunity_id, kind, content, status, due_at,
               priority, reason, severity, evidence_refs, fingerprint, payload,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'suggested', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(opportunity_id, kind, fingerprint) DO UPDATE SET
              content=excluded.content,
              due_at=COALESCE(excluded.due_at, suggestions.due_at),
              priority=COALESCE(excluded.priority, suggestions.priority),
              reason=COALESCE(excluded.reason, suggestions.reason),
              severity=COALESCE(excluded.severity, suggestions.severity),
              evidence_refs=excluded.evidence_refs,
              payload=excluded.payload,
              updated_at=excluded.updated_at
            """,
            (
                self._id("suggestion", opportunity_id, kind, fingerprint),
                conversation_id,
                opportunity_id,
                kind,
                content,
                due_at,
                priority,
                reason,
                severity,
                self._json(evidence_refs),
                fingerprint,
                self._json(payload or {}),
                now,
                now,
            ),
        )
        return fingerprint

    @staticmethod
    def _actionable_tasks(
        tasks: list[dict[str, Any]],
        stage: str,
        source_quality: str | None,
    ) -> list[dict[str, Any]]:
        if source_quality in {
            "cold_outreach_no_reply",
            "cold_outreach_vip_no_reply",
            "system_only",
        }:
            return []
        if stage in {"need_my_action", "interviewing"}:
            return tasks
        if stage != "waiting_feedback":
            return []
        return [
            task
            for task in tasks
            if task.get("due_at")
            or any(
                marker in f"{task.get('title') or ''} {task.get('reason') or ''}".lower()
                for marker in ("跟进", "反馈", "回复", "等待", "48 小时", "48小时")
            )
        ]

    @staticmethod
    def _draft_allowed(stage: str, source_quality: str | None) -> bool:
        return stage in {"need_my_action", "interviewing"} and source_quality not in {
            "cold_outreach_no_reply",
            "cold_outreach_vip_no_reply",
            "system_only",
        }

    @staticmethod
    def _default_next_action(stage: str) -> str:
        return {
            "need_my_action": "查看 HR 最新消息并完成回复或材料补充。",
            "waiting_feedback": "等待 HR 反馈；超过 48 小时仍无回应时再考虑跟进。",
            "interviewing": "确认面试时间并准备岗位相关项目说明。",
            "communicating": "继续沟通并补全岗位职责、地点和实习要求。",
            "discovered": "等待进一步沟通或补全岗位信息。",
            "closed": "机会已结束，无需继续跟进。",
        }.get(stage, "查看最新证据后决定下一步。")

    def save_fit_analysis(self, opportunity_id: str, result: dict[str, Any]) -> None:
        now = utc_now_iso()
        status = str(result.get("status") or "failed")
        job_fit_score = result.get("job_fit_score")
        priority_score = result.get("opportunity_priority_score")
        confidence = result.get("confidence")
        dimensions = result.get("dimensions") or []
        matched = result.get("matched_evidence") or []
        missing = result.get("missing_requirements") or []
        caps = result.get("hard_filter_caps") or []
        raw = result.get("raw_model_output") or result
        resume_hash = result.get("resume_version_hash")
        job_hash = result.get("job_context_hash")
        row_id = self._id("fit", opportunity_id, resume_hash, job_hash)
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO opportunity_fit_results
                (id, opportunity_id, status, job_fit_score, opportunity_priority_score, confidence,
                 dimensions, matched_evidence, missing_requirements, hard_filter_caps, raw_model_output,
                 resume_version_hash, job_context_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  status=excluded.status,
                  job_fit_score=excluded.job_fit_score,
                  opportunity_priority_score=excluded.opportunity_priority_score,
                  confidence=excluded.confidence,
                  dimensions=excluded.dimensions,
                  matched_evidence=excluded.matched_evidence,
                  missing_requirements=excluded.missing_requirements,
                  hard_filter_caps=excluded.hard_filter_caps,
                  raw_model_output=excluded.raw_model_output,
                  updated_at=excluded.updated_at
                """,
                (
                    row_id,
                    opportunity_id,
                    status,
                    int(job_fit_score) if job_fit_score is not None else None,
                    int(priority_score) if priority_score is not None else None,
                    float(confidence) if confidence is not None else None,
                    self._json(dimensions),
                    self._json(matched),
                    self._json(missing),
                    self._json(caps),
                    self._json(raw),
                    resume_hash,
                    job_hash,
                    now,
                    now,
                ),
            )
            db.execute(
                """
                UPDATE opportunities
                SET job_fit_score=?, opportunity_priority_score=?, fit_status=?,
                    fit_confidence=?, fit_reasons=?, fit_updated_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    int(job_fit_score) if job_fit_score is not None else None,
                    int(priority_score) if priority_score is not None else None,
                    status,
                    float(confidence) if confidence is not None else None,
                    self._json(dimensions),
                    now,
                    now,
                    opportunity_id,
                ),
            )

    def has_profile_inputs(self) -> bool:
        payload = self.profile_payload()
        profile = payload.get("profile") or {}
        preferences = payload.get("preferences") or {}
        resume = str(profile.get("resume_markdown") or "").strip()
        pref_text = " ".join(
            str(preferences.get(key) or "")
            for key in ["target_roles", "cities", "salary", "internship_time", "excluded"]
        )
        return bool(resume and pref_text.strip())

    def save_import_run_item(self, import_run_id: str, item: dict[str, Any]) -> None:
        now = utc_now_iso()
        item_id = self._id(
            "importitem",
            import_run_id,
            item.get("conversation_id"),
            item.get("opportunity_id"),
            item.get("analysis_mode"),
        )
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO import_run_items
                (id, import_run_id, conversation_id, opportunity_id, new_message_ids,
                 new_message_count, analysis_mode, skipped_reason, before_stage, after_stage, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  new_message_ids=excluded.new_message_ids,
                  new_message_count=excluded.new_message_count,
                  analysis_mode=excluded.analysis_mode,
                  skipped_reason=excluded.skipped_reason,
                  before_stage=excluded.before_stage,
                  after_stage=excluded.after_stage
                """,
                (
                    item_id,
                    import_run_id,
                    item.get("conversation_id"),
                    item.get("opportunity_id"),
                    self._json(item.get("new_message_ids") or []),
                    int(item.get("new_message_count") or 0),
                    item.get("analysis_mode") or "skipped",
                    item.get("skipped_reason"),
                    item.get("before_stage"),
                    item.get("after_stage"),
                    now,
                ),
            )

    def import_run_items(
        self, import_run_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.connect() as db:
            if import_run_id:
                rows = db.execute(
                    "SELECT * FROM import_run_items WHERE import_run_id=? ORDER BY created_at DESC LIMIT ?",
                    (import_run_id, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM import_run_items ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        out = [dict(row) for row in rows]
        for row in out:
            row["new_message_ids"] = self._loads(row.get("new_message_ids"), [])
        return out

    def latest_import_delta_panel(self) -> dict[str, Any] | None:
        account_id = self.current_account_id()
        with self.connect() as db:
            latest = db.execute(
                "SELECT id, report FROM import_runs WHERE account_id=? ORDER BY started_at DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            if latest is None:
                return None
            items = db.execute(
                "SELECT * FROM import_run_items WHERE import_run_id=? ORDER BY created_at DESC LIMIT 80",
                (latest["id"],),
            ).fetchall()
        report = self._loads(latest["report"], {})
        rows = [dict(row) for row in items]
        for row in rows:
            row["new_message_ids"] = self._loads(row.get("new_message_ids"), [])
        return {
            "import_run_id": latest["id"],
            "summary": {
                "new_messages": report.get("new_messages", 0),
                "changed_conversations": report.get("changed_conversations", 0),
                "skipped_conversations": report.get("skipped_conversations", 0),
                "analyzed_opportunities": report.get("analyzed_opportunities", 0),
                "queued_opportunities": report.get("queued_opportunities", 0),
            },
            "items": rows,
        }

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT value FROM apply_meta WHERE key=?", (key,)).fetchone()
        return self._loads(row["value"], default) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        now = utc_now_iso()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO apply_meta (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, self._json(value), now),
            )

    def overview(self) -> dict[str, Any]:
        from .agent_runs import AgentRunRepository

        account_id = self.current_account_id()
        with self.connect() as db:
            latest_import = db.execute(
                "SELECT * FROM import_runs WHERE account_id=? ORDER BY started_at DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            opportunities = [
                self._opportunity_list_payload(r)
                for r in db.execute(
                    "SELECT * FROM opportunities WHERE account_id=? ORDER BY updated_at DESC LIMIT 200",
                    (account_id,),
                ).fetchall()
            ]
            tasks = [
                dict(r)
                for r in db.execute(
                    """SELECT s.*, s.content AS title, s.evidence_refs AS evidence_message_ids
                FROM suggestions s JOIN opportunities o ON o.id=s.opportunity_id
                WHERE o.account_id=? AND s.kind='task'
                  AND s.status IN ('accepted', 'deferred')
                ORDER BY s.due_at IS NULL, s.due_at LIMIT 200""",
                    (account_id,),
                ).fetchall()
            ]
            suggestions = [
                dict(r)
                for r in db.execute(
                    """SELECT s.*, s.content AS title, s.evidence_refs AS evidence_message_ids
                FROM suggestions s JOIN opportunities o ON o.id=s.opportunity_id
                WHERE o.account_id=? AND s.status='suggested'
                ORDER BY s.created_at DESC LIMIT 50""",
                    (account_id,),
                ).fetchall()
            ]
            runs = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM agent_runs WHERE account_id=? ORDER BY started_at DESC LIMIT 8",
                    (account_id,),
                ).fetchall()
            ]
        profile_ready = self.has_profile_inputs()
        stage_suggestions = self._stage_suggestion_map(suggestions)
        for opportunity in opportunities:
            opportunity["stage_suggestion"] = stage_suggestions.get(str(opportunity.get("id")))
        effective_stages = {
            str(item.get("id")): stage_suggestions.get(str(item.get("id"))) or item.get("stage")
            for item in opportunities
        }
        scored = [
            o
            for o in opportunities
            if o.get("fit_status") == "ok" and o.get("job_fit_score") is not None
        ]
        priority = [o for o in opportunities if o.get("opportunity_priority_score") is not None]
        return {
            "metrics": {
                "opportunities": len(opportunities),
                "need_my_action": sum(
                    1 for stage in effective_stages.values() if stage == "need_my_action"
                ),
                "waiting_feedback": sum(
                    1 for stage in effective_stages.values() if stage == "waiting_feedback"
                ),
                "interviewing": sum(
                    1 for stage in effective_stages.values() if stage == "interviewing"
                ),
                "pending_reviews": len(suggestions),
                "active_tasks": len(tasks),
                "scored_opportunities": len(scored),
                "fit_pending": sum(
                    1 for o in opportunities if o.get("fit_status") in {None, "stale", "no_profile"}
                ),
            },
            "action_items": self._rank_action_items(opportunities, tasks, suggestions),
            "stage_changes": [o for o in opportunities if o.get("next_action")][:12],
            "risk_opportunities": [o for o in opportunities if o.get("risk_flags")][:12],
            "profile_ready": profile_ready,
            "top_job_fits": sorted(scored, key=lambda x: x.get("job_fit_score") or 0, reverse=True)[
                :12
            ]
            if profile_ready
            else [],
            "high_priority_opportunities": sorted(
                priority, key=lambda x: x.get("opportunity_priority_score") or 0, reverse=True
            )[:12]
            if profile_ready
            else [],
            "latest_import": dict(latest_import) if latest_import else None,
            "recent_delta_panel": self.latest_import_delta_panel(),
            "agent_runs": runs,
            "agent_metrics": AgentRunRepository(self).metrics(),
            "current_account": self.current_account(),
        }

    def opportunities(self) -> list[dict[str, Any]]:
        account_id = self.current_account_id()
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT o.*, COUNT(t.id) AS task_count
                FROM opportunities o
                LEFT JOIN suggestions t ON t.opportunity_id=o.id
                  AND t.kind='task' AND t.status IN ('suggested', 'accepted')
                WHERE o.account_id=?
                GROUP BY o.id
                ORDER BY
                  CASE o.stage
                    WHEN 'need_my_action' THEN 0
                    WHEN 'interviewing' THEN 1
                    WHEN 'waiting_feedback' THEN 2
                    WHEN 'communicating' THEN 3
                    WHEN 'discovered' THEN 4
                    ELSE 5
                  END,
                  o.opportunity_priority_score DESC NULLS LAST,
                  o.job_fit_score DESC NULLS LAST,
                  o.updated_at DESC
                """,
                (account_id,),
            ).fetchall()
            stage_rows = db.execute(
                """
                SELECT s.opportunity_id, s.payload
                FROM suggestions s
                JOIN opportunities o ON o.id=s.opportunity_id
                WHERE o.account_id=? AND s.kind='stage' AND s.status='suggested'
                ORDER BY s.created_at DESC
                """,
                (account_id,),
            ).fetchall()
        stage_suggestions = self._stage_suggestion_map(stage_rows)
        opportunities = [self._opportunity_list_payload(r) for r in rows]
        for opportunity in opportunities:
            opportunity["stage_suggestion"] = stage_suggestions.get(str(opportunity.get("id")))
        return opportunities

    def opportunity_detail(self, opportunity_id: str) -> dict[str, Any] | None:
        ctx = self.opportunity_context(opportunity_id)
        if not ctx["opportunity"]:
            return None
        with self.connect() as db:
            tasks = [
                dict(r)
                for r in db.execute(
                    """SELECT s.*, s.content AS title, s.evidence_refs AS evidence_message_ids
                    FROM suggestions s WHERE opportunity_id=? AND kind='task'
                    ORDER BY due_at IS NULL, due_at""",
                    (opportunity_id,),
                ).fetchall()
            ]
            drafts = [
                dict(r)
                for r in db.execute(
                    """SELECT s.*, s.evidence_refs AS evidence_message_ids
                    FROM suggestions s WHERE opportunity_id=? AND kind='draft'
                    ORDER BY updated_at DESC""",
                    (opportunity_id,),
                ).fetchall()
            ]
            suggestions = [
                dict(r)
                for r in db.execute(
                    """SELECT s.*, s.content AS title, s.evidence_refs AS evidence_message_ids
                    FROM suggestions s WHERE opportunity_id=? AND status='suggested'
                    ORDER BY created_at DESC""",
                    (opportunity_id,),
                ).fetchall()
            ]
            runs = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM agent_runs WHERE opportunity_id=? ORDER BY started_at DESC LIMIT 5",
                    (opportunity_id,),
                ).fetchall()
            ]
        ctx["opportunity"]["stage_suggestion"] = self._stage_suggestion_map(suggestions).get(
            opportunity_id
        )
        return {
            **ctx,
            "tasks": tasks,
            "drafts": drafts,
            "suggestions": suggestions,
            "agent_runs": runs,
        }

    def tasks_payload(self) -> dict[str, Any]:
        account_id = self.current_account_id()
        with self.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    """
                SELECT s.*, s.content AS title, s.evidence_refs AS evidence_message_ids,
                       o.title AS opportunity_title, o.company AS opportunity_company,
                       o.stage AS opportunity_stage
                FROM suggestions s
                LEFT JOIN opportunities o ON o.id=s.opportunity_id
                WHERE o.account_id=? AND s.kind='task'
                  AND s.status IN ('accepted', 'deferred')
                ORDER BY s.due_at IS NULL, s.due_at, s.updated_at DESC
                """,
                    (account_id,),
                ).fetchall()
            ]
            suggestions = [
                dict(r)
                for r in db.execute(
                    """SELECT s.*, s.content AS title, s.evidence_refs AS evidence_message_ids,
                          o.title AS opportunity_title, o.company AS opportunity_company,
                          o.stage AS opportunity_stage
                FROM suggestions s JOIN opportunities o ON o.id=s.opportunity_id
                WHERE o.account_id=? AND s.status='suggested'
                ORDER BY s.created_at DESC LIMIT 100""",
                    (account_id,),
                ).fetchall()
            ]
        return {"tasks": rows, "suggestions": suggestions}

    def message_evidence(self, message_ids: list[str]) -> dict[str, Any]:
        """Resolve BOSS message IDs through the canonical evidence ledger."""

        return self.resolve_evidence_refs(
            [value if ":" in value else f"boss_message:{value}" for value in message_ids if value]
        )

    def resolve_evidence_refs(self, evidence_refs: list[str]) -> dict[str, Any]:
        """Resolve canonical evidence references without exposing unrelated account data."""

        refs = list(dict.fromkeys(str(value).strip() for value in evidence_refs if value))
        buckets: dict[str, list[str]] = {
            "boss_message": [],
            "boss_job_snapshot": [],
            "web_source": [],
            "candidate_profile": [],
        }
        unknown: list[str] = []
        for ref in refs:
            if ":" not in ref:
                buckets["boss_message"].append(ref)
                continue
            namespace, value = ref.split(":", 1)
            if namespace in buckets and value:
                buckets[namespace].append(value)
            else:
                unknown.append(ref)

        account_id = self.current_account_id()
        payload: dict[str, Any] = {
            "messages": [],
            "job_snapshots": [],
            "web_sources": [],
            "candidate_profile": None,
            "missing_refs": unknown,
        }
        if not account_id:
            payload["missing_refs"].extend(refs)
            return payload

        found: set[str] = set()
        with self.connect() as db:
            message_ids = buckets["boss_message"]
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                rows = [
                    dict(row)
                    for row in db.execute(
                        f"""
                        SELECT m.*, c.contact_name, c.company
                        FROM boss_messages m
                        LEFT JOIN boss_conversations c ON c.id=m.conversation_id
                        WHERE m.account_id=? AND m.message_id IN ({placeholders})
                        ORDER BY COALESCE(m.sent_at, m.created_at), m.message_id
                        """,
                        [account_id, *message_ids],
                    ).fetchall()
                ]
                payload["messages"] = rows
                found.update(f"boss_message:{row['message_id']}" for row in rows)

            snapshot_ids = buckets["boss_job_snapshot"]
            if snapshot_ids:
                placeholders = ",".join("?" for _ in snapshot_ids)
                snapshots = [
                    dict(row)
                    for row in db.execute(
                        f"""
                        SELECT * FROM boss_job_snapshots
                        WHERE account_id=? AND id IN ({placeholders})
                        ORDER BY captured_at DESC
                        """,
                        [account_id, *snapshot_ids],
                    ).fetchall()
                ]
                found_snapshot_ids = {row["id"] for row in snapshots}
                fallback_ids = [value for value in snapshot_ids if value not in found_snapshot_ids]
                if fallback_ids:
                    fallback_placeholders = ",".join("?" for _ in fallback_ids)
                    snapshots.extend(
                        {
                            **dict(row),
                            "version": 0,
                            "source": "imported_job_card",
                            "captured_at": row["updated_at"] or row["created_at"],
                            "payload": self._json(dict(row)),
                        }
                        for row in db.execute(
                            f"""
                            SELECT * FROM boss_job_cards
                            WHERE account_id=? AND id IN ({fallback_placeholders})
                            """,
                            [account_id, *fallback_ids],
                        ).fetchall()
                    )
                for snapshot in snapshots:
                    snapshot["payload"] = self._loads(snapshot.get("payload"), {})
                payload["job_snapshots"] = snapshots
                found.update(f"boss_job_snapshot:{row['id']}" for row in snapshots)

            source_ids = buckets["web_source"]
            if source_ids:
                placeholders = ",".join("?" for _ in source_ids)
                sources = [
                    dict(row)
                    for row in db.execute(
                        f"""
                        SELECT * FROM research_sources
                        WHERE account_id=? AND id IN ({placeholders})
                        ORDER BY retrieved_at DESC
                        """,
                        [account_id, *source_ids],
                    ).fetchall()
                ]
                payload["web_sources"] = sources
                found.update(f"web_source:{row['id']}" for row in sources)

            profile_refs = buckets["candidate_profile"]
            if profile_refs:
                profile = db.execute(
                    """
                    SELECT profile_summary, skill_tags, project_tags, agent_tags,
                           weaknesses, updated_at
                    FROM candidate_profile
                    WHERE account_id=? AND id=1
                    """,
                    (account_id,),
                ).fetchone()
                if profile:
                    profile_payload = self._profile_payload(profile)
                    payload["candidate_profile"] = profile_payload
                    # The hash is validated by CommitGate at analysis time. The drawer
                    # intentionally shows only the local profile summary, not the full resume.
                    found.update(f"candidate_profile:{value}" for value in profile_refs)

        expected = {
            f"{namespace}:{value}" for namespace, values in buckets.items() for value in values
        }
        payload["missing_refs"].extend(sorted(expected - found))
        return payload

    def profile_payload(self) -> dict[str, Any]:
        account_id = self.current_account_id()
        with self.connect() as db:
            profile = db.execute(
                "SELECT * FROM candidate_profile WHERE account_id=? AND id=1", (account_id,)
            ).fetchone()
            prefs = db.execute(
                "SELECT * FROM job_preferences WHERE account_id=? AND id=1", (account_id,)
            ).fetchone()
        return {
            "profile": self._profile_payload(profile) if profile else None,
            "preferences": dict(prefs) if prefs else None,
        }

    def update_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        account_id = self.current_account_id()
        if not account_id:
            raise ValueError("保存简历前必须先确定 BOSS 账号")
        resume = payload.get("resume_markdown", "")
        profile_summary = payload.get("profile_summary") or self._summarize_resume(resume)
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO candidate_profile
                (account_id, id, resume_markdown, profile_summary, skill_tags, project_tags, agent_tags, weaknesses, updated_at)
                VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, id) DO UPDATE SET
                  resume_markdown=excluded.resume_markdown,
                  profile_summary=excluded.profile_summary,
                  skill_tags=excluded.skill_tags,
                  project_tags=excluded.project_tags,
                  agent_tags=excluded.agent_tags,
                  weaknesses=excluded.weaknesses,
                  updated_at=excluded.updated_at
                """,
                (
                    account_id,
                    resume,
                    profile_summary,
                    self._json(
                        payload.get("skill_tags")
                        or self._tags_from_text(
                            resume,
                            [
                                "Python",
                                "React",
                                "TypeScript",
                                "LangGraph",
                                "RAG",
                                "MCP",
                                "Agent",
                                "FastAPI",
                            ],
                        )
                    ),
                    self._json(
                        payload.get("project_tags")
                        or self._tags_from_text(
                            resume, ["Capybot", "RAG", "Agent", "Workflow", "评估", "工具调用"]
                        )
                    ),
                    self._json(
                        payload.get("agent_tags")
                        or self._tags_from_text(
                            resume, ["Agent", "MCP", "工具调用", "记忆", "eval", "LangGraph"]
                        )
                    ),
                    self._json(payload.get("weaknesses") or []),
                    now,
                ),
            )
            prefs = payload.get("preferences") or {}
            db.execute(
                """
                INSERT INTO job_preferences
                (account_id, id, target_roles, cities, salary, internship_time, excluded, updated_at)
                VALUES (?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, id) DO UPDATE SET
                  target_roles=excluded.target_roles,
                  cities=excluded.cities,
                  salary=excluded.salary,
                  internship_time=excluded.internship_time,
                  excluded=excluded.excluded,
                  updated_at=excluded.updated_at
                """,
                (
                    account_id,
                    prefs.get("target_roles"),
                    prefs.get("cities"),
                    prefs.get("salary"),
                    prefs.get("internship_time"),
                    prefs.get("excluded"),
                    now,
                ),
            )
            db.execute(
                """
                UPDATE opportunities
                SET fit_status='stale', job_fit_score=NULL, opportunity_priority_score=NULL,
                    fit_confidence=NULL, fit_updated_at=NULL
                WHERE account_id=?
                """
                ,
                (account_id,),
            )
        return self.profile_payload()

    def set_suggestion_status(
        self, suggestion_id: str, status: str, payload: dict[str, Any] | None = None
    ) -> bool:
        now = utc_now_iso()
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM suggestions WHERE id=?",
                (suggestion_id,),
            ).fetchone()
            if row is None:
                return False
            current_payload = self._loads(row["payload"], {})
            if payload:
                current_payload.update(payload)
            db.execute(
                "UPDATE suggestions SET status=?, payload=?, updated_at=? WHERE id=?",
                (status, self._json(current_payload), now, suggestion_id),
            )
            if status == "accepted":
                kind = row["kind"]
                if (
                    kind == "stage"
                    and row["opportunity_id"]
                    and current_payload.get("stage")
                ):
                    db.execute(
                        "UPDATE opportunities SET stage=?, manual_override_at=?, updated_at=? WHERE id=?",
                        (current_payload["stage"], now, now, row["opportunity_id"]),
                    )
        return True

    @classmethod
    def stage_label(cls, stage: str) -> str:
        return {
            "discovered": "已发现",
            "communicating": "已沟通",
            "need_my_action": "待我行动",
            "waiting_feedback": "待对方反馈",
            "interviewing": "面试中",
            "closed": "结束",
        }.get(stage, stage)

    @classmethod
    def _row_to_payload(cls, row: Any | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for key in ["fit_reasons", "risk_flags", "open_questions"]:
            data[key] = cls._loads(data.get(key), [])
        return data

    @classmethod
    def _opportunity_list_payload(cls, row: Any) -> dict[str, Any]:
        data = cls._row_to_payload(row) or {}
        keys = (
            "id",
            "title",
            "company",
            "salary",
            "city",
            "stage",
            "summary",
            "next_action",
            "confidence",
            "source_quality",
            "job_fit_score",
            "opportunity_priority_score",
            "fit_status",
            "fit_confidence",
            "risk_flags",
            "last_progress_at",
            "updated_at",
            "task_count",
        )
        return {key: data.get(key) for key in keys}

    @classmethod
    def _stage_suggestion_map(cls, rows: list[Any]) -> dict[str, str]:
        stages: dict[str, str] = {}
        for row in rows:
            item = dict(row)
            if item.get("kind") not in {None, "stage"}:
                continue
            opportunity_id = str(item.get("opportunity_id") or "")
            stage = cls._loads(item.get("payload"), {}).get("stage")
            if opportunity_id and stage and opportunity_id not in stages:
                stages[opportunity_id] = str(stage)
        return stages

    @classmethod
    def _profile_payload(cls, row: Any | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for key in ["skill_tags", "project_tags", "agent_tags", "weaknesses"]:
            data[key] = cls._loads(data.get(key), [])
        return data

    @staticmethod
    def _collect_evidence(result: dict[str, Any]) -> list[str]:
        evidence: list[str] = []
        for item in [
            *result.get("events", []),
            *result.get("tasks", []),
            *(result.get("risk_flags") or []),
        ]:
            for mid in item.get("evidence_message_ids") or []:
                if mid not in evidence:
                    evidence.append(str(mid))
        draft = result.get("reply_draft")
        if isinstance(draft, dict):
            for mid in draft.get("evidence_message_ids") or []:
                if mid not in evidence:
                    evidence.append(str(mid))
        return evidence

    @staticmethod
    def _rank_action_items(
        opportunities: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        suggestions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        opportunity_ids_with_action: set[str] = set()
        suggestions_by_opportunity: dict[str, list[dict[str, Any]]] = {}
        for suggestion in suggestions:
            if suggestion.get("kind") not in {"task", "draft", "risk"}:
                continue
            opportunity_id = str(suggestion.get("opportunity_id") or "")
            suggestions_by_opportunity.setdefault(opportunity_id, []).append(suggestion)

        for opp in opportunities:
            opportunity_id = str(opp.get("id") or "")
            effective_stage = opp.get("stage_suggestion") or opp.get("stage")
            pending = suggestions_by_opportunity.get(opportunity_id, [])
            actionable_interview = effective_stage == "interviewing" and bool(pending)
            if effective_stage == "need_my_action" or actionable_interview:
                preferred = next(
                    (
                        item
                        for kind in ("task", "risk", "draft")
                        for item in pending
                        if item.get("kind") == kind
                    ),
                    None,
                )
                out.append(
                    {
                        "kind": "opportunity",
                        "title": (
                            (preferred or {}).get("content")
                            or opp.get("next_action")
                            or opp.get("summary")
                            or opp.get("title")
                        ),
                        "opportunity": opp,
                        "priority": "high" if effective_stage == "need_my_action" else "medium",
                    }
                )
                opportunity_ids_with_action.add(opportunity_id)
        for task in tasks:
            if str(task.get("opportunity_id") or "") in opportunity_ids_with_action:
                continue
            out.append(
                {
                    "kind": "task",
                    "title": task.get("content") or task.get("title"),
                    "task": task,
                    "priority": task.get("priority", "medium"),
                }
            )
        priority = {"high": 0, "medium": 1, "low": 2}
        return sorted(out, key=lambda item: priority.get(item.get("priority"), 9))[:20]

    @staticmethod
    def _summarize_resume(resume: str) -> str:
        if not resume.strip():
            return "尚未填写简历。请上传 PDF 或粘贴 Markdown 简历后生成候选人画像。"
        text = " ".join(line.strip("#- * ") for line in resume.splitlines() if line.strip())
        text = ApplyStore._sanitize_trace_summary(text) or ""
        return text[:240] + ("..." if len(text) > 240 else "")

    @staticmethod
    def _tags_from_text(text: str, candidates: list[str]) -> list[str]:
        lower = text.lower()
        return [tag for tag in candidates if tag.lower() in lower][:12]
