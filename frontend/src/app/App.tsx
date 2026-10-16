import { useEffect } from "react";
import { BrowserRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Buy, Custom } from "./screens/Buy";
import { Checkout, Done } from "./screens/Checkout";
import { Desk } from "./screens/Desk";
import { Help } from "./screens/Help";
import { Home } from "./screens/Home";
import { Manage } from "./screens/Manage";
import { More } from "./screens/More";
import { Receipts } from "./screens/Receipts";
import { Payment } from "./screens/Payment";
import { Profile } from "./screens/Profile";
import { Extra, ServiceDetail, Services } from "./screens/Services";
import { NewTicket, TicketThread, Tickets } from "./screens/Tickets";
import { Wallet } from "./screens/Wallet";
import { AppProvider, useApp } from "./state";
import { tg } from "./telegram";
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

function Shell() {
  const { me, error, t } = useApp();
  if (error && !me) {
    return (
      <Screen>
        <div className="flex min-h-[70dvh] flex-col items-center justify-center gap-2 text-center">
          <p className="text-xl font-bold">{t(error === "invalid_init_data" ? "open_from_bot" : `err_${error}`)}</p>
          <p className="max-w-xs text-sm text-[var(--hint)]">{t("open_from_bot_hint")}</p>
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
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/buy" element={<Buy />} />
        <Route path="/custom" element={<Custom />} />
        <Route path="/checkout" element={<Checkout />} />
        <Route path="/done/:id" element={<Done />} />
        <Route path="/payment/:id" element={<Payment />} />
        <Route path="/receipts" element={<Receipts />} />
        <Route path="/services" element={<Services />} />
        <Route path="/services/:id" element={<ServiceDetail />} />
        <Route path="/extra/:id/:unit" element={<Extra />} />
        <Route path="/wallet" element={<Wallet />} />
        <Route path="/more" element={<More />} />
        <Route path="/help" element={<Help />} />
        <Route path="/tickets" element={<Tickets />} />
        <Route path="/tickets/new" element={<NewTicket />} />
        <Route path="/tickets/:id" element={<TicketThread />} />
        <Route path="/profile" element={<Profile />} />
        <Route path="/manage" element={<Manage />} />
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
