import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@shared/styles.css";
import { App } from "./App";

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
