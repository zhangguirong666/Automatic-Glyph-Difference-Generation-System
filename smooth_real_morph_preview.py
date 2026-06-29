from pathlib import Path
import re
import json
import math
from html import escape
from urllib.parse import quote

from fastapi import Query
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse
from fontTools.ttLib import TTFont


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_JOBS = BASE_DIR / "runtime_jobs"


def safe_name(s: str):
    s = str(s)
    keep = []
    for ch in s:
        if ch.isalnum() or ch in "._-":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:120] or "job"


def natural_key(p: Path):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", p.name)]


def job_dir(job_id: str):
    return RUNTIME_JOBS / safe_name(job_id)


def output_dir(job_id: str):
    return job_dir(job_id) / "outputs"


def list_fonts(job_id: str):
    d = output_dir(job_id)
    if not d.exists():
        return []
    return sorted(list(d.glob("*.ttf")) + list(d.glob("*.otf")), key=natural_key)


def get_cmap(font):
    cmap = {}
    for table in font["cmap"].tables:
        cmap.update(table.cmap)
    return cmap


def read_chars(job_id: str, fonts):
    chars_file = job_dir(job_id) / "chars.txt"
    chars = []
    seen = set()

    if chars_file.exists():
        text = chars_file.read_text(encoding="utf-8", errors="ignore")
        for ch in text:
            if ch in seen:
                continue
            seen.add(ch)
            cp = ord(ch)
            if ch.strip() == "" and cp != 0x20:
                continue
            chars.append({"ch": ch, "code": f"U+{cp:04X}", "cp": cp})

    if chars:
        return chars[:500]

    if not fonts:
        return []

    try:
        font = TTFont(str(fonts[0]), lazy=True)
        cps = set()
        for table in font["cmap"].tables:
            cps.update(table.cmap.keys())
        font.close()

        mongolian = [cp for cp in sorted(cps) if 0x1800 <= cp <= 0x18AF]
        if mongolian:
            cps = mongolian
        else:
            cps = [cp for cp in sorted(cps) if cp >= 0x20 and cp not in [0xFEFF]]

        for cp in cps[:500]:
            ch = chr(cp)
            if ch.strip() == "" and cp != 0x20:
                continue
            chars.append({"ch": ch, "code": f"U+{cp:04X}", "cp": cp})
    except Exception:
        pass

    return chars


def lerp(a, b, t):
    return a + (b - a) * t


def glyph_interpolated_path(font0, font1, gname, t):
    glyf0 = font0["glyf"]
    glyf1 = font1["glyf"]

    if gname not in glyf0 or gname not in glyf1:
        return ""

    g0 = glyf0[gname]
    g1 = glyf1[gname]

    g0.expand(glyf0)
    g1.expand(glyf1)

    if g0.isComposite() or g1.isComposite():
        return ""

    if g0.numberOfContours <= 0 or g1.numberOfContours <= 0:
        return ""

    coords0, end_pts0, flags0 = g0.getCoordinates(glyf0)
    coords1, end_pts1, flags1 = g1.getCoordinates(glyf1)

    if len(coords0) != len(coords1):
        return ""

    if list(end_pts0) != list(end_pts1):
        return ""

    coords = []
    for p0, p1 in zip(coords0, coords1):
        x = lerp(float(p0[0]), float(p1[0]), t)
        y = lerp(float(p0[1]), float(p1[1]), t)
        coords.append((x, y))

    parts = []
    start = 0

    for end in end_pts0:
        contour = coords[start:end + 1]
        start = end + 1

        if len(contour) < 2:
            continue

        x0, y0 = contour[0]
        parts.append(f"M{x0:.2f},{y0:.2f}")

        for x, y in contour[1:]:
            parts.append(f"L{x:.2f},{y:.2f}")

        parts.append("Z")

    return " ".join(parts)


