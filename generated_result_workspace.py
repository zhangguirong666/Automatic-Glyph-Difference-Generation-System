from pathlib import Path
import re
import json
import zipfile
import traceback
from html import escape
from urllib.parse import quote

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fontTools.ttLib import TTFont


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_JOBS = BASE_DIR / "runtime_jobs"
OUTPUT_DIR = BASE_DIR / "output"
ASSET_DIR = BASE_DIR / "runtime_generated_workspace_assets"
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


def read_cmap_chars_from_font(font_path: Path, limit=600):
    chars = []

    try:
        font = TTFont(str(font_path), lazy=True)
        cps = set()

        for table in font["cmap"].tables:
            cps.update(table.cmap.keys())

        font.close()

        # 优先传统蒙古文区块
        mongolian = [cp for cp in sorted(cps) if 0x1800 <= cp <= 0x18AF]
        if mongolian:
            cps_sorted = mongolian
        else:
            cps_sorted = [cp for cp in sorted(cps) if cp >= 0x20 and cp not in [0xFEFF]]

        for cp in cps_sorted[:limit]:
            try:
                ch = chr(cp)
            except Exception:
                continue

            if ch.strip() == "" and cp != 0x20:
                continue

            chars.append({
                "ch": ch,
                "code": f"U+{cp:04X}",
            })

    except Exception:
        pass

    return chars


def read_generated_chars(scope: str, job_id: str, font_files):
    scope = safe_name(scope)
    job_id = safe_name(job_id)

    chars = []

    if scope == "unicode":
        chars_file = RUNTIME_JOBS / job_id / "chars.txt"
        if chars_file.exists():
            text = chars_file.read_text(encoding="utf-8", errors="ignore")
            seen = set()
            for ch in text:
                if ch in seen:
                    continue
                seen.add(ch)
                cp = ord(ch)
                if ch.strip() == "" and cp != 0x20:
                    continue
                chars.append({
                    "ch": ch,
                    "code": f"U+{cp:04X}",
                })

    if not chars and font_files:
        chars = read_cmap_chars_from_font(font_files[0], limit=600)

    return chars


def make_zip(scope: str, job_id: str):
    out_dir = resolve_output_dir(scope, job_id)
    if not out_dir:
        return None, "没有找到已生成的字体输出目录。"

    fonts = list_fonts(out_dir)
    if not fonts:
        return None, "输出目录中没有 TTF/OTF 字体文件。"

    zpath = asset_dir(scope, job_id) / f"{safe_name(scope)}_{safe_name(job_id)}_all_fonts.zip"

    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in fonts:
            zf.write(p, arcname=p.name)

    return zpath, ""


def family_name_from_fonts(font_files):
    if not font_files:
        return "GeneratedFontFamily"

    stem = font_files[0].stem

    for token in ["_Morph", "-Morph", "_Weight", "-Weight"]:
        if token in stem:
            return stem.split(token)[0]

    return re.sub(r"[_-]?\d+$", "", stem) or "GeneratedFontFamily"


