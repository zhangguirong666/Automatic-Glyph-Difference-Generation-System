from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_keep_centerline_warp_original")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

MARK = "// ===== V6_KEEP_CENTERLINE_WARP_ORIGINAL_GLYPH_PATCH ====="

if MARK in text:
    print("已经安装过该补丁，不重复添加。")
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
// ===== V6_KEEP_CENTERLINE_WARP_ORIGINAL_GLYPH_PATCH =====

/*
  本补丁不改变骨架中心线提取方式。
  只改变显示与联动：
  1. 原始 SVG 字形作为灰色底图显示在后面；
  2. 当前编辑后的字形根据骨架中心线位移进行网格形变；
  3. 红色中心线和关键点显示在最上层。
*/

function V6_patchGlyphBounds() {
  if (state.svgBounds) return state.svgBounds;
  if (state.basePoints && state.basePoints.length) return bounds(state.basePoints);
  if (state.points && state.points.length) return bounds(state.points);
  return {minX: 0, minY: 0, width: 300, height: 300, cx: 150, cy: 150};
}

function V6_skeletonMovedAmount() {
  if (!state.basePoints || !state.points) return 0;
  if (state.basePoints.length !== state.points.length) return 0;

  let maxD = 0;

  for (let i = 0; i < state.points.length; i++) {
    const a = state.basePoints[i];
    const b = state.points[i];
    if (!a || !b) continue;

    maxD = Math.max(maxD, Math.hypot(b.x - a.x, b.y - a.y));
  }

  return maxD;
}

function V6_deltaFromSkeleton(q, radiusScale = 0.30) {
  if (!state.basePoints || !state.points) return {dx: 0, dy: 0};
  if (state.basePoints.length !== state.points.length) return {dx: 0, dy: 0};
  if (!state.points.length) return {dx: 0, dy: 0};

  const b = V6_patchGlyphBounds();
  const diag = Math.hypot(b.width || 1, b.height || 1);
  const radius = Math.max(30, diag * radiusScale);

  let sw = 0;
  let sx = 0;
  let sy = 0;

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

  if (sw < 1e-8) return {dx: 0, dy: 0};

  return {
    dx: sx / sw,
    dy: sy / sw
  };
}

function V6_drawOriginalGlyphBehind() {
  if (!state.rawSvgImage) return;

  const b = V6_patchGlyphBounds();

  ctx.save();
  ctx.globalAlpha = 0.18;
  ctx.drawImage(
    state.rawSvgImage,
    b.minX,
    b.minY,
    Math.max(1, b.width),
    Math.max(1, b.height)
  );
  ctx.restore();
}

function V6_drawWarpedOriginalGlyph() {
  if (!state.rawSvgImage) return;

  const b = V6_patchGlyphBounds();
  const img = state.rawSvgImage;

  const iw = img.naturalWidth || img.width;
  const ih = img.naturalHeight || img.height;

  if (!iw || !ih) return;

  const moved = V6_skeletonMovedAmount();

  // 没有拖动时，当前字形与原字重合，不重复画太重。
  if (moved < 0.5) {
    return;
  }

  const cols = 52;
  const rows = Math.max(20, Math.round(cols * Math.max(1, b.height) / Math.max(1, b.width)));

  const sw = iw / cols;
  const sh = ih / rows;
  const dw = b.width / cols;
  const dh = b.height / rows;

  ctx.save();
  ctx.globalAlpha = 0.42;

  for (let gy = 0; gy < rows; gy++) {
    for (let gx = 0; gx < cols; gx++) {
      const sx = gx * sw;
      const sy = gy * sh;

      const wx = b.minX + gx * dw;
      const wy = b.minY + gy * dh;

      const center = {
        x: wx + dw / 2,
        y: wy + dh / 2
      };

      const delta = V6_deltaFromSkeleton(center, 0.28);

      ctx.drawImage(
        img,
        sx,
        sy,
        sw + 1,
        sh + 1,
        wx + delta.dx,
        wy + delta.dy,
        dw + 0.8,
        dh + 0.8
      );
    }
  }

  ctx.restore();
}

function V6_drawSkeletonInfluenceStroke() {
  if (!state.points || !state.points.length) return;

  ctx.save();
  ctx.strokeStyle = "rgba(222,184,135,0.42)";
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
}

function V6_drawCenterline() {
  if (!state.points || !state.points.length) return;

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

function V6_drawKeyHandles() {
  if (!state.handleIndices || !state.handleIndices.length) return;

  const r = 6.3 / state.scale;

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

draw = function() {
  const rect = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);

  ctx.save();
  ctx.translate(state.offsetX, state.offsetY);
  ctx.scale(state.scale, state.scale);

  /*
    绘制顺序：
    1. 原始字形灰色底图；
    2. 跟随骨架移动后的字形；
    3. 骨架影响范围；
    4. 红色中心线；
    5. 关键控制点。
  */

  if (ui.showRawSvg.checked) {
    V6_drawOriginalGlyphBehind();
    V6_drawWarpedOriginalGlyph();
  }

  if (ui.showPreview.checked) {
    V6_drawSkeletonInfluenceStroke();
  }

  if (ui.showSkeleton.checked) {
    V6_drawCenterline();
  }

  if (ui.showSupportPoints && ui.showSupportPoints.checked && typeof drawSupportPoints === "function") {
    drawSupportPoints();
  }

  if (ui.showPoints.checked) {
    V6_drawKeyHandles();
  }

  ctx.restore();
  updateStatus();
};

// ===== END_V6_KEEP_CENTERLINE_WARP_ORIGINAL_GLYPH_PATCH =====

'''

    text = text[:init_pos] + patch + "\n" + text[init_pos:]
    APP.write_text(text, encoding="utf-8")
    print("已安装：保留当前中心线 + 原字背后显示 + 原字随骨架变形。")

print("准备重启 FastAPI。")
