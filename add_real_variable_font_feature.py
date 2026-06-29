from pathlib import Path
import re
import traceback
from html import escape

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fontTools.ttLib import TTFont


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_JOBS = BASE_DIR / "runtime_jobs"
VF_DIR = BASE_DIR / "runtime_variable_fonts"
VF_DIR.mkdir(exist_ok=True)


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


def family_name_from_fonts(fonts):
    if not fonts:
        return "FontMorphFamily"

    stem = fonts[0].stem

    for token in ["_Morph", "-Morph", "_Weight", "-Weight"]:
        if token in stem:
            return stem.split(token)[0]

    return re.sub(r"[_-]?\d+$", "", stem) or "FontMorphFamily"


def build_variable_font(job_id: str):
    """
    真实 Variable Font 合成。
    读取 runtime_jobs/{job_id}/outputs/*.ttf 作为 master，
    调用 fontTools.varLib 生成真正的 variable font。
    """
    job_id = safe_name(job_id)
    fonts = list_fonts(job_id)

    if len(fonts) < 2:
        return None, "至少需要 2 个已生成的 TTF/OTF 文件才能合成 Variable Font。"

    vf_job_dir = VF_DIR / job_id
    vf_job_dir.mkdir(parents=True, exist_ok=True)

    vf_path = vf_job_dir / f"{job_id}_RealVariable.ttf"
    ds_path = vf_job_dir / f"{job_id}.designspace"
    err_path = vf_job_dir / "variable_font_error.txt"

    if vf_path.exists():
        return vf_path, ""

    try:
        from fontTools.designspaceLib import DesignSpaceDocument, AxisDescriptor, SourceDescriptor
        from fontTools.varLib import build as varlib_build

        # 真实兼容性检查：glyphOrder 必须一致
        base = TTFont(str(fonts[0]))
        base_order = base.getGlyphOrder()
        base.close()

        for p in fonts[1:]:
            f = TTFont(str(p))
            order = f.getGlyphOrder()
            f.close()

            if order != base_order:
                raise RuntimeError(
                    f"glyphOrder 不一致，无法合成真正的 Variable Font：{p.name}"
                )

        family_name = family_name_from_fonts(fonts)

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


def get_preview_chars(job_id: str):
    chars_file = job_dir(job_id) / "chars.txt"

    if chars_file.exists():
        text = chars_file.read_text(encoding="utf-8", errors="ignore")
        text = "".join(ch for ch in text if not ch.isspace())
        text = "".join(dict.fromkeys(text))
        if text:
            return text[:16]

    return "ABCDEabcde123"


MAIN_PAGE_SCRIPT = r'''
<script id="real-variable-main-entry-v1">
(function(){
  if(window.__REAL_VARIABLE_MAIN_ENTRY_V1__) return;
  window.__REAL_VARIABLE_MAIN_ENTRY_V1__ = true;

  function updatePreviewButtonText(){
    const btn = document.getElementById("singleRealPreviewEntry");
    if(btn){
      btn.textContent = "查看全部生成结果预览（含可变字体）";
    }
  }

  document.addEventListener("DOMContentLoaded", updatePreviewButtonText);
  setInterval(updatePreviewButtonText, 1000);
})();
</script>
'''


