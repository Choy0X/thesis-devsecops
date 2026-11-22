import { defineConfig } from "@playwright/test";

// A separate port gives automation its own preview without adopting another local server.
const port = Number(process.env.ZTSA_E2E_PORT || 4173);

// A smoke test of the built app in the Chrome that is installed on this computer (no browser download): every page is opened with the API replaced by fixtures.
// `npm run e2e` builds into app/frontend/dist-e2e first and serves that folder with `vite preview`. It never builds into `dist`, which the running desktop app serves: a build
// replaces the hashed files of `dist`, and a window that was opened before it then cannot load its pages.
export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: { baseURL: `http://127.0.0.1:${port}`, channel: "chrome", headless: true, viewport: { width: 1360, height: 860 } },
  webServer: { command: `npx vite preview --outDir dist-e2e --host 127.0.0.1 --port ${port} --strictPort`, url: `http://127.0.0.1:${port}`, reuseExistingServer: !process.env.ZTSA_E2E_PORT, timeout: 60_000 },
});
