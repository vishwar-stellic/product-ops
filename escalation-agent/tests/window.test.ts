import { describe, expect, it } from "vitest";
import { inRunWindow } from "../agent/lib/window";

describe("inRunWindow (Mon-Fri, 8/10/12/14/16/18 America/New_York)", () => {
  it("is true at 8am Eastern on a weekday during daylight time (UTC-4)", () => {
    expect(inRunWindow(new Date("2026-10-05T12:00:00Z"))).toBe(true); // Mon
  });
  it("is false an hour off the slot", () => {
    expect(inRunWindow(new Date("2026-10-05T13:00:00Z"))).toBe(false); // 9am ET
  });
  it("tracks real local time across DST (UTC-5 in winter)", () => {
    expect(inRunWindow(new Date("2027-01-04T13:00:00Z"))).toBe(true); // Mon 8am EST
    expect(inRunWindow(new Date("2027-01-04T12:00:00Z"))).toBe(false); // Mon 7am EST
  });
  it("covers the last slot at 6pm and nothing at 8pm", () => {
    expect(inRunWindow(new Date("2026-10-05T22:00:00Z"))).toBe(true); // 6pm EDT
    expect(inRunWindow(new Date("2026-10-06T00:00:00Z"))).toBe(false); // 8pm EDT
  });
  it("is false on weekends", () => {
    expect(inRunWindow(new Date("2026-10-10T12:00:00Z"))).toBe(false); // Sat 8am ET
    expect(inRunWindow(new Date("2026-10-11T14:00:00Z"))).toBe(false); // Sun 10am ET
  });
  it("has exactly the six weekday slots", () => {
    let hits = 0;
    for (let h = 0; h < 24; h++) if (inRunWindow(new Date(Date.UTC(2026, 9, 7, h)))) hits++; // Wed
    expect(hits).toBe(6);
  });
});
