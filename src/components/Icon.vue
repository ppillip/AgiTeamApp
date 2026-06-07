<script>
import { h } from "vue";

// 인라인 SVG 아이콘 (24×24 viewBox). 의존성 없음. (프로토타입 ICON_PATHS 이식)
// path 문자열은 정적 신뢰 데이터이므로 innerHTML 사용 안전.
const ICON_PATHS = {
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/>',
  sparkle: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4"/><path d="M12 8.5 13 11l2.5 1L13 13l-1 2.5L11 13 8.5 12 11 11z"/>',
  flag: '<path d="M4 21V4h11l-1.2 3.2L15 11H4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  users: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M16 5.2a3.2 3.2 0 0 1 0 5.6M17.5 19a5.5 5.5 0 0 0-3-4.9"/>',
  send: '<path d="M21 4 3 11l6 2.2L11 20l3-5 7-11Z"/><path d="m9 13.2 5-4.7"/>',
  chevronDown: '<path d="m5 8 5 5 5-5"/>',
  chevronRight: '<path d="m8 5 5 5-5 5"/>',
  folder: '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.2H19a1.5 1.5 0 0 1 1.5 1.5V17a1.5 1.5 0 0 1-1.5 1.5H4.5A1.5 1.5 0 0 1 3 17z"/>',
  file: '<path d="M6 3h7l5 5v13H6z"/><path d="M13 3v5h5"/>',
  expand: '<path d="M14 4h6v6"/><path d="M20 4 13 11"/><path d="M10 20H4v-6"/><path d="M4 20l7-7"/>',
  arrowUpRight: '<path d="M7 17 17 7"/><path d="M8 7h9v9"/>',
  sort: '<path d="M7 4v16M7 4 4 7M7 4l3 3"/><path d="M17 20V4M17 20l3-3M17 20l-3-3"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  message: '<path d="M21 12a8 8 0 0 1-11.5 7.2L4 20.5l1.3-5.5A8 8 0 1 1 21 12Z"/>',
  layout: '<rect x="3.5" y="3.5" width="17" height="17" rx="2"/><path d="M3.5 9h17M9 9v11.5"/>',
  inbox: '<path d="M4 13h4l1.5 3h5L16 13h4"/><path d="M4 13 6 5h12l2 8v6H4z"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M21.5 12h-3M5.5 12h-3M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1M18.4 18.4l-2.1-2.1M7.7 7.7 5.6 5.6"/>',
  paperclip: '<path d="M20 11.5 12 19.5a4 4 0 0 1-5.7-5.7l8-8a2.6 2.6 0 0 1 3.7 3.7l-7.6 7.6a1.2 1.2 0 0 1-1.7-1.7l7-7"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  download: '<path d="M12 4v11M8 11l4 4 4-4"/><path d="M5 19h14"/>',
  alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 4v5h-5"/>',
};

export default {
  name: "Icon",
  props: {
    name: { type: String, required: true },
    size: { type: [Number, String], default: 18 },
    stroke: { type: [Number, String], default: 1.8 },
  },
  render() {
    return h("svg", {
      width: this.size,
      height: this.size,
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": this.stroke,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      style: { flexShrink: 0, display: "block" },
      innerHTML: ICON_PATHS[this.name] || "",
    });
  },
};
</script>
