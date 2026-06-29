from pathlib import Path
import re
import zipfile
import traceback
from html import escape

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_JOBS = BASE_DIR / "runtime_jobs"
OUTPUT_DIR = BASE_DIR / "output"
ASSET_DIR = BASE_DIR / "runtime_family_assets"
ASSET_DIR.mkdir(exist_ok=True)


def safe_name(s: str):
    s = str(s)
    out = []
    for ch in s:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:120] or "item"


def natural_key(p: Path):
    text = p.name
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", text)]


def font_files_in_dir(d: Path):
    if not d.exists():
        return []
    files = list(d.glob("*.ttf")) + list(d.glob("*.otf"))
    return sorted(files, key=natural_key)


def find_latest_font_dir(root: Path):
    if not root.exists():
        return None

    candidates = []

    for d in [root] + [x for x in root.rglob("*") if x.is_dir()]:
        files = font_files_in_dir(d)
        if len(files) >= 2:
            latest = max(p.stat().st_mtime for p in files)
            candidates.append((latest, d, len(files)))

    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda x: x[0])
    return candidates[0][1]


def resolve_output_dir(scope: str, job_id: str):
    scope = safe_name(scope)
    job_id = safe_name(job_id)

    if scope == "unicode":
        d = RUNTIME_JOBS / job_id / "outputs"
        if font_files_in_dir(d):
            return d
        return None

    if scope == "foundry":
        # 下半部分蒙古文字体公司规则的默认输出目录
        d = OUTPUT_DIR / "mongolian_gb_ttf_steps"
        if font_files_in_dir(d):
            return d

        # 兼容其他 output 子目录
        d2 = find_latest_font_dir(OUTPUT_DIR)
        if d2:
            return d2

        return None

    return None


