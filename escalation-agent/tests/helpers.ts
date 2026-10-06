import type { Store } from "../agent/lib/store";
import type { VitallyApi } from "../agent/lib/vitally";
import type { VitallyConversation } from "../agent/lib/filters";

/** In-memory Store; listKeys returns newest-written first. */
export function memoryStore(): Store & { data: Map<string, string> } {
  const data = new Map<string, string>();
  const order: string[] = [];
  return {
    data,
    async getJson<T>(key: string) {
      const raw = data.get(key);
      return raw === undefined ? null : (JSON.parse(raw) as T);
    },
    async putJson(key, value) {
      data.set(key, JSON.stringify(value));
      const i = order.indexOf(key);
      if (i >= 0) order.splice(i, 1);
      order.push(key);
    },
    async listKeys(prefix, limit = 1000) {
      return order
        .filter((k) => k.startsWith(prefix))
        .reverse()
        .slice(0, limit);
    },
  };
}

/** Fake Vitally API over a fixed per-account list of full conversations (newest first). */
export function fakeVitally(byAccount: Record<string, VitallyConversation[]>): VitallyApi & { fullFetches: string[] } {
  const fullFetches: string[] = [];
  const all = new Map<string, VitallyConversation>();
  for (const list of Object.values(byAccount)) for (const c of list) all.set(c.id, c);
  return {
    fullFetches,
    async *listAccountConversations(accountId) {
      for (const c of byAccount[accountId] ?? []) {
        // Summaries have no messages array.
        const { messages: _messages, ...summary } = c;
        yield summary as VitallyConversation;
      }
    },
    async getConversation(id) {
      fullFetches.push(id);
      const c = all.get(id);
      if (!c) throw new Error(`no conversation ${id}`);
      return c;
    },
  };
}

export const inbound = (timestamp: string, body: string, fromId = "u1") => ({
  type: "inbound",
  timestamp,
  message: `<p>${body}</p>`,
  from: { id: fromId },
});

export const conversation = (
  id: string,
  subject: string,
  updatedAt: string,
  messages: ReturnType<typeof inbound>[],
  source = "google",
): VitallyConversation => ({
  id,
  subject,
  source,
  updatedAt,
  users: [{ id: "u1", name: "Pat Partner", email: "pat@school.edu" }],
  messages,
});

export function itemJson(over: Record<string, unknown> = {}) {
  return {
    headline: "Registration blocked in Prod",
    score: 5,
    isFire: true,
    severity: "LIVE_FIRE",
    severityReason: "Many students blocked in production past the add deadline.",
    blastRadius: "hundreds of students",
    environment: "production",
    datedEvent: null,
    evidence: [{ quote: "Students cannot register and the deadline passed", sender: "Pat Partner", date: "2026-10-05T14:00:00Z" }],
    blockedOn: "us",
    blockedOnReason: "Needs a fix from Stellic",
    lastMovementAt: "2026-10-05T14:00:00Z",
    from: "Pat Partner",
    subject: "Registration down",
    lastEmailDate: "2026-10-05T14:00:00Z",
    ...over,
  };
}
