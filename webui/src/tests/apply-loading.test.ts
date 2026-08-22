import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ApplyInitialLoading } from "../components/apply/ApplyView";

describe("Apply 首次加载", () => {
  it("在事实库返回前展示加载态，而不是假空账号", () => {
    const html = renderToStaticMarkup(createElement(ApplyInitialLoading));

    expect(html).toContain("正在读取 PostgreSQL 中的本地机会记录");
    expect(html).not.toContain("暂无账号记录");
  });
});
