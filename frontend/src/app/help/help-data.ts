import type { CustomerHelp, HelpApp } from "../api";

/** Navigation-only defense in depth. No ownership/redirect/malware guarantee. */
export function safeHelpUrl(value: unknown): string | null {
  if (typeof value !== "string" || value.length < 1 || value.length > 2048
    || /[^\x21-\x7e]/.test(value) || value.includes("\\") || /%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)/i.test(value)
    || !/^https:\/\//i.test(value)) return null;
  try {
    const url = new URL(value);
    const authority = value.slice(8).split(/[/?#]/)[0] ?? "";
    if (authority.includes("@") || authority.includes("%") || url.username || url.password || (url.port && url.port !== "443")) return null;
    const labels = url.hostname.split(".");
    if (url.hostname.length > 253 || labels.length < 2
      || !labels.every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))
      || !/^[a-z][a-z0-9-]*$/.test(labels.at(-1) ?? "")
      || ["local", "localhost", "internal"].includes(labels.at(-1) ?? "")) return null;
    return value;
  } catch { return null; }
}

export function normaliseHelp(value: string): string {
  return value.toLowerCase().replace(/[يى]/g, "ی").replace(/ك/g, "ک").replace(/[أإآ]/g, "ا")
    .replace(/[ً-ٰٟـ]/g, "").replace(/[۰-۹]/g, digit => String(digit.charCodeAt(0) - 0x06f0))
    .replace(/[‌‍‎‏]/g, " ").replace(/\s+/g, " ").trim();
}

export function matchesHelp(value: string, query: string): boolean {
  const haystack = normaliseHelp(value);
  return normaliseHelp(query).split(" ").filter(Boolean).every(term => haystack.includes(term));
}

export function platformApps(items: HelpApp[], platform: string): HelpApp[] {
  return items.filter(item => platform === "all" || item.platform === platform);
}

/** Validate the small public contract before rendering: never render stale/malformed success. */
export function validateHelp(value: CustomerHelp): CustomerHelp {
  if (!value || !Array.isArray(value.articles) || value.articles.length > 30
    || !Array.isArray(value.apps) || value.apps.length > 20
    || !(value.bot_url === null || typeof value.bot_url === "string")) throw new Error("help_invalid");
  const articleIds = new Set<number>(), appIds = new Set<number>();
  for (const item of value.articles) {
    if (!item || !Number.isSafeInteger(item.id) || item.id < 0 || articleIds.has(item.id)
      || typeof item.title !== "string" || !item.title.trim() || [...item.title].length > 128
      || typeof item.text !== "string" || [...item.text].length > 8000
      || !(item.url === null || typeof item.url === "string")) throw new Error("help_invalid");
    articleIds.add(item.id);
  }
  for (const app of value.apps) {
    if (!app || !Number.isSafeInteger(app.id) || app.id < 0 || appIds.has(app.id)
      || typeof app.name !== "string" || !app.name.trim() || [...app.name].length > 32
      || !["android", "ios", "windows", "mac", "linux"].includes(app.platform)
      || !(app.store_url === null || typeof app.store_url === "string")
      || !(app.release_url === null || typeof app.release_url === "string")) throw new Error("help_invalid");
    appIds.add(app.id);
  }
  return value;
}
