/** Pure route grouping; no history guessing, query parsing or order/payment mutation. */
export type AppTab = "home" | "buy" | "services" | "wallet" | "more";
export function tabForPath(pathname: string): AppTab {
  const part = pathname.split("/")[1] ?? "";
  if (["buy", "custom", "checkout", "done"].includes(part)) return "buy";
  if (["services", "extra"].includes(part)) return "services";
  if (["wallet", "payment"].includes(part)) return "wallet";
  if (["more", "profile", "tickets", "help", "manage", "desk"].includes(part)) return "more";
  return "home";
}
export function parentForPath(pathname: string): string | null {
  const parts = pathname.split("/").filter(Boolean), part = parts[0] ?? "";
  if (["profile", "help", "manage", "desk"].includes(part)) return "/more";
  if (part === "tickets") return parts.length > 1 ? "/tickets" : "/more";
  if (part === "services" && parts.length > 1) return "/services";
  if (part === "extra") return parts[1] && /^\d+$/.test(parts[1]) ? `/services/${parts[1]}` : "/services";
  if (["custom", "checkout"].includes(part)) return "/buy";
  if (part === "payment") return "/wallet";
  if (part === "done") return "/services";
  return null;
}
