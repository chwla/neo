import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { rewriteDevOrigin } from "./devProxy.js";

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

//: The supported browsers, matching "browserslist" in package.json (which
//: autoprefixer reads). The floor is set by features the interface uses without a
//: fallback: color-mix() needs Chrome/Edge 111, Safari 16.2; :has() needs Firefox 121.
export const BUILD_TARGET = ["chrome111", "edge111", "firefox121", "safari16.2", "ios16.2"];

export default defineConfig({
  plugins: [react()],
  build: { target: BUILD_TARGET },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiProxyTarget,
        changeOrigin: true,
        // changeOrigin rewrites Host only; see devProxy.js for why Origin follows.
        configure(proxy) {
          proxy.on("proxyReq", (proxyReq, req) => {
            const origin = req.headers.origin;
            const next = rewriteDevOrigin(origin, req.headers.host, apiProxyTarget);
            if (origin && next !== origin) proxyReq.setHeader("origin", next);
          });
        },
      },
    },
  },
});
