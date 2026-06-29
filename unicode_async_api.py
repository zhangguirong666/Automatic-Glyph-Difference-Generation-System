import sys
import uuid
import shutil
import zipfile
from pathlib import Path
from threading import Thread
from subprocess import Popen, PIPE, STDOUT
from xml.sax.saxutils import escape

from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent
JOB_DIR = BASE_DIR / "runtime_jobs"
JOB_DIR.mkdir(exist_ok=True)

JOBS = {}


def update_job(job_id, **kwargs):
    if job_id in JOBS:
        JOBS[job_id].update(kwargs)


def chars_from_preset(preset: str, custom_text: str = ""):
    if preset == "english":
        return "".join(chr(i) for i in range(0x20, 0x7F))

    if preset == "german":
        return "".join(chr(i) for i in range(0x20, 0x7F)) + "ÄÖÜäöüß"

    if preset == "russian":
        return "".join(chr(i) for i in range(0x0400, 0x0500))

    if preset == "mongolian_35":
        return "".join(chr(i) for i in range(0x1820, 0x1843))

    if preset == "mongolian_unicode":
        return "".join(chr(i) for i in range(0x1800, 0x18B0))

    if preset == "japanese_kana":
        return "".join(chr(i) for i in range(0x3040, 0x3100))

    if preset == "korean_hangul_sample":
        return "".join(chr(i) for i in range(0xAC00, 0xAC00 + 500))

    if preset == "chinese_3500":
        return "".join(chr(i) for i in range(0x4E00, 0x4E00 + 3500))

    if preset == "chinese_6500":
        return "".join(chr(i) for i in range(0x4E00, 0x4E00 + 6500))

    if preset == "custom":
        return custom_text or ""

    return custom_text or ""


def get_cmap(font: TTFont):
    best = None
    best_score = -1
    for table in font["cmap"].tables:
        if not table.isUnicode():
            continue
        score = len(table.cmap)
        if table.platformID == 3:
            score += 1_000_000
        if table.format in (12, 13):
            score += 100_000
        if score > best_score:
            best = table.cmap
            best_score = score
    return dict(best or {})


def font_supported_codepoints(font_path: Path):
    font = TTFont(str(font_path), lazy=True)
    cps = set(get_cmap(font).keys())
    font.close()
    return cps


def filter_common_chars(font_a: Path, font_b: Path, candidate_chars: str):
    cps_a = font_supported_codepoints(font_a)
    cps_b = font_supported_codepoints(font_b)

    result = []
    seen = set()

    for ch in candidate_chars:
        cp = ord(ch)
        if cp in seen:
            continue
        seen.add(cp)

        if cp in cps_a and cp in cps_b:
            result.append(ch)

    return "".join(result)


def safe_name(name: str):
    keep = []
    for ch in name:
        if ch.isalnum() or ch in "._-":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:120] or "file"


def create_fonts_zip(job_path: Path, out_dir: Path):
    zip_path = job_path / "fonts_package.zip"

    files = sorted(out_dir.glob("*.ttf")) + sorted(out_dir.glob("*.otf"))

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.name)

    return zip_path


