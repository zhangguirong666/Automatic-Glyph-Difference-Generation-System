from pathlib import Path
import re
import json
import zipfile
import traceback
from html import escape
from urllib.parse import quote

from fastapi import Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_JOBS = BASE_DIR / "runtime_jobs"
OUTPUT_DIR = BASE_DIR / "output"
ASSET_DIR = BASE_DIR / "runtime_real_preview_center_assets"
ASSET_DIR.mkdir(exist_ok=True)


def safe_name(s: str):
    s = str(s)
    out = []
    for ch in s:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:160] or "item"


def natural_key(p: Path):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", p.name)]


def list_fonts(d: Path):
    if not d or not d.exists():
        return []
    return sorted(list(d.glob("*.ttf")) + list(d.glob("*.otf")), key=natural_key)


def latest_font_dir(root: Path):
    if not root.exists():
        return None

    candidates = []
    for d in [root] + [x for x in root.rglob("*") if x.is_dir()]:
        fonts = list_fonts(d)
        if len(fonts) >= 2:
            latest = max(p.stat().st_mtime for p in fonts)
            candidates.append((latest, d))

    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda x: x[0])
    return candidates[0][1]


def resolve_output_dir(scope: str, job_id: str):
    scope = safe_name(scope)
    job_id = safe_name(job_id)

    if scope == "unicode":
        d = RUNTIME_JOBS / job_id / "outputs"
        if list_fonts(d):
            return d
        return None

    if scope == "foundry":
        d = OUTPUT_DIR / "mongolian_gb_ttf_steps"
        if list_fonts(d):
            return d
        return latest_font_dir(OUTPUT_DIR)

    return None


