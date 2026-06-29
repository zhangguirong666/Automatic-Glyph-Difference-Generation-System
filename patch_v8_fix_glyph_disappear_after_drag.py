from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_v8_fix_disappear")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

MARK = "// ===== V8_FIX_GLYPH_DISAPPEAR_AFTER_DRAG ====="

if MARK in text:
    print("已经安装过 V8 修复补丁，不重复添加。")
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
// ===== V8_FIX_GLYPH_DISAPPEAR_AFTER_DRAG =====

/*
  修复点：
  V7 使用三角网格仿射形变，拖动幅度较大时容易出现三角翻折 / 裁剪异常，
  导致灰色字形消失。

  V8 改成稳定版本：
  1. 原始字形永远作为极淡底图保留；
  2. 骨架变化后，用小网格块做稳定位移形变；
  3. 不再用 triangle clip，所以不会消失；
  4. 中心线提取方式不变。
*/

function V8_bounds() {
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

function V8_hasMoved() {
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

function V8_projectToSegment(q, a, b) {
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

function V8_deltaBySkeleton(q) {
  if (!state.basePoints || !state.points || !state.edges) {
    return {dx: 0, dy: 0};
  }

  if (state.basePoints.length !== state.points.length) {
    return {dx: 0, dy: 0};
  }

  if (!state.points.length) {
    return {dx: 0, dy: 0};
  }

  const b = V8_bounds();
  const diag = Math.hypot(Math.max(1, b.width), Math.max(1, b.height));

  const segRadius = Math.max(28, diag * 0.22);
  const pointRadius = Math.max(35, diag * 0.30);

  let sw = 0;
  let sx = 0;
  let sy = 0;

  /*
    第一层：线段位移场。
    让字形主要跟随骨架中心线的整体走向。
  */
  for (const [ia, ib] of state.edges) {
    const a0 = state.basePoints[ia];
    const b0 = state.basePoints[ib];
    const a1 = state.points[ia];
    const b1 = state.points[ib];

    if (!a0 || !b0 || !a1 || !b1) continue;

    const proj = V8_projectToSegment(q, a0, b0);
    const t = proj.t;

    const nx = a1.x * (1 - t) + b1.x * t;
    const ny = a1.y * (1 - t) + b1.y * t;

    const dx = nx - proj.x;
    const dy = ny - proj.y;

    const d = proj.dist;
    const w = Math.exp(-(d * d) / (2 * segRadius * segRadius));

    sw += w * 1.25;
    sx += dx * w * 1.25;
    sy += dy * w * 1.25;
  }

  /*
    第二层：控制点位移场。
    补充关键点对附近字形的牵引。
  */
  for (let i = 0; i < state.basePoints.length; i++) {
    const p0 = state.basePoints[i];
    const p1 = state.points[i];

    if (!p0 || !p1) continue;

    const dx0 = q.x - p0.x;
    const dy0 = q.y - p0.y;
    const d2 = dx0 * dx0 + dy0 * dy0;

    const w = Math.exp(-d2 / (2 * pointRadius * pointRadius));

    sw += w * 0.45;
    sx += (p1.x - p0.x) * w * 0.45;
    sy += (p1.y - p0.y) * w * 0.45;
  }

  if (sw < 1e-8) {
    return {dx: 0, dy: 0};
  }

  let dx = sx / sw;
  let dy = sy / sw;

  /*
    位移限幅。
    防止拖动过大时某些网格块被拉飞，造成字形消失。
  */
  const maxMove = diag * 0.45;
  const len = Math.hypot(dx, dy);

  if (len > maxMove) {
    dx = dx / len * maxMove;
    dy = dy / len * maxMove;
  }

  return {dx, dy};
}

function V8_drawBaseGlyphGhost() {
  if (!state.rawSvgImage) return;

  const b = V8_bounds();

  ctx.save();
  ctx.globalAlpha = 0.14;
  ctx.drawImage(
    state.rawSvgImage,
    b.minX,
    b.minY,
    Math.max(1, b.width),
    Math.max(1, b.height)
  );
  ctx.restore();
}

function V8_drawWarpedGlyphStable() {
  if (!state.rawSvgImage) return;

  const img = state.rawSvgImage;
  const b = V8_bounds();

  const iw = img.naturalWidth || img.width;
  const ih = img.naturalHeight || img.height;

  if (!iw || !ih) return;

  if (!V8_hasMoved()) {
    ctx.save();
    ctx.globalAlpha = 0.28;
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
    稳定网格块形变：
    每一个小块根据骨架位移场移动。
    不做三角裁剪，所以不会因为网格翻折导致字形消失。
  */
  const cols = 58;
  const rows = Math.max(20, Math.round(cols * Math.max(1, b.height) / Math.max(1, b.width)));

  const sw = iw / cols;
  const sh = ih / rows;

  const dw = b.width / cols;
  const dh = b.height / rows;

  ctx.save();
  ctx.globalAlpha = 0.46;

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

      const delta = V8_deltaBySkeleton(q);

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

function V8_drawSkeletonLayer() {
  if (!state.points || !state.points.length) return;

  if (ui.showPreview.checked) {
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

function V8_drawHandlesLayer() {
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

/*
  最终覆盖 draw：
  1. 永远画一个极淡原字底图，防止视觉消失；
  2. 再画稳定的骨架驱动变形字形；
  3. 最上层画骨架线和控制点。
*/
draw = function() {
  const rect = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);

  ctx.save();
  ctx.translate(state.offsetX, state.offsetY);
  ctx.scale(state.scale, state.scale);

  if (ui.showRawSvg.checked) {
    V8_drawBaseGlyphGhost();
    V8_drawWarpedGlyphStable();
  }

  V8_drawSkeletonLayer();

  if (ui.showSupportPoints && ui.showSupportPoints.checked && typeof drawSupportPoints === "function") {
    drawSupportPoints();
  }

  if (ui.showPoints.checked) {
    V8_drawHandlesLayer();
  }

  ctx.restore();
  updateStatus();
};

// ===== END_V8_FIX_GLYPH_DISAPPEAR_AFTER_DRAG =====
'''

    text = text[:init_pos] + patch + "\n" + text[init_pos:]
    APP.write_text(text, encoding="utf-8")
    print("已安装 V8：修复拖动后灰色字形消失。")

print("准备重启服务。")
