import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// base + outDir match how the gateway serves the app: FastAPI mounts
// /static (StaticFiles) and dashboard.py answers "/" with the built
// index.html from static/ui, injecting the locale catalogs into
// window.__ENGRIX__. In dev the same injection happens here, straight from
// web/strings/*.json, so `npm run dev` shows the same page as production
// against a gateway running on 8450 (proxied below, no CORS).

const HERE = path.dirname(fileURLToPath(import.meta.url));

function injectCatalogs(): Plugin {
  return {
    name: "engrix-catalogs",
    apply: "serve",
    transformIndexHtml(html) {
      const dir = path.resolve(HERE, "../strings");
      const catalogs = {
        en: JSON.parse(readFileSync(path.join(dir, "en.json"), "utf8")),
        id: JSON.parse(readFileSync(path.join(dir, "id.json"), "utf8")),
      };
      return html
        .replace("{{STRINGS_JSON}}", JSON.stringify(catalogs))
        .replace("{{DEFAULT_LOCALE}}", "en")
        .replace("{{CLIENT_BASE}}", "http://127.0.0.1:8450")
        .replace(/{{APP_NAME}}/g, "engrix-router")
        .replace(/{{APP_VERSION}}/g, "dev");
    },
  };
}

export default defineConfig({
  plugins: [react(), injectCatalogs()],
  base: "/static/ui/",
  build: {
    outDir: "../static/ui",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8450",
      "/health": "http://127.0.0.1:8450",
      "/static": "http://127.0.0.1:8450",
    },
  },
});
