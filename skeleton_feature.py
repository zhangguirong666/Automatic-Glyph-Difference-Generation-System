# -*- coding: utf-8 -*-
import io
import os
import re
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image
import cairosvg
from scipy.ndimage import distance_transform_edt
from skimage.morphology import skeletonize


# -----------------------------
# 基础工具
# -----------------------------
def _strip_unit(v, default=1000.0):
    if v is None:
        return default
    s = str(v).strip()
    m = re.match(r"([0-9.+-eE]+)", s)
    if not m:
        return default
    try:
        return float(m.group(1))
    except Exception:
        return default


def _safe_variant_name(name: str) -> str:
    name = str(name).strip()
    name = re.sub(r"[^0-9A-Za-z_\-]+", "_", name)
    return name or "variant"


def _parse_svg_meta(svg_text: str):
    m = re.search(r'viewBox="([^"]+)"', svg_text)
    if m:
        parts = [float(x) for x in re.split(r"[\s,]+", m.group(1).strip()) if x]
        if len(parts) == 4:
            return {
                "viewBox": parts,
                "width": parts[2],
                "height": parts[3]
            }

    mw = re.search(r'width="([^"]+)"', svg_text)
    mh = re.search(r'height="([^"]+)"', svg_text)
    w = _strip_unit(mw.group(1) if mw else None, 1000.0)
    h = _strip_unit(mh.group(1) if mh else None, 1000.0)

    return {
        "viewBox": [0.0, 0.0, float(w), float(h)],
        "width": float(w),
        "height": float(h)
    }


def _rasterize_svg_to_mask(svg_text: str, side: int = 1024):
    png_bytes = cairosvg.svg2png(
        bytestring=svg_text.encode("utf-8"),
        output_width=side,
        output_height=side,
        background_color="white"
    )
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    arr = np.array(img)

    alpha = arr[:, :, 3]
    gray = np.array(img.convert("L"))

    # 优先用 alpha；如果 alpha 全满，再用灰度阈值
    mask = alpha > 0
    if mask.sum() > mask.size * 0.98:
        mask = gray < 245

    # 去除边界纯白
    mask = mask.astype(bool)
    return mask, arr


def _build_skeleton_segments(skel: np.ndarray):
    """
    把骨架像素变成若干 polyline 段。
    """
    ys, xs = np.where(skel > 0)
    nodes = {(int(y), int(x)) for y, x in zip(ys, xs)}
    if not nodes:
        return []

    nbr_map = {}
    offsets = [
        (-1, -1), (-1, 0), (-1, 1),
        ( 0, -1),          ( 0, 1),
        ( 1, -1), ( 1, 0), ( 1, 1),
    ]

    for p in nodes:
        y, x = p
        nbrs = []
        for dy, dx in offsets:
            q = (y + dy, x + dx)
            if q in nodes:
                nbrs.append(q)
        nbr_map[p] = nbrs

    deg = {p: len(nbr_map[p]) for p in nodes}
    visited_edges = set()
    segments = []

    def ekey(a, b):
        return tuple(sorted((a, b)))

    # 端点/分叉点出发
    specials = [p for p in nodes if deg[p] != 2]

    for p in specials:
        for nb in nbr_map[p]:
            ek = ekey(p, nb)
            if ek in visited_edges:
                continue

            seg = [p]
            prev = p
            curr = nb
            visited_edges.add(ek)
            seg.append(curr)

            while deg[curr] == 2:
                nxts = [q for q in nbr_map[curr] if q != prev]
                if not nxts:
                    break
                nxt = nxts[0]
                ek2 = ekey(curr, nxt)
                if ek2 in visited_edges:
                    break
                prev, curr = curr, nxt
                visited_edges.add(ek2)
                seg.append(curr)

            if len(seg) >= 2:
                segments.append(seg)

    # 剩余闭环
    for p in list(nodes):
        for nb in nbr_map[p]:
            ek = ekey(p, nb)
            if ek in visited_edges:
                continue

            seg = [p]
            prev = p
            curr = nb
            visited_edges.add(ek)
            seg.append(curr)

            guard = 0
            while guard < 100000:
                guard += 1
                nxts = [q for q in nbr_map[curr] if q != prev]
                if not nxts:
                    break
                nxt = nxts[0]
                ek2 = ekey(curr, nxt)
                if ek2 in visited_edges:
                    break
                prev, curr = curr, nxt
                visited_edges.add(ek2)
                seg.append(curr)
                if curr == seg[0]:
                    break

            if len(seg) >= 2:
                segments.append(seg)

    # 去掉过短段
    filtered = []
    for seg in segments:
        if len(seg) >= 4:
            filtered.append(seg)

    return filtered


def _simplify_segment_pixels(seg, max_points=28):
    pts = [(int(p[1]), int(p[0])) for p in seg]  # (x, y)
    if len(pts) <= max_points:
        out = pts
    else:
        idx = np.linspace(0, len(pts) - 1, max_points).astype(int)
        out = [pts[i] for i in idx]

    # 去重
    dedup = []
    prev = None
    for p in out:
        if p != prev:
            dedup.append(p)
        prev = p
    return dedup


def _pixel_to_svg(xp, yp, vb, side):
    vx, vy, vw, vh = vb
    x = vx + (xp / max(side - 1, 1)) * vw
    y = vy + (yp / max(side - 1, 1)) * vh
    return x, y


def _length_of_pts(pts):
    s = 0.0
    for i in range(1, len(pts)):
        dx = pts[i][0] - pts[i-1][0]
        dy = pts[i][1] - pts[i-1][1]
        s += math.hypot(dx, dy)
    return s


def _variant_sort_key(name: str):
    m = re.search(r"step_(\d+)", name)
    if m:
        return (0, int(m.group(1)), name)
    return (1, 9999, name)


def _variant_label(name: str):
    low = name.lower()
    m = re.search(r"step_(\d+)", low)
    if "fonta" in low:
        if m:
            return f"Font A / Step {int(m.group(1)):02d}"
        return "Font A"
    if "fontb" in low:
        if m:
            return f"Font B / Step {int(m.group(1)):02d}"
        return "Font B"
    if m:
        return f"Step {int(m.group(1)):02d}"
    return name


