// Built into the hub package (served over Home Assistant Ingress under a changing prefix): every path is relative,
// nothing is loaded from a CDN. `npm run dev` proxies the API to a local hub (development listener on 127.0.0.1).
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "../hub/src/solar_visor_hub/web",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:8099", "/card": "http://127.0.0.1:8099" },   // SOLAR_VISOR_UI_DEV_BIND of the hub
    fs: { allow: [".", "../contracts/fixtures"] },     // the card's parity test reads the apps' shared fixture
  },
  test: {
    environment: "jsdom",
    css: { include: [/styles\.css/] },                 // the style test reads it (default: CSS becomes empty)
  },
});
