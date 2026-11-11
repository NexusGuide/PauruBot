import { useEffect, useMemo, useState, type DragEvent } from "react";
import { call, type SettingItem } from "../api";
import { useAction, useLoad } from "../hooks";
import { useSession } from "../session";
import { Async, Button, Empty, Field, Input, Page, Section, Select, Textarea } from "../ui";
import { GuidesEditor, type Guide } from "./ClockTools2";

const BUTTONS = [
  "miniapp",
  "buy",
  "services",
  "wallet",
  "trial",
  "referral",
  "reseller",
  "tickets",
  "guide",
  "apps",
  "language",
  "wheel",
  "lottery",
  "accounts",
];
const DEFAULT_LAYOUT = [["miniapp"], ["buy", "services"], ["wallet", "trial"], ["referral", "reseller", "accounts"], ["tickets", "guide"], ["wheel", "lottery"], ["apps", "language"]];
const LABEL_KEY: Record<string, string> = Object.fromEntries(BUTTONS.map((b) => [b, `menu_btn_${b}`]));
const STYLES = ["", "primary", "success", "danger"];
const PLATFORMS = ["android", "ios", "windows", "mac", "linux"];
const MAX_PER_ROW = 4;

type Layout = string[][];
type Styles = Record<string, { style?: string; emoji_id?: string }>;
interface HelpItem {
  title: string;
  text: string;
  url: string;
}
interface AppItem {
  name: string;
  platform: string;
  repo: string;
  pattern: string;
  store_url: string;
}

const STYLE_CLASS: Record<string, string> = {
  "": "bg-[var(--surface)] ring-1 ring-[var(--line)]",
  primary: "bg-firouzeh-600 text-white",
  success: "bg-pista-700 text-white",
  danger: "bg-anar-500 text-white",
};

function move(layout: Layout, id: string, row: number, index: number): Layout {
  const next = layout.map((r) => r.filter((x) => x !== id));
  while (next.length <= row) next.push([]);
  const target = next[row] ?? [];
  if (target.length >= MAX_PER_ROW) return layout;
  target.splice(Math.min(index, target.length), 0, id);
  next[row] = target;
  return next.filter((r, i) => r.length > 0 || i === row);
}