def smooth_svg(job_id: str, pos: float, text: str, vertical: bool):
    fonts = list_fonts(job_id)

    if not fonts:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="260"><text x="40" y="120">没有找到生成字体</text></svg>'

    if len(fonts) == 1:
        idx0 = idx1 = 0
        local_t = 0.0
    else:
        pos = max(0.0, min(1.0, float(pos)))
        raw = pos * (len(fonts) - 1)
        idx0 = int(math.floor(raw))
        idx1 = min(idx0 + 1, len(fonts) - 1)
        local_t = raw - idx0

    font0 = TTFont(str(fonts[idx0]))
    font1 = TTFont(str(fonts[idx1]))

    cmap0 = get_cmap(font0)
    hmtx0 = font0["hmtx"].metrics
    hmtx1 = font1["hmtx"].metrics

    hhea = font0["hhea"]
    ascent = int(hhea.ascent)
    descent = int(hhea.descent)
    upm = int(font0["head"].unitsPerEm)

    text = text or "ABCDEabcde123"
    text = text[:80]

    paths = []

    if vertical:
        width = 620
        x = 310
        y = 90 + ascent
        height = max(900, len(text) * int(upm * 0.92) + 260)

        for ch in text:
            cp = ord(ch)
            gname = cmap0.get(cp)

            if not gname:
                y += int(upm * 0.75)
                continue

            d = glyph_interpolated_path(font0, font1, gname, local_t)

            if d:
                paths.append(
                    f'<path d="{escape(d)}" transform="translate({x},{y}) scale(1,-1)" fill="#000000"/>'
                )

            y += int(upm * 0.92)

    else:
        x = 70
        baseline = 90 + ascent
        height = max(620, ascent - descent + 180)

        for ch in text:
            cp = ord(ch)
            gname = cmap0.get(cp)

            if not gname:
                x += int(upm * 0.45)
                continue

            d = glyph_interpolated_path(font0, font1, gname, local_t)

            aw0 = hmtx0.get(gname, (upm, 0))[0]
            aw1 = hmtx1.get(gname, (aw0, 0))[0]
            aw = max(int(lerp(float(aw0), float(aw1), local_t)), int(upm * 0.45))

            if d:
                paths.append(
                    f'<path d="{escape(d)}" transform="translate({x},{baseline}) scale(1,-1)" fill="#000000"/>'
                )

            x += aw + 70

        width = max(900, x + 80)

    font0.close()
    font1.close()

    if not paths:
        paths.append('<text x="40" y="120" font-size="32" fill="#666">当前字符无法生成连续轮廓插值</text>')

    step_label = f"{idx0 + 1:02d} → {idx1 + 1:02d}"
    morph_label = f"{pos * 100:.1f}%"

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="24" y="34" font-size="22" fill="#333333">Smooth Morph ｜ Step {step_label} ｜ {morph_label}</text>
  {chr(10).join(paths)}
