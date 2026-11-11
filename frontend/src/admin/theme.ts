// The admin panel's theme: midnight by default, light on request, remembered on this browser.
import { useState } from "react";

export type Theme = "dark" | "light";
const KEY = "admin.theme";

export function savedTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "light" ? "#ffffff" : "#0b0e13");
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(savedTheme);
  const toggle = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // private window: the choice lasts for this visit only
    }
    applyTheme(next);
    setTheme(next);
  };
  return [theme, toggle];
}
