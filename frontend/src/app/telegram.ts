// Minimal typings for the parts of Telegram.WebApp the Mini App uses.
interface BottomButton {
  setText(text: string): void;
  show(): void;
  hide(): void;
  enable(): void;
  disable(): void;
  showProgress(leaveActive?: boolean): void;
  hideProgress(): void;
  onClick(cb: () => void): void;
  offClick(cb: () => void): void;
}

interface BackButton {
  show(): void;
  hide(): void;
  onClick(cb: () => void): void;
  offClick(cb: () => void): void;
}

interface WebApp {
  initData: string;
  initDataUnsafe: { user?: { language_code?: string } };
  colorScheme: "light" | "dark";
  ready(): void;
  expand(): void;
  openLink(url: string): void;
  openTelegramLink(url: string): void;
  openInvoice(url: string, cb?: (status: string) => void): void;
  /**
   * Bot API 6.9+: Telegram sends the user's own contact to the bot after they confirm, and hands
   * the app a signed copy in `result.response` (F-12) that the server can check by itself.
   */
  requestContact?(cb?: (sent: boolean, result?: { status?: string; response?: string }) => void): void;
  showAlert(message: string, cb?: () => void): void;
  close(): void;
  MainButton: BottomButton;
  BackButton: BackButton;
  HapticFeedback?: { notificationOccurred(type: "success" | "error" | "warning"): void; selectionChanged(): void };
  onEvent(event: string, cb: () => void): void;
}

declare global {
  interface Window {
    Telegram?: { WebApp: WebApp };
    /** Set by /app/launch.js before the app loads (REF-03). */
    __pauruLaunch?: { initData: string; sdk: Promise<boolean> };
  }
}

/** Telegram's SDK object; a live binding, filled in whenever the SDK script arrives. */
export let tg: WebApp | undefined = window.Telegram?.WebApp;
let sdkState: "pending" | "ready" | "failed" = tg ? "ready" : window.__pauruLaunch ? "pending" : "failed";

/** The signed launch data: the SDK's copy, or the one Telegram put in the address. */
export function initData(): string {
  return tg?.initData || window.__pauruLaunch?.initData || "";
}

/** True when Telegram's script could not be loaded (telegram.org unreachable). */
export function sdkFailed(): boolean {
  return sdkState === "failed" && !tg;
}

function connect(): void {
  tg = window.Telegram?.WebApp;
  sdkState = tg ? "ready" : "failed";
  if (!tg) return;
  tg.ready();
  tg.expand();
  tg.onEvent("themeChanged", () => {
    if (themePref() === "auto") applyTheme("auto");
  });
  applyTheme();
}

/** Wait for the SDK at most `ms`, then go on with or without it; a late SDK still connects. */
export async function whenTelegram(ms: number): Promise<boolean> {
  const launch = window.__pauruLaunch;
  if (tg || !launch) {
    if (tg) connect();
    return Boolean(tg);
  }
  const settled = launch.sdk.then(
    () => connect(),
    () => connect(),
  );
  await Promise.race([settled, new Promise((resolve) => window.setTimeout(resolve, ms))]);
  return Boolean(tg);
}

const THEME_KEY = "pauru.theme";

export type ThemePref = "light" | "auto" | "dark";

export function themePref(): ThemePref {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : "auto";
  } catch {
    return "auto";
  }
}

/** Paint the chosen theme. "auto" follows Telegram first, then the device. */
export function applyTheme(pref: ThemePref = themePref()): void {
  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)")?.matches ?? false;
  const scheme = pref !== "auto" ? pref : tg?.colorScheme ?? (prefersDark ? "dark" : "light");
  document.documentElement.dataset.theme = scheme;
}

export function setThemePref(pref: ThemePref): void {
  try {
    localStorage.setItem(THEME_KEY, pref);
  } catch {
    // storage may be unavailable inside some Telegram clients
  }
  applyTheme(pref);
}

/** How long the first screen waits for Telegram's script before starting without it. */
export const SDK_WAIT_MS = 2500;

export async function boot(): Promise<void> {
  applyTheme();
  await whenTelegram(SDK_WAIT_MS);
}

export function haptic(kind: "success" | "error" | "warning" | "select"): void {
  if (!tg?.HapticFeedback) return;
  if (kind === "select") tg.HapticFeedback.selectionChanged();
  else tg.HapticFeedback.notificationOccurred(kind);
}

/** A note the customer reads and closes before going on (e.g. the shop's help for a payment method). */
export function showNote(text: string): Promise<void> {
  return new Promise((resolve) => {
    if (tg) tg.showAlert(text.slice(0, 4000), () => resolve());
    else {
      window.alert(text);
      resolve();
    }
  });
}

export function openExternal(url: string): void {
  if (tg) tg.openLink(url);
  else window.open(url, "_blank", "noopener");
}

export function openInvoice(url: string): Promise<string> {
  return new Promise((resolve) => {
    if (!tg) {
      window.open(url, "_blank", "noopener");
      resolve("pending");
      return;
    }
    tg.openInvoice(url, resolve);
  });
}