def asset_path(scope: str, job_id: str):
    d = ASSET_DIR / safe_name(scope) / safe_name(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_zip(scope: str, job_id: str):
    out_dir = resolve_output_dir(scope, job_id)
    if not out_dir:
        return None, "没有找到已生成的 TTF/OTF 输出目录。"

    files = font_files_in_dir(out_dir)
    if not files:
        return None, "输出目录里没有 TTF/OTF 字体文件。"

    adir = asset_path(scope, job_id)
    zip_path = adir / f"{safe_name(scope)}_{safe_name(job_id)}_font_family.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.name)

    return zip_path, ""


def get_family_name(font_files):
    if not font_files:
        return "FontMorphFamily"

    stem = font_files[0].stem

    for token in ["_Morph", "_Weight", "-Morph", "-Weight"]:
        if token in stem:
            return stem.split(token)[0]

    return re.sub(r"[_-]?\d+$", "", stem) or "FontMorphFamily"


def check_same_glyph_order(font_files):
    from fontTools.ttLib import TTFont

    base_font = TTFont(str(font_files[0]))
    base_order = base_font.getGlyphOrder()
    base_font.close()

    for p in font_files[1:]:
        f = TTFont(str(p))
        order = f.getGlyphOrder()
        f.close()

        if order != base_order:
            return False, f"字形顺序不一致，无法真实合成 Variable Font：{p.name}"

    return True, ""


def build_variable_font(scope: str, job_id: str):
    """
    真实合成 Variable Font。
    注意：这不是假滑杆。这里会调用 fontTools.varLib 生成真正的 gvar/fvar 可变字体。
    """
    out_dir = resolve_output_dir(scope, job_id)
    if not out_dir:
        return None, "没有找到已生成的字体输出目录。"

    font_files = font_files_in_dir(out_dir)
    if len(font_files) < 2:
        return None, "至少需要 2 个 TTF/OTF 才能合成 Variable Font。"

    ok, msg = check_same_glyph_order(font_files)
    if not ok:
        return None, msg

    adir = asset_path(scope, job_id)
    vf_path = adir / f"{safe_name(scope)}_{safe_name(job_id)}_Variable.ttf"
    ds_path = adir / f"{safe_name(scope)}_{safe_name(job_id)}.designspace"
    err_path = adir / "variable_font_error.txt"

    if vf_path.exists():
        return vf_path, ""

    try:
        from fontTools.designspaceLib import DesignSpaceDocument, AxisDescriptor, SourceDescriptor
        from fontTools.varLib import build as varlib_build

        family_name = get_family_name(font_files)

        doc = DesignSpaceDocument()

        axis = AxisDescriptor()
        axis.name = "Weight"
        axis.tag = "wght"
        axis.minimum = 100
        axis.default = 100
        axis.maximum = 900
        doc.addAxis(axis)

        n = len(font_files)

        for i, p in enumerate(font_files):
            value = 100 if n == 1 else int(round(100 + i * 800 / (n - 1)))

            src = SourceDescriptor()
            src.path = str(p.resolve())
            src.name = f"master_{i:02d}"
            src.familyName = family_name
            src.styleName = f"Weight {value}"
            src.location = {"Weight": value}

            if i == 0:
                src.copyInfo = True
                src.copyFeatures = True
                src.copyLib = True
                src.copyGroups = True

            doc.addSource(src)

        doc.write(str(ds_path))

        result = varlib_build(str(ds_path))

        if isinstance(result, tuple):
            varfont = result[0]
        else:
            varfont = result

        varfont.save(str(vf_path))

        if err_path.exists():
            err_path.unlink()

        return vf_path, ""

    except Exception:
        err = traceback.format_exc()
        err_path.write_text(err, encoding="utf-8")
        return None, err


FRONTEND = r'''
<script id="real-vf-family-all-v1">
(function(){
  if(window.__REAL_VF_FAMILY_ALL_V1__) return;
  window.__REAL_VF_FAMILY_ALL_V1__ = true;

  function extractUnicodeJobId(){
    const links = Array.from(document.querySelectorAll("a[href]"));
    for(const a of links){
      const href = a.getAttribute("href") || "";

      let m = href.match(/\/api\/unicode\/zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/download_zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/download\/([^\/?#]+)\//);
      if(m) return decodeURIComponent(m[1]);
    }
    return "";
  }

  function makeBtn(text, href, bg){
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

  function installUnicodeButtons(){
    const jobId = extractUnicodeJobId();
    if(!jobId) return;
    if(document.getElementById("realUnicodeVFFamilyButtons")) return;

    const host =
      document.getElementById("unicodeZipPreviewActionBar") ||
      document.getElementById("unicodeBridgeOutputs") ||
      document.getElementById("outputs") ||
      document.querySelector(".outputs") ||
      document.body;

    const box = document.createElement("div");
    box.id = "realUnicodeVFFamilyButtons";
    box.style.cssText = "margin-top:12px;padding:12px;border:1px solid #ddd;border-radius:10px;background:#f8fafc;";

    const title = document.createElement("div");
    title.textContent = "上半部分：真实可变字体 / 字体家族功能";
    title.style.cssText = "font-weight:700;margin-bottom:8px;";

    box.appendChild(title);
    box.appendChild(makeBtn("下载全部 TTF 字体压缩包", "/api/real_family/zip/unicode/" + encodeURIComponent(jobId), "#2563eb"));
    box.appendChild(makeBtn("滑杆实时预览 / 字体家族", "/api/real_family/slider/unicode/" + encodeURIComponent(jobId), "#7c3aed"));
    box.appendChild(makeBtn("下载真实 Variable Font", "/api/real_family/vf/unicode/" + encodeURIComponent(jobId), "#0f766e"));

    host.appendChild(box);
  }

  function installFoundryButtons(){
    if(document.getElementById("realFoundryVFFamilyButtons")) return;

    const btn =
      document.getElementById("foundryRulesBuildBtn") ||
      Array.from(document.querySelectorAll("button")).find(b => (b.innerText || "").includes("按所选字体公司规则生成"));

    if(!btn) return;

    const formHost = btn.closest("div") || btn.parentElement || document.body;

    const box = document.createElement("div");
    box.id = "realFoundryVFFamilyButtons";
    box.style.cssText = "margin-top:14px;padding:12px;border:1px solid #ddd;border-radius:10px;background:#f8fafc;";

    const title = document.createElement("div");
    title.textContent = "下半部分：蒙古文字体公司规则生成结果";
    title.style.cssText = "font-weight:700;margin-bottom:8px;";

    const note = document.createElement("div");
    note.textContent = "先点击上方“按所选字体公司规则生成”，完成后再使用下面按钮。按钮会读取真实输出目录 output/mongolian_gb_ttf_steps/ 中的 TTF 文件。";
    note.style.cssText = "font-size:12px;color:#555;margin-bottom:8px;";

    box.appendChild(title);
    box.appendChild(note);
    box.appendChild(makeBtn("下载全部蒙古文 TTF 压缩包", "/api/real_family/zip/foundry/latest", "#2563eb"));
    box.appendChild(makeBtn("滑杆实时预览 / 蒙古文字体家族", "/api/real_family/slider/foundry/latest", "#7c3aed"));
    box.appendChild(makeBtn("下载蒙古文 Variable Font", "/api/real_family/vf/foundry/latest", "#0f766e"));

    formHost.appendChild(box);
  }

  function run(){
    installUnicodeButtons();
    installFoundryButtons();
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


def install_real_vf_family_all(app):
    if getattr(app.state, "_real_vf_family_all_v1", False):
        return

    app.state._real_vf_family_all_v1 = True

    @app.get("/api/real_family/zip/{scope}/{job_id}")
    def download_zip(scope: str, job_id: str):
        zip_path, err = make_zip(scope, job_id)

        if not zip_path or not zip_path.exists():
            return HTMLResponse(
                "<h2>字体压缩包生成失败</h2>"
                f"<pre>{escape(err)}</pre>",
                status_code=404,
            )

        return FileResponse(
            str(zip_path),
            filename=f"{safe_name(scope)}_{safe_name(job_id)}_font_family.zip",
            media_type="application/zip",
        )

    @app.get("/api/real_family/vf/{scope}/{job_id}")
    def download_vf(scope: str, job_id: str):
        vf_path, err = build_variable_font(scope, job_id)

        if not vf_path or not vf_path.exists():
            return HTMLResponse(
                "<h2>真实 Variable Font 合成失败</h2>"
                "<p>原因通常是：这些 TTF 的 glyph 顺序、点结构或轮廓兼容性不满足 varLib 要求。</p>"
                "<p>这不是演示失败，而是真实合成时 fontTools.varLib 给出的失败结果。字体家族滑杆仍可使用。</p>"
                f"<pre style='white-space:pre-wrap;background:#111827;color:#ddd;padding:12px;border-radius:8px;'>{escape((err or '')[-6000:])}</pre>",
                status_code=500,
            )

        return FileResponse(
            str(vf_path),
            filename=f"{safe_name(scope)}_{safe_name(job_id)}_Variable.ttf",
            media_type="font/ttf",
        )

    @app.get("/api/real_family/font/{scope}/{job_id}/{filename}")
    def font_file(scope: str, job_id: str, filename: str):
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return JSONResponse({"error": "没有找到输出目录。"}, status_code=404)

        target = out_dir / Path(filename).name

        if not target.exists():
            return JSONResponse({"error": "字体文件不存在。"}, status_code=404)

        return FileResponse(
            str(target),
            filename=target.name,
            media_type="font/ttf",
        )

    @app.get("/api/real_family/slider/{scope}/{job_id}")
    def slider_page(scope: str, job_id: str):
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return HTMLResponse(
                "<h2>没有找到真实字体输出目录</h2>"
                "<p>上半部分请先完成通用 Unicode 生成；下半部分请先完成蒙古文字体公司规则生成。</p>",
                status_code=404,
            )

        font_files = font_files_in_dir(out_dir)

        if not font_files:
            return HTMLResponse("<h2>没有找到 TTF/OTF 字体文件。</h2>", status_code=404)

        vf_path, vf_err = build_variable_font(scope, job_id)
        has_vf = bool(vf_path and vf_path.exists())

        family_name = get_family_name(font_files)

        static_faces = []
        items = []

        for i, p in enumerate(font_files):
            face = f"RealFamilyStep{i+1:02d}"
            url = f"/api/real_family/font/{safe_name(scope)}/{safe_name(job_id)}/{p.name}"

            static_faces.append(
                f"@font-face{{font-family:'{face}';src:url('{url}') format('truetype');font-weight:400;font-style:normal;}}"
            )

            items.append((face, p.name, url))

        vf_face = ""
        if has_vf:
            vf_face = (
                "@font-face{"
                "font-family:'RealGeneratedVF';"
                f"src:url('/api/real_family/vf/{safe_name(scope)}/{safe_name(job_id)}') format('truetype');"
                "font-weight:100 900;"
                "font-style:normal;"
                "}"
            )

        items_js = "[\n" + ",\n".join(
            "{face:'%s',name:'%s',url:'%s'}" % (
                escape(face),
                escape(name),
                escape(url),
            )
            for face, name, url in items
        ) + "\n]"

        warning = ""
        if not has_vf:
            warning = f"""
<div class="warn">
  <b>Variable Font 未能合成。</b>
  <p>系统已经真实尝试调用 fontTools.varLib。失败原因见下方日志。下方字体家族滑杆仍然是真实使用 {len(font_files)} 个 TTF 文件切换。</p>
  <details>
    <summary>查看 VF 合成错误</summary>
    <pre>{escape((vf_err or '')[-6000:])}</pre>
  </details>
</div>
"""

        html = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>真实可变字体 / 字体家族滑杆</title>
<style>
{vf_face}
{chr(10).join(static_faces)}
body {{
  margin:0;
  background:#f3f4f6;
  color:#111;
  font-family:Arial,"Microsoft YaHei",sans-serif;
}}
.header {{
  background:#111827;
  color:white;
  padding:24px 36px;
}}
.container {{
  max-width:1100px;
  margin:24px auto;
  background:white;
  padding:26px;
  border-radius:14px;
  box-shadow:0 8px 28px rgba(0,0,0,.08);
}}
.panel {{
  margin-top:18px;
  padding:18px;
  border:1px solid #e5e7eb;
  border-radius:12px;
  background:#fff;
}}
textarea {{
  width:100%;
  min-height:80px;
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
  min-height:170px;
  padding:24px;
  border:1px solid #e5e7eb;
  border-radius:12px;
  background:#fafafa;
  font-size:72px;
  line-height:1.2;
  overflow:auto;
}}
.btns a {{
  display:inline-block;
  margin:6px 8px 6px 0;
  padding:10px 14px;
  color:#fff;
  border-radius:8px;
  text-decoration:none;
  font-weight:700;
}}
.blue {{ background:#2563eb; }}
.black {{ background:#111827; }}
.green {{ background:#0f766e; }}
.warn {{
  margin-top:16px;
  padding:12px;
  background:#fff7ed;
  border:1px solid #fed7aa;
  border-radius:10px;
  color:#7c2d12;
}}
pre {{
  white-space:pre-wrap;
  background:#111827;
  color:#ddd;
  padding:12px;
  border-radius:8px;
  max-height:260px;
  overflow:auto;
}}
.grid {{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(190px,1fr));
  gap:12px;
  margin-top:16px;
}}
.card {{
  padding:12px;
  border:1px solid #e5e7eb;
  border-radius:10px;
  background:#f9fafb;
}}
.sample {{
  font-size:42px;
  line-height:1.1;
  margin-top:8px;
}}
.small {{
  font-size:13px;
  color:#555;
}}
</style>
</head>
<body>
<div class="header">
  <h1>真实可变字体 / 字体家族滑杆</h1>
  <p>范围：{escape(scope)} ｜ 任务：{escape(job_id)} ｜ 字体家族：{escape(family_name)} ｜ TTF 数量：{len(font_files)}</p>
</div>

<div class="container">
  <div class="btns">
    <a class="blue" href="/api/real_family/zip/{escape(safe_name(scope))}/{escape(safe_name(job_id))}" target="_blank">下载全部 TTF 压缩包</a>
    <a class="green" href="/api/real_family/vf/{escape(safe_name(scope))}/{escape(safe_name(job_id))}" target="_blank">下载真实 Variable Font</a>
  </div>

  {warning}

  <div class="panel">
    <h2>1. Variable Font 连续滑杆</h2>
    <div class="small">如果 VF 合成成功，这里使用真实 <code>font-variation-settings:'wght'</code> 连续调节。</div>
    <label>Weight：<span id="vfValue">100</span></label>
    <input id="vfSlider" type="range" min="100" max="900" value="100" step="1">

    <label>预览文字</label>
    <textarea id="previewText">ABCDEabcde123 蒙古文 ᠮᠣᠩᠭᠣᠯ</textarea>

    <div id="vfPreview" class="preview">ABCDEabcde123 蒙古文 ᠮᠣᠩᠭᠣᠯ</div>
  </div>

  <div class="panel">
    <h2>2. 字体家族滑杆</h2>
    <div class="small">这里真实加载生成出来的 {len(font_files)} 个 TTF，通过滑杆切换家族样式。</div>
    <label>Style：<span id="familyValue">1 / {len(font_files)}</span></label>
    <input id="familySlider" type="range" min="1" max="{len(font_files)}" value="1" step="1">
    <div id="familyPreview" class="preview">ABCDEabcde123 蒙古文 ᠮᠣᠩᠭᠣᠯ</div>
  </div>

  <div class="panel">
    <h2>3. 字体家族列表</h2>
    <div id="grid" class="grid"></div>
  </div>
</div>

<script>
const HAS_VF = {str(has_vf).lower()};
const FONT_ITEMS = {items_js};

const vfSlider = document.getElementById("vfSlider");
const vfValue = document.getElementById("vfValue");
const vfPreview = document.getElementById("vfPreview");

const familySlider = document.getElementById("familySlider");
const familyValue = document.getElementById("familyValue");
const familyPreview = document.getElementById("familyPreview");

const previewText = document.getElementById("previewText");
const grid = document.getElementById("grid");

function currentText(){{
  return previewText.value || "ABCDEabcde123";
}}

function updateVF(){{
  const v = parseInt(vfSlider.value, 10);
  vfValue.textContent = v;

  vfPreview.textContent = currentText();

  if(HAS_VF){{
    vfPreview.style.fontFamily = "RealGeneratedVF, sans-serif";
    vfPreview.style.fontVariationSettings = "'wght' " + v;
  }}else{{
    vfPreview.style.fontFamily = "Arial, sans-serif";
    vfPreview.textContent = "Variable Font 未合成成功。请使用下面的字体家族滑杆。";
  }}
}}

function updateFamily(){{
  const idx = parseInt(familySlider.value, 10) - 1;
  const item = FONT_ITEMS[idx];

  familyValue.textContent = (idx + 1) + " / " + FONT_ITEMS.length + " ｜ " + item.name;
  familyPreview.textContent = currentText();
  familyPreview.style.fontFamily = item.face + ", sans-serif";
}}

function buildGrid(){{
  grid.innerHTML = "";
  FONT_ITEMS.forEach((item, idx) => {{
    const card = document.createElement("div");
    card.className = "card";

    const title = document.createElement("div");
    title.textContent = String(idx + 1).padStart(2, "0") + " ｜ " + item.name;
    title.style.fontWeight = "700";

    const sample = document.createElement("div");
    sample.className = "sample";
    sample.textContent = currentText().slice(0, 12);
    sample.style.fontFamily = item.face + ", sans-serif";

    const link = document.createElement("a");
    link.href = item.url;
    link.target = "_blank";
    link.textContent = "下载该 TTF";
    link.style.display = "inline-block";
    link.style.marginTop = "8px";
    link.style.color = "#2563eb";
    link.style.fontWeight = "700";
    link.style.textDecoration = "none";

    card.appendChild(title);
    card.appendChild(sample);
    card.appendChild(link);
    grid.appendChild(card);
  }});
}}

vfSlider.addEventListener("input", updateVF);
familySlider.addEventListener("input", updateFamily);
previewText.addEventListener("input", () => {{
  updateVF();
  updateFamily();
  document.querySelectorAll(".sample").forEach(el => {{
    el.textContent = currentText().slice(0, 12);
  }});
}});

buildGrid();
updateVF();
updateFamily();
</script>
</body>
</html>'''
        return HTMLResponse(html)

    @app.middleware("http")
    async def inject_real_vf_family_buttons(request, call_next):
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

        if "real-vf-family-all-v1" not in text:
            if "</body>" in text:
                text = text.replace("</body>", FRONTEND + "\n</body>", 1)
            else:
                text += FRONTEND

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )
