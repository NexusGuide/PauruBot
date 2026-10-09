import { lazy, Suspense, useEffect, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Shell } from "./layout";
import { Login, Setup } from "./screens/Auth";
import { Catalog } from "./screens/Catalog";
import { Orders, Payments } from "./screens/Commerce";
import { CustomerPage, Customers } from "./screens/Customers";
import { Broadcasts, Discounts, Lotteries } from "./screens/Growth";
import { Home } from "./screens/Home";
import { Panels } from "./screens/Panels";
import { BotMenu } from "./screens/BotMenu";
import { Migration } from "./screens/Migration";
import { Updates } from "./screens/Updates";
import { AdminAccounts } from "./screens/AdminAccounts";
import { Desk } from "./screens/Desk";
import { MyBilling, Resellers, Staff, SubResellers, TicketPage, Tickets } from "./screens/People";
import { Services } from "./screens/Services";
import { Account, Audit, Backups, Channels, Health, Jobs, Reports, Settings, Texts } from "./screens/System";
import { SessionProvider, useSession } from "./session";
import { ConfirmProvider, Empty, Loading, Page, Section, Toasts } from "./ui";

const MyBot = lazy(() => import("./screens/MyBot").then((m) => ({ default: m.MyBot })));
const HelpIndex = lazy(() => import("./screens/Help").then((m) => ({ default: m.HelpIndex })));
const HelpArticle = lazy(() => import("./screens/Help").then((m) => ({ default: m.HelpArticle })));

function Guard({ permission, children }: { permission: string; children: ReactNode }) {
  const { can, t } = useSession();
  if (!can(permission)) {
    return (
      <Page title={t("no_access_title")}>
        <Section>
          <Empty title={t("no_access")} hint={t("no_access_hint")} />
        </Section>
      </Page>
    );
  }
  return <>{children}</>;
}

function SessionExpiry() {
  const { me, sessionExpired, t } = useSession();
  if (!sessionExpired || me) return null;
  return (
    <p role="alert" className="mx-auto mt-4 w-full max-w-sm rounded-xl bg-saffron-100 px-4 py-2.5 text-[0.85rem] font-semibold text-saffron-700">
      {t("err_session_revoked")}
    </p>
  );
}

const ATTEMPT_KEY = "pauru.admin.attempt";

/** In-app path (without the /admin basename) for the redirect memory. */
function inAppPath(pathname: string, search: string): string {
  const stripped = pathname.startsWith("/admin") ? pathname.slice("/admin".length) || "/" : pathname;
  return `${stripped}${search}`;
}

function Routed() {
  const { me, checking, setupRequired } = useSession();
  const location = useLocation();
  const navigate = useNavigate();
  const current = inAppPath(location.pathname, location.search);

  // Remember where the admin wanted to go; after a fresh login go back there once.
  useEffect(() => {
    if (checking || setupRequired) return;
    try {
      if (!me) {
        sessionStorage.setItem(ATTEMPT_KEY, current);
      } else {
        const back = sessionStorage.getItem(ATTEMPT_KEY);
        sessionStorage.removeItem(ATTEMPT_KEY);
        if (back && back.startsWith("/") && back !== current) navigate(back, { replace: true });
      }
    } catch {
      // storage blocked
    }
  }, [me, checking, setupRequired, current, navigate]);

  if (checking) return <Loading />;
  if (setupRequired) return <Setup />;
  if (!me)
    return (
      <>
        <SessionExpiry />
        <Login />
      </>
    );
  const g = (permission: string, element: ReactNode) => <Guard permission={permission}>{element}</Guard>;
  return (
    <Shell>
      <Routes>
        <Route path="/" element={g("dashboard.view", <Home />)} />
        <Route path="/customers" element={g("customers.read", <Customers />)} />
        <Route path="/customers/:id" element={g("customers.read", <CustomerPage />)} />
        <Route path="/services" element={g("services.read", <Services />)} />
        <Route path="/orders" element={g("orders.read", <Orders />)} />
        <Route path="/payments" element={g("payments.read", <Payments />)} />
        <Route path="/tickets" element={g("tickets.read", <Tickets />)} />
        <Route path="/tickets/:id" element={g("tickets.read", <TicketPage />)} />
        <Route path="/catalog" element={g("catalog.read", <Catalog />)} />
        <Route path="/panels" element={g("panels.read", <Panels />)} />
        <Route path="/discounts" element={g("discounts.write", <Discounts />)} />
        <Route path="/broadcasts" element={g("broadcast.send", <Broadcasts />)} />
        <Route path="/lotteries" element={g("broadcast.send", <Lotteries />)} />
        <Route path="/resellers" element={g("resellers.manage", <Resellers />)} />
        <Route path="/admin-accounts" element={g("resellers.manage", <AdminAccounts />)} />
        <Route path="/desk" element={g("services.create", <Desk />)} />
        <Route path="/sub-resellers" element={g("resellers.sub", <SubResellers />)} />
        <Route path="/billing" element={g("dashboard.view", <MyBilling />)} />
        <Route path="/bot-menu" element={g("settings.read", <BotMenu />)} />
        <Route path="/health" element={g("settings.write", <Health />)} />
        <Route path="/migration" element={g("backups.manage", <Migration />)} />
        <Route path="/updates" element={g("backups.manage", <Updates />)} />
        <Route path="/my-bot" element={g("resellers.bot", <Suspense fallback={null}><MyBot /></Suspense>)} />
        <Route path="/reports" element={g("reports.read", <Reports />)} />
        <Route path="/settings" element={g("settings.read", <Settings />)} />
        <Route path="/texts" element={g("texts.write", <Texts />)} />
        <Route path="/channels" element={g("settings.read", <Channels />)} />
        <Route path="/staff" element={g("staff.manage", <Staff />)} />
        <Route path="/audit" element={g("audit.read", <Audit />)} />
        <Route path="/jobs" element={g("settings.write", <Jobs />)} />
        <Route path="/backups" element={g("backups.manage", <Backups />)} />
        <Route path="/account" element={<Account />} />
        <Route path="/help" element={<Suspense fallback={<Loading />}><HelpIndex /></Suspense>} />
        <Route path="/help/:slug" element={<Suspense fallback={<Loading />}><HelpArticle /></Suspense>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  );
}

export function App() {
  return (
    <BrowserRouter basename="/admin">
      <SessionProvider>
        <ConfirmProvider>
          <Routed />
          <Toasts />
        </ConfirmProvider>
      </SessionProvider>
    </BrowserRouter>
  );
}