def variable_panel_html(job_id: str):
    job_id = safe_name(job_id)
    default_text = get_preview_chars(job_id)

    return f'''
<style id="real-variable-font-style-v1">
@font-face {{
  font-family: "RealGeneratedVariableFont";
  src: url("/api/unicode/variable_font/{job_id}") format("truetype");
  font-weight: 100 900;
  font-style: normal;
}}

.variable-panel {{
  background: white;
  border-radius: 14px;
  padding: 20px;
  margin-bottom: 18px;
  box-shadow: 0 8px 28px rgba(0,0,0,.08);
}}

.variable-preview-box {{
  min-height: 260px;
  padding: 24px;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  background: #fafafa;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 150px;
  line-height: 1.15;
  overflow: auto;
  font-family: "RealGeneratedVariableFont", sans-serif;
  font-variation-settings: "wght" 100;
}}

.variable-preview-box.vertical {{
  writing-mode: vertical-lr;
  text-orientation: mixed;
  min-height: 480px;
  font-size: 110px;
}}

.variable-controls {{
  margin-top: 16px;
}}

.variable-controls input[type="range"] {{
  width: 100%;
}}

.variable-controls textarea {{
  width: 100%;
  min-height: 74px;
  box-sizing: border-box;
  padding: 12px;
  font-size: 16px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
}}

.variable-status {{
  margin-top: 10px;
  font-size: 13px;
  color: #555;
}}

.variable-error {{
  background: #fff7ed;
  color: #7c2d12;
  border: 1px solid #fed7aa;
  border-radius: 10px;
  padding: 12px;
  margin-top: 14px;
  white-space: pre-wrap;
  display: none;
}}

.variable-download {{
  display: inline-block;
  padding: 10px 14px;
  border-radius: 8px;
  background: #0f766e;
  color: white;
  text-decoration: none;
  font-weight: 700;
  margin-top: 10px;
}}
</style>

<div class="variable-panel" id="realVariableFontPanel">
  <h2>4. 真实 Variable Font 连续滑杆预览</h2>
  <div class="small">
    这里读取本次生成的 20 个 TTF，调用 fontTools.varLib 真实合成 Variable Font。
    滑杆使用 <code>font-variation-settings: "wght"</code> 连续调节，不是 20 帧切换。
  </div>

  <div class="variable-status">
    当前轴值：<span id="realVfValue">100</span> / 900
  </div>

  <div id="realVfPreview" class="variable-preview-box">{escape(default_text)}</div>

  <div class="variable-controls">
    <input id="realVfSlider" type="range" min="100" max="900" value="100" step="1">
  </div>

  <div class="variable-controls">
    <textarea id="realVfText">{escape(default_text)}</textarea>
  </div>

  <div class="variable-controls">
    <button type="button" id="realVfVerticalBtn">切换竖排</button>
    <button type="button" id="realVfResetBtn">恢复默认文字</button>
    <a class="variable-download" href="/api/unicode/variable_font/{job_id}" target="_blank">下载真实 Variable Font</a>
  </div>

  <div id="realVfError" class="variable-error"></div>
</div>

<script id="real-variable-font-preview-script-v1">
(function(){{
  const JOB_ID = {job_id!r};
  const DEFAULT_TEXT = {default_text!r};

  const slider = document.getElementById("realVfSlider");
  const valueBox = document.getElementById("realVfValue");
  const preview = document.getElementById("realVfPreview");
  const textBox = document.getElementById("realVfText");
  const verticalBtn = document.getElementById("realVfVerticalBtn");
  const resetBtn = document.getElementById("realVfResetBtn");
  const errorBox = document.getElementById("realVfError");

  let targetValue = 100;
  let currentValue = 100;
  let running = true;

  async function checkVariableFont(){{
    try {{
      const res = await fetch("/api/unicode/variable_font_status/" + encodeURIComponent(JOB_ID) + "?t=" + Date.now());
      const data = await res.json();

      if(!data.ok){{
        errorBox.style.display = "block";
        errorBox.textContent =
          "真实 Variable Font 合成失败。\\n" +
          "原因通常是生成出来的 master 字体轮廓结构不满足 varLib 兼容条件。\\n\\n" +
          (data.error || "");
      }}
    }} catch(err) {{
      errorBox.style.display = "block";
      errorBox.textContent = "检查 Variable Font 状态失败：" + String(err);
    }}
  }}

  function applyPreview(){{
    const text = textBox.value || DEFAULT_TEXT;
    preview.textContent = text;
  }}

  function smoothLoop(){{
    if(!running) return;

    // 平滑追踪目标值，避免滑动时一顿一顿
    currentValue += (targetValue - currentValue) * 0.18;

    if(Math.abs(currentValue - targetValue) < 0.15){{
      currentValue = targetValue;
    }}

    const v = Math.round(currentValue * 100) / 100;

    preview.style.fontVariationSettings = "'wght' " + v;
    valueBox.textContent = Math.round(v);

    requestAnimationFrame(smoothLoop);
  }}

  slider.addEventListener("input", function(){{
    targetValue = parseFloat(slider.value);
  }});

  textBox.addEventListener("input", applyPreview);

  verticalBtn.addEventListener("click", function(){{
    preview.classList.toggle("vertical");
  }});

  resetBtn.addEventListener("click", function(){{
    textBox.value = DEFAULT_TEXT;
    applyPreview();
    slider.value = "100";
    targetValue = 100;
  }});

  applyPreview();
  checkVariableFont();
  requestAnimationFrame(smoothLoop);
}})();
</script>
'''


