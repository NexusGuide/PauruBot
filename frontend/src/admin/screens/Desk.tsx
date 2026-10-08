import { useState } from "react";
import { money } from "@shared/format";
import { call } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Async, Bytes, Button, Checkbox, Empty, Field, Input, Money, Page, Section, Select, Status, When } from "../ui";

interface DeskData {
  balance: number;
  figures: { active: number; expiring: number; month_orders: number; month_amount: number };
  plans: { id: number; title: string; days: number; data_gb: number; base_price: number; price: number }[];
}

interface DeskService {
  id: number;
  label: string | null;
  panel_username: string;
  status: string;
  expire_at: string | null;
  used_bytes: number;
  data_limit: number;
  days_left: number | null;
  gb_left: number | null;
  renew_price: number | null;
}

interface RenewResult {
  renewed: number[];
  failed: { id: number; error: string }[];
  balance: number;
}

function Sell({ data, onDone }: { data: DeskData; onDone: () => void }) {
  const { t, locale, toast, errorText } = useSession();
  const [planId, setPlanId] = useState<number>(data.plans[0]?.id ?? 0);
  const [label, setLabel] = useState("");
  const [busy, run] = useAction();
  const plan = data.plans.find((p) => p.id === planId);
  const sell = async () => {
    try {
      await run(() => call("/api/admin/desk/sell", { method: "POST", body: { plan_id: planId, label: label.trim() || null } }));
      toast(t("desk_sold"));
      setLabel("");
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  if (data.plans.length === 0) return <Empty title={t("desk_no_plans")} />;
  return (
    <div className="flex flex-col gap-4 p-4">
      <Field label={t("desk_plan")}>
        {(id) => (
          <Select id={id} value={String(planId)} onChange={(e) => setPlanId(Number(e.target.value))}>
            {data.plans.map((p) => (
              <option key={p.id} value={p.id}>
                {p.title} — {money(p.price, locale)}
              </option>
            ))}
          </Select>
        )}
      </Field>
      <Field label={t("desk_label")} hint={t("desk_label_hint")}>
        {(id) => <Input id={id} maxLength={48} value={label} onChange={(e) => setLabel(e.target.value)} />}
      </Field>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[0.85rem] text-[var(--hint)]">
          {t("desk_balance")}: <Money amount={data.balance} />
        </span>
        <Button tone="primary" busy={busy} disabled={!plan || plan.price > data.balance} onClick={() => void sell()}>
          {plan ? t("desk_sell_for", { price: money(plan.price, locale) }) : t("desk_sell")}
        </Button>
      </div>
      {plan && plan.price > data.balance && <p className="text-[0.85rem] text-anar-700">{t("desk_low_balance")}</p>}
    </div>
  );
}

function Expiring({ onDone }: { onDone: () => void }) {
  const { t, locale, toast, errorText } = useSession();
  const [days, setDays] = useState(3);
  const [q, setQ] = useState("");
  const { data, error, reload } = useLoad<{ items: DeskService[] }>(`/api/admin/desk/expiring?days=${days}`);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [busy, run] = useAction();
  const items = (data?.items ?? []).filter((s) => {
    const needle = q.trim().toLowerCase();
    return !needle || `${s.label ?? ""} ${s.panel_username}`.toLowerCase().includes(needle);
  });
  const renewable = items.filter((s) => s.renew_price !== null);
  const chosen = renewable.filter((s) => picked.has(s.id));
  const total = chosen.reduce((sum, s) => sum + (s.renew_price ?? 0), 0);
  const toggle = (id: number) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const renew = async () => {
    try {
      const result = await run(() =>
        call<RenewResult>("/api/admin/desk/renew", { method: "POST", body: { service_ids: chosen.map((s) => s.id) } }),
      );
      if (!result) return;
      toast(t("desk_renewed", { ok: money(result.renewed.length, locale), failed: money(result.failed.length, locale) }), result.failed.length ? "error" : "ok");
      setPicked(new Set());
      await reload();
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Section
      title={t("desk_expiring")}
      flush
      actions={
        <Select value={String(days)} onChange={(e) => setDays(Number(e.target.value))} aria-label={t("desk_expiring")}>
          {[3, 7, 30].map((d) => (
            <option key={d} value={d}>
              {t("desk_within_days", { days: money(d, locale) })}
            </option>
          ))}
        </Select>
      }
    >
      <div className="flex flex-col gap-3 px-4 py-3">
        <Input placeholder={t("desk_search")} value={q} onChange={(e) => setQ(e.target.value)} />
        {renewable.length > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label className="flex items-center gap-2 text-[0.85rem]">
              <Checkbox
                checked={chosen.length === renewable.length}
                indeterminate={chosen.length > 0 && chosen.length < renewable.length}
                onChange={(on) => setPicked(on ? new Set(renewable.map((s) => s.id)) : new Set())}
                label={t("desk_pick_all")}
              />
              {t("desk_pick_all")}
            </label>
            <Button small tone="primary" busy={busy} disabled={chosen.length === 0} onClick={() => void renew()}>
              {t("desk_renew_picked", { n: money(chosen.length, locale), total: money(total, locale) })}
            </Button>
          </div>
        )}
      </div>
      <Async data={data} error={error} reload={reload}>
        {() =>
          items.length === 0 ? (
            <Empty title={t("desk_nothing_expiring")} />
          ) : (
            <ul className="divide-y divide-[var(--line)]">
              {items.map((s) => (
                <li key={s.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                  {s.renew_price !== null ? (
                    <Checkbox checked={picked.has(s.id)} onChange={() => toggle(s.id)} label={s.label || s.panel_username} />
                  ) : (
                    <span className="w-5" />
                  )}
                  <div className="min-w-0 flex-1">
                    <p className="flex flex-wrap items-center gap-2 font-bold">
                      {s.label || s.panel_username} <Status value={s.status} />
                    </p>
                    <p className="text-[0.8rem] text-[var(--hint)]">
                      <span className="num">{s.panel_username}</span> · <Bytes value={s.used_bytes} /> / {s.data_limit ? <Bytes value={s.data_limit} /> : "∞"} ·{" "}
                      {s.expire_at ? <When iso={s.expire_at} /> : "∞"}
                    </p>
                  </div>
                  <span className="text-[0.85rem]">{s.renew_price !== null ? <Money amount={s.renew_price} /> : t("desk_no_renewal")}</span>
                </li>
              ))}
            </ul>
          )
        }
      </Async>
    </Section>
  );
}

export function Desk() {
  const { t, locale } = useSession();
  const { data, error, reload } = useLoad<DeskData>("/api/admin/desk");
  return (
    <Page title={t("nav_desk")}>
      <Async data={data} error={error} reload={reload}>
        {(value) => (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {(
                [
                  ["desk_balance", <Money key="b" amount={value.balance} />],
                  ["desk_active", money(value.figures.active, locale)],
                  ["desk_expiring_count", money(value.figures.expiring, locale)],
                  ["desk_month", <span key="m">{money(value.figures.month_orders, locale)} · <Money amount={value.figures.month_amount} /></span>],
                ] as const
              ).map(([key, val]) => (
                <div key={key} className="rounded-2xl bg-[var(--surface)] p-3 ring-1 ring-[var(--line)]">
                  <p className="text-[0.75rem] text-[var(--hint)]">{t(key)}</p>
                  <p className="mt-1 font-bold">{val}</p>
                </div>
              ))}
            </div>
            <Section title={t("desk_sell")} flush>
              <Sell data={value} onDone={() => void reload()} />
            </Section>
          </>
        )}
      </Async>
      <Expiring onDone={() => void reload()} />
    </Page>
  );
}
