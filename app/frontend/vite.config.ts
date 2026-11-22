import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// `npm run dev` serves only the page. The backend lets a request in only with the session cookie it sets when it serves "/", so the interface cannot talk to it from this server:
// develop with `make app-dev` (one address, http://127.0.0.1:8000, rebuilt on every change) or with `python -m app.desktop`. The proxy below is kept for tools that call the backend
// directly through the dev server; ZTSA_API names the backend when it is not on port 8000.
const api = process.env.ZTSA_API || "http://127.0.0.1:8000";

export default defineConfig({
  base: "/",
  plugins: [react()],
  server: {
    proxy: {
      "/api": api,
      "/ws": { target: api.replace(/^http/, "ws"), ws: true },
    },
  },
  build: { outDir: "dist" },
  test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], include: ["src/**/*.test.{ts,tsx}"] },
});
