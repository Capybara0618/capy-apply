"""Persistence helpers for versioned external evidence and tool observations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from capybot.apply.models import utc_now_iso
from capybot.apply.store import ApplyStore


class EvidenceRepository:
    def __init__(self, store: ApplyStore | None = None) -> None:
        self.store = store or ApplyStore()

    def save_job_snapshot(
        self,
        opportunity_id: str,
        *,
        conversation_id: str | None,
        platform_job_id: str | None,
        payload: dict[str, Any],
        source: str = "boss_refresh_opportunity",
    ) -> dict[str, Any]:
        account_id = self.store.current_account_id()
        content_hash = self._hash(payload)
        captured_at = utc_now_iso()
        with self.store.connect() as db:
            existing = db.execute(
                """
                SELECT * FROM boss_job_snapshots
                WHERE opportunity_id=? AND content_hash=?
                ORDER BY version DESC LIMIT 1
                """,
                (opportunity_id, content_hash),
            ).fetchone()
            if existing:
                return dict(existing)
            row = db.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM boss_job_snapshots WHERE opportunity_id=?",
                (opportunity_id,),
            ).fetchone()
            version = int(row["version"] or 0) + 1
            snapshot_id = self.store._id(
                "boss-job-snapshot",
                opportunity_id,
                version,
                content_hash,
            )
            db.execute(
                """
                INSERT INTO boss_job_snapshots
                (id, account_id, opportunity_id, conversation_id, platform_job_id,
                 version, source, content_hash, payload, captured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    account_id,
                    opportunity_id,
                    conversation_id,
                    platform_job_id,
                    version,
                    source,
                    content_hash,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    captured_at,
                ),
            )
        return {
            "id": snapshot_id,
            "account_id": account_id,
            "opportunity_id": opportunity_id,
            "conversation_id": conversation_id,
            "platform_job_id": platform_job_id,
            "version": version,
            "source": source,
            "content_hash": content_hash,
            "payload": payload,
            "captured_at": captured_at,
        }

    def save_research_source(
        self,
        opportunity_id: str,
        *,
        query: str,
        url: str,
        title: str | None,
        excerpt: str | None,
        research_type: str,
        source_tier: str,
        quality_score: float,
        verified: bool,
        published_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        account_id = self.store.current_account_id()
        payload = {
            "url": url,
            "title": title,
            "excerpt": excerpt,
            "research_type": research_type,
        }
        content_hash = self._hash(payload)
        source_id = self.store._id("web-source", opportunity_id, content_hash)
        retrieved_at = utc_now_iso()
        domain = urlparse(url).netloc.lower()
        source_metadata = metadata or {}
        with self.store.connect() as db:
            db.execute(
                """
                INSERT INTO research_sources
                (id, account_id, opportunity_id, query, url, title, source_domain,
                 excerpt, research_type, source_tier, quality_score, verified,
                 published_at, metadata, last_checked_at, content_hash, retrieved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_id, content_hash) DO UPDATE SET
                  query=excluded.query,
                  title=COALESCE(excluded.title, research_sources.title),
                  excerpt=COALESCE(excluded.excerpt, research_sources.excerpt),
                  research_type=excluded.research_type,
                  source_tier=excluded.source_tier,
                  quality_score=excluded.quality_score,
                  verified=excluded.verified,
                  published_at=COALESCE(excluded.published_at, research_sources.published_at),
                  metadata=excluded.metadata,
                  last_checked_at=excluded.last_checked_at,
                  retrieved_at=excluded.retrieved_at
                """,
                (
                    source_id,
                    account_id,
                    opportunity_id,
                    query,
                    url,
                    title,
                    domain,
                    excerpt,
                    research_type,
                    source_tier,
                    max(0.0, min(1.0, float(quality_score))),
                    verified,
                    published_at,
                    json.dumps(source_metadata, ensure_ascii=False, default=str),
                    retrieved_at,
                    content_hash,
                    retrieved_at,
                ),
            )
        return {
            "id": source_id,
            "opportunity_id": opportunity_id,
            "url": url,
            "title": title,
            "source_domain": domain,
            "excerpt": excerpt,
            "research_type": research_type,
            "source_tier": source_tier,
            "quality_score": max(0.0, min(1.0, float(quality_score))),
            "verified": verified,
            "published_at": published_at,
            "metadata": source_metadata,
            "last_checked_at": retrieved_at,
            "retrieved_at": retrieved_at,
        }

    def recent_research_sources(
        self,
        opportunity_id: str,
        *,
        research_type: str,
        max_age_hours: int = 24,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        account_id = self.store.current_account_id()
        with self.store.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM research_sources
                WHERE account_id=? AND opportunity_id=? AND research_type=?
                ORDER BY retrieved_at DESC
                LIMIT ?
                """,
                (account_id, opportunity_id, research_type, limit),
            ).fetchall()
        sources: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            retrieved_at = value.get("retrieved_at")
            if isinstance(retrieved_at, str):
                try:
                    retrieved_at = datetime.fromisoformat(retrieved_at)
                except ValueError:
                    continue
            if not isinstance(retrieved_at, datetime):
                continue
            if retrieved_at.tzinfo is None:
                retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
            if retrieved_at < cutoff:
                continue
            sources.append(
                {
                    "kind": "public_source",
                    "id": value["id"],
                    "title": value.get("title"),
                    "url": value.get("url"),
                    "source_domain": value.get("source_domain"),
                    "excerpt": value.get("excerpt"),
                    "research_type": value.get("research_type"),
                    "source_tier": value.get("source_tier"),
                    "quality_score": float(value.get("quality_score") or 0),
                    "verified": bool(value.get("verified")),
                    "trust": "untrusted_public_web",
                }
            )
        return sources

    def save_sync_checkpoint(
        self,
        *,
        conversation_id: str | None,
        source: str,
        last_message_id: str | None,
        snapshot_hash: str | None,
        cursor: str | None = None,
    ) -> None:
        account_id = self.store.current_account_id()
        checkpoint_id = self.store._id(
            "sync-checkpoint",
            account_id,
            conversation_id or "account",
            source,
        )
        with self.store.connect() as db:
            db.execute(
                """
                INSERT INTO sync_checkpoints
                (id, account_id, conversation_id, source, cursor, last_message_id,
                 snapshot_hash, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, conversation_id, source) DO UPDATE SET
                  cursor=excluded.cursor,
                  last_message_id=excluded.last_message_id,
                  snapshot_hash=excluded.snapshot_hash,
                  synced_at=excluded.synced_at
                """,
                (
                    checkpoint_id,
                    account_id,
                    conversation_id,
                    source,
                    cursor,
                    last_message_id,
                    snapshot_hash,
                    utc_now_iso(),
                ),
            )

    @staticmethod
    def _hash(value: Any) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
