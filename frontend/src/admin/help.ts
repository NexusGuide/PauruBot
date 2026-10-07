/**
 * In-panel help (ADM-05). Articles are Markdown files under ./help/<locale>/<slug>.md,
 * bundled at build time and parsed into a small, safe block model (no raw HTML).
 */

export type Inline = { kind: "text" | "bold" | "code"; text: string } | { kind: "link"; text: string; href: string };

export type Block =
  | { kind: "h2" | "h3"; text: string; id: string }
  | { kind: "p" | "tip"; text: string }
  | { kind: "ul" | "ol"; items: string[] }
  | { kind: "code"; text: string };

export interface Article {
  slug: string;
  locale: string;
  title: string;
  intro: string;
  screens: string[];
  blocks: Block[];
  /** Normalised text used for search. */
  index: { title: string; headings: string; body: string };
}

const SOURCES = import.meta.glob<string>("./help/*/*.md", { query: "?raw", import: "default", eager: true });

/** Reading order; articles not listed here follow alphabetically. */
export const ORDER = [
  "getting-started",
  "panels",
  "catalog",
  "payments",
  "customers-services",
  "resellers",
  "bot-menu",
  "growth",
  "reports",
  "staff-security",
  "backups-updates",
  "migration",
  "troubleshooting",
];

const SCREENS_RE = /^<!--\s*screens:\s*(.*?)\s*-->$/;

export function slugify(text: string): string {
  return normalise(text).replace(/\s+/g, "-").slice(0, 60);
}

/** Lowercase, unify Arabic and Persian letters, drop diacritics and joiners. */
export function normalise(text: string): string {
  return text
    .toLowerCase()
    .replace(/[يى]/g, "ی")
    .replace(/ك/g, "ک")
    .replace(/[أإآ]/g, "ا")
    .replace(/ة/g, "ه")
    .replace(/[ً-ٰٟـ]/g, "")
    .replace(/[۰-۹]/g, (d) => String(d.charCodeAt(0) - 0x06f0))
    .replace(/[‌‍‎‏]/g, " ")
    .replace(/[*`[\]()#>_]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function parse(slug: string, locale: string, source: string): Article {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  let title = slug;
  let screens: string[] = [];
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let list: { kind: "ul" | "ol"; items: string[] } | null = null;
  let code: string[] | null = null;

  const flush = () => {
    if (paragraph.length) blocks.push({ kind: "p", text: paragraph.join(" ") });
    paragraph = [];
    if (list) blocks.push(list);
    list = null;
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (code !== null) {
      if (line.startsWith("```")) {
        blocks.push({ kind: "code", text: code.join("\n") });
        code = null;
      } else code.push(raw);
      continue;
    }
    if (line.startsWith("```")) {
      flush();
      code = [];
      continue;
    }
    const screensMatch = SCREENS_RE.exec(line.trim());
    if (screensMatch) {
      screens = (screensMatch[1] ?? "").split(/\s+/).filter(Boolean);
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    let match: RegExpExecArray | null;
    if ((match = /^(#{1,3})\s+(.*)$/.exec(line))) {
      flush();
      const level = (match[1] ?? "").length;
      const heading = (match[2] ?? "").trim();
      if (level === 1) title = heading;
      else blocks.push({ kind: level === 2 ? "h2" : "h3", text: heading, id: slugify(heading) });
      continue;
    }
    if ((match = /^>\s?(.*)$/.exec(line))) {
      flush();
      const last = blocks[blocks.length - 1];
      const tip = match[1] ?? "";
      if (last && last.kind === "tip") last.text += ` ${tip}`;
      else blocks.push({ kind: "tip", text: tip });
      continue;
    }
    if ((match = /^(?:-|\*)\s+(.*)$/.exec(line)) || (match = /^\d+[.)]\s+(.*)$/.exec(line))) {
      const kind = /^\d/.test(line) ? "ol" : "ul";
      if (paragraph.length) {
        blocks.push({ kind: "p", text: paragraph.join(" ") });
        paragraph = [];
      }
      if (!list || list.kind !== kind) {
        if (list) blocks.push(list);
        list = { kind, items: [] };
      }
      list.items.push((match[1] ?? "").trim());
      continue;
    }
    if (list && /^\s+\S/.test(raw)) {
      list.items[list.items.length - 1] += ` ${line.trim()}`;
      continue;
    }
    if (list) {
      blocks.push(list);
      list = null;
    }
    paragraph.push(line.trim());
  }
  if (code !== null) blocks.push({ kind: "code", text: (code as string[]).join("\n") });
  flush();

  const firstParagraph = blocks.find((b) => b.kind === "p");
  const intro = firstParagraph && "text" in firstParagraph ? firstParagraph.text : "";
  const headings = blocks.filter((b) => b.kind === "h2" || b.kind === "h3").map((b) => ("text" in b ? b.text : ""));
  const body = blocks
    .map((b) => ("items" in b ? b.items.join(" ") : b.kind === "h2" || b.kind === "h3" ? "" : b.text))
    .join(" ");
  return {
    slug,
    locale,
    title,
    intro,
    screens,
    blocks,
    index: { title: normalise(title), headings: normalise(headings.join(" ")), body: normalise(body) },
  };
}

/** Split `**bold**`, `` `code` `` and `[text](href)` into inline pieces. */
export function inline(text: string): Inline[] {
  const out: Inline[] = [];
  const re = /\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text))) {
    if (match.index > last) out.push({ kind: "text", text: text.slice(last, match.index) });
    if (match[1] !== undefined) out.push({ kind: "bold", text: match[1] });
    else if (match[2] !== undefined) out.push({ kind: "code", text: match[2] });
    else out.push({ kind: "link", text: match[3] ?? "", href: match[4] ?? "" });
    last = re.lastIndex;
  }
  if (last < text.length) out.push({ kind: "text", text: text.slice(last) });
  return out;
}

