// How the bot-texts screen is laid out: the ~2,000 texts sorted into the parts of the bot they
// belong to, and a safe preview of Telegram's HTML. Pure, so it is tested on its own.

export const SECTIONS = [
  "start",
  "purchase",
  "payment",
  "services",
  "trial",
  "growth",
  "support",
  "resellers",
  "web",
  "reports",
  "admin",
  "other",
] as const;
export type Section = (typeof SECTIONS)[number];

const FAMILIES: Record<Exclude<Section, "other">, string[]> = {
  start: ["welcome", "start", "menu", "main", "home", "lang", "terms", "phone", "join", "cmd", "help", "back", "cancel", "trust", "no", "my", "already", "status"],
  purchase: ["buy", "choose", "plan", "order", "checkout", "custom", "bulk", "renew", "upgrade", "move", "discount", "extra", "stock", "national", "volume", "expiry", "method", "amount", "badge"],
  payment: ["payment", "pay", "card", "wallet", "topup", "tx", "transfer", "gift", "stars", "crypto", "receipt", "bonus", "gateway", "balance", "void", "history", "canceled", "approved"],
  services: ["service", "services", "sub", "svc", "usage", "config", "link", "links", "device", "alert", "alerts", "expire", "auto", "infocard", "qr", "app", "apps", "platform", "wireguard", "username", "panel", "account", "accounts", "customer", "all"],
  trial: ["trial"],
  growth: ["referral", "wheel", "lottery", "loyalty", "tier", "invite", "prize", "marketing"],
  support: ["ticket", "support", "faq", "dept"],
  resellers: ["reseller", "rs", "rb", "acc", "resale", "subreseller", "merchant"],
  web: ["guest"],
  reports: ["report", "notify", "business", "backup", "auth", "staff"],
  admin: ["adm"],
};
const FAMILY_OF = new Map<string, Section>(
  Object.entries(FAMILIES).flatMap(([section, words]) => words.map((word) => [word, section as Section] as const)),
);

/** The part of the bot a text belongs to, from its key; a button goes where its action does. */
export function sectionOf(key: string): Section {
  const [first, second] = key.split("_");
  if (first === "btn") return (second && FAMILY_OF.get(second)) || "start";
  return (first && FAMILY_OF.get(first)) || "other";
}

/** Texts on buttons: the main menu's (`btn_…`) and those inside a feature (`acc_btn_…`). */
export function isButton(key: string): boolean {
  return key.startsWith("btn_") || key.includes("_btn_");
}

/** The order inside a section: the welcome text first, then buttons, then the rest by key. */
export function bySectionOrder(a: string, b: string): number {
  const rank = (key: string) => (key === "welcome" ? 0 : key.startsWith("start_") ? 1 : isButton(key) ? 2 : 3);
  return rank(a) - rank(b) || a.localeCompare(b);
}

export function countBySection(keys: string[]): Record<Section, number> {
  const counts = Object.fromEntries(SECTIONS.map((section) => [section, 0])) as Record<Section, number>;
  for (const key of keys) counts[sectionOf(key)] += 1;
  return counts;
}

// --- preview -----------------------------------------------------------------------------

/** A text as Telegram shows it: plain strings and the few formatting tags it supports. */
export type PreviewNode = string | { tag: PreviewTag; children: PreviewNode[] };
export type PreviewTag = "b" | "i" | "u" | "s" | "code" | "pre" | "a" | "spoiler" | "quote" | "var";

const TAGS: Record<string, PreviewTag> = {
  b: "b", strong: "b", i: "i", em: "i", u: "u", ins: "u", s: "s", strike: "s", del: "s",
  code: "code", pre: "pre", a: "a", "tg-spoiler": "spoiler", blockquote: "quote",
};
const ENTITIES: Record<string, string> = { "&lt;": "<", "&gt;": ">", "&quot;": '"', "&amp;": "&" };

function decode(text: string): string {
  return text.replace(/&(lt|gt|quot|amp);/g, (entity) => ENTITIES[entity] ?? entity);
}

/** Fill `{name}`-style variables with sample values; one with no sample stays visible as a chip. */
function fill(text: string, samples: Record<string, string>): PreviewNode[] {
  const out: PreviewNode[] = [];
  let last = 0;
  for (const match of text.matchAll(/\{([a-z_][a-z0-9_]*)\}/gi)) {
    const index = match.index ?? 0;
    if (index > last) out.push(decode(text.slice(last, index)));
    const name = match[1] ?? "";
    out.push(name in samples ? (samples[name] ?? "") : { tag: "var", children: [name] });
    last = index + match[0].length;
  }
  if (last < text.length) out.push(decode(text.slice(last)));
  return out;
}

/**
 * Parse Telegram's HTML subset into nodes. Anything that is not a supported tag stays literal
 * text, and nothing is ever handed to the browser as markup, so a text cannot inject HTML.
 */
export function preview(text: string, samples: Record<string, string> = {}): PreviewNode[] {
  const root: { tag: PreviewTag | null; children: PreviewNode[] } = { tag: null, children: [] };
  const stack = [root];
  const top = () => stack[stack.length - 1] ?? root;
  let last = 0;
  for (const match of text.matchAll(/<(\/?)([a-z-]+)(?:\s[^<>]*)?>/gi)) {
    const index = match.index ?? 0;
    const name = (match[2] ?? "").toLowerCase();
    const tag = TAGS[name];
    if (!tag) continue; // not Telegram markup: shown as typed
    if (index > last) top().children.push(...fill(text.slice(last, index), samples));
    last = index + match[0].length;
    if (match[1]) {
      // A closing tag ends the nearest open element of its kind; a stray one is dropped.
      const at = stack.map((node) => node.tag).lastIndexOf(tag);
      if (at > 0) stack.length = at;
    } else {
      const node = { tag, children: [] as PreviewNode[] };
      top().children.push(node);
      stack.push(node);
    }
  }
  if (last < text.length) top().children.push(...fill(text.slice(last), samples));
  return root.children;
}
