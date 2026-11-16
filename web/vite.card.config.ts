// Dashboard card (plan lovelace-card): two single-file ES modules without React.
//   --mode renderer → the renderer the hub serves over Ingress (hub package: web/card/card.js)
//   --mode loader   → the loader installed once as a Lovelace resource (build/card/solar-visor-card.js)
import { defineConfig } from "vite";

export default defineConfig(({ mode }) => {
  const loader = mode === "loader";
  return {
    build: {
      lib: {
        entry: loader ? "src/card/loader.ts" : "src/card/renderer.ts",
        formats: ["es"],
        fileName: () => (loader ? "solar-visor-card.js" : "card.js"),
      },
      outDir: loader ? "../build/card" : "../hub/src/solar_visor_hub/web/card",
      emptyOutDir: true,
      sourcemap: false,
      target: "es2022",
      minify: true,
    },
  };
});
