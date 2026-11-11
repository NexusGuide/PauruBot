// Every button of the bot with its own color and premium emoji (ClockBot parity).
// Phone first: one column of cards, a sticky save bar, a live preview of each button.
import { useEffect, useMemo, useState } from "react";
import { call } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { SECTIONS, sectionOf, type Section as TextSection } from "../texts-layout";
import { Async, Button, Input, Page, SearchBox, Section, Intro } from "../ui";

export type Tone = "" | "primary" | "success" | "danger";
export interface Look { style?: Tone; emoji_id?: string }
export type Looks = Record<string, Look>;
interface Item { key: string; fa: string; en: string }

export const TONES: Tone[] = ["", "primary", "success", "danger"];
const SWATCH: Record<Tone, string> = {
  "": "bg-[var(--surface-2)] text-[var(--text)] ring-1 ring-[var(--line)]",
  primary: "bg-[#3390ec] text-white",
  success: "bg-[#31b545] text-white",
  danger: "bg-[#e5484d] text-white",
};

/** What is saved: only buttons with a color or a numeric premium emoji id. */
export function cleanLooks(looks: Looks): Looks {
  const out: Looks = {};
  for (const [key, look] of Object.entries(looks)) {
    const style = look.style && TONES.includes(look.style) ? look.style : "";
    const emoji = /^\d{5,25}$/.test(look.emoji_id ?? "") ? look.emoji_id : "";
    if (style || emoji) out[key] = { ...(style ? { style } : {}), ...(emoji ? { emoji_id: emoji } : {}) };
  }
  return out;
}

export function ButtonLooks() {
  const { t, locale, can, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<{ items: Item[]; looks: Looks }>("/api/admin/button-looks");
  const [looks, setLooks] = useState<Looks>({});
  const [query, setQuery] = useState("");
  const [busy, run] = useAction();
  useEffect(() => { if (data) setLooks(data.looks ?? {}); }, [data]);
  const dirty = useMemo(() => JSON.stringify(cleanLooks(looks)) !== JSON.stringify(cleanLooks(data?.looks ?? {})), [looks, data]);
  const writable = can("settings.write");
  const set = (key: string, patch: Look) => setLooks((all) => ({ ...all, [key]: { ...all[key], ...patch } }));
  const save = async () => {
    try {
      await run(() => call("/api/admin/settings", { method: "PUT", body: { values: { button_looks: cleanLooks(looks) } } }));
      toast(t("saved"));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };
  return (
    <Page title={t("nav_button_looks")} wide>
      <Async data={data} error={error} reload={() => void reload()}>
        {({ items }) => {
          const q = query.trim().toLowerCase();
          const shown = items.filter((i) => !q || i.key.includes(q) || i.fa.toLowerCase().includes(q) || i.en.toLowerCase().includes(q));
          const grouped = SECTIONS.map((s) => [s, shown.filter((i) => sectionOf(i.key) === s)] as const).filter(([, list]) => list.length);
          return (
            <div className="flex flex-col gap-4 pb-24">
              <Intro>{t("blooks_intro")}</Intro>
              <SearchBox value={query} onChange={setQuery} placeholder={t("blooks_search")} />
              {grouped.map(([section, list]) => (
                <Section key={section} title={t(`texts_section_${section as TextSection}`)}>
                  <ul className="flex flex-col divide-y divide-[var(--line)]">
                    {list.map((item) => {
                      const look = looks[item.key] ?? {};
                      const label = (locale === "en" ? item.en : item.fa) || item.key;
                      return (
                        <li key={item.key} className="flex flex-col gap-2.5 py-3">
                          <div className="flex items-center justify-between gap-3">
                            <span className={`inline-flex min-h-9 max-w-[70%] items-center gap-1.5 truncate rounded-[10px] px-3 text-[0.88rem] font-semibold ${SWATCH[look.style ?? ""]}`}>
                              {look.emoji_id && <span aria-hidden>✦</span>}
                              <span className="truncate">{label}</span>
                            </span>
                            <code dir="ltr" className="truncate text-[0.7rem] text-[var(--hint)]">{item.key}</code>
                          </div>
                          <div className="flex flex-wrap items-center gap-2">
                            <div role="radiogroup" aria-label={t("blooks_color")} className="flex gap-1.5">
                              {TONES.map((tone) => (
                                <button
                                  key={tone || "default"}
                                  type="button"
                                  role="radio"
                                  aria-checked={(look.style ?? "") === tone}
                                  aria-label={t(`blooks_tone_${tone || "default"}`)}
                                  disabled={!writable}
                                  onClick={() => set(item.key, { style: tone })}
                                  className={`size-8 rounded-full ring-2 ring-offset-2 ring-offset-[var(--surface)] transition ${SWATCH[tone]} ${(look.style ?? "") === tone ? "ring-[var(--text)]" : "ring-transparent"}`}
                                />
                              ))}
                            </div>
                            <Input
                              aria-label={t("blooks_emoji")}
                              placeholder={t("blooks_emoji")}
                              dir="ltr"
                              inputMode="numeric"
                              disabled={!writable}
                              className="min-w-0 flex-1 sm:max-w-64"
                              value={look.emoji_id ?? ""}
                              onChange={(e) => set(item.key, { emoji_id: e.target.value.replace(/\D/g, "").slice(0, 25) })}
                            />
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                </Section>
              ))}
              {writable && dirty && (
                <div className="fixed inset-x-0 bottom-0 z-30 border-t border-[var(--line)] bg-[var(--surface)]/95 px-4 py-3 backdrop-blur md:static md:border-0 md:bg-transparent md:px-0">
                  <Button tone="primary" busy={busy} className="w-full md:w-auto" onClick={() => void save()}>
                    {t("save")}
                  </Button>
                </div>
              )}
            </div>
          );
        }}
      </Async>
    </Page>
  );
}
