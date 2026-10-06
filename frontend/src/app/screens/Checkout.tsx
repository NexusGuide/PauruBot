import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { dateTime, money } from "@shared/format";
import { api, type Order } from "../api";
import { errorText, useApp } from "../state";
import { haptic, openExternal, openInvoice } from "../telegram";
import { Button, Loading, Panel, Screen } from "../ui";
import { rememberCard, rememberPayment } from "./Payment";

function Line({ label, value, strong, tone }: { label: string; value: string; strong?: boolean; tone?: string }) {
  return (
    <div className={`flex items-baseline justify-between gap-3 py-1.5 ${strong ? "text-lg font-extrabold" : "text-sm"}`}>
      <span className={strong ? "" : "text-[var(--hint)]"}>{label}</span>
      <span className={`num ${tone ?? ""}`}>{value}</span>
    </div>
  );
}

export function Checkout() {
  const { me, t, locale, reload, toast } = useApp();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [order, setOrder] = useState<Order | null>(null);
  const [due, setDue] = useState(0);
  const [balance, setBalance] = useState(0);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  const create = useCallback(
    async (discount?: string) => {
      const body = {
        kind: params.get("kind") ?? "purchase",
        plan_id: Number(params.get("plan")) || undefined,
        service_id: Number(params.get("service")) || undefined,
        extra_gb: Number(params.get("gb")) || undefined,
        extra_days: Number(params.get("days")) || undefined,
        custom_gb: Number(params.get("cgb")) || undefined,
        custom_days: Number(params.get("cdays")) || undefined,
        code: discount || undefined,
      };
      const result = await api.createOrder(body);
      setOrder(result.order);
      setDue(result.due);
      setBalance(result.balance);
    },
    [params],
  );

  useEffect(() => {
    create().catch((err) => setFailed(errorText(t, err)));
  }, [create, t]);

  if (failed) {
    return (
      <Screen title={t("checkout")}>
        <Panel className="text-center">{failed}</Panel>
      </Screen>
    );
  }
  if (!order || !me) return <Loading />;

  const applyCode = async () => {
    if (!code.trim()) return;
    setBusy("code");
    try {
      if (order) await api.cancelOrder(order.id);
      await create(code.trim());
      haptic("success");
      toast(t("code_applied"));
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setBusy(null);
    }
  };

  const pay = async (method: string) => {
    setBusy(method);
    try {
      const result = await api.payOrder(order.id, method);
      if (method === "wallet") {
        haptic("success");
        await reload();
        navigate(`/done/${order.id}`, { replace: true });
        return;
      }
      const payment = result.payment;
      if (!payment) return;
      if (payment.accounts) rememberCard(payment.payment_id, payment.accounts, payment.amount);
      if (payment.crypto) {
        rememberPayment({ paymentId: payment.payment_id, amount: payment.amount, crypto: payment.crypto });
      }
      if (payment.redirect_url) openExternal(payment.redirect_url);
      if (payment.invoice_link) {
        const status = await openInvoice(payment.invoice_link);
        if (status === "paid") navigate(`/done/${order.id}`, { replace: true });
        return;
      }
      navigate(`/payment/${payment.payment_id}?order=${order.id}`, { replace: payment.provider !== "card" });
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setBusy(null);
    }
  };

  const extra = order.params;
  return (
    <Screen title={t(`checkout_${order.kind}`)}>
      <Panel className="mb-4">
        {extra.custom ? (
          <>
            <Line label={t("custom_data")} value={`${money(extra.custom.gb, locale)} ${t("gb")}`} />
            <Line label={t("custom_time")} value={t("n_days", { days: money(extra.custom.days, locale) })} />
          </>
        ) : null}
        {extra.extra_gb ? <Line label={t("extra_data")} value={`${money(extra.extra_gb, locale)} ${t("gb")}`} /> : null}
        {extra.extra_days ? <Line label={t("extra_time")} value={t("n_days", { days: money(extra.extra_days, locale) })} /> : null}
        <Line label={t("price")} value={money(order.base_price, locale)} />
        {extra.credit ? <Line label={t("credit")} value={`− ${money(extra.credit, locale)}`} tone="text-pista-700" /> : null}
        {order.reseller_discount > 0 && (
          <Line label={t("reseller_discount")} value={`− ${money(order.reseller_discount, locale)}`} tone="text-pista-700" />
        )}
        {order.code_discount > 0 && (
          <Line label={t("code_discount")} value={`− ${money(order.code_discount, locale)}`} tone="text-pista-700" />
        )}
        <div className="my-2 border-t border-dashed border-[var(--line)]" />
        <Line label={t("total")} value={`${money(order.amount, locale)} ${t("currency")}`} strong />
        <Line label={t("balance")} value={money(balance, locale)} />
      </Panel>

      {order.code_discount === 0 && (
        <div className="mb-4 flex gap-2">
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder={t("discount_code")}
            className="min-h-12 flex-1 rounded-2xl bg-[var(--surface)] px-4 text-[var(--text)] outline-none ring-1 ring-[var(--line)] focus:ring-[var(--accent)]"
            autoCapitalize="characters"
          />
          <Button tone="quiet" className="!w-auto px-5" busy={busy === "code"} onClick={applyCode}>
            {t("apply")}
          </Button>
        </div>
      )}

      <div className="flex flex-col gap-3">
        {due === 0 ? (
          <Button busy={busy === "wallet"} onClick={() => pay("wallet")}>
            {t("pay_from_wallet")}
          </Button>
        ) : (
          <>
            <p className="text-center text-sm text-[var(--hint)]">
              {t("pay_due", { amount: money(due, locale) })}
            </p>
            {me.payment_methods.map((method) => (
              <Button key={method} tone={method === me.payment_methods[0] ? "primary" : "quiet"}
                busy={busy === method} onClick={() => pay(method)}>
                {t(`method_${method}`)}
              </Button>
            ))}
            {me.payment_methods.length === 0 && <p className="text-center text-sm">{t("no_methods")}</p>}
          </>
        )}
      </div>
    </Screen>
  );
}

export function Done() {
  const { t, locale } = useApp();
  const navigate = useNavigate();
  const { id } = useParams();
  const [queuedUntil, setQueuedUntil] = useState<string | null>(null);
  useEffect(() => {
    api
      .order(Number(id))
      .then((r) => setQueuedUntil(r.order.params.queued ? (r.order.params.queued_until ?? null) : null))
      .catch(() => undefined);
  }, [id]);
  return (
    <Screen>
      <div className="flex min-h-[60dvh] flex-col items-center justify-center gap-4 text-center">
        <div className="flex h-20 w-20 items-center justify-center rounded-full bg-pista-100 text-pista-700">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"
            strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M5 12.5 10 17 19 7" />
          </svg>
        </div>
        <h1 className="text-2xl font-extrabold">{t("paid_title")}</h1>
        <p className="max-w-xs text-[var(--hint)]">
          {queuedUntil
            ? t("renew_queued_hint", { date: dateTime(queuedUntil, locale) })
            : t("paid_hint")}
        </p>
        <div className="mt-2 w-full max-w-xs">
          <Button onClick={() => navigate("/services", { replace: true })}>{t("go_services")}</Button>
        </div>
      </div>
    </Screen>
  );
}
