/** Local date and time, short; "-" when unknown. */
export function when(iso: string | null | undefined): string {
  if (!iso) return "-";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
}

/** "17.6 s" under a minute, "3 min 20 s" over it. */
export function duration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const s = Math.round(seconds);
  return `${Math.floor(s / 60)} min ${s % 60} s`;
}

/** Notional API list price, as the runs record it. */
export function usd(n: number): string {
  return `$${n.toFixed(3)}`;
}
