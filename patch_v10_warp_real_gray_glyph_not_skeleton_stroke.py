from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_v10_warp_real_gray_glyph")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

MARK = "// ===== V10_WARP_REAL_GRAY_GLYPH_NOT_SKELETON_STROKE ====="

if MARK in text:
    print("已经安装过 V10 补丁，不重复添加。")
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
// ===== V10_WARP_REAL_GRAY_GLYPH_NOT_SKELETON_STROKE =====

/*
  V10 目标：
  1. 不改变当前骨架中心线提取方式；
  2. 不再把骨架中心线生成成一个“粗线字形”；
  3. 直接让原始浅灰色 SVG 字形图像层随着骨架中心线发生网格形变；
  4. 中心线移动，浅灰色原字整体随之移动和扭转。
*/

function V10_bounds() {
  if (state.svgBounds) return state.svgBounds;
  if (state.basePoints && state.basePoints.length) return bounds(state.basePoints);
  if (state.points && state.points.length) return bounds(state.points);

  return {
    minX: 0,
    minY: 0,
    width: 300,
    height: 300,
    cx: 150,
    cy: 150
  };
}

function V10_hasMoved() {
  if (!state.basePoints || !state.points) return false;
  if (state.basePoints.length !== state.points.length) return false;

  for (let i = 0; i < state.points.length; i++) {
    const a = state.basePoints[i];
    const b = state.points[i];

    if (!a || !b) continue;

    if (Math.hypot(b.x - a.x, b.y - a.y) > 0.5) {
      return true;
    }
  }

  return false;
}

function V10_projectToSegment(q, a, b) {
  const vx = b.x - a.x;
  const vy = b.y - a.y;
  const len2 = vx * vx + vy * vy;

  if (len2 < 1e-8) {
    return {
      t: 0,
      x: a.x,
      y: a.y,
      dist: Math.hypot(q.x - a.x, q.y - a.y)
    };
  }

  let t = ((q.x - a.x) * vx + (q.y - a.y) * vy) / len2;
  t = Math.max(0, Math.min(1, t));

  const x = a.x + vx * t;
  const y = a.y + vy * t;

  return {
    t,
    x,
    y,
    dist: Math.hypot(q.x - x, q.y - y)
  };
}

function V10_deltaBySkeleton(q) {
  if (!state.basePoints || !state.points || !state.edges) {
    return {dx: 0, dy: 0};
  }

  if (state.basePoints.length !== state.points.length || !state.points.length) {
    return {dx: 0, dy: 0};
  }

  const b = V10_bounds();
  const diag = Math.hypot(Math.max(1, b.width), Math.max(1, b.height));

  const segRadius = Math.max(35, diag * 0.30);
  const pointRadius = Math.max(45, diag * 0.36);

  let sw = 0;
  let sx = 0;
  let sy = 0;

  /*
    线段位移场：让灰色完整字形跟随整条中心线的移动。
  */
  for (const [ia, ib] of state.edges) {
    const a0 = state.basePoints[ia];
    const b0 = state.basePoints[ib];
    const a1 = state.points[ia];
    const b1 = state.points[ib];

    if (!a0 || !b0 || !a1 || !b1) continue;

    const proj = V10_projectToSegment(q, a0, b0);
    const t = proj.t;

    const nx = a1.x * (1 - t) + b1.x * t;
    const ny = a1.y * (1 - t) + b1.y * t;

    const dx = nx - proj.x;
    const dy = ny - proj.y;

    const d = proj.dist;
    const w = Math.exp(-(d * d) / (2 * segRadius * segRadius));

    sw += w * 1.45;
    sx += dx * w * 1.45;
    sy += dy * w * 1.45;
  }

  /*
    控制点位移场：增强关键点附近的牵引。
  */
  for (let i = 0; i < state.basePoints.length; i++) {
    const p0 = state.basePoints[i];
    const p1 = state.points[i];

    if (!p0 || !p1) continue;

    const dx0 = q.x - p0.x;
    const dy0 = q.y - p0.y;
    const d2 = dx0 * dx0 + dy0 * dy0;

    const w = Math.exp(-d2 / (2 * pointRadius * pointRadius));

    sw += w * 0.55;
    sx += (p1.x - p0.x) * w * 0.55;
    sy += (p1.y - p0.y) * w * 0.55;
  }

  if (sw < 1e-8) {
    return {dx: 0, dy: 0};
  }

  let dx = sx / sw;
  let dy = sy / sw;

  /*
    限幅，防止图像块被拉飞。
  */
  const maxMove = diag * 0.65;
  const len = Math.hypot(dx, dy);

  if (len > maxMove) {
    dx = dx / len * maxMove;
    dy = dy / len * maxMove;
  }

  return {dx, dy};
}

