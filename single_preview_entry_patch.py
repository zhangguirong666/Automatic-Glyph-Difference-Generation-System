from fastapi.responses import Response

SINGLE_PREVIEW_SCRIPT = r'''
<script id="single-preview-entry-patch-v1">
(function(){
  if(window.__SINGLE_PREVIEW_ENTRY_PATCH_V1__) return;
  window.__SINGLE_PREVIEW_ENTRY_PATCH_V1__ = true;

  function extractJobId(){
    const sources = Array.from(document.querySelectorAll("a[href]"));

    for(const a of sources){
      const href = a.getAttribute("href") || "";

      let m = href.match(/\/api\/unicode\/font_zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/real_preview\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/download\/([^\/?#]+)\//);
      if(m) return decodeURIComponent(m[1]);

      m = href.match(/\/api\/unicode\/download_zip\/([^\/?#]+)/);
      if(m) return decodeURIComponent(m[1]);
    }

    return "";
  }

  function isPreviewLink(el){
    const text = (el.innerText || el.textContent || "").trim();
    const href = el.getAttribute ? (el.getAttribute("href") || "") : "";

    if(el.id === "singleRealPreviewEntry") return false;

    return (
      text.includes("打开真实生成结果预览") ||
      text.includes("查看全部生成结果") ||
      text.includes("打开真实预览中心") ||
      text.includes("打开生成结果工作台") ||
      text.includes("打开 SVG 变化预览") ||
      text.includes("打开 TTF 字体家族预览") ||
      text.includes("打开实时可变滑杆预览") ||
      href.includes("/api/unicode/real_preview/") ||
      href.includes("/api/real_preview/page/") ||
      href.includes("/api/generated_workspace/page/")
    );
  }

  function cleanDuplicatePreviewLinks(){
    Array.from(document.querySelectorAll("a,button")).forEach(el => {
      if(isPreviewLink(el)){
        el.remove();
      }
    });

    Array.from(document.querySelectorAll("#realResultPreviewLinkBox")).forEach(el => {
      el.remove();
    });
  }

  function findResultHost(){
    const zipBox = document.getElementById("compactTtfZipButtonBox");
    if(zipBox) return zipBox;

    const zipLink = Array.from(document.querySelectorAll("a[href]")).find(a => {
      const text = (a.innerText || a.textContent || "").trim();
      const href = a.getAttribute("href") || "";
      return text.includes("下载全部 TTF") || href.includes("/api/unicode/font_zip/");
    });

    if(zipLink) return zipLink.parentElement || zipLink.closest("div");

    return (
      document.getElementById("outputs") ||
      document.querySelector(".outputs") ||
      document.body
    );
  }

  function installSinglePreviewButton(){
    const jobId = extractJobId();
    if(!jobId) return;

    cleanDuplicatePreviewLinks();

    if(document.getElementById("singleRealPreviewEntry")) return;

    const host = findResultHost();

    const a = document.createElement("a");
    a.id = "singleRealPreviewEntry";
    a.href = "/api/unicode/real_preview/" + encodeURIComponent(jobId);
    a.target = "_blank";
    a.textContent = "查看全部生成结果预览";
    a.style.cssText = [
      "display:inline-block",
      "padding:10px 16px",
      "background:#111827",
      "color:#fff",
      "border-radius:8px",
      "text-decoration:none",
      "font-weight:700",
      "margin-left:8px",
      "margin-top:8px"
    ].join(";");

    host.appendChild(a);
  }

  function run(){
    installSinglePreviewButton();
    cleanDuplicatePreviewLinks();

    const btn = document.getElementById("singleRealPreviewEntry");
    if(btn){
      btn.style.display = "inline-block";
    }
  }

  document.addEventListener("DOMContentLoaded", run);
  setInterval(run, 800);
})();
</script>
'''


def install_single_preview_entry_patch(app):
    if getattr(app.state, "_single_preview_entry_patch_v1", False):
        return

    app.state._single_preview_entry_patch_v1 = True

    @app.middleware("http")
    async def single_preview_entry_middleware(request, call_next):
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

        if "single-preview-entry-patch-v1" not in text:
            if "</body>" in text:
                text = text.replace("</body>", SINGLE_PREVIEW_SCRIPT + "\n</body>", 1)
            else:
                text += SINGLE_PREVIEW_SCRIPT

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )
