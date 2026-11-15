// The browser gate's write token. `cofabriq serve --write` prints the UI's URL
// with the token in the fragment (#token=...), which the browser never sends to
// the server. It is moved to sessionStorage (gone with the tab) and removed from
// the address bar, so it doesn't end up in history or a shared screenshot.

import { useSyncExternalStore } from "react";

const KEY = "cofabriq-token";
const listeners = new Set<() => void>();

function changed(): void {
  listeners.forEach((l) => l());
}

export function takeTokenFromUrl(): void {
  const match = window.location.hash.match(/token=([A-Za-z0-9_-]+)/);
  if (!match) return;
  sessionStorage.setItem(KEY, match[1]!);
  history.replaceState(null, "", window.location.pathname + window.location.search);
  changed();
}

// A new link pasted into an open tab only changes the fragment: no reload.
window.addEventListener("hashchange", takeTokenFromUrl);

export function writeToken(): string | null {
  return sessionStorage.getItem(KEY);
}

/** A token the server refused (401: it restarted with a new one). */
export function dropToken(): void {
  sessionStorage.removeItem(KEY);
  changed();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The token, re-rendering when a new link brings one or a refusal drops it. */
export function useWriteToken(): string | null {
  return useSyncExternalStore(subscribe, writeToken);
}

/** Fetch options for a write: the token as a bearer header, JSON body. */
export function writeAuth(): RequestInit {
  return { headers: { Authorization: `Bearer ${writeToken() ?? ""}` } };
}
