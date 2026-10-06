import { createHash, timingSafeEqual } from "node:crypto";

export type AuthResult = "ok" | "unauthorized" | "not-configured";

const digest = (value: string) => createHash("sha256").update(value).digest();

/**
 * Checks `Authorization: Bearer <secret>` against the expected secret in
 * constant time. Fails closed: an unset secret is "not-configured" (503), never
 * an open endpoint.
 */
export function checkBearer(header: string | null | undefined, secret: string | undefined): AuthResult {
  if (!secret) return "not-configured";
  const match = /^Bearer\s+(.+)$/i.exec(header ?? "");
  if (!match) return "unauthorized";
  return timingSafeEqual(digest(match[1]!.trim()), digest(secret)) ? "ok" : "unauthorized";
}
