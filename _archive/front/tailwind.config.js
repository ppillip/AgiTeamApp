/** @type {import('tailwindcss').Config} */
// 디자인 토큰 출처: documents/.../DS-55_디자인시안/4차시안/가이드.md §4
// 시안 방향: A · Polished Amber. 색을 새로 만들지 말고 이 토큰만 사용한다.
export default {
  content: ["./index.html", "./src/**/*.{vue,js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Pretendard", "system-ui", "-apple-system", "sans-serif"],
      },
      // 에이전트 동작중 깜빡 감쇠(요구사항 15-1, DS-110 §8.1)는 src/style.css 에 plain CSS 로 직접 정의한다.
      //   (tailwind JIT extend 는 dev 런타임 번들에 keyframe 이 누락되는 사례가 있어 style.css 로 이전 — 실측 수정)
      colors: {
        amber: {
          DEFAULT: "#DD6B1F", // 주요 액센트 · 선택 아바타 · 전송 버튼
          600: "#C2570B",     // 앰버 텍스트(칩·라벨)
          800: "#7A3D08",     // 내 말풍선 텍스트
          tint: "#FBEEDF",    // 앰버 배경(칩·아바타·말풍선)
          tintbd: "#F4D9BB",  // 앰버 보더
          sel: "#FDF6EE",     // 선택된 대화 행 배경
        },
        ink: {
          900: "#1A1A1E", // 기본 텍스트
          800: "#26262B",
          700: "#52525B", // 보조 텍스트
          600: "#6B6B73",
          500: "#9A9AA2", // 아이콘 기본색
          400: "#A1A1AA", // 메타/시간
          300: "#B5B5BC", // 가장 옅은 텍스트
        },
        line: {
          DEFAULT: "#ECEDEF", // 카드 보더
          soft: "#F0F0F2",    // 내부 구분선
        },
        grn: {
          DEFAULT: "#1A9E5F", // 연결됨·진행중 상태
          700: "#1A8E55",
          tint: "#EAF7F0",    // 초록 상태 배경
          tintbd: "#CFEBDC",  // 초록 상태 보더
        },
      },
    },
  },
  plugins: [],
};
