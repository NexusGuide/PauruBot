import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@shared/styles.css";
import "./theme.css";
import { applyTheme, savedTheme } from "./theme";
import { App } from "./App";

applyTheme(savedTheme());

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/admin/sw.js", { scope: "/admin/" }).catch(() => undefined);
  });
}
