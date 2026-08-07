import { describe, expect, it } from "vitest";
import { parseApiDate } from "./format";

describe("parseApiDate", () => {
  it("interprets timezone-less API timestamps as UTC", () => {
    expect(parseApiDate("2026-07-16T12:34:56").toISOString()).toBe("2026-07-16T12:34:56.000Z");
  });

  it("preserves explicit UTC timestamps", () => {
    expect(parseApiDate("2026-07-16T12:34:56Z").toISOString()).toBe("2026-07-16T12:34:56.000Z");
  });

  it("honors explicit timezone offsets", () => {
    expect(parseApiDate("2026-07-16T14:34:56+02:00").toISOString()).toBe("2026-07-16T12:34:56.000Z");
  });
});
