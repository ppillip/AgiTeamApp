# AgiTeamApp

Panthea AgiTeamBuilder의 **웹 GUI** (팀 채팅 · 산출물 검토). 요구사항 11-1 "웹 화면".

| 항목 | 값 |
|---|---|
| 스택 | Vue 3 + Vite + Tailwind CSS + pnpm |
| 디자인 | DS-55 4차시안 (A · Polished Amber), 폰트 Pretendard |
| 형제 자산 | `system/AgiTeamBuilder/` (CLI 본체) |
| 부모 | `Panthea/` (제우스 관리) |

> 2026-06-07: 기존 Tauri v2 + Svelte 스캐폴드에서 Vue 웹앱으로 전환.
> 직전 상태는 git 태그 `backup-svelte-tauri-*` 로 로컬 백업되어 있음.

## 개발

```bash
pnpm install
pnpm dev        # http://localhost:1420
```

기본 dev server는 localhost 전용이다. 같은 네트워크의 다른 장비에서 IP로 접속해야 할 때만 host를 공개한다.

```bash
# backend는 별도 터미널/cmux workspace에서 0.0.0.0 또는 지정 IP로 기동
cd backend
PYTHONPATH=. WEBGUI_CORS_ALLOW_ORIGINS="http://localhost:1420,http://127.0.0.1:1420,http://<host-ip>:1420" \
  ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info

# frontend dev server도 공개 host로 기동
cd ..
VITE_HOST=0.0.0.0 VITE_API_PROXY=http://127.0.0.1:8000 pnpm dev

# 브라우저 접속
open http://<host-ip>:1420/
```

`VITE_API_BASE` 또는 `VITE_WS_BASE`로 백엔드에 직접 연결하는 경우에는 `localhost` 대신 브라우저에서 접근 가능한 IP를 사용한다. 기본값은 동일 출처 `/api` 프록시이므로 IP 접속에서도 `VITE_API_PROXY`가 서버 측 백엔드 주소를 가리키면 된다.

## 빌드

```bash
pnpm build      # dist/ 정적 산출물
pnpm preview    # 빌드 결과 미리보기
```

## 구조

- `index.html` — 진입점 (Pretendard CDN 로드)
- `src/main.js` — Vue 앱 부트스트랩
- `src/App.vue` — 루트 컴포넌트 (현재 토큰 적용 확인용 스캐폴드)
- `src/style.css` — Tailwind 지시자 + 전역 스타일
- `tailwind.config.js` — 디자인 토큰 (amber/ink/line/grn) — DS-55 가이드 §4 이식
- `postcss.config.js` — Tailwind/Autoprefixer

## 디자인 토큰

색은 새로 만들지 않고 `tailwind.config.js`의 토큰만 사용한다.
출처: `documents/products/AgiTeamBuilder/04.development/02.설계/DS-55_디자인시안/4차시안/가이드.md`

## 후속 작업

- DS-55 4차시안 채팅 3분할 화면(대화 목록 · 대화 뷰 · 산출물 패널) SFC 이식
- mock 데이터 → 실제 API 연동 (`GET /conversations` 등)
