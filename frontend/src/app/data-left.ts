/** Bytes left of a quota; never below zero once usage passes it. `null` when there is no limit. */
export function dataLeft(limit: number | null | undefined, used: number): number | null {
  if (!limit) return null;
  return Math.max(0, limit - used);
}
