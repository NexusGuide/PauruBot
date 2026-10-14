import { useEffect } from "react";
import { BrowserRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Buy, Custom } from "./screens/Buy";
import { Checkout, Done } from "./screens/Checkout";
import { Desk } from "./screens/Desk";
import { Home } from "./screens/Home";
import { Manage } from "./screens/Manage";
import { More } from "./screens/More";
import { Receipts } from "./screens/Receipts";
import { Payment } from "./screens/Payment";
import { Extra, ServiceDetail, Services } from "./screens/Services";
import { Wallet } from "./screens/Wallet";
import { AppProvider, useApp } from "./state";
import { tg } from "./telegram";
import { Loading, Screen, TabBar, Toast } from "./ui";

const ROOTS = new Set(["/", "/buy", "/services", "/wallet", "/more"]);

function BackButton() {
  const location = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    if (!tg) return;
    const back = () => navigate(-1);
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
      <BackButton />
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
