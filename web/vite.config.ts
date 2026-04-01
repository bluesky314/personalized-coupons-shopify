import { vitePlugin as remix } from "@remix-run/dev";
import { defineConfig } from "vite";

export default defineConfig({
  server: {
    allowedHosts: [".trycloudflare.com", ".ngrok-free.dev", ".ngrok.io"],
  },
  plugins: [
    remix({
      ignoredRouteFiles: ["**/.*"],
    }),
  ],
});
