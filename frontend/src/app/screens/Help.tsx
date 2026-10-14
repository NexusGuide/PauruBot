import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type CustomerHelp } from "../api";
import { matchesHelp, platformApps, safeHelpUrl, validateHelp } from "../help/help-data";
import { errorText, useApp } from "../state";
import { Panel, Screen } from "../ui";

const control = "min-h-12 w-full rounded-2xl bg-[var(--surface)] p-3 text-start font-semibold disabled:opacity-50";

function HelpLink({ url, label }: { url: string | null; label: string }) {
  const target = safeHelpUrl(url);
  if (!target) return null;
  return <a href={target} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer"
    className="my-2 block min-h-12 break-words rounded-xl border border-[var(--line)] p-3 font-semibold">
    {label}<span aria-hidden="true"> ↗</span><span className="mt-1 block text-xs font-normal" dir="ltr">{new URL(target).hostname}</span>
  </a>;
}

export function Help() {
  const { t } = useApp();
  const navigate = useNavigate();
  const [data, setData] = useState<CustomerHelp | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [platform, setPlatform] = useState("all");
  const sequence = useRef(0);
  useEffect(() => {
    const controller = new AbortController(), id = ++sequence.current;
    setLoading(true); setError(null); setData(null);
    api.help(controller.signal).then(validateHelp).then(value => {
      if (!controller.signal.aborted && sequence.current === id) setData(value);
    }).catch(err => {
      if (!controller.signal.aborted && sequence.current === id) setError(err);
    }).finally(() => {
      if (!controller.signal.aborted && sequence.current === id) setLoading(false);
    });
    return () => { controller.abort(); sequence.current += 1; };
  }, [attempt]);
  const articles = data?.articles.filter(item => matchesHelp(`${item.title} ${item.text}`, query)) ?? [];
  const apps = platformApps(data?.apps ?? [], platform).filter(app => matchesHelp(app.name, query));
  return <Screen title={t("help_title")}>
    <p className="mb-4 text-sm text-[var(--hint)]">{t("help_intro")}</p>
    <section aria-label={t("help_connect")} className="mb-4">
      <Panel><h2 className="mb-3 text-lg font-bold">{t("help_connect")}</h2>
        <ol className="list-inside list-decimal space-y-2">{["help_step_install", "help_step_service", "help_step_import", "help_step_connect"].map(key => <li key={key}>{t(key)}</li>)}</ol>
        <p className="my-3 text-sm text-[var(--hint)]">{t("help_compatibility")}</p>
        <button type="button" className={control} onClick={() => navigate("/services")}>{t("help_services")}</button>
        <p className="mt-3 text-sm text-[var(--hint)]">{t("help_wireguard")}</p>
        <p className="mt-2 text-sm text-[var(--hint)]">{t("help_account")}</p>
      </Panel>
    </section>
    <label className="mb-4 block text-sm font-semibold">{t("help_search")}
      <input type="search" aria-label={t("help_search")} value={query} maxLength={120} onChange={event => setQuery(event.target.value)}
        className="mt-2 w-full rounded-xl border border-[var(--line)] bg-[var(--bg)] p-3" />
    </label>
    <button type="button" className={control} disabled={loading} onClick={() => setAttempt(value => value + 1)}>{t("help_refresh")}</button>
    {loading && <p role="status" className="my-3">{t("help_loading")}</p>}
    {Boolean(error) && <div role="alert" className="my-3"><p>{errorText(t, error)}</p>
      <button type="button" className={control} onClick={() => setAttempt(value => value + 1)}>{t("help_retry")}</button></div>}
    {data && <>
      <section aria-label={t("help_articles")} className="my-5">
        <h2 className="mb-3 text-lg font-bold">{t("help_articles")}</h2>
        {articles.length === 0 && <p role="status">{t(data.articles.length ? "help_no_results" : "help_no_articles")}</p>}
        {articles.map(article => <Panel key={`${attempt}:${article.id}`} className="mb-3">
          <details><summary className="min-h-12 cursor-pointer break-words py-3 font-bold">{article.title}</summary>
            <p className="my-3 whitespace-pre-wrap break-words" dir="auto">{article.text}</p>
            <HelpLink url={article.url} label={t("help_open_article")} />
          </details>
        </Panel>)}
      </section>
      <section aria-label={t("help_apps")} className="my-5">
        <h2 className="mb-3 text-lg font-bold">{t("help_apps")}</h2>
        <label className="block text-sm font-semibold">{t("help_platform")}
          <select aria-label={t("help_platform")} value={platform} onChange={event => setPlatform(event.target.value)} className="my-3 w-full rounded-xl border border-[var(--line)] bg-[var(--bg)] p-3">
            {["all", "android", "ios", "windows", "mac", "linux"].map(code => <option key={code} value={code}>{t(`help_platform_${code}`)}</option>)}
          </select>
        </label>
        <p className="mb-3 text-sm text-[var(--hint)]">{t("help_apps_notice")}</p>
        {apps.length === 0 && <p role="status">{t(data.apps.length ? "help_no_results" : "help_no_apps")}</p>}
        {apps.map(app => <Panel key={app.id} className="mb-3">
          <h3 className="break-words font-bold">{app.name}</h3><p className="text-sm text-[var(--hint)]">{t(`help_platform_${app.platform}`)}</p>
          <HelpLink url={app.store_url} label={t("help_store")} />
          <HelpLink url={app.release_url} label={t("help_releases")} />
        </Panel>)}
      </section>
      <p className="my-3 text-sm text-[var(--hint)]">{t("help_bot_hint")}</p>
      <HelpLink url={data.bot_url} label={t("help_bot")} />
    </>}
    <Panel className="my-4"><h2 className="font-bold">{t("help_trouble")}</h2><p className="my-3 text-sm">{t("help_trouble_hint")}</p>
      <button type="button" className={control} onClick={() => navigate("/tickets")}>{t("help_tickets")}</button>
    </Panel>
    <button type="button" className={control} onClick={() => navigate("/more")}>{t("profile_back")}</button>
  </Screen>;
}
