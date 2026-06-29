from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_v9_variable_width_glyph")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

MARK = "// ===== V9_VARIABLE_WIDTH_GLYPH_FOLLOW_SKELETON_PATCH ====="

if MARK in text:
    print("已经安装过 V9 补丁，不重复添加。")
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
// ===== V9_VARIABLE_WIDTH_GLYPH_FOLLOW_SKELETON_PATCH =====

/*
  V9 核心：
  不改变中心线提取方式。
  把灰色字形改成“由骨架中心线实时生成的可变宽度轮廓”。

  这样：
  1. 骨架中心线移动；
  2. 根据每个骨架点的原始左右宽度，重建灰色字形轮廓；
  3. 灰色字形一定会随着中心线变化。
*/

state.widthProfile = [];

function V9_bounds() {
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

function V9_tangent(points, i) {
  const n = points.length;

  if (n <= 1) {
    return {x: 1, y: 0};
  }

  let a, b;

  if (i <= 0) {
    a = points[0];
    b = points[1];
  } else if (i >= n - 1) {
    a = points[n - 2];
    b = points[n - 1];
  } else {
    a = points[i - 1];
    b = points[i + 1];
  }

  let tx = b.x - a.x;
  let ty = b.y - a.y;
  const len = Math.hypot(tx, ty) || 1;

  return {
    x: tx / len,
    y: ty / len
  };
}

function V9_normal(points, i) {
  const t = V9_tangent(points, i);
  return {
    x: -t.y,
    y: t.x
  };
}

function V9_renderRawSvgToMask() {
  if (!state.rawSvgImage || !state.svgBounds) return null;

  const b = V9_bounds();
  const maxDim = 900;
  const aspect = b.width / Math.max(1, b.height);

  let w, h;
  if (aspect >= 1) {
    w = maxDim;
    h = Math.max(260, Math.round(maxDim / aspect));
  } else {
    h = maxDim;
    w = Math.max(260, Math.round(maxDim * aspect));
  }

  const off = document.createElement("canvas");
  off.width = w;
  off.height = h;

  const octx = off.getContext("2d", {willReadFrequently: true});
  octx.clearRect(0, 0, w, h);
  octx.drawImage(state.rawSvgImage, 0, 0, w, h);

  const img = octx.getImageData(0, 0, w, h);

  function alphaAtSvg(x, y) {
    const px = Math.round((x - b.minX) / Math.max(1, b.width) * (w - 1));
    const py = Math.round((y - b.minY) / Math.max(1, b.height) * (h - 1));

    if (px < 0 || py < 0 || px >= w || py >= h) {
      return 0;
    }

    return img.data[(py * w + px) * 4 + 3];
  }

  return {
    width: w,
    height: h,
    bounds: b,
    alphaAtSvg
  };
}

function V9_measureOneSide(mask, p, normal, sign, maxDistance, step) {
  let lastInside = 0;
  let everInside = false;

  for (let d = 0; d <= maxDistance; d += step) {
    const x = p.x + normal.x * sign * d;
    const y = p.y + normal.y * sign * d;

    const inside = mask.alphaAtSvg(x, y) > 8;

    if (inside) {
      lastInside = d;
      everInside = true;
    } else if (everInside && d > step * 2) {
      break;
    }
  }

  return lastInside;
}

function V9_computeWidthProfile() {
  if (!state.basePoints || !state.basePoints.length || !state.rawSvgImage || !state.svgBounds) {
    state.widthProfile = [];
    return;
  }

  const mask = V9_renderRawSvgToMask();
  if (!mask) {
    state.widthProfile = [];
    return;
  }

  const b = V9_bounds();
  const diag = Math.hypot(Math.max(1, b.width), Math.max(1, b.height));

  const maxDistance = diag * 0.18;
  const step = Math.max(1.2, diag / 420);

  const defaultWidth = Math.max(8, diag * 0.025);
  const minWidth = Math.max(4, diag * 0.010);
  const maxWidth = Math.max(18, diag * 0.14);

  const raw = [];

  for (let i = 0; i < state.basePoints.length; i++) {
    const p = state.basePoints[i];
    const n = V9_normal(state.basePoints, i);

    let left = V9_measureOneSide(mask, p, n, 1, maxDistance, step);
    let right = V9_measureOneSide(mask, p, n, -1, maxDistance, step);

    if (left < minWidth) left = defaultWidth;
    if (right < minWidth) right = defaultWidth;

    left = Math.max(minWidth, Math.min(maxWidth, left));
    right = Math.max(minWidth, Math.min(maxWidth, right));

    raw.push({
      left,
      right
    });
  }

  /*
    宽度平滑，避免局部忽粗忽细。
  */
  const smooth = raw.map((w, i) => {
    let sl = 0;
    let sr = 0;
    let c = 0;

    for (let k = -2; k <= 2; k++) {
      const j = i + k;
      if (j < 0 || j >= raw.length) continue;

      sl += raw[j].left;
      sr += raw[j].right;
      c++;
    }

    return {
      left: sl / c,
      right: sr / c
    };
  });

  state.widthProfile = smooth;
}

function V9_drawOriginalGhost() {
  if (!state.rawSvgImage || !state.svgBounds) return;

  const b = V9_bounds();

  ctx.save();
  ctx.globalAlpha = 0.08;
  ctx.drawImage(
    state.rawSvgImage,
    b.minX,
    b.minY,
    Math.max(1, b.width),
    Math.max(1, b.height)
  );
  ctx.restore();
}

function V9_drawVariableWidthGlyph() {
  if (!state.points || state.points.length < 2) return;
  if (!state.widthProfile || state.widthProfile.length !== state.points.length) {
    V9_computeWidthProfile();
  }

  if (!state.widthProfile || state.widthProfile.length !== state.points.length) {
    return;
  }

  const left = [];
  const right = [];

  for (let i = 0; i < state.points.length; i++) {
    const p = state.points[i];
    const n = V9_normal(state.points, i);
    const w = state.widthProfile[i];

    left.push({
      x: p.x + n.x * w.left,
      y: p.y + n.y * w.left
    });

    right.push({
      x: p.x - n.x * w.right,
      y: p.y - n.y * w.right
    });
  }

  ctx.save();
  ctx.fillStyle = "rgba(120, 120, 120, 0.42)";

  ctx.beginPath();

  ctx.moveTo(left[0].x, left[0].y);

  for (let i = 1; i < left.length; i++) {
    const prev = left[i - 1];
    const cur = left[i];

    const mx = (prev.x + cur.x) / 2;
    const my = (prev.y + cur.y) / 2;

    ctx.quadraticCurveTo(prev.x, prev.y, mx, my);
  }

  ctx.lineTo(left[left.length - 1].x, left[left.length - 1].y);

  for (let i = right.length - 1; i >= 1; i--) {
    const prev = right[i];
    const cur = right[i - 1];

    const mx = (prev.x + cur.x) / 2;
    const my = (prev.y + cur.y) / 2;

    ctx.quadraticCurveTo(prev.x, prev.y, mx, my);
  }

  ctx.lineTo(right[0].x, right[0].y);

  ctx.closePath();
  ctx.fill();

  ctx.restore();
}

function V9_drawSkeletonBand() {
  if (!state.points || !state.points.length) return;

  if (ui.showPreview.checked) {
    ctx.save();
    ctx.strokeStyle = "rgba(222,184,135,0.36)";
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
}

function V9_drawCenterline() {
  if (!state.points || !state.points.length) return;

  if (!ui.showSkeleton.checked) return;

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

function V9_drawHandles() {
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
  包装 loadGlyphData：
  每次换字 / 换 step 后，重新测量当前字形沿中心线的左右宽度。
*/
const __V9_oldLoadGlyphData = typeof loadGlyphData === "function" ? loadGlyphData : null;

if (__V9_oldLoadGlyphData) {
  loadGlyphData = async function() {
    await __V9_oldLoadGlyphData();
    V9_computeWidthProfile();
    draw();
  };
}

/*
  重新提取骨架后也重新计算宽度。
*/
const __V9_reextractBtn = document.getElementById("reextractBtn");
if (__V9_reextractBtn) {
  const __V9_oldReextract = __V9_reextractBtn.onclick;
  __V9_reextractBtn.onclick = function() {
    if (typeof __V9_oldReextract === "function") {
      __V9_oldReextract();
    }

    setTimeout(() => {
      V9_computeWidthProfile();
      draw();
    }, 50);
  };
}

/*
  最终 draw：
  灰色字形不再是静态 SVG 图片，而是由当前骨架中心线实时生成的轮廓。
  所以骨架中心线变化，灰色字形必然变化。
*/
draw = function() {
  const rect = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);

  ctx.save();
  ctx.translate(state.offsetX, state.offsetY);
  ctx.scale(state.scale, state.scale);

  if (ui.showRawSvg.checked) {
    V9_drawOriginalGhost();
    V9_drawVariableWidthGlyph();
  }

  V9_drawSkeletonBand();
  V9_drawCenterline();

  if (ui.showSupportPoints && ui.showSupportPoints.checked && typeof drawSupportPoints === "function") {
    drawSupportPoints();
  }

  V9_drawHandles();

  ctx.restore();
  updateStatus();
};

// ===== END_V9_VARIABLE_WIDTH_GLYPH_FOLLOW_SKELETON_PATCH =====
'''

    text = text[:init_pos] + patch + "\n" + text[init_pos:]
    APP.write_text(text, encoding="utf-8")
    print("已安装 V9：灰色字形由骨架中心线实时生成并跟随变化。")

print("准备重启服务。")
