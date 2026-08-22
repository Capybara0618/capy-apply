"""Run the Opportunity Agent and persist only CommitGate-approved projections."""

from __future__ import annotations

from typing import Any

from capybot.apply.agent_runs import AgentRunRepository
from capybot.apply.agent_runtime import (
    ApplyToolbox,
    OpenAIAgentsLoop,
    OpenAIPlannerModel,
    OpportunityBootstrapBuilder,
)
from capybot.apply.agent_runtime.commit_gate import CommitGate, CommitResult
from capybot.apply.agent_runtime.decision_committer import DecisionCommitter
from capybot.apply.agent_runtime.mcp_client import MCPToolClient
from capybot.apply.decision_router import DecisionRoute, DecisionRouter
from capybot.apply.mcp_registry import create_apply_mcp_client
from capybot.apply.store import ApplyStore


class OpportunityAnalysisService:
    ENGINE = "openai_agents_sdk_v1"

    def __init__(
        self,
        store: ApplyStore | None = None,
        *,
        model: OpenAIPlannerModel | None = None,
        agent: OpenAIAgentsLoop | None = None,
        mcp_env: dict[str, str] | None = None,
    ) -> None:
        self.store = store or ApplyStore()
        self.runs = AgentRunRepository(self.store)
        self.committer = DecisionCommitter(self.store)
        self.model = model or OpenAIPlannerModel()
        self.mcp_env = dict(mcp_env or {})
        self.agent = agent or OpenAIAgentsLoop(
            model=self.model,
            bootstrap_builder=OpportunityBootstrapBuilder(self.store),
            toolbox_factory=self._toolbox,
        )

    async def analyze(
        self,
        opportunity_id: str,
        *,
        trigger: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = self.store.opportunity_context(opportunity_id)
        conversations = context.get("conversations") or []
        conversation_id = str(conversations[0]["id"]) if conversations else None
        route = DecisionRouter.route_context(context, trigger)
        if route.mode == "cold_projection" and route.decision is not None:
            return self._commit_routed_projection(
                opportunity_id,
                conversation_id=conversation_id,
                route=route,
            )
        run_id = self.runs.create(
            "opportunity",
            opportunity_id,
            conversation_id=conversation_id,
            opportunity_id=opportunity_id,
            model_provider=self.model.provider_label,
            model_name=self.model.model_label,
            input_summary=self._input_summary(context, trigger),
            engine=self.ENGINE,
            planner_mode="openai_agents_sdk",
        )
        step = 0
        pending_actions: dict[str, dict[str, Any]] = {}

        def trace(step_type: str, title: str, metadata: dict[str, Any]) -> None:
            nonlocal step
            step += 1
            self.runs.add_trace(
                run_id,
                step,
                step_type,
                title,
                summary=self._trace_summary(step_type, metadata),
                metadata=metadata,
            )
            call_id = str(metadata.get("tool_call_id") or "")
            if step_type == "tool_call" and call_id:
                pending_actions[call_id] = metadata
            elif step_type == "observation" and call_id:
                action = pending_actions.get(call_id, {})
                self.runs.save_observation(
                    run_id,
                    tool_call_id=call_id,
                    tool_name=str(metadata.get("tool") or action.get("tool") or ""),
                    server_name=str(
                        metadata.get("server")
                        or action.get("server")
                        or metadata.get("kind")
                        or "local"
                    ),
                    status=(
                        "duplicate"
                        if metadata.get("duplicate")
                        else "ok"
                        if metadata.get("ok", True)
                        else "failed"
                    ),
                    arguments_summary=action.get("arguments") or {},
                    result_summary=str(metadata.get("summary") or ""),
                    evidence_refs=metadata.get("evidence_refs") or [],
                    duration_ms=int(metadata.get("duration_ms") or 0),
                    fact_count=int(metadata.get("fact_count") or 0),
                    novel_evidence_count=int(
                        metadata.get("novel_evidence_count") or 0
                    ),
                    empty_result=bool(metadata.get("empty_result")),
                )
            elif step_type == "tool_utility" and call_id:
                self.runs.save_observation_utility(
                    run_id,
                    tool_call_id=call_id,
                    used_evidence_count=int(
                        metadata.get("used_evidence_count") or 0
                    ),
                    utility=str(metadata.get("utility") or "unknown"),
                )

        try:
            result, metrics = await self.agent.run(
                opportunity_id,
                trigger=trigger,
                trace=trace,
            )
            if not result.accepted or result.decision is None:
                self.runs.finish(
                    run_id,
                    status="failed",
                    error="；".join(result.errors),
                    output_summary="CommitGate 拒绝写入机会投影。",
                    planner_mode=self._planner_mode(metrics),
                    tool_call_count=int(metrics.get("tool_call_count") or 0),
                    boss_tool_call_count=int(metrics.get("boss_tool_call_count") or 0),
                    llm_call_count=int(metrics.get("iterations") or 0),
                    prompt_tokens=int(metrics.get("prompt_tokens") or 0),
                    completion_tokens=int(metrics.get("completion_tokens") or 0),
                )
                return self._response(run_id, result, metrics)

            self.committer.commit(
                opportunity_id,
                result,
                conversation_id=conversation_id,
            )
            self.runs.finish(
                run_id,
                status="needs_review" if result.status == "needs_review" else "ok",
                output_summary=result.decision["summary"],
                confidence=float(result.decision["confidence"]),
                planner_mode=self._planner_mode(metrics),
                tool_call_count=int(metrics.get("tool_call_count") or 0),
                boss_tool_call_count=int(metrics.get("boss_tool_call_count") or 0),
                llm_call_count=int(metrics.get("iterations") or 0),
                prompt_tokens=int(metrics.get("prompt_tokens") or 0),
                completion_tokens=int(metrics.get("completion_tokens") or 0),
            )
            return self._response(run_id, result, metrics)
        except Exception as exc:
            self.runs.finish(
                run_id,
                status="failed",
                error=str(exc),
                output_summary="Opportunity Agent 执行失败。",
            )
            raise

    def _commit_routed_projection(
        self,
        opportunity_id: str,
        *,
        conversation_id: str | None,
        route: DecisionRoute,
    ) -> dict[str, Any]:
        decision = route.decision or {}
        action = str((decision.get("next") or {}).get("action") or "wait")
        attachment_repair = action == "send_material"
        run_id = self.runs.create(
            "opportunity",
            opportunity_id,
            conversation_id=conversation_id,
            opportunity_id=opportunity_id,
            input_summary=(
                "没有真实 HR 回复，但平台明确要求修正附件后重发。"
                if attachment_repair
                else "首次/增量证据中没有真实 HR 回复。"
            ),
            engine="decision_router_v1",
            planner_mode="deterministic_router",
        )
        valid_refs = set(decision.get("evidence") or [])
        result = CommitGate().validate(
            decision,
            valid_evidence_refs=valid_refs,
            preserve_discovered_stage=attachment_repair,
        )
        self.runs.add_trace(
            run_id,
            1,
            "delta_router",
            (
                "识别附件发送失败"
                if attachment_repair
                else "识别单向冷启动会话"
            ),
            summary=(
                "平台明确要求修正附件，跳过 LLM 并创建重发建议。"
                if attachment_repair
                else "没有真实 HR 回复，跳过 LLM 并静默等待。"
            ),
            metadata={
                "routing_reason": (
                    "candidate_attachment_rejected_by_platform"
                    if attachment_repair
                    else "candidate_only_without_real_hr_reply"
                ),
                "route_reason": route.reason,
                "llm_skipped": True,
            },
        )
        if not result.accepted:
            self.runs.finish(
                run_id,
                status="failed",
                error="；".join(result.errors),
                output_summary="DecisionRouter 输出未通过 CommitGate。",
            )
            return self._response(
                run_id,
                result,
                {
                    "iterations": 0,
                    "llm_call_count": 0,
                    "tool_call_count": 0,
                    "duration_ms": 0,
                },
            )
        self.committer.commit(
            opportunity_id,
            result,
            conversation_id=conversation_id,
        )
        self.runs.add_trace(
            run_id,
            2,
            "commit_write",
            "写入冷启动机会投影",
            summary=(
                "机会保持已发现，创建附件修正和重发建议。"
                if attachment_repair
                else "机会保持已发现，不创建任务或草稿。"
            ),
            metadata={"stage": "discovered", "next_action": action},
        )
        self.runs.finish(
            run_id,
            status="ok",
            output_summary=decision["summary"],
            confidence=float(decision["confidence"]),
            planner_mode="deterministic_router",
            llm_call_count=0,
            tool_call_count=0,
            prompt_tokens=0,
            completion_tokens=0,
        )
        return self._response(
            run_id,
            result,
            {
                "iterations": 0,
                "llm_call_count": 0,
                "tool_call_count": 0,
                "duration_ms": 0,
                "final_repair_count": 0,
                "routing_mode": "cold_projection",
            },
        )

    def _toolbox(self, opportunity_id: str) -> ApplyToolbox:
        return ApplyToolbox(
            store=self.store,
            opportunity_id=opportunity_id,
            mcp_client=self._mcp_client(self.mcp_env),
        )

    @staticmethod
    def _mcp_client(env: dict[str, str] | None = None) -> MCPToolClient:
        return create_apply_mcp_client(env)

    @staticmethod
    def _input_summary(
        context: dict[str, Any],
        trigger: dict[str, Any] | None,
    ) -> str:
        return (
            f"机会 {context.get('opportunity', {}).get('title') or '待补全岗位'}；"
            f"新增证据 {len((trigger or {}).get('new_message_ids') or [])} 条；"
            f"触发类型 {(trigger or {}).get('type') or 'manual'}。"
        )

    @staticmethod
    def _trace_summary(step_type: str, metadata: dict[str, Any]) -> str:
        if step_type == "tool_call":
            return f"Agent 选择调用 {metadata.get('tool')}。"
        if step_type == "observation":
            return str(metadata.get("summary") or "工具已返回受控观察。")
        if step_type == "tool_utility":
            labels = {
                "evidence_used": "新增证据进入最终决策",
                "rule_context": "领域规则参与推理",
                "routing_context": "能力目录促成按需规则加载",
                "catalog_only": "仅查看能力目录，未加载规则正文",
                "novel_evidence_unused": "获得新证据但最终未引用",
                "duplicate_context": "没有增加新证据",
                "empty": "工具返回空结果",
            }
            return labels.get(
                str(metadata.get("utility") or ""),
                "工具价值已评估。",
            )
        if step_type == "commit_gate":
            return (
                "CommitGate 允许写入。"
                if metadata.get("accepted")
                else "CommitGate 拒绝写入。"
            )
        if step_type == "final_decision":
            return "Agent 已生成精简语义决策。"
        return str(metadata.get("summary") or metadata.get("opportunity_id") or "")

    @staticmethod
    def _planner_mode(metrics: dict[str, Any]) -> str:
        modes = {
            str(mode)
            for mode in metrics.get("response_modes") or []
            if mode and mode != "unknown"
        }
        if "json_fallback" in modes:
            return "openai_agents_sdk+json_fallback"
        if "json_schema" in modes:
            return "openai_agents_sdk+json_schema"
        return "openai_agents_sdk"

    @staticmethod
    def _response(
        run_id: str,
        result: CommitResult,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": result.status,
            "accepted": result.accepted,
            "decision": result.decision,
            "errors": result.errors,
            "warnings": result.warnings,
            "metrics": metrics,
        }
