import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: `cofabriq serve` runs the API on 127.0.0.1:8765, Vite proxies /api to
// it. Built: `cofabriq serve` serves ui/dist itself (one artifact, ADR-009).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
