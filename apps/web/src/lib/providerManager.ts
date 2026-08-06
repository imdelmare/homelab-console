import type { Provider, ProviderDefinition } from "./types";

export type ProviderFamily = "standard" | "standard-tcp" | "api-ready" | "special";

export type ApiReadyDraft = {
  id: string;
  name: string;
  baseUrl: string;
  verifyTls: boolean;
  timeoutSeconds: number;
};

export const SPECIAL_PROVIDER_IDS = new Set([
  "fritzbox_primary",
  "fritzbox_secondary",
]);

export const SPECIAL_PROVIDER_PROTOCOLS: Record<string, string> = {
  fritzbox_primary: "SOAP / TR-064",
  fritzbox_secondary: "SOAP / TR-064",
};

export function providerFamily(
  provider: Provider,
  definition?: ProviderDefinition,
): ProviderFamily {
  if (SPECIAL_PROVIDER_IDS.has(provider.id)) return "special";
  if (definition?.transport === "tcp_text") return "standard-tcp";
  if (definition?.supports_instances) {
    return "api-ready";
  }
  return "standard";
}

export function validateApiReadyDraft(
  draft: ApiReadyDraft,
  existingIds: ReadonlySet<string> = new Set(),
): Record<string, string> {
  const errors: Record<string, string> = {};
  const id = draft.id.trim();
  if (!/^[a-z][a-z0-9_]{1,62}$/.test(id)) {
    errors.id = "Use 2–63 lowercase letters, numbers or underscores; start with a letter.";
  } else if (existingIds.has(id)) {
    errors.id = "This provider ID is already registered.";
  }
  if (!draft.name.trim()) errors.name = "Display name is required.";
  try {
    const url = new URL(draft.baseUrl);
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error("protocol");
    if (url.username || url.password) errors.baseUrl = "Do not embed credentials in the URL.";
  } catch {
    errors.baseUrl = "Enter a complete HTTP or HTTPS base URL.";
  }
  if (!Number.isFinite(draft.timeoutSeconds) || draft.timeoutSeconds < 0.5 || draft.timeoutSeconds > 30) {
    errors.timeoutSeconds = "Timeout must be between 0.5 and 30 seconds.";
  }
  return errors;
}

export function buildApiReadyYaml(draft: ApiReadyDraft): string {
  return [
    "api_provider_instances:",
    `  - id: ${draft.id.trim()}`,
    `    name: ${JSON.stringify(draft.name.trim())}`,
    "    driver: json_health_v1",
    `    base_url: ${JSON.stringify(draft.baseUrl.replace(/\/+$/, ""))}`,
    `    verify_tls: ${draft.verifyTls ? "true" : "false"}`,
    `    timeout_seconds: ${draft.timeoutSeconds}`,
  ].join("\n");
}
