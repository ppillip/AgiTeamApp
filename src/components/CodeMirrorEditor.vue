<script>
// CodeMirror 6 코드 에디터 — 신택스 하이라이팅 보기 + 편집.
//
// 설계(아테나 기반): 코어(@codemirror/state·view·commands·codemirror basicSetup)와 언어팩을
// 모두 dynamic import 로 로드해 초기 번들에서 분리한다. 이 컴포넌트 자체도 ArtifactViewer 에서
// defineAsyncComponent 로 lazy 마운트되므로, 코드 파일을 처음 열 때만 CM 청크가 네트워크에 뜬다.
//
// props:
//   content       에디터 초기/동기 내용(부모가 v-model:content 로 바인딩)
//   languageHint  BE 가 준 언어 힌트(우선). 없으면 extension 으로 추론.
//   extension     파일 확장자(fallback 추론용)
//   readonly      true → 읽기 전용(오프라인/권한 등)
//   theme         'light' | 'dark' (현재 light 우선, dark 후속)
// emits:
//   update:content  편집 시 현재 문서 전체(디바운스 없음 — 부모가 dirty 판정)
//   save            Cmd/Ctrl+S — 부모가 saveArtifact 호출
export default {
  name: "CodeMirrorEditor",
  props: {
    content: { type: String, default: "" },
    languageHint: { type: String, default: null },
    extension: { type: String, default: null },
    readonly: { type: Boolean, default: false },
    theme: { type: String, default: "light" },
  },
  emits: ["update:content", "save"],
  data() {
    return { loading: true, error: null };
  },
  created() {
    // 비반응 인스턴스 필드(EditorView 를 Vue reactive 로 감싸지 않도록 — 내부 상태 보호)
    this.view = null;
    this.cm = null; // 로드된 CM 클래스 모음(reconfigure 에 재사용)
    this._lastEmitted = null; // 우리가 emit 한 변경 에코를 watch 에서 무시하기 위한 가드
  },
  async mounted() {
    await this.init();
  },
  beforeUnmount() {
    this.destroyView();
  },
  watch: {
    // 부모가 content 를 교체(다른 파일/저장 후 메타 갱신 등)하면 에디터 문서 동기화.
    // 우리가 방금 emit 한 값의 에코는 무시(커서 점프 방지).
    content(val) {
      if (!this.view) return;
      if (val === this._lastEmitted) return;
      const cur = this.view.state.doc.toString();
      if (val !== cur) {
        this.view.dispatch({ changes: { from: 0, to: cur.length, insert: val ?? "" } });
      }
    },
    readonly() {
      this.reconfigureReadonly();
    },
  },
  methods: {
    async init() {
      this.loading = true;
      this.error = null;
      try {
        const [cmMeta, cmState, cmView, cmCommands, lang] = await Promise.all([
          import("codemirror"),
          import("@codemirror/state"),
          import("@codemirror/view"),
          import("@codemirror/commands"),
          import("../lib/codeLang.js"),
        ]);
        if (this._destroyed) return; // 로드 중 언마운트 레이스 방어

        const { basicSetup } = cmMeta;
        const { EditorState, Compartment } = cmState;
        const { EditorView, keymap } = cmView;
        const { indentWithTab } = cmCommands;
        this.cm = { EditorState, EditorView, keymap, Compartment };

        // 언어 해석(hint 우선, ext fallback) → 언어팩 동적 로드. 실패/미지원이면 빈 배열(텍스트).
        const langId = lang.resolveLangId(this.languageHint, this.extension);
        let langExt = [];
        if (langId) {
          const e = await lang.loadLanguageExtension(langId);
          if (e) langExt = e;
        }
        if (this._destroyed) return;

        this.langCompartment = new Compartment();
        this.readonlyCompartment = new Compartment();

        // Cmd/Ctrl+S → save emit(브라우저 저장 다이얼로그 차단). basicSetup 보다 앞에 둬 우선권 확보.
        const saveKeymap = keymap.of([
          { key: "Mod-s", preventDefault: true, run: () => { this.$emit("save"); return true; } },
        ]);
        // 편집 변경 → 부모에 전파(dirty 판정은 부모가)
        const updateListener = EditorView.updateListener.of((u) => {
          if (u.docChanged) {
            const val = u.state.doc.toString();
            this._lastEmitted = val;
            this.$emit("update:content", val);
          }
        });

        const state = EditorState.create({
          doc: this.content ?? "",
          extensions: [
            saveKeymap,
            basicSetup,
            keymap.of([indentWithTab]),
            this.langCompartment.of(langExt),
            this.readonlyCompartment.of([
              EditorState.readOnly.of(this.readonly),
              EditorView.editable.of(!this.readonly),
            ]),
            EditorView.lineWrapping,
            updateListener,
            this.lightTheme(EditorView),
          ],
        });
        this.view = new EditorView({ state, parent: this.$refs.host });
        this.loading = false;
      } catch (e) {
        this.error = "코드 에디터를 불러오지 못했습니다.";
        this.loading = false;
      }
    },
    // 읽기전용 토글을 compartment 재구성으로 반영(에디터 재생성 없이).
    reconfigureReadonly() {
      if (!this.view || !this.cm) return;
      const { EditorState, EditorView } = this.cm;
      this.view.dispatch({
        effects: this.readonlyCompartment.reconfigure([
          EditorState.readOnly.of(this.readonly),
          EditorView.editable.of(!this.readonly),
        ]),
      });
    },
    destroyView() {
      this._destroyed = true;
      if (this.view) {
        try { this.view.destroy(); } catch {}
        this.view = null;
      }
    },
    // 앱 토큰(앰버 액센트·Pretendard/모노)에 맞춘 라이트 테마.
    lightTheme(EditorView) {
      return EditorView.theme(
        {
          "&": { height: "100%", fontSize: "13px", backgroundColor: "#ffffff", color: "#1a1a1e" },
          ".cm-scroller": {
            fontFamily: "'JetBrains Mono','SFMono-Regular',Menlo,Consolas,'Liberation Mono',monospace",
            lineHeight: "1.6",
            overflow: "auto",
          },
          ".cm-content": { caretColor: "#dd6b1f" },
          "&.cm-focused": { outline: "none" },
          ".cm-gutters": { backgroundColor: "#FAFAFB", color: "#9a9aa2", border: "none", borderRight: "1px solid #EDEDF0" },
          ".cm-activeLineGutter": { backgroundColor: "#F4EEE6", color: "#c2570b" },
          ".cm-activeLine": { backgroundColor: "#FBF7F2" },
          "&.cm-focused .cm-cursor": { borderLeftColor: "#dd6b1f" },
          "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": { backgroundColor: "#F4D9BB" },
          ".cm-matchingBracket": { backgroundColor: "#F4D9BB", color: "inherit", outline: "1px solid #dd6b1f33" },
        },
        { dark: false }
      );
    },
  },
};
</script>

<template>
  <div class="relative h-full min-h-0 w-full">
    <div ref="host" class="h-full min-h-0 w-full overflow-hidden"></div>
    <div v-if="loading" class="absolute inset-0 flex items-center justify-center bg-white/70 text-[13px] text-ink-400">
      코드 에디터 불러오는 중…
    </div>
    <div v-if="error" class="absolute inset-0 flex items-center justify-center bg-white/90 text-[13px] font-semibold text-red-500">
      {{ error }}
    </div>
  </div>
</template>
