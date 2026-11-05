import { useQuery } from "@tanstack/react-query";
import { api } from "@spm/contracts";
import { checked } from "../../lib/api";

export function useSecurity(id: string) {
  return useQuery({ queryKey: ["securities", "record", id], enabled: !!id, queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/securities/{id}", { params: { path: { id } }, signal })) });
}

export function useAccount(id: string) {
  return useQuery({ queryKey: ["accounts", "record", id], enabled: !!id, queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/accounts/{id}", { params: { path: { id } }, signal })) });
}
