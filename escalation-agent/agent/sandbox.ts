import { defineSandbox } from "eve/sandbox";
import { JustBashSandbox } from "eve/sandbox/just-bash";

// The agent has no tools that use a sandbox. A pure-JS sandbox keeps local
// builds and deploys from provisioning a VM/container it will never use.
export const environment = JustBashSandbox.environment({ autoInstall: false });
export default defineSandbox(() => environment.open());
