import { useEffect, useState } from "react";
import { date, money } from "@shared/format";
import { api, type MyDiscounts as Discounts } from "../api";
import { useApp } from "../state";
import { Panel } from "../ui";

/** What the customer has to spend less with: the standing percent, the next tier, own codes (F-33). */
export function MyDiscounts() {
  const { t, locale, toast } = useApp();
  const [data, setData] = useState<Discounts | null>(null);
  useEffect(() => {
    let live = true;
    api.myDiscounts().then((value) => { if (live) setData(value); }).catch(() => { /* optional card */ });
    return () => { live = false; };
  }, []);
  if (!data || (!data.standing_percent && !data.next_tier && data.codes.length === 0)) return null;
  const copy = async (code: string) => {
    try {
      await navigator.clipboard.writeText(code);
      toast(t("copied"));
    } catch {
      toast(code);
    }
  };
  return <Panel className="mb-4">
    <p className="mb-2 text-sm font-bold">{t("my_discounts_title")}</p>
    {data.standing_percent > 0 && <p className="text-sm">
      {t(data.reseller ? "my_discounts_reseller" : "my_discounts_tier", {
        title: data.tier?.title ?? "", percent: money(data.standing_percent, locale),
      })}
    </p>}
    {data.next_tier && <p className="text-xs text-[var(--hint)]">
      {t("my_discounts_next", {
        title: data.next_tier.title, percent: money(data.next_tier.percent, locale),
        amount: money(data.next_tier.remaining, locale),
      })}
    </p>}
    {data.codes.length > 0 && <ul className="mt-3 grid gap-2">
      {data.codes.map((c) => <li key={c.code}>
        <button type="button" className="app-chip w-full justify-between" onClick={() => void copy(c.code)}>
          <span dir="ltr" className="font-bold">{c.code}</span>
          <span>
            {c.kind === "percent" ? `${money(c.value, locale)}%` : `${money(c.value, locale)} ${t("currency")}`}
            {c.expires_at ? ` · ${t("my_codes_until", { date: date(c.expires_at, locale) })}` : ""}
          </span>
        </button>
        {(c.min_amount > 0 || c.first_purchase_only || c.sticky) && <p className="mt-1 text-xs text-[var(--hint)]">
          {[
            c.sticky ? t("my_codes_sticky") : "",
            c.first_purchase_only ? t("my_codes_first_only") : "",
            c.min_amount > 0 ? t("my_codes_min", { amount: money(c.min_amount, locale) }) : "",
          ].filter(Boolean).join(" · ")}
        </p>}
      </li>)}
    </ul>}
    {data.codes.length > 0 && <p className="mt-2 text-xs text-[var(--hint)]">{t("my_codes_hint")}</p>}
  </Panel>;
}
