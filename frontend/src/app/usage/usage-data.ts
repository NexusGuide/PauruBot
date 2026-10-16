import type { UsageHistory, UsagePoint } from "../api";

/** Only actual, ordered observations can become marks. Missing data never becomes zero. */
export function validateHistory(value: UsageHistory, serviceId: number, days: 7 | 30): UsageHistory {
  const start = Date.parse(value.from), end = Date.parse(value.to);
  if (value.service_id !== serviceId || value.days !== days || value.resolution !== "hourly_last_observation"
    || !Number.isFinite(start) || !Number.isFinite(end) || start >= end || !Array.isArray(value.points)
    || value.points.length > days * 24 + 1) throw new Error("usage_invalid");
  let previous = -Infinity;
  for (const p of value.points) {
    const at = Date.parse(p.at);
    if (!Number.isFinite(at) || at < start || at > end || at <= previous
      || !Number.isSafeInteger(p.used_bytes) || p.used_bytes < 0 || typeof p.break_before !== "boolean") {
      throw new Error("usage_invalid");
    }
    previous = at;
  }
  return value;
}

export function segments(points: UsagePoint[]): UsagePoint[][] {
  const result: UsagePoint[][] = [];
  let previous: UsagePoint | undefined;
  for (const p of points) {
    if (!previous || p.break_before || p.used_bytes < previous.used_bytes
      || Date.parse(p.at) - Date.parse(previous.at) > 2 * 3600_000) result.push([]);
    result[result.length - 1]!.push(p);
    previous = p;
  }
  return result;
}

export function valueScale(points: UsagePoint[]) {
  const max = Math.max(0, ...points.map(p => p.used_bytes));
  const units = [[1024 ** 3, "GiB"], [1024 ** 2, "MiB"], [1024, "KiB"], [1, "B"]] as const;
  const [divisor, unit] = units.find(([value]) => max >= value) ?? [1, "B"];
  const rawStep = max / divisor / 4;
  const power = 10 ** Math.floor(Math.log10(rawStep || 1));
  const step = [1, 2, 5, 10].map(v => v * power).find(v => v >= rawStep) ?? 1;
  return { divisor, unit, maximum: step * 4, ticks: [0, step, step * 2, step * 3, step * 4] };
}
