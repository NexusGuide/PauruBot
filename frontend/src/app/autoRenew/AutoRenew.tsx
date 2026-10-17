import { useCallback, useEffect, useRef, useState } from "react";
import { money } from "@shared/format";
import { ApiError } from "@shared/http";
import { api } from "../api";
import { useApp } from "../state";
import { haptic } from "../telegram";
import { Button, Seg, Switch } from "../ui";
import { translator, type Key } from "./copy";
import {
  buildBody,
  choiceFrom,
  matchesChoice,
  parsePreference,
  type Choice,
  type Preference,
} from "./model";

const OFF: Choice = { mode: "off", plan_id: 0, gb: 0, days: 0 };

/** What this service can currently buy with the wallet. */
function available(p: Preference) {
  return {
    renew: p.plans.length > 0,
    extra: p.gb_options.length > 0 || p.day_options.length > 0,
  };
}

/** One short line for the switch row: the saved rule, or what is wrong. */
function summary(
  p: Preference,
  t: (key: Key) => string,
  locale: string,
): string | null {
  const rule = p.rule;
  if (!rule) return null;
  if (rule.mode === "renew") {
    const plan = p.plans.find((x) => x.id === rule.plan_id);
    return t("onRenew").replace("{plan}", plan?.title ?? `#${rule.plan_id}`);
  }
  const parts = [
    rule.gb > 0 ? `${money(rule.gb, locale)} ${t("gbUnit")}` : "",
    rule.days > 0 ? `${money(rule.days, locale)} ${t("dayUnit")}` : "",
  ].filter(Boolean);
  return t("onExtra").replace("{parts}", parts.join(" + "));
}

/** One selectable plan or add-on with its price. */
function OptionRow({
  title,
  price,
  currency,
  selected,
  disabled,
  onSelect,
}: {
  title: string;
  price: string;
  currency: string;
  selected: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      disabled={disabled}
      className="app-plan"
      onClick={() => {
        haptic("select");
        onSelect();
      }}
    >
      <span className="app-grow">
        <b className="block text-[0.95rem] font-bold leading-7">{title}</b>
      </span>
      <span className="flex flex-col items-end justify-center text-end">
        <b className="num text-[1.02rem] font-extrabold">{price}</b>
        <small className="text-[0.7rem] font-medium text-[var(--hint)]">
          {currency}
        </small>
      </span>
      <span className="app-rad" aria-hidden="true" />
    </button>
  );
}

