# AgiTeamApp

AgiTeamBuilder의 macOS 사용자 배포용 GUI 앱.

| 항목 | 값 |
|---|---|
| 스택 | Tauri v2 + Svelte + TypeScript + pnpm |
| 형제 자산 | `system/AgiTeamBuilder/` (CLI 본체) |
| 부모 | `Panthea/` (제우스 관리) |

## 개발

```bash
pnpm install
pnpm tauri dev
```

## 빌드

```bash
pnpm tauri build
```

산출물: `src-tauri/target/release/bundle/dmg/AgiTeamApp.dmg`

## 구조

- `src/` — Svelte 프론트엔드 (정적 빌드)
- `src-tauri/` — Rust 백엔드 (Tauri)
- 자식 프로젝트 템플릿은 형제 폴더 `../AgiTeamBuilder/AgiTeam/` 참조 (build.rs에서 embed 예정)
