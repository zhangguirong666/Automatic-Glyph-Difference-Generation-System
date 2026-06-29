from unicode_async_api import router as unicode_async_router
# -*- coding: utf-8 -*-
import os
import csv
import uuid
import json
import re
import time
import shutil
import zipfile
import threading
import subprocess
from pathlib import Path

import numpy as np
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from svgpathtools import parse_path

APP_DIR = Path(__file__).resolve().parent
JOBS_DIR = APP_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

FONTFORGE_SCRIPT = APP_DIR / "build_step_fonts_ff.py"

app = FastAPI(title="Font Morph Web Tool")

JOBS = {}

# -----------------------------
# 字符集
# -----------------------------
def unique_chars(s):
    seen = set()
    out = []
    for ch in s:
        if ch and ch not in seen:
            seen.add(ch)
            out.append(ch)
    return "".join(out)

def charset_english():
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

def charset_mongolian_basic():
    return "".join(chr(c) for c in range(0x1820, 0x1843))

def charset_german_basic():
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyzÄÖÜäöüẞß"

def charset_russian_basic():
    return "".join(chr(c) for c in range(0x0410, 0x0450)) + "Ёё"

def charset_japanese_kana():
    hira = "".join(chr(c) for c in range(0x3041, 0x3097))
    kata = "".join(chr(c) for c in range(0x30A1, 0x30FB))
    return hira + kata

def charset_korean_basic():
    # 韩文完整音节有 11172 个。这里先给基础 Jamo + 前 1000 个音节，正式大量生成建议用自定义文本。
    jamo = "".join(chr(c) for c in range(0x3131, 0x3190))
    syllables = "".join(chr(c) for c in range(0xAC00, 0xAC00 + 1000))
    return jamo + syllables

def charset_chinese_3500():
    # 近似中文常用 3500：从 GB2312 一级汉字序列生成前 3500 个。
    chars = []
    for high in range(0xB0, 0xF8):
        for low in range(0xA1, 0xFF):
            try:
                ch = bytes([high, low]).decode("gb2312")
                if "\u4e00" <= ch <= "\u9fff":
                    chars.append(ch)
            except Exception:
                pass
            if len(chars) >= 3500:
                return "".join(chars)
    return "".join(chars[:3500])

def charset_chinese_6500():
    chars = []
    for high in range(0xB0, 0xF8):
        for low in range(0xA1, 0xFF):
            try:
                ch = bytes([high, low]).decode("gb2312")
                if "\u4e00" <= ch <= "\u9fff":
                    chars.append(ch)
            except Exception:
                pass
            if len(chars) >= 6500:
                return "".join(chars)
    return "".join(chars[:6500])

def get_charset(preset, custom_text):
    preset = (preset or "").strip().lower()
    custom_text = custom_text or ""
    custom_text = custom_text.strip()

    if preset == "custom":
        return unique_chars(custom_text)

    if preset in ["english", "english_basic", "english_letters"]:
        return charset_english()

    if preset in ["mongolian", "mongolian_basic_35", "traditional_mongolian_35", "china_gb"]:
        return charset_mongolian_basic()

    if preset in ["chinese3500", "chinese_3500"]:
        return charset_chinese_3500()

    if preset in ["chinese6500", "chinese_6500", "chinese"]:
        return charset_chinese_6500()

    if preset in ["japanese", "japanese_kana"]:
        return charset_japanese_kana()

    if preset in ["korean", "korean_basic"]:
        return charset_korean_basic()

    if preset in ["german", "german_basic"]:
        return charset_german_basic()

    if preset in ["russian", "russian_basic"]:
        return charset_russian_basic()

    if custom_text:
        return unique_chars(custom_text)

    return charset_english()

# -----------------------------
# 字体轮廓处理
# -----------------------------
def get_font_cmap(font_path):
    font = TTFont(font_path)
    cmap = font.getBestCmap()
    if cmap is None:
        cmap = {}
    return font, cmap

def glyph_to_svg_path(ttfont, glyph_name):
    glyph_set = ttfont.getGlyphSet()
    pen = SVGPathPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    return pen.getCommands()

def path_to_subpaths(path_d):
    if not path_d or not path_d.strip():
        return []
    p = parse_path(path_d)
    return p.continuous_subpaths()

def sample_subpath_by_length(subpath, n_points=120):
    length = subpath.length(error=1e-4)
    if length <= 1e-6:
        return None

    pts = []
    for i in range(n_points):
        s = length * i / n_points
        try:
            t = subpath.ilength(s)
        except Exception:
            t = i / n_points
        pts.append(subpath.point(t))

    return np.array(pts, dtype=np.complex128)

def signed_area(points):
    if len(points) < 3:
        return 0.0
    x = points.real
    y = points.imag
    return 0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)

def contour_area_abs(points):
    return abs(signed_area(points))

def normalize_orientation_and_start(a, b):
    if len(a) != len(b):
        return a, b

    area_a = signed_area(a)
    area_b = signed_area(b)

    candidates = []

    b1 = b.copy()
    if area_a * area_b < 0:
        b1 = b1[::-1]
    candidates.append(b1)

    candidates.append(b[::-1].copy())

    a_centered = a - np.mean(a)
    best_b = None
    best_score = float("inf")

    for cand in candidates:
        cand_centered = cand - np.mean(cand)
        for shift in range(len(cand)):
            shifted = np.roll(cand_centered, shift)
            score = np.mean(np.abs(a_centered - shifted) ** 2)
            if score < best_score:
                best_score = score
                best_b = np.roll(cand, shift)

    return a, best_b

def get_sampled_contours(ttfont, glyph_name, n_points):
    d = glyph_to_svg_path(ttfont, glyph_name)
    subpaths = path_to_subpaths(d)

    contours = []
    for sp in subpaths:
        pts = sample_subpath_by_length(sp, n_points)
        if pts is not None:
            contours.append(pts)

    contours = sorted(contours, key=contour_area_abs, reverse=True)
    return contours

def get_center_and_radius(contours):
    if len(contours) == 0:
        return 0 + 0j, 5.0

    all_pts = np.concatenate(contours)
    min_x = np.min(all_pts.real)
    max_x = np.max(all_pts.real)
    min_y = np.min(all_pts.imag)
    max_y = np.max(all_pts.imag)

    center = (min_x + max_x) / 2 + 1j * (min_y + max_y) / 2
    size = max(max_x - min_x, max_y - min_y)
    radius = max(size * 0.02, 3.0)
    return center, radius

def make_tiny_circle(center, radius, n_points):
    pts = []
    for i in range(n_points):
        t = 2 * np.pi * i / n_points
        z = (center.real + radius * np.cos(t)) + 1j * (center.imag + radius * np.sin(t))
        pts.append(z)
    return np.array(pts, dtype=np.complex128)

def match_contours(contours_a, contours_b, n_points, mode):
    if mode == "strict" and len(contours_a) != len(contours_b):
        raise ValueError("轮廓数量不一致：fontA=%d, fontB=%d" % (len(contours_a), len(contours_b)))

    k = max(len(contours_a), len(contours_b), 1)

    center_a, radius_a = get_center_and_radius(contours_a)
    center_b, radius_b = get_center_and_radius(contours_b)

    ca = [c.copy() for c in contours_a]
    cb = [c.copy() for c in contours_b]

    while len(ca) < k:
        ca.append(make_tiny_circle(center_a, radius_a, n_points))

    while len(cb) < k:
        cb.append(make_tiny_circle(center_b, radius_b, n_points))

    matched_a = []
    matched_b = []

    for i in range(k):
        aa, bb = normalize_orientation_and_start(ca[i], cb[i])
        matched_a.append(aa)
        matched_b.append(bb)

    return matched_a, matched_b

def interpolate_contours(contours_a, contours_b, alpha):
    out = []
    for ca, cb in zip(contours_a, contours_b):
        out.append((1.0 - alpha) * ca + alpha * cb)
    return out

def contours_to_svg(contours, save_path):
    save_path = Path(save_path)

    if len(contours) == 0:
        save_path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"></svg>', encoding="utf-8")
        return

    all_points = []
    for c in contours:
        pts = np.column_stack([c.real, -c.imag])
        all_points.append(pts)

    all_points_flat = np.vstack(all_points)

    min_x, min_y = np.min(all_points_flat, axis=0)
    max_x, max_y = np.max(all_points_flat, axis=0)

    pad = 80
    view_x = min_x - pad
    view_y = min_y - pad
    view_w = max((max_x - min_x) + pad * 2, 100)
    view_h = max((max_y - min_y) + pad * 2, 100)

    d_parts = []
    for pts in all_points:
        if len(pts) == 0:
            continue
        d = "M %.3f %.3f " % (pts[0, 0], pts[0, 1])
        for p in pts[1:]:
            d += "L %.3f %.3f " % (p[0], p[1])
        d += "Z "
        d_parts.append(d)

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_x:.3f} {view_y:.3f} {view_w:.3f} {view_h:.3f}">
  <path d="{"".join(d_parts)}" fill="black" stroke="none" fill-rule="evenodd"/>
</svg>
'''
    save_path.write_text(svg, encoding="utf-8")

def zip_dir(src_dir, out_zip):
    src_dir = Path(src_dir)
    out_zip = Path(out_zip)
    if out_zip.exists():
        out_zip.unlink()

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in src_dir.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(src_dir.parent))

def make_preview_html(job_dir, chars, steps):
    """
    SVG 预览版：
    不再用生成的 TTF 在浏览器里直接排文字。
    直接展示每个字符每一步生成出来的 SVG。
    这样更适合检查蒙古文、中文、日文、韩文等字形插值结果。
    """
    job_dir = Path(job_dir)
    svg_root = job_dir / "svg"
    html_path = job_dir / "preview.html"

    # 大字符集不要全量预览，否则网页会非常卡。
    preview_chars = chars[:120]

    html = []
    html.append("""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Font Morph SVG Preview</title>
<style>
body {
  font-family: Arial, "Microsoft YaHei", sans-serif;
  background: #f5f5f5;
  padding: 24px;
}
h1 {
  margin-bottom: 8px;
}
.note {
  color: #666;
  margin-bottom: 18px;
}
.table-wrap {
  overflow-x: auto;
  background: white;
  border-radius: 12px;
  box-shadow: 0 1px 8px rgba(0,0,0,.08);
}
table {
  border-collapse: collapse;
  min-width: 100%;
}
th, td {
  border: 1px solid #e5e5e5;
  padding: 8px;
  text-align: center;
  vertical-align: middle;
  white-space: nowrap;
}
th {
  background: #fafafa;
  position: sticky;
  top: 0;
  z-index: 5;
}
.char-cell {
  min-width: 90px;
  font-size: 24px;
}
.code {
  font-family: Consolas, monospace;
  color: #555;
  font-size: 12px;
  margin-top: 4px;
}
.svgcell {
  width: 110px;
  height: 110px;
  background: #fff;
}
.svgcell svg {
  width: 96px;
  height: 96px;
  max-width: 96px;
  max-height: 96px;
  object-fit: contain;
}
.missing {
  color: #999;
  font-size: 12px;
}
</style>
</head>
<body>
<h1>字体插值 SVG 预览</h1>
<div class="note">
这里直接展示生成的 SVG 文件，不依赖浏览器文本排版。适合检查字形中间变化是否正确。
为避免页面过重，最多预览前 120 个字符；完整结果请下载 SVG / TTF 压缩包。
</div>
<div class="note">
预览字符数：""" + str(len(preview_chars)) + """　步数：""" + str(steps) + """
</div>
<div class="table-wrap">
<table>
<thead>
<tr>
<th>字符</th>
<th>Unicode</th>
""")

    for step in range(1, steps + 1):
        html.append(f"<th>Step {step:02d}</th>\n")

    html.append("""</tr>
</thead>
<tbody>
""")

    for ch in preview_chars:
        cp = ord(ch)
        code = f"U{cp:04X}"

        html.append("<tr>\n")
        html.append(f"<td class='char-cell'>{ch}</td>\n")
        html.append(f"<td class='code'>{code}</td>\n")

        for step in range(1, steps + 1):
            svg_path = svg_root / code / f"{code}_step_{step:02d}.svg"

            if svg_path.exists():
                try:
                    svg_text = svg_path.read_text(encoding="utf-8")
                    # 去掉可能存在的 XML 声明，避免嵌入 HTML 时异常
                    svg_text = svg_text.replace('<?xml version="1.0" encoding="UTF-8"?>', "")
                    html.append(f"<td class='svgcell'>{svg_text}</td>\n")
                except Exception as e:
                    html.append(f"<td class='missing'>读取失败<br>{e}</td>\n")
            else:
                html.append("<td class='missing'>missing</td>\n")

        html.append("</tr>\n")

    html.append("""</tbody>
</table>
</div>
</body>
</html>
""")

    html_path.write_text("".join(html), encoding="utf-8")

def make_family_preview_html(job_dir, chars, steps):
    """
    字体家族 SVG 准确预览版：
    每一张卡片对应一个生成出来的 TTF 样式；
    但是显示内容不再用浏览器直接排 TTF 文本，
    而是读取该 step 对应的 SVG 字形，避免蒙古文/复杂文字 shaping 错误。
    """
    job_dir = Path(job_dir)
    fonts_dir = job_dir / "fonts"
    svg_root = job_dir / "svg"
    html_path = job_dir / "family_preview.html"

    font_files = sorted(fonts_dir.glob("*.ttf"))

    preview_chars = chars[:120]
    if not preview_chars:
        # 兜底：从 svg 目录推断字符
        cps = []
        if svg_root.exists():
            for d in svg_root.iterdir():
                if d.is_dir() and d.name.startswith("U"):
                    try:
                        cps.append(int(d.name[1:], 16))
                    except Exception:
                        pass
        cps = sorted(set(cps))
        preview_chars = "".join(chr(cp) for cp in cps[:120])

    html = []
    html.append("""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Font Family Accurate Preview</title>
<style>
body {
  font-family: Arial, "Microsoft YaHei", sans-serif;
  background: #f5f5f5;
  padding: 24px;
  color: #222;
}
h1 {
  margin-bottom: 8px;
}
.note {
  color: #666;
  margin-bottom: 20px;
  line-height: 1.7;
}
.card {
  background: white;
  padding: 18px;
  margin-bottom: 18px;
  border-radius: 12px;
  box-shadow: 0 1px 8px rgba(0,0,0,.08);
}
.font-title {
  font-weight: bold;
  margin-bottom: 14px;
  display: flex;
  gap: 12px;
  align-items: center;
}
.index {
  background: #1f5eff;
  color: white;
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 13px;
}
.filename {
  font-family: Consolas, monospace;
  color: #333;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(86px, 1fr));
  gap: 10px;
}
.cell {
  border: 1px solid #e5e5e5;
  border-radius: 8px;
  background: #fff;
  padding: 6px;
  text-align: center;
  min-height: 104px;
}
.cell svg {
  width: 72px;
  height: 72px;
  max-width: 72px;
  max-height: 72px;
}
.code {
  font-family: Consolas, monospace;
  color: #777;
  font-size: 11px;
  margin-top: 4px;
}
.char {
  font-size: 14px;
  color: #333;
}
.missing {
  color: #aaa;
  font-size: 12px;
  padding-top: 28px;
}
.toolbar {
  margin-bottom: 20px;
}
.small {
  font-size: 13px;
  color: #777;
}
</style>
</head>
<body>
<h1>TTF 字体家族准确预览</h1>
<div class="note">
这个页面用于预览“字体家族”的每一个样式。每张卡片对应一个生成出来的 TTF 文件。<br>
为避免传统蒙古文、阿拉伯文等复杂文字在浏览器里排版错误，下面直接显示对应 step 的 SVG 字形图像。<br>
所以这里看到的是最接近真实轮廓变化的结果。
</div>
""")

    html.append(f"<div class='note'>检测到 TTF 文件：{len(font_files)} 个；预览字符：{len(preview_chars)} 个；step 数：{steps}</div>\n")

    if not font_files:
        html.append("<div class='card'>没有找到 fonts/ 目录下的 .ttf 文件。</div>")
    else:
        # 注意：font_files 的排序顺序对应 step 01、step 02...
        for step_idx, font_path in enumerate(font_files, start=1):
            if step_idx > steps:
                break

            html.append("<section class='card'>\n")
            html.append("<div class='font-title'>\n")
            html.append(f"<span class='index'>#{step_idx:02d}</span>\n")
            html.append(f"<span class='filename'>{font_path.name}</span>\n")
            html.append(f"<span class='small'>对应 SVG Step {step_idx:02d}</span>\n")
            html.append("</div>\n")
            html.append("<div class='grid'>\n")

            for ch in preview_chars:
                cp = ord(ch)
                code = f"U{cp:04X}"
                svg_path = svg_root / code / f"{code}_step_{step_idx:02d}.svg"

                html.append("<div class='cell'>\n")

                if svg_path.exists():
                    try:
                        svg_text = svg_path.read_text(encoding="utf-8")
                        svg_text = svg_text.replace('<?xml version="1.0" encoding="UTF-8"?>', "")
                        html.append(svg_text)
                    except Exception as e:
                        html.append(f"<div class='missing'>读取失败</div>")
                else:
                    html.append("<div class='missing'>missing</div>")

                html.append(f"<div class='char'>{ch}</div>\n")
                html.append(f"<div class='code'>{code}</div>\n")
                html.append("</div>\n")

            html.append("</div>\n")
            html.append("</section>\n")

    html.append("""
</body>
</html>
""")

    html_path.write_text("".join(html), encoding="utf-8")

def make_variable_preview_html(job_dir, chars, steps):
    """
    实时可变滑杆预览：
    不再只是切换 step_01 / step_02 / step_03 图片，
    而是在浏览器端解析相邻 SVG path，并对路径坐标做实时线性插值。
    """
    job_dir = Path(job_dir)
    job_id = job_dir.name
    svg_root = job_dir / "svg"
    html_path = job_dir / "variable_preview.html"

    preview_chars = chars[:300]
    if not preview_chars:
        cps = []
        if svg_root.exists():
            for d in svg_root.iterdir():
                if d.is_dir() and d.name.startswith("U"):
                    try:
                        cps.append(int(d.name[1:], 16))
                    except Exception:
                        pass
        cps = sorted(set(cps))
        preview_chars = "".join(chr(cp) for cp in cps[:300])

    items = []
    for ch in preview_chars:
        cp = ord(ch)
        code = f"U{cp:04X}"
        items.append({"char": ch, "code": code})

    import json
    items_json = json.dumps(items, ensure_ascii=False)

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Realtime Variable Slider Preview</title>
<style>
body {{
  margin: 0;
  font-family: Arial, "Microsoft YaHei", sans-serif;
  background: #f5f5f5;
  color: #222;
}}
.header {{
  background: #1f5eff;
  color: white;
  padding: 22px 34px;
}}
.container {{
  padding: 24px;
  max-width: 1280px;
  margin: 0 auto;
}}
.note {{
  color: #666;
  line-height: 1.7;
  margin-bottom: 18px;
}}
.panel {{
  background: white;
  border-radius: 14px;
  box-shadow: 0 1px 10px rgba(0,0,0,.08);
  padding: 22px;
  margin-bottom: 20px;
}}
.viewer {{
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 22px;
}}
.char-list {{
  max-height: 680px;
  overflow-y: auto;
  border-right: 1px solid #eee;
  padding-right: 16px;
}}
.char-btn {{
  width: 100%;
  text-align: left;
  background: #fafafa;
  border: 1px solid #ddd;
  border-radius: 8px;
  padding: 9px 10px;
  margin-bottom: 8px;
  cursor: pointer;
  font-size: 15px;
}}
.char-btn.active {{
  background: #1f5eff;
  color: white;
  border-color: #1f5eff;
}}
.preview-area {{
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
}}
.big-box {{
  width: 100%;
  min-height: 430px;
  border: 1px solid #e5e5e5;
  border-radius: 14px;
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 22px;
  overflow: hidden;
}}
#mainSvg {{
  width: 390px;
  height: 390px;
  max-width: 90%;
  max-height: 390px;
}}
.info {{
  font-family: Consolas, monospace;
  color: #555;
  margin-bottom: 16px;
}}
.slider-wrap {{
  width: 100%;
  max-width: 820px;
}}
input[type=range] {{
  width: 100%;
}}
.controls {{
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 14px;
  flex-wrap: wrap;
}}
button {{
  background: #1f5eff;
  color: white;
  border: 0;
  border-radius: 8px;
  padding: 10px 16px;
  cursor: pointer;
}}
button.secondary {{
  background: #555;
}}
.value {{
  font-weight: bold;
  color: #1f5eff;
}}
.compare {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px;
  width: 100%;
  margin-top: 20px;
}}
.compare-card {{
  border: 1px solid #e5e5e5;
  border-radius: 12px;
  padding: 12px;
  text-align: center;
  background: #fff;
}}
.compare-card svg {{
  width: 160px;
  height: 160px;
}}
.compare-title {{
  font-size: 13px;
  color: #666;
  margin-bottom: 8px;
}}
.grid-preview {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(84px, 1fr));
  gap: 10px;
  margin-top: 18px;
}}
.grid-cell {{
  border: 1px solid #eee;
  background: white;
  border-radius: 8px;
  padding: 6px;
  text-align: center;
}}
.grid-cell img {{
  width: 66px;
  height: 66px;
  object-fit: contain;
}}
.small-code {{
  font-family: Consolas, monospace;
  font-size: 11px;
  color: #777;
}}
.warn {{
  color: #a15c00;
  font-size: 13px;
}}

/* ===== white black override ===== */
body {{
  background: #ffffff !important;
  color: #111111 !important;
}}
.header {{
  background: #ffffff !important;
  color: #111111 !important;
  border-bottom: 1px solid #dddddd !important;
}}
.panel, .card, .compare-card, .grid-cell, .big-box {{
  background: #ffffff !important;
  color: #111111 !important;
  border-color: #dddddd !important;
}}
.char-btn {{
  background: #ffffff !important;
  color: #111111 !important;
  border: 1px solid #dddddd !important;
}}
.char-btn.active {{
  background: #ffffff !important;
  color: #111111 !important;
  border: 2px solid #111111 !important;
}}
button {{
  background: #ffffff !important;
  color: #111111 !important;
  border: 1px solid #111111 !important;
}}
button.secondary {{
  background: #ffffff !important;
  color: #111111 !important;
}}
.value {{
  color: #111111 !important;
}}
.note, .small-code, .compare-title, .warn {{
  color: #333333 !important;
}}
</style>
</head>
<body>
<div class="header">
  <h1>实时可变滑杆预览</h1>
  <div>拖动滑杆时，浏览器会实时计算相邻两个 step 之间的中间字形。</div>
</div>

<div class="container">
  <div class="note">
    当前版本基于 SVG 路径坐标实时插值。它不是简单切换图片，因此滑杆可以显示连续变化。<br>
    如果某个字的相邻 step 路径点数不一致，系统会自动退回到最近 step 的 SVG 图片显示。
  </div>

  <div class="panel viewer">
    <div>
      <h3>选择字符</h3>
      <div class="char-list" id="charList"></div>
    </div>

    <div class="preview-area">
      <div class="info">
        当前字符：<span id="currentChar"></span>
        &nbsp; Unicode：<span id="currentCode"></span>
        &nbsp; Morph：<span class="value" id="currentValue">1.00</span> / {steps}
        &nbsp; <span id="modeInfo" class="warn"></span>
      </div>

      <div class="big-box">
        <div id="mainSvg"></div>
      </div>

      <div class="slider-wrap">
        <input id="slider" type="range" min="1" max="{steps}" value="1" step="0.01">
        <div class="controls">
          <button id="playBtn">播放</button>
          <button class="secondary" id="prevBtn">上一步</button>
          <button class="secondary" id="nextBtn">下一步</button>
          <button class="secondary" id="snapBtn">吸附到最近 Step</button>
          <span>现在是连续实时变化，不只是整数 Step 切换</span>
        </div>
      </div>

      <div class="compare">
        <div class="compare-card">
          <div class="compare-title">起始 Step 01</div>
          <div id="startSvg"></div>
        </div>
        <div class="compare-card">
          <div class="compare-title">当前实时 Morph</div>
          <div id="midSvg"></div>
        </div>
        <div class="compare-card">
          <div class="compare-title">结束 Step {steps:02d}</div>
          <div id="endSvg"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="panel">
    <h3>当前最近 Step 下的字符总览</h3>
    <div class="note">为了保证速度，这里仍用最近的整数 Step 显示整体字符网格；上方大图是实时插值结果。</div>
    <div id="gridPreview" class="grid-preview"></div>
  </div>
</div>

<script>
const JOB_ID = "{job_id}";
const ITEMS = {items_json};
const STEPS = {steps};

let currentIndex = 0;
let currentValue = 1.0;
let playing = false;
let timer = null;

// SVG cache: code_step -> parsed svg data
const svgCache = new Map();

const charList = document.getElementById("charList");
const slider = document.getElementById("slider");
const mainSvg = document.getElementById("mainSvg");
const startSvg = document.getElementById("startSvg");
const midSvg = document.getElementById("midSvg");
const endSvg = document.getElementById("endSvg");
const currentChar = document.getElementById("currentChar");
const currentCode = document.getElementById("currentCode");
const currentValueEl = document.getElementById("currentValue");
const modeInfo = document.getElementById("modeInfo");
const playBtn = document.getElementById("playBtn");
const prevBtn = document.getElementById("prevBtn");
const nextBtn = document.getElementById("nextBtn");
const snapBtn = document.getElementById("snapBtn");
const gridPreview = document.getElementById("gridPreview");

function svgUrl(code, step) {{
  return `/raw_svg/${{JOB_ID}}/${{code}}/${{step}}`;
}}

function cacheKey(code, step) {{
  return `${{code}}_${{step}}`;
}}

function renderCharList() {{
  charList.innerHTML = "";
  ITEMS.forEach((item, idx) => {{
    const btn = document.createElement("button");
    btn.className = "char-btn" + (idx === currentIndex ? " active" : "");
    btn.innerHTML = `${{item.char}} &nbsp; <span style="font-family:Consolas">${{item.code}}</span>`;
    btn.onclick = async () => {{
      currentIndex = idx;
      renderCharList();
      await preloadCurrentChar();
      await updateAll(true);
    }};
    charList.appendChild(btn);
  }});
}}

async function fetchSvgText(code, step) {{
  const key = cacheKey(code, step);
  if (svgCache.has(key)) return svgCache.get(key);

  const resp = await fetch(svgUrl(code, step));
  const text = await resp.text();
  const parsed = parseSvgText(text);
  svgCache.set(key, parsed);
  return parsed;
}}

function parseSvgText(text) {{
  const viewBoxMatch = text.match(/viewBox="([^"]+)"/);
  let viewBox = "0 0 1000 1000";
  if (viewBoxMatch) viewBox = viewBoxMatch[1];

  const pathMatch = text.match(/<path[^>]*d="([^"]+)"[^>]*>/);
  const fillMatch = text.match(/<path[^>]*fill="([^"]+)"[^>]*>/);

  if (!pathMatch) {{
    return {{
      ok: false,
      text,
      viewBox,
      d: "",
      nums: [],
      tokens: [],
      fill: "black"
    }};
  }}

  const d = pathMatch[1];
  const fill = fillMatch ? fillMatch[1] : "black";

  // 把 path d 拆成命令和数字 token。你的 SVG 基本是 M/L/Z 结构，适合实时插值。
  const tokens = d.match(/[MLCZmlcz]|-?\\d*\\.?\\d+(?:e[-+]?\\d+)?/g) || [];
  const nums = [];
  const tokenTypes = [];

  tokens.forEach(tok => {{
    if (/^[MLCZmlcz]$/.test(tok)) {{
      tokenTypes.push({{ type: "cmd", value: tok }});
    }} else {{
      const n = Number(tok);
      tokenTypes.push({{ type: "num", value: n }});
      nums.push(n);
    }}
  }});

  return {{
    ok: true,
    text,
    viewBox,
    d,
    tokens: tokenTypes,
    nums,
    fill
  }};
}}

function buildDFromTokens(tokens, nums) {{
  let idx = 0;
  const out = [];
  tokens.forEach(t => {{
    if (t.type === "cmd") {{
      out.push(t.value);
    }} else {{
      const v = nums[idx++];
      out.push(Number.isFinite(v) ? v.toFixed(3) : "0");
    }}
  }});
  return out.join(" ");
}}

function svgElementString(viewBox, d, fill="black") {{
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${{viewBox}}">
    <path d="${{d}}" fill="${{fill}}" stroke="none" fill-rule="evenodd"/>
  </svg>`;
}}

function nearestInt(v) {{
  return Math.max(1, Math.min(STEPS, Math.round(v)));
}}

async function realtimeSvgFor(code, value) {{
  const a = Math.floor(value);
  const b = Math.ceil(value);
  const left = Math.max(1, Math.min(STEPS, a));
  const right = Math.max(1, Math.min(STEPS, b));
  const t = right === left ? 0 : (value - left) / (right - left);

  const A = await fetchSvgText(code, left);
  const B = await fetchSvgText(code, right);

  if (!A.ok || !B.ok || A.nums.length !== B.nums.length || A.tokens.length !== B.tokens.length) {{
    const N = await fetchSvgText(code, nearestInt(value));
    modeInfo.textContent = "路径不兼容，显示最近 Step";
    return N.text;
  }}

  // 判断命令结构是否一致
  for (let i = 0; i < A.tokens.length; i++) {{
    if (A.tokens[i].type !== B.tokens[i].type) {{
      const N = await fetchSvgText(code, nearestInt(value));
      modeInfo.textContent = "路径结构不一致，显示最近 Step";
      return N.text;
    }}
    if (A.tokens[i].type === "cmd" && A.tokens[i].value !== B.tokens[i].value) {{
      const N = await fetchSvgText(code, nearestInt(value));
      modeInfo.textContent = "路径命令不一致，显示最近 Step";
      return N.text;
    }}
  }}

  const nums = A.nums.map((x, i) => x * (1 - t) + B.nums[i] * t);
  const d = buildDFromTokens(A.tokens, nums);
  modeInfo.textContent = "实时插值";
  return svgElementString(A.viewBox, d, A.fill || "black");
}}

async function setInlineSvg(target, svgText) {{
  target.innerHTML = svgText;
}}

async function preloadCurrentChar() {{
  if (!ITEMS.length) return;
  const code = ITEMS[currentIndex].code;

  // 先预加载当前左右相邻，避免拖动卡顿
  const v = Number(currentValue);
  const l = Math.max(1, Math.floor(v));
  const r = Math.min(STEPS, Math.ceil(v));
  await Promise.all([
    fetchSvgText(code, 1),
    fetchSvgText(code, STEPS),
    fetchSvgText(code, l),
    fetchSvgText(code, r)
  ]);
}}

async function updateMain() {{
  if (!ITEMS.length) return;

  const item = ITEMS[currentIndex];
  currentChar.textContent = item.char;
  currentCode.textContent = item.code;
  currentValueEl.textContent = Number(currentValue).toFixed(2);

  const svgText = await realtimeSvgFor(item.code, currentValue);
  await setInlineSvg(mainSvg, svgText);
  await setInlineSvg(midSvg, svgText);

  const s1 = await fetchSvgText(item.code, 1);
  const sN = await fetchSvgText(item.code, STEPS);
  await setInlineSvg(startSvg, s1.text);
  await setInlineSvg(endSvg, sN.text);
}}

function updateGrid() {{
  const step = nearestInt(currentValue);
  gridPreview.innerHTML = "";

  ITEMS.slice(0, 300).forEach(item => {{
    const cell = document.createElement("div");
    cell.className = "grid-cell";
    cell.innerHTML = `
      <img src="${{svgUrl(item.code, step)}}?v=${{Date.now()}}">
      <div>${{item.char}}</div>
      <div class="small-code">${{item.code}}</div>
    `;
    gridPreview.appendChild(cell);
  }});
}}

async function updateAll(updateGridFlag=false) {{
  slider.value = currentValue;
  renderCharList();
  await updateMain();
  if (updateGridFlag) updateGrid();
}}

let rafBusy = false;
slider.addEventListener("input", () => {{
  currentValue = parseFloat(slider.value);

  // 用 requestAnimationFrame 控制刷新频率，让拖动更顺
  if (!rafBusy) {{
    rafBusy = true;
    requestAnimationFrame(async () => {{
      await updateMain();
      rafBusy = false;
    }});
  }}
}});

slider.addEventListener("change", () => {{
  updateGrid();
}});

prevBtn.onclick = async () => {{
  currentValue = Math.max(1, currentValue - 1);
  await updateAll(true);
}};

nextBtn.onclick = async () => {{
  currentValue = Math.min(STEPS, currentValue + 1);
  await updateAll(true);
}};

snapBtn.onclick = async () => {{
  currentValue = nearestInt(currentValue);
  await updateAll(true);
}};

playBtn.onclick = () => {{
  playing = !playing;
  playBtn.textContent = playing ? "暂停" : "播放";

  if (playing) {{
    timer = setInterval(async () => {{
      currentValue += 0.18;
      if (currentValue > STEPS) currentValue = 1;
      slider.value = currentValue;
      await updateMain();
    }}, 40);
  }} else {{
    clearInterval(timer);
  }}
}};

(async function init() {{
  renderCharList();
  await preloadCurrentChar();
  await updateAll(true);
}})();
</script>
</body>
</html>
"""
    html_path.write_text(html, encoding="utf-8")

def _step_blend_font_sort_key(path):
    name = Path(path).name
    match = re.search(r"(?:step|morph|weight)[_-]?0*(\d+)", name, re.IGNORECASE)
    if not match:
        match = re.search(r"(\d+)", name)
    number = int(match.group(1)) if match else 999999
    return (number, name.lower())

def _variable_preview_chars_from_job(job_dir):
    job_dir = Path(job_dir)

    for name in ["ok_chars_codepoints.txt", "chars_codepoints.txt"]:
        path = job_dir / name
        if not path.exists():
            continue

        chars = []
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            value = raw.strip().replace("U+", "").replace("u+", "").replace("U", "")
            if not value:
                continue
            try:
                chars.append(chr(int(value, 16)))
            except Exception:
                pass

        chars = list(dict.fromkeys(chars))
        if chars:
            return "".join(chars[:300])

    svg_root = job_dir / "svg"
    cps = []
    if svg_root.exists():
        for item in svg_root.iterdir():
            if item.is_dir() and item.name.upper().startswith("U"):
                try:
                    cps.append(int(item.name[1:], 16))
                except Exception:
                    pass

    return "".join(chr(cp) for cp in sorted(set(cps))[:300])

def _variable_preview_steps_from_job(job_dir):
    job_dir = Path(job_dir)
    svg_root = job_dir / "svg"
    max_step = 0

    if svg_root.exists():
        for svg in svg_root.glob("U*/U*_step_*.svg"):
            match = re.search(r"_step_(\d+)\.svg$", svg.name, re.IGNORECASE)
            if match:
                max_step = max(max_step, int(match.group(1)))

    if max_step > 0:
        return max_step

    fonts_dir = job_dir / "fonts"
    if fonts_dir.exists():
        fonts = list(fonts_dir.glob("*.ttf")) + list(fonts_dir.glob("*.otf"))
        if fonts:
            return len(fonts)

    return 20

def make_step_blend_variable_preview_html(job_dir, chars="", steps=None):
    """
    Slider preview built from the generated Step TTF family.
    It avoids direct SVG coordinate interpolation, which can look twisted when
    matching points do not describe the same visual parts of a glyph.
    """
    import html as _preview_html
    from urllib.parse import quote as _preview_quote

    job_dir = Path(job_dir)
    job_id = job_dir.name
    html_path = job_dir / "variable_preview.html"

    fonts_dir = job_dir / "fonts"
    font_files = []
    if fonts_dir.exists():
        font_files = sorted(
            list(fonts_dir.glob("*.ttf")) + list(fonts_dir.glob("*.otf")),
            key=_step_blend_font_sort_key,
        )

    if not font_files:
        make_variable_preview_html(job_dir, chars or _variable_preview_chars_from_job(job_dir), steps or 20)
        return html_path

    steps = int(steps or len(font_files) or _variable_preview_steps_from_job(job_dir))
    steps = max(1, min(steps, len(font_files)))
    font_files = font_files[:steps]

    preview_chars = (chars or _variable_preview_chars_from_job(job_dir))[:300]
    if not preview_chars:
        preview_chars = "ABCDEabcde123"

    items = []
    for ch in preview_chars:
        cp = ord(ch)
        items.append({"char": ch, "code": f"U+{cp:04X}"})

    default_text = "".join(dict.fromkeys(preview_chars.replace("\n", "").replace("\r", "")))[:18]
    if not default_text:
        default_text = preview_chars[:18] or "ABCDEabcde123"

    font_faces = []
    font_data = []
    for index, font_path in enumerate(font_files):
        family = f"MorphStep{index + 1:02d}_{job_id}"
        suffix = font_path.suffix.lower()
        fmt = "opentype" if suffix == ".otf" else "truetype"
        url = f"/job_font/{_preview_quote(job_id)}/{_preview_quote(font_path.name)}"
        font_faces.append(
            "@font-face { "
            f"font-family: '{family}'; "
            f"src: url('{url}') format('{fmt}'); "
            "font-weight: 400; font-style: normal; font-display: block; "
            "}"
        )
        font_data.append({
            "family": family,
            "file": font_path.name,
            "step": index + 1,
            "label": f"Step {index + 1:02d}",
        })

    items_json = json.dumps(items, ensure_ascii=False)
    fonts_json = json.dumps(font_data, ensure_ascii=False)
    default_text_json = json.dumps(default_text, ensure_ascii=False)
    font_faces_css = "\n".join(font_faces)

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A 到 B 字体演变滑杆预览</title>
<style>
{font_faces_css}
* {{
  box-sizing: border-box;
}}
.page,
.panel,
.layout,
.layout > *,
.stage,
.morph-stack,
.slider-wrap,
.controls,
.compare,
.grid-preview,
.char-list,
textarea {{
  min-width: 0;
  max-width: 100%;
}}
body {{
  margin: 0;
  font-family: Arial, "Microsoft YaHei", sans-serif;
  background: #f4f6f8;
  color: #172033;
}}
.header {{
  padding: 24px 28px 20px;
  background: #ffffff;
  border-bottom: 1px solid #d8dee8;
}}
.header h1 {{
  margin: 0 0 6px;
  font-size: 24px;
  letter-spacing: 0;
}}
.header p {{
  margin: 0;
  color: #64748b;
  line-height: 1.65;
}}
.page {{
  max-width: 1240px;
  margin: 0 auto;
  padding: 22px;
}}
.panel {{
  background: #ffffff;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  padding: 18px;
  margin-bottom: 18px;
}}
.layout {{
  display: grid;
  grid-template-columns: 250px minmax(0, 1fr);
  gap: 18px;
}}
.char-list {{
  max-height: 690px;
  overflow: auto;
  padding-right: 8px;
}}
.char-btn {{
  width: 100%;
  min-height: 38px;
  margin-bottom: 7px;
  padding: 8px 10px;
  border: 1px solid #d8dee8;
  border-radius: 6px;
  background: #ffffff;
  color: #172033;
  text-align: left;
  cursor: pointer;
}}
.char-btn.active {{
  border-color: #2563eb;
  background: #eff6ff;
  color: #1d4ed8;
  font-weight: 700;
}}
.preview-meta {{
  display: flex;
  flex-wrap: wrap;
  gap: 10px 18px;
  align-items: center;
  margin-bottom: 12px;
  color: #475569;
  font-size: 13px;
}}
.stage {{
  min-height: 430px;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  background: #f8fafc;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  padding: 22px;
}}
.morph-stack {{
  position: relative;
  width: 100%;
  min-height: 330px;
  display: grid;
  place-items: center;
}}
.morph-layer {{
  position: absolute;
  inset: 0;
  width: 100%;
  max-width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  font-size: clamp(76px, 14vw, 180px);
  line-height: 1.12;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  transition: opacity 70ms linear;
  font-feature-settings: "liga" 1, "calt" 1;
}}
.morph-stack.vertical .morph-layer {{
  writing-mode: vertical-lr;
  text-orientation: mixed;
  font-size: clamp(58px, 10vw, 128px);
}}
.slider-wrap {{
  margin-top: 14px;
}}
input[type="range"] {{
  width: 100%;
}}
.range-labels {{
  display: flex;
  justify-content: space-between;
  color: #64748b;
  font-size: 12px;
  margin-top: 4px;
}}
.controls {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}}
button {{
  min-height: 36px;
  padding: 8px 12px;
  border: 1px solid #2563eb;
  border-radius: 6px;
  background: #2563eb;
  color: #ffffff;
  font-weight: 700;
  cursor: pointer;
}}
button.secondary {{
  border-color: #cbd5e1;
  background: #ffffff;
  color: #172033;
}}
textarea {{
  width: 100%;
  min-height: 72px;
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  resize: vertical;
  font: inherit;
}}
.compare {{
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-top: 14px;
}}
.compare-card {{
  min-height: 170px;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  background: #ffffff;
  padding: 12px;
  text-align: center;
}}
.compare-label {{
  color: #64748b;
  font-size: 12px;
  margin-bottom: 8px;
}}
.compare-glyph {{
  min-height: 112px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 78px;
  line-height: 1;
  overflow: hidden;
  white-space: pre-wrap;
}}
.grid-preview {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(76px, 1fr));
  gap: 8px;
}}
.grid-cell {{
  min-height: 86px;
  border: 1px solid #d8dee8;
  border-radius: 6px;
  background: #ffffff;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 8px;
  overflow: hidden;
}}
.grid-char {{
  font-size: 34px;
  line-height: 1;
}}
.grid-code {{
  margin-top: 6px;
  color: #64748b;
  font-family: Consolas, monospace;
  font-size: 10px;
}}
@media (max-width: 820px) {{
  .page {{
    padding: 12px;
  }}
  .layout,
  .compare {{
    grid-template-columns: minmax(0, 1fr);
  }}
  .char-list {{
    max-height: 230px;
  }}
  .stage {{
    min-height: 340px;
  }}
  button {{
    width: 100%;
  }}
}}
</style>
</head>
<body>
<!-- step-blend-variable-preview-v2 -->
<div class="header">
  <h1>A 到 B 字体演变滑杆预览</h1>
  <p>这里使用本次真正生成的 Step TTF 作为预览源，滑杆从靠近字体 A 的起点过渡到靠近字体 B 的终点，避免 SVG 点位硬插值造成的怪异扭曲。</p>
</div>

<div class="page">
  <div class="panel layout">
    <div>
      <b>选择字符</b>
      <div style="height:10px;"></div>
      <div class="char-list" id="charList"></div>
    </div>

    <div>
      <div class="preview-meta">
        <span>当前字符：<b id="currentChar"></b></span>
        <span>Unicode：<b id="currentCode"></b></span>
        <span>演变位置：<b id="percentValue">0%</b></span>
        <span>相邻 Step：<b id="stepValue">Step 01</b></span>
      </div>

      <div class="stage">
        <div id="morphStack" class="morph-stack">
          <div id="leftLayer" class="morph-layer"></div>
          <div id="rightLayer" class="morph-layer"></div>
        </div>
      </div>

      <div class="slider-wrap">
        <input id="slider" type="range" min="0" max="100" value="0" step="0.1">
        <div class="range-labels">
          <span>字体 A / Step 01</span>
          <span>字体 B / Step {steps:02d}</span>
        </div>
      </div>

      <div class="controls">
        <button id="playBtn" type="button">播放演变</button>
        <button id="startBtnPreview" class="secondary" type="button">回到 A</button>
        <button id="middleBtn" class="secondary" type="button">中间</button>
        <button id="endBtn" class="secondary" type="button">到 B</button>
        <button id="verticalBtn" class="secondary" type="button">切换竖排</button>
      </div>

      <textarea id="sampleText">{_preview_html.escape(default_text)}</textarea>

      <div class="compare">
        <div class="compare-card">
          <div class="compare-label">起点：靠近字体 A</div>
          <div id="startGlyph" class="compare-glyph"></div>
        </div>
        <div class="compare-card">
          <div class="compare-label">当前演变位置</div>
          <div id="currentGlyph" class="compare-glyph"></div>
        </div>
        <div class="compare-card">
          <div class="compare-label">终点：靠近字体 B</div>
          <div id="endGlyph" class="compare-glyph"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="panel">
    <b>当前 Step 字符总览</b>
    <div style="height:12px;"></div>
    <div id="gridPreview" class="grid-preview"></div>
  </div>
</div>

<script>
const JOB_ID = {json.dumps(job_id)};
const ITEMS = {items_json};
const FONTS = {fonts_json};
const DEFAULT_TEXT = {default_text_json};
const STEP_COUNT = FONTS.length;

let currentIndex = 0;
let percent = 0;
let playing = false;
let direction = 1;
let rafId = 0;

const charList = document.getElementById("charList");
const currentChar = document.getElementById("currentChar");
const currentCode = document.getElementById("currentCode");
const percentValue = document.getElementById("percentValue");
const stepValue = document.getElementById("stepValue");
const slider = document.getElementById("slider");
const leftLayer = document.getElementById("leftLayer");
const rightLayer = document.getElementById("rightLayer");
const morphStack = document.getElementById("morphStack");
const sampleText = document.getElementById("sampleText");
const startGlyph = document.getElementById("startGlyph");
const currentGlyph = document.getElementById("currentGlyph");
const endGlyph = document.getElementById("endGlyph");
const gridPreview = document.getElementById("gridPreview");
const playBtn = document.getElementById("playBtn");

function clamp(value, min, max) {{
  return Math.max(min, Math.min(max, value));
}}

function frameInfo(value) {{
  const raw = STEP_COUNT <= 1 ? 0 : (value / 100) * (STEP_COUNT - 1);
  const left = clamp(Math.floor(raw), 0, STEP_COUNT - 1);
  const right = clamp(Math.ceil(raw), 0, STEP_COUNT - 1);
  const mix = right === left ? 0 : raw - left;
  return {{ left, right, mix, raw }};
}}

function selectedItem() {{
  return ITEMS[currentIndex] || ITEMS[0] || {{ char: "", code: "" }};
}}

function selectedText() {{
  const text = sampleText.value || selectedItem().char || DEFAULT_TEXT;
  return text || DEFAULT_TEXT;
}}

function applyFont(el, index) {{
  const font = FONTS[clamp(index, 0, STEP_COUNT - 1)];
  if (!font) return;
  el.style.fontFamily = "'" + font.family + "', sans-serif";
}}

function renderCharList() {{
  charList.innerHTML = "";
  ITEMS.forEach((item, index) => {{
    const button = document.createElement("button");
    button.type = "button";
    button.className = "char-btn" + (index === currentIndex ? " active" : "");
    button.textContent = item.char + "  " + item.code;
    button.addEventListener("click", () => {{
      currentIndex = index;
      sampleText.value = item.char;
      renderCharList();
      updateAll(true);
    }});
    charList.appendChild(button);
  }});
}}

function updateMain() {{
  const info = frameInfo(percent);
  const text = selectedText();
  const item = selectedItem();

  currentChar.textContent = item.char || text.slice(0, 1);
  currentCode.textContent = item.code || "";
  percentValue.textContent = Math.round(percent) + "%";

  const leftLabel = FONTS[info.left] ? FONTS[info.left].label : "Step 01";
  const rightLabel = FONTS[info.right] ? FONTS[info.right].label : leftLabel;
  stepValue.textContent = leftLabel === rightLabel
    ? leftLabel
    : leftLabel + " -> " + rightLabel;

  leftLayer.textContent = text;
  rightLayer.textContent = text;
  currentGlyph.textContent = text;

  applyFont(leftLayer, info.left);
  applyFont(rightLayer, info.right);
  applyFont(currentGlyph, info.mix < 0.5 ? info.left : info.right);

  leftLayer.style.opacity = String(1 - info.mix);
  rightLayer.style.opacity = String(info.mix);
}}

function updateEndpoints() {{
  const text = selectedText();
  startGlyph.textContent = text;
  endGlyph.textContent = text;
  applyFont(startGlyph, 0);
  applyFont(endGlyph, STEP_COUNT - 1);
}}

function updateGrid() {{
  gridPreview.innerHTML = "";
  const info = frameInfo(percent);
  const fontIndex = info.mix < 0.5 ? info.left : info.right;

  ITEMS.slice(0, 180).forEach(item => {{
    const cell = document.createElement("div");
    cell.className = "grid-cell";

    const ch = document.createElement("div");
    ch.className = "grid-char";
    ch.textContent = item.char;
    applyFont(ch, fontIndex);

    const code = document.createElement("div");
    code.className = "grid-code";
    code.textContent = item.code;

    cell.appendChild(ch);
    cell.appendChild(code);
    gridPreview.appendChild(cell);
  }});
}}

function updateAll(refreshGrid) {{
  slider.value = String(percent);
  updateMain();
  updateEndpoints();
  if (refreshGrid) updateGrid();
}}

function setPercent(value, refreshGrid) {{
  percent = clamp(value, 0, 100);
  updateAll(refreshGrid);
}}

slider.addEventListener("input", () => {{
  setPercent(parseFloat(slider.value), false);
}});

slider.addEventListener("change", () => {{
  updateGrid();
}});

sampleText.addEventListener("input", () => {{
  updateAll(false);
}});

document.getElementById("startBtnPreview").addEventListener("click", () => setPercent(0, true));
document.getElementById("middleBtn").addEventListener("click", () => setPercent(50, true));
document.getElementById("endBtn").addEventListener("click", () => setPercent(100, true));
document.getElementById("verticalBtn").addEventListener("click", () => {{
  morphStack.classList.toggle("vertical");
}});

function animate() {{
  if (playing) {{
    percent += direction * 0.42;
    if (percent >= 100) {{
      percent = 100;
      direction = -1;
    }}
    if (percent <= 0) {{
      percent = 0;
      direction = 1;
    }}
    updateAll(false);
  }}
  rafId = requestAnimationFrame(animate);
}}

playBtn.addEventListener("click", () => {{
  playing = !playing;
  playBtn.textContent = playing ? "暂停" : "播放演变";
}});

renderCharList();
updateAll(true);
animate();
</script>
</body>
</html>
"""
    html_path.write_text(html, encoding="utf-8")
    return html_path

def update_job(job_id, **kwargs):
    if job_id not in JOBS:
        JOBS[job_id] = {}
    JOBS[job_id].update(kwargs)

def run_job(job_id, font_a_path, font_b_path, chars, steps, points, mode, family_name, style_mode, variable_font_mode):
    job_dir = JOBS_DIR / job_id
    svg_root = job_dir / "svg"
    fonts_dir = job_dir / "fonts"
    reports_dir = job_dir / "reports"

    svg_root.mkdir(parents=True, exist_ok=True)
    fonts_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = job_dir / "manifest.csv"
    report_path = job_dir / "report.txt"
    chars_file = job_dir / "chars_codepoints.txt"

    try:
        update_job(job_id, status="running", message="正在读取字体", progress=1)

        tt_a, cmap_a = get_font_cmap(font_a_path)
        tt_b, cmap_b = get_font_cmap(font_b_path)

        codepoints = [ord(ch) for ch in chars]
        total = len(codepoints)

        chars_file.write_text("\n".join("%04X" % cp for cp in codepoints), encoding="utf-8")

        manifest_rows = []
        report_lines = []
        ok_cps = []

        for idx, cp in enumerate(codepoints, start=1):
            ch = chr(cp)
            code = "U%04X" % cp

            progress = int(5 + idx / max(total, 1) * 70)
            update_job(job_id, message=f"正在处理 {code} ({idx}/{total})", progress=progress)

            glyph_a = cmap_a.get(cp)
            glyph_b = cmap_b.get(cp)

            if glyph_a is None or glyph_b is None:
                status = "MISSING"
                reason = []
                if glyph_a is None:
                    reason.append("fontA_missing")
                if glyph_b is None:
                    reason.append("fontB_missing")
                manifest_rows.append([ch, code, glyph_a or "", glyph_b or "", status, ";".join(reason)])
                report_lines.append(f"[MISSING] {ch} {code}: {';'.join(reason)}")
                continue

            try:
                contours_a = get_sampled_contours(tt_a, glyph_a, points)
                contours_b = get_sampled_contours(tt_b, glyph_b, points)

                matched_a, matched_b = match_contours(contours_a, contours_b, points, mode)

                char_dir = svg_root / code
                char_dir.mkdir(parents=True, exist_ok=True)

                for step in range(1, steps + 1):
                    alpha = step / (steps + 1)
                    blended = interpolate_contours(matched_a, matched_b, alpha)
                    contours_to_svg(blended, char_dir / f"{code}_step_{step:02d}.svg")

                ok_cps.append(cp)
                manifest_rows.append([
                    ch, code, glyph_a, glyph_b, "OK",
                    f"contoursA={len(contours_a)};contoursB={len(contours_b)};forced={len(matched_a)}"
                ])
                report_lines.append(f"[OK] {ch} {code}: {glyph_a} -> {glyph_b}, generated={steps}")

            except Exception as e:
                manifest_rows.append([ch, code, glyph_a or "", glyph_b or "", "FAIL", str(e)])
                report_lines.append(f"[FAIL] {ch} {code}: {e}")

        with open(manifest_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["char", "codepoint", "glyph_A", "glyph_B", "status", "note"])
            writer.writerows(manifest_rows)

        report_path.write_text("\n".join(report_lines), encoding="utf-8")

        ok_chars_file = job_dir / "ok_chars_codepoints.txt"
        ok_chars_file.write_text("\n".join("%04X" % cp for cp in ok_cps), encoding="utf-8")

        if len(ok_cps) == 0:
            raise RuntimeError("没有任何双方都支持并成功生成的字符。请检查字体是否支持所选字符集。")

        update_job(job_id, message="正在用 FontForge 生成 TTF 字体", progress=80)

        # 字体家族名称与样式模式
        family_name = (family_name or "FontMorphFamily").strip()
        if not family_name:
            family_name = "FontMorphFamily"

        if style_mode not in ["morph", "step", "weight"]:
            style_mode = "morph"

        cmd = [
            "fontforge",
            "-script",
            str(FONTFORGE_SCRIPT),
            str(svg_root),
            str(fonts_dir),
            str(ok_chars_file),
            str(steps),
            family_name,
            style_mode
        ]

        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        (job_dir / "fontforge.log").write_text(proc.stdout, encoding="utf-8")

        if proc.returncode != 0:
            raise RuntimeError("FontForge 生成失败，请查看 fontforge.log")

        update_job(job_id, message="正在生成预览页和压缩包", progress=92)

        ok_chars = "".join(chr(cp) for cp in ok_cps)
        make_preview_html(job_dir, ok_chars, steps)
        make_family_preview_html(job_dir, ok_chars, steps)
        make_step_blend_variable_preview_html(job_dir, ok_chars, steps)

        # 字体家族说明
        family_info = {
            "family_name": family_name,
            "style_mode": style_mode,
            "steps": steps,
            "success_chars": len(ok_cps),
            "note": "本版本生成的是 TTF 字体家族包。每一个 step 是同一字体家族下的一个样式。"
        }
        (job_dir / "font_family_info.json").write_text(
            json.dumps(family_info, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # 可变字体说明：先不生成正式 VF，避免误导
        if variable_font_mode != "off":
            (job_dir / "variable_font_experimental_note.txt").write_text(
                "Variable Font 实验说明\n"
                "当前任务已生成 TTF 字体家族。\n"
                "标准 Variable Font 要求两个 master 的每个 glyph 轮廓数量、点数、点序完全兼容。\n"
                "当前系统采用强制轮廓补齐与 SVG/TTF 构建流程，适合生成普通 TTF 字体家族，但不能保证直接生成标准可变字体。\n"
                "下一阶段可增加：兼容 glyph 检测、UFO master 构建、designspace 生成、fontmake/varLib 构建 VF.ttf。\n",
                encoding="utf-8"
            )

        zip_dir(svg_root, job_dir / "svg.zip")
        zip_dir(fonts_dir, job_dir / "ttf.zip")

        all_zip = job_dir / "all_results.zip"
        if all_zip.exists():
            all_zip.unlink()

        with zipfile.ZipFile(all_zip, "w", zipfile.ZIP_DEFLATED) as z:
            extra_files = [
                manifest_path,
                report_path,
                job_dir / "fontforge.log",
                job_dir / "preview.html",
                job_dir / "family_preview.html",
                job_dir / "variable_preview.html",
                job_dir / "font_family_info.json",
                job_dir / "variable_font_experimental_note.txt",
            ]
            for p in extra_files:
                if p.exists():
                    z.write(p, p.relative_to(job_dir))
            for folder in [svg_root, fonts_dir]:
                for p in folder.rglob("*"):
                    if p.is_file():
                        z.write(p, p.relative_to(job_dir))

        update_job(
            job_id,
            status="done",
            message="完成",
            progress=100,
            total_chars=total,
            success_chars=len(ok_cps),
            missing_or_failed=total - len(ok_cps),
            preview={
                "svg": f"/preview/{job_id}",
                "family": f"/family_preview/{job_id}",
                "variable": f"/variable_preview/{job_id}",
            },
            downloads={
                "svg": f"/download/{job_id}/svg",
                "ttf": f"/download/{job_id}/ttf",
                "all": f"/download/{job_id}/all",
                "manifest": f"/download/{job_id}/manifest",
                "report": f"/download/{job_id}/report",
            },
            real_variable_action="/generate_real_variable_font",
        )

    except Exception as e:
        update_job(job_id, status="error", message=str(e), progress=100)

# -----------------------------
# 网页
# -----------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>字体中间变化生成工具</title>
<style>
* {
  box-sizing: border-box;
}

:root {
  --bg: #eef2f7;
  --surface: #ffffff;
  --surface-soft: #f8fafc;
  --text: #0f172a;
  --muted: #64748b;
  --line: #d8dee8;
  --accent: #2563eb;
  --accent-dark: #1d4ed8;
  --ok: #0f766e;
  --warn-bg: #fff8e6;
  --warn-line: #f0d48a;
  --warn-text: #7a4b00;
  --shadow: 0 18px 45px rgba(15, 23, 42, .10);
}

body {
  margin: 0;
  min-height: 100vh;
  font-family: Arial, "Microsoft YaHei", sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 15px;
  line-height: 1.55;
}

.header {
  display: grid;
  gap: 14px;
  background:
    linear-gradient(180deg, #ffffff 0%, #f7f9fc 100%);
  color: var(--text);
  padding: 30px max(24px, calc((100vw - 1120px) / 2)) 26px;
  border-bottom: 1px solid var(--line);
}

.header h1 {
  order: 1;
  margin: 0;
  font-size: 30px;
  line-height: 1.18;
  font-weight: 800;
  letter-spacing: 0;
}

body > .header > div:not(#auto-selected-ttf-panel):not(#real-vf-panel) {
  order: 2;
  max-width: 760px;
  color: var(--muted);
  font-size: 15px;
}

.container {
  max-width: 1040px;
  margin: 28px auto 56px;
  background: var(--surface);
  padding: 30px;
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: var(--shadow);
}

.row {
  margin-bottom: 20px;
}

label {
  display: block;
  font-weight: 700;
  margin-bottom: 8px;
  color: var(--text);
}

input, select, textarea {
  width: 100%;
  padding: 11px 12px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  background: #ffffff;
  color: var(--text);
  font: inherit;
  transition: border-color .16s ease, box-shadow .16s ease, background .16s ease;
}

input:hover, select:hover, textarea:hover {
  border-color: #94a3b8;
}

input:focus, select:focus, textarea:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(37, 99, 235, .14);
}

textarea {
  min-height: 104px;
  resize: vertical;
}

.row.is-muted textarea {
  background: #f8fafc;
  color: #64748b;
}

.row.is-muted .note {
  color: #475569;
}

button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 42px;
  background: var(--accent);
  color: white;
  border: 0;
  padding: 10px 18px;
  border-radius: 6px;
  cursor: pointer;
  font: inherit;
  font-weight: 700;
  white-space: nowrap;
  transition: background .16s ease, transform .16s ease, box-shadow .16s ease;
}

button:hover {
  background: var(--accent-dark);
  box-shadow: 0 8px 18px rgba(37, 99, 235, .22);
  transform: translateY(-1px);
}

button:disabled {
  background: #94a3b8;
  box-shadow: none;
  cursor: not-allowed;
  transform: none;
}

.note {
  margin-top: 7px;
  color: var(--muted);
  font-size: 13px;
}

hr {
  border: 0;
  border-top: 1px solid var(--line);
  margin: 26px 0;
}

.progress {
  width: 100%;
  height: 12px;
  background: #e2e8f0;
  border-radius: 20px;
  overflow: hidden;
}

.bar {
  height: 100%;
  width: 0%;
  background: linear-gradient(90deg, var(--accent), var(--ok));
}

.result a,
.result button {
  display: inline-flex;
  align-items: center;
  min-height: 34px;
  margin: 10px 8px 0 0;
  padding: 6px 10px;
  border: 1px solid #bfdbfe;
  border-radius: 6px;
  background: #eff6ff;
  color: #1d4ed8;
  text-decoration: none;
  font-size: 13px;
  font-weight: 700;
}

.result button {
  border-color: #0f172a;
  background: #0f172a;
  color: #ffffff;
  cursor: pointer;
}

.warning {
  background: var(--warn-bg);
  border: 1px solid var(--warn-line);
  padding: 13px 14px;
  border-radius: 8px;
  color: var(--warn-text);
  margin-bottom: 22px;
}

#statusBox {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
  padding: 18px;
}

#statusBox h3 {
  margin: 0 0 12px;
}

.upper-result-dock {
  margin-top: 24px;
  padding-top: 22px;
  border-top: 1px solid var(--line);
}

.upper-result-dock:empty {
  display: none;
}

.upper-result-panel {
  margin-top: 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #ffffff;
  overflow: hidden;
}

.upper-result-head,
.upper-result-body {
  padding: 18px;
}

.upper-result-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  border-bottom: 1px solid var(--line);
  background: var(--surface-soft);
}

.upper-result-title {
  margin: 0 0 4px;
  font-size: 18px;
}

.upper-result-meta {
  color: var(--muted);
  font-size: 13px;
}

.upper-result-actions,
.upper-step-toolbar,
.upper-preview-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.upper-result-actions a,
.upper-preview-tabs button,
.upper-step-toolbar button,
.upper-download-selected,
.upper-real-vf-btn {
  min-height: 36px;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 700;
  text-decoration: none;
}

.upper-result-actions a {
  border: 1px solid #bfdbfe;
  background: #eff6ff;
  color: #1d4ed8;
}

.upper-result-actions a.upper-dark-action {
  border-color: #0f172a;
  background: #0f172a;
  color: #ffffff;
}

.upper-real-vf-btn {
  width: auto;
  margin-left: 8px;
  border: 1px solid #0f172a;
  background: #0f172a;
  color: #ffffff;
}

.upper-preview-tabs {
  margin-bottom: 10px;
}

.upper-preview-tabs button {
  border: 1px solid #cbd5e1;
  background: #ffffff;
  color: var(--text);
  box-shadow: none;
}

.upper-preview-tabs button.is-active {
  border-color: var(--accent);
  background: var(--accent);
  color: #ffffff;
}

.upper-preview-frame-wrap {
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: #ffffff;
}

.upper-preview-frame {
  display: block;
  width: 100%;
  height: 520px;
  border: 0;
  background: #ffffff;
}

.upper-step-section {
  margin-top: 18px;
}

.upper-step-topline {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.upper-step-title {
  margin: 0;
  font-size: 16px;
}

.upper-step-toolbar button {
  min-height: 34px;
  border: 1px solid #cbd5e1;
  background: #ffffff;
  color: var(--text);
  box-shadow: none;
}

.upper-step-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(142px, 1fr));
  gap: 8px;
  margin: 10px 0 14px;
}

.upper-step-item {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 48px;
  padding: 9px 10px;
  border: 1px solid #d8dee8;
  border-radius: 6px;
  background: #ffffff;
  cursor: pointer;
}

.upper-step-item:hover {
  border-color: #94a3b8;
  background: #f8fafc;
}

.upper-step-item input {
  width: auto;
  margin: 0;
}

.upper-step-name {
  display: block;
  font-weight: 800;
  line-height: 1.2;
}

.upper-step-file {
  display: block;
  margin-top: 2px;
  color: var(--muted);
  font-size: 12px;
  word-break: break-all;
}

.upper-download-selected {
  width: auto;
  background: var(--accent);
}

.upper-result-empty {
  padding: 14px;
  border: 1px dashed #cbd5e1;
  border-radius: 8px;
  color: var(--muted);
  background: #f8fafc;
}

#gb-real-variable-entry-v1,
#gb-variable-home-button-v2 {
  border-radius: 8px !important;
  border-color: var(--line) !important;
  background: var(--surface-soft) !important;
  box-shadow: none !important;
}

#gb-real-variable-entry-v1 .gb-var-actions {
  gap: 8px !important;
}

#gb-real-variable-entry-v1 .gb-var-button,
#gb-variable-home-button-v2 a {
  border-radius: 6px !important;
}

#previewLinksFloatingBox {
  right: 18px !important;
  bottom: 18px !important;
  border-radius: 8px !important;
  background: rgba(15, 23, 42, .94) !important;
  box-shadow: 0 12px 30px rgba(15, 23, 42, .25) !important;
}

#realResultPreviewLinkBox,
a[href*="/api/unicode/real_preview/"]:not(#singleRealPreviewEntry) {
  display: none !important;
}

@media (max-width: 760px) {
  .header {
    padding: 24px 16px 22px;
  }

  .header h1 {
    font-size: 25px;
  }

  .container {
    margin: 18px 12px 72px;
    padding: 18px;
  }

  button {
    width: 100%;
  }

  .upper-result-head,
  .upper-step-topline {
    display: block;
  }

  .upper-result-actions,
  .upper-step-toolbar {
    margin-top: 12px;
  }

  .upper-preview-frame {
    height: 440px;
  }

  #previewLinksFloatingBox {
    position: static !important;
    left: 12px !important;
    right: 12px !important;
    bottom: auto !important;
    width: auto !important;
    margin: 16px 12px 24px !important;
    text-align: center !important;
  }
}
</style>
</head>
<body>
<div class="header">
  <h1>字体中间变化生成工具</h1>
  <div>上传两个字体，选择文种和步数，自动生成 SVG 与 TTF。</div>
</div>

<div class="container">
  <div class="warning">
    建议先用英文、蒙古文或自定义少量字符测试。中文6500字 × 多步数会生成大量 SVG，耗时较长。
  </div>

  <form id="form">
    <div class="row">
      <label>字体 A（.ttf / .otf）</label>
      <input type="file" name="font_a" accept=".ttf,.otf" required>
    </div>

    <div class="row">
      <label>字体 B（.ttf / .otf）</label>
      <input type="file" name="font_b" accept=".ttf,.otf" required>
    </div>

    <div class="row">
      <label>选择文种 / 字符集</label>
      <select name="preset">
        <option value="mongolian_basic_35">传统蒙古文35个</option>
        <option value="english_basic">英文 A-Z / a-z</option>
        <option value="chinese_6500">中文6500字</option>
        <option value="japanese_kana">日文假名：平假名 + 片假名</option>
        <option value="korean_basic">韩文基础：Jamo + 部分常用音节</option>
        <option value="russian_basic">俄文基础：西里尔字母</option>
        <option value="german_basic">德文基础：äöüß + 德语字母</option>
        <option value="custom">自定义输入字符</option>
      </select>
    </div>

    <div class="row" id="customCharsetRow">
      <label>自定义字符</label>
      <textarea name="custom_text" placeholder="选择“自定义输入字符”时，在这里输入要处理的文字。也可以先用少量中文、日文或韩文测试。"></textarea>
      <div class="note" id="customCharsetNote">当前选择已有内置字符集，不需要在这里输入。</div>
    </div>

    <div class="row">
      <label>中间步数</label>
      <input type="number" name="steps" value="20" min="1" max="100">
    </div>

    <div class="row">
      <label>采样点数</label>
      <input type="number" name="points" value="120" min="40" max="300">
      <div class="note">数值越大越精细，但速度越慢。建议 100-160。</div>
    </div>

    <div class="row">
      <label>匹配模式</label>
      <select name="mode">
        <option value="force">强制模式：轮廓数量不一致也生成</option>
        <option value="strict">严格模式：轮廓数量不一致则跳过</option>
      </select>
    </div>

    
    <div class="row">
      <label>字体家族名称</label>
      <input type="text" name="family_name" value="FontMorphFamily" placeholder="例如 FontMorph_AB / MongolianBlend / ChineseMorph">
      <div class="note">生成的 TTF 会归入这个字体家族名称，便于在设计软件中作为同一组字体使用。</div>
    </div>

    <div class="row">
      <label>字体家族样式命名方式</label>
      <select name="style_mode">
        <option value="morph">Morph 005 / Morph 010 / Morph 015 ...</option>
        <option value="step">Step 01 / Step 02 / Step 03 ...</option>
        <option value="weight">Weight 100 / 200 / 300 ... 900</option>
      </select>
      <div class="note">
        推荐选择 Morph。Weight 模式会伪装成字重系列，但你的变化不一定是单纯粗细变化。
      </div>
    </div>

    <div class="row">
      <label>可变字体</label>
      <select name="variable_font_mode">
        <option value="off">生成可变字体文件（Variable Font）</option>
        <option value="placeholder">生成实验说明文件，不生成正式 Variable Font</option>
      </select>
      <div class="note">
        标准可变字体要求两个字体的轮廓数量、点数、点序完全兼容。当前版本先完成字体家族生成，生成 Variable Font开放。
      </div>
    </div>

    <button id="startBtn" type="submit">开始生成</button>

    <div id="upperResultDock" class="upper-result-dock"></div>

  </form>

  <hr>

  <div id="statusBox" style="display:none;">
    <h3>生成状态</h3>
    <div class="progress"><div class="bar" id="bar"></div></div>
    <p id="message"></p>
    <p id="summary"></p>
    <div class="result" id="links"></div>
  </div>
</div>

<script>
const form = document.getElementById("form");
const startBtn = document.getElementById("startBtn");
const statusBox = document.getElementById("statusBox");
const bar = document.getElementById("bar");
const message = document.getElementById("message");
const summary = document.getElementById("summary");
const links = document.getElementById("links");
const presetSelect = form.querySelector('select[name="preset"]');
const customText = form.querySelector('textarea[name="custom_text"]');
const customCharsetRow = document.getElementById("customCharsetRow");
const customCharsetNote = document.getElementById("customCharsetNote");

let timer = null;

function updateCustomCharsetState() {
  const isCustom = presetSelect && presetSelect.value === "custom";

  if (customText) {
    customText.disabled = !isCustom;
    customText.placeholder = isCustom
      ? "在这里输入要处理的文字，会自动去重。"
      : "当前选择已有内置字符集，不需要输入。";
  }

  if (customCharsetRow) {
    customCharsetRow.classList.toggle("is-muted", !isCustom);
  }

  if (customCharsetNote) {
    customCharsetNote.textContent = isCustom
      ? "只在选择“自定义输入字符”时使用这里的内容。"
      : "当前选择已有内置字符集，不需要在这里输入。";
  }
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function firstLowerMongolianPanel() {
  const ids = [
    "foundryRulesPanel",
    "topFoundryPanel",
    "gb-variable-home-button-v2",
    "gb-real-variable-entry-v1"
  ];

  return ids
    .map(id => document.getElementById(id))
    .find(el => el && el.parentElement === form && getComputedStyle(el).display !== "none") || null;
}

function ensureUpperResultDock() {
  let dock = document.getElementById("upperResultDock");

  if (!dock) {
    dock = document.createElement("div");
    dock.id = "upperResultDock";
    dock.className = "upper-result-dock";
  }

  if (dock.parentElement !== form) {
    form.appendChild(dock);
  }

  const lowerPanel = firstLowerMongolianPanel();

  if (lowerPanel && lowerPanel.parentElement === form && lowerPanel.previousElementSibling !== dock) {
    form.insertBefore(dock, lowerPanel);
  } else if (!lowerPanel && startBtn.nextSibling !== dock) {
    form.insertBefore(dock, startBtn.nextSibling);
  }

  return dock;
}

function placeStatusBox() {
  const dock = ensureUpperResultDock();

  if (statusBox.parentElement !== dock) {
    dock.appendChild(statusBox);
  }

  return dock;
}

function clearUpperResultReview() {
  const review = document.getElementById("upperResultReview");
  if (review) {
    review.remove();
  }
}

let upperLegacyOutputObserver = null;
let upperLegacyOutputQueued = false;

function normalizeUpperLegacyOutputs() {
  const dock = ensureUpperResultDock();
  const bridgePanel = document.getElementById("unicodeBridgePanel");

  if (bridgePanel) {
    bridgePanel.classList.add("upper-legacy-panel");

    if (bridgePanel.parentElement !== dock) {
      dock.appendChild(bridgePanel);
    }
  }

  document.querySelectorAll('a[href*="/api/unicode/real_preview/"]').forEach(link => {
    if (link.id === "singleRealPreviewEntry") {
      return;
    }

    link.style.display = "none";
    link.setAttribute("aria-hidden", "true");
    link.setAttribute("tabindex", "-1");
  });
}

function queueNormalizeUpperLegacyOutputs() {
  if (upperLegacyOutputQueued) {
    return;
  }

  upperLegacyOutputQueued = true;
  requestAnimationFrame(() => {
    upperLegacyOutputQueued = false;
    normalizeUpperLegacyOutputs();
  });
}

function startUpperLegacyOutputWatcher() {
  normalizeUpperLegacyOutputs();

  if (upperLegacyOutputObserver || !document.body) {
    return;
  }

  upperLegacyOutputObserver = new MutationObserver(queueNormalizeUpperLegacyOutputs);
  upperLegacyOutputObserver.observe(document.body, {
    childList: true,
    subtree: true
  });
}

function stepTitle(item, index) {
  const step = Number(item.step || 0);
  return step > 0 ? "Step " + String(step).padStart(2, "0") : "Step " + String(index + 1).padStart(2, "0");
}

function submitRealVariableFont(fontNames, familyName) {
  const names = Array.from(new Set((fontNames || []).filter(Boolean)));

  if (names.length < 2) {
    alert("真实 VF 诊断至少需要 2 个 Step TTF。");
    return;
  }

  const temp = document.createElement("form");
  temp.method = "post";
  temp.action = "/generate_real_variable_font";
  temp.target = "_blank";
  temp.style.display = "none";

  const nameInput = document.createElement("input");
  nameInput.type = "hidden";
  nameInput.name = "variable_name";
  nameInput.value = familyName || (form.querySelector('input[name="family_name"]')?.value || "Real Morph Variable");
  temp.appendChild(nameInput);

  names.forEach(name => {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "selected_fonts";
    input.value = name;
    temp.appendChild(input);
  });

  document.body.appendChild(temp);
  temp.submit();
  temp.remove();
}

async function submitRealVariableFontForJob(jobId) {
  try {
    const resp = await fetch("/api/job/" + encodeURIComponent(jobId) + "/fonts");
    const data = await resp.json();
    const names = (data.fonts || []).map(font => font.name);
    submitRealVariableFont(names);
  } catch (err) {
    alert("读取 Step TTF 文件失败：" + err);
  }
}

async function renderUpperResultPanel(jobId, data) {
  const dock = placeStatusBox();
  clearUpperResultReview();

  const panel = document.createElement("section");
  panel.id = "upperResultReview";
  panel.className = "upper-result-panel";
  panel.innerHTML = `
    <div class="upper-result-head">
      <div>
        <h3 class="upper-result-title">结果预览与输出选择</h3>
        <div class="upper-result-meta">
          Job：${escapeHtml(jobId)} · 成功 ${escapeHtml(data.success_chars)} / ${escapeHtml(data.total_chars)} · 失败或缺失 ${escapeHtml(data.missing_or_failed)}
        </div>
      </div>
      <div class="upper-result-actions">
        <a href="/preview/${encodeURIComponent(jobId)}" target="_blank">打开 SVG 预览</a>
        <a href="/family_preview/${encodeURIComponent(jobId)}" target="_blank">打开 TTF 预览</a>
        <a href="/variable_preview/${encodeURIComponent(jobId)}" target="_blank">打开滑杆预览</a>
        <a class="upper-dark-action" href="/download/${encodeURIComponent(jobId)}/all" target="_blank">下载全部结果</a>
      </div>
    </div>
    <div class="upper-result-body">
      <div class="upper-preview-tabs">
        <button type="button" class="is-active" data-preview-url="/family_preview/${encodeURIComponent(jobId)}">TTF 家族预览</button>
        <button type="button" data-preview-url="/preview/${encodeURIComponent(jobId)}">SVG 变化预览</button>
        <button type="button" data-preview-url="/variable_preview/${encodeURIComponent(jobId)}">可变滑杆预览</button>
      </div>
      <div class="upper-preview-frame-wrap">
        <iframe id="upperPreviewFrame" class="upper-preview-frame" src="/family_preview/${encodeURIComponent(jobId)}"></iframe>
      </div>
      <div id="upperStepDownloadArea" class="upper-step-section">
        <div class="upper-result-empty">正在读取 step TTF 文件...</div>
      </div>
    </div>
  `;

  dock.appendChild(panel);

  const frame = panel.querySelector("#upperPreviewFrame");

  function cleanEmbeddedPreview() {
    try {
      const doc = frame.contentDocument;

      if (!doc) {
        return;
      }

      [
        "auto-selected-ttf-panel",
        "selected-step-download-panel",
        "real-vf-panel",
        "previewLinksFloatingBox"
      ].forEach(id => {
        const el = doc.getElementById(id);
        if (el) {
          el.remove();
        }
      });

      doc.querySelectorAll(".auto-step-download-mark").forEach(el => el.remove());
    } catch (err) {
      // Same-origin preview pages can be cleaned; if a browser blocks it, the preview still works.
    }
  }

  frame.addEventListener("load", cleanEmbeddedPreview);

  panel.querySelectorAll("[data-preview-url]").forEach(btn => {
    btn.addEventListener("click", () => {
      panel.querySelectorAll("[data-preview-url]").forEach(x => x.classList.remove("is-active"));
      btn.classList.add("is-active");
      frame.src = btn.dataset.previewUrl;
    });
  });

  const area = panel.querySelector("#upperStepDownloadArea");

  try {
    const resp = await fetch("/api/job/" + encodeURIComponent(jobId) + "/fonts");
    const fontData = await resp.json();
    const fonts = fontData.fonts || [];

    if (!fontData.ok || !fonts.length) {
      area.innerHTML = `<div class="upper-result-empty">没有读取到可勾选的 step TTF 文件。仍可使用上方“下载全部结果”。</div>`;
      return;
    }

    area.innerHTML = `
      <div class="upper-step-topline">
        <div>
          <h4 class="upper-step-title">选择要下载的 TTF 步骤</h4>
          <div class="upper-result-meta">检测到 ${fonts.length} 个 step 字体。先在上方预览效果，再取消不满意的步骤。</div>
        </div>
        <div class="upper-step-toolbar">
          <button type="button" data-select-steps="all">全选</button>
          <button type="button" data-select-steps="none">取消全选</button>
          <button type="button" data-select-steps="invert">反选</button>
        </div>
      </div>
      <div class="upper-step-grid">
        ${fonts.map((font, index) => `
          <label class="upper-step-item" title="${escapeHtml(font.name)}">
            <input type="checkbox" data-step-font value="${escapeHtml(font.name)}" checked>
            <span>
              <span class="upper-step-name">${escapeHtml(stepTitle(font, index))}</span>
              <span class="upper-step-file">${escapeHtml(font.name)}</span>
            </span>
          </label>
        `).join("")}
      </div>
      <button type="button" class="upper-download-selected">下载勾选的 TTF 文件</button>
      <button type="button" class="upper-real-vf-btn">生成/诊断真实 VF</button>
    `;

    const checks = () => Array.from(panel.querySelectorAll("input[data-step-font]"));

    panel.querySelector('[data-select-steps="all"]').addEventListener("click", () => {
      checks().forEach(cb => cb.checked = true);
    });

    panel.querySelector('[data-select-steps="none"]').addEventListener("click", () => {
      checks().forEach(cb => cb.checked = false);
    });

    panel.querySelector('[data-select-steps="invert"]').addEventListener("click", () => {
      checks().forEach(cb => cb.checked = !cb.checked);
    });

    panel.querySelector(".upper-download-selected").addEventListener("click", () => {
      const picked = checks().filter(cb => cb.checked).map(cb => cb.value);

      if (!picked.length) {
        alert("请至少选择一个要下载的 TTF 步骤。");
        return;
      }

      const temp = document.createElement("form");
      temp.method = "post";
      temp.action = "/download/" + encodeURIComponent(jobId) + "/selected_ttf";
      temp.target = "_blank";
      temp.style.display = "none";

      picked.forEach(name => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "selected_fonts";
        input.value = name;
        temp.appendChild(input);
      });

      document.body.appendChild(temp);
      temp.submit();
      temp.remove();
    });

    panel.querySelector(".upper-real-vf-btn").addEventListener("click", () => {
      const picked = checks().filter(cb => cb.checked).map(cb => cb.value);
      submitRealVariableFont(picked);
    });
  } catch (err) {
    area.innerHTML = `<div class="upper-result-empty">读取 step TTF 文件失败：${escapeHtml(err)}</div>`;
  }
}

window.__renderUpperResultPanel = renderUpperResultPanel;

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  const dock = placeStatusBox();
  clearUpperResultReview();
  ensureUpperResultDock();

  startBtn.disabled = true;
  statusBox.style.display = "block";
  bar.style.width = "0%";
  message.innerText = "正在上传与创建任务...";
  summary.innerText = "";
  links.innerHTML = "";

  const fd = new FormData(form);

  const resp = await fetch("/api/generate", {
    method: "POST",
    body: fd
  });

  const data = await resp.json();

  if (!data.job_id) {
    message.innerText = "任务创建失败：" + JSON.stringify(data);
    startBtn.disabled = false;
    return;
  }

  poll(data.job_id);
});

async function poll(jobId) {
  if (timer) clearInterval(timer);

  timer = setInterval(async () => {
    const resp = await fetch("/api/status/" + jobId);
    const data = await resp.json();

    bar.style.width = (data.progress || 0) + "%";
    message.innerText = data.message || "";

    if (data.status === "done") {
      clearInterval(timer);
      startBtn.disabled = false;
      summary.innerText = `完成：成功字符 ${data.success_chars} / 总字符 ${data.total_chars}，失败或缺失 ${data.missing_or_failed}`;

      links.innerHTML = `
        <a href="/preview/${jobId}" target="_blank">打开 SVG 预览</a>
        <a href="/family_preview/${jobId}" target="_blank">打开 TTF 预览</a>
        <a href="/variable_preview/${jobId}" target="_blank">打开滑杆预览</a>
        <button type="button" id="statusRealVfBtn">生成/诊断真实 VF</button>
        <a href="/download/${jobId}/svg">下载 SVG 包</a>
        <a href="/download/${jobId}/ttf">下载全部 TTF 包</a>
        <a href="/download/${jobId}/manifest">下载 manifest.csv</a>
        <a href="/download/${jobId}/report">下载 report.txt</a>
      `;

      const statusRealVfBtn = document.getElementById("statusRealVfBtn");
      if (statusRealVfBtn) {
        statusRealVfBtn.addEventListener("click", () => submitRealVariableFontForJob(jobId));
      }

      renderUpperResultPanel(jobId, data);
    }

    if (data.status === "error") {
      clearInterval(timer);
      startBtn.disabled = false;
      summary.innerText = "生成失败";
    }
  }, 2000);
}

if (presetSelect) {
  presetSelect.addEventListener("change", updateCustomCharsetState);
}

document.addEventListener("DOMContentLoaded", updateCustomCharsetState);
document.addEventListener("DOMContentLoaded", ensureUpperResultDock);
document.addEventListener("DOMContentLoaded", startUpperLegacyOutputWatcher);
setTimeout(updateCustomCharsetState, 300);
setTimeout(updateCustomCharsetState, 1000);
setTimeout(updateCustomCharsetState, 2000);
setTimeout(ensureUpperResultDock, 500);
setTimeout(ensureUpperResultDock, 1500);
setTimeout(ensureUpperResultDock, 3000);
setTimeout(startUpperLegacyOutputWatcher, 500);
setTimeout(startUpperLegacyOutputWatcher, 1500);
setTimeout(startUpperLegacyOutputWatcher, 3000);

function openUpperResultFromUrl() {
  const params = new URLSearchParams(window.location.search || "");
  const jobId = params.get("job");

  if (!jobId) {
    return;
  }

  renderUpperResultPanel(jobId, {
    success_chars: "-",
    total_chars: "-",
    missing_or_failed: "-"
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setTimeout(openUpperResultFromUrl, 900);
});
</script>
</body>
</html>
""")

@app.post("/api/generate")
async def generate(
    font_a: UploadFile = File(...),
    font_b: UploadFile = File(...),
    preset: str = Form("mongolian"),
    custom_text: str = Form(""),
    steps: int = Form(20),
    points: int = Form(120),
    mode: str = Form("force"),
    family_name: str = Form("FontMorphFamily"),
    style_mode: str = Form("morph"),
    variable_font_mode: str = Form("off")
):
    job_id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    job_dir = JOBS_DIR / job_id
    upload_dir = job_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    font_a_path = upload_dir / font_a.filename
    font_b_path = upload_dir / font_b.filename

    with open(font_a_path, "wb") as f:
        shutil.copyfileobj(font_a.file, f)

    with open(font_b_path, "wb") as f:
        shutil.copyfileobj(font_b.file, f)

    chars = get_charset(preset, custom_text)
    chars = unique_chars(chars)

    if not chars:
        return JSONResponse({"error": "字符集为空，请选择文种或输入自定义字符。"}, status_code=400)

    if steps < 1:
        steps = 1

    if steps > 100:
        steps = 100

    if points < 40:
        points = 40

    if points > 300:
        points = 300

    if mode not in ["force", "strict"]:
        mode = "force"

    JOBS[job_id] = {
        "status": "queued",
        "message": "任务已创建",
        "progress": 0,
        "total_chars": len(chars),
        "success_chars": 0,
        "missing_or_failed": 0
    }

    thread = threading.Thread(
        target=run_job,
        args=(job_id, str(font_a_path), str(font_b_path), chars, steps, points, mode, family_name, style_mode, variable_font_mode),
        daemon=True
    )
    thread.start()

    return {
        "job_id": job_id,
        "total_chars": len(chars),
        "preset": preset,
        "preview": {
            "svg": f"/preview/{job_id}",
            "family": f"/family_preview/{job_id}",
            "variable": f"/variable_preview/{job_id}",
        },
        "downloads": {
            "svg": f"/download/{job_id}/svg",
            "ttf": f"/download/{job_id}/ttf",
            "all": f"/download/{job_id}/all",
        },
        "real_variable_action": "/generate_real_variable_font",
    }

@app.get("/api/status/{job_id}")
def status(job_id: str):
    return JOBS.get(job_id, {"status": "unknown", "message": "任务不存在", "progress": 0})

@app.get("/download/{job_id}/svg")
def download_svg(job_id: str):
    path = JOBS_DIR / job_id / "svg.zip"
    return FileResponse(path, filename=f"{job_id}_svg.zip")

@app.get("/download/{job_id}/ttf")
def download_ttf(job_id: str):
    path = JOBS_DIR / job_id / "ttf.zip"
    return FileResponse(path, filename=f"{job_id}_ttf.zip")

@app.get("/download/{job_id}/all")
def download_all(job_id: str):
    path = JOBS_DIR / job_id / "all_results.zip"
    return FileResponse(path, filename=f"{job_id}_all_results.zip")

@app.get("/download/{job_id}/manifest")
def download_manifest(job_id: str):
    path = JOBS_DIR / job_id / "manifest.csv"
    return FileResponse(path, filename=f"{job_id}_manifest.csv")

@app.get("/download/{job_id}/report")
def download_report(job_id: str):
    path = JOBS_DIR / job_id / "report.txt"
    return FileResponse(path, filename=f"{job_id}_report.txt")


def _safe_job_dir(job_id: str):
    safe_job_id = Path(str(job_id or "")).name

    if not safe_job_id or safe_job_id != str(job_id):
        return None

    job_dir = JOBS_DIR / safe_job_id

    if not job_dir.exists() or not job_dir.is_dir():
        return None

    return job_dir


def _step_number_from_name(name: str):
    m = re.search(r"step[_\-]?0*(\d+)", str(name), re.I)

    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass

    return 999999


def _job_font_files(job_id: str):
    job_dir = _safe_job_dir(job_id)

    if not job_dir:
        return []

    fonts_dir = job_dir / "fonts"

    if not fonts_dir.exists() or not fonts_dir.is_dir():
        return []

    files = [
        p for p in fonts_dir.iterdir()
        if p.is_file() and p.suffix.lower() in [".ttf", ".otf"]
    ]

    files.sort(key=lambda p: (_step_number_from_name(p.name), p.name.lower()))
    return files


@app.get("/api/job/{job_id}/fonts")
def job_fonts(job_id: str):
    job_dir = _safe_job_dir(job_id)

    if not job_dir:
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)

    fonts = []

    for p in _job_font_files(job_id):
        step = _step_number_from_name(p.name)
        fonts.append({
            "name": p.name,
            "step": None if step == 999999 else step,
            "size": p.stat().st_size,
            "size_kb": round(p.stat().st_size / 1024, 1),
            "url": f"/job_font/{job_id}/{p.name}",
        })

    return {
        "ok": True,
        "job_id": job_id,
        "count": len(fonts),
        "fonts": fonts,
        "preview": {
            "svg": f"/preview/{job_id}",
            "family": f"/family_preview/{job_id}",
            "variable": f"/variable_preview/{job_id}",
        },
        "downloads": {
            "svg": f"/download/{job_id}/svg",
            "ttf": f"/download/{job_id}/ttf",
            "all": f"/download/{job_id}/all",
            "manifest": f"/download/{job_id}/manifest",
            "report": f"/download/{job_id}/report",
        },
        "real_variable_action": "/generate_real_variable_font",
    }


@app.get("/job_font/{job_id}/{filename}")
def job_font_file(job_id: str, filename: str):
    safe_name = Path(str(filename or "")).name
    files = {p.name: p for p in _job_font_files(job_id)}
    path = files.get(safe_name)

    if not path:
        return JSONResponse({"ok": False, "error": "font not found"}, status_code=404)

    media_type = "font/otf" if path.suffix.lower() == ".otf" else "font/ttf"
    return FileResponse(path, filename=path.name, media_type=media_type)


@app.post("/download/{job_id}/selected_ttf")
async def download_selected_ttf_for_job(
    job_id: str,
    selected_fonts: list[str] = Form(default=[]),
):
    job_dir = _safe_job_dir(job_id)

    if not job_dir:
        return HTMLResponse("<h1>Job not found</h1>", status_code=404)

    available = {p.name: p for p in _job_font_files(job_id)}
    picked = []
    seen = set()

    for item in selected_fonts:
        name = Path(str(item or "")).name

        if name in seen:
            continue

        path = available.get(name)

        if path:
            picked.append(path)
            seen.add(name)

    if not picked:
        return HTMLResponse(
            "<h1>没有选择可下载的 TTF 文件</h1>"
            "<p>请返回结果面板，至少勾选一个 step 字体。</p>",
            status_code=400,
        )

    zip_name = f"{job_id}_selected_ttf_{int(time.time())}.zip"
    zip_path = job_dir / zip_name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for path in picked:
            z.write(path, arcname=f"selected_ttf/{path.name}")

    return FileResponse(zip_path, filename=zip_name, media_type="application/zip")

@app.get("/preview/{job_id}", response_class=HTMLResponse)
def preview(job_id: str):
    path = JOBS_DIR / job_id / "preview.html"
    if not path.exists():
        return HTMLResponse("<h1>Preview not found</h1>")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/family_preview/{job_id}", response_class=HTMLResponse)
def family_preview(job_id: str):
    path = JOBS_DIR / job_id / "family_preview.html"
    if not path.exists():
        return HTMLResponse("<h1>Family preview not found</h1><p>该任务还没有生成字体家族预览页面。</p>")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/raw_svg/{job_id}/{code}/{step}")
def raw_svg(job_id: str, code: str, step: int):
    safe_code = code.upper().strip()
    if not safe_code.startswith("U"):
        return HTMLResponse("<h1>Invalid code</h1>", status_code=400)
    path = JOBS_DIR / job_id / "svg" / safe_code / f"{safe_code}_step_{step:02d}.svg"
    if not path.exists():
        return HTMLResponse("<h1>SVG not found</h1>", status_code=404)
    return FileResponse(path, media_type="image/svg+xml", filename=path.name)


@app.get("/variable_preview/{job_id}", response_class=HTMLResponse)
def variable_preview(job_id: str):
    safe_job_id = Path(str(job_id or "")).name
    job_dir = JOBS_DIR / safe_job_id

    if not job_dir.exists():
        return HTMLResponse("<h1>Variable preview not found</h1><p>该任务还没有生成可变滑杆预览页面。</p>")

    chars = _variable_preview_chars_from_job(job_dir)
    steps = _variable_preview_steps_from_job(job_dir)
    path = make_step_blend_variable_preview_html(job_dir, chars, steps)

    if not path.exists():
        return HTMLResponse("<h1>Variable preview not found</h1><p>该任务还没有生成可变滑杆预览页面。</p>")

    return HTMLResponse(path.read_text(encoding="utf-8"))


from skeleton_feature import prepare_skeleton_assets, prepare_skeleton_pages_lazy, ensure_skeleton_json, save_edited_skeleton

from fastapi import Request

from fastapi.responses import JSONResponse, FileResponse, HTMLResponse


@app.get("/skeleton_preview/{job_id}", response_class=HTMLResponse)
def skeleton_preview(job_id: str):
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return HTMLResponse("<h1>Job not found</h1>", status_code=404)
    prepare_skeleton_pages_lazy(job_dir)
    path = job_dir / "skeleton_preview.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/skeleton_editor/{job_id}", response_class=HTMLResponse)
def skeleton_editor(job_id: str):
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return HTMLResponse("<h1>Job not found</h1>", status_code=404)
    prepare_skeleton_pages_lazy(job_dir)
    path = job_dir / "skeleton_editor.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/skeleton_manifest/{job_id}")
def skeleton_manifest(job_id: str):
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    manifest_path = prepare_skeleton_pages_lazy(job_dir)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return JSONResponse(data)


@app.get("/raw_svg_variant/{job_id}/{code}/{variant}")
def raw_svg_variant(job_id: str, code: str, variant: str):
    code = code.upper().strip()
    variant = variant.strip()
    path = JOBS_DIR / job_id / "svg" / code / f"{code}_{variant}.svg"
    if not path.exists():
        return HTMLResponse("<h1>SVG not found</h1>", status_code=404)
    return FileResponse(path, media_type="image/svg+xml", filename=path.name)


@app.get("/skeleton_json/{job_id}/{code}/{variant}")
def skeleton_json(job_id: str, code: str, variant: str):
    code = code.upper().strip()
    variant = variant.strip()
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)

    try:
        path = ensure_skeleton_json(job_dir, code, variant)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return FileResponse(path, media_type="application/json", filename=path.name)


@app.post("/save_skeleton_edit/{job_id}/{code}/{variant}")
async def save_skeleton_edit(job_id: str, code: str, variant: str, request: Request):
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)

    payload = await request.json()
    try:
        out = save_edited_skeleton(job_dir, code, variant, payload)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return JSONResponse({
        "ok": True,
        "svg_url": f"/download_edited_svg/{job_id}/{code.upper()}/{variant}",
        "png_url": f"/download_edited_png/{job_id}/{code.upper()}/{variant}"
    })


@app.get("/download_edited_svg/{job_id}/{code}/{variant}")
def download_edited_svg(job_id: str, code: str, variant: str):
    code = code.upper().strip()
    path = JOBS_DIR / job_id / "skeleton_edits" / code / f"{code}_{variant}_edited.svg"
    if not path.exists():
        return HTMLResponse("<h1>Edited SVG not found</h1>", status_code=404)
    return FileResponse(path, media_type="image/svg+xml", filename=path.name)


@app.get("/download_edited_png/{job_id}/{code}/{variant}")
def download_edited_png(job_id: str, code: str, variant: str):
    code = code.upper().strip()
    path = JOBS_DIR / job_id / "skeleton_edits" / code / f"{code}_{variant}_edited.png"
    if not path.exists():
        return HTMLResponse("<h1>Edited PNG not found</h1>", status_code=404)
    return FileResponse(path, media_type="image/png", filename=path.name)

# =========================================================
# Save deformed original outline SVG / PNG
# =========================================================
@app.post("/save_outline_edit/{job_id}/{code}/{variant}")
async def save_outline_edit(job_id: str, code: str, variant: str, request: Request):
    import cairosvg
    import re
    from pathlib import Path

    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)

    code = code.upper().strip()
    variant = variant.strip()

    try:
        payload = await request.json()
        svg_text = payload.get("svg", "")
        if not svg_text.strip():
            return JSONResponse({"ok": False, "error": "empty svg"}, status_code=400)

        out_dir = job_dir / "skeleton_edits" / code
        out_dir.mkdir(parents=True, exist_ok=True)

        out_svg = out_dir / f"{code}_{variant}_edited.svg"
        out_png = out_dir / f"{code}_{variant}_edited.png"

        out_svg.write_text(svg_text, encoding="utf-8")

        png_bytes = cairosvg.svg2png(
            bytestring=svg_text.encode("utf-8"),
            output_width=1400,
            output_height=1400
        )
        out_png.write_bytes(png_bytes)

        return JSONResponse({
            "ok": True,
            "svg_url": f"/download_edited_svg/{job_id}/{code}/{variant}",
            "png_url": f"/download_edited_png/{job_id}/{code}/{variant}"
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# =========================================================
# Save deformed original outline SVG / PNG
# =========================================================
@app.post("/save_outline_edit/{job_id}/{code}/{variant}")
async def save_outline_edit(job_id: str, code: str, variant: str, request: Request):
    import cairosvg
    import re
    from pathlib import Path

    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)

    code = code.upper().strip()
    variant = variant.strip()

    try:
        payload = await request.json()
        svg_text = payload.get("svg", "")
        if not svg_text.strip():
            return JSONResponse({"ok": False, "error": "empty svg"}, status_code=400)

        out_dir = job_dir / "skeleton_edits" / code
        out_dir.mkdir(parents=True, exist_ok=True)

        out_svg = out_dir / f"{code}_{variant}_edited.svg"
        out_png = out_dir / f"{code}_{variant}_edited.png"

        out_svg.write_text(svg_text, encoding="utf-8")

        png_bytes = cairosvg.svg2png(
            bytestring=svg_text.encode("utf-8"),
            output_width=1400,
            output_height=1400
        )
        out_png.write_bytes(png_bytes)

        return JSONResponse({
            "ok": True,
            "svg_url": f"/download_edited_svg/{job_id}/{code}/{variant}",
            "png_url": f"/download_edited_png/{job_id}/{code}/{variant}"
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# =========================================================
# Save deformed original outline SVG / PNG
# =========================================================
@app.post("/save_outline_edit/{job_id}/{code}/{variant}")
async def save_outline_edit(job_id: str, code: str, variant: str, request: Request):
    import cairosvg
    import re
    from pathlib import Path

    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)

    code = code.upper().strip()
    variant = variant.strip()

    try:
        payload = await request.json()
        svg_text = payload.get("svg", "")
        if not svg_text.strip():
            return JSONResponse({"ok": False, "error": "empty svg"}, status_code=400)

        out_dir = job_dir / "skeleton_edits" / code
        out_dir.mkdir(parents=True, exist_ok=True)

        out_svg = out_dir / f"{code}_{variant}_edited.svg"
        out_png = out_dir / f"{code}_{variant}_edited.png"

        out_svg.write_text(svg_text, encoding="utf-8")

        png_bytes = cairosvg.svg2png(
            bytestring=svg_text.encode("utf-8"),
            output_width=1400,
            output_height=1400
        )
        out_png.write_bytes(png_bytes)

        return JSONResponse({
            "ok": True,
            "svg_url": f"/download_edited_svg/{job_id}/{code}/{variant}",
            "png_url": f"/download_edited_png/{job_id}/{code}/{variant}"
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# =========================================================
# MLS_SKELETON_EDITOR_ROUTE_V1
# 新增独立 MLS 骨架整体变形编辑器，不覆盖旧功能。
# =========================================================
from fastapi.responses import HTMLResponse as _MLSHTMLResponse
from fastapi import Request as _MLSRequest

@app.get("/skeleton_mls_editor", response_class=_MLSHTMLResponse)
async def skeleton_mls_editor_latest():
    from skeleton_mls_editor import build_mls_editor_html

    jobs = []
    if JOBS_DIR.exists():
        jobs = [p for p in JOBS_DIR.iterdir() if p.is_dir()]

    if not jobs:
        return _MLSHTMLResponse("<h2>No jobs found.</h2>", status_code=404)

    latest = max(jobs, key=lambda p: p.stat().st_mtime)
    return _MLSHTMLResponse(build_mls_editor_html(latest.name))


@app.get("/skeleton_mls_editor/{job_id}", response_class=_MLSHTMLResponse)
async def skeleton_mls_editor_job(job_id: str):
    from skeleton_mls_editor import build_mls_editor_html

    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return _MLSHTMLResponse("<h2>Job not found.</h2>", status_code=404)

    return _MLSHTMLResponse(build_mls_editor_html(job_id))


@app.post("/save_outline_edit/{job_id}/{code}/{variant}")
async def save_outline_edit_mls_safe(job_id: str, code: str, variant: str, request: _MLSRequest):
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)

    code = code.upper().strip()
    variant = variant.strip()

    try:
        payload = await request.json()
        svg_text = payload.get("svg", "")
        if not svg_text.strip():
            return JSONResponse({"ok": False, "error": "empty svg"}, status_code=400)

        out_dir = job_dir / "skeleton_edits" / code
        out_dir.mkdir(parents=True, exist_ok=True)

        out_svg = out_dir / f"{code}_{variant}_mls_edited.svg"
        out_svg.write_text(svg_text, encoding="utf-8")

        return JSONResponse({"ok": True, "saved": str(out_svg)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# =========================================================
# MLS_EDITOR_AND_CONTOUR_API_V2
# 独立 MLS 骨架整体变形编辑器 + 后端轮廓提取接口
# =========================================================
from fastapi.responses import HTMLResponse as _MLSHTMLResponse
from fastapi import Request as _MLSRequest

@app.get("/skeleton_mls_editor", response_class=_MLSHTMLResponse)
async def skeleton_mls_editor_latest_v2():
    from skeleton_mls_editor import build_mls_editor_html

    jobs = []
    if JOBS_DIR.exists():
        jobs = [p for p in JOBS_DIR.iterdir() if p.is_dir()]

    if not jobs:
        return _MLSHTMLResponse("<h2>No jobs found.</h2>", status_code=404)

    latest = max(jobs, key=lambda p: p.stat().st_mtime)
    return _MLSHTMLResponse(build_mls_editor_html(latest.name))


@app.get("/skeleton_mls_editor/{job_id}", response_class=_MLSHTMLResponse)
async def skeleton_mls_editor_job_v2(job_id: str):
    from skeleton_mls_editor import build_mls_editor_html

    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return _MLSHTMLResponse("<h2>Job not found.</h2>", status_code=404)

    return _MLSHTMLResponse(build_mls_editor_html(job_id))


@app.get("/mls_contours/{job_id}/{code}/{variant}")
def mls_contours_v2(job_id: str, code: str, variant: str):
    import io
    import re
    import numpy as np
    import cairosvg
    from PIL import Image
    from skimage import measure

    code = code.upper().strip()
    variant = variant.strip()

    svg_path = JOBS_DIR / job_id / "svg" / code / f"{code}_{variant}.svg"

    if not svg_path.exists():
        return JSONResponse({"ok": False, "error": f"SVG not found: {svg_path}"}, status_code=404)

    try:
        svg_text = svg_path.read_text(encoding="utf-8", errors="ignore")

        m = re.search(r'viewBox="([^"]+)"', svg_text)
        if m:
            parts = [float(x) for x in re.split(r"[\s,]+", m.group(1).strip()) if x]
            if len(parts) == 4:
                vb = parts
            else:
                vb = [0.0, 0.0, 1000.0, 1000.0]
        else:
            vb = [0.0, 0.0, 1000.0, 1000.0]

        side = 1024

        png = cairosvg.svg2png(
            bytestring=svg_text.encode("utf-8"),
            output_width=side,
            output_height=side,
            background_color="white"
        )

        img = Image.open(io.BytesIO(png)).convert("RGBA")
        arr = np.array(img)

        alpha = arr[:, :, 3]
        gray = np.array(img.convert("L"))

        mask = alpha > 0
        if mask.sum() > mask.size * 0.98:
            mask = gray < 245

        mask = mask.astype(np.uint8)

        contours_raw = measure.find_contours(mask, 0.5)

        vx, vy, vw, vh = vb
        contours = []

        for c in contours_raw:
            if len(c) < 20:
                continue

            pts = []
            # c: row=y, col=x
            step = max(1, int(len(c) / 260))  # 控制轮廓点数量，避免前端太慢

            for row, col in c[::step]:
                x = vx + (float(col) / (side - 1)) * vw
                y = vy + (float(row) / (side - 1)) * vh
                pts.append({"x": round(x, 4), "y": round(y, 4)})

            if len(pts) >= 12:
                contours.append(pts)

        contours = sorted(contours, key=lambda pts: len(pts), reverse=True)

        # 最多保留 8 个轮廓，防止异常噪声
        contours = contours[:8]

        return JSONResponse({
            "ok": True,
            "viewBox": vb,
            "contours": contours,
            "count": len(contours)
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/save_outline_edit/{job_id}/{code}/{variant}")
async def save_outline_edit_mls_v2(job_id: str, code: str, variant: str, request: _MLSRequest):
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)

    code = code.upper().strip()
    variant = variant.strip()

    try:
        payload = await request.json()
        svg_text = payload.get("svg", "")
        if not svg_text.strip():
            return JSONResponse({"ok": False, "error": "empty svg"}, status_code=400)

        out_dir = job_dir / "skeleton_edits" / code
        out_dir.mkdir(parents=True, exist_ok=True)

        out_svg = out_dir / f"{code}_{variant}_mls_edited.svg"
        out_svg.write_text(svg_text, encoding="utf-8")

        return JSONResponse({"ok": True, "saved": str(out_svg)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# =========================================================
# GLYPH_LIVE_EDITOR_V3_FINAL
# 独立完整字形实时变形编辑器，不影响旧功能。
# =========================================================
from fastapi.responses import HTMLResponse as _GlyphV3HTMLResponse
from fastapi import Request as _GlyphV3Request

@app.get("/glyph_live_editor_v3", response_class=_GlyphV3HTMLResponse)
async def glyph_live_editor_v3_latest():
    from glyph_live_editor_v3 import build_glyph_live_editor_html

    jobs = []
    if JOBS_DIR.exists():
        jobs = [p for p in JOBS_DIR.iterdir() if p.is_dir()]

    if not jobs:
        return _GlyphV3HTMLResponse("<h2>No jobs found.</h2>", status_code=404)

    latest = max(jobs, key=lambda p: p.stat().st_mtime)
    return _GlyphV3HTMLResponse(build_glyph_live_editor_html(latest.name))


@app.get("/glyph_live_editor_v3/{job_id}", response_class=_GlyphV3HTMLResponse)
async def glyph_live_editor_v3_job(job_id: str):
    from glyph_live_editor_v3 import build_glyph_live_editor_html

    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        return _GlyphV3HTMLResponse("<h2>Job not found.</h2>", status_code=404)

    return _GlyphV3HTMLResponse(build_glyph_live_editor_html(job_id))


@app.get("/api/glyph_live_v3/manifest/{job_id}")
def glyph_live_v3_manifest(job_id: str):
    job_dir = JOBS_DIR / job_id
    svg_root = job_dir / "svg"

    if not svg_root.exists():
        return JSONResponse({"ok": False, "error": "svg dir not found", "codes": []}, status_code=404)

    codes = []

    for code_dir in sorted(svg_root.iterdir()):
        if not code_dir.is_dir():
            continue

        code = code_dir.name.upper()
        variants = []

        for f in sorted(code_dir.glob("*.svg")):
            stem = f.stem
            if stem.startswith(code + "_"):
                v = stem[len(code)+1:]
            else:
                v = stem

            variants.append({"name": v, "label": v})

        if not variants:
            continue

        try:
            ch = chr(int(code[1:], 16)) if code.startswith("U") else code
        except Exception:
            ch = code

        codes.append({
            "code": code,
            "char": ch,
            "variants": variants
        })

    return JSONResponse({"ok": True, "job_id": job_id, "codes": codes})


@app.get("/api/glyph_live_v3/data/{job_id}/{code}/{variant}")
def glyph_live_v3_data(job_id: str, code: str, variant: str):
    import io
    import re
    import numpy as np
    import cairosvg
    from PIL import Image
    from skimage import measure

    code = code.upper().strip()
    variant = variant.strip()

    svg_path = JOBS_DIR / job_id / "svg" / code / f"{code}_{variant}.svg"

    if not svg_path.exists():
        return JSONResponse({"ok": False, "error": f"SVG not found: {svg_path}"}, status_code=404)

    try:
        svg_text = svg_path.read_text(encoding="utf-8", errors="ignore")

        m = re.search(r'viewBox="([^"]+)"', svg_text)
        if m:
            parts = [float(x) for x in re.split(r"[\s,]+", m.group(1).strip()) if x]
            vb = parts if len(parts) == 4 else [0.0, 0.0, 1000.0, 1000.0]
        else:
            vb = [0.0, 0.0, 1000.0, 1000.0]

        side = 1200

        png = cairosvg.svg2png(
            bytestring=svg_text.encode("utf-8"),
            output_width=side,
            output_height=side,
            background_color="white"
        )

        img = Image.open(io.BytesIO(png)).convert("RGBA")
        arr = np.array(img)
        alpha = arr[:, :, 3]
        gray = np.array(img.convert("L"))

        mask = alpha > 0
        if mask.sum() > mask.size * 0.98:
            mask = gray < 245

        mask = mask.astype(np.uint8)

        contours_raw = measure.find_contours(mask, 0.5)

        vx, vy, vw, vh = vb
        contours = []

        for c in contours_raw:
            if len(c) < 24:
                continue

            step = max(1, int(len(c) / 300))
            pts = []

            for row, col in c[::step]:
                x = vx + (float(col) / (side - 1)) * vw
                y = vy + (float(row) / (side - 1)) * vh
                pts.append({"x": round(x, 4), "y": round(y, 4)})

            if len(pts) >= 12:
                contours.append(pts)

        contours = sorted(contours, key=lambda pts: len(pts), reverse=True)[:8]

        return JSONResponse({
            "ok": True,
            "job_id": job_id,
            "code": code,
            "variant": variant,
            "viewBox": vb,
            "contours": contours,
            "count": len(contours)
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/glyph_live_v3/save/{job_id}/{code}/{variant}")
async def glyph_live_v3_save(job_id: str, code: str, variant: str, request: _GlyphV3Request):
    job_dir = JOBS_DIR / job_id

    if not job_dir.exists():
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)

    code = code.upper().strip()
    variant = variant.strip()

    try:
        payload = await request.json()
        svg_text = payload.get("svg", "")

        if not svg_text.strip():
            return JSONResponse({"ok": False, "error": "empty svg"}, status_code=400)

        out_dir = job_dir / "glyph_live_edits" / code
        out_dir.mkdir(parents=True, exist_ok=True)

        out_svg = out_dir / f"{code}_{variant}_glyph_live_v3.svg"
        out_svg.write_text(svg_text, encoding="utf-8")

        return JSONResponse({"ok": True, "saved": str(out_svg)})

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ===== ELASTIC_SKELETON_EDITOR_ROUTE_V4 =====
from fastapi.responses import HTMLResponse as _ElasticHTMLResponse

@app.get("/skeleton_elastic_editor/{job_id}", response_class=_ElasticHTMLResponse)
async def skeleton_elastic_editor_v4(job_id: str):
    html = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>骨架编辑器｜整体弹性联动版</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f3f4f6;font-family:Arial,"Microsoft YaHei",sans-serif;color:#111827}
header{height:72px;background:#111;color:white;padding:14px 22px}
header h1{margin:0;font-size:24px}
header p{margin:6px 0 0;color:#d1d5db;font-size:13px}
.app{height:calc(100vh - 72px);display:grid;grid-template-columns:220px 1fr 280px;gap:14px;padding:14px}
.panel,.main{background:white;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
.panel{padding:14px;overflow:auto}
.main{padding:14px;display:flex;flex-direction:column;overflow:hidden}
.title{font-weight:700;margin-bottom:10px}
.glyph-card{border:1px solid #e5e7eb;border-radius:8px;padding:10px;margin-bottom:9px;background:#fafafa;cursor:pointer}
.glyph-card.active{background:#2563eb;color:white;border-color:#2563eb}
.glyph-card small{display:block;margin-top:5px;opacity:.75}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
button{border:0;border-radius:7px;padding:8px 12px;background:#4b5563;color:white;cursor:pointer;font-size:13px}
button.primary{background:#2563eb}
button.warn{background:#d97706}
button.good{background:#059669}
select{height:32px;border:1px solid #d1d5db;border-radius:6px;padding:0 8px;min-width:180px}
.canvas-wrap{flex:1;position:relative;border:1px solid #e5e7eb;border-radius:10px;background:white;overflow:hidden}
canvas{width:100%;height:100%;display:block}
.help{font-size:12px;line-height:1.7;color:#4b5563;margin-top:9px}
.row{display:flex;gap:8px;align-items:center;font-size:13px;margin:8px 0}
.group{margin:16px 0}
.group label{display:flex;justify-content:space-between;font-weight:700;font-size:13px;margin-bottom:6px}
input[type=range]{width:100%}
.status,.note{font-size:12px;line-height:1.7;border-radius:8px;padding:10px}
.status{background:#f9fafb;border:1px solid #e5e7eb}
.note{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;margin-top:12px}
.err{color:#b91c1c;white-space:pre-wrap;font-size:12px}
</style>
</head>
<body>
<header>
  <h1>骨架编辑器｜整体弹性联动版</h1>
  <p>拖动一个控制点时，按骨架拓扑距离、欧氏距离、边长保持和平滑约束，让整体字形一起联动。</p>
</header>

<div class="app">
  <aside class="panel">
    <div class="title">字形列表</div>
    <div id="glyphList">加载中...</div>
  </aside>

  <main class="main">
    <div class="toolbar">
      <button class="primary" id="saveBtn">保存当前字形</button>
      <button id="exportSvgBtn">导出 SVG</button>
      <button id="exportPngBtn">导出 PNG</button>
      <button id="smoothBtn">平滑控制点</button>
      <button class="warn" id="resetBtn">恢复当前版本</button>
      <select id="variantSelect"></select>
    </div>

    <div class="canvas-wrap">
      <canvas id="canvas"></canvas>
    </div>

    <div class="help">
      操作：拖动白色控制点进行弹性变形；Ctrl + 点击控制点可固定 / 取消固定；Shift + 拖动增强整体联动；Alt + 拖动增强局部变形。
    </div>
  </main>

  <aside class="panel">
    <div class="title">显示控制</div>

    <div class="row"><input id="showRawSvg" type="checkbox" checked><label for="showRawSvg">显示原始完整轮廓</label></div>
    <div class="row"><input id="showPreview" type="checkbox" checked><label for="showPreview">显示变形后预览</label></div>
    <div class="row"><input id="showSkeleton" type="checkbox" checked><label for="showSkeleton">显示当前骨架</label></div>
    <div class="row"><input id="showPoints" type="checkbox" checked><label for="showPoints">显示控制点</label></div>

    <hr>

    <div class="group">
      <label>整体联动强度 <span id="globalValue">0.68</span></label>
      <input id="globalStrength" type="range" min="0" max="1" step="0.01" value="0.68">
    </div>

    <div class="group">
      <label>局部影响半径 <span id="radiusValue">230</span></label>
      <input id="influenceRadius" type="range" min="40" max="650" step="1" value="230">
    </div>

    <div class="group">
      <label>结构保持强度 <span id="preserveValue">0.82</span></label>
      <input id="preserveStrength" type="range" min="0" max="1" step="0.01" value="0.82">
    </div>

    <div class="group">
      <label>平滑强度 <span id="smoothValue">0.055</span></label>
      <input id="smoothStrength" type="range" min="0" max="0.25" step="0.005" value="0.055">
    </div>

    <div class="group">
      <label>预览字形粗细 <span id="thickValue">28</span></label>
      <input id="strokeThickness" type="range" min="4" max="90" step="1" value="28">
    </div>

    <div class="status" id="statusBox"></div>

    <div class="note">
      这个版本不会只移动一个点，而是把骨架当作图结构处理：近处跟随多，远处跟随少，并用边长保持防止字形被拉散。
    </div>
  </aside>
</div>

<script>
"use strict";

const JOB_ID = "__JOB_ID__";

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");

const ui = {
  glyphList: document.getElementById("glyphList"),
  variantSelect: document.getElementById("variantSelect"),
  showRawSvg: document.getElementById("showRawSvg"),
  showPreview: document.getElementById("showPreview"),
  showSkeleton: document.getElementById("showSkeleton"),
  showPoints: document.getElementById("showPoints"),
  globalStrength: document.getElementById("globalStrength"),
  influenceRadius: document.getElementById("influenceRadius"),
  preserveStrength: document.getElementById("preserveStrength"),
  smoothStrength: document.getElementById("smoothStrength"),
  strokeThickness: document.getElementById("strokeThickness"),
  globalValue: document.getElementById("globalValue"),
  radiusValue: document.getElementById("radiusValue"),
  preserveValue: document.getElementById("preserveValue"),
  smoothValue: document.getElementById("smoothValue"),
  thickValue: document.getElementById("thickValue"),
  statusBox: document.getElementById("statusBox")
};

const state = {
  manifest: [],
  activeIndex: 0,
  code: null,
  variant: null,
  basePoints: [],
  points: [],
  edges: [],
  rawSvgText: "",
  rawSvgImage: null,
  svgBounds: null,
  pinned: new Set(),
  drag: null,
  hover: -1,
  scale: 1,
  offsetX: 0,
  offsetY: 0,
  loaded: false
};

function normalizeManifest(raw) {
  let arr = raw;

  if (!Array.isArray(arr)) {
    arr = raw.glyphs || raw.items || raw.chars || raw.manifest || raw.data || raw.results || [];
  }

  if (!Array.isArray(arr) && typeof arr === "object") {
    arr = Object.entries(arr).map(([code, value]) => {
      if (typeof value === "object") return {code, ...value};
      return {code, variants: value};
    });
  }

  arr = arr.map(x => {
    const code = x.code || x.unicode || x.char || x.name || x.glyph || x.id;
    let variants = x.variants || x.steps || x.versions || x.variant_list || x.files || [];
    if (!Array.isArray(variants)) variants = Object.keys(variants || {});
    variants = variants.map(v => {
      if (typeof v === "string") return v;
      return v.variant || v.step || v.name || v.id || v.key || "step_01";
    });
    if (!variants.length) variants = ["step_01"];
    return {code, variants};
  }).filter(x => x.code);

  return arr;
}

async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
  return await r.json();
}

async function fetchText(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
  return await r.text();
}

function normalizeSkeleton(data) {
  let points = [];
  let edges = [];

  if (Array.isArray(data.points)) points = data.points;
  else if (Array.isArray(data.control_points)) points = data.control_points;
  else if (Array.isArray(data.skeleton_points)) points = data.skeleton_points;
  else if (data.skeleton && Array.isArray(data.skeleton.points)) points = data.skeleton.points;
  else if (data.skeleton && Array.isArray(data.skeleton.nodes)) points = data.skeleton.nodes;
  else if (Array.isArray(data.nodes)) points = data.nodes;

  if (Array.isArray(data.edges)) edges = data.edges;
  else if (Array.isArray(data.skeleton_edges)) edges = data.skeleton_edges;
  else if (data.skeleton && Array.isArray(data.skeleton.edges)) edges = data.skeleton.edges;

  let finalPoints = [];

  if (Array.isArray(data.strokes) && !points.length) {
    for (const stroke of data.strokes) {
      const spts = stroke.points || stroke.nodes || stroke;
      if (!Array.isArray(spts)) continue;
      let last = -1;
      for (const p of spts) {
        const q = toPoint(p);
        if (!q) continue;
        finalPoints.push(q);
        const now = finalPoints.length - 1;
        if (last >= 0) edges.push([last, now]);
        last = now;
      }
    }
  } else {
    finalPoints = points.map(toPoint).filter(Boolean);
  }

  edges = edges.map(e => {
    if (Array.isArray(e)) return [Number(e[0]), Number(e[1])];
    return [Number(e.a ?? e.from ?? e.source ?? e.i), Number(e.b ?? e.to ?? e.target ?? e.j)];
  }).filter(e => Number.isFinite(e[0]) && Number.isFinite(e[1]) && e[0] >= 0 && e[1] >= 0 && e[0] < finalPoints.length && e[1] < finalPoints.length);

  if (!edges.length && finalPoints.length > 1) {
    for (let i = 0; i < finalPoints.length - 1; i++) edges.push([i, i + 1]);
  }

  return {points: finalPoints, edges};
}

function toPoint(p) {
  if (!p) return null;
  if (Array.isArray(p) && p.length >= 2) return {x: Number(p[0]), y: Number(p[1])};
  if (typeof p === "object") {
    const x = Number(p.x ?? p.X ?? p.cx ?? p[0]);
    const y = Number(p.y ?? p.Y ?? p.cy ?? p[1]);
    if (Number.isFinite(x) && Number.isFinite(y)) return {x, y};
  }
  return null;
}

async function init() {
  labels();
  bind();
  resize();

  try {
    const rawManifest = await fetchJson(`/elastic_skeleton_manifest/${JOB_ID}`);
    state.manifest = normalizeManifest(rawManifest);
    if (!state.manifest.length) throw new Error("manifest 为空，无法读取字形列表。");

    renderGlyphList();
    await selectGlyph(0);
  } catch (e) {
    ui.glyphList.innerHTML = `<div class="err">${e.stack || e}</div>`;
    ui.statusBox.innerHTML = `<div class="err">${e.stack || e}</div>`;
  }
}

function bind() {
  window.addEventListener("resize", () => { resize(); draw(); });
  canvas.addEventListener("pointerdown", pointerDown);
  canvas.addEventListener("pointermove", pointerMove);
  canvas.addEventListener("pointerup", pointerUp);
  canvas.addEventListener("pointerleave", pointerUp);

  document.getElementById("saveBtn").onclick = saveEdit;
  document.getElementById("exportSvgBtn").onclick = exportSVG;
  document.getElementById("exportPngBtn").onclick = exportPNG;
  document.getElementById("smoothBtn").onclick = smoothCurrent;
  document.getElementById("resetBtn").onclick = () => {
    state.points = clone(state.basePoints);
    state.pinned.clear();
    draw();
  };

  ui.variantSelect.onchange = async () => {
    state.variant = ui.variantSelect.value;
    await loadGlyphData();
  };

  [
    ui.showRawSvg, ui.showPreview, ui.showSkeleton, ui.showPoints,
    ui.globalStrength, ui.influenceRadius, ui.preserveStrength,
    ui.smoothStrength, ui.strokeThickness
  ].forEach(el => el.addEventListener("input", () => { labels(); draw(); }));
}

function labels() {
  ui.globalValue.textContent = Number(ui.globalStrength.value).toFixed(2);
  ui.radiusValue.textContent = ui.influenceRadius.value;
  ui.preserveValue.textContent = Number(ui.preserveStrength.value).toFixed(2);
  ui.smoothValue.textContent = Number(ui.smoothStrength.value).toFixed(3);
  ui.thickValue.textContent = ui.strokeThickness.value;
}

function resize() {
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(300, Math.floor(rect.width * dpr));
  canvas.height = Math.max(300, Math.floor(rect.height * dpr));
  canvas.style.width = rect.width + "px";
  canvas.style.height = rect.height + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  fit();
}

function renderGlyphList() {
  ui.glyphList.innerHTML = "";
  state.manifest.forEach((g, i) => {
    const div = document.createElement("div");
    div.className = "glyph-card" + (i === state.activeIndex ? " active" : "");
    div.innerHTML = `<b>${g.code}</b><small>${g.variants.length} 个版本</small>`;
    div.onclick = () => selectGlyph(i);
    ui.glyphList.appendChild(div);
  });
}

async function selectGlyph(i) {
  state.activeIndex = i;
  const item = state.manifest[i];
  state.code = item.code;

  ui.variantSelect.innerHTML = "";
  item.variants.forEach(v => {
    const op = document.createElement("option");
    op.value = v;
    op.textContent = v;
    ui.variantSelect.appendChild(op);
  });

  state.variant = item.variants[0] || "step_01";
  ui.variantSelect.value = state.variant;

  renderGlyphList();
  await loadGlyphData();
}

async function loadGlyphData() {
  state.loaded = false;
  state.pinned.clear();

  const data = await fetchJson(`/skeleton_json/${JOB_ID}/${state.code}/${state.variant}`);
  const norm = normalizeSkeleton(data);

  state.basePoints = clone(norm.points);
  state.points = clone(norm.points);
  state.edges = norm.edges.map(e => [...e]);

  try {
    state.rawSvgText = await fetchText(`/raw_svg_variant/${JOB_ID}/${state.code}/${state.variant}`);
    state.rawSvgImage = await svgToImage(state.rawSvgText);
    state.svgBounds = parseSvgBounds(state.rawSvgText);
  } catch (e) {
    state.rawSvgText = "";
    state.rawSvgImage = null;
    state.svgBounds = null;
  }

  state.loaded = true;
  fit();
  draw();
}


function parseSvgBounds(svgText) {
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(svgText, "image/svg+xml");
    const svg = doc.documentElement;

    const vb = svg.getAttribute("viewBox");
    if (vb) {
      const nums = vb.trim().split(/[\s,]+/).map(Number);
      if (nums.length === 4 && nums.every(n => Number.isFinite(n))) {
        const [minX, minY, width, height] = nums;
        return {
          minX,
          minY,
          maxX: minX + width,
          maxY: minY + height,
          width,
          height,
          cx: minX + width / 2,
          cy: minY + height / 2
        };
      }
    }

    const w = parseFloat((svg.getAttribute("width") || "300").replace(/[a-z%]+/ig, ""));
    const h = parseFloat((svg.getAttribute("height") || "300").replace(/[a-z%]+/ig, ""));
    if (Number.isFinite(w) && Number.isFinite(h)) {
      return {
        minX: 0,
        minY: 0,
        maxX: w,
        maxY: h,
        width: w,
        height: h,
        cx: w / 2,
        cy: h / 2
      };
    }
  } catch (e) {
    console.warn("parseSvgBounds failed:", e);
  }

  return {
    minX: 0,
    minY: 0,
    maxX: 300,
    maxY: 300,
    width: 300,
    height: 300,
    cx: 150,
    cy: 150
  };
}

function svgToImage(svgText) {
  return new Promise((resolve, reject) => {
    const blob = new Blob([svgText], {type: "image/svg+xml"});
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = err => {
      URL.revokeObjectURL(url);
      reject(err);
    };
    img.src = url;
  });
}

function fit() {
  const r = canvas.getBoundingClientRect();
  const pad = 90;

  let b = null;

  if (state.points && state.points.length) {
    b = bounds(state.points);
  } else if (state.svgBounds) {
    b = state.svgBounds;
  } else {
    state.scale = 1;
    state.offsetX = r.width / 2;
    state.offsetY = r.height / 2;
    return;
  }

  const sx = (r.width - pad * 2) / Math.max(1, b.width);
  const sy = (r.height - pad * 2) / Math.max(1, b.height);
  state.scale = Math.min(sx, sy);
  state.offsetX = r.width / 2 - b.cx * state.scale;
  state.offsetY = r.height / 2 - b.cy * state.scale;
}

function pointerDown(e) {
  if (!state.loaded) return;
  const m = screenToWorld(mouse(e));
  const hit = nearest(m, state.points, 14 / state.scale);
  if (hit < 0) return;

  if (e.ctrlKey || e.metaKey) {
    state.pinned.has(hit) ? state.pinned.delete(hit) : state.pinned.add(hit);
    draw();
    return;
  }

  canvas.setPointerCapture(e.pointerId);
  state.drag = {
    handle: hit,
    basePoints: clone(state.points),
    basePinned: new Set(state.pinned)
  };
}

function pointerMove(e) {
  if (!state.loaded) return;
  const m = screenToWorld(mouse(e));
  state.hover = nearest(m, state.points, 14 / state.scale);

  if (!state.drag) {
    draw();
    return;
  }

  let globalStrength = Number(ui.globalStrength.value);
  let influenceRadius = Number(ui.influenceRadius.value);

  if (e.shiftKey) {
    globalStrength = Math.min(1, globalStrength + 0.25);
    influenceRadius *= 1.45;
  }

  if (e.altKey) {
    globalStrength = Math.max(0.05, globalStrength - 0.28);
    influenceRadius *= 0.55;
  }

  state.points = elasticDeform({
    basePoints: state.drag.basePoints,
    edges: state.edges,
    handleIndex: state.drag.handle,
    target: m,
    pinned: state.drag.basePinned,
    globalStrength,
    influenceRadius,
    preserveStrength: Number(ui.preserveStrength.value),
    smoothStrength: Number(ui.smoothStrength.value),
    preserveIterations: 18,
    smoothIterations: 3
  });

  draw();
}

function pointerUp() {
  state.drag = null;
}

function elasticDeform(o) {
  const handle = o.basePoints[o.handleIndex];
  const dx = o.target.x - handle.x;
  const dy = o.target.y - handle.y;
  const gd = graphDistances(o.basePoints, o.edges, o.handleIndex);

  const fixed = new Set(o.pinned);
  fixed.add(o.handleIndex);

  let pts = o.basePoints.map((p, i) => {
    if (o.pinned.has(i)) return {...p};

    let w = influenceWeight({
      p,
      handle,
      graphDistance: gd[i],
      radius: o.influenceRadius,
      globalStrength: o.globalStrength
    });

    if (i === o.handleIndex) w = 1;

    return {
      x: p.x + dx * w,
      y: p.y + dy * w
    };
  });

  pts[o.handleIndex] = {...o.target};

  pts = preserveEdgeLengths({
    points: pts,
    basePoints: o.basePoints,
    edges: o.edges,
    fixed,
    strength: o.preserveStrength,
    iterations: o.preserveIterations
  });

  pts = laplacianSmooth({
    points: pts,
    edges: o.edges,
    fixed,
    strength: o.smoothStrength,
    iterations: o.smoothIterations
  });

  pts[o.handleIndex] = {...o.target};

  return pts;
}

function influenceWeight({p, handle, graphDistance, radius, globalStrength}) {
  const euclid = dist(p, handle);
  let topoWeight = 0;

  if (Number.isFinite(graphDistance)) {
    const t = clamp(1 - graphDistance / radius, 0, 1);
    topoWeight = t * t * (3 - 2 * t);
  }

  const euclidWeight = Math.exp(-(euclid * euclid) / (2 * radius * radius));
  let w = Math.max(topoWeight, euclidWeight * 0.7);

  w = w * globalStrength + 0.08 * globalStrength;

  return clamp(w, 0, 1);
}

function graphDistances(points, edges, start) {
  const n = points.length;
  const adj = Array.from({length: n}, () => []);

  for (const [a, b] of edges) {
    const l = dist(points[a], points[b]);
    adj[a].push([b, l]);
    adj[b].push([a, l]);
  }

  const d = Array(n).fill(Infinity);
  const used = Array(n).fill(false);
  d[start] = 0;

  for (let k = 0; k < n; k++) {
    let u = -1;
    let best = Infinity;

    for (let i = 0; i < n; i++) {
      if (!used[i] && d[i] < best) {
        best = d[i];
        u = i;
      }
    }

    if (u < 0) break;

    used[u] = true;

    for (const [v, w] of adj[u]) {
      if (d[u] + w < d[v]) d[v] = d[u] + w;
    }
  }

  return d;
}

function preserveEdgeLengths({points, basePoints, edges, fixed, strength, iterations}) {
  let pts = clone(points);
  const rest = edges.map(([a, b]) => dist(basePoints[a], basePoints[b]));

  for (let it = 0; it < iterations; it++) {
    for (let i = 0; i < edges.length; i++) {
      const [a, b] = edges[i];
      const pa = pts[a];
      const pb = pts[b];

      const vx = pb.x - pa.x;
      const vy = pb.y - pa.y;
      const len = Math.hypot(vx, vy);
      if (len < 0.0001) continue;

      const diff = (len - rest[i]) / len;
      const cx = vx * diff * 0.5 * strength;
      const cy = vy * diff * 0.5 * strength;

      const fa = fixed.has(a);
      const fb = fixed.has(b);

      if (!fa && !fb) {
        pts[a].x += cx;
        pts[a].y += cy;
        pts[b].x -= cx;
        pts[b].y -= cy;
      } else if (fa && !fb) {
        pts[b].x -= cx * 2;
        pts[b].y -= cy * 2;
      } else if (!fa && fb) {
        pts[a].x += cx * 2;
        pts[a].y += cy * 2;
      }
    }
  }

  return pts;
}

function laplacianSmooth({points, edges, fixed, strength, iterations}) {
  const n = points.length;
  const adj = Array.from({length: n}, () => []);

  for (const [a, b] of edges) {
    adj[a].push(b);
    adj[b].push(a);
  }

  let pts = clone(points);

  for (let it = 0; it < iterations; it++) {
    const next = clone(pts);

    for (let i = 0; i < n; i++) {
      if (fixed.has(i) || !adj[i].length) continue;

      let ax = 0;
      let ay = 0;

      for (const j of adj[i]) {
        ax += pts[j].x;
        ay += pts[j].y;
      }

      ax /= adj[i].length;
      ay /= adj[i].length;

      next[i].x = pts[i].x * (1 - strength) + ax * strength;
      next[i].y = pts[i].y * (1 - strength) + ay * strength;
    }

    pts = next;
  }

  return pts;
}

function smoothCurrent() {
  const fixed = new Set(state.pinned);
  state.points = laplacianSmooth({
    points: state.points,
    edges: state.edges,
    fixed,
    strength: 0.12,
    iterations: 8
  });
  draw();
}

async function saveEdit() {
  const body = {
    points: state.points,
    edges: state.edges,
    skeleton: {
      points: state.points,
      edges: state.edges
    },
    control_points: state.points,
    pinned: Array.from(state.pinned)
  };

  let r = await fetch(`/save_skeleton_edit/${JOB_ID}/${state.code}/${state.variant}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });

  if (!r.ok) {
    alert("保存失败：HTTP " + r.status + "\\n请查看后端 save_skeleton_edit 接口接收格式。");
    return;
  }

  alert("已保存：" + state.code + " / " + state.variant);
}

function draw() {
  const rect = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);

  ctx.save();
  ctx.translate(state.offsetX, state.offsetY);
  ctx.scale(state.scale, state.scale);

  if (ui.showRawSvg.checked && state.rawSvgImage) {
    const b = (state.basePoints && state.basePoints.length)
      ? bounds(state.basePoints)
      : (state.svgBounds || {minX:0,minY:0,width:300,height:300});

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

  if (ui.showPreview.checked) {
    drawThick(state.points, state.edges, Number(ui.strokeThickness.value), "rgba(222,184,135,0.72)");
  }

  if (ui.showSkeleton.checked) {
    drawLines(state.points, state.edges, "#ff5959", 2 / state.scale);
  }

  if (ui.showPoints.checked) {
    drawPoints();
  }

  ctx.restore();
  updateStatus();
}

function drawThick(points, edges, width, color) {
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
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
  }

  ctx.restore();
}

function drawLines(points, edges, color, width) {
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
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
  }

  ctx.restore();
}

function drawPoints() {
  const r = 4.5 / state.scale;

  for (let i = 0; i < state.points.length; i++) {
    const p = state.points[i];

    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);

    if (state.pinned.has(i)) {
      ctx.fillStyle = "#2563eb";
      ctx.strokeStyle = "#fff";
    } else if (i === state.hover) {
      ctx.fillStyle = "#facc15";
      ctx.strokeStyle = "#111827";
    } else {
      ctx.fillStyle = "#fff";
      ctx.strokeStyle = "#ef4444";
    }

    ctx.lineWidth = 1.4 / state.scale;
    ctx.fill();
    ctx.stroke();
  }
}

function updateStatus() {
  ui.statusBox.innerHTML =
    `Job ID：${JOB_ID}<br>` +
    `当前字形：${state.code || "-"}<br>` +
    `当前版本：${state.variant || "-"}<br>` +
    `控制点：${state.points.length}<br>` +
    `边数量：${state.edges.length}<br>` +
    `固定点：${state.pinned.size}<br>` +
    `状态：整体弹性联动已启用`;
}

function exportSVG() {
  const b = bounds(state.points);
  const pad = 80;
  const w = b.width + pad * 2;
  const h = b.height + pad * 2;
  const ox = pad - b.minX;
  const oy = pad - b.minY;
  const thick = Number(ui.strokeThickness.value);

  let lines = "";

  for (const [a, b2] of state.edges) {
    const p1 = state.points[a];
    const p2 = state.points[b2];
    if (!p1 || !p2) continue;
    lines += `<line x1="${p1.x + ox}" y1="${p1.y + oy}" x2="${p2.x + ox}" y2="${p2.y + oy}" stroke="#d6a46a" stroke-width="${thick}" stroke-linecap="round" stroke-linejoin="round"/>\\n`;
  }

  const svg = `<?xml version="1.0" encoding="UTF-8"?><svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" xmlns="http://www.w3.org/2000/svg"><rect width="100%" height="100%" fill="white"/>${lines}</svg>`;
  downloadText(`${state.code}_${state.variant}_elastic.svg`, svg);
}

function exportPNG() {
  const a = document.createElement("a");
  a.download = `${state.code}_${state.variant}_elastic.png`;
  a.href = canvas.toDataURL("image/png");
  a.click();
}

function downloadText(name, text) {
  const blob = new Blob([text], {type: "text/plain;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function mouse(e) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: e.clientX - rect.left,
    y: e.clientY - rect.top
  };
}

function screenToWorld(p) {
  return {
    x: (p.x - state.offsetX) / state.scale,
    y: (p.y - state.offsetY) / state.scale
  };
}

function nearest(m, points, th) {
  let best = -1;
  let bd = th;

  for (let i = 0; i < points.length; i++) {
    const d = dist(m, points[i]);
    if (d < bd) {
      bd = d;
      best = i;
    }
  }

  return best;
}

function bounds(points) {
  if (!points.length) return {minX:0,minY:0,maxX:1,maxY:1,width:1,height:1,cx:0.5,cy:0.5};

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  for (const p of points) {
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x);
    maxY = Math.max(maxY, p.y);
  }

  return {
    minX,
    minY,
    maxX,
    maxY,
    width: maxX - minX,
    height: maxY - minY,
    cx: (minX + maxX) / 2,
    cy: (minY + maxY) / 2
  };
}

function clone(points) {
  return points.map(p => ({x: Number(p.x), y: Number(p.y)}));
}

function dist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}

init();
</script>
</body>
</html>
"""
    return html.replace("__JOB_ID__", job_id)
# ===== END_ELASTIC_SKELETON_EDITOR_ROUTE_V4 =====



# ===== REDIRECT_OLD_SKELETON_EDITOR_TO_ELASTIC_V1 =====
from starlette.responses import RedirectResponse as _ElasticSkeletonRedirectResponse

@app.middleware("http")
async def _redirect_old_skeleton_editor_to_elastic(request, call_next):
    """
    把旧骨架编辑器入口：
        /skeleton_editor/{job_id}

    自动替换为新弹性骨架编辑器入口：
        /skeleton_elastic_editor/{job_id}

    这样首页里原来的“打开骨架编辑器”按钮不用改前端，也会直接跳到新版页面。
    """
    path = request.url.path

    if path.startswith("/skeleton_editor/"):
        suffix = path[len("/skeleton_editor/"):]
        target = "/skeleton_elastic_editor/" + suffix

        if request.url.query:
            target += "?" + request.url.query

        return _ElasticSkeletonRedirectResponse(url=target, status_code=302)

    return await call_next(request)

# ===== END_REDIRECT_OLD_SKELETON_EDITOR_TO_ELASTIC_V1 =====



# ===== ELASTIC_SKELETON_MANIFEST_FIX_V1 =====
from pathlib import Path as _ElasticPath
import re as _elastic_re

@app.get("/elastic_skeleton_manifest/{job_id}")
async def elastic_skeleton_manifest_fix(job_id: str):
    """
    给新版弹性骨架编辑器使用的稳健字形清单接口。
    优先扫描 jobs/{job_id} 里的 svg/json/png 文件名；
    如果扫描失败，则自动兜底为传统蒙古文 U1820-U1842 + step_01-step_20。
    """
    job_dir = _ElasticPath("jobs") / job_id

    code_to_variants = {}

    def norm_code(s):
        if not s:
            return None
        m = _elastic_re.search(r"U([0-9A-Fa-f]{4,6})", str(s))
        if not m:
            return None
        return "U" + m.group(1).upper()

    def norm_step(s):
        if not s:
            return None
        m = _elastic_re.search(r"step[_\-]?(\d+)", str(s), flags=_elastic_re.I)
        if not m:
            return None
        return f"step_{int(m.group(1)):02d}"

    def add(code, variant):
        code = norm_code(code)
        variant = norm_step(variant) or "step_01"
        if not code:
            return
        code_to_variants.setdefault(code, set()).add(variant)

    if job_dir.exists():
        # A. 从文件路径扫描 U1820 / step_01 等信息
        for p in job_dir.rglob("*"):
            if not p.is_file():
                continue

            if p.suffix.lower() not in {".svg", ".json", ".png"}:
                continue

            rel = str(p.relative_to(job_dir))
            code = norm_code(rel)
            step = norm_step(rel)

            if code:
                add(code, step or "step_01")

        # B. 从常见文本文件中扫描
        for name in ["manifest.csv", "manifest.json", "report.txt", "fontforge.log"]:
            f = job_dir / name
            if not f.exists():
                continue

            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            codes = sorted(set(norm_code(x) for x in _elastic_re.findall(r"U[0-9A-Fa-f]{4,6}", content)))
            codes = [c for c in codes if c]

            steps = sorted(set(norm_step(x) for x in _elastic_re.findall(r"step[_\-]?\d+", content, flags=_elastic_re.I)))
            steps = [s for s in steps if s]

            if not steps:
                steps = [f"step_{i:02d}" for i in range(1, 21)]

            for c in codes:
                for s in steps:
                    add(c, s)

    # C. 如果仍然为空，兜底：传统蒙古文 35 个名义字符 + 20 步
    if not code_to_variants:
        fallback_codes = [f"U{cp:04X}" for cp in range(0x1820, 0x1843)]
        fallback_steps = [f"step_{i:02d}" for i in range(1, 21)]

        for c in fallback_codes:
            code_to_variants[c] = set(fallback_steps)

    def code_key(c):
        try:
            return int(c[1:], 16)
        except Exception:
            return 999999

    glyphs = []
    for code in sorted(code_to_variants.keys(), key=code_key):
        variants = sorted(
            code_to_variants[code],
            key=lambda x: int(x.split("_")[-1]) if "_" in x and x.split("_")[-1].isdigit() else 999
        )
        glyphs.append({
            "code": code,
            "variants": variants
        })

    return {
        "job_id": job_id,
        "count": len(glyphs),
        "glyphs": glyphs
    }

# ===== END_ELASTIC_SKELETON_MANIFEST_FIX_V1 =====


# ===== ELASTIC_SKELETON_EDITOR_V5_PROXY_ROUTE =====
from fastapi.responses import HTMLResponse as _ElasticHTMLResponseV5

@app.get("/skeleton_elastic_editor_v5/{job_id}", response_class=_ElasticHTMLResponseV5)
async def skeleton_elastic_editor_v5(job_id: str):
    html = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>骨架编辑器｜整体弹性联动版 V5</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f3f4f6;font-family:Arial,"Microsoft YaHei",sans-serif;color:#111827}
header{height:72px;background:#111;color:white;padding:14px 22px}
header h1{margin:0;font-size:24px}
header p{margin:6px 0 0;color:#d1d5db;font-size:13px}
.app{height:calc(100vh - 72px);display:grid;grid-template-columns:220px 1fr 280px;gap:14px;padding:14px}
.panel,.main{background:white;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
.panel{padding:14px;overflow:auto}
.main{padding:14px;display:flex;flex-direction:column;overflow:hidden}
.title{font-weight:700;margin-bottom:10px}
.glyph-card{border:1px solid #e5e7eb;border-radius:8px;padding:10px;margin-bottom:9px;background:#fafafa;cursor:pointer}
.glyph-card.active{background:#2563eb;color:white;border-color:#2563eb}
.glyph-card small{display:block;margin-top:5px;opacity:.75}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
button{border:0;border-radius:7px;padding:8px 12px;background:#4b5563;color:white;cursor:pointer;font-size:13px}
button.primary{background:#2563eb}
button.warn{background:#d97706}
button.good{background:#059669}
select{height:32px;border:1px solid #d1d5db;border-radius:6px;padding:0 8px;min-width:180px}
.canvas-wrap{flex:1;position:relative;border:1px solid #e5e7eb;border-radius:10px;background:white;overflow:hidden}
canvas{width:100%;height:100%;display:block}
.help{font-size:12px;line-height:1.7;color:#4b5563;margin-top:9px}
.row{display:flex;gap:8px;align-items:center;font-size:13px;margin:8px 0}
.group{margin:16px 0}
.group label{display:flex;justify-content:space-between;font-weight:700;font-size:13px;margin-bottom:6px}
input[type=range]{width:100%}
.status,.note{font-size:12px;line-height:1.7;border-radius:8px;padding:10px}
.status{background:#f9fafb;border:1px solid #e5e7eb}
.note{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;margin-top:12px}
.err{color:#b91c1c;white-space:pre-wrap;font-size:12px}
</style>
</head>
<body>
<header>
  <h1>骨架编辑器｜整体弹性联动版</h1>
  <p>自动提取骨架中心线与关键控制点；拖动一个关键点时，按骨架拓扑距离、欧氏距离、边长保持和平滑约束，让整体字形一起协调变化。</p>
</header>

<div class="app">
  <aside class="panel">
    <div class="title">字形列表</div>
    <div id="glyphList">加载中...</div>
  </aside>

  <main class="main">
    <div class="toolbar">
      <button class="primary" id="saveBtn">保存当前字形</button>
      <button id="exportSvgBtn">导出 SVG</button>
      <button id="exportPngBtn">导出 PNG</button>
      <button id="smoothBtn">平滑控制点</button>
      <button class="warn" id="resetBtn">恢复当前版本</button>
      <button class="good" id="reextractBtn">重新提取骨架</button>
      <select id="variantSelect"></select>
    </div>

    <div class="canvas-wrap">
      <canvas id="canvas"></canvas>
    </div>

    <div class="help">
      操作：拖动白色关键控制点进行整体弹性变形；Ctrl + 点击关键点可固定 / 取消固定；Shift + 拖动增强整体联动；Alt + 拖动增强局部变形。
    </div>
  </main>

  <aside class="panel">
    <div class="title">显示控制</div>

    <div class="row"><input id="showRawSvg" type="checkbox" checked><label for="showRawSvg">显示原始完整轮廓</label></div>
    <div class="row"><input id="showPreview" type="checkbox" checked><label for="showPreview">显示变形后预览</label></div>
    <div class="row"><input id="showSkeleton" type="checkbox" checked><label for="showSkeleton">显示骨架中心线</label></div>
    <div class="row"><input id="showSupportPoints" type="checkbox"><label for="showSupportPoints">显示支撑点</label></div>
    <div class="row"><input id="showPoints" type="checkbox" checked><label for="showPoints">显示关键控制点</label></div>

    <hr>

    <div class="group">
      <label>整体联动强度 <span id="globalValue">0.72</span></label>
      <input id="globalStrength" type="range" min="0" max="1" step="0.01" value="0.72">
    </div>

    <div class="group">
      <label>局部影响半径 <span id="radiusValue">260</span></label>
      <input id="influenceRadius" type="range" min="40" max="800" step="1" value="260">
    </div>

    <div class="group">
      <label>结构保持强度 <span id="preserveValue">0.84</span></label>
      <input id="preserveStrength" type="range" min="0" max="1" step="0.01" value="0.84">
    </div>

    <div class="group">
      <label>平滑强度 <span id="smoothValue">0.055</span></label>
      <input id="smoothStrength" type="range" min="0" max="0.25" step="0.005" value="0.055">
    </div>

    <div class="group">
      <label>预览字形粗细 <span id="thickValue">28</span></label>
      <input id="strokeThickness" type="range" min="4" max="90" step="1" value="28">
    </div>

    <div class="group">
      <label>关键控制点数量 <span id="handleValue">8</span></label>
      <input id="handleCount" type="range" min="5" max="14" step="1" value="8">
    </div>

    <div class="status" id="statusBox"></div>

    <div class="note">
      说明：如果后端没有现成骨架数据，这个版本会自动从当前字形 SVG 中提取一条代理中心线，并从中选出少量关键控制点。拖一个点时，整体字形会协调联动，而不是只拉动局部。
    </div>
  </aside>
</div>

<script>
"use strict";

const JOB_ID = "__JOB_ID__";

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");

const ui = {
  glyphList: document.getElementById("glyphList"),
  variantSelect: document.getElementById("variantSelect"),
  showRawSvg: document.getElementById("showRawSvg"),
  showPreview: document.getElementById("showPreview"),
  showSkeleton: document.getElementById("showSkeleton"),
  showSupportPoints: document.getElementById("showSupportPoints"),
  showPoints: document.getElementById("showPoints"),
  globalStrength: document.getElementById("globalStrength"),
  influenceRadius: document.getElementById("influenceRadius"),
  preserveStrength: document.getElementById("preserveStrength"),
  smoothStrength: document.getElementById("smoothStrength"),
  strokeThickness: document.getElementById("strokeThickness"),
  handleCount: document.getElementById("handleCount"),
  globalValue: document.getElementById("globalValue"),
  radiusValue: document.getElementById("radiusValue"),
  preserveValue: document.getElementById("preserveValue"),
  smoothValue: document.getElementById("smoothValue"),
  thickValue: document.getElementById("thickValue"),
  handleValue: document.getElementById("handleValue"),
  statusBox: document.getElementById("statusBox")
};

const state = {
  manifest: [],
  activeIndex: 0,
  code: null,
  variant: null,
  basePoints: [],
  points: [],
  edges: [],
  handleIndices: [],
  rawSvgText: "",
  rawSvgImage: null,
  svgBounds: null,
  pinned: new Set(),
  drag: null,
  hover: -1,
  scale: 1,
  offsetX: 0,
  offsetY: 0,
  loaded: false,
  skeletonSource: "none"
};

function normalizeManifest(raw) {
  let arr = raw;
  if (!Array.isArray(arr)) {
    arr = raw.glyphs || raw.items || raw.chars || raw.manifest || raw.data || raw.results || [];
  }
  if (!Array.isArray(arr) && typeof arr === "object") {
    arr = Object.entries(arr).map(([code, value]) => {
      if (typeof value === "object") return {code, ...value};
      return {code, variants: value};
    });
  }

  arr = arr.map(x => {
    const code = x.code || x.unicode || x.char || x.name || x.glyph || x.id;
    let variants = x.variants || x.steps || x.versions || x.variant_list || x.files || [];
    if (!Array.isArray(variants)) variants = Object.keys(variants || {});
    variants = variants.map(v => {
      if (typeof v === "string") return v;
      return v.variant || v.step || v.name || v.id || v.key || "step_01";
    });
    if (!variants.length) variants = ["step_01"];
    return {code, variants};
  }).filter(x => x.code);

  return arr;
}

async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
  return await r.json();
}

async function fetchText(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
  return await r.text();
}

function toPoint(p) {
  if (!p) return null;
  if (Array.isArray(p) && p.length >= 2) return {x: Number(p[0]), y: Number(p[1])};
  if (typeof p === "object") {
    const x = Number(p.x ?? p.X ?? p.cx ?? p[0]);
    const y = Number(p.y ?? p.Y ?? p.cy ?? p[1]);
    if (Number.isFinite(x) && Number.isFinite(y)) return {x, y};
  }
  return null;
}

function normalizeSkeleton(data) {
  let points = [];
  let edges = [];

  if (Array.isArray(data?.points)) points = data.points;
  else if (Array.isArray(data?.control_points)) points = data.control_points;
  else if (Array.isArray(data?.skeleton_points)) points = data.skeleton_points;
  else if (data?.skeleton && Array.isArray(data.skeleton.points)) points = data.skeleton.points;
  else if (data?.skeleton && Array.isArray(data.skeleton.nodes)) points = data.skeleton.nodes;
  else if (Array.isArray(data?.nodes)) points = data.nodes;

  if (Array.isArray(data?.edges)) edges = data.edges;
  else if (Array.isArray(data?.skeleton_edges)) edges = data.skeleton_edges;
  else if (data?.skeleton && Array.isArray(data.skeleton.edges)) edges = data.skeleton.edges;

  let finalPoints = [];

  if (Array.isArray(data?.strokes) && !points.length) {
    for (const stroke of data.strokes) {
      const spts = stroke.points || stroke.nodes || stroke;
      if (!Array.isArray(spts)) continue;
      let last = -1;
      for (const p of spts) {
        const q = toPoint(p);
        if (!q) continue;
        finalPoints.push(q);
        const now = finalPoints.length - 1;
        if (last >= 0) edges.push([last, now]);
        last = now;
      }
    }
  } else {
    finalPoints = (points || []).map(toPoint).filter(Boolean);
  }

  edges = (edges || []).map(e => {
    if (Array.isArray(e)) return [Number(e[0]), Number(e[1])];
    return [Number(e.a ?? e.from ?? e.source ?? e.i), Number(e.b ?? e.to ?? e.target ?? e.j)];
  }).filter(e => Number.isFinite(e[0]) && Number.isFinite(e[1]) && e[0] >= 0 && e[1] >= 0 && e[0] < finalPoints.length && e[1] < finalPoints.length);

  if (!edges.length && finalPoints.length > 1) {
    for (let i = 0; i < finalPoints.length - 1; i++) edges.push([i, i + 1]);
  }

  return {points: finalPoints, edges};
}

function parseSvgBounds(svgText) {
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(svgText, "image/svg+xml");
    const svg = doc.documentElement;

    const vb = svg.getAttribute("viewBox");
    if (vb) {
      const nums = vb.trim().split(/[\s,]+/).map(Number);
      if (nums.length === 4 && nums.every(n => Number.isFinite(n))) {
        const [minX, minY, width, height] = nums;
        return {
          minX,
          minY,
          maxX: minX + width,
          maxY: minY + height,
          width,
          height,
          cx: minX + width / 2,
          cy: minY + height / 2
        };
      }
    }

    const w = parseFloat((svg.getAttribute("width") || "300").replace(/[a-z%]+/ig, ""));
    const h = parseFloat((svg.getAttribute("height") || "300").replace(/[a-z%]+/ig, ""));
    if (Number.isFinite(w) && Number.isFinite(h)) {
      return {
        minX: 0,
        minY: 0,
        maxX: w,
        maxY: h,
        width: w,
        height: h,
        cx: w / 2,
        cy: h / 2
      };
    }
  } catch (e) {
    console.warn("parseSvgBounds failed:", e);
  }

  return {
    minX: 0,
    minY: 0,
    maxX: 300,
    maxY: 300,
    width: 300,
    height: 300,
    cx: 150,
    cy: 150
  };
}

function svgToImage(svgText) {
  return new Promise((resolve, reject) => {
    const blob = new Blob([svgText], {type: "image/svg+xml"});
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = err => {
      URL.revokeObjectURL(url);
      reject(err);
    };
    img.src = url;
  });
}

async function init() {
  labels();
  bind();
  resize();

  try {
    const rawManifest = await fetchJson(`/elastic_skeleton_manifest/${JOB_ID}`);
    state.manifest = normalizeManifest(rawManifest);
    if (!state.manifest.length) throw new Error("manifest 为空，无法读取字形列表。");
    renderGlyphList();
    await selectGlyph(0);
  } catch (e) {
    ui.glyphList.innerHTML = `<div class="err">${e.stack || e}</div>`;
    ui.statusBox.innerHTML = `<div class="err">${e.stack || e}</div>`;
  }
}

function bind() {
  window.addEventListener("resize", () => { resize(); draw(); });

  canvas.addEventListener("pointerdown", pointerDown);
  canvas.addEventListener("pointermove", pointerMove);
  canvas.addEventListener("pointerup", pointerUp);
  canvas.addEventListener("pointerleave", pointerUp);

  document.getElementById("saveBtn").onclick = saveEdit;
  document.getElementById("exportSvgBtn").onclick = exportSVG;
  document.getElementById("exportPngBtn").onclick = exportPNG;
  document.getElementById("smoothBtn").onclick = smoothCurrent;
  document.getElementById("resetBtn").onclick = () => {
    state.points = clone(state.basePoints);
    state.pinned.clear();
    draw();
  };
  document.getElementById("reextractBtn").onclick = () => {
    if (state.rawSvgImage && state.svgBounds) {
      const proxy = generateProxySkeletonFromSvg(state.rawSvgImage, state.svgBounds, Number(ui.handleCount.value));
      state.basePoints = clone(proxy.points);
      state.points = clone(proxy.points);
      state.edges = proxy.edges.map(e => [...e]);
      state.handleIndices = [...proxy.handleIndices];
      state.pinned.clear();
      state.skeletonSource = "proxy-svg";
      fit();
      draw();
    }
  };

  ui.variantSelect.onchange = async () => {
    state.variant = ui.variantSelect.value;
    await loadGlyphData();
  };

  [
    ui.showRawSvg, ui.showPreview, ui.showSkeleton, ui.showSupportPoints, ui.showPoints,
    ui.globalStrength, ui.influenceRadius, ui.preserveStrength, ui.smoothStrength,
    ui.strokeThickness, ui.handleCount
  ].forEach(el => el.addEventListener("input", () => { labels(); draw(); }));
}

function labels() {
  ui.globalValue.textContent = Number(ui.globalStrength.value).toFixed(2);
  ui.radiusValue.textContent = ui.influenceRadius.value;
  ui.preserveValue.textContent = Number(ui.preserveStrength.value).toFixed(2);
  ui.smoothValue.textContent = Number(ui.smoothStrength.value).toFixed(3);
  ui.thickValue.textContent = ui.strokeThickness.value;
  ui.handleValue.textContent = ui.handleCount.value;
}

function resize() {
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(300, Math.floor(rect.width * dpr));
  canvas.height = Math.max(300, Math.floor(rect.height * dpr));
  canvas.style.width = rect.width + "px";
  canvas.style.height = rect.height + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  fit();
}

function renderGlyphList() {
  ui.glyphList.innerHTML = "";
  state.manifest.forEach((g, i) => {
    const div = document.createElement("div");
    div.className = "glyph-card" + (i === state.activeIndex ? " active" : "");
    div.innerHTML = `<b>${g.code}</b><small>${g.variants.length} 个版本</small>`;
    div.onclick = () => selectGlyph(i);
    ui.glyphList.appendChild(div);
  });
}

async function selectGlyph(i) {
  state.activeIndex = i;
  const item = state.manifest[i];
  state.code = item.code;

  ui.variantSelect.innerHTML = "";
  item.variants.forEach(v => {
    const op = document.createElement("option");
    op.value = v;
    op.textContent = v;
    ui.variantSelect.appendChild(op);
  });

  state.variant = item.variants[0] || "step_01";
  ui.variantSelect.value = state.variant;

  renderGlyphList();
  await loadGlyphData();
}

async function loadGlyphData() {
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
    state.basePoints = clone(norm.points);
    state.points = clone(norm.points);
    state.edges = norm.edges.map(e => [...e]);
    state.handleIndices = selectKeyHandlesGraph(state.points, state.edges, Number(ui.handleCount.value));
    state.skeletonSource = "skeleton-json";
  } else if (state.rawSvgImage && state.svgBounds) {
    const proxy = generateProxySkeletonFromSvg(state.rawSvgImage, state.svgBounds, Number(ui.handleCount.value));
    state.basePoints = clone(proxy.points);
    state.points = clone(proxy.points);
    state.edges = proxy.edges.map(e => [...e]);
    state.handleIndices = [...proxy.handleIndices];
    state.skeletonSource = "proxy-svg";
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
}


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

function smoothPolyline(points, rounds=2, window=5) {
  let arr = points.map(p => ({...p}));
  const half = Math.floor(window / 2);

  for (let r = 0; r < rounds; r++) {
    const next = arr.map((p, i) => {
      if (i === 0 || i === arr.length - 1) return {...p};
      let sx = 0, sy = 0, c = 0;
      for (let k = -half; k <= half; k++) {
        const j = i + k;
        if (j < 0 || j >= arr.length) continue;
        sx += arr[j].x;
        sy += arr[j].y;
        c++;
      }
      return {x: sx / c, y: sy / c, t: p.t};
    });
    arr = next;
  }
  return arr;
}

function selectKeyHandles(points, desired=8) {
  if (!points.length) return [];
  if (points.length <= desired) return points.map((_, i) => i);

  const selected = new Set([0, points.length - 1]);

  const curvatures = [];
  for (let i = 1; i < points.length - 1; i++) {
    const a = points[i - 1];
    const b = points[i];
    const c = points[i + 1];

    const v1x = b.x - a.x, v1y = b.y - a.y;
    const v2x = c.x - b.x, v2y = c.y - b.y;

    const n1 = Math.hypot(v1x, v1y);
    const n2 = Math.hypot(v2x, v2y);
    if (n1 < 1e-6 || n2 < 1e-6) continue;

    const dot = (v1x * v2x + v1y * v2y) / (n1 * n2);
    const ang = Math.acos(Math.max(-1, Math.min(1, dot)));
    curvatures.push({i, ang});
  }

  curvatures.sort((a, b) => b.ang - a.ang);

  const curvaturePick = Math.min(Math.max(2, Math.floor(desired / 2)), curvatures.length);
  for (let k = 0; k < curvaturePick; k++) selected.add(curvatures[k].i);

  while (selected.size < desired) {
    const step = (points.length - 1) / (desired - 1);
    for (let k = 0; k < desired && selected.size < desired; k++) {
      selected.add(Math.round(k * step));
    }
  }

  return Array.from(selected).sort((a, b) => a - b);
}

function fit() {
  const r = canvas.getBoundingClientRect();
  const pad = 90;

  let b = null;
  if (state.points && state.points.length) b = bounds(state.points);
  else if (state.svgBounds) b = state.svgBounds;
  else {
    state.scale = 1;
    state.offsetX = r.width / 2;
    state.offsetY = r.height / 2;
    return;
  }

  const sx = (r.width - pad * 2) / Math.max(1, b.width);
  const sy = (r.height - pad * 2) / Math.max(1, b.height);
  state.scale = Math.min(sx, sy);
  state.offsetX = r.width / 2 - b.cx * state.scale;
  state.offsetY = r.height / 2 - b.cy * state.scale;
}

function pointerDown(e) {
  if (!state.loaded || !state.points.length) return;
  const m = screenToWorld(mouse(e));
  const hit = nearestHandle(m, state.points, state.handleIndices, 18 / state.scale);
  if (hit < 0) return;

  if (e.ctrlKey || e.metaKey) {
    state.pinned.has(hit) ? state.pinned.delete(hit) : state.pinned.add(hit);
    draw();
    return;
  }

  canvas.setPointerCapture(e.pointerId);
  state.drag = {
    handle: hit,
    basePoints: clone(state.points),
    basePinned: new Set(state.pinned)
  };
}

function pointerMove(e) {
  if (!state.loaded) return;
  const m = screenToWorld(mouse(e));
  state.hover = nearestHandle(m, state.points, state.handleIndices, 18 / state.scale);

  if (!state.drag) {
    draw();
    return;
  }

  let globalStrength = Number(ui.globalStrength.value);
  let influenceRadius = Number(ui.influenceRadius.value);

  if (e.shiftKey) {
    globalStrength = Math.min(1, globalStrength + 0.20);
    influenceRadius *= 1.45;
  }

  if (e.altKey) {
    globalStrength = Math.max(0.05, globalStrength - 0.25);
    influenceRadius *= 0.60;
  }

  state.points = elasticDeform({
    basePoints: state.drag.basePoints,
    edges: state.edges,
    handleIndex: state.drag.handle,
    target: m,
    pinned: state.drag.basePinned,
    globalStrength,
    influenceRadius,
    preserveStrength: Number(ui.preserveStrength.value),
    smoothStrength: Number(ui.smoothStrength.value),
    preserveIterations: 22,
    smoothIterations: 4
  });

  draw();
}

function pointerUp() {
  state.drag = null;
}

function elasticDeform(o) {
  const handle = o.basePoints[o.handleIndex];
  const dx = o.target.x - handle.x;
  const dy = o.target.y - handle.y;
  const gd = graphDistances(o.basePoints, o.edges, o.handleIndex);

  const fixed = new Set(o.pinned);
  fixed.add(o.handleIndex);

  let pts = o.basePoints.map((p, i) => {
    if (o.pinned.has(i)) return {...p};

    let w = influenceWeight({
      p,
      handle,
      graphDistance: gd[i],
      radius: o.influenceRadius,
      globalStrength: o.globalStrength
    });

    if (i === o.handleIndex) w = 1;

    return {
      x: p.x + dx * w,
      y: p.y + dy * w
    };
  });

  pts[o.handleIndex] = {...o.target};

  pts = preserveEdgeLengths({
    points: pts,
    basePoints: o.basePoints,
    edges: o.edges,
    fixed,
    strength: o.preserveStrength,
    iterations: o.preserveIterations
  });

  pts = laplacianSmooth({
    points: pts,
    edges: o.edges,
    fixed,
    strength: o.smoothStrength,
    iterations: o.smoothIterations
  });

  pts[o.handleIndex] = {...o.target};

  return pts;
}

function influenceWeight({p, handle, graphDistance, radius, globalStrength}) {
  const euclid = dist(p, handle);
  let topoWeight = 0;

  if (Number.isFinite(graphDistance)) {
    const t = clamp(1 - graphDistance / radius, 0, 1);
    topoWeight = t * t * (3 - 2 * t);
  }

  const euclidWeight = Math.exp(-(euclid * euclid) / (2 * radius * radius));
  let w = Math.max(topoWeight, euclidWeight * 0.68);

  // 让整体始终带一点联动
  w = w * globalStrength + 0.12 * globalStrength;

  return clamp(w, 0, 1);
}

function graphDistances(points, edges, start) {
  const n = points.length;
  const adj = Array.from({length: n}, () => []);

  for (const [a, b] of edges) {
    const l = dist(points[a], points[b]);
    adj[a].push([b, l]);
    adj[b].push([a, l]);
  }

  const d = Array(n).fill(Infinity);
  const used = Array(n).fill(false);
  d[start] = 0;

  for (let k = 0; k < n; k++) {
    let u = -1;
    let best = Infinity;

    for (let i = 0; i < n; i++) {
      if (!used[i] && d[i] < best) {
        best = d[i];
        u = i;
      }
    }
    if (u < 0) break;

    used[u] = true;

    for (const [v, w] of adj[u]) {
      if (d[u] + w < d[v]) d[v] = d[u] + w;
    }
  }

  return d;
}

function preserveEdgeLengths({points, basePoints, edges, fixed, strength, iterations}) {
  let pts = clone(points);
  const rest = edges.map(([a, b]) => dist(basePoints[a], basePoints[b]));

  for (let it = 0; it < iterations; it++) {
    for (let i = 0; i < edges.length; i++) {
      const [a, b] = edges[i];
      const pa = pts[a];
      const pb = pts[b];

      const vx = pb.x - pa.x;
      const vy = pb.y - pa.y;
      const len = Math.hypot(vx, vy);
      if (len < 0.0001) continue;

      const diff = (len - rest[i]) / len;
      const cx = vx * diff * 0.5 * strength;
      const cy = vy * diff * 0.5 * strength;

      const fa = fixed.has(a);
      const fb = fixed.has(b);

      if (!fa && !fb) {
        pts[a].x += cx; pts[a].y += cy;
        pts[b].x -= cx; pts[b].y -= cy;
      } else if (fa && !fb) {
        pts[b].x -= cx * 2; pts[b].y -= cy * 2;
      } else if (!fa && fb) {
        pts[a].x += cx * 2; pts[a].y += cy * 2;
      }
    }
  }

  return pts;
}

function laplacianSmooth({points, edges, fixed, strength, iterations}) {
  const n = points.length;
  const adj = Array.from({length: n}, () => []);

  for (const [a, b] of edges) {
    adj[a].push(b);
    adj[b].push(a);
  }

  let pts = clone(points);

  for (let it = 0; it < iterations; it++) {
    const next = clone(pts);

    for (let i = 0; i < n; i++) {
      if (fixed.has(i) || !adj[i].length) continue;

      let ax = 0, ay = 0;
      for (const j of adj[i]) {
        ax += pts[j].x;
        ay += pts[j].y;
      }

      ax /= adj[i].length;
      ay /= adj[i].length;

      next[i].x = pts[i].x * (1 - strength) + ax * strength;
      next[i].y = pts[i].y * (1 - strength) + ay * strength;
    }

    pts = next;
  }

  return pts;
}

function smoothCurrent() {
  const fixed = new Set(state.pinned);
  state.points = laplacianSmooth({
    points: state.points,
    edges: state.edges,
    fixed,
    strength: 0.12,
    iterations: 8
  });
  draw();
}

async function saveEdit() {
  const body = {
    points: state.points,
    edges: state.edges,
    handle_indices: state.handleIndices,
    skeleton_source: state.skeletonSource,
    skeleton: {
      points: state.points,
      edges: state.edges
    },
    control_points: state.points,
    pinned: Array.from(state.pinned)
  };

  let r = await fetch(`/save_skeleton_edit/${JOB_ID}/${state.code}/${state.variant}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });

  if (!r.ok) {
    alert("保存失败：HTTP " + r.status + "\\n请查看后端 save_skeleton_edit 接口接收格式。");
    return;
  }

  alert("已保存：" + state.code + " / " + state.variant);
}

function draw() {
  const rect = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);

  ctx.save();
  ctx.translate(state.offsetX, state.offsetY);
  ctx.scale(state.scale, state.scale);

  if (ui.showRawSvg.checked && state.rawSvgImage) {
    const b = state.svgBounds || {minX:0, minY:0, width:300, height:300};
    ctx.save();
    ctx.globalAlpha = 0.18;
    ctx.drawImage(state.rawSvgImage, b.minX, b.minY, Math.max(1, b.width), Math.max(1, b.height));
    ctx.restore();
  }

  if (ui.showPreview.checked && state.points.length) {
    drawThick(state.points, state.edges, Number(ui.strokeThickness.value), "rgba(222,184,135,0.55)");
  }

  if (ui.showSkeleton.checked && state.points.length) {
    drawLines(state.points, state.edges, "#ff5f5f", 2.6 / state.scale);
  }

  if (ui.showSupportPoints.checked && state.points.length) {
    drawSupportPoints();
  }

  if (ui.showPoints.checked && state.handleIndices.length) {
    drawHandles();
  }

  ctx.restore();
  updateStatus();
}

function drawThick(points, edges, width, color) {
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
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
  }

  ctx.restore();
}

function drawLines(points, edges, color, width) {
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
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
  }

  ctx.restore();
}

function drawSupportPoints() {
  const r = 2.3 / state.scale;
  ctx.save();
  for (let i = 0; i < state.points.length; i++) {
    const p = state.points[i];
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(239,68,68,0.45)";
    ctx.fill();
  }
  ctx.restore();
}

function drawHandles() {
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

function updateStatus() {
  ui.statusBox.innerHTML =
    `Job ID：${JOB_ID}<br>` +
    `当前字形：${state.code || "-"}<br>` +
    `当前版本：${state.variant || "-"}<br>` +
    `骨架来源：${state.skeletonSource}<br>` +
    `支撑点：${state.points.length}<br>` +
    `关键控制点：${state.handleIndices.length}<br>` +
    `边数量：${state.edges.length}<br>` +
    `固定点：${state.pinned.size}<br>` +
    `状态：整体弹性联动已启用`;
}

function exportSVG() {
  const b = bounds(state.points.length ? state.points : [{x:0, y:0}, {x:300, y:300}]);
  const pad = 80;
  const w = b.width + pad * 2;
  const h = b.height + pad * 2;
  const ox = pad - b.minX;
  const oy = pad - b.minY;
  const thick = Number(ui.strokeThickness.value);

  let lines = "";

  for (const [a, b2] of state.edges) {
    const p1 = state.points[a];
    const p2 = state.points[b2];
    if (!p1 || !p2) continue;
    lines += `<line x1="${p1.x + ox}" y1="${p1.y + oy}" x2="${p2.x + ox}" y2="${p2.y + oy}" stroke="#d6a46a" stroke-width="${thick}" stroke-linecap="round" stroke-linejoin="round"/>\\n`;
  }

  for (const idx of state.handleIndices) {
    const p = state.points[idx];
    if (!p) continue;
    lines += `<circle cx="${p.x + ox}" cy="${p.y + oy}" r="4" fill="white" stroke="#ef4444" stroke-width="1.5"/>\\n`;
  }

  const svg = `<?xml version="1.0" encoding="UTF-8"?><svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" xmlns="http://www.w3.org/2000/svg"><rect width="100%" height="100%" fill="white"/>${lines}</svg>`;
  downloadText(`${state.code}_${state.variant}_elastic_v5.svg`, svg);
}

function exportPNG() {
  const a = document.createElement("a");
  a.download = `${state.code}_${state.variant}_elastic_v5.png`;
  a.href = canvas.toDataURL("image/png");
  a.click();
}

function downloadText(name, text) {
  const blob = new Blob([text], {type: "text/plain;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function mouse(e) {
  const rect = canvas.getBoundingClientRect();
  return {x: e.clientX - rect.left, y: e.clientY - rect.top};
}

function screenToWorld(p) {
  return {x: (p.x - state.offsetX) / state.scale, y: (p.y - state.offsetY) / state.scale};
}

function nearestHandle(m, points, handleIndices, th) {
  let best = -1;
  let bd = th;
  for (const idx of handleIndices) {
    const d = dist(m, points[idx]);
    if (d < bd) {
      bd = d;
      best = idx;
    }
  }
  return best;
}

function bounds(points) {
  if (!points.length) return {minX:0, minY:0, maxX:1, maxY:1, width:1, height:1, cx:0.5, cy:0.5};

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const p of points) {
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x);
    maxY = Math.max(maxY, p.y);
  }
  return {
    minX, minY, maxX, maxY,
    width: maxX - minX,
    height: maxY - minY,
    cx: (minX + maxX) / 2,
    cy: (minY + maxY) / 2
  };
}

function clone(points) {
  return points.map(p => ({x: Number(p.x), y: Number(p.y)}));
}

function dist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}


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

init();
</script>
</body>
</html>
"""
    return html.replace("__JOB_ID__", job_id)
# ===== END_ELASTIC_SKELETON_EDITOR_V5_PROXY_ROUTE =====



# ===== REDIRECT_ELASTIC_EDITOR_TO_V5_PROXY =====
from starlette.responses import RedirectResponse as _ElasticV5RedirectResponse

@app.middleware("http")
async def _redirect_elastic_editor_to_v5_proxy(request, call_next):
    path = request.url.path

    if path.startswith("/skeleton_elastic_editor/") and not path.startswith("/skeleton_elastic_editor_v5/"):
        suffix = path[len("/skeleton_elastic_editor/"):]
        target = "/skeleton_elastic_editor_v5/" + suffix
        if request.url.query:
            target += "?" + request.url.query
        return _ElasticV5RedirectResponse(url=target, status_code=302)

    return await call_next(request)

# ===== END_REDIRECT_ELASTIC_EDITOR_TO_V5_PROXY =====



# ===== PATCH_REAL_SKELETON_EXTRACT_INSTALLED =====


# ===== SVG_PATH_REAL_GLYPH_EDITOR_V1 =====
from fastapi import Request as _SvgEditRequest
from fastapi.responses import HTMLResponse as _SvgEditHTMLResponse
from fastapi.responses import Response as _SvgEditResponse
from fastapi.responses import JSONResponse as _SvgEditJSONResponse
from starlette.responses import RedirectResponse as _SvgEditRedirectResponse
from pathlib import Path as _SvgEditPath
import json as _svg_edit_json
import re as _svg_edit_re

@app.get("/svg_path_editor/{job_id}", response_class=_SvgEditHTMLResponse)
async def svg_path_real_editor(job_id: str):
    html = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>真实字形轮廓编辑器</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f3f4f6;font-family:Arial,"Microsoft YaHei",sans-serif;color:#111827}
header{height:72px;background:#111;color:#fff;padding:14px 22px}
header h1{margin:0;font-size:24px}
header p{margin:6px 0 0;font-size:13px;color:#d1d5db}
.app{height:calc(100vh - 72px);display:grid;grid-template-columns:220px 1fr 285px;gap:14px;padding:14px}
.panel,.main{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
.panel{padding:14px;overflow:auto}
.main{padding:14px;display:flex;flex-direction:column;overflow:hidden}
.title{font-weight:700;margin-bottom:10px}
.card{border:1px solid #e5e7eb;border-radius:8px;padding:10px;margin-bottom:9px;background:#fafafa;cursor:pointer}
.card.active{background:#2563eb;color:white;border-color:#2563eb}
.card small{display:block;margin-top:5px;opacity:.75}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
button{border:0;border-radius:7px;padding:8px 12px;background:#4b5563;color:white;cursor:pointer;font-size:13px}
button.primary{background:#2563eb}
button.green{background:#059669}
button.warn{background:#d97706}
select{height:32px;border:1px solid #d1d5db;border-radius:6px;padding:0 8px;min-width:150px}
.stage-wrap{flex:1;border:1px solid #e5e7eb;border-radius:10px;background:white;overflow:hidden;position:relative}
svg#stage{width:100%;height:100%;display:block;background:white}
.help{font-size:12px;color:#4b5563;line-height:1.7;margin-top:8px}
.row{display:flex;gap:8px;align-items:center;font-size:13px;margin:8px 0}
.group{margin:16px 0}
.group label{display:flex;justify-content:space-between;font-size:13px;font-weight:700;margin-bottom:6px}
input[type=range]{width:100%}
.status,.note{font-size:12px;line-height:1.7;border-radius:8px;padding:10px}
.status{background:#f9fafb;border:1px solid #e5e7eb}
.note{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;margin-top:12px}
.err{color:#b91c1c;font-size:12px;white-space:pre-wrap}
</style>
</head>
<body>
<header>
  <h1>真实字形轮廓编辑器</h1>
  <p>直接编辑 SVG 字形轮廓控制点。拖动红色 / 蓝色控制点，字形轮廓会立即变化。</p>
</header>

<div class="app">
  <aside class="panel">
    <div class="title">字形列表</div>
    <div id="glyphList">加载中...</div>
  </aside>

  <main class="main">
    <div class="toolbar">
      <button class="primary" id="saveBtn">保存 SVG 编辑</button>
      <button class="green" id="loadSavedBtn">读取已保存</button>
      <button id="downloadSvgBtn">下载 SVG</button>
      <button class="warn" id="reloadRawBtn">恢复原始 SVG</button>
      <select id="variantSelect"></select>
    </div>

    <div class="stage-wrap">
      <svg id="stage" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>

    <div class="help">
      操作：拖动红色点编辑轮廓端点；拖动蓝色点编辑 Bézier 控制点；滚轮缩放；按住鼠标中键或右键拖动画布。
    </div>
  </main>

  <aside class="panel">
    <div class="title">控制</div>

    <div class="row"><input type="checkbox" id="showPoints" checked><label for="showPoints">显示控制点</label></div>
    <div class="row"><input type="checkbox" id="showControls" checked><label for="showControls">显示控制线</label></div>

    <hr>

    <div class="group">
      <label>控制点大小 <span id="pointSizeValue">4</span></label>
      <input id="pointSize" type="range" min="2" max="10" step="1" value="4">
    </div>

    <div class="group">
      <label>字形透明度 <span id="opacityValue">0.75</span></label>
      <input id="glyphOpacity" type="range" min="0.2" max="1" step="0.05" value="0.75">
    </div>

    <div class="status" id="statusBox"></div>

    <div class="note">
      这个版本不再做骨架提取，也不做图片网格假变形，而是直接修改 SVG path 的 d 数据。它就是可编辑字形轮廓的版本。
    </div>
  </aside>
</div>

<script>
"use strict";

const JOB_ID = "__JOB_ID__";
const NS = "http://www.w3.org/2000/svg";

const stage = document.getElementById("stage");

const ui = {
  glyphList: document.getElementById("glyphList"),
  variantSelect: document.getElementById("variantSelect"),
  saveBtn: document.getElementById("saveBtn"),
  loadSavedBtn: document.getElementById("loadSavedBtn"),
  downloadSvgBtn: document.getElementById("downloadSvgBtn"),
  reloadRawBtn: document.getElementById("reloadRawBtn"),
  showPoints: document.getElementById("showPoints"),
  showControls: document.getElementById("showControls"),
  pointSize: document.getElementById("pointSize"),
  pointSizeValue: document.getElementById("pointSizeValue"),
  glyphOpacity: document.getElementById("glyphOpacity"),
  opacityValue: document.getElementById("opacityValue"),
  statusBox: document.getElementById("statusBox")
};

const state = {
  manifest: [],
  activeIndex: 0,
  code: null,
  variant: null,
  rawSvgText: "",
  viewBox: [0,0,1000,1000],
  zoom: 1,
  panX: 0,
  panY: 0,
  paths: [],
  handles: [],
  drag: null,
  panning: null
};

function labels() {
  ui.pointSizeValue.textContent = ui.pointSize.value;
  ui.opacityValue.textContent = ui.glyphOpacity.value;
}

function normalizeManifest(raw) {
  let arr = raw;

  if (!Array.isArray(arr)) {
    arr = raw.glyphs || raw.items || raw.chars || raw.manifest || raw.data || raw.results || [];
  }

  if (!Array.isArray(arr) && typeof arr === "object") {
    arr = Object.entries(arr).map(([code, value]) => {
      if (typeof value === "object") return {code, ...value};
      return {code, variants: value};
    });
  }

  return arr.map(x => {
    const code = x.code || x.unicode || x.char || x.name || x.glyph || x.id;
    let variants = x.variants || x.steps || x.versions || x.variant_list || x.files || [];
    if (!Array.isArray(variants)) variants = Object.keys(variants || {});
    variants = variants.map(v => {
      if (typeof v === "string") return v;
      return v.variant || v.step || v.name || v.id || v.key || "step_01";
    });
    if (!variants.length) variants = ["step_01"];
    return {code, variants};
  }).filter(x => x.code);
}

function fallbackManifest() {
  const out = [];
  for (let cp = 0x1820; cp <= 0x1842; cp++) {
    const code = "U" + cp.toString(16).toUpperCase().padStart(4, "0");
    const variants = [];
    for (let i = 1; i <= 20; i++) variants.push("step_" + String(i).padStart(2, "0"));
    out.push({code, variants});
  }
  return out;
}

async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " HTTP " + r.status);
  return await r.json();
}

async function fetchText(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " HTTP " + r.status);
  return await r.text();
}

function tokenizePath(d) {
  return d.match(/[a-zA-Z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?/g) || [];
}

function isCmd(t) {
  return /^[a-zA-Z]$/.test(t);
}

const ARG_COUNT = {
  M:2, L:2, H:1, V:1, C:6, S:4, Q:4, T:2, A:7, Z:0
};

function parsePath(d) {
  const tokens = tokenizePath(d);
  let i = 0;
  let cmd = null;
  let cx = 0, cy = 0;
  let sx = 0, sy = 0;
  const segs = [];

  function num() {
    return Number(tokens[i++]);
  }

  while (i < tokens.length) {
    if (isCmd(tokens[i])) {
      cmd = tokens[i++];
    }

    if (!cmd) break;

    const upper = cmd.toUpperCase();
    const rel = cmd !== upper;

    if (upper === "Z") {
      segs.push({cmd:"Z", vals:[]});
      cx = sx;
      cy = sy;
      cmd = null;
      continue;
    }

    const n = ARG_COUNT[upper];
    if (!n) break;

    let firstM = upper === "M";

    while (i < tokens.length && !isCmd(tokens[i])) {
      if (i + n > tokens.length) break;

      let vals = [];
      for (let k = 0; k < n; k++) vals.push(num());

      let outCmd = upper;

      if (upper === "M") {
        let x = vals[0], y = vals[1];
        if (rel) { x += cx; y += cy; }
        cx = x; cy = y; sx = x; sy = y;
        segs.push({cmd: firstM ? "M" : "L", vals:[x,y]});
        firstM = false;
        outCmd = "L";
        continue;
      }

      if (upper === "L" || upper === "T") {
        let x = vals[0], y = vals[1];
        if (rel) { x += cx; y += cy; }
        segs.push({cmd: upper, vals:[x,y]});
        cx = x; cy = y;
        continue;
      }

      if (upper === "H") {
        let x = vals[0];
        if (rel) x += cx;
        segs.push({cmd:"L", vals:[x, cy]});
        cx = x;
        continue;
      }

      if (upper === "V") {
        let y = vals[0];
        if (rel) y += cy;
        segs.push({cmd:"L", vals:[cx, y]});
        cy = y;
        continue;
      }

      if (upper === "C") {
        if (rel) {
          vals[0]+=cx; vals[1]+=cy;
          vals[2]+=cx; vals[3]+=cy;
          vals[4]+=cx; vals[5]+=cy;
        }
        segs.push({cmd:"C", vals});
        cx = vals[4]; cy = vals[5];
        continue;
      }

      if (upper === "S" || upper === "Q") {
        if (rel) {
          vals[0]+=cx; vals[1]+=cy;
          vals[2]+=cx; vals[3]+=cy;
        }
        segs.push({cmd:upper, vals});
        cx = vals[2]; cy = vals[3];
        continue;
      }

      if (upper === "A") {
        if (rel) {
          vals[5] += cx;
          vals[6] += cy;
        }
        segs.push({cmd:"A", vals});
        cx = vals[5]; cy = vals[6];
        continue;
      }
    }
  }

  return segs;
}

function fmt(n) {
  return Number(n).toFixed(2).replace(/\.00$/,"").replace(/(\.\d)0$/,"$1");
}

function buildPath(segs) {
  return segs.map(s => {
    if (s.cmd === "Z") return "Z";
    return s.cmd + " " + s.vals.map(fmt).join(" ");
  }).join(" ");
}

function getHandlesForSegment(pathObj, segIndex, seg) {
  const hs = [];
  const c = seg.cmd;
  const v = seg.vals;

  function add(pairIndex, role) {
    hs.push({
      pathObj,
      segIndex,
      pairIndex,
      role,
      get x(){ return pathObj.segs[segIndex].vals[pairIndex]; },
      get y(){ return pathObj.segs[segIndex].vals[pairIndex+1]; },
      setXY(x,y){
        pathObj.segs[segIndex].vals[pairIndex] = x;
        pathObj.segs[segIndex].vals[pairIndex+1] = y;
        pathObj.el.setAttribute("d", buildPath(pathObj.segs));
      }
    });
  }

  if (c === "M" || c === "L" || c === "T") add(0, "anchor");
  else if (c === "C") {
    add(0, "control");
    add(2, "control");
    add(4, "anchor");
  } else if (c === "S" || c === "Q") {
    add(0, "control");
    add(2, "anchor");
  } else if (c === "A") {
    add(5, "anchor");
  }

  return hs;
}

function clearStage() {
  while (stage.firstChild) stage.removeChild(stage.firstChild);
  state.paths = [];
  state.handles = [];
}

function parseViewBox(svg) {
  const vb = svg.getAttribute("viewBox");
  if (vb) {
    const nums = vb.trim().split(/[\s,]+/).map(Number);
    if (nums.length === 4 && nums.every(Number.isFinite)) return nums;
  }

  const w = parseFloat((svg.getAttribute("width") || "1000").replace(/[a-z%]+/ig, ""));
  const h = parseFloat((svg.getAttribute("height") || "1000").replace(/[a-z%]+/ig, ""));
  return [0,0,w || 1000,h || 1000];
}

function loadSvgIntoStage(svgText) {
  clearStage();

  const doc = new DOMParser().parseFromString(svgText, "image/svg+xml");
  const rawSvg = doc.documentElement;

  state.viewBox = parseViewBox(rawSvg);
  stage.setAttribute("viewBox", state.viewBox.join(" "));

  const contentGroup = document.createElementNS(NS, "g");
  contentGroup.setAttribute("id", "glyphContent");
  stage.appendChild(contentGroup);

  const imported = document.importNode(rawSvg, true);

  const paths = Array.from(imported.querySelectorAll("path"));

  for (const oldPath of paths) {
    const d = oldPath.getAttribute("d");
    if (!d) continue;

    const segs = parsePath(d);
    const newPath = document.createElementNS(NS, "path");

    newPath.setAttribute("d", buildPath(segs));
    newPath.setAttribute("fill", "#6b7280");
    newPath.setAttribute("fill-opacity", ui.glyphOpacity.value);
    newPath.setAttribute("stroke", "none");

    contentGroup.appendChild(newPath);

    const pathObj = {el:newPath, segs};
    state.paths.push(pathObj);
  }

  if (!state.paths.length) {
    const text = document.createElementNS(NS, "text");
    text.setAttribute("x", state.viewBox[0] + 20);
    text.setAttribute("y", state.viewBox[1] + 40);
    text.setAttribute("fill", "red");
    text.textContent = "没有解析到 path，当前 SVG 可能不是 path 轮廓。";
    stage.appendChild(text);
  }

  rebuildHandles();
  updateStatus();
}

function rebuildHandles() {
  const old = stage.querySelector("#handleLayer");
  if (old) old.remove();

  const layer = document.createElementNS(NS, "g");
  layer.setAttribute("id", "handleLayer");
  stage.appendChild(layer);

  state.handles = [];

  for (const pathObj of state.paths) {
    pathObj.segs.forEach((seg, segIndex) => {
      const hs = getHandlesForSegment(pathObj, segIndex, seg);
      state.handles.push(...hs);
    });
  }

  if (ui.showControls.checked) {
    for (const h of state.handles) {
      if (h.role !== "control") continue;
      const seg = h.pathObj.segs[h.segIndex];
      let anchor = null;

      if (seg.cmd === "C") {
        anchor = h.pairIndex === 0
          ? previousAnchor(h.pathObj.segs, h.segIndex)
          : {x: seg.vals[4], y: seg.vals[5]};
      } else if (seg.cmd === "Q" || seg.cmd === "S") {
        anchor = {x: seg.vals[2], y: seg.vals[3]};
      }

      if (!anchor) continue;

      const line = document.createElementNS(NS, "line");
      line.setAttribute("x1", anchor.x);
      line.setAttribute("y1", anchor.y);
      line.setAttribute("x2", h.x);
      line.setAttribute("y2", h.y);
      line.setAttribute("stroke", "#60a5fa");
      line.setAttribute("stroke-opacity", "0.45");
      line.setAttribute("stroke-width", 1.2);
      line.setAttribute("vector-effect", "non-scaling-stroke");
      layer.appendChild(line);
    }
  }

  if (ui.showPoints.checked) {
    const r = Number(ui.pointSize.value);

    state.handles.forEach((h, idx) => {
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", h.x);
      c.setAttribute("cy", h.y);
      c.setAttribute("r", r);
      c.setAttribute("fill", h.role === "anchor" ? "#ffffff" : "#93c5fd");
      c.setAttribute("stroke", h.role === "anchor" ? "#ef4444" : "#2563eb");
      c.setAttribute("stroke-width", 1.5);
      c.setAttribute("vector-effect", "non-scaling-stroke");
      c.style.cursor = "move";
      c.dataset.handleIndex = idx;
      layer.appendChild(c);
    });
  }
}

function previousAnchor(segs, idx) {
  for (let i = idx - 1; i >= 0; i--) {
    const s = segs[i];
    if (s.cmd === "M" || s.cmd === "L" || s.cmd === "T") return {x:s.vals[0], y:s.vals[1]};
    if (s.cmd === "C") return {x:s.vals[4], y:s.vals[5]};
    if (s.cmd === "S" || s.cmd === "Q") return {x:s.vals[2], y:s.vals[3]};
    if (s.cmd === "A") return {x:s.vals[5], y:s.vals[6]};
  }
  return null;
}

function svgPoint(evt) {
  const pt = stage.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(stage.getScreenCTM().inverse());
}

stage.addEventListener("pointerdown", e => {
  if (e.button === 1 || e.button === 2) {
    state.panning = {
      x: e.clientX,
      y: e.clientY,
      viewBox: [...state.viewBox]
    };
    stage.setPointerCapture(e.pointerId);
    return;
  }

  const target = e.target;
  if (!target.dataset || target.dataset.handleIndex === undefined) return;

  const idx = Number(target.dataset.handleIndex);
  const h = state.handles[idx];

  if (!h) return;

  const p = svgPoint(e);

  state.drag = {
    handle: h,
    start: p,
    startX: h.x,
    startY: h.y
  };

  stage.setPointerCapture(e.pointerId);
});

stage.addEventListener("pointermove", e => {
  if (state.panning) {
    const vb = state.panning.viewBox;
    const dx = (e.clientX - state.panning.x) * vb[2] / stage.clientWidth;
    const dy = (e.clientY - state.panning.y) * vb[3] / stage.clientHeight;

    state.viewBox = [vb[0] - dx, vb[1] - dy, vb[2], vb[3]];
    stage.setAttribute("viewBox", state.viewBox.join(" "));
    return;
  }

  if (!state.drag) return;

  const p = svgPoint(e);
  const dx = p.x - state.drag.start.x;
  const dy = p.y - state.drag.start.y;

  state.drag.handle.setXY(state.drag.startX + dx, state.drag.startY + dy);
  rebuildHandles();
  updateStatus();
});

stage.addEventListener("pointerup", () => {
  state.drag = null;
  state.panning = null;
});

stage.addEventListener("pointerleave", () => {
  state.drag = null;
  state.panning = null;
});

stage.addEventListener("contextmenu", e => e.preventDefault());

stage.addEventListener("wheel", e => {
  e.preventDefault();

  const vb = [...state.viewBox];
  const factor = e.deltaY < 0 ? 0.9 : 1.1;

  const p = svgPoint(e);

  const newW = vb[2] * factor;
  const newH = vb[3] * factor;

  const tx = (p.x - vb[0]) / vb[2];
  const ty = (p.y - vb[1]) / vb[3];

  state.viewBox = [
    p.x - tx * newW,
    p.y - ty * newH,
    newW,
    newH
  ];

  stage.setAttribute("viewBox", state.viewBox.join(" "));
}, {passive:false});

function serializeEditedSvg() {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("xmlns", NS);
  svg.setAttribute("viewBox", stage.getAttribute("viewBox") || state.viewBox.join(" "));

  for (const pathObj of state.paths) {
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", buildPath(pathObj.segs));
    p.setAttribute("fill", "#000000");
    svg.appendChild(p);
  }

  return new XMLSerializer().serializeToString(svg);
}

async function loadManifest() {
  try {
    const raw = await fetchJson(`/elastic_skeleton_manifest/${JOB_ID}`);
    const m = normalizeManifest(raw);
    if (m.length) return m;
  } catch(e) {
    console.warn(e);
  }
  return fallbackManifest();
}

function renderGlyphList() {
  ui.glyphList.innerHTML = "";

  state.manifest.forEach((g, i) => {
    const div = document.createElement("div");
    div.className = "card" + (i === state.activeIndex ? " active" : "");
    div.innerHTML = `<b>${g.code}</b><small>${g.variants.length} 个版本</small>`;
    div.onclick = () => selectGlyph(i);
    ui.glyphList.appendChild(div);
  });
}

async function selectGlyph(i) {
  state.activeIndex = i;
  const g = state.manifest[i];
  state.code = g.code;

  ui.variantSelect.innerHTML = "";
  g.variants.forEach(v => {
    const op = document.createElement("option");
    op.value = v;
    op.textContent = v;
    ui.variantSelect.appendChild(op);
  });

  state.variant = g.variants[0] || "step_01";
  ui.variantSelect.value = state.variant;

  renderGlyphList();
  await loadRawSvg();
}

async function loadRawSvg() {
  try {
    const saved = await fetch(`/load_svg_path_edit/${JOB_ID}/${state.code}/${state.variant}`);
    if (saved.ok) {
      state.rawSvgText = await saved.text();
      loadSvgIntoStage(state.rawSvgText);
      return;
    }
  } catch(e) {}

  state.rawSvgText = await fetchText(`/raw_svg_variant/${JOB_ID}/${state.code}/${state.variant}`);
  loadSvgIntoStage(state.rawSvgText);
}

async function reloadRaw() {
  state.rawSvgText = await fetchText(`/raw_svg_variant/${JOB_ID}/${state.code}/${state.variant}`);
  loadSvgIntoStage(state.rawSvgText);
}

async function saveSvg() {
  const svg = serializeEditedSvg();

  const r = await fetch(`/save_svg_path_edit/${JOB_ID}/${state.code}/${state.variant}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({svg})
  });

  if (!r.ok) {
    alert("保存失败：HTTP " + r.status);
    return;
  }

  alert("已保存 SVG 轮廓编辑");
}

function downloadSvg() {
  const svg = serializeEditedSvg();
  const blob = new Blob([svg], {type:"image/svg+xml;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${state.code}_${state.variant}_edited.svg`;
  a.click();
  URL.revokeObjectURL(url);
}

function updateGlyphOpacity() {
  stage.querySelectorAll("#glyphContent path").forEach(p => {
    p.setAttribute("fill-opacity", ui.glyphOpacity.value);
  });
}

function updateStatus() {
  ui.statusBox.innerHTML =
    `Job ID：${JOB_ID}<br>` +
    `当前字形：${state.code || "-"}<br>` +
    `当前版本：${state.variant || "-"}<br>` +
    `路径数量：${state.paths.length}<br>` +
    `可编辑控制点：${state.handles.length}<br>` +
    `状态：直接编辑 SVG path`;
}

async function init() {
  labels();

  [
    ui.showPoints,
    ui.showControls,
    ui.pointSize
  ].forEach(el => el.addEventListener("input", () => {
    labels();
    rebuildHandles();
  }));

  ui.glyphOpacity.addEventListener("input", () => {
    labels();
    updateGlyphOpacity();
  });

  ui.variantSelect.onchange = async () => {
    state.variant = ui.variantSelect.value;
    await loadRawSvg();
  };

  ui.saveBtn.onclick = saveSvg;
  ui.loadSavedBtn.onclick = loadRawSvg;
  ui.downloadSvgBtn.onclick = downloadSvg;
  ui.reloadRawBtn.onclick = reloadRaw;

  state.manifest = await loadManifest();
  renderGlyphList();
  await selectGlyph(0);
}


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

init();
</script>
</body>
</html>
"""
    return html.replace("__JOB_ID__", job_id)


@app.post("/save_svg_path_edit/{job_id}/{code}/{variant}")
async def save_svg_path_edit(job_id: str, code: str, variant: str, request: _SvgEditRequest):
    data = await request.json()
    svg = data.get("svg", "")

    if not svg.strip():
        return _SvgEditJSONResponse({"ok": False, "error": "empty svg"}, status_code=400)

    safe_code = _svg_edit_re.sub(r"[^0-9A-Za-z_\\-]", "_", code)
    safe_variant = _svg_edit_re.sub(r"[^0-9A-Za-z_\\-]", "_", variant)

    out_dir = _SvgEditPath("jobs") / job_id / "svg_path_edits"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{safe_code}_{safe_variant}.svg"
    out_file.write_text(svg, encoding="utf-8")

    return {"ok": True, "file": str(out_file)}


@app.get("/load_svg_path_edit/{job_id}/{code}/{variant}")
async def load_svg_path_edit(job_id: str, code: str, variant: str):
    safe_code = _svg_edit_re.sub(r"[^0-9A-Za-z_\\-]", "_", code)
    safe_variant = _svg_edit_re.sub(r"[^0-9A-Za-z_\\-]", "_", variant)

    f = _SvgEditPath("jobs") / job_id / "svg_path_edits" / f"{safe_code}_{safe_variant}.svg"

    if not f.exists():
        return _SvgEditJSONResponse({"ok": False, "error": "not found"}, status_code=404)

    return _SvgEditResponse(
        content=f.read_text(encoding="utf-8"),
        media_type="image/svg+xml"
    )


@app.middleware("http")
async def redirect_every_old_editor_to_svg_path_editor(request, call_next):
    # DISABLED_BY_RESTORE_TO_V5_CENTERLINE
    return await call_next(request)
    path = request.url.path

    prefixes = [
        "/skeleton_editor/",
        "/skeleton_elastic_editor/",
        "/skeleton_elastic_editor_v5/",
        "/glyph_warp_editor/"
    ]

    for prefix in prefixes:
        if path.startswith(prefix):
            suffix = path[len(prefix):]
            target = "/svg_path_editor/" + suffix

            if request.url.query:
                target += "?" + request.url.query

            return _SvgEditRedirectResponse(url=target, status_code=302)

    return await call_next(request)

# ===== END_SVG_PATH_REAL_GLYPH_EDITOR_V1 =====


# ===== CENTERLINE_KEYPOINT_EDITOR_V1 =====
from fastapi import Request as _CenterlineRequest
from fastapi.responses import HTMLResponse as _CenterlineHTMLResponse
from fastapi.responses import JSONResponse as _CenterlineJSONResponse
from starlette.responses import RedirectResponse as _CenterlineRedirectResponse
from pathlib import Path as _CenterlinePath
import json as _centerline_json
import re as _centerline_re

@app.get("/centerline_editor/{job_id}", response_class=_CenterlineHTMLResponse)
async def centerline_keypoint_editor(job_id: str):
    html = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>字形中心线关键点编辑器</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f3f4f6;font-family:Arial,"Microsoft YaHei",sans-serif;color:#111827}
header{height:72px;background:#111;color:#fff;padding:14px 22px}
header h1{margin:0;font-size:24px}
header p{margin:6px 0 0;font-size:13px;color:#d1d5db}
.app{height:calc(100vh - 72px);display:grid;grid-template-columns:220px 1fr 290px;gap:14px;padding:14px}
.panel,.main{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
.panel{padding:14px;overflow:auto}
.main{padding:14px;display:flex;flex-direction:column;overflow:hidden}
.title{font-weight:700;margin-bottom:10px}
.card{border:1px solid #e5e7eb;border-radius:8px;padding:10px;margin-bottom:9px;background:#fafafa;cursor:pointer}
.card.active{background:#2563eb;color:white;border-color:#2563eb}
.card small{display:block;margin-top:5px;opacity:.75}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
button{border:0;border-radius:7px;padding:8px 12px;background:#4b5563;color:white;cursor:pointer;font-size:13px}
button.primary{background:#2563eb}
button.green{background:#059669}
button.warn{background:#d97706}
select{height:32px;border:1px solid #d1d5db;border-radius:6px;padding:0 8px;min-width:150px}
.stage-wrap{flex:1;border:1px solid #e5e7eb;border-radius:10px;background:white;overflow:hidden;position:relative}
svg#stage{width:100%;height:100%;display:block;background:white}
.help{font-size:12px;color:#4b5563;line-height:1.7;margin-top:8px}
.row{display:flex;gap:8px;align-items:center;font-size:13px;margin:8px 0}
.group{margin:16px 0}
.group label{display:flex;justify-content:space-between;font-size:13px;font-weight:700;margin-bottom:6px}
input[type=range]{width:100%}
.status,.note{font-size:12px;line-height:1.7;border-radius:8px;padding:10px}
.status{background:#f9fafb;border:1px solid #e5e7eb}
.note{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;margin-top:12px}
.err{color:#b91c1c;font-size:12px;white-space:pre-wrap}
</style>
</head>
<body>
<header>
  <h1>字形中心线关键点编辑器</h1>
  <p>灰色显示原字，在字形上绘制一条可编辑中心线，用少量关键点控制字形结构。</p>
</header>

<div class="app">
  <aside class="panel">
    <div class="title">字形列表</div>
    <div id="glyphList">加载中...</div>
  </aside>

  <main class="main">
    <div class="toolbar">
      <button class="primary" id="saveBtn">保存中心线</button>
      <button class="green" id="loadBtn">读取已保存</button>
      <button id="exportBtn">导出 JSON</button>
      <button class="warn" id="resetBtn">重置中心线</button>
      <button id="clearBtn">清空</button>
      <select id="variantSelect"></select>
    </div>

    <div class="stage-wrap">
      <svg id="stage" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>

    <div class="help">
      操作：拖动白色点调整中心线；双击空白处添加点；点击点后按“删除选中点”删除；鼠标滚轮缩放；右键拖动画布。
    </div>
  </main>

  <aside class="panel">
    <div class="title">中心线控制</div>

    <div class="row"><input type="checkbox" id="showGlyph" checked><label for="showGlyph">显示灰色原字</label></div>
    <div class="row"><input type="checkbox" id="showLine" checked><label for="showLine">显示中心线</label></div>
    <div class="row"><input type="checkbox" id="showPoints" checked><label for="showPoints">显示关键点</label></div>

    <hr>

    <button class="green" id="autoBtn">生成粗略中心线</button>
    <button class="warn" id="deleteBtn">删除选中点</button>

    <div class="group">
      <label>线宽 <span id="lineWidthValue">5</span></label>
      <input id="lineWidth" type="range" min="1" max="16" step="1" value="5">
    </div>

    <div class="group">
      <label>关键点大小 <span id="pointSizeValue">7</span></label>
      <input id="pointSize" type="range" min="3" max="16" step="1" value="7">
    </div>

    <div class="group">
      <label>原字透明度 <span id="glyphOpacityValue">0.48</span></label>
      <input id="glyphOpacity" type="range" min="0.1" max="1" step="0.05" value="0.48">
    </div>

    <div class="status" id="statusBox"></div>

    <div class="note">
      这个版本只做你图里那种“字形中心线 + 少量关键点”。中心线数据会保存为 JSON，后续可以继续接到字形变形或字体导出流程。
    </div>
  </aside>
</div>

<script>
"use strict";

const JOB_ID = "__JOB_ID__";
const NS = "http://www.w3.org/2000/svg";
const stage = document.getElementById("stage");

const ui = {
  glyphList: document.getElementById("glyphList"),
  variantSelect: document.getElementById("variantSelect"),
  saveBtn: document.getElementById("saveBtn"),
  loadBtn: document.getElementById("loadBtn"),
  exportBtn: document.getElementById("exportBtn"),
  resetBtn: document.getElementById("resetBtn"),
  clearBtn: document.getElementById("clearBtn"),
  autoBtn: document.getElementById("autoBtn"),
  deleteBtn: document.getElementById("deleteBtn"),
  showGlyph: document.getElementById("showGlyph"),
  showLine: document.getElementById("showLine"),
  showPoints: document.getElementById("showPoints"),
  lineWidth: document.getElementById("lineWidth"),
  pointSize: document.getElementById("pointSize"),
  glyphOpacity: document.getElementById("glyphOpacity"),
  lineWidthValue: document.getElementById("lineWidthValue"),
  pointSizeValue: document.getElementById("pointSizeValue"),
  glyphOpacityValue: document.getElementById("glyphOpacityValue"),
  statusBox: document.getElementById("statusBox")
};

const state = {
  manifest: [],
  activeIndex: 0,
  code: null,
  variant: null,
  rawSvgText: "",
  viewBox: [0,0,1000,1000],
  points: [],
  selectedIndex: -1,
  drag: null,
  pan: null
};

function labels() {
  ui.lineWidthValue.textContent = ui.lineWidth.value;
  ui.pointSizeValue.textContent = ui.pointSize.value;
  ui.glyphOpacityValue.textContent = ui.glyphOpacity.value;
}

function normalizeManifest(raw) {
  let arr = raw;
  if (!Array.isArray(arr)) {
    arr = raw.glyphs || raw.items || raw.chars || raw.manifest || raw.data || raw.results || [];
  }
  if (!Array.isArray(arr) && typeof arr === "object") {
    arr = Object.entries(arr).map(([code, value]) => {
      if (typeof value === "object") return {code, ...value};
      return {code, variants: value};
    });
  }
  return arr.map(x => {
    const code = x.code || x.unicode || x.char || x.name || x.glyph || x.id;
    let variants = x.variants || x.steps || x.versions || x.variant_list || x.files || [];
    if (!Array.isArray(variants)) variants = Object.keys(variants || {});
    variants = variants.map(v => {
      if (typeof v === "string") return v;
      return v.variant || v.step || v.name || v.id || v.key || "step_01";
    });
    if (!variants.length) variants = ["step_01"];
    return {code, variants};
  }).filter(x => x.code);
}

function fallbackManifest() {
  const out = [];
  for (let cp = 0x1820; cp <= 0x1842; cp++) {
    const code = "U" + cp.toString(16).toUpperCase().padStart(4, "0");
    const variants = [];
    for (let i = 1; i <= 20; i++) variants.push("step_" + String(i).padStart(2, "0"));
    out.push({code, variants});
  }
  return out;
}

async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " HTTP " + r.status);
  return await r.json();
}

async function fetchText(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " HTTP " + r.status);
  return await r.text();
}

function parseViewBox(svg) {
  const vb = svg.getAttribute("viewBox");
  if (vb) {
    const nums = vb.trim().split(/[\s,]+/).map(Number);
    if (nums.length === 4 && nums.every(Number.isFinite)) return nums;
  }

  const w = parseFloat((svg.getAttribute("width") || "1000").replace(/[a-z%]+/ig, ""));
  const h = parseFloat((svg.getAttribute("height") || "1000").replace(/[a-z%]+/ig, ""));
  return [0,0,w || 1000,h || 1000];
}

function svgPoint(evt) {
  const pt = stage.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(stage.getScreenCTM().inverse());
}

function clearStage() {
  while (stage.firstChild) stage.removeChild(stage.firstChild);
}

function renderSvgGlyph(svgText) {
  clearStage();

  const doc = new DOMParser().parseFromString(svgText, "image/svg+xml");
  const rawSvg = doc.documentElement;

  state.viewBox = parseViewBox(rawSvg);
  stage.setAttribute("viewBox", state.viewBox.join(" "));

  const glyphLayer = document.createElementNS(NS, "g");
  glyphLayer.setAttribute("id", "glyphLayer");
  stage.appendChild(glyphLayer);

  for (const child of Array.from(rawSvg.childNodes)) {
    const imported = document.importNode(child, true);
    glyphLayer.appendChild(imported);
  }

  normalizeGlyphStyle();

  const lineLayer = document.createElementNS(NS, "g");
  lineLayer.setAttribute("id", "lineLayer");
  stage.appendChild(lineLayer);

  redraw();
}

function normalizeGlyphStyle() {
  const glyphLayer = stage.querySelector("#glyphLayer");
  if (!glyphLayer) return;

  glyphLayer.style.display = ui.showGlyph.checked ? "" : "none";

  const shapes = glyphLayer.querySelectorAll("path, polygon, polyline, rect, circle, ellipse");
  shapes.forEach(el => {
    el.setAttribute("fill", "#6b7280");
    el.setAttribute("fill-opacity", ui.glyphOpacity.value);
    el.setAttribute("stroke", "none");
  });
}

function autoCenterline() {
  const [x, y, w, h] = state.viewBox;

  state.points = [
    {x: x + w * 0.12, y: y + h * 0.42},
    {x: x + w * 0.24, y: y + h * 0.32},
    {x: x + w * 0.37, y: y + h * 0.40},
    {x: x + w * 0.42, y: y + h * 0.64},
    {x: x + w * 0.53, y: y + h * 0.58},
    {x: x + w * 0.66, y: y + h * 0.82},
    {x: x + w * 0.78, y: y + h * 0.50},
    {x: x + w * 0.88, y: y + h * 0.18}
  ];

  state.selectedIndex = -1;
  redraw();
}

function makePathD(points) {
  if (!points.length) return "";
  let d = `M ${points[0].x} ${points[0].y}`;

  if (points.length === 2) {
    d += ` L ${points[1].x} ${points[1].y}`;
    return d;
  }

  for (let i = 1; i < points.length - 1; i++) {
    const p = points[i];
    const next = points[i + 1];
    const mx = (p.x + next.x) / 2;
    const my = (p.y + next.y) / 2;
    d += ` Q ${p.x} ${p.y} ${mx} ${my}`;
  }

  const last = points[points.length - 1];
  d += ` T ${last.x} ${last.y}`;

  return d;
}

function redraw() {
  normalizeGlyphStyle();

  let lineLayer = stage.querySelector("#lineLayer");
  if (!lineLayer) {
    lineLayer = document.createElementNS(NS, "g");
    lineLayer.setAttribute("id", "lineLayer");
    stage.appendChild(lineLayer);
  }

  while (lineLayer.firstChild) lineLayer.removeChild(lineLayer.firstChild);

  if (ui.showLine.checked && state.points.length >= 2) {
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", makePathD(state.points));
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "#ef4444");
    path.setAttribute("stroke-width", ui.lineWidth.value);
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    path.setAttribute("vector-effect", "non-scaling-stroke");
    lineLayer.appendChild(path);
  }

  if (ui.showPoints.checked) {
    const r = Number(ui.pointSize.value);

    state.points.forEach((p, i) => {
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", p.x);
      c.setAttribute("cy", p.y);
      c.setAttribute("r", r);
      c.setAttribute("fill", i === state.selectedIndex ? "#facc15" : "#ffffff");
      c.setAttribute("stroke", "#ef4444");
      c.setAttribute("stroke-width", "2");
      c.setAttribute("vector-effect", "non-scaling-stroke");
      c.style.cursor = "move";
      c.dataset.index = i;
      lineLayer.appendChild(c);
    });
  }

  updateStatus();
}

function nearestSegmentIndex(p) {
  if (state.points.length < 2) return state.points.length;

  let best = 0;
  let bestD = Infinity;

  for (let i = 0; i < state.points.length - 1; i++) {
    const a = state.points[i];
    const b = state.points[i + 1];

    const vx = b.x - a.x;
    const vy = b.y - a.y;
    const len2 = vx * vx + vy * vy;

    let t = 0;
    if (len2 > 1e-9) {
      t = ((p.x - a.x) * vx + (p.y - a.y) * vy) / len2;
      t = Math.max(0, Math.min(1, t));
    }

    const qx = a.x + vx * t;
    const qy = a.y + vy * t;
    const d = Math.hypot(p.x - qx, p.y - qy);

    if (d < bestD) {
      bestD = d;
      best = i + 1;
    }
  }

  return best;
}

stage.addEventListener("pointerdown", e => {
  if (e.button === 2 || e.button === 1) {
    state.pan = {
      x: e.clientX,
      y: e.clientY,
      viewBox: [...state.viewBox]
    };
    stage.setPointerCapture(e.pointerId);
    return;
  }

  const target = e.target;
  if (target.dataset && target.dataset.index !== undefined) {
    const idx = Number(target.dataset.index);
    state.selectedIndex = idx;

    const p = svgPoint(e);
    state.drag = {
      index: idx,
      start: p,
      original: {...state.points[idx]}
    };

    stage.setPointerCapture(e.pointerId);
    redraw();
  }
});

stage.addEventListener("pointermove", e => {
  if (state.pan) {
    const vb = state.pan.viewBox;
    const dx = (e.clientX - state.pan.x) * vb[2] / stage.clientWidth;
    const dy = (e.clientY - state.pan.y) * vb[3] / stage.clientHeight;

    state.viewBox = [vb[0] - dx, vb[1] - dy, vb[2], vb[3]];
    stage.setAttribute("viewBox", state.viewBox.join(" "));
    return;
  }

  if (!state.drag) return;

  const p = svgPoint(e);
  const dx = p.x - state.drag.start.x;
  const dy = p.y - state.drag.start.y;

  state.points[state.drag.index] = {
    x: state.drag.original.x + dx,
    y: state.drag.original.y + dy
  };

  redraw();
});

stage.addEventListener("pointerup", () => {
  state.drag = null;
  state.pan = null;
});

stage.addEventListener("pointerleave", () => {
  state.drag = null;
  state.pan = null;
});

stage.addEventListener("dblclick", e => {
  const p = svgPoint(e);
  const idx = nearestSegmentIndex(p);
  state.points.splice(idx, 0, {x: p.x, y: p.y});
  state.selectedIndex = idx;
  redraw();
});

stage.addEventListener("wheel", e => {
  e.preventDefault();

  const factor = e.deltaY < 0 ? 0.9 : 1.1;
  const vb = [...state.viewBox];
  const p = svgPoint(e);

  const newW = vb[2] * factor;
  const newH = vb[3] * factor;

  const tx = (p.x - vb[0]) / vb[2];
  const ty = (p.y - vb[1]) / vb[3];

  state.viewBox = [
    p.x - tx * newW,
    p.y - ty * newH,
    newW,
    newH
  ];

  stage.setAttribute("viewBox", state.viewBox.join(" "));
}, {passive:false});

stage.addEventListener("contextmenu", e => e.preventDefault());

function deleteSelectedPoint() {
  if (state.selectedIndex < 0 || state.selectedIndex >= state.points.length) return;
  state.points.splice(state.selectedIndex, 1);
  state.selectedIndex = -1;
  redraw();
}

async function saveCenterline() {
  const body = {
    job_id: JOB_ID,
    code: state.code,
    variant: state.variant,
    viewBox: state.viewBox,
    points: state.points
  };

  const r = await fetch(`/save_centerline_edit/${JOB_ID}/${state.code}/${state.variant}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });

  if (!r.ok) {
    alert("保存失败：HTTP " + r.status);
    return;
  }

  alert("中心线已保存");
}

async function loadCenterline(silent=false) {
  const r = await fetch(`/load_centerline_edit/${JOB_ID}/${state.code}/${state.variant}`);

  if (!r.ok) {
    if (!silent) alert("没有找到已保存中心线");
    return false;
  }

  const data = await r.json();

  if (Array.isArray(data.points)) {
    state.points = data.points.map(p => ({x: Number(p.x), y: Number(p.y)}));
    state.selectedIndex = -1;
    redraw();
    return true;
  }

  return false;
}

function exportJson() {
  const body = {
    job_id: JOB_ID,
    code: state.code,
    variant: state.variant,
    viewBox: state.viewBox,
    points: state.points
  };

  const blob = new Blob([JSON.stringify(body, null, 2)], {type:"application/json"});
  const url = URL.createObjectURL(blob);

  const a = document.createElement("a");
  a.href = url;
  a.download = `${state.code}_${state.variant}_centerline.json`;
  a.click();

  URL.revokeObjectURL(url);
}

async function loadManifest() {
  try {
    const raw = await fetchJson(`/elastic_skeleton_manifest/${JOB_ID}`);
    const m = normalizeManifest(raw);
    if (m.length) return m;
  } catch(e) {
    console.warn(e);
  }

  return fallbackManifest();
}

function renderGlyphList() {
  ui.glyphList.innerHTML = "";

  state.manifest.forEach((g, i) => {
    const div = document.createElement("div");
    div.className = "card" + (i === state.activeIndex ? " active" : "");
    div.innerHTML = `<b>${g.code}</b><small>${g.variants.length} 个版本</small>`;
    div.onclick = () => selectGlyph(i);
    ui.glyphList.appendChild(div);
  });
}

async function selectGlyph(i) {
  state.activeIndex = i;

  const g = state.manifest[i];
  state.code = g.code;

  ui.variantSelect.innerHTML = "";
  g.variants.forEach(v => {
    const op = document.createElement("option");
    op.value = v;
    op.textContent = v;
    ui.variantSelect.appendChild(op);
  });

  state.variant = g.variants[0] || "step_01";
  ui.variantSelect.value = state.variant;

  renderGlyphList();
  await loadGlyph();
}

async function loadGlyph() {
  try {
    state.rawSvgText = await fetchText(`/raw_svg_variant/${JOB_ID}/${state.code}/${state.variant}`);
    renderSvgGlyph(state.rawSvgText);

    const loaded = await loadCenterline(true);
    if (!loaded) {
      autoCenterline();
    }
  } catch(e) {
    ui.statusBox.innerHTML = `<div class="err">${e.stack || e}</div>`;
  }
}

function updateStatus() {
  ui.statusBox.innerHTML =
    `Job ID：${JOB_ID}<br>` +
    `当前字形：${state.code || "-"}<br>` +
    `当前版本：${state.variant || "-"}<br>` +
    `中心线关键点：${state.points.length}<br>` +
    `选中点：${state.selectedIndex >= 0 ? state.selectedIndex + 1 : "-"}<br>` +
    `状态：中心线关键点编辑`;
}

async function init() {
  labels();

  [
    ui.showGlyph,
    ui.showLine,
    ui.showPoints,
    ui.lineWidth,
    ui.pointSize,
    ui.glyphOpacity
  ].forEach(el => el.addEventListener("input", () => {
    labels();
    redraw();
  }));

  ui.saveBtn.onclick = saveCenterline;
  ui.loadBtn.onclick = () => loadCenterline(false);
  ui.exportBtn.onclick = exportJson;
  ui.resetBtn.onclick = autoCenterline;
  ui.clearBtn.onclick = () => {
    state.points = [];
    state.selectedIndex = -1;
    redraw();
  };
  ui.autoBtn.onclick = autoCenterline;
  ui.deleteBtn.onclick = deleteSelectedPoint;

  ui.variantSelect.onchange = async () => {
    state.variant = ui.variantSelect.value;
    await loadGlyph();
  };

  state.manifest = await loadManifest();
  renderGlyphList();
  await selectGlyph(0);
}

init();
</script>
</body>
</html>
"""
    return html.replace("__JOB_ID__", job_id)


@app.post("/save_centerline_edit/{job_id}/{code}/{variant}")
async def save_centerline_edit(job_id: str, code: str, variant: str, request: _CenterlineRequest):
    data = await request.json()

    safe_code = _centerline_re.sub(r"[^0-9A-Za-z_\\-]", "_", code)
    safe_variant = _centerline_re.sub(r"[^0-9A-Za-z_\\-]", "_", variant)

    out_dir = _CenterlinePath("jobs") / job_id / "centerline_edits"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{safe_code}_{safe_variant}.json"
    out_file.write_text(_centerline_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"ok": True, "file": str(out_file)}


@app.get("/load_centerline_edit/{job_id}/{code}/{variant}")
async def load_centerline_edit(job_id: str, code: str, variant: str):
    safe_code = _centerline_re.sub(r"[^0-9A-Za-z_\\-]", "_", code)
    safe_variant = _centerline_re.sub(r"[^0-9A-Za-z_\\-]", "_", variant)

    f = _CenterlinePath("jobs") / job_id / "centerline_edits" / f"{safe_code}_{safe_variant}.json"

    if not f.exists():
        return _CenterlineJSONResponse({"ok": False, "error": "not found"}, status_code=404)

    return _centerline_json.loads(f.read_text(encoding="utf-8"))


@app.middleware("http")
async def redirect_old_editors_to_centerline_editor(request, call_next):
    # DISABLED_BY_RESTORE_TO_V5_CENTERLINE
    return await call_next(request)
    path = request.url.path

    prefixes = [
        "/skeleton_editor/",
        "/skeleton_elastic_editor/",
        "/skeleton_elastic_editor_v5/",
        "/glyph_warp_editor/",
        "/svg_path_editor/"
    ]

    for prefix in prefixes:
        if path.startswith(prefix):
            suffix = path[len(prefix):]
            target = "/centerline_editor/" + suffix

            if request.url.query:
                target += "?" + request.url.query

            return _CenterlineRedirectResponse(url=target, status_code=302)

    return await call_next(request)

# ===== END_CENTERLINE_KEYPOINT_EDITOR_V1 =====



# ===== FORCE_RESTORE_TO_V5_CENTERLINE_EDITOR =====
from starlette.responses import RedirectResponse as _RestoreV5RedirectResponse

@app.middleware("http")
async def force_restore_to_v5_centerline_editor(request, call_next):
    """
    恢复到之前“有中心线”的 V5 编辑器版本。
    原来的“打开骨架编辑器”入口会进入：
        /skeleton_elastic_editor_v5/{job_id}
    """
    path = request.url.path

    redirect_prefixes = [
        "/skeleton_editor/",
        "/skeleton_elastic_editor/",
        "/glyph_warp_editor/",
        "/svg_path_editor/",
        "/centerline_editor/",
    ]

    # 注意：/skeleton_elastic_editor_v5/ 本身不跳转，直接放行
    if path.startswith("/skeleton_elastic_editor_v5/"):
        return await call_next(request)

    for prefix in redirect_prefixes:
        if path.startswith(prefix):
            suffix = path[len(prefix):]
            target = "/skeleton_elastic_editor_v5/" + suffix

            if request.url.query:
                target += "?" + request.url.query

            return _RestoreV5RedirectResponse(url=target, status_code=302)

    return await call_next(request)

# ===== END_FORCE_RESTORE_TO_V5_CENTERLINE_EDITOR =====

# =========================================================
from fastapi.responses import HTMLResponse as _MGBHTMLResponse

@app.get("/mongolian_gb_version", response_class=_MGBHTMLResponse)
async def mongolian_gb_version_page():
    html = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>中国国标版</title>
<style>
body{font-family:Arial,"Microsoft YaHei",sans-serif;background:#fff;color:#111;margin:0;padding:24px;}
h1{font-size:32px;margin:0 0 12px 0;}
.card{border:1px solid #ddd;border-radius:14px;padding:18px;margin:16px 0;background:#fff;}
button{background:#111;color:#fff;border:1px solid #111;border-radius:8px;padding:10px 16px;font-weight:700;cursor:pointer;}
pre{background:#f7f7f7;border:1px solid #ddd;border-radius:8px;padding:12px;white-space:pre-wrap;}
a{color:#111;font-weight:700;}
</style>
</head>
<body>
<h1>中国国标版</h1>
<div class="card">
  <p>该页面保留原有功能，不覆盖旧模式。中国国标版使用：</p>
  <pre>data/mongolian_gb_runtime_strict.csv
output/mongolian_gb_ttf_steps/</pre>
  <p>当前已支持：名义字符、真实单个变形显现字符；表6强制性合体字需要在 <b>data/mongolian_gb_ligature_table6_strict.csv</b> 中逐项录入并 verified=1。</p>
</div>

<div class="card">
  <button onclick="build()">重新生成中国国标版 TTF</button>
  <pre id="out">等待操作...</pre>
</div>

<div class="card">
  <p>生成目录：</p>
  <pre>output/mongolian_gb_ttf_steps/</pre>
  <p>如需下载，请在服务器文件管理器中下载该目录或打包。</p>
</div>

<script>
async function build(){
  const out = document.getElementById("out");
  out.textContent = "正在生成，请稍等...";
  const res = await fetch("/api/mongolian_gb_version/build_ttf", {method:"POST"});
  const data = await res.json();
  out.textContent = JSON.stringify(data, null, 2);
}
</script>
</body>
</html>
"""
    return _MGBHTMLResponse(html)


@app.post("/api/mongolian_gb_version/build_ttf")
async def mongolian_gb_version_build_ttf():
    import subprocess, sys
    try:
        p1 = subprocess.run(
            [sys.executable, "scripts/build_mongolian_gb_runtime_strict.py"],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=120
        )
        p2 = subprocess.run(
            [sys.executable, "scripts/build_mongolian_gb_ttf_steps.py"],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=300
        )

        return JSONResponse({
            "ok": p1.returncode == 0 and p2.returncode == 0,
            "runtime_stdout": p1.stdout[-4000:],
            "runtime_stderr": p1.stderr[-4000:],
            "ttf_stdout": p2.stdout[-4000:],
            "ttf_stderr": p2.stderr[-4000:],
            "output_dir": "output/mongolian_gb_ttf_steps"
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# =========================================================
from starlette.responses import Response as _GBSyncResponse
from fastapi.responses import JSONResponse as _GBSyncJSONResponse
from fastapi.responses import FileResponse as _GBSyncFileResponse
from fastapi import Request as _GBSyncRequest
import subprocess as _gb_subprocess
import sys as _gb_sys
import os as _gb_os
import shutil as _gb_shutil
import zipfile as _gb_zipfile
from pathlib import Path as _GBPath


_GB_SYNC_JS = r"""
<script id="main-page-china-gb-sync-v2">
(function(){
  function textOf(el){ return (el && el.textContent || "").trim(); }

  function isCharsetSelect(sel){
    const all = Array.from(sel.options).map(o => o.textContent).join(" ");
    return all.includes("中文") || all.includes("英文") || all.includes("日文") || all.includes("韩文") || all.includes("蒙古");
  }

  function patchCharsetSelect(){
    document.querySelectorAll("select").forEach(sel => {
      if(!isCharsetSelect(sel)) return;

      let hasGB = false;

      Array.from(sel.options).forEach(opt => {
        const t = opt.textContent || "";

        if(
          t.includes("传统蒙古文基础字母") ||
          t.includes("U+1820-U+1842") ||
          t.includes("中国国标增强") ||
          t.includes("国标增强")
        ){
          opt.textContent = "中国国标版";
          opt.dataset.chinaGb = "1";
          hasGB = true;
        }

        if(t.includes("中国国标版")){
          opt.dataset.chinaGb = "1";
          hasGB = true;
        }
      });

      if(!hasGB){
        const opt = document.createElement("option");
        opt.value = "china_gb";
        opt.textContent = "中国国标版";
        opt.dataset.chinaGb = "1";
        sel.appendChild(opt);
      }
    });
  }

  function selectedChinaGB(form){
    let ok = false;
    form.querySelectorAll("select").forEach(sel => {
      const opt = sel.options[sel.selectedIndex];
      if(!opt) return;
      const t = opt.textContent || "";
      if(t.includes("中国国标版") || opt.value === "china_gb" || opt.dataset.chinaGb === "1"){
        ok = true;
      }
    });
    return ok;
  }

  function findSteps(form){
    let best = 20;

    const inputs = Array.from(form.querySelectorAll("input"));
    for(const input of inputs){
      const v = parseInt(input.value || "", 10);
      if(Number.isFinite(v) && v >= 2 && v <= 80){
        best = v;
        break;
      }
    }

    return best;
  }

  function ensureResultBox(form){
    let box = document.getElementById("chinaGbResultBox");
    if(box) return box;

    box = document.createElement("div");
    box.id = "chinaGbResultBox";
    box.style.marginTop = "18px";
    box.style.padding = "14px";
    box.style.border = "1px solid #ddd";
    box.style.borderRadius = "10px";
    box.style.background = "#fff";
    box.style.color = "#111";
    box.innerHTML = `
      <b>中国国标版生成状态</b>
      <pre id="chinaGbResultText" style="white-space:pre-wrap;background:#f7f7f7;border:1px solid #ddd;border-radius:8px;padding:10px;margin-top:10px;">等待生成...</pre>
      <div id="chinaGbDownloadArea"></div>
    `;
    form.appendChild(box);
    return box;
  }

  async function runChinaGBBuild(form){
    const box = ensureResultBox(form);
    const out = document.getElementById("chinaGbResultText");
    const dl = document.getElementById("chinaGbDownloadArea");

    out.textContent = "正在生成中国国标版，请不要关闭页面...";
    dl.innerHTML = "";

    const fd = new FormData();

    const fileInputs = Array.from(form.querySelectorAll('input[type="file"]'));
    let fileCount = 0;

    for(const input of fileInputs){
      if(input.files && input.files.length > 0){
        for(const f of input.files){
          fd.append("font_files", f);
          fileCount += 1;
        }
      }
    }

    fd.append("steps", String(findSteps(form)));

    const res = await fetch("/api/china_gb_main/build", {
      method: "POST",
      body: fd
    });

    const data = await res.json();

    out.textContent = JSON.stringify(data, null, 2);

    if(data.ok){
      dl.innerHTML = `
        <p style="margin-top:10px;">
          <a href="/api/china_gb_main/download_zip" target="_blank" style="font-weight:700;color:#111;">
            下载中国国标版 TTF 压缩包
          </a>
        </p>
        <p style="font-size:12px;color:#555;">
          输出目录：output/mongolian_gb_ttf_steps/
        </p>
      `;
    }
  }

  function bindSubmit(){
    document.querySelectorAll("form").forEach(form => {
      if(form.dataset.chinaGbBound === "1") return;
      form.dataset.chinaGbBound = "1";

      form.addEventListener("submit", function(e){
        if(!selectedChinaGB(form)) return;

        e.preventDefault();
        e.stopPropagation();

        runChinaGBBuild(form).catch(err => {
          const box = ensureResultBox(form);
          const out = document.getElementById("chinaGbResultText");
          out.textContent = "中国国标版生成失败：\n" + err;
        });
      }, true);
    });
  }

  function run(){
    patchCharsetSelect();
    bindSubmit();
  }

  document.addEventListener("DOMContentLoaded", run);
  setTimeout(run, 500);
  setTimeout(run, 1500);
})();
</script>
"""


@app.middleware("http")
async def _main_page_china_gb_sync_middleware(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    try:
        html = body.decode("utf-8")
    except Exception:
        return _GBSyncResponse(
            content=body,
            status_code=response.status_code,
            media_type=ctype
        )

    html = html.replace("传统蒙古文基础字母 U+1820-U+1842", "中国国标版")
    html = html.replace("传统蒙古文（中国国标增强）", "中国国标版")
    html = html.replace("传统蒙古文：中国国标增强", "中国国标版")
    html = html.replace("中国国标增强", "中国国标版")
    html = html.replace("国标增强版", "中国国标版")

    if "main-page-china-gb-sync-v2" not in html:
        if "</body>" in html:
            html = html.replace("</body>", _GB_SYNC_JS + "\n</body>")
        else:
            html += _GB_SYNC_JS

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return _GBSyncResponse(
        content=html,
        status_code=response.status_code,
        headers=headers,
        media_type="text/html"
    )


@app.post("/api/china_gb_main/build")
async def _api_china_gb_main_build(request: _GBSyncRequest):
    """
    首页“中国国标版”构建接口。
    - 如果页面上传了两个字体：保存到 input/fonts 后生成。
    - 如果没上传：使用 input/fonts 里已有字体。
    - 生成 output/mongolian_gb_ttf_steps/*.ttf
    """
    try:
        form = await request.form()

        steps = 20
        for key, value in form.multi_items():
            if key == "steps":
                try:
                    v = int(str(value))
                    if 2 <= v <= 80:
                        steps = v
                except Exception:
                    pass

        font_dir = _GBPath("input/fonts")
        font_dir.mkdir(parents=True, exist_ok=True)

        uploaded = []
        for key, value in form.multi_items():
            if hasattr(value, "filename") and value.filename:
                name = _GBPath(value.filename).name
                if not name.lower().endswith((".ttf", ".otf")):
                    continue
                data = await value.read()
                if not data:
                    continue
                uploaded.append((name, data))

        if len(uploaded) >= 2:
            # 只在用户确实上传了两个字体时替换 input/fonts
            for old in font_dir.glob("*"):
                if old.is_file() and old.suffix.lower() in [".ttf", ".otf"]:
                    old.unlink()

            for name, data in uploaded[:2]:
                (font_dir / name).write_bytes(data)

        fonts = []
        for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
            fonts.extend(font_dir.glob(ext))
        fonts = sorted(fonts)

        if len(fonts) < 2:
            return _GBSyncJSONResponse({
                "ok": False,
                "error": "中国国标版至少需要两个字体文件。请在首页上传字体 A 和字体 B。",
                "font_dir": str(font_dir)
            }, status_code=400)

        required_scripts = [
            "scripts/check_mongolian_gb_font_coverage.py",
            "scripts/build_mongolian_gb_effective_mapping.py",
            "scripts/build_mongolian_gb_ligature_table6_template.py",
            "scripts/build_mongolian_gb_runtime_strict.py",
            "scripts/build_mongolian_gb_ttf_steps.py",
        ]

        missing = [x for x in required_scripts if not _GBPath(x).exists()]
        if missing:
            return _GBSyncJSONResponse({
                "ok": False,
                "error": "缺少中国国标版脚本，请先补齐前面生成过的脚本。",
                "missing": missing
            }, status_code=500)

        env = dict(_gb_os.environ)
        env["MGB_STEPS"] = str(steps)

        commands = [
            [_gb_sys.executable, "scripts/check_mongolian_gb_font_coverage.py"],
            [_gb_sys.executable, "scripts/build_mongolian_gb_effective_mapping.py"],
            [_gb_sys.executable, "scripts/build_mongolian_gb_ligature_table6_template.py"],
            [_gb_sys.executable, "scripts/build_mongolian_gb_runtime_strict.py"],
            [_gb_sys.executable, "scripts/build_mongolian_gb_ttf_steps.py"],
        ]

        logs = []

        for cmd in commands:
            r = _gb_subprocess.run(
                cmd,
                cwd=str(_GBPath.cwd()),
                capture_output=True,
                text=True,
                timeout=600,
                env=env
            )
            logs.append({
                "cmd": " ".join(cmd),
                "returncode": r.returncode,
                "stdout": r.stdout[-4000:],
                "stderr": r.stderr[-4000:],
            })
            if r.returncode != 0:
                return _GBSyncJSONResponse({
                    "ok": False,
                    "failed_cmd": " ".join(cmd),
                    "logs": logs
                }, status_code=500)

        # 打包 TTF
        out_dir = _GBPath("output/mongolian_gb_ttf_steps")
        zip_path = _GBPath("output/mongolian_gb_ttf_steps.zip")
        zip_path.parent.mkdir(parents=True, exist_ok=True)

        if zip_path.exists():
            zip_path.unlink()

        with _gb_zipfile.ZipFile(zip_path, "w", compression=_gb_zipfile.ZIP_DEFLATED) as z:
            for f in out_dir.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(out_dir.parent))
            for extra in [
                _GBPath("data/mongolian_gb_runtime_strict.csv"),
                _GBPath("data/mongolian_gb_ligature_table6_strict.csv"),
            ]:
                if extra.exists():
                    z.write(extra, extra)

        ttf_files = sorted(str(x) for x in out_dir.glob("*.ttf"))

        return _GBSyncJSONResponse({
            "ok": True,
            "mode": "中国国标版",
            "steps": steps,
            "fonts": [x.name for x in fonts[:2]],
            "ttf_count": len(ttf_files),
            "output_dir": str(out_dir),
            "zip": str(zip_path),
            "download_url": "/api/china_gb_main/download_zip",
            "preview_hub": "/preview_links",
            "logs": logs[-2:],
        })

    except Exception as e:
        return _GBSyncJSONResponse({
            "ok": False,
            "error": str(e)
        }, status_code=500)


@app.get("/api/china_gb_main/download_zip")
async def _api_china_gb_main_download_zip():
    zip_path = _GBPath("output/mongolian_gb_ttf_steps.zip")

    if not zip_path.exists():
        return _GBSyncJSONResponse({
            "ok": False,
            "error": "还没有生成压缩包，请先在首页选择中国国标版并点击开始生成。"
        }, status_code=404)

    return _GBSyncFileResponse(
        path=str(zip_path),
        filename="mongolian_gb_ttf_steps.zip",
        media_type="application/zip"
    )

# =========================================================
from fastapi.responses import HTMLResponse as _PreviewHubHTMLResponse
from fastapi.responses import JSONResponse as _PreviewHubJSONResponse
from pathlib import Path as _PreviewHubPath
import re as _preview_re


def _preview_latest_job_id():
    try:
        jobs_dir = globals().get("JOBS_DIR", _PreviewHubPath("jobs"))
        jobs_dir = _PreviewHubPath(jobs_dir)
        if not jobs_dir.exists():
            return ""
        jobs = [x for x in jobs_dir.iterdir() if x.is_dir()]
        if not jobs:
            return ""
        latest = max(jobs, key=lambda x: x.stat().st_mtime)
        return latest.name
    except Exception:
        return ""


def _preview_fill_path(path: str, latest_job: str):
    if "{" not in path:
        return path
    if not latest_job:
        return path
    return _preview_re.sub(r"\{[^}]+\}", latest_job, path)


def _preview_collect_links():
    latest_job = _preview_latest_job_id()

    links = []

    def add(title, url, group, note=""):
        if not url:
            return
        key = (title, url)
        if key in {(x["title"], x["url"]) for x in links}:
            return
        links.append({
            "title": title,
            "url": url,
            "group": group,
            "note": note,
        })

    add("首页 / 字体中间变化生成工具", "/", "基础功能", "原有首页，不删除。")
    add("中国国标版", "/mongolian_gb_version", "中国国标版", "中国国标版 TTF 生成入口。")
    add("中国国标版 TTF 压缩包下载", "/api/china_gb_main/download_zip", "中国国标版", "生成后下载。")
    add("完整字形实时变形编辑器", "/glyph_live_editor_v3", "编辑器", "如果已启用该页面，可直接打开。")
    add("MLS 骨架整体变形编辑器", "/skeleton_mls_editor", "编辑器", "保留已有骨架相关功能。")

    # 自动扫描 FastAPI 里的 GET 页面路由
    try:
        for r in app.routes:
            path = getattr(r, "path", "")
            methods = getattr(r, "methods", set()) or set()

            if "GET" not in methods:
                continue

            if not path or path.startswith("/api/"):
                continue

            low = path.lower()

            keywords = [
                "preview",
                "family",
                "variable",
                "skeleton",
                "glyph",
                "font",
                "morph",
                "mongolian",
            ]

            if not any(k in low for k in keywords):
                continue

            url = _preview_fill_path(path, latest_job)

            if "family" in low:
                group = "字体家族功能"
                title = "字体家族预览 / 功能入口"
            elif "variable" in low:
                group = "可变字体功能"
                title = "可变字体预览 / 功能入口"
            elif "preview" in low:
                group = "预览功能"
                title = "生成结果预览"
            elif "skeleton" in low or "glyph" in low:
                group = "编辑器"
                title = "字形编辑器"
            else:
                group = "其他功能"
                title = "功能入口"

            add(title, url, group, f"自动扫描到的路由：{path}")

    except Exception as e:
        add("路由扫描失败", "#", "系统", str(e))

    return links


@app.get("/preview_links", response_class=_PreviewHubHTMLResponse)
async def preview_links_hub():
    latest_job = _preview_latest_job_id()
    links = _preview_collect_links()

    rows = []
    for i, item in enumerate(links, 1):
        rows.append(f"""
<tr>
  <td>{i}</td>
  <td>{item['group']}</td>
  <td><a href="{item['url']}" target="_blank">{item['title']}</a></td>
  <td><code>{item['url']}</code></td>
  <td>{item['note']}</td>
</tr>
""")

    html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>预览链接中心</title>
<style>
body {{
  font-family: Arial, "Microsoft YaHei", sans-serif;
  background: #f5f6f8;
  color: #111;
  margin: 0;
  padding: 24px;
}}
h1 {{
  margin: 0 0 10px 0;
  font-size: 30px;
}}
.card {{
  background: #fff;
  border: 1px solid #ddd;
  border-radius: 14px;
  padding: 18px;
  margin-bottom: 18px;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  background: #fff;
}}
th, td {{
  border: 1px solid #ddd;
  padding: 10px;
  font-size: 14px;
  vertical-align: top;
}}
th {{
  background: #f0f0f0;
}}
a {{
  color: #111;
  font-weight: 700;
}}
code {{
  background: #f7f7f7;
  padding: 2px 5px;
  border-radius: 4px;
}}
.notice {{
  line-height: 1.8;
}}
</style>
</head>
<body>
<h1>预览链接中心</h1>

<div class="card notice">
  <b>说明：</b><br>
  这个页面只新增预览入口，不删除原来的功能。<br>
  原有的字体家族、可变字体、普通预览、中国国标版都会保留。<br>
  当前最新任务 ID：<code>{latest_job or "未检测到 jobs 任务"}</code>
</div>

<div class="card">
<table>
<thead>
<tr>
  <th style="width:60px;">序号</th>
  <th style="width:140px;">类型</th>
  <th style="width:220px;">入口</th>
  <th>链接</th>
  <th>说明</th>
</tr>
</thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</div>
</body>
</html>
"""
    return _PreviewHubHTMLResponse(html)


@app.get("/api/preview_links")
async def preview_links_json():
    return _PreviewHubJSONResponse({
        "ok": True,
        "latest_job": _preview_latest_job_id(),
        "links": _preview_collect_links(),
    })


_PREVIEW_LINKS_FLOATING_HTML = """
<div id="previewLinksFloatingBox" style="
  position:fixed;
  right:22px;
  bottom:22px;
  z-index:99999;
  background:#111;
  color:#fff;
  border-radius:12px;
  padding:12px 16px;
  box-shadow:0 8px 24px rgba(0,0,0,.18);
  font-family:Arial,'Microsoft YaHei',sans-serif;
  font-size:14px;
">
  <a href="/preview_links" target="_blank" style="color:#fff;text-decoration:none;font-weight:700;">
    打开预览链接中心
  </a>
</div>
"""


@app.middleware("http")
async def preview_links_home_inject_middleware(request, call_next):
    response = await call_next(request)

    try:
        if request.url.path not in ["/", ""]:
            return response

        ctype = response.headers.get("content-type", "")
        if "text/html" not in ctype.lower():
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        html = body.decode("utf-8", errors="ignore")

        if 'id="previewLinksFloatingBox"' not in html and "id='previewLinksFloatingBox'" not in html:
            if "</body>" in html:
                html = html.replace("</body>", _PREVIEW_LINKS_FLOATING_HTML + "\n</body>")
            else:
                html += _PREVIEW_LINKS_FLOATING_HTML

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return _PreviewHubHTMLResponse(
            html,
            status_code=response.status_code,
            headers=headers,
        )
    except Exception:
        return response

# =========================================================
from fastapi import Request as _ChinaGBRequest
from fastapi.responses import Response as _ChinaGBResponse
from fastapi.responses import JSONResponse as _ChinaGBJSONResponse
from fastapi.responses import FileResponse as _ChinaGBFileResponse
from fastapi.responses import HTMLResponse as _ChinaGBHTMLResponse
from pathlib import Path as _ChinaGBPath
import subprocess as _china_gb_subprocess
import sys as _china_gb_sys
import os as _china_gb_os
import zipfile as _china_gb_zipfile
import json as _china_gb_json
import csv as _china_gb_csv


_CHINA_GB_HOME_PATCH_JS = r"""
<script id="china-gb-main-fixed-v3">
(function(){
  function log(x){ console.log("[ChinaGB]", x); }

  function optionIsOldMongolian(opt){
    const t = (opt.textContent || "").trim();
    const v = (opt.value || "").trim();
    return (
      t.includes("传统蒙古文") ||
      t.includes("U+1820") ||
      t.includes("U+1842") ||
      t.includes("中国国标") ||
      v.includes("mongol") ||
      v.includes("china_gb")
    );
  }

  function patchSelects(){
    document.querySelectorAll("select").forEach(sel => {
      let touched = false;

      Array.from(sel.options).forEach(opt => {
        if(optionIsOldMongolian(opt)){
          opt.textContent = "中国国标版";
          opt.value = "china_gb";
          opt.dataset.chinaGb = "1";
          touched = true;
        }
      });

      if(touched){
        sel.dataset.hasChinaGb = "1";
      }
    });
  }

  function selectedChinaGB(){
    let ok = false;

    document.querySelectorAll("select").forEach(sel => {
      const opt = sel.options[sel.selectedIndex];
      if(!opt) return;

      const t = opt.textContent || "";
      const v = opt.value || "";

      if(t.includes("中国国标版") || v === "china_gb" || opt.dataset.chinaGb === "1"){
        ok = true;
      }
    });

    return ok;
  }

  function findMainForm(){
    const btn = findStartButton();
    if(btn){
      const form = btn.closest("form");
      if(form) return form;
    }

    return document.querySelector("form") || document.body;
  }

  function findStartButton(){
    const candidates = Array.from(document.querySelectorAll("button,input[type='submit'],input[type='button']"));
    for(const el of candidates){
      const t = (el.innerText || el.value || "").trim();
      if(t.includes("开始生成")){
        return el;
      }
    }
    return null;
  }

  function findSteps(){
    const inputs = Array.from(document.querySelectorAll("input"));

    // 优先找值为 20 / 30 / 15 这类中间步数
    for(const input of inputs){
      if(input.type === "file") continue;
      const v = parseInt(input.value || "", 10);
      if(Number.isFinite(v) && v >= 2 && v <= 80){
        return v;
      }
    }

    return 20;
  }

  function ensureBox(){
    let box = document.getElementById("chinaGbCleanResultBox");
    if(box) return box;

    const btn = findStartButton();
    box = document.createElement("div");
    box.id = "chinaGbCleanResultBox";
    box.style.marginTop = "18px";
    box.style.padding = "14px";
    box.style.border = "1px solid #ddd";
    box.style.borderRadius = "10px";
    box.style.background = "#fff";
    box.style.color = "#111";

    box.innerHTML = `
      <b>中国国标版生成状态</b>
      <pre id="chinaGbCleanResultText" style="white-space:pre-wrap;background:#f7f7f7;border:1px solid #ddd;border-radius:8px;padding:10px;margin-top:10px;max-height:360px;overflow:auto;">等待生成...</pre>
      <div id="chinaGbCleanLinks" style="line-height:2;margin-top:10px;"></div>
    `;

    if(btn && btn.parentNode){
      btn.parentNode.insertBefore(box, btn.nextSibling);
    }else{
      document.body.appendChild(box);
    }

    return box;
  }

  async function runBuild(){
    const box = ensureBox();
    const text = document.getElementById("chinaGbCleanResultText");
    const links = document.getElementById("chinaGbCleanLinks");

    text.textContent = "正在生成中国国标版 TTF。不要关闭页面。";
    links.innerHTML = "";

    const fd = new FormData();

    const files = Array.from(document.querySelectorAll("input[type='file']"));
    for(const input of files){
      if(input.files && input.files.length){
        for(const f of input.files){
          fd.append("font_files", f);
        }
      }
    }

    fd.append("steps", String(findSteps()));

    const res = await fetch("/api/china_gb_clean/build", {
      method: "POST",
      body: fd
    });

    const data = await res.json();

    text.textContent = JSON.stringify(data, null, 2);

    if(data.ok){
      links.innerHTML = `
        <a href="/china_gb_preview" target="_blank">打开中国国标版 TTF 预览</a><br>
        <a href="/api/china_gb_clean/download_zip" target="_blank">下载中国国标版 TTF 压缩包</a><br>
        <span style="font-size:12px;color:#555;">原来的可变字体、字体家族功能没有删除，仍按原页面保留。</span>
      `;
    }
  }

  function bind(){
    const btn = findStartButton();
    if(!btn || btn.dataset.chinaGbFixedBound === "1") return;

    btn.dataset.chinaGbFixedBound = "1";

    btn.addEventListener("click", function(e){
      patchSelects();

      if(!selectedChinaGB()){
        return;
      }

      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();

      runBuild().catch(err => {
        ensureBox();
        document.getElementById("chinaGbCleanResultText").textContent =
          "中国国标版生成失败：\\n" + err;
      });

      return false;
    }, true);
  }

  function run(){
    patchSelects();
    bind();
  }

  document.addEventListener("DOMContentLoaded", run);
  setTimeout(run, 300);
  setTimeout(run, 1000);
  setTimeout(run, 2000);
})();
</script>
"""


@app.middleware("http")
async def _china_gb_main_fixed_html_patch(request, call_next):
    response = await call_next(request)

    # 只处理首页，不污染旧的 SVG 预览、家族预览、可变字体预览页面
    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    # 只改首页下拉框文案，不改旧预览页面
    html = html.replace("传统蒙古文基础字母 U+1820-U+1842", "中国国标版")
    html = html.replace("传统蒙古文（中国国标增强）", "中国国标版")
    html = html.replace("传统蒙古文：中国国标增强", "中国国标版")
    html = html.replace("中国国标增强", "中国国标版")
    html = html.replace("国标增强版", "中国国标版")

    if "china-gb-main-fixed-v3" not in html:
        if "</body>" in html:
            html = html.replace("</body>", _CHINA_GB_HOME_PATCH_JS + "\n</body>")
        else:
            html += _CHINA_GB_HOME_PATCH_JS

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return _ChinaGBHTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers
    )


def _china_gb_collect_fonts():
    font_dir = _ChinaGBPath("input/fonts")
    font_dir.mkdir(parents=True, exist_ok=True)

    fonts = []
    for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
        fonts.extend(font_dir.glob(ext))

    return sorted(fonts)


async def _china_gb_save_uploaded_fonts(form):
    font_dir = _ChinaGBPath("input/fonts")
    font_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []

    for key, value in form.multi_items():
        if hasattr(value, "filename") and value.filename:
            name = _ChinaGBPath(value.filename).name
            if not name.lower().endswith((".ttf", ".otf")):
                continue
            data = await value.read()
            if not data:
                continue
            uploaded.append((name, data))

    if len(uploaded) >= 2:
        # 只有确实上传两个字体时才替换 input/fonts
        for old in font_dir.glob("*"):
            if old.is_file() and old.suffix.lower() in [".ttf", ".otf"]:
                old.unlink()

        for name, data in uploaded[:2]:
            (font_dir / name).write_bytes(data)

    return uploaded


@app.post("/api/china_gb_clean/build")
async def _china_gb_clean_build(request: _ChinaGBRequest):
    try:
        form = await request.form()

        steps = 20
        for key, value in form.multi_items():
            if key == "steps":
                try:
                    v = int(str(value))
                    if 2 <= v <= 80:
                        steps = v
                except Exception:
                    pass

        await _china_gb_save_uploaded_fonts(form)

        fonts = _china_gb_collect_fonts()
        if len(fonts) < 2:
            return _ChinaGBJSONResponse({
                "ok": False,
                "error": "中国国标版至少需要两个字体文件。",
                "need": "请上传字体 A 和字体 B。",
                "font_dir": "input/fonts"
            }, status_code=400)

        required = [
            "scripts/check_mongolian_gb_font_coverage.py",
            "scripts/build_mongolian_gb_effective_mapping.py",
            "scripts/build_mongolian_gb_ligature_table6_template.py",
            "scripts/build_mongolian_gb_runtime_strict.py",
            "scripts/build_mongolian_gb_ttf_steps.py",
        ]

        missing = [x for x in required if not _ChinaGBPath(x).exists()]
        if missing:
            return _ChinaGBJSONResponse({
                "ok": False,
                "error": "缺少中国国标版脚本。",
                "missing": missing
            }, status_code=500)

        env = dict(_china_gb_os.environ)
        env["MGB_STEPS"] = str(steps)

        commands = [
            [_china_gb_sys.executable, "scripts/check_mongolian_gb_font_coverage.py"],
            [_china_gb_sys.executable, "scripts/build_mongolian_gb_effective_mapping.py"],
            [_china_gb_sys.executable, "scripts/build_mongolian_gb_ligature_table6_template.py"],
            [_china_gb_sys.executable, "scripts/build_mongolian_gb_runtime_strict.py"],
            [_china_gb_sys.executable, "scripts/build_mongolian_gb_ttf_steps.py"],
        ]

        logs = []

        for cmd in commands:
            r = _china_gb_subprocess.run(
                cmd,
                cwd=str(_ChinaGBPath.cwd()),
                env=env,
                capture_output=True,
                text=True,
                timeout=900
            )

            logs.append({
                "cmd": " ".join(cmd),
                "returncode": r.returncode,
                "stdout": r.stdout[-3000:],
                "stderr": r.stderr[-3000:],
            })

            if r.returncode != 0:
                return _ChinaGBJSONResponse({
                    "ok": False,
                    "failed_cmd": " ".join(cmd),
                    "logs": logs
                }, status_code=500)

        out_dir = _ChinaGBPath("output/mongolian_gb_ttf_steps")
        zip_path = _ChinaGBPath("output/mongolian_gb_ttf_steps.zip")

        if zip_path.exists():
            zip_path.unlink()

        with _china_gb_zipfile.ZipFile(zip_path, "w", compression=_china_gb_zipfile.ZIP_DEFLATED) as z:
            for f in out_dir.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(out_dir.parent))

            for extra in [
                _ChinaGBPath("data/mongolian_gb_runtime_strict.csv"),
                _ChinaGBPath("data/mongolian_gb_ligature_table6_strict.csv"),
            ]:
                if extra.exists():
                    z.write(extra, extra)

        ttf_files = sorted(out_dir.glob("complete_oyun_gb_step_*.ttf"))

        return _ChinaGBJSONResponse({
            "ok": True,
            "mode": "中国国标版",
            "steps": steps,
            "fonts": [x.name for x in fonts[:2]],
            "ttf_count": len(ttf_files),
            "output_dir": str(out_dir),
            "preview_url": "/china_gb_preview",
            "download_url": "/api/china_gb_clean/download_zip",
            "message": "已生成中国国标版 TTF。原有可变字体和字体家族功能没有删除。",
            "logs": logs[-2:],
        })

    except Exception as e:
        return _ChinaGBJSONResponse({
            "ok": False,
            "error": str(e)
        }, status_code=500)


@app.get("/api/china_gb_clean/download_zip")
async def _china_gb_clean_download_zip():
    zip_path = _ChinaGBPath("output/mongolian_gb_ttf_steps.zip")

    if not zip_path.exists():
        return _ChinaGBJSONResponse({
            "ok": False,
            "error": "还没有生成中国国标版 TTF 压缩包。"
        }, status_code=404)

    return _ChinaGBFileResponse(
        path=str(zip_path),
        filename="mongolian_gb_ttf_steps.zip",
        media_type="application/zip"
    )


@app.get("/api/china_gb_clean/ttf/{filename}")
async def _china_gb_clean_ttf_file(filename: str):
    safe = _ChinaGBPath(filename).name
    path = _ChinaGBPath("output/mongolian_gb_ttf_steps") / safe

    if not path.exists() or path.suffix.lower() != ".ttf":
        return _ChinaGBJSONResponse({"ok": False, "error": "TTF not found"}, status_code=404)

    return _ChinaGBFileResponse(
        path=str(path),
        filename=safe,
        media_type="font/ttf"
    )


def _china_gb_text_from_codepoints(s: str) -> str:
    out = []
    for part in str(s).replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        try:
            if part.upper().startswith("U+"):
                cp = int(part[2:], 16)
            else:
                cp = int(part, 16)
            out.append(chr(cp))
        except Exception:
            pass
    return "".join(out)


@app.get("/china_gb_preview", response_class=_ChinaGBHTMLResponse)
async def _china_gb_preview_page():
    out_dir = _ChinaGBPath("output/mongolian_gb_ttf_steps")
    files = sorted(out_dir.glob("*.ttf"))

    runtime_path = _ChinaGBPath("data/mongolian_gb_runtime_strict.csv")
    samples = []

    if runtime_path.exists():
        try:
            with runtime_path.open("r", encoding="utf-8-sig") as f:
                reader = _china_gb_csv.DictReader(f)
                for row in reader:
                    txt = _china_gb_text_from_codepoints(row.get("text_codepoints", ""))
                    if txt:
                        samples.append({
                            "text": txt,
                            "label": row.get("display_group", "") + " / " + row.get("base_unicode", "")
                        })
                    if len(samples) >= 80:
                        break
        except Exception:
            pass

    if not samples:
        samples = [
            {"text": "ᠠᠡᠢᠣᠤᠥᠦᠧᠨᠩ", "label": "默认蒙古文测试"},
            {"text": "ᠮᠣᠩᠭᠣᠯ", "label": "蒙古文单词测试"},
        ]

    css_fonts = []
    cards = []

    for i, f in enumerate(files[:30], 1):
        fam = f"ChinaGBStep{i}"
        css_fonts.append(f"""
@font-face {{
  font-family: '{fam}';
  src: url('/api/china_gb_clean/ttf/{f.name}') format('truetype');
}}
""")

        sample_html = []
        for item in samples:
            sample_html.append(f"""
<div class="glyph-card">
  <div class="glyph" style="font-family:'{fam}', serif;">{item['text']}</div>
  <div class="code">{item['label']}</div>
</div>
""")

        cards.append(f"""
<section class="step-card">
  <h2>{f.name}</h2>
  <div class="grid">
    {''.join(sample_html)}
  </div>
</section>
""")

    html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>中国国标版 TTF 预览</title>
<style>
{''.join(css_fonts)}
body {{
  margin:0;
  padding:24px;
  font-family:Arial,"Microsoft YaHei",sans-serif;
  background:#f5f6f8;
  color:#111;
}}
h1 {{
  font-size:32px;
  margin:0 0 12px 0;
}}
.top {{
  background:#fff;
  border:1px solid #ddd;
  border-radius:14px;
  padding:16px;
  margin-bottom:18px;
  line-height:1.8;
}}
.step-card {{
  background:#fff;
  border:1px solid #ddd;
  border-radius:14px;
  padding:16px;
  margin-bottom:18px;
}}
.step-card h2 {{
  margin:0 0 12px 0;
  font-size:18px;
}}
.grid {{
  display:flex;
  flex-wrap:wrap;
  gap:10px;
}}
.glyph-card {{
  width:96px;
  min-height:105px;
  border:1px solid #ddd;
  border-radius:10px;
  background:#fff;
  padding:8px;
  text-align:center;
}}
.glyph {{
  font-size:44px;
  line-height:1.1;
  color:#000;
  min-height:55px;
}}
.code {{
  font-size:10px;
  color:#666;
  word-break:break-all;
}}
a {{
  color:#111;
  font-weight:700;
}}
</style>
</head>
<body>
<h1>中国国标版 TTF 预览</h1>

<div class="top">
  <div>检测到 TTF 文件：{len(files)} 个</div>
  <div><a href="/api/china_gb_clean/download_zip" target="_blank">下载中国国标版 TTF 压缩包</a></div>
  <div><a href="/" target="_blank">返回首页</a></div>
  <div>说明：该页面只预览中国国标版 TTF。原来的 SVG 预览、字体家族预览、可变字体预览功能没有删除。</div>
</div>

{''.join(cards) if cards else '<div class="top">还没有生成 TTF。请先返回首页选择“中国国标版”并点击开始生成。</div>'}
</body>
</html>
"""
    return _ChinaGBHTMLResponse(html)

# =========================================================
from fastapi import Request as _ChinaGBRequest
from fastapi.responses import Response as _ChinaGBResponse
from fastapi.responses import JSONResponse as _ChinaGBJSONResponse
from fastapi.responses import FileResponse as _ChinaGBFileResponse
from fastapi.responses import HTMLResponse as _ChinaGBHTMLResponse
from pathlib import Path as _ChinaGBPath
import subprocess as _china_gb_subprocess
import sys as _china_gb_sys
import os as _china_gb_os
import zipfile as _china_gb_zipfile
import json as _china_gb_json
import csv as _china_gb_csv


_CHINA_GB_HOME_PATCH_JS = r"""
<script id="china-gb-main-fixed-v3">
(function(){
  function log(x){ console.log("[ChinaGB]", x); }

  function optionIsOldMongolian(opt){
    const t = (opt.textContent || "").trim();
    const v = (opt.value || "").trim();
    return (
      t.includes("传统蒙古文") ||
      t.includes("U+1820") ||
      t.includes("U+1842") ||
      t.includes("中国国标") ||
      v.includes("mongol") ||
      v.includes("china_gb")
    );
  }

  function patchSelects(){
    document.querySelectorAll("select").forEach(sel => {
      let touched = false;

      Array.from(sel.options).forEach(opt => {
        if(optionIsOldMongolian(opt)){
          opt.textContent = "中国国标版";
          opt.value = "china_gb";
          opt.dataset.chinaGb = "1";
          touched = true;
        }
      });

      if(touched){
        sel.dataset.hasChinaGb = "1";
      }
    });
  }

  function selectedChinaGB(){
    let ok = false;

    document.querySelectorAll("select").forEach(sel => {
      const opt = sel.options[sel.selectedIndex];
      if(!opt) return;

      const t = opt.textContent || "";
      const v = opt.value || "";

      if(t.includes("中国国标版") || v === "china_gb" || opt.dataset.chinaGb === "1"){
        ok = true;
      }
    });

    return ok;
  }

  function findMainForm(){
    const btn = findStartButton();
    if(btn){
      const form = btn.closest("form");
      if(form) return form;
    }

    return document.querySelector("form") || document.body;
  }

  function findStartButton(){
    const candidates = Array.from(document.querySelectorAll("button,input[type='submit'],input[type='button']"));
    for(const el of candidates){
      const t = (el.innerText || el.value || "").trim();
      if(t.includes("开始生成")){
        return el;
      }
    }
    return null;
  }

  function findSteps(){
    const inputs = Array.from(document.querySelectorAll("input"));

    // 优先找值为 20 / 30 / 15 这类中间步数
    for(const input of inputs){
      if(input.type === "file") continue;
      const v = parseInt(input.value || "", 10);
      if(Number.isFinite(v) && v >= 2 && v <= 80){
        return v;
      }
    }

    return 20;
  }

  function ensureBox(){
    let box = document.getElementById("chinaGbCleanResultBox");
    if(box) return box;

    const btn = findStartButton();
    box = document.createElement("div");
    box.id = "chinaGbCleanResultBox";
    box.style.marginTop = "18px";
    box.style.padding = "14px";
    box.style.border = "1px solid #ddd";
    box.style.borderRadius = "10px";
    box.style.background = "#fff";
    box.style.color = "#111";

    box.innerHTML = `
      <b>中国国标版生成状态</b>
      <pre id="chinaGbCleanResultText" style="white-space:pre-wrap;background:#f7f7f7;border:1px solid #ddd;border-radius:8px;padding:10px;margin-top:10px;max-height:360px;overflow:auto;">等待生成...</pre>
      <div id="chinaGbCleanLinks" style="line-height:2;margin-top:10px;"></div>
    `;

    if(btn && btn.parentNode){
      btn.parentNode.insertBefore(box, btn.nextSibling);
    }else{
      document.body.appendChild(box);
    }

    return box;
  }

  async function runBuild(){
    const box = ensureBox();
    const text = document.getElementById("chinaGbCleanResultText");
    const links = document.getElementById("chinaGbCleanLinks");

    text.textContent = "正在生成中国国标版 TTF。不要关闭页面。";
    links.innerHTML = "";

    const fd = new FormData();

    const files = Array.from(document.querySelectorAll("input[type='file']"));
    for(const input of files){
      if(input.files && input.files.length){
        for(const f of input.files){
          fd.append("font_files", f);
        }
      }
    }

    fd.append("steps", String(findSteps()));

    const res = await fetch("/api/china_gb_clean/build", {
      method: "POST",
      body: fd
    });

    const data = await res.json();

    text.textContent = JSON.stringify(data, null, 2);

    if(data.ok){
      links.innerHTML = `
        <a href="/china_gb_preview" target="_blank">打开中国国标版 TTF 预览</a><br>
        <a href="/api/china_gb_clean/download_zip" target="_blank">下载中国国标版 TTF 压缩包</a><br>
        <span style="font-size:12px;color:#555;">原来的可变字体、字体家族功能没有删除，仍按原页面保留。</span>
      `;
    }
  }

  function bind(){
    const btn = findStartButton();
    if(!btn || btn.dataset.chinaGbFixedBound === "1") return;

    btn.dataset.chinaGbFixedBound = "1";

    btn.addEventListener("click", function(e){
      patchSelects();

      if(!selectedChinaGB()){
        return;
      }

      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();

      runBuild().catch(err => {
        ensureBox();
        document.getElementById("chinaGbCleanResultText").textContent =
          "中国国标版生成失败：\\n" + err;
      });

      return false;
    }, true);
  }

  function run(){
    patchSelects();
    bind();
  }

  document.addEventListener("DOMContentLoaded", run);
  setTimeout(run, 300);
  setTimeout(run, 1000);
  setTimeout(run, 2000);
})();
</script>
"""


@app.middleware("http")
async def _china_gb_main_fixed_html_patch(request, call_next):
    response = await call_next(request)

    # 只处理首页，不污染旧的 SVG 预览、家族预览、可变字体预览页面
    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    # 只改首页下拉框文案，不改旧预览页面
    html = html.replace("传统蒙古文基础字母 U+1820-U+1842", "中国国标版")
    html = html.replace("传统蒙古文（中国国标增强）", "中国国标版")
    html = html.replace("传统蒙古文：中国国标增强", "中国国标版")
    html = html.replace("中国国标增强", "中国国标版")
    html = html.replace("国标增强版", "中国国标版")

    if "china-gb-main-fixed-v3" not in html:
        if "</body>" in html:
            html = html.replace("</body>", _CHINA_GB_HOME_PATCH_JS + "\n</body>")
        else:
            html += _CHINA_GB_HOME_PATCH_JS

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return _ChinaGBHTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers
    )


def _china_gb_collect_fonts():
    font_dir = _ChinaGBPath("input/fonts")
    font_dir.mkdir(parents=True, exist_ok=True)

    fonts = []
    for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
        fonts.extend(font_dir.glob(ext))

    return sorted(fonts)


async def _china_gb_save_uploaded_fonts(form):
    font_dir = _ChinaGBPath("input/fonts")
    font_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []

    for key, value in form.multi_items():
        if hasattr(value, "filename") and value.filename:
            name = _ChinaGBPath(value.filename).name
            if not name.lower().endswith((".ttf", ".otf")):
                continue
            data = await value.read()
            if not data:
                continue
            uploaded.append((name, data))

    if len(uploaded) >= 2:
        # 只有确实上传两个字体时才替换 input/fonts
        for old in font_dir.glob("*"):
            if old.is_file() and old.suffix.lower() in [".ttf", ".otf"]:
                old.unlink()

        for name, data in uploaded[:2]:
            (font_dir / name).write_bytes(data)

    return uploaded


@app.post("/api/china_gb_clean/build")
async def _china_gb_clean_build(request: _ChinaGBRequest):
    try:
        form = await request.form()

        steps = 20
        for key, value in form.multi_items():
            if key == "steps":
                try:
                    v = int(str(value))
                    if 2 <= v <= 80:
                        steps = v
                except Exception:
                    pass

        await _china_gb_save_uploaded_fonts(form)

        fonts = _china_gb_collect_fonts()
        if len(fonts) < 2:
            return _ChinaGBJSONResponse({
                "ok": False,
                "error": "中国国标版至少需要两个字体文件。",
                "need": "请上传字体 A 和字体 B。",
                "font_dir": "input/fonts"
            }, status_code=400)

        required = [
            "scripts/check_mongolian_gb_font_coverage.py",
            "scripts/build_mongolian_gb_effective_mapping.py",
            "scripts/build_mongolian_gb_ligature_table6_template.py",
            "scripts/build_mongolian_gb_runtime_strict.py",
            "scripts/build_mongolian_gb_ttf_steps.py",
        ]

        missing = [x for x in required if not _ChinaGBPath(x).exists()]
        if missing:
            return _ChinaGBJSONResponse({
                "ok": False,
                "error": "缺少中国国标版脚本。",
                "missing": missing
            }, status_code=500)

        env = dict(_china_gb_os.environ)
        env["MGB_STEPS"] = str(steps)

        commands = [
            [_china_gb_sys.executable, "scripts/check_mongolian_gb_font_coverage.py"],
            [_china_gb_sys.executable, "scripts/build_mongolian_gb_effective_mapping.py"],
            [_china_gb_sys.executable, "scripts/build_mongolian_gb_ligature_table6_template.py"],
            [_china_gb_sys.executable, "scripts/build_mongolian_gb_runtime_strict.py"],
            [_china_gb_sys.executable, "scripts/build_mongolian_gb_ttf_steps.py"],
        ]

        logs = []

        for cmd in commands:
            r = _china_gb_subprocess.run(
                cmd,
                cwd=str(_ChinaGBPath.cwd()),
                env=env,
                capture_output=True,
                text=True,
                timeout=900
            )

            logs.append({
                "cmd": " ".join(cmd),
                "returncode": r.returncode,
                "stdout": r.stdout[-3000:],
                "stderr": r.stderr[-3000:],
            })

            if r.returncode != 0:
                return _ChinaGBJSONResponse({
                    "ok": False,
                    "failed_cmd": " ".join(cmd),
                    "logs": logs
                }, status_code=500)

        out_dir = _ChinaGBPath("output/mongolian_gb_ttf_steps")
        zip_path = _ChinaGBPath("output/mongolian_gb_ttf_steps.zip")

        if zip_path.exists():
            zip_path.unlink()

        with _china_gb_zipfile.ZipFile(zip_path, "w", compression=_china_gb_zipfile.ZIP_DEFLATED) as z:
            for f in out_dir.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(out_dir.parent))

            for extra in [
                _ChinaGBPath("data/mongolian_gb_runtime_strict.csv"),
                _ChinaGBPath("data/mongolian_gb_ligature_table6_strict.csv"),
            ]:
                if extra.exists():
                    z.write(extra, extra)

        ttf_files = sorted(out_dir.glob("complete_oyun_gb_step_*.ttf"))

        return _ChinaGBJSONResponse({
            "ok": True,
            "mode": "中国国标版",
            "steps": steps,
            "fonts": [x.name for x in fonts[:2]],
            "ttf_count": len(ttf_files),
            "output_dir": str(out_dir),
            "preview_url": "/china_gb_preview",
            "download_url": "/api/china_gb_clean/download_zip",
            "message": "已生成中国国标版 TTF。原有可变字体和字体家族功能没有删除。",
            "logs": logs[-2:],
        })

    except Exception as e:
        return _ChinaGBJSONResponse({
            "ok": False,
            "error": str(e)
        }, status_code=500)


@app.get("/api/china_gb_clean/download_zip")
async def _china_gb_clean_download_zip():
    zip_path = _ChinaGBPath("output/mongolian_gb_ttf_steps.zip")

    if not zip_path.exists():
        return _ChinaGBJSONResponse({
            "ok": False,
            "error": "还没有生成中国国标版 TTF 压缩包。"
        }, status_code=404)

    return _ChinaGBFileResponse(
        path=str(zip_path),
        filename="mongolian_gb_ttf_steps.zip",
        media_type="application/zip"
    )


@app.get("/api/china_gb_clean/ttf/{filename}")
async def _china_gb_clean_ttf_file(filename: str):
    safe = _ChinaGBPath(filename).name
    path = _ChinaGBPath("output/mongolian_gb_ttf_steps") / safe

    if not path.exists() or path.suffix.lower() != ".ttf":
        return _ChinaGBJSONResponse({"ok": False, "error": "TTF not found"}, status_code=404)

    return _ChinaGBFileResponse(
        path=str(path),
        filename=safe,
        media_type="font/ttf"
    )


def _china_gb_text_from_codepoints(s: str) -> str:
    out = []
    for part in str(s).replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        try:
            if part.upper().startswith("U+"):
                cp = int(part[2:], 16)
            else:
                cp = int(part, 16)
            out.append(chr(cp))
        except Exception:
            pass
    return "".join(out)


@app.get("/china_gb_preview", response_class=_ChinaGBHTMLResponse)
async def _china_gb_preview_page():
    out_dir = _ChinaGBPath("output/mongolian_gb_ttf_steps")
    files = sorted(out_dir.glob("*.ttf"))

    runtime_path = _ChinaGBPath("data/mongolian_gb_runtime_strict.csv")
    samples = []

    if runtime_path.exists():
        try:
            with runtime_path.open("r", encoding="utf-8-sig") as f:
                reader = _china_gb_csv.DictReader(f)
                for row in reader:
                    txt = _china_gb_text_from_codepoints(row.get("text_codepoints", ""))
                    if txt:
                        samples.append({
                            "text": txt,
                            "label": row.get("display_group", "") + " / " + row.get("base_unicode", "")
                        })
                    if len(samples) >= 80:
                        break
        except Exception:
            pass

    if not samples:
        samples = [
            {"text": "ᠠᠡᠢᠣᠤᠥᠦᠧᠨᠩ", "label": "默认蒙古文测试"},
            {"text": "ᠮᠣᠩᠭᠣᠯ", "label": "蒙古文单词测试"},
        ]

    css_fonts = []
    cards = []

    for i, f in enumerate(files[:30], 1):
        fam = f"ChinaGBStep{i}"
        css_fonts.append(f"""
@font-face {{
  font-family: '{fam}';
  src: url('/api/china_gb_clean/ttf/{f.name}') format('truetype');
}}
""")

        sample_html = []
        for item in samples:
            sample_html.append(f"""
<div class="glyph-card">
  <div class="glyph" style="font-family:'{fam}', serif;">{item['text']}</div>
  <div class="code">{item['label']}</div>
</div>
""")

        cards.append(f"""
<section class="step-card">
  <h2>{f.name}</h2>
  <div class="grid">
    {''.join(sample_html)}
  </div>
</section>
""")

    html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>中国国标版 TTF 预览</title>
<style>
{''.join(css_fonts)}
body {{
  margin:0;
  padding:24px;
  font-family:Arial,"Microsoft YaHei",sans-serif;
  background:#f5f6f8;
  color:#111;
}}
h1 {{
  font-size:32px;
  margin:0 0 12px 0;
}}
.top {{
  background:#fff;
  border:1px solid #ddd;
  border-radius:14px;
  padding:16px;
  margin-bottom:18px;
  line-height:1.8;
}}
.step-card {{
  background:#fff;
  border:1px solid #ddd;
  border-radius:14px;
  padding:16px;
  margin-bottom:18px;
}}
.step-card h2 {{
  margin:0 0 12px 0;
  font-size:18px;
}}
.grid {{
  display:flex;
  flex-wrap:wrap;
  gap:10px;
}}
.glyph-card {{
  width:96px;
  min-height:105px;
  border:1px solid #ddd;
  border-radius:10px;
  background:#fff;
  padding:8px;
  text-align:center;
}}
.glyph {{
  font-size:44px;
  line-height:1.1;
  color:#000;
  min-height:55px;
}}
.code {{
  font-size:10px;
  color:#666;
  word-break:break-all;
}}
a {{
  color:#111;
  font-weight:700;
}}
</style>
</head>
<body>
<h1>中国国标版 TTF 预览</h1>

<div class="top">
  <div>检测到 TTF 文件：{len(files)} 个</div>
  <div><a href="/api/china_gb_clean/download_zip" target="_blank">下载中国国标版 TTF 压缩包</a></div>
  <div><a href="/" target="_blank">返回首页</a></div>
  <div>说明：该页面只预览中国国标版 TTF。原来的 SVG 预览、字体家族预览、可变字体预览功能没有删除。</div>
</div>

{''.join(cards) if cards else '<div class="top">还没有生成 TTF。请先返回首页选择“中国国标版”并点击开始生成。</div>'}
</body>
</html>
"""
    return _ChinaGBHTMLResponse(html)


# =========================================================
# OYUN_FOUNDRY_GB_V1
# 按字体公司区分：中国国标版 - 奥云。
# 不删除原有可变字体、字体家族、普通插值功能。
# =========================================================
from fastapi import Request as _OyunGBRequest
from fastapi.responses import Response as _OyunGBResponse
from fastapi.responses import JSONResponse as _OyunGBJSONResponse
from fastapi.responses import FileResponse as _OyunGBFileResponse
from fastapi.responses import HTMLResponse as _OyunGBHTMLResponse
from pathlib import Path as _OyunGBPath
import subprocess as _oyun_subprocess
import sys as _oyun_sys
import os as _oyun_os
import csv as _oyun_csv


_OYUN_HOME_JS = r"""<script id="oyun-foundry-gb-v1-disabled">(function(){})();</script>"""


@app.middleware("http")
async def _oyun_gb_home_patch(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    html = html.replace("传统蒙古文基础字母 U+1820-U+1842", "传统蒙古文35个")
    html = html.replace("中国国标增强", "传统蒙古文35个")
    html = html.replace("国标增强版", "传统蒙古文35个")

    if "oyun-foundry-gb-v1" not in html:
        if "</body>" in html:
            html = html.replace("</body>", _OYUN_HOME_JS + "\n</body>")
        else:
            html += _OYUN_HOME_JS

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return _OyunGBHTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers
    )


async def _oyun_save_uploaded_fonts(form):
    font_dir = _OyunGBPath("input/foundry_oyun")
    font_dir.mkdir(parents=True, exist_ok=True)

    files = []

    for key, value in form.multi_items():
        if hasattr(value, "filename") and value.filename:
            name = _OyunGBPath(value.filename).name
            if not name.lower().endswith((".ttf", ".otf")):
                continue
            data = await value.read()
            if not data:
                continue
            files.append((name, data))

    if len(files) >= 2:
        # 单独写入奥云工作目录，不破坏 input/fonts 里原来的字体。
        for old in font_dir.glob("*"):
            if old.is_file() and old.suffix.lower() in [".ttf", ".otf"]:
                old.unlink()

        (font_dir / files[0][0]).write_bytes(files[0][1])
        (font_dir / files[1][0]).write_bytes(files[1][1])

    return files


@app.post("/api/foundry/oyun_gb/build")
async def _oyun_gb_build(request: _OyunGBRequest):
    try:
        form = await request.form()
        await _oyun_save_uploaded_fonts(form)

        steps = 20
        for key, value in form.multi_items():
            if key == "steps":
                try:
                    v = int(str(value))
                    if 2 <= v <= 80:
                        steps = v
                except Exception:
                    pass

        font_dir = _OyunGBPath("input/foundry_oyun")
        fonts = []
        for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
            fonts.extend(font_dir.glob(ext))
        fonts = sorted(fonts)

        if len(fonts) < 2:
            # 允许 fallback 到 input/fonts 的 Oyun 文件。
            fallback = []
            for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
                fallback.extend(_OyunGBPath("input/fonts").glob(ext))
            fallback = sorted([f for f in fallback if "oyun" in f.name.lower()])
            if len(fallback) < 2:
                return _OyunGBJSONResponse({
                    "ok": False,
                    "error": "没有找到两个奥云字体。请上传两个奥云 .ttf/.otf 字体。",
                    "font_dir": str(font_dir)
                }, status_code=400)

        env = dict(_oyun_os.environ)
        env["MGB_STEPS"] = str(steps)
        env["OYUN_FONT_DIR"] = str(font_dir)

        cmd = [_oyun_sys.executable, "scripts/build_oyun_gb_version.py"]

        r = _oyun_subprocess.run(
            cmd,
            cwd=str(_OyunGBPath.cwd()),
            env=env,
            capture_output=True,
            text=True,
            timeout=900
        )

        if r.returncode != 0:
            return _OyunGBJSONResponse({
                "ok": False,
                "cmd": " ".join(cmd),
                "stdout": r.stdout[-5000:],
                "stderr": r.stderr[-5000:]
            }, status_code=500)

        runtime_path = _OyunGBPath("output/oyun_gb_ttf_steps/oyun_gb_runtime.csv")
        report_path = _OyunGBPath("output/oyun_gb_ttf_steps/build_report.csv")

        counts = {}
        if runtime_path.exists():
            with runtime_path.open("r", encoding="utf-8-sig") as f:
                reader = _oyun_csv.DictReader(f)
                for row in reader:
                    g = row.get("display_group", "")
                    counts[g] = counts.get(g, 0) + 1

        ttf_files = sorted(_OyunGBPath("output/oyun_gb_ttf_steps").glob("complete_oyun_gb_step_*.ttf"))

        return _OyunGBJSONResponse({
            "ok": True,
            "mode": "奥云｜中国国标版",
            "steps": steps,
            "ttf_count": len(ttf_files),
            "runtime_counts": counts,
            "preview_url": "/oyun_gb_preview",
            "download_url": "/api/foundry/oyun_gb/download_zip",
            "runtime_csv": str(runtime_path),
            "report_csv": str(report_path),
            "stdout": r.stdout[-5000:],
            "stderr": r.stderr[-3000:],
        })

    except Exception as e:
        return _OyunGBJSONResponse({"ok": False, "error": str(e)}, status_code=500)



@app.get("/api/foundry/oyun_gb/download_zip")
async def _oyun_gb_download_zip():
    """
    稳定下载接口：
    优先下载完整 GB 版 zip：
    output/oyun_gb_complete_ttf_steps.zip
    如果不存在，再回退到旧 zip。
    """
    from pathlib import Path as _Path
    from fastapi.responses import FileResponse, JSONResponse

    complete_zip = _Path("output/oyun_gb_complete_ttf_steps.zip")
    fallback_zip = _Path("output/oyun_gb_ttf_steps.zip")

    if complete_zip.exists():
        zip_path = complete_zip
        filename = "oyun_gb_complete_ttf_steps.zip"
    elif fallback_zip.exists():
        zip_path = fallback_zip
        filename = "oyun_gb_ttf_steps.zip"
    else:
        return JSONResponse(
            {
                "ok": False,
                "error": "zip not found",
                "checked": [
                    str(complete_zip),
                    str(fallback_zip),
                ],
            },
            status_code=404,
        )

    return FileResponse(
        path=str(zip_path),
        filename=filename,
        media_type="application/zip",
    )


@app.get("/api/foundry/oyun_gb/ttf/{filename}")
async def _oyun_gb_ttf_file(filename: str):
    safe = _OyunGBPath(filename).name
    path = _OyunGBPath("output/oyun_gb_ttf_steps") / safe

    if not path.exists() or path.suffix.lower() != ".ttf":
        return _OyunGBJSONResponse({"ok": False, "error": "TTF not found"}, status_code=404)

    return _OyunGBFileResponse(path=str(path), filename=safe, media_type="font/ttf")


def _oyun_text_from_codepoints(s: str):
    out = []
    for part in str(s).replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        try:
            if part.upper().startswith("U+"):
                cp = int(part[2:], 16)
            else:
                cp = int(part, 16)
            out.append(chr(cp))
        except Exception:
            pass
    return "".join(out)


@app.get("/oyun_gb_preview", response_class=_OyunGBHTMLResponse)
async def _oyun_gb_preview():
    out_dir = _OyunGBPath("output/oyun_gb_ttf_steps")
    files = sorted(out_dir.glob("*.ttf"))

    runtime_path = out_dir / "oyun_gb_runtime.csv"

    samples = []
    counts = {}

    if runtime_path.exists():
        with runtime_path.open("r", encoding="utf-8-sig") as f:
            reader = _oyun_csv.DictReader(f)
            for row in reader:
                group = row.get("display_group", "")
                counts[group] = counts.get(group, 0) + 1

                txt = _oyun_text_from_codepoints(row.get("text_codepoints", ""))
                if txt:
                    samples.append({
                        "text": txt,
                        "label": group + " / " + row.get("base_unicode", "")
                    })

    if not samples:
        samples = [
            {"text": "ᠮᠣᠩᠭᠣᠯ", "label": "默认测试"},
            {"text": "ᠠᠡᠢᠣᠤᠥᠦᠧᠨᠩ", "label": "基础字母测试"}
        ]

    css = []
    sections = []

    for i, f in enumerate(files[:30], 1):
        fam = f"OyunGBStep{i}"
        css.append(f"""
@font-face {{
  font-family: '{fam}';
  src: url('/api/foundry/oyun_gb/ttf/{f.name}') format('truetype');
}}
""")

        cards = []
        for item in samples:
            cards.append(f"""
<div class="glyph-card">
  <div class="glyph" style="font-family:'{fam}', serif;">{item['text']}</div>
  <div class="code">{item['label']}</div>
</div>
""")

        sections.append(f"""
<section class="step-card">
  <h2>{f.name}</h2>
  <div class="grid">{''.join(cards)}</div>
</section>
""")

    count_html = "".join([f"<li>{k}: {v}</li>" for k, v in counts.items()])

    html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>奥云｜中国国标版 TTF 预览</title>
<style>
{''.join(css)}
body {{
  margin:0;
  padding:24px;
  font-family:Arial,"Microsoft YaHei",sans-serif;
  background:#f5f6f8;
  color:#111;
}}
h1 {{font-size:32px;margin:0 0 12px 0;}}
.top {{
  background:#fff;
  border:1px solid #ddd;
  border-radius:14px;
  padding:16px;
  margin-bottom:18px;
  line-height:1.8;
}}
.step-card {{
  background:#fff;
  border:1px solid #ddd;
  border-radius:14px;
  padding:16px;
  margin-bottom:18px;
}}
.step-card h2 {{margin:0 0 12px 0;font-size:18px;}}
.grid {{display:flex;flex-wrap:wrap;gap:10px;}}
.glyph-card {{
  width:112px;
  min-height:118px;
  border:1px solid #ddd;
  border-radius:10px;
  background:#fff;
  padding:8px;
  text-align:center;
}}
.glyph {{
  font-size:44px;
  line-height:1.15;
  color:#000;
  min-height:60px;
}}
.code {{
  font-size:10px;
  color:#666;
  word-break:break-all;
}}
a {{color:#111;font-weight:700;}}
</style>
</head>
<body>
<h1>奥云｜中国国标版 TTF 预览</h1>
<div class="top">
  <div>检测到 TTF 文件：{len(files)} 个</div>
  <div>字形统计：</div>
  <ul>{count_html}</ul>
  <div><a href="/api/foundry/oyun_gb/download_zip" target="_blank">下载奥云中国国标版 TTF 压缩包</a></div>
  <div><a href="/" target="_blank">返回首页</a></div>
  <div>说明：本页按奥云字体规则与中国国标清单对照，有则显示，没有跳过。原有可变字体和字体家族功能没有删除。</div>
</div>
{''.join(sections) if sections else '<div class="top">还没有生成 TTF，请先返回首页点击“按奥云规则生成中国国标版”。</div>'}
</body>
</html>
"""
    return _OyunGBHTMLResponse(html)


# =========================================================
# MENK_FOUNDRY_GB_V1
# 按字体公司区分：中国国标版 - 蒙科立。
# 不删除奥云，不删除原有可变字体、字体家族、普通插值功能。
# =========================================================
from fastapi import Request as _MenkGBRequest
from fastapi.responses import JSONResponse as _MenkGBJSONResponse
from fastapi.responses import FileResponse as _MenkGBFileResponse
from fastapi.responses import HTMLResponse as _MenkGBHTMLResponse
from pathlib import Path as _MenkGBPath
import subprocess as _menk_subprocess
import sys as _menk_sys
import os as _menk_os
import csv as _menk_csv


_MENK_HOME_JS = r"""<script id="menk-foundry-gb-v1-disabled">(function(){})();</script>"""


@app.middleware("http")
async def _menk_gb_home_patch(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    if "menk-foundry-gb-v1" not in html:
        if "</body>" in html:
            html = html.replace("</body>", _MENK_HOME_JS + "\n</body>")
        else:
            html += _MENK_HOME_JS

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return _MenkGBHTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers
    )


async def _menk_save_uploaded_fonts(form):
    font_dir = _MenkGBPath("input/foundry_menk")
    font_dir.mkdir(parents=True, exist_ok=True)

    files = []

    for key, value in form.multi_items():
        if hasattr(value, "filename") and value.filename:
            name = _MenkGBPath(value.filename).name
            if not name.lower().endswith((".ttf", ".otf")):
                continue
            data = await value.read()
            if not data:
                continue
            files.append((name, data))

    if len(files) >= 2:
        for old in font_dir.glob("*"):
            if old.is_file() and old.suffix.lower() in [".ttf", ".otf"]:
                old.unlink()

        (font_dir / files[0][0]).write_bytes(files[0][1])
        (font_dir / files[1][0]).write_bytes(files[1][1])

    return files


@app.post("/api/foundry/menk_gb/build")
async def _menk_gb_build(request: _MenkGBRequest):
    try:
        form = await request.form()
        await _menk_save_uploaded_fonts(form)

        steps = 20
        for key, value in form.multi_items():
            if key == "steps":
                try:
                    v = int(str(value))
                    if 2 <= v <= 80:
                        steps = v
                except Exception:
                    pass

        font_dir = _MenkGBPath("input/foundry_menk")
        env = dict(_menk_os.environ)
        env["MGB_STEPS"] = str(steps)
        env["MENK_FONT_DIR"] = str(font_dir)

        cmd = [_menk_sys.executable, "scripts/build_menk_gb_version.py"]

        r = _menk_subprocess.run(
            cmd,
            cwd=str(_MenkGBPath.cwd()),
            env=env,
            capture_output=True,
            text=True,
            timeout=900
        )

        if r.returncode != 0:
            return _MenkGBJSONResponse({
                "ok": False,
                "cmd": " ".join(cmd),
                "stdout": r.stdout[-5000:],
                "stderr": r.stderr[-5000:]
            }, status_code=500)

        runtime_path = _MenkGBPath("output/menk_gb_ttf_steps/menk_gb_runtime.csv")
        report_path = _MenkGBPath("output/menk_gb_ttf_steps/build_report.csv")

        counts = {}
        if runtime_path.exists():
            with runtime_path.open("r", encoding="utf-8-sig") as f:
                reader = _menk_csv.DictReader(f)
                for row in reader:
                    g = row.get("display_group", "")
                    counts[g] = counts.get(g, 0) + 1

        ttf_files = sorted(_MenkGBPath("output/menk_gb_ttf_steps").glob("*.ttf"))

        return _MenkGBJSONResponse({
            "ok": True,
            "mode": "蒙科立｜中国国标版",
            "steps": steps,
            "ttf_count": len(ttf_files),
            "runtime_counts": counts,
            "preview_url": "/menk_gb_preview",
            "download_url": "/api/foundry/menk_gb/download_zip",
            "runtime_csv": str(runtime_path),
            "report_csv": str(report_path),
            "stdout": r.stdout[-5000:],
            "stderr": r.stderr[-3000:],
        })

    except Exception as e:
        return _MenkGBJSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/foundry/menk_gb/download_zip")
async def _menk_gb_download_zip():
    path = _MenkGBPath("output/menk_gb_ttf_steps.zip")
    if not path.exists():
        return _MenkGBJSONResponse({"ok": False, "error": "还没有生成压缩包。"}, status_code=404)

    return _MenkGBFileResponse(
        path=str(path),
        filename="menk_gb_ttf_steps.zip",
        media_type="application/zip"
    )


@app.get("/api/foundry/menk_gb/ttf/{filename}")
async def _menk_gb_ttf_file(filename: str):
    safe = _MenkGBPath(filename).name
    path = _MenkGBPath("output/menk_gb_ttf_steps") / safe

    if not path.exists() or path.suffix.lower() != ".ttf":
        return _MenkGBJSONResponse({"ok": False, "error": "TTF not found"}, status_code=404)

    return _MenkGBFileResponse(path=str(path), filename=safe, media_type="font/ttf")


def _menk_text_from_codepoints(s: str):
    out = []
    for part in str(s).replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        try:
            if part.upper().startswith("U+"):
                cp = int(part[2:], 16)
            else:
                cp = int(part, 16)
            out.append(chr(cp))
        except Exception:
            pass
    return "".join(out)


@app.get("/menk_gb_preview", response_class=_MenkGBHTMLResponse)
async def _menk_gb_preview():
    out_dir = _MenkGBPath("output/menk_gb_ttf_steps")
    files = sorted(out_dir.glob("*.ttf"))

    runtime_path = out_dir / "menk_gb_runtime.csv"

    samples = []
    counts = {}

    if runtime_path.exists():
        with runtime_path.open("r", encoding="utf-8-sig") as f:
            reader = _menk_csv.DictReader(f)
            for row in reader:
                group = row.get("display_group", "")
                counts[group] = counts.get(group, 0) + 1

                txt = _menk_text_from_codepoints(row.get("text_codepoints", ""))
                if txt:
                    samples.append({
                        "text": txt,
                        "label": group + " / " + row.get("base_unicode", "")
                    })

    if not samples:
        samples = [
            {"text": "ᠮᠣᠩᠭᠣᠯ", "label": "默认测试"},
            {"text": "ᠠᠡᠢᠣᠤᠥᠦᠧᠨᠩ", "label": "基础字母测试"}
        ]

    css = []
    sections = []

    for i, f in enumerate(files[:30], 1):
        fam = f"MenkGBStep{i}"
        css.append(f"""
@font-face {{
  font-family: '{fam}';
  src: url('/api/foundry/menk_gb/ttf/{f.name}') format('truetype');
}}
""")

        cards = []
        for item in samples:
            cards.append(f"""
<div class="glyph-card">
  <div class="glyph" style="font-family:'{fam}', serif;">{item['text']}</div>
  <div class="code">{item['label']}</div>
</div>
""")

        sections.append(f"""
<section class="step-card">
  <h2>{f.name}</h2>
  <div class="grid">{''.join(cards)}</div>
</section>
""")

    count_html = "".join([f"<li>{k}: {v}</li>" for k, v in counts.items()])

    html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>蒙科立｜中国国标版 TTF 预览</title>
<style>
{''.join(css)}
body {{
  margin:0;
  padding:24px;
  font-family:Arial,"Microsoft YaHei",sans-serif;
  background:#f5f6f8;
  color:#111;
}}
h1 {{font-size:32px;margin:0 0 12px 0;}}
.top {{
  background:#fff;
  border:1px solid #ddd;
  border-radius:14px;
  padding:16px;
  margin-bottom:18px;
  line-height:1.8;
}}
.step-card {{
  background:#fff;
  border:1px solid #ddd;
  border-radius:14px;
  padding:16px;
  margin-bottom:18px;
}}
.step-card h2 {{margin:0 0 12px 0;font-size:18px;}}
.grid {{display:flex;flex-wrap:wrap;gap:10px;}}
.glyph-card {{
  width:112px;
  min-height:118px;
  border:1px solid #ddd;
  border-radius:10px;
  background:#fff;
  padding:8px;
  text-align:center;
}}
.glyph {{
  font-size:44px;
  line-height:1.15;
  color:#000;
  min-height:60px;
}}
.code {{
  font-size:10px;
  color:#666;
  word-break:break-all;
}}
a {{color:#111;font-weight:700;}}
</style>
</head>
<body>
<h1>蒙科立｜中国国标版 TTF 预览</h1>
<div class="top">
  <div>检测到 TTF 文件：{len(files)} 个</div>
  <div>字形统计：</div>
  <ul>{count_html}</ul>
  <div><a href="/api/foundry/menk_gb/download_zip" target="_blank">下载蒙科立中国国标版 TTF 压缩包</a></div>
  <div><a href="/" target="_blank">返回首页</a></div>
  <div>说明：本页按蒙科立字体规则与中国国标清单对照，有则显示，没有跳过。奥云、可变字体和字体家族功能没有删除。</div>
</div>
{''.join(sections) if sections else '<div class="top">还没有生成 TTF，请先返回首页点击“按蒙科立规则生成中国国标版”。</div>'}
</body>
</html>
"""
    return _MenkGBHTMLResponse(html)


# =========================================================
# FOUNDRY_UPLOAD_RULES_V1
# 网页上传时按字体公司调用规则：
# - 奥云｜中国国标版 -> input/foundry_oyun -> build_oyun_gb_version.py
# - 蒙科立｜中国国标版 -> input/foundry_menk -> build_menk_gb_version.py
# 不删除原有功能。
# =========================================================
from fastapi import Request as _FoundryRulesRequest
from fastapi.responses import HTMLResponse as _FoundryRulesHTMLResponse
from fastapi.responses import JSONResponse as _FoundryRulesJSONResponse
from fastapi.responses import FileResponse as _FoundryRulesFileResponse
from pathlib import Path as _FoundryRulesPath
import subprocess as _foundry_subprocess
import sys as _foundry_sys
import os as _foundry_os
import csv as _foundry_csv


_FOUNDRY_RULES_JS = r"""
<script id="foundry-upload-rules-v1">
(function(){
  function findStartButton(){
    const all = Array.from(document.querySelectorAll("button,input[type='submit'],input[type='button']"));
    for(const el of all){
      const t = (el.innerText || el.value || "").trim();
      if(t.includes("开始生成")) return el;
    }
    return null;
  }

  function injectPanel(){
    if(document.getElementById("foundryRulesPanel")) return;

    const btn = findStartButton();
    const parent = btn && btn.parentNode ? btn.parentNode : document.body;

    const box = document.createElement("div");
    box.id = "foundryRulesPanel";
    box.style.marginTop = "18px";
    box.style.padding = "16px";
    box.style.border = "1px solid #ddd";
    box.style.borderRadius = "12px";
    box.style.background = "#fff";
    box.style.color = "#111";

    box.innerHTML = `
      <h3 style="margin:0 0 10px 0;">按字体公司规则生成</h3>
      <div style="font-size:13px;line-height:1.8;margin-bottom:12px;">
        这里用于传统蒙古文中国国标版。先选择字体公司，再上传该公司的两个字体文件。<br>
        奥云和蒙科立分开处理，不会互相调用；原有字体家族、可变字体、普通插值功能保留。
      </div>

      <label style="display:block;margin-bottom:8px;font-weight:700;">字体公司</label>
      <select id="foundryRulesSelect" style="width:100%;padding:8px;margin-bottom:12px;">
        <option value="oyun">奥云｜中国国标版</option>
        <option value="menk">蒙科立｜中国国标版</option>
      </select>

      <label style="display:block;margin-bottom:8px;font-weight:700;">字体文件 A</label>
      <input id="foundryFontA" type="file" accept=".ttf,.otf" style="width:100%;margin-bottom:12px;">

      <label style="display:block;margin-bottom:8px;font-weight:700;">字体文件 B</label>
      <input id="foundryFontB" type="file" accept=".ttf,.otf" style="width:100%;margin-bottom:12px;">

      <label style="display:block;margin-bottom:8px;font-weight:700;">中间步数</label>
      <input id="foundrySteps" type="number" min="2" max="80" value="20" style="width:100%;padding:8px;margin-bottom:12px;">

      <button id="foundryRulesBuildBtn" type="button" style="background:#111;color:#fff;border:1px solid #111;border-radius:8px;padding:10px 16px;font-weight:700;cursor:pointer;">
        按所选字体公司规则生成
      </button>

      <pre id="foundryRulesStatus" style="white-space:pre-wrap;background:#f7f7f7;border:1px solid #ddd;border-radius:8px;padding:10px;margin-top:12px;max-height:360px;overflow:auto;">等待生成...</pre>

      <div id="foundryRulesLinks" style="line-height:2;margin-top:8px;"></div>
    `;

    if(btn && btn.nextSibling){
      parent.insertBefore(box, btn.nextSibling);
    }else{
      parent.appendChild(box);
    }

    document.getElementById("foundryRulesBuildBtn").addEventListener("click", runBuild);
  }

  async function runBuild(){
    const foundry = document.getElementById("foundryRulesSelect").value;
    const fontA = document.getElementById("foundryFontA").files[0];
    const fontB = document.getElementById("foundryFontB").files[0];
    const steps = document.getElementById("foundrySteps").value || "20";
    const status = document.getElementById("foundryRulesStatus");
    const links = document.getElementById("foundryRulesLinks");

    links.innerHTML = "";

    if(!fontA || !fontB){
      status.textContent = "请上传两个字体文件。";
      return;
    }

    const fd = new FormData();
    fd.append("foundry", foundry);
    fd.append("steps", steps);
    fd.append("font_files", fontA);
    fd.append("font_files", fontB);

    status.textContent = "正在生成，请稍等。系统会根据字体公司调用对应规则。";

    const res = await fetch("/api/foundry_rules/build", {
      method: "POST",
      body: fd
    });

    const data = await res.json();
    status.textContent = JSON.stringify(data, null, 2);

    if(data.ok){
      links.innerHTML = `
        <a href="${data.preview_url}" target="_blank">打开预览页面</a><br>
        <a href="${data.download_url}" target="_blank">下载 TTF 压缩包</a><br>
        <span style="font-size:12px;color:#666;">输出目录：${data.output_dir}</span>
      `;
    }
  }

  function run(){
    injectPanel();
  }

  document.addEventListener("DOMContentLoaded", run);
  setTimeout(run, 300);
  setTimeout(run, 1000);
})();
</script>
"""


@app.middleware("http")
async def _foundry_rules_home_inject(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    if "foundry-upload-rules-v1" not in html:
        if "</body>" in html:
            html = html.replace("</body>", _FOUNDRY_RULES_JS + "\n</body>")
        else:
            html += _FOUNDRY_RULES_JS

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return _FoundryRulesHTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers
    )


def _foundry_rule_config(foundry: str):
    foundry = (foundry or "").strip().lower()

    if foundry == "oyun":
        return {
            "label": "奥云｜中国国标版",
            "font_dir": _FoundryRulesPath("input/foundry_oyun"),
            "script": "scripts/build_oyun_gb_version.py",
            "env_key": "OYUN_FONT_DIR",
            "output_dir": _FoundryRulesPath("output/oyun_gb_ttf_steps"),
            "runtime_csv": _FoundryRulesPath("output/oyun_gb_ttf_steps/oyun_gb_runtime.csv"),
            "report_csv": _FoundryRulesPath("output/oyun_gb_ttf_steps/build_report.csv"),
            "preview_url": "/oyun_gb_preview",
            "download_url": "/api/foundry_rules/download/oyun",
            "zip_path": _FoundryRulesPath("output/oyun_gb_ttf_steps.zip"),
            "forbidden": ["menk", "mengke", "menksoft", "蒙科立", "蒙克立"]
        }

    if foundry == "menk":
        return {
            "label": "蒙科立｜中国国标版",
            "font_dir": _FoundryRulesPath("input/foundry_menk"),
            "script": "scripts/build_menk_gb_version.py",
            "env_key": "MENK_FONT_DIR",
            "output_dir": _FoundryRulesPath("output/menk_gb_ttf_steps"),
            "runtime_csv": _FoundryRulesPath("output/menk_gb_ttf_steps/menk_gb_runtime.csv"),
            "report_csv": _FoundryRulesPath("output/menk_gb_ttf_steps/build_report.csv"),
            "preview_url": "/menk_gb_preview",
            "download_url": "/api/foundry_rules/download/menk",
            "zip_path": _FoundryRulesPath("output/menk_gb_ttf_steps.zip"),
            "forbidden": ["oyun", "奥云"]
        }

    return None


def _foundry_read_font_identity(fp: _FoundryRulesPath):
    parts = [fp.name]
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(str(fp), lazy=True)
        if "name" in font:
            for n in font["name"].names:
                if n.nameID in [1, 2, 4, 6]:
                    try:
                        parts.append(n.toUnicode())
                    except Exception:
                        pass
    except Exception:
        pass

    return " ".join(parts)


def _foundry_count_runtime(runtime_csv: _FoundryRulesPath):
    counts = {}
    total = 0

    if not runtime_csv.exists():
        return {"total": 0, "groups": counts}

    with runtime_csv.open("r", encoding="utf-8-sig") as f:
        reader = _foundry_csv.DictReader(f)
        for row in reader:
            total += 1
            g = row.get("display_group", "")
            counts[g] = counts.get(g, 0) + 1

    return {"total": total, "groups": counts}


def _foundry_first_report(report_csv: _FoundryRulesPath):
    if not report_csv.exists():
        return {}

    with report_csv.open("r", encoding="utf-8-sig") as f:
        rows = list(_foundry_csv.DictReader(f))
        return rows[0] if rows else {}


@app.post("/api/foundry_rules/build")
async def _foundry_rules_build(request: _FoundryRulesRequest):
    try:
        form = await request.form()

        foundry = str(form.get("foundry", "")).strip().lower()
        cfg = _foundry_rule_config(foundry)

        if cfg is None:
            return _FoundryRulesJSONResponse({
                "ok": False,
                "error": "未知字体公司。请选择 oyun 或 menk。"
            }, status_code=400)

        steps = 20
        try:
            steps = int(str(form.get("steps", "20")))
            if steps < 2:
                steps = 2
            if steps > 80:
                steps = 80
        except Exception:
            steps = 20

        uploaded = []
        for key, value in form.multi_items():
            if hasattr(value, "filename") and value.filename:
                name = _FoundryRulesPath(value.filename).name
                if not name.lower().endswith((".ttf", ".otf")):
                    continue
                data = await value.read()
                if data:
                    uploaded.append((name, data))

        if len(uploaded) < 2:
            return _FoundryRulesJSONResponse({
                "ok": False,
                "error": "必须上传两个字体文件。"
            }, status_code=400)

        font_dir = cfg["font_dir"]
        font_dir.mkdir(parents=True, exist_ok=True)

        # 清空该字体公司的目录，不影响另一个公司的目录
        for old in font_dir.glob("*"):
            if old.is_file() and old.suffix.lower() in [".ttf", ".otf"]:
                old.unlink()

        saved = []
        for name, data in uploaded[:2]:
            fp = font_dir / name
            fp.write_bytes(data)
            saved.append(fp)

        # 检测是否误传其他字体公司
        bad = []
        for fp in saved:
            ident = _foundry_read_font_identity(fp).lower()
            for kw in cfg["forbidden"]:
                if kw.lower() in ident:
                    bad.append(fp.name)
                    break

        if bad:
            return _FoundryRulesJSONResponse({
                "ok": False,
                "mode": cfg["label"],
                "error": "检测到疑似错误字体公司文件，已拒绝生成。",
                "bad_files": bad,
                "font_dir": str(font_dir),
                "rule": "奥云和蒙科立必须分开上传，不能混用。"
            }, status_code=400)

        if not _FoundryRulesPath(cfg["script"]).exists():
            return _FoundryRulesJSONResponse({
                "ok": False,
                "error": "缺少生成脚本。",
                "script": cfg["script"]
            }, status_code=500)

        env = dict(_foundry_os.environ)
        env["MGB_STEPS"] = str(steps)
        env[cfg["env_key"]] = str(font_dir)

        cmd = [_foundry_sys.executable, cfg["script"]]

        r = _foundry_subprocess.run(
            cmd,
            cwd=str(_FoundryRulesPath.cwd()),
            env=env,
            capture_output=True,
            text=True,
            timeout=900
        )

        if r.returncode != 0:
            return _FoundryRulesJSONResponse({
                "ok": False,
                "mode": cfg["label"],
                "cmd": " ".join(cmd),
                "stdout": r.stdout[-6000:],
                "stderr": r.stderr[-6000:]
            }, status_code=500)

        complete_stdout = ""
        complete_stderr = ""
        complete_script = _FoundryRulesPath(f"scripts/build_{foundry}_gb_complete_v5_outline.py")
        complete_zip = _FoundryRulesPath(f"output/{foundry}_gb_complete_ttf_steps.zip")

        if complete_script.exists():
            try:
                complete_cmd = [_foundry_sys.executable, str(complete_script)]
                cr = _foundry_subprocess.run(
                    complete_cmd,
                    cwd=str(_FoundryRulesPath.cwd()),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=900
                )
                complete_stdout = cr.stdout[-5000:]
                complete_stderr = cr.stderr[-3000:]

                if cr.returncode != 0:
                    return _FoundryRulesJSONResponse({
                        "ok": False,
                        "mode": cfg["label"],
                        "cmd": " ".join(complete_cmd),
                        "stdout": complete_stdout,
                        "stderr": complete_stderr
                    }, status_code=500)

                if complete_zip.exists():
                    import shutil as _foundry_shutil
                    _foundry_shutil.copyfile(complete_zip, cfg["zip_path"])
            except Exception as complete_error:
                return _FoundryRulesJSONResponse({
                    "ok": False,
                    "mode": cfg["label"],
                    "error": "complete glyph build failed: " + str(complete_error)
                }, status_code=500)

        counts = _foundry_count_runtime(cfg["runtime_csv"])
        first_report = _foundry_first_report(cfg["report_csv"])

        return _FoundryRulesJSONResponse({
            "ok": True,
            "mode": cfg["label"],
            "steps": steps,
            "fonts": [x.name for x in saved],
            "runtime_total": counts["total"],
            "runtime_counts": counts["groups"],
            "step01_report": first_report,
            "output_dir": str(cfg["output_dir"]),
            "preview_url": cfg["preview_url"],
            "download_url": cfg["download_url"],
            "stdout": r.stdout[-5000:],
            "stderr": r.stderr[-3000:],
            "complete_stdout": complete_stdout,
            "complete_stderr": complete_stderr
        })

    except Exception as e:
        return _FoundryRulesJSONResponse({
            "ok": False,
            "error": str(e)
        }, status_code=500)


@app.get("/api/foundry_rules/download/{foundry}")
async def _foundry_rules_download(foundry: str):
    cfg = _foundry_rule_config(foundry)

    if cfg is None:
        return _FoundryRulesJSONResponse({"ok": False, "error": "未知字体公司。"}, status_code=400)

    path = cfg["zip_path"]

    if not path.exists():
        return _FoundryRulesJSONResponse({
            "ok": False,
            "error": "还没有生成压缩包。请先在首页上传字体并生成。",
            "mode": cfg["label"]
        }, status_code=404)

    return _FoundryRulesFileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/zip"
    )


# =========================================================
# TABLE6_STRICT_UI_V1
# GB/T 25914 表6严格强制性合体字数据状态、上传、校验、重新生成。
# 不删除奥云、蒙科立、字体家族、可变字体等原功能。
# =========================================================
from fastapi import Request as _Table6Request
from fastapi.responses import HTMLResponse as _Table6HTMLResponse
from fastapi.responses import JSONResponse as _Table6JSONResponse
from pathlib import Path as _Table6Path
import csv as _table6_csv
import shutil as _table6_shutil
import subprocess as _table6_subprocess
import sys as _table6_sys
import os as _table6_os
import time as _table6_time


_TABLE6_PATH = _Table6Path("data/gbt25914_table6_mandatory_ligatures_strict.csv")


def _table6_expected_codes():
    cols = ["010", "011", "012", "013", "014", "016", "019", "01A", "01C"]
    return {f"{c}{r}" for c in cols for r in "0123456789ABCDEF"}


def _table6_read_rows():
    if not _TABLE6_PATH.exists():
        return []
    with _TABLE6_PATH.open("r", encoding="utf-8-sig") as f:
        return list(_table6_csv.DictReader(f))


def _table6_status():
    rows = _table6_read_rows()
    expected = _table6_expected_codes()

    codes = [r.get("gb_code", "").strip().upper() for r in rows]
    code_set = set(codes)

    total = len(rows)
    filled_seq = sum(1 for r in rows if r.get("unicode_sequence", "").strip())
    verified = sum(1 for r in rows if r.get("verified", "").strip() == "1")

    missing_codes = sorted(expected - code_set)
    extra_codes = sorted(code_set - expected)

    errors = []

    if total != 90:
        errors.append(f"附录E有效强制性合体字转换项必须是90行，当前是 {total} 行。")

    if missing_codes:
        errors.append("缺少 gb_code：" + ", ".join(missing_codes[:10]) + (" ..." if len(missing_codes) > 10 else ""))

    if extra_codes:
        errors.append("多余 gb_code：" + ", ".join(extra_codes[:10]) + (" ..." if len(extra_codes) > 10 else ""))

    if filled_seq != 90:
        errors.append(f"unicode_sequence 未填满：{filled_seq}/90。")

    if verified != 90:
        errors.append(f"verified=1 未填满：{verified}/90。")

    complete = total == 90 and filled_seq == 90 and verified == 90 and not missing_codes and not extra_codes

    return {
        "path": str(_TABLE6_PATH),
        "total_rows": total,
        "filled_unicode_sequence": filled_seq,
        "verified_rows": verified,
        "missing_unicode_sequence": max(0, 90 - filled_seq),
        "missing_verified": max(0, 90 - verified),
        "complete": complete,
        "errors": errors[:20],
    }


@app.get("/api/table6_strict/status")
async def table6_strict_status_api():
    return _Table6JSONResponse(_table6_status())


@app.get("/table6_strict_import", response_class=_Table6HTMLResponse)
async def table6_strict_import_page():
    st = _table6_status()

    html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>GB/T 25914 表6严格强制性合体字导入</title>
<style>
body {{
  margin:0;
  padding:24px;
  font-family:Arial,"Microsoft YaHei",sans-serif;
  background:#f5f6f8;
  color:#111;
}}
h1 {{font-size:30px;margin:0 0 16px 0;}}
.card {{
  background:#fff;
  border:1px solid #ddd;
  border-radius:14px;
  padding:18px;
  margin-bottom:18px;
}}
pre {{
  background:#f7f7f7;
  border:1px solid #ddd;
  border-radius:8px;
  padding:12px;
  white-space:pre-wrap;
}}
button {{
  background:#111;
  color:#fff;
  border:1px solid #111;
  border-radius:8px;
  padding:10px 16px;
  font-weight:700;
  cursor:pointer;
}}
a {{color:#111;font-weight:700;}}
.warn {{color:#b00020;font-weight:700;}}
.ok {{color:#0a7a26;font-weight:700;}}
</style>
</head>
<body>
<h1>GB/T 25914 表6严格强制性合体字导入</h1>

<div class="card">
  <h2>当前状态</h2>
  <pre id="statusBox">{st}</pre>
  <p class="warn">如果 unicode_sequence 和 verified 没有达到 90 / 90，预览页中强制性合体字就会显示为 0。</p>
</div>

<div class="card">
  <h2>上传表6严格 CSV</h2>
  <p>CSV 必须包含以下列：</p>
  <pre>gb_code,unicode_sequence,standard_name,verified,note</pre>
  <p>示例：</p>
  <pre>0100,U+1820 U+1821,标准中的合体字名称,1,
0101,U+1820 U+1822,标准中的合体字名称,1,</pre>

  <input id="csvFile" type="file" accept=".csv">
  <button onclick="uploadCsv()">上传并替换表6严格数据</button>
  <pre id="uploadResult">等待上传...</pre>
</div>

<div class="card">
  <h2>严格校验与重新生成</h2>
  <button onclick="validateOnly()">校验表6严格数据</button>
  <button onclick="regen('oyun')">重新生成奥云严格版</button>
  <button onclick="regen('menk')">重新生成蒙科立严格版</button>
  <button onclick="regen('all')">重新生成全部</button>
  <pre id="runResult">等待操作...</pre>
</div>

<div class="card">
  <h2>预览入口</h2>
  <p><a href="/oyun_gb_preview" target="_blank">奥云｜中国国标版 TTF 预览</a></p>
  <p><a href="/menk_gb_preview" target="_blank">蒙科立｜中国国标版 TTF 预览</a></p>
  <p><a href="/" target="_blank">返回首页</a></p>
</div>

<script>
async function refreshStatus(){{
  const res = await fetch('/api/table6_strict/status');
  const data = await res.json();
  document.getElementById('statusBox').textContent = JSON.stringify(data, null, 2);
}}

async function uploadCsv(){{
  const f = document.getElementById('csvFile').files[0];
  const out = document.getElementById('uploadResult');
  if(!f){{
    out.textContent = '请先选择 CSV 文件。';
    return;
  }}
  const fd = new FormData();
  fd.append('file', f);
  out.textContent = '正在上传...';
  const res = await fetch('/api/table6_strict/upload', {{method:'POST', body: fd}});
  const data = await res.json();
  out.textContent = JSON.stringify(data, null, 2);
  refreshStatus();
}}

async function validateOnly(){{
  const out = document.getElementById('runResult');
  out.textContent = '正在校验...';
  const res = await fetch('/api/table6_strict/validate', {{method:'POST'}});
  const data = await res.json();
  out.textContent = JSON.stringify(data, null, 2);
  refreshStatus();
}}

async function regen(foundry){{
  const out = document.getElementById('runResult');
  out.textContent = '正在重新生成：' + foundry;
  const res = await fetch('/api/table6_strict/regenerate/' + foundry, {{method:'POST'}});
  const data = await res.json();
  out.textContent = JSON.stringify(data, null, 2);
  refreshStatus();
}}

refreshStatus();
</script>
</body>
</html>
"""
    return _Table6HTMLResponse(html)


@app.post("/api/table6_strict/upload")
async def table6_strict_upload_api(request: _Table6Request):
    try:
        form = await request.form()
        upload = None

        for key, value in form.multi_items():
            if hasattr(value, "filename") and value.filename:
                upload = value
                break

        if upload is None:
            return _Table6JSONResponse({"ok": False, "error": "没有收到 CSV 文件。"}, status_code=400)

        data = await upload.read()
        if not data:
            return _Table6JSONResponse({"ok": False, "error": "CSV 文件为空。"}, status_code=400)

        text = data.decode("utf-8-sig", errors="ignore")
        lines = text.splitlines()
        reader = _table6_csv.DictReader(lines)
        rows = list(reader)

        required = ["gb_code", "unicode_sequence", "standard_name", "verified", "note"]
        missing = [x for x in required if x not in (reader.fieldnames or [])]

        if missing:
            return _Table6JSONResponse({
                "ok": False,
                "error": "CSV 缺少必要列。",
                "missing_columns": missing,
                "required_columns": required
            }, status_code=400)

        _TABLE6_PATH.parent.mkdir(parents=True, exist_ok=True)

        if _TABLE6_PATH.exists():
            bak = _TABLE6_PATH.with_suffix(".csv.bak_" + str(int(_table6_time.time())))
            _table6_shutil.copy2(_TABLE6_PATH, bak)

        with _TABLE6_PATH.open("w", encoding="utf-8-sig", newline="") as f:
            writer = _table6_csv.DictWriter(f, fieldnames=required)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k, "") for k in required})

        return _Table6JSONResponse({
            "ok": True,
            "message": "已上传并替换表6严格 CSV。",
            "status": _table6_status()
        })

    except Exception as e:
        return _Table6JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/table6_strict/validate")
async def table6_strict_validate_api():
    try:
        r = _table6_subprocess.run(
            [_table6_sys.executable, "scripts/validate_gbt25914_table6_strict.py"],
            cwd=str(_Table6Path.cwd()),
            capture_output=True,
            text=True,
            timeout=120
        )

        return _Table6JSONResponse({
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout": r.stdout[-6000:],
            "stderr": r.stderr[-6000:],
            "status": _table6_status()
        })

    except Exception as e:
        return _Table6JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _table6_runtime_counts(path: _Table6Path):
    if not path.exists():
        return {}

    counts = {}
    total = 0

    with path.open("r", encoding="utf-8-sig") as f:
        reader = _table6_csv.DictReader(f)
        for row in reader:
            total += 1
            g = row.get("display_group", "")
            counts[g] = counts.get(g, 0) + 1

    counts["_TOTAL"] = total
    return counts


@app.post("/api/table6_strict/regenerate/{foundry}")
async def table6_strict_regenerate_api(foundry: str):
    foundry = foundry.lower().strip()

    st = _table6_status()
    if not st["complete"]:
        return _Table6JSONResponse({
            "ok": False,
            "error": "表6严格数据未完成，不能生成含强制性合体字的严格版。",
            "status": st
        }, status_code=400)

    tasks = []

    if foundry in ["oyun", "all"]:
        tasks.append({
            "name": "奥云",
            "cmd": [_table6_sys.executable, "scripts/build_oyun_gb_version.py"],
            "runtime": _Table6Path("output/oyun_gb_ttf_steps/oyun_gb_runtime.csv"),
        })

    if foundry in ["menk", "all"]:
        tasks.append({
            "name": "蒙科立",
            "cmd": [_table6_sys.executable, "scripts/build_menk_gb_version.py"],
            "runtime": _Table6Path("output/menk_gb_ttf_steps/menk_gb_runtime.csv"),
        })

    if not tasks:
        return _Table6JSONResponse({"ok": False, "error": "foundry 只能是 oyun / menk / all。"}, status_code=400)

    results = []

    for task in tasks:
        env = dict(_table6_os.environ)
        env["MGB_STEPS"] = env.get("MGB_STEPS", "20")

        r = _table6_subprocess.run(
            task["cmd"],
            cwd=str(_Table6Path.cwd()),
            env=env,
            capture_output=True,
            text=True,
            timeout=900
        )

        results.append({
            "name": task["name"],
            "returncode": r.returncode,
            "stdout": r.stdout[-5000:],
            "stderr": r.stderr[-5000:],
            "runtime_counts": _table6_runtime_counts(task["runtime"])
        })

        if r.returncode != 0:
            return _Table6JSONResponse({
                "ok": False,
                "failed": task["name"],
                "results": results
            }, status_code=500)

    return _Table6JSONResponse({
        "ok": True,
        "message": "重新生成完成。",
        "results": results
    })


# [REMOVED] TABLE6_PREVIEW_INJECT_JS 已删除：预览页不再显示顶部状态提示框。



# [REMOVED] 预览页顶部 GB/T 25914 状态提示框已禁用。
# 原生成逻辑、奥云/蒙科立规则、可变字体、字体家族功能不受影响。



# =========================================================
# CHARSET_DROPDOWN_CLEAN_V1
# 只清理首页“选择文种 / 字符集”这个下拉框：
# - 中文常用3500字 -> 中文6500字
# - 删除奥云/蒙科立/蒙云在顶部字符集下拉框里的选项
# - 保留一个“传统蒙古文35个”
# 不影响下面“按字体公司规则生成”模块。
# =========================================================
from fastapi.responses import HTMLResponse as _CharsetCleanHTMLResponse

_CHARSET_DROPDOWN_CLEAN_JS = r"""
<script id="charset-dropdown-clean-v1">
(function(){
  function findCharsetSelect(){
    const labels = Array.from(document.querySelectorAll("label,div,span,p,b,strong"));
    for(const el of labels){
      const txt = (el.textContent || "").trim();
      if(txt.includes("选择文种") || txt.includes("字符集")){
        let cur = el;
        for(let i=0;i<8;i++){
          if(!cur) break;
          const sel = cur.parentElement ? cur.parentElement.querySelector("select") : null;
          if(sel) return sel;
          cur = cur.parentElement;
        }
      }
    }

    // 兜底：页面上第一个 select 通常就是“选择文种 / 字符集”
    return document.querySelector("select");
  }

  function normalize(){
    const sel = findCharsetSelect();
    if(!sel || sel.dataset.charsetCleaned === "1") return;

    const originalOptions = Array.from(sel.options);
    let hasMongolian35 = false;

    originalOptions.forEach(opt => {
      const txt = (opt.textContent || "").trim();
      const val = (opt.value || "").trim();

      // 中文3500改为中文6500
      if(txt.includes("中文") && txt.includes("3500")){
        opt.textContent = "中文6500字";
        opt.value = "chinese_6500";
      }

      // 把旧的传统蒙古文基础字母统一显示为“传统蒙古文35个”
      if(
        txt.includes("传统蒙古文基础字母") ||
        txt.includes("U+1820") ||
        txt.includes("U+1842")
      ){
        opt.textContent = "传统蒙古文35个";
        opt.value = "mongolian_basic_35";
        hasMongolian35 = true;
      }

      // 如果旧补丁把顶部字符集改成了奥云/蒙云，就改回传统蒙古文35个
      if(
        txt.includes("奥云") ||
        txt.includes("蒙云") ||
        txt.includes("中国国标版") ||
        val === "oyun_gb"
      ){
        opt.textContent = "传统蒙古文35个";
        opt.value = "mongolian_basic_35";
        hasMongolian35 = true;
      }
    });

    // 删除顶部字符集下拉框里的蒙科立选项；蒙科立只保留在下面“按字体公司规则生成”模块里
    Array.from(sel.options).forEach(opt => {
      const txt = (opt.textContent || "").trim();
      const val = (opt.value || "").trim();

      if(txt.includes("蒙科立") || txt.includes("蒙克立") || val === "menk_gb"){
        opt.remove();
      }
    });

    // 去重：只保留一个“传统蒙古文35个”
    let seenMongolian = false;
    Array.from(sel.options).forEach(opt => {
      const txt = (opt.textContent || "").trim();
      if(txt === "传统蒙古文35个"){
        if(seenMongolian){
          opt.remove();
        }else{
          seenMongolian = true;
        }
      }
    });

    if(!seenMongolian){
      const opt = document.createElement("option");
      opt.textContent = "传统蒙古文35个";
      opt.value = "mongolian_basic_35";
      sel.appendChild(opt);
    }

    sel.dataset.charsetCleaned = "1";
  }

  document.addEventListener("DOMContentLoaded", normalize);
  setTimeout(normalize, 300);
  setTimeout(normalize, 1000);
  setTimeout(normalize, 2000);
  setTimeout(normalize, 3000);
})();
</script>
"""


@app.middleware("http")
async def _charset_dropdown_clean_home(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    # 先做服务端文本清理
    html = html.replace("中文常用的3500字", "中文6500字")
    html = html.replace("中文常用 3500 字", "中文6500字")
    # [DISABLED] html = html.replace("奥云｜中国国标版", "传统蒙古文35个")
    # [DISABLED] html = html.replace("蒙云｜中国国标版", "传统蒙古文35个")
    html = html.replace("传统蒙古文基础字母 U+1820-U+1842", "传统蒙古文35个")

    if "charset-dropdown-clean-v1" not in html:
      if "</body>" in html:
          html = html.replace("</body>", _CHARSET_DROPDOWN_CLEAN_JS + "\n</body>")
      else:
          html += _CHARSET_DROPDOWN_CLEAN_JS

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return _CharsetCleanHTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers
    )


# =========================================================
# RESTORE_FOUNDRY_SELECT_V1
# 修复“按字体公司规则生成”模块：
# - 只恢复下方字体公司选择框
# - 保留奥云和蒙科立
# - 不影响上方“选择文种 / 字符集”
# =========================================================
from fastapi.responses import HTMLResponse as _RestoreFoundryHTMLResponse

_RESTORE_FOUNDRY_SELECT_JS = r"""
<script id="restore-foundry-select-v1">
(function(){
  function findFoundryPanel(){
    const all = Array.from(document.querySelectorAll("div,section,form"));
    for(const el of all){
      const txt = (el.textContent || "");
      if(txt.includes("按字体公司规则生成") && txt.includes("字体公司") && txt.includes("字体文件 A")){
        return el;
      }
    }
    return document.getElementById("foundryRulesPanel");
  }

  function restoreFoundrySelect(){
    const panel = findFoundryPanel();
    if(!panel) return;

    const selects = Array.from(panel.querySelectorAll("select"));
    if(!selects.length) return;

    // 下方模块里的第一个 select 就是“字体公司”
    const sel = selects[0];

    sel.innerHTML = "";

    const opt1 = document.createElement("option");
    opt1.value = "oyun";
    opt1.textContent = "奥云｜中国国标版";
    sel.appendChild(opt1);

    const opt2 = document.createElement("option");
    opt2.value = "menk";
    opt2.textContent = "蒙科立｜中国国标版";
    sel.appendChild(opt2);

    // 保持默认选择奥云
    if(!sel.value || sel.value === "china_gb" || sel.value === "mongolian_basic_35"){
      sel.value = "oyun";
    }

    // 修复说明文字，避免被上方字符集清理逻辑误改
    const textNodes = [];
    const walker = document.createTreeWalker(panel, NodeFilter.SHOW_TEXT);
    let node;
    while(node = walker.nextNode()){
      textNodes.push(node);
    }

    textNodes.forEach(n => {
      n.nodeValue = n.nodeValue
        .replaceAll("中国国标版。", "传统蒙古文中国国标版。")
        .replaceAll("奥云和蒙克立分开处理", "奥云和蒙科立分开处理");
    });
  }

  document.addEventListener("DOMContentLoaded", restoreFoundrySelect);
  setTimeout(restoreFoundrySelect, 300);
  setTimeout(restoreFoundrySelect, 800);
  setTimeout(restoreFoundrySelect, 1500);
  setTimeout(restoreFoundrySelect, 2500);
})();
</script>
"""


@app.middleware("http")
async def _restore_foundry_select_home(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    if "restore-foundry-select-v1" not in html:
        if "</body>" in html:
            html = html.replace("</body>", _RESTORE_FOUNDRY_SELECT_JS + "\n</body>")
        else:
            html += _RESTORE_FOUNDRY_SELECT_JS

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return _RestoreFoundryHTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers
    )


# =========================================================
# FINAL_FIX_FOUNDRY_COMPANY_SELECT_V2
# 最终修复：只修复“按字体公司规则生成”模块里的字体公司下拉框。
# 上方“选择文种/字符集”不受影响。
# =========================================================
from fastapi.responses import HTMLResponse as _FinalFoundrySelectHTMLResponse

_FINAL_FIX_FOUNDRY_COMPANY_SELECT_JS = r"""
<script id="final-fix-foundry-company-select-v2">
(function(){
  function findFoundryPanel(){
    const candidates = Array.from(document.querySelectorAll("div,section,form"))
      .filter(el => {
        const txt = el.textContent || "";
        return txt.includes("按字体公司规则生成")
          && txt.includes("字体公司")
          && txt.includes("字体文件 A")
          && txt.includes("字体文件 B");
      });

    if(!candidates.length) return null;

    // 取文本最短的那个，避免选到 body 或大容器
    candidates.sort((a,b) => (a.textContent || "").length - (b.textContent || "").length);
    return candidates[0];
  }

  function fixFoundrySelect(){
    const panel = findFoundryPanel();
    if(!panel) return;

    const selects = Array.from(panel.querySelectorAll("select"));
    if(!selects.length) return;

    // 这个模块里的第一个 select 就是“字体公司”
    const sel = selects[0];

    let oldValue = sel.value;
    let oldText = "";
    if(sel.selectedIndex >= 0 && sel.options[sel.selectedIndex]){
      oldText = sel.options[sel.selectedIndex].textContent || "";
    }

    let shouldKeepMenk = (
      oldValue === "menk" ||
      oldText.includes("蒙克立") ||
      oldText.includes("蒙科立")
    );

    let shouldKeepOyun = (
      oldValue === "oyun" ||
      oldText.includes("奥云")
    );

    sel.id = "foundryRulesSelect";
    sel.name = "foundry";
    sel.innerHTML = "";

    const optOyun = document.createElement("option");
    optOyun.value = "oyun";
    optOyun.textContent = "奥云｜中国国标版";
    sel.appendChild(optOyun);

    const optMenk = document.createElement("option");
    optMenk.value = "menk";
    optMenk.textContent = "蒙科立｜中国国标版";
    sel.appendChild(optMenk);

    if(shouldKeepMenk){
      sel.value = "menk";
    }else if(shouldKeepOyun){
      sel.value = "oyun";
    }else{
      sel.value = "oyun";
    }

    // 修正模块说明文字，避免出现只有“中国国标版”的误导
    const nodes = [];
    const walker = document.createTreeWalker(panel, NodeFilter.SHOW_TEXT);
    let node;
    while(node = walker.nextNode()){
      nodes.push(node);
    }

    nodes.forEach(n => {
      n.nodeValue = n.nodeValue
        .replaceAll("中国国标版和中国国标版", "奥云和蒙科立")
        .replaceAll("奥云和蒙克立", "奥云和蒙科立");
    });
  }

  function run(){
    fixFoundrySelect();
  }

  document.addEventListener("DOMContentLoaded", run);
  setTimeout(run, 200);
  setTimeout(run, 600);
  setTimeout(run, 1200);
  setTimeout(run, 2500);

  // 防止旧脚本后续又把它改坏，持续校正几秒
  let count = 0;
  const timer = setInterval(() => {
    run();
    count++;
    if(count > 12) clearInterval(timer);
  }, 500);
})();
</script>
"""


@app.middleware("http")
async def _final_fix_foundry_company_select_home(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    if "final-fix-foundry-company-select-v2" not in html:
        if "</body>" in html:
            html = html.replace("</body>", _FINAL_FIX_FOUNDRY_COMPANY_SELECT_JS + "\n</body>")
        else:
            html += _FINAL_FIX_FOUNDRY_COMPANY_SELECT_JS

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return _FinalFoundrySelectHTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers
    )


# =========================================================
# RESTORE_TOP_CHARSET_SELECT_ONLY_V3
# 只修复页面上半部分“选择文种 / 字符集”下拉框。
# 不修改下半部分“按字体公司规则生成”的奥云/蒙科立选项。
# =========================================================
from fastapi.responses import HTMLResponse as _TopCharsetOnlyHTMLResponse

_RESTORE_TOP_CHARSET_SELECT_ONLY_JS = r"""
<script id="restore-top-charset-select-only-v3">
(function(){
  function findTopCharsetSelect(){
    // 只找“选择文种 / 字符集”这个标签附近的 select
    const all = Array.from(document.querySelectorAll("label,div,p,span,b,strong"));
    for(const el of all){
      const txt = (el.textContent || "").trim();
      if(txt.includes("选择文种") && txt.includes("字符集")){
        let box = el;
        for(let i=0;i<8;i++){
          if(!box) break;

          // 不能进入下方字体公司模块
          const panelText = box.textContent || "";
          if(panelText.includes("按字体公司规则生成")) break;

          const sel = box.parentElement ? box.parentElement.querySelector("select") : null;
          if(sel) return sel;

          box = box.parentElement;
        }
      }
    }

    // 兜底：页面第一个 select 通常是上半部分字符集
    const selects = Array.from(document.querySelectorAll("select"));
    for(const sel of selects){
      const parentText = sel.closest("div,form,section") ? sel.closest("div,form,section").textContent || "" : "";
      if(parentText.includes("按字体公司规则生成")) continue;
      return sel;
    }

    return null;
  }

  function getExistingValues(sel){
    const map = {};

    Array.from(sel.options).forEach(opt => {
      const t = (opt.textContent || "").trim();
      const v = opt.value || "";

      if((t.includes("英文") || t.toLowerCase().includes("english") || (t.includes("A-Z") && !t.includes("德文"))) && !t.toLowerCase().includes("german")){
        map.english = v;
      }
      if(t.includes("蒙古") || t.includes("U+1820") || t.includes("传统蒙古文")){
        map.mongolian35 = v;
      }
      if(t.includes("中文") || t.includes("汉字") || t.includes("3500") || t.includes("6500")){
        map.chinese = v;
      }
      if(t.includes("日文") || t.includes("假名") || t.toLowerCase().includes("japanese")){
        map.japanese = v;
      }
      if(t.includes("韩文") || t.includes("Jamo") || t.toLowerCase().includes("korean")){
        map.korean = v;
      }
      if(t.includes("俄文") || t.includes("西里尔") || t.toLowerCase().includes("russian")){
        map.russian = v;
      }
      if(t.includes("德文") || t.includes("ä") || t.includes("ö") || t.includes("ß") || t.toLowerCase().includes("german")){
        map.german = v;
      }
      if(t.includes("自定义") || t.toLowerCase().includes("custom")){
        map.custom = v;
      }
    });

    return map;
  }

  function option(value, text){
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = text;
    return opt;
  }

  function restoreTopCharset(){
    const sel = findTopCharsetSelect();
    if(!sel) return;

    // 防止误伤下方字体公司模块
    const around = sel.closest("div,form,section");
    const aroundText = around ? around.textContent || "" : "";
    if(aroundText.includes("按字体公司规则生成") && aroundText.includes("字体公司")) return;

    const old = getExistingValues(sel);
    const currentValue = sel.value || "";
    const currentText = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].textContent || "" : "";

    sel.innerHTML = "";

    sel.appendChild(option(old.english || "english_basic", "英文 A-Z / a-z"));
    sel.appendChild(option(old.mongolian35 || "mongolian_basic_35", "传统蒙古文35个"));
    sel.appendChild(option(old.chinese || "chinese_6500", "中文6500字"));
    sel.appendChild(option(old.japanese || "japanese_kana", "日文假名：平假名 + 片假名"));
    sel.appendChild(option(old.korean || "korean_basic", "韩文基础：Jamo + 部分常用音节"));
    sel.appendChild(option(old.russian || "russian_basic", "俄文基础：西里尔字母"));
    sel.appendChild(option(old.german || "german_basic", "德文基础：äöüß + 德语字母"));
    sel.appendChild(option(old.custom || "custom", "自定义输入字符"));

    let nextValue = currentValue;

    if(currentText.includes("英文") || currentText.toLowerCase().includes("english") || (currentText.includes("A-Z") && !currentText.includes("德文"))){
      nextValue = old.english || "english_basic";
    }
    if(currentText.includes("中文") || currentText.includes("汉字") || currentText.includes("3500") || currentText.includes("6500")){
      nextValue = old.chinese || "chinese_6500";
    }
    if(currentText.includes("日文") || currentText.includes("假名")){
      nextValue = old.japanese || "japanese_kana";
    }
    if(currentText.includes("韩文") || currentText.includes("Jamo")){
      nextValue = old.korean || "korean_basic";
    }
    if(currentText.includes("俄文") || currentText.includes("西里尔")){
      nextValue = old.russian || "russian_basic";
    }
    if(currentText.includes("德文") || currentText.includes("ä") || currentText.includes("ö") || currentText.includes("ß")){
      nextValue = old.german || "german_basic";
    }
    if(currentText.includes("自定义")){
      nextValue = old.custom || "custom";
    }

    // 如果之前选的是蒙古文/奥云国标，恢复为传统蒙古文35个
    if(
      currentText.includes("奥云") ||
      currentText.includes("蒙云") ||
      currentText.includes("中国国标") ||
      currentText.includes("传统蒙古文") ||
      currentText.includes("蒙古")
    ){
      nextValue = old.mongolian35 || "mongolian_basic_35";
    }

    if(Array.from(sel.options).some(opt => opt.value === nextValue)){
      sel.value = nextValue;
    }

    sel.dispatchEvent(new Event("change", {bubbles:true}));
  }

  function fixTopHint(){
    const nodes = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;

    while(node = walker.nextNode()){
      const parentText = node.parentElement ? node.parentElement.closest("#foundryRulesPanel, div, section, form")?.textContent || "" : "";
      if(parentText.includes("按字体公司规则生成")) continue;

      if(node.nodeValue.includes("中文 3500 字")){
        node.nodeValue = node.nodeValue.replaceAll("中文 3500 字", "中文6500字");
      }
      if(node.nodeValue.includes("中文3500字")){
        node.nodeValue = node.nodeValue.replaceAll("中文3500字", "中文6500字");
      }
    }
  }

  function run(){
    restoreTopCharset();
    fixTopHint();
  }

  document.addEventListener("DOMContentLoaded", run);
  setTimeout(run, 200);
  setTimeout(run, 600);
  setTimeout(run, 1200);
  setTimeout(run, 2500);

  let count = 0;
  const timer = setInterval(() => {
    run();
    count++;
    if(count > 12) clearInterval(timer);
  }, 500);
})();
</script>
"""


@app.middleware("http")
async def _restore_top_charset_select_only_home(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    # 只改上半部分常见文案，不全局替换奥云/蒙克立
    html = html.replace("中文 3500 字", "中文6500字")
    html = html.replace("中文3500字", "中文6500字")

    if "restore-top-charset-select-only-v3" not in html:
        if "</body>" in html:
            html = html.replace("</body>", _RESTORE_TOP_CHARSET_SELECT_ONLY_JS + "\n</body>")
        else:
            html += _RESTORE_TOP_CHARSET_SELECT_ONLY_JS

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return _TopCharsetOnlyHTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers
    )


# =========================================================
# GENERATION_PROGRESS_AND_LINKS_PANEL_V1
# 给上半部分“中国国标版生成状态”添加：
# 1. 实时生成进度条
# 2. SVG预览 / 字体家族预览 / 可变字体预览
# 3. SVG下载 / 字体家族下载 / 可变字体下载
# 不修改下方“按字体公司规则生成”模块。
# =========================================================
from fastapi.responses import HTMLResponse as _ProgressLinksHTMLResponse

_GENERATION_PROGRESS_AND_LINKS_JS = r"""
<script id="generation-progress-and-links-panel-v1">
(function(){
  let progressTimer = null;
  let progressValue = 0;

  function isInFoundryPanel(el){
    const panel = el.closest ? el.closest("div,section,form") : null;
    if(!panel) return false;
    const t = panel.textContent || "";
    return t.includes("按字体公司规则生成") && t.includes("字体公司");
  }

  function findTopStatusCard(){
    const all = Array.from(document.querySelectorAll("div,section,form"));

    let candidates = all.filter(el => {
      const t = el.textContent || "";
      if(!t.includes("中国国标版生成状态")) return false;
      if(t.includes("按字体公司规则生成") && t.includes("字体公司")) return false;
      return true;
    });

    if(!candidates.length) return null;

    candidates.sort((a,b) => (a.textContent || "").length - (b.textContent || "").length);
    return candidates[0];
  }

  function ensurePanel(){
    const card = findTopStatusCard();
    if(!card) return null;

    let panel = card.querySelector("#topGenerationProgressPanel");
    if(panel) return panel;

    panel = document.createElement("div");
    panel.id = "topGenerationProgressPanel";
    panel.style.marginTop = "12px";
    panel.style.marginBottom = "12px";
    panel.style.padding = "12px";
    panel.style.border = "1px solid #ddd";
    panel.style.borderRadius = "10px";
    panel.style.background = "#fff";

    panel.innerHTML = `
      <div style="font-weight:700;margin-bottom:8px;">实时生成进度</div>
      <div style="height:14px;background:#eee;border-radius:999px;overflow:hidden;border:1px solid #ddd;">
        <div id="topGenerationProgressBar" style="height:100%;width:0%;background:#2563eb;transition:width .35s ease;"></div>
      </div>
      <div id="topGenerationProgressText" style="font-size:12px;color:#333;margin-top:6px;">等待开始生成。</div>

      <div style="font-weight:700;margin-top:14px;margin-bottom:8px;">生成结果</div>
      <div id="topGenerationResultButtons" style="display:flex;flex-wrap:wrap;gap:8px;"></div>
    `;

    const heading = Array.from(card.querySelectorAll("h1,h2,h3,h4,b,strong,div"))
      .find(x => (x.textContent || "").includes("中国国标版生成状态"));

    if(heading && heading.parentNode === card){
      heading.insertAdjacentElement("afterend", panel);
    }else{
      card.insertBefore(panel, card.firstChild.nextSibling || card.firstChild);
    }

    refreshResultButtons();
    return panel;
  }

  function setProgress(value, text, state){
    ensurePanel();
    const bar = document.getElementById("topGenerationProgressBar");
    const txt = document.getElementById("topGenerationProgressText");

    if(!bar || !txt) return;

    progressValue = Math.max(0, Math.min(100, value));
    bar.style.width = progressValue + "%";

    if(state === "ok"){
      bar.style.background = "#16a34a";
    }else if(state === "error"){
      bar.style.background = "#dc2626";
    }else{
      bar.style.background = "#2563eb";
    }

    txt.textContent = text || (progressValue + "%");
  }

  function startProgress(){
    ensurePanel();

    if(progressTimer){
      clearInterval(progressTimer);
      progressTimer = null;
    }

    progressValue = 3;
    setProgress(progressValue, "正在准备字体文件与字符集……", "running");

    progressTimer = setInterval(() => {
      const card = findTopStatusCard();
      const text = card ? (card.textContent || "") : "";

      if(
        text.includes('"ok": true') ||
        text.includes("'ok': true") ||
        text.includes("完成") ||
        text.includes("preview") ||
        text.includes("预览") && text.includes("下载")
      ){
        clearInterval(progressTimer);
        progressTimer = null;
        setProgress(100, "生成完成。下面可以打开预览或下载结果。", "ok");
        refreshResultButtons();
        return;
      }

      if(
        text.includes('"ok": false') ||
        text.includes("Traceback") ||
        text.includes("ERROR") ||
        text.includes("错误") ||
        text.includes("失败")
      ){
        clearInterval(progressTimer);
        progressTimer = null;
        setProgress(progressValue, "生成失败，请查看下方日志。", "error");
        refreshResultButtons();
        return;
      }

      if(progressValue < 35){
        progressValue += 4;
        setProgress(progressValue, "正在解析字体轮廓与字符集……", "running");
      }else if(progressValue < 65){
        progressValue += 2;
        setProgress(progressValue, "正在生成 SVG 与中间变化字形……", "running");
      }else if(progressValue < 88){
        progressValue += 1;
        setProgress(progressValue, "正在生成 TTF 字体家族与可变字体资源……", "running");
      }else if(progressValue < 96){
        progressValue += 0.3;
        setProgress(Math.floor(progressValue), "正在打包文件并生成预览链接……", "running");
      }
    }, 600);
  }

  function parseJsonObjectsFromPage(){
    const result = [];

    const nodes = Array.from(document.querySelectorAll("pre,textarea,code"));
    for(const n of nodes){
      const text = (n.value || n.textContent || "").trim();
      if(!text) continue;
      if(!text.includes("{") || !text.includes("}")) continue;

      try{
        const obj = JSON.parse(text);
        result.push(obj);
      }catch(e){}
    }

    return result;
  }

  function flattenJsonLinks(obj, out=[]){
    if(!obj || typeof obj !== "object") return out;

    for(const [k,v] of Object.entries(obj)){
      if(typeof v === "string"){
        const key = k.toLowerCase();
        if(
          key.includes("url") ||
          key.includes("link") ||
          key.includes("preview") ||
          key.includes("download") ||
          v.startsWith("/") ||
          v.startsWith("http")
        ){
          out.push({key:k, text:k, href:v});
        }
      }else if(Array.isArray(v)){
        v.forEach(x => flattenJsonLinks(x, out));
      }else if(typeof v === "object"){
        flattenJsonLinks(v, out);
      }
    }

    return out;
  }

  function collectLinks(){
    const links = [];

    Array.from(document.querySelectorAll("a[href]")).forEach(a => {
      const href = a.getAttribute("href") || "";
      const text = (a.textContent || "").trim();
      if(!href || href === "#") return;
      links.push({text, href, key:text});
    });

    for(const obj of parseJsonObjectsFromPage()){
      links.push(...flattenJsonLinks(obj));
    }

    // 去重
    const seen = new Set();
    return links.filter(x => {
      const key = x.text + "|" + x.href;
      if(seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function matchLink(includeWords, excludeWords=[]){
    const links = collectLinks();

    for(const l of links){
      const hay = ((l.text || "") + " " + (l.key || "") + " " + (l.href || "")).toLowerCase();

      let ok = true;
      for(const w of includeWords){
        if(!hay.includes(w.toLowerCase())){
          ok = false;
          break;
        }
      }
      if(!ok) continue;

      for(const w of excludeWords){
        if(hay.includes(w.toLowerCase())){
          ok = false;
          break;
        }
      }
      if(ok) return l.href;
    }

    return "";
  }

  function makeButton(label, href, type){
    const a = document.createElement(href ? "a" : "span");
    a.textContent = href ? label : label + "（未生成）";

    a.style.display = "inline-block";
    a.style.padding = "8px 12px";
    a.style.borderRadius = "8px";
    a.style.fontWeight = "700";
    a.style.fontSize = "13px";
    a.style.textDecoration = "none";

    if(href){
      a.href = href;
      a.target = "_blank";
      if(type === "preview"){
        a.style.background = "#2563eb";
        a.style.color = "#fff";
        a.style.border = "1px solid #2563eb";
      }else{
        a.style.background = "#111";
        a.style.color = "#fff";
        a.style.border = "1px solid #111";
      }
    }else{
      a.style.background = "#f1f1f1";
      a.style.color = "#999";
      a.style.border = "1px solid #ddd";
      a.style.cursor = "not-allowed";
    }

    return a;
  }

  function refreshResultButtons(){
    ensurePanel();
    const box = document.getElementById("topGenerationResultButtons");
    if(!box) return;

    const svgPreview =
      matchLink(["svg", "预览"]) ||
      matchLink(["svg", "preview"]);

    const familyPreview =
      matchLink(["字体家族", "预览"]) ||
      matchLink(["ttf", "家族"]) ||
      matchLink(["family", "preview"]);

    const variablePreview =
      matchLink(["可变", "预览"]) ||
      matchLink(["variable", "preview"]) ||
      matchLink(["variable", "font"]);

    const svgDownload =
      matchLink(["下载", "svg"]) ||
      matchLink(["svg", "zip"]) ||
      matchLink(["svg", "download"]);

    const familyDownload =
      matchLink(["下载", "字体家族"]) ||
      matchLink(["下载", "ttf"]) ||
      matchLink(["ttf", "zip"]) ||
      matchLink(["family", "download"]);

    const variableDownload =
      matchLink(["下载", "可变"]) ||
      matchLink(["variable", "download"]) ||
      matchLink(["variable", "zip"]);

    box.innerHTML = "";

    box.appendChild(makeButton("打开 SVG 预览", svgPreview, "preview"));
    box.appendChild(makeButton("打开字体家族预览", familyPreview, "preview"));
    box.appendChild(makeButton("打开可变字体预览", variablePreview, "preview"));

    box.appendChild(makeButton("下载 SVG 文件", svgDownload, "download"));
    box.appendChild(makeButton("下载字体家族 TTF", familyDownload, "download"));
    box.appendChild(makeButton("下载可变字体", variableDownload, "download"));
  }

  function bindStartButtons(){
    Array.from(document.querySelectorAll("button,input[type='submit'],input[type='button']")).forEach(btn => {
      if(btn.dataset.progressBound === "1") return;

      const text = (btn.innerText || btn.value || "").trim();
      if(!text.includes("开始生成")) return;
      if(isInFoundryPanel(btn)) return;

      btn.dataset.progressBound = "1";
      btn.addEventListener("click", () => {
        setTimeout(startProgress, 100);
      }, true);
    });
  }

  function observePage(){
    const obs = new MutationObserver(() => {
      ensurePanel();
      bindStartButtons();
      refreshResultButtons();

      const card = findTopStatusCard();
      if(!card) return;
      const text = card.textContent || "";

      if(text.includes("正在生成") || text.includes("正在处理")){
        if(!progressTimer && progressValue < 100){
          startProgress();
        }
      }

      if(text.includes('"ok": true') || text.includes("完成")){
        if(progressTimer){
          clearInterval(progressTimer);
          progressTimer = null;
        }
        setProgress(100, "生成完成。下面可以打开预览或下载结果。", "ok");
        refreshResultButtons();
      }
    });

    obs.observe(document.body, {childList:true, subtree:true, characterData:true});
  }

  function run(){
    ensurePanel();
    bindStartButtons();
    refreshResultButtons();
    observePage();
  }

  document.addEventListener("DOMContentLoaded", run);
  setTimeout(run, 300);
  setTimeout(run, 1000);
  setTimeout(run, 2000);
})();
</script>
"""


@app.middleware("http")
async def _generation_progress_and_links_panel_home(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    if "generation-progress-and-links-panel-v1" not in html:
        if "</body>" in html:
            html = html.replace("</body>", _GENERATION_PROGRESS_AND_LINKS_JS + "\n</body>")
        else:
            html += _GENERATION_PROGRESS_AND_LINKS_JS

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return _ProgressLinksHTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers
    )

app.include_router(unicode_async_router)


# UNICODE_REAL_BRIDGE_V2_INSTALL
try:
    from unicode_bridge_injector import install_unicode_bridge
    install_unicode_bridge(app)
    print("[OK] UNICODE_REAL_BRIDGE_V2 installed")
except Exception as e:
    print("[ERROR] UNICODE_REAL_BRIDGE_V2 install failed:", e)


# FORCE_FOUNDRY_TOP_V2_INSTALL
try:
    from force_foundry_top import install_force_foundry_top
    install_force_foundry_top(app)
    print("[OK] FORCE_FOUNDRY_TOP_V2 installed")
except Exception as e:
    print("[ERROR] FORCE_FOUNDRY_TOP_V2 install failed:", e)


# COMPACT_TTF_ZIP_DOWNLOAD_V1_INSTALL
try:
    from compact_ttf_zip_download import install_compact_ttf_zip_download
    install_compact_ttf_zip_download(app)
    print("[OK] COMPACT_TTF_ZIP_DOWNLOAD_V1 installed")
except Exception as e:
    print("[ERROR] COMPACT_TTF_ZIP_DOWNLOAD_V1 install failed:", e)


# ADD_REAL_RESULT_PREVIEW_LINK_V1_INSTALL
try:
    from add_real_result_preview_link import install_add_real_result_preview_link
    install_add_real_result_preview_link(app)
    print("[OK] ADD_REAL_RESULT_PREVIEW_LINK_V1 installed")
except Exception as e:
    print("[ERROR] ADD_REAL_RESULT_PREVIEW_LINK_V1 install failed:", e)


# SINGLE_PREVIEW_ENTRY_PATCH_V1_INSTALL
try:
    from single_preview_entry_patch import install_single_preview_entry_patch
    install_single_preview_entry_patch(app)
    print("[OK] SINGLE_PREVIEW_ENTRY_PATCH_V1 installed")
except Exception as e:
    print("[ERROR] SINGLE_PREVIEW_ENTRY_PATCH_V1 install failed:", e)


# REAL_MORPH_AXIS_VARIABLE_FONT_V1_INSTALL
try:
    from real_morph_axis_variable_font import install_real_morph_axis_variable_font
    install_real_morph_axis_variable_font(app)
    print("[OK] REAL_MORPH_AXIS_VARIABLE_FONT_V1 installed")
except Exception as e:
    print("[ERROR] REAL_MORPH_AXIS_VARIABLE_FONT_V1 install failed:", e)


# ================= AUTO_SELECTED_TTF_DOWNLOAD_START =================
# 功能：适配当前 font_morph_web 页面
# 在已有 TTF 预览页面中自动扫描 .ttf 文件名，并增加“勾选下载”功能
# 不改动原来的生成、预览、全部下载功能

from pathlib import Path as _auto_Path
from typing import List as _auto_List
import time as _auto_time
import zipfile as _auto_zipfile
import re as _auto_re
import html as _auto_html

from fastapi import Form as _auto_Form
from fastapi.responses import FileResponse as _auto_FileResponse
from fastapi.responses import HTMLResponse as _auto_HTMLResponse
from fastapi.responses import Response as _auto_Response


def _auto_project_root():
    return _auto_Path(__file__).resolve().parent


def _auto_safe_filename(name: str) -> str:
    """
    只保留文件名，防止路径穿越。
    """
    return _auto_Path(str(name)).name


def _auto_find_font_file(filename: str):
    """
    在当前项目中查找勾选的字体文件。
    适配你的系统：
    - 可能在 outputs/
    - 可能在 runs/
    - 可能在 generated/
    - 可能在 static/
    - 也可能在其他子目录
    搜到多个时，取最近修改的那个。
    """
    name = _auto_safe_filename(filename)

    if not name.lower().endswith((".ttf", ".otf", ".woff", ".woff2")):
        return None

    root = _auto_project_root()

    candidate_roots = [
        root / "runs",
        root / "outputs",
        root / "results",
        root / "generated",
        root / "static",
        root / "downloads",
        root,
    ]

    found = []

    for base in candidate_roots:
        if not base.exists():
            continue

        try:
            for f in base.rglob(name):
                if f.is_file() and f.suffix.lower() in [".ttf", ".otf", ".woff", ".woff2"]:
                    found.append(f)
        except Exception:
            pass

    # 去重
    found = list(dict.fromkeys(found))

    if not found:
        return None

    # 优先选择最新生成的文件
    found.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return found[0]


@app.post("/download_selected_ttf_auto")
async def download_selected_ttf_auto(
    selected_fonts: _auto_List[str] = _auto_Form(default=[]),
):
    """
    前端传入 selected_fonts 文件名列表。
    后端自动在当前项目目录下找到这些字体并打包下载。
    """
    if not selected_fonts:
        return _auto_HTMLResponse("""
        <h2>没有选择任何字体</h2>
        <p style="color:red;">请至少勾选一个变化步骤。</p>
        <p><a href="javascript:history.back()">返回预览页面</a></p>
        """)

    picked = []

    for item in selected_fonts:
        font_path = _auto_find_font_file(item)
        if font_path:
            picked.append(font_path)

    # 去重，保持顺序
    picked = list(dict.fromkeys(picked))

    if not picked:
        selected_text = "<br>".join([_auto_html.escape(str(x)) for x in selected_fonts])
        return _auto_HTMLResponse(f"""
        <h2>下载失败</h2>
        <p style="color:red;">没有在项目目录中找到勾选的字体文件。</p>
        <p>系统尝试查找的文件：</p>
        <div style="background:#f5f5f5;padding:10px;border-radius:8px;">{selected_text}</div>
        <p><a href="javascript:history.back()">返回预览页面</a></p>
        """)

    zip_name = f"selected_ttf_steps_{int(_auto_time.time())}.zip"
    zip_path = _auto_project_root() / zip_name

    with _auto_zipfile.ZipFile(zip_path, "w", _auto_zipfile.ZIP_DEFLATED) as zf:
        for font_path in picked:
            zf.write(font_path, arcname=f"selected_fonts/{font_path.name}")

    return _auto_FileResponse(
        path=str(zip_path),
        filename=zip_name,
        media_type="application/zip",
    )


_AUTO_SELECTED_TTF_JS = r"""
<script>
(function() {
  function extractTTFFilesFromPage() {
    const text = document.body ? document.body.innerText : "";
    const matches = text.match(/[^\s"'<>\/\\]+?\.(?:ttf|otf|woff|woff2)/ig) || [];

    let files = matches.map(x => x.trim());

    // 去掉明显不是文件名的异常内容
    files = files.filter(x => {
      return /\.(ttf|otf|woff|woff2)$/i.test(x)
        && !x.includes("http")
        && x.length < 160;
    });

    // 去重
    files = Array.from(new Set(files));

    // 优先按 step 数字排序
    files.sort((a, b) => {
      const na = parseInt((a.match(/step[_-]?0?(\d+)/i) || a.match(/(\d+)/) || [0, 0])[1]);
      const nb = parseInt((b.match(/step[_-]?0?(\d+)/i) || b.match(/(\d+)/) || [0, 0])[1]);
      return na - nb;
    });

    return files;
  }

  function ensureStyle() {
    if (document.getElementById("auto-selected-ttf-style")) return;

    const style = document.createElement("style");
    style.id = "auto-selected-ttf-style";
    style.textContent = `
      #auto-selected-ttf-panel {
        order: 3;
        position: relative;
        z-index: 2;
        background: #ffffff;
        color: #0f172a;
        border: 1px solid #d8dee8;
        border-radius: 8px;
        box-shadow: 0 12px 28px rgba(15,23,42,.08);
        padding: 16px;
        margin: 2px 0 0;
        font-family: Arial, "Microsoft YaHei", sans-serif;
      }
      #auto-selected-ttf-panel .ast-title {
        font-size: 16px;
        font-weight: 800;
        margin-bottom: 6px;
        color: #0f172a;
      }
      #auto-selected-ttf-panel .ast-desc {
        font-size: 13px;
        color: #64748b;
        line-height: 1.55;
        margin-bottom: 12px;
      }
      #auto-selected-ttf-panel .ast-list {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        max-height: 120px;
        overflow: auto;
        border: 1px solid #e2e8f0;
        background: #f8fafc;
        border-radius: 8px;
        padding: 10px;
      }
      #auto-selected-ttf-panel label {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 13px;
        color: #0f172a;
        white-space: nowrap;
      }
      #auto-selected-ttf-panel input[type="checkbox"] {
        width: auto !important;
        margin: 0 !important;
      }
      #auto-selected-ttf-panel .ast-actions {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin-top: 14px;
      }
      #auto-selected-ttf-panel button {
        width: auto !important;
        min-height: 38px;
        padding: 8px 13px;
        border-radius: 6px;
        border: 1px solid #cbd5e1;
        background: #ffffff;
        color: #0f172a;
        cursor: pointer;
        font-weight: 700;
      }
      #auto-selected-ttf-panel button.ast-download {
        background: #0f172a;
        color: #fff;
        border-color: #0f172a;
      }
      .auto-step-download-mark {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        margin-left: 10px;
        padding: 3px 8px;
        border-radius: 999px;
        border: 1px solid #cbd5e1;
        background: #ffffff;
        font-size: 12px;
        color: #0f172a;
      }
      .auto-step-download-mark input {
        width: auto !important;
        margin: 0 !important;
      }
    `;
    document.head.appendChild(style);
  }

  function submitSelected(files) {
    const picked = files.filter(f => {
      const cb = document.querySelector('#auto-selected-ttf-panel input[data-font="' + CSS.escape(f) + '"]');
      return cb && cb.checked;
    });

    if (!picked.length) {
      alert("请至少勾选一个字体文件。");
      return;
    }

    const form = document.createElement("form");
    form.method = "post";
    form.action = "/download_selected_ttf_auto";
    form.style.display = "none";

    picked.forEach(f => {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "selected_fonts";
      input.value = f;
      form.appendChild(input);
    });

    document.body.appendChild(form);
    form.submit();
  }

  function syncFile(file, checked) {
    document.querySelectorAll('input[data-font="' + CSS.escape(file) + '"]').forEach(cb => {
      cb.checked = checked;
    });
  }

  function addCheckboxAfterVisibleFileNames(files) {
    const elements = Array.from(document.querySelectorAll("h1,h2,h3,h4,b,strong,p,div,span"));

    elements.forEach(el => {
      if (el.dataset.autoStepMarked === "1") return;

      const txt = (el.childNodes.length === 1 ? el.textContent : "").trim();
      if (!txt) return;

      const file = files.find(f => txt.includes(f));
      if (!file) return;

      const mark = document.createElement("label");
      mark.className = "auto-step-download-mark";

      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = true;
      cb.dataset.font = file;
      cb.addEventListener("change", function() {
        syncFile(file, cb.checked);
      });

      const s = document.createElement("span");
      s.textContent = "下载";

      mark.appendChild(cb);
      mark.appendChild(s);

      el.appendChild(mark);
      el.dataset.autoStepMarked = "1";
    });
  }

  function createPanel(files) {
    if (document.getElementById("auto-selected-ttf-panel")) return;

    ensureStyle();

    const panel = document.createElement("div");
    panel.id = "auto-selected-ttf-panel";

    const title = document.createElement("div");
    title.className = "ast-title";
    title.textContent = "选择满意的变化步骤下载";

    const desc = document.createElement("div");
    desc.className = "ast-desc";
    desc.textContent = "系统已从当前预览页识别到生成的 TTF 文件。预览完成后，取消勾选不满意的步骤，只下载效果好的字体文件。";

    const list = document.createElement("div");
    list.className = "ast-list";

    files.forEach((file, index) => {
      const label = document.createElement("label");

      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = true;
      cb.dataset.font = file;
      cb.addEventListener("change", function() {
        syncFile(file, cb.checked);
      });

      const span = document.createElement("span");
      const m = file.match(/step[_-]?0?(\d+)/i);
      span.textContent = m ? ("Step " + String(parseInt(m[1])).padStart(2, "0")) : file;

      label.title = file;
      label.appendChild(cb);
      label.appendChild(span);
      list.appendChild(label);
    });

    const actions = document.createElement("div");
    actions.className = "ast-actions";

    const download = document.createElement("button");
    download.type = "button";
    download.className = "ast-download";
    download.textContent = "下载勾选的字体文件";
    download.onclick = () => submitSelected(files);

    const all = document.createElement("button");
    all.type = "button";
    all.textContent = "全选";
    all.onclick = () => files.forEach(f => syncFile(f, true));

    const none = document.createElement("button");
    none.type = "button";
    none.textContent = "取消全选";
    none.onclick = () => files.forEach(f => syncFile(f, false));

    const invert = document.createElement("button");
    invert.type = "button";
    invert.textContent = "反选";
    invert.onclick = () => {
      files.forEach(f => {
        const cb = document.querySelector('#auto-selected-ttf-panel input[data-font="' + CSS.escape(f) + '"]');
        syncFile(f, cb ? !cb.checked : true);
      });
    };

    actions.appendChild(download);
    actions.appendChild(all);
    actions.appendChild(none);
    actions.appendChild(invert);

    panel.appendChild(title);
    panel.appendChild(desc);
    panel.appendChild(list);
    panel.appendChild(actions);

    const firstCard = document.querySelector(".card") || document.querySelector("main") || document.body;
    const h1 = document.querySelector("h1");

    if (h1 && h1.parentNode) {
      h1.parentNode.insertBefore(panel, h1.nextSibling);
    } else {
      firstCard.insertBefore(panel, firstCard.firstChild);
    }
  }

  function main() {
    const files = extractTTFFilesFromPage();

    // 至少识别到 2 个才显示面板，避免首页误触发
    if (files.length < 2) return;

    createPanel(files);
    addCheckboxAfterVisibleFileNames(files);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", main);
  } else {
    main();
  }
})();
</script>
"""


@app.middleware("http")
async def _auto_selected_ttf_download_injector(request, call_next):
    response = await call_next(request)

    content_type = response.headers.get("content-type", "")

    if (
        request.url.path.startswith("/real_variable_preview/")
        or request.url.path == "/generate_real_variable_font"
        or request.url.path in {"/oyun_gb_preview", "/menk_gb_preview"}
        or request.url.path == "/text_fur"
        or request.url.path == "/text_motion"
        or request.url.path == "/lora_lab"
    ):
        return response

    if "text/html" not in content_type.lower():
        return response

    try:
        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        text = body.decode("utf-8", errors="ignore")

        should_inject = (
            ".ttf" in text.lower()
            or "TTF 预览" in text
            or "字体预览" in text
            or "生成结果" in text
            or "下载" in text and "压缩包" in text
        )

        if should_inject and "auto-selected-ttf-panel" not in text:
            if "</body>" in text:
                text = text.replace("</body>", _AUTO_SELECTED_TTF_JS + "\n</body>")
            else:
                text += _AUTO_SELECTED_TTF_JS

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return _auto_Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )

    except Exception:
        return response

# ================= AUTO_SELECTED_TTF_DOWNLOAD_END =================


# ================= REAL_VARIABLE_FONT_ADDON_START =================
# 真实可变字体生成补丁
# 核心：必须生成含 fvar/gvar 表的真实 Variable Font，否则直接报错。
# 不再只做 CSS 滑杆演示。

from pathlib import Path as _rvf_Path
from typing import List as _rvf_List
import time as _rvf_time
import re as _rvf_re
import html as _rvf_html
import json as _rvf_json
import shutil as _rvf_shutil
import traceback as _rvf_traceback

from fastapi import Form as _rvf_Form
from fastapi.responses import HTMLResponse as _rvf_HTMLResponse
from fastapi.responses import FileResponse as _rvf_FileResponse
from fastapi.responses import Response as _rvf_Response

from fontTools.ttLib import TTFont as _rvf_TTFont
from fontTools.designspaceLib import DesignSpaceDocument as _rvf_DesignSpaceDocument
from fontTools.designspaceLib import AxisDescriptor as _rvf_AxisDescriptor
from fontTools.designspaceLib import SourceDescriptor as _rvf_SourceDescriptor
from fontTools.varLib import build as _rvf_varlib_build
from fontTools.varLib import instancer as _rvf_instancer


class _rvf_DiagnosticError(RuntimeError):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report or {}


def _rvf_project_root():
    return _rvf_Path(__file__).resolve().parent


def _rvf_output_root():
    d = _rvf_project_root() / "real_variable_fonts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rvf_safe_file(name: str) -> str:
    return _rvf_Path(str(name)).name


def _rvf_safe_name(name: str) -> str:
    name = str(name or "RealMorphVariable")
    name = _rvf_re.sub(r"[^0-9A-Za-z_\- ]+", "", name).strip()
    return name[:50] or "RealMorphVariable"


def _rvf_safe_ps(name: str) -> str:
    name = _rvf_re.sub(r"[^0-9A-Za-z_\-]+", "", str(name or "RealMorphVariable"))
    return name[:60] or "RealMorphVariable"


def _rvf_find_font_file(filename: str):
    """
    查找页面里勾选的 step ttf。
    优先复用你已有的 _auto_find_font_file。
    """
    name = _rvf_safe_file(filename)

    if not name.lower().endswith((".ttf", ".otf")):
        return None

    if "_auto_find_font_file" in globals():
        try:
            f = globals()["_auto_find_font_file"](name)
            if f:
                return _rvf_Path(f)
        except Exception:
            pass

    root = _rvf_project_root()
    candidate_roots = [
        root / "runs",
        root / "outputs",
        root / "results",
        root / "generated",
        root / "static",
        root / "downloads",
        root,
    ]

    found = []

    for base in candidate_roots:
        if not base.exists():
            continue
        try:
            for f in base.rglob(name):
                if f.is_file() and f.suffix.lower() in [".ttf", ".otf"]:
                    found.append(f)
        except Exception:
            pass

    found = list(dict.fromkeys(found))

    if not found:
        return None

    found.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return found[0]


def _rvf_get_unicode_cmap(font):
    if "cmap" not in font:
        return {}

    best = None
    best_score = -1

    for table in font["cmap"].tables:
        if not table.isUnicode():
            continue

        score = len(table.cmap)

        if table.platformID == 3:
            score += 1000000

        if score > best_score:
            best = table.cmap
            best_score = score

    return dict(best or {})


def _rvf_set_names(font, family_name):
    if "name" not in font:
        return

    ps = _rvf_safe_ps(family_name + "-VF")
    records = {
        1: family_name,
        2: "Regular",
        4: family_name + " Variable",
        6: ps,
        16: family_name,
        17: "Variable",
    }

    for name_id, value in records.items():
        for platform_id, enc_id, lang_id in [
            (3, 1, 0x409),
            (1, 0, 0),
        ]:
            try:
                font["name"].setName(value, name_id, platform_id, enc_id, lang_id)
            except Exception:
                pass


def _rvf_verify_variable_font(font_path):
    """
    强制验证是否是真正可变字体。
    必须包含 fvar。
    TrueType 轮廓变化必须包含 gvar。
    """
    font = _rvf_TTFont(str(font_path))

    tables = set(font.keys())

    has_fvar = "fvar" in font
    has_gvar = "gvar" in font

    axes = []

    if has_fvar:
        for axis in font["fvar"].axes:
            axes.append({
                "tag": axis.axisTag,
                "min": axis.minValue,
                "default": axis.defaultValue,
                "max": axis.maxValue,
                "nameID": axis.axisNameID,
            })

    # glyf 字体必须有 gvar 才说明字形轮廓真的可变
    if "glyf" in font and not has_gvar:
        return {
            "ok": False,
            "has_fvar": has_fvar,
            "has_gvar": has_gvar,
            "axes": axes,
            "tables": sorted(tables),
            "reason": "该字体有 glyf 表，但没有 gvar 表，所以不是可变轮廓字体。",
        }

    if not has_fvar:
        return {
            "ok": False,
            "has_fvar": has_fvar,
            "has_gvar": has_gvar,
            "axes": axes,
            "tables": sorted(tables),
            "reason": "缺少 fvar 表，不是真正的 Variable Font。",
        }

    if not axes:
        return {
            "ok": False,
            "has_fvar": has_fvar,
            "has_gvar": has_gvar,
            "axes": axes,
            "tables": sorted(tables),
            "reason": "fvar 中没有任何变化轴。",
        }

    return {
        "ok": True,
        "has_fvar": has_fvar,
        "has_gvar": has_gvar,
        "axes": axes,
        "tables": sorted(tables),
        "reason": "已检测到真实可变字体表。",
    }


def _rvf_collect_preview_chars(font_path, max_items=120):
    font = _rvf_TTFont(str(font_path), lazy=True)
    cmap = _rvf_get_unicode_cmap(font)
    cps = sorted(cmap.keys())

    mongolian_letters = [cp for cp in cps if 0x1820 <= cp <= 0x1842]
    if mongolian_letters:
        return mongolian_letters[:max_items]

    mongolian_all = [cp for cp in cps if 0x1800 <= cp <= 0x18AF]
    if mongolian_all:
        return mongolian_all[:max_items]

    visible = [
        cp for cp in cps
        if 0x20 <= cp <= 0xFFFF
        and not (0xD800 <= cp <= 0xDFFF)
    ]

    return visible[:max_items]


def _rvf_effective_variation_report(font_path, preview_cps=None, max_checks=24):
    try:
        font = _rvf_TTFont(str(font_path))

        if "fvar" not in font or "gvar" not in font:
            return {
                "ok": False,
                "checked_glyphs": 0,
                "moving_glyphs": 0,
                "reason": "缺少 fvar 或 gvar 表。",
            }

        axes = font["fvar"].axes
        if not axes:
            return {
                "ok": False,
                "checked_glyphs": 0,
                "moving_glyphs": 0,
                "reason": "fvar 中没有变化轴。",
            }

        axis = axes[0]
        tag = axis.axisTag
        min_value = axis.minValue
        max_value = axis.maxValue
        cps = list(preview_cps or _rvf_collect_preview_chars(font_path, max_checks))[:max_checks]

        cmap = _rvf_get_unicode_cmap(font)
        checked = 0
        moving = []

        for cp in cps:
            glyph_name = cmap.get(cp)
            if not glyph_name:
                continue

            checked += 1
            hashes = []

            for value in [min_value, max_value]:
                instance = _rvf_instancer.instantiateVariableFont(font, {tag: value}, inplace=False)
                glyph = instance["glyf"].get(glyph_name)

                if glyph is None:
                    hashes.append("missing")
                    continue

                if glyph.isComposite():
                    glyph.expand(instance["glyf"])

                coords = getattr(glyph, "coordinates", None)
                hashes.append(repr(list(coords or [])))

            if len(set(hashes)) > 1:
                moving.append(f"U+{cp:04X}")

        return {
            "ok": bool(moving),
            "axis": tag,
            "checked_glyphs": checked,
            "moving_glyphs": len(moving),
            "examples": moving[:8],
            "reason": "检测到实例化后的字形坐标变化。" if moving else "已生成 fvar/gvar，但实例化后预览字符坐标没有变化。",
        }

    except Exception as e:
        return {
            "ok": False,
            "checked_glyphs": 0,
            "moving_glyphs": 0,
            "reason": str(e),
        }


def _rvf_remap_unicode_to_moving_glyphs(font):
    if "cmap" not in font or "gvar" not in font:
        return {
            "count": 0,
            "examples": [],
            "reason": "缺少 cmap 或 gvar，无法重映射 Unicode。"
        }

    glyph_order = set(font.getGlyphOrder())
    gvar_variations = getattr(font["gvar"], "variations", {}) or {}
    moving_by_cp = {}
    code_re = _rvf_re.compile(r"(?:^|_)U([0-9A-Fa-f]{4,6})(?:$|[^0-9A-Fa-f])")

    for glyph_name, variations in gvar_variations.items():
        if not variations or glyph_name not in glyph_order:
            continue
        m = code_re.search(glyph_name)
        if not m:
            continue
        cp = int(m.group(1), 16)
        if not (0 <= cp <= 0x10FFFF):
            continue
        current = moving_by_cp.get(cp)
        if current is None:
            moving_by_cp[cp] = glyph_name
            continue
        current_is_core = "_CORE_" in current.upper()
        glyph_is_core = "_CORE_" in glyph_name.upper()
        if glyph_is_core and not current_is_core:
            moving_by_cp[cp] = glyph_name

    if not moving_by_cp:
        return {
            "count": 0,
            "examples": [],
            "reason": "gvar 中没有可按 Unicode 代码点识别的变化 glyph。"
        }

    remapped = []
    for table in font["cmap"].tables:
        if not table.isUnicode():
            continue
        for cp in list(table.cmap.keys()):
            moving_glyph = moving_by_cp.get(cp)
            if moving_glyph and table.cmap.get(cp) != moving_glyph:
                table.cmap[cp] = moving_glyph
                remapped.append((cp, moving_glyph))

    unique = []
    seen = set()
    for cp, glyph_name in remapped:
        key = (cp, glyph_name)
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)

    return {
        "count": len(unique),
        "examples": [f"U+{cp:04X} -> {glyph_name}" for cp, glyph_name in unique[:20]],
        "reason": "已将 Unicode cmap 指向带 gvar delta 的 glyph。" if unique else "cmap 已经指向变化 glyph，或没有需要重映射的项。"
    }


def _rvf_glyph_master_signature(font, glyph_name):
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    coords, end_pts, flags = glyph.getCoordinates(glyf)

    component_signature = ()
    if glyph.isComposite():
        component_signature = tuple(
            (
                getattr(component, "glyphName", ""),
                tuple(round(float(x), 6) for x in getattr(component, "transform", ())),
                round(float(getattr(component, "x", 0)), 6),
                round(float(getattr(component, "y", 0)), 6),
            )
            for component in getattr(glyph, "components", [])
        )

    structure = {
        "is_composite": bool(glyph.isComposite()),
        "component_signature": component_signature,
        "contour_count": len(end_pts),
        "point_count": len(coords),
        "end_pts": tuple(int(x) for x in end_pts),
        "on_curve": tuple(int(flag) & 1 for flag in flags),
    }
    coord_key = tuple((round(float(x), 3), round(float(y), 3)) for x, y in coords)
    return structure, coord_key


def _rvf_diagnose_master_compatibility(font_paths, max_examples=50):
    report = {
        "ok": False,
        "master_count": len(font_paths),
        "masters": [_rvf_Path(p).name for p in font_paths],
        "glyphs_checked": 0,
        "incompatible_glyphs": 0,
        "moving_glyphs": 0,
        "examples": [],
        "reason": "",
    }

    if len(font_paths) < 2:
        report["reason"] = "至少需要 2 个 Step TTF 作为 master。"
        return report

    fonts = []
    try:
        for path in font_paths:
            font = _rvf_TTFont(str(path))
            if "glyf" not in font:
                report["reason"] = f"{_rvf_Path(path).name} 不是 glyf TrueType 轮廓字体。"
                return report
            fonts.append(font)

        base_order = fonts[0].getGlyphOrder()
        for index, font in enumerate(fonts[1:], start=2):
            order = font.getGlyphOrder()
            if order != base_order:
                report["reason"] = "master glyphOrder 不一致。"
                report["examples"].append({
                    "master": _rvf_Path(font_paths[index - 1]).name,
                    "issue": "glyphOrder mismatch",
                    "base_glyph_count": len(base_order),
                    "this_glyph_count": len(order),
                })
                return report

        glyph_names = [name for name in base_order if name != ".notdef"]

        for glyph_name in glyph_names:
            signatures = []
            coord_keys = []
            error = ""

            for font_index, font in enumerate(fonts):
                try:
                    structure, coords = _rvf_glyph_master_signature(font, glyph_name)
                    signatures.append(structure)
                    coord_keys.append(coords)
                except Exception as e:
                    error = f"{_rvf_Path(font_paths[font_index]).name}: {e}"
                    break

            report["glyphs_checked"] += 1

            if error or any(sig != signatures[0] for sig in signatures[1:]):
                report["incompatible_glyphs"] += 1
                if len(report["examples"]) < max_examples:
                    example = {
                        "glyph": glyph_name,
                        "issue": error or "structure mismatch",
                    }
                    if signatures:
                        example["base"] = {
                            "contours": signatures[0]["contour_count"],
                            "points": signatures[0]["point_count"],
                            "is_composite": signatures[0]["is_composite"],
                        }
                        for idx, sig in enumerate(signatures[1:], start=2):
                            if sig != signatures[0]:
                                example["different_master"] = _rvf_Path(font_paths[idx - 1]).name
                                example["different"] = {
                                    "contours": sig["contour_count"],
                                    "points": sig["point_count"],
                                    "is_composite": sig["is_composite"],
                                }
                                break
                    report["examples"].append(example)
                continue

            if len(set(coord_keys)) > 1:
                report["moving_glyphs"] += 1

        if report["incompatible_glyphs"]:
            report["reason"] = (
                f"发现 {report['incompatible_glyphs']} 个 glyph 的轮廓/点序/on-curve 结构不兼容，"
                "不能生成真实可变字体。"
            )
            return report

        if report["moving_glyphs"] == 0:
            report["reason"] = "所有 master 的 glyph 坐标都相同，没有可变轴可以表达的变化。"
            return report

        report["ok"] = True
        report["reason"] = (
            f"master 结构兼容；检查 {report['glyphs_checked']} 个 glyph，"
            f"其中 {report['moving_glyphs']} 个 glyph 存在坐标变化。"
        )
        return report

    finally:
        for font in fonts:
            try:
                font.close()
            except Exception:
                pass


def _rvf_build_real_variable_font(selected_fonts, variable_name):
    font_paths = []

    for item in selected_fonts:
        fp = _rvf_find_font_file(item)
        if fp and fp.exists():
            # 不允许把之前已经生成的可变字体再作为 master
            if "variable" in fp.name.lower() or "vf_" in str(fp.parent).lower():
                continue
            font_paths.append(fp)

    font_paths = list(dict.fromkeys(font_paths))

    if len(font_paths) < 2:
        raise RuntimeError("真实可变字体至少需要 2 个 Step TTF 作为 master。")

    master_compatibility = _rvf_diagnose_master_compatibility(font_paths)
    if not master_compatibility.get("ok"):
        raise _rvf_DiagnosticError(
            "当前结果不能生成真实 VF：Step master 的 glyph 结构不兼容，或没有可表达的坐标变化。",
            {"master_compatibility": master_compatibility},
        )

    # 检查所有 master 是 TrueType glyf 字体
    for fp in font_paths:
        f = _rvf_TTFont(str(fp), lazy=True)
        if "glyf" not in f:
            raise RuntimeError(f"{fp.name} 不是 glyf TrueType 轮廓字体，当前补丁暂不支持 CFF OTF 作为可变字体 master。")

    family_name = _rvf_safe_name(variable_name or "Real Morph Variable")
    timestamp = _rvf_time.strftime("%Y%m%d_%H%M%S")

    out_dir = _rvf_output_root() / f"rvf_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    master_dir = out_dir / "masters"
    master_dir.mkdir(parents=True, exist_ok=True)

    masters = []

    for i, src in enumerate(font_paths, start=1):
        dst = master_dir / f"master_{i:02d}.ttf"
        _rvf_shutil.copy2(src, dst)
        masters.append(dst)

    designspace_path = out_dir / "real_morph.designspace"
    output_name = f"{_rvf_safe_ps(family_name)}_{timestamp}.ttf"
    output_path = out_dir / output_name

    doc = _rvf_DesignSpaceDocument()

    axis = _rvf_AxisDescriptor()
    axis.name = "Morph"
    axis.tag = "MORF"
    axis.minimum = 0
    axis.maximum = 1000
    axis.default = 0
    axis.labelNames["en"] = "Morph"
    axis.labelNames["zh-Hans"] = "字形变化"
    doc.addAxis(axis)

    count = len(masters)

    for i, master_path in enumerate(masters):
        loc = 0 if count == 1 else round(1000 * i / (count - 1), 6)

        source = _rvf_SourceDescriptor()
        source.path = str(master_path.resolve())
        source.name = f"master_{i + 1:02d}"
        source.familyName = family_name
        source.styleName = f"Morph{int(loc)}"
        source.location = {"Morph": loc}

        if i == 0:
            source.copyInfo = True
            source.copyFeatures = True
            source.copyLib = True
            source.copyGroups = True

        doc.addSource(source)

    doc.write(str(designspace_path))

    built = _rvf_varlib_build(str(designspace_path))

    if isinstance(built, tuple):
        varfont = built[0]
    else:
        varfont = built

    cmap_remap = _rvf_remap_unicode_to_moving_glyphs(varfont)
    _rvf_set_names(varfont, family_name)
    varfont.save(str(output_path))

    verify = _rvf_verify_variable_font(output_path)

    if not verify["ok"]:
        raise RuntimeError("生成失败：输出文件不是真正可变字体。原因：" + verify["reason"])

    # 再实际实例化一次，验证轴确实可用
    try:
        test_font = _rvf_TTFont(str(output_path))
        _rvf_instancer.instantiateVariableFont(test_font, {"MORF": 500}, inplace=False)
    except Exception as e:
        raise RuntimeError("生成的字体虽然有 fvar/gvar，但无法按 MORF=500 实例化：" + str(e))

    preview_cps = _rvf_collect_preview_chars(output_path)
    effective_variation = _rvf_effective_variation_report(output_path, preview_cps)

    report = {
        "real_variable_font": True,
        "font_file": output_name,
        "family_name": family_name,
        "master_count": len(masters),
        "axis": {
            "tag": "MORF",
            "name": "Morph",
            "min": 0,
            "default": 0,
            "max": 1000,
        },
        "verify": verify,
        "master_compatibility": master_compatibility,
        "cmap_remap": cmap_remap,
        "effective_variation": effective_variation,
        "masters": [p.name for p in masters],
        "source_files": [p.name for p in font_paths],
    }

    (out_dir / "real_variable_font_report.json").write_text(
        _rvf_json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not effective_variation.get("ok"):
        raise _rvf_DiagnosticError(
            "当前结果不能生成真实 VF：输出文件实例化后没有检测到 glyph 坐标变化。",
            report,
        )

    return {
        "out_dir": out_dir,
        "font_path": output_path,
        "font_name": output_name,
        "family_name": family_name,
        "preview_cps": preview_cps,
        "report": report,
    }


@app.get("/real_variable_font_file/{dir_name}/{font_name}")
async def real_variable_font_file(dir_name: str, font_name: str):
    safe_dir = _rvf_re.sub(r"[^0-9A-Za-z_\-]", "", dir_name)
    safe_font = _rvf_safe_file(font_name)

    fp = _rvf_output_root() / safe_dir / safe_font

    if not fp.exists():
        return _rvf_HTMLResponse("<h2>找不到真实可变字体文件</h2>", status_code=404)

    return _rvf_FileResponse(
        path=str(fp),
        filename=safe_font,
        media_type="font/ttf",
    )


@app.get("/real_variable_master_font/{dir_name}/{font_name}")
async def real_variable_master_font(dir_name: str, font_name: str):
    safe_dir = _rvf_re.sub(r"[^0-9A-Za-z_\-]", "", dir_name)
    safe_font = _rvf_safe_file(font_name)

    fp = _rvf_output_root() / safe_dir / "masters" / safe_font

    if not fp.exists():
        return _rvf_HTMLResponse("<h2>找不到 master 字体文件</h2>", status_code=404)

    return _rvf_FileResponse(
        path=str(fp),
        filename=safe_font,
        media_type="font/ttf",
    )


@app.get("/download_real_variable_font/{dir_name}/{font_name}")
async def download_real_variable_font(dir_name: str, font_name: str):
    safe_dir = _rvf_re.sub(r"[^0-9A-Za-z_\-]", "", dir_name)
    safe_font = _rvf_safe_file(font_name)

    fp = _rvf_output_root() / safe_dir / safe_font

    if not fp.exists():
        return _rvf_HTMLResponse("<h2>找不到真实可变字体文件</h2>", status_code=404)

    return _rvf_FileResponse(
        path=str(fp),
        filename=safe_font,
        media_type="font/ttf",
    )


def _rvf_render_preview(result):
    out_dir = result["out_dir"]
    dir_name = out_dir.name
    font_name = result["font_name"]
    family_name = result["family_name"]
    cps = result["preview_cps"]
    report = result["report"]
    verify = report["verify"]

    first_cp = cps[0] if cps else 0x1820
    first_char = chr(first_cp)

    char_buttons = []

    for cp in cps[:120]:
        ch = chr(cp)
        char_buttons.append(f"""
<button type="button" class="rvf-char-btn" data-char="{_rvf_html.escape(ch)}" data-cp="U+{cp:04X}">
  <span class="rvf-char">{_rvf_html.escape(ch)}</span>
  <span class="rvf-code">U+{cp:04X}</span>
</button>
""")

    return _rvf_HTMLResponse(f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>真实可变字体预览</title>
<style>
@font-face {{
    font-family: "RealMorphVF";
    src: url("/real_variable_font_file/{dir_name}/{_rvf_html.escape(font_name)}") format("truetype");
}}

body {{
    margin: 0;
    background: #f5f6f8;
    color: #111;
    font-family: Arial, "Microsoft YaHei", sans-serif;
}}

.page {{
    max-width: 1220px;
    margin: 24px auto 50px;
    padding: 0 18px;
}}

h1 {{
    font-size: 24px;
    margin: 0 0 8px;
}}

.sub {{
    color: #555;
    font-size: 14px;
    line-height: 1.7;
    margin-bottom: 16px;
}}

.card {{
    background: #fff;
    border: 1px solid #e5e7eb;
    border-radius: 16px;
    padding: 18px;
    margin-bottom: 16px;
    box-shadow: 0 8px 24px rgba(0,0,0,.04);
}}

.verify {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
}}

.verify-item {{
    background: #f8fafc;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 10px;
    font-size: 13px;
}}

.verify-item b {{
    display: block;
    font-size: 18px;
    margin-top: 4px;
}}

.ok {{
    color: #047857;
}}

.bad {{
    color: #b91c1c;
}}

.layout {{
    display: grid;
    grid-template-columns: 250px 1fr;
    gap: 18px;
}}

.sidebar {{
    border: 1px solid #e5e7eb;
    border-radius: 14px;
    padding: 12px;
    max-height: 560px;
    overflow: auto;
}}

.rvf-char-btn {{
    width: 100%;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border: 1px solid #e5e7eb;
    background: #fafafa;
    border-radius: 8px;
    padding: 8px 10px;
    margin-bottom: 6px;
    cursor: pointer;
}}

.rvf-char {{
    font-family: "RealMorphVF", sans-serif;
    font-size: 28px;
    font-variation-settings: "MORF" 0;
}}

.rvf-code {{
    font-size: 12px;
    color: #666;
}}

.meta {{
    font-size: 13px;
    color: #444;
    margin-bottom: 10px;
}}

.preview {{
    height: 320px;
    border: 1px solid #e5e7eb;
    border-radius: 14px;
    background: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
}}

#bigGlyph {{
    font-family: "RealMorphVF", sans-serif;
    font-size: 190px;
    line-height: 1;
    font-variation-settings: "MORF" 0;
}}

.slider-row {{
    display: grid;
    grid-template-columns: 1fr 90px;
    gap: 12px;
    margin-top: 14px;
    align-items: center;
}}

#slider {{
    width: 100%;
}}

#value {{
    font-weight: 800;
    text-align: center;
}}

.actions {{
    margin-top: 12px;
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
}}

.actions button,
.actions a {{
    display: inline-block;
    border: 1px solid #d1d5db;
    border-radius: 10px;
    padding: 9px 14px;
    background: #f3f4f6;
    color: #111;
    text-decoration: none;
    cursor: pointer;
    font-weight: 700;
}}

.actions a.primary {{
    background: #111;
    color: #fff;
    border-color: #111;
}}

.mini-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin-top: 18px;
}}

.mini {{
    border: 1px solid #e5e7eb;
    border-radius: 14px;
    padding: 12px;
    background: #fafafa;
    text-align: center;
}}

.mini-label {{
    font-size: 12px;
    color: #666;
    margin-bottom: 8px;
}}

.mini-glyph {{
    font-family: "RealMorphVF", sans-serif;
    font-size: 82px;
    line-height: 1;
}}

pre {{
    background: #111;
    color: #eee;
    border-radius: 12px;
    padding: 12px;
    overflow: auto;
    font-size: 12px;
}}

@media(max-width:900px) {{
    .layout {{
        grid-template-columns: 1fr;
    }}
    .verify {{
        grid-template-columns: 1fr 1fr;
    }}
    .mini-grid {{
        grid-template-columns: 1fr;
    }}
}}
</style>
</head>
<body>
<div class="page">
  <h1>真实可变字体预览</h1>
  <div class="sub">
    该页面只在后端检测到 <b>fvar</b> 和 <b>gvar</b> 后才会出现。
    当前字体是由已生成的 Step TTF 真实构建出来的 Variable Font。
  </div>

  <div class="card verify">
    <div class="verify-item">是否真实可变字体 <b class="ok">是</b></div>
    <div class="verify-item">fvar 表 <b class="{ "ok" if verify["has_fvar"] else "bad" }">{ "存在" if verify["has_fvar"] else "缺失" }</b></div>
    <div class="verify-item">gvar 表 <b class="{ "ok" if verify["has_gvar"] else "bad" }">{ "存在" if verify["has_gvar"] else "缺失" }</b></div>
    <div class="verify-item">变化轴 <b>MORF 0–1000</b></div>
  </div>

  <div class="card layout">
    <div class="sidebar">
      <b>选择字符</b>
      <div style="height:10px;"></div>
      {"".join(char_buttons)}
    </div>

    <div>
      <div class="meta">
        当前字符：<b id="currentChar">{_rvf_html.escape(first_char)}</b>　
        Unicode：<b id="currentCp">U+{first_cp:04X}</b>　
        Axis：<b>MORF</b>　
        Value：<b id="currentValue">0</b>
      </div>

      <div class="preview">
        <div id="bigGlyph">{_rvf_html.escape(first_char)}</div>
      </div>

      <div class="slider-row">
        <input id="slider" type="range" min="0" max="1000" value="0" step="1">
        <div id="value">0</div>
      </div>

      <div class="actions">
        <button type="button" onclick="setMorph(0)">起始</button>
        <button type="button" onclick="setMorph(500)">中间</button>
        <button type="button" onclick="setMorph(1000)">结束</button>
        <a class="primary" href="/download_real_variable_font/{dir_name}/{_rvf_html.escape(font_name)}">下载真实可变字体 TTF</a>
      </div>

      <div class="mini-grid">
        <div class="mini">
          <div class="mini-label">MORF 0</div>
          <div class="mini-glyph" style="font-variation-settings:'MORF' 0;">{_rvf_html.escape(first_char)}</div>
        </div>
        <div class="mini">
          <div class="mini-label">MORF 500</div>
          <div class="mini-glyph" style="font-variation-settings:'MORF' 500;">{_rvf_html.escape(first_char)}</div>
        </div>
        <div class="mini">
          <div class="mini-label">MORF 1000</div>
          <div class="mini-glyph" style="font-variation-settings:'MORF' 1000;">{_rvf_html.escape(first_char)}</div>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <b>生成结果</b><br>
    字体文件：{_rvf_html.escape(font_name)}<br>
    字体名称：{_rvf_html.escape(family_name)}<br>
    Master 数量：{report["master_count"]}<br>
    变化轴：MORF / 0–1000
  </div>

  <div class="card">
    <b>真实可变字体检测报告</b>
    <pre>{_rvf_html.escape(_rvf_json.dumps(verify, ensure_ascii=False, indent=2))}</pre>
  </div>

  <div class="card">
    <b>参与生成的 Step 字体</b>
    <pre>{_rvf_html.escape(_rvf_json.dumps(report["source_files"], ensure_ascii=False, indent=2))}</pre>
  </div>

  <div class="actions">
    <a href="javascript:history.back()">返回原预览页面</a>
  </div>
</div>

<script>
const slider = document.getElementById("slider");
const value = document.getElementById("value");
const currentValue = document.getElementById("currentValue");
const bigGlyph = document.getElementById("bigGlyph");
const currentChar = document.getElementById("currentChar");
const currentCp = document.getElementById("currentCp");

function applyMorph(v) {{
    bigGlyph.style.fontVariationSettings = '"MORF" ' + v;
    value.textContent = v;
    currentValue.textContent = v;

    document.querySelectorAll(".rvf-char").forEach(el => {{
        el.style.fontVariationSettings = '"MORF" ' + v;
    }});
}}

function setMorph(v) {{
    slider.value = v;
    applyMorph(v);
}}

slider.addEventListener("input", function() {{
    applyMorph(this.value);
}});

document.querySelectorAll(".rvf-char-btn").forEach(btn => {{
    btn.addEventListener("click", function() {{
        const ch = this.dataset.char;
        const cp = this.dataset.cp;

        bigGlyph.textContent = ch;
        currentChar.textContent = ch;
        currentCp.textContent = cp;

        document.querySelectorAll(".mini-glyph").forEach(el => {{
            el.textContent = ch;
        }});
    }});
}});

applyMorph(0);
</script>
</body>
</html>
""")


def _rvf_master_sort_key(path):
    name = _rvf_Path(path).name
    match = _rvf_re.search(r"(\d+)", name)
    return (int(match.group(1)) if match else 999999, name.lower())


def _rvf_master_preview_response(result):
    out_dir = _rvf_Path(result["out_dir"])
    dir_name = out_dir.name
    font_name = result["font_name"]
    family_name = result["family_name"]
    cps = result["preview_cps"]
    report = result["report"]
    verify = report.get("verify", {})
    effective = report.get("effective_variation") or _rvf_effective_variation_report(result["font_path"], cps)

    masters_dir = out_dir / "masters"
    masters = sorted(
        list(masters_dir.glob("*.ttf")) + list(masters_dir.glob("*.otf")),
        key=_rvf_master_sort_key,
    )

    if len(masters) < 2:
        return _rvf_render_preview(result)

    if not cps:
        cps = _rvf_collect_preview_chars(masters[0])

    if not cps:
        cps = [0x41]

    first_cp = cps[0]
    default_text = chr(first_cp)

    font_faces = []
    font_data = []

    for index, master in enumerate(masters):
        family = f"RealMorphMaster{index + 1:02d}_{dir_name}"
        fmt = "opentype" if master.suffix.lower() == ".otf" else "truetype"
        font_faces.append(
            "@font-face { "
            f"font-family: '{family}'; "
            f"src: url('/real_variable_master_font/{dir_name}/{_rvf_html.escape(master.name)}') format('{fmt}'); "
            "font-weight: 400; font-style: normal; font-display: block; "
            "}"
        )
        font_data.append({
            "family": family,
            "name": master.name,
            "label": f"Step {index + 1:02d}",
        })

    items = [{"char": chr(cp), "code": f"U+{cp:04X}"} for cp in cps[:120]]
    items_json = _rvf_json.dumps(items, ensure_ascii=False)
    fonts_json = _rvf_json.dumps(font_data, ensure_ascii=False)
    default_text_json = _rvf_json.dumps(default_text, ensure_ascii=False)
    font_faces_css = "\n".join(font_faces)
    effective_label = "有效" if effective.get("ok") else "无有效变化"
    effective_class = "ok" if effective.get("ok") else "warn"
    download_label = "下载真实可变字体 TTF" if effective.get("ok") else "下载 VF 文件（MORF 轴无有效变化）"

    return _rvf_HTMLResponse(f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>实际 Step 演变预览</title>
<style>
{font_faces_css}
* {{
  box-sizing: border-box;
}}
.page,
.card,
.layout,
.layout > *,
.preview,
.morph-stack,
.slider-row,
.actions,
.mini-grid,
.sidebar,
textarea {{
  min-width: 0;
  max-width: 100%;
}}
body {{
  margin: 0;
  background: #f5f6f8;
  color: #111827;
  font-family: Arial, "Microsoft YaHei", sans-serif;
}}
.page {{
  max-width: 1220px;
  margin: 24px auto 50px;
  padding: 0 18px;
}}
h1 {{
  font-size: 24px;
  margin: 0 0 8px;
  letter-spacing: 0;
}}
.sub {{
  color: #475569;
  font-size: 14px;
  line-height: 1.7;
  margin-bottom: 16px;
}}
.card {{
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 18px;
  margin-bottom: 16px;
  box-shadow: 0 8px 24px rgba(15, 23, 42, .04);
}}
.verify {{
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}}
.verify-item {{
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 10px;
  font-size: 13px;
}}
.verify-item b {{
  display: block;
  font-size: 17px;
  margin-top: 4px;
}}
.ok {{
  color: #047857;
}}
.warn {{
  color: #b45309;
}}
.layout {{
  display: grid;
  grid-template-columns: 250px minmax(0, 1fr);
  gap: 18px;
}}
.sidebar {{
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 12px;
  max-height: 560px;
  overflow: auto;
}}
.rvf-char-btn {{
  width: 100%;
  min-height: 38px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  border: 1px solid #e5e7eb;
  background: #fafafa;
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 6px;
  cursor: pointer;
}}
.rvf-char-btn.active {{
  border-color: #2563eb;
  background: #eff6ff;
}}
.rvf-char {{
  font-size: 28px;
  line-height: 1;
}}
.rvf-code {{
  font-size: 12px;
  color: #64748b;
}}
.meta {{
  font-size: 13px;
  color: #334155;
  margin-bottom: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
}}
.preview {{
  height: 320px;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  padding: 16px;
}}
.morph-stack {{
  position: relative;
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
}}
.morph-layer {{
  position: absolute;
  inset: 0;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  font-size: clamp(96px, 14vw, 190px);
  line-height: 1;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  transition: opacity 70ms linear;
}}
.slider-row {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) 80px;
  gap: 12px;
  margin-top: 14px;
  align-items: center;
}}
#slider {{
  width: 100%;
}}
#value {{
  font-weight: 800;
  text-align: center;
}}
.actions {{
  margin-top: 12px;
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}}
.actions button,
.actions a {{
  display: inline-block;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  padding: 9px 14px;
  background: #f3f4f6;
  color: #111;
  text-decoration: none;
  cursor: pointer;
  font-weight: 700;
}}
.actions a.primary {{
  background: #111827;
  color: #fff;
  border-color: #111827;
}}
textarea {{
  width: 100%;
  min-height: 64px;
  margin-top: 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  padding: 10px 12px;
  font: inherit;
}}
.mini-grid {{
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-top: 18px;
}}
.mini {{
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 12px;
  background: #fafafa;
  text-align: center;
  overflow: hidden;
}}
.mini-label {{
  font-size: 12px;
  color: #666;
  margin-bottom: 8px;
}}
.mini-glyph {{
  min-height: 86px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 82px;
  line-height: 1;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}}
pre {{
  background: #111827;
  color: #e5e7eb;
  border-radius: 10px;
  padding: 12px;
  overflow: auto;
  font-size: 12px;
}}
@media(max-width:900px) {{
  .layout,
  .verify,
  .mini-grid {{
    grid-template-columns: minmax(0, 1fr);
  }}
  .actions button,
  .actions a {{
    width: 100%;
    text-align: center;
  }}
  .preview {{
    height: 270px;
  }}
}}
</style>
</head>
<body>
<!-- rvf-master-step-preview-v2 -->
<div class="page">
  <h1>实际 Step 演变预览</h1>
  <div class="sub">
    这个页面直接使用参与生成的 Step master 字体显示 A 到 B 的变化；如果真实 Variable Font 的 MORF 轴没有有效变化，预览仍然会按 master 字体展示实际演变。
  </div>

  <div class="card verify">
    <div class="verify-item">fvar 表 <b class="{ "ok" if verify.get("has_fvar") else "warn" }">{ "存在" if verify.get("has_fvar") else "缺失" }</b></div>
    <div class="verify-item">gvar 表 <b class="{ "ok" if verify.get("has_gvar") else "warn" }">{ "存在" if verify.get("has_gvar") else "缺失" }</b></div>
    <div class="verify-item">MORF 实际变化 <b class="{effective_class}">{_rvf_html.escape(effective_label)}</b></div>
    <div class="verify-item">预览来源 <b>Step master</b></div>
  </div>

  <div class="card layout">
    <div class="sidebar">
      <b>选择字符</b>
      <div style="height:10px;"></div>
      <div id="charList"></div>
    </div>

    <div>
      <div class="meta">
        <span>当前字符：<b id="currentChar"></b></span>
        <span>Unicode：<b id="currentCp"></b></span>
        <span>位置：<b id="currentPercent">0%</b></span>
        <span>Step：<b id="currentStep">Step 01</b></span>
      </div>

      <div class="preview">
        <div class="morph-stack">
          <div id="leftLayer" class="morph-layer"></div>
          <div id="rightLayer" class="morph-layer"></div>
        </div>
      </div>

      <div class="slider-row">
        <input id="slider" type="range" min="0" max="100" value="0" step="0.1">
        <div id="value">0%</div>
      </div>

      <div class="actions">
        <button type="button" onclick="setMorph(0)">起始</button>
        <button type="button" onclick="setMorph(50)">中间</button>
        <button type="button" onclick="setMorph(100)">结束</button>
        <a class="primary" href="/download_real_variable_font/{dir_name}/{_rvf_html.escape(font_name)}">{_rvf_html.escape(download_label)}</a>
      </div>

      <textarea id="sampleText">{_rvf_html.escape(default_text)}</textarea>

      <div class="mini-grid">
        <div class="mini">
          <div class="mini-label">起始 / Step 01</div>
          <div id="startGlyph" class="mini-glyph"></div>
        </div>
        <div class="mini">
          <div class="mini-label">当前</div>
          <div id="midGlyph" class="mini-glyph"></div>
        </div>
        <div class="mini">
          <div class="mini-label">结束 / Step {len(masters):02d}</div>
          <div id="endGlyph" class="mini-glyph"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <b>生成结果</b><br>
    字体文件：{_rvf_html.escape(font_name)}<br>
    字体名称：{_rvf_html.escape(family_name)}<br>
    Master 数量：{len(masters)}<br>
    MORF 有效变化检测：{_rvf_html.escape(effective.get("reason", ""))}
  </div>

  <div class="card">
    <b>检测报告</b>
    <pre>{_rvf_html.escape(_rvf_json.dumps({"verify": verify, "effective_variation": effective}, ensure_ascii=False, indent=2))}</pre>
  </div>
</div>

<script>
const ITEMS = {items_json};
const FONTS = {fonts_json};
const DEFAULT_TEXT = {default_text_json};
let currentIndex = 0;
let percent = 0;

const charList = document.getElementById("charList");
const slider = document.getElementById("slider");
const value = document.getElementById("value");
const currentPercent = document.getElementById("currentPercent");
const currentStep = document.getElementById("currentStep");
const currentChar = document.getElementById("currentChar");
const currentCp = document.getElementById("currentCp");
const sampleText = document.getElementById("sampleText");
const leftLayer = document.getElementById("leftLayer");
const rightLayer = document.getElementById("rightLayer");
const startGlyph = document.getElementById("startGlyph");
const midGlyph = document.getElementById("midGlyph");
const endGlyph = document.getElementById("endGlyph");

function clamp(v, a, b) {{
  return Math.max(a, Math.min(b, v));
}}

function frameInfo(v) {{
  const raw = FONTS.length <= 1 ? 0 : (v / 100) * (FONTS.length - 1);
  const left = clamp(Math.floor(raw), 0, FONTS.length - 1);
  const right = clamp(Math.ceil(raw), 0, FONTS.length - 1);
  const mix = right === left ? 0 : raw - left;
  return {{ left, right, mix }};
}}

function applyFont(el, index) {{
  const item = FONTS[clamp(index, 0, FONTS.length - 1)];
  if (item) el.style.fontFamily = "'" + item.family + "', sans-serif";
}}

function textValue() {{
  return sampleText.value || (ITEMS[currentIndex] ? ITEMS[currentIndex].char : DEFAULT_TEXT) || DEFAULT_TEXT;
}}

function renderCharList() {{
  charList.innerHTML = "";
  ITEMS.forEach((item, index) => {{
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "rvf-char-btn" + (index === currentIndex ? " active" : "");
    btn.innerHTML = '<span class="rvf-char">' + item.char + '</span><span class="rvf-code">' + item.code + '</span>';
    applyFont(btn.querySelector(".rvf-char"), 0);
    btn.addEventListener("click", () => {{
      currentIndex = index;
      sampleText.value = item.char;
      renderCharList();
      updateAll();
    }});
    charList.appendChild(btn);
  }});
}}

function updateAll() {{
  const info = frameInfo(percent);
  const text = textValue();
  const item = ITEMS[currentIndex] || {{ char: text.slice(0, 1), code: "" }};

  slider.value = String(percent);
  value.textContent = Math.round(percent) + "%";
  currentPercent.textContent = Math.round(percent) + "%";
  currentChar.textContent = item.char;
  currentCp.textContent = item.code;

  const leftLabel = FONTS[info.left] ? FONTS[info.left].label : "Step 01";
  const rightLabel = FONTS[info.right] ? FONTS[info.right].label : leftLabel;
  currentStep.textContent = leftLabel === rightLabel ? leftLabel : leftLabel + " -> " + rightLabel;

  leftLayer.textContent = text;
  rightLayer.textContent = text;
  midGlyph.textContent = text;
  startGlyph.textContent = text;
  endGlyph.textContent = text;

  applyFont(leftLayer, info.left);
  applyFont(rightLayer, info.right);
  applyFont(midGlyph, info.mix < 0.5 ? info.left : info.right);
  applyFont(startGlyph, 0);
  applyFont(endGlyph, FONTS.length - 1);

  leftLayer.style.opacity = String(1 - info.mix);
  rightLayer.style.opacity = String(info.mix);
}}

function setMorph(v) {{
  percent = clamp(v, 0, 100);
  updateAll();
}}

slider.addEventListener("input", function() {{
  setMorph(parseFloat(this.value));
}});

sampleText.addEventListener("input", updateAll);

renderCharList();
updateAll();
</script>
</body>
</html>
""")


def _rvf_render_diagnostic_failure(message, report=None, status_code=400):
    report = report or {}
    master = report.get("master_compatibility")
    if not isinstance(master, dict):
        master = report if "glyphs_checked" in report else {}

    effective = report.get("effective_variation") if isinstance(report.get("effective_variation"), dict) else {}
    verify = report.get("verify") if isinstance(report.get("verify"), dict) else {}

    def _text(value, default="-"):
        if value is None or value == "":
            return default
        return str(value)

    effective_text = "通过" if effective.get("ok") else _text(effective.get("reason"), "未运行")
    verify_text = "通过" if verify.get("ok") else _text(verify.get("reason"), "未运行")
    reason = master.get("reason") or effective.get("reason") or verify.get("reason") or message

    cards = [
        ("Master 数量", _text(master.get("master_count", report.get("master_count")))),
        ("已检查 glyph", _text(master.get("glyphs_checked"))),
        ("不兼容 glyph", _text(master.get("incompatible_glyphs"))),
        ("有坐标变化 glyph", _text(master.get("moving_glyphs"))),
        ("实例化坐标检查", effective_text),
        ("VF 表检查", verify_text),
    ]

    cards_html = "".join(
        f"""
        <div class="metric">
            <div class="label">{_rvf_html.escape(label)}</div>
            <div class="value">{_rvf_html.escape(value)}</div>
        </div>
        """
        for label, value in cards
    )

    examples = master.get("examples") or []
    examples_html = ""
    if examples:
        examples_html = f"""
        <h2>不兼容示例</h2>
        <pre>{_rvf_html.escape(_rvf_json.dumps(examples[:20], ensure_ascii=False, indent=2, default=str))}</pre>
        """

    report_json = _rvf_json.dumps(report, ensure_ascii=False, indent=2, default=str)

    return _rvf_HTMLResponse(f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>当前结果不能生成真实 VF</title>
<style>
* {{
    box-sizing: border-box;
}}
body {{
    margin: 0;
    background: #f5f6f8;
    color: #111827;
    font-family: Arial, "Microsoft YaHei", sans-serif;
}}
.page {{
    max-width: 1080px;
    margin: 28px auto 56px;
    padding: 0 18px;
}}
.card {{
    background: #fff;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 22px;
    box-shadow: 0 10px 30px rgba(15, 23, 42, .05);
}}
h1 {{
    margin: 0 0 10px;
    font-size: 24px;
    letter-spacing: 0;
}}
h2 {{
    margin: 22px 0 10px;
    font-size: 16px;
}}
.lead {{
    margin: 0 0 12px;
    color: #334155;
    line-height: 1.75;
}}
.reason {{
    border-left: 4px solid #dc2626;
    background: #fff7f7;
    padding: 12px 14px;
    margin: 16px 0;
    color: #7f1d1d;
    line-height: 1.7;
    font-weight: 700;
}}
.grid {{
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
    margin-top: 16px;
}}
.metric {{
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 12px;
    background: #f8fafc;
    min-width: 0;
}}
.label {{
    color: #64748b;
    font-size: 12px;
    margin-bottom: 7px;
}}
.value {{
    color: #0f172a;
    font-size: 14px;
    font-weight: 700;
    overflow-wrap: anywhere;
}}
pre {{
    background: #0f172a;
    color: #e5e7eb;
    border-radius: 10px;
    padding: 14px;
    overflow: auto;
    line-height: 1.55;
    font-size: 12px;
    max-height: 430px;
}}
.actions {{
    margin-top: 18px;
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
}}
a {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 36px;
    padding: 0 14px;
    border-radius: 8px;
    background: #111827;
    color: #fff;
    text-decoration: none;
    font-weight: 700;
}}
@media (max-width: 760px) {{
    .grid {{
        grid-template-columns: 1fr;
    }}
}}
</style>
</head>
<body>
<main class="page">
    <section class="card">
        <h1>当前结果不能生成真实 VF</h1>
        <p class="lead">已按真实 Variable Font 的 master 兼容性规则检查：轮廓数量、点数、点序、on-curve/off-curve 结构必须一致，并且实例化后坐标必须实际变化。</p>
        <div class="reason">{_rvf_html.escape(str(message))}<br>{_rvf_html.escape(str(reason))}</div>
        <p class="lead">因此这里不会展示 MORF 滑杆，避免把普通 Step TTF 的切换效果误认为真实可变字体。</p>
        <div class="grid">
            {cards_html}
        </div>
        {examples_html}
        <h2>完整诊断报告</h2>
        <pre>{_rvf_html.escape(report_json)}</pre>
        <div class="actions">
            <a href="javascript:history.back()">返回上一页</a>
        </div>
    </section>
</main>
</body>
</html>
""", status_code=status_code)


def _rvf_real_preview_block_reason(report):
    report = report or {}

    master = report.get("master_compatibility")
    if isinstance(master, dict) and not master.get("ok"):
        return "当前结果不能生成真实 VF：Step master 没有通过兼容性诊断。"

    verify = report.get("verify")
    if isinstance(verify, dict) and not verify.get("ok"):
        return "当前结果不能生成真实 VF：输出文件没有通过 fvar/gvar 表检查。"

    effective = report.get("effective_variation")
    if not isinstance(effective, dict):
        return "当前结果不能生成真实 VF：缺少实例化坐标变化诊断。"

    if not effective.get("ok"):
        return "当前结果不能生成真实 VF：实例化后没有检测到 glyph 坐标变化。"

    return ""


def _rvf_existing_result(dir_name):
    safe_dir = _rvf_re.sub(r"[^0-9A-Za-z_\-]", "", dir_name)
    out_dir = _rvf_output_root() / safe_dir
    report_path = out_dir / "real_variable_font_report.json"

    if not out_dir.exists() or not report_path.exists():
        return None

    report = _rvf_json.loads(report_path.read_text(encoding="utf-8", errors="ignore"))
    font_name = report.get("font_file", "")
    font_path = out_dir / _rvf_safe_file(font_name)

    if not font_path.exists():
        return None

    preview_cps = _rvf_collect_preview_chars(font_path)
    changed = False

    if "master_compatibility" not in report:
        masters_dir = out_dir / "masters"
        master_paths = sorted(
            list(masters_dir.glob("*.ttf")) + list(masters_dir.glob("*.otf")),
            key=_rvf_master_sort_key,
        )
        if len(master_paths) >= 2:
            report["master_compatibility"] = _rvf_diagnose_master_compatibility(master_paths)
            changed = True

    if "effective_variation" not in report:
        report["effective_variation"] = _rvf_effective_variation_report(font_path, preview_cps)
        changed = True

    if changed:
        try:
            report_path.write_text(
                _rvf_json.dumps(report, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass

    return {
        "out_dir": out_dir,
        "font_path": font_path,
        "font_name": font_path.name,
        "family_name": report.get("family_name", "Real Morph Variable"),
        "preview_cps": preview_cps,
        "report": report,
    }


@app.get("/real_variable_preview/{dir_name}", response_class=_rvf_HTMLResponse)
async def real_variable_preview_existing(dir_name: str):
    result = _rvf_existing_result(dir_name)

    if not result:
        return _rvf_HTMLResponse("<h2>找不到真实可变字体预览结果</h2>", status_code=404)

    block_reason = _rvf_real_preview_block_reason(result["report"])
    if block_reason:
        return _rvf_render_diagnostic_failure(block_reason, result["report"], status_code=200)

    return _rvf_render_preview(result)


@app.post("/generate_real_variable_font")
async def generate_real_variable_font(
    selected_fonts: _rvf_List[str] = _rvf_Form(default=[]),
    variable_name: str = _rvf_Form(default="Real Morph Variable"),
):
    try:
        result = _rvf_build_real_variable_font(selected_fonts, variable_name)
        return _rvf_render_preview(result)

    except _rvf_DiagnosticError as e:
        return _rvf_render_diagnostic_failure(str(e), e.report, status_code=400)

    except Exception as e:
        return _rvf_render_diagnostic_failure(
            "真实可变字体生成失败：" + str(e),
            {"error": str(e), "traceback": _rvf_traceback.format_exc()},
            status_code=500,
        )


_REAL_VARIABLE_FONT_JS = r"""
<script>
(function() {
  function extractTTFFilesFromPage() {
    const text = document.body ? document.body.innerText : "";
    const matches = text.match(/[^\s"'<>\/\\]+?\.(?:ttf|otf)/ig) || [];

    let files = matches.map(x => x.trim());

    files = files.filter(x => {
      const lower = x.toLowerCase();
      return /\.(ttf|otf)$/i.test(x)
        && !lower.includes("variable")
        && !lower.includes("generatedmorphvariable")
        && !x.includes("http")
        && x.length < 160;
    });

    files = Array.from(new Set(files));

    files.sort((a, b) => {
      const ma = a.match(/step[_-]?0?(\d+)/i) || a.match(/(\d+)/);
      const mb = b.match(/step[_-]?0?(\d+)/i) || b.match(/(\d+)/);
      const na = ma ? parseInt(ma[1]) : 0;
      const nb = mb ? parseInt(mb[1]) : 0;
      return na - nb;
    });

    return files;
  }

  function getSelectedFonts(files) {
    const selected = [];

    const checkboxes = Array.from(document.querySelectorAll(
      '#auto-selected-ttf-panel input[data-font], #selected-step-download-panel input[data-sd-font], input[name="selected_fonts"]'
    ));

    if (checkboxes.length) {
      checkboxes.forEach(cb => {
        if (!cb.checked) return;

        let file = cb.dataset.font || cb.dataset.sdFont || cb.value || "";
        file = String(file).split(/[\/\\]/).pop();

        if (files.includes(file) && !selected.includes(file)) {
          selected.push(file);
        }
      });
    }

    if (selected.length >= 2) {
      return selected;
    }

    return files;
  }

  function ensureStyle() {
    if (document.getElementById("real-vf-style")) return;

    const style = document.createElement("style");
    style.id = "real-vf-style";
    style.textContent = `
      #real-vf-panel {
        order: 4;
        background: #ffffff;
        color: #0f172a;
        border: 1px solid #d8dee8;
        border-radius: 8px;
        box-shadow: 0 12px 28px rgba(15,23,42,.08);
        padding: 16px;
        margin: 0;
        font-family: Arial, "Microsoft YaHei", sans-serif;
      }
      #real-vf-panel .rvf-title {
        font-size: 16px;
        font-weight: 800;
        margin-bottom: 6px;
        color: #0f172a;
      }
      #real-vf-panel .rvf-desc {
        font-size: 13px;
        color: #64748b;
        margin-bottom: 12px;
        line-height: 1.55;
      }
      #real-vf-panel .rvf-row {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 10px;
        align-items: center;
      }
      #real-vf-panel input[type="text"] {
        width: 100%;
        box-sizing: border-box;
        border: 1px solid #d1d5db;
        border-radius: 6px;
        padding: 10px 12px;
        min-height: 40px;
      }
      #real-vf-panel button {
        width: auto !important;
        min-height: 40px;
        padding: 9px 15px;
        border-radius: 6px;
        border: 1px solid #0f172a;
        background: #0f172a;
        color: #fff;
        cursor: pointer;
        font-weight: 700;
        white-space: nowrap;
      }
      #real-vf-panel .rvf-small {
        margin-top: 9px;
        font-size: 12px;
        color: #64748b;
      }
      @media (max-width: 760px) {
        #real-vf-panel .rvf-row {
          grid-template-columns: 1fr;
        }
        #real-vf-panel button {
          width: 100% !important;
        }
      }
    `;
    document.head.appendChild(style);
  }

  function submitVariableFont(files) {
    const picked = getSelectedFonts(files);

    if (picked.length < 2) {
      alert("真实可变字体至少需要选择 2 个 Step TTF。");
      return;
    }

    const nameInput = document.getElementById("real-vf-name");
    const variableName = nameInput ? nameInput.value.trim() : "Real Morph Variable";

    const form = document.createElement("form");
    form.method = "post";
    form.action = "/generate_real_variable_font";
    form.style.display = "none";

    picked.forEach(f => {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "selected_fonts";
      input.value = f;
      form.appendChild(input);
    });

    const name = document.createElement("input");
    name.type = "hidden";
    name.name = "variable_name";
    name.value = variableName || "Real Morph Variable";
    form.appendChild(name);

    document.body.appendChild(form);
    form.submit();
  }

  function createPanel(files) {
    if (document.getElementById("real-vf-panel")) return;

    ensureStyle();

    const panel = document.createElement("div");
    panel.id = "real-vf-panel";

    const title = document.createElement("div");
    title.className = "rvf-title";
    title.textContent = "生成真实可变字体";

    const desc = document.createElement("div");
    desc.className = "rvf-desc";
    desc.textContent = "使用当前 Step TTF 构建真正包含 fvar/gvar 表的 Variable Font。若生成结果不是可变字体，系统会直接报错，不再进入假预览。";

    const row = document.createElement("div");
    row.className = "rvf-row";

    const input = document.createElement("input");
    input.type = "text";
    input.id = "real-vf-name";
    input.value = "Real Morph Variable";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "生成真实可变字体";
    btn.onclick = function() {
      submitVariableFont(files);
    };

    row.appendChild(input);
    row.appendChild(btn);

    const small = document.createElement("div");
    small.className = "rvf-small";
    small.textContent = "已识别到 " + files.length + " 个 Step TTF/OTF。注意：之前生成过的 Variable 字体不会再被当作 master。";

    panel.appendChild(title);
    panel.appendChild(desc);
    panel.appendChild(row);
    panel.appendChild(small);

    const selectedPanel =
      document.getElementById("auto-selected-ttf-panel") ||
      document.getElementById("selected-step-download-panel");

    if (selectedPanel && selectedPanel.parentNode) {
      selectedPanel.parentNode.insertBefore(panel, selectedPanel.nextSibling);
      return;
    }

    const h1 = document.querySelector("h1");

    if (h1 && h1.parentNode) {
      h1.parentNode.insertBefore(panel, h1.nextSibling);
      return;
    }

    document.body.insertBefore(panel, document.body.firstChild);
  }

  function main() {
    const files = extractTTFFilesFromPage();
    if (files.length < 2) return;
    createPanel(files);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", main);
  } else {
    main();
  }
})();
</script>
"""


@app.middleware("http")
async def _real_variable_font_injector(request, call_next):
    response = await call_next(request)

    content_type = response.headers.get("content-type", "")

    if (
        request.url.path.startswith("/real_variable_preview/")
        or request.url.path == "/generate_real_variable_font"
        or request.url.path in {"/oyun_gb_preview", "/menk_gb_preview"}
        or request.url.path == "/text_fur"
        or request.url.path == "/text_motion"
        or request.url.path == "/lora_lab"
    ):
        return response

    if "text/html" not in content_type.lower():
        return response

    try:
        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        text = body.decode("utf-8", errors="ignore")

        should_inject = (
            ".ttf" in text.lower()
            or "TTF 预览" in text
            or "实时可变滑杆预览" in text
            or "字体预览" in text
            or "生成结果" in text
        )

        if should_inject and "real-vf-panel" not in text:
            if "</body>" in text:
                text = text.replace("</body>", _REAL_VARIABLE_FONT_JS + "\n</body>")
            else:
                text += _REAL_VARIABLE_FONT_JS

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return _rvf_Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )

    except Exception:
        return response

# ================= REAL_VARIABLE_FONT_ADDON_END =================


# ================= TRUE_GLYF_STEP_PREVIEW_START =================
# 真实 glyf 轮廓 Step 预览
# 不使用浏览器 @font-face，直接读取每个 Step TTF 的 glyf 轮廓并转 SVG。

from pathlib import Path as _tg_Path
import re as _tg_re
import html as _tg_html
import hashlib as _tg_hashlib
from functools import lru_cache as _tg_lru_cache

from fastapi import Query as _tg_Query
from fastapi.responses import HTMLResponse as _tg_HTMLResponse
from fastapi.responses import Response as _tg_Response

from fontTools.ttLib import TTFont as _tg_TTFont
from fontTools.pens.svgPathPen import SVGPathPen as _tg_SVGPathPen
from fontTools.pens.boundsPen import BoundsPen as _tg_BoundsPen


def _tg_root():
    return _tg_Path(__file__).resolve().parent


def _tg_mode_dir(mode: str):
    mode = (mode or "oyun").lower()

    if mode in ["menk", "menksoft", "mengke", "mengkelit"]:
        return _tg_root() / "output" / "menk_gb_ttf_steps"

    return _tg_root() / "output" / "oyun_gb_ttf_steps"


def _tg_step_no(path):
    m = _tg_re.search(r"step[_\-]?0*(\d+)", path.name, _tg_re.I)
    if m:
        return int(m.group(1))
    return 999999


def _tg_step_fonts(mode: str):
    d = _tg_mode_dir(mode)

    if not d.exists():
        return []

    fonts = [
        p for p in d.glob("*.ttf")
        if _tg_re.search(r"step[_\-]?0*\d+", p.name, _tg_re.I)
    ]

    return sorted(fonts, key=_tg_step_no)


def _tg_best_cmap(font):
    if "cmap" not in font:
        return {}

    best = None
    best_score = -1

    for table in font["cmap"].tables:
        if not table.isUnicode():
            continue

        score = len(table.cmap)

        if table.platformID == 3:
            score += 1000000

        if score > best_score:
            best = table.cmap
            best_score = score

    return dict(best or {})


def _tg_parse_cp(cp_hex: str):
    cp_hex = str(cp_hex).upper().replace("U+", "").replace("U", "")
    return int(cp_hex, 16)


@_tg_lru_cache(maxsize=100000)
def _tg_render_svg(font_path: str, cp: int, size: int = 86):
    font = _tg_TTFont(font_path)
    cmap = _tg_best_cmap(font)

    glyph_name = cmap.get(cp)

    if not glyph_name:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}"><rect width="{size}" height="{size}" fill="white"/><text x="{size/2}" y="{size/2}" font-size="9" text-anchor="middle" fill="#999">missing</text></svg>'

    glyph_set = font.getGlyphSet()
    glyph = glyph_set[glyph_name]

    path_pen = _tg_SVGPathPen(glyph_set)
    glyph.draw(path_pen)
    path_data = path_pen.getCommands()

    if not path_data:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}"><rect width="{size}" height="{size}" fill="white"/><text x="{size/2}" y="{size/2}" font-size="9" text-anchor="middle" fill="#999">empty</text></svg>'

    bounds_pen = _tg_BoundsPen(glyph_set)
    glyph.draw(bounds_pen)
    bounds = bounds_pen.bounds

    if not bounds:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}"><rect width="{size}" height="{size}" fill="white"/><text x="{size/2}" y="{size/2}" font-size="9" text-anchor="middle" fill="#999">empty</text></svg>'

    x_min, y_min, x_max, y_max = bounds

    w = max(x_max - x_min, 1)
    h = max(y_max - y_min, 1)

    pad = 8
    draw_w = size - pad * 2
    draw_h = size - pad * 2

    scale = min(draw_w / w, draw_h / h)

    tx = pad + (draw_w - w * scale) / 2 - x_min * scale
    ty = pad + (draw_h - h * scale) / 2 + y_max * scale

    path_data = _tg_html.escape(path_data, quote=True)

    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}"><rect width="{size}" height="{size}" fill="white"/><path d="{path_data}" fill="black" transform="translate({tx:.4f},{ty:.4f}) scale({scale:.7f},{-scale:.7f})"/></svg>'


def _tg_digest_for_font(font_path, cps):
    font = _tg_TTFont(str(font_path))
    cmap = _tg_best_cmap(font)

    if "glyf" not in font:
        return "no-glyf", 0

    glyf = font["glyf"]

    h = _tg_hashlib.sha256()
    n = 0

    for cp in cps:
        gn = cmap.get(cp)

        if not gn or gn not in glyf:
            continue

        try:
            coords, end_pts, flags = glyf[gn].getCoordinates(glyf)
        except Exception:
            continue

        n += 1
        h.update(f"U+{cp:04X}:{gn}:".encode())
        h.update(str(list(end_pts)).encode())
        h.update(str([(int(x), int(y)) for x, y in coords]).encode())
        h.update(str([int(f) & 1 for f in flags]).encode())

    return h.hexdigest(), n


@app.get("/true_glyf_svg/{mode}/{font_name}/{cp_hex}")
async def true_glyf_svg(mode: str, font_name: str, cp_hex: str):
    safe_font = _tg_Path(font_name).name
    fonts = _tg_step_fonts(mode)

    target = None

    for f in fonts:
        if f.name == safe_font:
            target = f
            break

    if not target:
        return _tg_Response(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 86 86"><text x="43" y="43" text-anchor="middle" font-size="8">font not found</text></svg>',
            media_type="image/svg+xml",
        )

    try:
        cp = _tg_parse_cp(cp_hex)
    except Exception:
        cp = 0x1820

    svg = _tg_render_svg(str(target), cp, 86)

    return _tg_Response(svg, media_type="image/svg+xml")


@app.get("/true_glyf_step_preview")
async def true_glyf_step_preview(
    mode: str = _tg_Query("oyun"),
    start: str = _tg_Query("1820"),
    end: str = _tg_Query("1842"),
):
    mode = (mode or "oyun").lower()
    fonts = _tg_step_fonts(mode)

    if not fonts:
        return _tg_HTMLResponse(
            f"<h1>找不到 Step 字体</h1><p>目录不存在或为空：{_tg_html.escape(str(_tg_mode_dir(mode)))}</p>",
            status_code=404,
        )

    try:
        start_cp = _tg_parse_cp(start)
        end_cp = _tg_parse_cp(end)
    except Exception:
        start_cp = 0x1820
        end_cp = 0x1842

    if start_cp > end_cp:
        start_cp, end_cp = end_cp, start_cp

    cps = list(range(start_cp, end_cp + 1))

    digests = []

    for f in fonts:
        d, n = _tg_digest_for_font(f, cps)
        digests.append(d)

    unique_count = len(set(digests))

    mode_name = "奥云" if mode == "oyun" else "蒙科立"

    head_cells = []

    for idx, f in enumerate(fonts, start=1):
        head_cells.append(
            '<th><div>Step %02d</div><div class="file">%s</div></th>'
            % (idx, _tg_html.escape(f.name))
        )

    rows = []

    for cp in cps:
        ch = chr(cp)

        cells = []

        for f in fonts:
            cells.append(
                '<td><img class="glyph-img" src="/true_glyf_svg/%s/%s/U+%04X?v=%s"></td>'
                % (
                    _tg_html.escape(mode),
                    _tg_html.escape(f.name),
                    cp,
                    int(f.stat().st_mtime),
                )
            )

        rows.append(
            '<tr><td class="sticky char">%s</td><td class="sticky2 code">U+%04X</td>%s</tr>'
            % (_tg_html.escape(ch), cp, "".join(cells))
        )

    digest_rows = []

    for f, d in zip(fonts, digests):
        digest_rows.append(
            "<tr><td>%s</td><td>%s</td></tr>"
            % (_tg_html.escape(f.name), d[:24])
        )

    status = "真实轮廓存在变化" if unique_count > 1 else "真实轮廓完全相同"
    status_class = "ok" if unique_count > 1 else "bad"

    html_doc = """
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
body {
  margin: 0;
  background: #f5f6f8;
  color: #111;
  font-family: Arial, "Microsoft YaHei", sans-serif;
}
.page {
  max-width: 1800px;
  margin: 24px auto 50px;
  padding: 0 18px;
}
h1 {
  margin: 0 0 8px;
  font-size: 24px;
}
.sub {
  color: #555;
  line-height: 1.7;
  font-size: 14px;
  margin-bottom: 16px;
}
.card {
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 14px;
  padding: 16px;
  margin-bottom: 16px;
  box-shadow: 0 8px 24px rgba(0,0,0,.04);
}
.ok {
  color: #047857;
  font-weight: 800;
}
.bad {
  color: #b91c1c;
  font-weight: 800;
}
.actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.actions a {
  display: inline-block;
  padding: 8px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  color: #111;
  text-decoration: none;
  background: #f3f4f6;
  font-weight: 700;
}
.table-wrap {
  overflow: auto;
  border: 1px solid #e5e7eb;
  border-radius: 14px;
  background: white;
}
table {
  border-collapse: collapse;
  min-width: 1600px;
  width: 100%;
}
th, td {
  border: 1px solid #e5e7eb;
  text-align: center;
  vertical-align: middle;
  padding: 6px;
}
th {
  position: sticky;
  top: 0;
  z-index: 10;
  background: #fafafa;
  font-size: 12px;
}
.file {
  color: #777;
  font-size: 10px;
  margin-top: 4px;
}
.sticky {
  position: sticky;
  left: 0;
  background: white;
  z-index: 8;
}
.sticky2 {
  position: sticky;
  left: 64px;
  background: white;
  z-index: 8;
}
.char {
  width: 64px;
  font-size: 22px;
}
.code {
  width: 90px;
  font-size: 12px;
  color: #555;
}
.glyph-img {
  width: 86px;
  height: 86px;
  display: block;
  margin: 0 auto;
}
.digest-table {
  width: auto;
  min-width: 600px;
}
.digest-table td {
  text-align: left;
  font-size: 12px;
}
</style>
</head>
<body>
<div class="page">
  <h1>__TITLE__</h1>
  <div class="sub">
    这个页面不使用浏览器字体加载，而是后端逐个读取 Step TTF 的真实 glyf 轮廓并转 SVG。
    如果这里能看到变化，说明算法没问题，原页面预览是字体加载或缓存问题。
  </div>

  <div class="card">
    <b>检测目录：</b>__DIR__<br>
    <b>Step 数量：</b>__FONT_COUNT__<br>
    <b>当前检测字符：</b>__RANGE__<br>
    <b>不同轮廓版本数量：</b><span class="__STATUS_CLASS__">__UNIQUE_COUNT__</span><br>
    <b>结论：</b><span class="__STATUS_CLASS__">__STATUS__</span>

    <div class="actions">
      <a href="/true_glyf_step_preview?mode=oyun&start=1820&end=1842">查看奥云基础字母</a>
      <a href="/true_glyf_step_preview?mode=menk&start=1820&end=1842">查看蒙科立基础字母</a>
      <a href="javascript:history.back()">返回原页面</a>
    </div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="sticky char">字符</th>
          <th class="sticky2 code">Unicode</th>
          __HEAD_CELLS__
        </tr>
      </thead>
      <tbody>
        __ROWS__
      </tbody>
    </table>
  </div>

  <div class="card" style="margin-top:16px;">
    <b>轮廓哈希检测</b>
    <div class="table-wrap">
      <table class="digest-table">
        <tbody>
          __DIGEST_ROWS__
        </tbody>
      </table>
    </div>
  </div>
</div>
</body>
</html>
"""

    html_doc = (
        html_doc
        .replace("__TITLE__", _tg_html.escape(f"{mode_name}｜真实 glyf Step 预览"))
        .replace("__DIR__", _tg_html.escape(str(_tg_mode_dir(mode))))
        .replace("__FONT_COUNT__", str(len(fonts)))
        .replace("__RANGE__", f"U+{start_cp:04X} - U+{end_cp:04X}")
        .replace("__STATUS_CLASS__", status_class)
        .replace("__UNIQUE_COUNT__", str(unique_count))
        .replace("__STATUS__", status)
        .replace("__HEAD_CELLS__", "".join(head_cells))
        .replace("__ROWS__", "".join(rows))
        .replace("__DIGEST_ROWS__", "".join(digest_rows))
    )

    return _tg_HTMLResponse(html_doc)

# ================= TRUE_GLYF_STEP_PREVIEW_END =================

# ================= FIXED_AXIS_GLYF_PREVIEW_START =================
# 固定坐标系 glyf 真实预览
# 目的：避免每个 SVG 单独缩放居中导致变化被抵消。
# 同一个字符在 Step01-Step20 使用统一 bounds、统一比例、统一基线。

from pathlib import Path as _fax_Path
import re as _fax_re
import html as _fax_html
import hashlib as _fax_hashlib
from functools import lru_cache as _fax_lru_cache

from fastapi import Query as _fax_Query
from fastapi.responses import HTMLResponse as _fax_HTMLResponse
from fastapi.responses import Response as _fax_Response

from fontTools.ttLib import TTFont as _fax_TTFont
from fontTools.pens.svgPathPen import SVGPathPen as _fax_SVGPathPen
from fontTools.pens.boundsPen import BoundsPen as _fax_BoundsPen


def _fax_root():
    return _fax_Path(__file__).resolve().parent


def _fax_mode_dir(mode: str):
    mode = (mode or "oyun").lower()
    if mode in ["menk", "menksoft", "mengke", "mengkelit"]:
        return _fax_root() / "output" / "menk_gb_ttf_steps"
    return _fax_root() / "output" / "oyun_gb_ttf_steps"


def _fax_step_no(path):
    m = _fax_re.search(r"step[_\-]?0*(\d+)", path.name, _fax_re.I)
    if m:
        return int(m.group(1))
    return 999999


def _fax_step_fonts(mode: str):
    d = _fax_mode_dir(mode)
    if not d.exists():
        return []

    fonts = [
        p for p in d.glob("*.ttf")
        if _fax_re.search(r"step[_\-]?0*\d+", p.name, _fax_re.I)
    ]
    return sorted(fonts, key=_fax_step_no)


def _fax_best_cmap(font):
    if "cmap" not in font:
        return {}

    best = None
    best_score = -1

    for table in font["cmap"].tables:
        if not table.isUnicode():
            continue

        score = len(table.cmap)
        if table.platformID == 3:
            score += 1000000

        if score > best_score:
            best = table.cmap
            best_score = score

    return dict(best or {})


def _fax_parse_cp(cp_hex: str):
    cp_hex = str(cp_hex).upper().replace("U+", "").replace("U", "")
    return int(cp_hex, 16)


def _fax_glyph_bounds(font_path, cp):
    font = _fax_TTFont(str(font_path))
    cmap = _fax_best_cmap(font)
    gn = cmap.get(cp)
    if not gn:
        return None

    gs = font.getGlyphSet()
    glyph = gs[gn]

    pen = _fax_BoundsPen(gs)
    glyph.draw(pen)
    return pen.bounds


@_fax_lru_cache(maxsize=10000)
def _fax_common_bounds(mode: str, cp: int):
    fonts = _fax_step_fonts(mode)
    bounds_list = []

    for f in fonts:
        b = _fax_glyph_bounds(str(f), cp)
        if b:
            bounds_list.append(b)

    if not bounds_list:
        return None

    x_min = min(b[0] for b in bounds_list)
    y_min = min(b[1] for b in bounds_list)
    x_max = max(b[2] for b in bounds_list)
    y_max = max(b[3] for b in bounds_list)

    if x_max <= x_min:
        x_max = x_min + 1
    if y_max <= y_min:
        y_max = y_min + 1

    return x_min, y_min, x_max, y_max


@_fax_lru_cache(maxsize=100000)
def _fax_render_svg_fixed(font_path: str, mode: str, cp: int, size: int = 110):
    font = _fax_TTFont(str(font_path))
    cmap = _fax_best_cmap(font)

    glyph_name = cmap.get(cp)

    if not glyph_name:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}"><rect width="{size}" height="{size}" fill="white"/><text x="{size/2}" y="{size/2}" font-size="9" text-anchor="middle" fill="#999">missing</text></svg>'

    glyph_set = font.getGlyphSet()
    glyph = glyph_set[glyph_name]

    path_pen = _fax_SVGPathPen(glyph_set)
    glyph.draw(path_pen)
    path_data = path_pen.getCommands()

    if not path_data:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}"><rect width="{size}" height="{size}" fill="white"/><text x="{size/2}" y="{size/2}" font-size="9" text-anchor="middle" fill="#999">empty</text></svg>'

    common = _fax_common_bounds(mode, cp)

    if not common:
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}"><rect width="{size}" height="{size}" fill="white"/><text x="{size/2}" y="{size/2}" font-size="9" text-anchor="middle" fill="#999">no bounds</text></svg>'

    x_min, y_min, x_max, y_max = common

    w = max(x_max - x_min, 1)
    h = max(y_max - y_min, 1)

    pad = 10
    draw_w = size - pad * 2
    draw_h = size - pad * 2

    scale = min(draw_w / w, draw_h / h)

    # 关键：所有 step 用同一个 common bounds 来计算 tx/ty
    tx = pad + (draw_w - w * scale) / 2 - x_min * scale
    ty = pad + (draw_h - h * scale) / 2 + y_max * scale

    path_data = _fax_html.escape(path_data, quote=True)

    # 灰色十字线/基准框用于看位置变化
    return f'''
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">
  <rect width="{size}" height="{size}" fill="white"/>
  <line x1="0" y1="{size/2}" x2="{size}" y2="{size/2}" stroke="#eeeeee" stroke-width="1"/>
  <line x1="{size/2}" y1="0" x2="{size/2}" y2="{size}" stroke="#eeeeee" stroke-width="1"/>
  <path d="{path_data}" fill="black" transform="translate({tx:.4f},{ty:.4f}) scale({scale:.7f},{-scale:.7f})"/>
</svg>
'''


def _fax_digest_one(font_path, cp):
    font = _fax_TTFont(str(font_path))
    cmap = _fax_best_cmap(font)

    if "glyf" not in font:
        return "no-glyf"

    gn = cmap.get(cp)
    if not gn:
        return "missing"

    glyf = font["glyf"]

    if gn not in glyf:
        return "missing"

    try:
        coords, end_pts, flags = glyf[gn].getCoordinates(glyf)
    except Exception:
        return "error"

    h = _fax_hashlib.sha256()
    h.update(str(list(end_pts)).encode())
    h.update(str([(int(x), int(y)) for x, y in coords]).encode())
    h.update(str([int(f) & 1 for f in flags]).encode())
    return h.hexdigest()


@app.get("/fixed_axis_glyf_svg/{mode}/{font_name}/{cp_hex}")
async def fixed_axis_glyf_svg(mode: str, font_name: str, cp_hex: str):
    safe_font = _fax_Path(font_name).name
    fonts = _fax_step_fonts(mode)

    target = None
    for f in fonts:
        if f.name == safe_font:
            target = f
            break

    if not target:
        return _fax_Response(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 110 110"><text x="55" y="55" text-anchor="middle" font-size="9">font not found</text></svg>',
            media_type="image/svg+xml",
        )

    try:
        cp = _fax_parse_cp(cp_hex)
    except Exception:
        cp = 0x1820

    svg = _fax_render_svg_fixed(str(target), mode, cp, 110)
    return _fax_Response(svg, media_type="image/svg+xml")


@app.get("/fixed_axis_glyf_preview")
async def fixed_axis_glyf_preview(
    mode: str = _fax_Query("oyun"),
    start: str = _fax_Query("1820"),
    end: str = _fax_Query("1842"),
):
    mode = (mode or "oyun").lower()
    fonts = _fax_step_fonts(mode)

    if not fonts:
        return _fax_HTMLResponse(
            f"<h1>找不到 Step 字体</h1><p>{_fax_html.escape(str(_fax_mode_dir(mode)))}</p>",
            status_code=404,
        )

    try:
        start_cp = _fax_parse_cp(start)
        end_cp = _fax_parse_cp(end)
    except Exception:
        start_cp = 0x1820
        end_cp = 0x1842

    if start_cp > end_cp:
        start_cp, end_cp = end_cp, start_cp

    cps = list(range(start_cp, end_cp + 1))

    mode_name = "奥云" if mode == "oyun" else "蒙科立"

    head_cells = []
    for idx, f in enumerate(fonts, start=1):
        head_cells.append(
            '<th><div>Step %02d</div><div class="file">%s</div></th>'
            % (idx, _fax_html.escape(f.name))
        )

    rows = []

    for cp in cps:
        ch = chr(cp)

        digests = [_fax_digest_one(str(f), cp) for f in fonts]
        unique_for_cp = len(set(digests))

        cells = []
        for f in fonts:
            cells.append(
                '<td><img class="glyph-img" src="/fixed_axis_glyf_svg/%s/%s/U+%04X?v=%s"></td>'
                % (
                    _fax_html.escape(mode),
                    _fax_html.escape(f.name),
                    cp,
                    int(f.stat().st_mtime),
                )
            )

        diff_class = "ok" if unique_for_cp > 1 else "bad"

        rows.append(
            '<tr><td class="sticky char">%s</td><td class="sticky2 code">U+%04X<br><span class="%s">版本:%d</span></td>%s</tr>'
            % (_fax_html.escape(ch), cp, diff_class, unique_for_cp, "".join(cells))
        )

    html_doc = """
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>固定坐标系 glyf 真实预览</title>
<style>
body {
  margin: 0;
  background: #f5f6f8;
  color: #111;
  font-family: Arial, "Microsoft YaHei", sans-serif;
}
.page {
  max-width: 1900px;
  margin: 24px auto 50px;
  padding: 0 18px;
}
h1 {
  margin: 0 0 8px;
  font-size: 24px;
}
.sub {
  color: #555;
  line-height: 1.8;
  font-size: 14px;
  margin-bottom: 16px;
}
.card {
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 14px;
  padding: 16px;
  margin-bottom: 16px;
  box-shadow: 0 8px 24px rgba(0,0,0,.04);
}
.actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.actions a {
  display: inline-block;
  padding: 8px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  color: #111;
  text-decoration: none;
  background: #f3f4f6;
  font-weight: 700;
}
.table-wrap {
  overflow: auto;
  border: 1px solid #e5e7eb;
  border-radius: 14px;
  background: white;
}
table {
  border-collapse: collapse;
  min-width: 1700px;
  width: 100%;
}
th, td {
  border: 1px solid #e5e7eb;
  text-align: center;
  vertical-align: middle;
  padding: 6px;
}
th {
  position: sticky;
  top: 0;
  z-index: 10;
  background: #fafafa;
  font-size: 12px;
}
.file {
  color: #777;
  font-size: 10px;
  margin-top: 4px;
}
.sticky {
  position: sticky;
  left: 0;
  background: white;
  z-index: 8;
}
.sticky2 {
  position: sticky;
  left: 64px;
  background: white;
  z-index: 8;
}
.char {
  width: 64px;
  font-size: 22px;
}
.code {
  width: 100px;
  font-size: 12px;
  color: #555;
}
.glyph-img {
  width: 110px;
  height: 110px;
  display: block;
  margin: 0 auto;
}
.ok {
  color: #047857;
  font-weight: 800;
}
.bad {
  color: #b91c1c;
  font-weight: 800;
}
</style>
</head>
<body>
<div class="page">
  <h1>__MODE__｜固定坐标系 glyf 真实预览</h1>
  <div class="sub">
    这个页面把同一个字符在 Step01-Step20 中使用同一套坐标范围、同一缩放比例、同一基准位置显示。
    它不会把每个字形单独缩放居中，所以能看出真实的大小、位置、轮廓变化。
  </div>

  <div class="card">
    <b>检测目录：</b>__DIR__<br>
    <b>Step 数量：</b>__COUNT__<br>
    <b>字符范围：</b>__RANGE__<br>
    <b>说明：</b>左侧“版本:1”表示这个字符在 20 步中完全一样；版本大于 1 表示真实轮廓发生变化。
    <div class="actions">
      <a href="/fixed_axis_glyf_preview?mode=oyun&start=1820&end=1842">查看奥云固定坐标预览</a>
      <a href="/fixed_axis_glyf_preview?mode=menk&start=1820&end=1842">查看蒙科立固定坐标预览</a>
      <a href="/true_glyf_step_preview?mode=oyun&start=1820&end=1842">旧真实 glyf 预览</a>
      <a href="javascript:history.back()">返回原页面</a>
    </div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="sticky char">字符</th>
          <th class="sticky2 code">Unicode</th>
          __HEAD__
        </tr>
      </thead>
      <tbody>
        __ROWS__
      </tbody>
    </table>
  </div>
</div>
</body>
</html>
"""

    html_doc = (
        html_doc
        .replace("__MODE__", _fax_html.escape(mode_name))
        .replace("__DIR__", _fax_html.escape(str(_fax_mode_dir(mode))))
        .replace("__COUNT__", str(len(fonts)))
        .replace("__RANGE__", f"U+{start_cp:04X} - U+{end_cp:04X}")
        .replace("__HEAD__", "".join(head_cells))
        .replace("__ROWS__", "".join(rows))
    )

    return _fax_HTMLResponse(html_doc)

# ================= FIXED_AXIS_GLYF_PREVIEW_END =================


# ================= TARGETED_GB_MORPH_PATCH_START =================
# V2：只接管旧页面两个中国国标生成接口，不改页面、不改预览、不改下载。
# 接口：
# /api/foundry/oyun_gb/build
# /api/foundry/menk_gb/build

from pathlib import Path as _gbv2_Path
from functools import wraps as _gbv2_wraps
import asyncio as _gbv2_asyncio
import traceback as _gbv2_traceback

try:
    from fastapi import UploadFile as _gbv2_UploadFile
except Exception:
    _gbv2_UploadFile = None

from gb_morph_algorithm import generate_gb_morph_steps as _gbv2_generate_gb_morph_steps
from gb_morph_algorithm import zip_ttf_dir as _gbv2_zip_ttf_dir


def _gbv2_root():
    return _gbv2_Path(__file__).resolve().parent


async def _gbv2_save_uploads(args, kwargs):
    uploads = []

    def collect(v):
        if _gbv2_UploadFile is not None and isinstance(v, _gbv2_UploadFile):
            name = (v.filename or "").lower()
            if name.endswith((".ttf", ".otf")):
                uploads.append(v)
        elif isinstance(v, (list, tuple)):
            for x in v:
                collect(x)
        elif isinstance(v, dict):
            for x in v.values():
                collect(x)

    for a in args:
        collect(a)

    for v in kwargs.values():
        collect(v)

    saved = []

    if len(uploads) >= 2:
        tmp = _gbv2_root() / "_targeted_gb_morph_uploads"
        tmp.mkdir(exist_ok=True)

        for i, up in enumerate(uploads[:2], start=1):
            suffix = _gbv2_Path(up.filename or "font.ttf").suffix or ".ttf"
            dst = tmp / f"input_{i}{suffix}"

            data = await up.read()
            dst.write_bytes(data)

            try:
                await up.seek(0)
            except Exception:
                try:
                    up.file.seek(0)
                except Exception:
                    pass

            saved.append(dst)

    return saved


def _gbv2_find_latest_two_input_fonts():
    root = _gbv2_root()
    candidates = []

    for p in root.rglob("*.ttf"):
        low = str(p).lower()
        name = p.name.lower()

        if "step" in name:
            continue
        if "variable" in name:
            continue
        if "output/oyun_gb_ttf_steps" in low:
            continue
        if "output/menk_gb_ttf_steps" in low:
            continue
        if "sequence_variable" in low:
            continue
        if "glyph_blend" in low:
            continue
        if "_targeted_gb_morph_uploads" in low:
            candidates.append(p)
            continue

        # 只保留可能是用户上传或输入字体的文件
        if p.is_file():
            candidates.append(p)

    candidates = sorted(candidates, key=lambda x: x.stat().st_mtime, reverse=True)
    return candidates[:2]


def _gbv2_output_paths(mode):
    root = _gbv2_root()
    output_root = root / "output"
    output_root.mkdir(exist_ok=True)

    if mode == "menk":
        out_dir = output_root / "menk_gb_ttf_steps"
        prefix = "menk_gb_step"
        zip_paths = [
            output_root / "menk_gb_ttf_steps.zip",
            output_root / "menk_gb.zip",
        ]
    else:
        out_dir = output_root / "oyun_gb_ttf_steps"
        prefix = "oyun_gb_step"
        zip_paths = [
            output_root / "oyun_gb_ttf_steps.zip",
            output_root / "oyun_gb.zip",
        ]

    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir, prefix, zip_paths


def _gbv2_regenerate(mode, font_a, font_b):
    out_dir, prefix, zip_paths = _gbv2_output_paths(mode)

    marker = out_dir / "_targeted_patch_called.txt"
    marker.write_text(
        f"called mode={mode}\nfont_a={font_a}\nfont_b={font_b}\n",
        encoding="utf-8",
    )

    print(f"[TARGETED-GB-MORPH-V2] regenerate {mode}: {font_a} -> {font_b}")

    report = _gbv2_generate_gb_morph_steps(
        font_a_path=str(font_a),
        font_b_path=str(font_b),
        out_dir=str(out_dir),
        prefix=prefix,
        steps=20,
        points_per_contour=160,
    )

    for zp in zip_paths:
        try:
            _gbv2_zip_ttf_dir(out_dir, zp)
        except Exception as e:
            print(f"[TARGETED-GB-MORPH-V2][ZIP-WARN] {zp}: {e}")

    print(
        f"[TARGETED-GB-MORPH-V2] done {mode}: "
        f"generated={report['generated_glyphs']}, skipped={report['skipped_glyphs']}, "
        f"report={out_dir / 'gb_morph_report.json'}"
    )


def _gbv2_patch_route(path, mode):
    for route in app.router.routes:
        if getattr(route, "path", "") != path:
            continue

        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue

        if getattr(endpoint, "_gbv2_patched", False):
            print(f"[TARGETED-GB-MORPH-V2] already patched {path}")
            return

        if _gbv2_asyncio.iscoroutinefunction(endpoint):

            @_gbv2_wraps(endpoint)
            async def wrapper(*args, __endpoint=endpoint, __mode=mode, **kwargs):
                print(f"[TARGETED-GB-MORPH-V2] endpoint called: {path}")

                saved = await _gbv2_save_uploads(args, kwargs)

                result = await __endpoint(*args, **kwargs)

                fonts = saved if len(saved) >= 2 else _gbv2_find_latest_two_input_fonts()

                if len(fonts) >= 2:
                    try:
                        _gbv2_regenerate(__mode, fonts[0], fonts[1])
                    except Exception:
                        print(f"[TARGETED-GB-MORPH-V2][ERROR] {__mode}")
                        print(_gbv2_traceback.format_exc())
                else:
                    print(f"[TARGETED-GB-MORPH-V2][ERROR] {__mode}: no two input fonts found")

                return result

            new_endpoint = wrapper

        else:

            @_gbv2_wraps(endpoint)
            def wrapper(*args, __endpoint=endpoint, __mode=mode, **kwargs):
                print(f"[TARGETED-GB-MORPH-V2] endpoint called: {path}")

                result = __endpoint(*args, **kwargs)

                fonts = _gbv2_find_latest_two_input_fonts()

                if len(fonts) >= 2:
                    try:
                        _gbv2_regenerate(__mode, fonts[0], fonts[1])
                    except Exception:
                        print(f"[TARGETED-GB-MORPH-V2][ERROR] {__mode}")
                        print(_gbv2_traceback.format_exc())
                else:
                    print(f"[TARGETED-GB-MORPH-V2][ERROR] {__mode}: no two input fonts found")

                return result

            new_endpoint = wrapper

        new_endpoint._gbv2_patched = True
        route.endpoint = new_endpoint

        if hasattr(route, "dependant"):
            route.dependant.call = new_endpoint

        print(f"[TARGETED-GB-MORPH-V2] patched {path} -> mode={mode}")
        return

    print(f"[TARGETED-GB-MORPH-V2][WARN] route not found: {path}")


# DISABLED: scripts/build_oyun_gb_version.py 已经完成最终强制插值；
# 这里不能再二次调用 _gbv2_regenerate，否则会覆盖 FORCE-GB-MULTI 的结果。
# _gbv2_patch_route("/api/foundry/oyun_gb/build", "oyun")
# _gbv2_patch_route("/api/foundry/menk_gb/build", "menk")

# ================= TARGETED_GB_MORPH_PATCH_END =================

# ================= FINAL_OYUN_REAL_PREVIEW_PATCH_START =================
# 真实结果预览：
# 1. 页面直接加载 output/oyun_gb_ttf_steps/oyun_gb_step_XX.ttf
# 2. SVG 轮廓直接从生成后的 TTF 中读取 glyf 表
# 3. 不再使用演示模板字体

from pathlib import Path as _op_Path
from functools import lru_cache as _op_lru_cache
import csv as _op_csv
import json as _op_json
import html as _op_html
import urllib.parse as _op_urlparse

from fastapi import Request as _op_Request
from fastapi.responses import HTMLResponse as _op_HTMLResponse
from fastapi.responses import Response as _op_Response


def _op_root():
    return _op_Path(__file__).resolve().parent


def _op_out_dir():
    return _op_root() / "output" / "oyun_gb_ttf_steps"


def _op_font_url(filename):
    p = _op_out_dir() / filename
    v = int(p.stat().st_mtime) if p.exists() else 0
    return f"/api/foundry/oyun_gb/ttf/{_op_urlparse.quote(filename)}?v={v}"


@_op_lru_cache(maxsize=64)
def _op_load_font_cached(path_str, mtime):
    from fontTools.ttLib import TTFont
    return TTFont(path_str, recalcBBoxes=True, recalcTimestamp=False)


def _op_load_generated_font(step: int):
    p = _op_out_dir() / f"oyun_gb_step_{step:02d}.ttf"
    if not p.exists():
        return None, p
    return _op_load_font_cached(str(p), int(p.stat().st_mtime)), p


def _op_glyph_svg_from_font(step: int, glyph_name: str):
    font, p = _op_load_generated_font(step)
    if font is None:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"><text x="10" y="80">no font</text></svg>'

    glyph_name = _op_urlparse.unquote(glyph_name)

    if glyph_name not in font.getGlyphOrder():
        msg = _op_html.escape(glyph_name)
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"><text x="10" y="80">missing {msg}</text></svg>'

    try:
        from fontTools.pens.svgPathPen import SVGPathPen

        glyph_set = font.getGlyphSet()
        pen = SVGPathPen(glyph_set)
        glyph_set[glyph_name].draw(pen)
        d = pen.getCommands()

        if not d.strip():
            return '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"><rect width="100%" height="100%" fill="#f7f7f7"/><text x="12" y="82" font-size="12" fill="#777">empty glyph</text></svg>'

        glyf = font["glyf"]
        g = glyf[glyph_name]

        try:
            g.recalcBounds(glyf)
            x_min = getattr(g, "xMin", 0)
            y_min = getattr(g, "yMin", 0)
            x_max = getattr(g, "xMax", 1000)
            y_max = getattr(g, "yMax", 1000)
        except Exception:
            x_min, y_min, x_max, y_max = 0, 0, 1000, 1000

        w = max(1, x_max - x_min)
        h = max(1, y_max - y_min)

        pad_x = max(40, int(w * 0.08))
        pad_y = max(40, int(h * 0.08))

        view_x = x_min - pad_x
        view_y = -y_max - pad_y
        view_w = w + pad_x * 2
        view_h = h + pad_y * 2

        return f'''
<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_x} {view_y} {view_w} {view_h}" width="160" height="160">
  <rect x="{view_x}" y="{view_y}" width="{view_w}" height="{view_h}" fill="#fff"/>
  <g transform="scale(1,-1)">
    <path d="{_op_html.escape(d)}" fill="#111"/>
  </g>
</svg>
'''
    except Exception as e:
        msg = _op_html.escape(str(e))
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"><text x="10" y="80">{msg}</text></svg>'


@app.get("/api/foundry/oyun_gb/glyph_svg/{step:int}/{glyph_name:path}")
async def _op_oyun_glyph_svg(step: int, glyph_name: str):
    svg = _op_glyph_svg_from_font(step, glyph_name)
    return _op_Response(content=svg, media_type="image/svg+xml")


def _op_read_report():
    p = _op_out_dir() / "gb_morph_report.json"
    if not p.exists():
        return {}
    try:
        return _op_json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _op_read_build_report():
    p = _op_out_dir() / "build_report.csv"
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            return list(_op_csv.DictReader(f))
    except Exception:
        return []


def _op_read_prepared_items(limit=120):
    data = _op_read_report()
    items = data.get("prepared", []) or []
    return items[:limit]


async def _op_real_oyun_preview(request: _op_Request = None):
    out_dir = _op_out_dir()
    fonts = sorted(out_dir.glob("oyun_gb_step_*.ttf"))

    report = _op_read_report()
    build_rows = _op_read_build_report()

    try:
        if request is not None:
            limit = int(request.query_params.get("limit", "120"))
        else:
            limit = 120
    except Exception:
        limit = 120
    limit = max(10, min(limit, 300))

    prepared = _op_read_prepared_items(limit)

    sample_text = (
        "ᠠ ᠡ ᠢ ᠣ ᠤ ᠥ ᠦ ᠨ ᠩ ᠪ ᠫ ᠬ ᠭ ᠮ ᠯ ᠰ ᠱ ᠲ ᠳ ᠴ ᠵ ᠶ ᠷ ᠸ ᠹ ᠺ ᠻ ᠼ ᠽ ᠾ"
    )

    css_font_faces = []
    cards = []

    for i in range(1, 21):
        filename = f"oyun_gb_step_{i:02d}.ttf"
        p = out_dir / filename
        if not p.exists():
            continue

        fam = f"OyunGBStep{i:02d}"
        css_font_faces.append(
            f"""
@font-face {{
  font-family: '{fam}';
  src: url('{_op_font_url(filename)}') format('truetype');
  font-display: swap;
}}
"""
        )

        cards.append(f"""
<div class="font-card">
  <div class="step-title">Step {i:02d}</div>
  <div class="mongolian-sample" style="font-family:'{fam}';">{_op_html.escape(sample_text)}</div>
  <div class="file-name">{_op_html.escape(filename)}</div>
</div>
""")

    summary_keys = [
        ("algorithm", "算法"),
        ("runtime_rows", "国标 runtime 项"),
        ("handled_runtime_rows", "已处理 runtime 项"),
        ("expanded_multi_rows", "已展开多 glyph 项"),
        ("generated_unique_glyph_pairs", "唯一 glyph 插值对"),
        ("duplicate_pairs_skipped", "重复 glyph 对"),
        ("skipped_rows_or_pairs", "跳过项"),
    ]

    summary_html = []
    for key, label in summary_keys:
        value = report.get(key, "")
        summary_html.append(f"""
<div class="stat">
  <div class="stat-label">{_op_html.escape(label)}</div>
  <div class="stat-value">{_op_html.escape(str(value))}</div>
</div>
""")

    build_table = ""
    if build_rows:
        last = build_rows[0]
        build_table = f"""
<table class="mini-table">
  <tr><th>runtime_items</th><td>{_op_html.escape(str(last.get("runtime_items", "")))}</td></tr>
  <tr><th>handled_runtime_rows</th><td>{_op_html.escape(str(last.get("handled_runtime_rows", "")))}</td></tr>
  <tr><th>interpolated_unique_glyph_pairs</th><td>{_op_html.escape(str(last.get("interpolated_unique_glyph_pairs", "")))}</td></tr>
  <tr><th>expanded_multi_rows</th><td>{_op_html.escape(str(last.get("expanded_multi_rows", "")))}</td></tr>
  <tr><th>skipped_incompatible</th><td>{_op_html.escape(str(last.get("skipped_incompatible", "")))}</td></tr>
</table>
"""

    glyph_rows = []

    for item in prepared:
        glyph_a = str(item.get("glyph_a", ""))
        glyph_b = str(item.get("glyph_b", ""))
        runtime_id = str(item.get("runtime_id", ""))
        group = str(item.get("display_group", ""))
        gb_code = str(item.get("gb_code", ""))

        glyph_q = _op_urlparse.quote(glyph_a, safe="")

        glyph_rows.append(f"""
<tr>
  <td class="meta">
    <div><b>{_op_html.escape(runtime_id)}</b></div>
    <div>{_op_html.escape(group)}</div>
    <div>GB: {_op_html.escape(gb_code)}</div>
    <div>A: {_op_html.escape(glyph_a)}</div>
    <div>B: {_op_html.escape(glyph_b)}</div>
  </td>
  <td><img class="glyph-svg" src="/api/foundry/oyun_gb/glyph_svg/1/{glyph_q}"></td>
  <td><img class="glyph-svg" src="/api/foundry/oyun_gb/glyph_svg/10/{glyph_q}"></td>
  <td><img class="glyph-svg" src="/api/foundry/oyun_gb/glyph_svg/20/{glyph_q}"></td>
</tr>
""")

    html_doc = f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>奥云国标版真实生成预览</title>
<style>
{''.join(css_font_faces)}

body {{
  margin: 0;
  padding: 28px;
  background: #f4f5f7;
  color: #111;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}}

h1 {{
  margin: 0 0 10px;
  font-size: 26px;
}}

.notice {{
  padding: 12px 14px;
  background: #fff8d8;
  border: 1px solid #eadb91;
  border-radius: 10px;
  margin: 16px 0 22px;
  line-height: 1.7;
}}

.stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin: 18px 0;
}}

.stat {{
  background: white;
  border-radius: 12px;
  padding: 14px;
  box-shadow: 0 1px 4px rgba(0,0,0,.08);
}}

.stat-label {{
  font-size: 12px;
  color: #666;
}}

.stat-value {{
  font-size: 20px;
  font-weight: 700;
  margin-top: 6px;
  word-break: break-all;
}}

.actions {{
  display: flex;
  gap: 12px;
  margin: 18px 0;
  flex-wrap: wrap;
}}

.actions a {{
  display: inline-block;
  padding: 10px 14px;
  background: #111;
  color: white;
  border-radius: 8px;
  text-decoration: none;
}}

.section {{
  margin-top: 28px;
}}

.font-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 14px;
}}

.font-card {{
  background: white;
  border-radius: 14px;
  padding: 16px;
  box-shadow: 0 1px 4px rgba(0,0,0,.08);
}}

.step-title {{
  font-weight: 700;
  margin-bottom: 10px;
}}

.mongolian-sample {{
  min-height: 160px;
  font-size: 34px;
  line-height: 1.6;
  writing-mode: vertical-lr;
  text-orientation: mixed;
  border: 1px solid #eee;
  border-radius: 10px;
  padding: 10px;
  overflow: auto;
  background: #fafafa;
}}

.file-name {{
  margin-top: 10px;
  font-size: 12px;
  color: #666;
}}

.mini-table {{
  border-collapse: collapse;
  background: white;
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 1px 4px rgba(0,0,0,.08);
}}

.mini-table th,
.mini-table td {{
  padding: 10px 14px;
  border-bottom: 1px solid #eee;
  text-align: left;
}}

.glyph-table {{
  width: 100%;
  border-collapse: collapse;
  background: white;
  box-shadow: 0 1px 4px rgba(0,0,0,.08);
}}

.glyph-table th,
.glyph-table td {{
  border: 1px solid #eee;
  padding: 10px;
  vertical-align: middle;
}}

.glyph-table th {{
  background: #f7f7f7;
  position: sticky;
  top: 0;
  z-index: 2;
}}

.meta {{
  font-size: 12px;
  line-height: 1.6;
  width: 260px;
}}

.glyph-svg {{
  width: 160px;
  height: 160px;
  border: 1px solid #eee;
  background: white;
}}

.small {{
  color: #666;
  font-size: 13px;
  line-height: 1.7;
}}
</style>
</head>
<body>

<h1>奥云｜中国国标版真实生成预览</h1>

<div class="notice">
  当前页面直接读取 <b>output/oyun_gb_ttf_steps</b> 中生成后的 TTF。<br>
  上方字体预览使用真实生成的 step 字体；下方 SVG 轮廓预览直接从生成后的 TTF 的 glyf 表提取，不是演示图。
</div>

<div class="actions">
  <a href="/api/foundry/oyun_gb/download_zip">下载生成字体 ZIP</a>
  <a href="/oyun_gb_preview?limit=300">显示更多 glyph 轮廓</a>
  <a href="/">返回首页</a>
</div>

<div class="stats">
  {''.join(summary_html)}
</div>

<div class="section">
  <h2>生成统计</h2>
  {build_table}
</div>

<div class="section">
  <h2>20 步 TTF 字体预览</h2>
  <p class="small">这里使用 @font-face 直接加载真实生成的 oyun_gb_step_01.ttf 到 oyun_gb_step_20.ttf。</p>
  <div class="font-grid">
    {''.join(cards)}
  </div>
</div>

<div class="section">
  <h2>真实 glyph 轮廓预览：Step 01 / Step 10 / Step 20</h2>
  <p class="small">
    这里不依赖浏览器蒙古文 shaping，而是直接读取生成后 TTF 的 glyf 轮廓，因此能看到真实插值结果。
    当前显示前 {limit} 个 prepared glyph；可用 <code>?limit=300</code> 增加数量。
  </p>

  <table class="glyph-table">
    <thead>
      <tr>
        <th>国标 / glyph 信息</th>
        <th>Step 01</th>
        <th>Step 10</th>
        <th>Step 20</th>
      </tr>
    </thead>
    <tbody>
      {''.join(glyph_rows)}
    </tbody>
  </table>
</div>

</body>
</html>
"""

    return _op_HTMLResponse(html_doc)


def _op_patch_preview_route():
    target = "/oyun_gb_preview"

    for route in app.router.routes:
        if getattr(route, "path", "") == target:
            route.endpoint = _op_real_oyun_preview
            if hasattr(route, "dependant"):
                route.dependant.call = _op_real_oyun_preview
            print("[FINAL-OYUN-PREVIEW] patched /oyun_gb_preview -> real generated preview")
            return

    app.add_api_route(target, _op_real_oyun_preview, methods=["GET"])
    print("[FINAL-OYUN-PREVIEW] added /oyun_gb_preview -> real generated preview")


_op_patch_preview_route()

# ================= FINAL_OYUN_REAL_PREVIEW_PATCH_END =================

# ================= FINAL_OYUN_PREVIEW_DIRECT_INTERCEPT_START =================
# 修复 /oyun_gb_preview Internal Server Error：
# 不再依赖旧 route 的 FastAPI 参数解析，直接在 middleware 中拦截该路径。

@app.middleware("http")
async def _final_oyun_preview_direct_intercept(request, call_next):
    if request.url.path == "/oyun_gb_preview":
        return await _op_real_oyun_preview(request)
    return await call_next(request)

print("[FINAL-OYUN-PREVIEW] direct middleware intercept installed")
# ================= FINAL_OYUN_PREVIEW_DIRECT_INTERCEPT_END =================

# ================= FINAL_OYUN_GLYPH_MATRIX_PREVIEW_START =================
# 将 /oyun_gb_preview 改为“每个生成字形 × 20步”矩阵预览。
# 每一行是一个实际生成的唯一 glyph pair，每一列是 step_01 到 step_20。

async def _op_real_oyun_preview_matrix(request):
    import json
    import html as _html
    import urllib.parse as _urlparse
    from pathlib import Path

    out_dir = _op_out_dir()
    report = _op_read_report()
    build_rows = _op_read_build_report()

    prepared = report.get("prepared", []) or []

    try:
        limit = int(request.query_params.get("limit", "200"))
    except Exception:
        limit = 200

    # 默认显示全部 127 个；上限给到 500，防止页面过大。
    limit = max(1, min(limit, 500))
    prepared = prepared[:limit]

    try:
        report_mtime = int((out_dir / "gb_morph_report.json").stat().st_mtime)
    except Exception:
        report_mtime = 0

    algorithm = report.get("algorithm", "")
    runtime_rows = report.get("runtime_rows", "")
    handled_runtime_rows = report.get("handled_runtime_rows", "")
    expanded_multi_rows = report.get("expanded_multi_rows", "")
    generated_unique_glyph_pairs = report.get("generated_unique_glyph_pairs", "")
    duplicate_pairs_skipped = report.get("duplicate_pairs_skipped", "")
    skipped_rows_or_pairs = report.get("skipped_rows_or_pairs", "")

    last = build_rows[0] if build_rows else {}

    summary_html = f"""
<div class="stats">
  <div class="stat"><div class="label">国标 runtime 项</div><div class="value">{_html.escape(str(runtime_rows))}</div></div>
  <div class="stat"><div class="label">已处理 runtime 项</div><div class="value">{_html.escape(str(handled_runtime_rows))}</div></div>
  <div class="stat"><div class="label">实际生成唯一 glyph</div><div class="value">{_html.escape(str(generated_unique_glyph_pairs))}</div></div>
  <div class="stat"><div class="label">已展开多 glyph 项</div><div class="value">{_html.escape(str(expanded_multi_rows))}</div></div>
  <div class="stat"><div class="label">重复 glyph 对</div><div class="value">{_html.escape(str(duplicate_pairs_skipped))}</div></div>
  <div class="stat"><div class="label">跳过项</div><div class="value">{_html.escape(str(skipped_rows_or_pairs))}</div></div>
</div>
"""

    head_cells = ['<th class="sticky-col">字形信息</th>']
    for step in range(1, 21):
        head_cells.append(f'<th>Step {step:02d}</th>')

    body_rows = []

    for idx, item in enumerate(prepared, start=1):
        glyph_a = str(item.get("glyph_a", ""))
        glyph_b = str(item.get("glyph_b", ""))
        runtime_id = str(item.get("runtime_id", ""))
        display_group = str(item.get("display_group", ""))
        gb_code = str(item.get("gb_code", ""))
        base_unicode = str(item.get("base_unicode", ""))
        contour_count = str(item.get("contour_count", ""))

        glyph_q = _urlparse.quote(glyph_a, safe="")

        info = f"""
<td class="sticky-col meta">
  <div class="idx">#{idx}</div>
  <div><b>{_html.escape(runtime_id)}</b></div>
  <div>{_html.escape(display_group)}</div>
  <div>GB: {_html.escape(gb_code)}</div>
  <div>Unicode: {_html.escape(base_unicode)}</div>
  <div>A: {_html.escape(glyph_a)}</div>
  <div>B: {_html.escape(glyph_b)}</div>
  <div>contours: {_html.escape(contour_count)}</div>
</td>
"""

        cells = [info]

        for step in range(1, 21):
            src = f"/api/foundry/oyun_gb/glyph_svg/{step}/{glyph_q}?v={report_mtime}"
            cells.append(f"""
<td class="glyph-cell">
  <img class="glyph-img" src="{src}" loading="lazy">
</td>
""")

        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    html_doc = f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>奥云国标版｜每字 20 步真实预览</title>
<style>
body {{
  margin: 0;
  padding: 20px;
  background: #f4f5f7;
  color: #111;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}}

h1 {{
  margin: 0 0 10px;
  font-size: 24px;
}}

.notice {{
  background: #fff8d8;
  border: 1px solid #eadb91;
  border-radius: 10px;
  padding: 12px 14px;
  line-height: 1.7;
  margin: 14px 0 18px;
}}

.actions {{
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin: 14px 0 18px;
}}

.actions a {{
  display: inline-block;
  background: #111;
  color: #fff;
  padding: 9px 13px;
  border-radius: 8px;
  text-decoration: none;
  font-size: 14px;
}}

.stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin: 16px 0 22px;
}}

.stat {{
  background: white;
  border-radius: 12px;
  padding: 14px;
  box-shadow: 0 1px 4px rgba(0,0,0,.08);
}}

.label {{
  font-size: 12px;
  color: #666;
}}

.value {{
  margin-top: 5px;
  font-size: 22px;
  font-weight: 700;
}}

.table-wrap {{
  overflow: auto;
  max-height: calc(100vh - 260px);
  background: white;
  border-radius: 12px;
  box-shadow: 0 1px 4px rgba(0,0,0,.1);
}}

.matrix {{
  border-collapse: collapse;
  min-width: 2600px;
  width: max-content;
}}

.matrix th,
.matrix td {{
  border: 1px solid #e8e8e8;
  padding: 8px;
  vertical-align: middle;
  background: white;
}}

.matrix th {{
  position: sticky;
  top: 0;
  z-index: 5;
  background: #f0f0f0;
  font-size: 13px;
}}

.sticky-col {{
  position: sticky;
  left: 0;
  z-index: 6;
  background: #fff;
  min-width: 260px;
  max-width: 260px;
}}

th.sticky-col {{
  z-index: 8;
  background: #eaeaea;
}}

.meta {{
  font-size: 12px;
  line-height: 1.55;
  word-break: break-all;
}}

.idx {{
  font-size: 16px;
  font-weight: 800;
  margin-bottom: 4px;
}}

.glyph-cell {{
  width: 112px;
  height: 122px;
  text-align: center;
}}

.glyph-img {{
  width: 108px;
  height: 108px;
  object-fit: contain;
  background: #fff;
}}

.small {{
  color: #666;
  font-size: 13px;
  line-height: 1.6;
}}
{_foundry_vf_panel_css()}
</style>
</head>
<body>

<h1>奥云｜中国国标版真实生成预览：每个字形的 1–20 步</h1>

<div class="notice">
  当前页面直接读取最终生成后的 <b>output/oyun_gb_ttf_steps</b> 中的 20 个 TTF。<br>
  每一行是一个实际生成的唯一 glyph 字形，每一列是 Step 01 到 Step 20。这里显示的是 glyf 轮廓，不是演示图。
</div>

<div class="actions">
  <a href="/api/foundry/oyun_gb/download_zip">下载生成字体 ZIP</a>
  <a href="/oyun_gb_preview?limit=127">显示全部 127 个唯一字形</a>
  <a href="/oyun_gb_preview?limit=300">显示更多</a>
  <a href="/">返回首页</a>
</div>

{summary_html}

<p class="small">
算法：<b>{_html.escape(str(algorithm))}</b><br>
当前显示：{len(prepared)} 个唯一 glyph 字形。实际唯一插值 glyph 数：{_html.escape(str(generated_unique_glyph_pairs))}。
</p>

<div class="table-wrap">
<table class="matrix">
  <thead>
    <tr>
      {''.join(head_cells)}
    </tr>
  </thead>
  <tbody>
    {''.join(body_rows)}
  </tbody>
</table>
</div>

</body>
</html>
"""

    return _op_HTMLResponse(html_doc)


@app.middleware("http")
async def _final_oyun_preview_matrix_intercept(request, call_next):
    if request.url.path == "/oyun_gb_preview":
        return await _op_real_oyun_preview_matrix(request)
    return await call_next(request)

print("[FINAL-OYUN-PREVIEW-MATRIX] /oyun_gb_preview now shows every glyph across 20 steps")

# ================= FINAL_OYUN_GLYPH_MATRIX_PREVIEW_END =================

# ================= FINAL_DUAL_GB_PREVIEW_AND_CSV_START =================
# 统一真实预览：
# - /oyun_gb_preview：奥云真实矩阵预览
# - /menk_gb_preview：蒙科立真实矩阵预览
# - /api/foundry/oyun_gb/glyph_table.csv：下载奥云有效 glyph 清单
# - /api/foundry/menk_gb/glyph_table.csv：下载蒙科立有效 glyph 清单
# - /api/foundry/menk_gb/glyph_svg/{step}/{glyph_name}：蒙科立真实 glyph SVG

from pathlib import Path as _dual_Path
from functools import lru_cache as _dual_lru_cache
import csv as _dual_csv
import json as _dual_json
import html as _dual_html
import urllib.parse as _dual_urlparse
import io as _dual_io

from fastapi.responses import HTMLResponse as _dual_HTMLResponse
from fastapi.responses import Response as _dual_Response


def _dual_root():
    return _dual_Path(__file__).resolve().parent


def _dual_mode_info(mode: str):
    if mode == "menk":
        return {
            "mode": "menk",
            "title": "蒙科立｜中国国标版",
            "out_dir": _dual_root() / "output" / "menk_gb_ttf_steps",
            "prefix": "menk_gb_step",
            "download_zip": "/api/foundry/menk_gb/download_zip",
            "csv_url": "/api/foundry/menk_gb/glyph_table.csv",
            "glyph_svg_base": "/api/foundry/menk_gb/glyph_svg",
            "preview_url": "/menk_gb_preview",
        }

    return {
        "mode": "oyun",
        "title": "奥云｜中国国标版",
        "out_dir": _dual_root() / "output" / "oyun_gb_ttf_steps",
        "prefix": "oyun_gb_step",
        "download_zip": "/api/foundry/oyun_gb/download_zip",
        "csv_url": "/api/foundry/oyun_gb/glyph_table.csv",
        "glyph_svg_base": "/api/foundry/oyun_gb/glyph_svg",
        "preview_url": "/oyun_gb_preview",
    }


def _dual_report_path(mode: str):
    return _dual_mode_info(mode)["out_dir"] / "gb_morph_report.json"


def _dual_build_report_path(mode: str):
    info = _dual_mode_info(mode)
    return info["out_dir"] / "build_report.csv"


def _dual_read_report(mode: str):
    p = _dual_report_path(mode)
    if not p.exists():
        return {}
    try:
        return _dual_json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _dual_read_build_report(mode: str):
    p = _dual_build_report_path(mode)
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            return list(_dual_csv.DictReader(f))
    except Exception:
        return []


@_dual_lru_cache(maxsize=128)
def _dual_load_font_cached(path_str, mtime):
    from fontTools.ttLib import TTFont
    return TTFont(path_str, recalcBBoxes=True, recalcTimestamp=False)


def _dual_load_generated_font(mode: str, step: int):
    info = _dual_mode_info(mode)
    p = info["out_dir"] / f"{info['prefix']}_{step:02d}.ttf"
    if not p.exists():
        return None, p
    return _dual_load_font_cached(str(p), int(p.stat().st_mtime)), p


def _dual_glyph_svg_from_font(mode: str, step: int, glyph_name: str):
    font, p = _dual_load_generated_font(mode, step)
    if font is None:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"><text x="8" y="60">no font</text></svg>'

    glyph_name = _dual_urlparse.unquote(glyph_name)

    if glyph_name not in font.getGlyphOrder():
        msg = _dual_html.escape(glyph_name)
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"><text x="8" y="60">missing {msg}</text></svg>'

    try:
        from fontTools.pens.svgPathPen import SVGPathPen

        glyph_set = font.getGlyphSet()
        pen = SVGPathPen(glyph_set)
        glyph_set[glyph_name].draw(pen)
        d = pen.getCommands()

        if not d.strip():
            return '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"><rect width="100%" height="100%" fill="#fafafa"/><text x="8" y="62" font-size="11" fill="#777">empty</text></svg>'

        glyf = font["glyf"]
        g = glyf[glyph_name]

        try:
            g.recalcBounds(glyf)
            x_min = getattr(g, "xMin", 0)
            y_min = getattr(g, "yMin", 0)
            x_max = getattr(g, "xMax", 1000)
            y_max = getattr(g, "yMax", 1000)
        except Exception:
            x_min, y_min, x_max, y_max = 0, 0, 1000, 1000

        w = max(1, x_max - x_min)
        h = max(1, y_max - y_min)

        pad_x = max(40, int(w * 0.08))
        pad_y = max(40, int(h * 0.08))

        view_x = x_min - pad_x
        view_y = -y_max - pad_y
        view_w = w + pad_x * 2
        view_h = h + pad_y * 2

        return f'''
<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_x} {view_y} {view_w} {view_h}" width="120" height="120">
  <rect x="{view_x}" y="{view_y}" width="{view_w}" height="{view_h}" fill="#fff"/>
  <g transform="scale(1,-1)">
    <path d="{_dual_html.escape(d)}" fill="#111"/>
  </g>
</svg>
'''
    except Exception as e:
        msg = _dual_html.escape(str(e))
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"><text x="8" y="60">{msg}</text></svg>'


@app.get("/api/foundry/menk_gb/glyph_svg/{step:int}/{glyph_name:path}")
async def _dual_menk_glyph_svg(step: int, glyph_name: str):
    svg = _dual_glyph_svg_from_font("menk", step, glyph_name)
    return _dual_Response(content=svg, media_type="image/svg+xml")


@app.get("/api/foundry/oyun_gb/glyph_table.csv")
async def _dual_oyun_glyph_table_csv():
    return _dual_glyph_table_csv_response("oyun")


@app.get("/api/foundry/menk_gb/glyph_table.csv")
async def _dual_menk_glyph_table_csv():
    return _dual_glyph_table_csv_response("menk")


def _dual_glyph_table_csv_response(mode: str):
    info = _dual_mode_info(mode)
    report = _dual_read_report(mode)
    prepared = report.get("prepared", []) or []

    fields = [
        "index",
        "runtime_id",
        "display_group",
        "gb_code",
        "base_unicode",
        "glyph_a",
        "glyph_b",
        "contour_count",
    ]

    buf = _dual_io.StringIO()
    buf.write("\ufeff")

    writer = _dual_csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()

    for i, item in enumerate(prepared, start=1):
        writer.writerow({
            "index": i,
            "runtime_id": item.get("runtime_id", ""),
            "display_group": item.get("display_group", ""),
            "gb_code": item.get("gb_code", ""),
            "base_unicode": item.get("base_unicode", ""),
            "glyph_a": item.get("glyph_a", ""),
            "glyph_b": item.get("glyph_b", ""),
            "contour_count": item.get("contour_count", ""),
        })

    filename = f"{info['mode']}_gb_valid_glyph_table.csv"

    return _dual_Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )


def _foundry_vf_step_no(path):
    import re as _fvf_re
    m = _fvf_re.search(r"step[_\-]?0*(\d+)", path.name, _fvf_re.I)
    return int(m.group(1)) if m else 999999


def _foundry_vf_complete_step_files(mode: str):
    mode = (mode or "oyun").strip().lower()
    root = _dual_root()
    if mode == "menk":
        out_dir = root / "output" / "menk_gb_ttf_steps"
        pattern = "complete_menk_gb_step_*.ttf"
    else:
        out_dir = root / "output" / "oyun_gb_ttf_steps"
        pattern = "complete_oyun_gb_step_*.ttf"
    files = [p for p in out_dir.glob(pattern) if p.is_file()]
    return sorted(files, key=_foundry_vf_step_no)


def _foundry_vf_preview_panel(mode: str):
    mode = "menk" if (mode or "").lower().strip() == "menk" else "oyun"
    title = "蒙科立｜中国国标版" if mode == "menk" else "奥云｜中国国标版"
    files = _foundry_vf_complete_step_files(mode)
    count = len(files)
    variable_name = "Menk GB Real Variable" if mode == "menk" else "Oyun GB Real Variable"
    hidden = "\n".join(
        f'<input type="hidden" name="selected_fonts" value="{_dual_html.escape(p.name)}">'
        for p in files
    )
    if count >= 2:
        action_html = f"""
        <form method="post" action="/generate_real_variable_font" target="_blank" class="fvf-form">
          {hidden}
          <input type="text" name="variable_name" value="{_dual_html.escape(variable_name)}" aria-label="可变字体名称">
          <button type="submit">基于当前 {count} 个 complete TTF 生成真实可变字体</button>
        </form>
        """
        state = (
            f"已识别当前 {count} 个 complete step master。提交后会检查轮廓数量、点数、点序、"
            "on-curve/off-curve 结构，并验证生成文件包含 fvar/gvar 且 MORF 实例化后坐标真实变化。"
        )
    else:
        action_html = """
        <div class="fvf-disabled">当前 complete step TTF 少于 2 个，暂不能生成真实可变字体。</div>
        """
        state = "请先完成字体公司规则生成，至少需要 2 个 complete TTF master。"

    return f"""
<section class="foundry-vf-panel">
  <div class="fvf-head">
    <div>
      <h2>下一步：生成真实可变字体</h2>
      <p>基于本页刚生成的 { _dual_html.escape(title) } complete TTF 构建 MORF 轴 Variable Font，不是静态帧切换演示。</p>
    </div>
  </div>
  {action_html}
  <div class="fvf-note">{_dual_html.escape(state)}</div>
</section>
"""


def _foundry_vf_panel_css():
    return """
.foundry-vf-panel {
  background: #fff;
  border: 1px solid #d9e1ea;
  border-radius: 12px;
  padding: 14px;
  margin: 14px 0 18px;
  box-shadow: 0 1px 4px rgba(0,0,0,.08);
}
.fvf-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  flex-wrap: wrap;
}
.foundry-vf-panel h2 {
  margin: 0 0 6px;
  font-size: 18px;
}
.foundry-vf-panel p {
  margin: 0;
  color: #4b5563;
  line-height: 1.6;
}
.fvf-form {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) auto;
  gap: 10px;
  margin-top: 12px;
}
.fvf-form input[type="text"] {
  width: 100%;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  padding: 9px 11px;
}
.fvf-form button,
.fvf-workbench {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 8px;
  background: #111827;
  color: #fff;
  padding: 9px 13px;
  font-size: 13px;
  font-weight: 800;
  text-decoration: none;
  cursor: pointer;
}
.fvf-workbench {
  background: #2563eb;
}
.fvf-note,
.fvf-disabled {
  margin-top: 10px;
  color: #64748b;
  font-size: 12px;
  line-height: 1.6;
}
.fvf-disabled {
  color: #9a3412;
  background: #fff7ed;
  border: 1px solid #fed7aa;
  border-radius: 8px;
  padding: 10px;
}
@media (max-width: 760px) {
  .fvf-form {
    grid-template-columns: 1fr;
  }
}
"""


async def _dual_real_matrix_preview(request, mode: str):
    info = _dual_mode_info(mode)
    report = _dual_read_report(mode)
    build_rows = _dual_read_build_report(mode)

    prepared_all = report.get("prepared", []) or []

    try:
        limit = int(request.query_params.get("limit", "500"))
    except Exception:
        limit = 500

    limit = max(1, min(limit, 1000))
    prepared = prepared_all[:limit]

    try:
        report_mtime = int(_dual_report_path(mode).stat().st_mtime)
    except Exception:
        report_mtime = 0

    algorithm = report.get("algorithm", "")
    runtime_rows = report.get("runtime_rows", "")
    handled_runtime_rows = report.get("handled_runtime_rows", "")
    expanded_multi_rows = report.get("expanded_multi_rows", "")
    generated_unique_glyph_pairs = report.get("generated_unique_glyph_pairs", "")
    duplicate_pairs_skipped = report.get("duplicate_pairs_skipped", "")
    skipped_rows_or_pairs = report.get("skipped_rows_or_pairs", "")

    summary_html = f"""
<div class="stats">
  <div class="stat"><div class="label">国标 runtime 项</div><div class="value">{_dual_html.escape(str(runtime_rows))}</div></div>
  <div class="stat"><div class="label">已处理 runtime 项</div><div class="value">{_dual_html.escape(str(handled_runtime_rows))}</div></div>
  <div class="stat"><div class="label">实际有效唯一 glyph</div><div class="value">{_dual_html.escape(str(generated_unique_glyph_pairs))}</div></div>
  <div class="stat"><div class="label">已展开多 glyph 项</div><div class="value">{_dual_html.escape(str(expanded_multi_rows))}</div></div>
  <div class="stat"><div class="label">重复 glyph 对</div><div class="value">{_dual_html.escape(str(duplicate_pairs_skipped))}</div></div>
  <div class="stat"><div class="label">跳过项</div><div class="value">{_dual_html.escape(str(skipped_rows_or_pairs))}</div></div>
</div>
"""

    head_cells = ['<th class="sticky-col">字形信息</th>']
    for step in range(1, 21):
        head_cells.append(f'<th>Step {step:02d}</th>')

    body_rows = []

    for idx, item in enumerate(prepared, start=1):
        glyph_a = str(item.get("glyph_a", ""))
        glyph_b = str(item.get("glyph_b", ""))
        runtime_id = str(item.get("runtime_id", ""))
        display_group = str(item.get("display_group", ""))
        gb_code = str(item.get("gb_code", ""))
        base_unicode = str(item.get("base_unicode", ""))
        contour_count = str(item.get("contour_count", ""))

        glyph_q = _dual_urlparse.quote(glyph_a, safe="")

        info_cell = f"""
<td class="sticky-col meta">
  <div class="idx">#{idx}</div>
  <div><b>{_dual_html.escape(runtime_id)}</b></div>
  <div>{_dual_html.escape(display_group)}</div>
  <div>GB: {_dual_html.escape(gb_code)}</div>
  <div>Unicode: {_dual_html.escape(base_unicode)}</div>
  <div>A: {_dual_html.escape(glyph_a)}</div>
  <div>B: {_dual_html.escape(glyph_b)}</div>
  <div>contours: {_dual_html.escape(contour_count)}</div>
</td>
"""

        cells = [info_cell]

        for step in range(1, 21):
            src = f"{info['glyph_svg_base']}/{step}/{glyph_q}?v={report_mtime}"
            cells.append(f"""
<td class="glyph-cell">
  <img class="glyph-img" src="{src}" loading="lazy">
</td>
""")

        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    html_doc = f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{_dual_html.escape(info['title'])}｜每字 20 步真实预览</title>
<style>
body {{
  margin: 0;
  padding: 20px;
  background: #f4f5f7;
  color: #111;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}}

h1 {{
  margin: 0 0 10px;
  font-size: 24px;
}}

.notice {{
  background: #fff8d8;
  border: 1px solid #eadb91;
  border-radius: 10px;
  padding: 12px 14px;
  line-height: 1.7;
  margin: 14px 0 18px;
}}

.actions {{
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin: 14px 0 18px;
}}

.actions a {{
  display: inline-block;
  background: #111;
  color: #fff;
  padding: 9px 13px;
  border-radius: 8px;
  text-decoration: none;
  font-size: 14px;
}}

.stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin: 16px 0 22px;
}}

.stat {{
  background: white;
  border-radius: 12px;
  padding: 14px;
  box-shadow: 0 1px 4px rgba(0,0,0,.08);
}}

.label {{
  font-size: 12px;
  color: #666;
}}

.value {{
  margin-top: 5px;
  font-size: 22px;
  font-weight: 700;
}}

.table-wrap {{
  overflow: auto;
  max-height: calc(100vh - 260px);
  background: white;
  border-radius: 12px;
  box-shadow: 0 1px 4px rgba(0,0,0,.1);
}}

.matrix {{
  border-collapse: collapse;
  min-width: 2600px;
  width: max-content;
}}

.matrix th,
.matrix td {{
  border: 1px solid #e8e8e8;
  padding: 8px;
  vertical-align: middle;
  background: white;
}}

.matrix th {{
  position: sticky;
  top: 0;
  z-index: 5;
  background: #f0f0f0;
  font-size: 13px;
}}

.sticky-col {{
  position: sticky;
  left: 0;
  z-index: 6;
  background: #fff;
  min-width: 270px;
  max-width: 270px;
}}

th.sticky-col {{
  z-index: 8;
  background: #eaeaea;
}}

.meta {{
  font-size: 12px;
  line-height: 1.55;
  word-break: break-all;
}}

.idx {{
  font-size: 16px;
  font-weight: 800;
  margin-bottom: 4px;
}}

.glyph-cell {{
  width: 112px;
  height: 122px;
  text-align: center;
}}

.glyph-img {{
  width: 108px;
  height: 108px;
  object-fit: contain;
  background: #fff;
}}

.small {{
  color: #666;
  font-size: 13px;
  line-height: 1.6;
}}
</style>
</head>
<body>

<h1>{_dual_html.escape(info['title'])}真实生成预览：每个有效字形的 1–20 步</h1>

<div class="notice">
  当前页面直接读取最终生成后的 <b>{_dual_html.escape(str(info['out_dir']))}</b> 中的 20 个 TTF。<br>
  每一行是一个实际有效唯一 glyph 字形，每一列是 Step 01 到 Step 20。这里显示的是 glyf 轮廓，不是演示图。
</div>

<div class="actions">
  <a href="{info['download_zip']}">下载生成字体 ZIP</a>
  <a href="{info['csv_url']}">下载有效 glyph 清单 CSV</a>
  <a href="{info['preview_url']}?limit=1000">显示全部有效 glyph</a>
  <a href="/">返回首页</a>
</div>

{_foundry_vf_preview_panel(mode)}

{summary_html}

<p class="small">
算法：<b>{_dual_html.escape(str(algorithm))}</b><br>
当前显示：{len(prepared)} 个；实际有效唯一 glyph 数：{_dual_html.escape(str(generated_unique_glyph_pairs))}。
</p>

<div class="table-wrap">
<table class="matrix">
  <thead>
    <tr>
      {''.join(head_cells)}
    </tr>
  </thead>
  <tbody>
    {''.join(body_rows)}
  </tbody>
</table>
</div>

</body>
</html>
"""

    return _dual_HTMLResponse(html_doc)


@app.middleware("http")
async def _final_dual_gb_preview_intercept(request, call_next):
    path = request.url.path

    if path == "/oyun_gb_preview":
        return await _dual_real_matrix_preview(request, "oyun")

    if path == "/menk_gb_preview":
        return await _dual_real_matrix_preview(request, "menk")

    return await call_next(request)


print("[FINAL-DUAL-GB-PREVIEW] OYUN + MENK matrix preview and CSV download installed")
# ================= FINAL_DUAL_GB_PREVIEW_AND_CSV_END =================


# ==================== AUTO_COMPLETE_MENK_GB_AFTER_BUILD_MIDDLEWARE ====================
# 作用：
# 点击网页 /api/foundry/menk_gb/build 后，先执行原始蒙科立国标生成，
# 再自动执行 complete outline 版本，并把完整 zip 覆盖到网页下载接口。
@app.middleware("http")
async def _auto_complete_menk_gb_after_build_middleware(request, call_next):
    response = await call_next(request)

    try:
        if (
            request.method.upper() == "POST"
            and request.url.path == "/api/foundry/menk_gb/build"
            and response.status_code == 200
        ):
            import subprocess
            import shutil
            from pathlib import Path as _Path

            _root = _Path(__file__).resolve().parent
            _script = _root / "scripts" / "build_menk_gb_complete_v5_outline.py"
            _complete_zip = _root / "output" / "menk_gb_complete_ttf_steps.zip"
            _web_zip = _root / "output" / "menk_gb_ttf_steps.zip"

            print("[AUTO-COMPLETE-MENK] start complete outline build...")

            if not _script.exists():
                print("[AUTO-COMPLETE-MENK][WARN] script not found:", _script)
            else:
                subprocess.run(
                    ["python", str(_script)],
                    cwd=str(_root),
                    check=True,
                    timeout=900,
                )

                if _complete_zip.exists():
                    shutil.copyfile(_complete_zip, _web_zip)
                    print("[AUTO-COMPLETE-MENK] replaced web zip:", _web_zip)
                else:
                    print("[AUTO-COMPLETE-MENK][WARN] complete zip not found:", _complete_zip)

    except Exception as e:
        print("[AUTO-COMPLETE-MENK][ERROR]", repr(e))

    return response
# =====================================================================================


# ================= TEXT_FUR_EFFECT_FEATURE_START =================
# 新增独立页面：毛绒 / 草丛 / 纤维感文字效果。
# 不修改现有字体生成、预览和下载流程。

@app.get("/text_fur", response_class=HTMLResponse)
async def text_fur_effect_page():
    return HTMLResponse(r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>毛绒文字特效</title>
<style>
* {
  box-sizing: border-box;
}
:root {
  --bg: #eef2f7;
  --panel: #ffffff;
  --line: #d8dee8;
  --text: #111827;
  --muted: #64748b;
  --accent: #2563eb;
  --dark: #0f172a;
}
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Arial, "Microsoft YaHei", "Noto Sans Mongolian", sans-serif;
}
.page {
  max-width: 1280px;
  margin: 0 auto;
  padding: 22px;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  margin-bottom: 16px;
}
h1 {
  margin: 0;
  font-size: 24px;
  letter-spacing: 0;
}
.sub {
  margin-top: 6px;
  color: var(--muted);
  font-size: 13px;
}
.back {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--dark);
  color: #fff;
  text-decoration: none;
  font-weight: 700;
  font-size: 13px;
  white-space: nowrap;
}
.layout {
  display: grid;
  grid-template-columns: minmax(270px, 340px) minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 14px;
  box-shadow: 0 10px 26px rgba(15, 23, 42, .05);
}
.controls {
  display: grid;
  gap: 12px;
}
label {
  display: block;
  font-size: 12px;
  color: var(--muted);
  font-weight: 700;
  margin-bottom: 6px;
}
textarea,
input,
select {
  width: 100%;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  background: #fff;
  color: var(--text);
  padding: 9px 10px;
  font: inherit;
}
textarea {
  min-height: 120px;
  resize: vertical;
  line-height: 1.55;
}
.row2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.range-line {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 9px;
  align-items: center;
}
.range-line input[type="range"] {
  padding: 0;
}
.value-pill {
  min-width: 42px;
  text-align: right;
  color: var(--dark);
  font-weight: 800;
  font-size: 12px;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
button {
  min-height: 38px;
  border: 1px solid var(--accent);
  border-radius: 6px;
  background: var(--accent);
  color: #fff;
  padding: 8px 12px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}
button.secondary {
  border-color: #cbd5e1;
  background: #fff;
  color: var(--text);
}
button.dark {
  border-color: var(--dark);
  background: var(--dark);
  color: #fff;
}
.swatches {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.swatch {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: 2px solid #fff;
  box-shadow: 0 0 0 1px #cbd5e1;
  cursor: pointer;
}
.stage-panel {
  padding: 0;
  overflow: hidden;
}
.stage-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  background: #f8fafc;
}
.stage-meta {
  color: var(--muted);
  font-size: 12px;
}
.canvas-wrap {
  position: relative;
  min-height: 560px;
  overflow: auto;
  background: #ffffff;
}
canvas {
  display: block;
  width: 100%;
  height: auto;
  min-height: 560px;
}
.hint {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}
.preset-texts {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 7px;
}
.preset-texts button {
  min-height: 32px;
  padding: 6px 8px;
  border-color: #cbd5e1;
  background: #f8fafc;
  color: #0f172a;
  font-size: 12px;
}
.file-note {
  margin-top: 5px;
  color: var(--muted);
  font-size: 12px;
}
@media (max-width: 900px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .topbar {
    align-items: flex-start;
    flex-direction: column;
  }
  .canvas-wrap {
    min-height: 430px;
  }
  canvas {
    min-height: 430px;
  }
}
</style>
</head>
<body>
<main class="page">
  <div class="topbar">
    <div>
      <h1>毛绒文字特效</h1>
      <div class="sub">输入自定义文字，生成类似视频里的草丛、毛绒、纤维颗粒文字；先支持汉字、英文字母、传统蒙古文输入预览。</div>
    </div>
    <a class="back" href="/">返回首页</a>
  </div>

  <div class="layout">
    <section class="panel controls">
      <div>
        <label for="textInput">自定义文字</label>
        <textarea id="textInput">汉字 ABCD ᠮᠣᠩᠭᠣᠯ</textarea>
      </div>

      <div class="preset-texts">
        <button type="button" data-text="汉字">汉字</button>
        <button type="button" data-text="ABCD">英文</button>
        <button type="button" data-text="ᠮᠣᠩᠭᠣᠯ">蒙古文</button>
      </div>

      <div class="row2">
        <div>
          <label for="layoutMode">排版</label>
          <select id="layoutMode">
            <option value="horizontal">横排</option>
            <option value="vertical">竖排 / 蒙古文试验</option>
          </select>
        </div>
        <div>
          <label for="fontFamily">字体</label>
          <select id="fontFamily">
            <option value='Arial, "Microsoft YaHei", sans-serif'>无衬线</option>
            <option value='"Microsoft YaHei", "Noto Sans CJK SC", sans-serif'>中文黑体</option>
            <option value='SimSun, "Songti SC", serif'>中文宋体</option>
            <option value='"Times New Roman", serif'>英文衬线</option>
            <option value='"Mongolian Baiti", "Noto Sans Mongolian", serif'>传统蒙古文</option>
          </select>
        </div>
      </div>

      <div>
        <label for="fontFile">可选：上传字体文件</label>
        <input id="fontFile" type="file" accept=".ttf,.otf,.woff,.woff2">
        <div class="file-note">浏览器本地加载，不上传服务器。适合测试蒙古文字体或品牌字体。</div>
      </div>

      <div>
        <label>文字颜色</label>
        <div class="row2">
          <input id="textColor" type="color" value="#22a455">
          <input id="bgColor" type="color" value="#ffffff" title="背景颜色">
        </div>
        <div class="swatches" aria-label="颜色预设">
          <button class="swatch" type="button" data-color="#22a455" style="background:#22a455"></button>
          <button class="swatch" type="button" data-color="#f8fafc" style="background:#f8fafc"></button>
          <button class="swatch" type="button" data-color="#f97316" style="background:#f97316"></button>
          <button class="swatch" type="button" data-color="#38bdf8" style="background:#38bdf8"></button>
          <button class="swatch" type="button" data-color="#111827" style="background:#111827"></button>
        </div>
      </div>

      <div>
        <label for="fontSize">字体大小</label>
        <div class="range-line"><input id="fontSize" type="range" min="48" max="260" value="150"><span class="value-pill" id="fontSizeVal">150</span></div>
      </div>
      <div>
        <label for="density">纤维密度</label>
        <div class="range-line"><input id="density" type="range" min="8" max="90" value="48"><span class="value-pill" id="densityVal">48</span></div>
      </div>
      <div>
        <label for="hairLength">毛刺长度</label>
        <div class="range-line"><input id="hairLength" type="range" min="2" max="28" value="12"><span class="value-pill" id="hairLengthVal">12</span></div>
      </div>
      <div>
        <label for="spread">边缘扩散</label>
        <div class="range-line"><input id="spread" type="range" min="0" max="26" value="10"><span class="value-pill" id="spreadVal">10</span></div>
      </div>
      <div>
        <label for="softness">柔化程度</label>
        <div class="range-line"><input id="softness" type="range" min="0" max="14" value="4"><span class="value-pill" id="softnessVal">4</span></div>
      </div>

      <div class="actions">
        <button type="button" id="renderBtn">重新生成</button>
        <button type="button" id="randomBtn" class="secondary">随机毛感</button>
        <button type="button" id="downloadBtn" class="dark">下载 PNG</button>
      </div>

      <div class="hint">提示：传统蒙古文显示效果取决于浏览器和本机字体。可以上传 `.ttf/.otf` 蒙古文字体来提高显示质量。</div>
    </section>

    <section class="panel stage-panel">
      <div class="stage-head">
        <b>实时预览</b>
        <div class="stage-meta" id="stageMeta">准备中</div>
      </div>
      <div class="canvas-wrap">
        <canvas id="canvas" width="1280" height="720"></canvas>
      </div>
    </section>
  </div>
</main>

<script>
(function(){
  const canvas = document.getElementById("canvas");
  const ctx = canvas.getContext("2d", { alpha: false });
  const maskCanvas = document.createElement("canvas");
  const maskCtx = maskCanvas.getContext("2d", { willReadFrequently: true });

  const controls = {
    textInput: document.getElementById("textInput"),
    layoutMode: document.getElementById("layoutMode"),
    fontFamily: document.getElementById("fontFamily"),
    fontFile: document.getElementById("fontFile"),
    textColor: document.getElementById("textColor"),
    bgColor: document.getElementById("bgColor"),
    fontSize: document.getElementById("fontSize"),
    density: document.getElementById("density"),
    hairLength: document.getElementById("hairLength"),
    spread: document.getElementById("spread"),
    softness: document.getElementById("softness")
  };

  const valueIds = ["fontSize", "density", "hairLength", "spread", "softness"];
  let seed = 24681357;
  let queued = false;
  let customFontFamily = "";

  function rng() {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 4294967296;
  }

  function hexToRgb(hex) {
    const raw = String(hex || "#000000").replace("#", "");
    const n = parseInt(raw.length === 3 ? raw.split("").map(x => x + x).join("") : raw, 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }

  function colorWithAlpha(hex, alpha) {
    const c = hexToRgb(hex);
    return "rgba(" + c.r + "," + c.g + "," + c.b + "," + alpha + ")";
  }

  function updateValues() {
    valueIds.forEach(id => {
      const el = document.getElementById(id);
      const val = document.getElementById(id + "Val");
      if (el && val) val.textContent = el.value;
    });
  }

  function resizeCanvases() {
    const wrap = canvas.parentElement;
    const cssWidth = Math.max(720, Math.floor(wrap.clientWidth || 1000));
    const cssHeight = Math.max(540, Math.floor(window.innerHeight * 0.72));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(cssWidth * dpr);
    canvas.height = Math.floor(cssHeight * dpr);
    maskCanvas.width = canvas.width;
    maskCanvas.height = canvas.height;
  }

  function fontString(size) {
    const family = customFontFamily || controls.fontFamily.value;
    return "900 " + size + "px " + family;
  }

  function splitGraphemes(text) {
    const clean = text || "";
    if (window.Intl && Intl.Segmenter) {
      return Array.from(new Intl.Segmenter("zh", { granularity: "grapheme" }).segment(clean), x => x.segment);
    }
    return Array.from(clean);
  }

  function wrapLines(text, maxWidth, size) {
    const rawLines = String(text || "").split(/\r?\n/);
    const lines = [];
    maskCtx.font = fontString(size);

    rawLines.forEach(raw => {
      const chars = splitGraphemes(raw);
      let line = "";
      chars.forEach(ch => {
        const next = line + ch;
        if (line && maskCtx.measureText(next).width > maxWidth) {
          lines.push(line);
          line = ch;
        } else {
          line = next;
        }
      });
      lines.push(line || " ");
    });

    return lines.slice(0, 12);
  }

  function drawTextMask() {
    const text = controls.textInput.value.trim() || "ENTER TEXT";
    const size = Number(controls.fontSize.value);
    const vertical = controls.layoutMode.value === "vertical";
    const pad = Math.max(42, Math.floor(size * 0.35));

    maskCtx.clearRect(0, 0, maskCanvas.width, maskCanvas.height);
    maskCtx.save();
    maskCtx.fillStyle = "#000";
    maskCtx.textAlign = "center";
    maskCtx.textBaseline = "middle";
    maskCtx.font = fontString(size);

    if (vertical) {
      const chars = splitGraphemes(text.replace(/\s+/g, ""));
      const step = size * 0.72;
      const total = Math.min(chars.length, 24) * step;
      const x = maskCanvas.width / 2;
      let y = (maskCanvas.height - total) / 2 + step / 2;
      chars.slice(0, 24).forEach(ch => {
        maskCtx.fillText(ch, x, y);
        y += step;
      });
    } else {
      const lines = wrapLines(text, maskCanvas.width - pad * 2, size);
      const lineHeight = size * 1.08;
      const total = lines.length * lineHeight;
      let y = (maskCanvas.height - total) / 2 + lineHeight / 2;
      lines.forEach(line => {
        maskCtx.fillText(line, maskCanvas.width / 2, y);
        y += lineHeight;
      });
    }

    maskCtx.restore();
  }

  function collectMaskPoints(step) {
    const data = maskCtx.getImageData(0, 0, maskCanvas.width, maskCanvas.height).data;
    const points = [];
    const w = maskCanvas.width;
    const h = maskCanvas.height;
    const s = Math.max(2, step);

    for (let y = 1; y < h - 1; y += s) {
      for (let x = 1; x < w - 1; x += s) {
        const a = data[(y * w + x) * 4 + 3];
        if (a < 40) continue;

        const left = data[(y * w + Math.max(0, x - s)) * 4 + 3];
        const right = data[(y * w + Math.min(w - 1, x + s)) * 4 + 3];
        const up = data[(Math.max(0, y - s) * w + x) * 4 + 3];
        const down = data[(Math.min(h - 1, y + s) * w + x) * 4 + 3];
        const edge = left < 40 || right < 40 || up < 40 || down < 40;
        points.push({ x, y, edge, a });
      }
    }

    return points;
  }

  function drawBaseText() {
    const textColor = controls.textColor.value;
    const softness = Number(controls.softness.value);
    const size = Number(controls.fontSize.value);
    const data = maskCtx.getImageData(0, 0, maskCanvas.width, maskCanvas.height);
    const temp = document.createElement("canvas");
    temp.width = maskCanvas.width;
    temp.height = maskCanvas.height;
    const tctx = temp.getContext("2d");
    tctx.putImageData(data, 0, 0);
    ctx.save();
    ctx.globalAlpha = 0.65;
    ctx.filter = "blur(" + softness + "px)";
    ctx.globalCompositeOperation = "source-over";
    ctx.drawImage(temp, 0, 0);
    ctx.globalCompositeOperation = "source-in";
    ctx.fillStyle = colorWithAlpha(textColor, 0.9);
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.restore();

    ctx.save();
    ctx.globalAlpha = 0.18;
    ctx.filter = "blur(" + Math.max(1, softness * 1.8) + "px)";
    ctx.drawImage(temp, -size * 0.02, -size * 0.02);
    ctx.restore();
  }

  function render() {
    queued = false;
    updateValues();
    resizeCanvases();
    seed = seed >>> 0;

    const bg = controls.bgColor.value;
    const textColor = controls.textColor.value;
    const density = Number(controls.density.value);
    const hairLength = Number(controls.hairLength.value);
    const spread = Number(controls.spread.value);
    const sampleStep = Math.max(2, Math.round(10 - density / 12));
    const loops = Math.max(1, Math.round(density / 24));

    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    drawTextMask();
    drawBaseText();

    const points = collectMaskPoints(sampleStep);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";

    points.forEach(p => {
      const passes = p.edge ? loops + 2 : loops;
      for (let i = 0; i < passes; i++) {
        const edgeBoost = p.edge ? 1.6 : 0.8;
        const jitter = (rng() - 0.5) * spread * edgeBoost;
        const angle = rng() * Math.PI * 2;
        const len = hairLength * (0.35 + rng() * 1.05) * (p.edge ? 1.15 : 0.8);
        const x = p.x + Math.cos(angle) * jitter;
        const y = p.y + Math.sin(angle) * jitter;
        const x2 = x + Math.cos(angle + (rng() - 0.5) * 0.9) * len;
        const y2 = y + Math.sin(angle + (rng() - 0.5) * 0.9) * len;

        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x2, y2);
        ctx.lineWidth = 0.75 + rng() * 1.7;
        ctx.strokeStyle = colorWithAlpha(textColor, 0.18 + rng() * 0.62);
        ctx.stroke();

        if (rng() > 0.84) {
          ctx.beginPath();
          ctx.arc(x + (rng() - 0.5) * spread, y + (rng() - 0.5) * spread, 0.8 + rng() * 1.9, 0, Math.PI * 2);
          ctx.fillStyle = colorWithAlpha(textColor, 0.18 + rng() * 0.55);
          ctx.fill();
        }
      }
    });

    document.getElementById("stageMeta").textContent =
      "采样点 " + points.length + " · " + canvas.width + "×" + canvas.height;
  }

  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(render);
  }

  Object.values(controls).forEach(el => {
    if (!el || el === controls.fontFile) return;
    el.addEventListener("input", schedule);
    el.addEventListener("change", schedule);
  });

  document.querySelectorAll("[data-text]").forEach(btn => {
    btn.addEventListener("click", () => {
      controls.textInput.value = btn.dataset.text || "";
      schedule();
    });
  });

  document.querySelectorAll("[data-color]").forEach(btn => {
    btn.addEventListener("click", () => {
      controls.textColor.value = btn.dataset.color;
      seed += 17;
      schedule();
    });
  });

  document.getElementById("renderBtn").addEventListener("click", () => {
    seed += 101;
    schedule();
  });

  document.getElementById("randomBtn").addEventListener("click", () => {
    seed = Math.floor(Math.random() * 4294967295);
    controls.density.value = String(24 + Math.floor(Math.random() * 54));
    controls.hairLength.value = String(6 + Math.floor(Math.random() * 18));
    controls.spread.value = String(4 + Math.floor(Math.random() * 18));
    schedule();
  });

  document.getElementById("downloadBtn").addEventListener("click", () => {
    render();
    const a = document.createElement("a");
    a.download = "fur_text_effect.png";
    a.href = canvas.toDataURL("image/png");
    a.click();
  });

  controls.fontFile.addEventListener("change", async () => {
    const file = controls.fontFile.files && controls.fontFile.files[0];
    if (!file) {
      customFontFamily = "";
      schedule();
      return;
    }
    const family = "UserFurFont_" + Date.now();
    const data = await file.arrayBuffer();
    const face = new FontFace(family, data);
    await face.load();
    document.fonts.add(face);
    customFontFamily = "'" + family + "'";
    schedule();
  });

  window.addEventListener("resize", schedule);
  updateValues();
  render();
})();
</script>
</body>
</html>
""")


_TEXT_FUR_HOME_ENTRY = r"""
<section id="textFurFeatureEntry" style="
  margin: 18px 0;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  background: #f8fafc;
  padding: 14px 16px;
">
  <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
    <div>
      <div style="font-weight:800;font-size:16px;color:#0f172a;">毛绒文字特效</div>
      <div style="font-size:13px;color:#64748b;margin-top:4px;">输入汉字、英文或传统蒙古文，生成草丛 / 毛绒 / 纤维感文字图片。</div>
    </div>
    <a href="/text_fur" target="_blank" style="
      display:inline-flex;
      min-height:36px;
      align-items:center;
      justify-content:center;
      padding:8px 12px;
      border-radius:6px;
      background:#0f172a;
      color:#fff;
      text-decoration:none;
      font-weight:700;
      font-size:13px;
    ">打开新功能</a>
  </div>
</section>
"""


@app.middleware("http")
async def _text_fur_home_entry_inject(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    if "textFurFeatureEntry" not in html:
        marker = '<form id="form">'
        if marker in html:
            html = html.replace(marker, _TEXT_FUR_HOME_ENTRY + "\n" + marker, 1)
        elif "</body>" in html:
            html = html.replace("</body>", _TEXT_FUR_HOME_ENTRY + "\n</body>")
        else:
            html += _TEXT_FUR_HOME_ENTRY

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return HTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers,
    )

# ================= TEXT_FUR_EFFECT_FEATURE_END =================

# ================= TEXT_MOTION_GRAPHICS_FEATURE_START =================
@app.get("/text_motion", response_class=HTMLResponse)
async def text_motion_graphics_page():
    return HTMLResponse(r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>文字动态图形</title>
<style>
:root {
  color-scheme: dark;
  --bg: #08090d;
  --panel: #111827;
  --panel-2: #0b1220;
  --line: #253047;
  --text: #f8fafc;
  --muted: #94a3b8;
  --blue: #38bdf8;
  --pink: #fb7185;
  --yellow: #facc15;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background:
    radial-gradient(circle at 18% 12%, rgba(56,189,248,.12), transparent 28%),
    radial-gradient(circle at 86% 78%, rgba(251,113,133,.14), transparent 30%),
    var(--bg);
  color: var(--text);
  font-family: Arial, "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
}
.page {
  width: min(1480px, calc(100vw - 32px));
  margin: 0 auto;
  padding: 22px 0 26px;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}
h1 {
  margin: 0;
  font-size: 26px;
  line-height: 1.15;
  letter-spacing: 0;
}
.sub {
  margin-top: 7px;
  color: var(--muted);
  font-size: 13px;
}
.back {
  display: inline-flex;
  align-items: center;
  min-height: 36px;
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text);
  text-decoration: none;
  font-weight: 700;
  background: rgba(15, 23, 42, .74);
}
.layout {
  display: grid;
  grid-template-columns: 340px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.panel {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(17, 24, 39, .92);
}
.controls {
  padding: 16px;
  display: grid;
  gap: 13px;
  position: sticky;
  top: 12px;
}
label {
  display: block;
  font-size: 12px;
  color: #cbd5e1;
  margin-bottom: 6px;
  font-weight: 700;
}
textarea,
select,
input[type="number"],
input[type="file"] {
  width: 100%;
  border: 1px solid #334155;
  border-radius: 6px;
  color: var(--text);
  background: #0b1220;
  min-height: 36px;
  padding: 8px 10px;
  font: inherit;
  outline: none;
}
textarea {
  min-height: 92px;
  resize: vertical;
  line-height: 1.45;
}
input[type="range"] {
  width: 100%;
  accent-color: var(--blue);
}
input[type="color"] {
  width: 100%;
  height: 36px;
  border: 1px solid #334155;
  border-radius: 6px;
  background: #0b1220;
  padding: 3px;
}
.row2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.preset-texts,
.actions,
.mode-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
button {
  border: 0;
  border-radius: 6px;
  min-height: 34px;
  padding: 8px 11px;
  font-weight: 800;
  color: #07111f;
  background: var(--blue);
  cursor: pointer;
}
button.secondary {
  color: var(--text);
  background: #1f2937;
  border: 1px solid #334155;
}
button.danger {
  color: #101018;
  background: var(--pink);
}
.range-line {
  display: grid;
  grid-template-columns: 1fr 52px;
  gap: 8px;
  align-items: center;
}
.value-pill {
  display: inline-flex;
  justify-content: center;
  align-items: center;
  min-height: 28px;
  border-radius: 6px;
  color: #e2e8f0;
  background: #0b1220;
  border: 1px solid #334155;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.stage-panel {
  overflow: hidden;
  background: #020617;
}
.stage-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 48px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
}
.stage-head b { font-size: 14px; }
.stage-meta {
  color: var(--muted);
  font-size: 12px;
  text-align: right;
}
.canvas-wrap {
  min-height: 660px;
  width: 100%;
  background: #000;
}
canvas {
  display: block;
  width: 100%;
  min-height: 660px;
  background: #000;
}
.small-note {
  font-size: 12px;
  line-height: 1.5;
  color: var(--muted);
}
@media (max-width: 980px) {
  .layout { grid-template-columns: 1fr; }
  .controls { position: static; }
  .canvas-wrap, canvas { min-height: 520px; }
  .topbar { align-items: flex-start; flex-direction: column; }
}
</style>
</head>
<body>
<main class="page">
  <div class="topbar">
    <div>
      <h1>文字动态图形</h1>
      <div class="sub">点阵流场、螺旋环、波纹条带；支持汉字、英文字母、传统蒙古文输入。</div>
    </div>
    <a class="back" href="/">返回首页</a>
  </div>

  <div class="layout">
    <section class="panel controls">
      <div>
        <label for="textInput">自定义文字</label>
        <textarea id="textInput">山海 TYPE ᠮᠣᠩᠭᠣᠯ</textarea>
      </div>

      <div class="preset-texts">
        <button type="button" data-text="山海之间">汉字</button>
        <button type="button" data-text="SPACE TYPE">英文</button>
        <button type="button" data-text="ᠮᠣᠩᠭᠣᠯ">蒙古文</button>
      </div>

      <div>
        <label for="motionMode">动效模式</label>
        <select id="motionMode">
          <option value="field">点阵流场</option>
          <option value="spiral">螺旋环</option>
          <option value="bands">波纹条带</option>
        </select>
      </div>

      <div class="row2">
        <div>
          <label for="layoutMode">排版</label>
          <select id="layoutMode">
            <option value="horizontal">横排</option>
            <option value="vertical">竖排 / 蒙古文试验</option>
          </select>
        </div>
        <div>
          <label for="fontFamily">字体</label>
          <select id="fontFamily">
            <option value='Arial, "Microsoft YaHei", sans-serif'>无衬线</option>
            <option value='"Microsoft YaHei", "Noto Sans CJK SC", sans-serif'>中文黑体</option>
            <option value='SimSun, "Songti SC", serif'>中文宋体</option>
            <option value='"Times New Roman", serif'>英文衬线</option>
            <option value='"Mongolian Baiti", "Noto Sans Mongolian", serif'>传统蒙古文</option>
          </select>
        </div>
      </div>

      <div>
        <label for="fontFile">可选：上传字体文件</label>
        <input id="fontFile" type="file" accept=".ttf,.otf,.woff,.woff2">
      </div>

      <div class="row2">
        <div>
          <label for="primaryColor">主色</label>
          <input id="primaryColor" type="color" value="#fb7185">
        </div>
        <div>
          <label for="accentColor">辅色</label>
          <input id="accentColor" type="color" value="#38bdf8">
        </div>
      </div>

      <div class="row2">
        <div>
          <label for="bgColor">背景</label>
          <input id="bgColor" type="color" value="#03050a">
        </div>
        <div>
          <label for="paletteMode">配色</label>
          <select id="paletteMode">
            <option value="duo">双色霓虹</option>
            <option value="mono">单色发光</option>
            <option value="multi">彩色条带</option>
          </select>
        </div>
      </div>

      <div>
        <label for="fontSize">字体大小</label>
        <div class="range-line"><input id="fontSize" type="range" min="36" max="210" value="118"><span class="value-pill" id="fontSizeVal">118</span></div>
      </div>
      <div>
        <label for="density">密度</label>
        <div class="range-line"><input id="density" type="range" min="18" max="100" value="62"><span class="value-pill" id="densityVal">62</span></div>
      </div>
      <div>
        <label for="amplitude">起伏</label>
        <div class="range-line"><input id="amplitude" type="range" min="0" max="100" value="48"><span class="value-pill" id="amplitudeVal">48</span></div>
      </div>
      <div>
        <label for="depth">纵深</label>
        <div class="range-line"><input id="depth" type="range" min="0" max="100" value="54"><span class="value-pill" id="depthVal">54</span></div>
      </div>
      <div>
        <label for="speed">速度</label>
        <div class="range-line"><input id="speed" type="range" min="0" max="100" value="42"><span class="value-pill" id="speedVal">42</span></div>
      </div>

      <div class="actions">
        <button type="button" id="pauseBtn">暂停</button>
        <button type="button" id="shuffleBtn" class="secondary">随机</button>
        <button type="button" id="downloadBtn" class="danger">下载 PNG</button>
      </div>
      <div class="small-note" id="stateNote">正在生成动态图形。</div>
    </section>

    <section class="panel stage-panel">
      <div class="stage-head">
        <b id="stageTitle">点阵流场</b>
        <div class="stage-meta" id="stageMeta">准备中</div>
      </div>
      <div class="canvas-wrap">
        <canvas id="motionCanvas" width="1400" height="860"></canvas>
      </div>
    </section>
  </div>
</main>

<script>
(function(){
  const canvas = document.getElementById("motionCanvas");
  const ctx = canvas.getContext("2d", { alpha: false });
  const mask = document.createElement("canvas");
  const mctx = mask.getContext("2d", { willReadFrequently: true });

  const controls = {
    textInput: document.getElementById("textInput"),
    motionMode: document.getElementById("motionMode"),
    layoutMode: document.getElementById("layoutMode"),
    fontFamily: document.getElementById("fontFamily"),
    fontFile: document.getElementById("fontFile"),
    primaryColor: document.getElementById("primaryColor"),
    accentColor: document.getElementById("accentColor"),
    bgColor: document.getElementById("bgColor"),
    paletteMode: document.getElementById("paletteMode"),
    fontSize: document.getElementById("fontSize"),
    density: document.getElementById("density"),
    amplitude: document.getElementById("amplitude"),
    depth: document.getElementById("depth"),
    speed: document.getElementById("speed")
  };

  const valueIds = ["fontSize", "density", "amplitude", "depth", "speed"];
  const modeNames = { field: "点阵流场", spiral: "螺旋环", bands: "波纹条带" };
  let running = true;
  let customFontFamily = "";
  let seed = 1357911;
  let lastPoints = [];

  function splitGraphemes(text) {
    const clean = String(text || "");
    if (window.Intl && Intl.Segmenter) {
      return Array.from(new Intl.Segmenter("zh", { granularity: "grapheme" }).segment(clean), x => x.segment);
    }
    return Array.from(clean);
  }

  function rng() {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 4294967296;
  }

  function hexToRgb(hex) {
    const raw = String(hex || "#000000").replace("#", "");
    const value = parseInt(raw.length === 3 ? raw.split("").map(x => x + x).join("") : raw, 16);
    return { r: (value >> 16) & 255, g: (value >> 8) & 255, b: value & 255 };
  }

  function rgba(hex, alpha) {
    const c = hexToRgb(hex);
    return "rgba(" + c.r + "," + c.g + "," + c.b + "," + alpha + ")";
  }

  function lerp(a, b, t) { return a + (b - a) * t; }

  function mixColor(a, b, t, alpha) {
    const ca = hexToRgb(a);
    const cb = hexToRgb(b);
    return "rgba(" + Math.round(lerp(ca.r, cb.r, t)) + "," + Math.round(lerp(ca.g, cb.g, t)) + "," + Math.round(lerp(ca.b, cb.b, t)) + "," + alpha + ")";
  }

  function palette(i, total, alpha) {
    const mode = controls.paletteMode.value;
    const p = controls.primaryColor.value;
    const a = controls.accentColor.value;
    if (mode === "mono") return rgba(p, alpha);
    if (mode === "multi") {
      const colors = [p, "#facc15", a, "#f8fafc", "#ef4444"];
      return rgba(colors[Math.abs(i) % colors.length], alpha);
    }
    return mixColor(p, a, total ? (i % total) / Math.max(1, total - 1) : 0.5, alpha);
  }

  function fontString(size, weight) {
    const family = customFontFamily || controls.fontFamily.value;
    return (weight || 800) + " " + size + "px " + family;
  }

  function updateValues() {
    valueIds.forEach(id => {
      const el = document.getElementById(id);
      const val = document.getElementById(id + "Val");
      if (el && val) val.textContent = el.value;
    });
    document.getElementById("stageTitle").textContent = modeNames[controls.motionMode.value] || "文字动态图形";
  }

  function resize() {
    const wrap = canvas.parentElement;
    const cssWidth = Math.max(760, Math.floor(wrap.clientWidth || 1100));
    const cssHeight = Math.max(620, Math.floor(window.innerHeight * 0.76));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(cssWidth * dpr);
    canvas.height = Math.floor(cssHeight * dpr);
    mask.width = canvas.width;
    mask.height = canvas.height;
  }

  function paintBackground(t) {
    const w = canvas.width;
    const h = canvas.height;
    ctx.fillStyle = controls.bgColor.value;
    ctx.fillRect(0, 0, w, h);

    const grad = ctx.createRadialGradient(w * .5, h * .45, 10, w * .5, h * .45, Math.max(w, h) * .55);
    grad.addColorStop(0, rgba(controls.accentColor.value, .18));
    grad.addColorStop(.55, rgba(controls.primaryColor.value, .06));
    grad.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, h);

    ctx.save();
    ctx.globalAlpha = .13;
    ctx.strokeStyle = rgba(controls.accentColor.value, .22);
    ctx.lineWidth = Math.max(1, w / 1100);
    const gap = Math.max(32, Math.floor(w / 24));
    for (let x = (t * 18) % gap - gap; x < w + gap; x += gap) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x + h * .28, h);
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawMask() {
    const text = controls.textInput.value.trim() || "TYPE";
    const size = Number(controls.fontSize.value);
    const vertical = controls.layoutMode.value === "vertical";
    const maxWidth = mask.width * .82;

    mctx.clearRect(0, 0, mask.width, mask.height);
    mctx.save();
    mctx.fillStyle = "#fff";
    mctx.font = fontString(size, 900);
    mctx.textAlign = "center";
    mctx.textBaseline = "middle";

    if (vertical) {
      const chars = splitGraphemes(text.replace(/\s+/g, ""));
      const step = size * .76;
      const shown = chars.slice(0, 28);
      let y = (mask.height - shown.length * step) / 2 + step / 2;
      shown.forEach(ch => {
        mctx.fillText(ch, mask.width / 2, y);
        y += step;
      });
    } else {
      const rawLines = text.split(/\r?\n/);
      const lines = [];
      rawLines.forEach(lineText => {
        let line = "";
        splitGraphemes(lineText).forEach(ch => {
          const next = line + ch;
          if (line && mctx.measureText(next).width > maxWidth) {
            lines.push(line);
            line = ch;
          } else {
            line = next;
          }
        });
        lines.push(line || " ");
      });
      const shown = lines.slice(0, 8);
      const lineHeight = size * 1.02;
      let y = (mask.height - shown.length * lineHeight) / 2 + lineHeight / 2;
      shown.forEach(line => {
        mctx.fillText(line, mask.width / 2, y);
        y += lineHeight;
      });
    }
    mctx.restore();
  }

  function collectPoints() {
    drawMask();
    const density = Number(controls.density.value);
    const step = Math.max(4, Math.round(22 - density / 5.6));
    const data = mctx.getImageData(0, 0, mask.width, mask.height).data;
    const points = [];
    const w = mask.width;
    const h = mask.height;
    for (let y = step; y < h - step; y += step) {
      for (let x = step; x < w - step; x += step) {
        const a = data[(y * w + x) * 4 + 3];
        if (a < 40) continue;
        const left = data[(y * w + Math.max(0, x - step)) * 4 + 3];
        const right = data[(y * w + Math.min(w - 1, x + step)) * 4 + 3];
        const up = data[(Math.max(0, y - step) * w + x) * 4 + 3];
        const down = data[(Math.min(h - 1, y + step) * w + x) * 4 + 3];
        points.push({ x, y, edge: left < 40 || right < 40 || up < 40 || down < 40, n: rng() });
      }
    }
    lastPoints = points.slice(0, 11000);
    return lastPoints;
  }

  function drawField(t) {
    const w = canvas.width;
    const h = canvas.height;
    const amp = Number(controls.amplitude.value) * Math.min(w, h) / 1250;
    const depth = Number(controls.depth.value) * Math.min(w, h) / 900;
    const points = collectPoints();

    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    ctx.lineCap = "round";
    ctx.shadowColor = rgba(controls.primaryColor.value, .65);
    ctx.shadowBlur = 10;

    points.forEach((p, i) => {
      const cx = p.x - w / 2;
      const cy = p.y - h / 2;
      const wave = Math.sin(cx * .012 + t * 1.4 + p.n * 6) + Math.cos(cy * .015 - t * 1.1);
      const z = Math.sin(cx * .006 + cy * .004 + t * 1.2 + p.n * 3) * depth;
      const scale = 1 + z * .0014;
      const x = w / 2 + cx * scale + wave * amp * .48;
      const y = h / 2 + cy * scale + Math.cos(t + cx * .007) * amp * .26 - z * .16;
      const angle = Math.atan2(Math.cos(cy * .016 + t), Math.sin(cx * .014 - t));
      const len = (p.edge ? 10 : 5) + Math.abs(z) * .025 + amp * .05;

      ctx.beginPath();
      ctx.moveTo(x - Math.cos(angle) * len, y - Math.sin(angle) * len);
      ctx.lineTo(x + Math.cos(angle) * len, y + Math.sin(angle) * len);
      ctx.lineWidth = p.edge ? 1.55 : .9;
      ctx.strokeStyle = palette(i, points.length, p.edge ? .82 : .46);
      ctx.stroke();

      if (i % 11 === 0) {
        ctx.beginPath();
        ctx.arc(x, y, p.edge ? 1.9 : 1.15, 0, Math.PI * 2);
        ctx.fillStyle = palette(i + 3, points.length, .62);
        ctx.fill();
      }
    });
    ctx.restore();

    return "采样点 " + points.length;
  }

  function repeatedChars() {
    const text = controls.textInput.value.trim() || "TYPE";
    const chars = splitGraphemes(text.replace(/\s+/g, ""));
    return chars.length ? chars : ["T", "Y", "P", "E"];
  }

  function drawSpiral(t) {
    const w = canvas.width;
    const h = canvas.height;
    const chars = repeatedChars();
    const density = Number(controls.density.value);
    const amp = Number(controls.amplitude.value) / 100;
    const depth = Number(controls.depth.value) / 100;
    const maxR = Math.min(w, h) * .42;
    const ringCount = Math.max(3, Math.round(3 + depth * 6));
    const fontBase = Math.max(18, Number(controls.fontSize.value) * .23);

    ctx.save();
    ctx.translate(w / 2, h / 2);
    ctx.globalCompositeOperation = "lighter";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.shadowBlur = 9;
    ctx.shadowColor = rgba(controls.accentColor.value, .5);

    for (let rIndex = 0; rIndex < ringCount; rIndex++) {
      const ringT = (rIndex + 1) / ringCount;
      const radius = lerp(maxR * .16, maxR, ringT);
      const segments = Math.max(24, Math.round(radius / 9 + density * .55));
      const spin = t * (.12 + Number(controls.speed.value) / 360) * (rIndex % 2 ? -1 : 1);
      const bandWidth = Math.max(12, fontBase * 1.18);

      ctx.save();
      ctx.rotate(spin + rIndex * .34);
      ctx.lineWidth = bandWidth;
      ctx.lineCap = "round";
      for (let s = 0; s < segments; s += Math.max(3, Math.round(segments / 14))) {
        const a0 = (s / segments) * Math.PI * 2;
        const a1 = ((s + Math.max(1, Math.round(segments / 20))) / segments) * Math.PI * 2;
        ctx.beginPath();
        ctx.arc(0, 0, radius + Math.sin(t + s) * amp * 20, a0, a1);
        ctx.strokeStyle = palette(s + rIndex, segments, controls.paletteMode.value === "multi" ? .78 : .28);
        ctx.stroke();
      }

      ctx.font = fontString(fontBase * lerp(.8, 1.12, ringT), 900);
      for (let i = 0; i < segments; i++) {
        const a = (i / segments) * Math.PI * 2;
        const wave = Math.sin(a * 3 + t * 1.8 + rIndex) * amp * 22;
        const x = Math.cos(a) * (radius + wave);
        const y = Math.sin(a) * (radius + wave);
        ctx.save();
        ctx.translate(x, y);
        ctx.rotate(a + Math.PI / 2);
        ctx.fillStyle = palette(i + rIndex, segments, .88);
        ctx.fillText(chars[(i + rIndex) % chars.length], 0, 0);
        ctx.restore();
      }
      ctx.restore();
    }
    ctx.restore();

    return "环数 " + ringCount + " · 字符 " + chars.length;
  }

  function drawBands(t) {
    const w = canvas.width;
    const h = canvas.height;
    const chars = repeatedChars();
    const density = Number(controls.density.value);
    const amp = Number(controls.amplitude.value) * h / 900;
    const bandCount = Math.max(4, Math.round(4 + Number(controls.depth.value) / 14));
    const size = Math.max(18, Number(controls.fontSize.value) * .21);
    const step = Math.max(28, 96 - density * .55);

    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = fontString(size, 900);
    ctx.shadowBlur = 8;
    ctx.shadowColor = rgba(controls.primaryColor.value, .55);

    for (let b = 0; b < bandCount; b++) {
      const yBase = h * ((b + 1) / (bandCount + 1));
      const phase = t * (0.9 + b * .06) + b * 1.37;
      ctx.beginPath();
      for (let x = -60; x <= w + 60; x += 18) {
        const y = yBase + Math.sin(x * .008 + phase) * amp + Math.cos(x * .014 - phase * .7) * amp * .36;
        if (x === -60) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.lineWidth = Math.max(22, size * 1.4);
      ctx.strokeStyle = palette(b, bandCount, controls.paletteMode.value === "multi" ? .42 : .2);
      ctx.stroke();

      for (let x = -40, i = 0; x < w + 40; x += step, i++) {
        const y = yBase + Math.sin(x * .008 + phase) * amp + Math.cos(x * .014 - phase * .7) * amp * .36;
        const y2 = yBase + Math.sin((x + 8) * .008 + phase) * amp + Math.cos((x + 8) * .014 - phase * .7) * amp * .36;
        const angle = Math.atan2(y2 - y, 8);
        ctx.save();
        ctx.translate(x + ((t * 28 + b * 17) % step), y);
        ctx.rotate(angle);
        ctx.fillStyle = palette(i + b, Math.round(w / step), .92);
        ctx.fillText(chars[(i + b) % chars.length], 0, 0);
        ctx.restore();
      }
    }
    ctx.restore();

    return "条带 " + bandCount + " · 字符 " + chars.length;
  }

  function drawHud() {
    const w = canvas.width;
    const h = canvas.height;
    ctx.save();
    ctx.globalAlpha = .58;
    ctx.fillStyle = rgba(controls.primaryColor.value, .12);
    ctx.strokeStyle = rgba(controls.accentColor.value, .35);
    ctx.lineWidth = 1;
    ctx.strokeRect(w * .045, h * .07, w * .12, h * .34);
    ctx.strokeRect(w * .82, h * .68, w * .12, h * .12);
    for (let i = 0; i < 12; i++) {
      const y = h * .095 + i * h * .023;
      ctx.fillRect(w * .06, y, w * (.045 + (i % 5) * .007), Math.max(2, h * .004));
    }
    ctx.restore();
  }

  function render(time) {
    const t = (time || 0) / 1000 * (0.25 + Number(controls.speed.value) / 65);
    updateValues();
    paintBackground(t);

    let meta = "";
    if (controls.motionMode.value === "spiral") meta = drawSpiral(t);
    else if (controls.motionMode.value === "bands") meta = drawBands(t);
    else meta = drawField(t);

    drawHud();
    document.getElementById("stageMeta").textContent =
      meta + " · " + canvas.width + "×" + canvas.height;

    if (running) requestAnimationFrame(render);
  }

  function restart() {
    seed += 97;
    resize();
    if (!running) {
      render(performance.now());
    }
  }

  Object.values(controls).forEach(el => {
    if (!el || el === controls.fontFile) return;
    el.addEventListener("input", restart);
    el.addEventListener("change", restart);
  });

  document.querySelectorAll("[data-text]").forEach(btn => {
    btn.addEventListener("click", () => {
      controls.textInput.value = btn.dataset.text || "";
      restart();
    });
  });

  document.getElementById("pauseBtn").addEventListener("click", () => {
    running = !running;
    document.getElementById("pauseBtn").textContent = running ? "暂停" : "继续";
    document.getElementById("stateNote").textContent = running ? "正在生成动态图形。" : "已暂停，可下载当前画面。";
    if (running) requestAnimationFrame(render);
  });

  document.getElementById("shuffleBtn").addEventListener("click", () => {
    seed = Math.floor(Math.random() * 4294967295);
    const modes = ["field", "spiral", "bands"];
    controls.motionMode.value = modes[Math.floor(Math.random() * modes.length)];
    controls.paletteMode.value = Math.random() > .45 ? "multi" : "duo";
    controls.density.value = String(38 + Math.floor(Math.random() * 52));
    controls.amplitude.value = String(28 + Math.floor(Math.random() * 58));
    controls.depth.value = String(24 + Math.floor(Math.random() * 68));
    controls.speed.value = String(24 + Math.floor(Math.random() * 58));
    restart();
  });

  document.getElementById("downloadBtn").addEventListener("click", () => {
    const a = document.createElement("a");
    a.download = "text_motion_graphics.png";
    a.href = canvas.toDataURL("image/png");
    a.click();
  });

  controls.fontFile.addEventListener("change", async () => {
    const file = controls.fontFile.files && controls.fontFile.files[0];
    if (!file) {
      customFontFamily = "";
      restart();
      return;
    }
    const family = "UserMotionFont_" + Date.now();
    const data = await file.arrayBuffer();
    const face = new FontFace(family, data);
    await face.load();
    document.fonts.add(face);
    customFontFamily = "'" + family + "'";
    restart();
  });

  window.addEventListener("resize", restart);
  resize();
  requestAnimationFrame(render);
})();
</script>
</body>
</html>
""")


_TEXT_MOTION_HOME_ENTRY = r"""
<section id="textMotionFeatureEntry" style="
  margin: 18px 0;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  background: #f8fafc;
  padding: 14px 16px;
">
  <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
    <div>
      <div style="font-weight:800;font-size:16px;color:#0f172a;">文字动态图形 / 数据雕塑</div>
      <div style="font-size:13px;color:#64748b;margin-top:4px;">输入汉字、英文或传统蒙古文，生成点阵流场、螺旋环、波纹条带动态图形。</div>
    </div>
    <a href="/text_motion" target="_blank" style="
      display:inline-flex;
      min-height:36px;
      align-items:center;
      justify-content:center;
      padding:8px 12px;
      border-radius:6px;
      background:#0f172a;
      color:#fff;
      text-decoration:none;
      font-weight:700;
      font-size:13px;
    ">打开新功能</a>
  </div>
</section>
"""


@app.middleware("http")
async def _text_motion_home_entry_inject(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    if "textMotionFeatureEntry" not in html:
        marker = '<form id="form">'
        if marker in html:
            html = html.replace(marker, _TEXT_MOTION_HOME_ENTRY + "\n" + marker, 1)
        elif "</body>" in html:
            html = html.replace("</body>", _TEXT_MOTION_HOME_ENTRY + "\n</body>")
        else:
            html += _TEXT_MOTION_HOME_ENTRY

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return HTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers,
    )

# ================= TEXT_MOTION_GRAPHICS_FEATURE_END =================

# ================= LORA_LAB_FEATURE_START =================
import base64 as _lora_base64
import importlib.util as _lora_importlib_util
import io as _lora_io
import json as _lora_json
import os as _lora_os
import re as _lora_re
import shutil as _lora_shutil
import subprocess as _lora_subprocess
import threading as _lora_threading
import time as _lora_time
import uuid as _lora_uuid
import urllib.error as _lora_urlerror
import urllib.request as _lora_urlrequest
from pathlib import Path as _lora_Path
from typing import List as _lora_List

from fastapi import File as _lora_File
from fastapi import Form as _lora_Form
from fastapi import Request as _lora_Request
from fastapi import UploadFile as _lora_UploadFile
from fastapi.responses import FileResponse as _lora_FileResponse
from fastapi.responses import JSONResponse as _lora_JSONResponse
from PIL import Image as _lora_Image
from PIL import ImageOps as _lora_ImageOps

_LORA_ROOT = _lora_Path(__file__).resolve().parent / "lora_workspace"
_LORA_JOBS_DIR = _LORA_ROOT / "jobs"
_LORA_MIN_IMAGES = int(_lora_os.environ.get("LORA_MIN_IMAGES", "8"))
_LORA_HARD_MAX_IMAGES = int(_lora_os.environ.get("LORA_MAX_IMAGES", "80"))
_LORA_MAX_FILE_MB = int(_lora_os.environ.get("LORA_MAX_FILE_MB", "25"))
_LORA_MAX_TOTAL_UPLOAD_MB = int(_lora_os.environ.get("LORA_MAX_TOTAL_UPLOAD_MB", "1800"))
_LORA_RESERVED_DISK_BYTES = int(_lora_os.environ.get("LORA_RESERVED_DISK_GB", "8")) * 1024**3
_LORA_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_LORA_JOBS = {}
_LORA_LOCK = _lora_threading.Lock()


def _lora_ensure_dirs():
    _LORA_JOBS_DIR.mkdir(parents=True, exist_ok=True)


def _lora_now():
    return _lora_time.strftime("%Y-%m-%d %H:%M:%S")


def _lora_slug(value, fallback="lora_job"):
    raw = str(value or "").strip().lower()
    raw = _lora_re.sub(r"[^a-z0-9._-]+", "_", raw)
    raw = raw.strip("._-")
    return (raw or fallback)[:60]


def _lora_job_dir(job_id):
    return _LORA_JOBS_DIR / _lora_slug(job_id, "job")


def _lora_job_json_path(job_id):
    return _lora_job_dir(job_id) / "job.json"


def _lora_public_job(job):
    public = dict(job or {})
    if "command" in public:
        public["command_preview"] = " ".join(str(x) for x in public.get("command") or [])
        public.pop("command", None)
    return public


def _lora_save_job(job):
    _lora_ensure_dirs()
    job_dir = _lora_job_dir(job["id"])
    job_dir.mkdir(parents=True, exist_ok=True)
    (_lora_job_json_path(job["id"])).write_text(
        _lora_json.dumps(job, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with _LORA_LOCK:
        _LORA_JOBS[job["id"]] = job


def _lora_load_job(job_id):
    with _LORA_LOCK:
        if job_id in _LORA_JOBS:
            return _LORA_JOBS[job_id]
    path = _lora_job_json_path(job_id)
    if not path.exists():
        return None
    try:
        job = _lora_json.loads(path.read_text(encoding="utf-8"))
        with _LORA_LOCK:
            _LORA_JOBS[job_id] = job
        return job
    except Exception:
        return None


def _lora_list_jobs():
    _lora_ensure_dirs()
    jobs = []
    for path in sorted(_LORA_JOBS_DIR.glob("*/job.json"), reverse=True):
        try:
            jobs.append(_lora_json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return jobs


def _lora_limits():
    _lora_ensure_dirs()
    usage = _lora_shutil.disk_usage(str(_LORA_ROOT))
    max_file_bytes = _LORA_MAX_FILE_MB * 1024 * 1024
    usable = max(0, usage.free - _LORA_RESERVED_DISK_BYTES)
    per_image_budget = max_file_bytes * 3
    max_by_disk = usable // per_image_budget if per_image_budget else _LORA_HARD_MAX_IMAGES
    max_images = int(max(0, min(_LORA_HARD_MAX_IMAGES, max_by_disk)))
    if max_images >= _LORA_MIN_IMAGES:
        trainable = True
    else:
        trainable = False
        max_images = max(_LORA_MIN_IMAGES, max_images)
    max_total = min(_LORA_MAX_TOTAL_UPLOAD_MB, max_images * _LORA_MAX_FILE_MB)
    return {
        "min_images": _LORA_MIN_IMAGES,
        "max_images": max_images,
        "hard_max_images": _LORA_HARD_MAX_IMAGES,
        "max_file_mb": _LORA_MAX_FILE_MB,
        "max_total_upload_mb": max_total,
        "reserved_disk_gb": _LORA_RESERVED_DISK_BYTES // 1024**3,
        "disk_free_gb": round(usage.free / 1024**3, 2),
        "disk_total_gb": round(usage.total / 1024**3, 2),
        "capacity_trainable": trainable,
    }


def _lora_dep(name):
    return bool(_lora_importlib_util.find_spec(name))


def _lora_run_text(cmd, timeout=8):
    try:
        out = _lora_subprocess.check_output(
            cmd,
            stderr=_lora_subprocess.STDOUT,
            timeout=timeout,
            text=True,
        )
        return out.strip()
    except Exception as e:
        return str(e)


def _lora_find_kohya():
    candidates = [
        _lora_os.environ.get("KOHYA_SS_DIR", ""),
        "/root/autodl-tmp/kohya_ss",
        "/root/kohya_ss",
        str(_lora_Path(__file__).resolve().parent / "kohya_ss"),
    ]
    for item in candidates:
        if not item:
            continue
        root = _lora_Path(item)
        if (root / "sd-scripts" / "train_network.py").exists():
            return {"root": str(root), "script": str(root / "sd-scripts" / "train_network.py")}
        if (root / "train_network.py").exists():
            return {"root": str(root), "script": str(root / "train_network.py")}
    return None


def _lora_find_diffusers_script():
    candidates = [
        _lora_os.environ.get("DIFFUSERS_LORA_TRAIN_SCRIPT", ""),
        str(_lora_Path(__file__).resolve().parent / "tools" / "train_text_to_image_lora.py"),
        "/root/autodl-tmp/diffusers/examples/text_to_image/train_text_to_image_lora.py",
        "/root/diffusers/examples/text_to_image/train_text_to_image_lora.py",
    ]
    for item in candidates:
        if item and _lora_Path(item).exists():
            return item
    return None


def _lora_local_lora_dirs():
    raw = [
        _lora_os.environ.get("SD_LORA_DIR", ""),
        _lora_os.environ.get("LORA_OUTPUT_DIR", ""),
        "/root/autodl-tmp/models/lora",
        str(_lora_Path(__file__).resolve().parent / "models" / "lora"),
    ]
    dirs = []
    seen = set()
    for item in raw:
        if not item:
            continue
        path = _lora_Path(item).expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        dirs.append(path)
    return dirs


def _lora_scan_local_loras():
    items = []
    seen = set()
    for folder in _lora_local_lora_dirs():
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*")):
            if path.suffix.lower() not in {".safetensors", ".pt", ".bin"}:
                continue
            if path.stem in seen:
                continue
            seen.add(path.stem)
            meta = {}
            meta_path = path.with_suffix(".json")
            if meta_path.exists():
                try:
                    meta = _lora_json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            label = meta.get("label") or meta.get("alias") or path.stem
            trigger = meta.get("trigger") or meta.get("trigger_word") or path.stem
            items.append({
                "name": path.stem,
                "alias": label,
                "path": str(path),
                "source": "local_scan",
                "metadata": meta,
                "trigger": trigger,
                "bytes": path.stat().st_size,
            })
    return items


def _lora_default_material_style_packs():
    return [
        {
            "id": "milk_glyph_style",
            "label": "牛奶字形",
            "style": "milk",
            "prompt": "glyphstyle, creamy white milk liquid material, glossy thick fluid surface, soft white highlights, milky splash texture, smooth bright raised glyph",
        },
        {
            "id": "water_glyph_style",
            "label": "水流字形",
            "style": "water",
            "prompt": "glyphstyle, transparent blue water material, flowing liquid surface, refraction, ripple texture, glossy wet highlights, bright reflective raised glyph",
        },
        {
            "id": "fire_glyph_style",
            "label": "火焰字形",
            "style": "fire",
            "prompt": "glyphstyle, orange flame material, glowing ember texture, burning red and yellow surface, hot luminous highlights, sparks around raised glyph",
        },
        {
            "id": "ice_glyph_style",
            "label": "冰块字形",
            "style": "ice",
            "prompt": "glyphstyle, transparent cyan blue ice crystal material, frozen glass refraction, frosted crystalline texture, bright cold highlights, raised icy glyph",
        },
        {
            "id": "jade_glyph_style",
            "label": "玉石字形",
            "style": "jade",
            "prompt": "glyphstyle, translucent green jade material, polished gemstone surface, cloudy mineral texture, soft inner glow, carved raised glyph",
        },
        {
            "id": "gold_glyph_style",
            "label": "鎏金字形",
            "style": "gold",
            "prompt": "glyphstyle, shiny gold foil material, metallic golden surface, hammered foil texture, strong warm highlights, raised luxury glyph",
        },
        {
            "id": "ceramic_glyph_style",
            "label": "青瓷字形",
            "style": "ceramic",
            "prompt": "glyphstyle, pale celadon ceramic glaze material, porcelain crackle texture, glossy smooth surface, soft green blue highlights, raised glazed glyph",
        },
        {
            "id": "glass_glyph_style",
            "label": "玻璃字形",
            "style": "glass",
            "prompt": "glyphstyle, transparent glass material, clear refraction, sharp glossy highlights, subtle blue edges, raised glass glyph",
        },
        {
            "id": "neon_glyph_style",
            "label": "霓虹字形",
            "style": "neon",
            "prompt": "glyphstyle, neon tube material, glowing cyan magenta light, luminous edge halo, dark glossy core, raised electric glyph",
        },
        {
            "id": "silk_glyph_style",
            "label": "丝绸字形",
            "style": "silk",
            "prompt": "glyphstyle, red silk fabric material, soft woven texture, satin sheen, flowing textile highlights, raised embroidered glyph",
        },
        {
            "id": "wood_glyph_style",
            "label": "木纹字形",
            "style": "wood",
            "prompt": "glyphstyle, polished wood grain material, carved walnut surface, warm brown rings, tactile relief, raised wooden glyph",
        },
        {
            "id": "lava_glyph_style",
            "label": "岩浆字形",
            "style": "lava",
            "prompt": "glyphstyle, molten lava material, black volcanic crust, bright orange glowing cracks, hot magma texture, dramatic raised glyph",
        },
        {
            "id": "marble_glyph_style",
            "label": "大理石字形",
            "style": "marble",
            "prompt": "glyphstyle, polished white marble stone material, gray veined texture, carved sculptural surface, cold stone highlights, raised glyph",
        },
        {
            "id": "pearl_glyph_style",
            "label": "珍珠字形",
            "style": "pearl",
            "prompt": "glyphstyle, mother of pearl material, iridescent nacre surface, creamy pearlescent highlights, soft rainbow sheen, raised glyph",
        },
        {
            "id": "candy_glyph_style",
            "label": "糖果玻璃字形",
            "style": "candy",
            "prompt": "glyphstyle, glossy candy glass material, colorful translucent stripes, sticky sugar shine, thick jelly surface, raised glyph",
        },
    ]


def _lora_material_style_pack_dirs():
    raw = [
        _lora_os.environ.get("LORA_STYLE_PACK_DIR", ""),
        "/root/autodl-tmp/models/lora_style_packs",
        str(_lora_Path(__file__).resolve().parent / "lora_style_packs"),
    ]
    dirs = []
    seen = set()
    for item in raw:
        if not item:
            continue
        path = _lora_Path(item).expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        dirs.append(path)
    return dirs


def _lora_ensure_default_material_style_pack():
    dirs = _lora_material_style_pack_dirs()
    if not dirs:
        return
    primary = dirs[0]
    try:
        primary.mkdir(parents=True, exist_ok=True)
        target = primary / "default_material_styles.json"
        if not target.exists():
            target.write_text(
                _lora_json.dumps(_lora_default_material_style_packs(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception as e:
        print("[LORA-STYLE-PACK][WARN]", repr(e))


def _lora_scan_material_style_packs():
    _lora_ensure_default_material_style_pack()
    items = []
    seen = set()
    for folder in _lora_material_style_pack_dirs():
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                data = _lora_json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                data = data.get("styles") or data.get("items") or [data]
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                style_id = str(item.get("id") or item.get("style") or item.get("label") or "").strip()
                prompt = str(item.get("prompt") or "").strip()
                if not style_id or not prompt or style_id in seen:
                    continue
                seen.add(style_id)
                normalized = {
                    "id": style_id,
                    "label": str(item.get("label") or style_id),
                    "style": str(item.get("style") or style_id),
                    "prompt": prompt,
                    "kind": "style_pack",
                    "source": "data_disk_style_pack",
                    "path": str(path),
                    "persistent": True,
                }
                items.append(normalized)
    return items


def _lora_env_info():
    limits = _lora_limits()
    gpu = _lora_run_text([
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.free",
        "--format=csv,noheader",
    ])
    deps = {
        "torch": _lora_dep("torch"),
        "diffusers": _lora_dep("diffusers"),
        "transformers": _lora_dep("transformers"),
        "accelerate": _lora_dep("accelerate"),
        "safetensors": _lora_dep("safetensors"),
        "PIL": _lora_dep("PIL"),
    }
    kohya = _lora_find_kohya()
    diffusers_script = _lora_find_diffusers_script()
    accelerate_bin = _lora_shutil.which("accelerate")
    python_bin = _lora_shutil.which("python") or "/root/miniconda3/bin/python"
    default_base_model = "/root/autodl-tmp/models/sd15"
    base_model = _lora_os.environ.get("LORA_BASE_MODEL", "")
    if not base_model and _lora_Path(default_base_model).exists():
        base_model = default_base_model
    sd_api = _lora_os.environ.get("SD_API_URL", "http://127.0.0.1:7860")
    sd_api_options = None
    try:
        sd_api_options = _lora_sd_api_call(sd_api, "/sdapi/v1/options", timeout=3)
    except Exception:
        sd_api_options = None
    local_loras = _lora_scan_local_loras()
    material_style_packs = _lora_scan_material_style_packs()
    if sd_api_options is None and local_loras:
        sd_api_options = {
            "sd_model_checkpoint": base_model,
            "sd_lora_dir": str(_lora_local_lora_dirs()[0]) if _lora_local_lora_dirs() else "",
            "loras": local_loras,
            "loaded_loras": [],
            "source": "local_scan_fallback",
        }
    return {
        "limits": limits,
        "gpu": gpu,
        "deps": deps,
        "kohya": kohya,
        "diffusers_script": diffusers_script,
        "accelerate_bin": accelerate_bin,
        "python_bin": python_bin,
        "base_model_env": base_model,
        "sd_api_default": sd_api,
        "local_loras": local_loras,
        "local_lora_dirs": [str(x) for x in _lora_local_lora_dirs()],
        "material_style_packs": material_style_packs,
        "material_style_pack_dirs": [str(x) for x in _lora_material_style_pack_dirs()],
        "sd_api_options": sd_api_options,
        "kohya_ready": bool(kohya and accelerate_bin),
        "diffusers_ready": bool(diffusers_script and deps["diffusers"] and deps["transformers"] and deps["accelerate"] and deps["safetensors"]),
    }


def _lora_validate_base_model(base_model):
    value = str(base_model or "").strip()
    if not value:
        return False, "请填写基础 SD 模型路径或 Hugging Face 模型名。"
    if value.startswith("/") and not _lora_Path(value).exists():
        return False, "基础模型路径不存在。"
    return True, ""


def _lora_job_log_tail(job, lines=120):
    log_path = _lora_Path(job.get("log_path", ""))
    if not log_path.exists():
        return ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        return "\n".join(text.splitlines()[-lines:])
    except Exception as e:
        return str(e)


def _lora_collect_outputs(job):
    output_dir = _lora_Path(job.get("output_dir", ""))
    if not output_dir.exists():
        return []
    items = []
    for path in sorted(output_dir.glob("*")):
        if path.suffix.lower() in {".safetensors", ".ckpt", ".pt", ".png", ".jpg", ".jpeg"}:
            items.append({
                "name": path.name,
                "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
                "url": f"/api/lora/file/{job['id']}/{path.name}",
            })
    return items


def _lora_build_train_command(job, backend, base_model, max_train_steps, resolution, rank, learning_rate):
    env = _lora_env_info()
    ok, reason = _lora_validate_base_model(base_model)
    if not ok:
        return None, None, reason

    dataset_dir = _lora_Path(job["dataset_dir"])
    output_dir = _lora_Path(job["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _lora_slug(job.get("project_name"), "glyph_style_lora")

    if backend == "kohya":
        if not env["kohya_ready"]:
            return None, None, "kohya_ss 未就绪：请安装 kohya_ss/sd-scripts，并确保 accelerate 命令可用。"
        kohya = env["kohya"]
        cmd = [
            env["accelerate_bin"],
            "launch",
            "--num_cpu_threads_per_process=2",
            kohya["script"],
            "--pretrained_model_name_or_path=" + base_model,
            "--train_data_dir=" + str(dataset_dir),
            "--resolution=" + str(resolution),
            "--output_dir=" + str(output_dir),
            "--output_name=" + safe_name,
            "--network_module=networks.lora",
            "--network_dim=" + str(rank),
            "--network_alpha=" + str(rank),
            "--train_batch_size=1",
            "--max_train_steps=" + str(max_train_steps),
            "--learning_rate=" + str(learning_rate),
            "--mixed_precision=fp16",
            "--save_model_as=safetensors",
            "--caption_extension=.txt",
            "--enable_bucket",
            "--bucket_no_upscale",
            "--cache_latents",
        ]
        return cmd, kohya["root"], ""

    if backend == "diffusers":
        if not env["diffusers_ready"]:
            return None, None, "diffusers 训练脚本或依赖未就绪：需要 diffusers、transformers、accelerate、safetensors 和 train_text_to_image_lora.py。"
        runner = [env["accelerate_bin"], "launch"] if env["accelerate_bin"] else [env["python_bin"]]
        cmd = runner + [
            env["diffusers_script"],
            "--pretrained_model_name_or_path=" + base_model,
            "--train_data_dir=" + str(dataset_dir),
            "--resolution=" + str(resolution),
            "--output_dir=" + str(output_dir),
            "--output_name=" + safe_name,
            "--rank=" + str(rank),
            "--train_batch_size=1",
            "--max_train_steps=" + str(max_train_steps),
            "--learning_rate=" + str(learning_rate),
            "--mixed_precision=no",
            "--checkpointing_steps=500",
            "--caption_extension=.txt",
        ]
        return cmd, str(_lora_Path(env["diffusers_script"]).parent), ""

    return None, None, "未知训练后端。"


def _lora_train_worker(job_id, command, cwd):
    job = _lora_load_job(job_id)
    if not job:
        return
    log_path = _lora_Path(job["log_path"])
    try:
        job["status"] = "training"
        job["message"] = "训练进程已启动。"
        job["started_at"] = _lora_now()
        job["command"] = command
        _lora_save_job(job)
        with log_path.open("a", encoding="utf-8", errors="ignore") as log:
            log.write("\n[" + _lora_now() + "] start training\n")
            log.write(" ".join(command) + "\n\n")
            log.flush()
            proc = _lora_subprocess.Popen(
                command,
                cwd=cwd,
                stdout=log,
                stderr=_lora_subprocess.STDOUT,
                text=True,
            )
            job["pid"] = proc.pid
            _lora_save_job(job)
            code = proc.wait()
            job["finished_at"] = _lora_now()
            job["return_code"] = code
            job["outputs"] = _lora_collect_outputs(job)
            if code == 0:
                job["status"] = "done"
                job["message"] = "训练完成。"
            else:
                job["status"] = "error"
                job["message"] = "训练进程退出，返回码：" + str(code)
            _lora_save_job(job)
    except Exception as e:
        job["status"] = "error"
        job["message"] = str(e)
        job["finished_at"] = _lora_now()
        _lora_save_job(job)


def _lora_sd_api_call(sd_url, path, payload=None, timeout=30):
    root = str(sd_url or "http://127.0.0.1:7860").rstrip("/")
    url = root + path
    data = None
    headers = {}
    if payload is not None:
        data = _lora_json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = _lora_urlrequest.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with _lora_urlrequest.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        ctype = resp.headers.get("content-type", "")
    if "application/json" in ctype:
        return _lora_json.loads(raw.decode("utf-8", errors="ignore"))
    return {"raw": raw.decode("utf-8", errors="ignore")}


def _lora_sd_api_is_reachable(sd_url=None, timeout=2):
    try:
        _lora_sd_api_call(sd_url or _lora_os.environ.get("SD_API_URL", "http://127.0.0.1:7860"), "/sdapi/v1/options", timeout=timeout)
        return True
    except Exception:
        return False


def _lora_autostart_sd_api():
    flag = str(_lora_os.environ.get("LORA_AUTOSTART_SD_API", "1")).strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return

    sd_url = _lora_os.environ.get("SD_API_URL", "http://127.0.0.1:7860")
    if "127.0.0.1" not in sd_url and "localhost" not in sd_url:
        return
    if _lora_sd_api_is_reachable(sd_url, timeout=1):
        return

    root = _lora_Path(__file__).resolve().parent
    server_py = root / "sd_api_server.py"
    model_dir = _lora_Path(_lora_os.environ.get("SD_MODEL_DIR", "/root/autodl-tmp/models/sd15"))
    if not server_py.exists() or not model_dir.exists():
        print("[LORA-SD-AUTOSTART] skip: sd_api_server.py or local SD model missing")
        return

    port_match = _lora_re.search(r":(\d+)(?:/|$)", sd_url)
    port = port_match.group(1) if port_match else _lora_os.environ.get("SD_API_PORT", "7860")
    python_bin = _lora_shutil.which("python") or "/root/miniconda3/bin/python"
    log_path = _lora_Path(_lora_os.environ.get("SD_API_LOG", "/tmp/font_morph_sd_api_7860.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        python_bin,
        "-m",
        "uvicorn",
        "sd_api_server:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    try:
        log = log_path.open("a", encoding="utf-8", errors="ignore")
        log.write("\n[" + _lora_now() + "] auto-start SD API: " + " ".join(command) + "\n")
        log.flush()
        proc = _lora_subprocess.Popen(
            command,
            cwd=str(root),
            stdout=log,
            stderr=_lora_subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        print(f"[LORA-SD-AUTOSTART] started pid={proc.pid} port={port}")
    except Exception as e:
        print("[LORA-SD-AUTOSTART][ERROR]", repr(e))


def _local_ai_api_call(path, payload=None, timeout=8):
    ai_url = _lora_os.environ.get("LOCAL_DEEPSEEK_URL", "http://127.0.0.1:8001").rstrip("/")
    data = None
    headers = {}
    if payload is not None:
        data = _lora_json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = _lora_urlrequest.Request(ai_url + path, data=data, headers=headers)
    with _lora_urlrequest.urlopen(req, timeout=timeout) as resp:
        return _lora_json.loads(resp.read().decode("utf-8", errors="ignore"))


def _local_ai_is_reachable(timeout=1):
    try:
        _local_ai_api_call("/health", timeout=timeout)
        return True
    except Exception:
        return False


def _local_ollama_autostart():
    backend = _lora_os.environ.get("LOCAL_DEEPSEEK_BACKEND", "ollama").strip().lower()
    if backend != "ollama":
        return
    ollama_url = _lora_os.environ.get("LOCAL_DEEPSEEK_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        req = _lora_urlrequest.Request(ollama_url + "/api/tags")
        with _lora_urlrequest.urlopen(req, timeout=2):
            return
    except Exception:
        pass

    ollama_bin = _lora_shutil.which("ollama") or "/usr/local/bin/ollama"
    if not _lora_Path(ollama_bin).exists():
        print("[LOCAL-AI-AUTOSTART] skip: ollama binary missing")
        return
    log_path = _lora_Path(_lora_os.environ.get("OLLAMA_LOG", "/tmp/font_morph_ollama_11434.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = _lora_os.environ.copy()
    env.setdefault("OLLAMA_MODELS", "/root/autodl-tmp/models/ollama")
    env.setdefault("GGML_CPU_ALL_VARIANTS", "0")
    try:
        log = log_path.open("a", encoding="utf-8", errors="ignore")
        log.write("\n[" + _lora_now() + "] auto-start Ollama: " + ollama_bin + " serve\n")
        log.flush()
        proc = _lora_subprocess.Popen(
            [ollama_bin, "serve"],
            stdout=log,
            stderr=_lora_subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=env,
        )
        print(f"[LOCAL-AI-AUTOSTART] started Ollama pid={proc.pid}")
        _lora_time.sleep(3)
    except Exception as e:
        print("[LOCAL-AI-AUTOSTART][OLLAMA-ERROR]", repr(e))


def _local_ai_autostart():
    flag = str(_lora_os.environ.get("LOCAL_DEEPSEEK_AUTOSTART", "1")).strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return
    ai_url = _lora_os.environ.get("LOCAL_DEEPSEEK_URL", "http://127.0.0.1:8001")
    if "127.0.0.1" not in ai_url and "localhost" not in ai_url:
        return
    if _local_ai_is_reachable(timeout=1):
        return

    root = _lora_Path(__file__).resolve().parent
    server_py = root / "local_deepseek_server.py"
    backend = _lora_os.environ.get("LOCAL_DEEPSEEK_BACKEND", "ollama").strip().lower()
    model_dir = _lora_Path(_lora_os.environ.get("LOCAL_DEEPSEEK_MODEL_DIR", "/root/autodl-tmp/models/deepseek-r1-distill-qwen-7b"))
    if not server_py.exists():
        print("[LOCAL-AI-AUTOSTART] skip: local_deepseek_server.py missing")
        return
    if backend != "ollama" and not model_dir.exists():
        print("[LOCAL-AI-AUTOSTART] skip: local_deepseek_server.py or model dir missing")
        return
    _local_ollama_autostart()

    port_match = _lora_re.search(r":(\d+)(?:/|$)", ai_url)
    port = port_match.group(1) if port_match else _lora_os.environ.get("LOCAL_DEEPSEEK_PORT", "8001")
    python_bin = _lora_shutil.which("python") or "/root/miniconda3/bin/python"
    log_path = _lora_Path(_lora_os.environ.get("LOCAL_DEEPSEEK_LOG", "/tmp/font_morph_deepseek_8001.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = _lora_os.environ.copy()
    env.setdefault("LOCAL_DEEPSEEK_BACKEND", "ollama")
    env.setdefault("LOCAL_DEEPSEEK_MODEL_NAME", "deepseek-r1:32b")
    env.setdefault("LOCAL_DEEPSEEK_OLLAMA_URL", "http://127.0.0.1:11434")
    env.setdefault("LOCAL_DEEPSEEK_OLLAMA_TIMEOUT", "600")
    env.setdefault("LOCAL_DEEPSEEK_MODEL_DIR", str(model_dir))
    env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    command = [
        python_bin,
        "-m",
        "uvicorn",
        "local_deepseek_server:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    try:
        log = log_path.open("a", encoding="utf-8", errors="ignore")
        log.write("\n[" + _lora_now() + "] auto-start local DeepSeek: " + " ".join(command) + "\n")
        log.flush()
        proc = _lora_subprocess.Popen(
            command,
            cwd=str(root),
            stdout=log,
            stderr=_lora_subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=env,
        )
        print(f"[LOCAL-AI-AUTOSTART] started pid={proc.pid} port={port}")
    except Exception as e:
        print("[LOCAL-AI-AUTOSTART][ERROR]", repr(e))


@app.on_event("startup")
async def _lora_autostart_sd_api_on_startup():
    _lora_autostart_sd_api()
    _local_ai_autostart()


@app.get("/api/local_ai/status")
async def local_ai_status_api(load: int = 0):
    try:
        suffix = "/health?load=1" if load else "/health"
        result = _local_ai_api_call(suffix, timeout=180 if load else 5)
        return _lora_JSONResponse({"ok": bool(result.get("ok", True)), "service": result})
    except Exception as e:
        return _lora_JSONResponse({"ok": False, "error": str(e)}, status_code=503)


def _local_mongolian_qa_direct_answer(prompt: str):
    text = (prompt or "").strip()
    compact = text.replace(" ", "")
    if not compact:
        return None

    if (("词首" in compact or "词中" in compact or "词尾" in compact or "独立形" in compact)
            and ("为什么" in compact or "区别" in compact or "是什么" in compact or "怎么" in compact)):
        return (
            "传统蒙古文会有独立形、词首形、词中形、词尾形，核心原因是它是连写文字，字母的外形要根据前后是否连接来变化。\n\n"
            "独立形：这个字母左右两边都不和其他字母连接，通常单独出现。\n"
            "词首形：出现在词开头，通常只向后连接。\n"
            "词中形：出现在词中间，前后都要连接。\n"
            "词尾形：出现在词末尾，通常只向前连接。\n\n"
            "所以这些形态本质上是字形显现形式，也就是字体 shaping 的结果，不是语法时态，也不是几个互不相关的不同字母。"
            "Unicode 通常编码的是抽象字母，具体显示成哪一种形态，要由字体、OpenType 规则、变体选择符和上下文共同决定。"
        )

    lower_text = compact.lower()
    if (("unicode" in lower_text or "编码" in compact or "国标" in compact or "GB" in text)
            and ("蒙古文" in compact or "传统蒙古文" in compact)):
        return (
            "可以这样理解：Unicode 更偏向编码“抽象字符”，而中国传统蒙古文国标、字体公司的字形清单，更偏向整理“实际可见字形”。\n\n"
            "Unicode：主要记录基础字母、标点、控制字符、变体选择符等。它不把每一个词首、词中、词尾形都简单当成独立文字来处理，"
            "实际显示依赖字体和 shaping 引擎。\n"
            "GB/国标字形清单：更关注传统蒙古文在排版中真正需要显示的字形，包括基础字母的不同显现形式、变形显现字、强制合体字、"
            "非强制合体字等。\n"
            "字体公司规则：奥云、蒙科立等字体可能使用不同的 glyph name、PUA 映射和合体规则，所以不能只按 Unicode 简单对应，"
            "也不能把两家公司的表混用。"
        )

    if "奥云" in compact and ("蒙科立" in compact or "Menk" in text or "MLD" in text or "MAM" in text):
        return (
            "奥云和蒙科立不能混用同一套规则，原因是它们虽然都服务于传统蒙古文排版，但字体内部的 glyph 命名、PUA 映射、"
            "合体字组织方式和显现形式规则可能不同。\n\n"
            "做字体插值或可变字体时，正确流程应该是：先判断字体公司；再读取该公司的 GB/PUA/glyph name 对应表；"
            "只匹配同公司两边都真实存在的 glyph 轮廓；再做轮廓兼容、重采样、插值和可变字体生成。\n"
            "如果把奥云表套到蒙科立字体上，或者只按 Unicode 简单对应，就容易漏掉变形显现字、强制合体字和非强制合体字，"
            "也可能把错误的 glyph 当成同一个字形来插值。"
        )

    if "纹样" in compact and ("吉祥" in compact or "如意" in compact or "象征" in compact):
        return (
            "蒙古族文化中常用于表达吉祥如意的纹样，可以优先考虑乌力吉纹，也常被理解为盘长纹、吉祥结一类的连续纹样。"
            "它有连绵不断、圆满、安宁、福运延续的象征意味，很适合放在贺卡边框、角花、中心印章或标题下方装饰。\n\n"
            "另外也可以搭配云纹、犄纹、回纹等。云纹适合表达祥瑞、天空、草原气息；犄纹常有力量、守护、生命力的意味；"
            "回纹适合做连续边框，视觉上稳定、有秩序。具体使用时最好把纹样做成边框或角部装饰，不要压住传统蒙古文正文。"
        )

    if any(word in compact for word in ["蒙古包", "马", "哈达", "云纹"]):
        return (
            "这些元素在蒙古族文化设计里都有比较明确的象征方向：\n\n"
            "蒙古包：常象征家园、团圆、草原生活和待客传统。\n"
            "马：常象征速度、力量、自由、迁徙和草原精神。\n"
            "哈达：常用于礼仪场景，象征尊敬、祝福、纯洁和善意。\n"
            "云纹：常用于装饰边框和背景，表达祥瑞、天空、流动感和祝福氛围。\n\n"
            "如果用在贺卡或字体设计里，蒙古包和马适合做主体插画，哈达适合做祝福或礼仪点缀，云纹适合做边框和背景纹样。"
        )

    return None


@app.post("/api/local_ai/chat")
async def local_ai_chat_api(request: _lora_Request):
    data = await request.json()
    direct_answer = _local_mongolian_qa_direct_answer(data.get("prompt") or "")
    if direct_answer:
        return _lora_JSONResponse({
            "ok": True,
            "model": "本地传统蒙古文知识库 + DeepSeek 后备",
            "text": direct_answer,
            "loaded": True,
            "cuda": True,
        })
    payload = {
        "system": data.get("system") or "你是嵌入在字体工具网页里的传统蒙古文与蒙古族文化问答助手。只输出最终答案，不要输出思考过程。主要回答传统蒙古文书写、Unicode/GB 编码、字形变体、字体规则、蒙古族节日、纹样、礼俗、历史文化等相关问题。回答用中文，必要时补充传统蒙古文原文、转写或术语说明；不确定的内容要明确说不确定，不能编造。不要把回答写成 Stable Diffusion、LoRA 或绘图提示词。基础事实：传统蒙古文的位置变体来自前后连接环境和字体 shaping，不是语法时态；Unicode 编码抽象字符，GB/PUA/glyph name 清单更多对应实际可见字形和合体字；奥云和蒙科立等公司的字形命名与映射规则不能混用。",
        "messages": data.get("messages") or None,
        "prompt": data.get("prompt") or "",
        "max_new_tokens": int(data.get("max_new_tokens") or 512),
        "temperature": float(data.get("temperature") or 0.6),
        "top_p": float(data.get("top_p") or 0.9),
        "strip_thinking": True,
    }
    try:
        result = _local_ai_api_call("/chat", payload=payload, timeout=240)
        status = 200 if result.get("ok") else 503
        return _lora_JSONResponse(result, status_code=status)
    except Exception as e:
        return _lora_JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.get("/api/lora/env")
async def lora_env_api():
    return _lora_JSONResponse(_lora_env_info())


@app.get("/api/lora/jobs")
async def lora_jobs_api():
    return _lora_JSONResponse({"jobs": [_lora_public_job(x) for x in _lora_list_jobs()]})


@app.get("/api/lora/status/{job_id}")
async def lora_status_api(job_id: str):
    job = _lora_load_job(job_id)
    if not job:
        return _lora_JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    public = _lora_public_job(job)
    public["log_tail"] = _lora_job_log_tail(job)
    public["outputs"] = _lora_collect_outputs(job)
    return _lora_JSONResponse({"ok": True, "job": public})


@app.get("/api/lora/file/{job_id}/{filename}")
async def lora_file_api(job_id: str, filename: str):
    job = _lora_load_job(job_id)
    if not job:
        return _lora_JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    safe_name = _lora_Path(filename).name
    candidates = [
        _lora_Path(job.get("output_dir", "")) / safe_name,
        _lora_Path(job.get("dataset_dir", "")) / safe_name,
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            return _lora_FileResponse(str(path), filename=path.name)
    return _lora_JSONResponse({"ok": False, "error": "file not found"}, status_code=404)


@app.post("/api/lora/upload")
async def lora_upload_api(
    project_name: str = _lora_Form(default="Glyph Style LoRA"),
    trigger_word: str = _lora_Form(default="glyphstyle"),
    caption_hint: str = _lora_Form(default="typography style, glyph texture"),
    files: _lora_List[_lora_UploadFile] = _lora_File(...),
):
    limits = _lora_limits()
    count = len(files or [])
    if count < limits["min_images"]:
        return _lora_JSONResponse(
            {"ok": False, "error": f"图片太少，至少需要 {limits['min_images']} 张。"},
            status_code=400,
        )
    if count > limits["max_images"]:
        return _lora_JSONResponse(
            {"ok": False, "error": f"图片太多，本机当前最多允许 {limits['max_images']} 张。"},
            status_code=400,
        )
    if not limits["capacity_trainable"]:
        return _lora_JSONResponse(
            {"ok": False, "error": "数据盘剩余空间不足，当前不建议创建新的 LoRA 训练集。"},
            status_code=507,
        )

    job_id = "lora_" + _lora_time.strftime("%Y%m%d_%H%M%S") + "_" + _lora_uuid.uuid4().hex[:8]
    job_dir = _lora_job_dir(job_id)
    dataset_dir = job_dir / "dataset" / "10_style"
    output_dir = job_dir / "output"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    max_file_bytes = limits["max_file_mb"] * 1024 * 1024
    total_bytes = 0
    images = []
    caption = ", ".join(x for x in [trigger_word.strip(), caption_hint.strip()] if x)

    for idx, upload in enumerate(files, 1):
        suffix = _lora_Path(upload.filename or "").suffix.lower()
        if suffix not in _LORA_ALLOWED_EXTS:
            return _lora_JSONResponse({"ok": False, "error": f"不支持的文件格式：{upload.filename}"}, status_code=400)

        raw = await upload.read()
        total_bytes += len(raw)
        if len(raw) > max_file_bytes:
            return _lora_JSONResponse({"ok": False, "error": f"{upload.filename} 超过 {limits['max_file_mb']}MB。"}, status_code=400)
        if total_bytes > limits["max_total_upload_mb"] * 1024 * 1024:
            return _lora_JSONResponse({"ok": False, "error": "本次上传总大小超过限制。"}, status_code=400)

        try:
            img = _lora_Image.open(_lora_io.BytesIO(raw))
            img = _lora_ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            if img.mode == "RGBA":
                bg = _lora_Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
            img.thumbnail((1536, 1536), _lora_Image.Resampling.LANCZOS)
        except Exception as e:
            return _lora_JSONResponse({"ok": False, "error": f"{upload.filename} 不是有效图片：{e}"}, status_code=400)

        stem = f"{idx:03d}_{_lora_slug(_lora_Path(upload.filename or 'image').stem, 'image')}"
        out_img = dataset_dir / (stem + ".jpg")
        out_txt = dataset_dir / (stem + ".txt")
        img.save(out_img, quality=94, optimize=True)
        out_txt.write_text(caption + "\n", encoding="utf-8")
        images.append({
            "name": out_img.name,
            "caption": caption,
            "width": img.width,
            "height": img.height,
            "size_mb": round(out_img.stat().st_size / 1024 / 1024, 2),
        })

    job = {
        "id": job_id,
        "project_name": project_name.strip() or "Glyph Style LoRA",
        "trigger_word": trigger_word.strip() or "glyphstyle",
        "caption_hint": caption_hint.strip(),
        "status": "uploaded",
        "message": "素材已上传，等待开始训练。",
        "created_at": _lora_now(),
        "dataset_dir": str(dataset_dir),
        "output_dir": str(output_dir),
        "log_path": str(job_dir / "train.log"),
        "image_count": len(images),
        "images": images,
        "limits": limits,
    }
    _lora_save_job(job)
    return _lora_JSONResponse({"ok": True, "job": _lora_public_job(job)})


@app.post("/api/lora/train/{job_id}")
async def lora_train_api(
    job_id: str,
    backend: str = _lora_Form(default="diffusers"),
    base_model: str = _lora_Form(default=""),
    max_train_steps: int = _lora_Form(default=800),
    resolution: int = _lora_Form(default=512),
    rank: int = _lora_Form(default=16),
    learning_rate: str = _lora_Form(default="0.0001"),
):
    job = _lora_load_job(job_id)
    if not job:
        return _lora_JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    if job.get("status") == "training":
        return _lora_JSONResponse({"ok": False, "error": "该任务正在训练。"}, status_code=409)
    if int(job.get("image_count") or 0) < _LORA_MIN_IMAGES:
        return _lora_JSONResponse({"ok": False, "error": f"至少需要 {_LORA_MIN_IMAGES} 张图片才能训练。"}, status_code=400)

    base_model = base_model.strip() or _lora_os.environ.get("LORA_BASE_MODEL", "")
    max_train_steps = max(100, min(int(max_train_steps), 6000))
    resolution = max(384, min(int(resolution), 1024))
    rank = max(4, min(int(rank), 128))
    try:
        lr_value = float(learning_rate)
    except Exception:
        lr_value = 0.0001
    lr_value = max(0.000001, min(lr_value, 0.01))

    command, cwd, error = _lora_build_train_command(
        job,
        backend,
        base_model,
        max_train_steps,
        resolution,
        rank,
        lr_value,
    )
    if error:
        job["status"] = "blocked"
        job["message"] = error
        job["updated_at"] = _lora_now()
        _lora_save_job(job)
        return _lora_JSONResponse({"ok": False, "error": error, "env": _lora_env_info(), "job": _lora_public_job(job)}, status_code=409)

    job["backend"] = backend
    job["base_model"] = base_model
    job["train_params"] = {
        "max_train_steps": max_train_steps,
        "resolution": resolution,
        "rank": rank,
        "learning_rate": lr_value,
    }
    job["status"] = "queued"
    job["message"] = "训练已加入队列。"
    _lora_save_job(job)
    thread = _lora_threading.Thread(target=_lora_train_worker, args=(job_id, command, cwd), daemon=True)
    thread.start()
    return _lora_JSONResponse({"ok": True, "job": _lora_public_job(job)})


@app.get("/api/lora/sd_status")
async def lora_sd_status_api(sd_url: str = "http://127.0.0.1:7860"):
    try:
        result = _lora_sd_api_call(sd_url, "/sdapi/v1/options", timeout=5)
        return _lora_JSONResponse({"ok": True, "sd_url": sd_url, "options": result})
    except Exception as e:
        return _lora_JSONResponse({"ok": False, "sd_url": sd_url, "error": str(e)}, status_code=503)


@app.post("/api/lora/sd_generate")
async def lora_sd_generate_api(request: _lora_Request):
    data = await request.json()
    sd_url = data.get("sd_url") or "http://127.0.0.1:7860"
    payload = {
        "prompt": data.get("prompt") or "glyph typography, beautiful texture",
        "negative_prompt": data.get("negative_prompt") or "low quality, blurry, distorted",
        "steps": int(data.get("steps") or 24),
        "width": int(data.get("width") or 768),
        "height": int(data.get("height") or 768),
        "cfg_scale": float(data.get("cfg_scale") or 7),
        "sampler_name": data.get("sampler_name") or "DPM++ 2M Karras",
    }
    if data.get("seed") not in (None, ""):
        payload["seed"] = int(data.get("seed"))
    try:
        result = _lora_sd_api_call(sd_url, "/sdapi/v1/txt2img", payload=payload, timeout=180)
    except _lora_urlerror.HTTPError as e:
        return _lora_JSONResponse({"ok": False, "error": e.read().decode("utf-8", errors="ignore")}, status_code=502)
    except Exception as e:
        return _lora_JSONResponse({"ok": False, "error": str(e)}, status_code=503)

    images = result.get("images") or []
    saved = []
    if images:
        job_id = "sd_" + _lora_time.strftime("%Y%m%d_%H%M%S") + "_" + _lora_uuid.uuid4().hex[:8]
        out_dir = _lora_job_dir(job_id) / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        for idx, image_b64 in enumerate(images[:4], 1):
            raw = _lora_base64.b64decode(image_b64.split(",", 1)[-1])
            path = out_dir / f"sd_result_{idx:02d}.png"
            path.write_bytes(raw)
            saved.append({"name": path.name, "url": f"/api/lora/file/{job_id}/{path.name}"})
        job = {
            "id": job_id,
            "project_name": "SD API Result",
            "status": "done",
            "message": "SD API 图片已生成。",
            "created_at": _lora_now(),
            "dataset_dir": str(out_dir),
            "output_dir": str(out_dir),
            "log_path": str(_lora_job_dir(job_id) / "train.log"),
            "image_count": 0,
            "outputs": saved,
        }
        _lora_save_job(job)
    return _lora_JSONResponse({"ok": True, "saved": saved, "raw_info": {k: v for k, v in result.items() if k != "images"}})


@app.post("/api/lora/sd_img2img")
async def lora_sd_img2img_api(
    image: _lora_UploadFile = _lora_File(...),
    sd_url: str = _lora_Form(default="http://127.0.0.1:7860"),
    prompt: str = _lora_Form(default="single readable glyph, preserve exact source character shape, material texture"),
    negative_prompt: str = _lora_Form(default="low quality, blurry, distorted, unreadable glyph, deformed character"),
    steps: int = _lora_Form(default=24),
    width: int = _lora_Form(default=768),
    height: int = _lora_Form(default=768),
    cfg_scale: float = _lora_Form(default=7),
    seed: str = _lora_Form(default=""),
    denoising_strength: float = _lora_Form(default=0.65),
    controlnet_enabled: str = _lora_Form(default="true"),
    controlnet_conditioning_scale: float = _lora_Form(default=1.15),
    glyph_lock_enabled: str = _lora_Form(default="true"),
    glyph_mask_dilate: int = _lora_Form(default=2),
    glyph_mask_blur: float = _lora_Form(default=1),
    material_fill_enabled: str = _lora_Form(default="true"),
    material_intensity: float = _lora_Form(default=2.35),
    depth_strength: float = _lora_Form(default=1.75),
    shadow_strength: float = _lora_Form(default=0.65),
    style_hint: str = _lora_Form(default="material"),
    canny_low: int = _lora_Form(default=80),
    canny_high: int = _lora_Form(default=180),
):
    raw = await image.read()
    if not raw:
        return _lora_JSONResponse({"ok": False, "error": "请上传要风格化的字形图片。"}, status_code=400)
    if len(raw) > _LORA_MAX_FILE_MB * 1024 * 1024:
        return _lora_JSONResponse({"ok": False, "error": f"图片超过 {_LORA_MAX_FILE_MB}MB 限制。"}, status_code=400)
    try:
        img = _lora_Image.open(_lora_io.BytesIO(raw))
        img = _lora_ImageOps.exif_transpose(img).convert("RGB")
        buf = _lora_io.BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()
    except Exception as e:
        return _lora_JSONResponse({"ok": False, "error": "图片无法读取：" + str(e)}, status_code=400)

    payload = {
        "prompt": prompt or "single readable glyph, preserve exact source character shape, material texture",
        "negative_prompt": negative_prompt or "low quality, blurry, distorted, unreadable glyph, deformed character",
        "steps": int(steps or 24),
        "width": int(width or 768),
        "height": int(height or 768),
        "cfg_scale": float(cfg_scale or 7),
        "denoising_strength": float(denoising_strength or 0.65),
        "controlnet_enabled": str(controlnet_enabled).lower() in {"1", "true", "yes", "on"},
        "controlnet_conditioning_scale": float(controlnet_conditioning_scale or 1.15),
        "glyph_lock_enabled": str(glyph_lock_enabled).lower() in {"1", "true", "yes", "on"},
        "glyph_mask_dilate": int(glyph_mask_dilate or 2),
        "glyph_mask_blur": float(glyph_mask_blur or 1),
        "material_fill_enabled": str(material_fill_enabled).lower() in {"1", "true", "yes", "on"},
        "material_intensity": float(material_intensity or 1.9),
        "depth_strength": float(depth_strength or 1.45),
        "shadow_strength": float(shadow_strength or 0.5),
        "style_hint": style_hint or "material",
        "canny_low": int(canny_low or 80),
        "canny_high": int(canny_high or 180),
        "init_images": [_lora_base64.b64encode(raw).decode("ascii")],
    }
    if seed not in (None, ""):
        payload["seed"] = int(seed)
    try:
        result = _lora_sd_api_call(sd_url, "/sdapi/v1/img2img", payload=payload, timeout=360)
    except _lora_urlerror.HTTPError as e:
        return _lora_JSONResponse({"ok": False, "error": e.read().decode("utf-8", errors="ignore")}, status_code=502)
    except Exception as e:
        return _lora_JSONResponse({"ok": False, "error": str(e)}, status_code=503)

    images = result.get("images") or []
    saved = []
    if images:
        job_id = "sd_img2img_" + _lora_time.strftime("%Y%m%d_%H%M%S") + "_" + _lora_uuid.uuid4().hex[:8]
        out_dir = _lora_job_dir(job_id) / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        source_path = out_dir / "source.png"
        source_path.write_bytes(raw)
        saved.append({"name": source_path.name, "url": f"/api/lora/file/{job_id}/{source_path.name}", "kind": "source"})
        for idx, image_b64 in enumerate(images[:4], 1):
            output_raw = _lora_base64.b64decode(image_b64.split(",", 1)[-1])
            path = out_dir / f"styled_result_{idx:02d}.png"
            path.write_bytes(output_raw)
            saved.append({"name": path.name, "url": f"/api/lora/file/{job_id}/{path.name}", "kind": "result"})
        job = {
            "id": job_id,
            "project_name": "SD Img2Img Result",
            "status": "done",
            "message": "字形图片已风格化生成。",
            "created_at": _lora_now(),
            "dataset_dir": str(out_dir),
            "output_dir": str(out_dir),
            "log_path": str(_lora_job_dir(job_id) / "train.log"),
            "image_count": 1,
            "outputs": saved,
        }
        _lora_save_job(job)
    return _lora_JSONResponse({"ok": True, "saved": saved, "raw_info": {k: v for k, v in result.items() if k != "images"}})


@app.get("/lora_lab", response_class=HTMLResponse)
async def lora_lab_page():
    return HTMLResponse(r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LoRA 字形风格训练</title>
<style>
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #eef2f7;
  color: #0f172a;
  font-family: Arial, "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
}
.page {
  width: min(1440px, calc(100vw - 32px));
  margin: 0 auto;
  padding: 18px 0 28px;
}
.topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
h1 { margin: 0; font-size: 24px; line-height: 1.2; letter-spacing: 0; }
.sub { margin-top: 6px; color: #64748b; font-size: 13px; }
.back {
  display: inline-flex;
  align-items: center;
  min-height: 36px;
  padding: 8px 12px;
  border-radius: 6px;
  background: #0f172a;
  color: white;
  text-decoration: none;
  font-weight: 800;
}
.grid {
  display: grid;
  grid-template-columns: 390px minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}
.panel {
  background: white;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  padding: 14px;
}
.stack { display: grid; gap: 12px; }
.section-title {
  margin: 0 0 10px;
  font-size: 16px;
  font-weight: 900;
}
label {
  display: block;
  color: #334155;
  font-size: 12px;
  font-weight: 800;
  margin-bottom: 5px;
}
input, select, textarea {
  width: 100%;
  min-height: 36px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  padding: 8px 10px;
  font: inherit;
  background: #fff;
  color: #0f172a;
}
textarea { min-height: 92px; resize: vertical; line-height: 1.45; }
.row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.row3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
button {
  border: 0;
  border-radius: 6px;
  min-height: 36px;
  padding: 8px 12px;
  background: #2563eb;
  color: white;
  font-weight: 900;
  cursor: pointer;
}
button.secondary { background: #111827; }
button.light { background: #e2e8f0; color: #0f172a; }
button.style-pack { background: #0f766e; color: #fff; }
button.prompt-style { background: #475569; color: #fff; }
button:disabled { opacity: .45; cursor: not-allowed; }
.status-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.stat {
  border: 1px solid #d8dee8;
  border-radius: 8px;
  padding: 10px;
  background: #f8fafc;
  min-height: 70px;
}
.stat .label { color: #64748b; font-size: 12px; }
.stat .value { margin-top: 5px; font-size: 16px; font-weight: 900; word-break: break-word; }
.notice {
  border: 1px solid #fde68a;
  background: #fffbeb;
  color: #854d0e;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.55;
}
.ok { color: #047857; font-weight: 900; }
.bad { color: #b91c1c; font-weight: 900; }
.muted { color: #64748b; font-size: 12px; line-height: 1.5; }
.jobs {
  display: grid;
  gap: 8px;
  max-height: 260px;
  overflow: auto;
}
.job {
  border: 1px solid #d8dee8;
  border-radius: 8px;
  padding: 10px;
  background: #f8fafc;
  cursor: pointer;
}
.job.active { border-color: #2563eb; box-shadow: 0 0 0 2px rgba(37, 99, 235, .12); }
.job b { display: block; margin-bottom: 4px; }
.preview-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
  gap: 8px;
}
.thumb {
  border: 1px solid #d8dee8;
  border-radius: 8px;
  background: #f8fafc;
  min-height: 78px;
  padding: 8px;
  font-size: 12px;
  overflow: hidden;
}
pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  background: #0b1220;
  color: #e2e8f0;
  border-radius: 8px;
  padding: 12px;
  min-height: 150px;
  max-height: 340px;
  overflow: auto;
  font-size: 12px;
}
.result-images {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 10px;
}
.result-images img {
  width: 100%;
  border-radius: 8px;
  border: 1px solid #d8dee8;
  background: #fff;
}
@media (max-width: 980px) {
  .grid, .status-grid { grid-template-columns: 1fr; }
  .row2, .row3 { grid-template-columns: 1fr; }
  .topbar { align-items: flex-start; flex-direction: column; }
}
</style>
</head>
<body>
<main class="page">
  <div class="topbar">
    <div>
      <h1>LoRA 字形风格训练</h1>
      <div class="sub">上传风格图片，创建字形质感 LoRA 训练任务，并连接本地 Stable Diffusion API 做风格化生成。</div>
    </div>
    <a class="back" href="/">返回首页</a>
  </div>

  <section class="panel stack" style="margin-bottom:14px;">
    <h2 class="section-title">机器与环境状态</h2>
    <div class="status-grid" id="envStats"></div>
    <div class="notice" id="envNotice">正在读取环境状态...</div>
  </section>

  <div class="grid">
    <section class="stack">
      <form class="panel stack" id="uploadForm">
        <h2 class="section-title">1. 上传训练素材</h2>
        <div>
          <label for="projectName">任务名称</label>
          <input id="projectName" name="project_name" value="Leather Glyph LoRA">
        </div>
        <div class="row2">
          <div>
            <label for="triggerWord">触发词</label>
            <input id="triggerWord" name="trigger_word" value="glyphstyle">
          </div>
          <div>
            <label for="imageFiles">图片</label>
            <input id="imageFiles" name="files" type="file" accept=".jpg,.jpeg,.png,.webp" multiple required>
          </div>
        </div>
        <div>
          <label for="captionHint">统一提示词描述</label>
          <textarea id="captionHint" name="caption_hint">typography style, glyph texture, high quality material surface</textarea>
        </div>
        <div class="muted" id="limitText">读取限制中...</div>
        <div class="actions">
          <button type="submit">上传并创建任务</button>
          <button class="light" type="button" id="refreshBtn">刷新状态</button>
        </div>
      </form>

      <section class="panel stack">
        <h2 class="section-title">任务列表</h2>
        <div class="jobs" id="jobList"></div>
      </section>
    </section>

    <section class="stack">
      <section class="panel stack">
        <h2 class="section-title">2. 训练 LoRA</h2>
        <div class="row2">
          <div>
            <label for="backend">训练后端</label>
            <select id="backend">
              <option value="diffusers">Local Diffusers LoRA</option>
              <option value="kohya">kohya_ss / sd-scripts</option>
            </select>
          </div>
          <div>
            <label for="baseModel">基础 SD 模型路径或模型名</label>
            <input id="baseModel" placeholder="/root/autodl-tmp/models/sd15.safetensors">
          </div>
        </div>
        <div class="row3">
          <div>
            <label for="steps">训练步数</label>
            <input id="steps" type="number" min="100" max="6000" value="800">
          </div>
          <div>
            <label for="resolution">分辨率</label>
            <input id="resolution" type="number" min="384" max="1024" value="512">
          </div>
          <div>
            <label for="rank">LoRA Rank</label>
            <input id="rank" type="number" min="4" max="128" value="16">
          </div>
        </div>
        <div class="row2">
          <div>
            <label for="learningRate">学习率</label>
            <input id="learningRate" value="0.0001">
          </div>
          <div>
            <label>当前任务</label>
            <input id="selectedJob" readonly placeholder="请先上传或选择任务">
          </div>
        </div>
        <div class="actions">
          <button id="trainBtn" type="button" disabled>开始训练</button>
          <button id="pollBtn" type="button" class="secondary" disabled>刷新任务日志</button>
        </div>
        <div class="preview-grid" id="imagePreview"></div>
        <pre id="jobLog">等待任务。</pre>
      </section>

      <section class="panel stack">
        <h2 class="section-title">3. 本地 Stable Diffusion API 调用</h2>
        <div>
          <label>快速风格按钮</label>
          <div class="actions" id="stylePresets"></div>
          <div class="muted" id="loraPresetNote">如果本地存在同名 LoRA，按钮会自动加入 LoRA 标签；否则先用提示词风格生成。</div>
        </div>
        <div>
          <label>本地可用 LoRA 模型</label>
          <div class="actions" id="installedLoras"></div>
          <div class="row2" style="margin-top:8px;">
            <select id="loraSelect"></select>
            <button id="useSelectedLoraBtn" type="button" class="secondary">使用选中 LoRA</button>
          </div>
          <div class="muted">训练成功后的 LoRA 会自动出现在这里；如果刚训练完没出现，点左侧“刷新状态”。</div>
        </div>
        <div class="row2">
          <div>
            <label for="sdUrl">SD API 地址</label>
            <input id="sdUrl" value="http://127.0.0.1:7860">
          </div>
          <div>
            <label for="sdPrompt">生成提示词</label>
            <input id="sdPrompt" value="glyphstyle, warm brown leather grain, 3D raised bevel, strong specular highlights, stitched leather surface, rich tactile material texture">
          </div>
        </div>
        <div>
          <label for="sdNegative">反向提示词</label>
          <input id="sdNegative" value="low quality, blurry, broken text, watermark, unreadable glyph, deformed character, extra strokes, missing strokes, flat black ink, plain black silhouette, monochrome, face, body, robot, object">
        </div>
        <div class="row3">
          <div><label for="sdWidth">宽</label><input id="sdWidth" type="number" value="768"></div>
          <div><label for="sdHeight">高</label><input id="sdHeight" type="number" value="768"></div>
          <div><label for="sdSteps">步数</label><input id="sdSteps" type="number" value="24"></div>
        </div>
        <div>
          <label for="styleImage">上传要风格化的字形图片</label>
          <input id="styleImage" type="file" accept=".jpg,.jpeg,.png,.webp">
          <div class="muted">上传图片后可走 img2img + ControlNet，尽量保持原始字形轮廓和可识别性。</div>
        </div>
        <div class="row3">
          <div>
            <label for="denoiseStrength">风格化强度</label>
            <input id="denoiseStrength" type="number" min="0.05" max="1" step="0.05" value="0.65">
          </div>
          <div>
            <label for="controlStrength">ControlNet 轮廓保持</label>
            <input id="controlStrength" type="number" min="0" max="2" step="0.05" value="1.15">
          </div>
          <div>
            <label for="useControlNet">结构控制</label>
            <select id="useControlNet">
              <option value="true" selected>启用 ControlNet Canny</option>
              <option value="false">仅 img2img</option>
            </select>
          </div>
        </div>
        <div class="row3">
          <div>
            <label for="glyphLock">字形锁定</label>
            <select id="glyphLock">
              <option value="true" selected>启用字形蒙版锁定</option>
              <option value="false">关闭字形锁定</option>
            </select>
          </div>
          <div>
            <label for="glyphMaskDilation">笔画覆盖</label>
            <input id="glyphMaskDilation" type="number" min="0" max="8" step="1" value="2">
          </div>
          <div>
            <label for="glyphMaskBlur">边缘柔化</label>
            <input id="glyphMaskBlur" type="number" min="0" max="4" step="0.5" value="1">
          </div>
        </div>
        <div class="row2">
          <div>
            <label for="materialFill">材质生成方式</label>
            <select id="materialFill">
              <option value="true" selected>材质填充到原字形</option>
              <option value="false">传统 img2img 重绘</option>
            </select>
          </div>
          <div>
            <label>当前逻辑</label>
            <input readonly value="原图定形，LoRA/提示词定材质">
          </div>
        </div>
        <div class="row3">
          <div>
            <label for="materialIntensity">材质强度</label>
            <input id="materialIntensity" type="number" min="0.2" max="3" step="0.05" value="2.35">
          </div>
          <div>
            <label for="depthStrength">立体厚度</label>
            <input id="depthStrength" type="number" min="0" max="2.5" step="0.05" value="1.75">
          </div>
          <div>
            <label for="shadowStrength">投影暗部</label>
            <input id="shadowStrength" type="number" min="0" max="1" step="0.05" value="0.65">
          </div>
        </div>
        <div class="actions">
          <button id="checkSdBtn" type="button" class="light">检查 SD API</button>
          <button id="generateBtn" type="button">调用 SD 生成</button>
          <button id="img2imgBtn" type="button" class="secondary">上传图片并风格化</button>
        </div>
        <div class="notice" id="sdNotice">如果本地 SD WebUI API 未启动，这里会提示连接失败。</div>
        <div class="result-images" id="sdResults"></div>
      </section>
    </section>
  </div>
</main>

<script>
(function(){
  const state = { env: null, selectedJob: null, jobs: [], pollTimer: null, currentStyle: "leather" };
  const stylePresets = [
    {
      label: "手绘线稿",
      lora: "how2draw_line_art_lora",
      style: "ink",
      prompt: "how2draw, crisp ink line texture, hand drawn brush grain, white paper fibers, high contrast stroke material"
    },
    {
      label: "童趣插画",
      lora: "little_tinies_illustration_lora",
      style: "illustration",
      prompt: "little tinies style, colorful soft illustration texture, playful painted surface, bright clean material, gentle highlights"
    },
    {
      label: "机械金属",
      lora: "mecha_metal_lora",
      style: "metal",
      prompt: "mecha, shiny chrome metal texture, brushed metal panels, red glowing seams, reflective mechanical surface, high contrast"
    },
    {
      label: "艺术 Logo",
      lora: "art_logo_lora",
      style: "logo",
      prompt: "logo, clean graphic material, glossy vector-like surface, bold color blocks, crisp highlight texture, high contrast"
    },
    {
      label: "细节增强",
      lora: "detail_tweaker_lora",
      style: "detail",
      prompt: "high detail material texture, micro surface detail, sharp specular highlights, crisp stroke edge texture, studio lighting"
    },
    {
      label: "皮革字形",
      lora: "leather_glyph_lora",
      style: "leather",
      prompt: "glyphstyle, warm brown leather grain, premium dark leather surface, embossed highlights, stitched texture, rich tactile material"
    },
    {
      label: "牛奶字形",
      lora: "milk_glyph_lora",
      style: "milk",
      prompt: "glyphstyle, creamy white milk liquid texture, glossy thick fluid, soft highlights, milky splash material, smooth bright surface"
    },
    {
      label: "水流字形",
      lora: "water_glyph_lora",
      style: "water",
      prompt: "glyphstyle, transparent water texture, flowing liquid surface, refraction, blue highlights, glossy wet material, bright reflections"
    },
    {
      label: "火焰字形",
      lora: "fire_glyph_lora",
      style: "fire",
      prompt: "glyphstyle, flame texture, glowing orange fire, burning ember surface, sparks, hot luminous material, dramatic contrast"
    },
    {
      label: "冰块字形",
      lora: "ice_glyph_lora",
      style: "ice",
      prompt: "glyphstyle, transparent blue ice crystal texture, frozen glass material, refraction, frost, sharp reflective highlights, translucent surface"
    },
    {
      label: "艺术字体",
      lora: "art_logo_lora",
      style: "artistic",
      prompt: "glyphstyle, artistic poster material texture, bold graphic color, glossy ink surface, elegant contrast, experimental visual texture"
    }
  ];
  const qs = id => document.getElementById(id);

  function htmlEscape(value) {
    return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[ch]));
  }

  function setLog(text) {
    qs("jobLog").textContent = text || "暂无日志。";
  }

  async function fetchJson(url, options) {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || data.message || ("HTTP " + res.status));
    }
    return data;
  }

  function renderEnv() {
    const env = state.env || {};
    const limits = env.limits || {};
    const deps = env.deps || {};
    qs("envStats").innerHTML = [
      ["GPU", env.gpu || "未检测到"],
      ["数据盘剩余", (limits.disk_free_gb ?? "-") + " GB"],
      ["图片数量", (limits.min_images ?? "-") + " - " + (limits.max_images ?? "-") + " 张"],
      ["训练环境", (env.kohya_ready || env.diffusers_ready) ? "可启动" : "未安装完整依赖"]
    ].map(([label, value]) => `<div class="stat"><div class="label">${label}</div><div class="value">${htmlEscape(value)}</div></div>`).join("");

    qs("limitText").textContent =
      `本机当前限制：最少 ${limits.min_images || "-"} 张，最多 ${limits.max_images || "-"} 张；单张不超过 ${limits.max_file_mb || "-"}MB；本次总上传不超过 ${limits.max_total_upload_mb || "-"}MB。`;

    const depText = Object.entries(deps).map(([k,v]) => `${k}: ${v ? "OK" : "缺失"}`).join(" / ");
    const messages = [];
    messages.push("依赖：" + depText);
    messages.push("kohya_ss：" + (env.kohya_ready ? "已就绪" : "未就绪"));
    messages.push("diffusers：" + (env.diffusers_ready ? "已就绪" : "未就绪"));
    if (env.base_model_env) messages.push("默认基础模型：" + env.base_model_env);
    qs("envNotice").innerHTML = messages.map(htmlEscape).join("<br>");
    if (env.base_model_env && !qs("baseModel").value) qs("baseModel").value = env.base_model_env;
    if (env.sd_api_default) qs("sdUrl").value = env.sd_api_default;
    renderStylePresets();
    renderInstalledLoras();
  }

  function localLoras() {
    const options = state.env && state.env.sd_api_options;
    const apiLoras = options && Array.isArray(options.loras) ? options.loras : [];
    const scannedLoras = state.env && Array.isArray(state.env.local_loras) ? state.env.local_loras : [];
    const merged = [];
    const seen = new Set();
    [...apiLoras, ...scannedLoras].forEach(item => {
      const name = item && item.name;
      if (!name || seen.has(name)) return;
      seen.add(name);
      merged.push(item);
    });
    return merged;
  }

  function localLoraNames() {
    return new Set(localLoras().map(x => x.name));
  }

  function materialStylePacks() {
    return state.env && Array.isArray(state.env.material_style_packs) ? state.env.material_style_packs : [];
  }

  function combinedStylePresets() {
    const merged = new Map();
    stylePresets.forEach(item => {
      const key = item.style || item.label || item.lora || item.prompt;
      merged.set(key, item);
    });
    materialStylePacks().forEach(item => {
      const key = item.style || item.label || item.id || item.prompt;
      merged.set(key, item);
    });
    return Array.from(merged.values());
  }

  function loraLabel(item) {
    return (item && (item.alias || (item.metadata && item.metadata.label) || item.name)) || "";
  }

  const loraMaterialDefaults = {
    leather_glyph_lora: "glyphstyle, warm brown leather grain, premium dark leather surface, embossed highlights, stitched texture, rich tactile material",
    mecha_metal_lora: "mecha, shiny chrome metal texture, brushed metal panels, red glowing seams, reflective mechanical surface, high contrast",
    art_logo_lora: "logo, clean graphic material, glossy vector-like surface, bold color blocks, crisp highlight texture, high contrast",
    detail_tweaker_lora: "high detail material texture, micro surface detail, sharp specular highlights, crisp edge texture, studio lighting",
    milk_glyph_lora: "glyphstyle, creamy white milk liquid texture, glossy thick fluid, soft highlights, milky splash material, smooth bright surface",
    water_glyph_lora: "glyphstyle, transparent water texture, flowing liquid surface, refraction, blue highlights, glossy wet material, bright reflections",
    fire_glyph_lora: "glyphstyle, flame texture, glowing orange fire, burning ember surface, sparks, hot luminous material, dramatic contrast",
    ice_glyph_lora: "glyphstyle, transparent blue ice crystal texture, frozen glass material, refraction, frost, sharp reflective highlights, translucent surface"
  };

  function inferStyleFromName(name) {
    const value = String(name || "").toLowerCase();
    if (value.includes("leather")) return "leather";
    if (value.includes("ice") || value.includes("frozen") || value.includes("crystal")) return "ice";
    if (value.includes("water")) return "water";
    if (value.includes("fire") || value.includes("flame")) return "fire";
    if (value.includes("milk") || value.includes("cream")) return "milk";
    if (value.includes("jade") || value.includes("玉")) return "jade";
    if (value.includes("gold") || value.includes("金")) return "gold";
    if (value.includes("ceramic") || value.includes("porcelain") || value.includes("瓷")) return "ceramic";
    if (value.includes("glass") || value.includes("玻璃")) return "glass";
    if (value.includes("neon") || value.includes("霓虹")) return "neon";
    if (value.includes("silk") || value.includes("丝绸")) return "silk";
    if (value.includes("wood") || value.includes("木")) return "wood";
    if (value.includes("metal") || value.includes("mecha") || value.includes("chrome")) return "metal";
    if (value.includes("logo")) return "logo";
    if (value.includes("detail")) return "detail";
    if (value.includes("line") || value.includes("draw") || value.includes("ink")) return "ink";
    return "material";
  }

  function withMaterialFillPrompt(text) {
    const base = text || "rich visible material texture, reflective highlights, strong color";
    const lower = base.toLowerCase();
    if (lower.includes("material") || lower.includes("texture") || lower.includes("leather") || lower.includes("metal") || lower.includes("ice") || lower.includes("water") || lower.includes("fire")) {
      return base;
    }
    return base + ", rich visible material texture, reflective highlights, strong color, not black ink";
  }

  function ensureGlyphNegativePrompt() {
    const input = qs("sdNegative");
    const existing = input.value || "";
    const additions = [
      "unreadable glyph",
      "deformed character",
      "extra strokes",
      "missing strokes",
      "flat black ink",
      "plain black silhouette",
      "monochrome",
      "face",
      "body",
      "robot",
      "object"
    ];
    const lower = existing.toLowerCase();
    const missing = additions.filter(item => !lower.includes(item));
    if (missing.length) input.value = existing ? existing + ", " + missing.join(", ") : missing.join(", ");
  }

  function applyLoraToPrompt(name, promptText, styleHint) {
    const tag = `<lora:${name}:0.8>, `;
    const preferred = loraMaterialDefaults[name] || promptText || `${name}, rich visible material texture, reflective highlights, strong color`;
    const base = withMaterialFillPrompt(preferred);
    qs("sdPrompt").value = tag + base;
    state.currentStyle = styleHint || inferStyleFromName(name || preferred);
    ensureGlyphNegativePrompt();
  }

  function applyStylePackToPrompt(preset) {
    const prompt = withMaterialFillPrompt(preset.prompt || preset.label || "rich visible material texture, reflective highlights, strong color");
    qs("sdPrompt").value = prompt;
    state.currentStyle = preset.style || inferStyleFromName(prompt);
    ensureGlyphNegativePrompt();
  }

  function renderStylePresets() {
    const wrap = qs("stylePresets");
    if (!wrap) return;
    const names = localLoraNames();
    const presets = combinedStylePresets();
    const stylePackCount = materialStylePacks().length;
    wrap.innerHTML = presets.map((preset, idx) => {
      const has = preset.lora && names.has(preset.lora);
      const isPack = preset.kind === "style_pack" || preset.source === "data_disk_style_pack";
      const cls = has ? "" : (isPack ? "style-pack" : "prompt-style");
      const suffix = has ? " LoRA" : " 风格";
      return `<button type="button" class="${cls}" data-preset="${idx}">${htmlEscape(preset.label)}${suffix}</button>`;
    }).join("");
    wrap.querySelectorAll("[data-preset]").forEach(btn => {
      btn.addEventListener("click", () => {
        const preset = presets[Number(btn.dataset.preset)];
        const namesNow = localLoraNames();
        state.currentStyle = preset.style || inferStyleFromName(preset.lora || preset.prompt);
        if (preset.lora && namesNow.has(preset.lora)) applyLoraToPrompt(preset.lora, preset.prompt, state.currentStyle);
        else applyStylePackToPrompt(preset);
      });
    });
    const note = qs("loraPresetNote");
    if (note) {
      note.textContent = names.size
        ? `当前已安装 ${names.size} 个 LoRA，并部署 ${stylePackCount} 个数据盘风格包；按钮会优先使用本地 LoRA，其余使用持久化材质风格。`
        : `当前已部署 ${stylePackCount} 个数据盘风格包；训练或放入 LoRA 后会自动升级为 LoRA 调用。`;
    }
  }

  function renderInstalledLoras() {
    const loras = localLoras();
    const wrap = qs("installedLoras");
    const select = qs("loraSelect");
    if (!wrap || !select) return;
    if (!loras.length) {
      wrap.innerHTML = '<div class="muted">暂无可用 LoRA。</div>';
      select.innerHTML = '<option value="">暂无可用 LoRA</option>';
      return;
    }
    wrap.innerHTML = loras.map((item, idx) =>
      `<button type="button" data-lora-index="${idx}">${htmlEscape(loraLabel(item))}</button>`
    ).join("");
    select.innerHTML = loras.map((item, idx) =>
      `<option value="${idx}">${htmlEscape(loraLabel(item))} · ${htmlEscape(item.name)}</option>`
    ).join("");
    wrap.querySelectorAll("[data-lora-index]").forEach(btn => {
      btn.addEventListener("click", () => {
        const item = loras[Number(btn.dataset.loraIndex)];
        const trigger = item && item.metadata && item.metadata.trigger;
        applyLoraToPrompt(item.name, trigger || loraLabel(item), inferStyleFromName(item.name || trigger || loraLabel(item)));
      });
    });
  }

  function renderJobs() {
    const list = qs("jobList");
    if (!state.jobs.length) {
      list.innerHTML = '<div class="muted">还没有 LoRA 任务。</div>';
      return;
    }
    list.innerHTML = state.jobs.map(job => {
      const active = state.selectedJob && state.selectedJob.id === job.id ? " active" : "";
      return `<div class="job${active}" data-id="${htmlEscape(job.id)}">
        <b>${htmlEscape(job.project_name || job.id)}</b>
        <div class="muted">${htmlEscape(job.status)} · ${htmlEscape(job.image_count || 0)} 张 · ${htmlEscape(job.created_at || "")}</div>
        <div>${htmlEscape(job.message || "")}</div>
      </div>`;
    }).join("");
    list.querySelectorAll(".job").forEach(el => {
      el.addEventListener("click", () => selectJob(el.dataset.id));
    });
  }

  function renderSelectedJob(job) {
    state.selectedJob = job;
    qs("selectedJob").value = job ? job.id : "";
    qs("trainBtn").disabled = !job;
    qs("pollBtn").disabled = !job;
    if (!job) return;
    qs("imagePreview").innerHTML = (job.images || []).slice(0, 24).map(img =>
      `<div class="thumb"><b>${htmlEscape(img.name)}</b><br>${htmlEscape(img.width)}×${htmlEscape(img.height)}<br>${htmlEscape(img.size_mb)}MB</div>`
    ).join("");
    const outputs = (job.outputs || []).map(x => `${x.name || ""} ${x.url || ""}`).join("\n");
    setLog([
      `任务：${job.project_name || job.id}`,
      `状态：${job.status}`,
      `信息：${job.message || ""}`,
      outputs ? "\n输出：\n" + outputs : "",
      job.command_preview ? "\n命令预览：\n" + job.command_preview : "",
      job.log_tail ? "\n日志：\n" + job.log_tail : ""
    ].join("\n"));
    renderJobs();
  }

  async function loadEnv() {
    state.env = await fetchJson("/api/lora/env");
    renderEnv();
  }

  async function loadJobs() {
    const data = await fetchJson("/api/lora/jobs");
    state.jobs = data.jobs || [];
    renderJobs();
  }

  async function selectJob(id) {
    const data = await fetchJson("/api/lora/status/" + encodeURIComponent(id));
    renderSelectedJob(data.job);
    return data.job;
  }

  function stopTrainingPoll() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function startTrainingPoll(jobId) {
    stopTrainingPoll();
    state.pollTimer = setInterval(async () => {
      try {
        const job = await selectJob(jobId);
        await loadJobs();
        if (job && ["done", "failed", "error"].includes(job.status)) {
          stopTrainingPoll();
          await loadEnv();
        }
      } catch (e) {
        setLog("刷新任务状态失败：" + e.message);
      }
    }, 5000);
  }

  qs("refreshBtn").addEventListener("click", async () => {
    try { await loadEnv(); await loadJobs(); }
    catch (e) { qs("envNotice").textContent = e.message; }
  });

  qs("uploadForm").addEventListener("submit", async evt => {
    evt.preventDefault();
    const form = new FormData(evt.currentTarget);
    setLog("正在上传素材...");
    try {
      const data = await fetchJson("/api/lora/upload", { method: "POST", body: form });
      await loadJobs();
      renderSelectedJob(data.job);
    } catch (e) {
      setLog("上传失败：" + e.message);
    }
  });

  qs("trainBtn").addEventListener("click", async () => {
    if (!state.selectedJob) return;
    const form = new FormData();
    form.append("backend", qs("backend").value);
    form.append("base_model", qs("baseModel").value);
    form.append("max_train_steps", qs("steps").value);
    form.append("resolution", qs("resolution").value);
    form.append("rank", qs("rank").value);
    form.append("learning_rate", qs("learningRate").value);
    setLog("正在提交训练任务...");
    try {
      const data = await fetchJson("/api/lora/train/" + encodeURIComponent(state.selectedJob.id), { method: "POST", body: form });
      renderSelectedJob(data.job);
      startTrainingPoll(state.selectedJob.id);
    } catch (e) {
      setLog("训练无法启动：" + e.message);
      await selectJob(state.selectedJob.id).catch(() => {});
    }
  });

  qs("pollBtn").addEventListener("click", () => {
    if (state.selectedJob) {
      selectJob(state.selectedJob.id)
        .then(job => {
          if (job && ["done", "failed", "error"].includes(job.status)) return loadEnv();
        })
        .catch(e => setLog(e.message));
    }
  });

  qs("useSelectedLoraBtn").addEventListener("click", () => {
    const loras = localLoras();
    const item = loras[Number(qs("loraSelect").value)];
    if (!item) return;
    const trigger = item.metadata && item.metadata.trigger;
    applyLoraToPrompt(item.name, trigger || loraLabel(item), inferStyleFromName(item.name || trigger || loraLabel(item)));
  });

  qs("checkSdBtn").addEventListener("click", async () => {
    qs("sdNotice").textContent = "正在检查 SD API...";
    try {
      const data = await fetchJson("/api/lora/sd_status?sd_url=" + encodeURIComponent(qs("sdUrl").value));
      qs("sdNotice").innerHTML = `<span class="ok">SD API 可用。</span> ${htmlEscape(data.sd_url)}`;
    } catch (e) {
      qs("sdNotice").innerHTML = `<span class="bad">SD API 不可用。</span> ${htmlEscape(e.message)}`;
    }
  });

  qs("generateBtn").addEventListener("click", async () => {
    qs("sdNotice").textContent = "正在调用 SD API 生成...";
    qs("sdResults").innerHTML = "";
    try {
      const payload = {
        sd_url: qs("sdUrl").value,
        prompt: qs("sdPrompt").value,
        negative_prompt: qs("sdNegative").value,
        width: Number(qs("sdWidth").value),
        height: Number(qs("sdHeight").value),
        steps: Number(qs("sdSteps").value)
      };
      const data = await fetchJson("/api/lora/sd_generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      qs("sdNotice").innerHTML = '<span class="ok">生成完成。</span>';
      qs("sdResults").innerHTML = (data.saved || []).map(x => `<a href="${htmlEscape(x.url)}" target="_blank"><img src="${htmlEscape(x.url)}" alt=""></a>`).join("");
    } catch (e) {
      qs("sdNotice").innerHTML = `<span class="bad">生成失败。</span> ${htmlEscape(e.message)}`;
    }
  });

  qs("img2imgBtn").addEventListener("click", async () => {
    const file = qs("styleImage").files && qs("styleImage").files[0];
    if (!file) {
      qs("sdNotice").innerHTML = '<span class="bad">请先上传要风格化的字形图片。</span>';
      return;
    }
    qs("sdNotice").textContent = "正在使用上传图片 + LoRA + ControlNet 风格化...";
    qs("sdResults").innerHTML = "";
    try {
      ensureGlyphNegativePrompt();
      const form = new FormData();
      form.append("image", file);
      form.append("sd_url", qs("sdUrl").value);
      form.append("prompt", withMaterialFillPrompt(qs("sdPrompt").value));
      form.append("negative_prompt", qs("sdNegative").value);
      form.append("width", qs("sdWidth").value);
      form.append("height", qs("sdHeight").value);
      form.append("steps", qs("sdSteps").value);
      form.append("denoising_strength", qs("denoiseStrength").value);
      form.append("controlnet_conditioning_scale", qs("controlStrength").value);
      form.append("controlnet_enabled", qs("useControlNet").value);
      form.append("glyph_lock_enabled", qs("glyphLock").value);
      form.append("glyph_mask_dilate", qs("glyphMaskDilation").value);
      form.append("glyph_mask_blur", qs("glyphMaskBlur").value);
      form.append("material_fill_enabled", qs("materialFill").value);
      form.append("material_intensity", qs("materialIntensity").value);
      form.append("depth_strength", qs("depthStrength").value);
      form.append("shadow_strength", qs("shadowStrength").value);
      form.append("style_hint", state.currentStyle || inferStyleFromName(qs("sdPrompt").value));
      const data = await fetchJson("/api/lora/sd_img2img", { method: "POST", body: form });
      qs("sdNotice").innerHTML = '<span class="ok">风格化完成。</span>';
      qs("sdResults").innerHTML = (data.saved || []).map(x => {
        const label = x.kind === "source" ? "原图" : "结果";
        return `<a href="${htmlEscape(x.url)}" target="_blank"><img src="${htmlEscape(x.url)}" alt="${htmlEscape(label)}"><div class="muted">${htmlEscape(label)} · ${htmlEscape(x.name)}</div></a>`;
      }).join("");
      await loadJobs().catch(() => {});
    } catch (e) {
      qs("sdNotice").innerHTML = `<span class="bad">风格化失败。</span> ${htmlEscape(e.message)}`;
    }
  });

  loadEnv().catch(e => qs("envNotice").textContent = e.message);
  loadJobs().catch(e => setLog(e.message));
})();
</script>
</body>
</html>
""")


_LORA_HOME_ENTRY = r"""
<section id="loraLabFeatureEntry" style="
  margin: 18px 0;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  background: #f8fafc;
  padding: 14px 16px;
">
  <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
    <div>
      <div style="font-weight:800;font-size:16px;color:#0f172a;">LoRA 字形风格训练</div>
      <div style="font-size:13px;color:#64748b;margin-top:4px;">上传风格图片，训练字形质感 LoRA，并连接本地 Stable Diffusion API 做风格化生成。</div>
    </div>
    <a href="/lora_lab" target="_blank" style="
      display:inline-flex;
      min-height:36px;
      align-items:center;
      justify-content:center;
      padding:8px 12px;
      border-radius:6px;
      background:#0f172a;
      color:#fff;
      text-decoration:none;
      font-weight:700;
      font-size:13px;
    ">打开 LoRA 工作台</a>
  </div>
</section>
"""


@app.middleware("http")
async def _lora_home_entry_inject(request, call_next):
    response = await call_next(request)

    if request.url.path not in ["/", ""]:
        return response

    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="ignore")

    if "loraLabFeatureEntry" not in html:
        marker = '<form id="form">'
        if marker in html:
            html = html.replace(marker, _LORA_HOME_ENTRY + "\n" + marker, 1)
        elif "</body>" in html:
            html = html.replace("</body>", _LORA_HOME_ENTRY + "\n</body>")
        else:
            html += _LORA_HOME_ENTRY

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return HTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers,
    )


@app.get("/local_deepseek", response_class=HTMLResponse)
async def local_deepseek_page():
    return HTMLResponse(r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>传统蒙古文与蒙古族文化问答</title>
<style>
* { box-sizing: border-box; }
body { margin:0; font-family:Arial,"Microsoft YaHei",sans-serif; color:#0f172a; background:#eaf0f6; }
main { max-width:1180px; margin:0 auto; padding:24px 18px 42px; }
header { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:14px; }
h1 { margin:0; font-size:28px; }
p { margin:6px 0 0; color:#52647a; }
.back, button { border:0; border-radius:6px; background:#0f172a; color:#fff; font-weight:700; padding:10px 14px; text-decoration:none; cursor:pointer; }
button.primary { background:#2563eb; }
button.light { background:#e8eef8; color:#0f172a; }
button:disabled { opacity:.55; cursor:not-allowed; }
.layout { display:grid; grid-template-columns:340px 1fr; gap:14px; align-items:start; }
.panel { background:#fff; border:1px solid #d4dde9; border-radius:8px; padding:14px; box-shadow:0 12px 30px rgba(15,23,42,.05); }
.status { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-bottom:12px; }
.stat { border:1px solid #d9e2ee; border-radius:8px; padding:10px; background:#f8fafc; }
.stat b { display:block; font-size:13px; color:#52647a; margin-bottom:5px; }
.stat span { font-size:18px; font-weight:800; }
label { display:block; font-weight:700; margin:12px 0 6px; font-size:14px; }
textarea, select { width:100%; border:1px solid #cbd5e1; border-radius:6px; padding:10px; font:inherit; background:#fff; }
textarea { min-height:138px; resize:vertical; }
.quick { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
.quick button { background:#e8eef8; color:#0f172a; padding:8px 10px; font-size:13px; }
.actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
.notice { border:1px solid #f3d27a; background:#fff8dd; color:#8a5400; border-radius:7px; padding:10px; margin-top:12px; white-space:pre-wrap; }
.chat { height:520px; overflow:auto; border:1px solid #d9e2ee; border-radius:8px; background:#f8fafc; padding:12px; }
.msg { margin:0 0 12px; padding:11px 12px; border-radius:8px; white-space:pre-wrap; line-height:1.6; }
.user { background:#dbeafe; margin-left:70px; }
.assistant { background:#fff; border:1px solid #d9e2ee; margin-right:30px; }
.meta { color:#64748b; font-size:12px; margin-top:8px; }
@media (max-width:860px) { .layout{grid-template-columns:1fr;} header{flex-direction:column;} .chat{height:420px;} .user{margin-left:0;} }
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>传统蒙古文与蒙古族文化问答</h1>
      <p>运行在本机数据盘模型上，不需要 API Key。专门用于回答传统蒙古文、字形变体、编码标准、字体规则和蒙古族文化相关问题。</p>
    </div>
    <a class="back" href="/">返回首页</a>
  </header>
  <section class="layout">
    <aside class="panel">
      <div class="status">
        <div class="stat"><b>服务</b><span id="svc">检查中</span></div>
        <div class="stat"><b>模型</b><span id="loaded">检查中</span></div>
      </div>
      <div class="actions">
        <button id="checkBtn" type="button" class="light">检查状态</button>
        <button id="loadBtn" type="button" class="primary">加载模型</button>
      </div>
      <label for="task">问答范围</label>
      <select id="task">
        <option value="general">综合问答</option>
        <option value="script">传统蒙古文书写与字形</option>
        <option value="font">字体、编码与国标规则</option>
        <option value="culture">蒙古族文化与礼俗</option>
        <option value="festival">节日祝福与用语</option>
        <option value="ornament">蒙古族纹样与象征</option>
      </select>
      <div class="quick">
        <button type="button" data-fill="传统蒙古文为什么会有词首、词中、词尾和独立形？">字形变体</button>
        <button type="button" data-fill="传统蒙古文 Unicode 编码和中国国标字形清单有什么区别？">编码国标</button>
        <button type="button" data-fill="奥云和蒙科立两家传统蒙古文字体规则为什么不能混用？">字体规则</button>
        <button type="button" data-fill="蒙古族文化里哪些纹样常用来表达吉祥如意？">吉祥纹样</button>
        <button type="button" data-fill="蒙古包、马、哈达、云纹在蒙古族文化里分别有什么象征意义？">文化象征</button>
      </div>
      <div id="notice" class="notice">首次加载 7B 模型大约需要十几秒。加载后会占用约 14GB 显存。</div>
    </aside>
    <section class="panel">
      <div id="chat" class="chat"></div>
      <label for="prompt">输入问题</label>
      <textarea id="prompt" placeholder="例如：传统蒙古文的 a、e、n、g 为什么在不同位置会变成不同字形？"></textarea>
      <div class="actions">
        <button id="sendBtn" type="button" class="primary">发送给本地 DeepSeek</button>
        <button id="clearBtn" type="button" class="light">清空</button>
      </div>
    </section>
  </section>
</main>
<script>
(function(){
  const $ = id => document.getElementById(id);
  const chat = $("chat");
  const notice = $("notice");
  function add(role, text, meta) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    div.textContent = text || "";
    if (meta) {
      const m = document.createElement("div");
      m.className = "meta";
      m.textContent = meta;
      div.appendChild(m);
    }
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
  }
  async function check(load) {
    $("checkBtn").disabled = true;
    $("loadBtn").disabled = true;
    notice.textContent = load ? "正在加载本地 DeepSeek 模型，请稍等..." : "正在检查本地 DeepSeek 服务...";
    try {
      const res = await fetch("/api/local_ai/status" + (load ? "?load=1" : ""));
      const data = await res.json();
      $("svc").textContent = data.ok ? "可用" : "不可用";
      $("loaded").textContent = data.service && data.service.loaded ? "已加载" : "未加载";
      notice.textContent = data.ok ? "本地 DeepSeek 服务正常。模型：" + ((data.service && data.service.model) || "DeepSeek") : "服务不可用：" + (data.error || (data.service && data.service.error) || "未知错误");
    } catch (err) {
      $("svc").textContent = "不可用";
      $("loaded").textContent = "未知";
      notice.textContent = "检查失败：" + err.message;
    } finally {
      $("checkBtn").disabled = false;
      $("loadBtn").disabled = false;
    }
  }
  function taskSystem() {
    const v = $("task").value;
    const base = "你是传统蒙古文与蒙古族文化问答助手。只输出最终答案，不要输出思考过程。回答用中文，必要时补充传统蒙古文原文、拉丁转写或术语解释；不确定的传统蒙古文、史实、标准条目要明确说不确定，不能编造。不要写 Stable Diffusion、LoRA 或绘图提示词。基础事实：传统蒙古文的位置变体来自前后连接环境和字体 shaping，不是语法时态；Unicode 编码抽象字符，GB/PUA/glyph name 清单更多对应实际可见字形和合体字；奥云和蒙科立等公司的字形命名与映射规则不能混用。";
    if (v === "script") return base + "重点回答传统蒙古文书写方向、词首/词中/词尾/独立形、字母变体、连写规则、满都拉/阿里嘎里等相关问题。";
    if (v === "font") return base + "重点回答 Unicode、GB/T 标准、PUA、glyph name、显现形式、强制合体字、非强制合体字、奥云/蒙科立字体规则等问题。";
    if (v === "culture") return base + "重点回答蒙古族历史文化、礼俗、服饰、音乐、草原生活、蒙古包、马文化、哈达等问题。";
    if (v === "festival") return base + "重点回答节日名称、祝福语、传统蒙古文表达、节日习俗和贺卡用语；无法确认的蒙古文翻译要说明不确定。";
    if (v === "ornament") return base + "重点回答蒙古族纹样、云纹、盘肠纹、回纹、犄纹、吉祥纹样、色彩象征和适合放在设计里的位置。";
    return base + "优先回答传统蒙古文、蒙古文字体与蒙古族文化相关问题。";
  }
  async function send() {
    const text = $("prompt").value.trim();
    if (!text) return;
    add("user", text);
    $("sendBtn").disabled = true;
    add("assistant", "正在生成...");
    const pending = chat.lastChild;
    try {
      const res = await fetch("/api/local_ai/chat", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ prompt: text, system: taskSystem(), max_new_tokens: 900, temperature: 0.55 })
      });
      const data = await res.json();
      pending.textContent = data.ok ? (data.text || "没有返回内容。") : ("生成失败：" + (data.error || "未知错误"));
      if (data.model) {
        const m = document.createElement("div");
        m.className = "meta";
        m.textContent = data.model + (data.gpu_memory_allocated_gb ? " · GPU " + data.gpu_memory_allocated_gb + "GB" : "");
        pending.appendChild(m);
      }
    } catch (err) {
      pending.textContent = "请求失败：" + err.message;
    } finally {
      $("sendBtn").disabled = false;
    }
  }
  document.querySelectorAll("[data-fill]").forEach(btn => {
    btn.addEventListener("click", () => { $("prompt").value = btn.dataset.fill; $("prompt").focus(); });
  });
  $("checkBtn").addEventListener("click", () => check(false));
  $("loadBtn").addEventListener("click", () => check(true));
  $("sendBtn").addEventListener("click", send);
  $("clearBtn").addEventListener("click", () => { chat.innerHTML = ""; $("prompt").value = ""; });
  add("assistant", "传统蒙古文与蒙古族文化问答助手已接入。可以直接问传统蒙古文书写、字形变体、编码规则、蒙古族节日、纹样和文化相关问题。");
  check(false);
})();
</script>
</body>
</html>
""")


_LOCAL_DEEPSEEK_HOME_ENTRY = r"""
<section id="localDeepSeekFeatureEntry" style="
  margin: 18px 0;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  background: #f8fafc;
  padding: 14px 16px;
">
  <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
    <div>
      <div style="font-weight:800;font-size:16px;color:#0f172a;">DeepSeek-R1 32B 本地问答助手</div>
      <div style="font-size:13px;color:#52647a;margin-top:4px;">不需要 API Key，使用数据盘本地 DeepSeek-R1-Distill-Qwen-32B 4bit，回答传统蒙古文、字体规则、编码标准和蒙古族文化问题。</div>
    </div>
    <a href="/local_deepseek" target="_blank" style="
      display:inline-flex;
      align-items:center;
      justify-content:center;
      min-height:38px;
      padding:0 14px;
      border-radius:6px;
      background:#0f172a;
      color:#fff;
      text-decoration:none;
      font-weight:700;
      font-size:13px;
    ">打开 DeepSeek 32B</a>
  </div>
</section>
"""


@app.middleware("http")
async def _local_deepseek_home_entry_inject(request, call_next):
    response = await call_next(request)
    if request.url.path not in ["/", ""]:
        return response
    ctype = response.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return response
    body = b""
    async for chunk in response.body_iterator:
        body += chunk
    html = body.decode("utf-8", errors="ignore")
    if "localDeepSeekFeatureEntry" not in html:
        marker = '<form id="form">'
        if marker in html:
            html = html.replace(marker, _LOCAL_DEEPSEEK_HOME_ENTRY + "\n" + marker, 1)
        elif "</body>" in html:
            html = html.replace("</body>", _LOCAL_DEEPSEEK_HOME_ENTRY + "\n</body>")
        else:
            html += _LOCAL_DEEPSEEK_HOME_ENTRY
    headers = dict(response.headers)
    headers.pop("content-length", None)
    return HTMLResponse(content=html, status_code=response.status_code, headers=headers)

# ================= LORA_LAB_FEATURE_END =================

# ================= FINAL_OYUN_COMPLETE_PREVIEW_ASGI_WRAPPER_START =================
# 最外层 ASGI 包装器：强制让 /oyun_gb_preview 使用 complete 版页面。
# 解决旧 middleware 先拦截导致新 complete middleware 不生效的问题。

class _OYCCompletePreviewASGIWrapper:
    def __init__(self, inner_app):
        self.inner_app = inner_app

    def __getattr__(self, name):
        return getattr(self.inner_app, name)

    async def __call__(self, scope, receive, send):
        try:
            if scope.get("type") == "http" and scope.get("path") == "/oyun_gb_preview":
                from starlette.requests import Request as _OYCStarletteRequest

                request = _OYCStarletteRequest(scope, receive=receive)
                response = await _oyc_complete_preview_page(request)
                await response(scope, receive, send)
                return
        except Exception as e:
            from starlette.responses import HTMLResponse as _OYCErrorHTMLResponse
            err = _OYCErrorHTMLResponse(
                "<h2>奥云 complete 预览出错</h2><pre>%s</pre>" % _oyc_html.escape(str(e)),
                status_code=500,
            )
            await err(scope, receive, send)
            return

        await self.inner_app(scope, receive, send)


app = _OYCCompletePreviewASGIWrapper(app)

print("[FINAL-OYUN-COMPLETE-PREVIEW-ASGI] outer wrapper installed for /oyun_gb_preview")
# ================= FINAL_OYUN_COMPLETE_PREVIEW_ASGI_WRAPPER_END =================

# ================= FIX_MISSING_OYUN_COMPLETE_PREVIEW_FUNCS_START =================
# 补齐 ASGI wrapper 所需的 complete 预览函数。
# 当前 app 已经被 _OYCCompletePreviewASGIWrapper 包装，所以这里只补函数和 SVG 接口。

import json as _oyc_json
import html as _oyc_html
from pathlib import Path as _oyc_Path
from fastapi.responses import HTMLResponse as _oyc_HTMLResponse, Response as _oyc_Response
from fontTools.ttLib import TTFont as _oyc_TTFont
from fontTools.pens.svgPathPen import SVGPathPen as _oyc_SVGPathPen
from fontTools.pens.boundsPen import BoundsPen as _oyc_BoundsPen
from fontTools.pens.transformPen import TransformPen as _oyc_TransformPen
from fontTools.misc.transform import Transform as _oyc_Transform

_oyc_root = _oyc_Path(__file__).resolve().parent
_oyc_out = _oyc_root / "output" / "oyun_gb_ttf_steps"


def _oyc_load_complete_report():
    p = _oyc_out / "gb_morph_complete_report.json"
    if not p.exists():
        return None
    return _oyc_json.loads(p.read_text(encoding="utf-8"))


def _oyc_svg_for_complete_glyph(step: int, glyph_name: str):
    fp = _oyc_out / f"complete_oyun_gb_step_{step:02d}.ttf"

    if not fp.exists():
        return """<svg xmlns="http://www.w3.org/2000/svg" width="90" height="120">
<text x="5" y="50" font-size="12">missing step</text>
</svg>"""

    try:
        font = _oyc_TTFont(str(fp))
        glyph_set = font.getGlyphSet()

        if glyph_name not in glyph_set:
            font.close()
            return """<svg xmlns="http://www.w3.org/2000/svg" width="90" height="120">
<text x="5" y="50" font-size="12">missing glyph</text>
</svg>"""

        glyph = glyph_set[glyph_name]
        bounds_pen = _oyc_BoundsPen(glyph_set)
        glyph.draw(bounds_pen)
        bounds = bounds_pen.bounds
        pen = _oyc_SVGPathPen(glyph_set)
        glyph.draw(pen)
        d = pen.getCommands()
        font.close()

        if not d:
            return '<svg xmlns="http://www.w3.org/2000/svg" width="136" height="136" viewBox="0 0 1000 1000"></svg>'

        if bounds:
            x0, y0, x1, y1 = [float(v) for v in bounds]
            w = max(1.0, x1 - x0)
            h = max(1.0, y1 - y0)
            pad = max(40.0, max(w, h) * 0.18)
            view_x = x0 - pad
            view_y = -y1 - pad
            view_w = w + pad * 2
            view_h = h + pad * 2
        else:
            view_x, view_y, view_w, view_h = 0.0, -1800.0, 1000.0, 1800.0

        return f'''<svg xmlns="http://www.w3.org/2000/svg" width="136" height="136" viewBox="{view_x:.3f} {view_y:.3f} {view_w:.3f} {view_h:.3f}" preserveAspectRatio="xMidYMid meet">
<g transform="scale(1,-1)"><path d="{_oyc_html.escape(d)}" fill="#111" fill-rule="evenodd"/></g>
</svg>'''

    except Exception as e:
        return f'''<svg xmlns="http://www.w3.org/2000/svg" width="90" height="120">
<text x="5" y="50" font-size="10">{_oyc_html.escape(str(e))}</text>
</svg>'''


@app.get("/api/foundry/oyun_gb/complete_glyph_svg/{step}/{glyph_name}")
async def _oyc_complete_glyph_svg(step: int, glyph_name: str):
    svg = _oyc_svg_for_complete_glyph(step, glyph_name)
    return _oyc_Response(content=svg, media_type="image/svg+xml")


async def _oyc_complete_preview_page(request):
    report = _oyc_load_complete_report()

    if not report:
        return _oyc_HTMLResponse("""
        <h2>奥云 complete 预览不可用</h2>
        <p>没有找到 output/oyun_gb_ttf_steps/gb_morph_complete_report.json</p>
        <p>请先运行 complete 奥云生成脚本。</p>
        """, status_code=404)

    prepared = report.get("prepared", []) or []
    skipped = report.get("skipped_items", []) or []

    try:
        limit = int(request.query_params.get("limit", "1000"))
    except Exception:
        limit = 1000

    shown = prepared[:limit]

    runtime_rows = report.get("runtime_rows", "")
    generated = report.get("generated_runtime_glyphs", "")
    skipped_count = report.get("skipped", "")
    algo = report.get("algorithm", "")
    step_files = _foundry_vf_complete_step_files("oyun")
    available_steps = [_foundry_vf_step_no(p) for p in step_files]
    if not available_steps:
        available_steps = list(range(1, 21))

    head_cells = ["<th>字形信息</th>"] + [f"<th>Step {i:02d}</th>" for i in available_steps]
    body_rows = []

    for idx, item in enumerate(shown, 1):
        gname = item.get("complete_glyph_name") or item.get("glyph_name") or ""
        rid = item.get("runtime_id", "")
        group = item.get("display_group", "")
        gb = item.get("gb_code", "")
        uni = item.get("base_unicode", "")
        src_a = item.get("source_glyph_a", "")
        src_b = item.get("source_glyph_b", "")
        synthetic = item.get("synthetic", "")

        info = f"""
        <td class="info">
          <b>#{idx}</b><br>
          <b>{_oyc_html.escape(str(rid))}</b><br>
          {_oyc_html.escape(str(group))}<br>
          GB: {_oyc_html.escape(str(gb))}<br>
          Unicode: {_oyc_html.escape(str(uni))}<br>
          glyph: {_oyc_html.escape(str(gname))}<br>
          synthetic: {_oyc_html.escape(str(synthetic))}<br>
          A: {_oyc_html.escape(str(src_a))}<br>
          B: {_oyc_html.escape(str(src_b))}
        </td>
        """

        cells = []
        for step in available_steps:
            src = f"/api/foundry/oyun_gb/complete_glyph_svg/{step}/{gname}"
            cells.append(f'<td><img src="{src}" loading="lazy"></td>')

        body_rows.append("<tr>" + info + "".join(cells) + "</tr>")

    skipped_html = "".join(
        f"<li>{_oyc_html.escape(str(x.get('runtime_id','')))} — {_oyc_html.escape(str(x.get('reason','')))}</li>"
        for x in skipped
    )

    html_doc = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>奥云 complete 国标预览</title>
<style>
body {{
    margin: 0;
    padding: 16px;
    font-family: Arial, "Microsoft YaHei", sans-serif;
    background: #f5f6f8;
}}
h2 {{ margin: 0 0 12px; }}
.notice {{
    background: #fff6cc;
    border: 1px solid #e8d27a;
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 12px;
}}
.btns a {{
    display: inline-block;
    background: #111;
    color: white;
    text-decoration: none;
    padding: 7px 12px;
    border-radius: 5px;
    margin-right: 8px;
    font-size: 13px;
}}
.cards {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 10px;
    margin: 12px 0;
}}
.card {{
    background: white;
    border-radius: 8px;
    padding: 10px;
    border: 1px solid #eee;
}}
.card .label {{
    font-size: 12px;
    color: #777;
}}
.card .num {{
    font-size: 22px;
    font-weight: bold;
    margin-top: 4px;
}}
.tablewrap {{
    overflow: auto;
    border: 1px solid #ddd;
    background: white;
    max-height: 78vh;
}}
table {{
    border-collapse: collapse;
    width: max-content;
    min-width: 100%;
}}
th, td {{
    border: 1px solid #eee;
    padding: 6px;
    text-align: center;
    vertical-align: middle;
}}
th {{
    position: sticky;
    top: 0;
    background: #f0f0f0;
    z-index: 3;
}}
td.info {{
    position: sticky;
    left: 0;
    background: #fff;
    z-index: 2;
    width: 220px;
    min-width: 220px;
    text-align: left;
    font-size: 12px;
    line-height: 1.45;
}}
td img {{
    width: 136px;
    height: 136px;
    object-fit: contain;
}}
.small {{
    color: #666;
    font-size: 12px;
    margin: 8px 0;
}}
.skipped {{
    background: white;
    border: 1px solid #eee;
    padding: 10px;
    margin-top: 12px;
    border-radius: 8px;
}}
{_foundry_vf_panel_css()}
</style>
</head>
<body>

<h2>奥云｜中国国标 complete 真实生成预览：每个可见国标项的 {len(available_steps)} 步</h2>

<div class="notice">
当前页面读取 <b>{len(available_steps)} 个 complete_oyun_gb_step_*.ttf</b>。<br>
这里显示的是 complete 版 {generated} 个可见国标项，不是旧版唯一 glyph 预览。
</div>

<div class="btns">
  <a href="/api/foundry/oyun_gb/download_zip">下载 complete 字体 ZIP</a>
  <a href="/api/foundry/oyun_gb/glyph_table.csv">下载 glyph 清单 CSV</a>
  <a href="/oyun_gb_preview?limit=1000">显示全部 complete glyph</a>
  <a href="/">返回首页</a>
</div>

{_foundry_vf_preview_panel("oyun")}

<div class="cards">
  <div class="card"><div class="label">国标 runtime 总项</div><div class="num">{runtime_rows}</div></div>
  <div class="card"><div class="label">complete 已生成可见项</div><div class="num">{generated}</div></div>
  <div class="card"><div class="label">剩余跳过项</div><div class="num">{skipped_count}</div></div>
  <div class="card"><div class="label">当前显示</div><div class="num">{len(shown)}</div></div>
  <div class="card"><div class="label">步数字体</div><div class="num">{len(available_steps)}</div></div>
</div>

<div class="small">算法：{_oyc_html.escape(str(algo))}</div>

<div class="tablewrap">
<table>
<thead>
<tr>
{''.join(head_cells)}
</tr>
</thead>
<tbody>
{''.join(body_rows)}
</tbody>
</table>
</div>

<div class="skipped">
<b>剩余跳过项：</b>
<ul>
{skipped_html}
</ul>
</div>

</body>
</html>
"""
    return _oyc_HTMLResponse(html_doc)


print("[FIX-MISSING-OYUN-COMPLETE-FUNCS] complete preview functions installed")
# ================= FIX_MISSING_OYUN_COMPLETE_PREVIEW_FUNCS_END =================


# ================= GB_REAL_VARIABLE_FEATURE_INSTALL =================
# 独立新页面：不覆盖现有首页、生成、预览和下载路由。
from pathlib import Path as _GBVarPath
from features.gb_real_variable import install_gb_real_variable_feature as _install_gb_real_variable_feature
app = _install_gb_real_variable_feature(app, _GBVarPath(__file__).resolve().parent)
# =====================================================================


# ================= GB_VARIABLE_HOME_LINK_INSTALL_V1 =================
# 只给首页下方蒙古文字体公司区域增加真实可变字体跳转入口。
# 不修改已有生成、预览、下载和旧可变字体路由。
from features.gb_variable_home_link import (
    install_gb_variable_home_link as _install_gb_variable_home_link,
)
app = _install_gb_variable_home_link(app)
# ====================================================================


# GB_VARIABLE_HOME_BUTTON_V2_INSTALL
# 只给首页蒙古文字体公司区域增加真实可变字体入口。
from features.gb_variable_home_button import (
    install_gb_variable_home_button as _install_gb_variable_home_button,
)
app = _install_gb_variable_home_button(app)


# ================= FESTIVAL_CARDS_FEATURE_INSTALL =================
# 独立新模块：节日祝福贺卡制作。只新增 /festival_cards 和 /api/festival_card/*。
# 不修改现有字体生成、LoRA、预览、下载、真实可变字体功能。
from pathlib import Path as _FestivalCardsPath
from features.festival_cards import (
    install_festival_cards_feature as _install_festival_cards_feature,
)
app = _install_festival_cards_feature(app, _FestivalCardsPath(__file__).resolve().parent)
# =================================================================


# ================= AUDIO_REACTIVE_FONT_FEATURE_INSTALL =================
# 独立新模块：音乐动态字体。只新增 /audio_reactive_font 和 /api/audio_reactive_font/*。
# 不修改现有字体生成、LoRA、ControlNet、贺卡、预览、下载功能。
from pathlib import Path as _AudioReactiveFontPath
from features.audio_reactive_font import (
    install_audio_reactive_font_feature as _install_audio_reactive_font_feature,
)
app = _install_audio_reactive_font_feature(app, _AudioReactiveFontPath(__file__).resolve().parent)
# ======================================================================


# ================= MENK_COMPLETE_PREVIEW_AND_VF_FIX_START =================
# Keep this as the final outer wrapper: Menk preview must use the complete
# Menk GB runtime result, not the earlier unique-glyph matrix report.

import csv as _mnc_csv
import html as _mnc_html
import json as _mnc_json
import re as _mnc_re
import urllib.parse as _mnc_urlparse
from pathlib import Path as _mnc_Path

from fastapi.responses import HTMLResponse as _mnc_HTMLResponse
from fastapi.responses import Response as _mnc_Response
from fontTools.misc.transform import Transform as _mnc_Transform
from fontTools.pens.boundsPen import BoundsPen as _mnc_BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen as _mnc_SVGPathPen
from fontTools.pens.transformPen import TransformPen as _mnc_TransformPen
from fontTools.ttLib import TTFont as _mnc_TTFont


_mnc_root = _mnc_Path(__file__).resolve().parent
_mnc_out = _mnc_root / "output" / "menk_gb_ttf_steps"


def _mnc_step_no(path):
    m = _mnc_re.search(r"step[_\-]?0*(\d+)", path.name, _mnc_re.I)
    return int(m.group(1)) if m else 999999


def _mnc_complete_step_files():
    return sorted(
        [p for p in _mnc_out.glob("complete_menk_gb_step_*.ttf") if p.is_file()],
        key=_mnc_step_no,
    )


def _mnc_complete_report_path():
    candidates = [
        _mnc_out / "menk_gb_complete_report.json",
        _mnc_out / "gb_morph_complete_report.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def _mnc_complete_runtime_csv_path():
    return _mnc_out / "menk_gb_complete_runtime_table.csv"


def _mnc_load_complete_report():
    p = _mnc_complete_report_path()
    if not p.exists():
        return {}, p
    return _mnc_json.loads(p.read_text(encoding="utf-8")), p


def _mnc_load_prepared_rows(report):
    rows = list(report.get("prepared") or [])
    if rows:
        return rows

    csv_path = _mnc_complete_runtime_csv_path()
    if not csv_path.exists():
        return []

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        return [
            row
            for row in _mnc_csv.DictReader(f)
            if row.get("complete_glyph_name")
        ]


def _mnc_svg_for_complete_glyph(step, glyph_name):
    step = int(step)
    glyph_name = _mnc_urlparse.unquote(str(glyph_name))
    fp = _mnc_out / f"complete_menk_gb_step_{step:02d}.ttf"

    if not fp.exists():
        return """<svg xmlns="http://www.w3.org/2000/svg" width="90" height="120">
<text x="5" y="55" font-size="12">missing step</text>
</svg>"""

    try:
        font = _mnc_TTFont(str(fp))
        glyph_set = font.getGlyphSet()

        if glyph_name not in glyph_set:
            font.close()
            return """<svg xmlns="http://www.w3.org/2000/svg" width="90" height="120">
<text x="5" y="55" font-size="12">missing glyph</text>
</svg>"""

        glyph = glyph_set[glyph_name]
        bounds_pen = _mnc_BoundsPen(glyph_set)
        glyph.draw(bounds_pen)
        bounds = bounds_pen.bounds
        pen = _mnc_SVGPathPen(glyph_set)
        glyph.draw(pen)
        d = pen.getCommands()
        font.close()

        if not d:
            return '<svg xmlns="http://www.w3.org/2000/svg" width="136" height="136" viewBox="0 0 1000 1000"></svg>'

        if bounds:
            x0, y0, x1, y1 = [float(v) for v in bounds]
            w = max(1.0, x1 - x0)
            h = max(1.0, y1 - y0)
            pad = max(40.0, max(w, h) * 0.18)
            view_x = x0 - pad
            view_y = -y1 - pad
            view_w = w + pad * 2
            view_h = h + pad * 2
        else:
            view_x, view_y, view_w, view_h = 0.0, -1800.0, 1000.0, 1800.0

        return f'''<svg xmlns="http://www.w3.org/2000/svg" width="136" height="136" viewBox="{view_x:.3f} {view_y:.3f} {view_w:.3f} {view_h:.3f}" preserveAspectRatio="xMidYMid meet">
<g transform="scale(1,-1)"><path d="{_mnc_html.escape(d)}" fill="#111" fill-rule="evenodd"/></g>
</svg>'''

    except Exception as e:
        return f'''<svg xmlns="http://www.w3.org/2000/svg" width="90" height="120">
<text x="5" y="55" font-size="10">{_mnc_html.escape(str(e))}</text>
</svg>'''


def _mnc_safe_int(value, fallback):
    try:
        return int(value)
    except Exception:
        return fallback


async def _mnc_complete_preview_page(request):
    report, report_path = _mnc_load_complete_report()
    step_files = _mnc_complete_step_files()
    available_steps = [_mnc_step_no(p) for p in step_files]

    if not report or not step_files:
        msg = _mnc_html.escape(str(report_path))
        return _mnc_HTMLResponse(f"""
<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>蒙科立 complete 预览未就绪</title></head>
<body style="font-family:Arial,'Microsoft YaHei',sans-serif;padding:24px">
<h2>蒙科立 complete 预览未就绪</h2>
<p>还没有找到完整生成结果：{msg}</p>
<p>请先在首页按“蒙科立｜中国国标版”重新生成，再打开本页。</p>
<p><a href="/">返回首页</a></p>
</body>
</html>
""", status_code=404)

    rows = _mnc_load_prepared_rows(report)
    try:
        limit = int(request.query_params.get("limit", "140"))
    except Exception:
        limit = 140
    limit = max(1, min(limit, 2000))
    shown = rows[:limit]

    runtime_rows = _mnc_safe_int(report.get("runtime_rows"), len(rows))
    generated = _mnc_safe_int(report.get("generated_runtime_glyphs"), len(rows))
    skipped_raw = report.get("skipped")
    skipped_items = list(report.get("skipped_items") or [])
    skipped_count = _mnc_safe_int(skipped_raw, len(skipped_items))
    algo = report.get("algorithm", "menk_complete_gb_runtime_item_generation")
    report_mtime = int(report_path.stat().st_mtime) if report_path.exists() else 0

    head_cells = ['<th class="info">字形信息</th>']
    for step in available_steps:
        head_cells.append(f"<th>Step {step:02d}</th>")

    body_rows = []
    for idx, row in enumerate(shown, start=1):
        gname = str(row.get("complete_glyph_name") or row.get("glyph") or "")
        if not gname:
            continue
        glyph_q = _mnc_urlparse.quote(gname, safe="")
        runtime_id = row.get("runtime_id", "")
        group = row.get("display_group", "")
        gb_code = row.get("gb_code", "")
        base_unicode = row.get("base_unicode", "")
        source_a = row.get("source_glyph_a", "")
        source_b = row.get("source_glyph_b", "")
        mode = row.get("mode", "")
        synthetic = row.get("synthetic", False)

        info = f"""
<td class="info">
  <b>#{idx}</b><br>
  <b>{_mnc_html.escape(str(runtime_id))}</b><br>
  {_mnc_html.escape(str(group))}<br>
  GB: {_mnc_html.escape(str(gb_code))}<br>
  Unicode: {_mnc_html.escape(str(base_unicode))}<br>
  glyph: {_mnc_html.escape(str(gname))}<br>
  A: {_mnc_html.escape(str(source_a))}<br>
  B: {_mnc_html.escape(str(source_b))}<br>
  mode: {_mnc_html.escape(str(mode))}<br>
  synthetic: {_mnc_html.escape(str(synthetic))}
</td>
"""
        cells = []
        for step in available_steps:
            src = f"/api/foundry/menk_gb/complete_glyph_svg/{step}/{glyph_q}?v={report_mtime}"
            cells.append(f'<td><img src="{src}" loading="lazy" alt="step {step:02d}"></td>')
        body_rows.append("<tr>" + info + "".join(cells) + "</tr>")

    skipped_html = "".join(
        f"<li>{_mnc_html.escape(str(item.get('runtime_id', '')))}：{_mnc_html.escape(str(item.get('reason', '')))}</li>"
        for item in skipped_items[:80]
    )
    if not skipped_html:
        skipped_html = "<li>无</li>"

    css = f"""
body {{
  margin: 0;
  padding: 18px;
  background: #f5f6f8;
  color: #111827;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}}
h1 {{ margin: 0 0 10px; font-size: 24px; }}
.notice {{
  background: #fff8d8;
  border: 1px solid #eadb91;
  border-radius: 10px;
  padding: 12px 14px;
  line-height: 1.7;
  margin: 14px 0 18px;
}}
.btns {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0; }}
.btns a {{
  display: inline-flex;
  align-items: center;
  min-height: 34px;
  padding: 8px 13px;
  border-radius: 8px;
  background: #111;
  color: white;
  text-decoration: none;
  font-size: 14px;
  font-weight: 700;
}}
.cards {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin: 16px 0 18px;
}}
.card {{
  background: white;
  border-radius: 12px;
  padding: 14px;
  box-shadow: 0 1px 4px rgba(0,0,0,.08);
}}
.card .label {{ color: #64748b; font-size: 12px; }}
.card .num {{ margin-top: 5px; font-size: 22px; font-weight: 800; }}
.small {{ color: #64748b; font-size: 12px; margin: 8px 0; }}
.tablewrap {{
  overflow: auto;
  border: 1px solid #ddd;
  background: white;
  max-height: 78vh;
}}
table {{ border-collapse: collapse; width: max-content; min-width: 100%; }}
th, td {{ border: 1px solid #eee; padding: 6px; text-align: center; vertical-align: middle; }}
th {{ position: sticky; top: 0; background: #f0f0f0; z-index: 3; }}
td.info {{
  position: sticky;
  left: 0;
  background: #fff;
  z-index: 2;
  width: 230px;
  min-width: 230px;
  text-align: left;
  font-size: 12px;
  line-height: 1.45;
}}
td img {{ width: 136px; height: 136px; object-fit: contain; background: #fff; }}
.skipped {{
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 10px 12px;
  margin-top: 12px;
}}
{_foundry_vf_panel_css()}
"""

    html_doc = f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>蒙科立｜中国国标 complete 真实生成预览</title>
<style>{css}</style>
</head>
<body>
<h1>蒙科立｜中国国标 complete 真实生成预览：每个可见国标项的 {len(available_steps)} 步</h1>

<div class="notice">
当前页面读取 <b>{len(available_steps)} 个 complete_menk_gb_step_*.ttf</b>。<br>
这里显示的是蒙科立 complete 版 <b>{generated}</b> 个可见国标项，使用 <b>menk_gb_complete_report.json</b> 和
<b>menk_gb_complete_runtime_table.csv</b>，不再使用旧的唯一 glyph 矩阵统计。
</div>

<div class="btns">
  <a href="/api/foundry/menk_gb/download_zip">下载 complete 字体 ZIP</a>
  <a href="/api/foundry/menk_gb/glyph_table.csv">下载 complete runtime CSV</a>
  <a href="/menk_gb_preview?limit=1000">显示全部 complete glyph</a>
  <a href="/">返回首页</a>
</div>

{_foundry_vf_preview_panel("menk")}

<div class="cards">
  <div class="card"><div class="label">国标 runtime 总项</div><div class="num">{runtime_rows}</div></div>
  <div class="card"><div class="label">complete 已生成可见项</div><div class="num">{generated}</div></div>
  <div class="card"><div class="label">剩余跳过项</div><div class="num">{skipped_count}</div></div>
  <div class="card"><div class="label">当前显示</div><div class="num">{len(shown)}</div></div>
  <div class="card"><div class="label">步数字体</div><div class="num">{len(available_steps)}</div></div>
</div>

<div class="small">算法：{_mnc_html.escape(str(algo))}</div>

<div class="tablewrap">
<table>
<thead><tr>{''.join(head_cells)}</tr></thead>
<tbody>{''.join(body_rows)}</tbody>
</table>
</div>

<div class="skipped">
<b>剩余跳过项：</b>
<ul>{skipped_html}</ul>
</div>
</body>
</html>
"""
    return _mnc_HTMLResponse(html_doc)


class _MenkCompletePreviewASGIWrapper:
    def __init__(self, inner_app):
        self.inner_app = inner_app

    def __getattr__(self, name):
        return getattr(self.inner_app, name)

    async def __call__(self, scope, receive, send):
        path = scope.get("path") or ""
        try:
            if scope.get("type") == "http" and path == "/menk_gb_preview":
                from starlette.requests import Request as _MNCStarletteRequest

                request = _MNCStarletteRequest(scope, receive=receive)
                response = await _mnc_complete_preview_page(request)
                await response(scope, receive, send)
                return

            if scope.get("type") == "http" and path.startswith("/api/foundry/menk_gb/complete_glyph_svg/"):
                prefix = "/api/foundry/menk_gb/complete_glyph_svg/"
                tail = path[len(prefix):]
                step_raw, _, glyph_raw = tail.partition("/")
                svg = _mnc_svg_for_complete_glyph(int(step_raw), glyph_raw)
                response = _mnc_Response(content=svg, media_type="image/svg+xml")
                await response(scope, receive, send)
                return

            if scope.get("type") == "http" and path in {
                "/api/foundry/menk_gb/glyph_table.csv",
                "/api/foundry/menk_gb/complete_runtime_table.csv",
            }:
                csv_path = _mnc_complete_runtime_csv_path()
                if csv_path.exists():
                    data = csv_path.read_bytes()
                    headers = {"Content-Disposition": 'attachment; filename="menk_gb_complete_runtime_table.csv"'}
                    response = _mnc_Response(content=data, media_type="text/csv; charset=utf-8", headers=headers)
                    await response(scope, receive, send)
                    return
        except Exception as e:
            err = _mnc_HTMLResponse(
                "<h2>蒙科立 complete 预览出错</h2><pre>%s</pre>" % _mnc_html.escape(str(e)),
                status_code=500,
            )
            await err(scope, receive, send)
            return

        await self.inner_app(scope, receive, send)


app = _MenkCompletePreviewASGIWrapper(app)

print("[MENK-COMPLETE-PREVIEW-AND-VF-FIX] outer wrapper installed for /menk_gb_preview")
# ================= MENK_COMPLETE_PREVIEW_AND_VF_FIX_END =================