function LayoutEditor({
  layout,
  styles,
  onLayout,
  onStyles,
}: {
  layout: Layout;
  styles: Styles;
  onLayout: (layout: Layout) => void;
  onStyles: (styles: Styles) => void;
}) {
  const { t } = useSession();
  const [selected, setSelected] = useState<string | null>(null);
  const used = new Set(layout.flat());
  const hidden = BUTTONS.filter((b) => !used.has(b));
  const where = (id: string): [number, number] => {
    for (let r = 0; r < layout.length; r += 1) {
      const c = (layout[r] ?? []).indexOf(id);
      if (c >= 0) return [r, c];
    }
    return [-1, -1];
  };
  const drop = (event: DragEvent, row: number, index: number) => {
    event.preventDefault();
    const id = event.dataTransfer.getData("text/plain");
    if (BUTTONS.includes(id)) onLayout(move(layout, id, row, index));
  };
  const nudge = (dr: number, dc: number) => {
    if (!selected) return;
    const [r, c] = where(selected);
    if (r < 0) return;
    if (dr !== 0) onLayout(move(layout, selected, Math.max(0, r + dr), dr > 0 && r + dr >= layout.length ? 0 : c));
    else onLayout(move(layout, selected, r, Math.max(0, c + dc)));
  };
  const hide = () => {
    if (!selected || selected === "buy" || selected === "services") return;
    onLayout(layout.map((r) => r.filter((x) => x !== selected)).filter((r) => r.length > 0));
    setSelected(null);
  };
  const style = selected ? (styles[selected] ?? {}) : {};
  const setStyle = (patch: { style?: string; emoji_id?: string }) => {
    if (!selected) return;
    const merged = { ...style, ...patch };
    const clean = Object.fromEntries(Object.entries(merged).filter(([, v]) => v));
    onStyles({ ...styles, [selected]: clean });
  };

  return (
    <div className="flex flex-col gap-4">
      <p className="text-[0.82rem] text-[var(--hint)]">{t("menu_editor_hint")}</p>
      <div className="mx-auto flex w-full max-w-sm flex-col gap-1.5 rounded-2xl bg-[var(--bg)] p-2.5" aria-label={t("menu_preview")}>
        {layout.map((row, r) => (
          <div
            key={r}
            className="flex min-h-11 gap-1.5"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => drop(e, r, row.length)}
          >
            {row.map((id, c) => (
              <button
                key={id}
                type="button"
                draggable
                onDragStart={(e) => e.dataTransfer.setData("text/plain", id)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  e.stopPropagation();
                  drop(e, r, c);
                }}
                onClick={() => setSelected(selected === id ? null : id)}
                aria-pressed={selected === id}
                className={`min-w-0 flex-1 truncate rounded-xl px-2 py-2.5 text-[0.82rem] font-semibold ${STYLE_CLASS[styles[id]?.style ?? ""]} ${
                  selected === id ? "outline outline-2 outline-offset-2 outline-saffron-500" : ""
                }`}
              >
                {t(LABEL_KEY[id] ?? id)}
              </button>
            ))}
          </div>
        ))}
        <div
          className="flex min-h-10 items-center justify-center rounded-xl border-2 border-dashed border-[var(--line)] text-[0.78rem] text-[var(--hint)]"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => drop(e, layout.length, 0)}
        >
          {t("menu_new_row")}
        </div>
        <div className="rounded-xl bg-[var(--surface)] px-2 py-2.5 text-center text-[0.82rem] font-semibold ring-1 ring-[var(--line)]">
          {t("menu_btn_admin")}
        </div>
      </div>

      {selected && (
        <div className="flex flex-col gap-3 rounded-xl bg-[var(--bg)] p-3">
          <p className="text-[0.88rem] font-bold">{t(LABEL_KEY[selected] ?? selected)}</p>
          <div className="flex flex-wrap gap-2">
            <Button small onClick={() => nudge(-1, 0)}>{t("move_up")}</Button>
            <Button small onClick={() => nudge(1, 0)}>{t("move_down")}</Button>
            <Button small onClick={() => nudge(0, -1)}>{t("move_start")}</Button>
            <Button small onClick={() => nudge(0, 1)}>{t("move_end")}</Button>
            {selected !== "buy" && selected !== "services" && (
              <Button small tone="danger" onClick={hide}>{t("hide_button")}</Button>
            )}
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("button_colour")} hint={t("button_colour_hint")}>
              {(id) => (
                <Select id={id} value={style.style ?? ""} onChange={(e) => setStyle({ style: e.target.value })}>
                  {STYLES.map((s) => (
                    <option key={s} value={s}>
                      {t(`button_style_${s || "default"}`)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label={t("button_emoji_id")} hint={t("button_emoji_id_hint")}>
              {(id) => (
                <Input id={id} dir="ltr" inputMode="numeric" maxLength={25} value={style.emoji_id ?? ""}
                  onChange={(e) => setStyle({ emoji_id: e.target.value.replace(/\D/g, "") })} />
              )}
            </Field>
          </div>
        </div>
      )}

      {hidden.length > 0 && (
        <div className="flex flex-col gap-2">
          <p className="text-[0.82rem] font-semibold">{t("hidden_buttons")}</p>
          <div className="flex flex-wrap gap-2">
            {hidden.map((id) => (
              <button
                key={id}
                type="button"
                draggable
                onDragStart={(e) => e.dataTransfer.setData("text/plain", id)}
                onClick={() => onLayout(move(layout, id, layout.length, 0))}
                className="rounded-full px-3 py-1.5 text-[0.82rem] border border-dashed border-[var(--line)] hover:bg-[var(--surface)]"
              >
                + {t(LABEL_KEY[id] ?? id)}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function HelpEditor({ items, onChange }: { items: HelpItem[]; onChange: (items: HelpItem[]) => void }) {
  const { t } = useSession();
  const update = (i: number, patch: Partial<HelpItem>) => onChange(items.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  return (
    <div className="flex flex-col gap-3">
      {items.length === 0 && <Empty title={t("help_empty")} hint={t("help_empty_hint")} />}
      {items.map((item, i) => (
        <div key={i} className="flex flex-col gap-2 rounded-xl bg-[var(--bg)] p-3">
          <div className="flex gap-2">
            <Input aria-label={t("help_title")} placeholder={t("help_title")} maxLength={48} value={item.title} onChange={(e) => update(i, { title: e.target.value })} />
            <Button small tone="ghost" icon="close" aria-label={t("remove")} onClick={() => onChange(items.filter((_, j) => j !== i))} />
          </div>
          <Textarea aria-label={t("help_text")} placeholder={t("help_text")} maxLength={3000} value={item.text} onChange={(e) => update(i, { text: e.target.value })} />
          <Input aria-label={t("help_url")} placeholder="https://" dir="ltr" maxLength={300} value={item.url} onChange={(e) => update(i, { url: e.target.value })} />
        </div>
      ))}
      <Button small icon="plus" className="self-start" disabled={items.length >= 20} onClick={() => onChange([...items, { title: "", text: "", url: "" }])}>
        {t("add_help_item")}
      </Button>
    </div>
  );
}

function AppsEditor({ items, onChange }: { items: AppItem[]; onChange: (items: AppItem[]) => void }) {
  const { t } = useSession();
  const update = (i: number, patch: Partial<AppItem>) => onChange(items.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  return (
    <div className="flex flex-col gap-3">
      {items.map((app, i) => (
        <div key={i} className="grid gap-2 rounded-xl bg-[var(--bg)] p-3 sm:grid-cols-2">
          <Input aria-label={t("app_name")} placeholder={t("app_name")} maxLength={32} value={app.name} onChange={(e) => update(i, { name: e.target.value })} />
          <Select aria-label={t("app_platform")} value={app.platform} onChange={(e) => update(i, { platform: e.target.value })}>
            {PLATFORMS.map((p) => (
              <option key={p} value={p}>
                {t(`platform_${p}`)}
              </option>
            ))}
          </Select>
          <Input aria-label={t("app_repo")} placeholder="owner/repo" dir="ltr" value={app.repo} onChange={(e) => update(i, { repo: e.target.value.trim() })} />
          <Input aria-label={t("app_pattern")} placeholder="*universal.apk" dir="ltr" value={app.pattern} onChange={(e) => update(i, { pattern: e.target.value.trim() })} />
          <Input aria-label={t("app_store_url")} placeholder="https://" dir="ltr" className="sm:col-span-2" value={app.store_url} onChange={(e) => update(i, { store_url: e.target.value.trim() })} />
          <Button small tone="ghost" icon="close" className="justify-self-start" onClick={() => onChange(items.filter((_, j) => j !== i))}>
            {t("remove")}
          </Button>
        </div>
      ))}
      <Button small icon="plus" className="self-start" disabled={items.length >= 20}
        onClick={() => onChange([...items, { name: "", platform: "android", repo: "", pattern: "", store_url: "" }])}>
        {t("add_app")}
      </Button>
    </div>
  );
}

export function BotMenu() {
  const { t, can, toast, errorText } = useSession();
  const { data, error, reload } = useLoad<{ groups: Record<string, SettingItem[]> }>("/api/admin/settings");
  const values = useMemo(() => Object.fromEntries((data?.groups.appearance ?? []).map((i) => [i.key, i.value])), [data]);
  const [layout, setLayout] = useState<Layout>(DEFAULT_LAYOUT);
  const [styles, setStyles] = useState<Styles>({});
  const [help, setHelp] = useState<HelpItem[]>([]);
  const [apps, setApps] = useState<AppItem[]>([]);
  const [guides, setGuides] = useState<Guide[]>([]);
  const [cacheChat, setCacheChat] = useState<number | null>(null);
  const [menuMode, setMenuMode] = useState("reply");
  const [busy, run] = useAction();
  const writable = can("settings.write");

  useEffect(() => {
    if (!data) return;
    const saved = values.menu_layout as Layout | undefined;
    setLayout(saved && saved.length ? saved : DEFAULT_LAYOUT);
    setStyles((values.button_styles as Styles) ?? {});
    setHelp(((values.help_items as HelpItem[]) ?? []).map((h) => ({ title: h.title, text: h.text ?? "", url: h.url ?? "" })));
    setApps(((values.download_apps as AppItem[]) ?? []).map((a) => ({ ...a, repo: a.repo ?? "", pattern: a.pattern ?? "", store_url: a.store_url ?? "" })));
    setCacheChat((values.app_cache_chat_id as number | null) ?? null);
    setGuides((values.connection_guides as Guide[]) ?? []);
    setMenuMode((values.menu_mode as string) || "reply");
  }, [data, values]);

  const save = async () => {
    try {
      await run(() =>
        call("/api/admin/settings", {
          method: "PUT",
          body: {
            values: {
              menu_layout: layout,
              button_styles: styles,
              help_items: help.filter((h) => h.title.trim()),
              download_apps: apps.filter((a) => a.name.trim()),
              connection_guides: guides
                .filter((g) => g.title.trim() && g.text.trim())
                .map((g) => ({ ...g, media_url: g.media_url || undefined, link: g.link || undefined })),
              app_cache_chat_id: cacheChat,
              menu_mode: menuMode,
            },
          },
        }),
      );
      toast(t("saved"));
      void reload();
    } catch (err) {
      toast(errorText(err), "error");
    }
  };

  return (
    <Page
      title={t("nav_bot_menu")}
      actions={
        writable && (
          <Button tone="primary" busy={busy} onClick={() => void save()}>
            {t("save")}
          </Button>
        )
      }
    >
      <Async data={data} error={error} reload={reload}>
        {() => (
          <>
            <Section
              title={t("menu_layout")}
              actions={
                <Button small tone="ghost" onClick={() => { setLayout(DEFAULT_LAYOUT); setStyles({}); }}>
                  {t("menu_reset")}
                </Button>
              }
            >
              <div className="mb-4 max-w-sm">
                <Field label={t("menu_mode")} hint={t("menu_mode_hint")}>
                  {(id) => (
                    <Select id={id} value={menuMode} onChange={(e) => setMenuMode(e.target.value)}>
                      {["reply", "inline", "miniapp"].map((mode) => (
                        <option key={mode} value={mode}>
                          {t(`menu_mode_${mode}`)}
                        </option>
                      ))}
                    </Select>
                  )}
                </Field>
              </div>
              <LayoutEditor layout={layout} styles={styles} onLayout={setLayout} onStyles={setStyles} />
            </Section>
            <Section title={t("help_topics")}>
              <p className="mb-3 text-[0.82rem] text-[var(--hint)]">{t("help_topics_hint")}</p>
              <HelpEditor items={help} onChange={setHelp} />
            </Section>
            <Section title={t("connection_guides_title")}>
              <p className="mb-3 text-[0.82rem] text-[var(--hint)]">{t("connection_guides_hint")}</p>
              <GuidesEditor items={guides} onChange={setGuides} />
            </Section>
            <Section title={t("app_downloads")}>
              <p className="mb-3 text-[0.82rem] text-[var(--hint)]">{t("app_downloads_hint")}</p>
              <AppsEditor items={apps} onChange={setApps} />
              <div className="mt-4 max-w-sm">
                <Field label={t("app_cache_chat")} hint={t("app_cache_chat_hint")}>
                  {(id) => (
                    <Input
                      id={id}
                      dir="ltr"
                      inputMode="numeric"
                      placeholder="-1001234567890"
                      value={cacheChat === null ? "" : String(cacheChat)}
                      onChange={(e) => {
                        const text = e.target.value.replace(/[^\d-]/g, "");
                        setCacheChat(/^-?\d+$/.test(text) ? Number(text) : null);
                      }}
                    />
                  )}
                </Field>
              </div>
            </Section>
          </>
        )}
      </Async>
    </Page>
  );
}
