import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ScrollRegion } from "./ScrollRegion";

describe("ScrollRegion", () => {
  const html = renderToStaticMarkup(
    <ScrollRegion label="Early buyers (scrollable)" className="rounded-lg border">
      <table />
    </ScrollRegion>,
  );

  it("is a focusable, labelled region", () => {
    expect(html).toContain('role="region"');
    expect(html).toContain('tabindex="0"');
    expect(html).toContain('aria-label="Early buyers (scrollable)"');
  });

  it("contains absolute descendants (relative) and scrolls horizontally", () => {
    const cls = html.match(/class="([^"]*)"/)![1].split(/\s+/);
    expect(cls).toContain("relative");
    expect(cls).toContain("overflow-x-auto");
    expect(cls).toContain("rounded-lg");
    expect(html).toContain("<table>");
  });
});
