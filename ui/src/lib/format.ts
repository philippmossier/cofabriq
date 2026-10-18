/** Local date and time, short; "-" when unknown. */
export function when(iso: string | null | undefined): string {
  if (!iso) return "-";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
}

/** Notional API list price, as the runs record it. */
export function usd(n: number): string {
  return `$${n.toFixed(3)}`;
}
