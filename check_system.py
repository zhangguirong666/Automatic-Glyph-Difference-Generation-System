import os
import re
import sys
import json
import socket
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

ROOT = Path.cwd()
REPORT = []

KEYWORDS = [
    "骨架",
    "skeleton",
    "control",
    "point",
    "canvas",
    "drag",
    "pointerdown",
    "pointermove",
    "mousedown",
    "mousemove",
    "elastic",
    "deform",
    "laplacian",
    "smooth",
    "preserve",
    "edge",
    "glyph",
    "font",
    "svg",
    "png"
]

COMMON_PORTS = [18082, 6006, 5173, 7860, 8000, 8080, 5000, 3000]

COMMON_URLS = [
    "http://127.0.0.1:18082/",
    "http://127.0.0.1:18082/index.html",
    "http://127.0.0.1:18082/elastic_skeleton_editor.html",
    "http://127.0.0.1:6006/",
    "http://127.0.0.1:6006/index.html",
    "http://127.0.0.1:5173/",
    "http://127.0.0.1:7860/",
    "http://127.0.0.1:8000/",
    "http://127.0.0.1:8080/",
    "http://127.0.0.1:5000/",
    "http://127.0.0.1:3000/"
]

def add(title, content=""):
    REPORT.append(f"\n{'=' * 80}\n{title}\n{'=' * 80}\n{content}")

def read_text(path, limit=2_000_000):
    try:
        data = path.read_bytes()
        if len(data) > limit:
            data = data[:limit]
        return data.decode("utf-8", errors="ignore")
    except Exception as e:
        return f"[READ_ERROR] {e}"

def list_files():
    lines = []
    lines.append(f"当前目录: {ROOT}")
    lines.append(f"检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("当前目录一级文件：")

    for p in sorted(ROOT.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        kind = "DIR " if p.is_dir() else "FILE"
        size = ""
        if p.is_file():
            size = f"{p.stat().st_size / 1024:.1f} KB"
        lines.append(f"{kind:4}  {p.name:45} {size}")

    add("1. 项目目录概览", "\n".join(lines))

def find_project_files():
    exts = {".html", ".js", ".css", ".py", ".json", ".txt", ".md"}
    files = []

    for p in ROOT.rglob("*"):
        if "__pycache__" in p.parts:
            continue
        if ".git" in p.parts:
            continue
        if p.is_file() and p.suffix.lower() in exts:
            try:
                rel = p.relative_to(ROOT)
            except Exception:
                rel = p
            files.append((str(rel), p.stat().st_size))

    files = sorted(files, key=lambda x: x[0].lower())

    lines = []
    for name, size in files:
        lines.append(f"{name:70} {size / 1024:.1f} KB")

    add("2. 项目内 HTML / JS / CSS / Python 文件", "\n".join(lines) if lines else "没有找到相关文件。")
    return [ROOT / name for name, _ in files]

def detect_entry_files(files):
    names = [p.name for p in files]

    candidates = [
        "index.html",
        "app.py",
        "main.py",
        "server.py",
        "web.py",
        "vite.config.js",
        "package.json",
        "requirements.txt"
    ]

    lines = []
    for c in candidates:
        found = [str(p.relative_to(ROOT)) for p in files if p.name == c]
        if found:
            lines.append(f"[存在] {c}: {', '.join(found)}")
        else:
            lines.append(f"[缺失] {c}")

    add("3. 入口文件检查", "\n".join(lines))

def scan_keywords(files):
    rows = []

    for p in files:
        if p.suffix.lower() not in [".html", ".js", ".py"]:
            continue

        text = read_text(p)
        lower = text.lower()

        hits = []
        for kw in KEYWORDS:
            if kw.lower() in lower:
                hits.append(kw)

        if hits:
            rows.append({
                "file": str(p.relative_to(ROOT)),
                "hits": hits[:20],
                "hit_count": len(hits)
            })

    lines = []
    for r in rows:
        lines.append(f"{r['file']}")
        lines.append(f"  命中关键词数量: {r['hit_count']}")
        lines.append(f"  关键词: {', '.join(r['hits'])}")
        lines.append("")

    add("4. 骨架编辑器 / 字体功能关键词扫描", "\n".join(lines) if lines else "没有扫描到明显相关关键词。")

def scan_functions(files):
    patterns = {
        "拖拽事件": r"(pointerdown|pointermove|pointerup|mousedown|mousemove|mouseup)",
        "Canvas 绘制": r"(getContext\(['\"]2d['\"]\)|canvas)",
        "SVG 导出": r"(exportSVG|toDataURL|Blob|download)",
        "骨架变量": r"(skeleton|edges|points|glyph|controlPoints)",
        "弹性变形": r"(elastic|deform|laplacian|preserve|smooth|influence|radius)",
        "Flask 路由": r"@app\.route|Flask\(",
        "FastAPI 路由": r"FastAPI\(|@app\.(get|post|put|delete)",
        "Gradio": r"gradio|gr\.Blocks|gr\.Interface",
    }

    lines = []

    for p in files:
        if p.suffix.lower() not in [".html", ".js", ".py"]:
            continue

        text = read_text(p)
        rel = str(p.relative_to(ROOT))

        file_hits = []
        for name, pat in patterns.items():
            count = len(re.findall(pat, text, flags=re.I))
            if count:
                file_hits.append(f"{name}: {count}")

        if file_hits:
            lines.append(rel)
            for h in file_hits:
                lines.append(f"  {h}")
            lines.append("")

    add("5. 功能结构扫描", "\n".join(lines) if lines else "未发现明显功能结构。")

def scan_routes(files):
    lines = []

    for p in files:
        if p.suffix.lower() != ".py":
            continue

        text = read_text(p)
        rel = str(p.relative_to(ROOT))

        flask_routes = re.findall(r"@app\.route\(['\"]([^'\"]+)['\"]", text)
        fastapi_routes = re.findall(r"@app\.(get|post|put|delete)\(['\"]([^'\"]+)['\"]", text)

        if flask_routes or fastapi_routes:
            lines.append(rel)

            for r in flask_routes:
                lines.append(f"  Flask route: {r}")

            for method, r in fastapi_routes:
                lines.append(f"  FastAPI {method.upper()}: {r}")

            lines.append("")

    add("6. 后端接口路由扫描", "\n".join(lines) if lines else "未发现 Flask / FastAPI 路由。")

def port_listening(port):
    def parse_proc_net(path):
        results = []
        if not Path(path).exists():
            return results

        port_hex = f"{port:04X}"

        try:
            lines = Path(path).read_text().splitlines()[1:]
        except Exception:
            return results

        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue

            local = parts[1]
            state = parts[3]
            inode = parts[9]

            try:
                local_port = local.split(":")[1].upper()
            except Exception:
                continue

            if local_port == port_hex and state == "0A":
                results.append(inode)

        return results

    inodes = set(parse_proc_net("/proc/net/tcp") + parse_proc_net("/proc/net/tcp6"))
    if not inodes:
        return []

    pids = []

    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue

        fd_dir = Path("/proc") / pid / "fd"
        if not fd_dir.exists():
            continue

        try:
            for fd in fd_dir.iterdir():
                try:
                    link = os.readlink(fd)
                except Exception:
                    continue

                if link.startswith("socket:[") and link[8:-1] in inodes:
                    cmdline_path = Path("/proc") / pid / "cmdline"
                    try:
                        cmd = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
                    except Exception:
                        cmd = ""
                    pids.append((pid, cmd))
        except Exception:
            pass

    return pids

def check_ports():
    lines = []

    for port in COMMON_PORTS:
        owners = port_listening(port)
        if owners:
            lines.append(f"[监听中] {port}")
            for pid, cmd in owners:
                lines.append(f"  PID {pid}: {cmd}")
        else:
            lines.append(f"[未监听] {port}")

    add("7. 常见端口监听检查", "\n".join(lines))

def check_urls():
    lines = []

    for url in COMMON_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "check-system"})
            with urllib.request.urlopen(req, timeout=3) as r:
                status = r.status
                ctype = r.headers.get("Content-Type", "")
                body = r.read(300).decode("utf-8", errors="ignore").replace("\n", " ")
                lines.append(f"[OK] {url}")
                lines.append(f"  status={status}, content-type={ctype}")
                lines.append(f"  preview={body[:180]}")
        except Exception as e:
            lines.append(f"[FAIL] {url}")
            lines.append(f"  {repr(e)}")

    add("8. 本地 URL 访问检查", "\n".join(lines))

