import type { ApplyEvent } from "./event-client";

export type ApplyRefreshTarget =
  | "health"
  | "jobs"
  | "import"
  | "overview"
  | "opportunities"
  | "opportunity"
  | "agent"
  | "tasks"
  | "profile";

export function getApplyRefreshTargets(
  event: ApplyEvent,
  selectedOpportunityId: string,
): ApplyRefreshTarget[] {
  switch (event.event) {
    case "apply_health_changed":
      return ["health"];
    case "apply_job_updated":
      return ["jobs"];
    case "apply_import_updated":
      return ["import"];
    case "apply_overview_invalidated":
      return selectedOpportunityId
        ? ["overview", "opportunities", "opportunity", "tasks"]
        : ["overview", "opportunities", "tasks"];
    case "apply_agent_run_updated":
      return ["agent"];
    case "apply_opportunity_updated": {
      const targets: ApplyRefreshTarget[] = ["overview", "opportunities", "tasks"];
      if (
        selectedOpportunityId
        && (!event.opportunity_id || event.opportunity_id === selectedOpportunityId)
      ) {
        targets.push("opportunity");
      }
      return targets;
    }
    case "apply_profile_pdf_uploaded":
      return ["profile"];
    default:
      return [];
  }
}
