import { useState } from "react";
import { Link } from "react-router-dom";
import { digits } from "@shared/format";
import { useLoad } from "../hooks";
import { useSession } from "../session";
import { SECTIONS, sectionOf, type Section as TextSection } from "../texts-layout";
import { Async, Badge, CopyText, Empty, Page, SearchBox, Section, Intro } from "../ui";

export interface QuickLink {
  item: string;
  start: string;
  on: boolean;
}

/** The deep link a customer presses; without a bot username the panel shows only the start value. */
export function linkFor(botUsername: string | null, start: string): string {
  return botUsername ? `https://t.me/${botUsername}?start=${start}` : `?start=${start}`;
}

export function QuickLinks() {
  const { t } = useSession();
  const { data, error, reload } = useLoad<{ bot_username: string | null; items: QuickLink[] }>("/api/admin/quick-links");
  return (
    <Page title={t("nav_quick_links")} wide>
      <Async data={data} error={error} reload={() => void reload()}>
        {({ bot_username, items }) => (
          <>
            <Intro>{t("qlinks_intro")}</Intro>
            {!bot_username && <p className="rounded-xl bg-saffron-100 p-3 text-[0.86rem] text-saffron-700">{t("qlinks_no_username")}</p>}
            <Section title={t("qlinks_sections")}>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                {items.map((link) => (
                  <div key={link.item} className="flex flex-col gap-1.5 rounded-xl p-3 ring-1 ring-[var(--line)]">
                    <div className="flex items-center gap-2">
                      <b className="flex-1 text-[0.9rem]">{t(`menu_btn_${link.item}`)}</b>
                      {!link.on && <Badge tone="wait">{t("qlinks_off")}</Badge>}
                    </div>
                    <CopyText text={linkFor(bot_username, link.start)} />
                  </div>
                ))}
              </div>
              <p className="mt-3 text-[0.8rem] text-[var(--hint)]">
                {t("qlinks_off_hint")} <Link to="/bot-menu" className="font-semibold text-firouzeh-700">{t("nav_bot_menu")}</Link>
              </p>
            </Section>
            <Section title={t("qlinks_more")}>
              <ul className="flex flex-col gap-2 text-[0.86rem]">
                <li>
                  <b>{t("qlinks_referral")}</b> — <span className="text-[var(--hint)]">{t("qlinks_referral_hint")}</span>{" "}
                  <code dir="ltr" className="rounded bg-[var(--line)] px-1.5">?start=ref_&lt;id&gt;</code>
                </li>
                <li>
                  <b>{t("qlinks_start")}</b> — <CopyText text={linkFor(bot_username, "")} short />
                </li>
              </ul>
            </Section>
          </>
        )}
      </Async>
    </Page>
  );
}

export interface MessageVariable {
  name: string;
  count: number;
  keys: string[];
}

/** A variable belongs to the part of the bot where most of its texts are. */
export function variableSection(keys: string[]): TextSection {
  const counts = new Map<TextSection, number>();
  for (const key of keys) counts.set(sectionOf(key), (counts.get(sectionOf(key)) ?? 0) + 1);
  let best: TextSection = "other";
  let most = 0;
  for (const section of SECTIONS) {
    const n = counts.get(section) ?? 0;
    if (n > most) [best, most] = [section, n];
  }
  return best;
}

export function Variables() {
  const { t, locale } = useSession();
  const { data, error, reload } = useLoad<{ items: MessageVariable[] }>("/api/admin/message-variables");
  const [query, setQuery] = useState("");
  return (
    <Page title={t("nav_variables")} wide>
      <Async data={data} error={error} reload={() => void reload()}>
        {({ items }) => {
          const q = query.trim().toLowerCase().replace(/[{}]/g, "");
          const shown = items.filter((v) => !q || v.name.includes(q) || v.keys.some((k) => k.includes(q)));
          const groups = SECTIONS.map((section) => ({
            section,
            vars: shown.filter((v) => variableSection(v.keys) === section),
          })).filter((g) => g.vars.length > 0);
          return (
            <>
              <Intro>{t("vars_intro")}</Intro>
              <div className="md:w-80">
                <SearchBox value={query} onChange={setQuery} placeholder={t("vars_search")} />
              </div>
              {groups.length === 0 && <Empty title={t("empty_list")} />}
              {groups.map(({ section, vars }) => (
                <Section key={section} title={`${t(`texts_section_${section}`)} · ${digits(vars.length, locale)}`}>
                  <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
                    {vars.map((v) => (
                      <div key={v.name} className="flex flex-col gap-1 rounded-xl p-3 ring-1 ring-[var(--line)]">
                        <div className="flex items-center gap-2">
                          <CopyText text={`{${v.name}}`} />
                          <span className="ms-auto text-[0.75rem] text-[var(--hint)]">{t("vars_used", { count: digits(v.count, locale) })}</span>
                        </div>
                        <p className="truncate text-[0.75rem] text-[var(--hint)]" dir="ltr" title={v.keys.join(", ")}>
                          {v.keys.join(", ")}
                        </p>
                      </div>
                    ))}
                  </div>
                </Section>
              ))}
            </>
          );
        }}
      </Async>
    </Page>
  );
}
