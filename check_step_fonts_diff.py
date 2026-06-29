from pathlib import Path
from fontTools.ttLib import TTFont
import hashlib
import re
import sys


def step_no(path: Path):
    m = re.search(r"step[_-]?0*(\d+)", path.name, re.I)
    if m:
        return int(m.group(1))
    return 999999


def best_cmap(font):
    best = None
    score = -1
    if "cmap" not in font:
        return {}
    for t in font["cmap"].tables:
        if not t.isUnicode():
            continue
        s = len(t.cmap)
        if t.platformID == 3:
            s += 1000000
        if s > score:
            best = t.cmap
            score = s
    return dict(best or {})


def glyph_digest(font_path, codepoints):
    font = TTFont(str(font_path))
    cmap = best_cmap(font)
    glyf = font["glyf"]

    h = hashlib.sha256()
    existing = 0

    for cp in codepoints:
        gn = cmap.get(cp)
        if not gn or gn not in glyf:
            continue

        g = glyf[gn]
        try:
            coords, end_pts, flags = g.getCoordinates(glyf)
        except Exception:
            continue

        existing += 1
        h.update(f"U+{cp:04X}:{gn}:".encode())
        h.update(str(list(end_pts)).encode())
        h.update(str([(int(x), int(y)) for x, y in coords]).encode())
        h.update(str([int(f) & 1 for f in flags]).encode())

    return h.hexdigest(), existing


root = Path(".").resolve()

step_fonts = sorted(
    [
        p for p in root.rglob("*.ttf")
        if re.search(r"step[_-]?0*\d+", p.name, re.I)
        and "variable" not in p.name.lower()
        and "realmorph" not in p.name.lower()
        and "generatedmorph" not in p.name.lower()
    ],
    key=step_no
)

# 优先取最近修改的一批 20 个
step_fonts = sorted(step_fonts, key=lambda p: p.stat().st_mtime, reverse=True)[:40]
step_fonts = sorted(step_fonts, key=step_no)

if not step_fonts:
    print("[ERROR] 没找到 step_XX.ttf 文件")
    sys.exit(1)

print("检测到 Step 字体：")
for p in step_fonts:
    print(" -", p)

# 传统蒙古文基础字母优先
codepoints = list(range(0x1820, 0x1843))

print("\n开始检测蒙古文基础字母 glyf 轮廓哈希：")
digests = []

for p in step_fonts:
    d, n = glyph_digest(p, codepoints)
    digests.append(d)
    print(f"{p.name:35s} glyphs={n:3d} digest={d[:16]}")

unique = len(set(digests))

print("\n==============================")
print("不同轮廓版本数量：", unique)
print("Step 文件数量：", len(step_fonts))

if unique <= 1:
    print("[结论] 这些 Step 字体的真实轮廓完全一样。问题在第一步插值生成算法。")
else:
    print("[结论] Step 字体真实轮廓存在差异。若页面不变，则是预览缓存或页面取错字体。")
print("==============================")
