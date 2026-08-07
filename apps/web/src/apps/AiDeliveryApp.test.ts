import { describe, expect, it } from "vitest";
import {
  deliveryMilliseconds,
  deliveryModelChain,
  deliveryPercent,
  deliveryProviderLabel,
  deliveryRouteLabel,
} from "./AiDeliveryApp";

describe("AI Delivery presentation helpers", () => {
  it("formats nullable and long latency values", () => {
    expect(deliveryMilliseconds(null)).toBe("No samples");
    expect(deliveryMilliseconds(842)).toBe("842 ms");
    expect(deliveryMilliseconds(12_450)).toBe("12.4 s");
  });

  it("labels effective providers", () => {
    expect(deliveryProviderLabel("opencode")).toBe("OpenCode");
    expect(deliveryProviderLabel("ai_manager")).toBe("AI Manager");
    expect(deliveryProviderLabel("openai")).toBe("Luna");
    expect(deliveryProviderLabel("")).toBe("Unknown");
  });

  it("renders rates and splits the declared fallback chain", () => {
    expect(deliveryPercent(0.125)).toBe("13%");
    expect(deliveryModelChain(undefined)).toEqual([]);
    expect(deliveryModelChain("OpenCode -> AI Manager -> Luna")).toEqual([
      "OpenCode",
      "AI Manager",
      "Luna",
    ]);
  });

  it("labels free chat and governed operation routes", () => {
    expect(deliveryRouteLabel("chat")).toBe("Free chat");
    expect(deliveryRouteLabel("operations_shortcut")).toBe("Operations shortcut");
    expect(deliveryRouteLabel("operations_question")).toBe("Operations question");
    expect(deliveryRouteLabel("legacy")).toBe("Legacy / unclassified");
  });
});
