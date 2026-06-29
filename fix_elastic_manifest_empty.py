from pathlib import Path
import re

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：当前目录没有 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")

backup = APP.with_suffix(".py.backup_fix_elastic_manifest_empty")
backup.write_text(text, encoding="utf-8")
print(f"已备份 app.py 到：{backup}")

# 1. 让新版弹性编辑器改用新的稳健 manifest 接口
old1 = "fetchJson(`/skeleton_manifest/${JOB_ID}`)"
new1 = "fetchJson(`/elastic_skeleton_manifest/${JOB_ID}`)"

if old1 in text:
    text = text.replace(old1, new1)
    print("已修改：弹性编辑器改用 /elastic_skeleton_manifest/{job_id}")
else:
    text = text.replace("/skeleton_manifest/${JOB_ID}", "/elastic_skeleton_manifest/${JOB_ID}")
    print("已尝试替换所有 /skeleton_manifest/${JOB_ID} 字符串")

MARK = "# ===== ELASTIC_SKELETON_MANIFEST_FIX_V1 ====="

# 2. 追加新的稳健 manifest 接口
if MARK not in text:
    patch = r'''

# ===== ELASTIC_SKELETON_MANIFEST_FIX_V1 =====
from pathlib import Path as _ElasticPath
import re as _elastic_re

@app.get("/elastic_skeleton_manifest/{job_id}")
async def elastic_skeleton_manifest_fix(job_id: str):
    """
    给新版弹性骨架编辑器使用的稳健字形清单接口。
    优先扫描 jobs/{job_id} 里的 svg/json/png 文件名；
    如果扫描失败，则自动兜底为传统蒙古文 U1820-U1842 + step_01-step_20。
    """
    job_dir = _ElasticPath("jobs") / job_id

    code_to_variants = {}

    def norm_code(s):
        if not s:
            return None
        m = _elastic_re.search(r"U([0-9A-Fa-f]{4,6})", str(s))
        if not m:
            return None
        return "U" + m.group(1).upper()

    def norm_step(s):
        if not s:
            return None
        m = _elastic_re.search(r"step[_\-]?(\d+)", str(s), flags=_elastic_re.I)
        if not m:
            return None
        return f"step_{int(m.group(1)):02d}"

    def add(code, variant):
        code = norm_code(code)
        variant = norm_step(variant) or "step_01"
        if not code:
            return
        code_to_variants.setdefault(code, set()).add(variant)

    if job_dir.exists():
        # A. 从文件路径扫描 U1820 / step_01 等信息
        for p in job_dir.rglob("*"):
            if not p.is_file():
                continue

            if p.suffix.lower() not in {".svg", ".json", ".png"}:
                continue

            rel = str(p.relative_to(job_dir))
            code = norm_code(rel)
            step = norm_step(rel)

            if code:
                add(code, step or "step_01")

        # B. 从常见文本文件中扫描
        for name in ["manifest.csv", "manifest.json", "report.txt", "fontforge.log"]:
            f = job_dir / name
            if not f.exists():
                continue

            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            codes = sorted(set(norm_code(x) for x in _elastic_re.findall(r"U[0-9A-Fa-f]{4,6}", content)))
            codes = [c for c in codes if c]

            steps = sorted(set(norm_step(x) for x in _elastic_re.findall(r"step[_\-]?\d+", content, flags=_elastic_re.I)))
            steps = [s for s in steps if s]

            if not steps:
                steps = [f"step_{i:02d}" for i in range(1, 21)]

            for c in codes:
                for s in steps:
                    add(c, s)

    # C. 如果仍然为空，兜底：传统蒙古文 35 个名义字符 + 20 步
    if not code_to_variants:
        fallback_codes = [f"U{cp:04X}" for cp in range(0x1820, 0x1843)]
        fallback_steps = [f"step_{i:02d}" for i in range(1, 21)]

        for c in fallback_codes:
            code_to_variants[c] = set(fallback_steps)

    def code_key(c):
        try:
            return int(c[1:], 16)
        except Exception:
            return 999999

    glyphs = []
    for code in sorted(code_to_variants.keys(), key=code_key):
        variants = sorted(
            code_to_variants[code],
            key=lambda x: int(x.split("_")[-1]) if "_" in x and x.split("_")[-1].isdigit() else 999
        )
        glyphs.append({
            "code": code,
            "variants": variants
        })

    return {
        "job_id": job_id,
        "count": len(glyphs),
        "glyphs": glyphs
    }

# ===== END_ELASTIC_SKELETON_MANIFEST_FIX_V1 =====
'''
    text = text.rstrip() + "\n\n" + patch + "\n"
    print("已添加 /elastic_skeleton_manifest/{job_id} 接口")
else:
    print("已存在 /elastic_skeleton_manifest/{job_id} 接口，不重复添加")

APP.write_text(text, encoding="utf-8")
print("修复完成。")
