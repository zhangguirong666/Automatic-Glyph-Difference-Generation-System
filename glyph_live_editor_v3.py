from __future__ import annotations


def build_glyph_live_editor_html(job_id: str) -> str:
    return r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>完整字形实时变形编辑器</title>
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
  <h1>完整字形实时变形编辑器</h1>
  <div class="sub">不再单独显示骨架中心线。显示完整字形，少量控制点叠加其上；拖动一个点，其他控制点和完整字形同步变化。</div>
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
      <g id="controlLineLayer"></g>
      <g id="controlPointLayer"></g>
    </svg>

    <div class="note">
      操作方式：<br>
      1. 黑色是编辑后的完整字形实时预览；<br>
      2. 白色圆点是少量字形控制点；<br>
      3. 拖动任意一个控制点，其他控制点会同步联动；<br>
      4. 黑色完整字形会根据所有控制点重新计算变形；<br>
      5. 浅灰色原始字形可在右侧开启。
    </div>
  </div>

  <div class="panel">
    <div class="label">显示控制</div>

    <label class="chk"><input type="checkbox" id="showOriginal">显示原始字形（浅灰）</label>
    <label class="chk"><input type="checkbox" id="showDeformed" checked>显示编辑后字形（黑）</label>
    <label class="chk"><input type="checkbox" id="showControlLines">显示控制点关联线</label>
    <label class="chk"><input type="checkbox" id="showControlPoints" checked>显示控制点</label>

    <div class="row">
      <div class="label">字形变化强度</div>
      <input type="range" id="strengthSlider" min="0" max="1" step="0.05" value="0.90">
      <div id="strengthVal" class="small">0.90</div>
    </div>

    <div class="row">
      <div class="label">控制点联动强度</div>
      <input type="range" id="cohesionSlider" min="0" max="1" step="0.05" value="0.88">
      <div id="cohesionVal" class="small">0.88</div>
    </div>

    <div class="row">
      <div class="label">影响范围</div>
      <input type="range" id="radiusSlider" min="0.4" max="2.5" step="0.05" value="1.55">
      <div id="radiusVal" class="small">1.55</div>
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
      这版不是骨架线编辑。<br>
      它是完整字形控制点编辑：控制点少，所有点有关联，拖一个点时完整黑色字形实时变化。
    </div>
  </div>
</div>

<script>
const JOB_ID = "__JOB_ID__";

let manifestData = null;
let currentCode = null;
let currentVariant = null;

let viewBox = [0, 0, 1000, 1000];
let contours = [];

let originalControls = [];
let currentControls = [];

let selectedIndex = null;
let dragging = false;
let dragStart = null;
let dragBaseControls = null;

const glyphList = document.getElementById("glyphList");
const variantSelect = document.getElementById("variantSelect");
const mainSvg = document.getElementById("mainSvg");

const originalLayer = document.getElementById("originalLayer");
const deformedLayer = document.getElementById("deformedLayer");
const controlLineLayer = document.getElementById("controlLineLayer");
const controlPointLayer = document.getElementById("controlPointLayer");

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
function escHtml(s) {
  return String(s ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}
function setStatus(msg) {
  statusBox.textContent = msg;
}

function bboxOfPoints(points) {
  if (!points || !points.length) return {minX:0,minY:0,maxX:1000,maxY:1000,w:1000,h:1000};
  let minX = points[0].x, minY = points[0].y, maxX = points[0].x, maxY = points[0].y;
  for (const p of points) {
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x);
    maxY = Math.max(maxY, p.y);
  }
  return {minX, minY, maxX, maxY, w:maxX-minX, h:maxY-minY};
}
function bboxOfContours(cs) {
  const pts = [];
  cs.forEach(c => c.forEach(p => pts.push(p)));
  return bboxOfPoints(pts);
}
function unionBbox(a, b) {
  const minX = Math.min(a.minX, b.minX);
  const minY = Math.min(a.minY, b.minY);
  const maxX = Math.max(a.maxX, b.maxX);
  const maxY = Math.max(a.maxY, b.maxY);
  return {minX, minY, maxX, maxY, w:maxX-minX, h:maxY-minY};
}

function makeGlobalControlsFromContours(cs) {
  const bb = bboxOfContours(cs);
  const cx = bb.minX + bb.w / 2;
  const cy = bb.minY + bb.h / 2;

  // 控制点数量固定少量：7 个
  return [
    {name:"中心", x:cx, y:cy},
    {name:"上", x:cx, y:bb.minY},
    {name:"下", x:cx, y:bb.maxY},
    {name:"左", x:bb.minX, y:cy},
    {name:"右", x:bb.maxX, y:cy},
    {name:"左上", x:bb.minX + bb.w*0.25, y:bb.minY + bb.h*0.20},
    {name:"右下", x:bb.minX + bb.w*0.75, y:bb.minY + bb.h*0.80}
  ];
}

