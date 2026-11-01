import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { direction, makeT, type Translate } from "@shared/i18n";
import { api, type GateInfo, type JoinChannel, type Me } from "./api";
import en from "./locales/en.json";
import fa from "./locales/fa.json";

const DICTS: Record<string, Record<string, string>> = { fa, en };

interface AppState {
  me: Me | null;
  error: string | null;
  /** Set while the API holds the customer at the join gate; null once they are in (GRW-07). */
  join: { channels: JoinChannel[] } | null;
  /** Set while the API holds the customer at the phone/terms gate (SEC-F11). */
  gate: GateInfo | null;
  locale: string;
  t: Translate;
  reload: () => Promise<void>;
  checkJoin: () => Promise<boolean>;
  /** Ask again what the gate still needs; true once it lets the customer in. */
  checkGate: (next?: GateInfo) => Promise<boolean>;
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
  const [join, setJoin] = useState<{ channels: JoinChannel[] } | null>(null);
  const [gate, setGate] = useState<GateInfo | null>(null);
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
      setJoin(null);
      setGate(null);
    } catch (err) {
      if (revision.current === initialRevision && reloadId.current === requestId) {
        const code = err instanceof Error ? err.message : "error";
        setError(code);
        if (code === "phone_required" || code === "terms_required") {
          // /gate is the one route the phone/terms gate leaves open.
          try {
            setGate(await api.gate());
          } catch {
            setGate({ requirement: code, bot_username: null });
          }
          return;
        }
        if (code !== "join_required") return;
        // The join screen still needs the channel list, and /join is the one ungated route.
        try {
          setJoin({ channels: (await api.join()).channels });
        } catch {
          setJoin({ channels: [] });
        }
      }
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const checkJoin = useCallback(async () => {
    const answer = await api.checkJoin();
    if (answer.ok) {
      setJoin(null);
      await reload();
      return true;
    }
    setJoin({ channels: answer.channels });
    return false;
  }, [reload]);

  const checkGate = useCallback(async (next?: GateInfo) => {
    const answer = next ?? (await api.gate());
    if (answer.requirement === null) {
      setGate(null);
      await reload();
      return true;
    }
    setGate(answer);
    return false;
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

  const toastTimer = useRef<number | undefined>(undefined);
  // Each toast gets its own full 2.6s: a new toast extends the timer instead of
  // being cut short by the previous toast's one.
  const toast = useCallback((text: string, tone: "ok" | "error" = "ok") => {
    if (toastTimer.current !== undefined) window.clearTimeout(toastTimer.current);
    setToast({ text, tone });
    toastTimer.current = window.setTimeout(() => {
      toastTimer.current = undefined;
      setToast(null);
    }, 2600);
  }, []);

  const value = useMemo(
    () => ({ me, error, join, gate, locale, t, reload, checkJoin, checkGate, setLocale, setMarketing, setAlerts, toast, toastMessage }),
    [me, error, join, gate, locale, t, reload, checkJoin, checkGate, setLocale, setMarketing, setAlerts, toast, toastMessage],
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
