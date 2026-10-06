import { defineSchedule } from "eve/schedules";

import { inRunWindow } from "../lib/window";
import { startConfiguredRun } from "../lib/run";

/**
 * Escalation sweep, same cadence as the Python job: every 2 hours, Mon-Fri,
 * 8am-6pm America/New_York. Eve schedules run in UTC, so the cron fires hourly
 * and the real gating happens here in Eastern local time (correct across DST).
 * Outside a slot this returns immediately and costs nothing.
 *
 * (Set ESCALATION_IGNORE_WINDOW=1 to sweep on every hourly fire - testing only.
 * To run on demand at any time, use POST /escalation/run - see channels/run.ts.)
 */
export default defineSchedule({
  cron: "0 * * * *",
  async run() {
    if (process.env.ESCALATION_IGNORE_WINDOW !== "1" && !inRunWindow()) {
      console.log("[escalation-agent] outside the Mon-Fri 8am-6pm ET run slots - skipping");
      return;
    }
    const run = await startConfiguredRun("schedule");
    if (!run) {
      console.log("[escalation-agent] a sweep is already running - skipping this scheduled run");
      return;
    }
    const result = await run.finished();
    if (result.status === "failed") throw new Error(result.error ?? "sweep failed");
  },
});
