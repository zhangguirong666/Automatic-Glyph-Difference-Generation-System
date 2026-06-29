from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_simplify_svg_points")
backup.write_text(text, encoding="utf-8")
print(f"已备份：{backup}")

MARK = "// ===== SVG_EDITOR_SIMPLIFY_POINTS_PATCH ====="

if MARK in text:
    print("已经安装过简化点数补丁，不重复添加。")
else:
    route_pos = text.find("# ===== SVG_PATH_REAL_GLYPH_EDITOR_V1 =====")
    if route_pos == -1:
        raise SystemExit("错误：没有找到 SVG path 编辑器路由，请先确认已经安装 /svg_path_editor/{job_id}")

    script_end = text.find("</script>", route_pos)
    if script_end == -1:
        raise SystemExit("错误：没有找到 SVG 编辑器页面中的 </script>")

    init_pos = text.rfind("init();", route_pos, script_end)
    if init_pos == -1:
        raise SystemExit("错误：没有找到 SVG 编辑器页面中的 init();")

    patch = r'''
// ===== SVG_EDITOR_SIMPLIFY_POINTS_PATCH =====

/*
  简化轮廓点数：
  适用于当前大量 L 线段点的 SVG 轮廓。
  使用 Douglas-Peucker 算法减少折线点。
  不改变可编辑逻辑，只减少 path 中的点数量。
*/

function addSimplifyPanel() {
  const rightPanel = document.querySelector(".panel:last-child");
  if (!rightPanel || document.getElementById("simplifyBtn")) return;

  const box = document.createElement("div");
  box.innerHTML = `
    <hr>
    <div class="group">
      <label>轮廓简化强度 <span id="simplifyValue">4</span></label>
      <input id="simplifyTolerance" type="range" min="0.5" max="30" step="0.5" value="4">
    </div>
    <button class="green" id="simplifyBtn">简化轮廓点数</button>
    <button class="warn" id="simplifyStrongBtn" style="margin-left:6px;">强力简化</button>
    <div class="note" style="margin-top:12px;">
      点太多时先点“简化轮廓点数”。强度越大，点越少，但字形细节会损失更多。
    </div>
  `;

  rightPanel.insertBefore(box, rightPanel.querySelector(".status"));

  const slider = document.getElementById("simplifyTolerance");
  const value = document.getElementById("simplifyValue");

  slider.addEventListener("input", () => {
    value.textContent = slider.value;
  });

  document.getElementById("simplifyBtn").onclick = () => {
    simplifyCurrentGlyph(Number(slider.value));
  };

  document.getElementById("simplifyStrongBtn").onclick = () => {
    simplifyCurrentGlyph(Number(slider.value) * 2.2);
  };
}

function pointFromSeg(seg) {
  if (!seg || !seg.vals) return null;

  if (seg.cmd === "M" || seg.cmd === "L" || seg.cmd === "T") {
    return {x: seg.vals[0], y: seg.vals[1]};
  }

  if (seg.cmd === "C") {
    return {x: seg.vals[4], y: seg.vals[5]};
  }

  if (seg.cmd === "S" || seg.cmd === "Q") {
    return {x: seg.vals[2], y: seg.vals[3]};
  }

  if (seg.cmd === "A") {
    return {x: seg.vals[5], y: seg.vals[6]};
  }

  return null;
}

function perpendicularDistanceSq(p, a, b) {
  const vx = b.x - a.x;
  const vy = b.y - a.y;

  const wx = p.x - a.x;
  const wy = p.y - a.y;

  const len2 = vx * vx + vy * vy;

  if (len2 < 1e-12) {
    const dx = p.x - a.x;
    const dy = p.y - a.y;
    return dx * dx + dy * dy;
  }

  let t = (wx * vx + wy * vy) / len2;
  t = Math.max(0, Math.min(1, t));

  const px = a.x + t * vx;
  const py = a.y + t * vy;

  const dx = p.x - px;
  const dy = p.y - py;

  return dx * dx + dy * dy;
}

function rdpSimplify(points, tolerance) {
  if (points.length <= 2) return points.slice();

  const tol2 = tolerance * tolerance;

  let maxDist = -1;
  let index = -1;

  const first = points[0];
  const last = points[points.length - 1];

  for (let i = 1; i < points.length - 1; i++) {
    const d = perpendicularDistanceSq(points[i], first, last);

    if (d > maxDist) {
      maxDist = d;
      index = i;
    }
  }

  if (maxDist > tol2 && index >= 0) {
    const left = rdpSimplify(points.slice(0, index + 1), tolerance);
    const right = rdpSimplify(points.slice(index), tolerance);

    return left.slice(0, -1).concat(right);
  }

  return [first, last];
}

function simplifyClosedPolyline(points, tolerance) {
  if (points.length <= 4) return points.slice();

  const closed = points.concat([{...points[0]}]);
  let simplified = rdpSimplify(closed, tolerance);

  if (simplified.length > 1) {
    const first = simplified[0];
    const last = simplified[simplified.length - 1];

    if (Math.hypot(first.x - last.x, first.y - last.y) < 1e-6) {
      simplified.pop();
    }
  }

  if (simplified.length < 3) {
    return points.slice();
  }

  return simplified;
}

function flushPolylineToSegments(out, polyline, closed, tolerance) {
  if (!polyline || !polyline.length) return;

  let pts;

  if (closed) {
    pts = simplifyClosedPolyline(polyline, tolerance);
  } else {
    pts = rdpSimplify(polyline, tolerance);
  }

  if (!pts.length) return;

  out.push({
    cmd: "M",
    vals: [pts[0].x, pts[0].y]
  });

  for (let i = 1; i < pts.length; i++) {
    out.push({
      cmd: "L",
      vals: [pts[i].x, pts[i].y]
    });
  }

  if (closed) {
    out.push({
      cmd: "Z",
      vals: []
    });
  }
}

function simplifyPathSegments(segs, tolerance) {
  const out = [];
  let polyline = [];
  let collecting = false;

  function flush(closed) {
    if (polyline.length) {
      flushPolylineToSegments(out, polyline, closed, tolerance);
    }
    polyline = [];
    collecting = false;
  }

  for (const seg of segs) {
    if (seg.cmd === "M") {
      flush(false);
      const p = pointFromSeg(seg);
      if (p) {
        polyline = [p];
        collecting = true;
      }
      continue;
    }

    if (seg.cmd === "L") {
      const p = pointFromSeg(seg);
      if (collecting && p) {
        polyline.push(p);
      } else if (p) {
        polyline = [p];
        collecting = true;
      }
      continue;
    }

    if (seg.cmd === "Z") {
      flush(true);
      continue;
    }

    /*
      如果遇到曲线命令，先把前面的折线简化，
      曲线本身暂时保留，避免破坏 Bézier 结构。
    */
    flush(false);
    out.push({
      cmd: seg.cmd,
      vals: seg.vals.slice()
    });
  }

  flush(false);

  return out;
}

function countEditablePoints() {
  let n = 0;

  for (const pathObj of state.paths) {
    for (const seg of pathObj.segs) {
      if (seg.cmd === "M" || seg.cmd === "L" || seg.cmd === "T") n += 1;
      else if (seg.cmd === "C") n += 3;
      else if (seg.cmd === "S" || seg.cmd === "Q") n += 2;
      else if (seg.cmd === "A") n += 1;
    }
  }

  return n;
}

function simplifyCurrentGlyph(tolerance) {
  if (!state.paths || !state.paths.length) {
    alert("当前没有可简化的 SVG path。");
    return;
  }

  const before = countEditablePoints();

  for (const pathObj of state.paths) {
    pathObj.segs = simplifyPathSegments(pathObj.segs, tolerance);
    pathObj.el.setAttribute("d", buildPath(pathObj.segs));
  }

  rebuildHandles();

  const after = countEditablePoints();

  updateStatus();

  alert(`轮廓点数已简化：${before} → ${after}`);
}

/*
  覆盖 updateStatus，增加点数信息。
*/
const __oldSvgEditorUpdateStatus = typeof updateStatus === "function" ? updateStatus : null;

updateStatus = function() {
  const pointCount = countEditablePoints();

  ui.statusBox.innerHTML =
    `Job ID：${JOB_ID}<br>` +
    `当前字形：${state.code || "-"}<br>` +
    `当前版本：${state.variant || "-"}<br>` +
    `路径数量：${state.paths.length}<br>` +
    `可编辑控制点：${state.handles.length}<br>` +
    `轮廓点估计：${pointCount}<br>` +
    `状态：直接编辑 SVG path`;
};

addSimplifyPanel();

// ===== END_SVG_EDITOR_SIMPLIFY_POINTS_PATCH =====
'''

    text = text[:init_pos] + patch + "\n" + text[init_pos:]
    APP.write_text(text, encoding="utf-8")
    print("已安装：SVG 编辑器轮廓点数简化功能。")

print("准备重启 FastAPI。")
