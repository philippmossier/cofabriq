// The browser gate's write token. `cofabriq serve --write` prints the UI's URL
// with the token in the fragment (#token=...), which the browser never sends to
// the server. It is moved to sessionStorage (gone with the tab) and removed from
// the address bar, so it doesn't end up in history or a shared screenshot.

const KEY = "cofabriq-token";

export function takeTokenFromUrl(): void {
  const match = window.location.hash.match(/token=([A-Za-z0-9_-]+)/);
  if (!match) return;
  sessionStorage.setItem(KEY, match[1]!);
  history.replaceState(null, "", window.location.pathname + window.location.search);
}

export function writeToken(): string | null {
  return sessionStorage.getItem(KEY);
}

/** Fetch options for a write: the token as a bearer header, JSON body. */
export function writeAuth(): RequestInit {
  return { headers: { Authorization: `Bearer ${writeToken() ?? ""}` } };
}
