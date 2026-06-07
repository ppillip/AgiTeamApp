import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  // 백엔드(FastAPI) 주소. 기본 localhost:8000. /api/* 를 백엔드로 프록시(WS 포함).
  const target = env.VITE_API_PROXY || "http://localhost:8000";
  return {
    plugins: [vue()],
    server: {
      port: 1420,
      strictPort: false,
      proxy: {
        "/api": {
          target,
          changeOrigin: true,
          ws: true,
        },
      },
    },
  };
});
