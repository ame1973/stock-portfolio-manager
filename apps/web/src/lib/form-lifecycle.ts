import { createContext } from "react";

// Signal a confirmed response without putting form values in shared state or caches.
export const FormSavedContext = createContext<() => void>(() => {});
