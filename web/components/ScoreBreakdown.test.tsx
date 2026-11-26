import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { leadText } from "@/lib/signalView";
import { ScoreBreakdown } from "./ScoreBreakdown";

describe("ScoreBreakdown", () => {
  it("renders all five rows with the totals from props", () => {
    const html = renderToStaticMarkup(
      <ScoreBreakdown detail={{ base: 40, priority_chain: 15, earliness: 15, fusion: 20, total: 90 }} />,
    );
    for (const label of ["Dasar", "Chain prioritas", "Earliness", "Fusion", "Total"]) expect(html).toContain(label);
    expect(html).toContain(">90<");
    expect(html).toContain(">+20<");
  });

  it("explains a missing score", () => {
    expect(renderToStaticMarkup(<ScoreBreakdown detail={null} />)).toContain("Belum dinilai");
  });
});

describe("leadText", () => {
  it("shows hours below 72 h and days above", () => {
    expect(leadText(50)).toBe("50 jam setelah sinyal");
    expect(leadText(80)).toBe("3 hari setelah sinyal");
    expect(leadText(0.4)).toBe("0 jam setelah sinyal");
  });
});
