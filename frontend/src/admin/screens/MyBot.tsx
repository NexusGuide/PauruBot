import { useEffect, useState } from "react";
import { call, type BotWebhookStatus, type CatalogData } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Async, Badge, Button, Facts, Field, Input, Money, NumberInput, Page, Section, Toggle, useConfirm } from "../ui";
import { BotLogsSheet } from "./BotLogsSheet";
import { CardAccounts, type CardAccount } from "./System";

interface BotSettings {
  card_accounts: CardAccount[];
  card_enabled: boolean;
  support_username: string;
  resale_markup_percent: number;
  resale_prices: Record<string, number>;
  bot_enabled: boolean;
  receipt_duplicate_block: boolean;
}

export interface ResellerBot {
  connected: boolean;
  blocked?: boolean;
  balance: number;
  discount_percent: number;
  metered: boolean;
  shop_id?: number;
  title?: string;
  bot_username?: string | null;
  status?: string;
  settings?: BotSettings;
  customers?: number;
  active_services?: number;
  orders_month?: number;
  revenue_month?: number;
}

const round100 = (value: number) => Math.round(value / 100) * 100;

export function WebhookHealth({
  basePath,
  compact = false,
}: {
  basePath: string;
  compact?: boolean;
}) {
  const { t, toast, errorText } = useSession();
  const [info, setInfo] = useState<BotWebhookStatus | null>(null);
  const [busy, run] = useAction();
  const check = async () => {
    try {
      const res = await run(() => call<BotWebhookStatus>(`${basePath}/webhook`));
      if (res) setInfo(res);
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  const repoint = async () => {
    try {
      const res = await run(() => call<BotWebhookStatus>(`${basePath}/webhook/repoint`, { method: "POST" }));
      if (res) setInfo(res);
      toast(t("webhook_repointed"));
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <div className={compact ? "flex flex-col gap-2" : "flex flex-col gap-2 rounded-xl bg-[var(--bg)] px-3 py-2.5"}>
      <div className="flex flex-wrap items-center gap-2">
        <Button small tone="quiet" busy={busy} onClick={() => void check()}>
          {t("webhook_check")}
        </Button>
        <Button small tone="quiet" busy={busy} onClick={() => void repoint()}>
          {t("webhook_repoint")}
        </Button>
        {info && (
          <Badge tone={info.ok ? "health" : "loss"}>
            {info.ok ? t("webhook_ok") : info.error ? t("webhook_error") : t("webhook_mismatch")}
          </Badge>
        )}
      </div>
      {info && (
        <div className="flex flex-col gap-1 text-[0.78rem] text-[var(--hint)]">
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            <span>{t("webhook_pending", { count: info.pending_update_count })}</span>
            {info.url && (
              <span className="truncate" dir="ltr">
                {info.url}
              </span>
            )}
          </div>
          {(info.last_error_message || info.error) && (
            <p className="text-[var(--loss)]" dir="ltr">
              {info.last_error_message || info.error}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export function BotFigures({ data }: { data: ResellerBot }) {
  const { t } = useSession();
  return (
    <Facts
      items={[
        [t("my_bot_status"), data.connected ? <Badge key="s" tone="health">{t("my_bot_running")}</Badge> : <Badge key="s">{t("my_bot_stopped")}</Badge>],
        [t("my_bot_username"), data.bot_username ? <span key="u" dir="ltr">@{data.bot_username}</span> : "-"],
        [t("my_bot_customers"), String(data.customers ?? 0)],
        [t("my_bot_active_services"), String(data.active_services ?? 0)],
        [t("my_bot_orders_month"), String(data.orders_month ?? 0)],
        [t("my_bot_revenue_month"), <Money key="r" amount={data.revenue_month ?? 0} />],
      ]}
    />
  );
}

export function MyBot() {
  const { t, toast, errorText } = useSession();
  const state = useLoad<ResellerBot>("/api/admin/my-bot");
  const catalog = useLoad<CatalogData>("/api/admin/catalog");
  const [busy, run] = useAction();
  const confirm = useConfirm();
  const [token, setToken] = useState("");
  const [title, setTitle] = useState("");
  const [draft, setDraft] = useState<BotSettings | null>(null);
  const [logsOpen, setLogsOpen] = useState(false);

  useEffect(() => {
    if (state.data?.settings) setDraft(state.data.settings);
    if (state.data?.title) setTitle(state.data.title);
  }, [state.data]);

  const save = async (withToken: boolean) => {
    try {
      const body: Record<string, unknown> = { title };
      if (withToken) body.token = token.trim();
      if (draft && state.data?.connected) body.settings = draft;
      await run(() => call<ResellerBot>("/api/admin/my-bot", { method: "PUT", body }));
      setToken("");
      toast(withToken ? t("my_bot_connected") : t("saved"));
      void state.reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  const stop = async () => {
    if (!(await confirm({ title: t("my_bot_stop"), body: t("my_bot_stop_body"), confirm: t("my_bot_stop"), danger: true }))) return;
    try {
      await run(() => call<ResellerBot>("/api/admin/my-bot", { method: "DELETE" }));
      void state.reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Page title={t("nav_my_bot")}>
      <Async {...state}>
        {(data) => (
          <>
            <p className="max-w-2xl text-[0.9rem] text-[var(--hint)]">{t("my_bot_intro", { percent: data.discount_percent })}</p>
            {data.blocked && <p className="rounded-xl bg-anar-100 px-3 py-2 text-[0.85rem] font-semibold text-anar-700">{t("my_bot_blocked")}</p>}
            {data.metered && <p className="rounded-xl bg-saffron-100 px-3 py-2 text-[0.85rem] font-semibold text-saffron-700">{t("my_bot_needs_prepaid")}</p>}
            {data.connected && (
              <Section title={t("my_bot_overview")}>
                <BotFigures data={data} />
              </Section>
            )}
            <Section title={data.connected ? t("my_bot_change_token") : t("my_bot_connect")}>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <Field label={t("my_bot_token")} hint={t("my_bot_token_hint")}>
                  {(id) => <Input id={id} type="password" dir="ltr" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />}
                </Field>
                <Field label={t("my_bot_title")}>
                  {(id) => <Input id={id} maxLength={96} value={title} onChange={(e) => setTitle(e.target.value)} />}
                </Field>
              </div>
              <div className="mt-4 flex flex-wrap gap-2">
                <Button tone="primary" busy={busy} disabled={!token.trim() || data.metered || data.blocked} onClick={() => void save(true)}>
                  {data.connected ? t("my_bot_replace") : t("my_bot_connect")}
                </Button>
                {data.shop_id && (
                  <Button tone="quiet" onClick={() => setLogsOpen(true)}>
                    {t("bot_logs_open")}
                  </Button>
                )}
                {data.connected && <Button tone="danger" busy={busy} onClick={() => void stop()}>{t("my_bot_stop")}</Button>}
              </div>
              {data.connected && (
                <div className="mt-3">
                  <WebhookHealth basePath="/api/admin/my-bot" />
                </div>
              )}
              {logsOpen && data.shop_id && (
                <BotLogsSheet
                  basePath="/api/admin/my-bot/logs"
                  title={data.bot_username ? `@${data.bot_username}` : data.title ?? t("nav_my_bot")}
                  onClose={() => setLogsOpen(false)}
                />
              )}
            </Section>
            {data.connected && draft && (
              <>
                <Section
                  title={t("my_bot_prices")}
                  actions={
                    Object.keys(draft.resale_prices).length > 0 ? (
                      <Button
                        small
                        tone="quiet"
                        onClick={() => setDraft({ ...draft, resale_prices: {} })}
                      >
                        {t("my_bot_prices_reset")}
                      </Button>
                    ) : undefined
                  }
                >
                  <div className="flex flex-col gap-4">
                    <Field label={t("my_bot_markup")} hint={t("my_bot_markup_hint")}>
                      {(id) => <NumberInput id={id} value={draft.resale_markup_percent} onChange={(v) => setDraft({ ...draft, resale_markup_percent: v ?? 0 })} />}
                    </Field>
                    <Async {...catalog}>
                      {(cat) => (
                        <div className="overflow-x-auto">
                          <table className="w-full text-[0.88rem]">
                            <thead>
                              <tr className="text-start text-[0.78rem] text-[var(--hint)]">
                                <th className="py-2 text-start font-semibold">{t("col_plan")}</th>
                                <th className="py-2 text-start font-semibold">{t("my_bot_your_cost")}</th>
                                <th className="py-2 text-start font-semibold">{t("my_bot_your_price")}</th>
                              </tr>
                            </thead>
                            <tbody>
                              {cat.plans
                                .filter((p) => p.is_active && p.audience !== "resellers")
                                .map((plan) => {
                                  const cost = round100((plan.price * (100 - data.discount_percent)) / 100);
                                  const auto = Math.max(cost, round100((cost * (100 + draft.resale_markup_percent)) / 100));
                                  const fixed = draft.resale_prices[String(plan.id)] ?? null;
                                  return (
                                    <tr key={plan.id} className="border-t border-[var(--line)]">
                                      <td className="py-2">{plan.title}</td>
                                      <td className="py-2"><Money amount={cost} /></td>
                                      <td className="py-2">
                                        <NumberInput
                                          aria-label={t("my_bot_your_price")}
                                          allowEmpty
                                          placeholder={String(auto)}
                                          value={fixed}
                                          onChange={(v) => {
                                            const next = { ...draft.resale_prices };
                                            if (v && v > 0) next[String(plan.id)] = v;
                                            else delete next[String(plan.id)];
                                            setDraft({ ...draft, resale_prices: next });
                                          }}
                                        />
                                      </td>
                                    </tr>
                                  );
                                })}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </Async>
                  </div>
                </Section>
                <Section title={t("my_bot_payments")}>
                  <div className="flex flex-col gap-4">
                    <Toggle checked={draft.card_enabled} onChange={(v) => setDraft({ ...draft, card_enabled: v })} label={t("setting_card_enabled")} />
                    <Field label={t("setting_card_accounts")} hint={t("my_bot_cards_hint")}>
                      {() => <CardAccounts value={draft.card_accounts ?? []} onChange={(v) => setDraft({ ...draft, card_accounts: v })} />}
                    </Field>
                    <Toggle checked={draft.receipt_duplicate_block} onChange={(v) => setDraft({ ...draft, receipt_duplicate_block: v })} label={t("setting_receipt_duplicate_block")} />
                  </div>
                </Section>
                <Section title={t("my_bot_general")}>
                  <div className="flex flex-col gap-4">
                    <Field label={t("setting_support_username")}>
                      {(id) => <Input id={id} dir="ltr" maxLength={64} value={draft.support_username ?? ""} onChange={(e) => setDraft({ ...draft, support_username: e.target.value })} />}
                    </Field>
                    <Toggle checked={draft.bot_enabled} onChange={(v) => setDraft({ ...draft, bot_enabled: v })} label={t("setting_bot_enabled")} hint={t("my_bot_enabled_hint")} />
                  </div>
                </Section>
                <div>
                  <Button tone="primary" busy={busy} onClick={() => void save(false)}>{t("save_changes")}</Button>
                </div>
              </>
            )}
          </>
        )}
      </Async>
    </Page>
  );
}