/** Only in-panel routes and other help articles are followed; anything else stays plain text. */
export function linkTarget(href: string): string | null {
  if (href.startsWith("help:")) {
    const slug = href.slice(5);
    return /^[a-z0-9-]+(#[a-z0-9؀-ۿ-]+)?$/.test(slug) ? `/help/${slug}` : null;
  }
  return /^\/[a-z0-9/-]*$/.test(href) ? href : null;
}

let cache: Map<string, Article[]> | null = null;

function all(): Map<string, Article[]> {
  if (cache) return cache;
  cache = new Map();
  for (const [path, source] of Object.entries(SOURCES)) {
    const match = /\.\/help\/([a-z]{2})\/([a-z0-9-]+)\.md$/.exec(path);
    if (!match) continue;
    const locale = match[1] ?? "";
    const slug = match[2] ?? "";
    const list = cache.get(locale) ?? [];
    list.push(parse(slug, locale, source));
    cache.set(locale, list);
  }
  const rank = (slug: string) => {
    const i = ORDER.indexOf(slug);
    return i === -1 ? ORDER.length : i;
  };
  for (const list of cache.values()) list.sort((a, b) => rank(a.slug) - rank(b.slug) || a.slug.localeCompare(b.slug));
  return cache;
}

export function articles(locale: string): Article[] {
  const map = all();
  return map.get(locale) ?? map.get("en") ?? map.get("fa") ?? [];
}

export function article(locale: string, slug: string): Article | undefined {
  return articles(locale).find((a) => a.slug === slug);
}

/** The article that explains the screen at `pathname`, if any (longest matching route wins). */
export function articleForPath(locale: string, pathname: string): Article | undefined {
  let best: Article | undefined;
  let bestLength = 0;
  for (const a of articles(locale)) {
    for (const screen of a.screens) {
      const fits = screen === "/" ? pathname === "/" : pathname === screen || pathname.startsWith(`${screen}/`);
      if (fits && screen.length > bestLength) {
        best = a;
        bestLength = screen.length;
      }
    }
  }
  return best;
}

export interface Hit {
  article: Article;
  score: number;
  snippet: string;
  section?: { id: string; text: string };
}

function snippetFor(a: Article, terms: string[]): { snippet: string; section?: { id: string; text: string } } {
  let section: { id: string; text: string } | undefined;
  for (const block of a.blocks) {
    if (block.kind === "h2" || block.kind === "h3") {
      section = { id: block.id, text: block.text };
      if (terms.every((t) => normalise(block.text).includes(t))) return { snippet: a.intro, section };
      continue;
    }
    const texts = "items" in block ? block.items : [block.text];
    for (const text of texts) {
      const plain = normalise(text);
      const at = plain.indexOf(terms[0] ?? "");
      if (at !== -1 && terms.every((t) => plain.includes(t))) {
        const clean = text.replace(/\*\*|`/g, "").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
        const start = Math.max(0, at - 50);
        const piece = clean.slice(start, start + 170);
        return { snippet: `${start > 0 ? "…" : ""}${piece}${start + 170 < clean.length ? "…" : ""}`, section };
      }
    }
  }
  return { snippet: a.intro };
}

export function search(locale: string, query: string): Hit[] {
  const terms = normalise(query).split(" ").filter((t) => t.length > 1 || /\d/.test(t));
  if (!terms.length) return [];
  const hits: Hit[] = [];
  for (const a of articles(locale)) {
    let score = 0;
    let all = true;
    for (const term of terms) {
      const inTitle = a.index.title.includes(term);
      const inHeadings = a.index.headings.includes(term);
      const count = a.index.body.split(term).length - 1;
      if (!inTitle && !inHeadings && !count) all = false;
      score += (inTitle ? 8 : 0) + (inHeadings ? 4 : 0) + Math.min(count, 6);
    }
    if (all && score > 0) hits.push({ article: a, score, ...snippetFor(a, terms) });
  }
  return hits.sort((x, y) => y.score - x.score).slice(0, 12);
}
