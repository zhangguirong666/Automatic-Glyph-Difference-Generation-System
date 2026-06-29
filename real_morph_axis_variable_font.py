from pathlib import Path
import re
import traceback
from html import escape

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fontTools.ttLib import TTFont


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_JOBS = BASE_DIR / "runtime_jobs"
VF_DIR = BASE_DIR / "runtime_morph_axis_vf"
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


def read_preview_text(job_id: str):
    chars_file = job_dir(job_id) / "chars.txt"

    if chars_file.exists():
        text = chars_file.read_text(encoding="utf-8", errors="ignore")
        text = "".join(ch for ch in text if not ch.isspace())
        text = "".join(dict.fromkeys(text))
        if text:
            return text[:16]

    return "ABCDEabcde123"


def build_morf_variable_font(job_id: str):
    """
    真正的 Variable Font：
    - master 来源：runtime_jobs/{job_id}/outputs/*.ttf
    - 轴：MORF
    - CSS 调用：font-variation-settings: "MORF" value
    """
    job_id = safe_name(job_id)
    fonts = list_fonts(job_id)

    if len(fonts) < 2:
        return None, "至少需要 2 个已生成的 TTF/OTF 文件才能合成 Variable Font。"

    vf_job_dir = VF_DIR / job_id
    vf_job_dir.mkdir(parents=True, exist_ok=True)

    vf_path = vf_job_dir / f"{job_id}_MorphAxisVariable.ttf"
    ds_path = vf_job_dir / f"{job_id}_MorphAxis.designspace"
    err_path = vf_job_dir / "morf_axis_vf_error.txt"

    if vf_path.exists():
        return vf_path, ""

    try:
        from fontTools.designspaceLib import DesignSpaceDocument, AxisDescriptor, SourceDescriptor
        from fontTools.varLib import build as varlib_build

        # 真实兼容性检查
        base = TTFont(str(fonts[0]))
        base_order = base.getGlyphOrder()
        base.close()

        for p in fonts[1:]:
            f = TTFont(str(p))
            order = f.getGlyphOrder()
            f.close()

            if order != base_order:
                raise RuntimeError(f"glyphOrder 不一致，无法合成真正 Variable Font：{p.name}")

        family_name = family_name_from_fonts(fonts)

        doc = DesignSpaceDocument()

        axis = AxisDescriptor()
        axis.name = "Morph"
        axis.tag = "MORF"
        axis.minimum = 0
        axis.default = 0
        axis.maximum = 1000
        doc.addAxis(axis)

        n = len(fonts)

        for i, p in enumerate(fonts):
            value = 0 if n == 1 else int(round(i * 1000 / (n - 1)))

            src = SourceDescriptor()
            src.path = str(p.resolve())
            src.name = f"morph_master_{i:02d}"
            src.familyName = family_name
            src.styleName = f"Morph {value}"
            src.location = {"Morph": value}

            if i == 0:
                src.copyInfo = True
                src.copyFeatures = True
                src.copyLib = True
                src.copyGroups = True

            doc.addSource(src)

        doc.write(str(ds_path))

        built = varlib_build(str(ds_path))
        vf = built[0] if isinstance(built, tuple) else built

        # 避免缓存混乱，写入明确 name
        try:
            name_table = vf["name"]
            for name_id, value in [
                (1, family_name + " Variable"),
                (2, "Regular"),
                (4, family_name + " Morph Variable"),
                (6, family_name.replace(" ", "") + "-MorphVariable"),
            ]:
                name_table.setName(value, name_id, 3, 1, 0x409)
                name_table.setName(value, name_id, 1, 0, 0)
        except Exception:
            pass

        vf.save(str(vf_path))

        if err_path.exists():
            err_path.unlink()

        return vf_path, ""

    except Exception:
        err = traceback.format_exc()
        err_path.write_text(err, encoding="utf-8")
        return None, err


