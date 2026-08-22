export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

export type ApplyOverviewPayload = Record<string, unknown> & {
  metrics?: Record<string, number>;
  action_items?: Array<Record<string, unknown>>;
  stage_changes?: Array<Record<string, unknown>>;
  risk_opportunities?: Array<Record<string, unknown>>;
  top_job_fits?: Array<Record<string, unknown>>;
  high_priority_opportunities?: Array<Record<string, unknown>>;
  profile_ready?: boolean;
  latest_import?: Record<string, unknown> | null;
  recent_delta_panel?: Record<string, unknown> | null;
  agent_runs?: Array<Record<string, unknown>>;
  current_account?: Record<string, unknown> | null;
  import_progress?: Record<string, unknown> | null;
  rebuild_progress?: Record<string, unknown> | null;
  health?: Record<string, unknown>;
};

export type ApplyOpportunity = Record<string, unknown> & {
  id: string;
  title: string;
  company?: string | null;
  stage?: string;
  stage_suggestion?: string | null;
  pursuit_recommendation?: string | null;
  job_fit_score?: number | null;
  opportunity_priority_score?: number | null;
  fit_status?: string | null;
  next_action?: string | null;
  risk_flags?: Array<Record<string, unknown>> | string;
  open_questions?: string[];
};

export type ApplyTasksPayload = {
  tasks: Array<Record<string, unknown>>;
  suggestions: Array<Record<string, unknown>>;
};

export type ApplyAgentRunsPayload = {
  runs?: Array<Record<string, unknown>>;
  run?: Record<string, unknown> | null;
  steps?: Array<Record<string, unknown>>;
  tool_observations?: Array<Record<string, unknown>>;
  metrics?: Record<string, unknown>;
};

export type ApplyProfilePayload = {
  profile: Record<string, unknown> | null;
  preferences: Record<string, unknown> | null;
};

export type ApplyHealthPayload = Record<string, unknown> & {
  postgres?: { ok?: boolean; error?: string | null };
  redis?: { ok?: boolean; error?: string | null };
  worker?: { ok?: boolean; error?: string | null };
  can_view?: boolean;
  can_enqueue?: boolean;
  message?: string;
};

export type ApplyJob = Record<string, unknown> & {
  id: string;
  job_type: string;
  status: string;
  message?: string | null;
  progress_current?: number;
  progress_total?: number;
  progress_percent?: number;
  target_type?: string | null;
  target_id?: string | null;
  error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  finished_at?: string | null;
};

export type ApplyImportProgressPayload = Record<string, unknown> & {
  status?: "idle" | "queued" | "running" | "ok" | "failed" | "cancelled";
  phase?: string;
  message?: string;
  current?: number;
  total?: number;
  percent?: number;
  report?: Record<string, unknown>;
  failures?: Array<Record<string, unknown>>;
};

export type ApplyEvidencePayload = {
  messages: Array<Record<string, unknown>>;
  job_snapshots: Array<Record<string, unknown>>;
  web_sources: Array<Record<string, unknown>>;
  candidate_profile: Record<string, unknown> | null;
  missing_refs: string[];
};

