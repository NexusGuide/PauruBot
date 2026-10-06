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

export function boot(): void {
  if (!tg) return;
  tg.ready();
  tg.expand();
  document.documentElement.dataset.theme = tg.colorScheme;
  tg.onEvent("themeChanged", () => {
    document.documentElement.dataset.theme = tg.colorScheme;
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
