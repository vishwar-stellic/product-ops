import { randomUUID } from "node:crypto";

import { SWEEP_LOCK_TTL_MS } from "./config";
import type { Store } from "./store";
import type { SweepSummary } from "./sweep";

export type RunTrigger = "schedule" | "manual";

/** Persisted at `runs/last.json` - what the status endpoint reports. */
export interface RunRecord {
  runId: string;
  trigger: RunTrigger;
  dryRun: boolean;
  /** Saved state but posted nothing (baseline run). */
  seed?: boolean;
  startedAt: string;
  finishedAt?: string;
  status: "running" | "succeeded" | "failed";
  summary?: SweepSummary;
  error?: string;
}

interface LockRecord {
  runId: string;
  trigger: RunTrigger;
  startedAt: string;
  releasedAt?: string;
}

export const LOCK_KEY = "locks/sweep.json";
export const LAST_RUN_KEY = "runs/last.json";

export interface ActiveRun {
  record: RunRecord;
  /** Runs `work`, records the outcome, and always releases the lock. Never throws. */
  complete(work: () => Promise<SweepSummary>): Promise<RunRecord>;
}

/**
 * Claims the single sweep lock so a manual run and the scheduled run (or two
 * manual runs) never triage and alert at the same time. Returns null when a
 * run that started less than SWEEP_LOCK_TTL_MS ago is still going. The lock
 * is best-effort (a blob store has no compare-and-swap): after writing, we
 * read it back and only proceed if our own runId won.
 */
export async function startRun(
  store: Store,
  meta: { trigger: RunTrigger; dryRun: boolean; seed?: boolean },
  now: Date = new Date(),
  ttlMs: number = SWEEP_LOCK_TTL_MS,
): Promise<ActiveRun | null> {
  const held = await store.getJson<LockRecord>(LOCK_KEY);
  if (held && !held.releasedAt && now.getTime() - Date.parse(held.startedAt) < ttlMs) return null;

  const runId = randomUUID();
  const startedAt = now.toISOString();
  await store.putJson(LOCK_KEY, { runId, trigger: meta.trigger, startedAt } satisfies LockRecord);
  const confirmed = await store.getJson<LockRecord>(LOCK_KEY);
  if (confirmed?.runId !== runId) return null;

  const record: RunRecord = {
    runId,
    trigger: meta.trigger,
    dryRun: meta.dryRun,
    ...(meta.seed ? { seed: true } : {}),
    startedAt,
    status: "running",
  };
  await store.putJson(LAST_RUN_KEY, record);

  return {
    record,
    async complete(work) {
      let final: RunRecord;
      try {
        const summary = await work();
        final = { ...record, status: "succeeded", summary, finishedAt: new Date().toISOString() };
      } catch (error) {
        final = {
          ...record,
          status: "failed",
          error: error instanceof Error ? error.message : String(error),
          finishedAt: new Date().toISOString(),
        };
      }
      try {
        await store.putJson(LAST_RUN_KEY, final);
        await store.putJson(LOCK_KEY, {
          runId,
          trigger: meta.trigger,
          startedAt,
          releasedAt: final.finishedAt,
        } satisfies LockRecord);
      } catch (error) {
        console.error("[escalation-agent] failed to record run outcome", error);
      }
      return final;
    },
  };
}

export async function readRunStatus(
  store: Store,
  now: Date = new Date(),
  ttlMs: number = SWEEP_LOCK_TTL_MS,
): Promise<{ running: boolean; lastRun: RunRecord | null }> {
  const [lock, lastRun] = await Promise.all([
    store.getJson<LockRecord>(LOCK_KEY),
    store.getJson<RunRecord>(LAST_RUN_KEY),
  ]);
  const running = Boolean(lock && !lock.releasedAt && now.getTime() - Date.parse(lock.startedAt) < ttlMs);
  return { running, lastRun };
}
