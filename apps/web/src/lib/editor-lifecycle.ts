import { createContext } from "react";

export const EditorCloseContext = createContext<(() => void) | null>(null);
