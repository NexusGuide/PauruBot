import type { PaymentStart } from "../api";

/** Read/resume never falls back to starting a new invoice, even on 409/network errors. */
export async function resumeSavedPayment(
  id: number,
  ports: {
    read: (id: number) => Promise<{ payment: PaymentStart }>;
    remember: (value: { paymentId: number; amount: number; accounts?: PaymentStart["accounts"]; crypto?: PaymentStart["crypto"] }) => void;
    open: (url: string) => void;
    navigate: (path: string) => void;
  },
) {
  const { payment } = await ports.read(id);
  if (payment.payment_id !== id || payment.invoice_link) throw new Error("invalid_saved_checkout");
  if (payment.redirect_url) {
    const url = new URL(payment.redirect_url);
    if (url.protocol !== "https:" || url.username || url.password) throw new Error("invalid_saved_checkout");
  }
  if (payment.accounts || payment.crypto) {
    ports.remember({ paymentId: id, amount: payment.amount, accounts: payment.accounts, crypto: payment.crypto });
  }
  if (payment.redirect_url) ports.open(payment.redirect_url);
  ports.navigate(`/payment/${id}`);
}
