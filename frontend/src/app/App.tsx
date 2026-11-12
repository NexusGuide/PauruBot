import { useEffect, useState } from "react";
import { BrowserRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Agency, AgencyAccount } from "./screens/Agency";
import { Buy, Custom, PanelStep, PlanStep } from "./screens/Buy";
import { CardVerification } from "./screens/CardVerification";
import { Checkout, Done } from "./screens/Checkout";
import { Desk } from "./screens/Desk";
import { Help } from "./screens/Help";
import { Home } from "./screens/Home";
import { Gate } from "./screens/Gate";
import { Join } from "./screens/Join";
import { Manage } from "./screens/Manage";
import { OwnFeaturesScreen, OwnHome, OwnPayment, OwnPlanEdit, OwnPlans, OwnStatus, OwnChannels } from "./screens/OwnShop";
import { More } from "./screens/More";
import { Notifications } from "./screens/Notifications";
import { Receipts } from "./screens/Receipts";
import { Payment } from "./screens/Payment";
import { Profile } from "./screens/Profile";
import { Extra, ServiceDetail, Services } from "./screens/Services";
import { NewTicket, TicketThread, Tickets } from "./screens/Tickets";
import { Wallet } from "./screens/Wallet";
import { AppProvider, useApp } from "./state";
import { initData, sdkFailed, tg } from "./telegram";
import { parentForPath } from "./navigation";
import { Loading, Screen, TabBar, Toast } from "./ui";

const ROOTS = new Set(["/", "/buy", "/services", "/wallet", "/more"]);

function BackButton() {
  const location = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    if (!tg) return;
    const back = () => {
      if (typeof window.history.state?.idx === "number" && window.history.state.idx > 0) navigate(-1);
      else navigate(parentForPath(location.pathname) ?? "/");
    };
    if (ROOTS.has(location.pathname)) {
      tg.BackButton.hide();
    } else {
      tg.BackButton.show();
      tg.BackButton.onClick(back);
    }
    return () => tg?.BackButton.offClick(back);
  }, [location.pathname, navigate]);
  return null;
}

function RouteFocus() {
  const { pathname } = useLocation();
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
    const focus = () => {
      const content = document.getElementById("app-content");
      if (!content) return false;
      content.focus({ preventScroll: true });
      return true;
    };
    if (focus()) return;
    const observer = new MutationObserver(() => { if (focus()) observer.disconnect(); });
    observer.observe(document.getElementById("root") ?? document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [pathname]);
  return null;
}

/** Ink ripple on every .app-rip-host surface (buttons, rows, icon buttons). */
function Ripple() {
  useEffect(() => {
    const onDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const host = target.closest(".app-rip-host");
      if (!(host instanceof HTMLElement) || host.matches("button:disabled")) return;
      const rect = host.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height) * 1.2;
      const ripple = document.createElement("span");
      ripple.className = "app-rip";
      ripple.setAttribute("aria-hidden", "true");
      ripple.style.width = `${size}px`;
      ripple.style.height = `${size}px`;
      ripple.style.left = `${event.clientX - rect.left - size / 2}px`;
      ripple.style.top = `${event.clientY - rect.top - size / 2}px`;
      host.appendChild(ripple);
      window.setTimeout(() => ripple.remove(), 620);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, []);
  return null;
}

function Shell() {
  const { me, error, join, gate, t, reload } = useApp();
  const [retrying, setRetrying] = useState(false);
  const retry = async () => {
    setRetrying(true);
    try { await reload(); } finally { setRetrying(false); }
  };
  if (join && !me) {
    // The API holds customers at the join gate; the screen is its only job until they are in.
    return (
      <>
        <Join />
        <Toast />
      </>
    );
  }
  if (gate) {
    // Phone, then terms, as the bot asks: nothing else answers until they are done (SEC-F11).
    // With terms asked only before buying, checkout opens this screen while the app is in use.
    return (
      <>
        <Gate />
        <Toast />
      </>
    );
  }
  if (error && !me) {
    // No launch data and no Telegram script: telegram.org is out of reach, not a wrong link (REF-03).
    const unreachable = error === "invalid_init_data" && !initData() && sdkFailed();
    return (
      <Screen>
        <div className="flex min-h-[70dvh] flex-col items-center justify-center gap-2 text-center">
          <p className="text-xl font-bold">
            {t(unreachable ? "telegram_unreachable" : error === "invalid_init_data" ? "open_from_bot" : `err_${error}`)}
          </p>
          <p className="max-w-xs text-sm text-[var(--hint)]">{t(unreachable ? "telegram_unreachable_hint" : "open_from_bot_hint")}</p>
          {error !== "invalid_init_data" && (
            <button type="button" className="app-rip-host mt-3 rounded-xl px-5 py-3" disabled={retrying} onClick={() => { void retry(); }}>
              {retrying ? "…" : t("ui_retry")}
            </button>
          )}
        </div>
      </Screen>
    );
  }
  if (!me) return <Loading />;
  return (
    <>
      <a className="app-skip" href="#app-content">{t("ui_skip")}</a>
      <BackButton />
      <RouteFocus />
      <Ripple />
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/notifications" element={<Notifications />} />
        <Route path="/buy" element={<Buy />} />
        <Route path="/panel" element={<PanelStep />} />
        <Route path="/plan" element={<PlanStep />} />
        <Route path="/custom" element={<Custom />} />
        <Route path="/checkout" element={<Checkout />} />
        <Route path="/done/:id" element={<Done />} />
        <Route path="/payment/:id" element={<Payment />} />
        <Route path="/agency" element={<Agency />} />
        <Route path="/agency/:id" element={<AgencyAccount />} />
        <Route path="/receipts" element={<Receipts />} />
        <Route path="/services" element={<Services />} />
        <Route path="/services/:id" element={<ServiceDetail />} />
        <Route path="/extra/:id/:unit" element={<Extra />} />
        <Route path="/wallet" element={<Wallet />} />
        <Route path="/card-verification" element={<CardVerification />} />
        <Route path="/more" element={<More />} />
        <Route path="/help" element={<Help />} />
        <Route path="/tickets" element={<Tickets />} />
        <Route path="/tickets/new" element={<NewTicket />} />
        <Route path="/tickets/:id" element={<TicketThread />} />
        <Route path="/profile" element={<Profile />} />
        <Route path="/manage" element={<Manage />} />
        <Route path="/shop" element={<OwnHome />} />
        <Route path="/shop/plans" element={<OwnPlans />} />
        <Route path="/shop/plans/:id" element={<OwnPlanEdit />} />
        <Route path="/shop/payment" element={<OwnPayment />} />
        <Route path="/shop/features" element={<OwnFeaturesScreen />} />
        <Route path="/shop/status" element={<OwnStatus />} />
        <Route path="/shop/channels" element={<OwnChannels />} />
        <Route path="/desk" element={<Desk />} />
        <Route path="*" element={<Home />} />
      </Routes>
      <TabBar />
      <Toast />
    </>
  );
}

export function App() {
  return (
    <BrowserRouter basename="/app">
      <AppProvider>
        <Shell />
      </AppProvider>
    </BrowserRouter>
  );
}
