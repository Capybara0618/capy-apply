import { describe, expect, it } from "vitest";

import {
  analysisModeLabel,
  jobTypeLabel,
  skippedReasonLabel,
} from "../components/apply/ApplyPages";

describe("Apply 中文标签", () => {
  it("隐藏增量分析内部枚举", () => {
    expect(analysisModeLabel("opportunity_agent")).toBe("机会 Agent");
    expect(analysisModeLabel("cold_projection")).toBe("冷会话投影");
    expect(skippedReasonLabel("no_new_messages")).toBe("没有新增消息");
  });

  it("后台任务类型全部使用中文", () => {
    expect(jobTypeLabel("trigger_import_analysis")).toBe("增量消息分流");
    expect(jobTypeLabel("analyze_job_fit")).toBe("岗位契合度评分");
  });
});
