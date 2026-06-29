from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_v6_main_centerline")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

MARK = "// ===== V6_MAIN_CENTERLINE_NO_TANGLE_PATCH ====="

if MARK in text:
    print("已经安装过 V6 主干中心线补丁，不重复添加。")
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
// ===== V6_MAIN_CENTERLINE_NO_TANGLE_PATCH =====

/*
  V6 改法：
  不再显示完整 medial-axis 分支图。
  只提取“最长主干中心线”，避免环路、短枝、缠绕。
  这样虽然不是全部分支骨架，但更适合作为可控字形编辑骨架。
*/

function V6_dist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function V6_renderMaskFromSvgImage(img, svgBounds) {
  const maxDim = 640;
  const aspect = svgBounds.width / Math.max(1, svgBounds.height);

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
  octx.drawImage(img, 0, 0, w, h);

  const image = octx.getImageData(0, 0, w, h);
  const data = image.data;

  const mask = new Uint8Array(w * h);

  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const idx = (y * w + x) * 4;
      const alpha = data[idx + 3];

      if (alpha > 8) {
        mask[y * w + x] = 1;
      }
    }
  }

  return {mask, w, h};
}

function V6_dilate(mask, w, h) {
  const out = new Uint8Array(mask);

  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      const idx = y * w + x;
      if (mask[idx]) continue;

      let hit = 0;

      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          if (mask[(y + dy) * w + (x + dx)]) hit = 1;
        }
      }

      if (hit) out[idx] = 1;
    }
  }

  return out;
}

function V6_erode(mask, w, h) {
  const out = new Uint8Array(mask);

  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      const idx = y * w + x;
      if (!mask[idx]) continue;

      let keep = 1;

      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          if (!mask[(y + dy) * w + (x + dx)]) keep = 0;
        }
      }

      if (!keep) out[idx] = 0;
    }
  }

  return out;
}

function V6_closeMask(mask, w, h) {
  // 轻微闭运算，弥合很小的断裂
  let m = mask;
  m = V6_dilate(m, w, h);
  m = V6_erode(m, w, h);
  return m;
}

function V6_zhangSuen(mask, w, h, maxIter = 70) {
  const img = new Uint8Array(mask);

  function p(x, y) {
    if (x < 0 || x >= w || y < 0 || y >= h) return 0;
    return img[y * w + x];
  }

  function ns(x, y) {
    return [
      p(x, y - 1),
      p(x + 1, y - 1),
      p(x + 1, y),
      p(x + 1, y + 1),
      p(x, y + 1),
      p(x - 1, y + 1),
      p(x - 1, y),
      p(x - 1, y - 1)
    ];
  }

  function transitions(a) {
    let n = 0;
    for (let i = 0; i < 8; i++) {
      if (a[i] === 0 && a[(i + 1) % 8] === 1) n++;
    }
    return n;
  }

  let changed = true;
  let iter = 0;

  while (changed && iter < maxIter) {
    changed = false;
    iter++;

    for (let pass = 0; pass < 2; pass++) {
      const del = [];

      for (let y = 1; y < h - 1; y++) {
        for (let x = 1; x < w - 1; x++) {
          const idx = y * w + x;
          if (!img[idx]) continue;

          const a = ns(x, y);
          const B = a.reduce((s, v) => s + v, 0);
          const A = transitions(a);

          const p2 = a[0], p4 = a[2], p6 = a[4], p8 = a[6];

          if (B < 2 || B > 6) continue;
          if (A !== 1) continue;

          if (pass === 0) {
            if (p2 * p4 * p6 !== 0) continue;
            if (p4 * p6 * p8 !== 0) continue;
          } else {
            if (p2 * p4 * p8 !== 0) continue;
            if (p2 * p6 * p8 !== 0) continue;
          }

          del.push(idx);
        }
      }

      if (del.length) {
        changed = true;
        for (const idx of del) img[idx] = 0;
      }
    }
  }

  return img;
}

