import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 開発時(vite dev)はAPI/メディアをstudio/server(7865)へプロキシする。
// 既存ハーネス(5173/7864)とは無関係な単独port。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5175,
    proxy: {
      "/api": "http://localhost:7865",
      "/media": "http://localhost:7865",
    },
  },
});