# -----------------------------
# 骨架提取
# -----------------------------
def extract_skeleton_json(svg_path: Path, out_json: Path, side: int = 1024):
    svg_text = svg_path.read_text(encoding="utf-8", errors="ignore")
    meta = _parse_svg_meta(svg_text)
    vb = meta["viewBox"]

    mask, _ = _rasterize_svg_to_mask(svg_text, side=side)

    if mask.sum() == 0:
        data = {
            "code": svg_path.parent.name,
            "variant": svg_path.stem.replace(svg_path.parent.name + "_", ""),
            "viewBox": vb,
            "segments": [],
            "source_svg": svg_path.name
        }
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    skel = skeletonize(mask)
    dist = distance_transform_edt(mask)

    raw_segments = _build_skeleton_segments(skel)
    seg_datas = []

    avg_scale = (vb[2] / side + vb[3] / side) / 2.0

    for idx, seg in enumerate(raw_segments):
        pix_pts = _simplify_segment_pixels(seg, max_points=30)
        if len(pix_pts) < 2:
            continue

        pts = []
        for xp, yp in pix_pts:
            xs, ys = _pixel_to_svg(xp, yp, vb, side)
            rr = float(dist[yp, xp]) * avg_scale
            rr = max(rr, 1.5)
            pts.append({
                "x": round(xs, 4),
                "y": round(ys, 4),
                "w": round(rr, 4)
            })

        if len(pts) >= 2:
            seg_datas.append({
                "id": idx,
                "points": pts,
                "length": round(_length_of_pts([(p["x"], p["y"]) for p in pts]), 4)
            })

    # 按长度排序
    seg_datas = sorted(seg_datas, key=lambda z: z["length"], reverse=True)

    code = svg_path.parent.name
    variant = svg_path.stem.replace(code + "_", "")

    data = {
        "code": code,
        "variant": variant,
        "viewBox": vb,
        "segments": seg_datas,
        "source_svg": svg_path.name
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


# -----------------------------
# 根据骨架重建字形
# -----------------------------
def _polygon_path_from_points(poly):
    if len(poly) < 3:
        return ""
    parts = [f"M {poly[0][0]:.3f} {poly[0][1]:.3f}"]
    for x, y in poly[1:]:
        parts.append(f"L {x:.3f} {y:.3f}")
    parts.append("Z")
    return " ".join(parts)


def _ribbon_from_segment(seg):
    pts = seg.get("points", [])
    if not pts:
        return {"polys": [], "caps": []}

    if len(pts) == 1:
        p = pts[0]
        return {"polys": [], "caps": [(p["x"], p["y"], p["w"])]}

    left = []
    right = []

    n = len(pts)
    for i, p in enumerate(pts):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1 = pts[i + 1] if i < n - 1 else pts[i]

        dx = p1["x"] - p0["x"]
        dy = p1["y"] - p0["y"]
        ln = math.hypot(dx, dy)
        if ln < 1e-6:
            nx, ny = 0.0, 1.0
        else:
            nx, ny = -dy / ln, dx / ln

        r = max(float(p.get("w", 4.0)), 1.0)

        left.append((p["x"] + nx * r, p["y"] + ny * r))
        right.append((p["x"] - nx * r, p["y"] - ny * r))

    poly = left + list(reversed(right))
    caps = [
        (pts[0]["x"], pts[0]["y"], max(float(pts[0].get("w", 4.0)), 1.0)),
        (pts[-1]["x"], pts[-1]["y"], max(float(pts[-1].get("w", 4.0)), 1.0)),
    ]
    return {"polys": [poly], "caps": caps}


def build_svg_from_segments(view_box, segments, fill="#C8A37A", stroke="none", show_skeleton=False):
    vx, vy, vw, vh = view_box
    pieces = []
    pieces.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx} {vy} {vw} {vh}">')
    pieces.append('<rect x="0" y="0" width="100%" height="100%" fill="white" fill-opacity="0"/>')

    # 重建轮廓
    for seg in segments:
        rb = _ribbon_from_segment(seg)
        for poly in rb["polys"]:
            d = _polygon_path_from_points(poly)
            if d:
                pieces.append(f'<path d="{d}" fill="{fill}" stroke="{stroke}"/>')
        for cx, cy, r in rb["caps"]:
            pieces.append(f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="{r:.3f}" fill="{fill}" stroke="{stroke}"/>')

    if show_skeleton:
        for seg in segments:
            pts = seg.get("points", [])
            if len(pts) >= 2:
                d = "M " + " L ".join([f'{p["x"]:.3f} {p["y"]:.3f}' for p in pts])
                pieces.append(f'<path d="{d}" fill="none" stroke="#ff4d4f" stroke-width="1.5"/>')
            for p in pts:
                pieces.append(f'<circle cx="{p["x"]:.3f}" cy="{p["y"]:.3f}" r="2.5" fill="#ffffff" stroke="#ff4d4f" stroke-width="1"/>')

    pieces.append("</svg>")
    return "\n".join(pieces)


def save_edited_skeleton(job_dir: Path, code: str, variant: str, payload: dict):
    code = code.upper().strip()
    variant = _safe_variant_name(variant)

    skeleton_dir = job_dir / "skeleton"
    json_path = skeleton_dir / code / f"{code}_{variant}.json"
    if not json_path.exists():
        raise FileNotFoundError(f"skeleton json not found: {json_path}")

    base = json.loads(json_path.read_text(encoding="utf-8"))
    view_box = payload.get("viewBox", base.get("viewBox"))
    segments = payload.get("segments", base.get("segments", []))

    out_dir = job_dir / "skeleton_edits" / code
    out_dir.mkdir(parents=True, exist_ok=True)

    out_json = out_dir / f"{code}_{variant}_edited.json"
    out_svg = out_dir / f"{code}_{variant}_edited.svg"
    out_png = out_dir / f"{code}_{variant}_edited.png"

    saved = {
        "code": code,
        "variant": variant,
        "viewBox": view_box,
        "segments": segments,
        "source": "edited"
    }
    out_json.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")

    svg_text = build_svg_from_segments(view_box, segments, fill="#C8A37A", stroke="none", show_skeleton=False)
    out_svg.write_text(svg_text, encoding="utf-8")

    png_bytes = cairosvg.svg2png(bytestring=svg_text.encode("utf-8"), output_width=1400, output_height=1400)
    out_png.write_bytes(png_bytes)

    return {
        "json": out_json,
        "svg": out_svg,
        "png": out_png
    }


# -----------------------------
# 生成清单 + HTML 页面
# -----------------------------
def _collect_svg_variants(job_dir: Path):
    svg_root = job_dir / "svg"
    manifest = {
        "job_id": job_dir.name,
        "codes": []
    }
    if not svg_root.exists():
        return manifest

    code_dirs = [d for d in svg_root.iterdir() if d.is_dir() and d.name.startswith("U")]
    code_dirs = sorted(code_dirs, key=lambda d: d.name)

    for code_dir in code_dirs:
        code = code_dir.name
        variants = []
        for svg_file in sorted(code_dir.glob("*.svg")):
            stem = svg_file.stem
            prefix = code + "_"
            if not stem.startswith(prefix):
                continue
            variant = stem[len(prefix):]
            variants.append({
                "name": variant,
                "label": _variant_label(variant),
                "file": svg_file.name
            })

        variants = sorted(variants, key=lambda z: _variant_sort_key(z["name"]))
        try:
            ch = chr(int(code[1:], 16))
        except Exception:
            ch = code

        manifest["codes"].append({
            "code": code,
            "char": ch,
            "variants": variants
        })

    return manifest


def make_skeleton_preview_html(job_dir: Path, manifest: dict):
    html_path = job_dir / "skeleton_preview.html"
    cards = []

    total_codes = len(manifest.get("codes", []))
    total_variants = 0
    for item in manifest.get("codes", []):
        total_variants += len(item.get("variants", []))

    for item in manifest.get("codes", [])[:80]:
        code = item["code"]
        ch = item["char"]
        cnt = len(item.get("variants", []))
        cards.append(f"""
        <div class="card">
          <div class="ch">{ch}</div>
          <div class="code">{code}</div>
          <div class="cnt">{cnt} 个版本</div>
        </div>
        """)

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>中心线骨架预览</title>
<style>
body {{
  font-family: Arial, "Microsoft YaHei", sans-serif;
  background:#f5f5f5;
  color:#222;
  padding:24px;
}}
.wrap {{
  max-width:1200px;
  margin:0 auto;
}}
.panel {{
  background:#fff;
  border-radius:14px;
  box-shadow:0 1px 8px rgba(0,0,0,.08);
  padding:20px;
  margin-bottom:20px;
}}
.grid {{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(110px,1fr));
  gap:12px;
}}
.card {{
  border:1px solid #eee;
  border-radius:10px;
  background:#fafafa;
  padding:12px;
  text-align:center;
}}
.ch {{
  font-size:28px;
  margin-bottom:8px;
}}
.code {{
  font-family:Consolas, monospace;
  font-size:12px;
  color:#666;
}}
.cnt {{
  margin-top:8px;
  color:#888;
  font-size:13px;
}}
a.btn {{
  display:inline-block;
  background:#1f5eff;
  color:#fff;
  text-decoration:none;
  padding:10px 16px;
  border-radius:8px;
  margin-right:10px;
}}
.note {{
  color:#666;
  line-height:1.8;
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="panel">
    <h1>中心线 / 骨架功能</h1>
    <div class="note">
      这个模块不会破坏你原有的字体插值、SVG 预览、TTF 家族预览、可变滑杆预览。<br>
      它是独立新增的功能：自动提取原字形与中间 step 的中心线，并提供中心线 / 关键点编辑。<br>
      编辑完成后，当前字形可保存为 SVG 或 PNG 图片。
    </div>
    <p>
      <a class="btn" href="/skeleton_editor/{job_dir.name}" target="_blank">打开骨架编辑器</a>
      <a class="btn" href="/preview/{job_dir.name}" target="_blank">打开原 SVG 变化预览</a>
    </p>
  </div>

  <div class="panel">
    <h2>任务概览</h2>
    <div class="note">
      字形数量：{total_codes} 个<br>
      总版本数量（包含原字体和中间步骤）：{total_variants} 个
    </div>
  </div>

  <div class="panel">
    <h2>已识别字形列表（前 80 个）</h2>
    <div class="grid">
      {''.join(cards)}
    </div>
  </div>
</div>
</body>
</html>
"""
    html_path.write_text(html, encoding="utf-8")


def make_skeleton_editor_html(job_dir: Path, manifest: dict):
    html_path = job_dir / "skeleton_editor.html"
    job_id = job_dir.name

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>骨架编辑器</title>
<style>
body {{
  margin:0;
  font-family:Arial, "Microsoft YaHei", sans-serif;
  background:#f5f5f5;
  color:#222;
}}
.header {{
  background:#111;
  color:#fff;
  padding:18px 26px;
}}
.wrap {{
  display:grid;
  grid-template-columns:280px 1fr 320px;
  gap:18px;
  padding:18px;
}}
.panel {{
  background:#fff;
  border-radius:14px;
  box-shadow:0 1px 8px rgba(0,0,0,.08);
  padding:16px;
}}
.left-list {{
  max-height:720px;
  overflow-y:auto;
}}
.item-btn {{
  width:100%;
  text-align:left;
  background:#fafafa;
  border:1px solid #ddd;
  border-radius:8px;
  padding:10px;
  margin-bottom:8px;
  cursor:pointer;
}}
.item-btn.active {{
  background:#1f5eff;
  color:#fff;
  border-color:#1f5eff;
}}
select, input[type=range] {{
  width:100%;
}}
canvas, svg {{
  background:#fff;
}}
.topbar {{
  display:flex;
  gap:10px;
  flex-wrap:wrap;
  margin-bottom:10px;
}}
button {{
  background:#1f5eff;
  color:#fff;
  border:0;
  border-radius:8px;
  padding:10px 14px;
  cursor:pointer;
}}
button.secondary {{
  background:#666;
}}
.note {{
  color:#666;
  line-height:1.7;
  font-size:13px;
}}
.row {{
  margin-bottom:12px;
}}
.label {{
  font-weight:bold;
  margin-bottom:6px;
}}
#mainSvg {{
  width:100%;
  height:740px;
  border:1px solid #e5e5e5;
  border-radius:12px;
  background:#ffffff;
}}
.small {{
  font-size:12px;
  color:#666;
}}
.infobox {{
  font-family:Consolas, monospace;
  font-size:12px;
  color:#555;
  background:#fafafa;
  border:1px solid #eee;
  border-radius:8px;
  padding:10px;
  white-space:pre-wrap;
}}
.chk {{
  display:flex;
  align-items:center;
  gap:8px;
  margin-bottom:8px;
}}
</style>
</head>
<body>
<div class="header">
  <h1 style="margin:0;">骨架编辑器</h1>
  <div style="margin-top:8px;color:#ddd;">
    左侧选择字形；中间显示原轮廓、中心线与关键点；右侧可调节点宽度并保存为 SVG / PNG。
  </div>
</div>

<div class="wrap">
  <div class="panel">
    <div class="row">
      <div class="label">字形列表</div>
      <div id="glyphList" class="left-list"></div>
    </div>
  </div>

  <div class="panel">
    <div class="topbar">
      <button id="saveBtn">保存当前字形</button>
      <button id="exportSvgBtn" class="secondary">导出 SVG</button>
      <button id="exportPngBtn" class="secondary">导出 PNG</button>
      <button id="smoothBtn" class="secondary">平滑骨架</button>
      <button id="resetBtn" class="secondary">恢复当前版本</button>
    </div>

    <div class="row">
      <span class="small">版本：</span>
      <select id="variantSelect"></select>
    </div>

    <svg id="mainSvg" viewBox="0 0 1000 1000" xmlns="http://www.w3.org/2000/svg">
      <g id="outlineLayer" opacity="0.20"></g>
      <g id="rebuildLayer"></g>
      <g id="skeletonLayer"></g>
      <g id="handleLayer"></g>
    </svg>

    <div class="note" style="margin-top:12px;">
      操作方式：<br>
      1. 拖动白色关键点，可以修改中心线形状；<br>
      2. 选中一个关键点后，右侧可调该点宽度；<br>
      3. “保存当前字形”后，可以导出 SVG / PNG。
    </div>
  </div>

  <div class="panel">
    <div class="row">
      <div class="label">显示控制</div>
      <label class="chk"><input type="checkbox" id="showOutline" checked>显示原轮廓（灰）</label>
      <label class="chk"><input type="checkbox" id="showRebuild" checked>显示编辑后轮廓（米色）</label>
      <label class="chk"><input type="checkbox" id="showSkeleton" checked>显示中心线（红）</label>
      <label class="chk"><input type="checkbox" id="showHandles" checked>显示关键点（白）</label>
    </div>

    <div class="row">
      <div class="label">当前点宽度</div>
      <input type="range" id="widthSlider" min="1" max="120" step="0.5" value="10">
      <div id="widthVal" class="small">10</div>
    </div>

    <div class="row">
      <div class="label">当前选中</div>
      <div id="selectedInfo" class="infobox">未选中任何点</div>
    </div>

    <div class="row">
      <div class="label">状态</div>
      <div id="statusBox" class="infobox">等待加载...</div>
    </div>
  </div>
</div>

<script>
const JOB_ID = "{job_id}";
let manifestData = null;
let currentCode = null;
let currentVariant = null;
let currentData = null;
let originalData = null;
let selected = null;
let dragging = false;

const glyphList = document.getElementById("glyphList");
const variantSelect = document.getElementById("variantSelect");
const mainSvg = document.getElementById("mainSvg");
const outlineLayer = document.getElementById("outlineLayer");
const rebuildLayer = document.getElementById("rebuildLayer");
const skeletonLayer = document.getElementById("skeletonLayer");
const handleLayer = document.getElementById("handleLayer");
const widthSlider = document.getElementById("widthSlider");
const widthVal = document.getElementById("widthVal");
const selectedInfo = document.getElementById("selectedInfo");
const statusBox = document.getElementById("statusBox");

function setStatus(msg) {{
  statusBox.textContent = msg;
}}

function escHtml(s) {{
  return String(s).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}}

async function loadManifest() {{
  const res = await fetch(`/skeleton_manifest/${{JOB_ID}}`);
  manifestData = await res.json();
  renderGlyphList();
  if (manifestData.codes && manifestData.codes.length > 0) {{
    currentCode = manifestData.codes[0].code;
    renderGlyphList();
    renderVariants();
    await loadCurrentSkeleton();
  }} else {{
    setStatus("没有检测到可编辑的骨架数据。");
  }}
}}

function renderGlyphList() {{
  glyphList.innerHTML = "";
  (manifestData.codes || []).forEach(item => {{
    const btn = document.createElement("button");
    btn.className = "item-btn" + (item.code === currentCode ? " active" : "");
    btn.innerHTML = `<div style="font-size:24px;margin-bottom:6px;">${{escHtml(item.char)}}</div>
                     <div style="font-family:Consolas;font-size:12px;">${{item.code}}</div>
                     <div class="small">${{item.variants.length}} 个版本</div>`;
    btn.onclick = async () => {{
      currentCode = item.code;
      renderGlyphList();
      renderVariants();
      await loadCurrentSkeleton();
    }};
    glyphList.appendChild(btn);
  }});
}}

function renderVariants() {{
  const item = (manifestData.codes || []).find(x => x.code === currentCode);
  variantSelect.innerHTML = "";
  if (!item) return;

  item.variants.forEach(v => {{
    const opt = document.createElement("option");
    opt.value = v.name;
    opt.textContent = v.label;
    variantSelect.appendChild(opt);
  }});

  if (!currentVariant || !item.variants.some(v => v.name === currentVariant)) {{
    currentVariant = item.variants.length ? item.variants[0].name : null;
  }}
  variantSelect.value = currentVariant || "";
}}

variantSelect.addEventListener("change", async () => {{
  currentVariant = variantSelect.value;
  await loadCurrentSkeleton();
}});

async function loadCurrentSkeleton() {{
  if (!currentCode || !currentVariant) return;
  setStatus(`正在加载 ${{currentCode}} / ${{currentVariant}} ...`);

  const r1 = await fetch(`/skeleton_json/${{JOB_ID}}/${{currentCode}}/${{currentVariant}}`);
  currentData = await r1.json();
  originalData = JSON.parse(JSON.stringify(currentData));

  const vb = currentData.viewBox || [0,0,1000,1000];
  mainSvg.setAttribute("viewBox", vb.join(" "));

  const r2 = await fetch(`/raw_svg_variant/${{JOB_ID}}/${{currentCode}}/${{currentVariant}}`);
  const rawSvg = await r2.text();
  outlineLayer.innerHTML = rawSvg;

  selected = null;
  updateSelectedInfo();
  redrawAll();
  setStatus(`已加载：${{currentCode}} / ${{currentVariant}}`);
}}

function ptToStr(p) {{
  return `${{p.x.toFixed(1)}},${{p.y.toFixed(1)}}`;
}}

function buildRibbonPieces(points) {{
  if (!points || points.length === 0) return [];
  if (points.length === 1) {{
    const p = points[0];
    return [`<circle cx="${{p.x}}" cy="${{p.y}}" r="${{p.w}}" fill="#C8A37A"/>`];
  }}

  let left = [];
  let right = [];

  for (let i = 0; i < points.length; i++) {{
    const p = points[i];
    const p0 = i > 0 ? points[i - 1] : points[i];
    const p1 = i < points.length - 1 ? points[i + 1] : points[i];

    let dx = p1.x - p0.x;
    let dy = p1.y - p0.y;
    let ln = Math.hypot(dx, dy);
    let nx = 0, ny = 1;
    if (ln > 1e-6) {{
      nx = -dy / ln;
      ny = dx / ln;
    }}

    const r = Math.max(1, p.w || 4);
    left.push([p.x + nx * r, p.y + ny * r]);
    right.push([p.x - nx * r, p.y - ny * r]);
  }}

  const poly = left.concat(right.reverse());
  let d = "";
  if (poly.length >= 3) {{
    d = "M " + poly.map(v => `${{v[0].toFixed(2)}} ${{v[1].toFixed(2)}}`).join(" L ") + " Z";
  }}

  const out = [];
  if (d) out.push(`<path d="${{d}}" fill="#C8A37A" stroke="none"/>`);
  out.push(`<circle cx="${{points[0].x}}" cy="${{points[0].y}}" r="${{Math.max(1, points[0].w)}}" fill="#C8A37A"/>`);
  out.push(`<circle cx="${{points[points.length-1].x}}" cy="${{points[points.length-1].y}}" r="${{Math.max(1, points[points.length-1].w)}}" fill="#C8A37A"/>`);
  return out;
}}

function redrawAll() {{
  if (!currentData) return;

  const showOutline = document.getElementById("showOutline").checked;
  const showRebuild = document.getElementById("showRebuild").checked;
  const showSkeleton = document.getElementById("showSkeleton").checked;
  const showHandles = document.getElementById("showHandles").checked;

  outlineLayer.style.display = showOutline ? "" : "none";

  rebuildLayer.innerHTML = "";
  skeletonLayer.innerHTML = "";
  handleLayer.innerHTML = "";

  if (showRebuild) {{
    const pieces = [];
    (currentData.segments || []).forEach(seg => {{
      pieces.push(...buildRibbonPieces(seg.points || []));
    }});
    rebuildLayer.innerHTML = pieces.join("\\n");
  }}

  if (showSkeleton) {{
    (currentData.segments || []).forEach((seg, si) => {{
      const pts = seg.points || [];
      if (pts.length >= 2) {{
        const d = "M " + pts.map(p => `${{p.x.toFixed(2)}} ${{p.y.toFixed(2)}}`).join(" L ");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", d);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "#ff4d4f");
        path.setAttribute("stroke-width", "1.8");
        skeletonLayer.appendChild(path);
      }}
    }});
  }}

  if (showHandles) {{
    (currentData.segments || []).forEach((seg, si) => {{
      const pts = seg.points || [];
      pts.forEach((p, pi) => {{
        const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        c.setAttribute("cx", p.x);
        c.setAttribute("cy", p.y);
        c.setAttribute("r", selected && selected.seg === si && selected.pt === pi ? 6 : 4.5);
        c.setAttribute("fill", "#fff");
        c.setAttribute("stroke", selected && selected.seg === si && selected.pt === pi ? "#1f5eff" : "#ff4d4f");
        c.setAttribute("stroke-width", selected && selected.seg === si && selected.pt === pi ? "2.2" : "1.2");
        c.style.cursor = "pointer";

        c.addEventListener("pointerdown", (ev) => {{
          ev.preventDefault();
          selected = {{ seg: si, pt: pi }};
          dragging = true;
          widthSlider.value = p.w;
          widthVal.textContent = Number(p.w).toFixed(1);
          updateSelectedInfo();
          redrawAll();
        }});

        handleLayer.appendChild(c);
      }});
    }});
  }}
}}

function updateSelectedInfo() {{
  if (!selected || !currentData) {{
    selectedInfo.textContent = "未选中任何点";
    return;
  }}
  const p = currentData.segments[selected.seg].points[selected.pt];
  selectedInfo.textContent = `字符: ${{currentCode}}
版本: ${{currentVariant}}
段: ${{selected.seg}}
点: ${{selected.pt}}
x: ${{p.x.toFixed(2)}}
y: ${{p.y.toFixed(2)}}
width: ${{p.w.toFixed(2)}}`;
}}

function getMouseSvgPoint(evt) {{
  const pt = mainSvg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(mainSvg.getScreenCTM().inverse());
}}

mainSvg.addEventListener("pointermove", (evt) => {{
  if (!dragging || !selected || !currentData) return;
  const p = getMouseSvgPoint(evt);
  const obj = currentData.segments[selected.seg].points[selected.pt];
  obj.x = Number(p.x);
  obj.y = Number(p.y);
  updateSelectedInfo();
  redrawAll();
}});

window.addEventListener("pointerup", () => {{
  dragging = false;
}});

widthSlider.addEventListener("input", () => {{
  widthVal.textContent = Number(widthSlider.value).toFixed(1);
  if (!selected || !currentData) return;
  const obj = currentData.segments[selected.seg].points[selected.pt];
  obj.w = Number(widthSlider.value);
  updateSelectedInfo();
  redrawAll();
}});

document.getElementById("showOutline").addEventListener("change", redrawAll);
document.getElementById("showRebuild").addEventListener("change", redrawAll);
document.getElementById("showSkeleton").addEventListener("change", redrawAll);
document.getElementById("showHandles").addEventListener("change", redrawAll);

document.getElementById("resetBtn").addEventListener("click", () => {{
  if (!originalData) return;
  currentData = JSON.parse(JSON.stringify(originalData));
  selected = null;
  updateSelectedInfo();
  redrawAll();
  setStatus("已恢复当前版本原始骨架。");
}});

document.getElementById("smoothBtn").addEventListener("click", () => {{
  if (!currentData) return;
  (currentData.segments || []).forEach(seg => {{
    const pts = seg.points || [];
    if (pts.length < 3) return;
    const copied = JSON.parse(JSON.stringify(pts));
    for (let i = 1; i < pts.length - 1; i++) {{
      pts[i].x = (copied[i-1].x + copied[i].x + copied[i+1].x) / 3;
      pts[i].y = (copied[i-1].y + copied[i].y + copied[i+1].y) / 3;
    }}
  }});
  redrawAll();
  setStatus("已执行平滑。");
}});

async function saveCurrent() {{
  if (!currentData) return null;

  setStatus("正在保存当前编辑结果...");
  const res = await fetch(`/save_skeleton_edit/${{JOB_ID}}/${{currentCode}}/${{currentVariant}}`, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{
      viewBox: currentData.viewBox,
      segments: currentData.segments
    }})
  }});
  const data = await res.json();
  if (data.ok) {{
    setStatus("保存成功。");
    return data;
  }} else {{
    setStatus("保存失败：" + JSON.stringify(data));
    return null;
  }}
}}

document.getElementById("saveBtn").addEventListener("click", async () => {{
  await saveCurrent();
}});

document.getElementById("exportSvgBtn").addEventListener("click", async () => {{
  const ret = await saveCurrent();
  if (ret && ret.svg_url) {{
    window.open(ret.svg_url, "_blank");
  }}
}});

document.getElementById("exportPngBtn").addEventListener("click", async () => {{
  const ret = await saveCurrent();
  if (ret && ret.png_url) {{
    window.open(ret.png_url, "_blank");
  }}
}});

loadManifest().catch(err => {{
  console.error(err);
  setStatus("加载失败：" + err);
}});
</script>
</body>
</html>
"""
    html_path.write_text(html, encoding="utf-8")


def prepare_skeleton_assets(job_dir: Path):
    job_dir = Path(job_dir)
    svg_root = job_dir / "svg"
    skeleton_root = job_dir / "skeleton"
    skeleton_root.mkdir(parents=True, exist_ok=True)

    manifest = _collect_svg_variants(job_dir)

    for item in manifest.get("codes", []):
        code = item["code"]
        code_dir = svg_root / code
        out_code_dir = skeleton_root / code
        out_code_dir.mkdir(parents=True, exist_ok=True)

        for v in item.get("variants", []):
            variant = v["name"]
            svg_path = code_dir / f"{code}_{variant}.svg"
            out_json = out_code_dir / f"{code}_{variant}.json"

            if (not out_json.exists()) or (svg_path.exists() and svg_path.stat().st_mtime > out_json.stat().st_mtime):
                try:
                    extract_skeleton_json(svg_path, out_json)
                except Exception as e:
                    # 即使失败也要写一个空 JSON，避免前端报错
                    empty = {
                        "code": code,
                        "variant": variant,
                        "viewBox": [0, 0, 1000, 1000],
                        "segments": [],
                        "source_svg": svg_path.name if svg_path.exists() else ""
                    }
                    out_json.write_text(json.dumps(empty, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest_path = job_dir / "skeleton_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    make_skeleton_preview_html(job_dir, manifest)
    make_skeleton_editor_html(job_dir, manifest)

    return manifest_path

# =========================================================
# Lazy skeleton loading patch
# 只生成页面和 manifest，不一次性提取所有骨架
# =========================================================
def prepare_skeleton_pages_lazy(job_dir: Path):
    """
    快速准备骨架页面：
    只扫描 SVG 列表，生成 manifest 和 HTML。
    不批量提取所有 skeleton json。
    """
    job_dir = Path(job_dir)
    manifest = _collect_svg_variants(job_dir)

    manifest_path = job_dir / "skeleton_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    make_skeleton_preview_html(job_dir, manifest)
    make_skeleton_editor_html(job_dir, manifest)

    return manifest_path


def ensure_skeleton_json(job_dir: Path, code: str, variant: str):
    """
    只在用户真正点开某个字形 / 某个 step 时，
    才提取这个 SVG 的中心线。
    """
    job_dir = Path(job_dir)
    code = code.upper().strip()
    variant = _safe_variant_name(variant)

    svg_path = job_dir / "svg" / code / f"{code}_{variant}.svg"
    out_json = job_dir / "skeleton" / code / f"{code}_{variant}.json"

    if not svg_path.exists():
        raise FileNotFoundError(f"SVG not found: {svg_path}")

    need_extract = True

    if out_json.exists():
        try:
            need_extract = svg_path.stat().st_mtime > out_json.stat().st_mtime
        except Exception:
            need_extract = True

    if need_extract:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        # 768 比 1024 快，编辑预览精度已经够用
        extract_skeleton_json(svg_path, out_json, side=768)

    return out_json

# =========================================================
# Override: make_skeleton_editor_html
# 目标：
# 1. 只显示少量关键点
# 2. 拖一个点时，整条骨架和其它相邻部分协调联动
# =========================================================
def make_skeleton_editor_html(job_dir: Path, manifest: dict):
    html_path = job_dir / "skeleton_editor.html"
    job_id = job_dir.name

    html = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>骨架编辑器（关键点联动版）</title>
<style>
body {
  margin:0;
  font-family:Arial, "Microsoft YaHei", sans-serif;
  background:#f5f5f5;
  color:#222;
}
.header {
  background:#111;
  color:#fff;
  padding:18px 26px;
}
.wrap {
  display:grid;
  grid-template-columns:280px 1fr 320px;
  gap:18px;
  padding:18px;
}
.panel {
  background:#fff;
  border-radius:14px;
  box-shadow:0 1px 8px rgba(0,0,0,.08);
  padding:16px;
}
.left-list {
  max-height:720px;
  overflow-y:auto;
}
.item-btn {
  width:100%;
  text-align:left;
  background:#fafafa;
  border:1px solid #ddd;
  border-radius:8px;
  padding:10px;
  margin-bottom:8px;
  cursor:pointer;
}
.item-btn.active {
  background:#1f5eff;
  color:#fff;
  border-color:#1f5eff;
}
select, input[type=range] {
  width:100%;
}
.topbar {
  display:flex;
  gap:10px;
  flex-wrap:wrap;
  margin-bottom:10px;
}
button {
  background:#1f5eff;
  color:#fff;
  border:0;
  border-radius:8px;
  padding:10px 14px;
  cursor:pointer;
}
button.secondary {
  background:#666;
}
.note {
  color:#666;
  line-height:1.7;
  font-size:13px;
}
.row {
  margin-bottom:12px;
}
.label {
  font-weight:bold;
  margin-bottom:6px;
}
#mainSvg {
  width:100%;
  height:740px;
  border:1px solid #e5e5e5;
  border-radius:12px;
  background:#ffffff;
}
.small {
  font-size:12px;
  color:#666;
}
.infobox {
  font-family:Consolas, monospace;
  font-size:12px;
  color:#555;
  background:#fafafa;
  border:1px solid #eee;
  border-radius:8px;
  padding:10px;
  white-space:pre-wrap;
}
.chk {
  display:flex;
  align-items:center;
  gap:8px;
  margin-bottom:8px;
}
.tip {
  background:#fff7e6;
  color:#8a5b00;
  border:1px solid #ffe0a3;
  border-radius:8px;
  padding:10px;
  font-size:12px;
  line-height:1.6;
}
</style>
</head>
<body>
<div class="header">
  <h1 style="margin:0;">骨架编辑器（关键点联动版）</h1>
  <div style="margin-top:8px;color:#ddd;">
    自动简化为少量关键点；拖动一个点时，其它点会协调联动，不再是孤立变形。
  </div>
</div>

<div class="wrap">
  <div class="panel">
    <div class="row">
      <div class="label">字形列表</div>
      <div id="glyphList" class="left-list"></div>
    </div>
  </div>

  <div class="panel">
    <div class="topbar">
      <button id="saveBtn">保存当前字形</button>
      <button id="exportSvgBtn" class="secondary">导出 SVG</button>
      <button id="exportPngBtn" class="secondary">导出 PNG</button>
      <button id="smoothBtn" class="secondary">平滑骨架</button>
      <button id="resetBtn" class="secondary">恢复当前版本</button>
    </div>

    <div class="row">
      <span class="small">版本：</span>
      <select id="variantSelect"></select>
    </div>

    <svg id="mainSvg" viewBox="0 0 1000 1000" xmlns="http://www.w3.org/2000/svg">
      <g id="outlineLayer" opacity="0.18"></g>
      <g id="rebuildLayer"></g>
      <g id="skeletonLayer"></g>
      <g id="handleLayer"></g>
    </svg>

    <div class="note" style="margin-top:12px;">
      操作方式：<br>
      1. 现在骨架点已经自动简化为“关键点”；<br>
      2. 拖动一个关键点时，会带动同笔画与相邻部分一起协调变化；<br>
      3. 选中一个关键点后，右侧调宽度时，附近点宽度也会一起变化；<br>
      4. 保存后可导出 SVG / PNG。
    </div>
  </div>

  <div class="panel">
    <div class="row">
      <div class="label">显示控制</div>
      <label class="chk"><input type="checkbox" id="showOutline" checked>显示原轮廓（灰）</label>
      <label class="chk"><input type="checkbox" id="showRebuild" checked>显示编辑后轮廓（米色）</label>
      <label class="chk"><input type="checkbox" id="showSkeleton" checked>显示中心线（红）</label>
      <label class="chk"><input type="checkbox" id="showHandles" checked>显示关键点（白）</label>
    </div>

    <div class="row">
      <div class="label">当前点宽度</div>
      <input type="range" id="widthSlider" min="1" max="120" step="0.5" value="10">
      <div id="widthVal" class="small">10</div>
    </div>

    <div class="row">
      <div class="label">当前选中</div>
      <div id="selectedInfo" class="infobox">未选中任何点</div>
    </div>

    <div class="row">
      <div class="label">状态</div>
      <div id="statusBox" class="infobox">等待加载...</div>
    </div>

    <div class="row">
      <div class="tip">
        联动参数已经内置：<br>
        - 同一骨架上的邻近点：强联动<br>
        - 其它骨架上的邻近区域：弱联动<br>
        - 端点：自动减弱，避免字形飞掉
      </div>
    </div>
  </div>
</div>

<script>
const JOB_ID = "__JOB_ID__";

let manifestData = null;
let currentCode = null;
let currentVariant = null;

let currentData = null;     // 当前正在编辑的数据（已简化为关键点）
let originalData = null;    // 当前版本的初始简化数据
let selected = null;

let dragging = false;
let dragStart = null;       // SVG 坐标
let dragBase = null;        // 拖拽开始前的 currentData 深拷贝

const glyphList = document.getElementById("glyphList");
const variantSelect = document.getElementById("variantSelect");
const mainSvg = document.getElementById("mainSvg");
const outlineLayer = document.getElementById("outlineLayer");
const rebuildLayer = document.getElementById("rebuildLayer");
const skeletonLayer = document.getElementById("skeletonLayer");
const handleLayer = document.getElementById("handleLayer");
const widthSlider = document.getElementById("widthSlider");
const widthVal = document.getElementById("widthVal");
const selectedInfo = document.getElementById("selectedInfo");
const statusBox = document.getElementById("statusBox");

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function setStatus(msg) {
  statusBox.textContent = msg;
}

function escHtml(s) {
  return String(s).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}

function dist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function bboxOfPoints(points) {
  if (!points || points.length === 0) return {minX:0, minY:0, maxX:1000, maxY:1000, w:1000, h:1000};
  let minX = points[0].x, minY = points[0].y, maxX = points[0].x, maxY = points[0].y;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  return {minX, minY, maxX, maxY, w:maxX-minX, h:maxY-minY};
}

function bboxOfData(data) {
  let all = [];
  (data.segments || []).forEach(seg => {
    (seg.points || []).forEach(p => all.push(p));
  });
  return bboxOfPoints(all);
}

// ------------------------------
// 关键点简化：RDP
// ------------------------------
function pointLineDistance(p, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  if (Math.abs(dx) < 1e-9 && Math.abs(dy) < 1e-9) return dist(p, a);
  const t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx*dx + dy*dy);
  const tt = Math.max(0, Math.min(1, t));
  const q = {x: a.x + tt * dx, y: a.y + tt * dy};
  return dist(p, q);
}

function rdp(points, epsilon) {
  if (points.length <= 2) return points.slice();

  let dmax = 0;
  let index = 0;
  const end = points.length - 1;

  for (let i = 1; i < end; i++) {
    const d = pointLineDistance(points[i], points[0], points[end]);
    if (d > dmax) {
      index = i;
      dmax = d;
    }
  }

  if (dmax > epsilon) {
    const rec1 = rdp(points.slice(0, index + 1), epsilon);
    const rec2 = rdp(points.slice(index), epsilon);
    return rec1.slice(0, -1).concat(rec2);
  } else {
    return [points[0], points[end]];
  }
}

function evenSample(points, n) {
  if (points.length <= n) return points.slice();
  const out = [];
  for (let i = 0; i < n; i++) {
    const idx = Math.round(i * (points.length - 1) / (n - 1));
    out.push(points[idx]);
  }
  return out;
}

function simplifySegment(points) {
  if (!points || points.length <= 1) return deepClone(points || []);
  if (points.length <= 8) return deepClone(points);

  const bb = bboxOfPoints(points);
  const diag = Math.max(1, Math.hypot(bb.w, bb.h));

  // 根据字形大小自动确定简化程度
  let epsilon = Math.max(6, diag * 0.018);
  let simp = rdp(points, epsilon);

  // 太少则补一点
  if (simp.length < 4) {
    simp = evenSample(points, Math.min(6, points.length));
  }

  // 太多则进一步压缩
  if (simp.length > 10) {
    simp = evenSample(simp, 10);
  }

  // 小分支最多留 4-5 个点
  if (diag < 180 && simp.length > 5) {
    simp = evenSample(simp, 5);
  }

  return simp.map(p => ({x:p.x, y:p.y, w:p.w}));
}

function reduceToKeyPoints(data) {
  const out = deepClone(data);
  out.segments = (out.segments || [])
    .map(seg => {
      return {
        ...seg,
        points: simplifySegment(seg.points || [])
      };
    })
    .filter(seg => (seg.points || []).length >= 2);
  return out;
}

// ------------------------------
// 轮廓重建
// ------------------------------
function buildRibbonPieces(points) {
  if (!points || points.length === 0) return [];
  if (points.length === 1) {
    const p = points[0];
    return [`<circle cx="${p.x}" cy="${p.y}" r="${p.w}" fill="#C8A37A"/>`];
  }

  let left = [];
  let right = [];

  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const p0 = i > 0 ? points[i - 1] : points[i];
    const p1 = i < points.length - 1 ? points[i + 1] : points[i];

    let dx = p1.x - p0.x;
    let dy = p1.y - p0.y;
    let ln = Math.hypot(dx, dy);
    let nx = 0, ny = 1;
    if (ln > 1e-6) {
      nx = -dy / ln;
      ny = dx / ln;
    }

    const r = Math.max(1, p.w || 4);
    left.push([p.x + nx * r, p.y + ny * r]);
    right.push([p.x - nx * r, p.y - ny * r]);
  }

  const poly = left.concat(right.reverse());
  let d = "";
  if (poly.length >= 3) {
    d = "M " + poly.map(v => `${v[0].toFixed(2)} ${v[1].toFixed(2)}`).join(" L ") + " Z";
  }

  const out = [];
  if (d) out.push(`<path d="${d}" fill="#C8A37A" stroke="none"/>`);
  out.push(`<circle cx="${points[0].x}" cy="${points[0].y}" r="${Math.max(1, points[0].w)}" fill="#C8A37A"/>`);
  out.push(`<circle cx="${points[points.length-1].x}" cy="${points[points.length-1].y}" r="${Math.max(1, points[points.length-1].w)}" fill="#C8A37A"/>`);
  return out;
}

// ------------------------------
// 联动变形：牵一发而动全身
// ------------------------------
function applyGlobalDeform(baseData, selSeg, selPt, dx, dy) {
  const out = deepClone(baseData);
  const ref = baseData.segments[selSeg].points[selPt];
  const bb = bboxOfData(baseData);
  const globalDiag = Math.max(1, Math.hypot(bb.w, bb.h));

  const sigmaGlobal = Math.max(80, globalDiag * 0.18); // 全局联动
  const sigmaLocal = 1.8;                              // 同段索引联动

  out.segments.forEach((seg, si) => {
    const baseSeg = baseData.segments[si];
    seg.points.forEach((p, pi) => {
      const bp = baseSeg.points[pi];

      // 同一段上：按索引距离衰减
      let wLocal = 0;
      if (si === selSeg) {
        const idxDist = Math.abs(pi - selPt);
        wLocal = Math.exp(-(idxDist * idxDist) / (2 * sigmaLocal * sigmaLocal));
      }

      // 全局上：按空间距离弱联动
      const d = Math.hypot(bp.x - ref.x, bp.y - ref.y);
      let wGlobal = 0.22 * Math.exp(-(d * d) / (2 * sigmaGlobal * sigmaGlobal));

      // 合成权重
      let w = Math.min(1, wLocal + wGlobal);

      // 端点减弱，避免字形边角飞掉
      if (pi === 0 || pi === seg.points.length - 1) {
        w *= 0.72;
      }

      p.x = bp.x + dx * w;
      p.y = bp.y + dy * w;
    });
  });

  return out;
}

function applyWidthInfluence(newWidth) {
  if (!selected || !currentData) return;

  const seg = currentData.segments[selected.seg];
  if (!seg) return;

  const pts = seg.points || [];
  const baseW = pts[selected.pt].w;
  const delta = Number(newWidth) - baseW;
  const sigma = 1.4;

  pts.forEach((p, pi) => {
    const idxDist = Math.abs(pi - selected.pt);
    const w = Math.exp(-(idxDist * idxDist) / (2 * sigma * sigma));
    p.w = Math.max(1, p.w + delta * w);
  });

  // 其它段弱联动一点宽度，避免完全断裂
  currentData.segments.forEach((otherSeg, si) => {
    if (si === selected.seg) return;
    otherSeg.points.forEach(op => {
      const sp = pts[selected.pt];
      const d = Math.hypot(op.x - sp.x, op.y - sp.y);
      const bb = bboxOfData(currentData);
      const sigmaGlobal = Math.max(60, Math.hypot(bb.w, bb.h) * 0.12);
      const wg = 0.08 * Math.exp(-(d * d) / (2 * sigmaGlobal * sigmaGlobal));
      op.w = Math.max(1, op.w + delta * wg);
    });
  });
}

// ------------------------------
// UI
// ------------------------------
async function loadManifest() {
  const res = await fetch(`/skeleton_manifest/${JOB_ID}`);
  manifestData = await res.json();
  renderGlyphList();

  if (manifestData.codes && manifestData.codes.length > 0) {
    currentCode = manifestData.codes[0].code;
    renderGlyphList();
    renderVariants();
    await loadCurrentSkeleton();
  } else {
    setStatus("没有检测到可编辑的骨架数据。");
  }
}

function renderGlyphList() {
  glyphList.innerHTML = "";
  (manifestData.codes || []).forEach(item => {
    const btn = document.createElement("button");
    btn.className = "item-btn" + (item.code === currentCode ? " active" : "");
    btn.innerHTML = `<div style="font-size:24px;margin-bottom:6px;">${escHtml(item.char)}</div>
                     <div style="font-family:Consolas;font-size:12px;">${item.code}</div>
                     <div class="small">${item.variants.length} 个版本</div>`;
    btn.onclick = async () => {
      currentCode = item.code;
      renderGlyphList();
      renderVariants();
      await loadCurrentSkeleton();
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
  await loadCurrentSkeleton();
});

async function loadCurrentSkeleton() {
  if (!currentCode || !currentVariant) return;
  setStatus(`正在加载 ${currentCode} / ${currentVariant} ...`);

  const r1 = await fetch(`/skeleton_json/${JOB_ID}/${currentCode}/${currentVariant}`);
  const rawData = await r1.json();

  // 核心：加载后立即压缩成关键点
  currentData = reduceToKeyPoints(rawData);
  originalData = deepClone(currentData);

  const vb = currentData.viewBox || [0,0,1000,1000];
  mainSvg.setAttribute("viewBox", vb.join(" "));

  const r2 = await fetch(`/raw_svg_variant/${JOB_ID}/${currentCode}/${currentVariant}`);
  const rawSvg = await r2.text();
  outlineLayer.innerHTML = rawSvg;

  selected = null;
  updateSelectedInfo();
  redrawAll();
  setStatus(`已加载：${currentCode} / ${currentVariant}；关键点模式已启用`);
}

function redrawAll() {
  if (!currentData) return;

  const showOutline = document.getElementById("showOutline").checked;
  const showRebuild = document.getElementById("showRebuild").checked;
  const showSkeleton = document.getElementById("showSkeleton").checked;
  const showHandles = document.getElementById("showHandles").checked;

  outlineLayer.style.display = showOutline ? "" : "none";

  rebuildLayer.innerHTML = "";
  skeletonLayer.innerHTML = "";
  handleLayer.innerHTML = "";

  if (showRebuild) {
    const pieces = [];
    (currentData.segments || []).forEach(seg => {
      pieces.push(...buildRibbonPieces(seg.points || []));
    });
    rebuildLayer.innerHTML = pieces.join("\\n");
  }

  if (showSkeleton) {
    (currentData.segments || []).forEach((seg, si) => {
      const pts = seg.points || [];
      if (pts.length >= 2) {
        const d = "M " + pts.map(p => `${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(" L ");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", d);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "#ff4d4f");
        path.setAttribute("stroke-width", "2");
        skeletonLayer.appendChild(path);
      }
    });
  }

  if (showHandles) {
    (currentData.segments || []).forEach((seg, si) => {
      const pts = seg.points || [];
      pts.forEach((p, pi) => {
        const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        c.setAttribute("cx", p.x);
        c.setAttribute("cy", p.y);
        c.setAttribute("r", selected && selected.seg === si && selected.pt === pi ? 7 : 5.2);
        c.setAttribute("fill", "#fff");
        c.setAttribute("stroke", selected && selected.seg === si && selected.pt === pi ? "#1f5eff" : "#ff4d4f");
        c.setAttribute("stroke-width", selected && selected.seg === si && selected.pt === pi ? "2.5" : "1.5");
        c.style.cursor = "pointer";

        c.addEventListener("pointerdown", (ev) => {
          ev.preventDefault();
          ev.stopPropagation();

          selected = { seg: si, pt: pi };
          dragging = true;
          dragStart = getMouseSvgPoint(ev);
          dragBase = deepClone(currentData);

          widthSlider.value = p.w;
          widthVal.textContent = Number(p.w).toFixed(1);

          updateSelectedInfo();
          redrawAll();
        });

        handleLayer.appendChild(c);
      });
    });
  }
}

function updateSelectedInfo() {
  if (!selected || !currentData) {
    selectedInfo.textContent = "未选中任何点";
    return;
  }
  const p = currentData.segments[selected.seg].points[selected.pt];
  selectedInfo.textContent =
`字符: ${currentCode}
版本: ${currentVariant}
段: ${selected.seg}
点: ${selected.pt}
x: ${p.x.toFixed(2)}
y: ${p.y.toFixed(2)}
width: ${p.w.toFixed(2)}`;
}

function getMouseSvgPoint(evt) {
  const pt = mainSvg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(mainSvg.getScreenCTM().inverse());
}

mainSvg.addEventListener("pointermove", (evt) => {
  if (!dragging || !selected || !dragBase || !currentData) return;

  const now = getMouseSvgPoint(evt);
  const dx = now.x - dragStart.x;
  const dy = now.y - dragStart.y;

  currentData = applyGlobalDeform(dragBase, selected.seg, selected.pt, dx, dy);
  updateSelectedInfo();
  redrawAll();
});

window.addEventListener("pointerup", () => {
  dragging = false;
  dragStart = null;
  dragBase = null;
});

widthSlider.addEventListener("input", () => {
  widthVal.textContent = Number(widthSlider.value).toFixed(1);
  if (!selected || !currentData) return;
  applyWidthInfluence(Number(widthSlider.value));
  updateSelectedInfo();
  redrawAll();
});

document.getElementById("showOutline").addEventListener("change", redrawAll);
document.getElementById("showRebuild").addEventListener("change", redrawAll);
document.getElementById("showSkeleton").addEventListener("change", redrawAll);
document.getElementById("showHandles").addEventListener("change", redrawAll);

document.getElementById("resetBtn").addEventListener("click", () => {
  if (!originalData) return;
  currentData = deepClone(originalData);
  selected = null;
  updateSelectedInfo();
  redrawAll();
  setStatus("已恢复当前版本原始关键点。");
});

document.getElementById("smoothBtn").addEventListener("click", () => {
  if (!currentData) return;

  (currentData.segments || []).forEach(seg => {
    const pts = seg.points || [];
    if (pts.length < 3) return;

    const copied = deepClone(pts);
    for (let i = 1; i < pts.length - 1; i++) {
      pts[i].x = (copied[i-1].x + copied[i].x + copied[i+1].x) / 3;
      pts[i].y = (copied[i-1].y + copied[i].y + copied[i+1].y) / 3;
      pts[i].w = Math.max(1, (copied[i-1].w + copied[i].w + copied[i+1].w) / 3);
    }
  });

  redrawAll();
  setStatus("已执行平滑。");
});

async function saveCurrent() {
  if (!currentData) return null;

  setStatus("正在保存当前编辑结果...");
  const res = await fetch(`/save_skeleton_edit/${JOB_ID}/${currentCode}/${currentVariant}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      viewBox: currentData.viewBox,
      segments: currentData.segments
    })
  });
  const data = await res.json();

  if (data.ok) {
    setStatus("保存成功。");
    return data;
  } else {
    setStatus("保存失败：" + JSON.stringify(data));
    return null;
  }
}

document.getElementById("saveBtn").addEventListener("click", async () => {
  await saveCurrent();
});

document.getElementById("exportSvgBtn").addEventListener("click", async () => {
  const ret = await saveCurrent();
  if (ret && ret.svg_url) {
    window.open(ret.svg_url, "_blank");
  }
});

document.getElementById("exportPngBtn").addEventListener("click", async () => {
  const ret = await saveCurrent();
  if (ret && ret.png_url) {
    window.open(ret.png_url, "_blank");
  }
});

loadManifest().catch(err => {
  console.error(err);
  setStatus("加载失败：" + err);
});
</script>
</body>
</html>
""".replace("__JOB_ID__", job_id)

    html_path.write_text(html, encoding="utf-8")

# =========================================================
# Override: make_skeleton_editor_html
# Bezier Control + Topological Adjacent Constraint Editor
# =========================================================
def make_skeleton_editor_html(job_dir: Path, manifest: dict):
    html_path = job_dir / "skeleton_editor.html"
    job_id = job_dir.name

    html = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>骨架编辑器（贝塞尔控制版）</title>
<style>
body {
  margin:0;
  font-family:Arial, "Microsoft YaHei", sans-serif;
  background:#f5f5f5;
  color:#222;
}
.header {
  background:#111;
  color:#fff;
  padding:18px 26px;
}
.wrap {
  display:grid;
  grid-template-columns:280px 1fr 330px;
  gap:18px;
  padding:18px;
}
.panel {
  background:#fff;
  border-radius:14px;
  box-shadow:0 1px 8px rgba(0,0,0,.08);
  padding:16px;
}
.left-list {
  max-height:720px;
  overflow-y:auto;
}
.item-btn {
  width:100%;
  text-align:left;
  background:#fafafa;
  border:1px solid #ddd;
  border-radius:8px;
  padding:10px;
  margin-bottom:8px;
  cursor:pointer;
}
.item-btn.active {
  background:#1f5eff;
  color:#fff;
  border-color:#1f5eff;
}
select, input[type=range] {
  width:100%;
}
.topbar {
  display:flex;
  gap:10px;
  flex-wrap:wrap;
  margin-bottom:10px;
}
button {
  background:#1f5eff;
  color:#fff;
  border:0;
  border-radius:8px;
  padding:10px 14px;
  cursor:pointer;
}
button.secondary {
  background:#666;
}
button.warn {
  background:#8a5b00;
}
.note {
  color:#666;
  line-height:1.7;
  font-size:13px;
}
.row {
  margin-bottom:12px;
}
.label {
  font-weight:bold;
  margin-bottom:6px;
}
#mainSvg {
  width:100%;
  height:740px;
  border:1px solid #e5e5e5;
  border-radius:12px;
  background:#ffffff;
}
.small {
  font-size:12px;
  color:#666;
}
.infobox {
  font-family:Consolas, monospace;
  font-size:12px;
  color:#555;
  background:#fafafa;
  border:1px solid #eee;
  border-radius:8px;
  padding:10px;
  white-space:pre-wrap;
}
.chk {
  display:flex;
  align-items:center;
  gap:8px;
  margin-bottom:8px;
}
.tip {
  background:#fff7e6;
  color:#8a5b00;
  border:1px solid #ffe0a3;
  border-radius:8px;
  padding:10px;
  font-size:12px;
  line-height:1.6;
}
.mode {
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:8px;
}
.mode button {
  background:#eee;
  color:#333;
}
.mode button.active {
  background:#1f5eff;
  color:white;
}
</style>
</head>
<body>
<div class="header">
  <h1 style="margin:0;">骨架编辑器（贝塞尔控制 + 邻接约束版）</h1>
  <div style="margin-top:8px;color:#ddd;">
    关键点像字体编辑器中的锚点；控制柄控制曲率；拖动锚点时，同一笔画与邻接骨架会协调联动。
  </div>
</div>

<div class="wrap">
  <div class="panel">
    <div class="row">
      <div class="label">字形列表</div>
      <div id="glyphList" class="left-list"></div>
    </div>
  </div>

  <div class="panel">
    <div class="topbar">
      <button id="saveBtn">保存当前字形</button>
      <button id="exportSvgBtn" class="secondary">导出 SVG</button>
      <button id="exportPngBtn" class="secondary">导出 PNG</button>
      <button id="autoSmoothBtn" class="secondary">自动顺滑控制柄</button>
      <button id="resetBtn" class="secondary">恢复当前版本</button>
    </div>

    <div class="row">
      <span class="small">版本：</span>
      <select id="variantSelect"></select>
    </div>

    <svg id="mainSvg" viewBox="0 0 1000 1000" xmlns="http://www.w3.org/2000/svg">
      <g id="outlineLayer" opacity="0.16"></g>
      <g id="rebuildLayer"></g>
      <g id="bezierLayer"></g>
      <g id="handleLineLayer"></g>
      <g id="handleLayer"></g>
    </svg>

    <div class="note" style="margin-top:12px;">
      操作方式：<br>
      1. 拖动圆形锚点：改变中心线主结构，并带动相邻骨架协调变化；<br>
      2. 拖动小方块控制柄：调整局部曲率，自动保持另一侧控制柄方向连续；<br>
      3. 选中锚点后，右侧可以调整局部笔画宽度；<br>
      4. 保存后可导出 SVG / PNG。
    </div>
  </div>

  <div class="panel">
    <div class="row">
      <div class="label">编辑模式</div>
      <div class="mode">
        <button id="modeAnchor" class="active">锚点模式</button>
        <button id="modeHandle">控制柄模式</button>
      </div>
      <div class="small">也可以直接拖动控制柄，小方块会自动进入控制柄编辑。</div>
    </div>

    <div class="row">
      <div class="label">显示控制</div>
      <label class="chk"><input type="checkbox" id="showOutline" checked>显示原轮廓（灰）</label>
      <label class="chk"><input type="checkbox" id="showRebuild" checked>显示编辑后轮廓（米色）</label>
      <label class="chk"><input type="checkbox" id="showBezier" checked>显示贝塞尔骨架（红）</label>
      <label class="chk"><input type="checkbox" id="showHandles" checked>显示锚点 / 控制柄</label>
    </div>

    <div class="row">
      <div class="label">当前锚点宽度</div>
      <input type="range" id="widthSlider" min="1" max="120" step="0.5" value="10">
      <div id="widthVal" class="small">10</div>
    </div>

    <div class="row">
      <div class="label">邻接联动强度</div>
      <input type="range" id="constraintSlider" min="0" max="1" step="0.05" value="0.55">
      <div id="constraintVal" class="small">0.55</div>
    </div>

    <div class="row">
      <div class="label">当前选中</div>
      <div id="selectedInfo" class="infobox">未选中任何点</div>
    </div>

    <div class="row">
      <div class="label">状态</div>
      <div id="statusBox" class="infobox">等待加载...</div>
    </div>

    <div class="row">
      <div class="tip">
        这个版本不是简单高斯拖动。<br>
        它使用：<br>
        1. 贝塞尔锚点与控制柄；<br>
        2. 同段拓扑邻接约束；<br>
        3. 端点邻接约束；<br>
        4. C1 方向连续控制柄。
      </div>
    </div>
  </div>
</div>

<script>
const JOB_ID = "__JOB_ID__";

let manifestData = null;
let currentCode = null;
let currentVariant = null;

let editData = null;
let originalEditData = null;
let rawData = null;

let selected = null;
// selected = {type:'anchor', seg, idx} or {type:'handle', seg, idx, side:'in'/'out'}

let dragging = false;
let dragStart = null;
let dragBase = null;
let editMode = "anchor";

const glyphList = document.getElementById("glyphList");
const variantSelect = document.getElementById("variantSelect");
const mainSvg = document.getElementById("mainSvg");
const outlineLayer = document.getElementById("outlineLayer");
const rebuildLayer = document.getElementById("rebuildLayer");
const bezierLayer = document.getElementById("bezierLayer");
const handleLineLayer = document.getElementById("handleLineLayer");
const handleLayer = document.getElementById("handleLayer");

const widthSlider = document.getElementById("widthSlider");
const widthVal = document.getElementById("widthVal");
const constraintSlider = document.getElementById("constraintSlider");
const constraintVal = document.getElementById("constraintVal");

const selectedInfo = document.getElementById("selectedInfo");
const statusBox = document.getElementById("statusBox");

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function setStatus(msg) {
  statusBox.textContent = msg;
}

function escHtml(s) {
  return String(s).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}

function dist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function setMode(m) {
  editMode = m;
  document.getElementById("modeAnchor").classList.toggle("active", m === "anchor");
  document.getElementById("modeHandle").classList.toggle("active", m === "handle");
}

document.getElementById("modeAnchor").onclick = () => setMode("anchor");
document.getElementById("modeHandle").onclick = () => setMode("handle");

constraintSlider.addEventListener("input", () => {
  constraintVal.textContent = Number(constraintSlider.value).toFixed(2);
});

// ------------------------------
// 数据简化：原始采样骨架 → 少量锚点
// ------------------------------
function pointLineDistance(p, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  if (Math.abs(dx) < 1e-9 && Math.abs(dy) < 1e-9) return dist(p, a);
  const t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx*dx + dy*dy);
  const tt = Math.max(0, Math.min(1, t));
  const q = {x: a.x + tt * dx, y: a.y + tt * dy};
  return dist(p, q);
}

function rdp(points, epsilon) {
  if (points.length <= 2) return points.slice();

  let dmax = 0;
  let index = 0;
  const end = points.length - 1;

  for (let i = 1; i < end; i++) {
    const d = pointLineDistance(points[i], points[0], points[end]);
    if (d > dmax) {
      index = i;
      dmax = d;
    }
  }

  if (dmax > epsilon) {
    const rec1 = rdp(points.slice(0, index + 1), epsilon);
    const rec2 = rdp(points.slice(index), epsilon);
    return rec1.slice(0, -1).concat(rec2);
  } else {
    return [points[0], points[end]];
  }
}

function bboxOfPoints(points) {
  if (!points || points.length === 0) return {minX:0,minY:0,maxX:1000,maxY:1000,w:1000,h:1000};
  let minX = points[0].x, minY = points[0].y, maxX = points[0].x, maxY = points[0].y;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  return {minX,minY,maxX,maxY,w:maxX-minX,h:maxY-minY};
}

function evenSample(points, n) {
  if (points.length <= n) return points.slice();
  const out = [];
  for (let i = 0; i < n; i++) {
    const idx = Math.round(i * (points.length - 1) / (n - 1));
    out.push(points[idx]);
  }
  return out;
}

function simplifyToAnchors(points) {
  if (!points || points.length <= 2) return deepClone(points || []);

  const bb = bboxOfPoints(points);
  const diag = Math.max(1, Math.hypot(bb.w, bb.h));
  const epsilon = Math.max(8, diag * 0.025);

  let simp = rdp(points, epsilon);

  if (simp.length < 4 && points.length >= 4) {
    simp = evenSample(points, Math.min(5, points.length));
  }

  if (simp.length > 8) {
    simp = evenSample(simp, 8);
  }

  return simp.map(p => ({x:p.x, y:p.y, w:p.w || 8}));
}

function makeBezierDataFromSkeleton(data) {
  const out = {
    code: data.code,
    variant: data.variant,
    viewBox: data.viewBox || [0,0,1000,1000],
    segments: []
  };

  (data.segments || []).forEach(seg => {
    const anchors = simplifyToAnchors(seg.points || []);
    if (anchors.length < 2) return;

    const bseg = {
      id: seg.id,
      anchors: anchors.map(p => ({
        x: p.x,
        y: p.y,
        w: p.w || 8,
        hin: null,
        hout: null
      }))
    };

    autoHandlesForSegment(bseg);
    out.segments.push(bseg);
  });

  return out;
}

// ------------------------------
// 贝塞尔控制柄
// ------------------------------
function autoHandlesForSegment(seg) {
  const a = seg.anchors || [];
  const n = a.length;
  if (n < 2) return;

  for (let i = 0; i < n; i++) {
    const curr = a[i];
    const prev = a[Math.max(0, i - 1)];
    const next = a[Math.min(n - 1, i + 1)];

    let tx = next.x - prev.x;
    let ty = next.y - prev.y;
    let len = Math.hypot(tx, ty);
    if (len < 1e-6) {
      tx = 1;
      ty = 0;
      len = 1;
    }

    tx /= len;
    ty /= len;

    const dPrev = i > 0 ? dist(curr, a[i-1]) : dist(curr, next);
    const dNext = i < n - 1 ? dist(curr, a[i+1]) : dist(curr, prev);

    const scaleIn = Math.max(4, dPrev * 0.32);
    const scaleOut = Math.max(4, dNext * 0.32);

    curr.hin = {
      x: curr.x - tx * scaleIn,
      y: curr.y - ty * scaleIn
    };

    curr.hout = {
      x: curr.x + tx * scaleOut,
      y: curr.y + ty * scaleOut
    };

    if (i === 0) {
      curr.hin = {x: curr.x, y: curr.y};
    }
    if (i === n - 1) {
      curr.hout = {x: curr.x, y: curr.y};
    }
  }
}

function smoothAllHandles() {
  if (!editData) return;
  editData.segments.forEach(seg => autoHandlesForSegment(seg));
  redrawAll();
  setStatus("已重新顺滑全部贝塞尔控制柄。");
}

function mirrorOppositeHandle(seg, idx, movedSide) {
  const a = seg.anchors[idx];
  if (!a) return;

  if (movedSide === "out" && a.hout && a.hin) {
    const vx = a.hout.x - a.x;
    const vy = a.hout.y - a.y;
    const oldLen = Math.max(1, Math.hypot(a.hin.x - a.x, a.hin.y - a.y));
    const newLen = Math.max(1, Math.hypot(vx, vy));
    const ratio = oldLen / newLen;
    a.hin.x = a.x - vx * ratio;
    a.hin.y = a.y - vy * ratio;
  }

  if (movedSide === "in" && a.hout && a.hin) {
    const vx = a.hin.x - a.x;
    const vy = a.hin.y - a.y;
    const oldLen = Math.max(1, Math.hypot(a.hout.x - a.x, a.hout.y - a.y));
    const newLen = Math.max(1, Math.hypot(vx, vy));
    const ratio = oldLen / newLen;
    a.hout.x = a.x - vx * ratio;
    a.hout.y = a.y - vy * ratio;
  }
}

// ------------------------------
// 贝塞尔采样
// ------------------------------
function cubic(p0, p1, p2, p3, t) {
  const mt = 1 - t;
  const a = mt * mt * mt;
  const b = 3 * mt * mt * t;
  const c = 3 * mt * t * t;
  const d = t * t * t;
  return {
    x: a*p0.x + b*p1.x + c*p2.x + d*p3.x,
    y: a*p0.y + b*p1.y + c*p2.y + d*p3.y
  };
}

function sampleBezierSegment(seg, perCurve=18) {
  const out = [];
  const a = seg.anchors || [];
  if (a.length < 2) return [];

  for (let i = 0; i < a.length - 1; i++) {
    const p0 = a[i];
    const p1 = a[i].hout || {x:a[i].x, y:a[i].y};
    const p2 = a[i+1].hin || {x:a[i+1].x, y:a[i+1].y};
    const p3 = a[i+1];

    for (let k = 0; k < perCurve; k++) {
      const t = k / perCurve;
      const q = cubic(p0, p1, p2, p3, t);
      const w = p0.w * (1 - t) + p3.w * t;
      out.push({x:q.x, y:q.y, w:w});
    }
  }

  const last = a[a.length - 1];
  out.push({x:last.x, y:last.y, w:last.w});
  return out;
}

function sampledExportData() {
  return {
    code: editData.code,
    variant: editData.variant,
    viewBox: editData.viewBox,
    segments: editData.segments.map(seg => ({
      id: seg.id,
      points: sampleBezierSegment(seg, 18)
    }))
  };
}

// ------------------------------
// 由采样点重建轮廓
// ------------------------------
function buildRibbonPieces(points) {
  if (!points || points.length === 0) return [];
  if (points.length === 1) {
    const p = points[0];
    return [`<circle cx="${p.x}" cy="${p.y}" r="${p.w}" fill="#C8A37A"/>`];
  }

  let left = [];
  let right = [];

  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const p0 = i > 0 ? points[i - 1] : points[i];
    const p1 = i < points.length - 1 ? points[i + 1] : points[i];

    let dx = p1.x - p0.x;
    let dy = p1.y - p0.y;
    let ln = Math.hypot(dx, dy);
    let nx = 0, ny = 1;
    if (ln > 1e-6) {
      nx = -dy / ln;
      ny = dx / ln;
    }

    const r = Math.max(1, p.w || 4);
    left.push([p.x + nx * r, p.y + ny * r]);
    right.push([p.x - nx * r, p.y - ny * r]);
  }

  const poly = left.concat(right.reverse());
  let d = "";
  if (poly.length >= 3) {
    d = "M " + poly.map(v => `${v[0].toFixed(2)} ${v[1].toFixed(2)}`).join(" L ") + " Z";
  }

  const out = [];
  if (d) out.push(`<path d="${d}" fill="#C8A37A" stroke="none"/>`);
  out.push(`<circle cx="${points[0].x}" cy="${points[0].y}" r="${Math.max(1, points[0].w)}" fill="#C8A37A"/>`);
  out.push(`<circle cx="${points[points.length-1].x}" cy="${points[points.length-1].y}" r="${Math.max(1, points[points.length-1].w)}" fill="#C8A37A"/>`);
  return out;
}

// ------------------------------
// 邻接骨架约束
// ------------------------------
function allAnchorsOf(data) {
  const arr = [];
  data.segments.forEach((seg, si) => {
    seg.anchors.forEach((a, ai) => {
      arr.push({seg:si, idx:ai, anchor:a});
    });
  });
  return arr;
}

function moveAnchorWithHandles(anchor, dx, dy, weight) {
  anchor.x += dx * weight;
  anchor.y += dy * weight;
  if (anchor.hin) {
    anchor.hin.x += dx * weight;
    anchor.hin.y += dy * weight;
  }
  if (anchor.hout) {
    anchor.hout.x += dx * weight;
    anchor.hout.y += dy * weight;
  }
}

function applyBezierAdjacentConstraint(base, selSeg, selIdx, dx, dy) {
  const out = deepClone(base);
  const strength = Number(constraintSlider.value || 0.55);

  const refBase = base.segments[selSeg].anchors[selIdx];

  // 1. 同段拓扑联动：不是高斯空间扩散，而是按锚点邻接层级传播
  out.segments.forEach((seg, si) => {
    const baseSeg = base.segments[si];

    seg.anchors.forEach((a, ai) => {
      let w = 0;

      if (si === selSeg) {
        const topo = Math.abs(ai - selIdx);

        if (topo === 0) w = 1.0;
        else if (topo === 1) w = 0.55 * strength;
        else if (topo === 2) w = 0.28 * strength;
        else if (topo === 3) w = 0.12 * strength;
        else w = 0.04 * strength;
      } else {
        // 2. 不同段之间，只在端点/空间邻接处弱联动
        const ba = baseSeg.anchors[ai];
        const d = Math.hypot(ba.x - refBase.x, ba.y - refBase.y);

        // 这里不是全局高斯，而是“邻接阈值约束”
        // 只有离选中锚点较近的其它骨架才参与
        const threshold = 140;
        if (d < threshold) {
          w = (1 - d / threshold) * 0.22 * strength;
        }
      }

      // 端点保护：避免整体飞掉
      if (ai === 0 || ai === seg.anchors.length - 1) {
        if (!(si === selSeg && ai === selIdx)) {
          w *= 0.72;
        }
      }

      if (w > 0) {
        moveAnchorWithHandles(a, dx, dy, w);
      }
    });
  });

  // 3. 对选中点两侧控制柄做连续性修正
  const movedSeg = out.segments[selSeg];
  if (movedSeg) {
    repairLocalTangents(movedSeg, selIdx);
  }

  return out;
}

function repairLocalTangents(seg, idx) {
  const a = seg.anchors;
  if (!a || a.length < 2) return;

  const curr = a[idx];
  if (!curr) return;

  // 根据前后锚点重新约束控制柄方向，但保留当前柄长度
  const prev = a[Math.max(0, idx - 1)];
  const next = a[Math.min(a.length - 1, idx + 1)];

  let tx = next.x - prev.x;
  let ty = next.y - prev.y;
  let len = Math.hypot(tx, ty);
  if (len < 1e-6) return;

  tx /= len;
  ty /= len;

  if (idx > 0 && curr.hin) {
    const oldLen = Math.max(4, Math.hypot(curr.hin.x - curr.x, curr.hin.y - curr.y));
    curr.hin.x = curr.x - tx * oldLen;
    curr.hin.y = curr.y - ty * oldLen;
  }

  if (idx < a.length - 1 && curr.hout) {
    const oldLen = Math.max(4, Math.hypot(curr.hout.x - curr.x, curr.hout.y - curr.y));
    curr.hout.x = curr.x + tx * oldLen;
    curr.hout.y = curr.y + ty * oldLen;
  }
}

function applyWidthConstraint(newWidth) {
  if (!selected || selected.type !== "anchor" || !editData) return;

  const seg = editData.segments[selected.seg];
  const a = seg.anchors[selected.idx];
  const delta = Number(newWidth) - a.w;
  const strength = Number(constraintSlider.value || 0.55);

  seg.anchors.forEach((p, i) => {
    const topo = Math.abs(i - selected.idx);
    let w = 0;

    if (topo === 0) w = 1.0;
    else if (topo === 1) w = 0.50 * strength;
    else if (topo === 2) w = 0.22 * strength;
    else w = 0.06 * strength;

    p.w = Math.max(1, p.w + delta * w);
  });

  // 其它邻接段宽度弱跟随
  editData.segments.forEach((otherSeg, si) => {
    if (si === selected.seg) return;
    otherSeg.anchors.forEach(op => {
      const d = Math.hypot(op.x - a.x, op.y - a.y);
      const threshold = 120;
      if (d < threshold) {
        const w = (1 - d / threshold) * 0.10 * strength;
        op.w = Math.max(1, op.w + delta * w);
      }
    });
  });
}

// ------------------------------
// 绘制
// ------------------------------
function bezierPathD(seg) {
  const a = seg.anchors || [];
  if (a.length < 2) return "";

  let d = `M ${a[0].x.toFixed(2)} ${a[0].y.toFixed(2)}`;

  for (let i = 0; i < a.length - 1; i++) {
    const p0 = a[i];
    const p1 = p0.hout || p0;
    const p2 = a[i+1].hin || a[i+1];
    const p3 = a[i+1];

    d += ` C ${p1.x.toFixed(2)} ${p1.y.toFixed(2)}, ${p2.x.toFixed(2)} ${p2.y.toFixed(2)}, ${p3.x.toFixed(2)} ${p3.y.toFixed(2)}`;
  }

  return d;
}

function redrawAll() {
  if (!editData) return;

  const showOutline = document.getElementById("showOutline").checked;
  const showRebuild = document.getElementById("showRebuild").checked;
  const showBezier = document.getElementById("showBezier").checked;
  const showHandles = document.getElementById("showHandles").checked;

  outlineLayer.style.display = showOutline ? "" : "none";

  rebuildLayer.innerHTML = "";
  bezierLayer.innerHTML = "";
  handleLineLayer.innerHTML = "";
  handleLayer.innerHTML = "";

  if (showRebuild) {
    const pieces = [];
    editData.segments.forEach(seg => {
      const pts = sampleBezierSegment(seg, 18);
      pieces.push(...buildRibbonPieces(pts));
    });
    rebuildLayer.innerHTML = pieces.join("\\n");
  }

  if (showBezier) {
    editData.segments.forEach((seg, si) => {
      const d = bezierPathD(seg);
      if (!d) return;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "#ff4d4f");
      path.setAttribute("stroke-width", "2");
      bezierLayer.appendChild(path);
    });
  }

  if (showHandles) {
    editData.segments.forEach((seg, si) => {
      seg.anchors.forEach((a, ai) => {
        // 控制柄线
        if (a.hin && ai > 0) {
          const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
          line.setAttribute("x1", a.x);
          line.setAttribute("y1", a.y);
          line.setAttribute("x2", a.hin.x);
          line.setAttribute("y2", a.hin.y);
          line.setAttribute("stroke", "#999");
          line.setAttribute("stroke-width", "1");
          line.setAttribute("stroke-dasharray", "3 3");
          handleLineLayer.appendChild(line);
        }
        if (a.hout && ai < seg.anchors.length - 1) {
          const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
          line.setAttribute("x1", a.x);
          line.setAttribute("y1", a.y);
          line.setAttribute("x2", a.hout.x);
          line.setAttribute("y2", a.hout.y);
          line.setAttribute("stroke", "#999");
          line.setAttribute("stroke-width", "1");
          line.setAttribute("stroke-dasharray", "3 3");
          handleLineLayer.appendChild(line);
        }

        // in handle
        if (a.hin && ai > 0) {
          drawHandleBox(si, ai, "in", a.hin.x, a.hin.y);
        }

        // out handle
        if (a.hout && ai < seg.anchors.length - 1) {
          drawHandleBox(si, ai, "out", a.hout.x, a.hout.y);
        }

        // anchor
        drawAnchor(si, ai, a.x, a.y);
      });
    });
  }
}

function drawAnchor(si, ai, x, y) {
  const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  const active = selected && selected.type === "anchor" && selected.seg === si && selected.idx === ai;
  c.setAttribute("cx", x);
  c.setAttribute("cy", y);
  c.setAttribute("r", active ? 7 : 5.5);
  c.setAttribute("fill", "#fff");
  c.setAttribute("stroke", active ? "#1f5eff" : "#ff4d4f");
  c.setAttribute("stroke-width", active ? "2.5" : "1.5");
  c.style.cursor = "move";

  c.addEventListener("pointerdown", ev => {
    ev.preventDefault();
    ev.stopPropagation();

    selected = {type:"anchor", seg:si, idx:ai};
    dragging = true;
    dragStart = getMouseSvgPoint(ev);
    dragBase = deepClone(editData);
    setMode("anchor");

    const a = editData.segments[si].anchors[ai];
    widthSlider.value = a.w;
    widthVal.textContent = Number(a.w).toFixed(1);

    updateSelectedInfo();
    redrawAll();
  });

  handleLayer.appendChild(c);
}

function drawHandleBox(si, ai, side, x, y) {
  const r = 4.5;
  const active = selected && selected.type === "handle" && selected.seg === si && selected.idx === ai && selected.side === side;

  const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  rect.setAttribute("x", x - r);
  rect.setAttribute("y", y - r);
  rect.setAttribute("width", r * 2);
  rect.setAttribute("height", r * 2);
  rect.setAttribute("fill", active ? "#1f5eff" : "#fff");
  rect.setAttribute("stroke", "#555");
  rect.setAttribute("stroke-width", "1.2");
  rect.style.cursor = "crosshair";

  rect.addEventListener("pointerdown", ev => {
    ev.preventDefault();
    ev.stopPropagation();

    selected = {type:"handle", seg:si, idx:ai, side};
    dragging = true;
    dragStart = getMouseSvgPoint(ev);
    dragBase = deepClone(editData);
    setMode("handle");

    updateSelectedInfo();
    redrawAll();
  });

  handleLayer.appendChild(rect);
}

// ------------------------------
// 交互
// ------------------------------
function getMouseSvgPoint(evt) {
  const pt = mainSvg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(mainSvg.getScreenCTM().inverse());
}

mainSvg.addEventListener("pointermove", evt => {
  if (!dragging || !selected || !dragBase) return;

  const now = getMouseSvgPoint(evt);
  const dx = now.x - dragStart.x;
  const dy = now.y - dragStart.y;

  if (selected.type === "anchor") {
    editData = applyBezierAdjacentConstraint(dragBase, selected.seg, selected.idx, dx, dy);
  }

  if (selected.type === "handle") {
    editData = deepClone(dragBase);
    const a = editData.segments[selected.seg].anchors[selected.idx];

    if (selected.side === "in") {
      a.hin.x += dx;
      a.hin.y += dy;
      mirrorOppositeHandle(editData.segments[selected.seg], selected.idx, "in");
    } else {
      a.hout.x += dx;
      a.hout.y += dy;
      mirrorOppositeHandle(editData.segments[selected.seg], selected.idx, "out");
    }
  }

  updateSelectedInfo();
  redrawAll();
});

window.addEventListener("pointerup", () => {
  dragging = false;
  dragStart = null;
  dragBase = null;
});

widthSlider.addEventListener("input", () => {
  widthVal.textContent = Number(widthSlider.value).toFixed(1);
  applyWidthConstraint(Number(widthSlider.value));
  updateSelectedInfo();
  redrawAll();
});

document.getElementById("showOutline").addEventListener("change", redrawAll);
document.getElementById("showRebuild").addEventListener("change", redrawAll);
document.getElementById("showBezier").addEventListener("change", redrawAll);
document.getElementById("showHandles").addEventListener("change", redrawAll);

function updateSelectedInfo() {
  if (!selected || !editData) {
    selectedInfo.textContent = "未选中任何点";
    return;
  }

  if (selected.type === "anchor") {
    const a = editData.segments[selected.seg].anchors[selected.idx];
    selectedInfo.textContent =
`类型: 锚点
字符: ${currentCode}
版本: ${currentVariant}
段: ${selected.seg}
点: ${selected.idx}
x: ${a.x.toFixed(2)}
y: ${a.y.toFixed(2)}
width: ${a.w.toFixed(2)}`;
  } else {
    const a = editData.segments[selected.seg].anchors[selected.idx];
    const h = selected.side === "in" ? a.hin : a.hout;
    selectedInfo.textContent =
`类型: 控制柄 ${selected.side}
字符: ${currentCode}
版本: ${currentVariant}
段: ${selected.seg}
锚点: ${selected.idx}
x: ${h.x.toFixed(2)}
y: ${h.y.toFixed(2)}`;
  }
}

// ------------------------------
// 数据加载
// ------------------------------
async function loadManifest() {
  const res = await fetch(`/skeleton_manifest/${JOB_ID}`);
  manifestData = await res.json();

  renderGlyphList();

  if (manifestData.codes && manifestData.codes.length > 0) {
    currentCode = manifestData.codes[0].code;
    renderGlyphList();
    renderVariants();
    await loadCurrentSkeleton();
  } else {
    setStatus("没有检测到可编辑的骨架数据。");
  }
}

function renderGlyphList() {
  glyphList.innerHTML = "";
  (manifestData.codes || []).forEach(item => {
    const btn = document.createElement("button");
    btn.className = "item-btn" + (item.code === currentCode ? " active" : "");
    btn.innerHTML = `<div style="font-size:24px;margin-bottom:6px;">${escHtml(item.char)}</div>
                     <div style="font-family:Consolas;font-size:12px;">${item.code}</div>
                     <div class="small">${item.variants.length} 个版本</div>`;
    btn.onclick = async () => {
      currentCode = item.code;
      renderGlyphList();
      renderVariants();
      await loadCurrentSkeleton();
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
  await loadCurrentSkeleton();
});

async function loadCurrentSkeleton() {
  if (!currentCode || !currentVariant) return;

  setStatus(`正在加载 ${currentCode} / ${currentVariant} ...`);

  const r1 = await fetch(`/skeleton_json/${JOB_ID}/${currentCode}/${currentVariant}`);
  rawData = await r1.json();

  editData = makeBezierDataFromSkeleton(rawData);
  originalEditData = deepClone(editData);

  const vb = editData.viewBox || [0,0,1000,1000];
  mainSvg.setAttribute("viewBox", vb.join(" "));

  const r2 = await fetch(`/raw_svg_variant/${JOB_ID}/${currentCode}/${currentVariant}`);
  const rawSvg = await r2.text();
  outlineLayer.innerHTML = rawSvg;

  selected = null;
  updateSelectedInfo();
  redrawAll();

  setStatus(`已加载：${currentCode} / ${currentVariant}；贝塞尔控制已启用。`);
}

// ------------------------------
// 保存 / 导出
// ------------------------------
async function saveCurrent() {
  if (!editData) return null;

  setStatus("正在保存当前编辑结果...");

  const payload = sampledExportData();

  const res = await fetch(`/save_skeleton_edit/${JOB_ID}/${currentCode}/${currentVariant}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      viewBox: payload.viewBox,
      segments: payload.segments
    })
  });

  const data = await res.json();

  if (data.ok) {
    setStatus("保存成功。");
    return data;
  } else {
    setStatus("保存失败：" + JSON.stringify(data));
    return null;
  }
}

document.getElementById("saveBtn").addEventListener("click", async () => {
  await saveCurrent();
});

document.getElementById("exportSvgBtn").addEventListener("click", async () => {
  const ret = await saveCurrent();
  if (ret && ret.svg_url) window.open(ret.svg_url, "_blank");
});

document.getElementById("exportPngBtn").addEventListener("click", async () => {
  const ret = await saveCurrent();
  if (ret && ret.png_url) window.open(ret.png_url, "_blank");
});

document.getElementById("resetBtn").addEventListener("click", () => {
  if (!originalEditData) return;
  editData = deepClone(originalEditData);
  selected = null;
  updateSelectedInfo();
  redrawAll();
  setStatus("已恢复当前版本。");
});

document.getElementById("autoSmoothBtn").addEventListener("click", () => {
  smoothAllHandles();
});

loadManifest().catch(err => {
  console.error(err);
  setStatus("加载失败：" + err);
});
</script>
</body>
</html>
""".replace("__JOB_ID__", job_id)

    html_path.write_text(html, encoding="utf-8")

# =========================================================
# Override: make_skeleton_editor_html
# Bezier Control + Topological Adjacent Constraint Editor
# =========================================================
def make_skeleton_editor_html(job_dir: Path, manifest: dict):
    html_path = job_dir / "skeleton_editor.html"
    job_id = job_dir.name

    html = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>骨架编辑器（贝塞尔控制版）</title>
<style>
body {
  margin:0;
  font-family:Arial, "Microsoft YaHei", sans-serif;
  background:#f5f5f5;
  color:#222;
}
.header {
  background:#111;
  color:#fff;
  padding:18px 26px;
}
.wrap {
  display:grid;
  grid-template-columns:280px 1fr 330px;
  gap:18px;
  padding:18px;
}
.panel {
  background:#fff;
  border-radius:14px;
  box-shadow:0 1px 8px rgba(0,0,0,.08);
  padding:16px;
}
.left-list {
  max-height:720px;
  overflow-y:auto;
}
.item-btn {
  width:100%;
  text-align:left;
  background:#fafafa;
  border:1px solid #ddd;
  border-radius:8px;
  padding:10px;
  margin-bottom:8px;
  cursor:pointer;
}
.item-btn.active {
  background:#1f5eff;
  color:#fff;
  border-color:#1f5eff;
}
select, input[type=range] {
  width:100%;
}
.topbar {
  display:flex;
  gap:10px;
  flex-wrap:wrap;
  margin-bottom:10px;
}
button {
  background:#1f5eff;
  color:#fff;
  border:0;
  border-radius:8px;
  padding:10px 14px;
  cursor:pointer;
}
button.secondary {
  background:#666;
}
button.warn {
  background:#8a5b00;
}
.note {
  color:#666;
  line-height:1.7;
  font-size:13px;
}
.row {
  margin-bottom:12px;
}
.label {
  font-weight:bold;
  margin-bottom:6px;
}
#mainSvg {
  width:100%;
  height:740px;
  border:1px solid #e5e5e5;
  border-radius:12px;
  background:#ffffff;
}
.small {
  font-size:12px;
  color:#666;
}
.infobox {
  font-family:Consolas, monospace;
  font-size:12px;
  color:#555;
  background:#fafafa;
  border:1px solid #eee;
  border-radius:8px;
  padding:10px;
  white-space:pre-wrap;
}
.chk {
  display:flex;
  align-items:center;
  gap:8px;
  margin-bottom:8px;
}
.tip {
  background:#fff7e6;
  color:#8a5b00;
  border:1px solid #ffe0a3;
  border-radius:8px;
  padding:10px;
  font-size:12px;
  line-height:1.6;
}
.mode {
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:8px;
}
.mode button {
  background:#eee;
  color:#333;
}
.mode button.active {
  background:#1f5eff;
  color:white;
}
</style>
</head>
<body>
<div class="header">
  <h1 style="margin:0;">骨架编辑器（贝塞尔控制 + 邻接约束版）</h1>
  <div style="margin-top:8px;color:#ddd;">
    关键点像字体编辑器中的锚点；控制柄控制曲率；拖动锚点时，同一笔画与邻接骨架会协调联动。
  </div>
</div>

<div class="wrap">
  <div class="panel">
    <div class="row">
      <div class="label">字形列表</div>
      <div id="glyphList" class="left-list"></div>
    </div>
  </div>

  <div class="panel">
    <div class="topbar">
      <button id="saveBtn">保存当前字形</button>
      <button id="exportSvgBtn" class="secondary">导出 SVG</button>
      <button id="exportPngBtn" class="secondary">导出 PNG</button>
      <button id="autoSmoothBtn" class="secondary">自动顺滑控制柄</button>
      <button id="resetBtn" class="secondary">恢复当前版本</button>
    </div>

    <div class="row">
      <span class="small">版本：</span>
      <select id="variantSelect"></select>
    </div>

    <svg id="mainSvg" viewBox="0 0 1000 1000" xmlns="http://www.w3.org/2000/svg">
      <g id="outlineLayer" opacity="0.16"></g>
      <g id="rebuildLayer"></g>
      <g id="bezierLayer"></g>
      <g id="handleLineLayer"></g>
      <g id="handleLayer"></g>
    </svg>

    <div class="note" style="margin-top:12px;">
      操作方式：<br>
      1. 拖动圆形锚点：改变中心线主结构，并带动相邻骨架协调变化；<br>
      2. 拖动小方块控制柄：调整局部曲率，自动保持另一侧控制柄方向连续；<br>
      3. 选中锚点后，右侧可以调整局部笔画宽度；<br>
      4. 保存后可导出 SVG / PNG。
    </div>
  </div>

  <div class="panel">
    <div class="row">
      <div class="label">编辑模式</div>
      <div class="mode">
        <button id="modeAnchor" class="active">锚点模式</button>
        <button id="modeHandle">控制柄模式</button>
      </div>
      <div class="small">也可以直接拖动控制柄，小方块会自动进入控制柄编辑。</div>
    </div>

    <div class="row">
      <div class="label">显示控制</div>
      <label class="chk"><input type="checkbox" id="showOutline" checked>显示原轮廓（灰）</label>
      <label class="chk"><input type="checkbox" id="showRebuild" checked>显示编辑后轮廓（米色）</label>
      <label class="chk"><input type="checkbox" id="showBezier" checked>显示贝塞尔骨架（红）</label>
      <label class="chk"><input type="checkbox" id="showHandles" checked>显示锚点 / 控制柄</label>
    </div>

    <div class="row">
      <div class="label">当前锚点宽度</div>
      <input type="range" id="widthSlider" min="1" max="120" step="0.5" value="10">
      <div id="widthVal" class="small">10</div>
    </div>

    <div class="row">
      <div class="label">邻接联动强度</div>
      <input type="range" id="constraintSlider" min="0" max="1" step="0.05" value="0.55">
      <div id="constraintVal" class="small">0.55</div>
    </div>

    <div class="row">
      <div class="label">当前选中</div>
      <div id="selectedInfo" class="infobox">未选中任何点</div>
    </div>

    <div class="row">
      <div class="label">状态</div>
      <div id="statusBox" class="infobox">等待加载...</div>
    </div>

    <div class="row">
      <div class="tip">
        这个版本不是简单高斯拖动。<br>
        它使用：<br>
        1. 贝塞尔锚点与控制柄；<br>
        2. 同段拓扑邻接约束；<br>
        3. 端点邻接约束；<br>
        4. C1 方向连续控制柄。
      </div>
    </div>
  </div>
</div>

<script>
const JOB_ID = "__JOB_ID__";

let manifestData = null;
let currentCode = null;
let currentVariant = null;

let editData = null;
let originalEditData = null;
let rawData = null;

let selected = null;
// selected = {type:'anchor', seg, idx} or {type:'handle', seg, idx, side:'in'/'out'}

let dragging = false;
let dragStart = null;
let dragBase = null;
let editMode = "anchor";

const glyphList = document.getElementById("glyphList");
const variantSelect = document.getElementById("variantSelect");
const mainSvg = document.getElementById("mainSvg");
const outlineLayer = document.getElementById("outlineLayer");
const rebuildLayer = document.getElementById("rebuildLayer");
const bezierLayer = document.getElementById("bezierLayer");
const handleLineLayer = document.getElementById("handleLineLayer");
const handleLayer = document.getElementById("handleLayer");

const widthSlider = document.getElementById("widthSlider");
const widthVal = document.getElementById("widthVal");
const constraintSlider = document.getElementById("constraintSlider");
const constraintVal = document.getElementById("constraintVal");

const selectedInfo = document.getElementById("selectedInfo");
const statusBox = document.getElementById("statusBox");

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function setStatus(msg) {
  statusBox.textContent = msg;
}

function escHtml(s) {
  return String(s).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}

function dist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function setMode(m) {
  editMode = m;
  document.getElementById("modeAnchor").classList.toggle("active", m === "anchor");
  document.getElementById("modeHandle").classList.toggle("active", m === "handle");
}

document.getElementById("modeAnchor").onclick = () => setMode("anchor");
document.getElementById("modeHandle").onclick = () => setMode("handle");

constraintSlider.addEventListener("input", () => {
  constraintVal.textContent = Number(constraintSlider.value).toFixed(2);
});

// ------------------------------
// 数据简化：原始采样骨架 → 少量锚点
// ------------------------------
function pointLineDistance(p, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  if (Math.abs(dx) < 1e-9 && Math.abs(dy) < 1e-9) return dist(p, a);
  const t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx*dx + dy*dy);
  const tt = Math.max(0, Math.min(1, t));
  const q = {x: a.x + tt * dx, y: a.y + tt * dy};
  return dist(p, q);
}

function rdp(points, epsilon) {
  if (points.length <= 2) return points.slice();

  let dmax = 0;
  let index = 0;
  const end = points.length - 1;

  for (let i = 1; i < end; i++) {
    const d = pointLineDistance(points[i], points[0], points[end]);
    if (d > dmax) {
      index = i;
      dmax = d;
    }
  }

  if (dmax > epsilon) {
    const rec1 = rdp(points.slice(0, index + 1), epsilon);
    const rec2 = rdp(points.slice(index), epsilon);
    return rec1.slice(0, -1).concat(rec2);
  } else {
    return [points[0], points[end]];
  }
}

function bboxOfPoints(points) {
  if (!points || points.length === 0) return {minX:0,minY:0,maxX:1000,maxY:1000,w:1000,h:1000};
  let minX = points[0].x, minY = points[0].y, maxX = points[0].x, maxY = points[0].y;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  return {minX,minY,maxX,maxY,w:maxX-minX,h:maxY-minY};
}

function evenSample(points, n) {
  if (points.length <= n) return points.slice();
  const out = [];
  for (let i = 0; i < n; i++) {
    const idx = Math.round(i * (points.length - 1) / (n - 1));
    out.push(points[idx]);
  }
  return out;
}

function simplifyToAnchors(points) {
  if (!points || points.length <= 2) return deepClone(points || []);

  const bb = bboxOfPoints(points);
  const diag = Math.max(1, Math.hypot(bb.w, bb.h));
  const epsilon = Math.max(8, diag * 0.025);

  let simp = rdp(points, epsilon);

  if (simp.length < 4 && points.length >= 4) {
    simp = evenSample(points, Math.min(5, points.length));
  }

  if (simp.length > 8) {
    simp = evenSample(simp, 8);
  }

  return simp.map(p => ({x:p.x, y:p.y, w:p.w || 8}));
}

function makeBezierDataFromSkeleton(data) {
  const out = {
    code: data.code,
    variant: data.variant,
    viewBox: data.viewBox || [0,0,1000,1000],
    segments: []
  };

  (data.segments || []).forEach(seg => {
    const anchors = simplifyToAnchors(seg.points || []);
    if (anchors.length < 2) return;

    const bseg = {
      id: seg.id,
      anchors: anchors.map(p => ({
        x: p.x,
        y: p.y,
        w: p.w || 8,
        hin: null,
        hout: null
      }))
    };

    autoHandlesForSegment(bseg);
    out.segments.push(bseg);
  });

  return out;
}

// ------------------------------
// 贝塞尔控制柄
// ------------------------------
function autoHandlesForSegment(seg) {
  const a = seg.anchors || [];
  const n = a.length;
  if (n < 2) return;

  for (let i = 0; i < n; i++) {
    const curr = a[i];
    const prev = a[Math.max(0, i - 1)];
    const next = a[Math.min(n - 1, i + 1)];

    let tx = next.x - prev.x;
    let ty = next.y - prev.y;
    let len = Math.hypot(tx, ty);
    if (len < 1e-6) {
      tx = 1;
      ty = 0;
      len = 1;
    }

    tx /= len;
    ty /= len;

    const dPrev = i > 0 ? dist(curr, a[i-1]) : dist(curr, next);
    const dNext = i < n - 1 ? dist(curr, a[i+1]) : dist(curr, prev);

    const scaleIn = Math.max(4, dPrev * 0.32);
    const scaleOut = Math.max(4, dNext * 0.32);

    curr.hin = {
      x: curr.x - tx * scaleIn,
      y: curr.y - ty * scaleIn
    };

    curr.hout = {
      x: curr.x + tx * scaleOut,
      y: curr.y + ty * scaleOut
    };

    if (i === 0) {
      curr.hin = {x: curr.x, y: curr.y};
    }
    if (i === n - 1) {
      curr.hout = {x: curr.x, y: curr.y};
    }
  }
}

function smoothAllHandles() {
  if (!editData) return;
  editData.segments.forEach(seg => autoHandlesForSegment(seg));
  redrawAll();
  setStatus("已重新顺滑全部贝塞尔控制柄。");
}

function mirrorOppositeHandle(seg, idx, movedSide) {
  const a = seg.anchors[idx];
  if (!a) return;

  if (movedSide === "out" && a.hout && a.hin) {
    const vx = a.hout.x - a.x;
    const vy = a.hout.y - a.y;
    const oldLen = Math.max(1, Math.hypot(a.hin.x - a.x, a.hin.y - a.y));
    const newLen = Math.max(1, Math.hypot(vx, vy));
    const ratio = oldLen / newLen;
    a.hin.x = a.x - vx * ratio;
    a.hin.y = a.y - vy * ratio;
  }

  if (movedSide === "in" && a.hout && a.hin) {
    const vx = a.hin.x - a.x;
    const vy = a.hin.y - a.y;
    const oldLen = Math.max(1, Math.hypot(a.hout.x - a.x, a.hout.y - a.y));
    const newLen = Math.max(1, Math.hypot(vx, vy));
    const ratio = oldLen / newLen;
    a.hout.x = a.x - vx * ratio;
    a.hout.y = a.y - vy * ratio;
  }
}

// ------------------------------
// 贝塞尔采样
// ------------------------------
function cubic(p0, p1, p2, p3, t) {
  const mt = 1 - t;
  const a = mt * mt * mt;
  const b = 3 * mt * mt * t;
  const c = 3 * mt * t * t;
  const d = t * t * t;
  return {
    x: a*p0.x + b*p1.x + c*p2.x + d*p3.x,
    y: a*p0.y + b*p1.y + c*p2.y + d*p3.y
  };
}

function sampleBezierSegment(seg, perCurve=18) {
  const out = [];
  const a = seg.anchors || [];
  if (a.length < 2) return [];

  for (let i = 0; i < a.length - 1; i++) {
    const p0 = a[i];
    const p1 = a[i].hout || {x:a[i].x, y:a[i].y};
    const p2 = a[i+1].hin || {x:a[i+1].x, y:a[i+1].y};
    const p3 = a[i+1];

    for (let k = 0; k < perCurve; k++) {
      const t = k / perCurve;
      const q = cubic(p0, p1, p2, p3, t);
      const w = p0.w * (1 - t) + p3.w * t;
      out.push({x:q.x, y:q.y, w:w});
    }
  }

  const last = a[a.length - 1];
  out.push({x:last.x, y:last.y, w:last.w});
  return out;
}

function sampledExportData() {
  return {
    code: editData.code,
    variant: editData.variant,
    viewBox: editData.viewBox,
    segments: editData.segments.map(seg => ({
      id: seg.id,
      points: sampleBezierSegment(seg, 18)
    }))
  };
}

// ------------------------------
// 由采样点重建轮廓
// ------------------------------
function buildRibbonPieces(points) {
  if (!points || points.length === 0) return [];
  if (points.length === 1) {
    const p = points[0];
    return [`<circle cx="${p.x}" cy="${p.y}" r="${p.w}" fill="#C8A37A"/>`];
  }

  let left = [];
  let right = [];

  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const p0 = i > 0 ? points[i - 1] : points[i];
    const p1 = i < points.length - 1 ? points[i + 1] : points[i];

    let dx = p1.x - p0.x;
    let dy = p1.y - p0.y;
    let ln = Math.hypot(dx, dy);
    let nx = 0, ny = 1;
    if (ln > 1e-6) {
      nx = -dy / ln;
      ny = dx / ln;
    }

    const r = Math.max(1, p.w || 4);
    left.push([p.x + nx * r, p.y + ny * r]);
    right.push([p.x - nx * r, p.y - ny * r]);
  }

  const poly = left.concat(right.reverse());
  let d = "";
  if (poly.length >= 3) {
    d = "M " + poly.map(v => `${v[0].toFixed(2)} ${v[1].toFixed(2)}`).join(" L ") + " Z";
  }

  const out = [];
  if (d) out.push(`<path d="${d}" fill="#C8A37A" stroke="none"/>`);
  out.push(`<circle cx="${points[0].x}" cy="${points[0].y}" r="${Math.max(1, points[0].w)}" fill="#C8A37A"/>`);
  out.push(`<circle cx="${points[points.length-1].x}" cy="${points[points.length-1].y}" r="${Math.max(1, points[points.length-1].w)}" fill="#C8A37A"/>`);
  return out;
}

// ------------------------------
// 邻接骨架约束
// ------------------------------
function allAnchorsOf(data) {
  const arr = [];
  data.segments.forEach((seg, si) => {
    seg.anchors.forEach((a, ai) => {
      arr.push({seg:si, idx:ai, anchor:a});
    });
  });
  return arr;
}

function moveAnchorWithHandles(anchor, dx, dy, weight) {
  anchor.x += dx * weight;
  anchor.y += dy * weight;
  if (anchor.hin) {
    anchor.hin.x += dx * weight;
    anchor.hin.y += dy * weight;
  }
  if (anchor.hout) {
    anchor.hout.x += dx * weight;
    anchor.hout.y += dy * weight;
  }
}

function applyBezierAdjacentConstraint(base, selSeg, selIdx, dx, dy) {
  const out = deepClone(base);
  const strength = Number(constraintSlider.value || 0.55);

  const refBase = base.segments[selSeg].anchors[selIdx];

  // 1. 同段拓扑联动：不是高斯空间扩散，而是按锚点邻接层级传播
  out.segments.forEach((seg, si) => {
    const baseSeg = base.segments[si];

    seg.anchors.forEach((a, ai) => {
      let w = 0;

      if (si === selSeg) {
        const topo = Math.abs(ai - selIdx);

        if (topo === 0) w = 1.0;
        else if (topo === 1) w = 0.55 * strength;
        else if (topo === 2) w = 0.28 * strength;
        else if (topo === 3) w = 0.12 * strength;
        else w = 0.04 * strength;
      } else {
        // 2. 不同段之间，只在端点/空间邻接处弱联动
        const ba = baseSeg.anchors[ai];
        const d = Math.hypot(ba.x - refBase.x, ba.y - refBase.y);

        // 这里不是全局高斯，而是“邻接阈值约束”
        // 只有离选中锚点较近的其它骨架才参与
        const threshold = 140;
        if (d < threshold) {
          w = (1 - d / threshold) * 0.22 * strength;
        }
      }

      // 端点保护：避免整体飞掉
      if (ai === 0 || ai === seg.anchors.length - 1) {
        if (!(si === selSeg && ai === selIdx)) {
          w *= 0.72;
        }
      }

      if (w > 0) {
        moveAnchorWithHandles(a, dx, dy, w);
      }
    });
  });

  // 3. 对选中点两侧控制柄做连续性修正
  const movedSeg = out.segments[selSeg];
  if (movedSeg) {
    repairLocalTangents(movedSeg, selIdx);
  }

  return out;
}

function repairLocalTangents(seg, idx) {
  const a = seg.anchors;
  if (!a || a.length < 2) return;

  const curr = a[idx];
  if (!curr) return;

  // 根据前后锚点重新约束控制柄方向，但保留当前柄长度
  const prev = a[Math.max(0, idx - 1)];
  const next = a[Math.min(a.length - 1, idx + 1)];

  let tx = next.x - prev.x;
  let ty = next.y - prev.y;
  let len = Math.hypot(tx, ty);
  if (len < 1e-6) return;

  tx /= len;
  ty /= len;

  if (idx > 0 && curr.hin) {
    const oldLen = Math.max(4, Math.hypot(curr.hin.x - curr.x, curr.hin.y - curr.y));
    curr.hin.x = curr.x - tx * oldLen;
    curr.hin.y = curr.y - ty * oldLen;
  }

  if (idx < a.length - 1 && curr.hout) {
    const oldLen = Math.max(4, Math.hypot(curr.hout.x - curr.x, curr.hout.y - curr.y));
    curr.hout.x = curr.x + tx * oldLen;
    curr.hout.y = curr.y + ty * oldLen;
  }
}

function applyWidthConstraint(newWidth) {
  if (!selected || selected.type !== "anchor" || !editData) return;

  const seg = editData.segments[selected.seg];
  const a = seg.anchors[selected.idx];
  const delta = Number(newWidth) - a.w;
  const strength = Number(constraintSlider.value || 0.55);

  seg.anchors.forEach((p, i) => {
    const topo = Math.abs(i - selected.idx);
    let w = 0;

    if (topo === 0) w = 1.0;
    else if (topo === 1) w = 0.50 * strength;
    else if (topo === 2) w = 0.22 * strength;
    else w = 0.06 * strength;

    p.w = Math.max(1, p.w + delta * w);
  });

  // 其它邻接段宽度弱跟随
  editData.segments.forEach((otherSeg, si) => {
    if (si === selected.seg) return;
    otherSeg.anchors.forEach(op => {
      const d = Math.hypot(op.x - a.x, op.y - a.y);
      const threshold = 120;
      if (d < threshold) {
        const w = (1 - d / threshold) * 0.10 * strength;
        op.w = Math.max(1, op.w + delta * w);
      }
    });
  });
}

// ------------------------------
// 绘制
// ------------------------------
function bezierPathD(seg) {
  const a = seg.anchors || [];
  if (a.length < 2) return "";

  let d = `M ${a[0].x.toFixed(2)} ${a[0].y.toFixed(2)}`;

  for (let i = 0; i < a.length - 1; i++) {
    const p0 = a[i];
    const p1 = p0.hout || p0;
    const p2 = a[i+1].hin || a[i+1];
    const p3 = a[i+1];

    d += ` C ${p1.x.toFixed(2)} ${p1.y.toFixed(2)}, ${p2.x.toFixed(2)} ${p2.y.toFixed(2)}, ${p3.x.toFixed(2)} ${p3.y.toFixed(2)}`;
  }

  return d;
}

function redrawAll() {
  if (!editData) return;

  const showOutline = document.getElementById("showOutline").checked;
  const showRebuild = document.getElementById("showRebuild").checked;
  const showBezier = document.getElementById("showBezier").checked;
  const showHandles = document.getElementById("showHandles").checked;

  outlineLayer.style.display = showOutline ? "" : "none";

  rebuildLayer.innerHTML = "";
  bezierLayer.innerHTML = "";
  handleLineLayer.innerHTML = "";
  handleLayer.innerHTML = "";

  if (showRebuild) {
    const pieces = [];
    editData.segments.forEach(seg => {
      const pts = sampleBezierSegment(seg, 18);
      pieces.push(...buildRibbonPieces(pts));
    });
    rebuildLayer.innerHTML = pieces.join("\\n");
  }

  if (showBezier) {
    editData.segments.forEach((seg, si) => {
      const d = bezierPathD(seg);
      if (!d) return;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "#ff4d4f");
      path.setAttribute("stroke-width", "2");
      bezierLayer.appendChild(path);
    });
  }

  if (showHandles) {
    editData.segments.forEach((seg, si) => {
      seg.anchors.forEach((a, ai) => {
        // 控制柄线
        if (a.hin && ai > 0) {
          const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
          line.setAttribute("x1", a.x);
          line.setAttribute("y1", a.y);
          line.setAttribute("x2", a.hin.x);
          line.setAttribute("y2", a.hin.y);
          line.setAttribute("stroke", "#999");
          line.setAttribute("stroke-width", "1");
          line.setAttribute("stroke-dasharray", "3 3");
          handleLineLayer.appendChild(line);
        }
        if (a.hout && ai < seg.anchors.length - 1) {
          const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
          line.setAttribute("x1", a.x);
          line.setAttribute("y1", a.y);
          line.setAttribute("x2", a.hout.x);
          line.setAttribute("y2", a.hout.y);
          line.setAttribute("stroke", "#999");
          line.setAttribute("stroke-width", "1");
          line.setAttribute("stroke-dasharray", "3 3");
          handleLineLayer.appendChild(line);
        }

        // in handle
        if (a.hin && ai > 0) {
          drawHandleBox(si, ai, "in", a.hin.x, a.hin.y);
        }

        // out handle
        if (a.hout && ai < seg.anchors.length - 1) {
          drawHandleBox(si, ai, "out", a.hout.x, a.hout.y);
        }

        // anchor
        drawAnchor(si, ai, a.x, a.y);
      });
    });
  }
}