function V6_buildPixelGraph(skel, w, h) {
  const pixels = [];
  const idMap = new Map();

  function key(x, y) {
    return y + "," + x;
  }

  function has(x, y) {
    if (x < 0 || x >= w || y < 0 || y >= h) return false;
    return skel[y * w + x] === 1;
  }

  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      if (has(x, y)) {
        idMap.set(key(x, y), pixels.length);
        pixels.push({x, y});
      }
    }
  }

  const adj = Array.from({length: pixels.length}, () => []);

  for (let i = 0; i < pixels.length; i++) {
    const p = pixels[i];

    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        if (dx === 0 && dy === 0) continue;

        const j = idMap.get(key(p.x + dx, p.y + dy));
        if (j !== undefined) adj[i].push(j);
      }
    }
  }

  return {pixels, adj};
}

function V6_components(adj) {
  const seen = Array(adj.length).fill(false);
  const comps = [];

  for (let i = 0; i < adj.length; i++) {
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

function V6_bfsFarthest(start, adj, allowedSet = null) {
  const n = adj.length;
  const dist = Array(n).fill(-1);
  const parent = Array(n).fill(-1);
  const q = [start];

  dist[start] = 0;

  let head = 0;
  let best = start;

  while (head < q.length) {
    const u = q[head++];

    if (dist[u] > dist[best]) best = u;

    for (const v of adj[u]) {
      if (allowedSet && !allowedSet.has(v)) continue;
      if (dist[v] >= 0) continue;

      dist[v] = dist[u] + 1;
      parent[v] = u;
      q.push(v);
    }
  }

  return {node: best, dist: dist[best], parent};
}

function V6_pathBetween(a, b, parent) {
  const path = [];
  let cur = b;
  let guard = 0;

  while (cur !== -1 && cur !== undefined && guard < 100000) {
    path.push(cur);
    if (cur === a) break;
    cur = parent[cur];
    guard++;
  }

  path.reverse();
  return path;
}

function V6_pathPixelLength(path, pixels) {
  let s = 0;

  for (let i = 1; i < path.length; i++) {
    const a = pixels[path[i - 1]];
    const b = pixels[path[i]];
    s += Math.hypot(b.x - a.x, b.y - a.y);
  }

  return s;
}

function V6_extractDiameterPathForComponent(comp, pixels, adj) {
  if (!comp.length) return [];

  const allowed = new Set(comp);

  const endpoints = comp.filter(i => {
    let d = 0;
    for (const v of adj[i]) {
      if (allowed.has(v)) d++;
    }
    return d <= 1;
  });

  const start = endpoints[0] ?? comp[0];

  const a = V6_bfsFarthest(start, adj, allowed).node;
  const fb = V6_bfsFarthest(a, adj, allowed);
  const b = fb.node;

  return V6_pathBetween(a, b, fb.parent);
}

function V6_smoothSvgPath(points, rounds = 2) {
  let arr = points.map(p => ({...p}));

  for (let r = 0; r < rounds; r++) {
    const next = arr.map((p, i) => {
      if (i === 0 || i === arr.length - 1) return {...p};

      const a = arr[i - 1];
      const b = arr[i];
      const c = arr[i + 1];

      return {
        x: a.x * 0.25 + b.x * 0.50 + c.x * 0.25,
        y: a.y * 0.25 + b.y * 0.50 + c.y * 0.25
      };
    });

    arr = next;
  }

  return arr;
}

function V6_sampleByArcLength(points, count) {
  if (points.length <= count) return points.map(p => ({...p}));

  const seg = [0];
  let total = 0;

  for (let i = 1; i < points.length; i++) {
    total += V6_dist(points[i - 1], points[i]);
    seg.push(total);
  }

  const out = [];

  for (let k = 0; k < count; k++) {
    const target = (k / (count - 1)) * total;

    let i = 1;
    while (i < seg.length && seg[i] < target) i++;

    if (i >= seg.length) {
      out.push({...points[points.length - 1]});
      continue;
    }

    const a = points[i - 1];
    const b = points[i];
    const t = (target - seg[i - 1]) / Math.max(1e-6, seg[i] - seg[i - 1]);

    out.push({
      x: a.x * (1 - t) + b.x * t,
      y: a.y * (1 - t) + b.y * t
    });
  }

  return out;
}

function V6_selectHandlesForPolyline(points, desired = 8) {
  if (points.length <= desired) return points.map((_, i) => i);

  const selected = new Set([0, points.length - 1]);

  const curves = [];

  for (let i = 1; i < points.length - 1; i++) {
    const a = points[i - 1];
    const b = points[i];
    const c = points[i + 1];

    const v1x = b.x - a.x;
    const v1y = b.y - a.y;
    const v2x = c.x - b.x;
    const v2y = c.y - b.y;

    const n1 = Math.hypot(v1x, v1y);
    const n2 = Math.hypot(v2x, v2y);

    if (n1 < 1e-6 || n2 < 1e-6) continue;

    const dot = (v1x * v2x + v1y * v2y) / (n1 * n2);
    const ang = Math.acos(Math.max(-1, Math.min(1, dot)));

    curves.push({i, score: ang});
  }

  curves.sort((a, b) => b.score - a.score);

  for (const c of curves) {
    if (selected.size >= Math.ceil(desired * 0.65)) break;
    selected.add(c.i);
  }

  while (selected.size < desired) {
    let best = -1;
    let bestD = -1;

    for (let i = 0; i < points.length; i++) {
      if (selected.has(i)) continue;

      let dmin = Infinity;
      for (const s of selected) {
        dmin = Math.min(dmin, V6_dist(points[i], points[s]));
      }

      if (dmin > bestD) {
        bestD = dmin;
        best = i;
      }
    }

    if (best < 0) break;
    selected.add(best);
  }

  return Array.from(selected).sort((a, b) => a - b);
}

function V6_fallbackSimpleCenterline(svgBounds, desiredHandles = 8) {
  const pts = [
    {x: svgBounds.minX + svgBounds.width * 0.08, y: svgBounds.minY + svgBounds.height * 0.35},
    {x: svgBounds.minX + svgBounds.width * 0.18, y: svgBounds.minY + svgBounds.height * 0.28},
    {x: svgBounds.minX + svgBounds.width * 0.34, y: svgBounds.minY + svgBounds.height * 0.45},
    {x: svgBounds.minX + svgBounds.width * 0.48, y: svgBounds.minY + svgBounds.height * 0.55},
    {x: svgBounds.minX + svgBounds.width * 0.62, y: svgBounds.minY + svgBounds.height * 0.68},
    {x: svgBounds.minX + svgBounds.width * 0.78, y: svgBounds.minY + svgBounds.height * 0.36},
    {x: svgBounds.minX + svgBounds.width * 0.90, y: svgBounds.minY + svgBounds.height * 0.22}
  ];

  const edges = [];
  for (let i = 0; i < pts.length - 1; i++) edges.push([i, i + 1]);

  return {
    points: pts,
    edges,
    handleIndices: V6_selectHandlesForPolyline(pts, desiredHandles)
  };
}

generateProxySkeletonFromSvg = function(img, svgBounds, desiredHandles = 8) {
  try {
    let {mask, w, h} = V6_renderMaskFromSvgImage(img, svgBounds);

    mask = V6_closeMask(mask, w, h);

    const skel = V6_zhangSuen(mask, w, h, 70);
    const {pixels, adj} = V6_buildPixelGraph(skel, w, h);

    if (pixels.length < 20) {
      return V6_fallbackSimpleCenterline(svgBounds, desiredHandles);
    }

    const comps = V6_components(adj)
      .filter(c => c.length >= 20)
      .sort((a, b) => b.length - a.length);

    if (!comps.length) {
      return V6_fallbackSimpleCenterline(svgBounds, desiredHandles);
    }

    const paths = [];

    for (const comp of comps.slice(0, 5)) {
      const path = V6_extractDiameterPathForComponent(comp, pixels, adj);
      const len = V6_pathPixelLength(path, pixels);

      if (path.length >= 8 && len >= 20) {
        paths.push({path, len});
      }
    }

    if (!paths.length) {
      return V6_fallbackSimpleCenterline(svgBounds, desiredHandles);
    }

    paths.sort((a, b) => b.len - a.len);

    // 关键：只保留最长主干路径。
    // 不再把所有分支都连进来，避免骨架缠绕。
    const main = paths[0].path;

    let svgPts = main.map(id => {
      const p = pixels[id];

      return {
        x: svgBounds.minX + (p.x / Math.max(1, w - 1)) * svgBounds.width,
        y: svgBounds.minY + (p.y / Math.max(1, h - 1)) * svgBounds.height
      };
    });

    svgPts = V6_smoothSvgPath(svgPts, 2);

    const supportCount = Math.max(18, Math.min(32, desiredHandles * 3));
    const points = V6_sampleByArcLength(svgPts, supportCount);

    const edges = [];
    for (let i = 0; i < points.length - 1; i++) edges.push([i, i + 1]);

    return {
      points,
      edges,
      handleIndices: V6_selectHandlesForPolyline(points, desiredHandles)
    };
  } catch (e) {
    console.warn("V6 generateProxySkeletonFromSvg failed:", e);
    return V6_fallbackSimpleCenterline(svgBounds, desiredHandles);
  }
};

// 强制重新加载时使用 V6 主干中心线，不再优先使用旧 skeleton_json，避免旧骨架污染。
loadGlyphData = async function() {
  state.loaded = false;
  state.pinned.clear();
  state.points = [];
  state.basePoints = [];
  state.edges = [];
  state.handleIndices = [];

  try {
    state.rawSvgText = await fetchText(`/raw_svg_variant/${JOB_ID}/${state.code}/${state.variant}`);
    state.rawSvgImage = await svgToImage(state.rawSvgText);
    state.svgBounds = parseSvgBounds(state.rawSvgText);
  } catch (e) {
    state.rawSvgText = "";
    state.rawSvgImage = null;
    state.svgBounds = null;
  }

  if (state.rawSvgImage && state.svgBounds) {
    const proxy = generateProxySkeletonFromSvg(
      state.rawSvgImage,
      state.svgBounds,
      Number(ui.handleCount.value)
    );

    state.basePoints = clone(proxy.points);
    state.points = clone(proxy.points);
    state.edges = proxy.edges.map(e => [...e]);
    state.handleIndices = [...proxy.handleIndices];
    state.skeletonSource = "v6-main-centerline-no-tangle";
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

document.getElementById("reextractBtn").onclick = () => {
  if (state.rawSvgImage && state.svgBounds) {
    const proxy = generateProxySkeletonFromSvg(
      state.rawSvgImage,
      state.svgBounds,
      Number(ui.handleCount.value)
    );

    state.basePoints = clone(proxy.points);
    state.points = clone(proxy.points);
    state.edges = proxy.edges.map(e => [...e]);
    state.handleIndices = [...proxy.handleIndices];
    state.pinned.clear();
    state.skeletonSource = "v6-main-centerline-no-tangle";
    fit();
    draw();
  }
};

// ===== END_V6_MAIN_CENTERLINE_NO_TANGLE_PATCH =====

'''

    text = text[:init_pos] + patch + "\n" + text[init_pos:]
    APP.write_text(text, encoding="utf-8")
    print("已安装 V6 主干中心线补丁：最长主干路径 + 去缠绕。")

print("准备重启服务。")
