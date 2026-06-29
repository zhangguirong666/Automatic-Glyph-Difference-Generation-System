from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_v7_integrated_glyph_follow_centerline")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

MARK = "// ===== V7_INTEGRATED_GLYPH_FOLLOW_CENTERLINE_PATCH ====="

if MARK in text:
    print("已经安装过 V7 补丁，不重复添加。")
else:
    route_pos = text.find("# ===== ELASTIC_SKELETON_EDITOR_V5_PROXY_ROUTE =====")
    if route_pos == -1:
        raise SystemExit("错误：没有找到 V5 编辑器路由。")

    script_end = text.find("</script>", route_pos)
    if script_end == -1:
        raise SystemExit("错误：没有找到 </script>。")

    init_pos = text.rfind("init();", route_pos, script_end)
    if init_pos == -1:
        raise SystemExit("错误：没有找到 init();。")

    patch = r'''
// ===== V7_INTEGRATED_GLYPH_FOLLOW_CENTERLINE_PATCH =====

/*
  目标：
  保留现有中心线提取方式，只改字形跟随方式。
  中心线与字形是一体的：
  - 中心线动
  - 灰色字形本体同步动
  - 不再把“静态原字”和“动态中心线”割裂显示
*/

function V7_glyphBounds() {
  if (state.svgBounds) return state.svgBounds;
  if (state.basePoints && state.basePoints.length) return bounds(state.basePoints);
  if (state.points && state.points.length) return bounds(state.points);
  return {minX:0, minY:0, width:300, height:300, cx:150, cy:150};
}

function V7_hasDeformation() {
  if (!state.basePoints || !state.points) return false;
  if (state.basePoints.length !== state.points.length) return false;

  for (let i = 0; i < state.points.length; i++) {
    const a = state.basePoints[i];
    const b = state.points[i];
    if (!a || !b) continue;
    if (Math.hypot(b.x - a.x, b.y - a.y) > 0.3) return true;
  }
  return false;
}

function V7_pointDelta(q, radiusScale = 0.24) {
  if (!state.basePoints || !state.points) return {dx:0, dy:0};
  if (state.basePoints.length !== state.points.length) return {dx:0, dy:0};

  const b = V7_glyphBounds();
  const diag = Math.hypot(Math.max(1, b.width), Math.max(1, b.height));
  const radius = Math.max(24, diag * radiusScale);

  let sw = 0, sx = 0, sy = 0;

  for (let i = 0; i < state.basePoints.length; i++) {
    const p0 = state.basePoints[i];
    const p1 = state.points[i];
    if (!p0 || !p1) continue;

    const dx0 = q.x - p0.x;
    const dy0 = q.y - p0.y;
    const d2 = dx0 * dx0 + dy0 * dy0;
    const w = Math.exp(-d2 / (2 * radius * radius));

    sw += w;
    sx += (p1.x - p0.x) * w;
    sy += (p1.y - p0.y) * w;
  }

  if (sw < 1e-8) return {dx:0, dy:0};
  return {dx: sx / sw, dy: sy / sw};
}

function V7_projectToSegment(q, a, b) {
  const vx = b.x - a.x;
  const vy = b.y - a.y;
  const len2 = vx * vx + vy * vy;

  if (len2 < 1e-8) {
    return {t:0, x:a.x, y:a.y, dist:Math.hypot(q.x - a.x, q.y - a.y)};
  }

  let t = ((q.x - a.x) * vx + (q.y - a.y) * vy) / len2;
  t = Math.max(0, Math.min(1, t));

  const px = a.x + vx * t;
  const py = a.y + vy * t;

  return {
    t,
    x: px,
    y: py,
    dist: Math.hypot(q.x - px, q.y - py)
  };
}

function V7_segmentDelta(q, radiusScale = 0.20) {
  if (!state.basePoints || !state.points || !state.edges) return {dx:0, dy:0};
  if (state.basePoints.length !== state.points.length) return {dx:0, dy:0};

  const b = V7_glyphBounds();
  const diag = Math.hypot(Math.max(1, b.width), Math.max(1, b.height));
  const radius = Math.max(20, diag * radiusScale);

  let sw = 0, sx = 0, sy = 0;

  for (const [ia, ib] of state.edges) {
    const a0 = state.basePoints[ia];
    const b0 = state.basePoints[ib];
    const a1 = state.points[ia];
    const b1 = state.points[ib];

    if (!a0 || !b0 || !a1 || !b1) continue;

    const proj = V7_projectToSegment(q, a0, b0);
    const t = proj.t;

    const mx = a1.x * (1 - t) + b1.x * t;
    const my = a1.y * (1 - t) + b1.y * t;

    const dx = mx - proj.x;
    const dy = my - proj.y;

    const d = proj.dist;
    const w = Math.exp(-(d * d) / (2 * radius * radius));

    sw += w;
    sx += dx * w;
    sy += dy * w;
  }

  if (sw < 1e-8) return {dx:0, dy:0};
  return {dx: sx / sw, dy: sy / sw};
}

function V7_warpPoint(q) {
  const dSeg = V7_segmentDelta(q, 0.19);
  const dPt  = V7_pointDelta(q, 0.25);

  return {
    x: q.x + dSeg.dx * 0.72 + dPt.dx * 0.28,
    y: q.y + dSeg.dy * 0.72 + dPt.dy * 0.28
  };
}

function V7_drawImageTriangle(img,
  sx0, sy0, sx1, sy1, sx2, sy2,
  dx0, dy0, dx1, dy1, dx2, dy2,
  alpha = 0.26
) {
  const denom = sx0 * (sy1 - sy2) + sx1 * (sy2 - sy0) + sx2 * (sy0 - sy1);
  if (Math.abs(denom) < 1e-8) return;

  const a = (dx0 * (sy1 - sy2) + dx1 * (sy2 - sy0) + dx2 * (sy0 - sy1)) / denom;
  const b = (dy0 * (sy1 - sy2) + dy1 * (sy2 - sy0) + dy2 * (sy0 - sy1)) / denom;
  const c = (dx0 * (sx2 - sx1) + dx1 * (sx0 - sx2) + dx2 * (sx1 - sx0)) / denom;
  const d = (dy0 * (sx2 - sx1) + dy1 * (sx0 - sx2) + dy2 * (sx1 - sx0)) / denom;
  const e = (dx0 * (sx1 * sy2 - sx2 * sy1) + dx1 * (sx2 * sy0 - sx0 * sy2) + dx2 * (sx0 * sy1 - sx1 * sy0)) / denom;
  const f = (dy0 * (sx1 * sy2 - sx2 * sy1) + dy1 * (sx2 * sy0 - sx0 * sy2) + dy2 * (sx0 * sy1 - sx1 * sy0)) / denom;

  ctx.save();
  ctx.globalAlpha = alpha;

  ctx.beginPath();
  ctx.moveTo(dx0, dy0);
  ctx.lineTo(dx1, dy1);
  ctx.lineTo(dx2, dy2);
  ctx.closePath();
  ctx.clip();

  ctx.transform(a, b, c, d, e, f);
  ctx.drawImage(img, 0, 0);
  ctx.restore();
}

function V7_drawIntegratedGlyph() {
  if (!state.rawSvgImage) return;

  const img = state.rawSvgImage;
  const b = V7_glyphBounds();

  const iw = img.naturalWidth || img.width;
  const ih = img.naturalHeight || img.height;
  if (!iw || !ih) return;

  // 没有发生骨架位移时，直接画原字
  if (!V7_hasDeformation()) {
    ctx.save();
    ctx.globalAlpha = 0.22;
    ctx.drawImage(
      img,
      b.minX,
      b.minY,
      Math.max(1, b.width),
      Math.max(1, b.height)
    );
    ctx.restore();
    return;
  }

  // 用三角网格仿射形变，让字形和中心线更像一体
  const cols = 26;
  const rows = Math.max(14, Math.round(cols * Math.max(1, b.height) / Math.max(1, b.width)));

  const srcGrid = [];
  const dstGrid = [];

  for (let gy = 0; gy <= rows; gy++) {
    const rowS = [];
    const rowD = [];

    for (let gx = 0; gx <= cols; gx++) {
      const tx = gx / cols;
      const ty = gy / rows;

      const sx = tx * iw;
      const sy = ty * ih;

      const wx = b.minX + tx * b.width;
      const wy = b.minY + ty * b.height;

      const warped = V7_warpPoint({x: wx, y: wy});

      rowS.push({x: sx, y: sy});
      rowD.push({x: warped.x, y: warped.y});
    }

    srcGrid.push(rowS);
    dstGrid.push(rowD);
  }

  for (let gy = 0; gy < rows; gy++) {
    for (let gx = 0; gx < cols; gx++) {
      const s00 = srcGrid[gy][gx];
      const s10 = srcGrid[gy][gx + 1];
      const s01 = srcGrid[gy + 1][gx];
      const s11 = srcGrid[gy + 1][gx + 1];

      const d00 = dstGrid[gy][gx];
      const d10 = dstGrid[gy][gx + 1];
      const d01 = dstGrid[gy + 1][gx];
      const d11 = dstGrid[gy + 1][gx + 1];

      // 三角 1
      V7_drawImageTriangle(
        img,
        s00.x, s00.y, s10.x, s10.y, s11.x, s11.y,
        d00.x, d00.y, d10.x, d10.y, d11.x, d11.y,
        0.34
      );

      // 三角 2
      V7_drawImageTriangle(
        img,
        s00.x, s00.y, s11.x, s11.y, s01.x, s01.y,
        d00.x, d00.y, d11.x, d11.y, d01.x, d01.y,
        0.34
      );
    }
  }
}

function V7_drawCenterlineOverlay() {
  if (!state.points || !state.points.length) return;

  // 橙色影响带
  ctx.save();
  ctx.strokeStyle = "rgba(222,184,135,0.38)";
  ctx.lineWidth = Number(ui.strokeThickness.value);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  for (const [a, b] of state.edges) {
    const p1 = state.points[a];
    const p2 = state.points[b];
    if (!p1 || !p2) continue;

    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
  }
  ctx.restore();

  // 红色中心线
  ctx.save();
  ctx.strokeStyle = "#ff5f5f";
  ctx.lineWidth = 2.8 / state.scale;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  for (const [a, b] of state.edges) {
    const p1 = state.points[a];
    const p2 = state.points[b];
    if (!p1 || !p2) continue;

    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
  }
  ctx.restore();
}

function V7_drawHandleOverlay() {
  if (!state.handleIndices || !state.handleIndices.length) return;

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

// 覆盖 draw：不再画“静态原字 + 动态前景”的分离结构，改为一体化变形字形
draw = function() {
  const rect = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);

  ctx.save();
  ctx.translate(state.offsetX, state.offsetY);
  ctx.scale(state.scale, state.scale);

  if (ui.showRawSvg.checked) {
    V7_drawIntegratedGlyph();
  }

  if (ui.showPreview.checked || ui.showSkeleton.checked) {
    V7_drawCenterlineOverlay();
  }

  if (ui.showSupportPoints && ui.showSupportPoints.checked && typeof drawSupportPoints === "function") {
    drawSupportPoints();
  }

  if (ui.showPoints.checked) {
    V7_drawHandleOverlay();
  }

  ctx.restore();
  updateStatus();
};

// ===== END_V7_INTEGRATED_GLYPH_FOLLOW_CENTERLINE_PATCH =====
'''

    text = text[:init_pos] + patch + "\n" + text[init_pos:]
    APP.write_text(text, encoding="utf-8")
    print("已安装 V7：字形与中心线一体联动补丁。")

print("准备重启服务。")