function drawAnchor(si, ai, x, y) {
  const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  const active = selected && selected.type === "anchor" && selected.seg === si && selected.idx === ai;
  c.setAttribute("cx", x);
  c.setAttribute("cy", y);
  c.setAttribute("r", active ? 7 : 5.5);
  c.setAttribute("fill", "#fff");
  c.setAttribute("stroke", active ? "#1f5eff" : "#ff4d4f");
  c.setAttribute("stroke-width", active ? "2.5" : "1.5");
  c.style.cursor = "move";

  c.addEventListener("pointerdown", ev => {
    ev.preventDefault();
    ev.stopPropagation();

    selected = {type:"anchor", seg:si, idx:ai};
    dragging = true;
    dragStart = getMouseSvgPoint(ev);
    dragBase = deepClone(editData);
    setMode("anchor");

    const a = editData.segments[si].anchors[ai];
    widthSlider.value = a.w;
    widthVal.textContent = Number(a.w).toFixed(1);

    updateSelectedInfo();
    redrawAll();
  });

  handleLayer.appendChild(c);
}

function drawHandleBox(si, ai, side, x, y) {
  const r = 4.5;
  const active = selected && selected.type === "handle" && selected.seg === si && selected.idx === ai && selected.side === side;

  const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  rect.setAttribute("x", x - r);
  rect.setAttribute("y", y - r);
  rect.setAttribute("width", r * 2);
  rect.setAttribute("height", r * 2);
  rect.setAttribute("fill", active ? "#1f5eff" : "#fff");
  rect.setAttribute("stroke", "#555");
  rect.setAttribute("stroke-width", "1.2");
  rect.style.cursor = "crosshair";

  rect.addEventListener("pointerdown", ev => {
    ev.preventDefault();
    ev.stopPropagation();

    selected = {type:"handle", seg:si, idx:ai, side};
    dragging = true;
    dragStart = getMouseSvgPoint(ev);
    dragBase = deepClone(editData);
    setMode("handle");

    updateSelectedInfo();
    redrawAll();
  });

  handleLayer.appendChild(rect);
}

