from pathlib import Path
import zipfile
from xml.sax.saxutils import escape

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen


BASE_DIR = Path(__file__).resolve().parent
JOB_DIR = BASE_DIR / "runtime_jobs"


def get_cmap(font):
    cmap = {}
    for table in font["cmap"].tables:
        cmap.update(table.cmap)
    return cmap


def safe_name(name: str):
    keep = []
    for ch in name:
        if ch.isalnum() or ch in "._-":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:120] or "file"


def job_paths(job_id: str):
    job_id = safe_name(job_id)
    job_path = JOB_DIR / job_id
    out_dir = job_path / "outputs"
    preview_dir = job_path / "svg_previews"
    return job_path, out_dir, preview_dir


def create_zip_for_job(job_id: str):
    job_path, out_dir, _ = job_paths(job_id)

    if not out_dir.exists():
        return None

    font_files = sorted(out_dir.glob("*.ttf")) + sorted(out_dir.glob("*.otf"))

    if not font_files:
        return None

    zip_path = job_path / "fonts_package.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in font_files:
            zf.write(p, arcname=p.name)

    return zip_path


def read_preview_chars(job_path: Path):
    chars_file = job_path / "chars.txt"

    if chars_file.exists():
        text = chars_file.read_text(encoding="utf-8", errors="ignore")
        text = "".join(ch for ch in text if not ch.isspace())
        text = "".join(dict.fromkeys(text))
        if text:
            return text[:12]

    return "ABCDEabcde123"


def generate_svg_previews(job_id: str):
    job_path, out_dir, preview_dir = job_paths(job_id)

    if not out_dir.exists():
        return []

    preview_dir.mkdir(parents=True, exist_ok=True)

    # 如果已经生成过 SVG，直接复用
    existed = sorted(preview_dir.glob("*.svg"))
    if existed:
        return [p.name for p in existed]

    preview_chars = read_preview_chars(job_path)
    font_files = sorted(out_dir.glob("*.ttf")) + sorted(out_dir.glob("*.otf"))
    result = []

    for font_path in font_files:
        try:
            font = TTFont(str(font_path))
            cmap = get_cmap(font)
            glyph_set = font.getGlyphSet()

            upm = int(font["head"].unitsPerEm)
            hhea = font["hhea"]
            ascent = int(hhea.ascent)
            descent = int(hhea.descent)

            hmtx = font["hmtx"].metrics

            baseline = 90 + ascent
            height = max(620, ascent - descent + 180)
            x = 80

            paths = []

            for ch in preview_chars:
                cp = ord(ch)
                gname = cmap.get(cp)

                if not gname or gname not in glyph_set:
                    continue

                pen = SVGPathPen(glyph_set)

                try:
                    glyph_set[gname].draw(pen)
                    d = pen.getCommands()
                except Exception:
                    continue

                aw = hmtx.get(gname, (upm, 0))[0]
                aw = max(int(aw), int(upm * 0.45))

                if d:
                    paths.append(
                        f'<path d="{escape(d)}" '
                        f'transform="translate({x},{baseline}) scale(1,-1)" '
                        f'fill="#111111"/>'
                    )

                x += aw + 80

            if not paths:
                font.close()
                continue

            width = max(900, x + 80)

            svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="24" y="34" font-size="22" fill="#333333">{escape(font_path.name)}</text>
  <line x1="60" y1="{baseline}" x2="{width - 60}" y2="{baseline}" stroke="#dddddd" stroke-width="1"/>
  {chr(10).join(paths)}
