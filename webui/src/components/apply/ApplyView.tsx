import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Bell,
  BriefcaseBusiness,
  Database,
  FileText,
  GitBranch,
  LayoutDashboard,
  LoaderCircle,
  LogIn,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  beginBossLogin,
  clearApplyData,
  clearApplyDerivedData,
  fetchApplyAgentRuns,
  fetchApplyEvidence,
  fetchApplyHealth,
  fetchApplyJobs,
  fetchApplyOpportunities,
  fetchApplyOpportunity,
  fetchApplyImportProgress,
  fetchApplyOverview,
  fetchApplyProfile,
  fetchApplyTasks,
  fetchBossStatus,
  importBossSnapshot,
  loadApplyDemo,
  reanalyzeAllApply,
  reanalyzeApplyFit,
  reanalyzeApplyOpportunity,
  refreshBossOpportunity,
  researchApplyOpportunity,
  updateApplyProfile,
  updateApplySuggestion,
  uploadApplyResumePdf,
  type ApplyAgentRunsPayload,
  type ApplyEvidencePayload,
  type ApplyHealthPayload,
  type ApplyImportProgressPayload,
  type ApplyJob,
  type ApplyOpportunity,
  type ApplyOverviewPayload,
  type ApplyProfilePayload,
  type ApplyTasksPayload,
} from "@/lib/apply-api";
import { subscribeApplyEvents } from "./event-client";
import {
  AgentPage,
  APPLY_OUTLINE_BUTTON,
  APPLY_PRIMARY_BUTTON,
  Banner,
  bossLoginButtonLabel,
  bossStatusLabel,
  EvidenceDrawer,
  HealthPanel,
  ImportPage,
  ImportProgressBanner,
  OpportunitiesPage,
  OverviewPage,
  parseEvidenceIds,
  ProfilePage,
  readFileAsDataUrl,
  stageLabel,
  StatusPill,
  TasksPage,
} from "./ApplyPages";
import { getApplyRefreshTargets } from "./realtime";

type PageKey = "overview" | "opportunities" | "tasks" | "agent" | "profile" | "import";

const PAGES: Array<{ key: PageKey; label: string; icon: ReactNode }> = [
  { key: "overview", label: "总览", icon: <LayoutDashboard className="h-4 w-4" /> },
  { key: "opportunities", label: "机会", icon: <BriefcaseBusiness className="h-4 w-4" /> },
  { key: "tasks", label: "任务", icon: <Bell className="h-4 w-4" /> },
  { key: "agent", label: "Agent", icon: <GitBranch className="h-4 w-4" /> },
  { key: "profile", label: "简历", icon: <FileText className="h-4 w-4" /> },
  { key: "import", label: "导入", icon: <Database className="h-4 w-4" /> },
];

