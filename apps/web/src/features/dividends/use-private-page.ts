import { useEffect, useState, useSyncExternalStore } from "react";
import { onlineManager } from "@tanstack/react-query";

type ReadState<T> = { scope: string; cursor: string | undefined; generation: number; data: T | undefined; error: unknown; pending: boolean };

// Keep form-derived read scopes and results in the mounted component, never in a query cache.
export function usePrivatePage<T>(scope: string, cursor: string | undefined, load: (cursor: string | undefined, signal: AbortSignal) => Promise<T>) {
  const [generation, setGeneration] = useState(0);
  const [state, setState] = useState<ReadState<T>>({ scope, cursor, generation: -1, data: undefined, error: null, pending: true });
  const online = useSyncExternalStore(listener => onlineManager.subscribe(listener), () => onlineManager.isOnline(), () => true);
  useEffect(() => onlineManager.subscribe(value => { if (!value) setGeneration(previous => previous + 1); }), []);
  useEffect(() => {
    if (!online) return;
    const controller = new AbortController();
    void load(cursor, controller.signal).then(data => {
      if (!controller.signal.aborted) setState({ scope, cursor, generation, data, error: null, pending: false });
    }, error => {
      if (!controller.signal.aborted) setState(previous => ({ scope, cursor, generation,
        data: previous.scope === scope && previous.cursor === cursor ? previous.data : undefined, error: error ?? new Error("READ_FAILED"), pending: false }));
    });
    return () => controller.abort();
  }, [scope, cursor, generation, online, load]);
  const samePage = state.scope === scope && state.cursor === cursor;
  const current = samePage && state.generation === generation;
  const data = samePage ? state.data : undefined;
  const isError = current && !state.pending && state.error !== null;
  return { data, error: isError ? state.error : null, isError, isPaused: !online, isFetching: online && (!current || state.pending),
    isPending: online && !data && !isError, refetch: () => setGeneration(value => value + 1) };
}
