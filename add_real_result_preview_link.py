from pathlib import Path
from html import escape
from urllib.parse import quote
import re
import json

from fastapi import Query
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen


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


def font_svg(font_path: Path, text: str, vertical: bool = False):
    text = text or "ABCDEabcde123"

    try:
        font = TTFont(str(font_path))
        cmap = get_cmap(font)
        glyph_set = font.getGlyphSet()
        hmtx = font["hmtx"].metrics

        upm = int(font["head"].unitsPerEm)
        hhea = font["hhea"]
        ascent = int(hhea.ascent)
        descent = int(hhea.descent)

        paths = []

        if vertical:
            x = 260
            y = 80 + ascent
            width = 520
            height = max(900, len(text) * int(upm * 0.9) + 260)

            for ch in text[:80]:
                cp = ord(ch)
                gname = cmap.get(cp)

                if not gname or gname not in glyph_set:
                    y += int(upm * 0.7)
                    continue

                pen = SVGPathPen(glyph_set)
                glyph_set[gname].draw(pen)
                d = pen.getCommands()

                if d:
                    paths.append(
                        f'<path d="{escape(d)}" transform="translate({x},{y}) scale(1,-1)" fill="#000000"/>'
                    )

                y += int(upm * 0.9)

        else:
            x = 60
            baseline = 90 + ascent
            height = max(620, ascent - descent + 180)

            for ch in text[:100]:
                cp = ord(ch)
                gname = cmap.get(cp)

                if not gname or gname not in glyph_set:
                    x += int(upm * 0.45)
                    continue

                pen = SVGPathPen(glyph_set)
                glyph_set[gname].draw(pen)
                d = pen.getCommands()

                aw = hmtx.get(gname, (upm, 0))[0]
                aw = max(int(aw), int(upm * 0.45))

                if d:
                    paths.append(
                        f'<path d="{escape(d)}" transform="translate({x},{baseline}) scale(1,-1)" fill="#000000"/>'
                    )

                x += aw + 60

            width = max(900, x + 70)

        font.close()

        if not paths:
            paths.append('<text x="40" y="120" font-size="32" fill="#666">该字体未包含这些字符</text>')

        return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="24" y="34" font-size="22" fill="#333333">{escape(font_path.name)}</text>
  {chr(10).join(paths)}
</svg>
'''

    except Exception as e:
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="900" height="260" viewBox="0 0 900 260">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="40" y="120" font-size="26" fill="#c00">SVG 生成失败：{escape(str(e))}</text>
</svg>
'''


FRONTEND_SCRIPT = r'''
<script id="add-real-result-preview-link-v1">
(function(){
  if(window.__ADD_REAL_RESULT_PREVIEW_LINK_V1__) return;
  window.__ADD_REAL_RESULT_PREVIEW_LINK_V1__ = true;

  function extractJobId(){
    const zip = document.querySelector('a[href*="/api/unicode/font_zip/"]');
    if(zip){
      const href = zip.getAttribute("href") || "";
      const m = href.match(/\/api\/unicode\/font_zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);
    }

    const one = document.querySelector('a[href*="/api/unicode/download/"]');
    if(one){
      const href = one.getAttribute("href") || "";
      const m = href.match(/\/api\/unicode\/download\/([^\/?#]+)\//);
      if(m) return decodeURIComponent(m[1]);
    }

    return "";
  }

  function installPreviewLink(){
    const jobId = extractJobId();
    if(!jobId) return;

    if(document.getElementById("realResultPreviewLinkBox")) return;

    const zipBox = document.getElementById("compactTtfZipButtonBox");
    const host =
      zipBox ||
      document.getElementById("outputs") ||
      document.querySelector(".outputs") ||
      document.body;

    const box = document.createElement("div");
    box.id = "realResultPreviewLinkBox";
    box.style.cssText = "margin:10px 0 12px 0;";

    const a = document.createElement("a");
    a.href = "/api/unicode/real_preview/" + encodeURIComponent(jobId);
    a.target = "_blank";
    a.textContent = "打开真实生成结果预览";
    a.style.cssText = [
      "display:inline-block",
      "padding:10px 16px",
      "background:#111827",
      "color:#fff",
      "border-radius:8px",
      "text-decoration:none",
      "font-weight:700",
      "margin-left:8px"
    ].join(";");

    box.appendChild(a);

    if(zipBox && zipBox.parentElement){
      zipBox.appendChild(a);
    }else{
      host.insertBefore(box, host.firstChild);
    }
  }

  document.addEventListener("DOMContentLoaded", installPreviewLink);
  setInterval(installPreviewLink, 1000);
})();
</script>
'''