async function request<T>(url: string, init?: RequestInit, attempt = 0): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
      credentials: "same-origin",
    });
  } catch (error) {
    if ((!init?.method || init.method === "GET") && attempt === 0) {
      await new Promise((resolve) => window.setTimeout(resolve, 500));
      return request<T>(url, init, attempt + 1);
    }
    throw error;
  }
  if (
    response.status >= 500
    && (!init?.method || init.method === "GET")
    && attempt === 0
  ) {
    await new Promise((resolve) => window.setTimeout(resolve, 500));
    return request<T>(url, init, attempt + 1);
  }
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const payload = (await response.json()) as { error?: string; detail?: string };
      message = payload.error || payload.detail || message;
    } catch {
      // Keep the status-only fallback.
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

export const fetchApplyOverview = (_token = "") =>
  request<ApplyOverviewPayload>("/api/apply/overview");
export const fetchApplyHealth = (_token = "") =>
  request<ApplyHealthPayload>("/api/apply/health");
export const fetchApplyJobs = (_token = "") =>
  request<{ jobs?: ApplyJob[]; job?: ApplyJob }>("/api/apply/jobs");
export const fetchApplyOpportunities = (_token = "") =>
  request<{ opportunities: ApplyOpportunity[] }>("/api/apply/opportunities");
export const fetchApplyOpportunity = (_token: string, id: string) =>
  request<Record<string, unknown>>(`/api/apply/opportunities/${encodeURIComponent(id)}`);
export const fetchApplyEvidence = (_token: string, opportunityId: string, messageIds: string[]) => {
  const query = new URLSearchParams({ evidence_refs: messageIds.join(",") });
  return request<ApplyEvidencePayload>(
    `/api/apply/opportunities/${encodeURIComponent(opportunityId)}/evidence?${query}`,
  );
};
export const fetchApplyTasks = (_token = "") =>
  request<ApplyTasksPayload>("/api/apply/tasks");
export const fetchApplyAgentRuns = (_token = "", runId?: string) =>
  request<ApplyAgentRunsPayload>(
    `/api/apply/agent-runs${runId ? `/${encodeURIComponent(runId)}` : ""}`,
  );
export const fetchApplyProfile = (_token = "") =>
  request<ApplyProfilePayload>("/api/apply/profile");
export const updateApplyProfile = (_token: string, payload: Record<string, unknown>) =>
  request<ApplyProfilePayload>("/api/apply/profile", {
    method: "POST",
    body: JSON.stringify(payload),
  });
export const uploadApplyResumePdf = (
  dataUrl: string,
  filename: string,
  preferences: Record<string, unknown>,
) =>
  request<Record<string, unknown>>("/api/apply/profile/pdf", {
    method: "POST",
    body: JSON.stringify({ data_url: dataUrl, filename, preferences }),
  });
export const clearApplyDerivedData = (_token = "") =>
  request<Record<string, unknown>>("/api/apply/derived/clear", { method: "DELETE" });
export const reanalyzeApplyOpportunity = (_token: string, id: string) =>
  request<Record<string, unknown>>(`/api/apply/opportunities/${encodeURIComponent(id)}/reanalyze`, { method: "POST" });
export const reanalyzeApplyFit = (_token: string, id: string) =>
  request<Record<string, unknown>>(`/api/apply/opportunities/${encodeURIComponent(id)}/fit/reanalyze`, { method: "POST" });
export const reanalyzeAllApplyFit = (_token = "", limit = 200) =>
  request<Record<string, unknown>>(`/api/apply/fit/reanalyze/all?limit=${limit}`, { method: "POST" });
export const researchApplyOpportunity = (_token: string, id: string) =>
  request<Record<string, unknown>>(`/api/apply/opportunities/${encodeURIComponent(id)}/research`, { method: "POST" });
export const refreshBossOpportunity = (_token: string, id: string) =>
  request<Record<string, unknown>>(`/api/apply/opportunities/${encodeURIComponent(id)}/refresh-boss`, { method: "POST" });
export const reanalyzeAllApply = (_token = "", limit = 50) =>
  request<Record<string, unknown>>(`/api/apply/reanalyze/all?limit=${limit}`, { method: "POST" });
export const fetchBossStatus = (_token = "", force = false) =>
  request<Record<string, unknown>>(`/api/apply/boss/status${force ? "?force=true" : ""}`);
export const beginBossLogin = (_token = "") =>
  request<Record<string, unknown>>("/api/apply/boss/login", { method: "POST" });
export const importBossSnapshot = (_token = "", days = 30) =>
  request<Record<string, unknown>>(`/api/apply/import?days=${days}`, { method: "POST" });
export const loadApplyDemo = (_token = "") =>
  request<Record<string, unknown>>("/api/apply/demo", { method: "POST" });
export const fetchApplyImportProgress = (_token = "") =>
  request<ApplyImportProgressPayload>("/api/apply/import/progress");
export const clearApplyData = (_token = "", login = false) =>
  request<Record<string, unknown>>(`/api/apply/clear?include_login=${login ? "true" : "false"}`, { method: "DELETE" });
export const updateApplySuggestion = (
  _token: string,
  id: string | number,
  status: "accepted" | "rejected" | "edited",
) =>
  request<Record<string, unknown>>(`/api/apply/suggestions/${encodeURIComponent(id)}`, {
    method: "POST",
    body: JSON.stringify({ status }),
  });
