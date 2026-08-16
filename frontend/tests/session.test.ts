import { describe, it, expect } from "vitest";
import { constantTimeEqual, createSessionToken, verifySessionToken } from "../lib/session";

const SECRET = "S3cr3t-Str0ng-Pass";
const USER = "admin";

describe("signed session token (branded login, replaces Basic Auth)", () => {
  it("accepts a freshly minted token for the same user + secret", async () => {
    const t = await createSessionToken(SECRET, USER);
    expect(await verifySessionToken(t, SECRET, USER)).toBe(true);
  });

  it("rejects a token signed with a different secret", async () => {
    const t = await createSessionToken(SECRET, USER);
    expect(await verifySessionToken(t, "other-secret", USER)).toBe(false);
  });

  it("rejects a token minted for a different user", async () => {
    const t = await createSessionToken(SECRET, "root");
    expect(await verifySessionToken(t, SECRET, USER)).toBe(false);
  });

  it("rejects an expired token", async () => {
    const t = await createSessionToken(SECRET, USER, -10); // already expired
    expect(await verifySessionToken(t, SECRET, USER)).toBe(false);
  });

  it("rejects a tampered signature and malformed tokens", async () => {
    const t = await createSessionToken(SECRET, USER);
    const [exp] = t.split(".");
    expect(await verifySessionToken(`${exp}.deadbeef`, SECRET, USER)).toBe(false);
    expect(await verifySessionToken("", SECRET, USER)).toBe(false);
    expect(await verifySessionToken(null, SECRET, USER)).toBe(false);
    expect(await verifySessionToken("no-dot-token", SECRET, USER)).toBe(false);
    expect(await verifySessionToken("abc.def", SECRET, USER)).toBe(false); // non-numeric exp
  });

  it("does not embed the raw secret in the token", async () => {
    const t = await createSessionToken(SECRET, USER);
    expect(t.includes(SECRET)).toBe(false);
  });

  it("constant-time compare is correct", () => {
    expect(constantTimeEqual("abc", "abc")).toBe(true);
    expect(constantTimeEqual("abc", "abd")).toBe(false);
    expect(constantTimeEqual("abc", "abcd")).toBe(false);
  });
});