def variable_panel_html(job_id: str):
    job_id = safe_name(job_id)
    default_text = read_preview_text(job_id)

    return f'''
<style id="morf-axis-vf-style-v1">
@font-face {{
  font-family: "RealMorphAxisVF";
  src: url("/api/unicode/morf_axis_variable_font/{job_id}?v=1") format("truetype");
  font-weight: normal;
  font-style: normal;
}}

.morf-vf-panel {{
  background: white;
  border-radius: 14px;
  padding: 20px;
  margin-bottom: 18px;
  box-shadow: 0 8px 28px rgba(0,0,0,.08);
}}

.morf-vf-preview {{
  min-height: 300px;
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
  font-family: "RealMorphAxisVF", sans-serif;
  font-variation-settings: "MORF" 0;
  will-change: font-variation-settings;
}}

.morf-vf-preview.vertical {{
  writing-mode: vertical-lr;
  text-orientation: mixed;
  min-height: 500px;
  font-size: 110px;
}}

.morf-vf-controls {{
  margin-top: 16px;
}}

.morf-vf-controls input[type="range"] {{
  width: 100%;
}}

.morf-vf-controls textarea {{
  width: 100%;
  min-height: 74px;
  box-sizing: border-box;
  padding: 12px;
  font-size: 16px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
}}

.morf-vf-status {{
  margin: 10px 0;
  font-size: 13px;
  color: #555;
  font-family: monospace;
}}

.morf-vf-error {{
  background: #fff7ed;
  color: #7c2d12;
  border: 1px solid #fed7aa;
  border-radius: 10px;
  padding: 12px;
  margin-top: 14px;
  white-space: pre-wrap;
  display: none;
}}

.morf-vf-download {{
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

<div class="morf-vf-panel" id="morfAxisVariablePanel">
  <h2>4. 真实 MORF 轴可变字体预览</h2>
  <div class="small">
    这里不是 SVG 帧切换，也不是普通 weight 轴。
    系统会读取你本次生成的 20 个 TTF，真实合成一个带 <code>MORF</code> 轴的 Variable Font。
  </div>

  <div class="morf-vf-status">
    MORF：<span id="morfAxisValue">0</span> / 1000
  </div>

  <div id="morfAxisPreview" class="morf-vf-preview">{escape(default_text)}</div>

  <div class="morf-vf-controls">
    <input id="morfAxisSlider" type="range" min="0" max="1000" value="0" step="1">
  </div>

  <div class="morf-vf-controls">
    <textarea id="morfAxisText">{escape(default_text)}</textarea>
  </div>

  <div class="morf-vf-controls">
    <button type="button" id="morfAxisPlayBtn">播放</button>
    <button type="button" id="morfAxisVerticalBtn">切换竖排</button>
    <button type="button" id="morfAxisResetBtn">恢复默认文字</button>
    <a class="morf-vf-download" href="/api/unicode/morf_axis_variable_font/{job_id}" target="_blank">下载真实 MORF 可变字体</a>
  </div>

  <div id="morfAxisError" class="morf-vf-error"></div>
</div>

<script id="morf-axis-vf-script-v1">
(function(){{
  const JOB_ID = {job_id!r};
  const DEFAULT_TEXT = {default_text!r};

  const slider = document.getElementById("morfAxisSlider");
  const valueBox = document.getElementById("morfAxisValue");
  const preview = document.getElementById("morfAxisPreview");
  const textBox = document.getElementById("morfAxisText");
  const playBtn = document.getElementById("morfAxisPlayBtn");
  const verticalBtn = document.getElementById("morfAxisVerticalBtn");
  const resetBtn = document.getElementById("morfAxisResetBtn");
  const errorBox = document.getElementById("morfAxisError");

  let target = 0;
  let current = 0;
  let playing = false;
  let direction = 1;

  async function checkVF(){{
    try {{
      const res = await fetch("/api/unicode/morf_axis_variable_status/" + encodeURIComponent(JOB_ID) + "?t=" + Date.now());
      const data = await res.json();

      if(!data.ok){{
        errorBox.style.display = "block";
        errorBox.textContent =
          "真实 MORF Variable Font 合成失败。\\n" +
          "说明当前生成出来的 20 个 TTF 不满足 fontTools.varLib 的 master 兼容条件。\\n\\n" +
          (data.error || "");
      }}
    }} catch(err) {{
      errorBox.style.display = "block";
      errorBox.textContent = "检查 MORF Variable Font 状态失败：" + String(err);
    }}
  }}

  function applyText(){{
    preview.textContent = textBox.value || DEFAULT_TEXT;
  }}

  function frame(){{
    if(playing){{
      target += direction * 4.2;
      if(target >= 1000){{
        target = 1000;
        direction = -1;
      }}
      if(target <= 0){{
        target = 0;
        direction = 1;
      }}
      slider.value = String(Math.round(target));
    }}

    // 平滑追踪，避免卡顿生硬
    current += (target - current) * 0.16;

    if(Math.abs(current - target) < 0.1){{
      current = target;
    }}

    const v = Math.round(current * 100) / 100;
    preview.style.fontVariationSettings = '"MORF" ' + v;
    valueBox.textContent = Math.round(v);

    requestAnimationFrame(frame);
  }}

  slider.addEventListener("input", function(){{
    target = parseFloat(slider.value);
  }});

  textBox.addEventListener("input", applyText);

  playBtn.addEventListener("click", function(){{
    playing = !playing;
    playBtn.textContent = playing ? "暂停" : "播放";
  }});

  verticalBtn.addEventListener("click", function(){{
    preview.classList.toggle("vertical");
  }});

  resetBtn.addEventListener("click", function(){{
    textBox.value = DEFAULT_TEXT;
    applyText();
    target = 0;
    current = 0;
    slider.value = "0";
    preview.style.fontVariationSettings = '"MORF" 0';
  }});

  applyText();
  checkVF();
  requestAnimationFrame(frame);
}})();
</script>
'''


