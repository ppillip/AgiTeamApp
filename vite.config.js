import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  // 백엔드(FastAPI) 주소. 기본 localhost:8000. /api/* 를 백엔드로 프록시(WS 포함).
  const target = env.VITE_API_PROXY || "http://localhost:8000";
  // WebSocket(update channel) 전용 타깃: http→ws 스킴 변환 (DV-48 / QI-WG-026).
  const wsTarget = target.replace(/^http/i, "ws");
  return {
    plugins: [vue()],
    server: {
      port: 1420,
      strictPort: false,
      proxy: {
        // WG-MSG-05 실시간 update channel. /api 통합 프록시의 ws:true 만으로는 일부
        // 환경에서 upgrade 가 라우팅되지 않아(QI-WG-026 증상), WS 경로를 ws:// 타깃의
        // 전용 엔트리로 먼저 매칭시켜 upgrade 를 확실히 백엔드로 넘긴다. (키 순서 = 매칭 우선순위)
        "/api/webgui/message-stream": {
          target: wsTarget,
          ws: true,
          changeOrigin: true,
        },
        // 나머지 REST + (보조) WS
        "/api": {
          target,
          changeOrigin: true,
          ws: true,
        },
      },
    },
  };
});
