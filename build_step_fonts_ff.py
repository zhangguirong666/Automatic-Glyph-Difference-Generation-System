# -*- coding: utf-8 -*-
import os
import re
import sys
import fontforge
import psMat

if len(sys.argv) < 5:
    print("Usage:")
    print("fontforge -script build_step_fonts_ff.py <svg_root> <font_out_dir> <chars_file> <steps> <font_family> [style_mode]")
    sys.exit(1)

SVG_ROOT = sys.argv[1]
FONT_OUT_DIR = sys.argv[2]
CHARS_FILE = sys.argv[3]
STEPS = int(sys.argv[4])
FONT_FAMILY = sys.argv[5] if len(sys.argv) > 5 else "FontMorphFamily"
STYLE_MODE = sys.argv[6] if len(sys.argv) > 6 else "morph"

EM_SIZE = 1000
GLYPH_WIDTH = 1000

os.makedirs(FONT_OUT_DIR, exist_ok=True)


def clean_family_name(name):
    name = name.strip()
    if not name:
        name = "FontMorphFamily"
    name = re.sub(r"[^\w\u4e00-\u9fff\- ]+", "", name)
    name = name.strip()
    if not name:
        name = "FontMorphFamily"
    return name


def clean_ps_name(name):
    name = re.sub(r"\s+", "", name)
    name = re.sub(r"[^A-Za-z0-9\-]+", "", name)
    if not name:
        name = "FontMorph"
    return name


FONT_FAMILY = clean_family_name(FONT_FAMILY)
FONT_FAMILY_PS = clean_ps_name(FONT_FAMILY)


def read_codepoints(path):
    cps = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            cps.append(int(s, 16))
    return cps


CODEPOINTS = read_codepoints(CHARS_FILE)


def style_name_for_step(step):
    if STYLE_MODE == "step":
        return "Step %02d" % step

    if STYLE_MODE == "weight":
        if STEPS <= 1:
            weight = 400
        else:
            weight = int(round(100 + (step - 1) * (800 / max(STEPS - 1, 1))))
        weight = int(round(weight / 100.0) * 100)
        weight = max(100, min(900, weight))
        return "Weight %03d" % weight

    # 默认 morph：把中间步映射到 0-100 的 Morph 轴百分比
    morph_value = int(round(step / (STEPS + 1) * 100))
    return "Morph %03d" % morph_value


def file_name_for_step(step):
    style = style_name_for_step(step)
    style_ps = clean_ps_name(style)
    return "%s-%s.ttf" % (FONT_FAMILY_PS, style_ps)


def weight_for_step(step):
    if STYLE_MODE == "weight":
        if STEPS <= 1:
            return 400
        weight = int(round(100 + (step - 1) * (800 / max(STEPS - 1, 1))))
        weight = int(round(weight / 100.0) * 100)
        return max(100, min(900, weight))

    # 非 Weight 模式也给一个稳定值
    return 400


def normalize_glyph(glyph):
    try:
        bbox = glyph.boundingBox()
        x_min, y_min, x_max, y_max = bbox
        w = x_max - x_min
        h = y_max - y_min

        if w <= 0 or h <= 0:
            glyph.width = GLYPH_WIDTH
            return

        scale = min(820.0 / h, 820.0 / w)

        glyph.transform(psMat.translate(-x_min, -y_min))
        glyph.transform(psMat.scale(scale))

        bbox = glyph.boundingBox()
        x_min, y_min, x_max, y_max = bbox
        w = x_max - x_min
        h = y_max - y_min

        x_offset = (GLYPH_WIDTH - w) / 2.0
        y_offset = 90.0

        glyph.transform(psMat.translate(x_offset - x_min, y_offset - y_min))
        glyph.width = GLYPH_WIDTH

    except Exception as e:
        print("[WARN] normalize failed:", e)
        glyph.width = GLYPH_WIDTH


def set_font_names(font, step):
    style = style_name_for_step(step)
    style_ps = clean_ps_name(style)
    ps_name = "%s-%s" % (FONT_FAMILY_PS, style_ps)
    full_name = "%s %s" % (FONT_FAMILY, style)

    font.familyname = FONT_FAMILY
    font.fontname = ps_name
    font.fullname = full_name
    font.weight = style

    try:
        font.os2_weight = weight_for_step(step)
    except Exception:
        pass

    # 写 name table，尽量让设计软件识别为一个字体家族
    try:
        font.appendSFNTName("English (US)", "Family", FONT_FAMILY)
        font.appendSFNTName("English (US)", "SubFamily", style)
        font.appendSFNTName("English (US)", "Fullname", full_name)
        font.appendSFNTName("English (US)", "PostScriptName", ps_name)
        font.appendSFNTName("English (US)", "Preferred Family", FONT_FAMILY)
        font.appendSFNTName("English (US)", "Preferred Styles", style)
        font.appendSFNTName("English (US)", "Compatible Full", full_name)
    except Exception as e:
        print("[WARN] appendSFNTName failed:", e)


def build_one_font(step):
    font = fontforge.font()
    font.encoding = "UnicodeFull"
    font.em = EM_SIZE
    font.ascent = 800
    font.descent = 200

    set_font_names(font, step)

    success = 0
    missing = []

    for cp in CODEPOINTS:
        code = "U%04X" % cp
        svg_path = os.path.join(SVG_ROOT, code, "%s_step_%02d.svg" % (code, step))

        if not os.path.exists(svg_path):
            missing.append(code)
            continue

        try:
            glyph = font.createChar(cp, code)
            glyph.width = GLYPH_WIDTH
            glyph.importOutlines(svg_path)
            glyph.correctDirection()
            glyph.removeOverlap()
            glyph.simplify()
            normalize_glyph(glyph)
            success += 1
        except Exception as e:
            print("[FAIL] %s step_%02d: %s" % (code, step, e))
            missing.append(code)

    out_ttf = os.path.join(FONT_OUT_DIR, file_name_for_step(step))

    if success == 0:
        print("[ERROR] step_%02d has 0 glyphs. Font not generated." % step)
        font.close()
        return

    font.generate(out_ttf)
    font.close()

    print("[OK] step_%02d -> %s, glyphs=%d, missing=%d, style=%s" % (
        step, out_ttf, success, len(missing), style_name_for_step(step)
    ))


def main():
    print("SVG_ROOT:", SVG_ROOT)
    print("FONT_OUT_DIR:", FONT_OUT_DIR)
    print("CHARS:", len(CODEPOINTS))
    print("STEPS:", STEPS)
    print("FONT_FAMILY:", FONT_FAMILY)
    print("STYLE_MODE:", STYLE_MODE)

    for step in range(1, STEPS + 1):
        build_one_font(step)

    print("Done.")


if __name__ == "__main__":
    main()