def install_real_variable_font_feature(app):
    if getattr(app.state, "_real_variable_font_feature_v1", False):
        return

    app.state._real_variable_font_feature_v1 = True

    @app.get("/api/unicode/variable_font/{job_id}")
    def variable_font_download(job_id: str):
        vf_path, err = build_variable_font(job_id)

        if not vf_path or not vf_path.exists():
            return HTMLResponse(
                "<h2>真实 Variable Font 合成失败</h2>"
                "<p>系统已经读取本次生成的 TTF 并调用 fontTools.varLib。失败说明当前生成结果不满足可变字体 master 兼容条件。</p>"
                f"<pre style='white-space:pre-wrap;background:#111827;color:#ddd;padding:12px;border-radius:8px;'>{escape((err or '')[-8000:])}</pre>",
                status_code=500
            )

        return FileResponse(
            str(vf_path),
            filename=f"real_variable_font_{safe_name(job_id)}.ttf",
            media_type="font/ttf",
        )

    @app.get("/api/unicode/variable_font_status/{job_id}")
    def variable_font_status(job_id: str):
        vf_path, err = build_variable_font(job_id)

        if vf_path and vf_path.exists():
            return JSONResponse({
                "ok": True,
                "path": str(vf_path),
                "message": "Variable Font 合成成功。"
            })

        return JSONResponse({
            "ok": False,
            "error": err or "未知错误。"
        })

    @app.middleware("http")
    async def real_variable_font_middleware(request, call_next):
        response = await call_next(request)

        path = request.url.path
        content_type = response.headers.get("content-type", "")

        # 主页面：只修改现有单一预览按钮文案，不增加入口数量
        if path in ["/", "/unicode", "/unicode_async"] and "text/html" in content_type:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk

            text = body.decode("utf-8", errors="ignore")

            if "real-variable-main-entry-v1" not in text:
                if "</body>" in text:
                    text = text.replace("</body>", MAIN_PAGE_SCRIPT + "\n</body>", 1)
                else:
                    text += MAIN_PAGE_SCRIPT

            headers = dict(response.headers)
            headers.pop("content-length", None)

            return Response(
                content=text,
                status_code=response.status_code,
                headers=headers,
                media_type="text/html",
            )

        # 预览页面：在“查看全部生成结果预览”页面内部追加真实 VF 功能区
        if path.startswith("/api/unicode/real_preview/") and "text/html" in content_type:
            job_id = path.rstrip("/").split("/")[-1]

            body = b""
            async for chunk in response.body_iterator:
                body += chunk

            text = body.decode("utf-8", errors="ignore")

            if "real-variable-font-preview-script-v1" not in text:
                panel = variable_panel_html(job_id)

                # 优先插入到 container 结束前；找不到就插到 body 前
                marker = "\n</div>\n\n<script>"
                if marker in text:
                    text = text.replace(marker, "\n" + panel + marker, 1)
                elif "</body>" in text:
                    text = text.replace("</body>", panel + "\n</body>", 1)
                else:
                    text += panel

            headers = dict(response.headers)
            headers.pop("content-length", None)

            return Response(
                content=text,
                status_code=response.status_code,
                headers=headers,
                media_type="text/html",
            )

        return response