function fitViewBox() {
  if (!contours.length) {
    mainSvg.setAttribute("viewBox", viewBox.join(" "));
    return;
  }

  const gb = bboxOfContours(contours);
  const cb = bboxOfPoints(currentControls);
  const ub = unionBbox(gb, cb);
  const pad = Math.max(40, Math.max(ub.w, ub.h) * 0.18);
  mainSvg.setAttribute("viewBox", `${ub.minX-pad} ${ub.minY-pad} ${ub.w+pad*2} ${ub.h+pad*2}`);
}

function makeMovedControls(base, selectedIdx, dx, dy) {
  const out = deepClone(base);
  const cohesion = Number(cohesionSlider.value || 0.88);

  const ref = base[selectedIdx];
  const bb = bboxOfPoints(base);
  const diag = Math.max(1, Math.hypot(bb.w, bb.h));
  const radius = diag * (0.95 + 1.20 * cohesion);

  out.forEach((p, i) => {
    if (i === selectedIdx) {
      p.x = base[i].x + dx;
      p.y = base[i].y + dy;
      return;
    }

    const bp = base[i];
    const d = Math.hypot(bp.x - ref.x, bp.y - ref.y);
    const local = Math.max(0, 1 - d / radius);

    // 关键：所有控制点都有最低联动，不会只有一个点动
    let w = 0.36 * cohesion + Math.pow(local, 1.5) * 0.58 * cohesion;
    w = Math.max(0.20 * cohesion, Math.min(0.92, w));

    p.x = base[i].x + dx * w;
    p.y = base[i].y + dy * w;
  });

  return out;
}

function deformPoint(p, baseControls, movedControls) {
  if (!baseControls.length || baseControls.length !== movedControls.length) return p;

  const strength = Number(strengthSlider.value || 0.90);
  const radiusFactor = Number(radiusSlider.value || 1.55);

  const bb = bboxOfPoints(baseControls);
  const diag = Math.max(1, Math.hypot(bb.w, bb.h));
  const radius = diag * radiusFactor;

  let sw = 0;
  let sx = 0;
  let sy = 0;

  let avgDx = 0;
  let avgDy = 0;

  for (let i=0; i<baseControls.length; i++) {
    avgDx += movedControls[i].x - baseControls[i].x;
    avgDy += movedControls[i].y - baseControls[i].y;
  }

  avgDx /= baseControls.length;
  avgDy /= baseControls.length;

  for (let i=0; i<baseControls.length; i++) {
    const b = baseControls[i];
    const m = movedControls[i];

    const dx = m.x - b.x;
    const dy = m.y - b.y;

    const d = Math.hypot(p.x - b.x, p.y - b.y);
    const local = Math.max(0.05, 1 - d / radius);

    // 所有控制点都会影响该轮廓点：基础权重 + 距离权重
    const w = 0.16 + Math.pow(local, 2.0) * 2.6;

    sw += w;
    sx += dx * w;
    sy += dy * w;
  }

  let dx = sx / sw;
  let dy = sy / sw;

  // 加入平均位移，保证整体协调
  dx = dx * 0.74 + avgDx * 0.26;
  dy = dy * 0.74 + avgDy * 0.26;

  return {
    x: p.x + dx * strength,
    y: p.y + dy * strength
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

function buildOriginalPath() {
  if (!contours.length) {
    return `<text x="${viewBox[0]+20}" y="${viewBox[1]+80}" font-size="40" fill="#999">未提取到完整字形轮廓</text>`;
  }
  const d = contours.map(c => contourToPath(c)).join(" ");
  return `<path d="${d}" fill="#d9d9d9" stroke="none" fill-rule="evenodd"/>`;
}

function buildDeformedPath() {
  if (!contours.length) {
    return `<text x="${viewBox[0]+20}" y="${viewBox[1]+80}" font-size="40" fill="#000">未提取到完整字形轮廓</text>`;
  }

  const warped = contours.map(c => c.map(p => deformPoint(p, originalControls, currentControls)));
  const d = warped.map(c => contourToPath(c)).join(" ");
  return `<path d="${d}" fill="#000000" stroke="none" fill-rule="evenodd"/>`;
}

function buildFullSvg() {
  const vb = mainSvg.getAttribute("viewBox") || viewBox.join(" ");
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${vb}">${buildDeformedPath()}</svg>`;
}

function drawControls() {
  controlLineLayer.innerHTML = "";
  controlPointLayer.innerHTML = "";

  const showLines = document.getElementById("showControlLines").checked;
  const showPoints = document.getElementById("showControlPoints").checked;

  if (showLines && currentControls.length) {
    const center = currentControls[0];

    for (let i=1; i<currentControls.length; i++) {
      const p = currentControls[i];
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", center.x);
      line.setAttribute("y1", center.y);
      line.setAttribute("x2", p.x);
      line.setAttribute("y2", p.y);
      line.setAttribute("stroke", "#ff4d4f");
      line.setAttribute("stroke-width", "1.8");
      line.setAttribute("stroke-dasharray", "4 4");
      controlLineLayer.appendChild(line);
    }
  }

  if (showPoints) {
    currentControls.forEach((p, i) => {
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      const active = selectedIndex === i;
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

        selectedIndex = i;
        dragging = true;
        dragStart = getMouseSvgPoint(ev);
        dragBaseControls = deepClone(currentControls);

        updateSelectedInfo();
        redrawAll();
      });

      controlPointLayer.appendChild(c);
    });
  }
}

