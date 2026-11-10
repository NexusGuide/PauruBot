import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { money } from "@shared/format";
import { api, type Review, type StaffDashboard } from "../api";
import { errorText, methodName, useApp } from "../state";
import { haptic, tg } from "../telegram";
import { Button, Empty, Loading, Panel, Screen } from "../ui";

function Figure({ label, value, tone = "" }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-2xl bg-[var(--bg)] p-3">
      <p className="text-xs text-[var(--hint)]">{label}</p>
      <p className={`num mt-1 text-lg font-extrabold ${tone}`}>{value}</p>
    </div>
  );
}

function Figures({ data }: { data: StaffDashboard }) {
  const { t, locale } = useApp();
  const n = (value: number | undefined) => money(value ?? 0, locale);
  if (data.reseller || data.spent_month !== undefined) {
    return (
      <Panel className="mb-4">
        <div className="grid grid-cols-2 gap-2">
          <Figure label={t("staff_active_services")} value={n(data.active_services)} />
          <Figure label={t("staff_orders_month")} value={n(data.orders_month)} />
          <Figure label={t("staff_spent_month")} value={t("toman_amount", { amount: n(data.spent_month) })} />
          {data.reseller && (
            <Figure
              label={t("staff_balance")}
              value={t("toman_amount", { amount: n(data.reseller.balance) })}
              tone={data.reseller.balance < 0 ? "text-anar-700" : ""}
            />
          )}
        </div>
        {data.reseller?.suspended && <p className="mt-3 text-sm font-semibold text-anar-700">{t("staff_suspended")}</p>}
      </Panel>
    );
  }
  const down = (data.panels ?? []).filter((p) => p.active && p.healthy === false);
  return (
    <Panel className="mb-4">
      <div className="grid grid-cols-2 gap-2">
        <Figure label={t("staff_revenue_today")} value={t("toman_amount", { amount: n(data.revenue_today) })} />
        <Figure label={t("staff_revenue_month")} value={t("toman_amount", { amount: n(data.revenue_month) })} />
        <Figure label={t("staff_customers_today")} value={n(data.customers_today)} />
        <Figure label={t("staff_active_services")} value={n(data.active_services)} />
        <Figure
          label={t("staff_pending_receipts")}
          value={n(data.pending_receipts)}
          tone={(data.pending_receipts ?? 0) > 0 ? "text-saffron-700" : ""}
        />
        <Figure label={t("staff_open_tickets")} value={n(data.open_tickets)} />
      </div>
      {down.length > 0 && (
        <p className="mt-3 text-sm font-semibold text-anar-700">
          {t("staff_panels_down", { names: down.map((p) => p.name).join(", ") })}
        </p>
      )}
    </Panel>
  );
}

