import { CloudOff } from "lucide-react";
import { getHead, type HeadId } from "@/lib/heads";

/** A head that timed out or is down. Names the env vars, never their values. */
export function HeadOffline({ head, status }: { head: HeadId; status?: number }) {
  const h = getHead(head);
  return (
    <div role="alert" className="flex items-start gap-3 rounded-lg border border-loss/40 bg-loss/10 p-5 text-fg">
      <CloudOff aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-loss" />
      <div>
        <p className="font-medium">
          {h.label} head {status ? `error · HTTP ${status}` : "offline"}
        </p>
        <p className="mt-1 text-sm text-muted">
          Check that the {h.label} head is running and that {h.baseUrlEnv} / {h.keyEnv} are set for the web app.
        </p>
      </div>
    </div>
  );
}
