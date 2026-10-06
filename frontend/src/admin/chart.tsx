import { useMemo, useRef, useState } from "react";
import { useSession } from "./session";

export interface ColumnDatum {
  key: string;
  label: string;
  value: number;
  detail?: string;
}

function niceStep(max: number, ticks: number): number {
  if (max <= 0) return 1;
  const raw = max / ticks;
  const power = 10 ** Math.floor(Math.log10(raw));
  const unit = raw / power;
  const nice = unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 2.5 ? 2.5 : unit <= 5 ? 5 : 10;
  return nice * power;
}

/**
 * Single-series column chart: thin columns with rounded tops, hairline grid, a tooltip per column
 * and a table view, so the numbers are never locked inside the picture.
 */
export function Columns({
  data,
  format,
  axisFormat,
  title,
  height = 180,
}: {
  data: ColumnDatum[];
  format: (value: number) => string;
  axisFormat: (value: number) => string;
  title: string;
  height?: number;
}) {
  const { t } = useSession();
  const [hover, setHover] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  const { step, top } = useMemo(() => {
    const max = Math.max(0, ...data.map((d) => d.value));
    const s = niceStep(max, 3);
    return { step: s, top: Math.max(s, Math.ceil(max / s) * s) };
  }, [data]);

  const width = 640;
  const pad = { top: 8, bottom: 22, side: 44 };
  const plotW = width - pad.side;
  const plotH = height - pad.top - pad.bottom;
  const band = plotW / Math.max(1, data.length);
  const barW = Math.min(24, Math.max(3, band - 2));
  const ticks = Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);
  const labelEvery = Math.ceil(data.length / 6);

  const columnPath = (x: number, y: number, w: number, h: number) => {
    if (h <= 0) return "";
    const r = Math.min(4, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  };

  const active = hover !== null ? data[hover] : undefined;

  return (
    <figure className="flex flex-col gap-2">
      <figcaption className="flex items-center justify-between gap-3">
        <span className="text-[0.88rem] font-bold">{title}</span>
        <button
          type="button"
          onClick={() => setAsTable((v) => !v)}
          className="text-[0.78rem] font-semibold text-[var(--hint)] hover:text-[var(--text)]"
        >
          {asTable ? t("chart_show_chart") : t("chart_show_table")}
        </button>
      </figcaption>
      {asTable ? (
        <div className="max-h-64 overflow-y-auto">
          <table className="w-full text-[0.82rem]">
            <tbody>
              {data.map((d) => (
                <tr key={d.key} className="border-b border-[var(--line)] last:border-0">
                  <td className="py-1.5">{d.label}</td>
                  <td className="num py-1.5 text-end">{format(d.value)}</td>
                  {d.detail !== undefined && <td className="num py-1.5 text-end text-[var(--hint)]">{d.detail}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div ref={box} className="relative" dir="ltr" onMouseLeave={() => setHover(null)}>
          <svg viewBox={`0 0 ${width} ${height}`} className="h-auto w-full" role="img" aria-label={title}>
            {ticks.map((tick) => {
              const y = pad.top + plotH - (tick / top) * plotH;
              return (
                <g key={tick}>
                  <line x1={pad.side} x2={width} y1={y} y2={y} stroke="var(--line)" strokeWidth={1} />
                  <text x={pad.side - 6} y={y + 4} textAnchor="end" fontSize="10" fill="var(--hint)" className="num">
                    {axisFormat(tick)}
                  </text>
                </g>
              );
            })}
            {data.map((d, i) => {
              const h = top > 0 ? (d.value / top) * plotH : 0;
              const x = pad.side + i * band + (band - barW) / 2;
              const y = pad.top + plotH - h;
              return (
                <g key={d.key}>
                  <path
                    d={columnPath(x, y, barW, h)}
                    fill="var(--color-firouzeh-500)"
                    opacity={hover === null || hover === i ? 1 : 0.45}
                  />
                  {i % labelEvery === 0 && (
                    <text x={x + barW / 2} y={height - 6} textAnchor="middle" fontSize="10" fill="var(--hint)">
                      {d.label}
                    </text>
                  )}
                  <rect
                    x={pad.side + i * band}
                    y={pad.top}
                    width={band}
                    height={plotH}
                    fill="transparent"
                    onMouseEnter={() => setHover(i)}
                    onFocus={() => setHover(i)}
                    onBlur={() => setHover(null)}
                    onClick={() => setHover(i)}
                    tabIndex={0}
                    aria-label={`${d.label}: ${format(d.value)}`}
                  />
                </g>
              );
            })}
          </svg>
          {active && hover !== null && (
            <div
              className="pointer-events-none absolute top-0 z-10 -translate-x-1/2 rounded-lg bg-ink px-2.5 py-1.5 text-[0.78rem] text-white shadow-lg"
              style={{ left: `${((pad.side + (hover + 0.5) * band) / width) * 100}%` }}
            >
              <p className="font-semibold">{active.label}</p>
              <p className="num">{format(active.value)}</p>
              {active.detail && <p className="num opacity-75">{active.detail}</p>}
            </div>
          )}
        </div>
      )}
    </figure>
  );
}
