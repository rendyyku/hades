import { Check, X } from "lucide-react";
import { gateRows, type GateResult, type GateStatus } from "@/lib/arena";

const TH = "px-3 py-2 text-left text-xs font-normal uppercase tracking-[0.14em] text-muted whitespace-nowrap";
const TD = "px-3 py-2 align-middle";

function GateTable({ title, result, rows }: { title: string; result: GateResult; rows: ReturnType<typeof gateRows> }) {
  return (
    <div className="rounded-lg border border-border bg-surface">
      <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-3">
        <h3 className="text-sm font-medium text-fg">{title}</h3>
        <span
          className={`rounded border px-1.5 py-0.5 font-mono text-[11px] uppercase tracking-wide ${
            result.passed ? "border-gain/60 text-gain" : "border-loss/60 text-loss"
          }`}
        >
          {result.passed ? "Passed" : "Not passed"}
        </span>
      </div>
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">{title}: each check with its value and threshold</caption>
        <thead className="border-b border-border">
          <tr>
            <th scope="col" className={TH}>Check</th>
            <th scope="col" className={`${TH} text-right`}>Value</th>
            <th scope="col" className={`${TH} text-right`}>Threshold</th>
            <th scope="col" className={`${TH} text-right`}>Result</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-b border-border last:border-b-0">
              <th scope="row" className={`${TD} text-left font-normal text-fg`}>{r.label}</th>
              <td className={`${TD} text-right font-mono tabular-nums text-fg`}>{r.value}</td>
              <td className={`${TD} text-right font-mono tabular-nums text-muted`}>{r.threshold}</td>
              <td className={`${TD} text-right`}>
                <span className={`inline-flex items-center gap-1 font-mono text-xs ${r.passed ? "text-gain" : "text-loss"}`}>
                  {r.passed ? <Check aria-hidden="true" className="size-3.5" /> : <X aria-hidden="true" className="size-3.5" />}
                  {r.passed ? "Pass" : "Fail"}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Both consistency gates of a paper follow: the full gate (promotion) and the rolling
 * gate (demotion). Values, thresholds and pass/fail all come from the head API.
 */
export function GateChecklist({ gate }: { gate: GateStatus | null }) {
  if (!gate) {
    return (
      <p className="rounded-lg border border-border bg-surface p-5 text-sm text-muted">
        The gate is not evaluated yet: the paper runtime has not produced a snapshot for this follow.
      </p>
    );
  }
  const config = gate.config ?? {};
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <GateTable title="Full gate" result={gate.full} rows={gateRows(gate.full, config, "full")} />
      <GateTable title="Rolling gate" result={gate.rolling} rows={gateRows(gate.rolling, config, "rolling")} />
    </div>
  );
}
