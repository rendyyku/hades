import { describe, expect, it } from "vitest";
import { showDeltaColumn, TRUNCATION_NOTICE, truncationAriaNote } from "./forensics";

describe("truncation notice", () => {
  it("uses the exact ruling wording", () => {
    expect(TRUNCATION_NOTICE).toBe(
      "History truncated: deployer and launch slot unknown; buyers are earliest observed, not launch buyers",
    );
  });

  it("hides the sniper Δ column when the launch anchor is null or history is truncated", () => {
    expect(showDeltaColumn({ pool_created_slot: 100, history_truncated: false })).toBe(true);
    expect(showDeltaColumn({ pool_created_slot: null, history_truncated: false })).toBe(false);
    expect(showDeltaColumn({ pool_created_slot: 100, history_truncated: true })).toBe(false);
    expect(showDeltaColumn({ pool_created_slot: 100 })).toBe(true);
  });

  it("gives the Styx map an aria note only when truncated", () => {
    expect(truncationAriaNote({ history_truncated: true })).toMatch(/earliest observed/);
    expect(truncationAriaNote({ history_truncated: false })).toBe("");
    expect(truncationAriaNote({})).toBe("");
  });
});
