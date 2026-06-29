from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_svg_path_real_editor")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

MARK = "# ===== SVG_PATH_REAL_GLYPH_EDITOR_V1 ====="

if MARK in text:
    print("已经安装过 SVG 真实轮廓编辑器，不重复添加。")
else:
    code = r'''
# ===== SVG_PATH_REAL_GLYPH_EDITOR_V1 =====
from fastapi import Request as _SvgEditRequest
from fastapi.responses import HTMLResponse as _SvgEditHTMLResponse
from fastapi.responses import Response as _SvgEditResponse
from fastapi.responses import JSONResponse as _SvgEditJSONResponse
from starlette.responses import RedirectResponse as _SvgEditRedirectResponse
from pathlib import Path as _SvgEditPath
import json as _svg_edit_json
import re as _svg_edit_re

@app.get("/svg_path_editor/{job_id}", response_class=_SvgEditHTMLResponse)
async def svg_path_real_editor(job_id: str):
    html = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>真实字形轮廓编辑器</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f3f4f6;font-family:Arial,"Microsoft YaHei",sans-serif;color:#111827}
header{height:72px;background:#111;color:#fff;padding:14px 22px}
header h1{margin:0;font-size:24px}
header p{margin:6px 0 0;font-size:13px;color:#d1d5db}
.app{height:calc(100vh - 72px);display:grid;grid-template-columns:220px 1fr 285px;gap:14px;padding:14px}
.panel,.main{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
.panel{padding:14px;overflow:auto}
.main{padding:14px;display:flex;flex-direction:column;overflow:hidden}
.title{font-weight:700;margin-bottom:10px}
.card{border:1px solid #e5e7eb;border-radius:8px;padding:10px;margin-bottom:9px;background:#fafafa;cursor:pointer}
.card.active{background:#2563eb;color:white;border-color:#2563eb}
.card small{display:block;margin-top:5px;opacity:.75}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
button{border:0;border-radius:7px;padding:8px 12px;background:#4b5563;color:white;cursor:pointer;font-size:13px}
button.primary{background:#2563eb}
button.green{background:#059669}
button.warn{background:#d97706}
select{height:32px;border:1px solid #d1d5db;border-radius:6px;padding:0 8px;min-width:150px}
.stage-wrap{flex:1;border:1px solid #e5e7eb;border-radius:10px;background:white;overflow:hidden;position:relative}
svg#stage{width:100%;height:100%;display:block;background:white}
.help{font-size:12px;color:#4b5563;line-height:1.7;margin-top:8px}
.row{display:flex;gap:8px;align-items:center;font-size:13px;margin:8px 0}
.group{margin:16px 0}
.group label{display:flex;justify-content:space-between;font-size:13px;font-weight:700;margin-bottom:6px}
input[type=range]{width:100%}
.status,.note{font-size:12px;line-height:1.7;border-radius:8px;padding:10px}
.status{background:#f9fafb;border:1px solid #e5e7eb}
.note{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;margin-top:12px}
.err{color:#b91c1c;font-size:12px;white-space:pre-wrap}
</style>
</head>
<body>
<header>
  <h1>真实字形轮廓编辑器</h1>
  <p>直接编辑 SVG 字形轮廓控制点。拖动红色 / 蓝色控制点，字形轮廓会立即变化。</p>
</header>

<div class="app">
  <aside class="panel">
    <div class="title">字形列表</div>
    <div id="glyphList">加载中...</div>
  </aside>

  <main class="main">
    <div class="toolbar">
      <button class="primary" id="saveBtn">保存 SVG 编辑</button>
      <button class="green" id="loadSavedBtn">读取已保存</button>
      <button id="downloadSvgBtn">下载 SVG</button>
      <button class="warn" id="reloadRawBtn">恢复原始 SVG</button>
      <select id="variantSelect"></select>
    </div>

    <div class="stage-wrap">
      <svg id="stage" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>

    <div class="help">
      操作：拖动红色点编辑轮廓端点；拖动蓝色点编辑 Bézier 控制点；滚轮缩放；按住鼠标中键或右键拖动画布。
    </div>
  </main>

  <aside class="panel">
    <div class="title">控制</div>

    <div class="row"><input type="checkbox" id="showPoints" checked><label for="showPoints">显示控制点</label></div>
    <div class="row"><input type="checkbox" id="showControls" checked><label for="showControls">显示控制线</label></div>

    <hr>

    <div class="group">
      <label>控制点大小 <span id="pointSizeValue">4</span></label>
      <input id="pointSize" type="range" min="2" max="10" step="1" value="4">
    </div>

    <div class="group">
      <label>字形透明度 <span id="opacityValue">0.75</span></label>
      <input id="glyphOpacity" type="range" min="0.2" max="1" step="0.05" value="0.75">
    </div>

    <div class="status" id="statusBox"></div>

    <div class="note">
      这个版本不再做骨架提取，也不做图片网格假变形，而是直接修改 SVG path 的 d 数据。它就是可编辑字形轮廓的版本。
    </div>
  </aside>
</div>

<script>
"use strict";

const JOB_ID = "__JOB_ID__";
const NS = "http://www.w3.org/2000/svg";

const stage = document.getElementById("stage");

const ui = {
  glyphList: document.getElementById("glyphList"),
  variantSelect: document.getElementById("variantSelect"),
  saveBtn: document.getElementById("saveBtn"),
  loadSavedBtn: document.getElementById("loadSavedBtn"),
  downloadSvgBtn: document.getElementById("downloadSvgBtn"),
  reloadRawBtn: document.getElementById("reloadRawBtn"),
  showPoints: document.getElementById("showPoints"),
  showControls: document.getElementById("showControls"),
  pointSize: document.getElementById("pointSize"),
  pointSizeValue: document.getElementById("pointSizeValue"),
  glyphOpacity: document.getElementById("glyphOpacity"),
  opacityValue: document.getElementById("opacityValue"),
  statusBox: document.getElementById("statusBox")
};

const state = {
  manifest: [],
  activeIndex: 0,
  code: null,
  variant: null,
  rawSvgText: "",
  viewBox: [0,0,1000,1000],
  zoom: 1,
  panX: 0,
  panY: 0,
  paths: [],
  handles: [],
  drag: null,
  panning: null
};

function labels() {
  ui.pointSizeValue.textContent = ui.pointSize.value;
  ui.opacityValue.textContent = ui.glyphOpacity.value;
}

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

  return arr.map(x => {
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
}

function fallbackManifest() {
  const out = [];
  for (let cp = 0x1820; cp <= 0x1842; cp++) {
    const code = "U" + cp.toString(16).toUpperCase().padStart(4, "0");
    const variants = [];
    for (let i = 1; i <= 20; i++) variants.push("step_" + String(i).padStart(2, "0"));
    out.push({code, variants});
  }
  return out;
}

async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " HTTP " + r.status);
  return await r.json();
}

async function fetchText(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " HTTP " + r.status);
  return await r.text();
}

function tokenizePath(d) {
  return d.match(/[a-zA-Z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?/g) || [];
}

function isCmd(t) {
  return /^[a-zA-Z]$/.test(t);
}

const ARG_COUNT = {
  M:2, L:2, H:1, V:1, C:6, S:4, Q:4, T:2, A:7, Z:0
};

function parsePath(d) {
  const tokens = tokenizePath(d);
  let i = 0;
  let cmd = null;
  let cx = 0, cy = 0;
  let sx = 0, sy = 0;
  const segs = [];

  function num() {
    return Number(tokens[i++]);
  }

  while (i < tokens.length) {
    if (isCmd(tokens[i])) {
      cmd = tokens[i++];
    }

    if (!cmd) break;

    const upper = cmd.toUpperCase();
    const rel = cmd !== upper;

    if (upper === "Z") {
      segs.push({cmd:"Z", vals:[]});
      cx = sx;
      cy = sy;
      cmd = null;
      continue;
    }

    const n = ARG_COUNT[upper];
    if (!n) break;

    let firstM = upper === "M";

    while (i < tokens.length && !isCmd(tokens[i])) {
      if (i + n > tokens.length) break;

      let vals = [];
      for (let k = 0; k < n; k++) vals.push(num());

      let outCmd = upper;

      if (upper === "M") {
        let x = vals[0], y = vals[1];
        if (rel) { x += cx; y += cy; }
        cx = x; cy = y; sx = x; sy = y;
        segs.push({cmd: firstM ? "M" : "L", vals:[x,y]});
        firstM = false;
        outCmd = "L";
        continue;
      }

      if (upper === "L" || upper === "T") {
        let x = vals[0], y = vals[1];
        if (rel) { x += cx; y += cy; }
        segs.push({cmd: upper, vals:[x,y]});
        cx = x; cy = y;
        continue;
      }

      if (upper === "H") {
        let x = vals[0];
        if (rel) x += cx;
        segs.push({cmd:"L", vals:[x, cy]});
        cx = x;
        continue;
      }

      if (upper === "V") {
        let y = vals[0];
        if (rel) y += cy;
        segs.push({cmd:"L", vals:[cx, y]});
        cy = y;
        continue;
      }

      if (upper === "C") {
        if (rel) {
          vals[0]+=cx; vals[1]+=cy;
          vals[2]+=cx; vals[3]+=cy;
          vals[4]+=cx; vals[5]+=cy;
        }
        segs.push({cmd:"C", vals});
        cx = vals[4]; cy = vals[5];
        continue;
      }

      if (upper === "S" || upper === "Q") {
        if (rel) {
          vals[0]+=cx; vals[1]+=cy;
          vals[2]+=cx; vals[3]+=cy;
        }
        segs.push({cmd:upper, vals});
        cx = vals[2]; cy = vals[3];
        continue;
      }

      if (upper === "A") {
        if (rel) {
          vals[5] += cx;
          vals[6] += cy;
        }
        segs.push({cmd:"A", vals});
        cx = vals[5]; cy = vals[6];
        continue;
      }
    }
  }

  return segs;
}

function fmt(n) {
  return Number(n).toFixed(2).replace(/\.00$/,"").replace(/(\.\d)0$/,"$1");
}

function buildPath(segs) {
  return segs.map(s => {
    if (s.cmd === "Z") return "Z";
    return s.cmd + " " + s.vals.map(fmt).join(" ");
  }).join(" ");
}

function getHandlesForSegment(pathObj, segIndex, seg) {
  const hs = [];
  const c = seg.cmd;
  const v = seg.vals;

  function add(pairIndex, role) {
    hs.push({
      pathObj,
      segIndex,
      pairIndex,
      role,
      get x(){ return pathObj.segs[segIndex].vals[pairIndex]; },
      get y(){ return pathObj.segs[segIndex].vals[pairIndex+1]; },
      setXY(x,y){
        pathObj.segs[segIndex].vals[pairIndex] = x;
        pathObj.segs[segIndex].vals[pairIndex+1] = y;
        pathObj.el.setAttribute("d", buildPath(pathObj.segs));
      }
    });
  }

  if (c === "M" || c === "L" || c === "T") add(0, "anchor");
  else if (c === "C") {
    add(0, "control");
    add(2, "control");
    add(4, "anchor");
  } else if (c === "S" || c === "Q") {
    add(0, "control");
    add(2, "anchor");
  } else if (c === "A") {
    add(5, "anchor");
  }

  return hs;
}

function clearStage() {
  while (stage.firstChild) stage.removeChild(stage.firstChild);
  state.paths = [];
  state.handles = [];
}

function parseViewBox(svg) {
  const vb = svg.getAttribute("viewBox");
  if (vb) {
    const nums = vb.trim().split(/[\s,]+/).map(Number);
    if (nums.length === 4 && nums.every(Number.isFinite)) return nums;
  }

  const w = parseFloat((svg.getAttribute("width") || "1000").replace(/[a-z%]+/ig, ""));
  const h = parseFloat((svg.getAttribute("height") || "1000").replace(/[a-z%]+/ig, ""));
  return [0,0,w || 1000,h || 1000];
}

function loadSvgIntoStage(svgText) {
  clearStage();

  const doc = new DOMParser().parseFromString(svgText, "image/svg+xml");
  const rawSvg = doc.documentElement;

  state.viewBox = parseViewBox(rawSvg);
  stage.setAttribute("viewBox", state.viewBox.join(" "));

  const contentGroup = document.createElementNS(NS, "g");
  contentGroup.setAttribute("id", "glyphContent");
  stage.appendChild(contentGroup);

  const imported = document.importNode(rawSvg, true);

  const paths = Array.from(imported.querySelectorAll("path"));

  for (const oldPath of paths) {
    const d = oldPath.getAttribute("d");
    if (!d) continue;

    const segs = parsePath(d);
    const newPath = document.createElementNS(NS, "path");

    newPath.setAttribute("d", buildPath(segs));
    newPath.setAttribute("fill", "#6b7280");
    newPath.setAttribute("fill-opacity", ui.glyphOpacity.value);
    newPath.setAttribute("stroke", "none");

    contentGroup.appendChild(newPath);

    const pathObj = {el:newPath, segs};
    state.paths.push(pathObj);
  }

  if (!state.paths.length) {
    const text = document.createElementNS(NS, "text");
    text.setAttribute("x", state.viewBox[0] + 20);
    text.setAttribute("y", state.viewBox[1] + 40);
    text.setAttribute("fill", "red");
    text.textContent = "没有解析到 path，当前 SVG 可能不是 path 轮廓。";
    stage.appendChild(text);
  }

  rebuildHandles();
  updateStatus();
}

function rebuildHandles() {
  const old = stage.querySelector("#handleLayer");
  if (old) old.remove();

  const layer = document.createElementNS(NS, "g");
  layer.setAttribute("id", "handleLayer");
  stage.appendChild(layer);

  state.handles = [];

  for (const pathObj of state.paths) {
    pathObj.segs.forEach((seg, segIndex) => {
      const hs = getHandlesForSegment(pathObj, segIndex, seg);
      state.handles.push(...hs);
    });
  }

  if (ui.showControls.checked) {
    for (const h of state.handles) {
      if (h.role !== "control") continue;
      const seg = h.pathObj.segs[h.segIndex];
      let anchor = null;

      if (seg.cmd === "C") {
        anchor = h.pairIndex === 0
          ? previousAnchor(h.pathObj.segs, h.segIndex)
          : {x: seg.vals[4], y: seg.vals[5]};
      } else if (seg.cmd === "Q" || seg.cmd === "S") {
        anchor = {x: seg.vals[2], y: seg.vals[3]};
      }

      if (!anchor) continue;

      const line = document.createElementNS(NS, "line");
      line.setAttribute("x1", anchor.x);
      line.setAttribute("y1", anchor.y);
      line.setAttribute("x2", h.x);
      line.setAttribute("y2", h.y);
      line.setAttribute("stroke", "#60a5fa");
      line.setAttribute("stroke-opacity", "0.45");
      line.setAttribute("stroke-width", 1.2);
      line.setAttribute("vector-effect", "non-scaling-stroke");
      layer.appendChild(line);
    }
  }

  if (ui.showPoints.checked) {
    const r = Number(ui.pointSize.value);

    state.handles.forEach((h, idx) => {
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", h.x);
      c.setAttribute("cy", h.y);
      c.setAttribute("r", r);
      c.setAttribute("fill", h.role === "anchor" ? "#ffffff" : "#93c5fd");
      c.setAttribute("stroke", h.role === "anchor" ? "#ef4444" : "#2563eb");
      c.setAttribute("stroke-width", 1.5);
      c.setAttribute("vector-effect", "non-scaling-stroke");
      c.style.cursor = "move";
      c.dataset.handleIndex = idx;
      layer.appendChild(c);
    });
  }
}

function previousAnchor(segs, idx) {
  for (let i = idx - 1; i >= 0; i--) {
    const s = segs[i];
    if (s.cmd === "M" || s.cmd === "L" || s.cmd === "T") return {x:s.vals[0], y:s.vals[1]};
    if (s.cmd === "C") return {x:s.vals[4], y:s.vals[5]};
    if (s.cmd === "S" || s.cmd === "Q") return {x:s.vals[2], y:s.vals[3]};
    if (s.cmd === "A") return {x:s.vals[5], y:s.vals[6]};
  }
  return null;
}

function svgPoint(evt) {
  const pt = stage.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(stage.getScreenCTM().inverse());
}

stage.addEventListener("pointerdown", e => {
  if (e.button === 1 || e.button === 2) {
    state.panning = {
      x: e.clientX,
      y: e.clientY,
      viewBox: [...state.viewBox]
    };
    stage.setPointerCapture(e.pointerId);
    return;
  }

  const target = e.target;
  if (!target.dataset || target.dataset.handleIndex === undefined) return;

  const idx = Number(target.dataset.handleIndex);
  const h = state.handles[idx];

  if (!h) return;

  const p = svgPoint(e);

  state.drag = {
    handle: h,
    start: p,
    startX: h.x,
    startY: h.y
  };

  stage.setPointerCapture(e.pointerId);
});

stage.addEventListener("pointermove", e => {
  if (state.panning) {
    const vb = state.panning.viewBox;
    const dx = (e.clientX - state.panning.x) * vb[2] / stage.clientWidth;
    const dy = (e.clientY - state.panning.y) * vb[3] / stage.clientHeight;

    state.viewBox = [vb[0] - dx, vb[1] - dy, vb[2], vb[3]];
    stage.setAttribute("viewBox", state.viewBox.join(" "));
    return;
  }

  if (!state.drag) return;

  const p = svgPoint(e);
  const dx = p.x - state.drag.start.x;
  const dy = p.y - state.drag.start.y;

  state.drag.handle.setXY(state.drag.startX + dx, state.drag.startY + dy);
  rebuildHandles();
  updateStatus();
});

stage.addEventListener("pointerup", () => {
  state.drag = null;
  state.panning = null;
});

stage.addEventListener("pointerleave", () => {
  state.drag = null;
  state.panning = null;
});

stage.addEventListener("contextmenu", e => e.preventDefault());

stage.addEventListener("wheel", e => {
  e.preventDefault();

  const vb = [...state.viewBox];
  const factor = e.deltaY < 0 ? 0.9 : 1.1;

  const p = svgPoint(e);

  const newW = vb[2] * factor;
  const newH = vb[3] * factor;

  const tx = (p.x - vb[0]) / vb[2];
  const ty = (p.y - vb[1]) / vb[3];

  state.viewBox = [
    p.x - tx * newW,
    p.y - ty * newH,
    newW,
    newH
  ];

  stage.setAttribute("viewBox", state.viewBox.join(" "));
}, {passive:false});

function serializeEditedSvg() {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("xmlns", NS);
  svg.setAttribute("viewBox", stage.getAttribute("viewBox") || state.viewBox.join(" "));

  for (const pathObj of state.paths) {
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", buildPath(pathObj.segs));
    p.setAttribute("fill", "#000000");
    svg.appendChild(p);
  }

  return new XMLSerializer().serializeToString(svg);
}

async function loadManifest() {
  try {
    const raw = await fetchJson(`/elastic_skeleton_manifest/${JOB_ID}`);
    const m = normalizeManifest(raw);
    if (m.length) return m;
  } catch(e) {
    console.warn(e);
  }
  return fallbackManifest();
}

function renderGlyphList() {
  ui.glyphList.innerHTML = "";

  state.manifest.forEach((g, i) => {
    const div = document.createElement("div");
    div.className = "card" + (i === state.activeIndex ? " active" : "");
    div.innerHTML = `<b>${g.code}</b><small>${g.variants.length} 个版本</small>`;
    div.onclick = () => selectGlyph(i);
    ui.glyphList.appendChild(div);
  });
}

async function selectGlyph(i) {
  state.activeIndex = i;
  const g = state.manifest[i];
  state.code = g.code;

  ui.variantSelect.innerHTML = "";
  g.variants.forEach(v => {
    const op = document.createElement("option");
    op.value = v;
    op.textContent = v;
    ui.variantSelect.appendChild(op);
  });

  state.variant = g.variants[0] || "step_01";
  ui.variantSelect.value = state.variant;

  renderGlyphList();
  await loadRawSvg();
}

async function loadRawSvg() {
  try {
    const saved = await fetch(`/load_svg_path_edit/${JOB_ID}/${state.code}/${state.variant}`);
    if (saved.ok) {
      state.rawSvgText = await saved.text();
      loadSvgIntoStage(state.rawSvgText);
      return;
    }
  } catch(e) {}

  state.rawSvgText = await fetchText(`/raw_svg_variant/${JOB_ID}/${state.code}/${state.variant}`);
  loadSvgIntoStage(state.rawSvgText);
}

async function reloadRaw() {
  state.rawSvgText = await fetchText(`/raw_svg_variant/${JOB_ID}/${state.code}/${state.variant}`);
  loadSvgIntoStage(state.rawSvgText);
}

async function saveSvg() {
  const svg = serializeEditedSvg();

  const r = await fetch(`/save_svg_path_edit/${JOB_ID}/${state.code}/${state.variant}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({svg})
  });

  if (!r.ok) {
    alert("保存失败：HTTP " + r.status);
    return;
  }

  alert("已保存 SVG 轮廓编辑");
}

function downloadSvg() {
  const svg = serializeEditedSvg();
  const blob = new Blob([svg], {type:"image/svg+xml;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${state.code}_${state.variant}_edited.svg`;
  a.click();
  URL.revokeObjectURL(url);
}

function updateGlyphOpacity() {
  stage.querySelectorAll("#glyphContent path").forEach(p => {
    p.setAttribute("fill-opacity", ui.glyphOpacity.value);
  });
}

function updateStatus() {
  ui.statusBox.innerHTML =
    `Job ID：${JOB_ID}<br>` +
    `当前字形：${state.code || "-"}<br>` +
    `当前版本：${state.variant || "-"}<br>` +
    `路径数量：${state.paths.length}<br>` +
    `可编辑控制点：${state.handles.length}<br>` +
    `状态：直接编辑 SVG path`;
}

async function init() {
  labels();

  [
    ui.showPoints,
    ui.showControls,
    ui.pointSize
  ].forEach(el => el.addEventListener("input", () => {
    labels();
    rebuildHandles();
  }));

  ui.glyphOpacity.addEventListener("input", () => {
    labels();
    updateGlyphOpacity();
  });

  ui.variantSelect.onchange = async () => {
    state.variant = ui.variantSelect.value;
    await loadRawSvg();
  };

  ui.saveBtn.onclick = saveSvg;
  ui.loadSavedBtn.onclick = loadRawSvg;
  ui.downloadSvgBtn.onclick = downloadSvg;
  ui.reloadRawBtn.onclick = reloadRaw;

  state.manifest = await loadManifest();
  renderGlyphList();
  await selectGlyph(0);
}

init();
</script>
</body>
</html>
"""
    return html.replace("__JOB_ID__", job_id)


@app.post("/save_svg_path_edit/{job_id}/{code}/{variant}")
async def save_svg_path_edit(job_id: str, code: str, variant: str, request: _SvgEditRequest):
    data = await request.json()
    svg = data.get("svg", "")

    if not svg.strip():
        return _SvgEditJSONResponse({"ok": False, "error": "empty svg"}, status_code=400)

    safe_code = _svg_edit_re.sub(r"[^0-9A-Za-z_\\-]", "_", code)
    safe_variant = _svg_edit_re.sub(r"[^0-9A-Za-z_\\-]", "_", variant)

    out_dir = _SvgEditPath("jobs") / job_id / "svg_path_edits"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{safe_code}_{safe_variant}.svg"
    out_file.write_text(svg, encoding="utf-8")

    return {"ok": True, "file": str(out_file)}


@app.get("/load_svg_path_edit/{job_id}/{code}/{variant}")
async def load_svg_path_edit(job_id: str, code: str, variant: str):
    safe_code = _svg_edit_re.sub(r"[^0-9A-Za-z_\\-]", "_", code)
    safe_variant = _svg_edit_re.sub(r"[^0-9A-Za-z_\\-]", "_", variant)

    f = _SvgEditPath("jobs") / job_id / "svg_path_edits" / f"{safe_code}_{safe_variant}.svg"

    if not f.exists():
        return _SvgEditJSONResponse({"ok": False, "error": "not found"}, status_code=404)

    return _SvgEditResponse(
        content=f.read_text(encoding="utf-8"),
        media_type="image/svg+xml"
    )


@app.middleware("http")
async def redirect_every_old_editor_to_svg_path_editor(request, call_next):
    path = request.url.path

    prefixes = [
        "/skeleton_editor/",
        "/skeleton_elastic_editor/",
        "/skeleton_elastic_editor_v5/",
        "/glyph_warp_editor/"
    ]

    for prefix in prefixes:
        if path.startswith(prefix):
            suffix = path[len(prefix):]
            target = "/svg_path_editor/" + suffix

            if request.url.query:
                target += "?" + request.url.query

            return _SvgEditRedirectResponse(url=target, status_code=302)

    return await call_next(request)

# ===== END_SVG_PATH_REAL_GLYPH_EDITOR_V1 =====
'''
    text = text.rstrip() + "\n\n" + code + "\n"
    APP.write_text(text, encoding="utf-8")
    print("已安装 SVG path 真实字形轮廓编辑器。")

print("准备重启 FastAPI。")
