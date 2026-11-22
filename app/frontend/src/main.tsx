import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { ToastProvider } from "./components/Toast";
import { DownloadNotice } from "./components/DownloadNotice";
import { ConfirmProvider } from "./components/hud/AlertDialog";
import { DevModeProvider } from "./lib/devMode";
import { reloadOnce } from "./lib/staleBuild";

// The bundler says so when a file it preloads is gone (the app was rebuilt under this window): reload once, which fetches the new index.
window.addEventListener("vite:preloadError", (event) => { if (reloadOnce()) event.preventDefault(); });

createRoot(document.getElementById("root")!).render(
  <StrictMode><BrowserRouter><DevModeProvider><ToastProvider><ConfirmProvider><App /><DownloadNotice /></ConfirmProvider></ToastProvider></DevModeProvider></BrowserRouter></StrictMode>
);