// ------------------------------
// 交互
// ------------------------------
function getMouseSvgPoint(evt) {
  const pt = mainSvg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(mainSvg.getScreenCTM().inverse());
}

mainSvg.addEventListener("pointermove", evt => {
  if (!dragging || !selected || !dragBase) return;

  const now = getMouseSvgPoint(evt);
  const dx = now.x - dragStart.x;
  const dy = now.y - dragStart.y;

  if (selected.type === "anchor") {
    editData = applyBezierAdjacentConstraint(dragBase, selected.seg, selected.idx, dx, dy);
  }

  if (selected.type === "handle") {
    editData = deepClone(dragBase);
    const a = editData.segments[selected.seg].anchors[selected.idx];

    if (selected.side === "in") {
      a.hin.x += dx;
      a.hin.y += dy;
      mirrorOppositeHandle(editData.segments[selected.seg], selected.idx, "in");
    } else {
      a.hout.x += dx;
      a.hout.y += dy;
      mirrorOppositeHandle(editData.segments[selected.seg], selected.idx, "out");
    }
  }

  updateSelectedInfo();
  redrawAll();
});

window.addEventListener("pointerup", () => {
  dragging = false;
  dragStart = null;
  dragBase = null;
});

widthSlider.addEventListener("input", () => {
  widthVal.textContent = Number(widthSlider.value).toFixed(1);
  applyWidthConstraint(Number(widthSlider.value));
  updateSelectedInfo();
  redrawAll();
});