def asset_dir(scope: str, job_id: str):
    d = ASSET_DIR / safe_name(scope) / safe_name(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_cmap(font):
    cmap = {}
    for table in font["cmap"].tables:
        cmap.update(table.cmap)
    return cmap


def read_chars_from_generated(scope: str, job_id: str, fonts):
    chars = []
    seen = set()

    if scope == "unicode":
        chars_file = RUNTIME_JOBS / safe_name(job_id) / "chars.txt"
        if chars_file.exists():
            text = chars_file.read_text(encoding="utf-8", errors="ignore")
            for ch in text:
                if ch in seen:
                    continue
                seen.add(ch)
                cp = ord(ch)
                if ch.strip() == "" and cp != 0x20:
                    continue
                chars.append({"ch": ch, "code": f"U+{cp:04X}"})

    if chars:
        return chars[:800]

    if not fonts:
        return []

    try:
        font = TTFont(str(fonts[0]), lazy=True)
        cps = set()
        for table in font["cmap"].tables:
            cps.update(table.cmap.keys())
        font.close()

        # 如果是蒙古文，优先显示蒙古文 Unicode 区块
        mongolian = [cp for cp in sorted(cps) if 0x1800 <= cp <= 0x18AF]
        if mongolian:
            cps = mongolian
        else:
            cps = [cp for cp in sorted(cps) if cp >= 0x20 and cp not in [0xFEFF]]

        for cp in cps[:800]:
            try:
                ch = chr(cp)
            except Exception:
                continue
            if ch.strip() == "" and cp != 0x20:
                continue
            chars.append({"ch": ch, "code": f"U+{cp:04X}"})

    except Exception:
        pass

    return chars


def family_name_from_fonts(fonts):
    if not fonts:
        return "GeneratedFontFamily"

    stem = fonts[0].stem

    for token in ["_Morph", "-Morph", "_Weight", "-Weight"]:
        if token in stem:
            return stem.split(token)[0]

    return re.sub(r"[_-]?\d+$", "", stem) or "GeneratedFontFamily"


def make_zip(scope: str, job_id: str):
    out_dir = resolve_output_dir(scope, job_id)

    if not out_dir:
        return None, "没有找到已生成的字体输出目录。"

    fonts = list_fonts(out_dir)

    if not fonts:
        return None, "输出目录中没有 TTF/OTF 字体文件。"

    zpath = asset_dir(scope, job_id) / f"{safe_name(scope)}_{safe_name(job_id)}_all_ttf.zip"

    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in fonts:
            zf.write(p, arcname=p.name)

    return zpath, ""


def try_build_variable_font(scope: str, job_id: str):
    """
    真实 Variable Font 合成：
    直接读取已经生成的 TTF 文件作为 masters，调用 fontTools.varLib。
    如果轮廓点结构不兼容，会返回真实错误，不伪造成功。
    """
    out_dir = resolve_output_dir(scope, job_id)

    if not out_dir:
        return None, "没有找到已生成的字体输出目录。"

    fonts = list_fonts(out_dir)

    if len(fonts) < 2:
        return None, "至少需要 2 个 TTF/OTF 才能合成 Variable Font。"

    adir = asset_dir(scope, job_id)
    vf_path = adir / f"{safe_name(scope)}_{safe_name(job_id)}_Variable.ttf"
    ds_path = adir / f"{safe_name(scope)}_{safe_name(job_id)}.designspace"
    err_path = adir / "variable_font_error.txt"

    if vf_path.exists():
        return vf_path, ""

    try:
        from fontTools.designspaceLib import DesignSpaceDocument, AxisDescriptor, SourceDescriptor
        from fontTools.varLib import build as varlib_build

        base = TTFont(str(fonts[0]))
        base_order = base.getGlyphOrder()
        base.close()

        for p in fonts[1:]:
            f = TTFont(str(p))
            order = f.getGlyphOrder()
            f.close()

            if order != base_order:
                raise RuntimeError(f"glyphOrder 不一致，无法合成 Variable Font：{p.name}")

        fam = family_name_from_fonts(fonts)

        doc = DesignSpaceDocument()

        axis = AxisDescriptor()
        axis.name = "Weight"
        axis.tag = "wght"
        axis.minimum = 100
        axis.default = 100
        axis.maximum = 900
        doc.addAxis(axis)

        n = len(fonts)

        for i, p in enumerate(fonts):
            value = 100 if n == 1 else int(round(100 + i * 800 / (n - 1)))

            src = SourceDescriptor()
            src.path = str(p.resolve())
            src.name = f"master_{i:02d}"
            src.familyName = fam
            src.styleName = f"Weight {value}"
            src.location = {"Weight": value}

            if i == 0:
                src.copyInfo = True
                src.copyFeatures = True
                src.copyLib = True
                src.copyGroups = True

            doc.addSource(src)

        doc.write(str(ds_path))

        built = varlib_build(str(ds_path))
        vf = built[0] if isinstance(built, tuple) else built
        vf.save(str(vf_path))

        if err_path.exists():
            err_path.unlink()

        return vf_path, ""

    except Exception:
        err = traceback.format_exc()
        err_path.write_text(err, encoding="utf-8")
        return None, err


def glyph_svg_line(font_path: Path, text: str, vertical: bool = False):
    """
    真实 SVG 预览：
    直接读取 TTF glyphSet，用 SVGPathPen 提取真实轮廓。
    """
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

            for ch in text[:60]:
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
                        f'<path d="{escape(d)}" transform="translate({x},{y}) scale(1,-1)" fill="#111111"/>'
                    )

                y += int(upm * 0.9)

        else:
            x = 70
            baseline = 90 + ascent
            height = max(620, ascent - descent + 180)

            for ch in text[:80]:
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
                        f'<path d="{escape(d)}" transform="translate({x},{baseline}) scale(1,-1)" fill="#111111"/>'
                    )

                x += aw + 70

            width = max(900, x + 80)

        font.close()

        if not paths:
            paths.append('<text x="40" y="120" font-size="32" fill="#666">该字体未包含这些字符</text>')

        svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="24" y="34" font-size="22" fill="#333333">{escape(font_path.name)}</text>
  {chr(10).join(paths)}
</svg>
'''
        return svg

    except Exception as e:
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="900" height="300" viewBox="0 0 900 300">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="40" y="100" font-size="26" fill="#c00">SVG 生成失败：{escape(str(e))}</text>
</svg>
'''


