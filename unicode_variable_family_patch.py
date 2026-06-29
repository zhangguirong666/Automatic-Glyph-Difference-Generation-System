from pathlib import Path
import traceback
from xml.sax.saxutils import escape

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response


BASE_DIR = Path(__file__).resolve().parent
JOB_DIR = BASE_DIR / "runtime_jobs"


def safe_name(name: str):
    keep = []
    for ch in name:
        if ch.isalnum() or ch in "._-":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:120] or "file"


def job_dir(job_id: str):
    return JOB_DIR / safe_name(job_id)


def get_font_files(job_id: str):
    out_dir = job_dir(job_id) / "outputs"
    if not out_dir.exists():
        return []
    return sorted(out_dir.glob("*.ttf")) + sorted(out_dir.glob("*.otf"))


def get_family_name(font_files):
    if not font_files:
        return "FontMorphFamily"
    stem = font_files[0].stem
    if "_Morph" in stem:
        return stem.split("_Morph")[0]
    if "_Weight" in stem:
        return stem.split("_Weight")[0]
    return "FontMorphFamily"


def build_variable_font(job_id: str):
    """
    尝试把 20 个中间 TTF 合成为单文件 Variable Font。
    如果字形点结构不兼容，会失败；失败时不影响现有 TTF 家族滑杆预览。
    """
    jdir = job_dir(job_id)
    vf_dir = jdir / "variable_font"
    vf_dir.mkdir(parents=True, exist_ok=True)

    vf_path = vf_dir / "FontMorphFamily_Variable.ttf"
    error_path = vf_dir / "variable_build_error.txt"

    if vf_path.exists():
        return vf_path, ""

    font_files = get_font_files(job_id)

    if len(font_files) < 2:
        return None, "生成可变字体至少需要 2 个 TTF/OTF 中间字体。"

    try:
        from fontTools.designspaceLib import DesignSpaceDocument, AxisDescriptor, SourceDescriptor
        from fontTools.varLib import build as varlib_build
        from fontTools.ttLib import TTFont

        # 检查 glyphOrder 是否一致
        base_order = TTFont(str(font_files[0])).getGlyphOrder()
        for p in font_files[1:]:
            order = TTFont(str(p)).getGlyphOrder()
            if order != base_order:
                raise RuntimeError(f"字形顺序不一致，无法合成 Variable Font：{p.name}")

        family_name = get_family_name(font_files)

        ds_path = vf_dir / "FontMorphFamily.designspace"

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

            source = SourceDescriptor()
            source.path = str(p.resolve())
            source.name = f"master.{i:02d}"
            source.familyName = family_name
            source.styleName = f"Weight {value}"
            source.location = {"Weight": value}

            if i == 0:
                source.copyInfo = True
                source.copyFeatures = True
                source.copyLib = True
                source.copyGroups = True

            doc.addSource(source)

        doc.write(str(ds_path))

        built = varlib_build(str(ds_path))
        vf = built[0] if isinstance(built, tuple) else built

        # 命名修正
        try:
            name_table = vf["name"]
            for record in name_table.names:
                if record.nameID in [1, 4, 6]:
                    pass
        except Exception:
            pass

        vf.save(str(vf_path))

        if error_path.exists():
            error_path.unlink()

        return vf_path, ""

    except Exception:
        err = traceback.format_exc()
        error_path.write_text(err, encoding="utf-8")
        return None, err


