import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@shared/styles.css";
import "./app.css";
import "./effects.css";
import { App } from "./App";
import { boot } from "./telegram";

document.documentElement.dataset.app = "miniapp";
const root = document.getElementById("root");
void boot().then(() => {
  if (root) {
    createRoot(root).render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  }
});