FRONTEND_ENTRY = r'''
<script id="real-preview-center-entry-v1">
(function(){
  if(window.__REAL_PREVIEW_CENTER_ENTRY_V1__) return;
  window.__REAL_PREVIEW_CENTER_ENTRY_V1__ = true;

  function btn(text, href, bg){
    const a = document.createElement("a");
    a.href = href;
    a.target = "_blank";
    a.textContent = text;
    a.style.cssText = [
      "display:inline-block",
      "padding:10px 14px",
      "background:" + bg,
      "color:#fff",
      "border-radius:8px",
      "text-decoration:none",
      "font-weight:700",
      "margin:6px 8px 6px 0"
    ].join(";");
    return a;
  }

  function extractUnicodeJobId(){
    const links = Array.from(document.querySelectorAll("a[href]"));

    for(const a of links){
      const href = a.getAttribute("href") || "";

      let m = href.match(/\/api\/unicode\/download\/([^\/?#]+)\//);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/download_zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/generated_workspace\/zip\/unicode\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);
    }

    return "";
  }

  function installUnicodeEntry(){
    const jobId = extractUnicodeJobId();
    if(!jobId) return;

    if(document.getElementById("realPreviewCenterUnicodeEntry")) return;

    const host =
      document.getElementById("unicodeZipPreviewActionBar") ||
      document.getElementById("outputs") ||
      document.getElementById("unicodeBridgeOutputs") ||
      document.querySelector(".outputs") ||
      document.body;

    const box = document.createElement("div");
    box.id = "realPreviewCenterUnicodeEntry";
    box.style.cssText = "margin-top:12px;padding:14px;border:1px solid #bfdbfe;background:#eff6ff;border-radius:10px;";

    const title = document.createElement("div");
    title.textContent = "上半部分：真实生成结果预览中心";
    title.style.cssText = "font-weight:800;margin-bottom:6px;font-size:16px;";

    const desc = document.createElement("div");
    desc.textContent = "读取本次已经生成的 TTF / SVG，用于矩阵预览、字体家族滑杆、真实 Variable Font 尝试合成。";
    desc.style.cssText = "font-size:12px;color:#555;margin-bottom:8px;";

    box.appendChild(title);
    box.appendChild(desc);
    box.appendChild(btn("打开真实预览中心", "/api/real_preview/page/unicode/" + encodeURIComponent(jobId), "#7c3aed"));
    box.appendChild(btn("下载全部 TTF 压缩包", "/api/real_preview/zip/unicode/" + encodeURIComponent(jobId), "#2563eb"));
    box.appendChild(btn("下载真实 Variable Font", "/api/real_preview/vf/unicode/" + encodeURIComponent(jobId), "#0f766e"));

    host.appendChild(box);
  }

  function installFoundryEntry(){
    if(document.getElementById("realPreviewCenterFoundryEntry")) return;

    const foundryBtn =
      document.getElementById("foundryRulesBuildBtn") ||
      Array.from(document.querySelectorAll("button")).find(b => (b.innerText || "").includes("按所选字体公司规则生成"));

    if(!foundryBtn) return;

    const host = foundryBtn.closest("div") || foundryBtn.parentElement || document.body;

    const box = document.createElement("div");
    box.id = "realPreviewCenterFoundryEntry";
    box.style.cssText = "margin-top:12px;padding:14px;border:1px solid #bfdbfe;background:#eff6ff;border-radius:10px;";

    const title = document.createElement("div");
    title.textContent = "下半部分：蒙古文真实生成结果预览中心";
    title.style.cssText = "font-weight:800;margin-bottom:6px;font-size:16px;";

    const desc = document.createElement("div");
    desc.textContent = "先完成蒙古文字体公司规则生成，再打开这里。它读取 output/mongolian_gb_ttf_steps/ 里的真实 TTF。";
    desc.style.cssText = "font-size:12px;color:#555;margin-bottom:8px;";

    box.appendChild(title);
    box.appendChild(desc);
    box.appendChild(btn("打开蒙古文真实预览中心", "/api/real_preview/page/foundry/latest", "#7c3aed"));
    box.appendChild(btn("下载蒙古文全部 TTF 压缩包", "/api/real_preview/zip/foundry/latest", "#2563eb"));
    box.appendChild(btn("下载蒙古文真实 Variable Font", "/api/real_preview/vf/foundry/latest", "#0f766e"));

    host.appendChild(box);
  }

  function run(){
    installUnicodeEntry();
    installFoundryEntry();
  }

  setInterval(run, 1000);
  document.addEventListener("DOMContentLoaded", function(){
    run();
    const mo = new MutationObserver(run);
    mo.observe(document.body, {childList:true, subtree:true});
  });
})();
</script>
'''


