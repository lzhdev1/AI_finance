import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时把 /api 转到后端容器；生产由 nginx 反代（见 nginx.conf）
export default defineConfig({
  base: process.env.VITE_BASE_PATH || "/",
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET || "http://localhost:8000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    rollupOptions: {
      output: { manualChunks: { charts: ["recharts"], react: ["react", "react-dom"] } },
    },
  },
});