document.getElementById("showOutline").addEventListener("change", redrawAll);
document.getElementById("showRebuild").addEventListener("change", redrawAll);
document.getElementById("showBezier").addEventListener("change", redrawAll);
document.getElementById("showHandles").addEventListener("change", redrawAll);

function updateSelectedInfo() {
  if (!selected || !editData) {
    selectedInfo.textContent = "未选中任何点";
    return;
  }

  if (selected.type === "anchor") {
    const a = editData.segments[selected.seg].anchors[selected.idx];
    selectedInfo.textContent =
`类型: 锚点
字符: ${currentCode}
版本: ${currentVariant}
段: ${selected.seg}
点: ${selected.idx}
x: ${a.x.toFixed(2)}
y: ${a.y.toFixed(2)}
width: ${a.w.toFixed(2)}`;
  } else {
    const a = editData.segments[selected.seg].anchors[selected.idx];
    const h = selected.side === "in" ? a.hin : a.hout;
    selectedInfo.textContent =
`类型: 控制柄 ${selected.side}
字符: ${currentCode}
版本: ${currentVariant}
段: ${selected.seg}
锚点: ${selected.idx}
x: ${h.x.toFixed(2)}
y: ${h.y.toFixed(2)}`;
  }
}

// ------------------------------
// 数据加载
// ------------------------------
async function loadManifest() {
  const res = await fetch(`/skeleton_manifest/${JOB_ID}`);
  manifestData = await res.json();

  renderGlyphList();

  if (manifestData.codes && manifestData.codes.length > 0) {
    currentCode = manifestData.codes[0].code;
    renderGlyphList();
    renderVariants();
    await loadCurrentSkeleton();
  } else {
    setStatus("没有检测到可编辑的骨架数据。");
  }
}