def try_build_variable_font(scope: str, job_id: str):
    """
    真实 Variable Font 合成。
    它直接使用已经生成的 TTF 作为 masters。
    如果 varLib 判断轮廓/点结构不兼容，会返回真实错误，不伪造成功。
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
        from fontTools.ttLib import TTFont
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


FRONTEND_INJECT = r'''
<script id="generated-result-workspace-entry-v1">
(function(){
  if(window.__GENERATED_RESULT_WORKSPACE_ENTRY_V1__) return;
  window.__GENERATED_RESULT_WORKSPACE_ENTRY_V1__ = true;

  function findUnicodeJobId(){
    const links = Array.from(document.querySelectorAll("a[href]"));

    for(const a of links){
      const href = a.getAttribute("href") || "";

      let m = href.match(/\/api\/unicode\/download\/([^\/?#]+)\//);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/download_zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/real_family\/zip\/unicode\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);
    }

    return "";
  }

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

  function hideOldTemplateButtons(){
    const badTexts = [
      "滑杆实时预览 / 字体家族",
      "下载可变字体 VF",
      "下载真实 Variable Font"
    ];

    Array.from(document.querySelectorAll("a,button")).forEach(el => {
      const t = (el.innerText || el.textContent || "").trim();
      if(badTexts.some(x => t === x || t.includes(x))){
        if(!el.closest("#generatedWorkspaceEntryBox") && !el.closest("#foundryWorkspaceEntryBox")){
          el.style.display = "none";
        }
      }
    });
  }

  function installUnicodeEntry(){
    const jobId = findUnicodeJobId();
    if(!jobId) return;

    if(document.getElementById("generatedWorkspaceEntryBox")) return;

    const host =
      document.getElementById("unicodeZipPreviewActionBar") ||
      document.getElementById("outputs") ||
      document.getElementById("unicodeBridgeOutputs") ||
      document.querySelector(".outputs") ||
      document.body;

    const box = document.createElement("div");
    box.id = "generatedWorkspaceEntryBox";
    box.style.cssText = "margin-top:12px;padding:12px;border:1px solid #dbeafe;background:#eff6ff;border-radius:10px;";

    const title = document.createElement("div");
    title.textContent = "上半部分：基于本次已生成字体的真实工作台";
    title.style.cssText = "font-weight:700;margin-bottom:8px;";

    const note = document.createElement("div");
    note.textContent = "读取本次 runtime_jobs 输出的 20 个 TTF，不使用模板。";
    note.style.cssText = "font-size:12px;color:#555;margin-bottom:8px;";

    box.appendChild(title);
    box.appendChild(note);
    box.appendChild(btn("打开生成结果工作台", "/api/generated_workspace/page/unicode/" + encodeURIComponent(jobId), "#7c3aed"));
    box.appendChild(btn("下载全部 TTF 压缩包", "/api/generated_workspace/zip/unicode/" + encodeURIComponent(jobId), "#2563eb"));
    box.appendChild(btn("下载真实 Variable Font", "/api/generated_workspace/vf/unicode/" + encodeURIComponent(jobId), "#0f766e"));

    host.appendChild(box);
  }

  function installFoundryEntry(){
    if(document.getElementById("foundryWorkspaceEntryBox")) return;

    const foundryBtn =
      document.getElementById("foundryRulesBuildBtn") ||
      Array.from(document.querySelectorAll("button")).find(b => (b.innerText || "").includes("按所选字体公司规则生成"));

    if(!foundryBtn) return;

    const host = foundryBtn.closest("div") || foundryBtn.parentElement || document.body;

    const box = document.createElement("div");
    box.id = "foundryWorkspaceEntryBox";
    box.style.cssText = "margin-top:12px;padding:12px;border:1px solid #dbeafe;background:#eff6ff;border-radius:10px;";

    const title = document.createElement("div");
    title.textContent = "下半部分：蒙古文公司规则生成结果真实工作台";
    title.style.cssText = "font-weight:700;margin-bottom:8px;";

    const note = document.createElement("div");
    note.textContent = "先完成“按所选字体公司规则生成”，再打开这里。它读取 output/mongolian_gb_ttf_steps/ 中真实生成的 TTF。";
    note.style.cssText = "font-size:12px;color:#555;margin-bottom:8px;";

    box.appendChild(title);
    box.appendChild(note);
    box.appendChild(btn("打开蒙古文生成结果工作台", "/api/generated_workspace/page/foundry/latest", "#7c3aed"));
    box.appendChild(btn("下载蒙古文全部 TTF 压缩包", "/api/generated_workspace/zip/foundry/latest", "#2563eb"));
    box.appendChild(btn("下载蒙古文真实 Variable Font", "/api/generated_workspace/vf/foundry/latest", "#0f766e"));

    host.appendChild(box);
  }

  function run(){
    hideOldTemplateButtons();
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


def install_generated_result_workspace(app):
    if getattr(app.state, "_generated_result_workspace_v1", False):
        return

    app.state._generated_result_workspace_v1 = True

    @app.get("/api/generated_workspace/zip/{scope}/{job_id}")
    def download_zip(scope: str, job_id: str):
        zpath, err = make_zip(scope, job_id)

        if not zpath or not zpath.exists():
            return HTMLResponse(f"<h2>压缩包生成失败</h2><pre>{escape(err)}</pre>", status_code=404)

        return FileResponse(
            str(zpath),
            filename=f"{safe_name(scope)}_{safe_name(job_id)}_all_ttf.zip",
            media_type="application/zip"
        )

    @app.get("/api/generated_workspace/vf/{scope}/{job_id}")
    def download_vf(scope: str, job_id: str):
        vf_path, err = try_build_variable_font(scope, job_id)

        if not vf_path or not vf_path.exists():
            return HTMLResponse(
                "<h2>真实 Variable Font 合成失败</h2>"
                "<p>系统已经读取已生成 TTF 并调用 fontTools.varLib。失败通常说明轮廓点结构或 master 兼容性不满足 VF 要求。</p>"
                "<p>字体家族列表和滑杆切换仍然可用，因为它直接加载真实生成的 20 个 TTF。</p>"
                f"<pre style='white-space:pre-wrap;background:#111827;color:#ddd;padding:12px;border-radius:8px;'>{escape((err or '')[-7000:])}</pre>",
                status_code=500
            )

        return FileResponse(
            str(vf_path),
            filename=f"{safe_name(scope)}_{safe_name(job_id)}_Variable.ttf",
            media_type="font/ttf"
        )

    @app.get("/api/generated_workspace/font/{scope}/{job_id}/{filename}")
    def font_file(scope: str, job_id: str, filename: str):
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return JSONResponse({"error": "没有找到输出目录。"}, status_code=404)

        target = out_dir / Path(filename).name

        if not target.exists():
            return JSONResponse({"error": "字体文件不存在。"}, status_code=404)

        return FileResponse(str(target), filename=target.name, media_type="font/ttf")

    @app.get("/api/generated_workspace/page/{scope}/{job_id}")
    def workspace_page(scope: str, job_id: str):
        out_dir = resolve_output_dir(scope, job_id)

        if not out_dir:
            return HTMLResponse(
                "<h2>没有找到已生成的字体输出目录</h2>"
                "<p>上半部分请先完成生成；下半部分请先完成蒙古文公司规则生成。</p>",
                status_code=404
            )

        font_files = list_fonts(out_dir)

        if not font_files:
            return HTMLResponse("<h2>输出目录中没有 TTF/OTF 文件。</h2>", status_code=404)

        chars = read_generated_chars(scope, job_id, font_files)
        family_name = family_name_from_fonts(font_files)

        vf_path, vf_err = try_build_variable_font(scope, job_id)
        has_vf = bool(vf_path and vf_path.exists())

        font_items = []
        faces = []

        for i, p in enumerate(font_files):
            face = f"GeneratedStep{i+1:02d}"
            fname = p.name
            url = f"/api/generated_workspace/font/{safe_name(scope)}/{safe_name(job_id)}/{quote(fname)}"

            faces.append(
                f"@font-face{{font-family:'{face}';src:url('{url}') format('truetype');font-weight:400;font-style:normal;}}"
            )

            font_items.append({
                "index": i,
                "face": face,
                "name": fname,
                "url": url,
            })

        vf_face = ""
        if has_vf:
            vf_face = (
                "@font-face{"
                "font-family:'GeneratedRealVF';"
                f"src:url('/api/generated_workspace/vf/{safe_name(scope)}/{safe_name(job_id)}') format('truetype');"
                "font-weight:100 900;"
                "font-style:normal;"
                "}"
            )

        default_text = "".join(item["ch"] for item in chars[:12]) or "ABCDEabcde123"
        if scope == "foundry" and chars:
            default_text = "".join(item["ch"] for item in chars[:20])

        chars_json = json.dumps(chars, ensure_ascii=False)
        fonts_json = json.dumps(font_items, ensure_ascii=False)

        vf_warning = ""
        if not has_vf:
            vf_warning = f"""
