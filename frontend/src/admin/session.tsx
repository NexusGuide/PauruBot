import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { direction, makeT, type Translate } from "@shared/i18n";
import { ApiError, call, onUnauthorized, setCsrf, type Me } from "./api";
import en from "./locales/en.json";
import fa from "./locales/fa.json";

const DICTS: Record<string, Record<string, string>> = { fa, en };
const LOCALE_KEY = "pauru.admin.locale";

export type Tone = "ok" | "error";

interface Session {
  me: Me | null;
  checking: boolean;
  setupRequired: boolean;
  locale: string;
  t: Translate;
  can: (permission: string) => boolean;
  login: (username: string, password: string) => Promise<void>;
  loginWithCode: (username: string, code: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  setLocale: (locale: string) => void;
  toast: (text: string, tone?: Tone) => void;
  toasts: { id: number; text: string; tone: Tone }[];
  /** Translate an API error code, falling back to a generic message. */
  errorText: (err: unknown) => string;
}

const Ctx = createContext<Session | null>(null);

function storedLocale(): string {
  try {
    const value = localStorage.getItem(LOCALE_KEY);
    if (value && DICTS[value]) return value;
  } catch {
    // storage blocked
  }
  return "fa";
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [checking, setChecking] = useState(true);
  const [setupRequired, setSetupRequired] = useState(false);
  const [locale, setLocaleState] = useState(storedLocale);
  const [toasts, setToasts] = useState<{ id: number; text: string; tone: Tone }[]>([]);
  const nextToast = useRef(1);

  const t = useMemo(() => makeT(DICTS[locale] ?? fa, fa), [locale]);

  useEffect(() => {
    document.documentElement.lang = locale;
    document.documentElement.dir = direction(locale);
    document.title = t("app_title");
  }, [locale, t]);

  const refresh = useCallback(async () => {
    try {
      const status = await call<{ setup_required: boolean }>("/api/setup/status");
      setSetupRequired(status.setup_required);
      if (status.setup_required) {
        setMe(null);
        return;
      }
      const data = await call<Me>("/api/auth/me");
      setCsrf(data.csrf_token);
      setMe(data);
    } catch {
      setMe(null);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return onUnauthorized(() => setMe(null));
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    const data = await call<Me>("/api/auth/login", { method: "POST", body: { username, password } });
    setCsrf(data.csrf_token);
    setMe(data);
  }, []);

  const loginWithCode = useCallback(async (username: string, code: string) => {
    const data = await call<Me>("/api/auth/code/verify", { method: "POST", body: { username, code } });
    setCsrf(data.csrf_token);
    setMe(data);
  }, []);

  const logout = useCallback(async () => {
    try {
      await call("/api/auth/logout", { method: "POST" });
    } finally {
      setCsrf("");
      setMe(null);
    }
  }, []);

  const setLocale = useCallback((value: string) => {
    if (!DICTS[value]) return;
    setLocaleState(value);
    try {
      localStorage.setItem(LOCALE_KEY, value);
    } catch {
      // storage blocked
    }
  }, []);

  const toast = useCallback((text: string, tone: Tone = "ok") => {
    const id = nextToast.current++;
    setToasts((list) => [...list.slice(-2), { id, text, tone }]);
    window.setTimeout(() => setToasts((list) => list.filter((item) => item.id !== id)), 3500);
  }, []);

  const errorText = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError) {
        if (err.reason) {
          const reasonKey = `err_${err.reason}`;
          const reasonText = t(reasonKey);
          if (reasonText !== reasonKey) return reasonText;
        }
        const key = `err_${err.code}`;
        const text = t(key);
        const base = text === key ? t("err_generic", { code: err.code }) : text;
        if (err.code === "validation_error" && err.fields && err.fields.length > 0) {
          return `${base} (${err.fields.join(", ")})`;
        }
        return base;
      }
      return t("err_network");
    },
    [t],
  );

  const permissions = useMemo(() => new Set(me?.permissions ?? []), [me]);
  const can = useCallback((permission: string) => permissions.has(permission), [permissions]);

  const value = useMemo<Session>(
    () => ({
      me,
      checking,
      setupRequired,
      locale,
      t,
      can,
      login,
      loginWithCode,
      logout,
      refresh,
      setLocale,
      toast,
      toasts,
      errorText,
    }),
    [me, checking, setupRequired, locale, t, can, login, loginWithCode, logout, refresh, setLocale, toast, toasts, errorText],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): Session {
  const value = useContext(Ctx);
  if (!value) throw new Error("useSession outside SessionProvider");
  return value;
}
