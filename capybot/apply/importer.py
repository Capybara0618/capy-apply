"""Snapshot import pipeline for BOSS conversations."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from capybot.connectors.boss import BossConnector

from .decision_router import DecisionRouter
from .models import ImportReport, parse_utc_datetime, utc_now_iso
from .normalizer import BossMessageNormalizer
from .store import ApplyStore

ProgressCallback = Callable[[dict[str, Any]], None]


class SnapshotImporter:
    def __init__(
        self,
        store: ApplyStore | None = None,
        boss: BossConnector | None = None,
        progress_callback: ProgressCallback | None = None,
    ):
        self.store = store or ApplyStore()
        self.boss = boss or BossConnector()
        self.progress_callback = progress_callback

    def import_boss(self, days: int = 30, conversation_limit: int = 200) -> dict[str, Any]:
        started_at = utc_now_iso()
        import_run_id = self.store._id("import", started_at)
        report = ImportReport(failures=[])
        self._emit(
            "running",
            phase="读取账号",
            message="正在确认 BOSS 本地账号档案。",
            started_at=started_at,
        )
        account_id = self.store.upsert_account(self.boss.account_snapshot())
        try:
            self._emit(
                "running",
                phase="扫描会话",
                message=f"正在读取近 {days} 天会话列表。",
                started_at=started_at,
            )
            conversations = self.boss.list_conversations(days=days, limit=conversation_limit)
        except Exception as exc:
            return self._failed_list_report(started_at, import_run_id, report, exc)
        conversations = [{**row, "account_id": account_id} for row in conversations]
        report.scanned_conversations = len(conversations)
        self._emit(
            "running",
            phase="导入消息",
            message=f"已扫描到 {len(conversations)} 个会话，开始逐个读取聊天记录。",
            current=0,
            total=len(conversations),
            started_at=started_at,
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        delta_items = self._import_conversations_sync(conversations, report, import_run_id, cutoff)
        return self._finish(
            started_at, import_run_id, report, delta_items, account=self.boss.account_snapshot()
        )

    async def import_boss_async(
        self, days: int = 30, conversation_limit: int = 200
    ) -> dict[str, Any]:
        started_at = utc_now_iso()
        import_run_id = self.store._id("import", started_at)
        report = ImportReport(failures=[])
        self._emit(
            "running",
            phase="读取账号",
            message="正在确认 BOSS 本地账号档案。",
            started_at=started_at,
        )
        account = await self.boss.account_snapshot_async()
        account_id = self.store.upsert_account(account)
        try:
            self._emit(
                "running",
                phase="扫描会话",
                message=f"正在读取近 {days} 天会话列表。",
                started_at=started_at,
            )
            conversations = await self.boss.list_conversations_async(
                days=days, limit=conversation_limit
            )
        except Exception as exc:
            return self._failed_list_report(started_at, import_run_id, report, exc)
        conversations = [{**row, "account_id": account_id} for row in conversations]
        report.scanned_conversations = len(conversations)
        self._emit(
            "running",
            phase="导入消息",
            message=f"已扫描到 {len(conversations)} 个会话，开始逐个读取聊天记录。",
            current=0,
            total=len(conversations),
            started_at=started_at,
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        delta_items = await self._import_conversations_async(
            conversations, report, import_run_id, cutoff
        )
        return self._finish(started_at, import_run_id, report, delta_items, account=account)

    def _import_conversations_sync(
        self,
        conversations: list[dict[str, Any]],
        report: ImportReport,
        import_run_id: str,
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        delta_items: list[dict[str, Any]] = []
        total = len(conversations)
        for index, row in enumerate(conversations, start=1):
            cid = self.store.upsert_conversation(row)
            self.store.upsert_contact_from_conversation(cid)
            contact = row.get("contact_name") or row.get("boss_name") or cid
            self._emit(
                "running",
                phase="导入消息",
                message=f"正在读取 {contact} 的聊天记录。",
                current=index,
                total=total,
                contact=contact,
            )
            try:
                boss_uid = row.get("boss_uid") or row.get("id")
                raw_context = row.get("raw_payload") or {}
                messages = self.boss.fetch_messages(
                    str(boss_uid),
                    security_id=str(row.get("security_id") or raw_context.get("security_id") or ""),
                    group_id=str(row.get("group_id") or raw_context.get("group_id") or ""),
                    friend_source=int(
                        row.get("friend_source") or raw_context.get("friend_source") or 0
                    ),
                )
                new_message_ids, in_window_count = self._save_messages_and_jobs(
                    cid, messages, report, import_run_id, cutoff
                )
                opportunity_ids = (
                    self.store.ensure_opportunities_for_conversation(cid)
                    if in_window_count
                    else self.store.opportunity_ids_for_conversation(cid)
                )
                self._append_delta_items(delta_items, cid, opportunity_ids, new_message_ids, report)
                report.successful_conversations += 1
                self._emit(
                    "running",
                    phase="导入消息",
                    message=f"{contact} 读取完成，新增 {len(new_message_ids)} 条消息。",
                    current=index,
                    total=total,
                    contact=contact,
                    successful_conversations=report.successful_conversations,
                    failed_conversations=report.failed_conversations,
                    new_messages=report.new_messages,
                )
                time.sleep(0 if self.boss.fixture_mode else 1.2)
            except Exception as exc:
                self._record_conversation_failure(report, cid, row, exc)
                self._emit(
                    "running",
                    phase="导入消息",
                    message=f"{contact} 获取失败：{exc}",
                    current=index,
                    total=total,
                    contact=contact,
                    successful_conversations=report.successful_conversations,
                    failed_conversations=report.failed_conversations,
                    failures=report.failures[-5:],
                )
        return delta_items

    async def _import_conversations_async(
        self,
        conversations: list[dict[str, Any]],
        report: ImportReport,
        import_run_id: str,
        cutoff: datetime,
    ) -> list[dict[str, Any]]:
        delta_items: list[dict[str, Any]] = []
        total = len(conversations)
        for index, row in enumerate(conversations, start=1):
            cid = self.store.upsert_conversation(row)
            self.store.upsert_contact_from_conversation(cid)
            contact = row.get("contact_name") or row.get("boss_name") or cid
            self._emit(
                "running",
                phase="导入消息",
                message=f"正在读取 {contact} 的聊天记录。",
                current=index,
                total=total,
                contact=contact,
            )
            try:
                boss_uid = row.get("boss_uid") or row.get("id")
                raw_context = row.get("raw_payload") or {}
                messages = await self.boss.fetch_messages_async(
                    str(boss_uid),
                    security_id=str(row.get("security_id") or raw_context.get("security_id") or ""),
                    group_id=str(row.get("group_id") or raw_context.get("group_id") or ""),
                    friend_source=int(
                        row.get("friend_source") or raw_context.get("friend_source") or 0
                    ),
                )
                new_message_ids, in_window_count = self._save_messages_and_jobs(
                    cid, messages, report, import_run_id, cutoff
                )
                opportunity_ids = (
                    self.store.ensure_opportunities_for_conversation(cid)
                    if in_window_count
                    else self.store.opportunity_ids_for_conversation(cid)
                )
                self._append_delta_items(delta_items, cid, opportunity_ids, new_message_ids, report)
                report.successful_conversations += 1
                self._emit(
                    "running",
                    phase="导入消息",
                    message=f"{contact} 读取完成，新增 {len(new_message_ids)} 条消息。",
                    current=index,
                    total=total,
                    contact=contact,
                    successful_conversations=report.successful_conversations,
                    failed_conversations=report.failed_conversations,
                    new_messages=report.new_messages,
                )
                await asyncio.sleep(0 if self.boss.fixture_mode else 1.2)
            except Exception as exc:
                self._record_conversation_failure(report, cid, row, exc)
                self._emit(
                    "running",
                    phase="导入消息",
                    message=f"{contact} 获取失败：{exc}",
                    current=index,
                    total=total,
                    contact=contact,
                    successful_conversations=report.successful_conversations,
                    failed_conversations=report.failed_conversations,
                    failures=report.failures[-5:],
                )
        return delta_items

    def _save_messages_and_jobs(
        self,
        conversation_id: str,
        messages: list[dict[str, Any]],
        report: ImportReport,
        import_run_id: str,
        cutoff: datetime,
    ) -> tuple[list[str], int]:
        new_message_ids: list[str] = []
        in_window_count = 0
        identity_hints = BossMessageNormalizer.identity_hints(messages)
        for raw in messages:
            normalized = self._normalize_message(
                conversation_id,
                raw,
                import_run_id=import_run_id,
                identity_hints=identity_hints,
            )
            sent_at = parse_utc_datetime(normalized.get("sent_at"))
            if sent_at is not None and sent_at < cutoff:
                continue
            in_window_count += 1
            _, inserted = self.store.upsert_message(normalized)
            if inserted:
                report.new_messages += 1
                new_message_ids.append(normalized["message_id"])
            job = self._job_from_raw(raw)
            if job:
                _, job_inserted = self.store.upsert_job_card(conversation_id, job)
                if job_inserted:
                    report.new_jobs += 1
        return new_message_ids, in_window_count

    def _append_delta_items(
        self,
        delta_items: list[dict[str, Any]],
        conversation_id: str,
        opportunity_ids: list[str],
        new_message_ids: list[str],
        report: ImportReport,
    ) -> None:
        if not new_message_ids:
            report.skipped_conversations += 1
            for oid in opportunity_ids or [None]:
                delta_items.append(
                    {
                        "conversation_id": conversation_id,
                        "opportunity_id": oid,
                        "new_message_ids": [],
                        "new_message_count": 0,
                        "analysis_mode": "skipped",
                        "skipped_reason": "本次导入没有新增消息。",
                    }
                )
            return
        report.changed_conversations += 1
        messages = self.store.message_evidence(new_message_ids)["messages"]
        for oid in opportunity_ids:
            before = self.store.opportunity_detail(oid)
            opp = (before or {}).get("opportunity") or {}
            route = DecisionRouter.route_delta(
                messages,
                source_quality=opp.get("source_quality"),
            )
            delta_items.append(
                {
                    "conversation_id": conversation_id,
                    "opportunity_id": oid,
                    "new_message_ids": new_message_ids,
                    "new_message_count": len(new_message_ids),
                    **route.import_payload(),
                    "before_stage": opp.get("stage"),
                    "before_next_action": opp.get("next_action"),
                }
            )

    def _finish(
        self,
        started_at: str,
        import_run_id: str,
        report: ImportReport,
        delta_items: list[dict[str, Any]],
        *,
        account: dict[str, Any],
    ) -> dict[str, Any]:
        pending_items = [
            item
            for item in delta_items
            if item.get("opportunity_id") and item.get("analysis_mode") not in {"skipped", "failed"}
        ]
        report.pending_analysis = len(pending_items)
        report.queued_opportunities = len(pending_items)
        payload = report.to_dict()
        payload["import_run_id"] = import_run_id
        payload["account_id"] = self.store.current_account_id()
        payload["started_at"] = started_at
        payload["finished_at"] = utc_now_iso()
        payload["analyses"] = []
        scan_status = getattr(self.boss, "conversation_scan_status", None)
        payload["source_status"] = (
            scan_status()
            if callable(scan_status)
            else {
                "readable": True,
                "explicitly_empty": report.scanned_conversations == 0,
                "conversation_count": report.scanned_conversations,
                "message": (
                    "BOSS 明确显示近 30 天暂无联系人，本地历史数据保持不变。"
                    if report.scanned_conversations == 0
                    else f"BOSS 会话列表可读，共扫描到 {report.scanned_conversations} 个会话。"
                ),
            }
        )
        payload["changed_opportunity_ids"] = [
            item.get("opportunity_id") for item in pending_items if item.get("opportunity_id")
        ]
        self._save_import_run(import_run_id, started_at, payload)
        for item in delta_items:
            self.store.save_import_run_item(import_run_id, item)
        self.store.upsert_account(account, imported=True)
        source_message = str(payload["source_status"].get("message") or "导入完成。")
        self._emit(
            "ok",
            phase="完成",
            message=source_message,
            current=report.successful_conversations + report.failed_conversations,
            total=report.scanned_conversations,
            report=payload,
        )
        return payload

    def _failed_list_report(
        self, started_at: str, import_run_id: str, report: ImportReport, exc: Exception
    ) -> dict[str, Any]:
        report.failed_conversations = 1
        report.failures.append({"stage": "list_conversations", "error": str(exc)})
        payload = report.to_dict()
        payload["import_run_id"] = import_run_id
        payload["started_at"] = started_at
        payload["finished_at"] = utc_now_iso()
        payload["analyses"] = []
        self._save_import_run(import_run_id, started_at, payload)
        self._emit(
            "failed",
            phase="获取失败",
            message=f"获取会话列表失败：{exc}",
            report=payload,
            failures=payload.get("failures"),
        )
        return payload

    def _save_import_run(self, run_id: str, started_at: str, payload: dict[str, Any]) -> None:
        account_id = (self.store.current_account() or {}).get("id")
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO import_runs (id, account_id, started_at, finished_at, report) VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    account_id,
                    started_at,
                    payload["finished_at"],
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    @staticmethod
    def _record_conversation_failure(
        report: ImportReport, cid: str, row: dict[str, Any], exc: Exception
    ) -> None:
        report.failed_conversations += 1
        report.failures.append(
            {
                "conversation_id": cid,
                "contact": row.get("contact_name"),
                "error": str(exc),
            }
        )

    def _emit(self, status: str, **payload: Any) -> None:
        if self.progress_callback is None:
            return
        data = {"status": status, **payload}
        current = data.get("current")
        total = data.get("total")
        if isinstance(current, int) and isinstance(total, int) and total > 0:
            data["percent"] = round(min(100, max(0, current / total * 100)))
        try:
            self.progress_callback(data)
        except Exception:
            pass

    @staticmethod
    def _normalize_message(
        conversation_id: str,
        raw: dict[str, Any],
        *,
        import_run_id: str | None = None,
        identity_hints: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        return BossMessageNormalizer.normalize(
            conversation_id,
            raw,
            import_run_id=import_run_id,
            identity_hints=identity_hints,
        )

    @staticmethod
    def _job_from_raw(raw: dict[str, Any]) -> dict[str, Any] | None:
        body = raw.get("body") or {}
        job = body.get("jobDesc")
        if not isinstance(job, dict):
            return None
        boss = job.get("boss") or {}
        return {
            "platform_job_id": str(job.get("encryptJobId") or job.get("jobId") or ""),
            "title": job.get("title") or "待补全岗位",
            "company": job.get("company"),
            "salary": job.get("salary"),
            "city": job.get("city"),
            "experience": job.get("experience"),
            "education": job.get("education"),
            "boss_uid": str(boss.get("uid") or ""),
            "boss_name": boss.get("name"),
            "raw_payload": job,
        }
