<script>
// 독립 새창(viewer.html) 루트 — '새창' 버튼이 여는 페이지.
// '크게보기'와 동일한 ArtifactViewer 를 그대로 마운트해, 쿼리(path·project_id·root_type)로
// 파일을 직접 fetch 해 store.viewer 를 채운다. → md/code/image/html 이 raw 가 아니라 렌더된 뷰로 표시.
import { store } from "../stores/monitor.js";
import { fetchFile } from "../api/index.js";
import { ApiError } from "../api/client.js";
import ArtifactViewer from "./ArtifactViewer.vue";

export default {
  name: "ViewerWindow",
  components: { ArtifactViewer },
  async mounted() {
    const q = new URLSearchParams(window.location.search);
    const path = q.get("path");
    const projectId = q.get("project_id") || null;
    const rootType = q.get("root_type") || null;
    const name = q.get("name") || (path ? path.split("/").pop() : "산출물");
    const ext = q.get("ext") || (name.includes(".") ? name.split(".").pop() : "");

    // ArtifactViewer 가 streamUrl·저장 등에서 참조하는 전역 컨텍스트 세팅
    store.selectedProjectId = projectId;
    store.rootType = rootType;

    document.title = `${name} · 산출물 뷰어`;

    if (!path) {
      store.viewer = { open: true, loading: false, path: null, file: null, error: "표시할 파일 경로가 없습니다." };
      return;
    }

    store.viewer = { open: true, loading: true, path, file: null, error: null };
    try {
      const file = await fetchFile(path, { prefer: "inline", projectId, rootType });
      store.viewer.file = file;
    } catch (e) {
      store.viewer.error = e instanceof ApiError ? e.message : "파일을 불러오지 못했습니다.";
    } finally {
      store.viewer.loading = false;
    }
  },
};
</script>

<template>
  <div class="flex h-screen w-screen flex-col overflow-hidden bg-white">
    <ArtifactViewer :big="true" :popup="true" />
  </div>
</template>
