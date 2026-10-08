import { ESCALATION_RUN_HOURS, ESCALATION_TIMEZONE } from "./config";

/**
 * Whether `now`, in real America/New_York local time, falls on one of the
 * 2-hour business-hours slots (Mon-Fri, 8/10/12/14/16/18). Eve crons run in UTC, so the
 * cron is an hourly superset and this does the gating in local time, which
 * stays correct across daylight saving.
 */
export function inRunWindow(now: Date = new Date()): boolean {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: ESCALATION_TIMEZONE,
    weekday: "short",
    hour: "numeric",
    hourCycle: "h23",
  }).formatToParts(now);
  const weekday = parts.find((p) => p.type === "weekday")?.value ?? "";
  const hour = Number(parts.find((p) => p.type === "hour")?.value);
  const isWeekday = ["Mon", "Tue", "Wed", "Thu", "Fri"].includes(weekday);
  return isWeekday && (ESCALATION_RUN_HOURS as readonly number[]).includes(hour);
}
