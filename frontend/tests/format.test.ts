import { describe, it, expect } from "vitest";
import { money, num, pct, price, spread, sign, NO_DATA, isPresent } from "../lib/format";

describe("formatters never fabricate data", () => {
  it("money/num/pct/price show NO DATA for null and undefined", () => {
    for (const f of [money, num, pct, price]) {
      expect(f(null)).toBe(NO_DATA);
      expect(f(undefined)).toBe(NO_DATA);
    }
  });

  it("NaN is never shown as a number", () => {
    expect(money(NaN)).toBe(NO_DATA);
    expect(price(NaN)).toBe(NO_DATA);
    expect(isPresent(NaN)).toBe(false);
  });

  it("a real zero is shown, an absent value is NO DATA (never invented 0)", () => {
    expect(money(0, 0)).toBe("$0");        // present zero renders
    expect(money(undefined, 0)).toBe(NO_DATA); // absent never becomes 0
  });

  it("real values format correctly", () => {
    expect(money(1000000, 0)).toBe("$1,000,000");
    expect(pct(0.153)).toBe("15.3%");
    expect(price(1.15234)).toBe("1.15234");
    expect(spread(1.15234, 1.15235)).toBe("0.00001");
  });

  it("sign classes", () => {
    expect(sign(5)).toBe("pos");
    expect(sign(-5)).toBe("neg");
    expect(sign(0)).toBe("");
    expect(sign(null)).toBe("");
  });
});
