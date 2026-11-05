import { z } from "zod";

const MAX = (1n << 63n) - 1n;
const MIN = -(1n << 63n);

function scaled(value: string, scale: number) {
  const [whole, fraction = ""] = value.replace(/^-/, "").split(".");
  return BigInt(whole + fraction.padEnd(scale, "0")) * (value.startsWith("-") ? -1n : 1n);
}

function decimal(scale: number, minimum: bigint) {
  const pattern = new RegExp(`^${minimum < 0n ? "-?" : ""}[0-9]+(\\.[0-9]{1,${scale}})?$`);
  return z.string().max(80, "輸入過長，請核對數值。").superRefine((value, context) => {
    if (value.length > 80) return;
    if (!pattern.test(value)) {
      context.addIssue({ code: "custom", message: `請輸入十進位字串，最多 ${scale} 位小數；不能使用科學記號。` });
      return;
    }
    const integer = scaled(value, scale);
    if (integer < minimum || integer > MAX) context.addIssue({ code: "custom", message: minimum === 1n && integer === 0n ? "數值必須大於零。" : "數值超出允許範圍，請核對原始資料。" });
  });
}

// Keep the original string: validation never rounds, coerces or calculates a financial result.
export const money = decimal(6, 0n);
export function sameMoney(left: string, right: string) {
  return money.safeParse(left).success && money.safeParse(right).success && scaled(left, 6) === scaled(right, 6);
}
export const signedMoney = decimal(6, MIN);
export const quantity = decimal(8, 1n);
export const price = decimal(8, 0n);
export const fx = decimal(12, 1n);
export const optional = <T extends z.ZodType<string>>(schema: T) => z.union([schema, z.literal("")]);
export const date = z.iso.date("請填寫有效日期（YYYY-MM-DD）。").refine(value => !value.startsWith("0000"), "日期年份必須大於零。");
export const reason = z.string().trim().min(10, "請提供至少 10 個字元的具體依據。").max(1000, "依據最多 1000 個字元。");
export const note = z.string().max(1000, "備註最多 1000 個字元。");