export function ApplyView() {
  const token = "";
  const [initialLoading, setInitialLoading] = useState(true);
  const [page, setPage] = useState<PageKey>("overview");
  const [overview, setOverview] = useState<ApplyOverviewPayload | null>(null);
  const [opportunities, setOpportunities] = useState<ApplyOpportunity[]>([]);
  const [selectedOpportunityId, setSelectedOpportunityId] = useState("");
  const [selectedOpportunity, setSelectedOpportunity] = useState<Record<string, unknown> | null>(null);
  const [tasks, setTasks] = useState<ApplyTasksPayload | null>(null);
  const [agentRuns, setAgentRuns] = useState<ApplyAgentRunsPayload | null>(null);
  const [selectedRun, setSelectedRun] = useState<ApplyAgentRunsPayload | null>(null);
  const [profile, setProfile] = useState<ApplyProfilePayload | null>(null);
  const [health, setHealth] = useState<ApplyHealthPayload | null>(null);
  const [jobs, setJobs] = useState<ApplyJob[]>([]);
  const [bossStatus, setBossStatus] = useState<Record<string, unknown> | null>(null);
  const [importProgress, setImportProgress] = useState<ApplyImportProgressPayload | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [evidence, setEvidence] = useState<ApplyEvidencePayload | null>(null);
  const [profileUploading, setProfileUploading] = useState(false);
  const [profileDraft, setProfileDraft] = useState("");
  const [prefDraft, setPrefDraft] = useState({
    target_roles: "Agent 开发实习, 大模型应用开发, Python 后端",
    cities: "杭州, 上海, 远程",
    salary: "",
    internship_time: "每周 4-5 天，3-6 个月",
    excluded: "培训贷, 收费培训, 无薪",
  });

  const refresh = useCallback(async () => {
    setError("");
    const overviewPromise = fetchApplyOverview(token);
    const pagePromise = page === "overview" || page === "opportunities"
      ? fetchApplyOpportunities(token)
      : Promise.resolve(null);
    const [nextOverview, initialPageData] = await Promise.all([overviewPromise, pagePromise]);
    setOverview(nextOverview);
    if (nextOverview.health) setHealth(nextOverview.health as ApplyHealthPayload);
    if (nextOverview.import_progress) setImportProgress(nextOverview.import_progress as ApplyImportProgressPayload);

    if (page === "overview" || page === "opportunities") {
      setOpportunities(initialPageData?.opportunities ?? []);
    } else if (page === "tasks") {
      setTasks(await fetchApplyTasks(token));
    } else if (page === "agent") {
      const [nextRuns, nextJobs] = await Promise.all([
        fetchApplyAgentRuns(token),
        fetchApplyJobs(token).catch(() => ({ jobs: [] })),
      ]);
      setAgentRuns(nextRuns);
      setJobs(nextJobs.jobs ?? []);
    } else if (page === "profile") {
      const nextProfile = await fetchApplyProfile(token);
      setProfile(nextProfile);
      const resume = String(nextProfile.profile?.resume_markdown ?? "");
      if (resume) setProfileDraft((current) => current || resume);
      if (nextProfile.preferences) {
        setPrefDraft((current) => ({
          target_roles: String(nextProfile.preferences?.target_roles ?? current.target_roles),
          cities: String(nextProfile.preferences?.cities ?? current.cities),
          salary: String(nextProfile.preferences?.salary ?? ""),
          internship_time: String(nextProfile.preferences?.internship_time ?? current.internship_time),
          excluded: String(nextProfile.preferences?.excluded ?? current.excluded),
        }));
      }
    } else if (page === "import") {
      const [nextHealth, nextJobs, status, progress] = await Promise.all([
        fetchApplyHealth(token).catch((e) => ({ message: (e as Error).message, can_view: false, can_enqueue: false })),
        fetchApplyJobs(token).catch(() => ({ jobs: [] })),
        fetchBossStatus(token, true),
        fetchApplyImportProgress(token),
      ]);
      setHealth(nextHealth);
      setJobs(nextJobs.jobs ?? []);
      setBossStatus(status);
      setImportProgress(progress);
    }
  }, [page, token]);

  const refreshImportSurfaces = useCallback(async () => {
    const [nextJobs, progress, nextOverview, nextOpps] = await Promise.all([
      fetchApplyJobs(token).catch(() => ({ jobs: [] })),
      fetchApplyImportProgress(token),
      fetchApplyOverview(token),
      fetchApplyOpportunities(token),
    ]);
    setJobs(nextJobs.jobs ?? []);
    setImportProgress(progress);
    setOverview(nextOverview);
    setOpportunities(nextOpps.opportunities ?? []);
  }, [token]);

  useEffect(() => {
    return subscribeApplyEvents((ev) => {
      const targets = new Set(getApplyRefreshTargets(ev, selectedOpportunityId));
      if (targets.has("health")) {
        void fetchApplyHealth(token).then(setHealth).catch(() => undefined);
      }
      if (targets.has("jobs")) {
        void fetchApplyJobs(token).then((payload) => setJobs(payload.jobs ?? [])).catch(() => undefined);
      }
      if (targets.has("import")) {
        void fetchApplyImportProgress(token).then(setImportProgress).catch(() => undefined);
      }
      if (targets.has("overview")) {
        void fetchApplyOverview(token).then(setOverview).catch(() => undefined);
      }
      if (targets.has("opportunities")) {
        void fetchApplyOpportunities(token).then((payload) => setOpportunities(payload.opportunities ?? [])).catch(() => undefined);
      }
      if (targets.has("agent")) {
        void fetchApplyAgentRuns(token).then(setAgentRuns).catch(() => undefined);
      }
      if (targets.has("opportunity") && selectedOpportunityId) {
        void fetchApplyOpportunity(token, selectedOpportunityId).then(setSelectedOpportunity).catch(() => undefined);
      }
      if (targets.has("tasks")) {
        void fetchApplyTasks(token).then(setTasks).catch(() => undefined);
      }
      if (targets.has("profile")) {
        void fetchApplyProfile(token).then(setProfile).catch(() => undefined);
      }
    });
  }, [selectedOpportunityId, token]);

  useEffect(() => {
    let active = true;
    void refresh()
      .catch((e) => setError((e as Error).message))
      .finally(() => {
        if (active) setInitialLoading(false);
      });
    return () => {
      active = false;
    };
  }, [refresh]);

  useEffect(() => {
    const firstRun = agentRuns?.runs?.[0];
    if (page !== "agent" || selectedRun?.run || !firstRun?.id) return;
    void fetchApplyAgentRuns(token, String(firstRun.id)).then(setSelectedRun).catch(() => undefined);
  }, [agentRuns?.runs, page, selectedRun?.run, token]);

  useEffect(() => {
    if (importProgress?.status !== "running") return;
    const timer = window.setInterval(() => {
      void fetchApplyImportProgress(token)
        .then((progress) => {
          setImportProgress(progress);
          if (progress.status === "ok" || progress.status === "failed") {
            void refreshImportSurfaces().catch((e) => setError((e as Error).message));
          }
        })
        .catch((e) => setError((e as Error).message));
    }, 1200);
    return () => window.clearInterval(timer);
  }, [importProgress?.status, refreshImportSurfaces, token]);

  const selectedOpportunitySummary = useMemo(
    () => opportunities.find((item) => item.id === selectedOpportunityId) ?? opportunities[0],
    [opportunities, selectedOpportunityId],
  );

  useEffect(() => {
    const id = selectedOpportunitySummary?.id;
    if (!id) return;
    if (selectedOpportunityId !== id) setSelectedOpportunityId(id);
    void fetchApplyOpportunity(token, id)
      .then(setSelectedOpportunity)
      .catch((e) => setError((e as Error).message));
  }, [token, selectedOpportunitySummary?.id, selectedOpportunityId]);

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setError("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  const startImport = async () => {
    setBusy("启动导入任务");
    setError("");
    try {
      let status = await fetchBossStatus(token, true);
      if (!status.logged_in && status.profile_ready && !status.cdp_alive) {
        status = await beginBossLogin(token);
      }
      setBossStatus(status);
      if (!status.logged_in) {
        throw new Error("Capybot 专用浏览器已打开。请在该窗口完成登录并确认聊天页可见，然后再次点击导入。");
      }
      const payload = await importBossSnapshot(token, 30);
      const job = payload.job as ApplyJob | undefined;
      if (job) {
        setImportProgress({
          status: job.status as ApplyImportProgressPayload["status"],
          phase: "导入任务已入队",
          message: String(job.message ?? "等待 Worker 执行导入。"),
          current: Number(job.progress_current ?? 0),
          total: Number(job.progress_total ?? 0),
          percent: Number(job.progress_percent ?? 0),
          job_id: job.id,
        });
      }
      await refreshImportSurfaces();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  const filteredOpportunities = opportunities.filter((item) => {
    const haystack = `${item.title ?? ""} ${item.company ?? ""} ${stageLabel(String(item.stage ?? ""))} ${item.next_action ?? ""}`.toLowerCase();
    return haystack.includes(query.toLowerCase());
  });
  const currentAccount = overview?.current_account as
    | Record<string, unknown>
    | null
    | undefined;
  const demoMode = currentAccount?.source === "demo_fixture";

  const openEvidence = async (opportunityId: string, ids: unknown) => {
    const messageIds = parseEvidenceIds(ids);
    if (!messageIds.length) {
      setEvidence(null);
      return;
    }
    const payload = await fetchApplyEvidence(token, opportunityId, messageIds);
    setEvidence(payload);
  };

  const uploadResumePdf = async (file: File) => {
    setProfileUploading(true);
    setError("");
    try {
      const dataUrl = await readFileAsDataUrl(file);
      const result = await uploadApplyResumePdf(dataUrl, file.name, prefDraft);
      const payload = result as {
        profile?: { resume_markdown?: string };
        pdf_parse?: { markdown?: string };
      };
      const markdown = String(payload.pdf_parse?.markdown ?? payload.profile?.resume_markdown ?? "");
      if (markdown) setProfileDraft(markdown);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setProfileUploading(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 bg-[#f6f7f8] text-[#1f2329]">
      <aside className="flex w-16 shrink-0 flex-col border-r border-[#e5e6eb] bg-white lg:w-48">
        <div className="flex h-14 items-center gap-2 border-b border-[#e5e6eb] px-3">
          <div className="hidden min-w-0 lg:block">
            <div className="truncate text-sm font-semibold">Capybot Apply</div>
            <div className="text-xs text-[#86909c]">求职进度 Agent</div>
          </div>
        </div>
        <nav className="space-y-1 p-2">
          {PAGES.map((item) => (
            <button
              key={item.key}
              aria-label={item.label}
              title={item.label}
              className={`flex h-9 w-full items-center gap-2 rounded px-3 text-left text-sm ${
                page === item.key ? "bg-[#e8fff7] text-[#00a66a]" : "text-[#4e5969] hover:bg-[#f2f3f5]"
              }`}
              onClick={() => setPage(item.key)}
            >
              {item.icon}
              <span className="hidden lg:inline">{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="mt-auto hidden border-t border-[#e5e6eb] p-3 text-xs text-[#86909c] lg:block">
          只读导入，不自动发送 BOSS 消息。
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex min-h-14 shrink-0 flex-wrap items-center justify-between gap-2 border-b border-[#e5e6eb] bg-white px-3 py-2 sm:px-4">
          <div className="min-w-0">
            <div className="text-sm font-semibold">{PAGES.find((item) => item.key === page)?.label}</div>
            <div className="hidden text-xs text-[#86909c] xl:block">以机会为核心，把聊天记录转成进度、任务、风险和 Agent 轨迹。</div>
          </div>
          <div className="flex items-center gap-2">
            <div className="hidden sm:block"><StatusPill
              label={page === "import" ? bossStatusLabel(bossStatus) : initialLoading ? "正在读取本地记录" : demoMode ? "中文演示数据" : currentAccount ? "本地记录已加载" : "暂无账号记录"}
              tone={page === "import" ? bossStatus?.logged_in ? "green" : "gray" : overview?.current_account ? "green" : "gray"}
            /></div>
            <Button className={APPLY_OUTLINE_BUTTON} variant="outline" size="sm" onClick={() => run("打开 BOSS 专用浏览器", () => beginBossLogin(token))} disabled={!!busy}>
              <LogIn className="mr-2 h-4 w-4" />
              {bossLoginButtonLabel(bossStatus)}
            </Button>
            <Button className={APPLY_PRIMARY_BUTTON} size="sm" onClick={startImport} disabled={!!busy || importProgress?.status === "running"}>
              <Database className="mr-2 h-4 w-4" />
              导入近 30 天
            </Button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          <HealthPanel health={health} />
          {error ? <Banner tone="red">{error}</Banner> : null}
          {busy ? <Banner tone="green">正在执行：{busy}</Banner> : null}
          {importProgress?.status === "running" ? <ImportProgressBanner progress={importProgress} /> : null}
          {initialLoading ? (
            <ApplyInitialLoading />
          ) : (
            <>
              {page === "overview" ? <OverviewPage overview={overview} onOpenOpportunity={(id) => { setSelectedOpportunityId(id); setPage("opportunities"); }} /> : null}
              {page === "opportunities" ? (
                <OpportunitiesPage
                  opportunities={filteredOpportunities}
                  query={query}
                  setQuery={setQuery}
                  selectedId={selectedOpportunitySummary?.id ?? ""}
                  setSelectedId={setSelectedOpportunityId}
                  detail={selectedOpportunity}
                  onEvidence={openEvidence}
                  onReanalyze={(id) => run("重新分析机会", () => reanalyzeApplyOpportunity(token, id))}
                  onReanalyzeFit={(id) => run("重新计算岗位契合度", () => reanalyzeApplyFit(token, id))}
                  onResearch={(id) => run("补全岗位与公司信息", () => researchApplyOpportunity(token, id))}
                  onRefreshBoss={(id) => run("刷新 BOSS 机会证据", () => refreshBossOpportunity(token, id))}
                />
              ) : null}
              {page === "tasks" ? <TasksPage tasks={tasks} onSuggestion={(id, status) => run("更新 Agent 建议", () => updateApplySuggestion(token, id, status))} onEvidence={openEvidence} /> : null}
              {page === "agent" ? (
                <AgentPage
                  runs={agentRuns}
                  selectedRun={selectedRun}
                  onOpenRun={(id) => fetchApplyAgentRuns(token, id).then(setSelectedRun)}
                  onReanalyzeAll={() => run("批量重分析", () => reanalyzeAllApply(token, 50))}
                  jobs={jobs}
                />
              ) : null}
              {page === "profile" ? (
                <ProfilePage
                  profile={profile}
                  profileDraft={profileDraft}
                  setProfileDraft={setProfileDraft}
                  prefDraft={prefDraft}
                  setPrefDraft={setPrefDraft}
                  onUploadPdf={uploadResumePdf}
                  uploading={profileUploading}
                  onSave={() => run("保存简历画像", () => updateApplyProfile(token, { resume_markdown: profileDraft, preferences: prefDraft }))}
                />
              ) : null}
              {page === "import" ? (
                <ImportPage
                  overview={overview}
                  bossStatus={bossStatus}
                  importProgress={importProgress}
                  onImport={startImport}
                  onDemo={() => run("加载中文演示", () => loadApplyDemo(token))}
                  onLogin={() => run("打开 BOSS 专用浏览器", () => beginBossLogin(token))}
                  onClearDerived={() => run("清空派生分析", () => clearApplyDerivedData(token))}
                  onClearAll={() => run("清空全部数据和登录态", () => clearApplyData(token, true))}
                  jobs={jobs}
                />
              ) : null}
            </>
          )}
        </div>
      </main>
      {evidence ? <EvidenceDrawer evidence={evidence} onClose={() => setEvidence(null)} /> : null}
    </div>
  );
}

export function ApplyInitialLoading() {
  return (
    <div className="flex min-h-[360px] items-center justify-center" role="status" aria-live="polite">
      <div className="flex items-center gap-3 text-sm text-[#4e5969]">
        <LoaderCircle className="h-5 w-5 animate-spin text-[#00a66a]" />
        <span>正在读取 PostgreSQL 中的本地机会记录…</span>
      </div>
    </div>
  );
}
