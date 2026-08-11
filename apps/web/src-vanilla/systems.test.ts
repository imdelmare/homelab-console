import { describe, expect, it } from "vitest";
import { providerAttention } from "./systems";

describe("vanilla systems", () => {
  it("maps provider states to concise attention levels", () => {
    expect(providerAttention("healthy")).toBe("healthy");
    expect(providerAttention("degraded")).toBe("warning");
    expect(providerAttention("unreachable")).toBe("critical");
  });
});
