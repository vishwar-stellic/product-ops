import { localDev, vercelOidc } from "eve/channels/auth";
import { eveChannel } from "eve/channels/eve";

// This agent has no user-facing API. Keep the HTTP session routes locked to
// Vercel OIDC callers (and local dev) rather than leaving them open.
export default eveChannel({
  auth: [vercelOidc(), localDev()],
});