def install_real_preview_center(app):
    if getattr(app.state, "_real_preview_center_v1", False):
        return

    app.state._real_preview_center_v1 = True

    @app.get("/api/real_preview/zip/{scope}/{job_id}")
    def api_zip(scope: str, job_id: str):
        zpath, err = make_zip(scope, job_id)

        if not zpath or not zpath.exists():
            return HTMLResponse(f"<h2>压缩包生成失败</h2><pre>{escape(err)}</pre>", status_code=404)

        return FileResponse(
            str(zpath),
            filename=f"{safe_name(scope)}_{safe_name(job_id)}_all_ttf.zip",
            media_type="application/zip"
        )

    @app.get("/api/real_preview/vf/{scope}/{job_id}")
    def api_vf(scope: str, job_id: str):
        vf_path, err = try_build_variable_font(scope, job_id)

        if not vf_path or not vf_path.exists():
            return HTMLResponse(
                "<h2>真实 Variable Font 合成失败</h2>"
                "<p>系统已经读取生成出来的 TTF 并调用 fontTools.varLib。失败说明当前字体 master 不满足 VF 兼容条件，不是演示。</p>"
                "<p>字体家族滑杆仍然可用，因为它直接加载每一个真实 TTF。</p>"
                f"<pre style='white-space:pre-wrap;background:#111827;color:#ddd;padding:12px;border-radius:8px;'>{escape((err or '')[-8000:])}</pre>",
                status_code=500
            )

        return FileResponse(
            str(vf_path),
            filename=f"{safe_name(scope)}_{safe_name(job_id)}_Variable.ttf",
            media_type="font/ttf"
        )

    @app.get("/api/real_preview/font/{scope}/{job_id}/{filename}")
    def api_font(scope: str, job_id: str, filename: str):
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return JSONResponse({"error": "没有找到输出目录。"}, status_code=404)

        target = out_dir / Path(filename).name

        if not target.exists():
            return JSONResponse({"error": "字体文件不存在。"}, status_code=404)

        return FileResponse(str(target), filename=target.name, media_type="font/ttf")

    @app.get("/api/real_preview/svg_line/{scope}/{job_id}/{index}")
    def api_svg_line(
        scope: str,
        job_id: str,
        index: int,
        text: str = Query("ABCDEabcde123"),
        vertical: int = Query(0),
    ):
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return Response("<svg></svg>", media_type="image/svg+xml", status_code=404)

        fonts = list_fonts(out_dir)

        if not fonts:
            return Response("<svg></svg>", media_type="image/svg+xml", status_code=404)

        index = max(0, min(len(fonts) - 1, int(index)))

        svg = glyph_svg_line(fonts[index], text, vertical=bool(vertical))

        return Response(svg, media_type="image/svg+xml")

    @app.get("/api/real_preview/page/{scope}/{job_id}")
    def api_preview_page(scope: str, job_id: str):
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return HTMLResponse(
                "<h2>没有找到真实字体输出目录</h2>"
                "<p>上半部分请先完成生成；下半部分请先完成蒙古文公司规则生成。</p>",
                status_code=404
            )

        fonts = list_fonts(out_dir)

        if not fonts:
            return HTMLResponse("<h2>输出目录里没有 TTF / OTF 文件。</h2>", status_code=404)

        chars = read_chars_from_generated(scope, job_id, fonts)
        default_text = "".join(x["ch"] for x in chars[:12]) or "ABCDEabcde123"

        if scope == "foundry":
            default_text = "".join(x["ch"] for x in chars[:20]) or "ᠮᠣᠩᠭᠣᠯ"

        family_name = family_name_from_fonts(fonts)

        vf_path, vf_err = try_build_variable_font(scope, job_id)
        has_vf = bool(vf_path and vf_path.exists())

        faces = []
        font_items = []

        for i, p in enumerate(fonts):
            face = f"PreviewStep{i+1:02d}"
            url = f"/api/real_preview/font/{safe_name(scope)}/{safe_name(job_id)}/{quote(p.name)}"

            faces.append(
                f"@font-face{{font-family:'{face}';src:url('{url}') format('truetype');font-weight:400;font-style:normal;}}"
            )

            font_items.append({
                "index": i,
                "face": face,
                "name": p.name,
                "url": url,
            })

        vf_face = ""
        if has_vf:
            vf_face = (
                "@font-face{"
                "font-family:'RealPreviewVF';"
                f"src:url('/api/real_preview/vf/{safe_name(scope)}/{safe_name(job_id)}') format('truetype');"
                "font-weight:100 900;"
                "font-style:normal;"
                "}"
            )

        vf_warning = ""
        if not has_vf:
            vf_warning = f"""
<div class="warn">
  <b>Variable Font 未合成成功。</b>
  <div>系统已经真实读取生成 TTF 并调用 fontTools.varLib。失败不影响矩阵预览和字体家族滑杆。</div>
  <details>
    <summary>查看真实 VF 合成错误</summary>
    <pre>{escape((vf_err or '')[-8000:])}</pre>
  </details>
</div>
"""

        try:
            rel_out = str(out_dir.relative_to(BASE_DIR))
        except Exception:
            rel_out = str(out_dir)

        fonts_json = json.dumps(font_items, ensure_ascii=False)
        chars_json = json.dumps(chars, ensure_ascii=False)

        html = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>真实生成结果预览中心</title>
