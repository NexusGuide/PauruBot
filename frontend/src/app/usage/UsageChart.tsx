import { useEffect, useRef, useState } from "react";
import { size } from "@shared/format";
import { api, type UsageHistory } from "../api";
import { errorText, useApp } from "../state";
import { segments, validateHistory, valueScale } from "./usage-data";

export function UsageChart({ serviceId }: { serviceId: number }) {
  const { t, locale } = useApp();
  const [days, setDays] = useState<7 | 30>(7);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ key: string; data?: UsageHistory; error?: unknown } | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const host = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const key = `${serviceId}:${days}:${attempt}`;

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setSelected(null);
    api.usage(serviceId, days, controller.signal)
      .then(value => validateHistory(value, serviceId, days))
      .then(data => { if (active) setResult({ key, data }); })
      .catch(error => { if (active) setResult({ key, error }); });
    return () => { active = false; controller.abort(); };
  }, [serviceId, days, key]);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const resize = () => setWidth(el.getBoundingClientRect().width);
    const observer = new ResizeObserver(resize);
    observer.observe(el);
    resize();
    return () => observer.disconnect();
  }, []);

  const current = result?.key === key ? result : null;
  const data = current?.data;
  const points = data?.points ?? [];
  const scale = valueScale(points);
  const number = new Intl.NumberFormat(locale, { maximumFractionDigits: 3 });
  const stamp = (at: string, compact = false) => new Date(at).toLocaleString(locale, compact
    ? { month: "short", day: "numeric" }
    : { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
  const value = (bytes: number) => size(bytes, locale, t("units").split(","));
  const chosen = points.find(p => p.at === selected) ?? points[points.length - 1];
  const height = 210, left = 58, right = 12, top = 12, bottom = 34;
  const x = (at: string) => left + (Date.parse(at) - Date.parse(data!.from))
    / (Date.parse(data!.to) - Date.parse(data!.from)) * Math.max(1, width - left - right);
  const y = (bytes: number) => height - bottom - bytes / scale.divisor / scale.maximum * (height - top - bottom);
  const last = points[points.length - 1];
  const stale = !!last && Date.now() - Date.parse(last.at) > 2 * 3600_000;

  return (
    <section className="mb-4 rounded-3xl bg-[var(--surface)] p-4" aria-label={t("usage_history")}>
      <h2 className="font-bold">{t("usage_history")}</h2>
      <p className="mt-1 text-sm text-[var(--hint)]">{t("usage_counter_hint")}</p>
      <div className="my-3 flex gap-2">
        {([7, 30] as const).map(window => <button key={window} type="button" aria-pressed={days === window}
          className="rounded-xl border border-[var(--line)] px-3 py-2 text-sm" onClick={() => setDays(window)}>
          {t("usage_days", { days: window })}
        </button>)}
      </div>
      <div ref={host} className="w-full min-w-0">
        {!current && <p role="status">{t("usage_loading")}</p>}
        {current?.error !== undefined && <div role="alert">
          <p>{errorText(t, current.error)}</p>
          <button type="button" onClick={() => setAttempt(v => v + 1)} className="mt-2 underline">{t("usage_retry")}</button>
        </div>}
        {data && points.length < 2 && <p role="status" className="text-sm">{t("usage_collecting")}</p>}
        {data && points.length >= 2 && width > left + right && <>
          <p className="mb-1 text-xs text-[var(--hint)]">{t("usage_counter_unit", { unit: scale.unit })}</p>
          <svg role="img" aria-label={t("usage_chart_label", { count: points.length, value: value(last!.used_bytes) })}
            width="100%" height={height} viewBox={`0 0 ${width} ${height}`} direction="ltr">
            {scale.ticks.map(tick => <g key={tick}>
              <line x1={left} x2={width - right} y1={y(tick * scale.divisor)} y2={y(tick * scale.divisor)} stroke="var(--line)" />
              <text x={left - 6} y={y(tick * scale.divisor) + 4} textAnchor="end" fontSize={11} fill="var(--hint)">{number.format(tick)}</text>
            </g>)}
            {segments(points).filter(group => group.length > 1).map(group =>
              <polyline key={group[0]!.at} data-usage-segment="true" fill="none" stroke="var(--accent)" strokeWidth={2}
                points={group.map(p => `${x(p.at)},${y(p.used_bytes)}`).join(" ")} />)}
            {points.map(p => <circle key={p.at} data-usage-point="true" cx={x(p.at)} cy={y(p.used_bytes)} r={2}
              fill="var(--accent)"><title>{stamp(p.at)}: {value(p.used_bytes)}</title></circle>)}
            <text x={left} y={height - 8} fontSize={11} fill="var(--hint)">{stamp(data.from, true)}</text>
            <text x={width - right} y={height - 8} textAnchor="end" fontSize={11} fill="var(--hint)">{stamp(data.to, true)}</text>
          </svg>
        </>}
        {data && chosen && <div className="mt-2 text-sm">
          <label className="block">{t("usage_observation")}
            <select className="mt-1 w-full min-w-0 rounded-lg bg-[var(--bg)] p-2" value={chosen.at}
              onChange={event => setSelected(event.target.value)}>
              {points.map(p => <option key={p.at} value={p.at}>{stamp(p.at)}</option>)}
            </select>
          </label>
          <p className="num mt-2" aria-live="polite">{value(chosen.used_bytes)}</p>
          <p className="sr-only">{chosen.used_bytes} bytes</p>
          <div className="sr-only"><table><caption>{t("usage_history")}</caption>
            <thead><tr><th>{t("usage_observation")}</th><th>bytes</th></tr></thead>
            <tbody>{points.map(p => <tr key={p.at}><td>{stamp(p.at)}</td><td>{p.used_bytes}</td></tr>)}</tbody>
          </table></div>
        </div>}
        {stale && <p role="status" className="mt-2 text-sm">{t("usage_stale")}</p>}
      </div>
    </section>
  );
}
