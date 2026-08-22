"""Targeted BOSS refresh used by the external read-only MCP server."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from capybot.apply.agent_runtime.bootstrap import OpportunityBootstrapBuilder
from capybot.apply.evidence import EvidenceRepository
from capybot.apply.importer import SnapshotImporter
from capybot.apply.normalizer import BossMessageNormalizer
from capybot.apply.store import ApplyStore
from capybot.connectors.boss import BossConnector, BossConnectorError


class BossOpportunityReader:
    def __init__(
        self,
        store: ApplyStore | None = None,
        connector: BossConnector | None = None,
        evidence: EvidenceRepository | None = None,
    ) -> None:
        self.store = store or ApplyStore()
        self.connector = connector or BossConnector()
        self.evidence = evidence or EvidenceRepository(self.store)

    async def read(self, opportunity_id: str, *, max_pages: int = 3) -> dict[str, Any]:
        self.store.bind_opportunity_account(opportunity_id)
        context = self.store.opportunity_context(opportunity_id)
        opportunity = context.get("opportunity")
        if not opportunity:
            raise ValueError(f"机会不存在: {opportunity_id}")
        conversations = context.get("conversations") or []
        if not conversations:
            raise ValueError("机会没有关联 BOSS 会话")

        new_message_ids: list[str] = []
        message_refs: list[str] = []
        snapshot_refs: list[str] = []
        facts: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        contact_names = sorted(
            {
                str(message.get("sender_name") or "").strip()
                for message in context.get("messages") or []
                if not bool(message.get("from_me"))
                and str(message.get("sender_name") or "").strip()
            }
        )
        for conversation in conversations[:2]:
            conversation_id = str(conversation["id"])
            boss_uid = str(conversation.get("boss_uid") or "")
            request_context = self._conversation_request_context(
                conversation,
                context.get("messages") or [],
            )
            if not boss_uid:
                failures.append(
                    {
                        "conversation_id": conversation_id,
                        "error": "会话缺少 boss_uid",
                    }
                )
                continue
            try:
                raw_messages = await self.connector.fetch_messages_async(
                    boss_uid,
                    max_pages=max_pages,
                    **request_context,
                )
            except Exception as first_error:
                resolver = getattr(self.connector, "resolve_conversation_async", None)
                if not callable(resolver):
                    failures.append(
                        {
                            "conversation_id": conversation_id,
                            "error": str(first_error),
                        }
                    )
                    continue
                try:
                    resolved = await resolver(
                        company=opportunity.get("company"),
                        contact_names=contact_names,
                        title=opportunity.get("title"),
                    )
                    boss_uid = str(resolved.get("boss_uid") or "")
                    request_context = self._conversation_request_context(resolved, [])
                    raw_messages = await self.connector.fetch_messages_async(
                        boss_uid,
                        max_pages=max_pages,
                        **request_context,
                    )
                    facts.append(
                        {
                            "kind": "conversation_relocated",
                            "match_score": resolved.get("match_score"),
                        }
                    )
                except Exception as retry_error:
                    failures.append(
                        {
                            "conversation_id": conversation_id,
                            "error": (f"{first_error}; 重新定位会话后仍失败：{retry_error}"),
                        }
                    )
                    continue
            identity_hints = BossMessageNormalizer.identity_hints(raw_messages)
            for raw_message in raw_messages:
                normalized = BossMessageNormalizer.normalize(
                    conversation_id,
                    raw_message,
                    import_run_id=None,
                    identity_hints=identity_hints,
                )
                _, inserted = self.store.upsert_message(normalized)
                message_ref = f"boss_message:{normalized['message_id']}"
                message_refs.append(message_ref)
                if normalized.get("is_human_message", True) and normalized.get(
                    "message_type"
                ) not in {"platform_card", "system", "auto_followup"}:
                    facts.append(
                        {
                            "ref": message_ref,
                            "speaker": "me" if normalized.get("from_me") else "hr",
                            "type": normalized.get("message_type") or "text",
                            "content": OpportunityBootstrapBuilder.redact(
                                str(normalized.get("text") or "")
                            ),
                            "sent_at": normalized.get("sent_at"),
                        }
                    )
                if inserted:
                    new_message_ids.append(normalized["message_id"])
                job = SnapshotImporter._job_from_raw(raw_message)
                if not job:
                    continue
                self.store.upsert_job_card(conversation_id, job)
                snapshot = self.evidence.save_job_snapshot(
                    opportunity_id,
                    conversation_id=conversation_id,
                    platform_job_id=job.get("platform_job_id"),
                    payload=job,
                )
                snapshot_refs.append(f"boss_job_snapshot:{snapshot['id']}")
            last_message_id = (
                str(raw_messages[-1].get("mid") or raw_messages[-1].get("message_id") or "")
                if raw_messages
                else None
            )
            snapshot_hash = hashlib.sha256(
                json.dumps(raw_messages, ensure_ascii=False, sort_keys=True, default=str).encode(
                    "utf-8"
                )
            ).hexdigest()
            self.evidence.save_sync_checkpoint(
                conversation_id=conversation_id,
                source="boss_refresh_opportunity",
                last_message_id=last_message_id,
                snapshot_hash=snapshot_hash,
            )
        await self.connector.close_async()
        evidence_refs = sorted(set(message_refs + snapshot_refs))
        ok = not failures or bool(evidence_refs)
        if failures and not evidence_refs:
            outside_window = all(
                "不在 BOSS 当前 30 天会话窗口" in failure["error"] for failure in failures
            )
            if outside_window:
                summary = "该机会已超出 BOSS 当前 30 天会话窗口，保留本地历史证据。"
                freshness = "boss_conversation_outside_window"
                facts.append(
                    {
                        "kind": "conversation_refresh_status",
                        "status": "outside_current_window",
                    }
                )
            else:
                summary = "BOSS 实时刷新失败：" + failures[0]["error"]
                freshness = "failed"
        elif failures:
            summary = (
                f"部分刷新成功：新增 {len(new_message_ids)} 条消息，"
                f"保存 {len(set(snapshot_refs))} 个岗位快照，"
                f"{len(failures)} 个会话失败。"
            )
            freshness = "boss_live_partial"
        else:
            summary = (
                f"已刷新 {len(conversations[:2])} 个关联会话，"
                f"新增 {len(new_message_ids)} 条消息，"
                f"保存 {len(set(snapshot_refs))} 个岗位快照。"
            )
            freshness = "boss_live_refresh"
        return {
            "ok": ok,
            "opportunity_id": opportunity_id,
            "new_message_ids": new_message_ids,
            "facts": facts[-12:],
            "evidence_refs": evidence_refs,
            "freshness": freshness,
            "failures": failures,
            "summary": summary,
        }

    @staticmethod
    def _conversation_request_context(
        conversation: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        raw = BossJobDetailReader._json_object(conversation.get("raw_payload"))
        security_candidates = [
            conversation.get("security_id"),
            raw.get("security_id"),
            raw.get("securityId"),
        ]
        group_candidates = [
            conversation.get("group_id"),
            raw.get("group_id"),
            raw.get("groupId"),
            raw.get("gid"),
        ]
        source_candidates = [
            conversation.get("friend_source"),
            raw.get("friend_source"),
            raw.get("friendSource"),
            raw.get("source"),
        ]
        for message in messages:
            payload = BossJobDetailReader._json_object(message.get("raw_payload"))
            security_candidates.append(payload.get("securityId"))
        security_id = next(
            (str(value).strip() for value in security_candidates if str(value or "").strip()),
            "",
        )
        group_id = next(
            (str(value).strip() for value in group_candidates if str(value or "").strip()),
            "",
        )
        friend_source = next(
            (int(value) for value in source_candidates if value is not None and str(value).strip()),
            0,
        )
        return {
            "security_id": security_id,
            "group_id": group_id,
            "friend_source": friend_source,
        }


class BossJobDetailReader:
    """Fetch and version one BOSS job detail without exposing free-form lookup."""

    def __init__(
        self,
        store: ApplyStore | None = None,
        connector: BossConnector | None = None,
        evidence: EvidenceRepository | None = None,
    ) -> None:
        self.store = store or ApplyStore()
        self.connector = connector or BossConnector()
        self.evidence = evidence or EvidenceRepository(self.store)

    async def read(self, opportunity_id: str) -> dict[str, Any]:
        self.store.bind_opportunity_account(opportunity_id)
        context = self.store.opportunity_context(opportunity_id)
        opportunity = context.get("opportunity")
        if not opportunity:
            raise ValueError(f"机会不存在: {opportunity_id}")
        platform_job_id, security_id = self._job_identity(context)
        if not platform_job_id:
            return {
                "ok": False,
                "opportunity_id": opportunity_id,
                "facts": [
                    {
                        "kind": "job_detail_status",
                        "status": "needs_context",
                        "reason": "当前机会缺少 BOSS 岗位详情所需的岗位 ID。",
                    }
                ],
                "evidence_refs": [],
                "freshness": "missing_job_identity",
                "summary": "当前机会缺少可验证的 BOSS 岗位身份，无法补查岗位详情。",
            }

        try:
            try:
                page_reader = getattr(
                    self.connector,
                    "fetch_job_detail_for_opportunity_async",
                    None,
                )
                if callable(page_reader):
                    raw = await page_reader(
                        platform_job_id=platform_job_id,
                        title=str(opportunity.get("title") or ""),
                        company=str(opportunity.get("company") or ""),
                        security_id=security_id,
                        source_url=self._job_source_url(context, platform_job_id),
                    )
                else:
                    raw = await self.connector.fetch_job_detail_async(security_id)
            except BossConnectorError as exc:
                if self._environment_is_blocked(exc):
                    return {
                        "ok": False,
                        "opportunity_id": opportunity_id,
                        "facts": [
                            {
                                "kind": "job_detail_status",
                                "status": "environment_blocked",
                                "reason": ("BOSS 风控拒绝了当前浏览器环境的岗位详情请求。"),
                            }
                        ],
                        "evidence_refs": [],
                        "freshness": "boss_environment_blocked",
                        "summary": (
                            "BOSS 风控拦截了岗位详情补查；保留本地证据，不把空结果写入机会。"
                        ),
                    }
                if self._job_not_found(exc):
                    return {
                        "ok": False,
                        "opportunity_id": opportunity_id,
                        "facts": [
                            {
                                "kind": "job_detail_status",
                                "status": "not_found_current",
                                "reason": (
                                    "BOSS 当前职位列表未找到与本地岗位卡完全一致的职位；"
                                    "可能已下线，也可能已更名。"
                                ),
                            }
                        ],
                        "evidence_refs": [],
                        "freshness": "boss_job_not_found_current",
                        "summary": "未安全定位到同一岗位，已保留本地证据且未写入相似职位。",
                    }
                if not self._job_is_offline(exc):
                    raise
                return {
                    "ok": False,
                    "opportunity_id": opportunity_id,
                    "facts": [
                        {
                            "kind": "job_detail_status",
                            "status": "job_offline",
                            "reason": "BOSS 已明确返回该岗位不存在或已下线。",
                        }
                    ],
                    "evidence_refs": [],
                    "freshness": "boss_job_offline",
                    "summary": "BOSS 岗位已下线，保留本地岗位卡和原有机会状态。",
                }
            detail = self._normalize(
                raw,
                platform_job_id,
                str(raw.get("_capybot_security_id") or security_id),
            )
            self._validate_identity(opportunity, detail)
            conversations = context.get("conversations") or []
            conversation_id = (
                str(conversations[0].get("id") or "") if conversations else None
            ) or None
            if conversation_id:
                self.store.upsert_job_card(conversation_id, detail)
            snapshot = self.evidence.save_job_snapshot(
                opportunity_id,
                conversation_id=conversation_id,
                platform_job_id=platform_job_id,
                payload=detail,
                source="boss_fetch_job_detail",
            )
            ref = f"boss_job_snapshot:{snapshot['id']}"
            return {
                "ok": True,
                "opportunity_id": opportunity_id,
                "facts": [
                    {
                        "kind": "boss_job_detail",
                        "ref": ref,
                        **{
                            key: detail.get(key)
                            for key in (
                                "title",
                                "company",
                                "salary",
                                "city",
                                "experience",
                                "education",
                                "description",
                                "skills",
                                "address",
                                "days_per_week",
                                "least_month",
                                "company_industry",
                                "company_scale",
                                "company_stage",
                            )
                        },
                    }
                ],
                "evidence_refs": [ref],
                "freshness": "boss_live_job_detail",
                "summary": "已从 BOSS 读取并保存完整岗位详情。",
            }
        finally:
            await self.connector.close_async()

    @staticmethod
    def _job_identity(context: dict[str, Any]) -> tuple[str, str]:
        opportunity = context.get("opportunity") or {}
        platform_candidates: list[Any] = [opportunity.get("platform_job_id")]
        security_candidates: list[Any] = []
        for row in context.get("job_snapshots") or []:
            platform_candidates.append(row.get("platform_job_id"))
            payload = BossJobDetailReader._json_object(row.get("payload"))
            platform_candidates.extend(
                [
                    payload.get("platform_job_id"),
                    payload.get("encryptJobId"),
                    payload.get("jobId"),
                ]
            )
            security_candidates.extend(BossJobDetailReader._security_ids(payload))
        for row in context.get("jobs") or []:
            platform_candidates.append(row.get("platform_job_id"))
            payload = BossJobDetailReader._json_object(row.get("raw_payload"))
            platform_candidates.extend([payload.get("encryptJobId"), payload.get("jobId")])
            security_candidates.extend(BossJobDetailReader._security_ids(payload))
        platform_job_id = next(
            (str(value).strip() for value in platform_candidates if str(value or "").strip()),
            "",
        )
        security_id = next(
            (str(value).strip() for value in security_candidates if str(value or "").strip()),
            "",
        )
        return platform_job_id, security_id

    @staticmethod
    def _security_ids(payload: dict[str, Any]) -> list[Any]:
        candidates: list[Any] = [
            payload.get("security_id"),
            payload.get("securityId"),
            payload.get("_capybot_security_id"),
        ]
        raw_payload = BossJobDetailReader._json_object(payload.get("raw_payload"))
        if raw_payload:
            candidates.extend(BossJobDetailReader._security_ids(raw_payload))
        for key in ("url", "jobUrl", "job_url"):
            url = str(payload.get(key) or "").strip()
            if url:
                candidates.extend(parse_qs(urlparse(url).query).get("securityId", []))
        return candidates

    @staticmethod
    def _job_source_url(context: dict[str, Any], platform_job_id: str) -> str:
        expected_id = str(platform_job_id or "").strip()
        for row in [
            *(context.get("job_snapshots") or []),
            *(context.get("jobs") or []),
        ]:
            payload = BossJobDetailReader._json_object(
                row.get("payload") or row.get("raw_payload")
            )
            nested = BossJobDetailReader._json_object(payload.get("raw_payload"))
            for source in (payload, nested):
                for key in (
                    "source_url",
                    "_capybot_source_url",
                    "url",
                    "jobUrl",
                    "job_url",
                ):
                    value = str(source.get(key) or "").strip()
                    parsed = urlparse(value)
                    match = re.search(r"/job_detail/([^./?]+)", parsed.path)
                    if (
                        parsed.scheme == "https"
                        and parsed.hostname in {"zhipin.com", "www.zhipin.com"}
                        and match
                        and match.group(1) == expected_id
                    ):
                        return value
        return ""

    @staticmethod
    def _normalize(
        raw: dict[str, Any],
        platform_job_id: str,
        security_id: str,
    ) -> dict[str, Any]:
        job = raw.get("jobInfo") if isinstance(raw.get("jobInfo"), dict) else {}
        company = raw.get("brandComInfo") if isinstance(raw.get("brandComInfo"), dict) else {}
        boss = raw.get("bossInfo") if isinstance(raw.get("bossInfo"), dict) else {}
        description = (
            raw.get("jobDetail") or job.get("postDescription") or job.get("description") or ""
        )
        skills = job.get("jobLabels") or job.get("showSkills") or job.get("skills") or []
        if not isinstance(skills, list):
            skills = [str(skills)]
        company_labels = company.get("labels") or []
        if not isinstance(company_labels, list):
            company_labels = [str(company_labels)]
        return {
            "platform_job_id": platform_job_id,
            "security_id": security_id,
            "encrypt_job_id": str(job.get("encryptJobId") or job.get("encryptId") or ""),
            "title": job.get("jobName") or job.get("title") or "待补全岗位",
            "company": company.get("brandName") or job.get("brandName") or job.get("company"),
            "salary": job.get("salaryDesc") or job.get("salary"),
            "city": job.get("cityName") or job.get("locationName") or job.get("city"),
            "experience": job.get("experienceName") or job.get("experience"),
            "education": job.get("degreeName") or job.get("education"),
            "description": str(description).strip(),
            "requirements": str(description).strip(),
            "skills": [str(value) for value in skills if str(value).strip()],
            "address": job.get("address"),
            "days_per_week": job.get("daysPerWeekDesc"),
            "least_month": job.get("leastMonthDesc"),
            "pay_type": job.get("payTypeDesc"),
            "employment_type": job.get("jobType"),
            "company_industry": company.get("industryName"),
            "company_scale": company.get("scaleName"),
            "company_stage": company.get("stageName") or company.get("customerBrandStageName"),
            "company_introduction": company.get("introduce"),
            "company_labels": [str(value) for value in company_labels if str(value).strip()],
            "boss_uid": str(
                boss.get("encryptBossId") or boss.get("uid") or boss.get("bossId") or ""
            ),
            "boss_name": boss.get("name") or boss.get("bossName"),
            "raw_payload": {**raw, "_capybot_security_id": security_id},
        }

    @staticmethod
    def _validate_identity(
        opportunity: dict[str, Any],
        detail: dict[str, Any],
    ) -> None:
        expected_id = str(opportunity.get("platform_job_id") or "").strip()
        actual_id = str(detail.get("platform_job_id") or "").strip()
        if expected_id and actual_id and expected_id != actual_id:
            raise ValueError("BOSS 岗位详情 ID 与当前机会不一致，已拒绝写入")

    @staticmethod
    def _job_is_offline(exc: Exception) -> bool:
        message = str(exc)
        return any(
            marker in message
            for marker in (
                "职位已不存在",
                "岗位已不存在",
                "职位已下线",
                "岗位已下线",
                "job is offline",
            )
        )

    @staticmethod
    def _job_not_found(exc: Exception) -> bool:
        message = str(exc)
        return any(
            marker in message
            for marker in (
                "当前职位列表中未找到",
                "未找到与该机会一致",
                "no matching job",
            )
        )

    @staticmethod
    def _environment_is_blocked(exc: Exception) -> bool:
        message = str(exc)
        return any(
            marker in message
            for marker in (
                "您的环境存在异常",
                "当前环境存在异常",
                "访问环境异常",
                "访问过于频繁",
            )
        )

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
