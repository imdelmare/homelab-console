import { describe, expect, it } from "vitest";
import { buildApiReadyYaml, providerFamily, validateApiReadyDraft } from "./providerManager";
import type { Provider, ProviderDefinition } from "./types";

const provider: Provider = {
  id: "homeassistant",
  name: "Home Assistant",
  status: "healthy",
  last_ok_at: null,
  checked_at: null,
  detail: null,
  tool_count: 1,
  watchers: [],
  last_error: null,
};

const definition: ProviderDefinition = {
  id: "homeassistant",
  name: "Home Assistant",
  transport: "http_json",
  driver_id: "homeassistant",
  configuration_keys: [],
  capability_tool_ids: [],
  observation_ids: [],
  supports_instances: false,
};

describe("provider manager helpers", () => {
  it("keeps special providers outside standard transports", () => {
    expect(providerFamily({ ...provider, id: "fritzbox_primary" })).toBe("special");
    expect(providerFamily(provider, definition)).toBe("standard");
  });

  it("recognizes standard TCP drivers without treating them as API-ready", () => {
    expect(
      providerFamily(
        { ...provider, id: "asterisk" },
        { ...definition, id: "asterisk", transport: "tcp_text", driver_id: "asterisk_ami_v1" },
      ),
    ).toBe("standard-tcp");
  });

  it("recognizes configuration-driven API-ready instances", () => {
    expect(
      providerFamily(
        { ...provider, id: "paperless" },
        { ...definition, id: "paperless", driver_id: "json_health_v1", supports_instances: true },
      ),
    ).toBe("api-ready");
    expect(
      providerFamily(
        { ...provider, id: "cloudflare_home" },
        {
          ...definition,
          id: "cloudflare_home",
          driver_id: "cloudflare_tunnel_v1",
          supports_instances: true,
        },
      ),
    ).toBe("api-ready");
  });

  it("rejects unsafe or duplicate drafts", () => {
    const errors = validateApiReadyDraft(
      {
        id: "homeassistant",
        name: "",
        baseUrl: "https://user:password@example.test",
        verifyTls: true,
        timeoutSeconds: 60,
      },
      new Set(["homeassistant"]),
    );
    expect(errors.id).toMatch(/already/);
    expect(errors.name).toBeTruthy();
    expect(errors.baseUrl).toMatch(/credentials/);
    expect(errors.timeoutSeconds).toBeTruthy();
  });

  it("generates only the fixed json_health_v1 YAML contract", () => {
    const yaml = buildApiReadyYaml({
      id: "paperless",
      name: "Paperless NGX",
      baseUrl: "https://paperless.internal/",
      verifyTls: true,
      timeoutSeconds: 5,
    });
    expect(yaml).toContain("driver: json_health_v1");
    expect(yaml).toContain('base_url: "https://paperless.internal"');
    expect(yaml).not.toContain("health_path");
  });
});
