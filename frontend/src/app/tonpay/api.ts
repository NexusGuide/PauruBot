import { ApiError, request } from "@shared/http";
import { initData } from "../telegram";
import { payment, type CustomPayment } from "./model";
const root = "/api/webapp/tonpay";
const headers = () => ({ "X-Telegram-Init-Data": initData() });
const post = async (path: string, body: unknown = {}): Promise<CustomPayment> =>
  payment(await request<unknown>(root + path, { method: "POST", headers: headers(), body }));
export const tonpay = {
  create: async (amount: number, intent_key: string): Promise<CustomPayment> => {
    try { return await post("/create", { amount, intent_key }); }
    catch (err) {
      if (!(err instanceof ApiError) || err.status >= 500 || err.code === "creation_uncertain") {
        // A read of this exact owned key, never a second provider creation or a guessed reference.
        try { return await post("/resume", { amount, intent_key }); } catch { /* preserve original error */ }
      }
      throw err;
    }
  },
  view: async (id: number) => payment(await request<unknown>(`${root}/${id}`, { headers: headers() })),
  refresh: (id: number) => post(`/${id}/refresh`),
  consent: (id: number, revision: number, amount: number) => post(`/${id}/consent`, { quote_revision: revision, payable_amount: amount }),
  change: async (id: number, revision: number, key: string) => {
    const result = await request<{ payment: unknown }>(`${root}/${id}/change-card`, {
      method: "POST", headers: headers(), body: { quote_revision: revision, intent_key: key },
    });
    return payment(result.payment);
  },
  receipt: async (id: number, revision: number, key: string, file: File) => {
    const response = await fetch(`${root}/${id}/receipt`, { method: "POST", credentials: "same-origin", body: file,
      headers: { ...headers(), "Content-Type": file.type,
        "X-Tonpay-Operation": JSON.stringify({ quote_revision: revision, intent_key: key }) } });
    if (!response.ok) {
      let code = `http_${response.status}`;
      try { const body: unknown = await response.json();
        if (body && typeof body === "object" && "error" in body && typeof body.error === "string") code = body.error;
      } catch { /* bounded API error, no provider text is echoed */ }
      throw new ApiError(response.status, code);
    }
    const result: { payment: unknown } = await response.json();
    return payment(result.payment);
  },
};
