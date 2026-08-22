import type { ReactNode } from "react";
import {
  Activity,
  Check,
  ChevronRight,
  Database,
  FileText,
  LogIn,
  RefreshCcw,
  Search,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import type {
  ApplyAgentRunsPayload,
  ApplyEvidencePayload,
  ApplyHealthPayload,
  ApplyImportProgressPayload,
  ApplyJob,
  ApplyOpportunity,
  ApplyOverviewPayload,
  ApplyProfilePayload,
  ApplyTasksPayload,
} from "@/lib/apply-api";

const STAGE_LABELS: Record<string, string> = {
  discovered: "已发现",
  communicating: "已沟通",
  need_my_action: "待我行动",
  waiting_feedback: "待对方反馈",
  interviewing: "面试中",
  closed: "结束",
};

const STATUS_LABELS: Record<string, string> = {
  suggested: "待确认",
  pending: "待确认",
  accepted: "已接受",
  rejected: "已拒绝",
  edited: "已编辑",
  done: "已完成",
  deferred: "已延期",
  ignored: "已忽略",
};

const REVIEW_LABELS: Record<string, string> = {
  task: "任务建议",
  stage: "阶段建议",
  draft: "回复草稿",
  risk: "风险提示",
};

const SOURCE_QUALITY_LABELS: Record<string, string> = {
  real_recruiter_reply: "真实 HR 已回应",
  cold_outreach_vip_no_reply: "VIP 追聊后仍无真实回应",
  cold_outreach_no_reply: "仅我方自荐，暂无真实回应",
  job_card: "岗位卡来源",
  unknown: "来源待确认",
};

export const APPLY_PRIMARY_BUTTON = "bg-[#1f2329] text-white hover:bg-[#34383e]";
export const APPLY_OUTLINE_BUTTON = "border-[#c9cdd4] bg-white text-[#1f2329] hover:bg-[#f2f3f5] hover:text-[#1f2329]";
export function HealthPanel({ health }: { health: ApplyHealthPayload | null }) {
  if (!health || health.can_view !== false) return null;
  return (
    <Banner tone="red">
      {String(health.message ?? "Apply 依赖未就绪。请先运行 docker compose up -d postgres redis，并启动 capybot apply worker。")}
    </Banner>
  );
}

export function OverviewPage({ overview, onOpenOpportunity }: { overview: ApplyOverviewPayload | null; onOpenOpportunity: (id: string) => void }) {
  const metrics = overview?.metrics ?? {};
  const actions = (overview?.action_items ?? []) as Array<Record<string, unknown>>;
  const profileReady = Boolean(overview?.profile_ready);
  const latestReport = parseReport(overview?.latest_import?.report);
  return (
    <div className="space-y-4">
      <div className="rounded border border-[#d9dce1] bg-white px-3 py-2 text-sm text-[#4e5969]">
        本地事实库长期保留历史导入，目前共有 {String(metrics.opportunities ?? 0)} 个历史机会。
        最近一次近 30 天快照扫描 {String(latestReport?.scanned_conversations ?? 0)} 个会话，
        新增 {String(latestReport?.new_messages ?? 0)} 条消息；当前 BOSS 列表为空不会删除历史记录。
      </div>
      <DeltaPanel panel={overview?.recent_delta_panel as Record<string, unknown> | null | undefined} />
      <RebuildPanel progress={overview?.rebuild_progress as Record<string, unknown> | null | undefined} />
      <div className="grid gap-3 md:grid-cols-6">
        <Metric label="历史机会" value={metrics.opportunities ?? 0} />
        <Metric label="待我行动" value={metrics.need_my_action ?? 0} tone="green" />
        <Metric label="待反馈" value={metrics.waiting_feedback ?? 0} />
        <Metric label="面试中" value={metrics.interviewing ?? 0} />
        <Metric label="待确认" value={metrics.pending_reviews ?? 0} />
        <Metric label="任务" value={metrics.active_tasks ?? 0} />
      </div>
      <Section title="优先行动">
        <div className="divide-y divide-[#e5e6eb]">
          {actions.slice(0, 12).map((item, index) => {
            const opp = item.opportunity as ApplyOpportunity | undefined;
            return (
              <button key={index} className="flex w-full items-center justify-between py-3 text-left hover:bg-[#f7f8fa]" onClick={() => opp?.id && onOpenOpportunity(opp.id)}>
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium">{String(item.title ?? "暂无行动建议")}</div>
                  {opp ? <div className="mt-1 text-xs text-[#86909c]">{opp.company ?? "未知公司"} · {opp.title}</div> : null}
                </div>
                <ChevronRight className="h-4 w-4 text-[#c9cdd4]" />
              </button>
            );
          })}
          {!actions.length ? <Empty label="暂无需要你立即处理的行动。等待反馈和普通观察项不会在这里重复打扰。" /> : null}
        </div>
      </Section>
      <div className="grid gap-4 xl:grid-cols-2">
        {profileReady ? (
          <OpportunityList title="高契合岗位" items={(overview?.top_job_fits ?? []) as ApplyOpportunity[]} onOpen={onOpenOpportunity} scoreKey="job_fit_score" />
        ) : (
          <Section title="高契合岗位">
            <Empty label="请先在简历页上传 PDF 或粘贴 Markdown 简历，并填写求职偏好，系统才会计算岗位契合度。" />
          </Section>
        )}
        {profileReady ? (
          <OpportunityList title="高优先级机会" items={(overview?.high_priority_opportunities ?? []) as ApplyOpportunity[]} onOpen={onOpenOpportunity} scoreKey="opportunity_priority_score" />
        ) : null}
        <OpportunityList title="风险机会" items={(overview?.risk_opportunities ?? []) as ApplyOpportunity[]} onOpen={onOpenOpportunity} />
      </div>
    </div>
  );
}

function DeltaPanel({ panel }: { panel?: Record<string, unknown> | null }) {
  const summary = (panel?.summary ?? {}) as Record<string, unknown>;
  const items = (panel?.items ?? []) as Array<Record<string, unknown>>;
  if (!panel) return null;
  return (
    <Section title="本次导入变化">
      <div className="grid gap-3 md:grid-cols-5">
        <Metric label="新增消息" value={String(summary.new_messages ?? 0)} tone="green" />
        <Metric label="变化会话" value={String(summary.changed_conversations ?? 0)} />
        <Metric label="跳过未变" value={String(summary.skipped_conversations ?? 0)} />
        <Metric label="已分析机会" value={String(summary.analyzed_opportunities ?? 0)} />
        <Metric label="排队机会" value={String(summary.queued_opportunities ?? 0)} />
      </div>
      {items.length ? (
        <div className="mt-3 divide-y divide-[#e5e6eb] rounded border border-[#e5e6eb] bg-white">
          {items.slice(0, 8).map((item, index) => (
            <div key={index} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
              <div className="min-w-0">
                <div className="truncate font-medium">
                  {analysisModeLabel(String(item.analysis_mode ?? "skipped"))}
                </div>
                <div className="truncate text-xs text-[#86909c]">
                  新增 {String(item.new_message_count ?? 0)} 条
                  {item.before_stage || item.after_stage ? ` · ${stageLabel(String(item.before_stage ?? "未知"))} -> ${stageLabel(String(item.after_stage ?? item.before_stage ?? "未知"))}` : ""}
                </div>
              </div>
              {item.skipped_reason ? (
                <span className="text-xs text-[#86909c]">
                  {skippedReasonLabel(String(item.skipped_reason))}
                </span>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </Section>
  );
}

function RebuildPanel({ progress }: { progress?: Record<string, unknown> | null }) {
  if (!progress || progress.status === "idle") return null;
  const percent = Number(progress.percent ?? 0);
  const failures = (progress.failures ?? []) as Array<Record<string, unknown>>;
  return (
    <Section title="Opportunity Agent 重建">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-medium">{String(progress.message ?? "正在重建旧派生分析")}</div>
          <div className="mt-1 text-xs text-[#86909c]">
            Tool Loop 会重新生成阶段、任务、草稿、摘要、证据和 Agent trace。
          </div>
        </div>
        <StatusPill label={String(progress.status ?? "running")} tone={progress.status === "ok" ? "green" : "gray"} />
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-[#f2f3f5]">
        <div className="h-full rounded-full bg-[#00a66a] transition-all" style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} />
      </div>
      <div className="mt-2 text-xs text-[#86909c]">
        {progress.total ? `进度 ${String(progress.current ?? 0)}/${String(progress.total)}` : "等待扫描本地会话"}
      </div>
      {failures.length ? (
        <div className="mt-3 space-y-1">
          {failures.slice(-3).map((failure, index) => (
            <div key={index} className="rounded bg-[#fff1f0] px-2 py-1 text-xs text-[#f53f3f]">
              {String(failure.opportunity_id ?? "未知机会")}：{String(failure.error ?? "重建失败")}
            </div>
          ))}
        </div>
      ) : null}
    </Section>
  );
}

function JobsPanel({ jobs }: { jobs: ApplyJob[] }) {
  const active = jobs.filter((job) => job.status === "queued" || job.status === "running");
  const recentCutoff = Date.now() - 48 * 60 * 60 * 1000;
  const recentTerminal = jobs.filter((job) => {
    if (job.status === "queued" || job.status === "running") return false;
    const updatedAt = Date.parse(String(job.updated_at ?? job.finished_at ?? job.created_at ?? ""));
    return Number.isFinite(updatedAt) && updatedAt >= recentCutoff;
  });
  const visible = [...active, ...recentTerminal].slice(0, 8);
  if (!visible.length) return null;
  return (
    <div className="mb-4 rounded border border-[#e5e6eb] bg-white p-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="text-sm font-semibold">后台任务</div>
        <div className="text-xs text-[#86909c]">仅显示运行中和近 48 小时记录</div>
      </div>
      <div className="space-y-2">
        {visible.map((job) => {
          const percent = Number(job.progress_percent ?? 0);
          return (
            <div key={job.id} className="rounded border border-[#e5e6eb] bg-[#f7f8fa] p-2">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium">{jobTypeLabel(String(job.job_type))}</div>
                  <div className="break-all text-xs text-[#86909c]">{String(job.message ?? job.status)}</div>
                  <div className="mt-1 text-xs text-[#c9cdd4]">更新于 {formatDate(job.updated_at ?? job.finished_at ?? job.created_at)}</div>
                  {job.status === "failed" && job.error ? <div className="mt-1 break-all text-xs text-[#f53f3f]">{String(job.error)}</div> : null}
                </div>
                <StatusPill label={jobStatusLabel(String(job.status))} tone={job.status === "ok" ? "green" : job.status === "failed" ? "red" : "gray"} />
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-[#e5e6eb]">
                <div className="h-full rounded-full bg-[#00a66a]" style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function OpportunitiesPage({
  opportunities,
  query,
  setQuery,
  selectedId,
  setSelectedId,
  detail,
  onEvidence,
  onReanalyze,
  onReanalyzeFit,
  onResearch,
  onRefreshBoss,
}: {
  opportunities: ApplyOpportunity[];
  query: string;
  setQuery: (value: string) => void;
  selectedId: string;
  setSelectedId: (value: string) => void;
  detail: Record<string, unknown> | null;
  onEvidence: (opportunityId: string, ids: unknown) => void;
  onReanalyze: (id: string) => void;
  onReanalyzeFit: (id: string) => void;
  onResearch: (id: string) => void;
  onRefreshBoss: (id: string) => void;
}) {
  const opportunity = detail?.opportunity as ApplyOpportunity | undefined;
  const tasks = (detail?.tasks ?? []) as Array<Record<string, unknown>>;
  const drafts = ((detail?.drafts ?? []) as Array<Record<string, unknown>>).filter(
    (draft) => !["superseded", "rejected"].includes(String(draft.status ?? "")),
  );
  const events = (detail?.events ?? []) as Array<Record<string, unknown>>;
  const jobs = (detail?.jobs ?? []) as Array<Record<string, unknown>>;
  const fitAnalysis = detail?.fit_analysis as Record<string, unknown> | null | undefined;
  return (
    <div className="grid min-h-[calc(100vh-6.5rem)] gap-4 lg:grid-cols-[420px_minmax(0,1fr)]">
      <Section title="机会管道" tight>
        <div className="mb-3 flex items-center gap-2 rounded border border-[#e5e6eb] bg-white px-2">
          <Search className="h-4 w-4 text-[#86909c]" />
          <input className="h-9 min-w-0 flex-1 bg-transparent text-sm outline-none" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索岗位、公司、阶段或下一步" />
        </div>
        <div className="max-h-[calc(100vh-11rem)] divide-y divide-[#e5e6eb] overflow-auto">
          {opportunities.map((item) => (
            <button key={item.id} className={`w-full px-2 py-3 text-left hover:bg-[#f7f8fa] ${selectedId === item.id ? "bg-[#e8fff7]" : ""}`} onClick={() => setSelectedId(item.id)}>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold">{item.title}</div>
                  <div className="mt-1 truncate text-xs text-[#86909c]">{item.company ?? "未知公司"} · {opportunityStageText(item)}</div>
                  <div className="mt-1 truncate text-xs text-[#4e5969]">{opportunityNextAction(item)}</div>
                </div>
                <Score value={Number(item.job_fit_score ?? 0)} status={String(item.fit_status ?? "")} />
              </div>
            </button>
          ))}
          {!opportunities.length ? <Empty label="还没有机会。请先导入 BOSS 聊天记录。" /> : null}
        </div>
      </Section>
      <div className="space-y-4">
        <Section title="机会详情">
          {opportunity ? (
            <div className="space-y-4">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h2 className="text-lg font-semibold">{opportunity.title}</h2>
                  <p className="mt-1 text-sm text-[#4e5969]">{opportunity.company ?? "未知公司"} · {opportunityStageText(opportunity)}</p>
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <Score value={Number(opportunity.job_fit_score ?? 0)} status={String(opportunity.fit_status ?? "")} />
                  <Button className={APPLY_OUTLINE_BUTTON} variant="outline" size="sm" onClick={() => onReanalyzeFit(opportunity.id)}>
                    <Sparkles className="mr-2 h-4 w-4" />
                    重新评分
                  </Button>
                  <Button className={APPLY_OUTLINE_BUTTON} variant="outline" size="sm" onClick={() => onResearch(opportunity.id)}>
                    <Search className="mr-2 h-4 w-4" />
                    补全岗位/公司
                  </Button>
                  <Button className={APPLY_OUTLINE_BUTTON} variant="outline" size="sm" onClick={() => onRefreshBoss(opportunity.id)}>
                    <RefreshCcw className="mr-2 h-4 w-4" />
                    刷新 BOSS 证据
                  </Button>
                  <Button className={APPLY_OUTLINE_BUTTON} variant="outline" size="sm" onClick={() => onReanalyze(opportunity.id)}>
                    <Sparkles className="mr-2 h-4 w-4" />
                    重新分析
                  </Button>
                </div>
              </div>
              <InfoGrid rows={[
                ["下一步", String(opportunity.next_action ?? "暂无")],
                ["置信度", `${Math.round(Number(opportunity.confidence ?? 0) * 100)}%`],
                ["来源质量", sourceQualityLabel(opportunity.source_quality ?? "job_card")],
                ["更新时间", formatDate(opportunity.updated_at)],
              ]} />
              <Subsection title="岗位卡">
                <div className="grid gap-2 md:grid-cols-2">
                  {jobs.map((job, index) => (
                    <div key={index} className="rounded border border-[#e5e6eb] bg-[#f7f8fa] p-3 text-sm">
                      <div className="font-medium">{String(job.title ?? "待补全岗位")}</div>
                      <div className="mt-1 text-[#4e5969]">{String(job.salary ?? "薪资未知")} · {String(job.city ?? "城市未知")} · {String(job.experience ?? "实习要求未知")}</div>
                    </div>
                  ))}
                  {!jobs.length ? <Empty label="该机会没有岗位卡，Agent 会标记低置信度。" /> : null}
                </div>
              </Subsection>
              <Subsection title="岗位契合度">
                <FitAnalysisPanel analysis={fitAnalysis} opportunity={opportunity} />
              </Subsection>
              <Subsection title="Agent 策略与任务">
                <Rows items={tasks} empty="暂无任务建议" render={(task) => (
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-medium">{String(task.title)}</div>
                      <div className="text-xs text-[#86909c]">{STATUS_LABELS[String(task.status)] ?? task.status} · 截止 {formatDate(task.due_at)}</div>
                    </div>
                    <Button variant="ghost" size="sm" onClick={() => onEvidence(opportunity.id, task.evidence_message_ids)}>证据</Button>
                  </div>
                )} />
              </Subsection>
              <Subsection title="回复草稿">
                <Rows items={drafts} empty="暂无回复草稿" render={(draft) => (
                  <div>
                    <p className="whitespace-pre-wrap text-sm">{String(draft.content)}</p>
                    <div className="mt-2 flex items-center justify-between text-xs text-[#86909c]">
                      <span>{String(draft.reason ?? "只生成草稿，不自动发送")}</span>
                      <Button variant="ghost" size="sm" onClick={() => onEvidence(opportunity.id, draft.evidence_message_ids)}>证据</Button>
                    </div>
                  </div>
                )} />
              </Subsection>
              <Subsection title="进度事件">
                <Rows items={events} empty="暂无结构化事件" render={(event) => (
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-medium">{String(event.title)}</div>
                      <div className="text-xs text-[#86909c]">{String(event.event_type)} · {String(event.detail ?? "")}</div>
                    </div>
                    <Button variant="ghost" size="sm" onClick={() => onEvidence(opportunity.id, event.evidence_message_ids)}>证据</Button>
                  </div>
                )} />
              </Subsection>
            </div>
          ) : (
            <Empty label="请选择一个机会查看 Agent 分析详情。" />
          )}
        </Section>
      </div>
    </div>
  );
}

export function TasksPage({ tasks, onSuggestion, onEvidence }: { tasks: ApplyTasksPayload | null; onSuggestion: (id: string, status: "accepted" | "rejected") => void; onEvidence: (opportunityId: string, ids: unknown) => void }) {
  const rows = tasks?.tasks ?? [];
  const suggestions = tasks?.suggestions ?? [];
  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_420px]">
      <Section title="任务与提醒">
        <Rows items={rows} empty="暂无任务。Agent 分析后会在这里展示待确认、今日、逾期和等待反馈任务。" render={(task) => (
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-medium">{String(task.title)}</div>
              <div className="text-xs text-[#86909c]">{String(task.opportunity_company ?? "未知公司")} · {String(task.opportunity_title ?? "未知岗位")} · {STATUS_LABELS[String(task.status)] ?? task.status}</div>
            </div>
            <Button variant="ghost" size="sm" onClick={() => onEvidence(String(task.opportunity_id ?? ""), task.evidence_message_ids)}>证据</Button>
          </div>
        )} />
      </Section>
      <Section title="Agent 建议确认">
        <Rows items={suggestions} empty="暂无待确认建议。" render={(suggestion) => (
          <div>
            <div className="text-xs font-medium text-[#00a66a]">{REVIEW_LABELS[String(suggestion.kind)] ?? String(suggestion.kind)}</div>
            <div className="mt-1 text-sm font-medium">{String(suggestion.content ?? suggestion.title)}</div>
            <div className="mt-1 text-xs text-[#86909c]">
              {String(suggestion.opportunity_company ?? "未知公司")} · {String(suggestion.opportunity_title ?? "未知岗位")}
            </div>
            <div className="mt-3 flex gap-2">
              <Button className={APPLY_OUTLINE_BUTTON} size="sm" variant="outline" onClick={() => onSuggestion(String(suggestion.id), "accepted")}>
                <Check className="mr-2 h-4 w-4" />
                接受
              </Button>
              <Button size="sm" variant="ghost" onClick={() => onSuggestion(String(suggestion.id), "rejected")}>
                <X className="mr-2 h-4 w-4" />
                拒绝
              </Button>
              <Button size="sm" variant="ghost" onClick={() => onEvidence(String(suggestion.opportunity_id ?? ""), suggestion.evidence_message_ids)}>
                证据
              </Button>
            </div>
          </div>
        )} />
      </Section>
    </div>
  );
}

export function AgentPage({ runs, selectedRun, onOpenRun, onReanalyzeAll, jobs }: { runs: ApplyAgentRunsPayload | null; selectedRun: ApplyAgentRunsPayload | null; onOpenRun: (id: string) => void; onReanalyzeAll: () => void; jobs: ApplyJob[] }) {
  const runRows = runs?.runs ?? [];
  const steps = selectedRun?.steps ?? [];
  const selectedRunId = String(selectedRun?.run?.id ?? "");
  const allMetrics = runs?.metrics ?? {};
  const metrics = allMetrics;
  const duration = (metrics.duration_ms ?? {}) as Record<string, unknown>;
  const toolUtility = asRecord(metrics.tool_utility);
  return (
    <div className="grid min-h-[calc(100vh-6.5rem)] gap-4 lg:grid-cols-[420px_minmax(0,1fr)]">
      <Section title="Agent 运行">
        <div className="mb-3 flex justify-end gap-2">
          <Button className={APPLY_PRIMARY_BUTTON} size="sm" onClick={onReanalyzeAll}>
            <Sparkles className="mr-2 h-4 w-4" />
            批量重分析
          </Button>
        </div>
        <div className="mb-4 grid grid-cols-2 gap-2">
          <Metric label="新引擎运行样本" value={String(metrics.sample_size ?? 0)} />
          <Metric label="有效 / 完成率" value={`${formatPercent(metrics.success_rate)} / ${formatPercent(metrics.completion_rate)}`} tone="green" />
          <Metric label="P50 / P95 耗时" value={`${formatDuration(duration.p50)} / ${formatDuration(duration.p95)}`} />
          <Metric label="工具调用" value={String(metrics.tool_calls ?? 0)} />
          <Metric label="有效工具率" value={formatPercent(toolUtility.useful_rate)} tone="green" />
          <Metric label="采用新证据" value={String(toolUtility.used_evidence ?? 0)} />
        </div>
        <div className="max-h-[calc(100vh-22rem)] overflow-y-auto pr-1">
          <Rows items={runRows} empty="暂无 Agent 运行记录。" render={(run) => (
            <button
              className={`w-full text-left ${selectedRunId === String(run.id) ? "text-[#00875a]" : ""}`}
              onClick={() => onOpenRun(String(run.id))}
            >
              <div className="text-sm font-medium">{String(run.output_summary ?? run.input_summary ?? "Agent 分析")}</div>
              {run.opportunity_title ? (
                <div className="mt-1 text-xs text-[#4e5969]">
                  {String(run.opportunity_company ?? "未知公司")} · {String(run.opportunity_title)}
                </div>
              ) : null}
              <div className="mt-1 text-xs text-[#86909c]">{String(run.status)} · {String(run.engine ?? "unknown")} · {formatDuration(run.duration_ms)} · {formatDate(run.started_at)}</div>
            </button>
          )} />
        </div>
      </Section>
      <div className="space-y-4">
        <Section title="运行轨迹">
          <div className="max-h-[calc(100vh-13rem)] overflow-y-auto pr-1">
            <Rows items={steps} empty="选择一次 Agent 运行，查看新增证据、工具选择、Observation 和 CommitGate 轨迹。" render={(step) => (
              <details className="group">
                <summary className="cursor-pointer list-none">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-medium">{String(step.step_index)}. {String(step.title)}</div>
                      <div className="text-xs text-[#86909c]">{String(step.step_type)} · {String(step.summary ?? "")}</div>
                    </div>
                    <Activity className="h-4 w-4 text-[#00a66a]" />
                  </div>
                </summary>
                <TraceStepDetails step={step} />
              </details>
            )} />
          </div>
        </Section>
        <JobsPanel jobs={jobs.filter((job) => String(job.job_type).includes("analyze") || String(job.job_type).includes("rebuild"))} />
      </div>
    </div>
  );
}

function TraceStepDetails({ step }: { step: Record<string, unknown> }) {
  const metadata = asRecord(step.metadata);
  const decision = asRecord(metadata.decision);
  const next = asRecord(decision.next);
  const tools = Array.isArray(metadata.selected_tools) ? metadata.selected_tools.map(String) : [];
  const refs = Array.isArray(metadata.evidence_refs) ? metadata.evidence_refs.map(String) : [];
  return (
    <div className="mt-3 space-y-3 rounded bg-[#f7f8fa] p-3 text-xs text-[#4e5969]">
      {String(step.step_type) === "bootstrap" ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Metric label="新增消息" value={String(metadata.delta_count ?? 0)} />
          <Metric label="首轮可见" value={String(metadata.visible_delta_count ?? metadata.delta_count ?? 0)} />
          <Metric label="是否截断" value={metadata.truncated ? "是" : "否"} />
          <Metric label="触发方式" value={String(metadata.trigger_type ?? "manual")} />
        </div>
      ) : null}
      {String(step.step_type) === "planner" ? (
        <p>{tools.length ? `Agent 选择：${tools.join("、")}` : metadata.has_final ? "证据已经足够，Agent 直接生成最终决策。" : "Agent 尚未形成最终决策。"}</p>
      ) : null}
      {String(step.step_type) === "tool_call" ? (
        <div className="flex flex-wrap gap-2">
          <Tag>{String(metadata.kind ?? "tool")}</Tag>
          <Tag>{String(metadata.tool ?? "未知工具")}</Tag>
          {metadata.server ? <Tag>{String(metadata.server)}</Tag> : null}
        </div>
      ) : null}
      {String(step.step_type) === "observation" ? (
        <div className="space-y-2">
          <p>{String(metadata.summary ?? "工具已返回结果。")}</p>
          <p>获得事实 {String(metadata.fact_count ?? 0)} 条 · 新证据 {String(metadata.novel_evidence_count ?? 0)} 条 · 耗时 {formatDuration(metadata.duration_ms)} · 时效 {String(metadata.freshness ?? "unknown")}</p>
          {refs.length ? <div className="flex flex-wrap gap-1">{refs.map((ref) => <Tag key={ref}>{ref}</Tag>)}</div> : null}
        </div>
      ) : null}
      {String(step.step_type) === "tool_utility" ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Metric label="价值判断" value={toolUtilityLabel(metadata.utility)} />
          <Metric label="返回事实" value={String(metadata.fact_count ?? 0)} />
          <Metric label="新增证据" value={String(metadata.novel_evidence_count ?? 0)} />
          <Metric label="最终采用" value={String(metadata.used_evidence_count ?? 0)} />
        </div>
      ) : null}
      {String(step.step_type) === "final_decision" && Object.keys(decision).length ? (
        <div className="grid gap-2 sm:grid-cols-2">
          <Metric label="阶段" value={STAGE_LABELS[String(decision.stage)] ?? String(decision.stage ?? "-")} />
          <Metric label="下一步" value={String(next.action ?? "-")} />
          <div className="sm:col-span-2">{String(decision.summary ?? "")}</div>
        </div>
      ) : null}
      {String(step.step_type) === "commit_gate" ? (
        <p className={metadata.accepted ? "text-[#00875a]" : "text-[#d92d20]"}>
          {metadata.accepted ? "证据与安全校验通过，可以提交。" : `校验拒绝：${String((metadata.errors as unknown[])?.join("；") ?? "")}`}
        </p>
      ) : null}
      <details>
        <summary className="cursor-pointer text-[#86909c]">查看原始步骤元数据</summary>
        <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap">{formatJson(metadata)}</pre>
      </details>
    </div>
  );
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function Tag({ children }: { children: ReactNode }) {
  return <span className="rounded border border-[#dfe3e8] bg-white px-2 py-1 text-[11px] text-[#4e5969]">{children}</span>;
}

export function ProfilePage({
  profile,
  profileDraft,
  setProfileDraft,
  prefDraft,
  setPrefDraft,
  onSave,
  onUploadPdf,
  uploading,
}: {
  profile: ApplyProfilePayload | null;
  profileDraft: string;
  setProfileDraft: (value: string) => void;
  prefDraft: Record<string, string>;
  setPrefDraft: (value: any) => void;
  onSave: () => void;
  onUploadPdf: (file: File) => void;
  uploading: boolean;
}) {
  const tags = profile?.profile;
  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_420px]">
      <Section title="候选人简历">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded border border-dashed border-[#c9cdd4] bg-[#f7f8fa] p-3">
          <div>
            <div className="text-sm font-medium text-[#1f2329]">上传 PDF 简历</div>
            <div className="text-xs text-[#86909c]">系统会优先使用 PaddleOCR 识别扫描件，并转成 Markdown 后写入候选人画像。</div>
          </div>
          <label className="inline-flex h-9 cursor-pointer items-center rounded bg-[#1f2329] px-3 text-sm text-white hover:bg-[#2f3338]">
            <Upload className="mr-2 h-4 w-4" />
            {uploading ? "解析中..." : "选择 PDF"}
            <input
              type="file"
              accept="application/pdf,.pdf"
              className="hidden"
              disabled={uploading}
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = "";
                if (file) onUploadPdf(file);
              }}
            />
          </label>
        </div>
        <textarea className="min-h-[460px] w-full rounded border border-[#e5e6eb] bg-white p-3 text-sm outline-none focus:border-[#00a66a]" value={profileDraft} onChange={(e) => setProfileDraft(e.target.value)} placeholder="粘贴 Markdown 简历。Agent 会从这里生成候选人画像、技能标签和项目标签。" />
        <div className="mt-3 flex justify-end">
          <Button className={APPLY_PRIMARY_BUTTON} onClick={onSave}>
            <FileText className="mr-2 h-4 w-4" />
            保存并生成画像
          </Button>
        </div>
        <div className="mt-2 text-xs text-[#86909c]">保存后会自动把所有已导入机会标记为待重算，并在后台重新生成岗位契合度。</div>
      </Section>
      <div className="space-y-4">
        <Section title="求职偏好">
          {Object.entries({
            target_roles: "目标岗位",
            cities: "城市/远程",
            salary: "期望薪资",
            internship_time: "实习时间",
            excluded: "排除公司/行业",
          }).map(([key, label]) => (
            <label key={key} className="mb-3 block text-sm">
              <span className="mb-1 block text-xs text-[#86909c]">{label}</span>
              <input className="h-9 w-full rounded border border-[#e5e6eb] bg-white px-2 text-[#1f2329] outline-none focus:border-[#00a66a]" value={prefDraft[key] ?? ""} onChange={(e) => setPrefDraft((prev: Record<string, string>) => ({ ...prev, [key]: e.target.value }))} />
            </label>
          ))}
        </Section>
        <Section title="候选人画像">
          <p className="mb-3 text-sm text-[#4e5969]">{redactUiText(String(tags?.profile_summary ?? "尚未生成画像。"))}</p>
          <TagGroup title="技能标签" values={tags?.skill_tags} />
          <TagGroup title="项目标签" values={tags?.project_tags} />
          <TagGroup title="Agent 标签" values={tags?.agent_tags} />
        </Section>
      </div>
    </div>
  );
}

export function ImportPage({
  overview,
  bossStatus,
  importProgress,
  onImport,
  onDemo,
  onLogin,
  onClearDerived,
  onClearAll,
  jobs,
}: {
  overview: ApplyOverviewPayload | null;
  bossStatus: Record<string, unknown> | null;
  importProgress: ApplyImportProgressPayload | null;
  onImport: () => void;
  onDemo: () => void;
  onLogin: () => void;
  onClearDerived: () => void;
  onClearAll: () => void;
  jobs: ApplyJob[];
}) {
  const report = parseReport(overview?.latest_import?.report);
  const account = (overview?.current_account ?? bossStatus?.account) as Record<string, unknown> | undefined;
  const failures = Array.isArray(report?.failures) ? report.failures as Array<Record<string, unknown>> : [];
  const rebuildJob = jobs.find((job) => job.job_type === "rebuild_derived_from_l1");
  const rebuildProgress = (overview?.rebuild_progress ?? (rebuildJob ? {
    status: rebuildJob.status,
    current: rebuildJob.progress_current,
    total: rebuildJob.progress_total,
    percent: rebuildJob.progress_percent,
    message: rebuildJob.message,
    error: rebuildJob.error,
  } : null)) as Record<string, unknown> | null | undefined;
  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_420px]">
      <Section title="BOSS 导入">
        {account?.source === "demo_fixture" ? (
          <Banner tone="green">
            当前展示隔离中文演示账号。它使用生产导入、Agent 和评分链路，但不会修改任何真实 BOSS 账号数据。
          </Banner>
        ) : null}
        {!bossStatus?.logged_in && bossStatus?.profile_ready && !bossStatus?.cdp_alive ? (
          <div className="mb-4 rounded border border-[#d9dce1] bg-[#f7f8fa] px-3 py-2 text-sm text-[#4e5969]">
            登录凭证仍保存在本机，但 Capybot 专用浏览器当前未启动。点击“打开 BOSS 专用浏览器”通常会自动恢复登录；只有跳转到登录页时才需要重新扫码。
          </div>
        ) : null}
        {!bossStatus?.logged_in && bossStatus?.cdp_alive ? (
          <Banner tone="red">专用浏览器已启动，但聊天页不可读。请在该窗口完成登录并保持 BOSS 聊天页打开。</Banner>
        ) : null}
        <div className="mb-4 grid gap-3 md:grid-cols-3">
          <Metric label="登录状态" value={bossStatusLabel(bossStatus)} />
          <Metric label="专用浏览器" value={bossStatus?.cdp_alive ? "运行中" : "未启动"} />
          <Metric label="只读边界" value="不发送消息" tone="green" />
        </div>
        <div className="mb-4 rounded border border-[#e5e6eb] bg-[#f7f8fa] p-3 text-sm">
          <div className="font-medium">本地账号档案</div>
          <div className="mt-1 text-[#4e5969]">
            {String(account?.display_name ?? "BOSS 本地账号")} · {String(account?.account_uid ?? "等待首次导入")}
          </div>
          <div className="mt-1 text-xs text-[#86909c]">
            同一个独立浏览器 Profile 会复用同一份本地历史记录；BOSS 当前列表为空不会清除 PostgreSQL 中已经导入的数据。
          </div>
        </div>
        {importProgress ? <ImportProgressPanel progress={importProgress} /> : null}
        <RebuildPanel progress={rebuildProgress} />
        <JobsPanel jobs={jobs.filter((job) => ["import_boss_snapshot", "rebuild_derived_from_l1"].includes(String(job.job_type)))} />
        <div className="flex flex-wrap gap-2">
          <Button className={APPLY_OUTLINE_BUTTON} variant="outline" onClick={onDemo}>
            <Sparkles className="mr-2 h-4 w-4" />
            一键中文演示
          </Button>
          <Button className={APPLY_OUTLINE_BUTTON} variant="outline" onClick={onLogin}>
            <LogIn className="mr-2 h-4 w-4" />
            {bossLoginButtonLabel(bossStatus)}
          </Button>
          <Button className={APPLY_PRIMARY_BUTTON} onClick={onImport} disabled={importProgress?.status === "running"}>
            <Database className="mr-2 h-4 w-4" />
            {importProgress?.status === "running" ? "正在导入" : "导入近 30 天聊天"}
          </Button>
          <Button className={APPLY_OUTLINE_BUTTON} variant="outline" onClick={onClearDerived}>
            <RefreshCcw className="mr-2 h-4 w-4" />
            清空派生分析
          </Button>
          <Button className="bg-[#f53f3f] text-white hover:bg-[#d9363e]" variant="destructive" onClick={onClearAll}>
            <Trash2 className="mr-2 h-4 w-4" />
            清空全部数据和登录态
          </Button>
        </div>
      </Section>
      <Section title="最近导入报告">
        {report ? (
          <InfoGrid rows={[
            ["扫描会话", String(report.scanned_conversations ?? 0)],
            ["成功会话", String(report.successful_conversations ?? 0)],
            ["失败会话", String(report.failed_conversations ?? 0)],
            ["新增消息", String(report.new_messages ?? 0)],
            ["新增岗位", String(report.new_jobs ?? 0)],
            ["变化会话", String(report.changed_conversations ?? 0)],
            ["跳过未变", String(report.skipped_conversations ?? 0)],
            ["已分析机会", String(report.analyzed_opportunities ?? 0)],
            ["排队机会", String(report.queued_opportunities ?? 0)],
            ["完成时间", formatDate(report.finished_at)],
          ]} />
        ) : <Empty label="还没有导入报告。" />}
        {failures.length ? (
          <div className="mt-4">
            <div className="mb-2 text-sm font-medium">失败会话</div>
            <Rows items={failures} empty="暂无失败会话" render={(failure) => (
              <div className="text-sm">
                <div className="font-medium">{String(failure.contact ?? failure.conversation_id ?? "未知会话")}</div>
                <div className="mt-1 break-words text-xs text-[#f53f3f]">{String(failure.error ?? "获取失败")}</div>
              </div>
            )} />
          </div>
        ) : null}
      </Section>
    </div>
  );
}

export function ImportProgressBanner({ progress }: { progress: ApplyImportProgressPayload }) {
  return (
    <Banner tone="green">
      {String(progress.phase ?? "导入中")}：{String(progress.message ?? "正在导入 BOSS 聊天记录。")}
      {progress.total ? `（${progress.current ?? 0}/${progress.total}）` : ""}
    </Banner>
  );
}

function ImportProgressPanel({ progress }: { progress: ApplyImportProgressPayload }) {
  const percent = Number(progress.percent ?? 0);
  const failures = (progress.failures ?? []) as Array<Record<string, unknown>>;
  const tone = progress.status === "failed" ? "text-[#f53f3f]" : progress.status === "ok" ? "text-[#00a66a]" : "text-[#1f2329]";
  return (
    <div className="mb-4 rounded border border-[#e5e6eb] bg-white p-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className={`text-sm font-medium ${tone}`}>{progress.phase ? jobTypeLabel(String(progress.phase)) : "未开始"}</div>
          <div className="mt-1 break-words text-xs text-[#4e5969]">{String(progress.message ?? "尚未开始导入。")}</div>
        </div>
        <div className="text-sm font-semibold">{Math.round(percent)}%</div>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-[#f2f3f5]">
        <div className="h-full rounded-full bg-[#00a66a] transition-all" style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} />
      </div>
      <div className="mt-2 text-xs text-[#86909c]">
        {progress.total
          ? `进度 ${progress.current ?? 0}/${progress.total}`
          : progress.status === "ok"
            ? "近 30 天暂无新增会话，本次导入已正常完成"
            : "等待会话列表返回"}
        {typeof progress.successful_conversations === "number" ? ` · 成功 ${progress.successful_conversations}` : ""}
        {typeof progress.failed_conversations === "number" ? ` · 失败 ${progress.failed_conversations}` : ""}
      </div>
      {failures.length ? (
        <div className="mt-3 space-y-1">
          {failures.slice(-3).map((failure, index) => (
            <div key={index} className="rounded bg-[#fff1f0] px-2 py-1 text-xs text-[#f53f3f]">
              {String(failure.contact ?? failure.conversation_id ?? "未知会话")}：{String(failure.error ?? "获取失败")}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function EvidenceDrawer({ evidence, onClose }: { evidence: ApplyEvidencePayload; onClose: () => void }) {
  const messages = evidence.messages ?? [];
  const snapshots = evidence.job_snapshots ?? [];
  const sources = evidence.web_sources ?? [];
  return (
    <div className="fixed inset-y-0 right-0 z-50 flex w-[520px] max-w-full flex-col border-l border-[#e5e6eb] bg-white shadow-xl">
      <div className="flex h-14 items-center justify-between border-b border-[#e5e6eb] px-4">
        <div className="font-semibold">原始证据</div>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="关闭证据">
          <X className="h-4 w-4" />
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        <div className="space-y-5">
          {messages.length ? <Subsection title="BOSS 原始消息">
            <Rows items={messages} empty="" render={(msg) => (
              <div>
                <div className="mb-1 flex items-center justify-between text-xs text-[#86909c]">
                  <span>{msg.from_me ? "我" : String(msg.sender_name ?? msg.contact_name ?? "HR")}</span>
                  <span>{formatDate(msg.sent_at ?? msg.created_at)}</span>
                </div>
                <div className="rounded bg-[#f7f8fa] p-3 text-sm">{String(msg.text ?? msg.message_type ?? "非文本消息")}</div>
                <div className="mt-1 text-xs text-[#c9cdd4]">boss_message:{String(msg.message_id)}</div>
              </div>
            )} />
          </Subsection> : null}
          {snapshots.length ? <Subsection title="BOSS 岗位快照">
            <Rows items={snapshots} empty="" render={(snapshot) => {
              const job = (snapshot.payload ?? {}) as Record<string, unknown>;
              return (
                <div>
                  <div className="text-sm font-medium">{String(job.title ?? snapshot.title ?? "待补全岗位")}</div>
                  <div className="mt-1 text-xs text-[#86909c]">
                    {String(job.company ?? snapshot.company ?? "未知公司")} · {String(job.salary ?? snapshot.salary ?? "薪资未提供")}
                  </div>
                  <div className="mt-1 text-xs text-[#c9cdd4]">boss_job_snapshot:{String(snapshot.id)}</div>
                </div>
              );
            }} />
          </Subsection> : null}
          {sources.length ? <Subsection title="公开情报来源">
            <Rows items={sources} empty="" render={(source) => (
              <div>
                <a className="text-sm font-medium text-[#008a57] hover:underline" href={String(source.url)} target="_blank" rel="noreferrer">
                  {String(source.title ?? source.source_domain ?? source.url)}
                </a>
                <div className="mt-1 flex flex-wrap gap-1.5 text-xs">
                  <span className={`rounded px-1.5 py-0.5 ${source.verified ? "bg-[#e8ffea] text-[#00a870]" : "bg-[#f2f3f5] text-[#86909c]"}`}>
                    {source.verified ? "已验证页面" : "仅搜索摘要"}
                  </span>
                  <span className="rounded bg-[#f2f3f5] px-1.5 py-0.5 text-[#4e5969]">
                    来源：{sourceTierLabel(source.source_tier)}
                  </span>
                  <span className="rounded bg-[#f2f3f5] px-1.5 py-0.5 text-[#4e5969]">
                    质量：{Math.round(Number(source.quality_score ?? 0) * 100)}
                  </span>
                </div>
                <div className="mt-1 text-xs text-[#4e5969]">{String(source.excerpt ?? "无摘要")}</div>
                <div className="mt-1 text-xs text-[#c9cdd4]">
                  {String(source.research_type ?? "公开补查")} · web_source:{String(source.id)}
                </div>
              </div>
            )} />
          </Subsection> : null}
          {evidence.candidate_profile ? <Subsection title="候选人画像版本">
            <div className="rounded bg-[#f7f8fa] p-3 text-sm">
              {String(evidence.candidate_profile.profile_summary ?? "已引用当前本地简历画像。")}
            </div>
          </Subsection> : null}
          {!messages.length && !snapshots.length && !sources.length && !evidence.candidate_profile ? (
            <Empty label="没有找到对应原始证据。" />
          ) : null}
          {evidence.missing_refs?.length ? (
            <div className="text-xs text-[#f53f3f]">未解析引用：{evidence.missing_refs.join("，")}</div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function Section({ title, children, tight = false }: { title: string; children: ReactNode; tight?: boolean }) {
  return (
    <section className="rounded border border-[#e5e6eb] bg-white">
      <div className="border-b border-[#e5e6eb] px-4 py-3 text-sm font-semibold">{title}</div>
      <div className={tight ? "p-3" : "p-4"}>{children}</div>
    </section>
  );
}

function Subsection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-2 text-sm font-semibold">{title}</div>
      {children}
    </div>
  );
}

function Metric({ label, value, tone = "gray" }: { label: string; value: ReactNode; tone?: "gray" | "green" }) {
  return (
    <div className="rounded border border-[#e5e6eb] bg-white px-3 py-3">
      <div className="text-xs text-[#86909c]">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${tone === "green" ? "text-[#00a66a]" : "text-[#1f2329]"}`}>{value}</div>
    </div>
  );
}

function Rows<T extends Record<string, unknown>>({ items, render, empty }: { items: T[]; render: (item: T, index: number) => ReactNode; empty: string }) {
  if (!items.length) return <Empty label={empty} />;
  return <div className="divide-y divide-[#e5e6eb]">{items.map((item, index) => <div key={String(item.id ?? index)} className="py-3">{render(item, index)}</div>)}</div>;
}

function Empty({ label }: { label: string }) {
  return <div className="rounded border border-dashed border-[#c9cdd4] px-3 py-8 text-center text-sm text-[#86909c]">{label}</div>;
}

export function Banner({ children, tone }: { children: ReactNode; tone: "red" | "green" }) {
  return <div className={`mb-4 rounded border px-3 py-2 text-sm ${tone === "red" ? "border-red-200 bg-red-50 text-red-700" : "border-[#b7f4dc] bg-[#e8fff7] text-[#008a57]"}`}>{children}</div>;
}

export function StatusPill({ label, tone }: { label: string; tone: "green" | "gray" | "red" }) {
  const cls = tone === "green" ? "bg-[#e8fff7] text-[#00a66a]" : tone === "red" ? "bg-[#fff1f0] text-[#f53f3f]" : "bg-[#f2f3f5] text-[#4e5969]";
  return <span className={`rounded-full px-2 py-1 text-xs ${cls}`}>{label}</span>;
}

function Score({ value, status }: { value: number | null; status?: string }) {
  if ((status !== undefined && !["ok", "needs_review"].includes(status)) || value === null || Number.isNaN(value)) {
    return <div className="rounded bg-[#f2f3f5] px-2 py-1 text-xs font-semibold text-[#86909c]">待评分</div>;
  }
  return <div className="rounded bg-[#e8fff7] px-2 py-1 text-xs font-semibold text-[#00a66a]">{value}</div>;
}

function OpportunityList({ title, items, onOpen, scoreKey = "job_fit_score" }: { title: string; items: ApplyOpportunity[]; onOpen: (id: string) => void; scoreKey?: "job_fit_score" | "opportunity_priority_score" }) {
  return (
    <Section title={title}>
      <Rows items={items.slice(0, 8)} empty="暂无数据" render={(item) => (
        <button className="flex w-full items-center justify-between gap-3 text-left" onClick={() => onOpen(item.id)}>
          <div className="min-w-0">
            <div className="truncate text-sm font-medium">{item.title}</div>
            <div className="text-xs text-[#86909c]">{item.company ?? "未知公司"} · {opportunityStageText(item)}</div>
          </div>
          <Score value={Number(item[scoreKey] ?? 0)} status={String(item.fit_status ?? "")} />
        </button>
      )} />
    </Section>
  );
}

function FitAnalysisPanel({ analysis, opportunity }: { analysis?: Record<string, unknown> | null; opportunity: ApplyOpportunity }) {
  const status = String(analysis?.status ?? opportunity.fit_status ?? "pending");
  if (status === "no_profile") {
    return <Empty label="缺少简历画像或求职偏好，暂不计算岗位契合度。请先上传 PDF 或粘贴 Markdown 简历，并填写 5 项偏好。" />;
  }
  if (!analysis || status === "stale" || status === "pending") {
    return <Empty label="简历画像已就绪，岗位契合度正在后台计算。完成后本页会自动刷新。" />;
  }
  const dimensions = (analysis.dimensions ?? []) as Array<Record<string, unknown>>;
  const matched = (analysis.matched_evidence ?? []) as Array<Record<string, unknown>>;
  const missing = ((analysis.missing_requirements ?? []) as unknown[])
    .map((item) => typeof item === "string" ? { requirement: item } : item)
    .filter((item): item is Record<string, unknown> => (
      Boolean(item)
      && typeof item === "object"
      && Boolean(String((item as Record<string, unknown>).requirement ?? "").trim())
    ));
  const caps = (analysis.hard_filter_caps ?? []) as Array<Record<string, unknown>>;
  return (
    <div className="space-y-3">
      <InfoGrid rows={[
        ["岗位契合度", `${String(analysis.job_fit_score ?? opportunity.job_fit_score ?? "-")}/100`],
        ["机会优先级", `${String(analysis.opportunity_priority_score ?? opportunity.opportunity_priority_score ?? "-")}/100`],
        ["置信度", `${Math.round(Number(analysis.confidence ?? opportunity.fit_confidence ?? 0) * 100)}%`],
        ["状态", fitStatusLabel(status)],
      ]} />
      <Rows items={dimensions} empty="暂无维度评分" render={(dim) => (
        <div>
          <div className="flex items-center justify-between gap-3">
            <div className="text-sm font-medium">{String(dim.name)}</div>
            <Score value={Number(dim.score ?? 0)} />
          </div>
          <div className="mt-1 text-xs text-[#4e5969]">{String(dim.reason ?? "")}</div>
          <div className="mt-1 text-xs text-[#86909c]">
            证据：{((dim.evidence_refs ?? dim.evidence ?? []) as unknown[]).map(String).join("，") || "暂无"}
          </div>
        </div>
      )} />
      {matched.length ? (
        <Subsection title="匹配证据">
          <Rows items={matched} empty="暂无匹配证据" render={(item) => (
            <div className="text-sm">
              <div className="font-medium">{String(item.statement ?? item.requirement ?? "匹配点")}</div>
              <div className="mt-1 text-xs text-[#4e5969]">{String(item.reason ?? "")}</div>
              <div className="mt-1 text-xs text-[#86909c]">
                {Array.isArray(item.evidence_refs)
                  ? item.evidence_refs.map(String).join("，")
                  : [item.resume_ref, item.job_ref].filter(Boolean).map(String).join("，")}
              </div>
            </div>
          )} />
        </Subsection>
      ) : null}
      {missing.length ? (
        <Subsection title="缺口">
          <Rows items={missing} empty="暂无明显缺口" render={(item) => (
            <div className="text-sm">
              <div className="font-medium">{String(item.requirement ?? "缺口")}</div>
              <div className="mt-1 text-xs text-[#4e5969]">{String(item.reason ?? "")}</div>
            </div>
          )} />
        </Subsection>
      ) : null}
      {caps.length ? (
        <Subsection title="硬性条件封顶">
          <Rows items={caps} empty="暂无封顶扣分" render={(item) => (
            <div className="text-sm">
              <div className="font-medium">最高 {String(item.max_score ?? "-")} 分</div>
              <div className="mt-1 text-xs text-[#f53f3f]">{String(item.reason ?? "")}</div>
            </div>
          )} />
        </Subsection>
      ) : null}
    </div>
  );
}

function InfoGrid({ rows }: { rows: Array<[string, string]> }) {
  return <dl className="grid gap-2 md:grid-cols-2">{rows.map(([key, value]) => <div key={key} className="rounded bg-[#f7f8fa] p-3"><dt className="text-xs text-[#86909c]">{key}</dt><dd className="mt-1 text-sm font-medium">{value}</dd></div>)}</dl>;
}

function TagGroup({ title, values }: { title: string; values: unknown }) {
  const tags = Array.isArray(values) ? values : [];
  return (
    <div className="mb-3">
      <div className="mb-2 text-xs text-[#86909c]">{title}</div>
      <div className="flex flex-wrap gap-2">
        {tags.length ? tags.map((tag) => <span key={String(tag)} className="rounded-full bg-[#e8fff7] px-2 py-1 text-xs text-[#00a66a]">{String(tag)}</span>) : <span className="text-xs text-[#c9cdd4]">暂无</span>}
      </div>
    </div>
  );
}

export function stageLabel(stage: string) {
  return STAGE_LABELS[stage] ?? (stage || "未判断");
}

function opportunityStageText(item: ApplyOpportunity) {
  const current = stageLabel(String(item.stage ?? ""));
  const suggested = item.stage_suggestion
    ? stageLabel(String(item.stage_suggestion))
    : "";
  return suggested && suggested !== current ? `${current} · 建议：${suggested}` : current;
}

function sourceQualityLabel(value: unknown) {
  const key = String(value ?? "unknown");
  return SOURCE_QUALITY_LABELS[key] ?? key;
}

function sourceTierLabel(value: unknown) {
  const key = String(value ?? "general");
  return {
    authority: "政府/权威",
    registry: "工商信息",
    recruitment: "招聘平台",
    media: "媒体",
    community: "社区",
    general: "一般网页",
    legacy: "历史来源",
  }[key] ?? key;
}

function toolUtilityLabel(value: unknown) {
  const key = String(value ?? "unknown");
  return {
    evidence_used: "证据被采用",
    rule_context: "规则参与推理",
    routing_context: "用于选择后续 Skill",
    context_unused: "上下文未形成有效结论",
    novel_evidence_unused: "新证据未采用",
    duplicate_context: "重复上下文",
    empty: "空结果",
    unknown: "未评估",
  }[key] ?? key;
}

function opportunityNextAction(item: ApplyOpportunity): string {
  if (item.next_action) return String(item.next_action);
  return {
    need_my_action: "查看 HR 最新消息并完成回复或材料补充",
    waiting_feedback: "等待 HR 反馈；超过 48 小时仍无回应时再考虑跟进",
    interviewing: "确认面试时间并准备岗位相关项目说明",
    communicating: "继续沟通并补全岗位信息",
    discovered: "等待进一步沟通或补全岗位信息",
    closed: "机会已结束，无需继续跟进",
  }[String(item.stage ?? "")] ?? "查看最新证据后决定下一步";
}

function fitStatusLabel(value: string) {
  return {
    ok: "已评分",
    needs_review: "待确认",
    failed: "评分失败",
    no_profile: "缺少简历",
    stale: "待重算",
  }[value] ?? value;
}

export function analysisModeLabel(value: string) {
  return {
    skipped: "跳过未变化",
    cold_projection: "冷会话投影",
    opportunity_agent: "机会 Agent",
    queued: "等待分析",
    failed: "分析失败",
  }[value] ?? "机会分析";
}

export function skippedReasonLabel(value: string) {
  return {
    no_new_messages: "没有新增消息",
    non_human_messages_only: "只有平台自动消息",
    queued_limit: "超过本批分析上限",
  }[value] ?? "本轮无需分析";
}

export function jobTypeLabel(value: string) {
  return {
    import_boss_snapshot: "导入 BOSS 快照",
    sync_account: "同步 BOSS 账号",
    trigger_import_analysis: "增量消息分流",
    analyze_opportunity: "Agent 分析机会",
    analyze_job_fit: "岗位契合度评分",
    rebuild_derived_from_l1: "重建派生分析",
    heartbeat: "任务到期检查",
  }[value] ?? value;
}

function jobStatusLabel(value: string) {
  return {
    queued: "排队中",
    running: "运行中",
    ok: "完成",
    failed: "失败",
    cancelled: "已取消",
  }[value] ?? value;
}

export function bossStatusLabel(status: Record<string, unknown> | null) {
  if (!status) return "未知";
  if (status.logged_in) return "已登录且可导入";
  if (status.profile_ready && !status.cdp_alive) return "浏览器未启动，登录态待恢复";
  if (status.cdp_alive) return "浏览器已启动，聊天页不可读";
  return "首次登录";
}

export function bossLoginButtonLabel(status: Record<string, unknown> | null) {
  if (!status) return "打开 BOSS";
  if (status?.logged_in) return "打开 BOSS 聊天页";
  if (status?.profile_ready) return "打开 BOSS 专用浏览器";
  return "首次登录 BOSS";
}

function formatDate(value: unknown) {
  if (!value) return "未安排";
  return String(value).replace("T", " ").slice(0, 16);
}

function formatDuration(value: unknown) {
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "暂无";
  return milliseconds < 1000 ? `${Math.round(milliseconds)}ms` : `${(milliseconds / 1000).toFixed(1)}s`;
}

function formatPercent(value: unknown) {
  const ratio = Number(value);
  if (!Number.isFinite(ratio)) return "暂无";
  const percentage = ratio * 100;
  return `${Number.isInteger(percentage) ? percentage.toFixed(0) : percentage.toFixed(1)}%`;
}

export function parseEvidenceIds(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw.map(String);
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed.map(String);
    } catch {
      return raw.split(",").map((item) => item.trim()).filter(Boolean);
    }
  }
  return [];
}

function parseReport(raw: unknown): Record<string, unknown> | null {
  if (!raw) return null;
  if (typeof raw === "object") return raw as Record<string, unknown>;
  try {
    return JSON.parse(String(raw)) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function redactUiText(value: string): string {
  return value
    .replace(/1[3-9]\d{9}/g, "[手机号]")
    .replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, "[邮箱]")
    .replace(/https?:\/\/\S+/g, "[链接]");
}

export function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("读取 PDF 文件失败"));
    reader.onload = () => {
      if (typeof reader.result === "string") {
        resolve(reader.result);
      } else {
        reject(new Error("PDF 文件读取结果异常"));
      }
    };
    reader.readAsDataURL(file);
  });
}

function formatJson(raw: unknown) {
  try {
    return JSON.stringify(typeof raw === "string" ? JSON.parse(raw) : raw, null, 2);
  } catch {
    return String(raw ?? "");
  }
}

