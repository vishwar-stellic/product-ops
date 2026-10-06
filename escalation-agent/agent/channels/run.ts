import { defineChannel, GET, POST } from "eve/channels";

import { checkBearer } from "../lib/auth";
import { startConfiguredRun } from "../lib/run";
import { readRunStatus } from "../lib/runs";
import { getStore } from "../lib/store";

/**
 * On-demand control surface for the escalation sweep. Authenticated with the
 * same `Authorization: Bearer <CRON_SECRET>` the dashboard uses (fails closed
 * with 503 when CRON_SECRET is unset). It does not start an agent session; it
 * just runs the same sweep the schedule runs.
 *
 *   POST /escalation/run            start a sweep now  -> 202 { runId, dryRun }
 *   POST /escalation/run  {"dryRun": true}   same, but log alerts instead of posting
 *   GET  /escalation/run            status of the current / last run
 *
 * A run is refused with 409 while another sweep (scheduled or manual) is in
 * flight. The scheduled cadence is unaffected by manual runs.
 */
function reject(request: Request): Response | null {
  const auth = checkBearer(request.headers.get("authorization"), process.env.CRON_SECRET);
  if (auth === "ok") return null;
  if (auth === "not-configured") return Response.json({ error: "CRON_SECRET is not configured" }, { status: 503 });
  return Response.json({ error: "unauthorized" }, { status: 401 });
}

export default defineChannel({
  routes: [
    POST("/escalation/run", async (request, { waitUntil }) => {
      const denied = reject(request);
      if (denied) return denied;

      let dryRun = false;
      try {
        const body = (await request.json()) as { dryRun?: unknown } | null;
        dryRun = body?.dryRun === true;
      } catch {
        // No / invalid JSON body: run with defaults.
      }

      const run = await startConfiguredRun("manual", dryRun);
      if (!run) {
        return Response.json({ error: "a sweep is already running" }, { status: 409 });
      }
      // The sweep takes minutes; answer now and let it finish in the background.
      waitUntil(run.finished());
      return Response.json(
        { runId: run.record.runId, dryRun: run.record.dryRun, startedAt: run.record.startedAt, status: "running" },
        { status: 202 },
      );
    }),

    GET("/escalation/run", async (request) => {
      const denied = reject(request);
      if (denied) return denied;
      return Response.json(await readRunStatus(getStore()));
    }),
  ],
});