<style>
{vf_face}
{chr(10).join(faces)}

body {{
  margin:0;
  background:#f2f4f8;
  color:#111;
  font-family:Arial,"Microsoft YaHei",sans-serif;
}}

.header {{
  background:#101827;
  color:white;
  padding:22px 34px;
}}

.header h1 {{
  margin:0 0 6px 0;
}}

.layout {{
  max-width:1500px;
  margin:22px auto;
  padding:0 22px;
  display:grid;
  grid-template-columns:320px 1fr;
  gap:18px;
}}

.sidebar, .main {{
  background:white;
  border-radius:16px;
  box-shadow:0 8px 28px rgba(0,0,0,.08);
}}

.sidebar {{
  padding:16px;
  max-height:calc(100vh - 130px);
  overflow:auto;
}}

.main {{
  padding:22px;
}}

.section {{
  margin-top:18px;
  padding:18px;
  border:1px solid #e5e7eb;
  border-radius:14px;
  background:#fff;
}}

.section h2 {{
  margin-top:0;
}}

.font-btn {{
  width:100%;
  text-align:left;
  margin:4px 0;
  padding:10px;
  border:1px solid #e5e7eb;
  background:#f9fafb;
  border-radius:9px;
  cursor:pointer;
  font-size:13px;
}}

.font-btn.active {{
  background:#2563eb;
  color:white;
  border-color:#2563eb;
}}

.actions a {{
  display:inline-block;
  margin:6px 8px 6px 0;
  padding:10px 14px;
  color:white;
  border-radius:8px;
  text-decoration:none;
  font-weight:700;
}}