function ReviewCard({ item, onDone }: { item: Review; onDone: (id: number) => void }) {
  const { me, t, locale, toast } = useApp();
  const [image, setImage] = useState<string | null>(null);
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");

  useEffect(() => {
    if (!item.has_receipt) return;
    let url: string | null = null;
    api.receipt(item.id).then((made) => {
      url = made;
      setImage(made);
    }).catch(() => setImage(null));
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [item.id, item.has_receipt]);

  const decide = async (kind: "approve" | "reject") => {
    setBusy(kind);
    try {
      if (kind === "approve") {
        const result = await api.approve(item.id);
        toast(result.order_settled ? t("staff_approved_delivered") : t("staff_approved"));
      } else {
        await api.reject(item.id, reason.trim());
        toast(t("staff_rejected"));
      }
      haptic("success");
      onDone(item.id);
    } catch (err) {
      haptic("error");
      toast(errorText(t, err), "error");
    } finally {
      setBusy(null);
    }
  };

  const credit = item.review_amount ?? item.amount;
  return (
    <Panel className="mb-3">
      <div className="flex items-baseline justify-between gap-3">
        <p className="num text-lg font-extrabold">{t("toman_amount", { amount: money(credit, locale) })}</p>
        <p className="text-xs text-[var(--hint)]">#{item.id} · {methodName(t, me, item.provider)}</p>
      </div>
      <p className="mt-1 text-sm"><bdi>{item.customer ?? "-"}</bdi></p>
      {item.review_amount !== null && (
        <p className="mt-2 rounded-xl bg-saffron-100 px-3 py-2 text-sm font-semibold text-saffron-700">
          {t("staff_amount_differs", { asked: money(item.amount, locale), got: money(item.review_amount, locale) })}
        </p>
      )}
      {item.duplicate_of_id && (
        <p className="mt-2 rounded-xl bg-anar-100 px-3 py-2 text-sm font-semibold text-anar-700">
          {t("staff_duplicate", { id: item.duplicate_of_id })}
        </p>
      )}
      {image && <img src={image} alt={t("staff_receipt")} className="mt-3 max-h-80 w-full rounded-2xl object-contain" />}
      {item.receipt_text && <p className="mt-2 whitespace-pre-wrap text-sm text-[var(--hint)]">{item.receipt_text}</p>}
      {rejecting ? (
        <div className="mt-3 flex flex-col gap-2">
          <label className="text-sm font-semibold" htmlFor={`reason-${item.id}`}>{t("staff_reject_reason")}</label>
          <input
            id={`reason-${item.id}`}
            className="min-h-12 rounded-2xl bg-[var(--bg)] px-4 ring-1 ring-[var(--line)]"
            value={reason}
            maxLength={200}
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="grid grid-cols-2 gap-2">
            <Button tone="quiet" onClick={() => setRejecting(false)}>{t("cancel")}</Button>
            <Button tone="danger" busy={busy === "reject"} onClick={() => void decide("reject")}>{t("staff_reject")}</Button>
          </div>
        </div>
      ) : (
        <div className="mt-3 grid grid-cols-2 gap-2">
          <Button tone="danger" disabled={busy !== null} onClick={() => setRejecting(true)}>{t("staff_reject")}</Button>
          <Button busy={busy === "approve"} disabled={busy !== null} onClick={() => void decide("approve")}>
            {t("staff_approve")}
          </Button>
        </div>
      )}
    </Panel>
  );
}

export function Manage() {
  const { me, t } = useApp();
  const navigate = useNavigate();
  const [data, setData] = useState<StaffDashboard | null>(null);
  const [items, setItems] = useState<Review[] | null>(null);
  const staff = me?.staff;

  const load = useCallback(() => {
    if (!staff) return;
    if (staff.dashboard) api.staffDashboard().then(setData).catch(() => setData({}));
    if (staff.review) api.reviews().then((r) => setItems(r.items)).catch(() => setItems([]));
  }, [staff]);

  useEffect(load, [load]);

  if (!staff) return <Screen title={t("staff_title")}><Empty title={t("staff_none")} /></Screen>;
  const openAdmin = () => {
    const url = `${window.location.origin}/admin/`;
    if (tg) tg.openLink(url);
    else window.open(url, "_blank", "noopener");
  };

  return (
    <Screen title={t("staff_title")} action={<span className="text-sm text-[var(--hint)]">{t(`role_${staff.role}`)}</span>}>
      {staff.dashboard && (data ? <Figures data={data} /> : <Loading />)}
      {staff.review && (
        <section className="mb-4">
          <h2 className="mb-3 text-lg font-bold">{t("staff_reviews")}</h2>
          {items === null ? (
            <Loading />
          ) : items.length === 0 ? (
            <Panel><p className="text-sm text-[var(--hint)]">{t("staff_no_reviews")}</p></Panel>
          ) : (
            items.map((item) => (
              <ReviewCard key={item.id} item={item} onDone={(id) => {
                setItems((prev) => (prev ?? []).filter((r) => r.id !== id));
                if (staff.dashboard) api.staffDashboard().then(setData).catch(() => undefined);
              }} />
            ))
          )}
        </section>
      )}
      {staff.own ? (
        // A customer's dedicated bot has no web panel login: its shop is run from "My shop" (OPB).
        <Button tone="quiet" onClick={() => navigate("/shop")}>{t("own_title")}</Button>
      ) : (
        <Button tone="quiet" onClick={openAdmin}>{t("staff_open_admin")}</Button>
      )}
    </Screen>
  );
}
