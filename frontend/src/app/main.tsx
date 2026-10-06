import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@shared/styles.css";
import { App } from "./App";
import { boot } from "./telegram";

boot();
const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
