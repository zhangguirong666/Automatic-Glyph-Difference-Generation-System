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
ASSET_DIR = BASE_DIR / "runtime_clean_preview_assets"
ASSET_DIR.mkdir(exist_ok=True)


def safe_name(s: str):
    s = str(s)
    keep = []
    for ch in s:
        if ch.isalnum() or ch in "._-":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:160] or "item"


def natural_key(p: Path):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", p.name)]


def list_fonts(d: Path):
    if not d or not d.exists():
        return []
    return sorted(list(d.glob("*.ttf")) + list(d.glob("*.otf")), key=natural_key)


def list_svg_files(d: Path):
    if not d or not d.exists():
        return []
    return sorted(list(d.glob("*.svg")), key=natural_key)


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


def resolve_svg_dir(scope: str, job_id: str):
    scope = safe_name(scope)
    job_id = safe_name(job_id)

    if scope == "unicode":
        d = RUNTIME_JOBS / job_id / "svg_previews"
        if list_svg_files(d):
            return d

    # 下半部分如果没有独立 svg_previews，就从 TTF 动态生成 SVG，不依赖目录。
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


def read_chars(scope: str, job_id: str, fonts):
    chars = []
    seen = set()

    if safe_name(scope) == "unicode":
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
                chars.append({"ch": ch, "code": f"U+{cp:04X}", "cp": cp})

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

        mongolian = [cp for cp in sorted(cps) if 0x1800 <= cp <= 0x18AF]
        if mongolian:
            cps = mongolian
        else:
            cps = [cp for cp in sorted(cps) if cp >= 0x20 and cp not in [0xFEFF]]

        for cp in cps[:800]:
            ch = chr(cp)
            if ch.strip() == "" and cp != 0x20:
                continue
            chars.append({"ch": ch, "code": f"U+{cp:04X}", "cp": cp})
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


def make_zip(scope: str, job_id: str, kind: str):
    scope = safe_name(scope)
    job_id = safe_name(job_id)
    kind = safe_name(kind)

    ad = asset_dir(scope, job_id)

    if kind == "ttf":
        out_dir = resolve_output_dir(scope, job_id)
        if not out_dir:
            return None, "没有找到字体输出目录。"
        files = list_fonts(out_dir)
        name = f"{scope}_{job_id}_ttf_family.zip"

    elif kind == "svg":
        svg_dir = resolve_svg_dir(scope, job_id)
        files = list_svg_files(svg_dir) if svg_dir else []
        name = f"{scope}_{job_id}_svg_previews.zip"

        # 没有现成 SVG 时，先不强行打包空目录。
        if not files:
            return None, "没有找到已生成的 SVG 预览文件。可打开 SVG 变化预览页面，页面会从 TTF 动态生成 SVG。"

    else:
        out_dir = resolve_output_dir(scope, job_id)
        svg_dir = resolve_svg_dir(scope, job_id)
        files = []
        files += list_fonts(out_dir) if out_dir else []
        files += list_svg_files(svg_dir) if svg_dir else []
        name = f"{scope}_{job_id}_all_results.zip"

    if not files:
        return None, "没有可打包文件。"

    zpath = ad / name

    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.name)

    return zpath, ""


def try_build_variable_font(scope: str, job_id: str):
    out_dir = resolve_output_dir(scope, job_id)

    if not out_dir:
        return None, "没有找到字体输出目录。"

    fonts = list_fonts(out_dir)

    if len(fonts) < 2:
        return None, "至少需要 2 个 TTF/OTF 才能合成 Variable Font。"

    ad = asset_dir(scope, job_id)
    vf_path = ad / f"{safe_name(scope)}_{safe_name(job_id)}_Variable.ttf"
    ds_path = ad / f"{safe_name(scope)}_{safe_name(job_id)}.designspace"
    err_path = ad / "variable_font_error.txt"

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


def glyph_svg(font_path: Path, text: str, vertical: bool = False, title: str = ""):
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

        svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="24" y="34" font-size="22" fill="#333333">{escape(title or font_path.name)}</text>
  {chr(10).join(paths)}