</svg>
'''

            out_name = safe_name(font_path.stem) + ".svg"
            out_path = preview_dir / out_name
            out_path.write_text(svg, encoding="utf-8")

            result.append(out_name)
            font.close()

        except Exception as e:
            print("[WARN] SVG preview failed:", font_path, e)

    return result


FRONTEND_PATCH = r'''
<script id="unicode-zip-preview-buttons-v1">
(function(){
  if(window.__UNICODE_ZIP_PREVIEW_BUTTONS_V1__) return;
  window.__UNICODE_ZIP_PREVIEW_BUTTONS_V1__ = true;

  function extractJobId(){
    const links = Array.from(document.querySelectorAll("a[href]"));

    for(const a of links){
      const href = a.getAttribute("href") || "";
      let m = href.match(/\/api\/unicode\/download\/([^\/?#]+)\//);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/download_zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);
    }

    return "";
  }

  function findOutputContainer(){
    const firstFontLink = document.querySelector('a[href*="/api/unicode/download/"]');
    if(firstFontLink){
      return firstFontLink.parentElement || firstFontLink.closest("div") || document.body;
    }

    return (
      document.getElementById("outputs") ||
      document.getElementById("unicodeBridgeOutputs") ||
      document.querySelector(".outputs") ||
      document.body
    );
  }

  function hideSingleFontLinks(){
    const links = Array.from(document.querySelectorAll('a[href*="/api/unicode/download/"]'));

    links.forEach(a => {
      const href = a.getAttribute("href") || "";
      if(/\.(ttf|otf)(\?|#|$)/i.test(href) || /\.(ttf|otf)$/i.test(a.textContent.trim())){
        a.style.display = "none";
      }
    });
  }

  function installButtons(){
    const jobId = extractJobId();
    if(!jobId) return;

    hideSingleFontLinks();

    if(document.getElementById("unicodeZipPreviewActionBar")) return;

    const container = findOutputContainer();

    const bar = document.createElement("div");
    bar.id = "unicodeZipPreviewActionBar";
    bar.style.cssText = [
      "margin-top:12px",
      "margin-bottom:12px",
      "padding:12px",
      "border:1px solid #dbeafe",
      "background:#eff6ff",
      "border-radius:10px",
      "display:flex",
      "gap:10px",
      "flex-wrap:wrap",
      "align-items:center"
    ].join(";");

    const zip = document.createElement("a");
    zip.href = "/api/unicode/zip/" + encodeURIComponent(jobId);
    zip.textContent = "下载全部 TTF 字体压缩包";
    zip.target = "_blank";
    zip.style.cssText = [
      "display:inline-block",
      "padding:10px 14px",
      "background:#2563eb",
      "color:#fff",
      "border-radius:8px",
      "text-decoration:none",
      "font-weight:700"
    ].join(";");

    const preview = document.createElement("a");
    preview.href = "/api/unicode/svg_preview_page/" + encodeURIComponent(jobId);
    preview.textContent = "预览 SVG 字体差值";
    preview.target = "_blank";
    preview.style.cssText = [
      "display:inline-block",
      "padding:10px 14px",
      "background:#111827",
      "color:#fff",
      "border-radius:8px",
      "text-decoration:none",
      "font-weight:700"
    ].join(";");

    const note = document.createElement("div");
    note.textContent = "单个 TTF 链接已隐藏，避免页面杂乱。";
    note.style.cssText = "font-size:12px;color:#555;width:100%;";

    bar.appendChild(zip);
    bar.appendChild(preview);
    bar.appendChild(note);

    container.insertBefore(bar, container.firstChild);
  }

  setInterval(installButtons, 1000);

  document.addEventListener("DOMContentLoaded", function(){
    installButtons();

    const mo = new MutationObserver(function(){
      installButtons();
    });

    mo.observe(document.body, {
      childList:true,
      subtree:true
    });
  });
})();
</script>
'''


def install_zip_preview_patch(app):
    if getattr(app.state, "_unicode_zip_preview_buttons_v1", False):
        return

    app.state._unicode_zip_preview_buttons_v1 = True

    @app.get("/api/unicode/zip/{job_id}")
    def unicode_zip_download(job_id: str):
        zip_path = create_zip_for_job(job_id)

        if not zip_path or not zip_path.exists():
            return JSONResponse({"error": "没有找到可打包的 TTF/OTF 字体文件。"}, status_code=404)

        return FileResponse(
            str(zip_path),
            filename=f"unicode_font_morph_{safe_name(job_id)}.zip",
            media_type="application/zip",
        )

    @app.get("/api/unicode/svg_preview_file/{job_id}/{filename}")
    def unicode_svg_preview_file(job_id: str, filename: str):
        _, _, preview_dir = job_paths(job_id)
        target = preview_dir / Path(filename).name

        if not target.exists():
            return JSONResponse({"error": "SVG 文件不存在。"}, status_code=404)

        return FileResponse(str(target), media_type="image/svg+xml", filename=target.name)

    @app.get("/api/unicode/svg_preview_page/{job_id}")
    def unicode_svg_preview_page(job_id: str):
        names = generate_svg_previews(job_id)

        if not names:
            return HTMLResponse(
                "<h2>没有生成 SVG 预览</h2><p>请确认该任务已经生成 TTF 字体文件。</p>",
                status_code=404,
            )

        cards = []

        for idx, name in enumerate(names, 1):
            url = f"/api/unicode/svg_preview_file/{safe_name(job_id)}/{name}"
            cards.append(f'''
<div class="card">
  <div class="title">SVG 预览 {idx:02d} ｜ {escape(name)}</div>
  <img src="{url}" />
  <div><a href="{url}" target="_blank">下载 SVG</a></div>
</div>
''')

        html = f'''
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>SVG 字体差值预览</title>
<style>
body {{
  font-family: Arial, "Microsoft YaHei", sans-serif;
  margin: 0;
  background: #f3f4f6;
  color: #111;
}}
.header {{
  padding: 22px 32px;
  background: #111827;
  color: white;
}}
.grid {{
  padding: 24px 32px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 18px;
}}
.card {{
  background: white;
  border-radius: 12px;
  padding: 14px;
  box-shadow: 0 6px 22px rgba(0,0,0,.08);
}}
.title {{
  font-weight: 700;
  margin-bottom: 10px;
  font-size: 14px;
}}
img {{
  width: 100%;
  height: auto;
  border: 1px solid #e5e7eb;
  background: white;
}}
a {{
  display: inline-block;
  margin-top: 10px;
  color: #2563eb;
  text-decoration: none;
  font-weight: 700;
}}
</style>
</head>
<body>
<div class="header">
  <h1>SVG 字体差值预览</h1>
  <p>任务 ID：{escape(safe_name(job_id))} ｜ 共 {len(names)} 个 SVG 预览</p>
</div>
<div class="grid">
{''.join(cards)}
</div>
</body>
</html>
'''
        return HTMLResponse(html)

    @app.middleware("http")
    async def unicode_zip_preview_button_middleware(request, call_next):
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

        if "unicode-zip-preview-buttons-v1" not in text:
            if "</body>" in text:
                text = text.replace("</body>", FRONTEND_PATCH + "\n</body>", 1)
            else:
                text += FRONTEND_PATCH

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )
