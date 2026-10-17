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
  showAlert(message: string): void;
  close(): void;
  MainButton: BottomButton;
  BackButton: BackButton;
  HapticFeedback?: { notificationOccurred(type: "success" | "error" | "warning"): void; selectionChanged(): void };
  onEvent(event: string, cb: () => void): void;
}

declare global {
  interface Window {
    Telegram?: { WebApp: WebApp };
  }
}

export const tg: WebApp | undefined = window.Telegram?.WebApp;

export function initData(): string {
  return tg?.initData ?? "";
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

export function boot(): void {
  applyTheme();
  if (!tg) return;
  tg.ready();
  tg.expand();
  tg.onEvent("themeChanged", () => {
    if (themePref() === "auto") applyTheme("auto");
  });
}

export function haptic(kind: "success" | "error" | "warning" | "select"): void {
  if (!tg?.HapticFeedback) return;
  if (kind === "select") tg.HapticFeedback.selectionChanged();
  else tg.HapticFeedback.notificationOccurred(kind);
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
