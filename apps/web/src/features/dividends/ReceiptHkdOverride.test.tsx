import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { ReceiptWriteForm } from "./ReceiptWriteForm";
import { body, created, target } from "./receipt-test-data";

type Schemas = components["schemas"];
function fill() {
  fireEvent.change(screen.getByLabelText("實收日期"), { target: { value: body.received_date } });
  fireEvent.change(screen.getByLabelText("實收幣別"), { target: { value: "HKD" } });
  fireEvent.change(screen.getByLabelText("實收淨額"), { target: { value: body.received_amount } });
  fireEvent.change(screen.getByLabelText("實際稅額"), { target: { value: "UNKNOWN" } });
  fireEvent.change(screen.getByLabelText("實際費用"), { target: { value: "PROVIDED" } });
  fireEvent.change(screen.getByLabelText("已記錄費用"), { target: { value: "0" } });
  fireEvent.change(screen.getByLabelText("收款依據"), { target: { value: body.reason } });
  fireEvent.click(screen.getByRole("checkbox", { name: /我明白這只是擁有人記錄的現金/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /另行指定有依據的實際港幣金額/ }));
}
function show() {
  vi.spyOn(api, "GET").mockResolvedValue({ data: { csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" }, response: new Response() });
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); const onSaved = vi.fn();
  render(<QueryClientProvider client={cache}><ReceiptWriteForm target={target} onSaved={onSaved} onCancel={vi.fn()} /></QueryClientProvider>);
  fill(); return onSaved;
}

describe("HKD receipt override", () => {
  it("records a separately sourced identity override with exactly equal decimal values", async () => {
    const post = vi.spyOn(api, "POST").mockImplementation(async (_path, options) => {
      const request = options as { body: Schemas["ReceiptCreate"] };
      return { data: { ...created, id: request.body.receipt_id, currency: "HKD", fx_rate_hkd: "1.000000000000",
        fx_source: "IDENTITY", received_hkd: "6.125000", actual_hkd_override: true }, response: new Response() };
    });
    const onSaved = show(); fireEvent.change(screen.getByLabelText("實際港幣金額"), { target: { value: "006.125000" } });
    fireEvent.change(screen.getByLabelText("實際港幣金額依據"), { target: { value: "Synthetic actual HKD evidence" } });
    fireEvent.click(screen.getByRole("button", { name: "記錄收款" })); await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const request = post.mock.calls[0]![1] as { body: Schemas["ReceiptCreate"] };
    expect(request.body).toMatchObject({ currency: "HKD", fx_rate_hkd: null, fx_source: null,
      actual_hkd: "006.125000", actual_hkd_reason: "Synthetic actual HKD evidence" });
  });
  it("rejects a different HKD amount and a missing override reason", async () => {
    const post = vi.spyOn(api, "POST"); show();
    fireEvent.change(screen.getByLabelText("實際港幣金額"), { target: { value: "6.125001" } });
    fireEvent.click(screen.getByRole("button", { name: "記錄收款" }));
    expect(await screen.findByText(/港幣實收與實際港幣金額必須相同/)).toBeVisible();
    expect(screen.getByText(/實際金額依據/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
  it("clears the HKD override when changing the receipt currency", () => {
    show(); fireEvent.change(screen.getByLabelText("實際港幣金額"), { target: { value: "6.125" } });
    fireEvent.change(screen.getByLabelText("實際港幣金額依據"), { target: { value: "Synthetic actual HKD evidence" } });
    fireEvent.change(screen.getByLabelText("實收幣別"), { target: { value: "USD" } });
    fireEvent.change(screen.getByLabelText("港幣歷史匯率"), { target: { value: "PROVIDED" } });
    expect(screen.getByRole("checkbox", { name: /另行指定有依據的實際港幣金額/ })).not.toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: /另行指定有依據的實際港幣金額/ }));
    expect(screen.getByLabelText("實際港幣金額")).toHaveValue(""); expect(screen.getByLabelText("實際港幣金額依據")).toHaveValue("");
  });
});
