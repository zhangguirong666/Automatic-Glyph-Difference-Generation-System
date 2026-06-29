from __future__ import annotations

import csv
import json
import os
import copy
import zipfile
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates

try:
    import uharfbuzz as hb
except Exception as e:
    raise RuntimeError("缺少 uharfbuzz，请先 pip install uharfbuzz") from e


CHARSET_CSV = Path("data/mongolian_gb_charset.csv")
LIGA_TABLE6 = Path("data/gbt25914_table6_mandatory_ligatures_strict.csv")

FONT_DIR = Path(os.environ.get("MENK_FONT_DIR", "input/foundry_menk"))
FALLBACK_FONT_DIR = Path("input/fonts")

OUT_DIR = Path("output/menk_gb_ttf_steps")
OUT_DIR.mkdir(parents=True, exist_ok=True)

RUNTIME_CSV = OUT_DIR / "menk_gb_runtime.csv"
RUNTIME_JSON = OUT_DIR / "menk_gb_runtime.json"
COVERAGE_CSV = OUT_DIR / "menk_gb_coverage_report.csv"
REPORT_CSV = OUT_DIR / "build_report.csv"
ZIP_PATH = Path("output/menk_gb_ttf_steps.zip")

STEPS = int(os.environ.get("MGB_STEPS", "20"))
if STEPS < 2:
    STEPS = 2


RUNTIME_FIELDS = [
    "runtime_id",
    "display_group",
    "category",
    "subtype",
    "gb_code",
    "base_unicode",
    "base_name",
    "text_codepoints",
    "fontA",
    "fontA_target_glyph_ids",
    "fontA_target_glyph_names",
    "fontB",
    "fontB_target_glyph_ids",
    "fontB_target_glyph_names",
    "usable_for_ttf",
    "source",
    "note",
]

COVERAGE_FIELDS = [
    "gb_item_id",
    "gb_table",
    "gb_code",
    "category",
    "subtype",
    "unicode_code",
    "unicode_name",
    "status",
    "reason",
    "fontA_glyph",
    "fontB_glyph",
]


