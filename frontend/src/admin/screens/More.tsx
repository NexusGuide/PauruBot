// The phone's "more": every page this person may open, in its folder, with a search box.
import { useState } from "react";
import { Link } from "react-router-dom";
import { useSession } from "../session";
import { AccountBlock, useCounts, useGroups } from "../layout";
import { NAV_COUNTS } from "../nav";
import { matchesQuery } from "../settings-layout";
import { Empty, Icon } from "../ui";

// Each folder keeps one tint so a page is easy to find again by colour.
const TINTS = [
  "bg-firouzeh-100 text-firouzeh-700",
  "bg-pista-100 text-pista-700",
  "bg-saffron-100 text-saffron-700",
  "bg-[#2a2344] text-[#cdbbff]",
  "bg-anar-100 text-anar-700",
  "bg-[#14303a] text-[#8fd8e8]",
  "bg-firouzeh-100 text-firouzeh-700",
];

export function More() {
  const { t, locale } = useSession();
  const groups = useGroups();
  const counts = useCounts();
  const [query, setQuery] = useState("");
  const shown = groups
    .map((group, index) => ({
      key: group.key,
      tint: TINTS[index % TINTS.length]!,
      pages: group.hubs.flatMap((hub) => hub.tabs).filter((page) => matchesQuery(query, t(page.key), page.to)),
    }))
    .filter((group) => group.pages.length > 0);

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 pb-28 pt-4 md:px-8 md:pt-8">
      <label className="flex h-12 items-center gap-2.5 rounded-2xl bg-[var(--surface)] px-4 ring-1 ring-[var(--line)] focus-within:ring-2 focus-within:ring-firouzeh-500">
        <Icon name="search" size={19} className="text-[var(--hint)]" />
        <span className="sr-only">{t("more_search")}</span>
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("more_search")}
          className="min-w-0 flex-1 bg-transparent text-[0.95rem] outline-none"
        />
      </label>

      {shown.map((group) => (
        <section key={group.key} className="flex flex-col gap-2.5">
          <h2 className="px-1 text-[0.8rem] font-bold text-[var(--hint)]">{t(group.key)}</h2>
          <ul className="grid grid-cols-3 gap-2 sm:grid-cols-4">
            {group.pages.map((page) => {
              const field = NAV_COUNTS[page.to];
              const n = field ? (counts[field] ?? 0) : 0;
              return (
                <li key={page.to}>
                  <Link
                    to={page.to}
                    className="relative flex min-h-24 flex-col items-center justify-center gap-2 rounded-2xl bg-[var(--surface)] px-1 py-3 text-center ring-1 ring-[var(--line)] transition-colors hover:bg-[var(--surface-2)]"
                  >
                    <span className={`grid h-10 w-10 place-items-center rounded-xl ${group.tint}`}>
                      <Icon name={page.icon} size={20} />
                    </span>
                    <span className="text-[0.78rem] font-semibold leading-snug">{t(page.key)}</span>
                    {n > 0 && (
                      <span className="num absolute end-2 top-2 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-saffron-500 px-1.5 text-[0.7rem] font-extrabold text-[#1a1406]">
                        {new Intl.NumberFormat(locale === "fa" ? "fa-IR" : "en-US").format(n)}
                      </span>
                    )}
                  </Link>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
      {shown.length === 0 && <Empty title={t("more_none")} />}

      <section className="rounded-2xl bg-[var(--surface)] p-3 ring-1 ring-[var(--line)]">
        <AccountBlock />
      </section>
    </div>
  );
}
