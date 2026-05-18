import { describe, expect, it } from "bun:test";
import { formatGreeting, userKey } from "../src/user_format";

describe("user_format", () => {
  it("userKey is lowercase email", () => {
    expect(userKey({ email: "ALICE@example.com", fullName: "Alice" })).toBe(
      "alice@example.com",
    );
  });

  it("formatGreeting is a string", () => {
    expect(typeof formatGreeting({ email: "a@b.c", fullName: "Alice" })).toBe(
      "string",
    );
  });
});
