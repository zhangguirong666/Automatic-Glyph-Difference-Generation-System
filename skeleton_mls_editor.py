from __future__ import annotations


def build_mls_editor_html(job_id: str) -> str:
    return r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>MLS 骨架整体变形编辑器</title>
<style>
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Arial, "Microsoft YaHei", sans-serif;
  background: #ffffff;
  color: #111111;
}
.header {
  background: #ffffff;
  color: #111111;
  padding: 18px 24px;
  border-bottom: 1px solid #dddddd;
}
.header h1 {
  margin: 0 0 8px 0;
  font-size: 32px;
  font-weight: 800;
}
.header .sub {
  color: #333333;
  font-size: 15px;
}
.wrap {
  display: grid;
  grid-template-columns: 280px 1fr 330px;
  gap: 16px;
  padding: 16px;
}
.panel {
  background: #ffffff;
  border: 1px solid #dddddd;
  border-radius: 14px;
  padding: 14px;
}
.left-list {
  height: 760px;
  overflow-y: auto;
}
.label {
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 10px;
}
.item-btn {
  width: 100%;
  text-align: left;
  background: #ffffff;
  color: #111111;
  border: 1px solid #dddddd;
  border-radius: 10px;
  padding: 10px;
  margin-bottom: 10px;
  cursor: pointer;
}
.item-btn.active {
  border: 2px solid #111111;
}
.item-btn .glyph-char {
  font-size: 28px;
  color: #111111;
  line-height: 1.2;
  margin-bottom: 6px;
}
.item-btn .glyph-code {
  font-size: 13px;
  color: #111111;
  font-family: Consolas, monospace;
}
.item-btn .glyph-count {
  font-size: 12px;
  color: #333333;
  margin-top: 4px;
}
.topbar {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
button {
  background: #ffffff;
  color: #111111;
  border: 1px solid #111111;
  border-radius: 8px;
  padding: 10px 14px;
  cursor: pointer;
  font-weight: 600;
}
button.primary {
  background: #111111;
  color: #ffffff;
}
select, input[type=range] {
  width: 100%;
}
#mainSvg {
  width: 100%;
  height: 760px;
  border: 1px solid #dddddd;
  border-radius: 12px;
  background: #ffffff;
}
.row {
  margin-bottom: 14px;
}
.small {
  font-size: 12px;
  color: #333333;
}
.infobox {
  font-family: Consolas, monospace;
  font-size: 12px;
  color: #111111;
  background: #ffffff;
  border: 1px solid #dddddd;
  border-radius: 8px;
  padding: 10px;
  white-space: pre-wrap;
  line-height: 1.6;
}
.chk {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.note {
  color: #111111;
  font-size: 13px;
  line-height: 1.8;
  margin-top: 10px;
}
.tip {
  background: #ffffff;
  color: #111111;
  border: 1px solid #dddddd;
  border-radius: 8px;
  padding: 10px;
  font-size: 12px;
  line-height: 1.7;
}
</style>
</head>
<body>
<div class="header">
  <h1>MLS 骨架整体变形编辑器</h1>
  <div class="sub">完整字形优先显示。控制点叠加在字形上，拖动控制点时黑色完整字形整体协调变化。</div>
</div>

<div class="wrap">
  <div class="panel">
    <div class="label">字形列表</div>
    <div id="glyphList" class="left-list"></div>
  </div>

  <div class="panel">
    <div class="topbar">
      <button class="primary" id="saveBtn">保存当前字形</button>
      <button id="exportSvgBtn">导出 SVG</button>
      <button id="exportPngBtn">导出 PNG</button>
      <button id="fitBtn">适配显示</button>
      <button id="resetBtn">恢复当前版本</button>
    </div>

    <div class="row">
      <div class="small">版本：</div>
      <select id="variantSelect"></select>
    </div>

    <svg id="mainSvg" viewBox="0 0 1000 1000" xmlns="http://www.w3.org/2000/svg">
      <g id="originalLayer"></g>
      <g id="deformedLayer"></g>
      <g id="skeletonLayer"></g>
      <g id="handleLayer"></g>
    </svg>

    <div class="note">
      操作方式：<br>
      1. 白色圆点是少量骨架控制点；<br>
      2. 拖动任意一个白点，其他控制点会跟着协调移动；<br>
      3. 黑色完整字形是变化后预览，会实时更新；<br>
      4. 浅灰色为原始轮廓，可在右侧开启。
    </div>
  </div>

  <div class="panel">
    <div class="label">显示控制</div>

    <label class="chk"><input type="checkbox" id="showOriginal">显示原始轮廓（浅灰）</label>
    <label class="chk"><input type="checkbox" id="showDeformed" checked>显示变化后字形（黑）</label>
    <label class="chk"><input type="checkbox" id="showSkeleton">显示骨架中心线（红）</label>
    <label class="chk"><input type="checkbox" id="showHandles" checked>显示控制点（白）</label>

    <div class="row">
      <div class="label">字形变化强度</div>
      <input type="range" id="strengthSlider" min="0" max="1" step="0.05" value="0.90">
      <div id="strengthVal" class="small">0.90</div>
    </div>

    <div class="row">
      <div class="label">整体联动强度</div>
      <input type="range" id="cohesionSlider" min="0" max="1" step="0.05" value="0.85">
      <div id="cohesionVal" class="small">0.85</div>
    </div>

    <div class="row">
      <div class="label">影响范围</div>
      <input type="range" id="radiusSlider" min="0.4" max="2.5" step="0.05" value="1.45">
      <div id="radiusVal" class="small">1.45</div>
    </div>

    <div class="row">
      <div class="label">当前选中</div>
      <div id="selectedInfo" class="infobox">未选中任何点</div>
    </div>

    <div class="row">
      <div class="label">状态</div>
      <div id="statusBox" class="infobox">等待加载...</div>
    </div>

    <div class="tip">
      这版改动：<br>
      1. 默认显示完整黑色字形，不再单独显示骨架中心线；<br>
      2. 控制点叠加在完整字形上；<br>
      3. 拖动控制点时，变化后黑色字形实时显示；<br>
      4. 如果后端轮廓提取失败，也会回退显示原始完整字形。
    </div>
  </div>
</div>

<script>
const JOB_ID = "__JOB_ID__";

let manifestData = null;
let currentCode = null;
let currentVariant = null;

let skeletonData = null;
let originalControls = null;
let currentControls = null;

let viewBox = [0, 0, 1000, 1000];
let sampledContours = [];

let selected = null;
let dragging = false;
let dragStart = null;
let dragBaseControls = null;

const glyphList = document.getElementById("glyphList");
const variantSelect = document.getElementById("variantSelect");
const mainSvg = document.getElementById("mainSvg");

const originalLayer = document.getElementById("originalLayer");
const deformedLayer = document.getElementById("deformedLayer");
const skeletonLayer = document.getElementById("skeletonLayer");
const handleLayer = document.getElementById("handleLayer");

const strengthSlider = document.getElementById("strengthSlider");
const strengthVal = document.getElementById("strengthVal");
const cohesionSlider = document.getElementById("cohesionSlider");
const cohesionVal = document.getElementById("cohesionVal");
const radiusSlider = document.getElementById("radiusSlider");
const radiusVal = document.getElementById("radiusVal");

const selectedInfo = document.getElementById("selectedInfo");
const statusBox = document.getElementById("statusBox");

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}
function setStatus(msg) {
  statusBox.textContent = msg;
}
function escHtml(s) {
  return String(s ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}

function bboxOfPoints(points) {
  if (!points || !points.length) return {minX:0,minY:0,maxX:1000,maxY:1000,w:1000,h:1000};
  let minX = points[0].x, minY = points[0].y, maxX = points[0].x, maxY = points[0].y;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  return {minX, minY, maxX, maxY, w:maxX-minX, h:maxY-minY};
}
function flatControls(data) {
  const out = [];
  if (!data || !data.segments) return out;
  data.segments.forEach((seg, si) => {
    (seg.points || []).forEach((p, pi) => {
      out.push({seg:si, idx:pi, x:p.x, y:p.y});
    });
  });
  return out;
}
function bboxOfControls(data) {
  return bboxOfPoints(flatControls(data));
}
function bboxOfContours(contours) {
  const pts = [];
  contours.forEach(c => c.forEach(p => pts.push(p)));
  return bboxOfPoints(pts);
}
function unionBbox(a, b) {
  const minX = Math.min(a.minX, b.minX);
  const minY = Math.min(a.minY, b.minY);
  const maxX = Math.max(a.maxX, b.maxX);
  const maxY = Math.max(a.maxY, b.maxY);
  return {minX, minY, maxX, maxY, w:maxX-minX, h:maxY-minY};
}
function fitViewBox() {
  if (!currentControls || !sampledContours.length) return;
  const cb = bboxOfControls(currentControls);
  const gb = bboxOfContours(sampledContours);
  const ub = unionBbox(cb, gb);
  const pad = Math.max(40, Math.max(ub.w, ub.h) * 0.18);
  mainSvg.setAttribute("viewBox", `${ub.minX-pad} ${ub.minY-pad} ${ub.w+pad*2} ${ub.h+pad*2}`);
}

function distancePointLine(p, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  if (Math.abs(dx) < 1e-9 && Math.abs(dy) < 1e-9) return Math.hypot(p.x-a.x, p.y-a.y);
  const t = ((p.x-a.x)*dx + (p.y-a.y)*dy) / (dx*dx + dy*dy);
  const tt = Math.max(0, Math.min(1, t));
  const qx = a.x + tt * dx;
  const qy = a.y + tt * dy;
  return Math.hypot(p.x-qx, p.y-qy);
}
function rdp(points, epsilon) {
  if (points.length <= 2) return points.slice();
  let dmax = 0, index = 0;
  const end = points.length - 1;
  for (let i=1; i<end; i++) {
    const d = distancePointLine(points[i], points[0], points[end]);
    if (d > dmax) { dmax = d; index = i; }
  }
  if (dmax > epsilon) {
    const rec1 = rdp(points.slice(0, index+1), epsilon);
    const rec2 = rdp(points.slice(index), epsilon);
    return rec1.slice(0,-1).concat(rec2);
  }
  return [points[0], points[end]];
}
function evenSample(points, n) {
  if (points.length <= n) return points.slice();
  const out = [];
  for (let i=0; i<n; i++) {
    const idx = Math.round(i*(points.length-1)/(n-1));
    out.push(points[idx]);
  }
  return out;
}
function segLength(points) {
  let s = 0;
  for (let i=1; i<points.length; i++) {
    s += Math.hypot(points[i].x-points[i-1].x, points[i].y-points[i-1].y);
  }
  return s;
}

function simplifyControls(data) {
  const rawSegs = (data.segments || [])
    .map(seg => ({...seg, _len: segLength(seg.points || [])}))
    .filter(seg => (seg.points || []).length >= 2 && seg._len > 8)
    .sort((a,b) => b._len - a._len)
    .slice(0, 3);  // 控制轴最多保留 3 条，避免点太多

  const out = deepClone(data);
  out.segments = rawSegs.map(seg => {
    const pts = seg.points || [];
    const bb = bboxOfPoints(pts);
    const diag = Math.max(1, Math.hypot(bb.w, bb.h));

    let simp = rdp(pts, Math.max(16, diag * 0.055));

    // 每条线最多 4 个点，最少 2 个点
    if (simp.length < 2) simp = evenSample(pts, Math.min(2, pts.length));
    if (simp.length > 4) simp = evenSample(simp, 4);

    return {
      id: seg.id,
      points: simp.map(p => ({x:p.x, y:p.y, w:p.w || 8}))
    };
  }).filter(seg => (seg.points || []).length >= 2);

  return out;
}

function normalizeControlsToContours(ctrl, contours) {
  if (!contours.length) return ctrl;

  const out = deepClone(ctrl);
  const cb = bboxOfControls(out);
  const gb = bboxOfContours(contours);

  const ccx = cb.minX + cb.w / 2;
  const ccy = cb.minY + cb.h / 2;
  const gcx = gb.minX + gb.w / 2;
  const gcy = gb.minY + gb.h / 2;

  const gbDiag = Math.max(1, Math.hypot(gb.w, gb.h));
  const centerDist = Math.hypot(ccx-gcx, ccy-gcy);

  const ratioX = gb.w / Math.max(1, cb.w);
  const ratioY = gb.h / Math.max(1, cb.h);

  const need =
    centerDist > gbDiag * 0.30 ||
    ratioX > 2.4 || ratioX < 0.40 ||
    ratioY > 2.4 || ratioY < 0.40;

  if (!need) return out;

  let s = Math.min(
    gb.w / Math.max(1, cb.w),
    gb.h / Math.max(1, cb.h)
  ) * 0.86;

  if (!Number.isFinite(s) || s <= 0) s = 1.0;
  s = Math.max(0.25, Math.min(4.0, s));

  out.segments.forEach(seg => {
    seg.points.forEach(p => {
      p.x = gcx + (p.x - ccx) * s;
      p.y = gcy + (p.y - ccy) * s;
    });
  });

  return out;
}

function makeMovedControls(base, selSeg, selIdx, dx, dy) {
  const out = deepClone(base);
  const cohesion = Number(cohesionSlider.value || 0.85);

  const ref = base.segments[selSeg].points[selIdx];
  const bb = bboxOfControls(base);
  const diag = Math.max(1, Math.hypot(bb.w, bb.h));
  const radius = diag * (0.85 + 1.25 * cohesion);

  out.segments.forEach((seg, si) => {
    const baseSeg = base.segments[si];

    seg.points.forEach((p, pi) => {
      const bp = baseSeg.points[pi];

      let w = 0;

      // 同一条控制轴强联动
      if (si === selSeg) {
        const topo = Math.abs(pi - selIdx);
        if (topo === 0) w = 1.0;
        else if (topo === 1) w = 0.88 * cohesion;
        else if (topo === 2) w = 0.70 * cohesion;
        else w = 0.55 * cohesion;
      }

      // 所有其他控制轴也会明显跟随
      const d = Math.hypot(bp.x - ref.x, bp.y - ref.y);
      const local = Math.max(0, 1 - d / radius);
      const spatialW = (0.32 + Math.pow(local, 1.5) * 0.58) * cohesion;

      w = Math.max(w, spatialW);

      // 全局最低联动，解决“只动单线”的问题
      if (!(si === selSeg && pi === selIdx)) {
        w = Math.max(w, 0.35 * cohesion);
      }

      w = Math.max(0, Math.min(1, w));

      p.x = bp.x + dx * w;
      p.y = bp.y + dy * w;
    });
  });

  return out;
}

function mlsWeightedPoint(v, baseControls, movedControls) {
  const pList = flatControls(baseControls);
  const qList = flatControls(movedControls);

  if (!pList.length || pList.length !== qList.length) return v;

  const strength = Number(strengthSlider.value || 0.90);
  const radiusFactor = Number(radiusSlider.value || 1.45);

  const bb = bboxOfControls(baseControls);
  const diag = Math.max(1, Math.hypot(bb.w, bb.h));
  const radius = diag * radiusFactor;

  let sw = 0, sx = 0, sy = 0;
  let avgDx = 0, avgDy = 0;

  for (let i=0; i<pList.length; i++) {
    avgDx += qList[i].x - pList[i].x;
    avgDy += qList[i].y - pList[i].y;
  }
  avgDx /= pList.length;
  avgDy /= pList.length;

  for (let i=0; i<pList.length; i++) {
    const p = pList[i];
    const q = qList[i];

    const dx = q.x - p.x;
    const dy = q.y - p.y;

    const d = Math.hypot(v.x - p.x, v.y - p.y);
    const local = Math.max(0.04, 1 - d / radius);

    // 基础权重 + 局部权重
    const w = 0.12 + Math.pow(local, 2.0) * 2.5;

    sw += w;
    sx += dx * w;
    sy += dy * w;
  }

  if (sw < 1e-9) return v;

  let dx = sx / sw;
  let dy = sy / sw;

  // 加一点全局平均位移，让整个字形协调动
  dx = dx * 0.76 + avgDx * 0.24;
  dy = dy * 0.76 + avgDy * 0.24;

  return {
    x: v.x + dx * strength,
    y: v.y + dy * strength
  };
}

function contourToPath(contour) {
  if (!contour || contour.length < 3) return "";
  let d = `M ${contour[0].x.toFixed(3)} ${contour[0].y.toFixed(3)}`;
  for (let i=1; i<contour.length; i++) {
    d += ` L ${contour[i].x.toFixed(3)} ${contour[i].y.toFixed(3)}`;
  }
  d += " Z";
  return d;
}
function buildOriginalContoursPathElements() {
  const d = sampledContours.map(c => contourToPath(c)).join(" ");
  return `<path d="${d}" fill="#d9d9d9" stroke="none" fill-rule="evenodd"/>`;
}
function buildDeformedContoursPathElements() {
  if (!sampledContours.length) {
    return `<text x="${viewBox[0] + 20}" y="${viewBox[1] + 80}" font-size="40" fill="black">未提取到字形轮廓</text>`;
  }

  const warpedContours = sampledContours.map(contour => {
    return contour.map(p => mlsWeightedPoint(p, originalControls, currentControls));
  });
  const d = warpedContours.map(c => contourToPath(c)).join(" ");
  return `<path d="${d}" fill="#000000" stroke="none" fill-rule="evenodd"/>`;
}
function buildFullEditedSvg() {
  const paths = buildDeformedContoursPathElements();
  const vb = mainSvg.getAttribute("viewBox") || viewBox.join(" ");
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${vb}">${paths}</svg>`;
}

function drawSkeleton(data) {
  skeletonLayer.innerHTML = "";
  handleLayer.innerHTML = "";
  if (!data) return;

  const showSkeleton = document.getElementById("showSkeleton").checked;
  const showHandles = document.getElementById("showHandles").checked;

  if (showSkeleton) {
    data.segments.forEach((seg) => {
      const pts = seg.points || [];
      if (pts.length >= 2) {
        const d = "M " + pts.map(p => `${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(" L ");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", d);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "#ff4d4f");
        path.setAttribute("stroke-width", "2.3");
        skeletonLayer.appendChild(path);
      }
    });
  }

  if (showHandles) {
    data.segments.forEach((seg, si) => {
      const pts = seg.points || [];
      pts.forEach((p, pi) => {
        const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        const active = selected && selected.seg === si && selected.idx === pi;
        c.setAttribute("cx", p.x);
        c.setAttribute("cy", p.y);
        c.setAttribute("r", active ? 8 : 6);
        c.setAttribute("fill", "#ffffff");
        c.setAttribute("stroke", active ? "#2563eb" : "#111111");
        c.setAttribute("stroke-width", active ? "2.8" : "1.8");
        c.style.cursor = "move";
        c.addEventListener("pointerdown", ev => {
          ev.preventDefault();
          ev.stopPropagation();
          selected = {seg:si, idx:pi};
          dragging = true;
          dragStart = getMouseSvgPoint(ev);
          dragBaseControls = deepClone(currentControls);
          updateSelectedInfo();
          redrawAll();
        });
        handleLayer.appendChild(c);
      });
    });
  }
}

function redrawAll() {
  if (!currentControls || !originalControls) return;

  originalLayer.style.display = document.getElementById("showOriginal").checked ? "" : "none";
  deformedLayer.style.display = document.getElementById("showDeformed").checked ? "" : "none";
  skeletonLayer.style.display = document.getElementById("showSkeleton").checked ? "" : "none";
  handleLayer.style.display = document.getElementById("showHandles").checked ? "" : "none";

  originalLayer.innerHTML = buildOriginalContoursPathElements();
  deformedLayer.innerHTML = buildDeformedContoursPathElements();
  drawSkeleton(currentControls);
}

function getMouseSvgPoint(evt) {
  const pt = mainSvg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(mainSvg.getScreenCTM().inverse());
}

mainSvg.addEventListener("pointermove", evt => {
  if (!dragging || !selected || !dragBaseControls) return;

  const now = getMouseSvgPoint(evt);
  const dx = now.x - dragStart.x;
  const dy = now.y - dragStart.y;

  currentControls = makeMovedControls(dragBaseControls, selected.seg, selected.idx, dx, dy);

  updateSelectedInfo();
  redrawAll();
});

window.addEventListener("pointerup", () => {
  dragging = false;
  dragStart = null;
  dragBaseControls = null;
});

function updateSelectedInfo() {
  if (!selected || !currentControls) {
    selectedInfo.textContent = "未选中任何点";
    return;
  }
  const p = currentControls.segments[selected.seg].points[selected.idx];
  selectedInfo.textContent =
`字符: ${currentCode}
版本: ${currentVariant}
段: ${selected.seg}
控制点: ${selected.idx}
x: ${p.x.toFixed(2)}
y: ${p.y.toFixed(2)}

说明:
控制点已经减少。
拖动一个控制点时，
其他控制点和黑色字形预览都会同步变化。`;
}

async function loadManifest() {
  const res = await fetch(`/skeleton_manifest/${JOB_ID}`);
  manifestData = await res.json();
  renderGlyphList();
  if (manifestData.codes && manifestData.codes.length > 0) {
    currentCode = manifestData.codes[0].code;
    renderGlyphList();
    renderVariants();
    await loadCurrent();
  } else {
    setStatus("没有检测到可编辑数据。");
  }
}

function renderGlyphList() {
  glyphList.innerHTML = "";
  (manifestData.codes || []).forEach(item => {
    const btn = document.createElement("button");
    btn.className = "item-btn" + (item.code === currentCode ? " active" : "");
    const ch = item.char && String(item.char).trim() ? item.char : item.code;
    btn.innerHTML =
      `<div class="glyph-char">${escHtml(ch)}</div>
       <div class="glyph-code">${escHtml(item.code)}</div>
       <div class="glyph-count">${item.variants.length} 个版本</div>`;
    btn.onclick = async () => {
      currentCode = item.code;
      renderGlyphList();
      renderVariants();
      await loadCurrent();
    };
    glyphList.appendChild(btn);
  });
}

function renderVariants() {
  const item = (manifestData.codes || []).find(x => x.code === currentCode);
  variantSelect.innerHTML = "";
  if (!item) return;
  item.variants.forEach(v => {
    const opt = document.createElement("option");
    opt.value = v.name;
    opt.textContent = v.label;
    variantSelect.appendChild(opt);
  });
  if (!currentVariant || !item.variants.some(v => v.name === currentVariant)) {
    currentVariant = item.variants.length ? item.variants[0].name : null;
  }
  variantSelect.value = currentVariant || "";
}

variantSelect.addEventListener("change", async () => {
  currentVariant = variantSelect.value;
  await loadCurrent();
});

async function loadCurrent() {
  if (!currentCode || !currentVariant) return;

  setStatus(`正在加载 ${currentCode} / ${currentVariant} ...`);

  const r1 = await fetch(`/skeleton_json/${JOB_ID}/${currentCode}/${currentVariant}`);
  skeletonData = await r1.json();

  let ctrl = simplifyControls(skeletonData);

  const r2 = await fetch(`/mls_contours/${JOB_ID}/${currentCode}/${currentVariant}`);
  const contourData = await r2.json();

  if (!contourData.ok) {
    setStatus("轮廓提取失败：" + JSON.stringify(contourData));
    sampledContours = [];
    viewBox = skeletonData.viewBox || [0, 0, 1000, 1000];
  } else {
    sampledContours = contourData.contours || [];
    viewBox = contourData.viewBox || [0, 0, 1000, 1000];
  }

  ctrl = normalizeControlsToContours(ctrl, sampledContours);

  originalControls = deepClone(ctrl);
  currentControls = deepClone(ctrl);

  mainSvg.setAttribute("viewBox", viewBox.join(" "));

  selected = null;
  updateSelectedInfo();
  fitViewBox();
  redrawAll();

  setStatus(`已加载：${currentCode} / ${currentVariant}
变化后黑色字形预览：已启用
轮廓数量：${sampledContours.length}
控制点数量：${flatControls(currentControls).length}`);
}

strengthSlider.addEventListener("input", () => {
  strengthVal.textContent = Number(strengthSlider.value).toFixed(2);
  redrawAll();
});
cohesionSlider.addEventListener("input", () => {
  cohesionVal.textContent = Number(cohesionSlider.value).toFixed(2);
});
radiusSlider.addEventListener("input", () => {
  radiusVal.textContent = Number(radiusSlider.value).toFixed(2);
  redrawAll();
});

document.getElementById("showOriginal").addEventListener("change", redrawAll);
document.getElementById("showDeformed").addEventListener("change", redrawAll);
document.getElementById("showSkeleton").addEventListener("change", redrawAll);
document.getElementById("showHandles").addEventListener("change", redrawAll);

document.getElementById("fitBtn").addEventListener("click", () => {
  fitViewBox();
  redrawAll();
});

document.getElementById("resetBtn").addEventListener("click", () => {
  currentControls = deepClone(originalControls);
  selected = null;
  updateSelectedInfo();
  fitViewBox();
  redrawAll();
  setStatus("已恢复当前版本。");
});

async function saveCurrent() {
  const svgText = buildFullEditedSvg();
  const res = await fetch(`/save_outline_edit/${JOB_ID}/${currentCode}/${currentVariant}`, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({svg: svgText})
  });
  const data = await res.json();
  if (data.ok) setStatus("保存成功。");
  else setStatus("保存失败：" + JSON.stringify(data));
  return data;
}

document.getElementById("saveBtn").addEventListener("click", async () => {
  await saveCurrent();
});

document.getElementById("exportSvgBtn").addEventListener("click", async () => {
  const svgText = buildFullEditedSvg();
  const blob = new Blob([svgText], {type:"image/svg+xml;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${currentCode}_${currentVariant}_mls_edited.svg`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
});

document.getElementById("exportPngBtn").addEventListener("click", async () => {
  const svgText = buildFullEditedSvg();
  const svgBlob = new Blob([svgText], {type:"image/svg+xml;charset=utf-8"});
  const url = URL.createObjectURL(svgBlob);

  const img = new Image();
  img.onload = function() {
    const canvas = document.createElement("canvas");
    canvas.width = 1400;
    canvas.height = 1400;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    URL.revokeObjectURL(url);

    const pngUrl = canvas.toDataURL("image/png");
    const a = document.createElement("a");
    a.href = pngUrl;
    a.download = `${currentCode}_${currentVariant}_mls_edited.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };
  img.src = url;
});


// =========================================================
// FULL_GLYPH_FIRST_PATCH_V1
// 完整字形优先显示补丁：
// 1. 骨架中心线默认隐藏；
// 2. 黑色完整字形默认显示；
// 3. 如果后端轮廓提取失败，回退显示原始完整 SVG 字形；
// 4. 控制点叠加在完整字形上。
// =========================================================

let FULL_GLYPH_RAW_SVG = "";

function fullGlyphRawSvgToPaths(svgText, fillColor) {
  if (!svgText || !svgText.trim()) {
    return `<text x="20" y="80" font-size="40" fill="${fillColor}">未加载到完整字形</text>`;
  }

  const pathRegex = /<path[^>]*d="([^"]+)"[^>]*>/g;
  let m;
  const out = [];

  while ((m = pathRegex.exec(svgText)) !== null) {
    out.push(`<path d="${m[1]}" fill="${fillColor}" stroke="none" fill-rule="evenodd"/>`);
  }

  if (out.length > 0) {
    return out.join("\n");
  }

  // 如果不是 path 结构，尽量直接嵌入原 SVG 内容。
  // 这是兜底方案，主要保证不要只显示骨架线。
  const cleaned = svgText
    .replace(/<\?xml[\s\S]*?\?>/g, "")
    .replace(/<!DOCTYPE[\s\S]*?>/g, "")
    .replace(/<svg[^>]*>/, "")
    .replace(/<\/svg>/, "");

  return `<g fill="${fillColor}" color="${fillColor}">${cleaned}</g>`;
}

function buildOriginalContoursPathElements() {
  if (sampledContours && sampledContours.length > 0) {
    const d = sampledContours.map(c => contourToPath(c)).join(" ");
    return `<path d="${d}" fill="#d9d9d9" stroke="none" fill-rule="evenodd"/>`;
  }

  return fullGlyphRawSvgToPaths(FULL_GLYPH_RAW_SVG, "#d9d9d9");
}

function buildDeformedContoursPathElements() {
  if (sampledContours && sampledContours.length > 0) {
    const warpedContours = sampledContours.map(contour => {
      return contour.map(p => mlsWeightedPoint(p, originalControls, currentControls));
    });

    const d = warpedContours.map(c => contourToPath(c)).join(" ");
    return `<path d="${d}" fill="#000000" stroke="none" fill-rule="evenodd"/>`;
  }

  // 兜底：即使轮廓提取失败，也显示完整黑色字形，不再只显示骨架线。
  return fullGlyphRawSvgToPaths(FULL_GLYPH_RAW_SVG, "#000000");
}

async function loadCurrent() {
  if (!currentCode || !currentVariant) return;

  setStatus(`正在加载 ${currentCode} / ${currentVariant} ...`);

  const r1 = await fetch(`/skeleton_json/${JOB_ID}/${currentCode}/${currentVariant}`);
  skeletonData = await r1.json();

  let ctrl = simplifyControls(skeletonData);

  // 先加载完整 SVG，作为完整字形兜底显示。
  try {
    const rawRes = await fetch(`/raw_svg_variant/${JOB_ID}/${currentCode}/${currentVariant}`);
    FULL_GLYPH_RAW_SVG = await rawRes.text();
  } catch (e) {
    FULL_GLYPH_RAW_SVG = "";
  }

  // 再加载后端提取轮廓，用于实时变形。
  try {
    const r2 = await fetch(`/mls_contours/${JOB_ID}/${currentCode}/${currentVariant}`);
    const contourData = await r2.json();

    if (!contourData.ok) {
      sampledContours = [];
      viewBox = skeletonData.viewBox || [0, 0, 1000, 1000];
    } else {
      sampledContours = contourData.contours || [];
      viewBox = contourData.viewBox || [0, 0, 1000, 1000];
    }
  } catch (e) {
    sampledContours = [];
    viewBox = skeletonData.viewBox || [0, 0, 1000, 1000];
  }

  ctrl = normalizeControlsToContours(ctrl, sampledContours);

  originalControls = deepClone(ctrl);
  currentControls = deepClone(ctrl);

  mainSvg.setAttribute("viewBox", viewBox.join(" "));

  // 默认：显示完整黑色字形 + 控制点；不显示骨架中心线。
  const sk = document.getElementById("showSkeleton");
  if (sk) sk.checked = false;

  const def = document.getElementById("showDeformed");
  if (def) def.checked = true;

  selected = null;
  updateSelectedInfo();
  fitViewBox();
  redrawAll();

  setStatus(`已加载：${currentCode} / ${currentVariant}
显示模式：完整字形优先
变化后黑色字形预览：已启用
骨架中心线：默认隐藏
轮廓数量：${sampledContours.length}
控制点数量：${flatControls(currentControls).length}`);
}


loadManifest().catch(err => {
  console.error(err);
  setStatus("加载失败：" + err);
});
</script>
</body>
</html>
""".replace("__JOB_ID__", job_id)
