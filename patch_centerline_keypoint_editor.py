from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_centerline_keypoint_editor")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

MARK = "# ===== CENTERLINE_KEYPOINT_EDITOR_V1 ====="

if MARK in text:
    print("已经安装过中心线关键点编辑器，不重复添加。")
else:
    code = r'''
# ===== CENTERLINE_KEYPOINT_EDITOR_V1 =====
from fastapi import Request as _CenterlineRequest
from fastapi.responses import HTMLResponse as _CenterlineHTMLResponse
from fastapi.responses import JSONResponse as _CenterlineJSONResponse
from starlette.responses import RedirectResponse as _CenterlineRedirectResponse
from pathlib import Path as _CenterlinePath
import json as _centerline_json
import re as _centerline_re

@app.get("/centerline_editor/{job_id}", response_class=_CenterlineHTMLResponse)
async def centerline_keypoint_editor(job_id: str):
    html = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>字形中心线关键点编辑器</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f3f4f6;font-family:Arial,"Microsoft YaHei",sans-serif;color:#111827}
header{height:72px;background:#111;color:#fff;padding:14px 22px}
header h1{margin:0;font-size:24px}
header p{margin:6px 0 0;font-size:13px;color:#d1d5db}
.app{height:calc(100vh - 72px);display:grid;grid-template-columns:220px 1fr 290px;gap:14px;padding:14px}
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
  <h1>字形中心线关键点编辑器</h1>
  <p>灰色显示原字，在字形上绘制一条可编辑中心线，用少量关键点控制字形结构。</p>
</header>

<div class="app">
  <aside class="panel">
    <div class="title">字形列表</div>
    <div id="glyphList">加载中...</div>
  </aside>

  <main class="main">
    <div class="toolbar">
      <button class="primary" id="saveBtn">保存中心线</button>
      <button class="green" id="loadBtn">读取已保存</button>
      <button id="exportBtn">导出 JSON</button>
      <button class="warn" id="resetBtn">重置中心线</button>
      <button id="clearBtn">清空</button>
      <select id="variantSelect"></select>
    </div>

    <div class="stage-wrap">
      <svg id="stage" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>

    <div class="help">
      操作：拖动白色点调整中心线；双击空白处添加点；点击点后按“删除选中点”删除；鼠标滚轮缩放；右键拖动画布。
    </div>
  </main>

  <aside class="panel">
    <div class="title">中心线控制</div>

    <div class="row"><input type="checkbox" id="showGlyph" checked><label for="showGlyph">显示灰色原字</label></div>
    <div class="row"><input type="checkbox" id="showLine" checked><label for="showLine">显示中心线</label></div>
    <div class="row"><input type="checkbox" id="showPoints" checked><label for="showPoints">显示关键点</label></div>

    <hr>

    <button class="green" id="autoBtn">生成粗略中心线</button>
    <button class="warn" id="deleteBtn">删除选中点</button>

    <div class="group">
      <label>线宽 <span id="lineWidthValue">5</span></label>
      <input id="lineWidth" type="range" min="1" max="16" step="1" value="5">
    </div>

    <div class="group">
      <label>关键点大小 <span id="pointSizeValue">7</span></label>
      <input id="pointSize" type="range" min="3" max="16" step="1" value="7">
    </div>

    <div class="group">
      <label>原字透明度 <span id="glyphOpacityValue">0.48</span></label>
      <input id="glyphOpacity" type="range" min="0.1" max="1" step="0.05" value="0.48">
    </div>

    <div class="status" id="statusBox"></div>

    <div class="note">
      这个版本只做你图里那种“字形中心线 + 少量关键点”。中心线数据会保存为 JSON，后续可以继续接到字形变形或字体导出流程。
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
  loadBtn: document.getElementById("loadBtn"),
  exportBtn: document.getElementById("exportBtn"),
  resetBtn: document.getElementById("resetBtn"),
  clearBtn: document.getElementById("clearBtn"),
  autoBtn: document.getElementById("autoBtn"),
  deleteBtn: document.getElementById("deleteBtn"),
  showGlyph: document.getElementById("showGlyph"),
  showLine: document.getElementById("showLine"),
  showPoints: document.getElementById("showPoints"),
  lineWidth: document.getElementById("lineWidth"),
  pointSize: document.getElementById("pointSize"),
  glyphOpacity: document.getElementById("glyphOpacity"),
  lineWidthValue: document.getElementById("lineWidthValue"),
  pointSizeValue: document.getElementById("pointSizeValue"),
  glyphOpacityValue: document.getElementById("glyphOpacityValue"),
  statusBox: document.getElementById("statusBox")
};

const state = {
  manifest: [],
  activeIndex: 0,
  code: null,
  variant: null,
  rawSvgText: "",
  viewBox: [0,0,1000,1000],
  points: [],
  selectedIndex: -1,
  drag: null,
  pan: null
};

function labels() {
  ui.lineWidthValue.textContent = ui.lineWidth.value;
  ui.pointSizeValue.textContent = ui.pointSize.value;
  ui.glyphOpacityValue.textContent = ui.glyphOpacity.value;
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

function svgPoint(evt) {
  const pt = stage.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(stage.getScreenCTM().inverse());
}

function clearStage() {
  while (stage.firstChild) stage.removeChild(stage.firstChild);
}

function renderSvgGlyph(svgText) {
  clearStage();

  const doc = new DOMParser().parseFromString(svgText, "image/svg+xml");
  const rawSvg = doc.documentElement;

  state.viewBox = parseViewBox(rawSvg);
  stage.setAttribute("viewBox", state.viewBox.join(" "));

  const glyphLayer = document.createElementNS(NS, "g");
  glyphLayer.setAttribute("id", "glyphLayer");
  stage.appendChild(glyphLayer);

  for (const child of Array.from(rawSvg.childNodes)) {
    const imported = document.importNode(child, true);
    glyphLayer.appendChild(imported);
  }

  normalizeGlyphStyle();

  const lineLayer = document.createElementNS(NS, "g");
  lineLayer.setAttribute("id", "lineLayer");
  stage.appendChild(lineLayer);

  redraw();
}

function normalizeGlyphStyle() {
  const glyphLayer = stage.querySelector("#glyphLayer");
  if (!glyphLayer) return;

  glyphLayer.style.display = ui.showGlyph.checked ? "" : "none";

  const shapes = glyphLayer.querySelectorAll("path, polygon, polyline, rect, circle, ellipse");
  shapes.forEach(el => {
    el.setAttribute("fill", "#6b7280");
    el.setAttribute("fill-opacity", ui.glyphOpacity.value);
    el.setAttribute("stroke", "none");
  });
}

function autoCenterline() {
  const [x, y, w, h] = state.viewBox;

  state.points = [
    {x: x + w * 0.12, y: y + h * 0.42},
    {x: x + w * 0.24, y: y + h * 0.32},
    {x: x + w * 0.37, y: y + h * 0.40},
    {x: x + w * 0.42, y: y + h * 0.64},
    {x: x + w * 0.53, y: y + h * 0.58},
    {x: x + w * 0.66, y: y + h * 0.82},
    {x: x + w * 0.78, y: y + h * 0.50},
    {x: x + w * 0.88, y: y + h * 0.18}
  ];

  state.selectedIndex = -1;
  redraw();
}

function makePathD(points) {
  if (!points.length) return "";
  let d = `M ${points[0].x} ${points[0].y}`;

  if (points.length === 2) {
    d += ` L ${points[1].x} ${points[1].y}`;
    return d;
  }

  for (let i = 1; i < points.length - 1; i++) {
    const p = points[i];
    const next = points[i + 1];
    const mx = (p.x + next.x) / 2;
    const my = (p.y + next.y) / 2;
    d += ` Q ${p.x} ${p.y} ${mx} ${my}`;
  }

  const last = points[points.length - 1];
  d += ` T ${last.x} ${last.y}`;

  return d;
}

function redraw() {
  normalizeGlyphStyle();

  let lineLayer = stage.querySelector("#lineLayer");
  if (!lineLayer) {
    lineLayer = document.createElementNS(NS, "g");
    lineLayer.setAttribute("id", "lineLayer");
    stage.appendChild(lineLayer);
  }

  while (lineLayer.firstChild) lineLayer.removeChild(lineLayer.firstChild);

  if (ui.showLine.checked && state.points.length >= 2) {
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", makePathD(state.points));
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "#ef4444");
    path.setAttribute("stroke-width", ui.lineWidth.value);
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    path.setAttribute("vector-effect", "non-scaling-stroke");
    lineLayer.appendChild(path);
  }

  if (ui.showPoints.checked) {
    const r = Number(ui.pointSize.value);

    state.points.forEach((p, i) => {
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", p.x);
      c.setAttribute("cy", p.y);
      c.setAttribute("r", r);
      c.setAttribute("fill", i === state.selectedIndex ? "#facc15" : "#ffffff");
      c.setAttribute("stroke", "#ef4444");
      c.setAttribute("stroke-width", "2");
      c.setAttribute("vector-effect", "non-scaling-stroke");
      c.style.cursor = "move";
      c.dataset.index = i;
      lineLayer.appendChild(c);
    });
  }

  updateStatus();
}

function nearestSegmentIndex(p) {
  if (state.points.length < 2) return state.points.length;

  let best = 0;
  let bestD = Infinity;

  for (let i = 0; i < state.points.length - 1; i++) {
    const a = state.points[i];
    const b = state.points[i + 1];

    const vx = b.x - a.x;
    const vy = b.y - a.y;
    const len2 = vx * vx + vy * vy;

    let t = 0;
    if (len2 > 1e-9) {
      t = ((p.x - a.x) * vx + (p.y - a.y) * vy) / len2;
      t = Math.max(0, Math.min(1, t));
    }

    const qx = a.x + vx * t;
    const qy = a.y + vy * t;
    const d = Math.hypot(p.x - qx, p.y - qy);

    if (d < bestD) {
      bestD = d;
      best = i + 1;
    }
  }

  return best;
}

stage.addEventListener("pointerdown", e => {
  if (e.button === 2 || e.button === 1) {
    state.pan = {
      x: e.clientX,
      y: e.clientY,
      viewBox: [...state.viewBox]
    };
    stage.setPointerCapture(e.pointerId);
    return;
  }

  const target = e.target;
  if (target.dataset && target.dataset.index !== undefined) {
    const idx = Number(target.dataset.index);
    state.selectedIndex = idx;

    const p = svgPoint(e);
    state.drag = {
      index: idx,
      start: p,
      original: {...state.points[idx]}
    };

    stage.setPointerCapture(e.pointerId);
    redraw();
  }
});

stage.addEventListener("pointermove", e => {
  if (state.pan) {
    const vb = state.pan.viewBox;
    const dx = (e.clientX - state.pan.x) * vb[2] / stage.clientWidth;
    const dy = (e.clientY - state.pan.y) * vb[3] / stage.clientHeight;

    state.viewBox = [vb[0] - dx, vb[1] - dy, vb[2], vb[3]];
    stage.setAttribute("viewBox", state.viewBox.join(" "));
    return;
  }

  if (!state.drag) return;

  const p = svgPoint(e);
  const dx = p.x - state.drag.start.x;
  const dy = p.y - state.drag.start.y;

  state.points[state.drag.index] = {
    x: state.drag.original.x + dx,
    y: state.drag.original.y + dy
  };

  redraw();
});

stage.addEventListener("pointerup", () => {
  state.drag = null;
  state.pan = null;
});

stage.addEventListener("pointerleave", () => {
  state.drag = null;
  state.pan = null;
});

stage.addEventListener("dblclick", e => {
  const p = svgPoint(e);
  const idx = nearestSegmentIndex(p);
  state.points.splice(idx, 0, {x: p.x, y: p.y});
  state.selectedIndex = idx;
  redraw();
});

stage.addEventListener("wheel", e => {
  e.preventDefault();

  const factor = e.deltaY < 0 ? 0.9 : 1.1;
  const vb = [...state.viewBox];
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

stage.addEventListener("contextmenu", e => e.preventDefault());

function deleteSelectedPoint() {
  if (state.selectedIndex < 0 || state.selectedIndex >= state.points.length) return;
  state.points.splice(state.selectedIndex, 1);
  state.selectedIndex = -1;
  redraw();
}

async function saveCenterline() {
  const body = {
    job_id: JOB_ID,
    code: state.code,
    variant: state.variant,
    viewBox: state.viewBox,
    points: state.points
  };

  const r = await fetch(`/save_centerline_edit/${JOB_ID}/${state.code}/${state.variant}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });

  if (!r.ok) {
    alert("保存失败：HTTP " + r.status);
    return;
  }

  alert("中心线已保存");
}

async function loadCenterline(silent=false) {
  const r = await fetch(`/load_centerline_edit/${JOB_ID}/${state.code}/${state.variant}`);

  if (!r.ok) {
    if (!silent) alert("没有找到已保存中心线");
    return false;
  }

  const data = await r.json();

  if (Array.isArray(data.points)) {
    state.points = data.points.map(p => ({x: Number(p.x), y: Number(p.y)}));
    state.selectedIndex = -1;
    redraw();
    return true;
  }

  return false;
}

function exportJson() {
  const body = {
    job_id: JOB_ID,
    code: state.code,
    variant: state.variant,
    viewBox: state.viewBox,
    points: state.points
  };

  const blob = new Blob([JSON.stringify(body, null, 2)], {type:"application/json"});
  const url = URL.createObjectURL(blob);

  const a = document.createElement("a");
  a.href = url;
  a.download = `${state.code}_${state.variant}_centerline.json`;
  a.click();

  URL.revokeObjectURL(url);
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
  await loadGlyph();
}

async function loadGlyph() {
  try {
    state.rawSvgText = await fetchText(`/raw_svg_variant/${JOB_ID}/${state.code}/${state.variant}`);
    renderSvgGlyph(state.rawSvgText);

    const loaded = await loadCenterline(true);
    if (!loaded) {
      autoCenterline();
    }
  } catch(e) {
    ui.statusBox.innerHTML = `<div class="err">${e.stack || e}</div>`;
  }
}

function updateStatus() {
  ui.statusBox.innerHTML =
    `Job ID：${JOB_ID}<br>` +
    `当前字形：${state.code || "-"}<br>` +
    `当前版本：${state.variant || "-"}<br>` +
    `中心线关键点：${state.points.length}<br>` +
    `选中点：${state.selectedIndex >= 0 ? state.selectedIndex + 1 : "-"}<br>` +
    `状态：中心线关键点编辑`;
}

async function init() {
  labels();

  [
    ui.showGlyph,
    ui.showLine,
    ui.showPoints,
    ui.lineWidth,
    ui.pointSize,
    ui.glyphOpacity
  ].forEach(el => el.addEventListener("input", () => {
    labels();
    redraw();
  }));

  ui.saveBtn.onclick = saveCenterline;
  ui.loadBtn.onclick = () => loadCenterline(false);
  ui.exportBtn.onclick = exportJson;
  ui.resetBtn.onclick = autoCenterline;
  ui.clearBtn.onclick = () => {
    state.points = [];
    state.selectedIndex = -1;
    redraw();
  };
  ui.autoBtn.onclick = autoCenterline;
  ui.deleteBtn.onclick = deleteSelectedPoint;

  ui.variantSelect.onchange = async () => {
    state.variant = ui.variantSelect.value;
    await loadGlyph();
  };

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


@app.post("/save_centerline_edit/{job_id}/{code}/{variant}")
async def save_centerline_edit(job_id: str, code: str, variant: str, request: _CenterlineRequest):
    data = await request.json()

    safe_code = _centerline_re.sub(r"[^0-9A-Za-z_\\-]", "_", code)
    safe_variant = _centerline_re.sub(r"[^0-9A-Za-z_\\-]", "_", variant)

    out_dir = _CenterlinePath("jobs") / job_id / "centerline_edits"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{safe_code}_{safe_variant}.json"
    out_file.write_text(_centerline_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"ok": True, "file": str(out_file)}


@app.get("/load_centerline_edit/{job_id}/{code}/{variant}")
async def load_centerline_edit(job_id: str, code: str, variant: str):
    safe_code = _centerline_re.sub(r"[^0-9A-Za-z_\\-]", "_", code)
    safe_variant = _centerline_re.sub(r"[^0-9A-Za-z_\\-]", "_", variant)

    f = _CenterlinePath("jobs") / job_id / "centerline_edits" / f"{safe_code}_{safe_variant}.json"

    if not f.exists():
        return _CenterlineJSONResponse({"ok": False, "error": "not found"}, status_code=404)

    return _centerline_json.loads(f.read_text(encoding="utf-8"))


@app.middleware("http")
async def redirect_old_editors_to_centerline_editor(request, call_next):
    path = request.url.path

    prefixes = [
        "/skeleton_editor/",
        "/skeleton_elastic_editor/",
        "/skeleton_elastic_editor_v5/",
        "/glyph_warp_editor/",
        "/svg_path_editor/"
    ]

    for prefix in prefixes:
        if path.startswith(prefix):
            suffix = path[len(prefix):]
            target = "/centerline_editor/" + suffix

            if request.url.query:
                target += "?" + request.url.query

            return _CenterlineRedirectResponse(url=target, status_code=302)

    return await call_next(request)

# ===== END_CENTERLINE_KEYPOINT_EDITOR_V1 =====
'''
    text = text.rstrip() + "\n\n" + code + "\n"
    APP.write_text(text, encoding="utf-8")
    print("已安装中心线关键点编辑器。")

print("准备重启 FastAPI。")
