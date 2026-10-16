import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { direction, makeT, type Translate } from "@shared/i18n";
import { api, type Me } from "./api";
import en from "./locales/en.json";
import fa from "./locales/fa.json";

const DICTS: Record<string, Record<string, string>> = { fa, en };

interface AppState {
  me: Me | null;
  error: string | null;
  locale: string;
  t: Translate;
  reload: () => Promise<void>;
  setLocale: (locale: string) => Promise<void>;
  setMarketing: (optOut: boolean) => Promise<void>;
  setAlerts: (muted: boolean) => Promise<void>;
  toast: (message: string, tone?: "ok" | "error") => void;
  toastMessage: { text: string; tone: "ok" | "error" } | null;
}

const Ctx = createContext<AppState | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toastMessage, setToast] = useState<{ text: string; tone: "ok" | "error" } | null>(null);

  const revision = useRef(0);
  const reloadId = useRef(0);

  const reload = useCallback(async () => {
    const initialRevision = revision.current;
    const requestId = ++reloadId.current;
    try {
      const profile = await api.me();
      if (revision.current !== initialRevision || reloadId.current !== requestId) return;
      setMe(profile);
      setError(null);
    } catch (err) {
      if (revision.current === initialRevision && reloadId.current === requestId) {
        setError(err instanceof Error ? err.message : "error");
      }
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const locale = me?.locale && DICTS[me.locale] ? me.locale : "fa";
  const t = useMemo(() => makeT(DICTS[locale] ?? fa, fa), [locale]);

  useEffect(() => {
    document.documentElement.lang = locale;
    document.documentElement.dir = direction(locale);
  }, [locale]);

  const setLocale = useCallback(async (next: string) => {
    const confirmed = await api.setLocale(next);
    revision.current += 1;
    setMe(current => current ? { ...current, locale: confirmed.locale } : current);
  }, []);

  const setMarketing = useCallback(async (optOut: boolean) => {
    const preferences = await api.setMarketing(optOut);
    revision.current += 1;
    setMe(current => current ? { ...current, preferences } : current);
  }, []);

  const setAlerts = useCallback(async (muted: boolean) => {
    const confirmed = await api.setAlerts(muted);
    revision.current += 1;
    setMe(current => current ? { ...current, alerts_muted: confirmed.alerts_muted } : current);
  }, []);

  const toast = useCallback((text: string, tone: "ok" | "error" = "ok") => {
    setToast({ text, tone });
    window.setTimeout(() => setToast(null), 2600);
  }, []);

  const value = useMemo(
    () => ({ me, error, locale, t, reload, setLocale, setMarketing, setAlerts, toast, toastMessage }),
    [me, error, locale, t, reload, setLocale, setMarketing, setAlerts, toast, toastMessage],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppState {
  const value = useContext(Ctx);
  if (!value) throw new Error("useApp outside provider");
  return value;
}

export function errorText(t: Translate, err: unknown): string {
  const code = err instanceof Error ? err.message : "error";
  const text = t(`err_${code}`);
  return text === `err_${code}` ? t("err_generic") : text;
}