export function AutoRenew({ serviceId }: { serviceId: number }) {
  const { locale, toast } = useApp();
  const t = translator(locale);
  const [confirmed, setConfirmed] = useState<Preference | null>(null),
    [choice, setChoice] = useState<Choice | null>(null),
    [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [blocked, setBlocked] = useState(false),
    [failure, setFailure] = useState("");
  const lock = useRef(false),
    seq = useRef(0),
    alive = useRef(true);
  const row = useRef<HTMLButtonElement>(null),
    editor = useRef<HTMLDivElement>(null);

  const read = useCallback(async () => {
    if (lock.current) return;
    const n = ++seq.current;
    setLoading(true);
    try {
      const p = parsePreference(await api.autoRenew(serviceId), serviceId);
      if (alive.current && n === seq.current) {
        setConfirmed(p);
        setChoice(choiceFrom(p));
        setOpen(false);
        setBlocked(false);
        setFailure("");
      }
    } catch {
      if (alive.current && n === seq.current) {
        setBlocked(true);
        setFailure("");
      }
    } finally {
      if (alive.current && n === seq.current) setLoading(false);
    }
  }, [serviceId]);

  useEffect(() => {
    alive.current = true;
    void read();
    return () => {
      alive.current = false;
      seq.current++;
    };
  }, [read]);

  useEffect(() => {
    if (open) editor.current?.focus();
  }, [open]);

  const frozen = blocked || !!failure;
  const on =
    confirmed?.status === "on" || confirmed?.status === "waiting_balance";
  const modes = confirmed ? available(confirmed) : { renew: false, extra: false };
  const canOpen =
    !!confirmed &&
    !frozen &&
    !loading &&
    confirmed.eligible &&
    (modes.renew || modes.extra);

  const sub = (() => {
    if (blocked) return t("loadError");
    if (!confirmed) return t("loading");
    if (confirmed.status === "stopped") return t("stopped");
    if (confirmed.status === "waiting_balance") return t("waiting");
    if (confirmed.status === "unknown") return t("unknown");
    if (!confirmed.eligible) return t("ineligible");
    return summary(confirmed, t, locale) ?? t("hint");
  })();

  const valid =
    !!confirmed &&
    !!choice &&
    (() => {
      try {
        buildBody(choice, confirmed);
        return true;
      } catch {
        return false;
      }
    })();

  /** Applies a whole rule over the wallet; the server verifies the revision. */
  const commit = async (next: Choice, note: "enabled" | "saved" | "disabled") => {
    if (!confirmed || frozen || loading || lock.current) return;
    let body;
    try {
      body = buildBody(next, confirmed);
    } catch {
      setFailure(t("invalid"));
      return;
    }
    lock.current = true;
    seq.current++;
    setBusy(true);
    setFailure("");
    try {
      const saved = parsePreference(
        await api.setAutoRenew(serviceId, body),
        serviceId,
      );
      if (!matchesChoice(next, saved)) throw Error("unverified_rule");
      if (alive.current) {
        setConfirmed(saved);
        setChoice(choiceFrom(saved));
        setOpen(false);
        setFailure("");
        toast(t(note));
        window.setTimeout(() => row.current?.focus(), 0);
      }
    } catch (e) {
      if (alive.current) {
        setFailure(
          t(
            e instanceof ApiError && e.status === 409
              ? "changed"
              : e instanceof ApiError &&
                  e.status >= 400 &&
                  e.status < 500 &&
                  e.status !== 408
                ? "rejected"
                : "uncertain",
          ),
        );
      }
    } finally {
      lock.current = false;
      if (alive.current) setBusy(false);
    }
  };

  const openEditor = () => {
    if (!canOpen || !confirmed) return;
    haptic("select");
    const current = choiceFrom(confirmed);
    let draft: Choice;
    if (current.mode === "extra" && modes.extra) {
      draft = {
        mode: "extra",
        plan_id: 0,
        gb: confirmed.gb_options.some((x) => x.value === current.gb)
          ? current.gb
          : 0,
        days: confirmed.day_options.some((x) => x.value === current.days)
          ? current.days
          : 0,
      };
    } else if (
      current.mode === "renew" &&
      modes.renew &&
      confirmed.plans.some((x) => x.id === current.plan_id)
    ) {
      draft = current;
    } else {
      draft = {
        mode: modes.renew ? "renew" : "extra",
        plan_id: 0,
        gb: 0,
        days: 0,
      };
    }
    setChoice(draft);
    setFailure("");
    setOpen(true);
  };

  const closeEditor = () => {
    setOpen(false);
    setFailure("");
    if (confirmed) setChoice(choiceFrom(confirmed));
    window.setTimeout(() => row.current?.focus(), 0);
  };

  const toggleSwitch = () => {
    if (!confirmed || frozen || loading || busy) return;
    if (on) {
      void commit(OFF, "disabled");
      return;
    }
    if (confirmed.eligible) openEditor();
  };

  return (
    <section className="app-card" aria-label={t("title")}>
      <div className="flex items-center gap-3 px-4 py-3.5">
        <button
          type="button"
          ref={row}
          className="app-grow min-w-0 py-1 text-start"
          disabled={!canOpen}
          aria-expanded={open}
          onClick={() => (open ? closeEditor() : openEditor())}
        >
          <b className="block text-[0.95rem] font-bold leading-7">
            {t("title")}
          </b>
          <small className="mt-0.5 block text-[0.8rem] font-medium leading-6 text-[var(--hint)]">
            {sub}
          </small>
        </button>
        <Switch
          checked={on}
          disabled={
            loading || !confirmed || frozen || (!on && !confirmed.eligible)
          }
          busy={busy}
          label={t("title")}
          onChange={toggleSwitch}
        />
      </div>

      {frozen && (
        <div className="border-t border-[var(--line)] px-4 py-3.5">
          <p className="app-note" data-tone="danger" role="alert">
            {blocked ? t("loadError") : failure}
          </p>
          <Button tone="quiet" onClick={() => void read()}>
            {t("retry")}
          </Button>
        </div>
      )}

      {open && confirmed && choice && (
        <div
          ref={editor}
          tabIndex={-1}
          className="border-t border-[var(--line)] px-4 py-4"
        >
          {modes.renew && modes.extra && (
            <Seg
              options={[
                { value: "renew", label: t("renew") },
                { value: "extra", label: t("extra") },
              ]}
              value={choice.mode === "extra" ? "extra" : "renew"}
              onChange={(value) => {
                setFailure("");
                setChoice({
                  mode: value as Choice["mode"],
                  plan_id: 0,
                  gb: 0,
                  days: 0,
                });
              }}
              disabled={busy}
            />
          )}
          {choice.mode === "renew" && (
            <>
              <p className="app-lbl mt-3">{t("plan")}</p>
              <div
                className="flex flex-col gap-3"
                role="radiogroup"
                aria-label={t("plan")}
              >
                {confirmed.plans.map((plan) => (
                  <OptionRow
                    key={plan.id}
                    title={plan.title}
                    price={money(plan.base_price, locale)}
                    currency={confirmed.currency}
                    selected={choice.plan_id === plan.id}
                    disabled={busy || frozen}
                    onSelect={() => {
                      setFailure("");
                      setChoice({
                        mode: "renew",
                        plan_id: plan.id,
                        gb: 0,
                        days: 0,
                      });
                    }}
                  />
                ))}
              </div>
            </>
          )}
          {choice.mode === "extra" && (
            <>
              {confirmed.gb_options.length > 0 && (
                <>
                  <p className="app-lbl mt-3">{t("gb")}</p>
                  <div
                    className="flex flex-col gap-3"
                    role="radiogroup"
                    aria-label={t("gb")}
                  >
                    {confirmed.gb_options.map((option) => (
                      <OptionRow
                        key={`gb-${option.value}`}
                        title={`${money(option.value, locale)} ${t("gbUnit")}`}
                        price={money(option.base_price, locale)}
                        currency={confirmed.currency}
                        selected={choice.gb === option.value}
                        disabled={busy || frozen}
                        onSelect={() => {
                          setFailure("");
                          setChoice({
                            mode: "extra",
                            plan_id: 0,
                            gb: option.value,
                            days: choice.days,
                          });
                        }}
                      />
                    ))}
                  </div>
                </>
              )}
              {confirmed.day_options.length > 0 && (
                <>
                  <p className="app-lbl mt-3">{t("days")}</p>
                  <div
                    className="flex flex-col gap-3"
                    role="radiogroup"
                    aria-label={t("days")}
                  >
                    {confirmed.day_options.map((option) => (
                      <OptionRow
                        key={`days-${option.value}`}
                        title={`${money(option.value, locale)} ${t("dayUnit")}`}
                        price={money(option.base_price, locale)}
                        currency={confirmed.currency}
                        selected={choice.days === option.value}
                        disabled={busy || frozen}
                        onSelect={() => {
                          setFailure("");
                          setChoice({
                            mode: "extra",
                            plan_id: 0,
                            gb: choice.gb,
                            days: option.value,
                          });
                        }}
                      />
                    ))}
                  </div>
                </>
              )}
            </>
          )}
          <div className="mt-4 grid grid-cols-2 gap-3">
            <Button tone="quiet" disabled={busy} onClick={closeEditor}>
              {t("cancel")}
            </Button>
            <Button
              disabled={!valid || busy || frozen}
              busy={busy}
              onClick={() => void commit(choice, on ? "saved" : "enabled")}
            >
              {t("save")}
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
