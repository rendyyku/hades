import { describe, expect, it } from "vitest";
import { NOTE_MAX, validateWatchInput } from "./watchForm";

const ADDR = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU";

describe("validateWatchInput", () => {
  it("accepts a valid address with and without a note", () => {
    expect(validateWatchInput({ address: ADDR, note: "" })).toEqual({ ok: true });
    expect(validateWatchInput({ address: ADDR, note: "smart dev" })).toEqual({ ok: true });
  });

  it("trims surrounding whitespace from the address", () => {
    expect(validateWatchInput({ address: `  ${ADDR}\n`, note: "" })).toEqual({ ok: true });
  });

  it("requires an address", () => {
    expect(validateWatchInput({ address: "   ", note: "" })).toEqual({
      ok: false,
      field: "address",
      message: "Enter a wallet address.",
    });
  });

  it("rejects non-base58 and wrong-length addresses", () => {
    for (const bad of ["0OIl" + "a".repeat(30), "short", "a".repeat(45), `${ADDR.slice(0, -1)}!`]) {
      const r = validateWatchInput({ address: bad, note: "" });
      expect(r.ok).toBe(false);
      if (!r.ok) {
        expect(r.field).toBe("address");
        expect(r.message).toMatch(/base58/i);
      }
    }
  });

  it("limits the note to 280 characters", () => {
    expect(NOTE_MAX).toBe(280);
    expect(validateWatchInput({ address: ADDR, note: "x".repeat(280) })).toEqual({ ok: true });
    const r = validateWatchInput({ address: ADDR, note: "x".repeat(281) });
    expect(r).toEqual({ ok: false, field: "note", message: "Note must be 280 characters or fewer (281)." });
  });

  it("reports the address error first when both fields are invalid", () => {
    const r = validateWatchInput({ address: "bad", note: "x".repeat(300) });
    expect(r.ok === false && r.field).toBe("address");
  });
});
