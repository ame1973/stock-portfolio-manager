import { describe, expect, it } from "vitest";
import { date, fx, money, price, quantity, signedMoney } from "./inputs";

describe("exact string-only financial input", () => {
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