def make_svg_previews(ttf_files, preview_dir: Path, preview_chars: str, max_chars=12):
    """
    从真实生成的 TTF 中提取字形轮廓，生成 SVG 预览。
    这不是截图，也不是假图，而是直接读取生成字体里的 glyf/CFF 轮廓。
    """
    preview_dir.mkdir(parents=True, exist_ok=True)

    preview_chars = "".join(dict.fromkeys(preview_chars))[:max_chars]
    preview_files = []

    for font_path in ttf_files:
        try:
            font = TTFont(str(font_path))
            cmap = get_cmap(font)
            glyph_set = font.getGlyphSet()

            upm = int(font["head"].unitsPerEm)
            hhea = font["hhea"]
            ascent = int(hhea.ascent)
            descent = int(hhea.descent)
            height = max(600, ascent - descent + 160)
            baseline = 80 + ascent

            hmtx = font["hmtx"].metrics

            x = 80
            paths = []

            for ch in preview_chars:
                cp = ord(ch)
                gname = cmap.get(cp)

                if not gname or gname not in glyph_set:
                    continue

                pen = SVGPathPen(glyph_set)

                try:
                    glyph_set[gname].draw(pen)
                    d = pen.getCommands()
                except Exception:
                    continue

                aw = hmtx.get(gname, (upm, 0))[0]
                aw = max(int(aw), int(upm * 0.45))

                if d:
                    paths.append(
                        f'<path d="{escape(d)}" '
                        f'transform="translate({x},{baseline}) scale(1,-1)" '
                        f'fill="#111111"/>'
                    )

                x += aw + 80

            if not paths:
                font.close()
                continue

            width = max(900, x + 80)

            svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="24" y="34" font-size="22" fill="#333333">{escape(font_path.name)}</text>
  <line x1="60" y1="{baseline}" x2="{width - 60}" y2="{baseline}" stroke="#dddddd" stroke-width="1"/>
  {chr(10).join(paths)}
