import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ApiError } from "@shared/http";
import { dateTime, money } from "@shared/format";
import { api, type MyCode, type Order } from "../api";
import { Icon } from "../icons";
import { prepareDraft, reviseDraft } from "../order-draft";
import { actionGate } from "../topup-intents";
import { RenewalPreview } from "../renewal/Preview";
import { errorText, methodName, useApp } from "../state";
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
  const { me, t, locale, reload, toast, checkGate } = useApp();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [order, setOrder] = useState<Order | null>(null);
  const [previewedOrderId, setPreviewedOrderId] = useState<number | null>(null);
  const [due, setDue] = useState(0);
  const [balance, setBalance] = useState(0);
  const [code, setCode] = useState("");
  const [appliedCode, setAppliedCode] = useState<string | undefined>(params.get("draft_code") || undefined);
  const isPurchase = (params.get("kind") ?? "purchase") === "purchase";
  const [label, setLabel] = useState(() => params.get("draft_label") ?? (isPurchase ? randomName() : ""));
  const gate = useRef(actionGate());
  const [retry, setRetry] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  const create = useCallback(
    async () => {
      const body = {
        intent_key: params.get("draft")!,
        kind: params.get("kind") ?? "purchase",
        plan_id: Number(params.get("plan")) || undefined,
        service_id: Number(params.get("service")) || undefined,
        extra_gb: Number(params.get("gb")) || undefined,
        extra_days: Number(params.get("days")) || undefined,
        custom_gb: Number(params.get("cgb")) || undefined,
        custom_days: Number(params.get("cdays")) || undefined,
        account_plan_id: Number(params.get("aplan")) || undefined,
        account_id: Number(params.get("acc")) || undefined,
        account_amount: Number(params.get("amt")) || undefined,
        renew_package: params.has("rpkg") ? Number(params.get("rpkg")) : undefined,
        location_id: Number(params.get("loc")) || undefined,
        code: params.get("draft_code") || undefined,
        label: params.get("draft_label") || undefined,
      };
      const result = await api.createOrder(body);
      return result;
    },
    [params],
  );

  useEffect(() => {
    if (!me) return;
    if (!params.has("draft")) {
      setParams(prepareDraft(params, label), { replace: true });
      return;
    }
    let active = true;
    setOrder(null);
    setFailed(null);
    create().then((result) => {
      if (!active) return;
      if (["paid", "provisioning", "completed"].includes(result.order.status)) {
        navigate(`/done/${result.order.id}`, { replace: true });
        return;
      }
      setOrder(result.order);
      setDue(result.due);
      setBalance(result.balance);
    }).catch(async (err) => {
      if (!active) return;
      if (err instanceof Error && err.message === "terms_required") {
        // The shop asks for its terms before a purchase: show them, then this checkout runs again.
        try {
          if (!(await checkGate(await api.gate("purchase")))) return;
        } catch {
          // fall through to the error below
        }
      }
      setFailed(errorText(t, err));
    });
    return () => { active = false; };
  }, [create, params, setParams, me?.id, retry, navigate, t]);

  if (failed) {
    return (
      <Screen title={t("checkout")}>
        <Panel className="text-center">{failed}</Panel>
        <Button onClick={() => { setFailed(null); setRetry((n) => n + 1); }}>{t("ui_retry")}</Button>
      </Screen>
    );
  }
  if (!order || !me) return <Loading />;

  const dirtyLabel = label.trim() !== "" && label.trim() !== (order.params.label ?? "");
  const after = balance - order.amount;
  const methods = me.payment_methods;
  const method = methods[0] ?? "card";

  const applyCode = async (value: string = code) => {
    const chosenCode = value.trim();
    if (!chosenCode || !gate.current.enter()) return;
    setBusy("code");
    try {
      if (order) await api.cancelOrder(order.id);
      setOrder(null);
      setParams(reviseDraft(params, chosenCode, order?.params.label ?? ""), { replace: true });
      setAppliedCode(chosenCode);
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      gate.current.leave();
      setBusy(null);
    }
  };

  const applyLabel = async () => {
    if (!gate.current.enter()) return;
    setBusy("label");
    try {
      if (order) await api.cancelOrder(order.id);
      setOrder(null);
      setParams(reviseDraft(params, appliedCode ?? "", label.trim()), { replace: true });
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      gate.current.leave();
      setBusy(null);
    }
  };

  const renewalReady = order.kind !== "renew" || Boolean(order.params.national) || previewedOrderId === order.id;
  const pay = async (selected: string) => {
    if (!renewalReady) return;
    if (!gate.current.enter()) return;
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
      if (err instanceof ApiError && err.code === "card_verify_required") {
        navigate("/card-verification", { state: { customerId: me.id, returnTo: `/checkout?${params.toString()}` } });
      }
      toast(errorText(t, err), "error");
    } finally {
      gate.current.leave();
      setBusy(null);
    }
  };

  const extra = order.params;
  return (
    <Screen title={t(`checkout_${order.kind}`)}>
      {order.kind === "purchase" && <Steps n={3} note={t("step_note_confirm")} />}
      {order.params.national && <Panel className="mb-4"><p>{t("national_checkout_notice")}</p></Panel>}

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
            <Button tone="quiet" className="mt-3" busy={busy === "label"} disabled={busy !== null} onClick={applyLabel}>
              {t("apply")}
            </Button>
          )}
        </>
      )}

      {order.kind === "renew" && !order.params.national && <RenewalPreview order={order} onReady={setPreviewedOrderId} />}

      <div className="app-card mt-4 p-4">
        {order.kind === "account_purchase" && (
          <Line label={t("agency_plan")} value={params.get("name") ?? "…"} />
        )}
        {order.kind === "account_renew" && (
          <Line label={t("agency_renew")} value={params.get("name") ?? "…"} />
        )}
        {order.kind === "account_renew" && order.params.renew && (
          <Line
            label={t("agency_renew_size")}
            value={`${t("n_days", { days: money(order.params.renew.days, locale) })} · ${
              order.params.renew.gb ? `${money(order.params.renew.gb, locale)} ${t("gb")}` : t("unlimited_data")
            }`}
          />
        )}
        {order.kind === "account_gb" && (
          <Line
            label={t("agency_add_data")}
            value={`${money(Number(params.get("amt")) || (order.params.gb ?? 0), locale)} ${t("gb")}`}
          />
        )}
        {order.kind === "account_days" && (
          <Line
            label={t("agency_add_days")}
            value={t("n_days", { days: money(Number(params.get("amt")) || (order.params.days ?? 0), locale) })}
          />
        )}
        {order.kind === "account_users" && (
          <Line
            label={t("agency_add_users")}
            value={money(Number(params.get("amt")) || (order.params.users ?? 0), locale)}
          />
        )}
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

      {order.code_discount === 0 && (!order.kind.startsWith("account_") || order.params.discounts) && (
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
            <Button tone="quiet" className="w-auto px-5" busy={busy === "code"} disabled={busy !== null} onClick={() => void applyCode()}>
              {t("apply")}
            </Button>
          </div>
          <CodeHints orderId={order.id} disabled={busy !== null} apply={(value) => { setCode(value); void applyCode(value); }} />
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
                <Button key={option} tone="quiet" busy={busy === option} disabled={busy !== null || !renewalReady} onClick={() => pay(option)}>
                  {methodName(t, me, option)}
                </Button>
              ))}
            </div>
          )}
        </>
      )}

      <ActionBar
        summary={t(`checkout_${order.kind}`)}
        price={`${money(order.amount, locale)} ${t("currency")}`}
        action={due === 0 ? t("pay_from_wallet") : methodName(t, me, method)}
        busy={busy !== null}
        disabled={!renewalReady || (due > 0 && methods.length === 0)}
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
  const [target, setTarget] = useState<{ path: string; label: string } | null>(null);
  useEffect(() => {
    api
      .order(Number(id))
      .then((r) => {
        setQueuedUntil(r.order.params.queued ? (r.order.params.queued_until ?? null) : null);
        setTarget(
          r.order.kind.startsWith("account")
            ? { path: "/agency", label: t("go_agency") }
            : null,
        );
      })
      .catch(() => undefined);
  }, [id, t]);
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
          <Button onClick={() => navigate(target?.path ?? "/services", { replace: true })}>
            {target?.label ?? t("go_services")}
          </Button>
        </div>
      </div>
    </Screen>
  );
}

/** The customer's own codes that fit this order, best saving first (F-33). */
function CodeHints({ orderId, disabled, apply }: { orderId: number; disabled: boolean; apply: (code: string) => void }) {
  const { t, locale } = useApp();
  const [codes, setCodes] = useState<MyCode[]>([]);
  useEffect(() => {
    let live = true;
    api.myDiscounts(orderId).then((value) => { if (live) setCodes(value.codes.filter((c) => c.fits && (c.saving ?? 0) > 0)); })
      .catch(() => { /* hints are optional; the code field still works */ });
    return () => { live = false; };
  }, [orderId]);
  if (codes.length === 0) return null;
  return (
    <div className="mt-2 grid gap-2" role="group" aria-label={t("my_codes_fit")}>
      <p className="text-xs text-[var(--hint)]">{t("my_codes_fit")}</p>
      {codes.slice(0, 3).map((c) => (
        <button key={c.code} type="button" className="app-chip justify-between" disabled={disabled} onClick={() => apply(c.code)}>
          <span dir="ltr" className="font-bold">{c.code}</span>
          <span>{t("my_codes_saves", { amount: money(c.saving ?? 0, locale) })}</span>
        </button>
      ))}
    </div>
  );
}
