import { DEFAULT_OPENAI_BASE_URL, DEFAULT_OPENAI_MODEL } from "./config";

/** One prompt in, raw assistant text out. Injected so tests never hit the network. */
export type LlmFn = (prompt: string) => Promise<string>;

/**
 * One user-turn Chat Completions request, with reasoning_effort "low" and
 * max_completion_tokens 6000.
 */
export function createOpenAiLlm(
  apiKey = process.env.OPENAI_API_KEY,
  baseUrl = process.env.OPENAI_BASE_URL || DEFAULT_OPENAI_BASE_URL,
  model = process.env.OPENAI_MODEL || DEFAULT_OPENAI_MODEL,
): LlmFn {
  if (!apiKey) throw new Error("OPENAI_API_KEY is not set - see .env.example");
  return async (prompt) => {
    const response = await fetch(`${baseUrl.replace(/\/+$/, "")}/chat/completions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        model,
        max_completion_tokens: 6000,
        reasoning_effort: "low",
        messages: [{ role: "user", content: prompt.slice(0, 60000) }],
      }),
      signal: AbortSignal.timeout(60_000),
    });
    if (!response.ok) {
      throw new Error(`OpenAI error ${response.status}: ${(await response.text()).slice(0, 300)}`);
    }
    const body = (await response.json()) as {
      choices?: Array<{ finish_reason?: string; message?: { content?: string | null } }>;
    };
    const choice = body.choices?.[0];
    const content = choice?.message?.content;
    if (!content) throw new Error(`OpenAI returned no content (finish_reason=${choice?.finish_reason})`);
    return content;
  };
}
