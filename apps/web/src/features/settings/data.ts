import { useContext, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type components } from "@spm/contracts";
import { checked, failureMessage } from "../../lib/api";
import { FormSavedContext } from "../../lib/form-lifecycle";
import { useViewState } from "../../lib/view-state";

type Schemas = components["schemas"];
export type Portfolio = Schemas["Portfolio"];
export type Broker = Schemas["Broker"];
export type Account = Schemas["Account"];
export type Security = Schemas["Security"];
export type Identifier = Schemas["Identifier"];
export type ProviderSymbol = Schemas["ProviderSymbol"];

export function usePortfolios(q = "", cursor?: string) {
  return useQuery({ queryKey: ["portfolios", q, cursor], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/portfolios", { params: { query: { q, cursor, limit: 25 } }, signal })) });
}
export function useBrokers(q = "", cursor?: string) {
  return useQuery({ queryKey: ["brokers", q, cursor], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/brokers", { params: { query: { q, cursor, limit: 25 } }, signal })) });
}
export function useAccounts(q = "", cursor?: string) {
  return useQuery({ queryKey: ["accounts", q, cursor], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/accounts", { params: { query: { q, cursor, limit: 25 } }, signal })) });
}
export function useSecurities(q = "", cursor?: string) {
  return useQuery({ queryKey: ["securities", q, cursor], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/securities", { params: { query: { q, cursor, limit: 25 } }, signal })) });
}
export function useExchanges() {
  return useQuery({ queryKey: ["exchanges"], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/exchanges", { signal })) });
}

export function usePageState(view?: string) {
  const [state, setState] = useViewState(view ? `page:${view}` : undefined, { q: "", cursors: [] as string[] });
  return {
    q: state.q, cursor: state.cursors.at(-1), number: state.cursors.length + 1,
    search: (q: string) => setState({ q, cursors: [] }),
    reset: () => setState({ q: "", cursors: [] }),
    next: (cursor: string) => setState(previous => ({ ...previous, cursors: [...previous.cursors, cursor] })),
    previous: () => setState(previous => ({ ...previous, cursors: previous.cursors.slice(0, -1) })),
  };
}

export function useFormAction<T>(onSaved: (record: T) => void) {
  const markSaved = useContext(FormSavedContext);
  const key = useRef(crypto.randomUUID());
  const [error, setError] = useState<string | null>(null);
  return {
    error,
    changed: () => { key.current = crypto.randomUUID(); setError(null); },
    run: async (action: (key: string) => Promise<T>) => {
      setError(null);
      try {
        const result = await action(key.current);
        key.current = crypto.randomUUID();
        markSaved();
        onSaved(result);
      } catch (cause) { setError(failureMessage(cause)); }
    },
  };
}
