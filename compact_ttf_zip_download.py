from pathlib import Path
import zipfile
from fastapi.responses import FileResponse, JSONResponse, Response

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_JOBS = BASE_DIR / "runtime_jobs"


def safe_name(s: str):
    s = str(s)
    keep = []
    for ch in s:
        if ch.isalnum() or ch in "._-":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:120] or "job"


def make_unicode_ttf_zip(job_id: str):
    job_id = safe_name(job_id)
    job_dir = RUNTIME_JOBS / job_id
    out_dir = job_dir / "outputs"

    if not out_dir.exists():
        return None, "没有找到该任务的 outputs 目录。"

    font_files = sorted(out_dir.glob("*.ttf")) + sorted(out_dir.glob("*.otf"))

    if not font_files:
        return None, "outputs 目录中没有 TTF/OTF 字体文件。"

    zip_path = job_dir / "all_ttf_fonts.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in font_files:
            zf.write(p, arcname=p.name)

    return zip_path, ""


FRONTEND_SCRIPT = r'''
<script id="compact-ttf-zip-download-v1">
(function(){
  if(window.__COMPACT_TTF_ZIP_DOWNLOAD_V1__) return;
  window.__COMPACT_TTF_ZIP_DOWNLOAD_V1__ = true;

  function extractJobId(){
    const links = Array.from(document.querySelectorAll('a[href*="/api/unicode/download/"]'));

    for(const a of links){
      const href = a.getAttribute("href") || "";
      const m = href.match(/\/api\/unicode\/download\/([^\/?#]+)\//);
      if(m) return decodeURIComponent(m[1]);
    }

    return "";
  }

  function findOutputHost(){
    const first = document.querySelector('a[href*="/api/unicode/download/"]');
    if(first){
      return first.parentElement || first.closest("div") || document.body;
    }

    return (
      document.getElementById("outputs") ||
      document.querySelector(".outputs") ||
      document.body
    );
  }

  function hideSingleTTFLinks(){
    const links = Array.from(document.querySelectorAll('a[href*="/api/unicode/download/"]'));

    links.forEach(a => {
      const text = (a.textContent || "").trim();
      const href = a.getAttribute("href") || "";

      if(
        text.match(/\.(ttf|otf)$/i) ||
        href.match(/\.(ttf|otf)(\?|#|$)/i)
      ){
        a.style.display = "none";
      }
    });
  }

  function installZipButton(){
    const jobId = extractJobId();
    if(!jobId) return;

    hideSingleTTFLinks();

    if(document.getElementById("compactTtfZipButtonBox")) return;

    const host = findOutputHost();

    const box = document.createElement("div");
    box.id = "compactTtfZipButtonBox";
    box.style.cssText = "margin:10px 0 12px 0;";

    const a = document.createElement("a");
    a.href = "/api/unicode/font_zip/" + encodeURIComponent(jobId);
    a.target = "_blank";
    a.textContent = "下载全部 TTF 字体压缩包";
    a.style.cssText = [
      "display:inline-block",
      "padding:10px 16px",
      "background:#2563eb",
      "color:#fff",
      "border-radius:8px",
      "text-decoration:none",
      "font-weight:700"
    ].join(";");

    box.appendChild(a);
    host.insertBefore(box, host.firstChild);
  }

  function run(){
    installZipButton();
    hideSingleTTFLinks();
  }

  document.addEventListener("DOMContentLoaded", run);
  setInterval(run, 1000);
})();
</script>
'''


def install_compact_ttf_zip_download(app):
    if getattr(app.state, "_compact_ttf_zip_download_v1", False):
        return

    app.state._compact_ttf_zip_download_v1 = True

    @app.get("/api/unicode/font_zip/{job_id}")
    def unicode_font_zip(job_id: str):
        zip_path, err = make_unicode_ttf_zip(job_id)

        if not zip_path or not zip_path.exists():
            return JSONResponse({"error": err or "压缩包生成失败。"}, status_code=404)

        return FileResponse(
            str(zip_path),
            filename=f"unicode_ttf_fonts_{safe_name(job_id)}.zip",
            media_type="application/zip",
        )

    @app.middleware("http")
    async def compact_ttf_zip_download_middleware(request, call_next):
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

        if "compact-ttf-zip-download-v1" not in text:
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
            media_type="text/html",
        )