</svg>
'''
        return svg

    except Exception as e:
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="900" height="260" viewBox="0 0 900 260">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="40" y="120" font-size="26" fill="#c00">SVG 生成失败：{escape(str(e))}</text>
</svg>
'''


ENTRY_SCRIPT = r'''
<script id="clean-real-preview-ui-entry-v1">
(function(){
  if(window.__CLEAN_REAL_PREVIEW_UI_ENTRY_V1__) return;
  window.__CLEAN_REAL_PREVIEW_UI_ENTRY_V1__ = true;

  const oldPanelIds = [
    "realFoundryVFFamilyButtons",
    "foundryWorkspaceEntryBox",
    "realUnicodeVFFamilyButtons",
    "generatedWorkspaceEntryBox",
    "unicodeVariableFamilyButtons",
    "realPreviewCenterUnicodeEntry",
    "realPreviewCenterFoundryEntry",
    "generatedWorkspaceEntryBox",
    "foundryWorkspaceEntryBox"
  ];

  function removeOldPanels(){
    oldPanelIds.forEach(id => {
      const el = document.getElementById(id);
      if(el) el.remove();
    });
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

      m = href.match(/\/api\/clean_preview\/zip\/unicode\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);
    }

    return "";
  }

  function link(text, href){
    const a = document.createElement("a");
    a.href = href;
    a.target = "_blank";
    a.textContent = text;
    a.style.cssText = "margin-right:14px;color:#2563eb;text-decoration:underline;font-weight:600;";
    return a;
  }

  function hideSingleTTFLinks(){
    Array.from(document.querySelectorAll('a[href*="/api/unicode/download/"]')).forEach(a => {
      const text = (a.textContent || "").trim();
      const href = a.getAttribute("href") || "";
      if(text.match(/\.(ttf|otf)$/i) || href.match(/\.(ttf|otf)(\?|#|$)/i)){
        a.style.display = "none";
      }
    });
  }

  function installUnicodeLinks(){
    const jobId = extractUnicodeJobId();
    if(!jobId) return;

    if(document.getElementById("cleanUnicodeLinksPanel")) return;

    const host =
      document.getElementById("outputs") ||
      document.getElementById("unicodeBridgeOutputs") ||
      document.querySelector(".outputs") ||
      document.body;

    const box = document.createElement("div");
    box.id = "cleanUnicodeLinksPanel";
    box.style.cssText = "margin-top:12px;padding:8px 0;font-size:14px;line-height:2;";

    box.appendChild(link("下载 SVG 压缩包", "/api/clean_preview/zip/unicode/" + encodeURIComponent(jobId) + "?kind=svg"));
    box.appendChild(link("下载 TTF 字体家族压缩包", "/api/clean_preview/zip/unicode/" + encodeURIComponent(jobId) + "?kind=ttf"));
    box.appendChild(link("下载全部结果", "/api/clean_preview/zip/unicode/" + encodeURIComponent(jobId) + "?kind=all"));
    box.appendChild(link("打开 SVG 变化预览", "/api/clean_preview/svg_matrix/unicode/" + encodeURIComponent(jobId)));
    box.appendChild(link("打开 TTF 字体家族预览", "/api/clean_preview/family/unicode/" + encodeURIComponent(jobId)));
    box.appendChild(link("打开实时可变滑杆预览", "/api/clean_preview/variable/unicode/" + encodeURIComponent(jobId)));

    host.appendChild(box);
  }

  function installFoundryLinks(){
    if(document.getElementById("cleanFoundryLinksPanel")) return;

    const foundryBtn =
      document.getElementById("foundryRulesBuildBtn") ||
      Array.from(document.querySelectorAll("button")).find(b => (b.innerText || "").includes("按所选字体公司规则生成"));

    if(!foundryBtn) return;

    const statusBox = foundryBtn.closest("div") || foundryBtn.parentElement || document.body;

    const box = document.createElement("div");
    box.id = "cleanFoundryLinksPanel";
    box.style.cssText = "margin-top:12px;padding:8px 0;font-size:14px;line-height:2;";

    box.appendChild(link("下载蒙古文 TTF 字体家族压缩包", "/api/clean_preview/zip/foundry/latest?kind=ttf"));
    box.appendChild(link("下载蒙古文全部结果", "/api/clean_preview/zip/foundry/latest?kind=all"));
    box.appendChild(link("打开蒙古文 SVG 变化预览", "/api/clean_preview/svg_matrix/foundry/latest"));
    box.appendChild(link("打开蒙古文 TTF 字体家族预览", "/api/clean_preview/family/foundry/latest"));
    box.appendChild(link("打开蒙古文实时可变滑杆预览", "/api/clean_preview/variable/foundry/latest"));

    statusBox.appendChild(box);
  }

  function run(){
    removeOldPanels();
    hideSingleTTFLinks();
    installUnicodeLinks();
    installFoundryLinks();
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


def install_clean_real_preview_ui(app):
    if getattr(app.state, "_clean_real_preview_ui_v1", False):
        return

    app.state._clean_real_preview_ui_v1 = True

    @app.get("/api/clean_preview/zip/{scope}/{job_id}")
    def api_zip(scope: str, job_id: str, kind: str = Query("ttf")):
        zpath, err = make_zip(scope, job_id, kind)

        if not zpath or not zpath.exists():
            return HTMLResponse(
                f"<h2>压缩包生成失败</h2><pre>{escape(err)}</pre>",
                status_code=404
            )

        return FileResponse(
            str(zpath),
            filename=zpath.name,
            media_type="application/zip"
        )

    @app.get("/api/clean_preview/vf/{scope}/{job_id}")
    def api_vf(scope: str, job_id: str):
        vf_path, err = try_build_variable_font(scope, job_id)

        if not vf_path or not vf_path.exists():
            return HTMLResponse(
                "<h2>真实 Variable Font 合成失败</h2>"
                "<p>系统已经读取生成出来的 TTF 并调用 fontTools.varLib。失败说明当前 master 不满足可变字体兼容条件。</p>"
                "<p>这不是演示失败；TTF 字体家族预览仍然可用。</p>"
                f"<pre style='white-space:pre-wrap;background:#111827;color:#ddd;padding:12px;border-radius:8px;'>{escape((err or '')[-8000:])}</pre>",
                status_code=500
            )

        return FileResponse(
            str(vf_path),
            filename=vf_path.name,
            media_type="font/ttf"
        )

    @app.get("/api/clean_preview/font/{scope}/{job_id}/{filename}")
    def api_font(scope: str, job_id: str, filename: str):
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return JSONResponse({"error": "没有找到字体输出目录。"}, status_code=404)

        target = out_dir / Path(filename).name

        if not target.exists():
            return JSONResponse({"error": "字体文件不存在。"}, status_code=404)

        return FileResponse(str(target), filename=target.name, media_type="font/ttf")

    @app.get("/api/clean_preview/svg/{scope}/{job_id}/{index}")
    def api_svg(
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
        svg = glyph_svg(fonts[index], text, vertical=bool(vertical), title=fonts[index].name)

        return Response(svg, media_type="image/svg+xml")

    @app.get("/api/clean_preview/svg_glyph/{scope}/{job_id}/{font_index}/{cp}")
    def api_svg_glyph(scope: str, job_id: str, font_index: int, cp: int):
        ch = chr(int(cp))
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return Response("<svg></svg>", media_type="image/svg+xml", status_code=404)

        fonts = list_fonts(out_dir)

        if not fonts:
            return Response("<svg></svg>", media_type="image/svg+xml", status_code=404)

        font_index = max(0, min(len(fonts) - 1, int(font_index)))
        svg = glyph_svg(fonts[font_index], ch, vertical=False, title=f"{ch} U+{cp:04X}")

        return Response(svg, media_type="image/svg+xml")

    @app.get("/api/clean_preview/svg_matrix/{scope}/{job_id}")
    def svg_matrix_page(scope: str, job_id: str):
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return HTMLResponse("<h2>没有找到真实字体输出目录。</h2>", status_code=404)

        fonts = list_fonts(out_dir)

        if not fonts:
            return HTMLResponse("<h2>没有找到 TTF/OTF 文件。</h2>", status_code=404)

        chars = read_chars(scope, job_id, fonts)
        chars = chars[:120]

        html_rows = []

        for item in chars:
            cp = int(item["cp"])
            ch = item["ch"]

            cells = [
                f"<td class='char'>{escape(ch)}</td>",
                f"<td class='code'>{escape(item['code'])}</td>",
            ]

            for i, _ in enumerate(fonts):
                url = f"/api/clean_preview/svg_glyph/{safe_name(scope)}/{safe_name(job_id)}/{i}/{cp}"
                cells.append(f"<td><img src='{url}' /></td>")

            html_rows.append("<tr>" + "".join(cells) + "</tr>")

        headers = "<th>字符</th><th>Unicode</th>" + "".join(
            f"<th>Step {i+1:02d}</th>" for i in range(len(fonts))
        )

        html = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>字体插值 SVG 预览</title>
<style>
body{{font-family:Arial,"Microsoft YaHei",sans-serif;margin:24px;background:#f7f7f7;color:#111;}}
h1{{margin-bottom:6px;}}
.note{{font-size:13px;color:#555;margin-bottom:18px;}}
table{{border-collapse:collapse;background:white;width:max-content;min-width:100%;}}
th,td{{border:1px solid #e5e7eb;padding:8px;text-align:center;vertical-align:middle;}}
th{{background:#fafafa;font-weight:700;position:sticky;top:0;z-index:2;}}
td.char{{font-size:24px;min-width:72px;}}
td.code{{font-size:11px;color:#666;min-width:70px;}}
td img{{width:82px;height:82px;object-fit:contain;display:block;margin:auto;}}
.wrap{{overflow:auto;max-height:calc(100vh - 140px);border:1px solid #e5e7eb;background:white;}}
</style>
</head>
<body>
<h1>字体插值 SVG 预览</h1>
<div class="note">读取真实生成的 TTF 轮廓动态生成 SVG。字符数：{len(chars)}；Step 数：{len(fonts)}。</div>
<div class="wrap">
<table>
<thead><tr>{headers}</tr></thead>
<tbody>
{''.join(html_rows)}
</tbody>
</table>
</div>
</body>
</html>'''
        return HTMLResponse(html)

    @app.get("/api/clean_preview/family/{scope}/{job_id}")
    def family_page(scope: str, job_id: str):
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return HTMLResponse("<h2>没有找到真实字体输出目录。</h2>", status_code=404)

        fonts = list_fonts(out_dir)

        if not fonts:
            return HTMLResponse("<h2>没有找到 TTF/OTF 文件。</h2>", status_code=404)

        chars = read_chars(scope, job_id, fonts)
        chars = chars[:80]
        chars_text = "".join(x["ch"] for x in chars[:35]) or "ABCDEabcde123"

        faces = []
        sections = []

        for i, p in enumerate(fonts):
            face = f"FamilyPreviewStep{i+1:02d}"
            url = f"/api/clean_preview/font/{safe_name(scope)}/{safe_name(job_id)}/{quote(p.name)}"

            faces.append(
                f"@font-face{{font-family:'{face}';src:url('{url}') format('truetype');font-weight:400;font-style:normal;}}"
            )

            glyph_cards = []

            for item in chars[:60]:
                glyph_cards.append(
                    f"<div class='glyph' style=\"font-family:'{face}',sans-serif;\"><div class='g'>{escape(item['ch'])}</div><div class='u'>{escape(item['code'])}</div></div>"
                )

            sections.append(f'''
<div class="family-row">
  <div class="row-title"><span>#{i+1:02d}</span> {escape(p.name)}</div>
  <div class="glyph-grid">{''.join(glyph_cards)}</div>
</div>
''')

        html = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>TTF 字体家族准确预览</title>