function V10_drawOriginalReferenceVeryLight() {
  if (!state.rawSvgImage) return;

  const b = V10_bounds();

  ctx.save();
  ctx.globalAlpha = 0.055;
  ctx.drawImage(
    state.rawSvgImage,
    b.minX,
    b.minY,
    Math.max(1, b.width),
    Math.max(1, b.height)
  );
  ctx.restore();
}

function V10_drawWarpedRealGrayGlyph() {
  if (!state.rawSvgImage) return;

  const img = state.rawSvgImage;
  const b = V10_bounds();

  const iw = img.naturalWidth || img.width;
  const ih = img.naturalHeight || img.height;

  if (!iw || !ih) return;

  /*
    没拖动时，直接显示原字。
  */
  if (!V10_hasMoved()) {
    ctx.save();
    ctx.globalAlpha = 0.34;
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

  /*
    关键：这里画的是“原始 SVG 字形图像层”的变形，
    不是根据骨架重新生成粗线。
  */
  const cols = 76;
  const rows = Math.max(
    24,
    Math.round(cols * Math.max(1, b.height) / Math.max(1, b.width))
  );

  const sw = iw / cols;
  const sh = ih / rows;

  const dw = b.width / cols;
  const dh = b.height / rows;

  ctx.save();
  ctx.globalAlpha = 0.52;

  for (let gy = 0; gy < rows; gy++) {
    for (let gx = 0; gx < cols; gx++) {
      const sx = gx * sw;
      const sy = gy * sh;

      const wx = b.minX + gx * dw;
      const wy = b.minY + gy * dh;

      const q = {
        x: wx + dw / 2,
        y: wy + dh / 2
      };

      const delta = V10_deltaBySkeleton(q);

      /*
        只移动原始字形的小图像块，
        保证看起来是“原字形被骨架带着变”。
      */
      ctx.drawImage(
        img,
        sx,
        sy,
        sw + 1,
        sh + 1,
        wx + delta.dx,
        wy + delta.dy,
        dw + 0.9,
        dh + 0.9
      );
    }
  }

  ctx.restore();
}

function V10_drawSkeletonOverlay() {
  if (!state.points || !state.points.length) return;

  /*
    橙色影响带：只是辅助显示骨架控制范围，不代表字形。
  */
  if (ui.showPreview.checked) {
    ctx.save();
    ctx.strokeStyle = "rgba(222,184,135,0.34)";
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

  if (ui.showSkeleton.checked) {
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
}

function V10_drawHandles() {
  if (!state.handleIndices || !state.handleIndices.length) return;
  if (!ui.showPoints.checked) return;

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

/*
  覆盖 draw：
  注意：这里不再调用 V9_drawVariableWidthGlyph。
  也就是说，灰色字形不是骨架粗线，而是原始 SVG 字形图像层的变形。
*/
draw = function() {
  const rect = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);

  ctx.save();
  ctx.translate(state.offsetX, state.offsetY);
  ctx.scale(state.scale, state.scale);

  if (ui.showRawSvg.checked) {
    V10_drawOriginalReferenceVeryLight();
    V10_drawWarpedRealGrayGlyph();
  }

  V10_drawSkeletonOverlay();

  if (ui.showSupportPoints && ui.showSupportPoints.checked && typeof drawSupportPoints === "function") {
    drawSupportPoints();
  }

  V10_drawHandles();

  ctx.restore();
  updateStatus();
};

// ===== END_V10_WARP_REAL_GRAY_GLYPH_NOT_SKELETON_STROKE =====
'''

    text = text[:init_pos] + patch + "\n" + text[init_pos:]
    APP.write_text(text, encoding="utf-8")
    print("已安装 V10：灰色原字图像层跟随骨架中心线变形。")

print("准备重启服务。")