def read_csv(path: Path):
    with path.open("r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def find_fonts():
    """
    蒙科立 流程只读取自己的字体目录。
    禁止 fallback 到其他字体公司目录，避免误用字体。
    """
    FONT_DIR.mkdir(parents=True, exist_ok=True)

    fonts = []
    for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
        fonts.extend(FONT_DIR.glob(ext))

    fonts = sorted(fonts)

    if len(fonts) < 2:
        raise SystemExit(
            "蒙科立目录中不足两个字体文件。请从网页上传两个蒙科立字体，"
            "或手动放入：" + str(FONT_DIR)
        )

    forbidden_keywords = ['oyun', '奥云']

    def read_font_identity(fp):
        parts = [fp.name]
        try:
            font = TTFont(str(fp), lazy=True)
            if "name" in font:
                for n in font["name"].names:
                    if n.nameID in [1, 2, 4, 6]:
                        try:
                            parts.append(n.toUnicode())
                        except Exception:
                            pass
        except Exception:
            pass
        return " ".join(parts).lower()

    bad_files = []
    for fp in fonts[:2]:
        ident = read_font_identity(fp)
        for kw in forbidden_keywords:
            if kw.lower() in ident:
                bad_files.append(fp.name)
                break

    if bad_files:
        raise SystemExit(
            "蒙科立流程检测到疑似其他字体公司的字体，已拒绝生成：" +
            ", ".join(bad_files) +
            "。请清空对应目录后重新上传正确字体。"
        )

    return fonts[0], fonts[1]


def load_cmap(font: TTFont):
    cmap = {}
    if "cmap" not in font:
        return cmap

    for table in font["cmap"].tables:
        cmap.update(table.cmap)

    return cmap


def shape_gids(font_path: Path, text: str):
    data = font_path.read_bytes()
    face = hb.Face(data)
    font = hb.Font(face)

    buf = hb.Buffer()
    buf.add_str(text)
    buf.direction = "ltr"
    buf.script = "mong"
    buf.language = "mn"

    hb.shape(font, buf, {
        "ccmp": True,
        "isol": True,
        "init": True,
        "medi": True,
        "fina": True,
        "rlig": True,
        "liga": True,
        "calt": True,
    })

    return [info.codepoint for info in buf.glyph_infos]


def glyph_names(font: TTFont, gids: list[int]):
    order = font.getGlyphOrder()
    out = []

    for gid in gids:
        if 0 <= gid < len(order):
            out.append(order[gid])
        else:
            out.append("")

    return out


def ustr(text: str):
    return " ".join(f"U+{ord(c):04X}" for c in text)


def text_from_codepoints(seq: str):
    out = []
    for part in str(seq).replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        if part.upper().startswith("U+"):
            cp = int(part[2:], 16)
        else:
            cp = int(part, 16)
        out.append(chr(cp))
    return "".join(out)


def add_runtime(rows, **kw):
    row = {k: "" for k in RUNTIME_FIELDS}
    row.update(kw)
    rows.append(row)


def build_nominal_by_gb_charset(font_a_path, font_b_path, font_a, font_b):
    if not CHARSET_CSV.exists():
        raise SystemExit("缺少 data/mongolian_gb_charset.csv，请先生成中国国标清单。")

    items = read_csv(CHARSET_CSV)

    cmap_a = load_cmap(font_a)
    cmap_b = load_cmap(font_b)

    runtime = []
    coverage = []

    for item in items:
        if item.get("category") != "nominal":
            continue

        uc = item.get("unicode_code", "")
        cp = None

        if uc.startswith("U+"):
            try:
                cp = int(uc[2:], 16)
            except Exception:
                cp = None

        if cp is None:
            coverage.append({
                "gb_item_id": item.get("item_id", ""),
                "gb_table": item.get("gb_table", ""),
                "gb_code": item.get("gb_code", ""),
                "category": item.get("category", ""),
                "subtype": item.get("subtype", ""),
                "unicode_code": uc,
                "unicode_name": item.get("unicode_name", ""),
                "status": "skip",
                "reason": "无有效 Unicode 码位",
                "fontA_glyph": "",
                "fontB_glyph": "",
            })
            continue

        g_a = cmap_a.get(cp, "")
        g_b = cmap_b.get(cp, "")

        if not g_a or not g_b:
            coverage.append({
                "gb_item_id": item.get("item_id", ""),
                "gb_table": item.get("gb_table", ""),
                "gb_code": item.get("gb_code", ""),
                "category": item.get("category", ""),
                "subtype": item.get("subtype", ""),
                "unicode_code": uc,
                "unicode_name": item.get("unicode_name", ""),
                "status": "skip",
                "reason": "蒙科立两个字体中至少一方缺失该名义字符",
                "fontA_glyph": g_a,
                "fontB_glyph": g_b,
            })
            continue

        coverage.append({
            "gb_item_id": item.get("item_id", ""),
            "gb_table": item.get("gb_table", ""),
            "gb_code": item.get("gb_code", ""),
            "category": item.get("category", ""),
            "subtype": item.get("subtype", ""),
            "unicode_code": uc,
            "unicode_name": item.get("unicode_name", ""),
            "status": "include",
            "reason": "蒙科立两个字体均支持该名义字符",
            "fontA_glyph": g_a,
            "fontB_glyph": g_b,
        })

        add_runtime(
            runtime,
            runtime_id="MENK_NOM_" + uc.replace("+", ""),
            display_group="名义字符",
            category="nominal",
            subtype=item.get("subtype", ""),
            gb_code=item.get("gb_code", ""),
            base_unicode=uc,
            base_name=item.get("unicode_name", ""),
            text_codepoints=uc,
            fontA=font_a_path.name,
            fontA_target_glyph_names=g_a,
            fontB=font_b_path.name,
            fontB_target_glyph_names=g_b,
            usable_for_ttf="1",
            source="menk_cmap_vs_gb_charset",
            note="蒙科立字体中存在的国标名义字符；不存在的已跳过。",
        )

    return runtime, coverage


def build_core_35(font_a_path, font_b_path, font_a, font_b):
    rows = []

    for cp in range(0x1820, 0x1843):
        code = f"U+{cp:04X}"
        ch = chr(cp)

        try:
            gids_a = shape_gids(font_a_path, ch)
            gids_b = shape_gids(font_b_path, ch)
            names_a = glyph_names(font_a, gids_a)
            names_b = glyph_names(font_b, gids_b)
        except Exception:
            continue

        if not gids_a or not gids_b:
            continue

        add_runtime(
            rows,
            runtime_id=f"MENK_CORE_U{cp:04X}",
            display_group="传统蒙古文35基础字母",
            category="nominal_core_35",
            subtype="letter",
            gb_code=f"{cp:04X}",
            base_unicode=code,
            base_name=f"Traditional Mongolian core letter {code}",
            text_codepoints=code,
            fontA=font_a_path.name,
            fontA_target_glyph_ids=str(gids_a[0]),
            fontA_target_glyph_names=names_a[0] if names_a else "",
            fontB=font_b_path.name,
            fontB_target_glyph_ids=str(gids_b[0]),
            fontB_target_glyph_names=names_b[0] if names_b else "",
            usable_for_ttf="1",
            source="menk_core_35_shaping",
            note="原来的35个传统蒙古文基础字母，蒙科立字体有则加入。",
        )

    return rows


def build_presentation_by_menk_shaping(font_a_path, font_b_path, font_a, font_b):
    A = "\u1820"
    fvs = {
        "fvs1": "\u180B",
        "fvs2": "\u180C",
        "fvs3": "\u180D",
        "fvs4": "\u180F",
    }

    rows = []

    for cp in range(0x1820, 0x1843):
        ch = chr(cp)
        code = f"U+{cp:04X}"

        local_probes = [
            ("isol", ch, "all"),
            ("init", ch + A, "first"),
            ("medi", A + ch + A, "middle"),
            ("fina", A + ch, "last"),
        ]

        for name, f in fvs.items():
            local_probes.append((name, ch + f, "first"))

        for subtype, text, rule in local_probes:
            try:
                gids_a = shape_gids(font_a_path, text)
                gids_b = shape_gids(font_b_path, text)
            except Exception:
                continue

            if not gids_a or not gids_b:
                continue

            def pick(gids):
                if rule == "all":
                    return gids[:1]
                if rule == "first":
                    return gids[:1]
                if rule == "last":
                    return gids[-1:]
                if rule == "middle":
                    if len(gids) >= 3:
                        return [gids[len(gids)//2]]
                    return gids[:1]
                return gids[:1]

            ta = pick(gids_a)
            tb = pick(gids_b)

            if len(ta) != 1 or len(tb) != 1:
                continue

            na = glyph_names(font_a, ta)
            nb = glyph_names(font_b, tb)

            add_runtime(
                rows,
                runtime_id=f"MENK_PRES_{code.replace('+','')}_{subtype}",
                display_group="单个变形显现字符",
                category="presentation",
                subtype=subtype,
                gb_code="",
                base_unicode=code,
                base_name=f"Menk presentation form {code} {subtype}",
                text_codepoints=ustr(text),
                fontA=font_a_path.name,
                fontA_target_glyph_ids=str(ta[0]),
                fontA_target_glyph_names=na[0] if na else "",
                fontB=font_b_path.name,
                fontB_target_glyph_ids=str(tb[0]),
                fontB_target_glyph_names=nb[0] if nb else "",
                usable_for_ttf="1",
                source="menk_harfbuzz_presentation",
                note="按蒙科立字体 shaping 规则提取的显现形式；两字体都有则加入。",
            )

    return rows


def build_verified_table6(font_a_path, font_b_path, font_a, font_b):
    rows = []

    if not LIGA_TABLE6.exists():
        return rows

    for item in read_csv(LIGA_TABLE6):
        if item.get("verified") != "1":
            continue

        seq = item.get("unicode_sequence", "").strip()
        if not seq:
            continue

        try:
            text = text_from_codepoints(seq)
            gids_a = shape_gids(font_a_path, text)
            gids_b = shape_gids(font_b_path, text)
            names_a = glyph_names(font_a, gids_a)
            names_b = glyph_names(font_b, gids_b)
        except Exception:
            continue

        if not gids_a or not gids_b:
            continue

        if len(gids_a) != len(gids_b):
            continue

        add_runtime(
            rows,
            runtime_id=item.get("liga_id", "MENK_LIGA_" + item.get("gb_code", "")),
            display_group="强制性合体字",
            category="ligature",
            subtype="mandatory_ligature",
            gb_code=item.get("gb_code", ""),
            base_unicode=seq,
            base_name=item.get("standard_name", item.get("description", "")),
            text_codepoints=ustr(text),
            fontA=font_a_path.name,
            fontA_target_glyph_ids=" ".join(str(x) for x in gids_a),
            fontA_target_glyph_names=" | ".join(names_a),
            fontB=font_b_path.name,
            fontB_target_glyph_ids=" ".join(str(x) for x in gids_b),
            fontB_target_glyph_names=" | ".join(names_b),
            usable_for_ttf="1",
            source="menk_verified_table6",
            note="表6中已人工验证的强制性合体字。",
        )

    return rows




# =========================================================
# AUTO_IMPORT_GSUB_LIGATURES_V1
# 自动从字体 GSUB LookupType 4 中导入字体内置合体字。
# 说明：
# - 这是字体公司内部真实存在的 GSUB 合体字；
# - 两个字体都有同一组输入序列时才进入 runtime；
# - 如果要严格绑定 GB/T 表6编号，还需要后续把 gb_code 逐项补入。
# =========================================================
def _gsub_inverse_cmap(font):
    inv = {}
    if "cmap" not in font:
        return inv
    for table in font["cmap"].tables:
        for cp, gname in table.cmap.items():
            inv.setdefault(gname, cp)
    return inv


def _gsub_extract_ligatures(font):
    """
    返回：
    {
      key: {
        "seq_glyphs": [...],
        "seq_codepoints": "U+1820 U+1821",
        "lig_glyph": "...",
        "lookup_index": "...",
        "feature": "rlig/liga/..."
      }
    }
    """
    result = {}

    if "GSUB" not in font:
        return result

    inv_cmap = _gsub_inverse_cmap(font)

    try:
        gsub = font["GSUB"].table
        lookup_list = gsub.LookupList
        feature_list = gsub.FeatureList
    except Exception:
        return result

    if not lookup_list:
        return result

    # 优先收集 rlig / liga / clig / ccmp / calt 里的 LookupType 4。
    feature_lookup_map = {}
    preferred_tags = {"rlig", "liga", "clig", "ccmp", "calt"}

    if feature_list:
        for frec in feature_list.FeatureRecord:
            tag = frec.FeatureTag
            if tag not in preferred_tags:
                continue
            for li in frec.Feature.LookupListIndex or []:
                feature_lookup_map.setdefault(li, set()).add(tag)

    lookup_indices = sorted(feature_lookup_map.keys())

    # 如果 feature 没有显式列出，则扫描全部 LookupType 4。
    if not lookup_indices:
        lookup_indices = list(range(len(lookup_list.Lookup)))

    for li in lookup_indices:
        lookup = lookup_list.Lookup[li]
        if lookup.LookupType != 4:
            continue

        feature_tags = ",".join(sorted(feature_lookup_map.get(li, {"GSUB4"})))

        for subtable in lookup.SubTable or []:
            try:
                coverage = subtable.Coverage.glyphs
                ligature_sets = subtable.ligatures
            except Exception:
                continue

            for first_glyph, lig_set in zip(coverage, ligature_sets):
                for lig in lig_set:
                    seq_glyphs = [first_glyph] + list(lig.Component)
                    lig_glyph = lig.LigGlyph

                    key_parts = []
                    cp_parts = []
                    has_mongolian_cp = False

                    for g in seq_glyphs:
                        cp = inv_cmap.get(g)

                        if cp is not None:
                            cp_text = f"U+{cp:04X}" if cp <= 0xFFFF else f"U+{cp:06X}"
                            key_parts.append(cp_text)
                            cp_parts.append(cp_text)

                            if (0x1800 <= cp <= 0x18AF) or (0x11660 <= cp <= 0x1167F):
                                has_mongolian_cp = True
                        else:
                            key_parts.append(g)

                    # 过滤：必须和蒙古文相关，避免把英文字体 ligature 也导入。
                    raw_key = " ".join(key_parts)
                    raw_seq = " ".join(seq_glyphs).lower()
                    raw_lig = lig_glyph.lower()

                    looks_mongolian = (
                        has_mongolian_cp
                        or "mong" in raw_key.lower()
                        or "mong" in raw_seq
                        or "mong" in raw_lig
                        or "uni18" in raw_key.lower()
                        or "uni18" in raw_seq
                    )

                    if not looks_mongolian:
                        continue

                    result[raw_key] = {
                        "seq_glyphs": " ".join(seq_glyphs),
                        "seq_codepoints": " ".join(cp_parts),
                        "lig_glyph": lig_glyph,
                        "lookup_index": str(li),
                        "feature": feature_tags,
                    }

    return result


def build_gsub_ligatures(font_a_path, font_b_path, font_a, font_b, prefix="FOUNDRY"):
    """
    从两个字体共同支持的 GSUB 合体字中生成 runtime rows。
    """
    map_a = _gsub_extract_ligatures(font_a)
    map_b = _gsub_extract_ligatures(font_b)

    common_keys = sorted(set(map_a) & set(map_b))
    rows = []

    for idx, key in enumerate(common_keys, 1):
        a = map_a[key]
        b = map_b[key]

        seq_codepoints = a.get("seq_codepoints", "") or b.get("seq_codepoints", "")
        text_codepoints = seq_codepoints if seq_codepoints else key

        add_runtime(
            rows,
            runtime_id=f"{prefix}_GSUB_LIGA_{idx:04d}",
            display_group="强制性合体字",
            category="ligature",
            subtype="gsub_ligature",
            gb_code="",
            base_unicode=seq_codepoints,
            base_name=f"GSUB ligature: {key}",
            text_codepoints=text_codepoints,
            fontA=font_a_path.name,
            fontA_target_glyph_ids="",
            fontA_target_glyph_names=a["lig_glyph"],
            fontB=font_b_path.name,
            fontB_target_glyph_ids="",
            fontB_target_glyph_names=b["lig_glyph"],
            usable_for_ttf="1",
            source=f"{prefix.lower()}_gsub_ligature_auto",
            note=(
                f"从字体 GSUB LookupType 4 自动导入。"
                f"feature={a.get('feature','')}; "
                f"fontA_seq={a.get('seq_glyphs','')}; "
                f"fontB_seq={b.get('seq_glyphs','')}; "
                f"后续可再绑定 GB/T 表6 gb_code。"
            ),
        )

    print(f"[GSUB-LIGA] {prefix}: fontA ligatures={len(map_a)}, fontB ligatures={len(map_b)}, common={len(rows)}")
    return rows




# =========================================================
# APPENDIX_F_OPTIONAL_LIGATURES_V1
# 附录F是资料性附录：非强制性合体字示例。
# 仅作为“非强制性合体字（附录F示例）”进入预览，不计入强制性合体字。
# =========================================================
APPENDIX_F_OPTIONAL = Path("data/gbt25914_appendix_f_optional_ligatures.csv")

def build_appendix_f_optional_ligatures(font_a_path, font_b_path, font_a, font_b):
    rows = []
    if not APPENDIX_F_OPTIONAL.exists():
        return rows

    for item in read_csv(APPENDIX_F_OPTIONAL):
        if item.get("verified") != "1":
            continue

        seq = item.get("unicode_sequence", "").strip()
        if not seq:
            continue

        try:
            text = text_from_codepoints(seq)
            gids_a = shape_gids(font_a_path, text)
            gids_b = shape_gids(font_b_path, text)
            names_a = glyph_names(font_a, gids_a)
            names_b = glyph_names(font_b, gids_b)
        except Exception:
            continue

        if not gids_a or not gids_b:
            continue

        add_runtime(
            rows,
            runtime_id="APPF_" + item.get("gb_code", ""),
            display_group="非强制性合体字（附录F示例）",
            category="optional_ligature",
            subtype="appendix_f_example",
            gb_code=item.get("gb_code", ""),
            base_unicode=seq,
            base_name=item.get("standard_name", ""),
            text_codepoints=ustr(text),
            fontA=font_a_path.name,
            fontA_target_glyph_ids=" ".join(str(x) for x in gids_a),
            fontA_target_glyph_names=" | ".join(names_a),
            fontB=font_b_path.name,
            fontB_target_glyph_ids=" ".join(str(x) for x in gids_b),
            fontB_target_glyph_names=" | ".join(names_b),
            usable_for_ttf="1",
            source="gbt25914_appendix_f_informative",
            note="GB/T 25914-2023 附录F资料性示例，非强制项，不计入强制性合体字数量。",
        )

    print("[APPENDIX-F] optional examples:", len(rows))
    return rows


def first_name(s: str):
    if not s:
        return ""
    return s.split("|")[0].strip()


def gid_to_name(font: TTFont, gid_str: str):
    parts = str(gid_str).strip().split()
    if len(parts) != 1:
        return ""
    try:
        gid = int(parts[0])
    except Exception:
        return ""

    order = font.getGlyphOrder()
    if 0 <= gid < len(order):
        return order[gid]
    return ""


def compatible_glyph(g1, g2):
    if g1.isComposite() or g2.isComposite():
        return False
    if not hasattr(g1, "coordinates") or not hasattr(g2, "coordinates"):
        return False
    if len(g1.coordinates) != len(g2.coordinates):
        return False
    if list(g1.endPtsOfContours) != list(g2.endPtsOfContours):
        return False
    return True


def interpolate_glyph(g1, g2, t: float):
    ng = copy.deepcopy(g1)
    coords = []

    for p1, p2 in zip(g1.coordinates, g2.coordinates):
        x = p1[0] + (p2[0] - p1[0]) * t
        y = p1[1] + (p2[1] - p1[1]) * t
        coords.append((round(x), round(y)))

    ng.coordinates = GlyphCoordinates(coords)

    try:
        ng.recalcBounds(glyfTable=None)
    except Exception:
        pass

    return ng


def update_names(font: TTFont, family_name: str):
    try:
        for n in font["name"].names:
            if n.nameID in [1, 4, 6]:
                try:
                    txt = family_name.replace(" ", "") if n.nameID == 6 else family_name
                    n.string = txt.encode(n.getEncoding())
                except Exception:
                    pass
    except Exception:
        pass


def build_ttf_steps(font_a_path, font_b_path, font_a, font_b, runtime):
    if "glyf" not in font_a or "glyf" not in font_b:
        raise SystemExit("当前脚本只支持 TrueType glyf 轮廓字体。")

    report = []

    for step in range(1, STEPS + 1):
        t = (step - 1) / (STEPS - 1)

        out = TTFont(str(font_a_path), lazy=False)

        interpolated = 0
        skipped_missing = 0
        skipped_incompatible = 0
        skipped_multi = 0

        for r in runtime:
            if r.get("usable_for_ttf") != "1":
                continue

            name_a = first_name(r.get("fontA_target_glyph_names", ""))
            name_b = first_name(r.get("fontB_target_glyph_names", ""))

            if not name_a:
                name_a = gid_to_name(font_a, r.get("fontA_target_glyph_ids", ""))
            if not name_b:
                name_b = gid_to_name(font_b, r.get("fontB_target_glyph_ids", ""))

            if " " in str(r.get("fontA_target_glyph_ids", "")).strip() or " " in str(r.get("fontB_target_glyph_ids", "")).strip():
                skipped_multi += 1
                continue

            if not name_a or not name_b:
                skipped_missing += 1
                continue

            if name_a not in out["glyf"] or name_a not in font_a["glyf"] or name_b not in font_b["glyf"]:
                skipped_missing += 1
                continue

            g1 = font_a["glyf"][name_a]
            g2 = font_b["glyf"][name_b]

            if not compatible_glyph(g1, g2):
                skipped_incompatible += 1
                continue

            try:
                out["glyf"][name_a] = interpolate_glyph(g1, g2, t)

                if "hmtx" in out and "hmtx" in font_a and "hmtx" in font_b:
                    if name_a in font_a["hmtx"].metrics and name_b in font_b["hmtx"].metrics:
                        aw1, lsb1 = font_a["hmtx"].metrics[name_a]
                        aw2, lsb2 = font_b["hmtx"].metrics[name_b]
                        out["hmtx"].metrics[name_a] = (
                            round(aw1 + (aw2 - aw1) * t),
                            round(lsb1 + (lsb2 - lsb1) * t),
                        )

                interpolated += 1
            except Exception:
                skipped_incompatible += 1

        family = f"Menk GB Step {step:02d}"
        update_names(out, family)

        out_path = OUT_DIR / f"menk_gb_step_{step:02d}.ttf"
        out.save(str(out_path))

        report.append({
            "step": step,
            "ttf": str(out_path),
            "runtime_items": len(runtime),
            "interpolated": interpolated,
            "skipped_missing": skipped_missing,
            "skipped_incompatible": skipped_incompatible,
            "skipped_multi_glyph_sequence": skipped_multi,
        })

        print(
            f"[OK] step {step:02d}: interpolated={interpolated}, "
            f"missing={skipped_missing}, incompatible={skipped_incompatible}, multi={skipped_multi}"
        )

    write_csv(REPORT_CSV, report, [
        "step",
        "ttf",
        "runtime_items",
        "interpolated",
        "skipped_missing",
        "skipped_incompatible",
        "skipped_multi_glyph_sequence",
    ])


def zip_outputs():
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()

    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in OUT_DIR.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(OUT_DIR.parent))

    return ZIP_PATH


def main():
    font_a_path, font_b_path = find_fonts()

    font_a = TTFont(str(font_a_path), lazy=False)
    font_b = TTFont(str(font_b_path), lazy=False)

    runtime = []
    coverage_all = []

    nominal_runtime, nominal_coverage = build_nominal_by_gb_charset(font_a_path, font_b_path, font_a, font_b)
    core_runtime = build_core_35(font_a_path, font_b_path, font_a, font_b)
    presentation_runtime = build_presentation_by_menk_shaping(font_a_path, font_b_path, font_a, font_b)
    liga_runtime = build_verified_table6(font_a_path, font_b_path, font_a, font_b)
    appendix_f_runtime = build_appendix_f_optional_ligatures(font_a_path, font_b_path, font_a, font_b)
    gsub_liga_runtime = build_gsub_ligatures(font_a_path, font_b_path, font_a, font_b, prefix="MENK")

    runtime.extend(core_runtime)
    runtime.extend(nominal_runtime)
    runtime.extend(presentation_runtime)
    runtime.extend(liga_runtime)
    runtime.extend(appendix_f_runtime)
    runtime.extend(gsub_liga_runtime)
    coverage_all.extend(nominal_coverage)

    seen = set()
    unique = []

    for r in runtime:
        key = r["runtime_id"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    runtime = unique

    write_csv(RUNTIME_CSV, runtime, RUNTIME_FIELDS)
    write_json(RUNTIME_JSON, runtime)
    write_csv(COVERAGE_CSV, coverage_all, COVERAGE_FIELDS)

    counts = {}
    for r in runtime:
        counts[r["display_group"]] = counts.get(r["display_group"], 0) + 1

    print("========== 蒙科立｜中国国标版 ==========")
    print("fontA:", font_a_path.name)
    print("fontB:", font_b_path.name)
    print("steps:", STEPS)
    print("----------------------------------")
    for k, v in counts.items():
        print(f"{k}: {v}")
    print("----------------------------------")
    print("TOTAL runtime:", len(runtime))
    print("说明：按中国国标清单检测蒙科立字体；有则生成，没有跳过。")
    print("==================================")

    build_ttf_steps(font_a_path, font_b_path, font_a, font_b, runtime)
    zip_path = zip_outputs()

    print("========== DONE ==========")
    print("RUNTIME :", RUNTIME_CSV)
    print("COVERAGE:", COVERAGE_CSV)
    print("REPORT  :", REPORT_CSV)
    print("ZIP     :", zip_path)


if __name__ == "__main__":
    main()

# ================= FORCE_MENK_GB_MULTI_GLYPH_EXPAND_PATCH_START =================
# 蒙科立中国国标版最终覆盖补丁：
# 读取 menk_gb_runtime.csv 中的 fontA_target_glyph_names / fontB_target_glyph_names；
# 按 runtime 项强制配对；
# 轮廓不一致也重采样；
# 多 glyph 序列按 "|" 展开；
# 过滤 .notdef / null / 空轮廓；
# 最终覆盖 output/menk_gb_ttf_steps 中的 20 个 TTF。

def _force_menk_gb_multi_glyph_expand_patch():
    try:
        import os
        import sys
        import csv
        import json
        import copy
        import zipfile
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        from fontTools.ttLib import TTFont

        from gb_morph_algorithm import (
            glyph_to_sampled_contours,
            match_contours,
            interpolate_contours,
            contours_to_glyf,
            interp_metric,
            set_names,
        )

        out_dir = root / "output" / "menk_gb_ttf_steps"
        runtime_csv = out_dir / "menk_gb_runtime.csv"

        if not runtime_csv.exists():
            print("[FORCE-MENK-GB][WARN] runtime csv not found:", runtime_csv)
            return

        rows = []
        with runtime_csv.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

        if not rows:
            print("[FORCE-MENK-GB][WARN] runtime csv empty")
            return

        steps = int(os.environ.get("MGB_STEPS", "20"))

        font_dir = Path(os.environ.get("MENK_FONT_DIR", root / "input" / "foundry_menk"))

        def find_font_by_name(name):
            name = str(name or "").strip()
            if not name:
                return None

            search_dirs = [
                font_dir,
                root / "input" / "foundry_menk",
                root / "input" / "fonts",
            ]

            for d in search_dirs:
                if not d.exists():
                    continue
                p = d / name
                if p.exists():
                    return p

            # 兜底全项目搜索，但排除 output
            for p in root.rglob(name):
                low = str(p).lower()
                if "/output/" in low:
                    continue
                if p.is_file():
                    return p

            return None

        font_a_name = rows[0].get("fontA", "")
        font_b_name = rows[0].get("fontB", "")

        font_a_path = find_font_by_name(font_a_name)
        font_b_path = find_font_by_name(font_b_name)

        if not font_a_path or not font_b_path:
            fonts = []
            for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
                fonts.extend(font_dir.glob(ext))
            fonts = sorted(fonts)

            if len(fonts) >= 2:
                font_a_path = fonts[0]
                font_b_path = fonts[1]

        if not font_a_path or not font_b_path:
            print("[FORCE-MENK-GB][ERROR] cannot find two menk fonts")
            print("fontA from runtime:", font_a_name)
            print("fontB from runtime:", font_b_name)
            print("font_dir:", font_dir)
            return

        font_a = TTFont(str(font_a_path), recalcBBoxes=True, recalcTimestamp=False)
        font_b = TTFont(str(font_b_path), recalcBBoxes=True, recalcTimestamp=False)

        if "glyf" not in font_a or "glyf" not in font_b:
            print("[FORCE-MENK-GB][ERROR] only TrueType glyf fonts are supported")
            return

        glyphs_a = set(font_a.getGlyphOrder())
        glyphs_b = set(font_b.getGlyphOrder())

        def usable(row):
            return str(row.get("usable_for_ttf", "")).strip() in ["1", "true", "True", "yes", "YES"]

        def split_names(value):
            value = str(value or "").strip()
            if not value:
                return []
            return [x.strip() for x in value.split("|") if x.strip()]

        bad_names = {
            ".notdef",
            "notdef",
            "gid0",
            "null",
            "NULL",
            "zerowidth",
            "zeroWidth",
            "ZeroWidth",
        }

        prepared = []
        skipped = []
        seen = set()

        handled_runtime_rows = 0
        expanded_multi_rows = 0
        duplicate_pairs = 0
        points_per_contour = 320

        for row in rows:
            if not usable(row):
                skipped.append({
                    "runtime_id": row.get("runtime_id", ""),
                    "reason": "usable_for_ttf != 1",
                })
                continue

            names_a = split_names(row.get("fontA_target_glyph_names", ""))
            names_b = split_names(row.get("fontB_target_glyph_names", ""))

            if not names_a or not names_b:
                skipped.append({
                    "runtime_id": row.get("runtime_id", ""),
                    "display_group": row.get("display_group", ""),
                    "reason": "empty glyph names",
                    "fontA_target_glyph_names": row.get("fontA_target_glyph_names", ""),
                    "fontB_target_glyph_names": row.get("fontB_target_glyph_names", ""),
                })
                continue

            if len(names_a) != len(names_b):
                skipped.append({
                    "runtime_id": row.get("runtime_id", ""),
                    "display_group": row.get("display_group", ""),
                    "reason": f"glyph sequence length mismatch A={len(names_a)} B={len(names_b)}",
                    "fontA_target_glyph_names": row.get("fontA_target_glyph_names", ""),
                    "fontB_target_glyph_names": row.get("fontB_target_glyph_names", ""),
                })
                continue

            if len(names_a) > 1:
                expanded_multi_rows += 1

            row_had_valid_pair = False

            for name_a, name_b in zip(names_a, names_b):
                if name_a in bad_names or name_b in bad_names:
                    skipped.append({
                        "runtime_id": row.get("runtime_id", ""),
                        "display_group": row.get("display_group", ""),
                        "gb_code": row.get("gb_code", ""),
                        "reason": "invalid .notdef/null/zerowidth glyph",
                        "glyph_a": name_a,
                        "glyph_b": name_b,
                    })
                    continue

                if name_a not in glyphs_a or name_b not in glyphs_b:
                    skipped.append({
                        "runtime_id": row.get("runtime_id", ""),
                        "display_group": row.get("display_group", ""),
                        "gb_code": row.get("gb_code", ""),
                        "reason": "glyph name not found",
                        "glyph_a": name_a,
                        "glyph_b": name_b,
                    })
                    continue

                key = (name_a, name_b)

                if key in seen:
                    duplicate_pairs += 1
                    row_had_valid_pair = True
                    continue

                try:
                    ca = glyph_to_sampled_contours(font_a, name_a, points_per_contour)
                    cb = glyph_to_sampled_contours(font_b, name_b, points_per_contour)
                    ca, cb = match_contours(
                        ca,
                        cb,
                        glyph_name_a=row.get("base_unicode", "") or name_a,
                        glyph_name_b=row.get("base_unicode", "") or name_b,
                    )

                    prepared.append({
                        "runtime_id": row.get("runtime_id", ""),
                        "display_group": row.get("display_group", ""),
                        "gb_code": row.get("gb_code", ""),
                        "base_unicode": row.get("base_unicode", ""),
                        "glyph_a": name_a,
                        "glyph_b": name_b,
                        "contours_a": ca,
                        "contours_b": cb,
                        "contour_count": len(ca),
                    })

                    seen.add(key)
                    row_had_valid_pair = True

                except Exception as e:
                    skipped.append({
                        "runtime_id": row.get("runtime_id", ""),
                        "display_group": row.get("display_group", ""),
                        "gb_code": row.get("gb_code", ""),
                        "glyph_a": name_a,
                        "glyph_b": name_b,
                        "reason": str(e),
                    })

            if row_had_valid_pair:
                handled_runtime_rows += 1

        if not prepared:
            print("[FORCE-MENK-GB][ERROR] no prepared glyph pairs")
            return

        for old in out_dir.glob("*.ttf"):
            old.unlink()

        generated_files = []

        for step in range(1, steps + 1):
            t = step / (steps + 1)

            out_font = copy.deepcopy(font_a)
            glyf = out_font["glyf"]

            for item in prepared:
                contours = interpolate_contours(item["contours_a"], item["contours_b"], t)
                new_glyph = contours_to_glyf(contours, glyf)

                name_a = item["glyph_a"]
                name_b = item["glyph_b"]

                glyf[name_a] = new_glyph

                metric = interp_metric(font_a, font_b, name_a, name_b, t)
                if metric and "hmtx" in out_font:
                    out_font["hmtx"].metrics[name_a] = metric

            family = f"menk_gb_step_Step_{step:02d}"
            set_names(out_font, family)

            out_file = out_dir / f"menk_gb_step_{step:02d}.ttf"
            out_font.save(str(out_file))
            generated_files.append(out_file.name)

            print(
                f"[FORCE-MENK-GB] step {step:02d}/{steps}, "
                f"t={t:.6f}, unique_glyph_pairs={len(prepared)}"
            )

        report = {
            "algorithm": "menk_runtime_glyph_name_force_outline_morph_with_multi_expand",
            "rule": "same GB/runtime item -> expand glyph sequence -> force resampled outline interpolation",
            "font_a": str(font_a_path),
            "font_b": str(font_b_path),
            "runtime_csv": str(runtime_csv),
            "runtime_rows": len(rows),
            "handled_runtime_rows": handled_runtime_rows,
            "expanded_multi_rows": expanded_multi_rows,
            "generated_unique_glyph_pairs": len(prepared),
            "duplicate_pairs_skipped": duplicate_pairs,
            "skipped_rows_or_pairs": len(skipped),
            "steps": steps,
            "points_per_contour": points_per_contour,
            "generated_files": generated_files,
            "prepared": [
                {
                    "runtime_id": x["runtime_id"],
                    "display_group": x["display_group"],
                    "gb_code": x["gb_code"],
                    "base_unicode": x["base_unicode"],
                    "glyph_a": x["glyph_a"],
                    "glyph_b": x["glyph_b"],
                    "contour_count": x["contour_count"],
                }
                for x in prepared
            ],
            "skipped": skipped[:800],
        }

        report_path = out_dir / "gb_morph_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        build_report = out_dir / "build_report.csv"
        with build_report.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "step",
                    "ttf",
                    "runtime_items",
                    "handled_runtime_rows",
                    "interpolated_unique_glyph_pairs",
                    "duplicate_pairs_skipped",
                    "skipped_incompatible",
                    "expanded_multi_rows",
                ],
            )
            w.writeheader()

            for step in range(1, steps + 1):
                w.writerow({
                    "step": step,
                    "ttf": str(out_dir / f"menk_gb_step_{step:02d}.ttf"),
                    "runtime_items": len(rows),
                    "handled_runtime_rows": handled_runtime_rows,
                    "interpolated_unique_glyph_pairs": len(prepared),
                    "duplicate_pairs_skipped": duplicate_pairs,
                    "skipped_incompatible": len(skipped),
                    "expanded_multi_rows": expanded_multi_rows,
                })

        zip_path = root / "output" / "menk_gb_ttf_steps.zip"
        if zip_path.exists():
            zip_path.unlink()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for fp in sorted(out_dir.glob("*.ttf")):
                z.write(fp, fp.name)
            z.write(report_path, report_path.name)
            z.write(build_report, build_report.name)

        print(
            f"[FORCE-MENK-GB] done: "
            f"runtime_rows={len(rows)}, handled_runtime_rows={handled_runtime_rows}, "
            f"unique_glyph_pairs={len(prepared)}, expanded_multi_rows={expanded_multi_rows}, "
            f"duplicate_pairs={duplicate_pairs}, skipped={len(skipped)}, zip={zip_path}"
        )

    except Exception as e:
        import traceback
        print("[FORCE-MENK-GB][ERROR]", e)
        print(traceback.format_exc())


_force_menk_gb_multi_glyph_expand_patch()
# ================= FORCE_MENK_GB_MULTI_GLYPH_EXPAND_PATCH_END =================