</svg>
'''

            out_name = safe_name(font_path.stem) + ".svg"
            out_path = preview_dir / out_name
            out_path.write_text(svg, encoding="utf-8")
            preview_files.append(out_name)

            font.close()

        except Exception as e:
            print(f"[WARN] SVG preview failed for {font_path}: {e}")

    return preview_files


def unicode_job_paths(job_id: str):
    safe_job = safe_name(job_id)
    job_path = JOB_DIR / safe_job
    return job_path, job_path / "outputs", job_path / "svg_previews", job_path / "chars.txt"


def unicode_output_files(job_id: str):
    _, out_dir, _, _ = unicode_job_paths(job_id)
    if not out_dir.exists():
        return []
    return sorted(out_dir.glob("*.ttf")) + sorted(out_dir.glob("*.otf"))


def unicode_preview_chars(job_id: str):
    _, _, _, chars_file = unicode_job_paths(job_id)
    if chars_file.exists():
        chars = chars_file.read_text(encoding="utf-8", errors="ignore").strip()
        if chars:
            return chars[:12]
    return "中文预览ABC123"


def ensure_unicode_preview_files(job_id: str):
    _, _, preview_dir, _ = unicode_job_paths(job_id)
    existing = sorted(p.name for p in preview_dir.glob("*.svg")) if preview_dir.exists() else []
    if existing:
        return existing

    ttf_files = unicode_output_files(job_id)
    if not ttf_files:
        return []

    return make_svg_previews(ttf_files, preview_dir, unicode_preview_chars(job_id), max_chars=12)


def unicode_disk_status(job_id: str):
    job_path, _, _, chars_file = unicode_job_paths(job_id)
    ttf_files = unicode_output_files(job_id)
    zip_path = job_path / "fonts_package.zip"
    if not ttf_files:
        return None

    preview_files = ensure_unicode_preview_files(job_id)
    common_count = 0
    if chars_file.exists():
        common_count = len(chars_file.read_text(encoding="utf-8", errors="ignore"))

    return {
        "job_id": safe_name(job_id),
        "status": "done",
        "progress": 100,
        "message": f"生成成功：{len(ttf_files)} 个字体文件，{len(preview_files)} 个 SVG 预览。",
        "outputs": [p.name for p in ttf_files],
        "zip_name": zip_path.name if zip_path.exists() else "",
        "zip_url": f"/api/unicode/download_zip/{safe_name(job_id)}" if zip_path.exists() else "",
        "previews": preview_files,
        "preview_urls": [f"/api/unicode/preview/{safe_name(job_id)}/{name}" for name in preview_files],
        "preview_page_url": f"/api/unicode/preview_page/{safe_name(job_id)}",
        "common_count": common_count,
    }


def run_generation_job(
    job_id: str,
    font_a: Path,
    font_b: Path,
    preset: str,
    custom_text: str,
    preview_text: str,
    steps: int,
    sample_points: int,
    family_name: str,
    naming_mode: str,
    variable_font: str,
):
    job_path = JOB_DIR / job_id
    out_dir = job_path / "outputs"
    preview_dir = job_path / "svg_previews"
    chars_file = job_path / "chars.txt"

    out_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)

    try:
        update_job(job_id, status="running", progress=3, message="正在读取 Unicode 字符集...")

        candidate_chars = chars_from_preset(preset, custom_text)

        if not candidate_chars:
            raise RuntimeError("字符集为空，请选择文种或输入自定义字符。")

        update_job(job_id, progress=8, message="正在检测两个字体共同支持的字符...")

        common_chars = filter_common_chars(font_a, font_b, candidate_chars)
        chars_file.write_text(common_chars, encoding="utf-8")

        if not common_chars:
            raise RuntimeError("两个字体没有共同支持的 Unicode 字符，无法生成。")

        update_job(
            job_id,
            progress=12,
            message=f"共同字符数量：{len(common_chars)}，开始真实字体差值生成。",
            common_count=len(common_chars),
        )

        generator = BASE_DIR / "scripts" / "build_unicode_morph_ttf_steps.py"

        if not generator.exists():
            raise RuntimeError(
                "没有找到真实字体差值生成脚本：scripts/build_unicode_morph_ttf_steps.py。"
                "这不是前端问题，需要先把真实生成脚本放进去。"
            )

        command = [
            sys.executable,
            str(generator),
            "--font-a", str(font_a),
            "--font-b", str(font_b),
            "--chars-file", str(chars_file),
            "--steps", str(steps),
            "--sample-points", str(sample_points),
            "--out-dir", str(out_dir),
            "--family-name", family_name,
            "--naming-mode", naming_mode,
            "--variable-font", variable_font,
        ]

        log_file = job_path / "run.log"

        with open(log_file, "w", encoding="utf-8") as lf:
            proc = Popen(
                command,
                stdout=PIPE,
                stderr=STDOUT,
                text=True,
                bufsize=1,
            )

            line_count = 0

            for line in proc.stdout:
                line_count += 1
                lf.write(line)
                lf.flush()

                low = line.lower()
                current_progress = JOBS[job_id].get("progress", 12)

                for i in range(1, steps + 1):
                    if f"step {i:02d}" in low or f"step {i}" in low:
                        current_progress = max(current_progress, int(12 + i / steps * 72))

                if line_count % 6 == 0:
                    current_progress = min(88, current_progress + 1)

                update_job(
                    job_id,
                    progress=current_progress,
                    message=line.strip()[-500:] if line.strip() else "生成中...",
                )

            ret = proc.wait()

        if ret != 0:
            last_log = log_file.read_text(encoding="utf-8", errors="ignore")[-5000:]
            raise RuntimeError("真实字体差值生成失败：\n" + last_log)

        ttf_files = sorted(out_dir.glob("*.ttf")) + sorted(out_dir.glob("*.otf"))

        if not ttf_files:
            raise RuntimeError("生成结束，但没有找到输出的 TTF/OTF 字体文件。")

        update_job(job_id, progress=90, message="字体已生成，正在打包 TTF 文件...")

        zip_path = create_fonts_zip(job_path, out_dir)

        update_job(job_id, progress=94, message="字体压缩包已生成，正在生成 SVG 预览...")

        svg_chars = (preview_text.strip() or custom_text.strip() or common_chars[:12])
        preview_files = make_svg_previews(ttf_files, preview_dir, svg_chars, max_chars=12)

        update_job(
            job_id,
            status="done",
            progress=100,
            message=f"生成成功：{len(ttf_files)} 个字体文件，{len(preview_files)} 个 SVG 预览。",
            common_count=len(common_chars),
            outputs=[p.name for p in ttf_files],
            zip_name=zip_path.name,
            zip_url=f"/api/unicode/download_zip/{job_id}",
            previews=preview_files,
            preview_urls=[f"/api/unicode/preview/{job_id}/{name}" for name in preview_files],
            preview_page_url=f"/api/unicode/preview_page/{job_id}",
        )

    except Exception as e:
        update_job(job_id, status="error", progress=100, message=str(e))


@router.post("/api/unicode/start")
async def start_unicode_job(
    font_a: UploadFile = File(...),
    font_b: UploadFile = File(...),
    preset: str = Form("english"),
    custom_text: str = Form(""),
    preview_text: str = Form(""),
    steps: int = Form(20),
    sample_points: int = Form(240),
    family_name: str = Form("FontMorphFamily"),
    naming_mode: str = Form("morph"),
    variable_font: str = Form("no"),
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
        "zip_name": "",
        "zip_url": "",
        "previews": [],
        "preview_urls": [],
        "common_count": 0,
    }

    thread = Thread(
        target=run_generation_job,
        args=(
            job_id,
            font_a_path,
            font_b_path,
            preset,
            custom_text,
            preview_text,
            int(steps),
            int(sample_points),
            family_name,
            naming_mode,
            variable_font,
        ),
        daemon=True,
    )
    thread.start()

    return JSONResponse({"job_id": job_id})


@router.get("/api/unicode/status/{job_id}")
def unicode_job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        disk_job = unicode_disk_status(job_id)
        if disk_job:
            return JSONResponse(disk_job)
        return JSONResponse({"status": "error", "message": "任务不存在。"}, status_code=404)
    if job.get("status") == "done" and not job.get("preview_page_url"):
        job["preview_page_url"] = f"/api/unicode/preview_page/{safe_name(job_id)}"
    return JSONResponse(job)


@router.get("/api/unicode/download/{job_id}/{filename}")
def download_unicode_output(job_id: str, filename: str):
    filename = Path(filename).name
    target = JOB_DIR / job_id / "outputs" / filename

    if not target.exists():
        return JSONResponse({"error": "文件不存在。"}, status_code=404)

    return FileResponse(str(target), filename=filename)


@router.get("/api/unicode/download_zip/{job_id}")
def download_unicode_zip(job_id: str):
    target = JOB_DIR / job_id / "fonts_package.zip"

    if not target.exists():
        return JSONResponse({"error": "字体压缩包不存在。"}, status_code=404)

    return FileResponse(
        str(target),
        filename=f"unicode_font_morph_{job_id}.zip",
        media_type="application/zip",
    )


@router.get("/api/unicode/preview/{job_id}/{filename}")
def unicode_svg_preview(job_id: str, filename: str):
    filename = Path(filename).name
    target = JOB_DIR / job_id / "svg_previews" / filename

    if not target.exists():
        return JSONResponse({"error": "SVG 预览不存在。"}, status_code=404)

    return FileResponse(
        str(target),
        filename=filename,
        media_type="image/svg+xml",
    )


@router.get("/api/unicode/preview_page/{job_id}")
def unicode_preview_page(job_id: str):
    safe_job = safe_name(job_id)
    job_path, _, preview_dir, _ = unicode_job_paths(safe_job)
    ttf_files = unicode_output_files(safe_job)
    preview_files = ensure_unicode_preview_files(safe_job)

    if not ttf_files:
        return HTMLResponse(
            "<h2>没有找到字体输出</h2><p>请确认该任务已经生成完成，或重新提交 Unicode 字体差值任务。</p>",
            status_code=404,
        )

    cards = []
    for idx, name in enumerate(preview_files, 1):
        url = f"/api/unicode/preview/{safe_job}/{name}"
        cards.append(f"""
        <div class="card">
          <div class="title">SVG 预览 {idx:02d}</div>
          <img src="{url}" alt="SVG preview {idx}">
          <div class="links"><a href="{url}" target="_blank">打开 SVG</a></div>
        </div>
        """)

    font_links = []
    for idx, p in enumerate(ttf_files, 1):
        url = f"/api/unicode/download/{safe_job}/{p.name}"
        font_links.append(f"<li><a href=\"{url}\" target=\"_blank\">Step {idx:02d} ｜ {escape(p.name)}</a></li>")

    zip_url = f"/api/unicode/download_zip/{safe_job}"
    chars = unicode_preview_chars(safe_job)
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Unicode 字体差值生成预览</title>
<style>
body{{margin:0;background:#f3f6fb;color:#111827;font-family:Arial,"Microsoft YaHei",sans-serif;}}
.wrap{{max-width:1180px;margin:0 auto;padding:26px 20px 46px;}}
.top{{background:#fff;border:1px solid #d7e0ec;border-radius:10px;padding:16px 18px;margin-bottom:16px;box-shadow:0 8px 24px rgba(15,23,42,.05);}}
h1{{margin:0 0 8px;font-size:26px;}}
.muted{{color:#52647a;line-height:1.7;}}
.actions a{{display:inline-block;margin:10px 8px 0 0;padding:9px 13px;background:#0f172a;color:#fff;text-decoration:none;border-radius:6px;font-weight:700;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;}}
.card{{background:#fff;border:1px solid #d7e0ec;border-radius:10px;padding:12px;}}
.title{{font-weight:800;margin-bottom:10px;}}
img{{width:100%;height:auto;background:#fff;border:1px solid #e5e7eb;border-radius:6px;}}
.links a{{display:inline-block;margin-top:8px;color:#1d4ed8;font-weight:700;text-decoration:none;}}
.fonts{{columns:2;line-height:1.9;}}
@media(max-width:760px){{.fonts{{columns:1;}}}}
</style>
</head>
<body>
<div class="wrap">
  <section class="top">
    <h1>Unicode 字体差值生成预览</h1>
    <div class="muted">任务 ID：{escape(safe_job)} ｜ 字体文件：{len(ttf_files)} 个 ｜ SVG 预览：{len(preview_files)} 个</div>
    <div class="muted">预览字符：{escape(chars)}</div>
    <div class="actions">
      <a href="{zip_url}" target="_blank">下载全部 TTF 字体压缩包</a>
      <a href="/unicode" target="_blank">返回 Unicode 生成器</a>
      <a href="/" target="_blank">返回首页</a>
    </div>
  </section>
  <section class="grid">
    {''.join(cards) if cards else '<div class="card">没有生成 SVG 预览，但 TTF 文件已经生成，可先下载字体包。</div>'}
  </section>
  <section class="top" style="margin-top:16px;">
    <h2>生成的字体文件</h2>
    <ol class="fonts">{''.join(font_links)}</ol>
  </section>
</div>
</body>
</html>"""
    return HTMLResponse(html)


@router.get("/unicode")
def unicode_page():
    return HTMLResponse(r'''
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>通用 Unicode 字体差值生成器</title>
<style>
body{font-family:Arial,"Microsoft YaHei",sans-serif;background:#f5f6f8;margin:0;color:#111}
.header{background:#0f2e6d;color:white;padding:24px 40px}
.container{max-width:980px;margin:24px auto;background:white;padding:28px;border-radius:14px;box-shadow:0 8px 28px rgba(0,0,0,.08)}
label{display:block;font-weight:700;margin-top:16px}
input,select,textarea{width:100%;box-sizing:border-box;padding:10px 12px;margin-top:8px;border:1px solid #ccc;border-radius:7px}
textarea{min-height:80px}
button{margin-top:22px;padding:12px 24px;border:none;border-radius:8px;background:#2563eb;color:white;font-weight:700;cursor:pointer}
button:disabled{opacity:.55}
.progress-wrap{margin-top:24px;display:none}
.progress-bg{width:100%;height:16px;background:#e5e7eb;border-radius:999px;overflow:hidden}
.progress-bar{height:100%;width:0%;background:#2563eb;transition:width .25s}
.status-line{margin-top:10px;color:#333}
.log{margin-top:12px;background:#111827;color:#d1d5db;padding:14px;border-radius:8px;white-space:pre-wrap;min-height:80px;font-size:13px}
.outputs{margin-top:20px}
.outputs a{display:inline-block;margin:6px 8px 6px 0;padding:8px 12px;background:#eef2ff;color:#1d4ed8;border-radius:6px;text-decoration:none}
.outputs a.preview-entry{background:#111827;color:#fff}
.outputs .hint{margin:8px 0;color:#475569;font-size:13px;line-height:1.6}
.preview-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin-top:16px}
.preview-card{border:1px solid #e5e7eb;border-radius:10px;padding:10px;background:#fff}
.preview-card img{width:100%;height:auto;border:1px solid #eee;background:white}
.notice{background:#fff3d8;color:#8a5a00;padding:12px 16px;border-radius:8px;margin-bottom:18px}
</style>
</head>
<body>
<div class="header">
  <h1>通用 Unicode 字体差值生成器</h1>
  <p>真实计算两个字体之间的轮廓差值，生成中间 TTF 字体，并提供 SVG 预览。</p>
</div>

<div class="container">
  <div class="notice">
    建议先用英文、德文、俄文或传统蒙古文 35 个基础字母测试。中文、韩文数量大，生成时间会明显增加。
  </div>

  <form id="unicodeForm">
    <label>字体 A（.ttf / .otf）</label>
    <input type="file" name="font_a" accept=".ttf,.otf" required>

    <label>字体 B（.ttf / .otf）</label>
    <input type="file" name="font_b" accept=".ttf,.otf" required>

    <label>选择文种 / Unicode 字符集</label>
    <select name="preset">
      <option value="english">英文 Basic Latin</option>
      <option value="german">德文 Latin + ÄÖÜäöüß</option>
      <option value="russian">俄文 Cyrillic U+0400–U+04FF</option>
      <option value="mongolian_35">传统蒙古文 35 个基础字母 U+1820–U+1842</option>
      <option value="mongolian_unicode">传统蒙古文 Unicode 区块 U+1800–U+18AF</option>
      <option value="japanese_kana">日文假名 Hiragana + Katakana</option>
      <option value="korean_hangul_sample">韩文 Hangul 抽样 500 个</option>
      <option value="chinese_3500">中文常用 3500 粗略测试</option>
      <option value="chinese_6500">中文 6500 粗略测试，耗时较长</option>
      <option value="custom">自定义字符</option>
    </select>

    <label>自定义字符</label>
    <textarea name="custom_text" placeholder="选择自定义字符时，在这里输入要生成差值的字符。"></textarea>

    <label>SVG 预览文字</label>
    <input type="text" name="preview_text" value="ABCDEabcde123" placeholder="生成完成后用于 SVG 预览的文字">

    <label>中间步数</label>
    <input type="number" name="steps" value="20" min="1" max="100">

    <label>采样点数</label>
    <input type="number" name="sample_points" value="240" min="80" max="600">

    <label>字体家族名称</label>
    <input type="text" name="family_name" value="FontMorphFamily">

    <label>字体家族样式命名方式</label>
    <select name="naming_mode">
      <option value="morph">Morph 01 / Morph 02 / Morph 03 ...</option>
      <option value="weight">Weight 100 / Weight 200 / Weight 300 ...</option>
    </select>

    <label>可变字体</label>
    <select name="variable_font">
      <option value="no">暂不生成可变字体，只生成中间 TTF</option>
      <option value="yes">尝试生成可变字体</option>
    </select>

    <button id="startBtn" type="button" onclick="startUnicodeJob()">开始生成</button>
  </form>

  <div class="progress-wrap" id="progressWrap">
    <div class="progress-bg"><div class="progress-bar" id="progressBar"></div></div>
    <div class="status-line" id="statusLine">等待开始。</div>
    <div class="log" id="logBox"></div>
    <div class="outputs" id="outputs"></div>
    <div class="preview-grid" id="previewGrid"></div>
  </div>
</div>

<script>
let unicodeTimer = null;

function $(id){return document.getElementById(id);}
function setText(id,text){const el=$(id);if(el)el.textContent=text;}
function setProgress(p){const bar=$("progressBar");if(bar)bar.style.width=(p||0)+"%";}

async function startUnicodeJob(){
  const form = $("unicodeForm");
  const btn = $("startBtn");

  const fontA = form.querySelector('input[name="font_a"]').files[0];
  const fontB = form.querySelector('input[name="font_b"]').files[0];

  if(!fontA || !fontB){
    alert("请先选择字体 A 和字体 B。");
    return;
  }

  $("progressWrap").style.display = "block";
  $("outputs").innerHTML = "";
  $("previewGrid").innerHTML = "";
  setProgress(3);
  setText("statusLine","正在提交真实字体差值生成任务...");
  setText("logBox","正在上传字体文件。");
  btn.disabled = true;

  try{
    const fd = new FormData(form);

    const res = await fetch("/api/unicode/start", {
      method:"POST",
      body:fd
    });

    const text = await res.text();

    if(!res.ok){
      throw new Error("提交失败：HTTP " + res.status + "\n" + text);
    }

    const data = JSON.parse(text);

    if(!data.job_id){
      throw new Error("接口没有返回 job_id：" + text);
    }

    pollJob(data.job_id);

  }catch(err){
    setProgress(100);
    setText("statusLine","提交失败。");
    setText("logBox",String(err));
    btn.disabled = false;
  }
}

function pollJob(jobId){
  if(unicodeTimer) clearInterval(unicodeTimer);

  unicodeTimer = setInterval(async ()=>{
    try{
      const res = await fetch("/api/unicode/status/" + jobId + "?t=" + Date.now());
      const job = await res.json();

      const p = job.progress || 0;
      setProgress(p);
      setText("statusLine", "状态：" + job.status + " ｜ 进度：" + p + "% ｜ 共同字符：" + (job.common_count || 0));
      setText("logBox", job.message || "");

      if(job.status === "done"){
        clearInterval(unicodeTimer);
        $("startBtn").disabled = false;

        const out = $("outputs");
        out.innerHTML = "<h3>生成成功</h3>";

        const directPreviewPage = job.preview_page_url || ("/api/unicode/preview_page/" + encodeURIComponent(jobId));
        const directPreviewLink = document.createElement("a");
        directPreviewLink.href = directPreviewPage;
        directPreviewLink.textContent = "打开完整生成预览";
        directPreviewLink.target = "_blank";
        directPreviewLink.className = "preview-entry";
        directPreviewLink.style.fontWeight = "700";
        out.appendChild(directPreviewLink);

        if(job.zip_url){
          const a = document.createElement("a");
          a.href = job.zip_url;
          a.textContent = "下载全部 TTF 字体压缩包";
          a.target = "_blank";
          a.style.fontWeight = "700";
          out.appendChild(a);
        }

        const previewHint = document.createElement("div");
        previewHint.className = "hint";
        previewHint.textContent = "预览页面会展示 SVG 预览图和每一步 TTF 文件，方便先查看效果再下载。";
        out.appendChild(previewHint);

        if(job.preview_urls && job.preview_urls.length){
          const grid = $("previewGrid");
          grid.innerHTML = "";

          job.preview_urls.forEach((url, idx)=>{
            const card = document.createElement("div");
            card.className = "preview-card";

            const title = document.createElement("div");
            title.textContent = "SVG 预览 " + String(idx + 1).padStart(2, "0");
            title.style.fontWeight = "700";
            title.style.marginBottom = "8px";

            const img = document.createElement("img");
            img.src = url + "?t=" + Date.now();

            const link = document.createElement("a");
            link.href = url;
            link.textContent = "下载 SVG";
            link.target = "_blank";

            card.appendChild(title);
            card.appendChild(img);
            card.appendChild(link);
            grid.appendChild(card);
          });
        }
      }

      if(job.status === "error"){
        clearInterval(unicodeTimer);
        $("startBtn").disabled = false;
        setProgress(100);
      }

    }catch(err){
      clearInterval(unicodeTimer);
      $("startBtn").disabled = false;
      setText("statusLine","轮询失败。");
      setText("logBox",String(err));
    }
  },1000);
}
</script>
</body>
</html>
''')


@router.get("/unicode_async")
def unicode_async_page():
    return unicode_page()
