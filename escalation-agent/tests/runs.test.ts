import { describe, expect, it } from "vitest";
import { checkBearer } from "../agent/lib/auth";
import { LAST_RUN_KEY, LOCK_KEY, readRunStatus, startRun } from "../agent/lib/runs";
import type { SweepSummary } from "../agent/lib/sweep";
import { memoryStore } from "./helpers";

const summary: SweepSummary = {
  partners: 2,
  withNewEmails: 1,
  llmFailures: 0,
  fetchFailures: 0,
  alertsPosted: 1,
  alertFailures: 0,
  feedbackExamples: 0,
};
const meta = { trigger: "manual", dryRun: false } as const;

describe("checkBearer", () => {
  it("accepts only the exact bearer secret and fails closed when unset", () => {
    expect(checkBearer("Bearer s3cret", "s3cret")).toBe("ok");
    expect(checkBearer("bearer s3cret", "s3cret")).toBe("ok");
    expect(checkBearer("Bearer wrong", "s3cret")).toBe("unauthorized");
    expect(checkBearer("s3cret", "s3cret")).toBe("unauthorized");
    expect(checkBearer(null, "s3cret")).toBe("unauthorized");
    expect(checkBearer("Bearer s3cret", undefined)).toBe("not-configured");
    expect(checkBearer("Bearer ", "")).toBe("not-configured");
  });
});

describe("startRun lock", () => {
  it("records success, releases the lock, and allows the next run", async () => {
    const store = memoryStore();
    const first = await startRun(store, meta);
    expect(first).not.toBeNull();
    expect((await readRunStatus(store)).running).toBe(true);

    const done = await first!.complete(async () => summary);
    expect(done.status).toBe("succeeded");
    expect(done.summary).toEqual(summary);

    const status = await readRunStatus(store);
    expect(status.running).toBe(false);
    expect(status.lastRun?.runId).toBe(first!.record.runId);
    expect(await startRun(store, { trigger: "schedule", dryRun: true })).not.toBeNull();
  });

  it("refuses a second run while one is in flight", async () => {
    const store = memoryStore();
    const first = await startRun(store, meta);
    expect(first).not.toBeNull();
    expect(await startRun(store, { trigger: "schedule", dryRun: false })).toBeNull();
    expect(await startRun(store, meta)).toBeNull();
  });

  it("records a failure (without throwing) and still releases the lock", async () => {
    const store = memoryStore();
    const run = await startRun(store, meta);
    const done = await run!.complete(async () => {
      throw new Error("registry down");
    });
    expect(done.status).toBe("failed");
    expect(done.error).toBe("registry down");
    expect((await store.getJson<{ status: string }>(LAST_RUN_KEY))?.status).toBe("failed");
    expect((await readRunStatus(store)).running).toBe(false);
    expect(await startRun(store, meta)).not.toBeNull();
  });

  it("treats a stale lock (crashed run) as abandoned", async () => {
    const store = memoryStore();
    const long = new Date(Date.now() - 31 * 60_000);
    expect(await startRun(store, meta, long)).not.toBeNull();
    expect((await readRunStatus(store)).running).toBe(false);
    expect(await startRun(store, meta)).not.toBeNull();
  });

  it("loses the race when another run overwrote the lock first", async () => {
    const store = memoryStore();
    const realPut = store.putJson.bind(store);
    store.putJson = async (key, value) => {
      await realPut(key, value);
      // Someone else claims the lock right after our write.
      if (key === LOCK_KEY) await realPut(key, { runId: "other", trigger: "schedule", startedAt: new Date().toISOString() });
    };
    expect(await startRun(store, meta)).toBeNull();
  });
});

describe("dry-run store and reset", () => {
  it("dry-run store reads real data but persists nothing", async () => {
    const { createDryRunStore } = await import("../agent/lib/store");
    const base = memoryStore();
    await base.putJson("partners/a.json", { n: 1 });
    const dry = createDryRunStore(base);
    expect(await dry.getJson("partners/a.json")).toEqual({ n: 1 });
    await dry.putJson("partners/a.json", { n: 2 });
    await dry.putJson("alerts/C/1.json", { x: 1 });
    expect(await dry.getJson("partners/a.json")).toEqual({ n: 2 }); // visible within the run
    expect(await base.getJson("partners/a.json")).toEqual({ n: 1 }); // never persisted
    expect(await base.getJson("alerts/C/1.json")).toBeNull();
    await expect(dry.deleteByPrefix("partners/")).rejects.toThrow();
  });

  it("deleteByPrefix removes only that prefix", async () => {
    const store = memoryStore();
    await store.putJson("partners/a.json", 1);
    await store.putJson("partners/b.json", 2);
    await store.putJson("alerts/C/1.json", 3);
    expect(await store.deleteByPrefix("partners/")).toBe(2);
    expect(await store.getJson("partners/a.json")).toBeNull();
    expect(await store.getJson("alerts/C/1.json")).toBe(3);
  });
});