.blue {{ background:#2563eb; }}
.green {{ background:#0f766e; }}
.black {{ background:#111827; }}
.purple {{ background:#7c3aed; }}

textarea {{
  width:100%;
  min-height:76px;
  box-sizing:border-box;
  padding:12px;
  font-size:16px;
  border:1px solid #d1d5db;
  border-radius:8px;
}}

input[type=range] {{
  width:100%;
}}

.preview {{
  margin-top:14px;
  min-height:180px;
  padding:26px;
  border:1px solid #e5e7eb;
  border-radius:14px;
  background:#fafafa;
  font-size:76px;
  line-height:1.25;
  overflow:auto;
}}

.preview.vertical {{
  writing-mode:vertical-lr;
  text-orientation:mixed;
  min-height:420px;
}}

.matrix {{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(260px,1fr));
  gap:14px;
  margin-top:14px;
}}

.card {{
  border:1px solid #e5e7eb;
  border-radius:12px;
  padding:12px;
  background:#f9fafb;
}}

.card-title {{
  font-weight:700;
  font-size:13px;
  margin-bottom:8px;
}}

.card img {{
  width:100%;
  height:auto;
  border:1px solid #e5e7eb;
  background:white;
}}

.char-grid {{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(74px,1fr));
  gap:8px;
  max-height:240px;
  overflow:auto;
  margin-top:10px;
}}

.char-btn {{
  border:1px solid #e5e7eb;
  background:#fff;
  border-radius:9px;
  padding:7px 4px;
  cursor:pointer;
  text-align:center;
}}

.char-btn .ch {{
  display:block;
  font-size:28px;
}}

.char-btn .code {{
  display:block;
  font-size:11px;
  color:#666;
}}

.warn {{
  margin-top:14px;
  padding:12px;
  background:#fff7ed;
  border:1px solid #fed7aa;
  color:#7c2d12;
  border-radius:10px;
}}

pre {{
  white-space:pre-wrap;
  max-height:260px;
  overflow:auto;
  background:#111827;
  color:#ddd;
  padding:12px;
  border-radius:8px;
}}

.small {{
  color:#555;
  font-size:13px;
}}

.row {{
  display:flex;
  gap:12px;
  flex-wrap:wrap;
  align-items:center;
}}
</style>
</head>
<body>

<div class="header">
  <h1>真实生成结果预览中心</h1>
  <div>范围：{escape(scope)} ｜ 任务：{escape(job_id)} ｜ 字体家族：{escape(family_name)} ｜ TTF数量：{len(fonts)} ｜ 输出目录：{escape(rel_out)}</div>
</div>

<div class="layout">
  <aside class="sidebar">
    <h2>字体列表</h2>
    <div class="small">读取第一步已经生成出来的真实 TTF。选择哪个，右侧就预览哪个。</div>
    <div id="fontList"></div>
  </aside>

  <main class="main">

    <div class="actions">
      <a class="blue" href="/api/real_preview/zip/{escape(safe_name(scope))}/{escape(safe_name(job_id))}" target="_blank">下载全部 TTF 压缩包</a>
      <a class="green" href="/api/real_preview/vf/{escape(safe_name(scope))}/{escape(safe_name(job_id))}" target="_blank">下载真实 Variable Font</a>
    </div>

    {vf_warning}

    <div class="section">
      <h2>1. 预览文字与生成字符列表</h2>
      <div class="small">字符来自本次生成的 chars.txt 或字体 cmap，不是写死模板。</div>
      <textarea id="previewText">{escape(default_text)}</textarea>
      <div class="row" style="margin-top:10px;">
        <label><input type="checkbox" id="verticalMode"> 竖排预览</label>
        <button type="button" onclick="clearText()">清空文字</button>
        <button type="button" onclick="resetText()">恢复默认文字</button>
      </div>
      <div id="charGrid" class="char-grid"></div>
    </div>

    <div class="section">
      <h2>2. TTF 字体家族准确预览</h2>
      <div class="small">这里通过 @font-face 真实加载每个生成出来的 TTF 文件。</div>
      <label>当前字体：<span id="currentFontName"></span></label>
      <input id="familySlider" type="range" min="0" max="{len(fonts)-1}" value="0" step="1">
      <div id="familyPreview" class="preview">{escape(default_text)}</div>
    </div>

    <div class="section">
      <h2>3. 字体插值 SVG 预览矩阵</h2>
      <div class="small">每张 SVG 都由服务器读取对应 TTF 的真实 glyph 轮廓后生成。</div>
      <div id="svgMatrix" class="matrix"></div>
    </div>

    <div class="section">
      <h2>4. Variable Font 实时滑杆</h2>
      <div class="small">如果真实 VF 合成成功，这里使用 font-variation-settings:'wght' 连续调节；如果失败，会显示真实错误。</div>
      <label>Weight：<span id="vfValue">100</span></label>
      <input id="vfSlider" type="range" min="100" max="900" value="100" step="1">
      <div id="vfPreview" class="preview">{escape(default_text)}</div>
    </div>

  </main>
</div>

<script>
const SCOPE = {json.dumps(scope, ensure_ascii=False)};
const JOB_ID = {json.dumps(job_id, ensure_ascii=False)};
const FONT_ITEMS = {fonts_json};
const CHAR_ITEMS = {chars_json};
const HAS_VF = {str(has_vf).lower()};
const DEFAULT_TEXT = {json.dumps(default_text, ensure_ascii=False)};

let currentIndex = 0;

const fontList = document.getElementById("fontList");
const previewText = document.getElementById("previewText");
const familySlider = document.getElementById("familySlider");
const familyPreview = document.getElementById("familyPreview");
const currentFontName = document.getElementById("currentFontName");
const charGrid = document.getElementById("charGrid");
const svgMatrix = document.getElementById("svgMatrix");
const verticalMode = document.getElementById("verticalMode");
const vfSlider = document.getElementById("vfSlider");
const vfValue = document.getElementById("vfValue");
const vfPreview = document.getElementById("vfPreview");

function currentText(){{
  return previewText.value || "";
}}

function encodedText(){{
  return encodeURIComponent(currentText() || DEFAULT_TEXT);
}}

function verticalFlag(){{
  return verticalMode.checked ? "1" : "0";
}}

function renderFontList(){{
  fontList.innerHTML = "";

  FONT_ITEMS.forEach((item, idx) => {{
    const b = document.createElement("button");
    b.className = "font-btn";
    b.textContent = String(idx + 1).padStart(2, "0") + " ｜ " + item.name;
    b.onclick = () => setFontIndex(idx);
    fontList.appendChild(b);
  }});
}}

function setFontIndex(idx){{
  currentIndex = Math.max(0, Math.min(FONT_ITEMS.length - 1, idx));
  familySlider.value = currentIndex;

  const item = FONT_ITEMS[currentIndex];
  currentFontName.textContent = String(currentIndex + 1).padStart(2, "0") + " ｜ " + item.name;

  familyPreview.style.fontFamily = item.face + ", sans-serif";
  familyPreview.textContent = currentText() || DEFAULT_TEXT;

  Array.from(document.querySelectorAll(".font-btn")).forEach((b, i) => {{
    b.classList.toggle("active", i === currentIndex);
  }});

  updateVertical();
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

function renderMatrix(){{
  svgMatrix.innerHTML = "";

  FONT_ITEMS.forEach((item, idx) => {{
    const card = document.createElement("div");
    card.className = "card";

    const title = document.createElement("div");
    title.className = "card-title";
    title.textContent = String(idx + 1).padStart(2, "0") + " ｜ " + item.name;

    const img = document.createElement("img");
    img.src = "/api/real_preview/svg_line/" + encodeURIComponent(SCOPE) + "/" + encodeURIComponent(JOB_ID) + "/" + idx + "?text=" + encodedText() + "&vertical=" + verticalFlag() + "&t=" + Date.now();

    card.appendChild(title);
    card.appendChild(img);
    svgMatrix.appendChild(card);
  }});
}}

function updateVF(){{
  const v = parseInt(vfSlider.value, 10);
  vfValue.textContent = v;

  if(HAS_VF){{
    vfPreview.style.fontFamily = "RealPreviewVF, sans-serif";
    vfPreview.style.fontVariationSettings = "'wght' " + v;
    vfPreview.textContent = currentText() || DEFAULT_TEXT;
  }}else{{
    vfPreview.style.fontFamily = "Arial, sans-serif";
    vfPreview.textContent = "Variable Font 未合成成功；请使用上面的真实 TTF 字体家族滑杆。";
  }}

  updateVertical();
}}

function updateVertical(){{
  const targets = [familyPreview, vfPreview];

  targets.forEach(el => {{
    if(verticalMode.checked){{
      el.classList.add("vertical");
    }}else{{
      el.classList.remove("vertical");
    }}
  }});
}}

function updateAll(){{
  familyPreview.textContent = currentText() || DEFAULT_TEXT;
  updateVF();
  renderMatrix();
  updateVertical();
}}

function clearText(){{
  previewText.value = "";
  updateAll();
}}

function resetText(){{
  previewText.value = DEFAULT_TEXT;
  updateAll();
}}

familySlider.addEventListener("input", () => {{
  setFontIndex(parseInt(familySlider.value, 10));
}});

previewText.addEventListener("input", updateAll);

verticalMode.addEventListener("change", updateAll);

vfSlider.addEventListener("input", updateVF);

renderFontList();
renderChars();
setFontIndex(0);
renderMatrix();
updateVF();
</script>

</body>
</html>
'''
        return HTMLResponse(html)

    @app.middleware("http")
    async def inject_real_preview_center_entry(request, call_next):
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

        if "real-preview-center-entry-v1" not in text:
            if "</body>" in text:
                text = text.replace("</body>", FRONTEND_ENTRY + "\n</body>", 1)
            else:
                text += FRONTEND_ENTRY

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )
