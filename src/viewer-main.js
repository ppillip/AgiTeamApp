import { createApp } from "vue";
import ViewerWindow from "./components/ViewerWindow.vue";
import "./style.css";

// 산출물 '새창' 전용 엔트리 — 메인 앱과 분리된 페이지에서 ArtifactViewer 를 단독 마운트.
createApp(ViewerWindow).mount("#app");
