import { describe, expect, it } from "vitest";
import { createOpenAiLlm } from "../agent/lib/llm";
import { updateEscalations } from "../agent/lib/triage";
import type { SourceEmail } from "../agent/lib/vitally";

/**
 * LIVE regression suite - calls the real model. Skipped unless
 * LIVE_CALIBRATION=1 and OPENAI_API_KEY are set (`npm run calibrate`).
 *
 * Each case is a synthetic partner email modelled on one of the human-scored
 * calibration examples in the rubric (agent/lib/triage-rubric.ts).
 * The check is deliberately coarse - "fire (score >= 4)" vs "not a fire" -
 * because that's the line the Slack alerts are drawn on. Run it before
 * deploying a change that could shift scoring (new feedback logic, a different
 * model) to catch drift away from the human-scored examples.
 */
const live = process.env.LIVE_CALIBRATION === "1" && !!process.env.OPENAI_API_KEY;

const mail = (subject: string, body: string): SourceEmail => ({
  from: "Jordan Registrar",
  subject,
  date: "2026-10-05T14:00:00Z",
  body,
  vitallyConversationId: "calibration",
});

const cases: Array<{ name: string; fire: boolean; email: SourceEmail }> = [
  {
    name: "5: students cannot add classes in Prod, add deadline passed",
    fire: true,
    email: mail(
      "URGENT: students can't add classes",
      "In our production environment, hundreds of students have been unable to add or enroll in classes all day. The add deadline passed at noon today and they are now locked out of their schedules. We need this fixed immediately.",
    ),
  },
  {
    name: "4: production seat availability API returning 500s",
    fire: true,
    email: mail(
      "Seat availability API down",
      "The production seat availability API has been returning 500 errors since this morning. Advisors cannot see open seats and our registration day operations are stalled.",
    ),
  },
  {
    name: "4: nightly data refresh failed in Production",
    fire: true,
    email: mail(
      "Data refresh did not run",
      "The production data refresh failed to run today so advisors are looking at yesterday's data. Can you please run an overnight refresh tonight? This is blocking our advising day.",
    ),
  },
  {
    name: "3: roadmap and ETA request",
    fire: false,
    email: mail(
      "ETA for role permissions in CARE?",
      "Could you share the roadmap and an expected timeline for role permissions in the CARE module? We would like to plan around it. No rush on a specific date.",
    ),
  },
  {
    name: "3: cannot clone students in the test environment",
    fire: false,
    email: mail(
      "Clone students failing in test",
      "We cannot clone students in our test environment. We are not live yet and nothing depends on this next week, but please take a look when you can.",
    ),
  },
  {
    name: "3: enhancement request",
    fire: false,
    email: mail(
      "Enhancement: per-student term load limits",
      "We would love a feature that lets us set term load limits per student rather than per program. Submitting this as an enhancement request.",
    ),
  },
  {
    name: "0: InfoSec response now with legal review",
    fire: false,
    email: mail(
      "Re: InfoSec questionnaire",
      "Thanks for the completed InfoSec responses. They are now with our internal legal team for review and we will follow up once they are done.",
    ),
  },
  {
    name: "0: cannot join a scheduled meeting",
    fire: false,
    email: mail("Can't make our call", "I can't join the scheduled meeting this morning, can we move it to Thursday?"),
  },
];

describe.skipIf(!live)("live calibration against the rubric's human-scored examples", () => {
  for (const c of cases) {
    it(c.name, async () => {
      const items = await updateEscalations(createOpenAiLlm(), { previousItems: [], newEmails: [c.email], feedbackBlock: "" });
      expect(items, "the model call must succeed").not.toBeNull();
      const maxScore = Math.max(-1, ...(items ?? []).map((i) => i.score));
      if (c.fire) expect(maxScore, JSON.stringify(items)).toBeGreaterThanOrEqual(4);
      else expect(maxScore, JSON.stringify(items)).toBeLessThan(4);
    }, 90_000);
  }
});