def check_html_titles(files):
    lines = []

    for p in files:
        if p.suffix.lower() != ".html":
            continue

        text = read_text(p)
        title = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
        body = re.search(r"<body[^>]*>", text, flags=re.I)

        rel = str(p.relative_to(ROOT))
        lines.append(rel)
        lines.append(f"  title: {title.group(1).strip() if title else '[无 title]'}")
        lines.append(f"  body: {'存在' if body else '缺失'}")

        if "Directory listing for" in text:
            lines.append("  警告: 这是目录列表页面，不是你的可视化界面。")

        lines.append("")

    add("9. HTML 页面检查", "\n".join(lines) if lines else "没有找到 HTML 文件。")

def check_recent_logs():
    logs = list(ROOT.glob("*.log")) + list(ROOT.rglob("*.log"))
    logs = sorted(set(logs), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:10]

    lines = []

    for p in logs:
        rel = str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)
        lines.append(f"--- {rel} ---")
        try:
            content = p.read_text(errors="ignore").splitlines()
            tail = content[-30:]
            lines.extend(tail)
        except Exception as e:
            lines.append(f"[读取失败] {e}")
        lines.append("")

    add("10. 最近日志检查", "\n".join(lines) if lines else "没有找到 log 文件。")

def write_report():
    out = "\n".join(REPORT)
    report_path = ROOT / "check_report.txt"
    report_path.write_text(out, encoding="utf-8")
    print(out)
    print("\n" + "=" * 80)
    print(f"检查完成，报告已保存：{report_path}")
    print("=" * 80)

def main():
    list_files()
    files = find_project_files()
    detect_entry_files(files)
    scan_keywords(files)
    scan_functions(files)
    scan_routes(files)
    check_ports()
    check_urls()
    check_html_titles(files)
    check_recent_logs()
    write_report()

if __name__ == "__main__":
    main()
