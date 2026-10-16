export type Rule =
  | { mode: "renew"; plan_id: number }
  | { mode: "extra"; gb: number; days: number };
export type Status = "off" | "on" | "stopped" | "waiting_balance" | "unknown";
export interface Option {
  value: number;
  base_price: number;
}
export interface Preference {
  service_id: number;
  revision: string;
  status: Status;
  rule: Rule | null;
  reason: string | null;
  eligible: boolean;
  plans: { id: number; title: string; base_price: number }[];
  gb_options: Option[];
  day_options: Option[];
  balance: number;
  currency: string;
}
export interface Choice {
  mode: "off" | "renew" | "extra";
  plan_id: number;
  gb: number;
  days: number;
}
export interface Body extends Choice {
  expected_revision: string;
  acknowledge_wallet: true;
}
const obj = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const int = (v: unknown): v is number =>
  typeof v === "number" && Number.isSafeInteger(v);
const count = (v: unknown): v is number => int(v) && v >= 0;
const positive = (v: unknown): v is number => count(v) && v > 0;
function options(v: unknown): Option[] {
  if (
    !Array.isArray(v) ||
    v.some((x) => !obj(x) || !positive(x.value) || !count(x.base_price)) ||
    new Set(v.map((x) => x.value)).size !== v.length
  )
    throw Error("invalid_auto");
  return v.map((x) => ({ value: x.value, base_price: x.base_price }));
}
export function parsePreference(v: unknown, id: number): Preference {
  if (
    !obj(v) ||
    v.service_id !== id ||
    !positive(v.service_id) ||
    typeof v.revision !== "string" ||
    !/^[a-f0-9]{64}$/.test(v.revision) ||
    typeof v.status !== "string" ||
    !["off", "on", "stopped", "waiting_balance", "unknown"].includes(
      v.status,
    ) ||
    typeof v.eligible !== "boolean" ||
    !int(v.balance) ||
    typeof v.currency !== "string" ||
    !v.currency ||
    !(v.reason === null || typeof v.reason === "string") ||
    !Array.isArray(v.plans)
  )
    throw Error("invalid_auto");
  const plans = v.plans.map((p) => {
    if (
      !obj(p) ||
      !positive(p.id) ||
      typeof p.title !== "string" ||
      !count(p.base_price)
    )
      throw Error("invalid_auto");
    return { id: p.id, title: p.title, base_price: p.base_price };
  });
  if (new Set(plans.map((p) => p.id)).size !== plans.length)
    throw Error("invalid_auto");
  let rule: Rule | null = null;
  if (v.rule !== null) {
    if (!obj(v.rule)) throw Error("invalid_auto");
    if (v.rule.mode === "renew" && positive(v.rule.plan_id))
      rule = { mode: "renew", plan_id: v.rule.plan_id };
    else if (
      v.rule.mode === "extra" &&
      count(v.rule.gb) &&
      count(v.rule.days) &&
      (v.rule.gb > 0 || v.rule.days > 0)
    )
      rule = { mode: "extra", gb: v.rule.gb, days: v.rule.days };
    else throw Error("invalid_auto");
  }
  if (
    (["on", "waiting_balance", "stopped"].includes(v.status) && !rule) ||
    (["off", "unknown"].includes(v.status) && rule)
  )
    throw Error("invalid_auto");
  return {
    service_id: v.service_id,
    revision: v.revision,
    status: v.status as Status,
    rule,
    reason: v.reason as string | null,
    eligible: v.eligible,
    plans,
    gb_options: options(v.gb_options),
    day_options: options(v.day_options),
    balance: v.balance,
    currency: v.currency,
  };
}
export function choiceFrom(p: Preference): Choice {
  return p.rule?.mode === "renew"
    ? { mode: "renew", plan_id: p.rule.plan_id, gb: 0, days: 0 }
    : p.rule?.mode === "extra"
      ? { mode: "extra", plan_id: 0, gb: p.rule.gb, days: p.rule.days }
      : { mode: "off", plan_id: 0, gb: 0, days: 0 };
}
export function buildBody(choice: Choice, p: Preference): Body {
  const { mode, plan_id, gb, days } = choice;
  if (
    !["off", "renew", "extra"].includes(mode) ||
    ![plan_id, gb, days].every(count) ||
    gb > 10000 ||
    days > 3650
  )
    throw Error("invalid_choice");
  if (mode !== "off" && !p.eligible) throw Error("ineligible");
  if (mode === "off" && (plan_id || gb || days)) throw Error("invalid_choice");
  if (
    mode === "renew" &&
    (gb || days || !p.plans.some((x) => x.id === plan_id))
  )
    throw Error("invalid_choice");
  if (
    mode === "extra" &&
    (plan_id ||
      !(gb || days) ||
      (gb && !p.gb_options.some((x) => x.value === gb)) ||
      (days && !p.day_options.some((x) => x.value === days)))
  )
    throw Error("invalid_choice");
  return { ...choice, expected_revision: p.revision, acknowledge_wallet: true };
}
export function basePrices(choice: Choice, p: Preference): number[] {
  if (choice.mode === "off") return [];
  if (choice.mode === "renew")
    return p.plans
      .filter((x) => x.id === choice.plan_id)
      .map((x) => x.base_price);
  return [
    p.gb_options.find((x) => x.value === choice.gb)?.base_price,
    p.day_options.find((x) => x.value === choice.days)?.base_price,
  ].filter((x): x is number => x !== undefined);
}

export function matchesChoice(choice: Choice, p: Preference): boolean {
  const actual = choiceFrom(p);
  return (
    (choice.mode !== "off" || p.status === "off") &&
    choice.mode === actual.mode &&
    choice.plan_id === actual.plan_id &&
    choice.gb === actual.gb &&
    choice.days === actual.days
  );
}