def install_add_real_result_preview_link(app):
    if getattr(app.state, "_add_real_result_preview_link_v1", False):
        return

    app.state._add_real_result_preview_link_v1 = True

    @app.get("/api/unicode/real_preview_font/{job_id}/{filename}")
    def real_preview_font(job_id: str, filename: str):
        target = output_dir(job_id) / Path(filename).name

        if not target.exists():
            return JSONResponse({"error": "字体文件不存在。"}, status_code=404)

        return FileResponse(str(target), filename=target.name, media_type="font/ttf")

    @app.get("/api/unicode/real_preview_svg/{job_id}/{index}")
    def real_preview_svg(
        job_id: str,
        index: int,
        text: str = Query("ABCDEabcde123"),
        vertical: int = Query(0),
    ):
        fonts = list_fonts(job_id)

        if not fonts:
            return Response("<svg></svg>", media_type="image/svg+xml", status_code=404)

        index = max(0, min(len(fonts) - 1, int(index)))

        svg = font_svg(fonts[index], text, vertical=bool(vertical))

        return Response(svg, media_type="image/svg+xml")

    @app.get("/api/unicode/real_preview/{job_id}")
    def real_preview_page(job_id: str):
        fonts = list_fonts(job_id)

        if not fonts:
            return HTMLResponse(
                "<h2>没有找到本次生成的 TTF 文件</h2>"
                "<p>请确认已经生成成功，并且 runtime_jobs/{job_id}/outputs/ 中存在字体文件。</p>",
                status_code=404
            )

        chars = read_chars(job_id, fonts)
        default_text = "".join(x["ch"] for x in chars[:12]) or "ABCDEabcde123"

        faces = []
        font_items = []

        for i, p in enumerate(fonts):
            face = f"RealPreviewStep{i+1:02d}"
            url = f"/api/unicode/real_preview_font/{safe_name(job_id)}/{quote(p.name)}"

            faces.append(
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
<title>真实生成结果预览</title>
<style>
{chr(10).join(faces)}

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

.header h1 {{
  margin: 0 0 6px 0;
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

textarea {{
  width: 100%;
  min-height: 76px;
  box-sizing: border-box;
  padding: 12px;
  font-size: 16px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
}}

.preview {{
  min-height: 180px;
  padding: 24px;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  background: #fafafa;
  font-size: 72px;
  line-height: 1.25;
  overflow: auto;
  margin-top: 14px;
}}

.preview.vertical {{
  writing-mode: vertical-lr;
  text-orientation: mixed;
  min-height: 420px;
}}

.font-list {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 8px;
}}

.font-btn {{
  text-align: left;
  padding: 10px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #f9fafb;
  cursor: pointer;
}}

.font-btn.active {{
  background: #2563eb;
  color: white;
}}

.matrix {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
}}

.card {{
  background: #f9fafb;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 10px;
}}

.card-title {{
  font-weight: 700;
  font-size: 13px;
  margin-bottom: 8px;
}}

.card img {{
  width: 100%;
  height: auto;
  background: white;
  border: 1px solid #e5e7eb;
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

.small {{
  font-size: 13px;
  color: #555;
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
  <h1>真实生成结果预览</h1>
  <div>任务 ID：{escape(safe_name(job_id))} ｜ 真实 TTF 文件：{len(fonts)} 个</div>
</div>

<div class="container">

  <div class="panel">
    <a class="button" href="/api/unicode/font_zip/{escape(safe_name(job_id))}" target="_blank">下载全部 TTF 字体压缩包</a>
  </div>

  <div class="panel">
    <h2>1. 选择预览文字</h2>
    <div class="small">字符来自本次生成任务的 chars.txt 或生成字体 cmap。</div>
    <textarea id="previewText">{escape(default_text)}</textarea>
    <div style="margin-top:10px;">
      <label><input type="checkbox" id="verticalMode"> 竖排预览</label>
      <button type="button" onclick="clearText()">清空文字</button>
      <button type="button" onclick="resetText()">恢复默认文字</button>
    </div>
    <div id="charGrid" class="char-grid"></div>
  </div>

  <div class="panel">
    <h2>2. TTF 字体家族预览</h2>
    <div class="small">这里真实加载生成出来的 TTF 文件。点击哪个字体，下面就用哪个字体预览。</div>
    <div id="fontList" class="font-list"></div>
    <div id="fontPreview" class="preview">{escape(default_text)}</div>
  </div>

  <div class="panel">
    <h2>3. SVG 变化预览矩阵</h2>
    <div class="small">每张 SVG 都由服务器读取对应 TTF 的真实 glyph 轮廓动态生成。</div>
    <div id="svgMatrix" class="matrix"></div>
  </div>

</div>

<script>
const JOB_ID = {json.dumps(safe_name(job_id), ensure_ascii=False)};
const FONT_ITEMS = {fonts_json};
const CHAR_ITEMS = {chars_json};
const DEFAULT_TEXT = {json.dumps(default_text, ensure_ascii=False)};

let currentFontIndex = 0;

const previewText = document.getElementById("previewText");
const fontList = document.getElementById("fontList");
const fontPreview = document.getElementById("fontPreview");
const svgMatrix = document.getElementById("svgMatrix");
const charGrid = document.getElementById("charGrid");
const verticalMode = document.getElementById("verticalMode");

function currentText(){{
  return previewText.value || DEFAULT_TEXT;
}}

function renderChars(){{
  charGrid.innerHTML = "";

  CHAR_ITEMS.forEach(item => {{
    const b = document.createElement("button");
    b.className = "char-btn";
    b.innerHTML = "<span class='ch'>" + item.ch + "</span><span class='code'>" + item.code + "</span>";
    b.onclick = () => {{
      previewText.value += item.ch;
      updateAll();
    }};
    charGrid.appendChild(b);
  }});
}}

function renderFontList(){{
  fontList.innerHTML = "";

  FONT_ITEMS.forEach((item, idx) => {{
    const b = document.createElement("button");
    b.className = "font-btn";
    b.textContent = String(idx + 1).padStart(2, "0") + " ｜ " + item.name;
    b.onclick = () => setFont(idx);
    fontList.appendChild(b);
  }});
}}

function setFont(idx){{
  currentFontIndex = idx;
  const item = FONT_ITEMS[idx];

  fontPreview.style.fontFamily = item.face + ", sans-serif";
  fontPreview.textContent = currentText();

  Array.from(document.querySelectorAll(".font-btn")).forEach((b, i) => {{
    b.classList.toggle("active", i === idx);
  }});

  updateVertical();
}}

function renderMatrix(){{
  svgMatrix.innerHTML = "";

  FONT_ITEMS.forEach((item, idx) => {{
    const card = document.createElement("div");
    card.className = "card";

    const title = document.createElement("div");
    title.className = "card-title";
    title.textContent = String(idx + 1).padStart(2, "0") + " ｜ " + item.name;

    const img = document.createElement("img");
    img.src = "/api/unicode/real_preview_svg/" + encodeURIComponent(JOB_ID) + "/" + idx +
      "?text=" + encodeURIComponent(currentText()) +
      "&vertical=" + (verticalMode.checked ? "1" : "0") +
      "&t=" + Date.now();

    card.appendChild(title);
    card.appendChild(img);
    svgMatrix.appendChild(card);
  }});
}}

function updateVertical(){{
  if(verticalMode.checked){{
    fontPreview.classList.add("vertical");
  }}else{{
    fontPreview.classList.remove("vertical");
  }}
}}

function updateAll(){{
  fontPreview.textContent = currentText();
  updateVertical();
  renderMatrix();
}}

function clearText(){{
  previewText.value = "";
  updateAll();
}}

function resetText(){{
  previewText.value = DEFAULT_TEXT;
  updateAll();
}}

previewText.addEventListener("input", updateAll);
verticalMode.addEventListener("change", updateAll);

renderChars();
renderFontList();
setFont(0);
renderMatrix();
</script>

</body>
</html>'''
        return HTMLResponse(html)

    @app.middleware("http")
    async def add_preview_link_middleware(request, call_next):
        response = await call_next(request)

        path = request.url.path
        content_type = response.headers.get("content-type", "")

        if path not in ["/", "/unicode", "/unicode_async"]:
            return response

        if "text/html" not in content_type:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        text = body.decode("utf-8", errors="ignore")

        if "add-real-result-preview-link-v1" not in text:
            if "</body>" in text:
                text = text.replace("</body>", FRONTEND_SCRIPT + "\n</body>", 1)
            else:
                text += FRONTEND_SCRIPT

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )
