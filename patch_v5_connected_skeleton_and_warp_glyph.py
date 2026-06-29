from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_connected_skeleton_warp")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

MARK = "// ===== V5_CONNECTED_SKELETON_AND_WARP_GLYPH_PATCH ====="

if MARK in text:
    print("已经安装过该补丁，不重复添加。")
else:
    route_pos = text.find("# ===== ELASTIC_SKELETON_EDITOR_V5_PROXY_ROUTE =====")
    if route_pos == -1:
        raise SystemExit("错误：没有找到 V5 编辑器路由，请先确认 /skeleton_elastic_editor_v5 已经安装。")

    script_end = text.find("</script>", route_pos)
    if script_end == -1:
        raise SystemExit("错误：没有找到 V5 页面中的 </script>。")

    init_pos = text.rfind("init();", route_pos, script_end)
    if init_pos == -1:
        raise SystemExit("错误：没有找到 V5 页面中的 init();。")

    patch = r'''
// ===== V5_CONNECTED_SKELETON_AND_WARP_GLYPH_PATCH =====

function connectedComponents(points, edges) {
  const n = points.length;
  const adj = Array.from({length: n}, () => []);

  for (const [a, b] of edges) {
    if (a < 0 || b < 0 || a >= n || b >= n || a === b) continue;
    adj[a].push(b);
    adj[b].push(a);
  }

  const seen = Array(n).fill(false);
  const comps = [];

  for (let i = 0; i < n; i++) {
    if (seen[i]) continue;

    const q = [i];
    const comp = [];
    seen[i] = true;

    while (q.length) {
      const u = q.shift();
      comp.push(u);

      for (const v of adj[u]) {
        if (seen[v]) continue;
        seen[v] = true;
        q.push(v);
      }
    }

    comps.push(comp);
  }

  return comps;
}

function buildAdj(points, edges) {
  const adj = Array.from({length: points.length}, () => []);
  for (const [a, b] of edges) {
    if (a < 0 || b < 0 || a >= points.length || b >= points.length || a === b) continue;
    adj[a].push(b);
    adj[b].push(a);
  }
  return adj;
}

function uniqueEdges(edges) {
  const out = [];
  const set = new Set();

  for (const [a, b] of edges) {
    if (a === b) continue;
    const k = a < b ? `${a}-${b}` : `${b}-${a}`;
    if (set.has(k)) continue;
    set.add(k);
    out.push([a, b]);
  }

  return out;
}

function makeSkeletonGraphConnected(points, edges) {
  if (!points || points.length <= 1) {
    return {
      points: points || [],
      edges: edges || []
    };
  }

  let newEdges = uniqueEdges(edges || []);
  let comps = connectedComponents(points, newEdges);

  if (comps.length <= 1) {
    return {
      points,
      edges: newEdges
    };
  }

  const diag = (() => {
    const b = bounds(points);
    return Math.hypot(b.width, b.height);
  })();

  let guard = 0;

  while (comps.length > 1 && guard < 100) {
    guard++;

    const adj = buildAdj(points, newEdges);

    function candidates(comp) {
      const ends = comp.filter(i => adj[i].length <= 1);
      return ends.length ? ends : comp;
    }

    let best = null;

    for (let ci = 0; ci < comps.length; ci++) {
      const ca = candidates(comps[ci]);

      for (let cj = ci + 1; cj < comps.length; cj++) {
        const cb = candidates(comps[cj]);

        for (const a of ca) {
          for (const b of cb) {
            const d = dist(points[a], points[b]);
            if (!best || d < best.d) {
              best = {a, b, d, ci, cj};
            }
          }
        }
      }
    }

    if (!best) break;

    // 即使距离较大，也连接最近的两个断点，保证骨架是整体连贯的。
    // 但如果距离过大，后面用虚线表现桥接段，视觉上仍能看出这是连接关系。
    newEdges.push([best.a, best.b]);
    newEdges = uniqueEdges(newEdges);
    comps = connectedComponents(points, newEdges);
  }

  return {
    points,
    edges: newEdges
  };
}

function bridgeEdgesOnly(points, edges) {
  const real = new Set();
  const adj0 = buildAdj(points, edges);
  for (const [a, b] of edges) {
    const k = a < b ? `${a}-${b}` : `${b}-${a}`;
    real.add(k);
  }
  return real;
}

const __oldSkeletonPixelsToGraphForConnect = typeof skeletonPixelsToGraph === "function" ? skeletonPixelsToGraph : null;

if (__oldSkeletonPixelsToGraphForConnect) {
  skeletonPixelsToGraph = function(skel, w, h, svgBounds, desiredHandles=8) {
    const g = __oldSkeletonPixelsToGraphForConnect(skel, w, h, svgBounds, desiredHandles);
    const fixed = makeSkeletonGraphConnected(g.points || [], g.edges || []);
    return {
      points: fixed.points,
      edges: fixed.edges,
      handleIndices: selectKeyHandlesGraph(fixed.points, fixed.edges, desiredHandles)
    };
  };
}

const __oldFallbackCenterlineForConnect = typeof fallbackCenterlineFromMask === "function" ? fallbackCenterlineFromMask : null;

if (__oldFallbackCenterlineForConnect) {
  fallbackCenterlineFromMask = function(mask, w, h, svgBounds, desiredHandles=8) {
    const g = __oldFallbackCenterlineForConnect(mask, w, h, svgBounds, desiredHandles);
    const fixed = makeSkeletonGraphConnected(g.points || [], g.edges || []);
    return {
      points: fixed.points,
      edges: fixed.edges,
      handleIndices: selectKeyHandlesGraph(fixed.points, fixed.edges, desiredHandles)
    };
  };
}

const __oldLoadGlyphDataForConnect = typeof loadGlyphData === "function" ? loadGlyphData : null;

loadGlyphData = async function() {
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
    const fixed = makeSkeletonGraphConnected(clone(norm.points), norm.edges.map(e => [...e]));
    state.basePoints = clone(fixed.points);
    state.points = clone(fixed.points);
    state.edges = fixed.edges.map(e => [...e]);
    state.handleIndices = selectKeyHandlesGraph(state.points, state.edges, Number(ui.handleCount.value));
    state.skeletonSource = "skeleton-json-connected";
  } else if (state.rawSvgImage && state.svgBounds) {
    const proxy = generateProxySkeletonFromSvg(state.rawSvgImage, state.svgBounds, Number(ui.handleCount.value));
    const fixed = makeSkeletonGraphConnected(clone(proxy.points), proxy.edges.map(e => [...e]));
    state.basePoints = clone(fixed.points);
    state.points = clone(fixed.points);
    state.edges = fixed.edges.map(e => [...e]);
    state.handleIndices = selectKeyHandlesGraph(state.points, state.edges, Number(ui.handleCount.value));
    state.skeletonSource = "proxy-svg-connected";
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
};

function skeletonDeltaAt(q, radiusScale=0.32) {
  if (!state.basePoints || !state.points) return {dx: 0, dy: 0};
  if (state.basePoints.length !== state.points.length || state.points.length === 0) {
    return {dx: 0, dy: 0};
  }

  const b = state.svgBounds || bounds(state.basePoints);
  const diag = Math.hypot(b.width, b.height);
  const radius = Math.max(40, diag * radiusScale);

  let sw = 0;
  let sx = 0;
  let sy = 0;

  for (let i = 0; i < state.basePoints.length; i++) {
    const p0 = state.basePoints[i];
    const p1 = state.points[i];

    const d2 = (q.x - p0.x) * (q.x - p0.x) + (q.y - p0.y) * (q.y - p0.y);
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

function drawWarpedRawGlyph() {
  if (!state.rawSvgImage || !state.svgBounds) return;

  const b = state.svgBounds;
  const img = state.rawSvgImage;

  if (!state.points.length || state.basePoints.length !== state.points.length) {
    ctx.save();
    ctx.globalAlpha = 0.20;
    ctx.drawImage(img, b.minX, b.minY, Math.max(1, b.width), Math.max(1, b.height));
    ctx.restore();
    return;
  }

  const iw = img.naturalWidth || img.width;
  const ih = img.naturalHeight || img.height;

  if (!iw || !ih) return;

  // 网格越密，灰色字形越能跟随骨架，但也越耗性能。
  const cols = 38;
  const rows = Math.max(16, Math.round(cols * b.height / Math.max(1, b.width)));

  const sw = iw / cols;
  const sh = ih / rows;
  const dw = b.width / cols;
  const dh = b.height / rows;

  ctx.save();
  ctx.globalAlpha = 0.22;

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

      const delta = skeletonDeltaAt(center, 0.34);

      ctx.drawImage(
        img,
        sx,
        sy,
        sw + 1,
        sh + 1,
        wx + delta.dx,
        wy + delta.dy,
        dw + 0.7,
        dh + 0.7
      );
    }
  }

  ctx.restore();
}

function drawConnectedSkeletonLines(points, edges, color, width) {
  const comps = connectedComponents(points, edges);
  const adj = buildAdj(points, edges);

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

    const d = dist(p1, p2);
    const bb = bounds(points);
    const diag = Math.hypot(bb.width, bb.height);

    // 过长桥接线使用虚线，普通骨架线用实线
    if (d > diag * 0.18) {
      ctx.setLineDash([8 / state.scale, 8 / state.scale]);
    } else {
      ctx.setLineDash([]);
    }

    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
  }

  ctx.setLineDash([]);
  ctx.restore();
}

draw = function() {
  const rect = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);

  ctx.save();
  ctx.translate(state.offsetX, state.offsetY);
  ctx.scale(state.scale, state.scale);

  // 灰色完整字形：现在不是静态背景，而是随骨架变形的完整字形预览
  if (ui.showRawSvg.checked && state.rawSvgImage) {
    drawWarpedRawGlyph();
  }

  // 橙色粗线：辅助显示骨架影响范围
  if (ui.showPreview.checked && state.points.length) {
    drawThick(state.points, state.edges, Number(ui.strokeThickness.value), "rgba(222,184,135,0.45)");
  }

  // 红色骨架中心线：强制连贯连接
  if (ui.showSkeleton.checked && state.points.length) {
    drawConnectedSkeletonLines(state.points, state.edges, "#ff5f5f", 2.6 / state.scale);
  }

  if (ui.showSupportPoints.checked && state.points.length) {
    drawSupportPoints();
  }

  if (ui.showPoints.checked && state.handleIndices.length) {
    drawHandles();
  }

  ctx.restore();
  updateStatus();
};

// ===== END_V5_CONNECTED_SKELETON_AND_WARP_GLYPH_PATCH =====

'''

    text = text[:init_pos] + patch + "\n" + text[init_pos:]
    APP.write_text(text, encoding="utf-8")
    print("已安装：连贯骨架 + 灰色完整字形随骨架变形补丁。")

print("重启 FastAPI 前检查完成。")
