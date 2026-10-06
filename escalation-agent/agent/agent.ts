import { defineAgent } from "eve";

// This agent is a background worker (see instructions.md): the triage model
// calls happen in agent/lib/llm.ts, not in agent sessions. Keep the agent
// itself minimal - no optional default tools (bash, file access, web fetch...)
// - so nothing that can reach partner email content is ever exposed to a chat.
export default defineAgent({
  model: "openai/gpt-5-mini",
  defaultTools: false,
});
