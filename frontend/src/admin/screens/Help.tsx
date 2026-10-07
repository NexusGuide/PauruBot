import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { article, articles, inline, linkTarget, search, type Block } from "../help";
import { useSession } from "../session";
import { Empty, Icon, Page, SearchBox, Section } from "../ui";

function Text({ text }: { text: string }) {
  return (
    <>
      {inline(text).map((piece, i) => {
        if (piece.kind === "bold") return <strong key={i}>{piece.text}</strong>;
        if (piece.kind === "code")
          return (
            <code key={i} dir="ltr" className="rounded-md bg-[var(--bg)] px-1.5 py-0.5 text-[0.85em] ring-1 ring-[var(--line)]">
              {piece.text}
            </code>
          );
        if (piece.kind === "link") {
          const to = linkTarget(piece.href);
          return to ? (
            <Link key={i} to={to} className="font-semibold text-firouzeh-700 underline decoration-firouzeh-500/40 underline-offset-4 dark:text-firouzeh-100">
              {piece.text}
            </Link>
          ) : (
            <span key={i}>{piece.text}</span>
          );
        }
        return <span key={i}>{piece.text}</span>;
      })}
    </>
  );
}

function Blocks({ blocks, locale }: { blocks: Block[]; locale: string }) {
  return (
    <div className="flex flex-col gap-4 text-[0.95rem] leading-[1.9]">
      {blocks.map((block, i) => {
        switch (block.kind) {
          case "h2":
            return (
              <h2 key={i} id={block.id} className="mt-4 scroll-mt-20 text-[1.15rem] font-extrabold leading-snug">
                {block.text}
              </h2>
            );
          case "h3":
            return (
              <h3 key={i} id={block.id} className="mt-2 scroll-mt-20 font-bold">
                {block.text}
              </h3>
            );
          case "p":
            return (
              <p key={i}>
                <Text text={block.text} />
              </p>
            );
          case "tip":
            return (
              <p key={i} className="rounded-2xl border-s-4 border-saffron-500 bg-saffron-100 px-4 py-3 text-[0.9rem] text-[var(--text)] dark:bg-saffron-700/20">
                <Text text={block.text} />
              </p>
            );
          case "ul":
          case "ol": {
            const List = block.kind;
            return (
              <List key={i} className={`flex flex-col gap-1.5 ps-6 ${block.kind === "ol" ? (locale === "fa" ? "[list-style-type:persian]" : "list-decimal") : "list-disc"} marker:text-[var(--hint)]`}>
                {block.items.map((item, j) => (
                  <li key={j}>
                    <Text text={item} />
                  </li>
                ))}
              </List>
            );
          }
          case "code":
            return (
              <pre key={i} dir="ltr" className="overflow-x-auto rounded-xl bg-[var(--bg)] p-3 text-[0.82rem] leading-relaxed ring-1 ring-[var(--line)]">
                {block.text}
              </pre>
            );
        }
      })}
    </div>
  );
}

export function HelpIndex() {
  const { t, locale } = useSession();
  const [query, setQuery] = useState("");
  const list = articles(locale);
  const hits = useMemo(() => (query.trim() ? search(locale, query) : []), [locale, query]);

  return (
    <Page title={t("nav_help")}>
      <Section flush>
        <div className="border-b border-[var(--line)] p-3">
          <SearchBox value={query} onChange={setQuery} placeholder={t("help_search")} />
        </div>
        {query.trim() ? (
          hits.length === 0 ? (
            <Empty title={t("help_no_results")} hint={t("help_no_results_hint")} />
          ) : (
            <ul className="divide-y divide-[var(--line)]">
              {hits.map((hit) => (
                <li key={hit.article.slug}>
                  <Link
                    to={`/help/${hit.article.slug}${hit.section ? `#${hit.section.id}` : ""}`}
                    className="block px-4 py-3 hover:bg-[var(--bg)]"
                  >
                    <p className="font-bold">
                      {hit.article.title}
                      {hit.section && <span className="font-semibold text-[var(--hint)]"> / {hit.section.text}</span>}
                    </p>
                    <p className="mt-1 line-clamp-2 text-[0.85rem] leading-relaxed text-[var(--hint)]">{hit.snippet}</p>
                  </Link>
                </li>
              ))}
            </ul>
          )
        ) : (
          <ul className="divide-y divide-[var(--line)]">
            {list.map((a) => (
              <li key={a.slug}>
                <Link to={`/help/${a.slug}`} className="flex items-center gap-3 px-4 py-3.5 hover:bg-[var(--bg)]">
                  <div className="min-w-0 flex-1">
                    <p className="font-bold">{a.title}</p>
                    <p className="mt-0.5 line-clamp-2 text-[0.84rem] leading-relaxed text-[var(--hint)]">{a.intro}</p>
                  </div>
                  <Icon name="down" size={18} className="shrink-0 text-[var(--hint)] ltr:-rotate-90 rtl:rotate-90" />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </Page>
  );
}

export function HelpArticle() {
  const { t, locale } = useSession();
  const { slug = "" } = useParams();
  const { hash } = useLocation();
  const list = articles(locale);
  const found = article(locale, slug);

  useEffect(() => {
    if (!hash) {
      window.scrollTo(0, 0);
      return;
    }
    const target = document.getElementById(decodeURIComponent(hash.slice(1)));
    target?.scrollIntoView({ block: "start" });
  }, [slug, hash]);

  if (!found) {
    return (
      <Page title={t("nav_help")}>
        <Section>
          <Empty title={t("help_missing")} action={<Link to="/help" className="font-semibold text-firouzeh-700">{t("help_all")}</Link>} />
        </Section>
      </Page>
    );
  }
  const position = list.findIndex((a) => a.slug === found.slug);
  const next = list[position + 1];
  const sections = found.blocks.flatMap((b) => (b.kind === "h2" ? [b] : []));

  return (
    <Page
      title={found.title}
      actions={
        <Link to="/help" className="inline-flex min-h-10 items-center gap-1.5 rounded-xl px-3 font-semibold text-firouzeh-700 ring-1 ring-[var(--line)] dark:text-firouzeh-100">
          <Icon name="search" size={18} />
          {t("help_all")}
        </Link>
      }
    >
      {sections.length > 2 && (
        <nav aria-label={t("help_contents")} className="flex flex-wrap gap-2">
          {sections.map((s) => (
            <a key={s.id} href={`#${s.id}`} className="rounded-full bg-[var(--surface)] px-3 py-1.5 text-[0.82rem] font-semibold ring-1 ring-[var(--line)]">
              {s.text}
            </a>
          ))}
        </nav>
      )}
      <Section>
        <article className="mx-auto max-w-[68ch]">
          <Blocks blocks={found.blocks} locale={locale} />
        </article>
      </Section>
      {next && (
        <Link to={`/help/${next.slug}`} className="block rounded-2xl bg-[var(--surface)] px-4 py-3 ring-1 ring-[var(--line)]">
          <span className="text-[0.8rem] text-[var(--hint)]">{t("help_next")}</span>
          <span className="block font-bold">{next.title}</span>
        </Link>
      )}
    </Page>
  );
}
