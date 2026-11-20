import { TriangleAlert } from "lucide-react";

export function ErrorCard({ error }: { error: string }) {
  return (
    <div
      role="alert"
      className="flex items-start gap-3 rounded-lg border border-loss/40 bg-loss/10 p-5 text-fg"
    >
      <TriangleAlert aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-loss" />
      <div>
        <p className="font-medium">Engine unreachable · {error}</p>
        <p className="mt-1 text-sm text-muted">
          Check that the HADES engine is running (<code className="font-mono">hades serve</code>) and that
          HADES_API_URL / HADES_API_KEY are set for the web app.
        </p>
      </div>
    </div>
  );
}
