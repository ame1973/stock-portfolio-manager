import { createContext, useContext, useState, type Dispatch, type SetStateAction } from "react";

export type Views = Record<string, unknown>;
export const ViewContext = createContext<{ views: Views; setViews: Dispatch<SetStateAction<Views>> } | null>(null);

// View state survives route changes only inside the authenticated, memory-only workspace.
export function useViewState<T>(key: string | undefined, initial: T): [T, Dispatch<SetStateAction<T>>] {
  const store = useContext(ViewContext);
  const [local, setLocal] = useState(initial);
  if (!store || !key) return [local, setLocal];
  const value = Object.hasOwn(store.views, key) ? store.views[key] as T : initial;
  return [value, action => store.setViews(previous => {
    const observed = Object.hasOwn(previous, key) ? previous[key] as T : initial;
    const next = typeof action === "function" ? (action as (value: T) => T)(observed) : action;
    return { ...previous, [key]: next };
  })];
}
