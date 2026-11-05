import { useSyncExternalStore } from "react";

const query = "(min-width: 1024px)";
const snapshot = () => typeof window.matchMedia === "function" && window.matchMedia(query).matches;
function subscribe(changed: () => void) {
  if (typeof window.matchMedia !== "function") return () => {};
  const media = window.matchMedia(query);
  media.addEventListener("change", changed);
  return () => media.removeEventListener("change", changed);
}
export function useDesktop() { return useSyncExternalStore(subscribe, snapshot, () => false); }
