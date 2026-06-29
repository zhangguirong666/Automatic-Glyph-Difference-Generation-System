import sys
import uuid
import shutil
import zipfile
import subprocess
from pathlib import Path
from threading import Thread

from fastapi import UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse, Response


BASE_DIR = Path(__file__).resolve().parent
JOB_DIR = BASE_DIR / "runtime_top_foundry_jobs"
OUT_DIR = BASE_DIR / "output" / "mongolian_gb_ttf_steps"
JOB_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

JOBS = {}


def safe_name(s: str):
    s = str(s)
    keep = []
    for ch in s:
        if ch.isalnum() or ch in "._-":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:120] or "file"


def update_job(job_id, **kw):
    if job_id in JOBS:
        JOBS[job_id].update(kw)


def make_zip():
    files = sorted(OUT_DIR.glob("*.ttf")) + sorted(OUT_DIR.glob("*.otf"))
    zip_path = OUT_DIR.parent / "mongolian_gb_ttf_steps_package.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.name)

    return zip_path


def build_command(script, font_a, font_b, steps, company, out_dir):
    """
    根据脚本 --help 自动选择参数。这里是真实调用现有蒙古文生成脚本，不是演示。
    """
    help_text = ""
    try:
        r = subprocess.run(
            [sys.executable, str(script), "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
        help_text = r.stdout or ""
    except Exception:
        help_text = ""

    cmd = [sys.executable, str(script)]

    # 字体 A/B 参数
    if "--font-a" in help_text:
        cmd += ["--font-a", str(font_a)]
    elif "--font_a" in help_text:
        cmd += ["--font_a", str(font_a)]
    else:
        cmd += ["--font-a", str(font_a)]

    if "--font-b" in help_text:
        cmd += ["--font-b", str(font_b)]
    elif "--font_b" in help_text:
        cmd += ["--font_b", str(font_b)]
    else:
        cmd += ["--font-b", str(font_b)]

    # 步数
    if "--steps" in help_text:
        cmd += ["--steps", str(steps)]

    # 输出目录
    if "--out-dir" in help_text:
        cmd += ["--out-dir", str(out_dir)]
    elif "--out_dir" in help_text:
        cmd += ["--out_dir", str(out_dir)]

    # 字体公司 / 规则参数
    if "--company" in help_text:
        cmd += ["--company", company]
    elif "--foundry" in help_text:
        cmd += ["--foundry", company]
    elif "--rule" in help_text:
        cmd += ["--rule", company]
    elif "--ruleset" in help_text:
        cmd += ["--ruleset", company]

    return cmd, help_text


def run_foundry_job(job_id, font_a, font_b, steps, company):
    try:
        update_job(job_id, status="running", progress=5, message="正在准备蒙古文字体公司规则生成...")

        script = BASE_DIR / "scripts" / "build_mongolian_gb_ttf_steps.py"

        if not script.exists():
            raise RuntimeError(f"找不到真实蒙古文生成脚本：{script}")

        # 清理旧输出，避免新旧结果混在一起
        for p in OUT_DIR.glob("*.ttf"):
            p.unlink()
        for p in OUT_DIR.glob("*.otf"):
            p.unlink()

        cmd, help_text = build_command(
            script=script,
            font_a=font_a,
            font_b=font_b,
            steps=steps,
            company=company,
            out_dir=OUT_DIR,
        )

        job_path = JOB_DIR / job_id
        log_path = job_path / "run.log"

        update_job(
            job_id,
            progress=10,
            message="开始调用真实蒙古文公司规则生成脚本。",
            command=" ".join(cmd),
        )

        with open(log_path, "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            line_count = 0

            for line in proc.stdout:
                line_count += 1
                lf.write(line)
                lf.flush()

                low = line.lower()
                progress = JOBS[job_id].get("progress", 10)

                for i in range(1, int(steps) + 1):
                    if f"step {i:02d}" in low or f"step_{i:02d}" in low or f"step {i}" in low:
                        progress = max(progress, int(10 + i / int(steps) * 80))

                if line_count % 5 == 0:
                    progress = min(92, progress + 1)

                update_job(job_id, progress=progress, message=line.strip()[-500:] if line.strip() else "生成中...")

            ret = proc.wait()

        if ret != 0:
            log_text = log_path.read_text(encoding="utf-8", errors="ignore")[-5000:]
            raise RuntimeError("蒙古文公司规则生成失败：\n" + log_text)

        files = sorted(OUT_DIR.glob("*.ttf")) + sorted(OUT_DIR.glob("*.otf"))

        if not files:
            raise RuntimeError("脚本执行结束，但 output/mongolian_gb_ttf_steps/ 中没有生成 TTF/OTF。")

        zip_path = make_zip()

        update_job(
            job_id,
            status="done",
            progress=100,
            message=f"生成成功：{len(files)} 个蒙古文字体文件。",
            outputs=[p.name for p in files],
            zip_url="/api/top_foundry/download_zip",
            preview_svg_url="/svg_preview",
            preview_ttf_url="/ttf_family_preview",
        )

    except Exception as e:
        update_job(job_id, status="error", progress=100, message=str(e))


FORCE_TOP_SCRIPT = r'''
<script id="force-foundry-top-v2">
(function(){
  if(window.__FORCE_FOUNDRY_TOP_V2__) return;
  window.__FORCE_FOUNDRY_TOP_V2__ = true;

  function findCharsetSelect(){
    const selects = Array.from(document.querySelectorAll("select"));

    for(const s of selects){
      const txt = Array.from(s.options || []).map(o => o.textContent || "").join(" ");
      if(
        txt.includes("传统蒙古文35个") ||
        txt.includes("英文 A-Z") ||
        txt.includes("中文6500") ||
        txt.includes("自定义输入字符")
      ){
        return s;
      }
    }

    return selects[0] || null;
  }

  function findTopStartButton(){
    const byId = document.getElementById("startBtn");
    if(byId) return byId;

    return Array.from(document.querySelectorAll("button,input[type='submit'],input[type='button']")).find(el => {
      const t = (el.innerText || el.value || "").trim();
      return t.includes("开始生成");
    });
  }

  function addFoundryOptions(select){
    if(!select || select.dataset.forceFoundryAdded === "1") return;

    const group = document.createElement("optgroup");
    group.label = "蒙古文字体公司规则";

    const options = [
      ["foundry_oyun_gb", "蒙古文公司规则：奥云｜中国国标版"],
      ["foundry_oyun_private", "蒙古文公司规则：奥云｜私有编码版"],
      ["foundry_menksoft", "蒙古文公司规则：蒙科立｜传统蒙古文版"],
      ["foundry_menksoft_gb", "蒙古文公司规则：蒙科立｜中国国标版"]
    ];

    options.forEach(([value, text]) => {
      const o = document.createElement("option");
      o.value = value;
      o.textContent = text;
      group.appendChild(o);
    });

    select.appendChild(group);
    select.dataset.forceFoundryAdded = "1";
  }

  function makePanel(select){
    let panel = document.getElementById("topFoundryPanel");
    if(panel) return panel;

    panel = document.createElement("div");
    panel.id = "topFoundryPanel";
    panel.style.cssText = [
      "margin-top:14px",
      "padding:16px",
      "border:1px solid #d1d5db",
      "border-radius:10px",
      "background:#f8fafc",
      "display:none"
    ].join(";");

    panel.innerHTML = `
      <h3 style="margin-top:0;">蒙古文字体公司规则生成</h3>
      <p style="font-size:12px;color:#555;line-height:1.7;">
        该功能调用真实蒙古文公司规则生成脚本，输出目录为
        <code>output/mongolian_gb_ttf_steps/</code>。
      </p>

      <label style="font-weight:700;">字体公司规则</label>
      <select id="topFoundryCompany" style="width:100%;padding:9px;margin-top:6px;border:1px solid #ccc;border-radius:6px;">
        <option value="foundry_oyun_gb">奥云｜中国国标版</option>
        <option value="foundry_oyun_private">奥云｜私有编码版</option>
        <option value="foundry_menksoft">蒙科立｜传统蒙古文版</option>
        <option value="foundry_menksoft_gb">蒙科立｜中国国标版</option>
      </select>

      <label style="display:block;font-weight:700;margin-top:12px;">字体文件 A</label>
      <input id="topFoundryFontA" type="file" accept=".ttf,.otf" style="width:100%;box-sizing:border-box;margin-top:6px;">

      <label style="display:block;font-weight:700;margin-top:12px;">字体文件 B</label>
      <input id="topFoundryFontB" type="file" accept=".ttf,.otf" style="width:100%;box-sizing:border-box;margin-top:6px;">

      <label style="display:block;font-weight:700;margin-top:12px;">中间步数</label>
      <input id="topFoundrySteps" type="number" value="20" min="1" max="100" style="width:100%;box-sizing:border-box;padding:9px;margin-top:6px;border:1px solid #ccc;border-radius:6px;">

      <button id="topFoundryBuildBtn" type="button" style="margin-top:14px;background:#111;color:#fff;border:0;border-radius:8px;padding:10px 16px;font-weight:700;cursor:pointer;">
        按所选蒙古文公司规则生成
      </button>

      <div id="topFoundryProgressWrap" style="display:none;margin-top:16px;">
        <div style="height:14px;background:#e5e7eb;border-radius:999px;overflow:hidden;">
          <div id="topFoundryProgressBar" style="height:100%;width:0%;background:#2563eb;transition:width .25s;"></div>
        </div>
        <div id="topFoundryStatus" style="font-size:13px;margin-top:8px;color:#333;">等待开始。</div>
        <pre id="topFoundryLog" style="white-space:pre-wrap;background:#111827;color:#ddd;border-radius:8px;padding:12px;min-height:80px;margin-top:10px;"></pre>
        <div id="topFoundryLinks" style="margin-top:10px;"></div>
      </div>
    `;

    const anchor = select.parentElement;
    anchor.parentElement.insertBefore(panel, anchor.nextSibling);

    const btn = panel.querySelector("#topFoundryBuildBtn");
    btn.addEventListener("click", startTopFoundryJob);

    return panel;
  }

  function syncPanel(){
    const select = findCharsetSelect();
    if(!select) return;

    addFoundryOptions(select);

    const panel = makePanel(select);
    const normalBtn = findTopStartButton();

    const value = select.value || "";
    const isFoundry = value.startsWith("foundry_");

    panel.style.display = isFoundry ? "block" : "none";

    if(normalBtn){
      normalBtn.style.display = isFoundry ? "none" : "";
    }

    const company = document.getElementById("topFoundryCompany");
    if(isFoundry && company){
      company.value = value;
    }
  }

  async function startTopFoundryJob(){
    const fontA = document.getElementById("topFoundryFontA");
    const fontB = document.getElementById("topFoundryFontB");
    const steps = document.getElementById("topFoundrySteps");
    const company = document.getElementById("topFoundryCompany");

    if(!fontA.files.length || !fontB.files.length){
      alert("请先选择字体文件 A 和字体文件 B。");
      return;
    }

    const wrap = document.getElementById("topFoundryProgressWrap");
    const bar = document.getElementById("topFoundryProgressBar");
    const status = document.getElementById("topFoundryStatus");
    const log = document.getElementById("topFoundryLog");
    const links = document.getElementById("topFoundryLinks");

    wrap.style.display = "block";
    bar.style.width = "3%";
    status.textContent = "正在提交蒙古文公司规则生成任务...";
    log.textContent = "正在上传字体文件。";
    links.innerHTML = "";

    const fd = new FormData();
    fd.append("font_a", fontA.files[0]);
    fd.append("font_b", fontB.files[0]);
    fd.append("steps", steps.value || "20");
    fd.append("company", company.value || "foundry_oyun_gb");

    try{
      const res = await fetch("/api/top_foundry/start", {
        method:"POST",
        body:fd
      });

      const data = await res.json();

      if(!res.ok || !data.job_id){
        throw new Error(JSON.stringify(data));
      }

      pollTopFoundry(data.job_id);

    }catch(err){
      bar.style.width = "100%";
      status.textContent = "提交失败。";
      log.textContent = String(err);
    }
  }

  function link(text, href){
    const a = document.createElement("a");
    a.href = href;
    a.target = "_blank";
    a.textContent = text;
    a.style.cssText = "margin-right:14px;color:#2563eb;text-decoration:underline;font-weight:600;";
    return a;
  }

  function pollTopFoundry(jobId){
    const bar = document.getElementById("topFoundryProgressBar");
    const status = document.getElementById("topFoundryStatus");
    const log = document.getElementById("topFoundryLog");
    const links = document.getElementById("topFoundryLinks");

    const timer = setInterval(async () => {
      try{
        const res = await fetch("/api/top_foundry/status/" + jobId + "?t=" + Date.now());
        const job = await res.json();

        const p = job.progress || 0;
        bar.style.width = p + "%";
        status.textContent = "状态：" + job.status + "｜进度：" + p + "%";
        log.textContent = job.message || "";

        if(job.status === "done"){
          clearInterval(timer);

          links.innerHTML = "";
          links.appendChild(link("下载蒙古文 TTF 字体家族压缩包", "/api/top_foundry/download_zip"));
          links.appendChild(link("打开蒙古文 SVG 变化预览", "/svg_preview"));
          links.appendChild(link("打开蒙古文 TTF 字体家族预览", "/ttf_family_preview"));
          links.appendChild(link("打开蒙古文实时可变滑杆预览", "/variable_preview"));
        }

        if(job.status === "error"){
          clearInterval(timer);
          bar.style.width = "100%";
        }

      }catch(err){
        clearInterval(timer);
        status.textContent = "轮询失败。";
        log.textContent = String(err);
      }
    }, 1000);
  }

  function init(){
    const select = findCharsetSelect();
    if(!select) return;

    syncPanel();

    if(!select.dataset.forceFoundryChangeBound){
      select.addEventListener("change", syncPanel);
      select.dataset.forceFoundryChangeBound = "1";
    }
  }

  document.addEventListener("DOMContentLoaded", init);
  setTimeout(init, 300);
  setTimeout(init, 1000);
})();
</script>
'''


def install_force_foundry_top(app):
    if getattr(app.state, "_force_foundry_top_v2", False):
        return

    app.state._force_foundry_top_v2 = True

    @app.post("/api/top_foundry/start")
    async def top_foundry_start(
        font_a: UploadFile = File(...),
        font_b: UploadFile = File(...),
        steps: int = Form(20),
        company: str = Form("foundry_oyun_gb"),
    ):
        job_id = uuid.uuid4().hex[:12]
        job_path = JOB_DIR / job_id
        job_path.mkdir(parents=True, exist_ok=True)

        font_a_path = job_path / safe_name(font_a.filename)
        font_b_path = job_path / safe_name(font_b.filename)

        with open(font_a_path, "wb") as f:
            shutil.copyfileobj(font_a.file, f)

        with open(font_b_path, "wb") as f:
            shutil.copyfileobj(font_b.file, f)

        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "任务已创建。",
            "outputs": [],
        }

        t = Thread(
            target=run_foundry_job,
            args=(job_id, font_a_path, font_b_path, int(steps), company),
            daemon=True,
        )
        t.start()

        return JSONResponse({"job_id": job_id})

    @app.get("/api/top_foundry/status/{job_id}")
    def top_foundry_status(job_id: str):
        job = JOBS.get(job_id)
        if not job:
            return JSONResponse({"status": "error", "message": "任务不存在。"}, status_code=404)
        return JSONResponse(job)

    @app.get("/api/top_foundry/download_zip")
    def top_foundry_download_zip():
        zip_path = make_zip()
        if not zip_path.exists():
            return JSONResponse({"error": "压缩包不存在。"}, status_code=404)

        return FileResponse(
            str(zip_path),
            filename="mongolian_gb_ttf_steps_package.zip",
            media_type="application/zip",
        )

    @app.middleware("http")
    async def force_foundry_top_middleware(request, call_next):
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

        if "force-foundry-top-v2" not in text:
            if "</body>" in text:
                text = text.replace("</body>", FORCE_TOP_SCRIPT + "\n</body>", 1)
            else:
                text += FORCE_TOP_SCRIPT

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html"
        )
