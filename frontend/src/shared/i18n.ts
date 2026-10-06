export type Dict = Record<string, string>;
export type Translate = (key: string, vars?: Record<string, string | number>) => string;

export function makeT(dict: Dict, fallback: Dict): Translate {
  return (key, vars) => {
    const template = dict[key] ?? fallback[key] ?? key;
    if (!vars) return template;
    return template.replace(/\{([a-z_]+)\}/g, (_, name: string) =>
      vars[name] === undefined ? `{${name}}` : String(vars[name]),
    );
  };
}

export function direction(locale: string): "rtl" | "ltr" {
  return locale === "fa" ? "rtl" : "ltr";
}
