import { describe, expect, it } from "vitest";
import { date, fx, money, price, quantity, ratio, sameMoney, signedMoney } from "./inputs";

describe("exact string-only financial input", () => {
  it.each(["1\n", "1\r\n"])("rejects line endings without throwing or trimming: %j", value => {
    for (const codec of [money, price, quantity, fx, signedMoney]) expect(codec.safeParse(value).success).toBe(false);
  });

  it("rejects ratio line endings instead of treating them as integer whitespace", () => {
    expect(ratio.safeParse("3\n").success).toBe(false);
  });
  it.each([3, 3.5, true, null, "", "0", "-1", "3.0", "3e0", "+3", " 3", "9223372036854775808", "12345678901234567890"])("rejects an inexact/out-of-range ratio %s", value => {
    expect(ratio.safeParse(value).success).toBe(false);
  });

  it("keeps every integer ratio digit beyond Number-safe precision", () => {
    expect(ratio.parse("9007199254740993")).toBe("9007199254740993");
    expect(ratio.parse("9223372036854775807")).toBe("9223372036854775807");
    expect(ratio.parse("0003")).toBe("0003");
  });

  it("compares same-currency principal exactly without normalizing the wire strings", () => {
    expect(sameMoney("000100.000001", "100.000001")).toBe(true);
    expect(sameMoney("100", "100.000000")).toBe(true);
    expect(sameMoney("9007199254.740992", "9007199254.740993")).toBe(false);
    expect(sameMoney("1e3", "1000")).toBe(false);
    expect(sameMoney("1.0000001", "1")).toBe(false);
  });

  it.each([0.1, 1, true, null, "", "NaN", "Infinity", "1e3", " 1", "1 ", "1,000", "+1", ".1", "1.", "0.0000001", "-1", "9223372036854.775808", "0".repeat(81)])("rejects money %s without rounding or coercion", value => {
    expect(money.safeParse(value).success).toBe(false);
  });

  it("retains original digits and full scaled INT64 boundaries", () => {
    expect(money.parse("00001.000001")).toBe("00001.000001");
    expect(money.parse("9223372036854.775807")).toBe("9223372036854.775807");
    expect(signedMoney.parse("-9223372036854.775808")).toBe("-9223372036854.775808");
    expect(signedMoney.safeParse("-9223372036854.775809").success).toBe(false);
    expect(quantity.parse("0.00000001")).toBe("0.00000001");
    expect(price.parse("92233720368.54775807")).toBe("92233720368.54775807");
    expect(price.parse("0")).toBe("0");
    expect(fx.parse("0.000000000001")).toBe("0.000000000001");
    expect(fx.parse("9223372.036854775807")).toBe("9223372.036854775807");
  });

  it.each(["0", "0.000000000000", "7.8000000000001", "9223372.036854775808"])("rejects invalid FX %s", value => {
    expect(fx.safeParse(value).success).toBe(false);
  });

  it.each(["0", "-1", "1.000000001", "92233720368.54775808"])("rejects invalid quantity %s", value => {
    expect(quantity.safeParse(value).success).toBe(false);
  });

  it.each(["0000-01-01", "2026-02-29", "1900-02-29", "2026-02-30", "2026-1-01"])("rejects invalid calendar date %s", value => {
    expect(date.safeParse(value).success).toBe(false);
  });

  it("keeps calendar dates, not guessed UTC execution times", () => {
    expect(date.parse("2024-02-29")).toBe("2024-02-29");
    expect(date.parse("0001-01-01")).toBe("0001-01-01");
  });
});
