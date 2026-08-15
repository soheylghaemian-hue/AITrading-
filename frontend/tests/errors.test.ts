import { describe, it, expect } from "vitest";
import { translateError, humanStatus } from "@/lib/errors";

describe("error translation — friendly title, raw code only in details", () => {
  it("maps known IBKR codes to human titles and keeps the raw code in detail", () => {
    const md = translateError(10089, "subscription required");
    expect(md.title).toBe("Market data unavailable");
    expect(md.detail).toContain("IBKR 10089");

    const broker = translateError(10141);
    expect(broker.title).toBe("Broker requires attention");
    expect(broker.detail).toBe("IBKR 10141");
  });

  it("unknown numeric codes stay friendly, never a bare number on the primary title", () => {
    const t = translateError(99999);
    expect(t.title).toBe("Market data unavailable");
    expect(t.detail).toContain("IBKR 99999");
    expect(t.title).not.toMatch(/\d/);
  });

  it("raw-only and empty inputs degrade gracefully", () => {
    expect(translateError(null, "boom")).toEqual({ title: "Requires attention", detail: "boom" });
    expect(translateError(null, null).detail).toBeNull();
  });

  it("humanStatus never leaks the raw enum on primary screens", () => {
    expect(humanStatus("DATA_NOT_AVAILABLE")).toBe("Market data unavailable");
    expect(humanStatus("DATA_AVAILABLE")).toBe("Live");
    expect(humanStatus("DELAYED")).toBe("Delayed");
    expect(humanStatus(null)).toBe("NO DATA");
  });
});
