import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

declare const process: { env: Record<string, string | undefined> };

const apiHost = process.env.API_HOST || process.env.APP_HOST || "127.0.0.1";
const proxyHost = apiHost === "0.0.0.0" ? "127.0.0.1" : apiHost;
const apiPort = process.env.API_PORT || process.env.APP_PORT || "8000";
const apiTarget = `http://${proxyHost}:${apiPort}`;

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  define: {
    "process.env.NODE_ENV": JSON.stringify(mode === "production" ? "production" : "development"),
  },
  server: {
    // Mirrors production (Caddy proxies /api and /health to the backend),
    // so VITE_API_BASE_URL can stay empty ("same origin") in dev too.
    proxy: {
      "/api": apiTarget,
      "/health": apiTarget,
    },
  },
}));
