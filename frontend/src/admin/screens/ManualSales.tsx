import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { call, qs } from "../api";
import { useLoad, useSelection } from "../hooks";
import { useSession } from "../session";
import { Button, Icon, Input, Select, Sheet, Textarea } from "../ui";
import "./manual-sales.css";

type Mode = "single" | "bundle" | "bulk";
interface Part {
  text: string;
  filename?: string | null;
  data?: string | null;
  as_file?: boolean;
  subscription_url?: string | null;
}
interface Plan {
  id: number;
  title: string;
  delivery: string;
  panel_id: number | null;
  stock_plan_id: number | null;
  active: boolean;
}
interface Panel {
  id: number;
  name: string;
  national_mode: boolean;
}
interface Item {
  id: number;
  name: string;
  plan: string;
  status: string;
  parts: number;
  bytes: number;
  order_id: number | null;
  remaining_days: number | null;
}
interface Inventory {
  items: Item[];
  total: number;
  counts: Record<string, number>;
  storage: { used: number; limit: number };
  plans: Plan[];
  panels: Panel[];
}
const ROOT = "/api/admin/manual-sales";
const MIB = 1024 * 1024;
const bytes = (n: number) =>
  n < MIB
    ? `${Math.ceil(n / 1024)} KB`
    : n < 1024 * MIB
      ? `${(n / MIB).toFixed(1)} MB`
      : `${(n / (1024 * MIB)).toFixed(2)} GB`;

