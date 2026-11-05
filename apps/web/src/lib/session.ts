import type { QueryClient } from "@tanstack/react-query";

const privateQuery = (query: { queryKey: readonly unknown[] }) =>
  !["setup", "owner"].includes(String(query.queryKey[0]));

export async function clearSession(cache: QueryClient) {
  // Cancel owner revalidation too, so an earlier response cannot restore a logged-out session.
  await cache.cancelQueries({ predicate: query => query.queryKey[0] !== "setup" });
  cache.setQueryData(["owner"], null);
  cache.removeQueries({ predicate: privateQuery });
  cache.getMutationCache().clear();
}