function renderGlyphList() {
  glyphList.innerHTML = "";
  (manifestData.codes || []).forEach(item => {
    const btn = document.createElement("button");
    btn.className = "item-btn" + (item.code === currentCode ? " active" : "");
    btn.innerHTML = `<div style="font-size:24px;margin-bottom:6px;">${escHtml(item.char)}</div>
                     <div style="font-family:Consolas;font-size:12px;">${item.code}</div>
                     <div class="small">${item.variants.length} 个版本</div>`;
    btn.onclick = async () => {
      currentCode = item.code;
      renderGlyphList();
      renderVariants();
      await loadCurrentSkeleton();
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
  await loadCurrentSkeleton();
});

async function loadCurrentSkeleton() {
  if (!currentCode || !currentVariant) return;

  setStatus(`正在加载 ${currentCode} / ${currentVariant} ...`);

  const r1 = await fetch(`/skeleton_json/${JOB_ID}/${currentCode}/${currentVariant}`);
  rawData = await r1.json();

  editData = makeBezierDataFromSkeleton(rawData);
  originalEditData = deepClone(editData);

  const vb = editData.viewBox || [0,0,1000,1000];
  mainSvg.setAttribute("viewBox", vb.join(" "));

  const r2 = await fetch(`/raw_svg_variant/${JOB_ID}/${currentCode}/${currentVariant}`);
  const rawSvg = await r2.text();
  outlineLayer.innerHTML = rawSvg;

  selected = null;
  updateSelectedInfo();
  redrawAll();

  setStatus(`已加载：${currentCode} / ${currentVariant}；贝塞尔控制已启用。`);
}

// ------------------------------
// 保存 / 导出
// ------------------------------
async function saveCurrent() {
  if (!editData) return null;

  setStatus("正在保存当前编辑结果...");

  const payload = sampledExportData();

  const res = await fetch(`/save_skeleton_edit/${JOB_ID}/${currentCode}/${currentVariant}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      viewBox: payload.viewBox,
      segments: payload.segments
    })
  });

  const data = await res.json();

  if (data.ok) {
    setStatus("保存成功。");
    return data;
  } else {
    setStatus("保存失败：" + JSON.stringify(data));
    return null;
  }
}

document.getElementById("saveBtn").addEventListener("click", async () => {
  await saveCurrent();
});

document.getElementById("exportSvgBtn").addEventListener("click", async () => {
  const ret = await saveCurrent();
  if (ret && ret.svg_url) window.open(ret.svg_url, "_blank");
});

document.getElementById("exportPngBtn").addEventListener("click", async () => {
  const ret = await saveCurrent();
  if (ret && ret.png_url) window.open(ret.png_url, "_blank");
});

document.getElementById("resetBtn").addEventListener("click", () => {
  if (!originalEditData) return;
  editData = deepClone(originalEditData);
  selected = null;
  updateSelectedInfo();
  redrawAll();
  setStatus("已恢复当前版本。");
});

document.getElementById("autoSmoothBtn").addEventListener("click", () => {
  smoothAllHandles();
});

loadManifest().catch(err => {
  console.error(err);
  setStatus("加载失败：" + err);
});
</script>
</body>
</html>
""".replace("__JOB_ID__", job_id)

    html_path.write_text(html, encoding="utf-8")

# =========================================================
# Override: make_skeleton_editor_html
# Outline Warp Editor
# 重点：拖动骨架控制点时，不再只变骨架重建轮廓，
# 而是直接变形原始 SVG path 的所有坐标点。
# =========================================================
def make_skeleton_editor_html(job_dir: Path, manifest: dict):
    html_path = job_dir / "skeleton_editor.html"
    job_id = job_dir.name

    html = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>骨架编辑器（整体轮廓变形版）</title>
<style>
body {
  margin:0;
  font-family:Arial, "Microsoft YaHei", sans-serif;
  background:#f5f5f5;
  color:#222;
}
.header {
  background:#111;
  color:#fff;
  padding:18px 26px;
}
.wrap {
  display:grid;
  grid-template-columns:280px 1fr 330px;
  gap:18px;
  padding:18px;
}
.panel {
  background:#fff;
  border-radius:14px;
  box-shadow:0 1px 8px rgba(0,0,0,.08);
  padding:16px;
}
.left-list {
  max-height:720px;
  overflow-y:auto;
}
.item-btn {
  width:100%;
  text-align:left;
  background:#fafafa;
  border:1px solid #ddd;
  border-radius:8px;
  padding:10px;
  margin-bottom:8px;
  cursor:pointer;
}
.item-btn.active {
  background:#1f5eff;
  color:#fff;
  border-color:#1f5eff;
}
select, input[type=range] {
  width:100%;
}
.topbar {
  display:flex;
  gap:10px;
  flex-wrap:wrap;
  margin-bottom:10px;
}
button {
  background:#1f5eff;
  color:#fff;
  border:0;
  border-radius:8px;
  padding:10px 14px;
  cursor:pointer;
}
button.secondary {
  background:#666;
}
button.warn {
  background:#8a5b00;
}
.note {
  color:#666;
  line-height:1.7;
  font-size:13px;
}
.row {
  margin-bottom:12px;
}
.label {
  font-weight:bold;
  margin-bottom:6px;
}
#mainSvg {
  width:100%;
  height:740px;
  border:1px solid #e5e5e5;
  border-radius:12px;
  background:#ffffff;
}
.small {
  font-size:12px;
  color:#666;
}
.infobox {
  font-family:Consolas, monospace;
  font-size:12px;
  color:#555;
  background:#fafafa;
  border:1px solid #eee;
  border-radius:8px;
  padding:10px;
  white-space:pre-wrap;
}
.chk {
  display:flex;
  align-items:center;
  gap:8px;
  margin-bottom:8px;
}
.tip {
  background:#fff7e6;
  color:#8a5b00;
  border:1px solid #ffe0a3;
  border-radius:8px;
  padding:10px;
  font-size:12px;
  line-height:1.6;
}
.mode {
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:8px;
}
.mode button {
  background:#eee;
  color:#333;
}
.mode button.active {
  background:#1f5eff;
  color:white;
}
</style>
</head>
<body>
<div class="header">
  <h1 style="margin:0;">骨架编辑器（整体轮廓变形版）</h1>
  <div style="margin-top:8px;color:#ddd;">
    拖动骨架关键点时，将直接变形原始字形完整轮廓，而不是只移动某一条边。
  </div>
</div>

<div class="wrap">
  <div class="panel">
    <div class="row">
      <div class="label">字形列表</div>
      <div id="glyphList" class="left-list"></div>
    </div>
  </div>

  <div class="panel">
    <div class="topbar">
      <button id="saveBtn">保存当前字形</button>
      <button id="exportSvgBtn" class="secondary">导出 SVG</button>
      <button id="exportPngBtn" class="secondary">导出 PNG</button>
      <button id="smoothBtn" class="secondary">平滑控制点</button>
      <button id="resetBtn" class="secondary">恢复当前版本</button>
    </div>

    <div class="row">
      <span class="small">版本：</span>
      <select id="variantSelect"></select>
    </div>

    <svg id="mainSvg" viewBox="0 0 1000 1000" xmlns="http://www.w3.org/2000/svg">
      <g id="originalLayer" opacity="0.14"></g>
      <g id="deformedLayer"></g>
      <g id="skeletonLayer"></g>
      <g id="handleLayer"></g>
    </svg>

    <div class="note" style="margin-top:12px;">
      操作方式：<br>
      1. 白色圆点是骨架控制点；<br>
      2. 拖动控制点时，整个原始字形轮廓会根据控制点场一起变形；<br>
      3. 调整右侧“整体联动强度”，可以控制整个字形跟随程度；<br>
      4. 保存后可导出 SVG / PNG。
    </div>
  </div>

  <div class="panel">
    <div class="row">
      <div class="label">显示控制</div>
      <label class="chk"><input type="checkbox" id="showOriginal" checked>显示原始轮廓（灰）</label>
      <label class="chk"><input type="checkbox" id="showDeformed" checked>显示变形后轮廓（米色）</label>
      <label class="chk"><input type="checkbox" id="showSkeleton" checked>显示骨架线（红）</label>
      <label class="chk"><input type="checkbox" id="showHandles" checked>显示控制点（白）</label>
    </div>

    <div class="row">
      <div class="label">整体联动强度</div>
      <input type="range" id="globalSlider" min="0" max="1" step="0.05" value="0.55">
      <div id="globalVal" class="small">0.55</div>
    </div>

    <div class="row">
      <div class="label">局部影响半径</div>
      <input type="range" id="radiusSlider" min="0.2" max="1.5" step="0.05" value="0.75">
      <div id="radiusVal" class="small">0.75</div>
    </div>

    <div class="row">
      <div class="label">当前选中</div>
      <div id="selectedInfo" class="infobox">未选中任何点</div>
    </div>

    <div class="row">
      <div class="label">状态</div>
      <div id="statusBox" class="infobox">等待加载...</div>
    </div>

    <div class="row">
      <div class="tip">
        这版的核心变化：<br>
        原来的米色轮廓不是重新“画一条边”，而是从原始 SVG path 坐标整体变形得到。<br>
        因此拖一个点时，整个字形会协调变化。
      </div>
    </div>
  </div>
</div>

<script>
const JOB_ID = "__JOB_ID__";

let manifestData = null;
let currentCode = null;
let currentVariant = null;

let skeletonData = null;
let controlData = null;
let originalControlData = null;

let rawSvgText = "";
let parsedSvg = null;

let selected = null;
let dragging = false;
let dragStart = null;
let dragBaseControls = null;

const glyphList = document.getElementById("glyphList");
const variantSelect = document.getElementById("variantSelect");
const mainSvg = document.getElementById("mainSvg");

const originalLayer = document.getElementById("originalLayer");
const deformedLayer = document.getElementById("deformedLayer");
const skeletonLayer = document.getElementById("skeletonLayer");
const handleLayer = document.getElementById("handleLayer");

const globalSlider = document.getElementById("globalSlider");
const globalVal = document.getElementById("globalVal");
const radiusSlider = document.getElementById("radiusSlider");
const radiusVal = document.getElementById("radiusVal");

const selectedInfo = document.getElementById("selectedInfo");
const statusBox = document.getElementById("statusBox");

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function setStatus(msg) {
  statusBox.textContent = msg;
}

function escHtml(s) {
  return String(s).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}

function dist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function bboxOfPoints(points) {
  if (!points || points.length === 0) return {minX:0,minY:0,maxX:1000,maxY:1000,w:1000,h:1000};
  let minX = points[0].x, minY = points[0].y, maxX = points[0].x, maxY = points[0].y;
  for (const p of points) {
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x);
    maxY = Math.max(maxY, p.y);
  }
  return {minX,minY,maxX,maxY,w:maxX-minX,h:maxY-minY};
}

function bboxOfControls(data) {
  let pts = [];
  (data.segments || []).forEach(seg => {
    (seg.points || []).forEach(p => pts.push(p));
  });
  return bboxOfPoints(pts);
}

// ------------------------------
// 简化骨架为控制点
// ------------------------------
function pointLineDistance(p, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  if (Math.abs(dx) < 1e-9 && Math.abs(dy) < 1e-9) return dist(p, a);
  const t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx*dx + dy*dy);
  const tt = Math.max(0, Math.min(1, t));
  const q = {x: a.x + tt * dx, y: a.y + tt * dy};
  return dist(p, q);
}

function rdp(points, epsilon) {
  if (points.length <= 2) return points.slice();

  let dmax = 0;
  let index = 0;
  const end = points.length - 1;

  for (let i = 1; i < end; i++) {
    const d = pointLineDistance(points[i], points[0], points[end]);
    if (d > dmax) {
      index = i;
      dmax = d;
    }
  }

  if (dmax > epsilon) {
    const rec1 = rdp(points.slice(0, index + 1), epsilon);
    const rec2 = rdp(points.slice(index), epsilon);
    return rec1.slice(0, -1).concat(rec2);
  } else {
    return [points[0], points[end]];
  }
}

function evenSample(points, n) {
  if (points.length <= n) return points.slice();
  const out = [];
  for (let i = 0; i < n; i++) {
    const idx = Math.round(i * (points.length - 1) / (n - 1));
    out.push(points[idx]);
  }
  return out;
}

function simplifyControls(data) {
  const out = deepClone(data);
  out.segments = (out.segments || []).map(seg => {
    const pts = seg.points || [];
    if (pts.length <= 2) return {...seg, points: pts};

    const bb = bboxOfPoints(pts);
    const diag = Math.max(1, Math.hypot(bb.w, bb.h));
    let simp = rdp(pts, Math.max(8, diag * 0.026));

    if (simp.length < 4 && pts.length >= 4) {
      simp = evenSample(pts, Math.min(5, pts.length));
    }

    if (simp.length > 9) {
      simp = evenSample(simp, 9);
    }

    return {
      ...seg,
      points: simp.map(p => ({x:p.x, y:p.y, w:p.w || 8}))
    };
  }).filter(seg => (seg.points || []).length >= 2);

  return out;
}

// ------------------------------
// 解析原 SVG path
// ------------------------------
function parseSvg(svgText) {
  const viewBoxMatch = svgText.match(/viewBox="([^"]+)"/);
  const viewBox = viewBoxMatch ? viewBoxMatch[1] : "0 0 1000 1000";

  const pathRegex = /<path[^>]*d="([^"]+)"[^>]*>/g;
  const paths = [];
  let m;

  while ((m = pathRegex.exec(svgText)) !== null) {
    const pathTag = m[0];
    const d = m[1];
    const fillMatch = pathTag.match(/fill="([^"]+)"/);
    const fill = fillMatch ? fillMatch[1] : "#C8A37A";

    const tokens = d.match(/[MLCQZmlcqz]|-?\\d*\\.?\\d+(?:e[-+]?\\d+)?/g) || [];
    const structured = [];

    tokens.forEach(tok => {
      if (/^[MLCQZmlcqz]$/.test(tok)) {
        structured.push({type:"cmd", value:tok});
      } else {
        structured.push({type:"num", value:Number(tok)});
      }
    });

    paths.push({
      tag: pathTag,
      d,
      fill,
      tokens: structured
    });
  }

  return {viewBox, paths};
}

function transformPathTokens(tokens, transformPoint) {
  let out = [];
  let coordBuffer = [];

  function flushCoordBuffer() {
    if (coordBuffer.length === 2) {
      const p = transformPoint({x:coordBuffer[0], y:coordBuffer[1]});
      out.push(p.x.toFixed(3));
      out.push(p.y.toFixed(3));
    } else {
      coordBuffer.forEach(v => out.push(Number(v).toFixed(3)));
    }
    coordBuffer = [];
  }

  tokens.forEach(t => {
    if (t.type === "cmd") {
      flushCoordBuffer();
      out.push(t.value);
    } else {
      coordBuffer.push(t.value);
      if (coordBuffer.length === 2) {
        flushCoordBuffer();
      }
    }
  });

  flushCoordBuffer();
  return out.join(" ");
}

// ------------------------------
// 控制点移动场：整个字形轮廓都参与变形
// ------------------------------
function makeMovedControls(base, selSeg, selIdx, dx, dy) {
  const out = deepClone(base);
  const globalStrength = Number(globalSlider.value || 0.55);

  out.segments.forEach((seg, si) => {
    const baseSeg = base.segments[si];

    seg.points.forEach((p, pi) => {
      const bp = baseSeg.points[pi];
      let w = 0;

      if (si === selSeg) {
        const topo = Math.abs(pi - selIdx);
        if (topo === 0) w = 1.0;
        else if (topo === 1) w = 0.68 * globalStrength;
        else if (topo === 2) w = 0.42 * globalStrength;
        else if (topo === 3) w = 0.25 * globalStrength;
        else w = 0.14 * globalStrength;
      } else {
        const ref = base.segments[selSeg].points[selIdx];
        const d = Math.hypot(bp.x - ref.x, bp.y - ref.y);

        // 其它骨架也要被带动，避免只有一个边变化
        const bb = bboxOfControls(base);
        const diag = Math.max(1, Math.hypot(bb.w, bb.h));
        const radius = diag * Number(radiusSlider.value || 0.75);

        const proximity = Math.max(0, 1 - d / radius);
        w = (0.12 + 0.38 * proximity) * globalStrength;
      }

      // 整体牵引底量：所有控制点都至少轻微跟随
      if (!(si === selSeg && pi === selIdx)) {
        w = Math.max(w, 0.08 * globalStrength);
      }

      p.x = bp.x + dx * w;
      p.y = bp.y + dy * w;
    });
  });

  return out;
}

function collectControlDisplacements(base, moved) {
  const arr = [];
  base.segments.forEach((seg, si) => {
    seg.points.forEach((p, pi) => {
      const mp = moved.segments[si].points[pi];
      arr.push({
        x: p.x,
        y: p.y,
        dx: mp.x - p.x,
        dy: mp.y - p.y
      });
    });
  });
  return arr;
}

function deformPointByControls(pt, baseControls, movedControls) {
  const disps = collectControlDisplacements(baseControls, movedControls);
  if (!disps.length) return pt;

  const bb = bboxOfControls(baseControls);
  const diag = Math.max(1, Math.hypot(bb.w, bb.h));
  const radius = diag * Number(radiusSlider.value || 0.75);
  const globalStrength = Number(globalSlider.value || 0.55);

  let sw = 0;
  let sx = 0;
  let sy = 0;

  // 全局平均位移：保证“整个字形”有协调移动
  let avgDx = 0;
  let avgDy = 0;
  disps.forEach(c => {
    avgDx += c.dx;
    avgDy += c.dy;
  });
  avgDx /= disps.length;
  avgDy /= disps.length;

  disps.forEach(c => {
    const d = Math.hypot(pt.x - c.x, pt.y - c.y);
    const local = Math.max(0, 1 - d / radius);

    // 不是只靠最近点，而是“全局底量 + 局部权重”
    const w = 0.10 * globalStrength + Math.pow(local, 2.2) * 1.6;

    sw += w;
    sx += c.dx * w;
    sy += c.dy * w;
  });

  let dx = sw > 1e-9 ? sx / sw : 0;
  let dy = sw > 1e-9 ? sy / sw : 0;

  // 加入全局形变底量，让另一侧边也产生协调变化
  dx = dx * 0.82 + avgDx * 0.18 * globalStrength;
  dy = dy * 0.82 + avgDy * 0.18 * globalStrength;

  return {
    x: pt.x + dx,
    y: pt.y + dy
  };
}

function buildDeformedSvg(baseControls, movedControls) {
  if (!parsedSvg) return "";

  const paths = parsedSvg.paths.map(p => {
    const newD = transformPathTokens(p.tokens, pt => deformPointByControls(pt, baseControls, movedControls));
    return `<path d="${newD}" fill="#C8A37A" stroke="none" fill-rule="evenodd"/>`;
  }).join("\\n");

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${parsedSvg.viewBox}">
${paths}
</svg>`;
}

// ------------------------------
// 绘制
// ------------------------------
function drawSkeleton(data) {
  skeletonLayer.innerHTML = "";
  handleLayer.innerHTML = "";

  if (!data) return;

  const showSkeleton = document.getElementById("showSkeleton").checked;
  const showHandles = document.getElementById("showHandles").checked;

  if (showSkeleton) {
    data.segments.forEach((seg, si) => {
      const pts = seg.points || [];
      if (pts.length >= 2) {
        const d = "M " + pts.map(p => `${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(" L ");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", d);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "#ff4d4f");
        path.setAttribute("stroke-width", "2");
        skeletonLayer.appendChild(path);
      }
    });
  }

  if (showHandles) {
    data.segments.forEach((seg, si) => {
      const pts = seg.points || [];
      pts.forEach((p, pi) => {
        const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        const active = selected && selected.seg === si && selected.idx === pi;
        c.setAttribute("cx", p.x);
        c.setAttribute("cy", p.y);
        c.setAttribute("r", active ? 7 : 5.5);
        c.setAttribute("fill", "#fff");
        c.setAttribute("stroke", active ? "#1f5eff" : "#ff4d4f");
        c.setAttribute("stroke-width", active ? "2.5" : "1.5");
        c.style.cursor = "move";

        c.addEventListener("pointerdown", ev => {
          ev.preventDefault();
          ev.stopPropagation();

          selected = {seg:si, idx:pi};
          dragging = true;
          dragStart = getMouseSvgPoint(ev);
          dragBaseControls = deepClone(controlData);

          updateSelectedInfo();
          redrawAll();
        });

        handleLayer.appendChild(c);
      });
    });
  }
}

function redrawAll() {
  if (!controlData || !originalControlData || !parsedSvg) return;

  originalLayer.style.display = document.getElementById("showOriginal").checked ? "" : "none";
  deformedLayer.style.display = document.getElementById("showDeformed").checked ? "" : "none";

  const deformedSvg = buildDeformedSvg(originalControlData, controlData);

  // 原始轮廓
  if (rawSvgText && originalLayer.innerHTML.trim() === "") {
    originalLayer.innerHTML = rawSvgText;
  }

  // 变形轮廓
  deformedLayer.innerHTML = deformedSvg;

  drawSkeleton(controlData);
}

function getMouseSvgPoint(evt) {
  const pt = mainSvg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(mainSvg.getScreenCTM().inverse());
}

mainSvg.addEventListener("pointermove", evt => {
  if (!dragging || !selected || !dragBaseControls) return;

  const now = getMouseSvgPoint(evt);
  const dx = now.x - dragStart.x;
  const dy = now.y - dragStart.y;

  controlData = makeMovedControls(dragBaseControls, selected.seg, selected.idx, dx, dy);
  updateSelectedInfo();
  redrawAll();
});

window.addEventListener("pointerup", () => {
  dragging = false;
  dragStart = null;
  dragBaseControls = null;
});

function updateSelectedInfo() {
  if (!selected || !controlData) {
    selectedInfo.textContent = "未选中任何点";
    return;
  }

  const p = controlData.segments[selected.seg].points[selected.idx];

  selectedInfo.textContent =
`字符: ${currentCode}
版本: ${currentVariant}
段: ${selected.seg}
控制点: ${selected.idx}
x: ${p.x.toFixed(2)}
y: ${p.y.toFixed(2)}

说明:
拖动该点时，系统会直接变形原始 SVG 轮廓的所有坐标点。`;
}

// ------------------------------
// 加载
// ------------------------------
async function loadManifest() {
  const res = await fetch(`/skeleton_manifest/${JOB_ID}`);
  manifestData = await res.json();

  renderGlyphList();

  if (manifestData.codes && manifestData.codes.length > 0) {
    currentCode = manifestData.codes[0].code;
    renderGlyphList();
    renderVariants();
    await loadCurrent();
  } else {
    setStatus("没有检测到可编辑数据。");
  }
}

function renderGlyphList() {
  glyphList.innerHTML = "";

  (manifestData.codes || []).forEach(item => {
    const btn = document.createElement("button");
    btn.className = "item-btn" + (item.code === currentCode ? " active" : "");
    btn.innerHTML = `<div style="font-size:24px;margin-bottom:6px;">${escHtml(item.char)}</div>
                     <div style="font-family:Consolas;font-size:12px;">${item.code}</div>
                     <div class="small">${item.variants.length} 个版本</div>`;

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

  const r1 = await fetch(`/skeleton_json/${JOB_ID}/${currentCode}/${currentVariant}`);
  skeletonData = await r1.json();

  controlData = simplifyControls(skeletonData);
  originalControlData = deepClone(controlData);

  const vb = skeletonData.viewBox || [0,0,1000,1000];
  mainSvg.setAttribute("viewBox", vb.join(" "));

  const r2 = await fetch(`/raw_svg_variant/${JOB_ID}/${currentCode}/${currentVariant}`);
  rawSvgText = await r2.text();
  parsedSvg = parseSvg(rawSvgText);

  originalLayer.innerHTML = rawSvgText;

  selected = null;
  updateSelectedInfo();
  redrawAll();

  setStatus(`已加载：${currentCode} / ${currentVariant}；整体轮廓变形模式已启用。`);
}

// ------------------------------
// 控件
// ------------------------------
globalSlider.addEventListener("input", () => {
  globalVal.textContent = Number(globalSlider.value).toFixed(2);
  redrawAll();
});

radiusSlider.addEventListener("input", () => {
  radiusVal.textContent = Number(radiusSlider.value).toFixed(2);
  redrawAll();
});

document.getElementById("showOriginal").addEventListener("change", redrawAll);
document.getElementById("showDeformed").addEventListener("change", redrawAll);
document.getElementById("showSkeleton").addEventListener("change", redrawAll);
document.getElementById("showHandles").addEventListener("change", redrawAll);

document.getElementById("resetBtn").addEventListener("click", () => {
  controlData = deepClone(originalControlData);
  selected = null;
  originalLayer.innerHTML = rawSvgText;
  updateSelectedInfo();
  redrawAll();
  setStatus("已恢复当前版本。");
});

document.getElementById("smoothBtn").addEventListener("click", () => {
  if (!controlData) return;

  controlData.segments.forEach(seg => {
    const pts = seg.points || [];
    if (pts.length < 3) return;

    const old = deepClone(pts);
    for (let i = 1; i < pts.length - 1; i++) {
      pts[i].x = (old[i-1].x + old[i].x + old[i+1].x) / 3;
      pts[i].y = (old[i-1].y + old[i].y + old[i+1].y) / 3;
    }
  });

  redrawAll();
  setStatus("已平滑控制点。");
});

// ------------------------------
// 保存 / 导出
// ------------------------------
async function saveCurrent() {
  if (!controlData || !originalControlData || !parsedSvg) return null;

  setStatus("正在保存整体轮廓变形结果...");

  const svgText = buildDeformedSvg(originalControlData, controlData);

  const res = await fetch(`/save_outline_edit/${JOB_ID}/${currentCode}/${currentVariant}`, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({svg: svgText})
  });

  const data = await res.json();

  if (data.ok) {
    setStatus("保存成功。");
    return data;
  } else {
    setStatus("保存失败：" + JSON.stringify(data));
    return null;
  }
}

document.getElementById("saveBtn").addEventListener("click", async () => {
  await saveCurrent();
});

document.getElementById("exportSvgBtn").addEventListener("click", async () => {
  const ret = await saveCurrent();
  if (ret && ret.svg_url) window.open(ret.svg_url, "_blank");
});

document.getElementById("exportPngBtn").addEventListener("click", async () => {
  const ret = await saveCurrent();
  if (ret && ret.png_url) window.open(ret.png_url, "_blank");
});

loadManifest().catch(err => {
  console.error(err);
  setStatus("加载失败：" + err);
});
</script>
</body>
</html>
""".replace("__JOB_ID__", job_id)

    html_path.write_text(html, encoding="utf-8")