export function ManualSales() {
  const { locale, can, toast, errorText } = useSession();
  const fa = locale === "fa";
  const tr = (faText: string, enText: string) => (fa ? faText : enText);
  const [tab, setTab] = useState("inventory");
  const [plan, setPlan] = useState("");
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const [days, setDays] = useState("");
  const [page, setPage] = useState(0);
  const path =
    ROOT +
    qs({
      plan_id: plan,
      status,
      search,
      remaining_days: days,
      offset: page * 25,
    });
  const loaded = useLoad<Inventory>(path);
  const data = loaded.data;
  const selection = useSelection(
    path + (data?.items.map((i) => i.id).join(",") ?? ""),
  );
  const [adding, setAdding] = useState(false);
  const [content, setContent] = useState<{ id: number; parts: Part[] } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [confirmation, setConfirmation] = useState<{
    action: string;
    panel?: Panel;
  } | null>(null);
  const writer = can("catalog.write");
  const nationalWriter = writer && can("panels.write");
  const stockPlans =
    data?.plans.filter((p) => p.delivery === "stock" && p.active) ?? [];
  const run = async (fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setProblem("");
    try {
      await fn();
    } catch (err) {
      setProblem(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const filter = (update: () => void) => {
    update();
    setPage(0);
  };
  const statusName = (s: string) =>
    s === "reserved"
      ? tr("رزروشده", "Reserved")
      : s === "sold"
        ? tr("فروخته‌شده", "Sold")
        : s === "disabled"
          ? tr("غیرفعال", "Disabled")
          : tr("آمادهٔ فروش", "Available");
  const execute = async () => {
    if (!confirmation) return;
    await run(async () => {
      if (confirmation.panel)
        await call(`${ROOT}/national/${confirmation.panel.id}`, {
          method: "PUT",
          body: { enabled: !confirmation.panel.national_mode },
        });
      else
        await call(`${ROOT}/bulk`, {
          body: { ids: [...selection.selected], action: confirmation.action },
        });
      setConfirmation(null);
      selection.clear();
      await loaded.reload();
      toast(tr("تغییرات ذخیره شد", "Changes saved"));
    });
  };
  return (
    <main className="manual-center">
      <header className="manual-heading">
        <div>
          <p className="manual-eyebrow">
            {tr("مرکز تحویل آماده", "READY DELIVERY CENTER")}
          </p>
          <h1>{tr("فروش دستی و نت ملی", "Manual sales & national network")}</h1>
          <p>
            {tr(
              "موجودی را آماده کنید؛ تحویل را به پارو بسپارید.",
              "Prepare your inventory. Let Pauru handle delivery.",
            )}
          </p>
        </div>
        {writer && (
          <Button
            tone="primary"
            icon="plus"
            onClick={() => setAdding(true)}
            disabled={!stockPlans.length || !data}
          >
            {tr("افزودن کانفیگ", "Add configuration")}
          </Button>
        )}
      </header>
      <div
        className="manual-tabs"
        role="tablist"
        aria-label={tr("بخش مدیریت", "Management section")}
      >
        <button
          role="tab"
          id="stock-tab"
          aria-controls="stock-panel"
          aria-selected={tab === "inventory"}
          onClick={() => setTab("inventory")}
        >
          <Icon name="catalog" />
          {tr("موجودی فروش دستی", "Manual inventory")}
        </button>
        <button
          role="tab"
          id="national-tab"
          aria-controls="national-panel"
          aria-selected={tab === "national"}
          onClick={() => setTab("national")}
        >
          <Icon name="panels" />
          {tr("مسیر جایگزین نت ملی", "National-network routing")}
        </button>
      </div>
      {problem || loaded.error ? (
        <div role="alert" className="manual-error">
          {problem || errorText(loaded.error)}{" "}
          <Button
            onClick={() => {
              setProblem("");
              void loaded.reload();
            }}
          >
            {tr("تلاش دوباره", "Try again")}
          </Button>
        </div>
      ) : null}
      {!data && loaded.loading && (
        <p role="status">{tr("در حال دریافت موجودی…", "Loading inventory…")}</p>
      )}
      {data && tab === "inventory" && (
        <section id="stock-panel" role="tabpanel" aria-labelledby="stock-tab">
          <div className="manual-stats">
            {[
              [
                tr("کل کانفیگ‌ها", "Total units"),
                Object.values(data.counts).reduce((a, b) => a + b, 0),
                "catalog",
              ],
              [
                tr("آمادهٔ فروش", "Ready to sell"),
                data.counts.available ?? 0,
                "check",
              ],
              [tr("فروخته‌شده", "Sold"), data.counts.sold ?? 0, "orders"],
              [
                tr("غیرفعال", "Disabled"),
                data.counts.disabled ?? 0,
                "settings",
              ],
            ].map(([label, n, icon]) => (
              <div key={label} className="manual-stat">
                <span>{label}</span>
                <strong>{Number(n).toLocaleString(locale)}</strong>
                <Icon name={String(icon)} size={22} />
              </div>
            ))}
          </div>
          <div className="manual-storage">
            <div>
              <Icon name="backup" />
              <span>
                {tr("فضای امن موجودی", "Encrypted inventory storage")}
              </span>
              <b dir="ltr">
                {bytes(data.storage.used)} / {bytes(data.storage.limit)}
              </b>
            </div>
            <progress
              aria-label={tr("فضای مصرف‌شده", "Storage used")}
              value={data.storage.used}
              max={data.storage.limit}
            />
            <p>
              {tr(
                "محتوا رمزنگاری می‌شود؛ اطلاعات خریدار و فاکتور پس از فروش محفوظ می‌ماند.",
                "Contents are encrypted. Sold units retain their customer and order history.",
              )}
            </p>
          </div>
          {!stockPlans.length && (
            <div className="manual-note">
              {tr(
                "ابتدا یک محصول با روش تحویل «موجودی آماده» بسازید.",
                "Create a product with stock delivery first.",
              )}{" "}
              <Link to="/catalog">
                {tr("رفتن به محصولات ←", "Open catalog →")}
              </Link>
            </div>
          )}
          <div className="manual-inventory">
            <div className="manual-filters">
              <label>
                <span>{tr("جست‌وجوی نام", "Search name")}</span>
                <Input
                  value={search}
                  onChange={(e) => filter(() => setSearch(e.target.value))}
                  placeholder={tr("نام کانفیگ…", "Configuration name…")}
                />
              </label>
              <label>
                <span>{tr("محصول", "Product")}</span>
                <Select
                  value={plan}
                  onChange={(e) => filter(() => setPlan(e.target.value))}
                >
                  <option value="">{tr("همهٔ محصولات", "All products")}</option>
                  {stockPlans.map((p) => (
                    <option value={p.id} key={p.id}>
                      {p.title}
                    </option>
                  ))}
                </Select>
              </label>
              <label>
                <span>{tr("وضعیت", "Status")}</span>
                <Select
                  value={status}
                  onChange={(e) => filter(() => setStatus(e.target.value))}
                >
                  <option value="">
                    {tr("همهٔ وضعیت‌ها", "All statuses")}
                  </option>
                  {["available", "sold", "disabled", "reserved"].map((s) => (
                    <option key={s} value={s}>
                      {statusName(s)}
                    </option>
                  ))}
                </Select>
              </label>
              <label>
                <span>
                  {tr("روز باقی‌مانده، حداکثر", "Remaining days, at most")}
                </span>
                <Input
                  type="number"
                  min={0}
                  max={3650}
                  value={days}
                  onChange={(e) => filter(() => setDays(e.target.value))}
                  placeholder="—"
                />
              </label>
            </div>
            {selection.selected.size > 0 && (
              <div className="manual-bulk">
                <strong>
                  {selection.selected.size} {tr("مورد انتخاب‌شده", "selected")}
                </strong>
                {["enable", "disable", "delete"].map((a) => (
                  <Button
                    key={a}
                    disabled={busy}
                    onClick={() => setConfirmation({ action: a })}
                  >
                    {a === "delete"
                      ? tr("حذف", "Delete")
                      : a === "enable"
                        ? tr("فعال‌سازی", "Enable")
                        : tr("غیرفعال‌سازی", "Disable")}
                  </Button>
                ))}
              </div>
            )}
            <div className="manual-list" aria-busy={loaded.loading}>
              <div className="manual-list-head">
                <span>{tr("کانفیگ / محصول", "Configuration / product")}</span>
                <span>{tr("وضعیت و اعتبار", "Status & validity")}</span>
                <span>{tr("محتوا", "Contents")}</span>
                <span>{tr("جزئیات", "Details")}</span>
              </div>
              {data.items.map((item) => (
                <article className="manual-row" key={item.id}>
                  <div className="manual-unit">
                    {writer && !["sold", "reserved"].includes(item.status) && (
                      <input
                        type="checkbox"
                        aria-label={tr("انتخاب ", "Select ") + item.name}
                        checked={selection.selected.has(item.id)}
                        onChange={() => selection.toggle(item.id)}
                      />
                    )}
                    <div>
                      <strong>{item.name}</strong>
                      <small>
                        {item.plan} · #{item.id}
                      </small>
                    </div>
                  </div>
                  <div>
                    <span className={`manual-status ${item.status}`}>
                      {statusName(item.status)}
                    </span>
                    {item.remaining_days !== null && (
                      <small>
                        {item.remaining_days}{" "}
                        {tr("روز باقی‌مانده", "days left")}
                      </small>
                    )}
                  </div>
                  <div className="manual-file-count">
                    {item.parts} {tr("محتوا", "parts")}
                    <small dir="ltr">{bytes(item.bytes)}</small>
                  </div>
                  <div>
                    {writer && (
                      <Button
                        busy={busy}
                        onClick={() =>
                          void run(async () =>
                            setContent(
                              await call(`${ROOT}/${item.id}/content`),
                            ),
                          )
                        }
                      >
                        {tr("نمایش", "View")}
                      </Button>
                    )}
                    {item.order_id && (
                      <small>
                        #{item.order_id} {tr("فاکتور", "order")}
                      </small>
                    )}
                  </div>
                </article>
              ))}
              {!data.items.length && (
                <div className="manual-empty">
                  <Icon name="catalog" size={38} />
                  <h2>
                    {tr("هنوز موردی اینجا نیست", "No matching configurations")}
                  </h2>
                  <p>
                    {tr(
                      "کانفیگ اضافه کنید یا فیلترها را تغییر دهید.",
                      "Import configurations or adjust your filters.",
                    )}
                  </p>
                </div>
              )}
            </div>
            <footer className="manual-pagination">
              <span>
                {data.total.toLocaleString(locale)} {tr("مورد", "units")}
              </span>
              <Button
                disabled={page === 0 || loaded.loading}
                onClick={() => setPage((p) => p - 1)}
              >
                {tr("قبلی", "Previous")}
              </Button>
              <span>{page + 1}</span>
              <Button
                disabled={(page + 1) * 25 >= data.total || loaded.loading}
                onClick={() => setPage((p) => p + 1)}
              >
                {tr("بعدی", "Next")}
              </Button>
            </footer>
          </div>
        </section>
      )}
      {data && tab === "national" && (
        <section
          id="national-panel"
          role="tabpanel"
          aria-labelledby="national-tab"
          className="manual-national"
        >
          <div className="manual-note">
            <h2>
              {tr(
                "تحویل مستقل از پنل در زمان محدودیت شبکه",
                "Delivery without panel access during network restrictions",
              )}
            </h2>
            <p>
              {tr(
                "برای هر محصول پنل، یک محصول موجودی تعیین کنید. با فعال‌سازی نت ملی، خرید و تمدید از این موجودی تحویل می‌شود؛ بدون تماس با پنل و بدون تغییر خودکار مسیر پس از خطای پرداخت یا پنل.",
                "Map each fixed-panel product to a stock product. While enabled, purchases and renewals use that inventory, without panel calls or automatic fallback after an uncertain API result.",
              )}
            </p>
            <p>
              {tr(
                "محصولات بدون نگاشت از فروش پنهان می‌شوند. پنل‌های نت ملی از انتخاب خودکار موقعیت کنار گذاشته می‌شوند. تمدید، کانفیگ جایگزین می‌دهد و اشتراک اصلی را تغییر نمی‌دهد؛ با خاموش‌کردن حالت، مسیر عادی برمی‌گردد.",
                "Unmapped products are hidden. National-mode panels are excluded from location pools. Renewal delivers a replacement without modifying the original subscription. Switching off restores normal operation.",
              )}
            </p>
          </div>
          {!data.panels.length && (
            <p>{tr("هنوز پنلی اضافه نشده است.", "No panels yet.")}</p>
          )}
          {data.panels.map((panel) => (
            <article className="manual-panel-card" key={panel.id}>
              <header>
                <div>
                  <h2>{panel.name}</h2>
                  <span
                    className={`manual-status ${panel.national_mode ? "available" : "disabled"}`}
                  >
                    {panel.national_mode
                      ? tr("نت ملی فعال", "National mode on")
                      : tr("تحویل عادی پنل", "Normal panel delivery")}
                  </span>
                </div>
                <Button
                  disabled={!nationalWriter || busy}
                  onClick={() => setConfirmation({ action: "national", panel })}
                >
                  {panel.national_mode
                    ? tr("بازگشت به حالت عادی", "Restore normal mode")
                    : tr("فعال‌سازی نت ملی", "Enable national mode")}
                </Button>
              </header>
              {data.plans
                .filter(
                  (p) => p.panel_id === panel.id && p.delivery !== "stock",
                )
                .map((p) => (
                  <label className="manual-mapping" key={p.id}>
                    <span>{p.title}</span>
                    <Select
                      aria-label={
                        tr("موجودی جایگزین ", "Replacement stock for ") +
                        p.title
                      }
                      value={p.stock_plan_id ?? ""}
                      disabled={!nationalWriter || busy}
                      onChange={(e) =>
                        void run(async () => {
                          await call(`${ROOT}/mapping/${p.id}`, {
                            method: "PUT",
                            body: {
                              stock_plan_id: e.target.value
                                ? Number(e.target.value)
                                : null,
                            },
                          });
                          await loaded.reload();
                          toast(tr("نگاشت ذخیره شد", "Mapping saved"));
                        })
                      }
                    >
                      <option value="">
                        {tr(
                          "بدون نگاشت — فروش متوقف",
                          "Unmapped — sales paused",
                        )}
                      </option>
                      {stockPlans.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.title}
                        </option>
                      ))}
                    </Select>
                  </label>
                ))}
            </article>
          ))}
        </section>
      )}
      {adding && (
        <ImportSheet
          plans={stockPlans}
          onClose={() => setAdding(false)}
          onDone={() => {
            setAdding(false);
            void loaded.reload();
          }}
        />
      )}
      <Sheet
        open={content !== null}
        onClose={() => setContent(null)}
        title={tr("محتوای کانفیگ", "Configuration contents")}
      >
        <p className="manual-sensitive">
          {tr(
            "این مشاهده در گزارش حسابرسی ثبت شد. محتوا محرمانه است.",
            "This view was audited. Treat these credentials as confidential.",
          )}
        </p>
        {content?.parts.map((p, i) => (
          <section className="manual-part" key={i}>
            <h3>{p.filename || `${tr("محتوا", "Part")} ${i + 1}`}</h3>
            {p.data ? (
              <Button onClick={() => downloadPart(p, content.id, i)}>
                {tr("دریافت فایل", "Download file")}
              </Button>
            ) : (
              <pre dir="ltr">{p.text}</pre>
            )}
            {p.subscription_url && <pre dir="ltr">{p.subscription_url}</pre>}
          </section>
        ))}
      </Sheet>
      <Sheet
        open={confirmation !== null}
        onClose={() => {
          if (!busy) setConfirmation(null);
        }}
        title={tr("تأیید تغییر", "Confirm change")}
        footer={
          <>
            <Button disabled={busy} onClick={() => setConfirmation(null)}>
              {tr("انصراف", "Cancel")}
            </Button>
            <Button tone="primary" busy={busy} onClick={() => void execute()}>
              {tr("تأیید و اعمال", "Confirm & apply")}
            </Button>
          </>
        }
      >
        <p>
          {confirmation?.panel
            ? tr(
                `حالت تحویل پنل «${confirmation.panel.name}» تغییر می‌کند. سفارش‌های موجودی پرداخت‌شده، طبق مسیر ثبت‌شده تحویل می‌شوند.`,
                `Change delivery mode for “${confirmation.panel.name}”? Paid stock orders keep their original delivery route.`,
              )
            : tr(
                `این عملیات روی ${selection.selected.size} مورد اعمال می‌شود. حذف موجودی برگشت‌پذیر نیست؛ موارد فروخته‌شده قابل حذف نیستند.`,
                `Apply to ${selection.selected.size} units? Deletion cannot be undone. Sold units cannot be removed.`,
              )}
        </p>
        {problem && (
          <p role="alert" className="manual-error">
            {problem}
          </p>
        )}
      </Sheet>
    </main>
  );
}