FRONTEND_SCRIPT = r'''
<script id="unicode-variable-family-buttons-v1">
(function(){
  if(window.__UNICODE_VARIABLE_FAMILY_BUTTONS_V1__) return;
  window.__UNICODE_VARIABLE_FAMILY_BUTTONS_V1__ = true;

  function extractJobId(){
    const links = Array.from(document.querySelectorAll("a[href]"));

    for(const a of links){
      const href = a.getAttribute("href") || "";

      let m = href.match(/\/api\/unicode\/zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/download_zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/svg_preview_page\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/download\/([^\/?#]+)\//);
      if(m) return decodeURIComponent(m[1]);
    }

    return "";
  }

  function findActionBar(){
    return (
      document.getElementById("unicodeZipPreviewActionBar") ||
      document.getElementById("unicodeBridgeOutputs") ||
      document.getElementById("outputs") ||
      document.querySelector(".outputs") ||
      document.body
    );
  }

  function makeBtn(text, href, bg){
    const a = document.createElement("a");
    a.href = href;
    a.textContent = text;
    a.target = "_blank";
    a.style.cssText = [
      "display:inline-block",
      "padding:10px 14px",
      "background:" + bg,
      "color:#fff",
      "border-radius:8px",
      "text-decoration:none",
      "font-weight:700",
      "margin-right:10px",
      "margin-top:8px"
    ].join(";");
    return a;
  }

  function installButtons(){
    const jobId = extractJobId();
    if(!jobId) return;

    if(document.getElementById("unicodeVariableFamilyButtons")) return;

    const host = findActionBar();

    const wrap = document.createElement("div");
    wrap.id = "unicodeVariableFamilyButtons";
    wrap.style.cssText = [
      "margin-top:10px",
      "padding:12px",
      "border:1px solid #e5e7eb",
      "background:#f8fafc",
      "border-radius:10px"
    ].join(";");

    wrap.appendChild(
      makeBtn(
        "滑杆实时预览 / 字体家族",
        "/api/unicode/family_slider_page/" + encodeURIComponent(jobId),
        "#7c3aed"
      )
    );

    wrap.appendChild(
      makeBtn(
        "下载可变字体 VF",
        "/api/unicode/variable_font/" + encodeURIComponent(jobId),
        "#0f766e"
      )
    );

    const note = document.createElement("div");
    note.textContent = "说明：滑杆预览优先使用 Variable Font；如果合成失败，则自动使用 20 个 TTF 字体家族进行滑杆切换。";
    note.style.cssText = "font-size:12px;color:#555;margin-top:8px;";
    wrap.appendChild(note);

    host.appendChild(wrap);
  }

  setInterval(installButtons, 1000);

  document.addEventListener("DOMContentLoaded", function(){
    installButtons();
    const mo = new MutationObserver(installButtons);
    mo.observe(document.body, {childList:true, subtree:true});
  });
})();
</script>
'''