MAIN_SCRIPT = r'''
<script id="morf-axis-main-entry-v1">
(function(){
  if(window.__MORF_AXIS_MAIN_ENTRY_V1__) return;
  window.__MORF_AXIS_MAIN_ENTRY_V1__ = true;

  function updateText(){
    const btn = document.getElementById("singleRealPreviewEntry");
    if(btn){
      btn.textContent = "查看全部生成结果预览（含真实 MORF 可变字体）";
    }
  }

  document.addEventListener("DOMContentLoaded", updateText);
  setInterval(updateText, 1000);
})();
</script>
'''


def install_real_morph_axis_variable_font(app):
    if getattr(app.state, "_real_morph_axis_vf_v1", False):
        return

    app.state._real_morph_axis_vf_v1 = True

    @app.get("/api/unicode/morf_axis_variable_font/{job_id}")
    def morf_axis_variable_font(job_id: str):
        vf_path, err = build_morf_variable_font(job_id)

        if not vf_path or not vf_path.exists():
            return HTMLResponse(
                "<h2>真实 MORF Variable Font 合成失败</h2>"
                "<p>系统已经读取本次生成的 TTF 并调用 fontTools.varLib。失败说明当前生成结果不满足可变字体 master 兼容条件。</p>"
                f"<pre style='white-space:pre-wrap;background:#111827;color:#ddd;padding:12px;border-radius:8px;'>{escape((err or '')[-10000:])}</pre>",
                status_code=500
            )

        return FileResponse(
            str(vf_path),
            filename=f"real_morf_variable_font_{safe_name(job_id)}.ttf",
            media_type="font/ttf",
        )

    @app.get("/api/unicode/morf_axis_variable_status/{job_id}")
    def morf_axis_variable_status(job_id: str):
        vf_path, err = build_morf_variable_font(job_id)

        if vf_path and vf_path.exists():
            return JSONResponse({
                "ok": True,
                "message": "真实 MORF Variable Font 合成成功。",
                "path": str(vf_path),
            })

        return JSONResponse({
            "ok": False,
            "error": err or "未知错误。",
        })

    @app.middleware("http")
    async def morf_axis_vf_middleware(request, call_next):
        response = await call_next(request)

        path = request.url.path
        content_type = response.headers.get("content-type", "")

        if path in ["/", "/unicode", "/unicode_async"] and "text/html" in content_type:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk

            text = body.decode("utf-8", errors="ignore")

            if "morf-axis-main-entry-v1" not in text:
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

        if path.startswith("/api/unicode/real_preview/") and "text/html" in content_type:
            job_id = path.rstrip("/").split("/")[-1]

            body = b""
            async for chunk in response.body_iterator:
                body += chunk

            text = body.decode("utf-8", errors="ignore")

            # 移除之前错误的 wght 面板或 smooth 面板残留
            text = text.replace("真实 Variable Font 连续滑杆预览", "旧 Variable Font 预览已替换")
            text = text.replace("连续可变字体预览", "旧连续预览已替换")

            if "morf-axis-vf-script-v1" not in text:
                panel = variable_panel_html(job_id)

                if "</body>" in text:
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
