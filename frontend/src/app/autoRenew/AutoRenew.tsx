import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@shared/http";
import { api } from "../api";
import { useApp } from "../state";
import { Button } from "../ui";
import {
  basePrices,
  buildBody,
  choiceFrom,
  matchesChoice,
  parsePreference,
  type Choice,
  type Preference,
} from "./model";
import { translator } from "./copy";

/** Badge tone for the saved auto-renewal status. */
function statusTone(blocked: boolean, status: string) {
  if (blocked || status === "off") return "gray";
  if (status === "on") return "green";
  if (status === "waiting_balance") return "orange";
  if (status === "stopped") return "red";
  return "gray";
}

export function AutoRenew({ serviceId }: { serviceId: number }) {
  const { locale } = useApp(),
    t = translator(locale);
  const [confirmed, setConfirmed] = useState<Preference | null>(null),
    [choice, setChoice] = useState<Choice | null>(null),
    [review, setReview] = useState<Choice | null>(null);
  const [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [ack, setAck] = useState(false),
    [blocked, setBlocked] = useState(false),
    [message, setMessage] = useState("");
  const lock = useRef(false),
    seq = useRef(0),
    alive = useRef(true),
    heading = useRef<HTMLHeadingElement>(null);
  const read = useCallback(async () => {
    if (lock.current) return;
    const n = ++seq.current;
    setLoading(true);
    try {
      const p = parsePreference(await api.autoRenew(serviceId), serviceId);
      if (alive.current && n === seq.current) {
        setConfirmed(p);
        setChoice(choiceFrom(p));
        setReview(null);
        setAck(false);
        setBlocked(false);
        setMessage("");
      }
    } catch {
      if (alive.current && n === seq.current) {
        setBlocked(true);
        setMessage(t("loadError"));
      }
    } finally {
      if (alive.current && n === seq.current) setLoading(false);
    }
  }, [serviceId, locale]); // translator is locale-bound, not a dependency that changes each render
  useEffect(() => {
    alive.current = true;
    void read();
    return () => {
      alive.current = false;
      seq.current++;
    };
  }, [read]);
  useEffect(() => {
    if (review) heading.current?.focus();
  }, [review]);
  const change = (next: Choice) => {
    if (lock.current) return;
    setChoice(next);
    setReview(null);
    setAck(false);
    setMessage("");
  };
  const prepare = () => {
    if (!confirmed || !choice || blocked || loading || lock.current) return;
    try {
      buildBody(choice, confirmed);
      setReview({ ...choice });
      setAck(false);
      setMessage("");
    } catch {
      setMessage(t("invalid"));
    }
  };
  const save = async () => {
    if (!confirmed || !review || !ack || blocked || loading || lock.current)
      return;
    lock.current = true;
    seq.current++;
    setBusy(true);
    setMessage("");
    try {
      const body = buildBody(review, confirmed);
      const p = parsePreference(
        await api.setAutoRenew(serviceId, body),
        serviceId,
      );
      if (!matchesChoice(review, p)) throw Error("unverified_rule");
      if (alive.current) {
        setConfirmed(p);
        setChoice(choiceFrom(p));
        setReview(null);
        setAck(false);
        setMessage(t("saved"));
      }
    } catch (e) {
      if (alive.current) {
        setBlocked(true);
        setAck(false);
        setMessage(
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
  const money = (n: number) => new Intl.NumberFormat(locale).format(n);
  const selected = review ?? choice;
  return (
    <section className="app-card p-4" aria-label={t("title")}>
      <h2 className="mb-2 font-bold">{t("title")}</h2>
      <p className="text-xs leading-6 text-[var(--hint)]">{t("rules")}</p>
      <p className="mt-1.5 text-xs leading-6 text-[var(--hint)]">{t("wallet")}</p>
      {loading && <p role="status">{t("loading")}</p>}
      {busy && <p role="status">{t("busy")}</p>}
      {message && (
        <p role={blocked ? "alert" : "status"} className="mt-2" tabIndex={-1}>
          {message}
        </p>
      )}
      <button
        type="button"
        disabled={busy || loading}
        onClick={() => void read()}
        className="app-chip my-3"
      >
        {t("refresh")}
      </button>
      {confirmed && choice && (
        <>
          <p className="my-2 flex flex-wrap items-center gap-2">
            <span className="app-badge" data-tone={statusTone(blocked, confirmed.status)}>
              {t(blocked ? "unknown" : confirmed.status)}
            </span>
            {!blocked && confirmed.reason && (
              <span className="text-sm text-[var(--hint)]">
                {t("reason")}: <bdi>{confirmed.reason}</bdi>
              </span>
            )}
          </p>
          <div className="mt-1 border-t border-[var(--line)]">
            <div className="app-ln">
              <small>{t("current")}</small>
              <bdi className="font-bold">
                {confirmed.rule?.mode === "renew"
                  ? `${t("renew")} #${confirmed.rule.plan_id}`
                  : confirmed.rule?.mode === "extra"
                    ? `${confirmed.rule.gb} GB / ${confirmed.rule.days} ${t("dayUnit")}`
                    : t(confirmed.status)}
              </bdi>
            </div>
            <div className="app-ln border-t border-[var(--line)]">
              <small>{t("balance")}</small>
              <bdi className="num font-bold">
                {money(confirmed.balance)} {confirmed.currency}
              </bdi>
            </div>
          </div>
          {!confirmed.eligible && <p className="mt-2 text-sm text-[var(--hint)]">{t("ineligible")}</p>}
          {!review ? (
            <fieldset
              disabled={busy || loading || blocked}
              className="mt-3 grid gap-2"
            >
              <label htmlFor={`ar-mode-${serviceId}`}>{t("mode")}</label>
              <select
                id={`ar-mode-${serviceId}`}
                value={choice.mode}
                onChange={(e) =>
                  change({
                    mode: e.target.value as Choice["mode"],
                    plan_id: 0,
                    gb: 0,
                    days: 0,
                  })
                }
                className="min-w-0 w-full rounded-xl bg-[var(--bg)] p-2"
              >
                <option value="off">{t("off")}</option>
                <option
                  value="renew"
                  disabled={!confirmed.eligible || !confirmed.plans.length}
                >
                  {t("renew")}
                </option>
                <option
                  value="extra"
                  disabled={
                    !confirmed.eligible ||
                    !(
                      confirmed.gb_options.length ||
                      confirmed.day_options.length
                    )
                  }
                >
                  {t("extra")}
                </option>
              </select>
              {choice.mode === "renew" && (
                <>
                  <label htmlFor={`ar-plan-${serviceId}`}>{t("plan")}</label>
                  <select
                    id={`ar-plan-${serviceId}`}
                    value={choice.plan_id}
                    onChange={(e) =>
                      change({ ...choice, plan_id: Number(e.target.value) })
                    }
                    className="min-w-0 w-full rounded-xl bg-[var(--bg)] p-2"
                  >
                    <option value="0">{t("choose")}</option>
                    {choice.plan_id > 0 &&
                      !confirmed.plans.some((x) => x.id === choice.plan_id) && (
                        <option value={choice.plan_id} disabled>
                          {t("missing")} #{choice.plan_id}
                        </option>
                      )}
                    {confirmed.plans.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.title} · {money(p.base_price)} {confirmed.currency}
                      </option>
                    ))}
                  </select>
                </>
              )}
              {choice.mode === "extra" &&
                (["gb", "days"] as const).map((k) => (
                  <label key={k} className="grid gap-1">
                    {t(k)}
                    <select
                      value={choice[k]}
                      onChange={(e) =>
                        change({ ...choice, [k]: Number(e.target.value) })
                      }
                      className="min-w-0 w-full rounded-xl bg-[var(--bg)] p-2"
                    >
                      <option value="0">{t("none")}</option>
                      {choice[k] > 0 &&
                        !(
                          k === "gb"
                            ? confirmed.gb_options
                            : confirmed.day_options
                        ).some((x) => x.value === choice[k]) && (
                          <option value={choice[k]} disabled>
                            {t("missing")} {choice[k]}
                          </option>
                        )}
                      {(k === "gb"
                        ? confirmed.gb_options
                        : confirmed.day_options
                      ).map((p) => (
                        <option key={p.value} value={p.value}>
                          {p.value} · {money(p.base_price)} {confirmed.currency}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              <Button type="button" onClick={prepare}>
                {t("review")}
              </Button>
            </fieldset>
          ) : (
            <div className="mt-3 grid gap-3">
              <h3 tabIndex={-1} ref={heading}>
                {t("reviewTitle")}
              </h3>
              <p>
                {t(review.mode)}
                {review.mode === "renew" && (
                  <>
                    {" "}
                    · #{review.plan_id}{" "}
                    {
                      confirmed.plans.find((p) => p.id === review.plan_id)
                        ?.title
                    }
                  </>
                )}
                {review.mode === "extra" && (
                  <>
                    {" "}
                    · {review.gb} GB / {review.days} {t("dayUnit")}
                  </>
                )}
              </p>
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  checked={ack}
                  disabled={busy || blocked}
                  onChange={(e) => setAck(e.target.checked)}
                />
                {t("ack")}
              </label>
              <Button
                type="button"
                disabled={busy || blocked || !ack}
                busy={busy}
                onClick={() => void save()}
              >
                {t("save")}
              </Button>
              <Button
                tone="quiet"
                type="button"
                disabled={busy}
                onClick={() => {
                  setReview(null);
                  setAck(false);
                }}
              >
                {t("edit")}
              </Button>
            </div>
          )}
          {selected && basePrices(selected, confirmed).length > 0 && (
            <p className="mt-3 text-sm">
              {t("base")}:{" "}
              <bdi>
                {basePrices(selected, confirmed).map(money).join(" + ")}{" "}
                {confirmed.currency}
              </bdi>
            </p>
          )}
        </>
      )}
    </section>
  );
}
