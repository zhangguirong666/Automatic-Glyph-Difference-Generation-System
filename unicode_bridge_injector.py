from starlette.responses import Response

BRIDGE_SCRIPT = r'''
<script id="unicode-real-bridge-v2">
(function(){
  if (window.__UNICODE_REAL_BRIDGE_V2__) return;
  window.__UNICODE_REAL_BRIDGE_V2__ = true;

  console.log("[UNICODE_BRIDGE] loaded");

  let bridgeTimer = null;

  function $(id){ return document.getElementById(id); }

  function firstStartButton(){
    const byId = $("startBtn");
    if (byId) return byId;

    const all = Array.from(document.querySelectorAll("button,input[type='submit'],input[type='button']"));
    for (const el of all){
      const text = (el.innerText || el.value || "").trim();
      const id = (el.id || "").toLowerCase();

      // 避免误接管下半部分字体公司规则按钮
      if (id.includes("foundry")) continue;
      if (text === "开始生成" || text.includes("开始生成")) return el;
    }
    return null;
  }

  function findMainForm(){
    const btn = firstStartButton();
    if (btn && btn.form) return btn.form;

    const forms = Array.from(document.querySelectorAll("form"));
    for (const f of forms){
      if (f.querySelector('input[name="font_a"]') && f.querySelector('input[name="font_b"]')) {
        return f;
      }
    }

    return document.querySelector("form");
  }

  function ensurePanel(form){
    let panel = $("unicodeBridgePanel");
    if (panel) return panel;

    panel = document.createElement("div");
    panel.id = "unicodeBridgePanel";
    panel.style.cssText = [
      "margin-top:18px",
      "padding:16px",
      "border:1px solid #d1d5db",
      "border-radius:10px",
      "background:#f9fafb",
      "font-family:Arial,'Microsoft YaHei',sans-serif"
    ].join(";");

    panel.innerHTML = `
      <div style="font-weight:700;margin-bottom:8px;">通用 Unicode 生成进度</div>
      <div style="height:14px;background:#e5e7eb;border-radius:999px;overflow:hidden;">
        <div id="unicodeBridgeBar" style="height:100%;width:0%;background:#2563eb;transition:width .25s;"></div>
      </div>
      <div id="unicodeBridgeStatus" style="font-size:13px;margin-top:8px;color:#333;">等待开始生成。</div>
      <pre id="unicodeBridgeLog" style="margin-top:10px;min-height:70px;background:#111827;color:#d1d5db;padding:12px;border-radius:8px;white-space:pre-wrap;font-size:12px;"></pre>
      <div id="unicodeBridgeOutputs" style="margin-top:10px;"></div>
    `;

    if (form && form.parentNode) {
      form.parentNode.insertBefore(panel, form.nextSibling);
    } else {
      document.body.appendChild(panel);
    }

    return panel;
  }

  function setProgress(p, msg){
    const bar = $("unicodeBridgeBar");
    const status = $("unicodeBridgeStatus");

    if (bar) bar.style.width = Math.max(0, Math.min(100, p || 0)) + "%";
    if (status && msg) status.textContent = msg;
  }

  function setLog(msg){
    const log = $("unicodeBridgeLog");
    if (log) log.textContent = msg || "";
  }

  function detectPreset(form){
    const existing = form.querySelector('[name="preset"]');
    if (existing && existing.value) return existing.value;

    const selectors = Array.from(form.querySelectorAll("select"));
    let text = "";

    for (const s of selectors){
      const name = (s.name || s.id || "").toLowerCase();
      const selected = s.options && s.selectedIndex >= 0 ? s.options[s.selectedIndex] : null;
      const valueText = ((s.value || "") + " " + (selected ? selected.text : "")).toLowerCase();

      if (
        name.includes("script") ||
        name.includes("lang") ||
        name.includes("language") ||
        name.includes("unicode") ||
        name.includes("charset") ||
        valueText.includes("中文") ||
        valueText.includes("英文") ||
        valueText.includes("日文") ||
        valueText.includes("韩文") ||
        valueText.includes("蒙古") ||
        valueText.includes("德文") ||
        valueText.includes("俄文")
      ){
        text = valueText;
        break;
      }
    }

    if (text.includes("德") || text.includes("german")) return "german";
    if (text.includes("俄") || text.includes("russian") || text.includes("cyrillic")) return "russian";
    if (text.includes("蒙古") || text.includes("mongol")) return "mongolian_35";
    if (text.includes("日") || text.includes("japan") || text.includes("kana")) return "japanese_kana";
    if (text.includes("韩") || text.includes("korea") || text.includes("hangul")) return "korean_hangul_sample";
    if (text.includes("中") || text.includes("chinese") || text.includes("cjk")) return "chinese_3500";
    if (text.includes("英") || text.includes("english") || text.includes("latin")) return "english";

    return "english";
  }

  function normalizeFormData(form){
    const fd = new FormData(form);

    if (!fd.has("preset") || !fd.get("preset")) {
      fd.set("preset", detectPreset(form));
    }

    if (!fd.has("custom_text")) {
      const custom = form.querySelector("textarea") || form.querySelector('input[type="text"][name*="text"]');
      fd.set("custom_text", custom ? custom.value : "");
    }

    if (!fd.has("steps") || !fd.get("steps")) {
      const stepInput = form.querySelector('input[name="steps"], input[id*="steps"], input[id*="Steps"]');
      fd.set("steps", stepInput ? stepInput.value : "20");
    }

    if (!fd.has("sample_points")) fd.set("sample_points", "120");
    if (!fd.has("family_name")) fd.set("family_name", "FontMorphFamily");
    if (!fd.has("naming_mode")) fd.set("naming_mode", "morph");
    if (!fd.has("variable_font")) fd.set("variable_font", "no");

    return fd;
  }

  async function startUnicodeBridge(){
    const form = findMainForm();

    if (!form) {
      alert("没有找到原页面表单。");
      return;
    }

    const fontA = form.querySelector('input[name="font_a"]');
    const fontB = form.querySelector('input[name="font_b"]');

    if (!fontA || !fontB) {
      alert("没有找到字体 A / 字体 B 上传控件。");
      return;
    }

    if (!fontA.files.length || !fontB.files.length) {
      alert("请先上传字体 A 和字体 B。");
      return;
    }

    ensurePanel(form);
    setProgress(3, "按钮已接管，正在提交后台任务……");
    setLog("正在上传字体文件，请稍等。");

    const btn = firstStartButton();
    if (btn) btn.disabled = true;

    try {
      const fd = normalizeFormData(form);

      const res = await fetch("/api/unicode/start", {
        method: "POST",
        body: fd
      });

      const text = await res.text();

      if (!res.ok) {
        throw new Error("提交失败：HTTP " + res.status + "\\n" + text);
      }

      let data;
      try {
        data = JSON.parse(text);
      } catch(e) {
        throw new Error("接口没有返回 JSON：\\n" + text);
      }

      if (!data.job_id) {
        throw new Error("接口没有返回 job_id：\\n" + text);
      }

      setProgress(6, "任务已提交，任务 ID：" + data.job_id);
      setLog("后台任务已开始。");
      pollUnicodeBridge(data.job_id);

    } catch(err) {
      console.error(err);
      setProgress(100, "提交失败。");
      setLog(String(err));
      alert("提交失败，错误已显示在进度日志区域。");
      if (btn) btn.disabled = false;
    }
  }

  function pollUnicodeBridge(jobId){
    const btn = firstStartButton();

    if (bridgeTimer) clearInterval(bridgeTimer);

    bridgeTimer = setInterval(async function(){
      try {
        const res = await fetch("/api/unicode/status/" + jobId + "?t=" + Date.now());
        const job = await res.json();

        const p = job.progress || 0;
        setProgress(
          p,
          "状态：" + job.status + " ｜ 进度：" + p + "% ｜ 共同字符：" + (job.common_count || 0)
        );
        setLog(job.message || "");

        if (job.status === "done") {
          clearInterval(bridgeTimer);
          if (btn) btn.disabled = false;

          const out = $("unicodeBridgeOutputs");
          if (out) {
            out.innerHTML = "<div style='font-weight:700;margin-bottom:8px;'>生成成功，可下载：</div>";

            const preview = document.createElement("a");
            preview.href = "/api/unicode/real_preview/" + encodeURIComponent(jobId);
            preview.textContent = "打开生成结果预览";
            preview.target = "_blank";
            preview.style.cssText = "display:inline-block;margin:4px 10px 8px 0;padding:8px 12px;background:#111827;color:#fff;border-radius:6px;text-decoration:none;font-weight:700;";
            out.appendChild(preview);

            const fullPreview = document.createElement("a");
            fullPreview.href = (job.preview_page_url || ("/api/unicode/preview_page/" + encodeURIComponent(jobId)));
            fullPreview.textContent = "打开完整 SVG/TTF 列表";
            fullPreview.target = "_blank";
            fullPreview.style.cssText = "display:inline-block;margin:4px 10px 8px 0;padding:8px 12px;background:#2563eb;color:#fff;border-radius:6px;text-decoration:none;font-weight:700;";
            out.appendChild(fullPreview);

            if (job.outputs && job.outputs.length) {
              job.outputs.forEach(function(name){
                const a = document.createElement("a");
                a.href = "/api/unicode/download/" + jobId + "/" + encodeURIComponent(name);
                a.textContent = name;
                a.target = "_blank";
                a.style.cssText = "display:inline-block;margin:4px 8px 4px 0;padding:7px 10px;background:#eef2ff;color:#1d4ed8;border-radius:6px;text-decoration:none;";
                out.appendChild(a);
              });
            }
          }
        }

        if (job.status === "error") {
          clearInterval(bridgeTimer);
          setProgress(100, "生成失败。");
          if (btn) btn.disabled = false;
        }

      } catch(err) {
        clearInterval(bridgeTimer);
        setProgress(100, "轮询失败。");
        setLog(String(err));
        if (btn) btn.disabled = false;
      }
    }, 1000);
  }

  // 捕获阶段强制接管点击，阻止旧脚本继续处理
  document.addEventListener("click", function(e){
    const el = e.target.closest && e.target.closest("button,input[type='submit'],input[type='button']");
    if (!el) return;

    const text = (el.innerText || el.value || "").trim();
    const id = (el.id || "").toLowerCase();

    if (id.includes("foundry")) return;

    if (el.id === "startBtn" || text === "开始生成" || text.includes("开始生成")) {
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      startUnicodeBridge();
      return false;
    }
  }, true);

  // 捕获阶段强制拦截原 form submit
  document.addEventListener("submit", function(e){
    const form = e.target;
    if (!form) return;

    if (form.querySelector && form.querySelector('input[name="font_a"]') && form.querySelector('input[name="font_b"]')) {
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      startUnicodeBridge();
      return false;
    }
  }, true);

  window.startUnicodeBridge = startUnicodeBridge;

  document.addEventListener("DOMContentLoaded", function(){
    const btn = firstStartButton();
    if (btn) {
      btn.type = "button";
      btn.dataset.unicodeBridge = "1";
    }
    console.log("[UNICODE_BRIDGE] ready");
  });

})();
</script>
'''

def install_unicode_bridge(app):
    if getattr(app.state, "_unicode_real_bridge_v2_installed", False):
        return

    app.state._unicode_real_bridge_v2_installed = True

    @app.middleware("http")
    async def unicode_real_bridge_middleware(request, call_next):
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

        if "unicode-real-bridge-v2" not in text:
            if "</body>" in text:
                text = text.replace("</body>", BRIDGE_SCRIPT + "\n</body>", 1)
            else:
                text = text + BRIDGE_SCRIPT

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html"
        )
