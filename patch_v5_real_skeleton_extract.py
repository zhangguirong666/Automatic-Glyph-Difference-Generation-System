from pathlib import Path
import re

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_real_skeleton_extract")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

start = text.find("function generateProxySkeletonFromSvg(")
end = text.find("function smoothPolyline(", start)

if start == -1 or end == -1:
    raise SystemExit("错误：没有找到 generateProxySkeletonFromSvg 或 smoothPolyline，说明当前 app.py 结构和预期不一致。")

new_block = r'''
function generateProxySkeletonFromSvg(img, svgBounds, desiredHandles=8) {
  /*
    标准化骨架提取流程：
    1. SVG 渲染到离屏 canvas；
    2. 读取 alpha，得到二值轮廓；
    3. Zhang-Suen thinning 得到单像素骨架；
    4. 将骨架像素转为图结构；
    5. 在端点、分叉点、曲率点处生成关键控制点。
  */

  const maxDim = 760;
  const aspect = svgBounds.width / Math.max(1, svgBounds.height);

  let w, h;
  if (aspect >= 1) {
    w = maxDim;
    h = Math.max(240, Math.round(maxDim / aspect));
  } else {
    h = maxDim;
    w = Math.max(240, Math.round(maxDim * aspect));
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
      const a = data[idx + 3];

      // alpha 大于阈值认为是字形内部
      if (a > 8) {
        mask[y * w + x] = 1;
      }
    }
  }

  cleanMask(mask, w, h);
  const skel = zhangSuenThinning(mask, w, h, 90);
  const graph = skeletonPixelsToGraph(skel, w, h, svgBounds, desiredHandles);

  if (graph.points.length >= 3 && graph.edges.length >= 2) {
    return graph;
  }

  // 如果细化失败，退回到较保守的水平中轴提取
  return fallbackCenterlineFromMask(mask, w, h, svgBounds, desiredHandles);
}

function cleanMask(mask, w, h) {
  // 简单 3x3 多数滤波，去掉孤立噪声
  const copy = new Uint8Array(mask);

  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      let c = 0;

      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          c += copy[(y + dy) * w + (x + dx)];
        }
      }

      const idx = y * w + x;

      if (copy[idx] && c <= 2) mask[idx] = 0;
      if (!copy[idx] && c >= 8) mask[idx] = 1;
    }
  }
}

function zhangSuenThinning(mask, w, h, maxIter=80) {
  const img = new Uint8Array(mask);

  function p(x, y) {
    if (x < 0 || x >= w || y < 0 || y >= h) return 0;
    return img[y * w + x];
  }

  function neighbors(x, y) {
    return [
      p(x, y - 1),     // p2
      p(x + 1, y - 1), // p3
      p(x + 1, y),     // p4
      p(x + 1, y + 1), // p5
      p(x, y + 1),     // p6
      p(x - 1, y + 1), // p7
      p(x - 1, y),     // p8
      p(x - 1, y - 1)  // p9
    ];
  }

  function transitions(ns) {
    let a = 0;
    for (let i = 0; i < 8; i++) {
      if (ns[i] === 0 && ns[(i + 1) % 8] === 1) a++;
    }
    return a;
  }

  let changed = true;
  let iter = 0;

  while (changed && iter < maxIter) {
    changed = false;
    iter++;

    for (let step = 0; step < 2; step++) {
      const del = [];

      for (let y = 1; y < h - 1; y++) {
        for (let x = 1; x < w - 1; x++) {
          const idx = y * w + x;
          if (!img[idx]) continue;

          const ns = neighbors(x, y);
          const B = ns.reduce((a, b) => a + b, 0);
          const A = transitions(ns);

          const p2 = ns[0], p4 = ns[2], p6 = ns[4], p8 = ns[6];

          if (B < 2 || B > 6) continue;
          if (A !== 1) continue;

          if (step === 0) {
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

function skeletonPixelsToGraph(skel, w, h, svgBounds, desiredHandles=8) {
  const pix = [];
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
        const k = key(x, y);
        idMap.set(k, pix.length);
        pix.push({x, y});
      }
    }
  }

  if (pix.length < 10) {
    return {points: [], edges: [], handleIndices: []};
  }

  const adj = Array.from({length: pix.length}, () => []);

  for (let i = 0; i < pix.length; i++) {
    const p = pix[i];

    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        if (dx === 0 && dy === 0) continue;

        const j = idMap.get(key(p.x + dx, p.y + dy));
        if (j !== undefined) adj[i].push(j);
      }
    }
  }

  const degree = adj.map(a => a.length);
  const anchors = new Set();

  for (let i = 0; i < pix.length; i++) {
    if (degree[i] !== 2) anchors.add(i);
  }

  // 没有端点和分叉点，说明可能是闭环，任选一点作为锚点
  if (!anchors.size) {
    anchors.add(0);
  }

  const points = [];
  const edges = [];
  const pixelToPoint = new Map();

  function toSvgPoint(pp) {
    return {
      x: svgBounds.minX + (pp.x / Math.max(1, w - 1)) * svgBounds.width,
      y: svgBounds.minY + (pp.y / Math.max(1, h - 1)) * svgBounds.height
    };
  }

  function addGraphPoint(pixelIndex, forceReuse=false) {
    if (forceReuse && pixelToPoint.has(pixelIndex)) {
      return pixelToPoint.get(pixelIndex);
    }

    if (forceReuse) {
      const q = toSvgPoint(pix[pixelIndex]);
      points.push(q);
      const id = points.length - 1;
      pixelToPoint.set(pixelIndex, id);
      return id;
    }

    const q = toSvgPoint(pix[pixelIndex]);
    points.push(q);
    return points.length - 1;
  }

  const visitedEdge = new Set();

  function edgeKey(a, b) {
    return a < b ? a + "-" + b : b + "-" + a;
  }

  const minSegmentPixelLength = 8;
  const sampleEvery = Math.max(8, Math.round(Math.min(w, h) / 38));

  function traceFrom(anchor, next) {
    const chain = [anchor, next];

    let prev = anchor;
    let cur = next;
    visitedEdge.add(edgeKey(anchor, next));

    let guard = 0;

    while (!anchors.has(cur) && degree[cur] === 2 && guard < 10000) {
      guard++;

      const ns = adj[cur];
      let nxt = ns[0] === prev ? ns[1] : ns[0];

      if (nxt === undefined) break;

      visitedEdge.add(edgeKey(cur, nxt));
      chain.push(nxt);

      prev = cur;
      cur = nxt;
    }

    return chain;
  }

  for (const a of anchors) {
    for (const n of adj[a]) {
      const ek = edgeKey(a, n);
      if (visitedEdge.has(ek)) continue;

      const chain = traceFrom(a, n);

      if (chain.length < minSegmentPixelLength) continue;

      const simplified = simplifyPixelChain(chain, pix, sampleEvery);
      if (simplified.length < 2) continue;

      let lastPoint = null;

      for (let k = 0; k < simplified.length; k++) {
        const pxId = simplified[k];

        const isEndpoint = k === 0 || k === simplified.length - 1;
        const graphPointId = addGraphPoint(pxId, anchors.has(pxId) || isEndpoint);

        if (lastPoint !== null && lastPoint !== graphPointId) {
          edges.push([lastPoint, graphPointId]);
        }

        lastPoint = graphPointId;
      }
    }
  }

  // 如果上面的分叉追踪没有提取到有效图，退回最长路径
  if (points.length < 3 || edges.length < 2) {
    return longestPathSkeletonGraph(pix, adj, degree, svgBounds, w, h, desiredHandles);
  }

  const compact = compactGraph(points, edges);
  const handleIndices = selectKeyHandlesGraph(compact.points, compact.edges, desiredHandles);

  return {
    points: compact.points,
    edges: compact.edges,
    handleIndices
  };
}

function simplifyPixelChain(chain, pix, sampleEvery) {
  if (chain.length <= 2) return chain;

  const out = [chain[0]];
  let acc = 0;

  for (let i = 1; i < chain.length; i++) {
    const a = pix[chain[i - 1]];
    const b = pix[chain[i]];
    acc += Math.hypot(b.x - a.x, b.y - a.y);

    if (acc >= sampleEvery) {
      out.push(chain[i]);
      acc = 0;
    }
  }

  if (out[out.length - 1] !== chain[chain.length - 1]) {
    out.push(chain[chain.length - 1]);
  }

  return out;
}

function longestPathSkeletonGraph(pix, adj, degree, svgBounds, w, h, desiredHandles) {
  const endpoints = [];
  for (let i = 0; i < degree.length; i++) {
    if (degree[i] === 1) endpoints.push(i);
  }

  const start = endpoints[0] ?? 0;
  const a = farthestBfs(start, adj).node;
  const fb = farthestBfs(a, adj);
  const b = fb.node;
  const parent = fb.parent;

  const path = [];
  let cur = b;
  while (cur !== -1 && cur !== undefined) {
    path.push(cur);
    if (cur === a) break;
    cur = parent[cur];
  }
  path.reverse();

  const sampleEvery = Math.max(8, Math.round(Math.min(w, h) / 34));
  const simplified = simplifyPixelChain(path, pix, sampleEvery);

  const points = simplified.map(id => ({
    x: svgBounds.minX + (pix[id].x / Math.max(1, w - 1)) * svgBounds.width,
    y: svgBounds.minY + (pix[id].y / Math.max(1, h - 1)) * svgBounds.height
  }));

  const edges = [];
  for (let i = 0; i < points.length - 1; i++) edges.push([i, i + 1]);

  return {
    points,
    edges,
    handleIndices: selectKeyHandlesGraph(points, edges, desiredHandles)
  };
}

function farthestBfs(start, adj) {
  const n = adj.length;
  const distArr = Array(n).fill(-1);
  const parent = Array(n).fill(-1);
  const q = [start];
  distArr[start] = 0;

  let head = 0;
  let best = start;

  while (head < q.length) {
    const u = q[head++];

    if (distArr[u] > distArr[best]) best = u;

    for (const v of adj[u]) {
      if (distArr[v] >= 0) continue;
      distArr[v] = distArr[u] + 1;
      parent[v] = u;
      q.push(v);
    }
  }

  return {node: best, distance: distArr[best], parent};
}

function compactGraph(points, edges) {
  const outPoints = [];
  const map = new Map();

  function qkey(p) {
    return Math.round(p.x * 10) + "," + Math.round(p.y * 10);
  }

  for (let i = 0; i < points.length; i++) {
    const k = qkey(points[i]);
    if (!map.has(k)) {
      map.set(k, outPoints.length);
      outPoints.push(points[i]);
    }
  }

  const outEdges = [];
  const edgeSet = new Set();

  for (const [a, b] of edges) {
    const ka = qkey(points[a]);
    const kb = qkey(points[b]);
    const na = map.get(ka);
    const nb = map.get(kb);

    if (na === undefined || nb === undefined || na === nb) continue;

    const ek = na < nb ? na + "-" + nb : nb + "-" + na;
    if (edgeSet.has(ek)) continue;

    edgeSet.add(ek);
    outEdges.push([na, nb]);
  }

  return {points: outPoints, edges: outEdges};
}

function selectKeyHandlesGraph(points, edges, desired=8) {
  if (!points.length) return [];
  if (points.length <= desired) return points.map((_, i) => i);

  const adj = Array.from({length: points.length}, () => []);
  for (const [a, b] of edges) {
    adj[a].push(b);
    adj[b].push(a);
  }

  const selected = new Set();

  // 端点和分叉点必须成为关键控制点
  for (let i = 0; i < points.length; i++) {
    if (adj[i].length === 1 || adj[i].length >= 3) {
      selected.add(i);
    }
  }

  // 如果太多，优先保留分散的点
  if (selected.size > desired) {
    const arr = Array.from(selected);
    return farthestPointSubset(points, arr, desired);
  }

  // 加入曲率较大的点
  const curves = [];

  for (let i = 0; i < points.length; i++) {
    if (selected.has(i)) continue;
    if (adj[i].length !== 2) continue;

    const a = points[adj[i][0]];
    const b = points[i];
    const c = points[adj[i][1]];

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
    if (selected.size >= desired) break;
    selected.add(c.i);
  }

  // 不够则用最远点采样补齐
  while (selected.size < desired) {
    let best = -1;
    let bestD = -1;

    for (let i = 0; i < points.length; i++) {
      if (selected.has(i)) continue;

      let dmin = Infinity;
      for (const s of selected) {
        dmin = Math.min(dmin, dist(points[i], points[s]));
      }

      if (!selected.size) dmin = 1e9;

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

function farthestPointSubset(points, candidates, desired) {
  if (candidates.length <= desired) return candidates;

  const selected = [candidates[0]];

  while (selected.length < desired) {
    let best = -1;
    let bestD = -1;

    for (const c of candidates) {
      if (selected.includes(c)) continue;

      let dmin = Infinity;
      for (const s of selected) {
        dmin = Math.min(dmin, dist(points[c], points[s]));
      }

      if (dmin > bestD) {
        bestD = dmin;
        best = c;
      }
    }

    if (best < 0) break;
    selected.push(best);
  }

  return selected.sort((a, b) => a - b);
}

function fallbackCenterlineFromMask(mask, w, h, svgBounds, desiredHandles=8) {
  const mids = [];

  for (let x = 0; x < w; x++) {
    let minY = -1;
    let maxY = -1;

    for (let y = 0; y < h; y++) {
      if (mask[y * w + x]) {
        minY = y;
        break;
      }
    }

    if (minY < 0) continue;

    for (let y = h - 1; y >= 0; y--) {
      if (mask[y * w + x]) {
        maxY = y;
        break;
      }
    }

    if (maxY >= minY) {
      mids.push({
        x,
        y: (minY + maxY) / 2
      });
    }
  }

  const supportCount = 24;
  const points = [];

  for (let i = 0; i < supportCount; i++) {
    const t = i / (supportCount - 1);
    const idx = Math.max(0, Math.min(mids.length - 1, Math.round(t * (mids.length - 1))));
    const p = mids[idx];

    if (!p) continue;

    points.push({
      x: svgBounds.minX + (p.x / Math.max(1, w - 1)) * svgBounds.width,
      y: svgBounds.minY + (p.y / Math.max(1, h - 1)) * svgBounds.height
    });
  }

  const edges = [];
  for (let i = 0; i < points.length - 1; i++) edges.push([i, i + 1]);

  return {
    points,
    edges,
    handleIndices: selectKeyHandlesGraph(points, edges, desiredHandles)
  };
}

'''

text = text[:start] + new_block + text[end:]

# 替换已有调用：如果还有 selectKeyHandles(...)，尽量转成 graph 版本
text = text.replace(
    "state.handleIndices = selectKeyHandles(state.points, Number(ui.handleCount.value));",
    "state.handleIndices = selectKeyHandlesGraph(state.points, state.edges, Number(ui.handleCount.value));"
)

# 追加缓存版本标记，方便确认
mark = "# ===== PATCH_REAL_SKELETON_EXTRACT_INSTALLED ====="
if mark not in text:
    text += "\n\n" + mark + "\n"

APP.write_text(text, encoding="utf-8")
print("已替换为 Zhang-Suen 标准骨架细化提取算法。")