function redrawAll() {
  originalLayer.style.display = document.getElementById("showOriginal").checked ? "" : "none";
  deformedLayer.style.display = document.getElementById("showDeformed").checked ? "" : "none";
  controlLineLayer.style.display = document.getElementById("showControlLines").checked ? "" : "none";
  controlPointLayer.style.display = document.getElementById("showControlPoints").checked ? "" : "none";

  originalLayer.innerHTML = buildOriginalPath();
  deformedLayer.innerHTML = buildDeformedPath();
  drawControls();
}

function getMouseSvgPoint(evt) {
  const pt = mainSvg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(mainSvg.getScreenCTM().inverse());
}

mainSvg.addEventListener("pointermove", evt => {
  if (!dragging || selectedIndex === null || !dragBaseControls) return;

  const now = getMouseSvgPoint(evt);
  const dx = now.x - dragStart.x;
  const dy = now.y - dragStart.y;

  currentControls = makeMovedControls(dragBaseControls, selectedIndex, dx, dy);

  updateSelectedInfo();
  redrawAll();
});

window.addEventListener("pointerup", () => {
  dragging = false;
  dragStart = null;
  dragBaseControls = null;
});

function updateSelectedInfo() {
  if (selectedIndex === null || !currentControls[selectedIndex]) {
    selectedInfo.textContent = "未选中任何点";
    return;
  }

  const p = currentControls[selectedIndex];

  selectedInfo.textContent =
`字符: ${currentCode}
版本: ${currentVariant}
控制点: ${selectedIndex}
名称: ${p.name || ""}
x: ${p.x.toFixed(2)}
y: ${p.y.toFixed(2)}

说明:
拖动一个控制点时，
其他控制点会一起移动，
黑色完整字形会实时重新变形。`;
}

async function loadManifest() {
  const res = await fetch(`/api/glyph_live_v3/manifest/${JOB_ID}`);
  manifestData = await res.json();

  renderGlyphList();

  if (manifestData.codes && manifestData.codes.length > 0) {
    currentCode = manifestData.codes[0].code;
    renderGlyphList();
    renderVariants();
    await loadCurrent();
  } else {
    setStatus("没有检测到可编辑字形。");
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

  const res = await fetch(`/api/glyph_live_v3/data/${JOB_ID}/${currentCode}/${currentVariant}`);
  const data = await res.json();

  if (!data.ok) {
    setStatus("加载失败：" + JSON.stringify(data));
    contours = [];
    viewBox = [0, 0, 1000, 1000];
    originalControls = [];
    currentControls = [];
    redrawAll();
    return;
  }

  contours = data.contours || [];
  viewBox = data.viewBox || [0, 0, 1000, 1000];

  originalControls = makeGlobalControlsFromContours(contours);
  currentControls = deepClone(originalControls);

  selectedIndex = null;

  mainSvg.setAttribute("viewBox", viewBox.join(" "));
  fitViewBox();

  updateSelectedInfo();
  redrawAll();

  setStatus(`已加载：${currentCode} / ${currentVariant}
完整字形轮廓数量：${contours.length}
控制点数量：${currentControls.length}
编辑后黑色字形预览：已启用
控制点联动：已启用`);
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
document.getElementById("showControlLines").addEventListener("change", redrawAll);
document.getElementById("showControlPoints").addEventListener("change", redrawAll);

document.getElementById("fitBtn").addEventListener("click", () => {
  fitViewBox();
  redrawAll();
});

document.getElementById("resetBtn").addEventListener("click", () => {
  currentControls = deepClone(originalControls);
  selectedIndex = null;
  updateSelectedInfo();
  fitViewBox();
  redrawAll();
  setStatus("已恢复当前版本。");
});

document.getElementById("saveBtn").addEventListener("click", async () => {
  const svgText = buildFullSvg();
  const res = await fetch(`/api/glyph_live_v3/save/${JOB_ID}/${currentCode}/${currentVariant}`, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({svg: svgText})
  });

  const data = await res.json();
  if (data.ok) setStatus("保存成功：" + data.saved);
  else setStatus("保存失败：" + JSON.stringify(data));
});

document.getElementById("exportSvgBtn").addEventListener("click", () => {
  const svgText = buildFullSvg();
  const blob = new Blob([svgText], {type:"image/svg+xml;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${currentCode}_${currentVariant}_edited.svg`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
});

document.getElementById("exportPngBtn").addEventListener("click", () => {
  const svgText = buildFullSvg();
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
    a.download = `${currentCode}_${currentVariant}_edited.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };
  img.src = url;
});

loadManifest().catch(err => {
  console.error(err);
  setStatus("加载失败：" + err);
});
</script>
</body>
</html>
""".replace("__JOB_ID__", job_id)