<div class="warn">
  <b>Variable Font 未合成成功。</b>
  <div>系统已经根据当前生成的 TTF 真实尝试合成，不是模板。失败不影响左侧字体列表和字体家族滑杆。</div>
  <details>
    <summary>查看 fontTools.varLib 真实错误</summary>
    <pre>{escape((vf_err or '')[-7000:])}</pre>
  </details>
</div>
"""

        html = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>生成结果工作台</title>
<style>
{vf_face}
{chr(10).join(faces)}

body {{
  margin:0;
  background:#f3f4f6;
  color:#111;
  font-family:Arial,"Microsoft YaHei",sans-serif;
}}

.header {{
  background:#111827;
  color:white;
  padding:22px 32px;
}}

.header h1 {{
  margin:0 0 6px 0;
}}

.layout {{
  display:grid;
  grid-template-columns:320px 1fr;
  gap:18px;
  max-width:1380px;
  margin:22px auto;
  padding:0 20px;
}}

.sidebar, .main {{
  background:white;
  border-radius:14px;
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

.font-btn {{
  width:100%;
  text-align:left;
  padding:10px;
  margin:4px 0;
  border:1px solid #e5e7eb;
  background:#f9fafb;
  border-radius:8px;
  cursor:pointer;
  font-size:13px;
}}

.font-btn.active {{
  background:#2563eb;
  color:white;
  border-color:#2563eb;
}}

.panel {{
  margin-top:18px;
  border:1px solid #e5e7eb;
  border-radius:12px;
  padding:16px;
}}

textarea {{
  width:100%;
  min-height:80px;
  box-sizing:border-box;
  padding:12px;
  border:1px solid #d1d5db;
  border-radius:8px;
  font-size:16px;
}}

input[type=range] {{
  width:100%;
}}

.preview {{
  margin-top:14px;
  min-height:190px;
  padding:26px;
  border:1px solid #e5e7eb;
  border-radius:12px;
  background:#fafafa;
  font-size:76px;
  line-height:1.25;
  overflow:auto;
}}

.preview.vertical {{
  writing-mode: vertical-lr;
  text-orientation: mixed;
  min-height:360px;
}}

.char-grid {{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(76px,1fr));
  gap:8px;
  max-height:260px;
  overflow:auto;
  margin-top:10px;
}}

.char-btn {{
  border:1px solid #e5e7eb;
  background:#fff;
  border-radius:8px;
  padding:8px 4px;
  cursor:pointer;
  text-align:center;
}}

.char-btn .ch {{
  font-size:28px;
  display:block;
}}

.char-btn .code {{
  font-size:11px;
  color:#666;
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

.warn {{
  background:#fff7ed;
  border:1px solid #fed7aa;
  color:#7c2d12;
  border-radius:10px;
  padding:12px;
  margin-top:14px;
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

.small {{
  font-size:13px;
  color:#555;
}}

.row {{
  display:flex;
  gap:12px;
  flex-wrap:wrap;
  align-items:center;
}}

label {{
  font-weight:700;
}}
</style>
</head>
<body>

<div class="header">
  <h1>生成结果工作台</h1>
  <div>范围：{escape(scope)} ｜ 任务：{escape(job_id)} ｜ 字体家族：{escape(family_name)} ｜ 真实字体文件：{len(font_files)} 个 ｜ 输出目录：{escape(str(out_dir.relative_to(BASE_DIR) if out_dir.is_relative_to(BASE_DIR) else out_dir))}</div>
</div>

<div class="layout">
  <aside class="sidebar">
    <h2>字体列表</h2>
    <div class="small">这里读取的是第一步已经生成出来的真实 TTF 文件。点击哪个，右侧就切换到哪个字体。</div>
    <div id="fontList"></div>
  </aside>

  <main class="main">
    <div class="actions">
      <a class="blue" href="/api/generated_workspace/zip/{escape(safe_name(scope))}/{escape(safe_name(job_id))}" target="_blank">下载全部 TTF 压缩包</a>
      <a class="green" href="/api/generated_workspace/vf/{escape(safe_name(scope))}/{escape(safe_name(job_id))}" target="_blank">下载真实 Variable Font</a>
    </div>

    {vf_warning}

    <div class="panel">
      <h2>1. 字体家族滑杆</h2>
      <div class="small">这个滑杆不是模板，它直接在 {len(font_files)} 个真实 TTF 文件之间切换。</div>
      <label>当前字体：<span id="currentFontName"></span></label>
      <input id="familySlider" type="range" min="0" max="{len(font_files)-1}" value="0" step="1">
    </div>

    <div class="panel">
      <h2>2. 当前生成字符列表</h2>
      <div class="small">字符从本次生成的 chars.txt 或字体 cmap 读取。蒙古文整套生成时，这里会显示对应的蒙古文字符。</div>
      <div class="row" style="margin-top:10px;">
        <label><input type="checkbox" id="verticalMode"> 蒙古文竖排预览</label>
        <button type="button" onclick="clearPreviewText()">清空预览文字</button>
      </div>
      <div id="charGrid" class="char-grid"></div>
    </div>

    <div class="panel">
      <h2>3. 预览内容</h2>
      <textarea id="previewText">{escape(default_text)}</textarea>
      <div id="familyPreview" class="preview">{escape(default_text)}</div>
    </div>

    <div class="panel">
      <h2>4. Variable Font 连续滑杆</h2>
      <div class="small">如果真实 VF 合成成功，这里用同一个 Variable Font 做连续变化；如果失败，说明当前 master 不满足 VF 兼容条件。</div>
      <label>Weight：<span id="vfValue">100</span></label>
      <input id="vfSlider" type="range" min="100" max="900" value="100" step="1">
      <div id="vfPreview" class="preview">{escape(default_text)}</div>
    </div>
  </main>
</div>

<script>
const FONT_ITEMS = {fonts_json};
const CHAR_ITEMS = {chars_json};
const HAS_VF = {str(has_vf).lower()};

let currentIndex = 0;

const fontList = document.getElementById("fontList");
const familySlider = document.getElementById("familySlider");
const currentFontName = document.getElementById("currentFontName");
const previewText = document.getElementById("previewText");
const familyPreview = document.getElementById("familyPreview");
const charGrid = document.getElementById("charGrid");
const verticalMode = document.getElementById("verticalMode");
const vfSlider = document.getElementById("vfSlider");
const vfValue = document.getElementById("vfValue");
const vfPreview = document.getElementById("vfPreview");

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
  familyPreview.textContent = previewText.value || "";

  Array.from(document.querySelectorAll(".font-btn")).forEach((b, i) => {{
    b.classList.toggle("active", i === currentIndex);
  }});
}}

function renderCharGrid(){{
  charGrid.innerHTML = "";

  CHAR_ITEMS.forEach(item => {{
    const b = document.createElement("button");
    b.className = "char-btn";
    b.innerHTML = "<span class='ch'>" + item.ch + "</span><span class='code'>" + item.code + "</span>";
    b.onclick = () => {{
      previewText.value += item.ch;
      updatePreviews();
    }};
    charGrid.appendChild(b);
  }});
}}

function updatePreviews(){{
  const txt = previewText.value || "";
  familyPreview.textContent = txt;
  vfPreview.textContent = txt;

  if(verticalMode.checked){{
    familyPreview.classList.add("vertical");
    vfPreview.classList.add("vertical");
  }}else{{
    familyPreview.classList.remove("vertical");
    vfPreview.classList.remove("vertical");
  }}

  if(HAS_VF){{
    vfPreview.style.fontFamily = "GeneratedRealVF, sans-serif";
    vfPreview.style.fontVariationSettings = "'wght' " + parseInt(vfSlider.value, 10);
  }}else{{
    vfPreview.style.fontFamily = "Arial, sans-serif";
    vfPreview.textContent = "Variable Font 未合成成功；请使用上面的真实 TTF 字体列表和字体家族滑杆。";
  }}
}}

function clearPreviewText(){{
  previewText.value = "";
  updatePreviews();
}}

familySlider.addEventListener("input", () => {{
  setFontIndex(parseInt(familySlider.value, 10));
}});

previewText.addEventListener("input", updatePreviews);

verticalMode.addEventListener("change", updatePreviews);

vfSlider.addEventListener("input", () => {{
  vfValue.textContent = vfSlider.value;
  updatePreviews();
}});

renderFontList();
renderCharGrid();
setFontIndex(0);
updatePreviews();
</script>

</body>
</html>
'''
        return HTMLResponse(html)

    @app.middleware("http")
    async def inject_workspace_entry(request, call_next):
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

        if "generated-result-workspace-entry-v1" not in text:
            if "</body>" in text:
                text = text.replace("</body>", FRONTEND_INJECT + "\n</body>", 1)
            else:
                text += FRONTEND_INJECT

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html"
        )
