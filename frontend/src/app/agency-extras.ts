// Extra traffic and days for a panel admin account (TEN-14): the one-tap amounts inside the
// shop's range and a typed amount. Pure, so it is tested on its own; the server checks again.

export type Range = [number, number];

/** One-tap amounts, the same as the bot's. */
export const GB_STEPS = [10, 50, 100, 200, 500];
export const DAY_STEPS = [7, 15, 30, 60, 90];
export const USER_STEPS = [5, 10, 20, 50, 100];

/** The steps inside [low, high]; the range's own ends when none fit. */
export function stepsWithin(steps: number[], [low, high]: Range): number[] {
  const inside = steps.filter((step) => step >= low && step <= high);
  return inside.length ? inside : [...new Set([low, high])].sort((a, b) => a - b);
}

const DIGITS: Record<string, string> = Object.fromEntries(
  [..."۰۱۲۳۴۵۶۷۸۹"].map((d, i) => [d, String(i)]).concat([..."٠١٢٣٤٥٦٧٨٩"].map((d, i) => [d, String(i)])),
);

/** A typed whole number (Persian or Arabic digits too) inside the range, else null. */
export function parseAmount(text: string, [low, high]: Range): number | null {
  const latin = [...text.trim()].map((ch) => DIGITS[ch] ?? ch).join("");
  if (!/^\d{1,7}$/.test(latin)) return null;
  const value = Number(latin);
  return value >= low && value <= high ? value : null;
}
