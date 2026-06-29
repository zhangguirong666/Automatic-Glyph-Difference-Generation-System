from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：当前目录没有 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_proxy_skeleton_v5")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

ROUTE_MARK = "# ===== ELASTIC_SKELETON_EDITOR_V5_PROXY_ROUTE ====="
REDIRECT_MARK = "# ===== REDIRECT_ELASTIC_EDITOR_TO_V5_PROXY ====="

if ROUTE_MARK not in text:
    route_code = r'''
# ===== ELASTIC_SKELETON_EDITOR_V5_PROXY_ROUTE =====
from fastapi.responses import HTMLResponse as _ElasticHTMLResponseV5

@app.get("/skeleton_elastic_editor_v5/{job_id}", response_class=_ElasticHTMLResponseV5)
async def skeleton_elastic_editor_v5(job_id: str):
    html = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>骨架编辑器｜整体弹性联动版 V5</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f3f4f6;font-family:Arial,"Microsoft YaHei",sans-serif;color:#111827}
header{height:72px;background:#111;color:white;padding:14px 22px}
header h1{margin:0;font-size:24px}
header p{margin:6px 0 0;color:#d1d5db;font-size:13px}
.app{height:calc(100vh - 72px);display:grid;grid-template-columns:220px 1fr 280px;gap:14px;padding:14px}
.panel,.main{background:white;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
.panel{padding:14px;overflow:auto}
.main{padding:14px;display:flex;flex-direction:column;overflow:hidden}
.title{font-weight:700;margin-bottom:10px}
.glyph-card{border:1px solid #e5e7eb;border-radius:8px;padding:10px;margin-bottom:9px;background:#fafafa;cursor:pointer}
.glyph-card.active{background:#2563eb;color:white;border-color:#2563eb}
.glyph-card small{display:block;margin-top:5px;opacity:.75}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
button{border:0;border-radius:7px;padding:8px 12px;background:#4b5563;color:white;cursor:pointer;font-size:13px}
button.primary{background:#2563eb}
button.warn{background:#d97706}
button.good{background:#059669}
select{height:32px;border:1px solid #d1d5db;border-radius:6px;padding:0 8px;min-width:180px}
.canvas-wrap{flex:1;position:relative;border:1px solid #e5e7eb;border-radius:10px;background:white;overflow:hidden}
canvas{width:100%;height:100%;display:block}
.help{font-size:12px;line-height:1.7;color:#4b5563;margin-top:9px}
.row{display:flex;gap:8px;align-items:center;font-size:13px;margin:8px 0}
.group{margin:16px 0}
.group label{display:flex;justify-content:space-between;font-weight:700;font-size:13px;margin-bottom:6px}
input[type=range]{width:100%}
.status,.note{font-size:12px;line-height:1.7;border-radius:8px;padding:10px}
.status{background:#f9fafb;border:1px solid #e5e7eb}
.note{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;margin-top:12px}
.err{color:#b91c1c;white-space:pre-wrap;font-size:12px}
</style>
</head>
<body>
<header>
  <h1>骨架编辑器｜整体弹性联动版</h1>
  <p>自动提取骨架中心线与关键控制点；拖动一个关键点时，按骨架拓扑距离、欧氏距离、边长保持和平滑约束，让整体字形一起协调变化。</p>
</header>

<div class="app">
  <aside class="panel">
    <div class="title">字形列表</div>
    <div id="glyphList">加载中...</div>
  </aside>

  <main class="main">
    <div class="toolbar">
      <button class="primary" id="saveBtn">保存当前字形</button>
      <button id="exportSvgBtn">导出 SVG</button>
      <button id="exportPngBtn">导出 PNG</button>
      <button id="smoothBtn">平滑控制点</button>
      <button class="warn" id="resetBtn">恢复当前版本</button>
      <button class="good" id="reextractBtn">重新提取骨架</button>
      <select id="variantSelect"></select>
    </div>

    <div class="canvas-wrap">
      <canvas id="canvas"></canvas>
    </div>

    <div class="help">
      操作：拖动白色关键控制点进行整体弹性变形；Ctrl + 点击关键点可固定 / 取消固定；Shift + 拖动增强整体联动；Alt + 拖动增强局部变形。
    </div>
  </main>

  <aside class="panel">
    <div class="title">显示控制</div>

    <div class="row"><input id="showRawSvg" type="checkbox" checked><label for="showRawSvg">显示原始完整轮廓</label></div>
    <div class="row"><input id="showPreview" type="checkbox" checked><label for="showPreview">显示变形后预览</label></div>
    <div class="row"><input id="showSkeleton" type="checkbox" checked><label for="showSkeleton">显示骨架中心线</label></div>
    <div class="row"><input id="showSupportPoints" type="checkbox"><label for="showSupportPoints">显示支撑点</label></div>
    <div class="row"><input id="showPoints" type="checkbox" checked><label for="showPoints">显示关键控制点</label></div>

    <hr>

    <div class="group">
      <label>整体联动强度 <span id="globalValue">0.72</span></label>
      <input id="globalStrength" type="range" min="0" max="1" step="0.01" value="0.72">
    </div>

    <div class="group">
      <label>局部影响半径 <span id="radiusValue">260</span></label>
      <input id="influenceRadius" type="range" min="40" max="800" step="1" value="260">
    </div>

    <div class="group">
      <label>结构保持强度 <span id="preserveValue">0.84</span></label>
      <input id="preserveStrength" type="range" min="0" max="1" step="0.01" value="0.84">
    </div>

    <div class="group">
      <label>平滑强度 <span id="smoothValue">0.055</span></label>
      <input id="smoothStrength" type="range" min="0" max="0.25" step="0.005" value="0.055">
    </div>

    <div class="group">
      <label>预览字形粗细 <span id="thickValue">28</span></label>
      <input id="strokeThickness" type="range" min="4" max="90" step="1" value="28">
    </div>

    <div class="group">
      <label>关键控制点数量 <span id="handleValue">8</span></label>
      <input id="handleCount" type="range" min="5" max="14" step="1" value="8">
    </div>

    <div class="status" id="statusBox"></div>

    <div class="note">
      说明：如果后端没有现成骨架数据，这个版本会自动从当前字形 SVG 中提取一条代理中心线，并从中选出少量关键控制点。拖一个点时，整体字形会协调联动，而不是只拉动局部。
    </div>
  </aside>
</div>

<script>
"use strict";

const JOB_ID = "__JOB_ID__";

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");

const ui = {
  glyphList: document.getElementById("glyphList"),
  variantSelect: document.getElementById("variantSelect"),
  showRawSvg: document.getElementById("showRawSvg"),
  showPreview: document.getElementById("showPreview"),
  showSkeleton: document.getElementById("showSkeleton"),
  showSupportPoints: document.getElementById("showSupportPoints"),
  showPoints: document.getElementById("showPoints"),
  globalStrength: document.getElementById("globalStrength"),
  influenceRadius: document.getElementById("influenceRadius"),
  preserveStrength: document.getElementById("preserveStrength"),
  smoothStrength: document.getElementById("smoothStrength"),
  strokeThickness: document.getElementById("strokeThickness"),
  handleCount: document.getElementById("handleCount"),
  globalValue: document.getElementById("globalValue"),
  radiusValue: document.getElementById("radiusValue"),
  preserveValue: document.getElementById("preserveValue"),
  smoothValue: document.getElementById("smoothValue"),
  thickValue: document.getElementById("thickValue"),
  handleValue: document.getElementById("handleValue"),
  statusBox: document.getElementById("statusBox")
};

const state = {
  manifest: [],
  activeIndex: 0,
  code: null,
  variant: null,
  basePoints: [],
  points: [],
  edges: [],
  handleIndices: [],
  rawSvgText: "",
  rawSvgImage: null,
  svgBounds: null,
  pinned: new Set(),
  drag: null,
  hover: -1,
  scale: 1,
  offsetX: 0,
  offsetY: 0,
  loaded: false,
  skeletonSource: "none"
};

function normalizeManifest(raw) {
  let arr = raw;
  if (!Array.isArray(arr)) {
    arr = raw.glyphs || raw.items || raw.chars || raw.manifest || raw.data || raw.results || [];
  }
  if (!Array.isArray(arr) && typeof arr === "object") {
    arr = Object.entries(arr).map(([code, value]) => {
      if (typeof value === "object") return {code, ...value};
      return {code, variants: value};
    });
  }

  arr = arr.map(x => {
    const code = x.code || x.unicode || x.char || x.name || x.glyph || x.id;
    let variants = x.variants || x.steps || x.versions || x.variant_list || x.files || [];
    if (!Array.isArray(variants)) variants = Object.keys(variants || {});
    variants = variants.map(v => {
      if (typeof v === "string") return v;
      return v.variant || v.step || v.name || v.id || v.key || "step_01";
    });
    if (!variants.length) variants = ["step_01"];
    return {code, variants};
  }).filter(x => x.code);

  return arr;
}

async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
  return await r.json();
}

async function fetchText(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
  return await r.text();
}

function toPoint(p) {
  if (!p) return null;
  if (Array.isArray(p) && p.length >= 2) return {x: Number(p[0]), y: Number(p[1])};
  if (typeof p === "object") {
    const x = Number(p.x ?? p.X ?? p.cx ?? p[0]);
    const y = Number(p.y ?? p.Y ?? p.cy ?? p[1]);
    if (Number.isFinite(x) && Number.isFinite(y)) return {x, y};
  }
  return null;
}

function normalizeSkeleton(data) {
  let points = [];
  let edges = [];

  if (Array.isArray(data?.points)) points = data.points;
  else if (Array.isArray(data?.control_points)) points = data.control_points;
  else if (Array.isArray(data?.skeleton_points)) points = data.skeleton_points;
  else if (data?.skeleton && Array.isArray(data.skeleton.points)) points = data.skeleton.points;
  else if (data?.skeleton && Array.isArray(data.skeleton.nodes)) points = data.skeleton.nodes;
  else if (Array.isArray(data?.nodes)) points = data.nodes;

  if (Array.isArray(data?.edges)) edges = data.edges;
  else if (Array.isArray(data?.skeleton_edges)) edges = data.skeleton_edges;
  else if (data?.skeleton && Array.isArray(data.skeleton.edges)) edges = data.skeleton.edges;

  let finalPoints = [];

  if (Array.isArray(data?.strokes) && !points.length) {
    for (const stroke of data.strokes) {
      const spts = stroke.points || stroke.nodes || stroke;
      if (!Array.isArray(spts)) continue;
      let last = -1;
      for (const p of spts) {
        const q = toPoint(p);
        if (!q) continue;
        finalPoints.push(q);
        const now = finalPoints.length - 1;
        if (last >= 0) edges.push([last, now]);
        last = now;
      }
    }
  } else {
    finalPoints = (points || []).map(toPoint).filter(Boolean);
  }

  edges = (edges || []).map(e => {
    if (Array.isArray(e)) return [Number(e[0]), Number(e[1])];
    return [Number(e.a ?? e.from ?? e.source ?? e.i), Number(e.b ?? e.to ?? e.target ?? e.j)];
  }).filter(e => Number.isFinite(e[0]) && Number.isFinite(e[1]) && e[0] >= 0 && e[1] >= 0 && e[0] < finalPoints.length && e[1] < finalPoints.length);

  if (!edges.length && finalPoints.length > 1) {
    for (let i = 0; i < finalPoints.length - 1; i++) edges.push([i, i + 1]);
  }

  return {points: finalPoints, edges};
}

function parseSvgBounds(svgText) {
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(svgText, "image/svg+xml");
    const svg = doc.documentElement;

    const vb = svg.getAttribute("viewBox");
    if (vb) {
      const nums = vb.trim().split(/[\s,]+/).map(Number);
      if (nums.length === 4 && nums.every(n => Number.isFinite(n))) {
        const [minX, minY, width, height] = nums;
        return {
          minX,
          minY,
          maxX: minX + width,
          maxY: minY + height,
          width,
          height,
          cx: minX + width / 2,
          cy: minY + height / 2
        };
      }
    }

    const w = parseFloat((svg.getAttribute("width") || "300").replace(/[a-z%]+/ig, ""));
    const h = parseFloat((svg.getAttribute("height") || "300").replace(/[a-z%]+/ig, ""));
    if (Number.isFinite(w) && Number.isFinite(h)) {
      return {
        minX: 0,
        minY: 0,
        maxX: w,
        maxY: h,
        width: w,
        height: h,
        cx: w / 2,
        cy: h / 2
      };
    }
  } catch (e) {
    console.warn("parseSvgBounds failed:", e);
  }

  return {
    minX: 0,
    minY: 0,
    maxX: 300,
    maxY: 300,
    width: 300,
    height: 300,
    cx: 150,
    cy: 150
  };
}

function svgToImage(svgText) {
  return new Promise((resolve, reject) => {
    const blob = new Blob([svgText], {type: "image/svg+xml"});
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = err => {
      URL.revokeObjectURL(url);
      reject(err);
    };
    img.src = url;
  });
}

async function init() {
  labels();
  bind();
  resize();

  try {
    const rawManifest = await fetchJson(`/elastic_skeleton_manifest/${JOB_ID}`);
    state.manifest = normalizeManifest(rawManifest);
    if (!state.manifest.length) throw new Error("manifest 为空，无法读取字形列表。");
    renderGlyphList();
    await selectGlyph(0);
  } catch (e) {
    ui.glyphList.innerHTML = `<div class="err">${e.stack || e}</div>`;
    ui.statusBox.innerHTML = `<div class="err">${e.stack || e}</div>`;
  }
}

function bind() {
  window.addEventListener("resize", () => { resize(); draw(); });

  canvas.addEventListener("pointerdown", pointerDown);
  canvas.addEventListener("pointermove", pointerMove);
  canvas.addEventListener("pointerup", pointerUp);
  canvas.addEventListener("pointerleave", pointerUp);

  document.getElementById("saveBtn").onclick = saveEdit;
  document.getElementById("exportSvgBtn").onclick = exportSVG;
  document.getElementById("exportPngBtn").onclick = exportPNG;
  document.getElementById("smoothBtn").onclick = smoothCurrent;
  document.getElementById("resetBtn").onclick = () => {
    state.points = clone(state.basePoints);
    state.pinned.clear();
    draw();
  };
  document.getElementById("reextractBtn").onclick = () => {
    if (state.rawSvgImage && state.svgBounds) {
      const proxy = generateProxySkeletonFromSvg(state.rawSvgImage, state.svgBounds, Number(ui.handleCount.value));
      state.basePoints = clone(proxy.points);
      state.points = clone(proxy.points);
      state.edges = proxy.edges.map(e => [...e]);
      state.handleIndices = [...proxy.handleIndices];
      state.pinned.clear();
      state.skeletonSource = "proxy-svg";
      fit();
      draw();
    }
  };

  ui.variantSelect.onchange = async () => {
    state.variant = ui.variantSelect.value;
    await loadGlyphData();
  };

  [
    ui.showRawSvg, ui.showPreview, ui.showSkeleton, ui.showSupportPoints, ui.showPoints,
    ui.globalStrength, ui.influenceRadius, ui.preserveStrength, ui.smoothStrength,
    ui.strokeThickness, ui.handleCount
  ].forEach(el => el.addEventListener("input", () => { labels(); draw(); }));
}

function labels() {
  ui.globalValue.textContent = Number(ui.globalStrength.value).toFixed(2);
  ui.radiusValue.textContent = ui.influenceRadius.value;
  ui.preserveValue.textContent = Number(ui.preserveStrength.value).toFixed(2);
  ui.smoothValue.textContent = Number(ui.smoothStrength.value).toFixed(3);
  ui.thickValue.textContent = ui.strokeThickness.value;
  ui.handleValue.textContent = ui.handleCount.value;
}

function resize() {
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(300, Math.floor(rect.width * dpr));
  canvas.height = Math.max(300, Math.floor(rect.height * dpr));
  canvas.style.width = rect.width + "px";
  canvas.style.height = rect.height + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  fit();
}

function renderGlyphList() {
  ui.glyphList.innerHTML = "";
  state.manifest.forEach((g, i) => {
    const div = document.createElement("div");
    div.className = "glyph-card" + (i === state.activeIndex ? " active" : "");
    div.innerHTML = `<b>${g.code}</b><small>${g.variants.length} 个版本</small>`;
    div.onclick = () => selectGlyph(i);
    ui.glyphList.appendChild(div);
  });
}

async function selectGlyph(i) {
  state.activeIndex = i;
  const item = state.manifest[i];
  state.code = item.code;

  ui.variantSelect.innerHTML = "";
  item.variants.forEach(v => {
    const op = document.createElement("option");
    op.value = v;
    op.textContent = v;
    ui.variantSelect.appendChild(op);
  });

  state.variant = item.variants[0] || "step_01";
  ui.variantSelect.value = state.variant;

  renderGlyphList();
  await loadGlyphData();
}

async function loadGlyphData() {
  state.loaded = false;
  state.pinned.clear();
  state.points = [];
  state.basePoints = [];
  state.edges = [];
  state.handleIndices = [];

  let norm = {points: [], edges: []};

  try {
    const data = await fetchJson(`/skeleton_json/${JOB_ID}/${state.code}/${state.variant}`);
    norm = normalizeSkeleton(data);
  } catch (e) {
    console.warn("skeleton_json load failed:", e);
  }

  try {
    state.rawSvgText = await fetchText(`/raw_svg_variant/${JOB_ID}/${state.code}/${state.variant}`);
    state.rawSvgImage = await svgToImage(state.rawSvgText);
    state.svgBounds = parseSvgBounds(state.rawSvgText);
  } catch (e) {
    state.rawSvgText = "";
    state.rawSvgImage = null;
    state.svgBounds = null;
  }

  if (norm.points.length >= 4) {
    state.basePoints = clone(norm.points);
    state.points = clone(norm.points);
    state.edges = norm.edges.map(e => [...e]);
    state.handleIndices = selectKeyHandles(state.points, Number(ui.handleCount.value));
    state.skeletonSource = "skeleton-json";
  } else if (state.rawSvgImage && state.svgBounds) {
    const proxy = generateProxySkeletonFromSvg(state.rawSvgImage, state.svgBounds, Number(ui.handleCount.value));
    state.basePoints = clone(proxy.points);
    state.points = clone(proxy.points);
    state.edges = proxy.edges.map(e => [...e]);
    state.handleIndices = [...proxy.handleIndices];
    state.skeletonSource = "proxy-svg";
  } else {
    state.basePoints = [];
    state.points = [];
    state.edges = [];
    state.handleIndices = [];
    state.skeletonSource = "none";
  }

  state.loaded = true;
  fit();
  draw();
}

function generateProxySkeletonFromSvg(img, svgBounds, desiredHandles=8) {
  const w = 900;
  const h = Math.max(220, Math.round(w * (svgBounds.height / Math.max(1, svgBounds.width))));

  const off = document.createElement("canvas");
  off.width = w;
  off.height = h;
  const octx = off.getContext("2d");
  octx.clearRect(0, 0, w, h);
  octx.drawImage(img, 0, 0, w, h);

  const data = octx.getImageData(0, 0, w, h).data;

  function alphaAt(x, y) {
    const idx = (y * w + x) * 4 + 3;
    return data[idx];
  }

  let mids = [];

  for (let x = 0; x < w; x++) {
    let minY = -1;
    let maxY = -1;

    for (let y = 0; y < h; y++) {
      if (alphaAt(x, y) > 12) {
        minY = y;
        break;
      }
    }
    if (minY < 0) continue;

    for (let y = h - 1; y >= 0; y--) {
      if (alphaAt(x, y) > 12) {
        maxY = y;
        break;
      }
    }
    if (maxY < 0 || maxY < minY) continue;

    mids.push({
      x,
      y: (minY + maxY) / 2,
      t: maxY - minY
    });
  }

  if (mids.length < 8) {
    const pts = [
      {x: svgBounds.minX + svgBounds.width * 0.05, y: svgBounds.minY + svgBounds.height * 0.35},
      {x: svgBounds.minX + svgBounds.width * 0.18, y: svgBounds.minY + svgBounds.height * 0.25},
      {x: svgBounds.minX + svgBounds.width * 0.34, y: svgBounds.minY + svgBounds.height * 0.40},
      {x: svgBounds.minX + svgBounds.width * 0.50, y: svgBounds.minY + svgBounds.height * 0.55},
      {x: svgBounds.minX + svgBounds.width * 0.68, y: svgBounds.minY + svgBounds.height * 0.45},
      {x: svgBounds.minX + svgBounds.width * 0.85, y: svgBounds.minY + svgBounds.height * 0.60}
    ];
    const edges = [];
    for (let i = 0; i < pts.length - 1; i++) edges.push([i, i + 1]);
    return {
      points: pts,
      edges,
      handleIndices: selectKeyHandles(pts, desiredHandles)
    };
  }

  mids = smoothPolyline(mids, 2, 5);

  const supportCount = Math.max(16, Math.min(28, Math.round(mids.length / 35)));
  const sampled = [];

  for (let i = 0; i < supportCount; i++) {
    const t = supportCount === 1 ? 0 : i / (supportCount - 1);
    const idx = Math.max(0, Math.min(mids.length - 1, Math.round(t * (mids.length - 1))));
    const p = mids[idx];
    sampled.push({
      x: svgBounds.minX + (p.x / w) * svgBounds.width,
      y: svgBounds.minY + (p.y / h) * svgBounds.height
    });
  }

  const cleaned = [];
  for (const p of sampled) {
    if (!cleaned.length || dist(cleaned[cleaned.length - 1], p) > 1.5) {
      cleaned.push(p);
    }
  }

  const edges = [];
  for (let i = 0; i < cleaned.length - 1; i++) edges.push([i, i + 1]);

  return {
    points: cleaned,
    edges,
    handleIndices: selectKeyHandles(cleaned, desiredHandles)
  };
}

function smoothPolyline(points, rounds=2, window=5) {
  let arr = points.map(p => ({...p}));
  const half = Math.floor(window / 2);

  for (let r = 0; r < rounds; r++) {
    const next = arr.map((p, i) => {
      if (i === 0 || i === arr.length - 1) return {...p};
      let sx = 0, sy = 0, c = 0;
      for (let k = -half; k <= half; k++) {
        const j = i + k;
        if (j < 0 || j >= arr.length) continue;
        sx += arr[j].x;
        sy += arr[j].y;
        c++;
      }
      return {x: sx / c, y: sy / c, t: p.t};
    });
    arr = next;
  }
  return arr;
}

function selectKeyHandles(points, desired=8) {
  if (!points.length) return [];
  if (points.length <= desired) return points.map((_, i) => i);

  const selected = new Set([0, points.length - 1]);

  const curvatures = [];
  for (let i = 1; i < points.length - 1; i++) {
    const a = points[i - 1];
    const b = points[i];
    const c = points[i + 1];

    const v1x = b.x - a.x, v1y = b.y - a.y;
    const v2x = c.x - b.x, v2y = c.y - b.y;

    const n1 = Math.hypot(v1x, v1y);
    const n2 = Math.hypot(v2x, v2y);
    if (n1 < 1e-6 || n2 < 1e-6) continue;

    const dot = (v1x * v2x + v1y * v2y) / (n1 * n2);
    const ang = Math.acos(Math.max(-1, Math.min(1, dot)));
    curvatures.push({i, ang});
  }

  curvatures.sort((a, b) => b.ang - a.ang);

  const curvaturePick = Math.min(Math.max(2, Math.floor(desired / 2)), curvatures.length);
  for (let k = 0; k < curvaturePick; k++) selected.add(curvatures[k].i);

  while (selected.size < desired) {
    const step = (points.length - 1) / (desired - 1);
    for (let k = 0; k < desired && selected.size < desired; k++) {
      selected.add(Math.round(k * step));
    }
  }

  return Array.from(selected).sort((a, b) => a - b);
}

function fit() {
  const r = canvas.getBoundingClientRect();
  const pad = 90;

  let b = null;
  if (state.points && state.points.length) b = bounds(state.points);
  else if (state.svgBounds) b = state.svgBounds;
  else {
    state.scale = 1;
    state.offsetX = r.width / 2;
    state.offsetY = r.height / 2;
    return;
  }

  const sx = (r.width - pad * 2) / Math.max(1, b.width);
  const sy = (r.height - pad * 2) / Math.max(1, b.height);
  state.scale = Math.min(sx, sy);
  state.offsetX = r.width / 2 - b.cx * state.scale;
  state.offsetY = r.height / 2 - b.cy * state.scale;
}

function pointerDown(e) {
  if (!state.loaded || !state.points.length) return;
  const m = screenToWorld(mouse(e));
  const hit = nearestHandle(m, state.points, state.handleIndices, 18 / state.scale);
  if (hit < 0) return;

  if (e.ctrlKey || e.metaKey) {
    state.pinned.has(hit) ? state.pinned.delete(hit) : state.pinned.add(hit);
    draw();
    return;
  }

  canvas.setPointerCapture(e.pointerId);
  state.drag = {
    handle: hit,
    basePoints: clone(state.points),
    basePinned: new Set(state.pinned)
  };
}

function pointerMove(e) {
  if (!state.loaded) return;
  const m = screenToWorld(mouse(e));
  state.hover = nearestHandle(m, state.points, state.handleIndices, 18 / state.scale);

  if (!state.drag) {
    draw();
    return;
  }

  let globalStrength = Number(ui.globalStrength.value);
  let influenceRadius = Number(ui.influenceRadius.value);

  if (e.shiftKey) {
    globalStrength = Math.min(1, globalStrength + 0.20);
    influenceRadius *= 1.45;
  }

  if (e.altKey) {
    globalStrength = Math.max(0.05, globalStrength - 0.25);
    influenceRadius *= 0.60;
  }

  state.points = elasticDeform({
    basePoints: state.drag.basePoints,
    edges: state.edges,
    handleIndex: state.drag.handle,
    target: m,
    pinned: state.drag.basePinned,
    globalStrength,
    influenceRadius,
    preserveStrength: Number(ui.preserveStrength.value),
    smoothStrength: Number(ui.smoothStrength.value),
    preserveIterations: 22,
    smoothIterations: 4
  });

  draw();
}

function pointerUp() {
  state.drag = null;
}

function elasticDeform(o) {
  const handle = o.basePoints[o.handleIndex];
  const dx = o.target.x - handle.x;
  const dy = o.target.y - handle.y;
  const gd = graphDistances(o.basePoints, o.edges, o.handleIndex);

  const fixed = new Set(o.pinned);
  fixed.add(o.handleIndex);

  let pts = o.basePoints.map((p, i) => {
    if (o.pinned.has(i)) return {...p};

    let w = influenceWeight({
      p,
      handle,
      graphDistance: gd[i],
      radius: o.influenceRadius,
      globalStrength: o.globalStrength
    });

    if (i === o.handleIndex) w = 1;

    return {
      x: p.x + dx * w,
      y: p.y + dy * w
    };
  });

  pts[o.handleIndex] = {...o.target};

  pts = preserveEdgeLengths({
    points: pts,
    basePoints: o.basePoints,
    edges: o.edges,
    fixed,
    strength: o.preserveStrength,
    iterations: o.preserveIterations
  });

  pts = laplacianSmooth({
    points: pts,
    edges: o.edges,
    fixed,
    strength: o.smoothStrength,
    iterations: o.smoothIterations
  });

  pts[o.handleIndex] = {...o.target};

  return pts;
}

function influenceWeight({p, handle, graphDistance, radius, globalStrength}) {
  const euclid = dist(p, handle);
  let topoWeight = 0;

  if (Number.isFinite(graphDistance)) {
    const t = clamp(1 - graphDistance / radius, 0, 1);
    topoWeight = t * t * (3 - 2 * t);
  }

  const euclidWeight = Math.exp(-(euclid * euclid) / (2 * radius * radius));
  let w = Math.max(topoWeight, euclidWeight * 0.68);

  // 让整体始终带一点联动
  w = w * globalStrength + 0.12 * globalStrength;

  return clamp(w, 0, 1);
}

function graphDistances(points, edges, start) {
  const n = points.length;
  const adj = Array.from({length: n}, () => []);

  for (const [a, b] of edges) {
    const l = dist(points[a], points[b]);
    adj[a].push([b, l]);
    adj[b].push([a, l]);
  }

  const d = Array(n).fill(Infinity);
  const used = Array(n).fill(false);
  d[start] = 0;

  for (let k = 0; k < n; k++) {
    let u = -1;
    let best = Infinity;

    for (let i = 0; i < n; i++) {
      if (!used[i] && d[i] < best) {
        best = d[i];
        u = i;
      }
    }
    if (u < 0) break;

    used[u] = true;

    for (const [v, w] of adj[u]) {
      if (d[u] + w < d[v]) d[v] = d[u] + w;
    }
  }

  return d;
}

function preserveEdgeLengths({points, basePoints, edges, fixed, strength, iterations}) {
  let pts = clone(points);
  const rest = edges.map(([a, b]) => dist(basePoints[a], basePoints[b]));

  for (let it = 0; it < iterations; it++) {
    for (let i = 0; i < edges.length; i++) {
      const [a, b] = edges[i];
      const pa = pts[a];
      const pb = pts[b];

      const vx = pb.x - pa.x;
      const vy = pb.y - pa.y;
      const len = Math.hypot(vx, vy);
      if (len < 0.0001) continue;

      const diff = (len - rest[i]) / len;
      const cx = vx * diff * 0.5 * strength;
      const cy = vy * diff * 0.5 * strength;

      const fa = fixed.has(a);
      const fb = fixed.has(b);

      if (!fa && !fb) {
        pts[a].x += cx; pts[a].y += cy;
        pts[b].x -= cx; pts[b].y -= cy;
      } else if (fa && !fb) {
        pts[b].x -= cx * 2; pts[b].y -= cy * 2;
      } else if (!fa && fb) {
        pts[a].x += cx * 2; pts[a].y += cy * 2;
      }
    }
  }

  return pts;
}

function laplacianSmooth({points, edges, fixed, strength, iterations}) {
  const n = points.length;
  const adj = Array.from({length: n}, () => []);

  for (const [a, b] of edges) {
    adj[a].push(b);
    adj[b].push(a);
  }

  let pts = clone(points);

  for (let it = 0; it < iterations; it++) {
    const next = clone(pts);

    for (let i = 0; i < n; i++) {
      if (fixed.has(i) || !adj[i].length) continue;

      let ax = 0, ay = 0;
      for (const j of adj[i]) {
        ax += pts[j].x;
        ay += pts[j].y;
      }

      ax /= adj[i].length;
      ay /= adj[i].length;

      next[i].x = pts[i].x * (1 - strength) + ax * strength;
      next[i].y = pts[i].y * (1 - strength) + ay * strength;
    }

    pts = next;
  }

  return pts;
}

function smoothCurrent() {
  const fixed = new Set(state.pinned);
  state.points = laplacianSmooth({
    points: state.points,
    edges: state.edges,
    fixed,
    strength: 0.12,
    iterations: 8
  });
  draw();
}

async function saveEdit() {
  const body = {
    points: state.points,
    edges: state.edges,
    handle_indices: state.handleIndices,
    skeleton_source: state.skeletonSource,
    skeleton: {
      points: state.points,
      edges: state.edges
    },
    control_points: state.points,
    pinned: Array.from(state.pinned)
  };

  let r = await fetch(`/save_skeleton_edit/${JOB_ID}/${state.code}/${state.variant}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });

  if (!r.ok) {
    alert("保存失败：HTTP " + r.status + "\\n请查看后端 save_skeleton_edit 接口接收格式。");
    return;
  }

  alert("已保存：" + state.code + " / " + state.variant);
}

function draw() {
  const rect = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);

  ctx.save();
  ctx.translate(state.offsetX, state.offsetY);
  ctx.scale(state.scale, state.scale);

  if (ui.showRawSvg.checked && state.rawSvgImage) {
    const b = state.svgBounds || {minX:0, minY:0, width:300, height:300};
    ctx.save();
    ctx.globalAlpha = 0.18;
    ctx.drawImage(state.rawSvgImage, b.minX, b.minY, Math.max(1, b.width), Math.max(1, b.height));
    ctx.restore();
  }

  if (ui.showPreview.checked && state.points.length) {
    drawThick(state.points, state.edges, Number(ui.strokeThickness.value), "rgba(222,184,135,0.55)");
  }

  if (ui.showSkeleton.checked && state.points.length) {
    drawLines(state.points, state.edges, "#ff5f5f", 2.6 / state.scale);
  }

  if (ui.showSupportPoints.checked && state.points.length) {
    drawSupportPoints();
  }

  if (ui.showPoints.checked && state.handleIndices.length) {
    drawHandles();
  }

  ctx.restore();
  updateStatus();
}

function drawThick(points, edges, width, color) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  for (const [a, b] of edges) {
    const p1 = points[a];
    const p2 = points[b];
    if (!p1 || !p2) continue;
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
  }

  ctx.restore();
}

function drawLines(points, edges, color, width) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  for (const [a, b] of edges) {
    const p1 = points[a];
    const p2 = points[b];
    if (!p1 || !p2) continue;
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
  }

  ctx.restore();
}

function drawSupportPoints() {
  const r = 2.3 / state.scale;
  ctx.save();
  for (let i = 0; i < state.points.length; i++) {
    const p = state.points[i];
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(239,68,68,0.45)";
    ctx.fill();
  }
  ctx.restore();
}

function drawHandles() {
  const r = 6.2 / state.scale;

  for (const idx of state.handleIndices) {
    const p = state.points[idx];
    if (!p) continue;

    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);

    if (state.pinned.has(idx)) {
      ctx.fillStyle = "#2563eb";
      ctx.strokeStyle = "#ffffff";
    } else if (idx === state.hover) {
      ctx.fillStyle = "#facc15";
      ctx.strokeStyle = "#111827";
    } else {
      ctx.fillStyle = "#ffffff";
      ctx.strokeStyle = "#ef4444";
    }

    ctx.lineWidth = 2.0 / state.scale;
    ctx.fill();
    ctx.stroke();
  }
}

function updateStatus() {
  ui.statusBox.innerHTML =
    `Job ID：${JOB_ID}<br>` +
    `当前字形：${state.code || "-"}<br>` +
    `当前版本：${state.variant || "-"}<br>` +
    `骨架来源：${state.skeletonSource}<br>` +
    `支撑点：${state.points.length}<br>` +
    `关键控制点：${state.handleIndices.length}<br>` +
    `边数量：${state.edges.length}<br>` +
    `固定点：${state.pinned.size}<br>` +
    `状态：整体弹性联动已启用`;
}

function exportSVG() {
  const b = bounds(state.points.length ? state.points : [{x:0, y:0}, {x:300, y:300}]);
  const pad = 80;
  const w = b.width + pad * 2;
  const h = b.height + pad * 2;
  const ox = pad - b.minX;
  const oy = pad - b.minY;
  const thick = Number(ui.strokeThickness.value);

  let lines = "";

  for (const [a, b2] of state.edges) {
    const p1 = state.points[a];
    const p2 = state.points[b2];
    if (!p1 || !p2) continue;
    lines += `<line x1="${p1.x + ox}" y1="${p1.y + oy}" x2="${p2.x + ox}" y2="${p2.y + oy}" stroke="#d6a46a" stroke-width="${thick}" stroke-linecap="round" stroke-linejoin="round"/>\\n`;
  }

  for (const idx of state.handleIndices) {
    const p = state.points[idx];
    if (!p) continue;
    lines += `<circle cx="${p.x + ox}" cy="${p.y + oy}" r="4" fill="white" stroke="#ef4444" stroke-width="1.5"/>\\n`;
  }

  const svg = `<?xml version="1.0" encoding="UTF-8"?><svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" xmlns="http://www.w3.org/2000/svg"><rect width="100%" height="100%" fill="white"/>${lines}</svg>`;
  downloadText(`${state.code}_${state.variant}_elastic_v5.svg`, svg);
}

function exportPNG() {
  const a = document.createElement("a");
  a.download = `${state.code}_${state.variant}_elastic_v5.png`;
  a.href = canvas.toDataURL("image/png");
  a.click();
}

function downloadText(name, text) {
  const blob = new Blob([text], {type: "text/plain;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function mouse(e) {
  const rect = canvas.getBoundingClientRect();
  return {x: e.clientX - rect.left, y: e.clientY - rect.top};
}

function screenToWorld(p) {
  return {x: (p.x - state.offsetX) / state.scale, y: (p.y - state.offsetY) / state.scale};
}

function nearestHandle(m, points, handleIndices, th) {
  let best = -1;
  let bd = th;
  for (const idx of handleIndices) {
    const d = dist(m, points[idx]);
    if (d < bd) {
      bd = d;
      best = idx;
    }
  }
  return best;
}

function bounds(points) {
  if (!points.length) return {minX:0, minY:0, maxX:1, maxY:1, width:1, height:1, cx:0.5, cy:0.5};

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const p of points) {
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x);
    maxY = Math.max(maxY, p.y);
  }
  return {
    minX, minY, maxX, maxY,
    width: maxX - minX,
    height: maxY - minY,
    cx: (minX + maxX) / 2,
    cy: (minY + maxY) / 2
  };
}

function clone(points) {
  return points.map(p => ({x: Number(p.x), y: Number(p.y)}));
}

function dist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}

init();
</script>
</body>
</html>
"""
    return html.replace("__JOB_ID__", job_id)
# ===== END_ELASTIC_SKELETON_EDITOR_V5_PROXY_ROUTE =====
'''
    text = text.rstrip() + "\n\n" + route_code + "\n"
    print("已追加 /skeleton_elastic_editor_v5/{job_id} 路由。")
else:
    print("V5 路由已存在，不重复追加。")

if REDIRECT_MARK not in text:
    redirect_code = r'''

# ===== REDIRECT_ELASTIC_EDITOR_TO_V5_PROXY =====
from starlette.responses import RedirectResponse as _ElasticV5RedirectResponse

@app.middleware("http")
async def _redirect_elastic_editor_to_v5_proxy(request, call_next):
    path = request.url.path

    if path.startswith("/skeleton_elastic_editor/") and not path.startswith("/skeleton_elastic_editor_v5/"):
        suffix = path[len("/skeleton_elastic_editor/"):]
        target = "/skeleton_elastic_editor_v5/" + suffix
        if request.url.query:
            target += "?" + request.url.query
        return _ElasticV5RedirectResponse(url=target, status_code=302)

    return await call_next(request)

# ===== END_REDIRECT_ELASTIC_EDITOR_TO_V5_PROXY =====
'''
    text = text.rstrip() + "\n\n" + redirect_code + "\n"
    print("已添加 /skeleton_elastic_editor/* -> /skeleton_elastic_editor_v5/* 自动跳转。")
else:
    print("V5 跳转中间件已存在，不重复添加。")

APP.write_text(text, encoding="utf-8")
print("补丁写入完成。")
