import { describe, expect, it } from "vitest";

import { getApplyRefreshTargets } from "@/components/apply/realtime";

describe("Apply realtime invalidation", () => {
  it("does not fan out a job progress event to every Apply API", () => {
    const targets = getApplyRefreshTargets(
      { event: "apply_job_updated", job_id: "job-1", status: "running" },
      "opp-1",
    );

    expect(targets).toEqual(["jobs"]);
  });

  it("refreshes only the matching open opportunity detail", () => {
    const matching = getApplyRefreshTargets(
      { event: "apply_opportunity_updated", opportunity_id: "opp-1" },
      "opp-1",
    );
    const different = getApplyRefreshTargets(
      { event: "apply_opportunity_updated", opportunity_id: "opp-2" },
      "opp-1",
    );

    expect(matching).toContain("opportunity");
    expect(different).not.toContain("opportunity");
    expect(different).toEqual(["overview", "opportunities", "tasks"]);
  });

  it("keeps import and agent invalidations independent", () => {
    expect(
      getApplyRefreshTargets({ event: "apply_import_updated", import_run_id: "run-1" }, ""),
    ).toEqual(["import"]);
    expect(
      getApplyRefreshTargets({ event: "apply_agent_run_updated", agent_run_id: "agent-1" }, ""),
    ).toEqual(["agent"]);
  });

  it("refreshes the selected detail when a rebuild invalidates all projections", () => {
    expect(
      getApplyRefreshTargets({ event: "apply_overview_invalidated" }, "opp-1"),
    ).toEqual(["overview", "opportunities", "opportunity", "tasks"]);
  });
});
