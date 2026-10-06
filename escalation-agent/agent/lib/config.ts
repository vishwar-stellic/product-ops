/**
 * Shared constants. These deliberately mirror the Python triage
 * (`product_status/escalation_report.py` and `server.py`) so the two jobs are
 * comparable side by side.
 */

/** "Analyze the last N days of email" - also the hard cap on how far back a stale lastMessageAt can reach. */
export const ESCALATION_LOOKBACK_DAYS = 3;

/** Safety valve on how many of an account's most-recent conversations are walked. */
export const MAX_CONVERSATIONS_PER_ACCOUNT = 60;

/** Raw emails from the latest batch kept per partner (for debugging / the dashboard later). */
export const RECENT_EMAILS_MAX = 25;

/** Vitally conversation sources the triage looks at. */
export const ELIGIBLE_SOURCES = ["google", "intercom"] as const;

/** How many partners are triaged in parallel (the Python job uses 8). */
export const PARTNER_CONCURRENCY = 8;

/** Run slots - Mon-Fri, these local hours in America/New_York (same as server.py's _ESCALATION_RUN_HOURS). */
export const ESCALATION_RUN_HOURS = [8, 10, 12, 14, 16, 18] as const;
export const ESCALATION_TIMEZONE = "America/New_York";

/** A sweep lock older than this is treated as abandoned (the run crashed or timed out). */
export const SWEEP_LOCK_TTL_MS = 30 * 60_000;

export const DEFAULT_OPENAI_MODEL = "gpt-5-mini";
export const DEFAULT_OPENAI_BASE_URL = "https://us.api.openai.com/v1";

/** Feedback calibration: at most this many labeled examples are injected into a prompt. */
export const FEEDBACK_MAX_EXAMPLES = 20;
/** Only the most recent alert records are read when building the feedback block. */
export const FEEDBACK_MAX_ALERTS_SCANNED = 150;
/** Each example's quote/headline/reason text is clipped to keep the prompt bounded. */
export const FEEDBACK_TEXT_CLIP = 220;
