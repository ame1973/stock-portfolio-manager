import { useAccount, useSecurity } from "./data";

export function AccountName({ id }: { id: string }) {
  const record = useAccount(id);
  return <span className="block max-w-56 break-words">{record.data?.name ?? id}{record.isError && "（名稱未能更新）"}</span>;
}

export function SecurityName({ id }: { id: string }) {
  const record = useSecurity(id);
  return <span className="block max-w-56 break-words">{record.data ? `${record.data.display_ticker} / ${record.data.exchange_code}` : id}{record.isError && "（代號未能更新）"}</span>;
}
