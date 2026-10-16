/** Runtime boundary for the existing F-04 API. Unknown checks remain visible as plain text. */
export type CheckStatus = "ok" | "warn" | "fail" | "skip";
export interface CapabilityCheck { key: string; status: CheckStatus; detail: string }
export interface CapabilityReport { status: "ok" | "warn" | "fail"; write_tested: boolean; checks: CapabilityCheck[] }
export function validateReport(value: unknown): CapabilityReport {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("capability_invalid");
  const data = value as Record<string, unknown>;
  if (!["ok", "warn", "fail"].includes(String(data.status)) || typeof data.write_tested !== "boolean"
    || !Array.isArray(data.checks) || data.checks.length < 1 || data.checks.length > 64) throw new Error("capability_invalid");
  const keys = new Set<string>();
  const checks = data.checks.map((item: unknown): CapabilityCheck => {
    if (!item || typeof item !== "object" || Array.isArray(item)) throw new Error("capability_invalid");
    const c = item as Record<string, unknown>;
    if (typeof c.key !== "string" || !c.key || c.key.length > 128 || keys.has(c.key)
      || typeof c.status !== "string" || !["ok", "warn", "fail", "skip"].includes(c.status)
      || typeof c.detail !== "string" || c.detail.length > 8192) throw new Error("capability_invalid");
    keys.add(c.key);
    return { key: c.key, status: c.status as CheckStatus, detail: c.detail };
  });
  const expected = checks.some(c => c.status === "fail") ? "fail" : checks.some(c => c.status === "warn") ? "warn" : "ok";
  if (data.write_tested !== checks.some(c => c.key === "create")) throw new Error("capability_invalid");
  if (data.status !== expected) throw new Error("capability_invalid");
  return { status: expected, write_tested: data.write_tested, checks };
}
export const CHECK_KEYS = ["connect", "list_options", "list_users", "list_admins", "client_ips", "webhooks", "delivery", "changes", "create", "read", "update", "revoke", "delete"] as const;
export function needsCleanup(report: CapabilityReport): boolean { return report.checks.some(c => c.key === "delete" && c.status === "fail"); }

export function cleanupUnconfirmed(report: CapabilityReport): boolean { return report.write_tested && !report.checks.some(c => c.key === "delete" && c.status === "ok"); }