<style>
{chr(10).join(faces)}
body{{font-family:Arial,"Microsoft YaHei",sans-serif;margin:24px;background:#f3f4f6;color:#111;}}
h1{{margin-bottom:6px;}}
.note{{font-size:13px;color:#555;margin-bottom:18px;}}
.family-row{{background:white;border-radius:12px;padding:14px;margin:12px 0;box-shadow:0 4px 16px rgba(0,0,0,.06);}}
.row-title{{font-weight:700;font-family:monospace;margin-bottom:10px;}}
.row-title span{{background:#2563eb;color:white;border-radius:6px;padding:3px 8px;margin-right:8px;}}
.glyph-grid{{display:flex;flex-wrap:wrap;gap:8px;}}
.glyph{{width:64px;height:76px;border:1px solid #e5e7eb;border-radius:8px;background:#fff;text-align:center;display:flex;flex-direction:column;justify-content:center;}}
.g{{font-size:36px;line-height:1;}}
.u{{font-size:10px;color:#777;margin-top:5px;font-family:Arial,sans-serif;}}
.preview-line{{background:white;border:1px solid #e5e7eb;border-radius:12px;padding:18px;margin-bottom:18px;font-size:64px;}}
</style>
</head>
<body>
<h1>TTF 字体家族准确预览</h1>
<div class="note">每一行对应一个真实生成出来的 TTF 字体文件，不是截图或模板。</div>
<div class="preview-line">{escape(chars_text)}</div>
{''.join(sections)}
</body>
</html>'''
        return HTMLResponse(html)

    @app.get("/api/clean_preview/variable/{scope}/{job_id}")
    def variable_page(scope: str, job_id: str):
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return HTMLResponse("<h2>没有找到真实字体输出目录。</h2>", status_code=404)

        fonts = list_fonts(out_dir)

        if not fonts:
            return HTMLResponse("<h2>没有找到 TTF/OTF 文件。</h2>", status_code=404)

        chars = read_chars(scope, job_id, fonts)
        default_text = "".join(x["ch"] for x in chars[:12]) or "ABCDEabcde123"
        if safe_name(scope) == "foundry":
            default_text = "".join(x["ch"] for x in chars[:20]) or "ᠮᠣᠩᠭᠣᠯ"

        vf_path, vf_err = try_build_variable_font(scope, job_id)
        has_vf = bool(vf_path and vf_path.exists())

        vf_face = ""
        if has_vf:
            vf_face = (
                "@font-face{"
                "font-family:'CleanRealVF';"
                f"src:url('/api/clean_preview/vf/{safe_name(scope)}/{safe_name(job_id)}') format('truetype');"
                "font-weight:100 900;"
                "font-style:normal;"
                "}"
            )

        chars_json = json.dumps(chars[:240], ensure_ascii=False)

        vf_error_html = ""
        if not has_vf:
            vf_error_html = f'''
<div class="error">
  <b>真实 Variable Font 合成失败。</b>
  <p>滑杆连续变化必须依赖真正的 Variable Font。系统已经调用 fontTools.varLib 尝试合成；失败说明当前 20 个 TTF 的 master 兼容性不满足要求。</p>
  <p>你仍然可以使用 TTF 字体家族预览，但那是离散 Step，不是连续 VF。</p>
  <details>
    <summary>查看真实错误</summary>
    <pre>{escape((vf_err or '')[-8000:])}</pre>
  </details>
</div>
'''

        html = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>实时可变滑杆预览</title>
<style>
{vf_face}
body{{font-family:Arial,"Microsoft YaHei",sans-serif;margin:0;background:#f6f7fb;color:#111;}}
h1{{margin:0 0 8px 0;}}
.header{{padding:22px 34px;background:#fff;border-bottom:1px solid #e5e7eb;}}
.center{{max-width:980px;margin:32px auto;background:white;border-radius:16px;box-shadow:0 8px 28px rgba(0,0,0,.08);padding:26px;}}
.layout{{display:grid;grid-template-columns:200px 1fr;gap:24px;align-items:start;}}
.char-list{{max-height:520px;overflow:auto;border-right:1px solid #e5e7eb;padding-right:12px;}}
.char-btn{{display:block;width:100%;margin:5px 0;padding:9px;border:1px solid #e5e7eb;background:#fff;border-radius:8px;text-align:left;cursor:pointer;}}
.preview{{min-height:300px;border:1px solid #e5e7eb;border-radius:14px;background:#fff;display:flex;align-items:center;justify-content:center;font-size:180px;line-height:1.1;overflow:auto;}}
.preview.vertical{{writing-mode:vertical-lr;text-orientation:mixed;font-size:120px;min-height:520px;}}
.controls{{margin-top:16px;}}
input[type=range]{{width:100%;}}
textarea{{width:100%;box-sizing:border-box;min-height:74px;padding:12px;border:1px solid #d1d5db;border-radius:8px;font-size:16px;}}
.meta{{font-family:monospace;color:#555;font-size:13px;text-align:center;margin-bottom:12px;}}
.error{{background:#fff7ed;border:1px solid #fed7aa;color:#7c2d12;border-radius:10px;padding:14px;margin:18px 0;}}
pre{{white-space:pre-wrap;background:#111827;color:#ddd;padding:12px;border-radius:8px;max-height:260px;overflow:auto;}}
button{{padding:9px 13px;border-radius:8px;border:1px solid #d1d5db;background:#fff;cursor:pointer;margin-right:8px;}}
</style>
</head>
<body>
<div class="header">
  <h1>实时可变滑杆预览</h1>
  <div>如果 VF 合成成功，滑杆会连续改变字形，不是单帧切换。</div>
</div>

<div class="center">
  {vf_error_html}

  <div class="layout">
    <div class="char-list" id="charList"></div>

    <div>
      <div class="meta">当前值：<span id="val">100</span> / 900 ｜ 实时插值轴：wght</div>
      <div id="preview" class="preview">{escape(default_text[:2])}</div>

      <div class="controls">
        <input id="slider" type="range" min="100" max="900" value="100" step="1">
      </div>

      <div class="controls">
        <textarea id="textInput">{escape(default_text[:12])}</textarea>
      </div>

      <div class="controls">
        <button type="button" id="verticalBtn">切换竖排</button>
        <button type="button" id="resetBtn">恢复默认文字</button>
      </div>
    </div>
  </div>
</div>

<script>
const HAS_VF = {str(has_vf).lower()};
const CHARS = {chars_json};
const DEFAULT_TEXT = {json.dumps(default_text[:12], ensure_ascii=False)};

const preview = document.getElementById("preview");
const slider = document.getElementById("slider");
const val = document.getElementById("val");
const textInput = document.getElementById("textInput");
const charList = document.getElementById("charList");
const verticalBtn = document.getElementById("verticalBtn");
const resetBtn = document.getElementById("resetBtn");

function apply(){{
  const v = parseInt(slider.value, 10);
  val.textContent = v;
  preview.textContent = textInput.value || DEFAULT_TEXT;

  if(HAS_VF){{
    preview.style.fontFamily = "CleanRealVF, sans-serif";
    preview.style.fontVariationSettings = "'wght' " + v;
  }}else{{
    preview.style.fontFamily = "Arial, sans-serif";
  }}
}}

function renderChars(){{
  charList.innerHTML = "";
  CHARS.forEach(item => {{
    const b = document.createElement("button");
    b.className = "char-btn";
    b.textContent = item.ch + "  " + item.code;
    b.onclick = () => {{
      textInput.value = item.ch;
      apply();
    }};
    charList.appendChild(b);
  }});
}}

slider.addEventListener("input", apply);
textInput.addEventListener("input", apply);

verticalBtn.onclick = () => {{
  preview.classList.toggle("vertical");
}};

resetBtn.onclick = () => {{
  textInput.value = DEFAULT_TEXT;
  apply();
}};

renderChars();
apply();
</script>
</body>
</html>'''
        return HTMLResponse(html)

    @app.middleware("http")
    async def clean_real_preview_ui_middleware(request, call_next):
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

        if "clean-real-preview-ui-entry-v1" not in text:
            if "</body>" in text:
                text = text.replace("</body>", ENTRY_SCRIPT + "\n</body>", 1)
            else:
                text += ENTRY_SCRIPT

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html"
        )
