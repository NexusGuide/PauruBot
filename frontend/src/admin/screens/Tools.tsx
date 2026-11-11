// Staff tools the bot's admin menu had and the web panel lacked, each placed on the page it
// belongs to: a wallet gift for an audience (customers), plan prices at once (plans), a new trial
// round (trial settings), the reseller ranking (resellers) and taking every pin down (broadcasts).
import { useState } from "react";
import { digits } from "@shared/format";
import { call } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Button, Field, Money, NumberInput, Section, Select, Sheet, Table, useConfirm } from "../ui";

export const AUDIENCES = ["all", "buyers", "active", "inactive", "never_bought", "resellers"] as const;
type Audience = (typeof AUDIENCES)[number];
export const GIFT_MIN = 1_000;
export const GIFT_MAX = 10_000_000;

/** A whole-percent price change the server accepts: -90…90 and not zero. */
export function validPercent(value: number | null): value is number {
  return value !== null && Number.isInteger(value) && value !== 0 && value >= -90 && value <= 90;
}

/** A price after the change, rounded to hundreds like the server does. */
export function priceAfter(price: number, percent: number): number {
  return Math.round((price * (100 + percent)) / 100 / 100) * 100;
}

export function GiftAllButton() {
  const { t, can, toast, errorText } = useSession();
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState<number | null>(10_000);
  const [audience, setAudience] = useState<Audience>("all");
  const size = useLoad<{ count: number }>(open ? `/api/admin/tools/gift?audience=${audience}` : null);
  const [busy, run] = useAction();
  const confirm = useConfirm();
  if (!can("wallet.adjust")) return null;
  const ok = amount !== null && amount >= GIFT_MIN && amount <= GIFT_MAX;
  const send = async () => {
    if (!ok) return;
    const sure = await confirm({
      title: t("tools_gift_title"),
      body: t("tools_gift_confirm", { count: size.data?.count ?? 0, audience: t(`audience_${audience}`) }),
      confirm: t("tools_gift_send"),
    });
    if (!sure) return;
    try {
      await run(() => call("/api/admin/tools/gift", { method: "POST", body: { amount, audience } }));
      toast(t("tools_gift_queued"));
      setOpen(false);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <>
      <Button tone="quiet" icon="payments" onClick={() => setOpen(true)}>
        {t("tools_gift_title")}
      </Button>
      <Sheet
        open={open}
        onClose={() => setOpen(false)}
        title={t("tools_gift_title")}
        footer={
          <Button tone="primary" busy={busy} disabled={!ok} onClick={() => void send()}>
            {t("tools_gift_send")}
          </Button>
        }
      >
        <div className="flex flex-col gap-4">
          <p className="text-[0.86rem] text-[var(--hint)]">{t("tools_gift_hint")}</p>
          <Field label={t("tools_gift_amount")} hint={t("tools_gift_amount_hint")}>
            {(id) => <NumberInput id={id} value={amount} onChange={setAmount} />}
          </Field>
          <Field label={t("tools_audience")}>
            {(id) => (
              <Select id={id} value={audience} onChange={(e) => setAudience(e.target.value as Audience)}>
                {AUDIENCES.map((a) => (
                  <option key={a} value={a}>
                    {t(`audience_${a}`)}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {size.data && <p className="text-[0.88rem] font-semibold">{t("tools_gift_reach", { count: size.data.count })}</p>}
        </div>
      </Sheet>
    </>
  );
}

export function PlanPricesButton({ onDone, samplePrice }: { onDone: () => void; samplePrice?: number }) {
  const { t, can, toast, errorText } = useSession();
  const [open, setOpen] = useState(false);
  const [percent, setPercent] = useState<number | null>(10);
  const [busy, run] = useAction();
  if (!can("catalog.write")) return null;
  const apply = async () => {
    if (!validPercent(percent)) return;
    try {
      const result = await run(() => call<{ plans: number }>("/api/admin/tools/plan-prices", { method: "POST", body: { percent } }));
      toast(t("tools_prices_done", { count: result?.plans ?? 0 }));
      setOpen(false);
      onDone();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <>
      <Button tone="quiet" onClick={() => setOpen(true)}>
        {t("tools_prices_title")}
      </Button>
      <Sheet
        open={open}
        onClose={() => setOpen(false)}
        title={t("tools_prices_title")}
        footer={
          <Button tone="primary" busy={busy} disabled={!validPercent(percent)} onClick={() => void apply()}>
            {t("tools_prices_apply")}
          </Button>
        }
      >
        <div className="flex flex-col gap-4">
          <p className="text-[0.86rem] text-[var(--hint)]">{t("tools_prices_hint")}</p>
          <Field label={t("tools_prices_percent")}>{(id) => <NumberInput id={id} value={percent} onChange={setPercent} />}</Field>
          {validPercent(percent) && samplePrice ? (
            <p className="text-[0.86rem]">
              {t("tools_prices_example")} <Money amount={samplePrice} /> → <Money amount={priceAfter(samplePrice, percent)} />
            </p>
          ) : null}
        </div>
      </Sheet>
    </>
  );
}

export function TrialResetCard() {
  const { t, can, toast, errorText } = useSession();
  const [busy, run] = useAction();
  const confirm = useConfirm();
  if (!can("settings.write")) return null;
  const reset = async () => {
    const sure = await confirm({ title: t("tools_trial_title"), body: t("tools_trial_confirm"), confirm: t("tools_trial_go"), danger: true });
    if (!sure) return;
    try {
      await run(() => call("/api/admin/tools/trial-reset", { method: "POST" }));
      toast(t("tools_trial_done"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Section title={t("tools_trial_title")}>
      <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-0 flex-1 text-[0.86rem] text-[var(--hint)]">{t("tools_trial_hint")}</p>
        <Button tone="danger" busy={busy} onClick={() => void reset()}>
          {t("tools_trial_go")}
        </Button>
      </div>
    </Section>
  );
}

interface RankRow {
  rank: number;
  profile_id: number;
  name: string;
  score: number;
  sales: number;
  revenue: number;
  complaints: number;
}

export function ResellerRanking() {
  const { t, locale } = useSession();
  const [days, setDays] = useState(30);
  const { data } = useLoad<{ items: RankRow[] }>(`/api/admin/tools/reseller-ranking?days=${days}`);
  const rows = (data?.items ?? []).map((row) => ({ ...row, id: row.profile_id }));
  return (
    <Section
      flush
      title={t("tools_ranking_title")}
      actions={
        <Select aria-label={t("tools_ranking_period")} value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {[7, 30, 90, 0].map((d) => (
            <option key={d} value={d}>
              {d ? t("tools_ranking_days", { days: digits(d, locale) }) : t("tools_ranking_all")}
            </option>
          ))}
        </Select>
      }
    >
      <Table
        rows={rows}
        columns={[
          { key: "rank", label: "#", render: (r) => digits(r.rank, locale), mobile: "meta" },
          { key: "name", label: t("tools_ranking_name"), render: (r) => <b>{r.name}</b>, mobile: "title" },
          { key: "score", label: t("tools_ranking_score"), render: (r) => digits(r.score, locale), mobile: "end" },
          { key: "sales", label: t("tools_ranking_sales"), render: (r) => digits(r.sales, locale) },
          { key: "revenue", label: t("tools_ranking_revenue"), render: (r) => <Money amount={r.revenue} /> },
          { key: "complaints", label: t("tools_ranking_complaints"), render: (r) => digits(r.complaints, locale) },
        ]}
      />
    </Section>
  );
}

export function UnpinAllButton() {
  const { t, can, toast, errorText } = useSession();
  const [busy, run] = useAction();
  const confirm = useConfirm();
  if (!can("broadcast.send")) return null;
  const go = async () => {
    const sure = await confirm({ title: t("tools_unpin_title"), body: t("tools_unpin_confirm"), confirm: t("tools_unpin_go") });
    if (!sure) return;
    try {
      await run(() => call("/api/admin/tools/unpin-all", { method: "POST" }));
      toast(t("tools_unpin_done"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Button tone="quiet" busy={busy} onClick={() => void go()}>
      {t("tools_unpin_title")}
    </Button>
  );
}

interface ClaimRow {
  id: number;
  status: string;
  panel_username: string;
  panel: { id: number; name: string } | null;
  customer: { id: number; name: string } | null;
  created_at: string;
}

/** Customers' requests to add a previous service, waiting for staff (Services page). */
export function ServiceClaims() {
  const { t, can, toast, errorText } = useSession();
  const allowed = can("services.write");
  const { data, reload } = useLoad<{ items: ClaimRow[] }>(allowed ? "/api/admin/service-claims?status=pending" : null);
  const [busy, run] = useAction();
  if (!allowed || !data || data.items.length === 0) return null;
  const decide = async (row: ClaimRow, approve: boolean) => {
    try {
      await run(() => call(`/api/admin/service-claims/${row.id}`, { method: "POST", body: { approve } }));
      toast(t(approve ? "claims_approved" : "claims_rejected"));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Section flush title={`${t("claims_title")} · ${data.items.length}`}>
      <Table
        rows={data.items}
        columns={[
          { key: "customer", label: t("claims_customer"), mobile: "title", render: (r) => (r.customer ? <a className="font-semibold text-firouzeh-700" href={`/admin/customers/${r.customer.id}`}>{r.customer.name}</a> : "—") },
          { key: "username", label: t("claims_username"), mobile: "meta", render: (r) => <code dir="ltr">{r.panel_username}</code> },
          { key: "panel", label: t("claims_panel"), render: (r) => r.panel?.name ?? "—" },
          {
            key: "actions",
            label: "",
            mobile: "end",
            render: (r) => (
              <span className="flex gap-2">
                <Button small tone="approve" disabled={busy} onClick={() => void decide(r, true)}>
                  {t("claims_approve")}
                </Button>
                <Button small tone="danger" disabled={busy} onClick={() => void decide(r, false)}>
                  {t("claims_reject")}
                </Button>
              </span>
            ),
          },
        ]}
      />
    </Section>
  );
}
