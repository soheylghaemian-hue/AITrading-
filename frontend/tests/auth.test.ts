import { describe, it, expect } from "vitest";
import { checkBasicAuth, constantTimeEqual } from "../lib/basicAuth";

const header = (u: string, p: string) => "Basic " + Buffer.from(`${u}:${p}`).toString("base64");

describe("site password gate (HTTP Basic Auth)", () => {
  const USER = "admin";
  const PASS = "S3cr3t-Str0ng-Pass";

  it("accepts the correct credentials", () => {
    expect(checkBasicAuth(header(USER, PASS), USER, PASS)).toBe(true);
  });

  it("rejects a wrong password", () => {
    expect(checkBasicAuth(header(USER, "wrong"), USER, PASS)).toBe(false);
  });

  it("rejects a wrong user", () => {
    expect(checkBasicAuth(header("root", PASS), USER, PASS)).toBe(false);
  });

  it("rejects missing / malformed headers", () => {
    expect(checkBasicAuth(null, USER, PASS)).toBe(false);
    expect(checkBasicAuth("", USER, PASS)).toBe(false);
    expect(checkBasicAuth("Bearer xyz", USER, PASS)).toBe(false);
    expect(checkBasicAuth("Basic", USER, PASS)).toBe(false);
    expect(checkBasicAuth("Basic !!!notbase64", USER, PASS)).toBe(false);
  });

  it("handles passwords containing a colon", () => {
    const p = "a:b:c:strongpart";
    expect(checkBasicAuth(header(USER, p), USER, p)).toBe(true);
  });

  it("constant-time compare is correct", () => {
    expect(constantTimeEqual("abc", "abc")).toBe(true);
    expect(constantTimeEqual("abc", "abd")).toBe(false);
    expect(constantTimeEqual("abc", "abcd")).toBe(false);
  });
});