</svg>
'''


MAIN_SCRIPT = r'''
<script id="smooth-real-morph-main-entry-v1">
(function(){
  if(window.__SMOOTH_REAL_MORPH_MAIN_ENTRY_V1__) return;
  window.__SMOOTH_REAL_MORPH_MAIN_ENTRY_V1__ = true;

  function updateEntry(){
    const btn = document.getElementById("singleRealPreviewEntry");
    if(!btn) return;

    const href = btn.getAttribute("href") || "";
    const m = href.match(/\/api\/unicode\/real_preview\/([^\/?#]+)/);
    if(!m) return;

    const jobId = decodeURIComponent(m[1]);

    btn.href = "/api/unicode/smooth_morph_preview/" + encodeURIComponent(jobId);
    btn.textContent = "查看全部生成结果预览（连续可变）";
  }

  document.addEventListener("DOMContentLoaded", updateEntry);
  setInterval(updateEntry, 800);
})();
</script>
'''


def install_smooth_real_morph_preview(app):
    if getattr(app.state, "_smooth_real_morph_preview_v1", False):
        return

    app.state._smooth_real_morph_preview_v1 = True

    @app.get("/api/unicode/smooth_morph_svg/{job_id}")
    def api_smooth_morph_svg(
        job_id: str,
        pos: float = Query(0.0),
        text: str = Query("ABCDEabcde123"),
        vertical: int = Query(0),
    ):
        svg = smooth_svg(job_id, pos, text, bool(vertical))
        return Response(svg, media_type="image/svg+xml")

    @app.get("/api/unicode/smooth_morph_font/{job_id}/{filename}")
    def api_smooth_morph_font(job_id: str, filename: str):
        target = output_dir(job_id) / Path(filename).name

        if not target.exists():
            return JSONResponse({"error": "字体文件不存在。"}, status_code=404)

        return FileResponse(str(target), filename=target.name, media_type="font/ttf")

    @app.get("/api/unicode/smooth_morph_preview/{job_id}")
    def smooth_morph_preview_page(job_id: str):
        fonts = list_fonts(job_id)

        if not fonts:
            return HTMLResponse("<h2>没有找到本次生成的 TTF 文件</h2>", status_code=404)

        chars = read_chars(job_id, fonts)
        default_text = "".join(x["ch"] for x in chars[:12]) or "ABCDEabcde123"

        font_faces = []
        font_items = []

        for i, p in enumerate(fonts):
            face = f"SmoothStep{i+1:02d}"
            url = f"/api/unicode/smooth_morph_font/{safe_name(job_id)}/{quote(p.name)}"

            font_faces.append(
                f"@font-face{{font-family:'{face}';src:url('{url}') format('truetype');font-weight:400;font-style:normal;}}"
            )

            font_items.append({
                "index": i,
                "face": face,
                "name": p.name,
                "url": url,
            })

        chars_json = json.dumps(chars, ensure_ascii=False)
        fonts_json = json.dumps(font_items, ensure_ascii=False)

        html = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>连续可变字体预览</title>
<style>
{chr(10).join(font_faces)}

body {{
  margin: 0;
  background: #f3f4f6;
  color: #111;
  font-family: Arial, "Microsoft YaHei", sans-serif;
}}

.header {{
  background: #111827;
  color: white;
  padding: 22px 34px;
}}

.container {{
  max-width: 1280px;
  margin: 24px auto;
  padding: 0 24px;
}}

.panel {{
  background: white;
  border-radius: 14px;
  padding: 20px;
  margin-bottom: 18px;
  box-shadow: 0 8px 28px rgba(0,0,0,.08);
}}

.smooth-preview {{
  width: 100%;
  min-height: 420px;
  border: 1px solid #e5e7eb;
  border-radius: 14px;
  background: white;
  object-fit: contain;
}}

input[type=range] {{
  width: 100%;
}}

textarea {{
  width: 100%;
  min-height: 74px;
  box-sizing: border-box;
  padding: 12px;
  font-size: 16px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
}}

.meta {{
  font-family: monospace;
  color: #555;
  font-size: 13px;
  margin-bottom: 12px;
}}

.char-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
  gap: 8px;
  max-height: 220px;
  overflow: auto;
  margin-top: 10px;
}}

.char-btn {{
  border: 1px solid #e5e7eb;
  background: white;
  border-radius: 8px;
  padding: 6px 4px;
  cursor: pointer;
  text-align: center;
}}

.char-btn .ch {{
  font-size: 28px;
  display: block;
}}

.char-btn .code {{
  font-size: 11px;
  color: #666;
}}

.family-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}}

.family-card {{
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fafafa;
  padding: 12px;
}}

.family-title {{
  font-weight: 700;
  font-size: 13px;
  margin-bottom: 8px;
}}

.family-sample {{
  font-size: 54px;
  line-height: 1.15;
  min-height: 72px;
}}

button {{
  padding: 9px 13px;
  border-radius: 8px;
  border: 1px solid #d1d5db;
  background: white;
  cursor: pointer;
  margin-right: 8px;
}}

a.button {{
  display: inline-block;
  padding: 10px 14px;
  border-radius: 8px;
  background: #2563eb;
  color: white;
  text-decoration: none;
  font-weight: 700;
  margin-right: 8px;
}}
</style>
</head>
<body>

<div class="header">
  <h1>连续可变字体预览</h1>
  <div>基于本次生成的 {len(fonts)} 个真实 TTF，实时计算相邻 Step 的轮廓插值。</div>
</div>

<div class="container">

  <div class="panel">
    <a class="button" href="/api/unicode/font_zip/{escape(safe_name(job_id))}" target="_blank">下载全部 TTF 字体压缩包</a>
  </div>

  <div class="panel">
    <h2>1. 连续轮廓插值滑杆</h2>
    <div class="meta">
      当前连续位置：<span id="posText">0.0%</span>
      ｜ 不是帧切换，而是根据相邻两个真实 TTF 的 glyph 坐标实时插值
    </div>

    <img id="smoothImg" class="smooth-preview" src="">

    <div style="margin-top:16px;">
      <input id="smoothSlider" type="range" min="0" max="1000" value="0" step="1">
    </div>

    <div style="margin-top:12px;">
      <textarea id="previewText">{escape(default_text)}</textarea>
    </div>

    <div style="margin-top:12px;">
      <button id="playBtn" type="button">播放</button>
      <button id="verticalBtn" type="button">切换竖排</button>
      <button id="resetBtn" type="button">恢复默认文字</button>
    </div>
  </div>

  <div class="panel">
    <h2>2. 本次生成字符列表</h2>
    <div id="charGrid" class="char-grid"></div>
  </div>

  <div class="panel">
    <h2>3. 20 个真实 TTF 字体家族预览</h2>
    <div id="familyGrid" class="family-grid"></div>
  </div>

</div>

<script>
const JOB_ID = {json.dumps(safe_name(job_id), ensure_ascii=False)};
const CHARS = {chars_json};
const FONTS = {fonts_json};
const DEFAULT_TEXT = {json.dumps(default_text, ensure_ascii=False)};

const img = document.getElementById("smoothImg");
const slider = document.getElementById("smoothSlider");
const posText = document.getElementById("posText");
const previewText = document.getElementById("previewText");
const charGrid = document.getElementById("charGrid");
const familyGrid = document.getElementById("familyGrid");
const playBtn = document.getElementById("playBtn");
const verticalBtn = document.getElementById("verticalBtn");
const resetBtn = document.getElementById("resetBtn");

let target = 0;
let current = 0;
let vertical = false;
let playing = false;
let rafStarted = false;

function textValue(){{
  return previewText.value || DEFAULT_TEXT;
}}

function updateImg(force=false){{
  const diff = Math.abs(current - target);
  current += (target - current) * 0.22;

  if(diff < 0.15){{
    current = target;
  }}

  const pos = current / 1000;
  posText.textContent = (pos * 100).toFixed(1) + "%";

  const url =
    "/api/unicode/smooth_morph_svg/" + encodeURIComponent(JOB_ID) +
    "?pos=" + encodeURIComponent(pos.toFixed(5)) +
    "&text=" + encodeURIComponent(textValue()) +
    "&vertical=" + (vertical ? "1" : "0") +
    "&t=" + Date.now();

  // 为了避免过度请求，只有位置变化明显或强制刷新时才更新图像
  if(force || Math.abs(current - Number(img.dataset.lastValue || -9999)) > 2){{
    img.src = url;
    img.dataset.lastValue = String(current);
  }}

  if(playing){{
    target += 3.2;
    if(target > 1000) target = 0;
    slider.value = String(Math.round(target));
  }}

  requestAnimationFrame(updateImg);
}}

slider.addEventListener("input", function(){{
  target = parseFloat(slider.value);
}});

previewText.addEventListener("input", function(){{
  img.dataset.lastValue = "-9999";
  updateImg(true);
  updateFamilySamples();
}});

verticalBtn.onclick = function(){{
  vertical = !vertical;
  img.dataset.lastValue = "-9999";
  updateImg(true);
}};

resetBtn.onclick = function(){{
  previewText.value = DEFAULT_TEXT;
  target = 0;
  slider.value = "0";
  img.dataset.lastValue = "-9999";
  updateImg(true);
  updateFamilySamples();
}};

playBtn.onclick = function(){{
  playing = !playing;
  playBtn.textContent = playing ? "暂停" : "播放";
}};

function renderChars(){{
  charGrid.innerHTML = "";

  CHARS.forEach(item => {{
    const b = document.createElement("button");
    b.className = "char-btn";
    b.innerHTML = "<span class='ch'>" + item.ch + "</span><span class='code'>" + item.code + "</span>";
    b.onclick = function(){{
      previewText.value = item.ch;
      img.dataset.lastValue = "-9999";
      updateImg(true);
      updateFamilySamples();
    }};
    charGrid.appendChild(b);
  }});
}}

function renderFamily(){{
  familyGrid.innerHTML = "";

  FONTS.forEach((item, idx) => {{
    const card = document.createElement("div");
    card.className = "family-card";

    const title = document.createElement("div");
    title.className = "family-title";
    title.textContent = String(idx + 1).padStart(2, "0") + " ｜ " + item.name;

    const sample = document.createElement("div");
    sample.className = "family-sample";
    sample.style.fontFamily = item.face + ", sans-serif";
    sample.textContent = textValue().slice(0, 12);

    card.appendChild(title);
    card.appendChild(sample);
    familyGrid.appendChild(card);
  }});
}}

function updateFamilySamples(){{
  document.querySelectorAll(".family-sample").forEach(el => {{
    el.textContent = textValue().slice(0, 12);
  }});
}}

renderChars();
renderFamily();
updateImg(true);

if(!rafStarted){{
  rafStarted = true;
  requestAnimationFrame(updateImg);
}}
</script>

</body>
</html>
'''
        return HTMLResponse(html)

    @app.middleware("http")
    async def smooth_real_morph_middleware(request, call_next):
        response = await call_next(request)

        path = request.url.path
        content_type = response.headers.get("content-type", "")

        if path in ["/", "/unicode", "/unicode_async"] and "text/html" in content_type:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk

            text = body.decode("utf-8", errors="ignore")

            if "smooth-real-morph-main-entry-v1" not in text:
                if "</body>" in text:
                    text = text.replace("</body>", MAIN_SCRIPT + "\n</body>", 1)
                else:
                    text += MAIN_SCRIPT

            headers = dict(response.headers)
            headers.pop("content-length", None)

            return Response(
                content=text,
                status_code=response.status_code,
                headers=headers,
                media_type="text/html",
            )

        return response