function downloadPart(p: Part, id: number, index: number) {
  const raw = p.data
    ? Uint8Array.from(atob(p.data), (c) => c.charCodeAt(0))
    : new TextEncoder().encode(p.text);
  const url = URL.createObjectURL(
    new Blob([raw], { type: "application/octet-stream" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = p.filename || `config-${id}-${index + 1}.txt`;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function ImportSheet({
  plans,
  onClose,
  onDone,
}: {
  plans: Plan[];
  onClose: () => void;
  onDone: () => void;
}) {
  const { locale, toast, errorText } = useSession();
  const tr = (a: string, b: string) => (locale === "fa" ? a : b);
  const [plan, setPlan] = useState(String(plans[0]?.id ?? ""));
  const [name, setName] = useState("");
  const [mode, setMode] = useState<Mode>("single");
  const [source, setSource] = useState("text");
  const [text, setText] = useState("");
  const [files, setFiles] = useState<Part[]>([]);
  const [fileDelivery, setFileDelivery] = useState(false);
  const [extension, setExtension] = useState("txt");
  const [links, setLinks] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const parts = useMemo(() => {
    const list: Part[] =
      source === "files"
        ? files
        : (mode === "single"
            ? [text.trim()]
            : text
                .trim()
                .split(/\n\s*\n/)
                .filter(Boolean)
          ).map((value, i) => ({
            text: value,
            as_file: fileDelivery,
            filename: fileDelivery ? `config-${i + 1}.${extension}` : undefined,
          }));
    return list.map((p, i) => ({
      ...p,
      subscription_url: links[i]?.trim() || undefined,
    }));
  }, [source, files, mode, text, fileDelivery, extension, links]);
  const readFiles = async (selected: FileList | null) => {
    setError("");
    if (!selected) return;
    const list = Array.from(selected);
    if (
      list.length > (mode === "single" ? 1 : mode === "bundle" ? 20 : 200) ||
      list.some((f) => f.size > MIB || f.size === 0) ||
      list.reduce((n, f) => n + f.size, 0) > 8 * MIB
    ) {
      setFiles([]);
      setError(
        tr(
          "هر فایل حداکثر ۱ مگابایت، مجموع حداکثر ۸ مگابایت؛ تعداد را با نوع ورود هماهنگ کنید.",
          "Maximum 1 MB per file and 8 MB total. File count must match import mode.",
        ),
      );
      return;
    }
    setBusy(true);
    try {
      const result = await Promise.all(
        list.map(async (file) => {
          const buffer = new Uint8Array(await file.arrayBuffer());
          let binary = "";
          for (let offset = 0; offset < buffer.length; offset += 8192)
            binary += String.fromCharCode(
              ...buffer.subarray(offset, offset + 8192),
            );
          return { text: "", filename: file.name, data: btoa(binary) };
        }),
      );
      setFiles(result);
      setLinks({});
    } catch {
      setFiles([]);
      setError(tr("خواندن فایل ناموفق بود.", "Could not read files."));
    } finally {
      setBusy(false);
    }
  };
  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await call<{ added: number; duplicates: number }>(
        `${ROOT}/import`,
        { body: { plan_id: Number(plan), name: name.trim(), mode, parts } },
      );
      toast(
        tr(
          `${result.added} مورد اضافه شد؛ ${result.duplicates} تکراری کنار گذاشته شد.`,
          `${result.added} added; ${result.duplicates} duplicates skipped.`,
        ),
      );
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Sheet
      open
      onClose={() => {
        if (!busy) onClose();
      }}
      title={tr("افزودن موجودی آماده", "Add ready-to-sell inventory")}
      wide
      footer={
        <>
          <Button disabled={busy} onClick={onClose}>
            {tr("انصراف", "Cancel")}
          </Button>
          <Button
            tone="primary"
            busy={busy}
            disabled={
              !plan ||
              !name.trim() ||
              !parts.length ||
              parts.some((p) => !p.data && !p.text.trim()) ||
              (mode === "single" && parts.length !== 1)
            }
            onClick={() => void submit()}
          >
            {tr("ثبت در موجودی", "Save inventory")}
          </Button>
        </>
      }
    >
      <div className="manual-import">
        <p className="manual-sensitive">
          {tr(
            "ورود امن · ذخیرهٔ رمزنگاری‌شده · تحویل یک‌باره به خریدار",
            "Secure import · Encrypted storage · One buyer per unit",
          )}
        </p>
        <div className="manual-import-grid">
          <label>
            {tr("محصول مرتبط", "Product")}
            <Select value={plan} onChange={(e) => setPlan(e.target.value)}>
              {plans.map((p) => (
                <option value={p.id} key={p.id}>
                  {p.title}
                </option>
              ))}
            </Select>
          </label>
          <label>
            {tr("نام موجودی", "Inventory name")}
            <Input
              maxLength={80}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={tr(
                "مثلاً همراه‌نت — بستهٔ ۳۰ روزه",
                "e.g. Mobile — 30-day pack",
              )}
            />
          </label>
        </div>
        <fieldset>
          <legend>{tr("نوع ورود", "Import mode")}</legend>
          <div className="manual-mode-options">
            {(["single", "bundle", "bulk"] as Mode[]).map((m) => (
              <label className={mode === m ? "chosen" : ""} key={m}>
                <input
                  type="radio"
                  name="mode"
                  value={m}
                  checked={mode === m}
                  onChange={() => {
                    setMode(m);
                    setFiles([]);
                    setLinks({});
                  }}
                />
                <strong>
                  {m === "single"
                    ? tr("تکی", "Single")
                    : m === "bundle"
                      ? tr("بستهٔ چندموردی", "Bundle")
                      : tr("دسته‌ای", "Bulk")}
                </strong>
                <span>
                  {m === "single"
                    ? tr("یک محتوا، یک فروش", "One part, one sale")
                    : m === "bundle"
                      ? tr("همهٔ محتواها، یک فروش", "All parts, one sale")
                      : tr(
                          "هر محتوا، یک فروش مستقل",
                          "Each part, a separate sale",
                        )}
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        <fieldset>
          <legend>{tr("نحوهٔ ورود", "Input source")}</legend>
          <div className="manual-source">
            {["text", "files"].map((s) => (
              <label key={s}>
                <input
                  type="radio"
                  name="source"
                  checked={source === s}
                  onChange={() => {
                    setSource(s);
                    setLinks({});
                  }}
                />
                {s === "text"
                  ? tr("متن / لینک", "Text / link")
                  : tr("آپلود فایل", "Upload files")}
              </label>
            ))}
          </div>
        </fieldset>
        {source === "text" ? (
          <>
            <label>
              {tr("محتوای کانفیگ", "Configuration content")}
              <Textarea
                dir="ltr"
                rows={7}
                maxLength={MIB}
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="vless://…"
              />
            </label>
            {mode !== "single" && (
              <p className="manual-hint">
                {tr(
                  "محتواهای مستقل را با یک خط خالی جدا کنید. خطوط داخل هر محتوا حفظ می‌شوند.",
                  "Separate parts with a blank line. Lines within each part are preserved.",
                )}
              </p>
            )}
            <div className="manual-file-option">
              <label>
                <input
                  type="checkbox"
                  checked={fileDelivery}
                  onChange={(e) => setFileDelivery(e.target.checked)}
                />
                {tr("تحویل متن به‌صورت فایل", "Deliver text as a file")}
              </label>
              {fileDelivery && (
                <Select
                  aria-label={tr("پسوند فایل", "File extension")}
                  value={extension}
                  onChange={(e) => setExtension(e.target.value)}
                >
                  {["txt", "conf", "ovpn", "json", "yaml"].map((x) => (
                    <option key={x}>{x}</option>
                  ))}
                </Select>
              )}
            </div>
          </>
        ) : (
          <label className="manual-upload">
            <Icon name="upload" size={28} />
            <strong>
              {tr("انتخاب فایل‌های کانفیگ", "Choose configuration files")}
            </strong>
            <span>
              {tr(
                "هر فایل تا ۱ مگابایت؛ مجموع تا ۸ مگابایت",
                "Up to 1 MB per file; 8 MB total",
              )}
            </span>
            <input
              type="file"
              accept=".txt,.conf,.ovpn,.json,.yaml,.yml,.npv,.pcf,.ehi"
              multiple={mode !== "single"}
              disabled={busy}
              onChange={(e) => void readFiles(e.target.files)}
            />
          </label>
        )}
        {parts.length > 0 && (source === "files" || text.trim()) && (
          <div className="manual-parts-preview">
            <h3>
              {parts.length} {tr("محتوا آمادهٔ ورود", "parts ready to import")}{" "}
              · {mode === "bulk" ? parts.length : 1}{" "}
              {tr("واحد فروش", "sale units")}
            </h3>
            {parts.slice(0, 200).map((p, i) => (
              <label key={i}>
                <span>{p.filename || `${tr("محتوا", "Part")} ${i + 1}`}</span>
                <Input
                  dir="ltr"
                  type="url"
                  maxLength={2048}
                  value={links[i] ?? ""}
                  onChange={(e) =>
                    setLinks((v) => ({ ...v, [i]: e.target.value }))
                  }
                  placeholder={tr(
                    "لینک اشتراک مجزا (اختیاری)",
                    "Separate subscription URL (optional)",
                  )}
                />
              </label>
            ))}
          </div>
        )}
        {error && (
          <p className="manual-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Sheet>
  );
}
