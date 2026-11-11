// Foxima parity, panel side: editors added beside ours; nothing replaced.
import { useSession } from "../session";
import { Button, Input, NumberInput, Select } from "../ui";

/** The payment methods the server knows (payments/engine.py PROVIDERS). */
export const METHODS = ["card", "zarinpal", "stars", "usdt_trc20", "trx", "ton", "nowpayments", "plisio", "aqayepardakht", "tetra98", "variza", "abangateway", "blupal", "tronado", "cubepay", "atlaspay", "tonpay"] as const;

export interface MethodRule {
  provider: string;
  min: number;
  max: number;
  help: string;
}

/** Rules the server accepts: a known method once, 0 ≤ min ≤ max (0: no own limit), help ≤ 2000. */
export function cleanRules(rows: MethodRule[]): MethodRule[] {
  const seen = new Set<string>();
  return rows.filter((r) => {
    const ok =
      (METHODS as readonly string[]).includes(r.provider) &&
      !seen.has(r.provider) &&
      Number.isInteger(r.min) && r.min >= 0 &&
      Number.isInteger(r.max) && r.max >= 0 &&
      !(r.min && r.max && r.min > r.max) &&
      r.help.length <= 2000;
    if (ok) seen.add(r.provider);
    return ok;
  });
}

export function MethodRules({ value, onChange }: { value: MethodRule[]; onChange: (value: MethodRule[]) => void }) {
  const { t } = useSession();
  const update = (i: number, patch: Partial<MethodRule>) => onChange(value.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const free = METHODS.find((m) => !value.some((r) => r.provider === m));
  return (
    <div className="flex flex-col gap-2">
      {value.map((row, i) => (
        <div key={i} className="grid gap-2 rounded-xl bg-[var(--bg)] p-2 sm:grid-cols-[1fr_1fr_1fr_auto]">
          <Select aria-label={t("method_rule_provider")} value={row.provider} onChange={(e) => update(i, { provider: e.target.value })}>
            {METHODS.map((m) => <option key={m} value={m}>{t(`provider_${m}`)}</option>)}
          </Select>
          <NumberInput aria-label={t("method_rule_min")} placeholder={t("method_rule_min")} value={row.min} onChange={(v) => update(i, { min: v ?? 0 })} />
          <NumberInput aria-label={t("method_rule_max")} placeholder={t("method_rule_max")} value={row.max} onChange={(v) => update(i, { max: v ?? 0 })} />
          <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(value.filter((_, j) => j !== i))} />
          <Input aria-label={t("method_rule_help")} placeholder={t("method_rule_help")} maxLength={2000} className="sm:col-span-4" value={row.help} onChange={(e) => update(i, { help: e.target.value })} />
        </div>
      ))}
      <Button small icon="plus" className="self-start" disabled={!free} onClick={() => free && onChange([...value, { provider: free, min: 0, max: 0, help: "" }])}>
        {t("method_rule_add")}
      </Button>
    </div>
  );
}