def install_variable_family_patch(app):
    if getattr(app.state, "_unicode_variable_family_buttons_v1", False):
        return

    app.state._unicode_variable_family_buttons_v1 = True

    @app.get("/api/unicode/family_font/{job_id}/{filename}")
    def family_font_file(job_id: str, filename: str):
        filename = Path(filename).name
        target = job_dir(job_id) / "outputs" / filename

        if not target.exists():
            return JSONResponse({"error": "字体文件不存在。"}, status_code=404)

        return FileResponse(
            str(target),
            filename=target.name,
            media_type="font/ttf"
        )

    @app.get("/api/unicode/variable_font/{job_id}")
    def variable_font_file(job_id: str):
        vf_path, err = build_variable_font(job_id)

        if not vf_path or not vf_path.exists():
            return HTMLResponse(
                "<h2>Variable Font 合成失败</h2>"
                "<p>这通常是因为不同字体的字形点结构不完全兼容。</p>"
                "<p>不影响 20 个 TTF 字体家族和滑杆切换预览。</p>"
                "<pre style='white-space:pre-wrap;background:#111827;color:#ddd;padding:12px;border-radius:8px;'>"
                + escape(err[-5000:] if err else "未知错误")
                + "</pre>",
                status_code=500
            )

        return FileResponse(
            str(vf_path),
            filename=f"unicode_variable_font_{safe_name(job_id)}.ttf",
            media_type="font/ttf"
        )

    @app.get("/api/unicode/family_slider_page/{job_id}")
    def family_slider_page(job_id: str):
        font_files = get_font_files(job_id)

        if not font_files:
            return HTMLResponse(
                "<h2>没有找到已生成的 TTF 字体文件</h2><p>请先完成字体差值生成。</p>",
                status_code=404
            )

        vf_path, vf_err = build_variable_font(job_id)
        has_vf = bool(vf_path and vf_path.exists())

        family_name = get_family_name(font_files)

        static_faces = []
        font_items = []

        for idx, p in enumerate(font_files):
            face_name = f"FamilyStep{idx+1:02d}"
            url = f"/api/unicode/family_font/{safe_name(job_id)}/{p.name}"

            static_faces.append(
                f"@font-face{{font-family:'{face_name}';src:url('{url}') format('truetype');font-weight:400;font-style:normal;}}"
            )

            font_items.append({
                "idx": idx,
                "face": face_name,
                "name": p.name,
                "url": url,
            })

        vf_face = ""
        if has_vf:
            vf_face = (
                "@font-face{"
                "font-family:'GeneratedVariableFont';"
                f"src:url('/api/unicode/variable_font/{safe_name(job_id)}') format('truetype');"
                "font-weight:100 900;"
                "font-style:normal;"
                "}"
            )

        names_js = "[\n" + ",\n".join(
            "{face:'%s',name:'%s',url:'%s'}" % (
                escape(item["face"]),
                escape(item["name"]),
                escape(item["url"]),
            )
            for item in font_items
        ) + "\n]"

        vf_error_block = ""
        if not has_vf and vf_err:
            vf_error_block = f"""
<div class="warn">
  <b>Variable Font 单文件合成失败。</b>
  <div>不影响下方“字体家族滑杆预览”。当前仍可用 20 个 TTF 进行实时切换。</div>
  <details>
    <summary>查看 VF 合成错误</summary>
    <pre>{escape(vf_err[-5000:])}</pre>
  </details>
</div>
"""

        html = f'''
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>可变字体 / 字体家族滑杆预览</title>
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
  border:1px solid #e5e7eb;
  border-radius:12px;
  padding:18px;
  margin-top:18px;
  background:#fff;
}}

label {{
  display:block;
  font-weight:700;
  margin-top:14px;
}}

input[type="range"] {{
  width:100%;
}}

textarea {{
  width:100%;
  box-sizing:border-box;
  min-height:86px;
  padding:12px;
  border:1px solid #d1d5db;
  border-radius:8px;
  font-size:16px;
}}

.preview {{
  min-height:180px;
  border:1px solid #e5e7eb;
  border-radius:12px;
  padding:24px;
  margin-top:16px;
  background:#fafafa;
  font-size:72px;
  line-height:1.25;
  overflow:auto;
}}

.small {{
  font-size:13px;
  color:#555;
}}

.btns a {{
  display:inline-block;
  margin:8px 10px 0 0;
  padding:10px 14px;
  border-radius:8px;
  text-decoration:none;
  font-weight:700;
  color:white;
}}

.blue {{ background:#2563eb; }}
.black {{ background:#111827; }}
.green {{ background:#0f766e; }}

.warn {{
  background:#fff7ed;
  border:1px solid #fed7aa;
  color:#7c2d12;
  border-radius:10px;
  padding:12px;
  margin-top:16px;
}}

pre {{
  white-space:pre-wrap;
  background:#111827;
  color:#ddd;
  padding:12px;
  border-radius:8px;
  max-height:240px;
  overflow:auto;
}}

.grid {{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(190px,1fr));
  gap:12px;
  margin-top:16px;
}}

.card {{
  border:1px solid #e5e7eb;
  border-radius:10px;
  padding:12px;
  background:#f9fafb;
}}

.card .sample {{
  font-size:42px;
  line-height:1.1;
  margin-top:8px;
}}
</style>
</head>
<body>

<div class="header">
  <h1>可变字体 / 字体家族滑杆预览</h1>
  <p>任务 ID：{escape(safe_name(job_id))} ｜ 字体家族：{escape(family_name)} ｜ 中间字体数量：{len(font_files)}</p>
</div>

<div class="container">

  <div class="btns">
    <a class="blue" href="/api/unicode/zip/{escape(safe_name(job_id))}" target="_blank">下载全部 TTF 字体压缩包</a>
    <a class="black" href="/api/unicode/svg_preview_page/{escape(safe_name(job_id))}" target="_blank">预览 SVG 字体差值</a>
    <a class="green" href="/api/unicode/variable_font/{escape(safe_name(job_id))}" target="_blank">下载可变字体 VF</a>
  </div>

  {vf_error_block}

  <div class="panel">
    <h2>1. 可变字体滑杆实时预览</h2>
    <div class="small">
      如果 Variable Font 合成成功，下面滑杆会通过 <code>font-variation-settings: 'wght'</code> 实时调节。
    </div>

    <label>变化轴：<span id="vfValue">100</span></label>
    <input id="vfSlider" type="range" min="100" max="900" value="100" step="1">

    <label>预览文字</label>
    <textarea id="previewText">ABCDEabcde123 蒙古文 字体差值</textarea>

    <div id="vfPreview" class="preview">
      ABCDEabcde123 蒙古文 字体差值
    </div>
  </div>

  <div class="panel">
    <h2>2. 字体家族滑杆预览</h2>
    <div class="small">
      这里使用生成出来的 {len(font_files)} 个 TTF 作为一个字体家族，通过滑杆切换不同 Morph 样式。
      这个功能不依赖 Variable Font 合成，稳定可用。
    </div>

    <label>家族样式：<span id="familyStepLabel">1 / {len(font_files)}</span></label>
    <input id="familySlider" type="range" min="1" max="{len(font_files)}" value="1" step="1">

    <div id="familyPreview" class="preview">
      ABCDEabcde123 蒙古文 字体差值
    </div>
  </div>

  <div class="panel">
    <h2>3. 字体家族样式列表</h2>
    <div class="grid" id="familyGrid"></div>
  </div>

</div>

<script>
const HAS_VF = {str(has_vf).lower()};
const FONT_ITEMS = {names_js};

const vfSlider = document.getElementById("vfSlider");
const vfValue = document.getElementById("vfValue");
const vfPreview = document.getElementById("vfPreview");

const familySlider = document.getElementById("familySlider");
const familyStepLabel = document.getElementById("familyStepLabel");
const familyPreview = document.getElementById("familyPreview");

const previewText = document.getElementById("previewText");
const familyGrid = document.getElementById("familyGrid");

function updateText(){{
  const text = previewText.value || "ABCDEabcde123";
  vfPreview.textContent = text;
  familyPreview.textContent = text;

  document.querySelectorAll(".card .sample").forEach(el => {{
    el.textContent = text.slice(0, 12);
  }});
}}

function updateVF(){{
  const value = parseInt(vfSlider.value, 10);
  vfValue.textContent = value;

  if(HAS_VF){{
    vfPreview.style.fontFamily = "GeneratedVariableFont, sans-serif";
    vfPreview.style.fontVariationSettings = "'wght' " + value;
  }}else{{
    vfPreview.textContent = "Variable Font 未合成成功，请使用下方字体家族滑杆预览。";
    vfPreview.style.fontFamily = "Arial, sans-serif";
  }}
}}

function updateFamily(){{
  const idx = parseInt(familySlider.value, 10) - 1;
  const item = FONT_ITEMS[idx];

  familyStepLabel.textContent = (idx + 1) + " / " + FONT_ITEMS.length + " ｜ " + item.name;
  familyPreview.style.fontFamily = item.face + ", sans-serif";
}}

function buildGrid(){{
  familyGrid.innerHTML = "";

  FONT_ITEMS.forEach((item, idx) => {{
    const card = document.createElement("div");
    card.className = "card";

    const title = document.createElement("div");
    title.textContent = String(idx + 1).padStart(2, "0") + " ｜ " + item.name;
    title.style.fontWeight = "700";

    const sample = document.createElement("div");
    sample.className = "sample";
    sample.textContent = (previewText.value || "ABCDEabcde123").slice(0, 12);
    sample.style.fontFamily = item.face + ", sans-serif";

    const link = document.createElement("a");
    link.href = item.url;
    link.target = "_blank";
    link.textContent = "下载该样式";
    link.style.display = "inline-block";
    link.style.marginTop = "8px";
    link.style.color = "#2563eb";
    link.style.fontWeight = "700";
    link.style.textDecoration = "none";

    card.appendChild(title);
    card.appendChild(sample);
    card.appendChild(link);
    familyGrid.appendChild(card);
  }});
}}

vfSlider.addEventListener("input", updateVF);
familySlider.addEventListener("input", updateFamily);
previewText.addEventListener("input", function(){{
  updateText();
  updateVF();
  updateFamily();
}});

buildGrid();
updateText();
updateVF();
updateFamily();
</script>

</body>
</html>
'''
        return HTMLResponse(html)

    @app.middleware("http")
    async def unicode_variable_family_button_middleware(request, call_next):
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

        if "unicode-variable-family-buttons-v1" not in text:
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
            media_type="text/html"
        )
