import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { dateTime, money } from "@shared/format";
import { api, type Order } from "../api";
import { Icon } from "../icons";
import { errorText, useApp } from "../state";
import { haptic, openExternal, openInvoice } from "../telegram";
import { ActionBar, Button, Loading, Panel, Screen, Steps } from "../ui";
import { rememberCard, rememberPayment } from "./Payment";

function randomName() {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789";
  return Array.from({ length: 10 }, () => chars.charAt(Math.floor(Math.random() * chars.length))).join("");
}

function Line({ label, value, strong, tone }: { label: string; value: string; strong?: boolean; tone?: string }) {
  return (
    <div className="app-ln">
      <span className={strong ? "font-semibold" : "app-hint"}>{label}</span>
      {strong ? (
        <b className={`num text-[1.08rem] font-extrabold ${tone ?? ""}`}>{value}</b>
      ) : (
        <span className={`num font-semibold ${tone ?? ""}`}>{value}</span>
      )}
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
  const [appliedCode, setAppliedCode] = useState<string | undefined>(undefined);
  const isPurchase = (params.get("kind") ?? "purchase") === "purchase";
  const [label, setLabel] = useState(() => (isPurchase ? randomName() : ""));
  const initialLabel = useRef(label);
  const [busy, setBusy] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  const create = useCallback(
    async (discount?: string, name?: string) => {
      const body = {
        kind: params.get("kind") ?? "purchase",
        plan_id: Number(params.get("plan")) || undefined,
        service_id: Number(params.get("service")) || undefined,
        extra_gb: Number(params.get("gb")) || undefined,
        extra_days: Number(params.get("days")) || undefined,
        custom_gb: Number(params.get("cgb")) || undefined,
        custom_days: Number(params.get("cdays")) || undefined,
        code: discount || undefined,
        label: name || undefined,
      };
      const result = await api.createOrder(body);
      setOrder(result.order);
      setDue(result.due);
      setBalance(result.balance);
    },
    [params],
  );

  useEffect(() => {
    create(undefined, initialLabel.current || undefined).catch((err) => setFailed(errorText(t, err)));
  }, [create, t]);

  if (failed) {
    return (
      <Screen title={t("checkout")}>
        <Panel className="text-center">{failed}</Panel>
      </Screen>
    );
  }
  if (!order || !me) return <Loading />;

  const dirtyLabel = label.trim() !== "" && label.trim() !== (order.params.label ?? "");
  const after = balance - order.amount;
  const methods = me.payment_methods;
  const method = methods[0] ?? "card";

  const applyCode = async () => {
    if (!code.trim()) return;
    setBusy("code");
    try {
      if (order) await api.cancelOrder(order.id);
      await create(code.trim(), order?.params.label);
      setAppliedCode(code.trim());
      haptic("success");
      toast(t("code_applied"));
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setBusy(null);
    }
  };

  const applyLabel = async () => {
    setBusy("label");
    try {
      if (order) await api.cancelOrder(order.id);
      await create(appliedCode, label.trim());
      haptic("success");
      toast(t("label_set"));
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setBusy(null);
    }
  };

  const pay = async (selected: string) => {
    setBusy(selected);
    try {
      const result = await api.payOrder(order.id, selected);
      if (selected === "wallet") {
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
      {order.kind === "purchase" && <Steps n={3} note={t("step_note_confirm")} />}

      {order.kind === "purchase" && (
        <>
          <p className="app-lbl">{t("config_label")}</p>
          <div className="app-fr">
            <input
              dir="ltr"
              aria-label={t("config_label")}
              value={label}
              maxLength={48}
              onChange={(e) => setLabel(e.target.value.replace(/[^A-Za-z0-9_]/g, ""))}
              placeholder={t("config_label")}
              className="w-full pe-14 text-left"
            />
            <button type="button" className="app-gen" aria-label={t("random_name")} onClick={() => setLabel(randomName())}>
              <Icon name="dice" size={18} />
            </button>
          </div>
          <p className="app-hint mt-2 px-1 text-xs">{t("config_label_hint")}</p>
          {dirtyLabel && (
            <Button tone="quiet" className="mt-3" busy={busy === "label"} onClick={applyLabel}>
              {t("apply")}
            </Button>
          )}
        </>
      )}

      <div className="app-card mt-4 p-4">
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
      </div>

      {order.code_discount === 0 && (
        <>
          <p className="app-lbl">{t("code_discount")}</p>
          <div className="app-dc">
            <input
              aria-label={t("discount_code")}
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder={t("discount_code")}
              autoCapitalize="characters"
            />
            <Button tone="quiet" className="w-auto px-5" busy={busy === "code"} onClick={applyCode}>
              {t("apply")}
            </Button>
          </div>
        </>
      )}

      <div className="app-bal" data-tone={after < 0 ? "low" : undefined}>
        <span>{t("balance_after")}</span>
        <b className="num">
          {after < 0 ? `− ${money(-after, locale)}` : money(after, locale)} {t("currency")}
        </b>
      </div>

      {due > 0 && (
        <>
          <p className="app-hint mt-4 text-center text-sm">{t("pay_due", { amount: money(due, locale) })}</p>
          {methods.length === 0 && <p className="mt-2 text-center text-sm">{t("no_methods")}</p>}
          {methods.length > 1 && (
            <div className="mt-3 flex flex-col gap-3">
              {methods.slice(1).map((option) => (
                <Button key={option} tone="quiet" busy={busy === option} onClick={() => pay(option)}>
                  {t(`method_${option}`)}
                </Button>
              ))}
            </div>
          )}
        </>
      )}

      <ActionBar
        summary={t(`checkout_${order.kind}`)}
        price={`${money(order.amount, locale)} ${t("currency")}`}
        action={due === 0 ? t("pay_from_wallet") : t(`method_${method}`)}
        busy={busy !== null}
        disabled={due > 0 && methods.length === 0}
        onClick={() => pay(due === 0 ? "wallet" : method)}
      />
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
      <div className="app-done">
        <div className="app-check">
          <svg viewBox="0 0 120 120" aria-hidden="true">
            <circle cx="60" cy="60" r="48" />
            <path d="M38 62l16 16 28-32" />
          </svg>
        </div>
        <h1 className="mt-2 text-2xl font-extrabold">{t("paid_title")}</h1>
        <p>{queuedUntil ? t("renew_queued_hint", { date: dateTime(queuedUntil, locale) }) : t("paid_hint")}</p>
        <div className="mt-3 w-full max-w-xs">
          <Button onClick={() => navigate("/services", { replace: true })}>{t("go_services")}</Button>
        </div>
      </div>
    </Screen>
  );
}
