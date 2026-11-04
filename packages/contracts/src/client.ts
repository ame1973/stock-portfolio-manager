import createClient from "openapi-fetch";
import type { paths } from "./schema";

export const api = createClient<paths>({
  baseUrl: "",
  credentials: "same-origin",
  cache: "no-store",
});

export type { components, paths } from "./schema";
