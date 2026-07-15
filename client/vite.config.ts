import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 開発時(vite dev)は API/メディアを Express(7864)へプロキシする
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:7864",
      "/media": "http://localhost:7864",
    },
  },
});
