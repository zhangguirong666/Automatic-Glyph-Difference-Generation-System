from __future__ import annotations





# ===== GB_COMPLETE_V2_PREPARED_FALLBACK_V1 =====
def _gb_complete_v2_load_prepared_from_v1_report():
    """
    V2 兜底读取：
    如果 V2 自己没有拿到 prepared glyphs，
    就从 V1 已经生成的 gb_morph_complete_report.json 里读取 data["prepared"]。
    """
    try:
        import json
        from pathlib import Path

        candidates = [
            Path("output/oyun_gb_ttf_steps/gb_morph_complete_report.json"),
            Path("/root/autodl-tmp/font_morph_web/output/oyun_gb_ttf_steps/gb_morph_complete_report.json"),
        ]

        for rp in candidates:
            if not rp.exists():
                continue

            data = json.loads(rp.read_text(encoding="utf-8"))
            prepared = data.get("prepared") or []

            if prepared:
                print(f"[GB-COMPLETE-OYUN-V2] fallback loaded prepared from V1 report: {len(prepared)}")
                return prepared

        print("[GB-COMPLETE-OYUN-V2][WARN] V1 report exists? no prepared loaded")
        return []

    except Exception as e:
        print("[GB-COMPLETE-OYUN-V2][WARN] fallback load prepared failed:", e)
        return []
# ===== /GB_COMPLETE_V2_PREPARED_FALLBACK_V1 =====

# ===== GB_HMTX_COMPILE_PATCH_V1 =====
def _gb_patch_hmtx_compile_final_guard():
    """
    最终兜底：
    直接 patch fontTools 的 hmtx.compile()。
    因为 KeyError: 'gb_OYUN_CORE_U1820' 正是在 _h_m_t_x.py compile() 里发生的。
    """
    try:
        import re
        from fontTools.ttLib.tables import _h_m_t_x as _gb_hmtx_module

        hmtx_cls = _gb_hmtx_module.table__h_m_t_x

        if getattr(hmtx_cls, "_gb_compile_guard_patched", False):
            return

        original_compile = hmtx_cls.compile

        def compile_with_gb_guard(self, ttFont):
            try:
                if not hasattr(self, "metrics") or self.metrics is None:
                    self.metrics = {}

                hmtx = self.metrics

                try:
                    glyph_order = list(ttFont.getGlyphOrder())
                except Exception:
                    glyph_order = []

                existing = [
                    v for v in hmtx.values()
                    if isinstance(v, tuple) and len(v) >= 2
                ]

                if existing:
                    fallback_aw, fallback_lsb = existing[0]
                else:
                    fallback_aw, fallback_lsb = 1000, 0

                # 用正常宽度的中位数作为 fallback，避免 .notdef 异常宽度
                try:
                    widths = sorted(
                        int(v[0]) for v in existing
                        if len(v) >= 1 and int(v[0]) > 0
                    )
                    if widths:
                        fallback_aw = widths[len(widths) // 2]
                except Exception:
                    fallback_aw = 1000

                fallback_aw = int(fallback_aw)
                fallback_lsb = int(fallback_lsb)

                # 从 cmap 中找 U+1820 这种原始字符对应的 glyph metric
                cmap_tables = []
                try:
                    if "cmap" in ttFont:
                        cmap_tables = list(ttFont["cmap"].tables)
                except Exception:
                    cmap_tables = []

                def metric_from_codepoint(cp):
                    for table in cmap_tables:
                        try:
                            gname = table.cmap.get(cp)
                        except Exception:
                            continue
                        if gname in hmtx:
                            return hmtx[gname]
                    return None

                repaired = 0

                for glyph_name in glyph_order:
                    if glyph_name in hmtx:
                        continue

                    metric = None

                    # 识别 gb_OYUN_CORE_U1820、xxx_U1820_xxx 这种名字
                    m = re.search(r"U([0-9A-Fa-f]{4,6})", glyph_name)
                    if m:
                        try:
                            cp = int(m.group(1), 16)
                            metric = metric_from_codepoint(cp)
                        except Exception:
                            metric = None

                    if metric is None:
                        metric = (fallback_aw, fallback_lsb)

                    aw = int(metric[0]) if len(metric) >= 1 else fallback_aw
                    lsb = int(metric[1]) if len(metric) >= 2 else fallback_lsb

                    # 如果 glyf 有真实 xMin，用 xMin 做 lsb
                    try:
                        if "glyf" in ttFont and glyph_name in ttFont["glyf"]:
                            g = ttFont["glyf"][glyph_name]
                            if hasattr(g, "xMin"):
                                lsb = int(g.xMin)
                    except Exception:
                        pass

                    hmtx[glyph_name] = (aw, lsb)
                    repaired += 1

                try:
                    if "hhea" in ttFont:
                        ttFont["hhea"].numberOfHMetrics = len(glyph_order)
                except Exception:
                    pass

                if repaired:
                    print(f"[GB-HMTX-COMPILE-GUARD] repaired missing hmtx metrics: {repaired}")

            except Exception as e:
                print("[GB-HMTX-COMPILE-GUARD][WARN]", e)

            return original_compile(self, ttFont)

        hmtx_cls.compile = compile_with_gb_guard
        hmtx_cls._gb_compile_guard_patched = True

        print("[GB-HMTX-COMPILE-GUARD] hmtx.compile patched")

    except Exception as e:
        print("[GB-HMTX-COMPILE-GUARD][PATCH-WARN]", e)


_gb_patch_hmtx_compile_final_guard()
# ===== /GB_HMTX_COMPILE_PATCH_V1 =====

# ===== GB_TTFONT_SAVE_MONKEYPATCH_V1 =====
def _gb_repair_hmtx_before_any_save(font):
    """
    全局保存前修复：
    fontTools 保存 TTF 时，glyphOrder 里的每个 glyphName 都必须存在于 hmtx.metrics。
    否则会出现 KeyError: 'gb_OYUN_CORE_U1820' 这类错误。
    """
    try:
        import re

        if "hmtx" not in font:
            return font

        hmtx = font["hmtx"].metrics
        glyph_order = list(font.getGlyphOrder())

        existing_metrics = [
            v for v in hmtx.values()
            if isinstance(v, tuple) and len(v) >= 2
        ]

        if existing_metrics:
            fallback_aw, fallback_lsb = existing_metrics[0]
        else:
            fallback_aw, fallback_lsb = 1000, 0

        # 尽量使用正常宽度的中位数作为 fallback
        try:
            widths = sorted(
                int(v[0]) for v in existing_metrics
                if len(v) >= 1 and int(v[0]) > 0
            )
            if widths:
                fallback_aw = widths[len(widths) // 2]
        except Exception:
            fallback_aw = 1000

        fallback_aw = int(fallback_aw)
        fallback_lsb = int(fallback_lsb)

        # cmap: codepoint -> glyphName，用来给 gb_OYUN_CORE_U1820 复制 U+1820 原字形宽度
        cmap_tables = []
        try:
            if "cmap" in font:
                cmap_tables = list(font["cmap"].tables)
        except Exception:
            cmap_tables = []

        def metric_from_codepoint(cp):
            for table in cmap_tables:
                try:
                    gname = table.cmap.get(cp)
                except Exception:
                    continue
                if gname in hmtx:
                    return hmtx[gname]
            return None

        repaired = 0

        for glyph_name in glyph_order:
            if glyph_name in hmtx:
                continue

            metric = None

            # 支持 gb_OYUN_CORE_U1820 / xxx_U1820_xxx 这类命名
            m = re.search(r"U([0-9A-Fa-f]{4,6})", glyph_name)
            if m:
                try:
                    cp = int(m.group(1), 16)
                    metric = metric_from_codepoint(cp)
                except Exception:
                    metric = None

            if metric is None:
                metric = (fallback_aw, fallback_lsb)

            aw = int(metric[0]) if len(metric) >= 1 else fallback_aw
            lsb = int(metric[1]) if len(metric) >= 2 else fallback_lsb

            # 如果 glyf 中有真实 xMin，用 xMin 作为 lsb 更合理
            try:
                if "glyf" in font and glyph_name in font["glyf"]:
                    g = font["glyf"][glyph_name]
                    if hasattr(g, "xMin"):
                        lsb = int(g.xMin)
            except Exception:
                pass

            hmtx[glyph_name] = (aw, lsb)
            repaired += 1

        # hhea.numberOfHMetrics 同步到 glyphOrder 数量
        try:
            if "hhea" in font:
                font["hhea"].numberOfHMetrics = len(glyph_order)
        except Exception:
            pass

        if repaired:
            print(f"[GB-TTFONT-SAVE-REPAIR] repaired hmtx metrics: {repaired}")

    except Exception as e:
        print("[GB-TTFONT-SAVE-REPAIR][WARN]", e)

    return font


try:
    from fontTools.ttLib import TTFont as _GB_TTFont_SavePatch

    if not getattr(_GB_TTFont_SavePatch, "_gb_save_repair_patched", False):
        _GB_original_save = _GB_TTFont_SavePatch.save

        def _GB_save_with_hmtx_repair(self, *args, **kwargs):
            _gb_repair_hmtx_before_any_save(self)
            return _GB_original_save(self, *args, **kwargs)

        _GB_TTFont_SavePatch.save = _GB_save_with_hmtx_repair
        _GB_TTFont_SavePatch._gb_save_repair_patched = True
        print("[GB-TTFONT-SAVE-REPAIR] TTFont.save patched")
except Exception as e:
    print("[GB-TTFONT-SAVE-REPAIR][PATCH-WARN]", e)
# ===== /GB_TTFONT_SAVE_MONKEYPATCH_V1 =====

# ===== GB_SAVE_HMTX_REPAIR_V3 =====
def _gb_repair_hmtx_for_all_glyphs(font):
    """
    在 TTFont.save() 前强制修复 hmtx：
    glyphOrder 里出现的每一个 glyphName，都必须在 hmtx.metrics 里有记录。
    否则 fontTools 保存 TTF 时会 KeyError。
    """
    try:
        if "hmtx" not in font:
            return font

        hmtx = font["hmtx"].metrics
        glyph_order = list(font.getGlyphOrder())

        # 1. 取一个安全 fallback metric
        vals = [
            v for v in hmtx.values()
            if isinstance(v, tuple) and len(v) >= 2
        ]

        if vals:
            fallback_aw, fallback_lsb = vals[0]
        else:
            fallback_aw, fallback_lsb = 1000, 0

        # 优先用已有宽度的中位数，避免 .notdef 宽度异常
        try:
            widths = sorted(int(v[0]) for v in vals if int(v[0]) > 0)
            if widths:
                fallback_aw = widths[len(widths) // 2]
        except Exception:
            fallback_aw = 1000

        fallback = (int(fallback_aw), int(fallback_lsb))

        # 2. 建立 cmap 查询，用于 gb_OYUN_CORE_U1820 这类名字复制 U+1820 原 glyph 的宽度
        cmap_tables = []
        try:
            if "cmap" in font:
                cmap_tables = list(font["cmap"].tables)
        except Exception:
            cmap_tables = []

        def metric_from_codepoint(cp):
            for table in cmap_tables:
                try:
                    base_glyph = table.cmap.get(cp)
                except Exception:
                    continue
                if base_glyph in hmtx:
                    return hmtx[base_glyph]
            return None

        def metric_from_glyph_name(glyph_name):
            # 支持 gb_OYUN_CORE_U1820 / xxx_U1820_yyy 这类命名
            m = re.search(r"U([0-9A-Fa-f]{4,6})", glyph_name)
            if m:
                try:
                    cp = int(m.group(1), 16)
                    metric = metric_from_codepoint(cp)
                    if metric is not None:
                        return metric
                except Exception:
                    pass
            return None

        repaired = 0

        # 3. 核心：glyphOrder 里每个 glyph 都必须有 hmtx
        for glyph_name in glyph_order:
            if glyph_name in hmtx:
                continue

            metric = metric_from_glyph_name(glyph_name)
            if metric is None:
                metric = fallback

            aw = int(metric[0]) if len(metric) > 0 else int(fallback[0])
            lsb = int(metric[1]) if len(metric) > 1 else 0

            # 如果 glyf 里有真实轮廓，左边距尽量用 xMin
            try:
                if "glyf" in font and glyph_name in font["glyf"]:
                    g = font["glyf"][glyph_name]
                    if hasattr(g, "xMin"):
                        lsb = int(g.xMin)
            except Exception:
                pass

            hmtx[glyph_name] = (aw, lsb)
            repaired += 1

        # 4. 同步 hhea，避免 numberOfHMetrics 与 glyphOrder 不一致
        try:
            if "hhea" in font:
                font["hhea"].numberOfHMetrics = len(glyph_order)
        except Exception:
            pass

        if repaired:
            print(f"[GB-HMTX-REPAIR] repaired missing hmtx metrics: {repaired}")

    except Exception as e:
        print("[GB-HMTX-REPAIR][WARN]", e)

    return font
# ===== /GB_SAVE_HMTX_REPAIR_V3 =====

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

FONT_DIR = Path(os.environ.get("OYUN_FONT_DIR", "input/foundry_oyun"))
FALLBACK_FONT_DIR = Path("input/fonts")

OUT_DIR = Path("output/oyun_gb_ttf_steps")
OUT_DIR.mkdir(parents=True, exist_ok=True)

RUNTIME_CSV = OUT_DIR / "oyun_gb_runtime.csv"
RUNTIME_JSON = OUT_DIR / "oyun_gb_runtime.json"
COVERAGE_CSV = OUT_DIR / "oyun_gb_coverage_report.csv"
REPORT_CSV = OUT_DIR / "build_report.csv"
ZIP_PATH = Path("output/oyun_gb_ttf_steps.zip")

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
    奥云 流程只读取自己的字体目录。
    禁止 fallback 到其他字体公司目录，避免误用字体。
    """
    FONT_DIR.mkdir(parents=True, exist_ok=True)

    fonts = []
    for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
        fonts.extend(FONT_DIR.glob(ext))

    fonts = sorted(fonts)

    if len(fonts) < 2:
        raise SystemExit(
            "奥云目录中不足两个字体文件。请从网页上传两个奥云字体，"
            "或手动放入：" + str(FONT_DIR)
        )

    forbidden_keywords = ['menk', 'mengke', 'menksoft', '蒙科立', '蒙克立']

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
            "奥云流程检测到疑似其他字体公司的字体，已拒绝生成：" +
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
    """
    按 data/mongolian_gb_charset.csv 对比奥云字体：
    - 国标清单里有的名义字符
    - 两个奥云字体都存在才进入 runtime
    - 任意一边缺失则跳过
    """

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
                "reason": "奥云两个字体中至少一方缺失该名义字符",
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
            "reason": "奥云两个字体均支持该名义字符",
            "fontA_glyph": g_a,
            "fontB_glyph": g_b,
        })

        add_runtime(
            runtime,
            runtime_id="OYUN_NOM_" + uc.replace("+", ""),
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
            source="oyun_cmap_vs_gb_charset",
            note="奥云字体中存在的国标名义字符；不存在的已跳过。",
        )

    return runtime, coverage


def build_core_35(font_a_path, font_b_path, font_a, font_b):
    """
    强制检测 U+1820-U+1842。
    有则加入；没有则跳过，但不乱造。
    """
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
            runtime_id=f"OYUN_CORE_U{cp:04X}",
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
            source="oyun_core_35_shaping",
            note="原来的35个传统蒙古文基础字母，奥云字体有则加入。",
        )

    return rows


def build_presentation_by_oyun_shaping(font_a_path, font_b_path, font_a, font_b):
    """
    奥云字体公司的显现形式：
    - 不硬套 glyph 顺序
    - 用 HarfBuzz 按上下文和 FVS 触发
    - 两个奥云字体都能得到目标 glyph，则加入
    """

    A = "\u1820"
    fvs = {
        "fvs1": "\u180B",
        "fvs2": "\u180C",
        "fvs3": "\u180D",
        "fvs4": "\u180F",
    }

    probes = []

    for cp in range(0x1820, 0x1843):
        ch = chr(cp)
        code = f"U+{cp:04X}"

        probes.extend([
            ("isol", ch, "all"),
            ("init", ch + A, "first"),
            ("medi", A + ch + A, "middle"),
            ("fina", A + ch, "last"),
        ])

        for name, f in fvs.items():
            probes.append((name, ch + f, "first"))

        for subtype, text, rule in probes[-8:]:
            pass

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
                runtime_id=f"OYUN_PRES_{code.replace('+','')}_{subtype}",
                display_group="单个变形显现字符",
                category="presentation",
                subtype=subtype,
                gb_code="",
                base_unicode=code,
                base_name=f"Oyun presentation form {code} {subtype}",
                text_codepoints=ustr(text),
                fontA=font_a_path.name,
                fontA_target_glyph_ids=str(ta[0]),
                fontA_target_glyph_names=na[0] if na else "",
                fontB=font_b_path.name,
                fontB_target_glyph_ids=str(tb[0]),
                fontB_target_glyph_names=nb[0] if nb else "",
                usable_for_ttf="1",
                source="oyun_harfbuzz_presentation",
                note="按奥云字体 shaping 规则提取的显现形式；两字体都有则加入。",
            )

    return rows


def build_verified_table6(font_a_path, font_b_path, font_a, font_b):
    """
    表6强制性合体字：
    - 必须在 data/mongolian_gb_ligature_table6_strict.csv 中 verified=1
    - 没填就跳过
    """
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
            runtime_id=item.get("liga_id", "OYUN_LIGA_" + item.get("gb_code", "")),
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
            source="oyun_verified_table6",
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

        # 关键：以字体A为底，保留其所有已有 glyph、cmap、GSUB。
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

            # 多 glyph 序列不在单 glyph 插值里硬做，保留字体A。
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

        family = f"Oyun GB Step {step:02d}"
        update_names(out, family)

        out_path = OUT_DIR / f"oyun_gb_step_{step:02d}.ttf"
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
    presentation_runtime = build_presentation_by_oyun_shaping(font_a_path, font_b_path, font_a, font_b)
    liga_runtime = build_verified_table6(font_a_path, font_b_path, font_a, font_b)
    appendix_f_runtime = build_appendix_f_optional_ligatures(font_a_path, font_b_path, font_a, font_b)
    gsub_liga_runtime = build_gsub_ligatures(font_a_path, font_b_path, font_a, font_b, prefix="OYUN")

    runtime.extend(core_runtime)
    runtime.extend(nominal_runtime)
    runtime.extend(presentation_runtime)
    runtime.extend(liga_runtime)
    runtime.extend(appendix_f_runtime)
    runtime.extend(gsub_liga_runtime)
    coverage_all.extend(nominal_coverage)

    # runtime 去重
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

    print("========== 奥云｜中国国标版 ==========")
    print("fontA:", font_a_path.name)
    print("fontB:", font_b_path.name)
    print("steps:", STEPS)
    print("----------------------------------")
    for k, v in counts.items():
        print(f"{k}: {v}")
    print("----------------------------------")
    print("TOTAL runtime:", len(runtime))
    print("说明：按中国国标清单检测奥云字体；有则生成，没有跳过。")
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


# ================= FORCE_GB_OUTLINE_MORPH_POST_PATCH_START =================
# Runtime glyph-name 强制插值版：
# 不再只按 Unicode cmap 做 56 个；
# 直接读取 oyun_gb_runtime.csv 中的 fontA_target_glyph_names / fontB_target_glyph_names；
# 只要 runtime 里两个字体有同一国标项对应 glyph，就强制采样插值。

def _force_gb_outline_morph_post_patch():
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

        font_dir = Path(os.environ.get("OYUN_FONT_DIR", root / "input" / "foundry_oyun"))
        steps = int(os.environ.get("MGB_STEPS", "20"))

        fonts = []
        for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
            fonts.extend(font_dir.glob(ext))
        fonts = sorted(fonts)

        if len(fonts) < 2:
            print("[FORCE-GB-MORPH][WARN] no two fonts found:", font_dir)
            return

        font_a_path = fonts[0]
        font_b_path = fonts[1]

        out_dir = root / "output" / "oyun_gb_ttf_steps"
        out_dir.mkdir(parents=True, exist_ok=True)

        runtime_csv = out_dir / "oyun_gb_runtime.csv"
        if not runtime_csv.exists():
            print("[FORCE-GB-MORPH][WARN] runtime csv not found:", runtime_csv)
            return

        font_a = TTFont(str(font_a_path), recalcBBoxes=True, recalcTimestamp=False)
        font_b = TTFont(str(font_b_path), recalcBBoxes=True, recalcTimestamp=False)

        if "glyf" not in font_a or "glyf" not in font_b:
            print("[FORCE-GB-MORPH][ERROR] only TrueType glyf fonts are supported")
            return

        glyphs_a = set(font_a.getGlyphOrder())
        glyphs_b = set(font_b.getGlyphOrder())

        def is_usable(row):
            return str(row.get("usable_for_ttf", "")).strip() in ["1", "true", "True", "YES", "yes"]

        def has_multi_ids(row):
            a = str(row.get("fontA_target_glyph_ids", "")).strip()
            b = str(row.get("fontB_target_glyph_ids", "")).strip()
            return (" " in a) or (" " in b)

        def single_glyph_name(value):
            value = str(value or "").strip()
            if not value:
                return ""

            # 多 glyph 序列通常用 | 分隔，先不在单 glyph 插值里处理
            parts = [x.strip() for x in value.split("|") if x.strip()]
            if len(parts) != 1:
                return ""

            return parts[0]

        rows = []
        with runtime_csv.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

        prepared = []
        skipped = []
        skipped_multi = 0
        seen = set()

        points_per_contour = 320

        for row in rows:
            if not is_usable(row):
                skipped.append({
                    "runtime_id": row.get("runtime_id", ""),
                    "gb_code": row.get("gb_code", ""),
                    "reason": "usable_for_ttf != 1",
                })
                continue

            # 多 glyph 序列后面单独处理；当前先处理单个目标 glyph
            if has_multi_ids(row):
                skipped_multi += 1
                skipped.append({
                    "runtime_id": row.get("runtime_id", ""),
                    "gb_code": row.get("gb_code", ""),
                    "display_group": row.get("display_group", ""),
                    "reason": "multi glyph id sequence",
                    "fontA_target_glyph_ids": row.get("fontA_target_glyph_ids", ""),
                    "fontB_target_glyph_ids": row.get("fontB_target_glyph_ids", ""),
                })
                continue

            name_a = single_glyph_name(row.get("fontA_target_glyph_names", ""))
            name_b = single_glyph_name(row.get("fontB_target_glyph_names", ""))

            if not name_a or not name_b:
                skipped.append({
                    "runtime_id": row.get("runtime_id", ""),
                    "gb_code": row.get("gb_code", ""),
                    "display_group": row.get("display_group", ""),
                    "reason": "empty or multi glyph name",
                    "fontA_target_glyph_names": row.get("fontA_target_glyph_names", ""),
                    "fontB_target_glyph_names": row.get("fontB_target_glyph_names", ""),
                })
                continue

            if name_a not in glyphs_a or name_b not in glyphs_b:
                skipped.append({
                    "runtime_id": row.get("runtime_id", ""),
                    "gb_code": row.get("gb_code", ""),
                    "display_group": row.get("display_group", ""),
                    "reason": "glyph name not found",
                    "glyph_a": name_a,
                    "glyph_b": name_b,
                })
                continue

            key = (name_a, name_b)
            if key in seen:
                # 同一个 glyph pair 重复出现时，不重复写轮廓
                continue
            seen.add(key)

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

            except Exception as e:
                skipped.append({
                    "runtime_id": row.get("runtime_id", ""),
                    "gb_code": row.get("gb_code", ""),
                    "display_group": row.get("display_group", ""),
                    "glyph_a": name_a,
                    "glyph_b": name_b,
                    "reason": str(e),
                })

        if not prepared:
            print("[FORCE-GB-MORPH][ERROR] no prepared glyphs")
            return

        # 删除旧 ttf
        for old in out_dir.glob("*.ttf"):
            old.unlink()

        generated_files = []

        for step in range(1, steps + 1):
            t = step / (steps + 1)

            out_font = copy.deepcopy(font_a)
            glyf = out_font["glyf"]

            for item in prepared:
                if "contours_a" not in item or "contours_b" not in item:
                    print("[GB-COMPLETE-OYUN-V2][SKIP] item has no contours_a/contours_b; V2 requires in-memory contours")
                    return None
                contours = interpolate_contours(item["contours_a"], item["contours_b"], t)
                new_glyph = contours_to_glyf(contours, glyf)

                name_a = item["glyph_a"]
                name_b = item["glyph_b"]

                glyf[name_a] = new_glyph

                metric = interp_metric(font_a, font_b, name_a, name_b, t)
                if metric and "hmtx" in out_font:
                    out_font["hmtx"].metrics[name_a] = metric

            family = f"oyun_gb_step_Step_{step:02d}"
            set_names(out_font, family)

            out_file = out_dir / f"oyun_gb_step_{step:02d}.ttf"
            _gb_repair_hmtx_for_all_glyphs(out_font)
            out_font.save(str(out_file))
            generated_files.append(out_file.name)

            print(
                f"[FORCE-GB-MORPH-RUNTIME] step {step:02d}/{steps}, "
                f"t={t:.6f}, glyph_pairs={len(prepared)}"
            )

        report = {
            "algorithm": "runtime_glyph_name_force_outline_morph",
            "rule": "same GB/runtime item -> glyphA/glyphB -> force resampled outline interpolation",
            "font_a": str(font_a_path),
            "font_b": str(font_b_path),
            "runtime_csv": str(runtime_csv),
            "out_dir": str(out_dir),
            "steps": steps,
            "points_per_contour": points_per_contour,
            "runtime_rows": len(rows),
            "generated_unique_glyph_pairs": len(prepared),
            "skipped_rows": len(skipped),
            "skipped_multi_glyph_sequence": skipped_multi,
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
            "skipped": skipped[:300],
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
                    "interpolated",
                    "skipped_missing",
                    "skipped_incompatible",
                    "skipped_multi_glyph_sequence",
                ],
            )
            w.writeheader()

            for step in range(1, steps + 1):
                w.writerow({
                    "step": step,
                    "ttf": str(out_dir / f"oyun_gb_step_{step:02d}.ttf"),
                    "runtime_items": len(rows),
                    "interpolated": len(prepared),
                    "skipped_missing": 0,
                    "skipped_incompatible": len(skipped) - skipped_multi,
                    "skipped_multi_glyph_sequence": skipped_multi,
                })

        zip_path = root / "output" / "oyun_gb_ttf_steps.zip"
        if zip_path.exists():
            zip_path.unlink()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for fp in sorted(out_dir.glob("*.ttf")):
                z.write(fp, fp.name)
            z.write(report_path, report_path.name)
            z.write(build_report, build_report.name)

        print(
            f"[FORCE-GB-MORPH-RUNTIME] done: "
            f"runtime_rows={len(rows)}, interpolated={len(prepared)}, "
            f"skipped_incompatible={len(skipped) - skipped_multi}, "
            f"multi={skipped_multi}, zip={zip_path}"
        )

    except Exception as e:
        import traceback
        print("[FORCE-GB-MORPH-RUNTIME][ERROR]", e)
        print(traceback.format_exc())


_force_gb_outline_morph_post_patch()
# ================= FORCE_GB_OUTLINE_MORPH_POST_PATCH_END =================


# ================= FORCE_GB_MULTI_GLYPH_EXPAND_PATCH_START =================
# 最终覆盖补丁：
# 处理 runtime 中的多 glyph 序列。
# 原来 multi=104 的项目，不再直接跳过；
# 如果 fontA_target_glyph_names / fontB_target_glyph_names 可以按 "|" 拆成等长 glyph 序列，
# 就逐个 glyph 强制轮廓插值。

def _force_gb_multi_glyph_expand_patch():
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

        font_dir = Path(os.environ.get("OYUN_FONT_DIR", root / "input" / "foundry_oyun"))
        steps = int(os.environ.get("MGB_STEPS", "20"))

        fonts = []
        for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
            fonts.extend(font_dir.glob(ext))
        fonts = sorted(fonts)

        if len(fonts) < 2:
            print("[FORCE-GB-MULTI][WARN] no two fonts found:", font_dir)
            return

        font_a_path = fonts[0]
        font_b_path = fonts[1]

        out_dir = root / "output" / "oyun_gb_ttf_steps"
        runtime_csv = out_dir / "oyun_gb_runtime.csv"

        if not runtime_csv.exists():
            print("[FORCE-GB-MULTI][WARN] runtime csv not found:", runtime_csv)
            return

        font_a = TTFont(str(font_a_path), recalcBBoxes=True, recalcTimestamp=False)
        font_b = TTFont(str(font_b_path), recalcBBoxes=True, recalcTimestamp=False)

        glyphs_a = set(font_a.getGlyphOrder())
        glyphs_b = set(font_b.getGlyphOrder())

        def usable(row):
            return str(row.get("usable_for_ttf", "")).strip() in ["1", "true", "True", "yes", "YES"]

        def split_names(value):
            value = str(value or "").strip()
            if not value:
                return []
            return [x.strip() for x in value.split("|") if x.strip()]

        rows = []
        with runtime_csv.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

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
                bad_names = {".notdef", "notdef", "gid0", "null", "NULL"}

                if name_a in bad_names or name_b in bad_names:
                    skipped.append({
                        "runtime_id": row.get("runtime_id", ""),
                        "display_group": row.get("display_group", ""),
                        "reason": "invalid .notdef/null glyph",
                        "glyph_a": name_a,
                        "glyph_b": name_b,
                    })
                    continue

                if name_a not in glyphs_a or name_b not in glyphs_b:
                    skipped.append({
                        "runtime_id": row.get("runtime_id", ""),
                        "display_group": row.get("display_group", ""),
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
            print("[FORCE-GB-MULTI][ERROR] no prepared glyph pairs")
            return

        for old in out_dir.glob("*.ttf"):
            old.unlink()

        generated_files = []

        for step in range(1, steps + 1):
            t = step / (steps + 1)

            out_font = copy.deepcopy(font_a)
            glyf = out_font["glyf"]

            for item in prepared:
                if "contours_a" not in item or "contours_b" not in item:
                    print("[GB-COMPLETE-OYUN-V2][SKIP] item has no contours_a/contours_b; V2 requires in-memory contours")
                    return None
                contours = interpolate_contours(item["contours_a"], item["contours_b"], t)
                new_glyph = contours_to_glyf(contours, glyf)

                name_a = item["glyph_a"]
                name_b = item["glyph_b"]

                glyf[name_a] = new_glyph

                metric = interp_metric(font_a, font_b, name_a, name_b, t)
                if metric and "hmtx" in out_font:
                    out_font["hmtx"].metrics[name_a] = metric

            family = f"oyun_gb_step_Step_{step:02d}"
            set_names(out_font, family)

            out_file = out_dir / f"oyun_gb_step_{step:02d}.ttf"
            _gb_repair_hmtx_for_all_glyphs(out_font)
            out_font.save(str(out_file))
            generated_files.append(out_file.name)

            print(
                f"[FORCE-GB-MULTI] step {step:02d}/{steps}, "
                f"t={t:.6f}, unique_glyph_pairs={len(prepared)}"
            )

        report = {
            "algorithm": "runtime_glyph_name_force_outline_morph_with_multi_expand",
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
            "skipped": skipped[:500],
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
                    "ttf": str(out_dir / f"oyun_gb_step_{step:02d}.ttf"),
                    "runtime_items": len(rows),
                    "handled_runtime_rows": handled_runtime_rows,
                    "interpolated_unique_glyph_pairs": len(prepared),
                    "duplicate_pairs_skipped": duplicate_pairs,
                    "skipped_incompatible": len(skipped),
                    "expanded_multi_rows": expanded_multi_rows,
                })

        zip_path = root / "output" / "oyun_gb_ttf_steps.zip"
        if zip_path.exists():
            zip_path.unlink()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for fp in sorted(out_dir.glob("*.ttf")):
                z.write(fp, fp.name)
            z.write(report_path, report_path.name)
            z.write(build_report, build_report.name)

        print(
            f"[FORCE-GB-MULTI] done: "
            f"runtime_rows={len(rows)}, handled_runtime_rows={handled_runtime_rows}, "
            f"unique_glyph_pairs={len(prepared)}, expanded_multi_rows={expanded_multi_rows}, "
            f"duplicate_pairs={duplicate_pairs}, skipped={len(skipped)}, zip={zip_path}"
        )

    except Exception as e:
        import traceback
        print("[FORCE-GB-MULTI][ERROR]", e)
        print(traceback.format_exc())


_force_gb_multi_glyph_expand_patch()
# ================= FORCE_GB_MULTI_GLYPH_EXPAND_PATCH_END =================

# ================= FORCE_OYUN_GB_COMPLETE_RUNTIME_ITEMS_START =================
# 目标：
# 1. 不再只按唯一 glyph pair 去重显示；
# 2. 每个可见 GB/runtime 项都生成一个独立 glyph；
# 3. 对 .notdef 的强制性合体字，尝试用 base_unicode 中的组件字形拼合补全；
# 4. 输出 gb_morph_complete_report.json 和 complete_oyun_gb_step_XX.ttf。

def _force_oyun_gb_complete_runtime_items_patch():
    try:
        import os
        import sys
        import csv
        import json
        import copy
        import re
        import zipfile
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        from fontTools.ttLib import TTFont
        from fontTools.ttLib.tables._g_l_y_f import Glyph

        from gb_morph_algorithm import (
            glyph_to_sampled_contours,
            match_contours,
            interpolate_contours,
            contours_to_glyf,
            interp_metric,
            set_names,
        )

        out_dir = root / "output" / "oyun_gb_ttf_steps"
        runtime_csv = out_dir / "oyun_gb_runtime.csv"

        if not runtime_csv.exists():
            print("[GB-COMPLETE-OYUN][WARN] runtime csv not found:", runtime_csv)
            return

        rows = []
        with runtime_csv.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            rows = list(csv.DictReader(f))

        if not rows:
            print("[GB-COMPLETE-OYUN][WARN] empty runtime")
            return

        steps = int(os.environ.get("MGB_STEPS", "20"))
        points_per_contour = 320

        font_dir = root / "input" / "foundry_oyun"

        def find_font(name):
            name = str(name or "").strip()
            if name:
                for d in [font_dir, root / "input" / "fonts"]:
                    p = d / name
                    if p.exists():
                        return p
                for p in root.rglob(name):
                    if "/output/" not in str(p):
                        return p

            fonts = []
            for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
                fonts.extend(font_dir.glob(ext))
            fonts = sorted(fonts)
            return None

        font_a_name = rows[0].get("fontA", "")
        font_b_name = rows[0].get("fontB", "")

        font_a_path = find_font(font_a_name)
        font_b_path = find_font(font_b_name)

        if not font_a_path or not font_b_path:
            fonts = []
            for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
                fonts.extend(font_dir.glob(ext))
            fonts = sorted(fonts)
            if len(fonts) >= 2:
                font_a_path = fonts[0]
                font_b_path = fonts[1]

        if not font_a_path or not font_b_path:
            print("[GB-COMPLETE-OYUN][ERROR] cannot find fontA/fontB")
            return

        font_a = TTFont(str(font_a_path), recalcBBoxes=True, recalcTimestamp=False)
        font_b = TTFont(str(font_b_path), recalcBBoxes=True, recalcTimestamp=False)

        cmap_a = font_a.getBestCmap() or {}
        cmap_b = font_b.getBestCmap() or {}

        glyphs_a = set(font_a.getGlyphOrder())
        glyphs_b = set(font_b.getGlyphOrder())

        bad_names = {
            ".notdef", "notdef", "gid0", "null", "NULL",
            "zerowidth", "zeroWidth", "ZeroWidth",
        }

        invisible_cps = {
            0x180B,  # FVS1
            0x180C,  # FVS2
            0x180D,  # FVS3
            0x180E,  # MVS
            0x180F,  # FVS4
        }

        def usable(row):
            return str(row.get("usable_for_ttf", "")).strip() in ["1", "true", "True", "yes", "YES"]

        def split_names(v):
            v = str(v or "").strip()
            if not v:
                return []
            return [x.strip() for x in v.split("|") if x.strip()]

        def safe_name(s):
            s = str(s or "")
            s = re.sub(r"[^0-9A-Za-z_.-]+", "_", s)
            s = s.strip("._-")
            return s or "unnamed"

        def parse_codepoints(base_unicode):
            cps = []
            for m in re.finditer(r"U\+([0-9A-Fa-f]{4,6})", str(base_unicode or "")):
                cps.append(int(m.group(1), 16))
            return cps

        def glyph_for_cp(font, cmap, cp):
            if cp in cmap:
                return cmap[cp]

            candidates = [
                f"uni{cp:04X}",
                f"uni{cp:04x}",
                f"u{cp:04X}",
                f"u{cp:04x}",
                f"uni{cp:05X}",
                f"u{cp:05X}",
            ]

            order = set(font.getGlyphOrder())
            for name in candidates:
                if name in order:
                    return name

            return None

        def translate_contours(contours, dx=0, dy=0):
            out = []
            for c in contours:
                nc = []
                for p in c:
                    try:
                        x, y = p[0], p[1]
                        rest = list(p[2:]) if len(p) > 2 else []
                        nc.append([x + dx, y + dy] + rest)
                    except Exception:
                        nc.append(p)
                out.append(nc)
            return out

        def bbox_from_contours(contours):
            xs = []
            ys = []
            for c in contours:
                for p in c:
                    xs.append(p[0])
                    ys.append(p[1])
            if not xs:
                return None
            return min(xs), min(ys), max(xs), max(ys)

        def advance_for_glyph(font, glyph_name):
            try:
                return font["hmtx"].metrics.get(glyph_name, (600, 0))[0]
            except Exception:
                return 600

        def synthesize_from_unicode_sequence(font, cmap, base_unicode):
            """
            对强制性合体字 .notdef 做兜底：
            从 base_unicode 中取可见 codepoint，逐个取 glyph，纵向拼合成一个可见轮廓。
            这不是字体厂商原始 ligature，但可以保证国标项有可见字形参与插值。
            """
            cps = [cp for cp in parse_codepoints(base_unicode) if cp not in invisible_cps]

            parts = []
            for cp in cps:
                g = glyph_for_cp(font, cmap, cp)
                if not g:
                    continue
                if g in bad_names:
                    continue
                try:
                    contours = glyph_to_sampled_contours(font, g, points_per_contour)
                    if contours:
                        parts.append((g, contours))
                except Exception:
                    continue

            if not parts:
                raise ValueError("cannot synthesize from unicode sequence: no visible component glyphs")

            combined = []
            y_cursor = 0

            for g, contours in parts:
                box = bbox_from_contours(contours)
                if not box:
                    continue

                x0, y0, x1, y1 = box
                h = max(1, y1 - y0)

                # 蒙古文纵向书写，兜底拼合时按竖向排列；
                # 把每个组件向下错开，使其成为一个组合轮廓。
                shifted = translate_contours(contours, dx=0, dy=y_cursor - y0)
                combined.extend(shifted)

                y_cursor -= int(h * 0.82)

            if not combined:
                raise ValueError("synthesized contours empty")

            return combined

        prepared = []
        skipped = []
        complete_rows = []

        for row in rows:
            group = row.get("display_group", "")
            rid = row.get("runtime_id", "")
            gb_code = row.get("gb_code", "")
            base_unicode = row.get("base_unicode", "")

            # FVS/MVS 控制符不补，因为它们没有可见轮廓
            if rid in ["OYUN_NOM_U180B", "OYUN_NOM_U180C", "OYUN_NOM_U180D", "OYUN_NOM_U180E"]:
                skipped.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "reason": "control character, intentionally no visible outline",
                })
                continue

            if group not in [
                "传统蒙古文35基础字母",
                "名义字符",
                "单个变形显现字符",
                "强制性合体字",
                "非强制性合体字（附录F示例）",
            ]:
                continue

            if not usable(row):
                skipped.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "reason": "usable_for_ttf != 1",
                })
                continue

            names_a = split_names(row.get("fontA_target_glyph_names", ""))
            names_b = split_names(row.get("fontB_target_glyph_names", ""))

            synthetic = False

            try:
                if names_a and names_b and len(names_a) == len(names_b):
                    pair_contours_a = []
                    pair_contours_b = []

                    invalid_pair = False

                    for ga, gb in zip(names_a, names_b):
                        if ga in bad_names or gb in bad_names:
                            invalid_pair = True
                            break

                        if ga not in glyphs_a or gb not in glyphs_b:
                            invalid_pair = True
                            break

                        ca = glyph_to_sampled_contours(font_a, ga, points_per_contour)
                        cb = glyph_to_sampled_contours(font_b, gb, points_per_contour)

                        if not ca or not cb:
                            invalid_pair = True
                            break

                        ca, cb = match_contours(
                            ca,
                            cb,
                            glyph_name_a=base_unicode or ga,
                            glyph_name_b=base_unicode or gb,
                        )

                        pair_contours_a.extend(ca)
                        pair_contours_b.extend(cb)

                    if invalid_pair:
                        if group == "强制性合体字":
                            pair_contours_a = synthesize_from_unicode_sequence(font_a, cmap_a, base_unicode)
                            pair_contours_b = synthesize_from_unicode_sequence(font_b, cmap_b, base_unicode)
                            pair_contours_a, pair_contours_b = match_contours(
                                pair_contours_a,
                                pair_contours_b,
                                glyph_name_a=base_unicode or rid,
                                glyph_name_b=base_unicode or rid,
                            )
                            synthetic = True
                        else:
                            raise ValueError("invalid glyph pair and not ligature fallback")
                else:
                    if group == "强制性合体字":
                        pair_contours_a = synthesize_from_unicode_sequence(font_a, cmap_a, base_unicode)
                        pair_contours_b = synthesize_from_unicode_sequence(font_b, cmap_b, base_unicode)
                        pair_contours_a, pair_contours_b = match_contours(
                            pair_contours_a,
                            pair_contours_b,
                            glyph_name_a=base_unicode or rid,
                            glyph_name_b=base_unicode or rid,
                        )
                        synthetic = True
                    else:
                        raise ValueError("empty or mismatched glyph sequence")

                complete_name = "gb_" + safe_name(rid)

                prepared.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "glyph_a": names_a[0] if names_a else "",
                    "glyph_b": names_b[0] if names_b else "",
                    "complete_glyph_name": complete_name,
                    "contours_a": pair_contours_a,
                    "contours_b": pair_contours_b,
                    "contour_count": len(pair_contours_a),
                    "synthetic": synthetic,
                })

                complete_rows.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "complete_glyph_name": complete_name,
                    "source_glyph_a": "|".join(names_a),
                    "source_glyph_b": "|".join(names_b),
                    "contour_count": len(pair_contours_a),
                    "synthetic": synthetic,
                    "status": "generated",
                })

            except Exception as e:
                skipped.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "fontA_target_glyph_names": row.get("fontA_target_glyph_names", ""),
                    "fontB_target_glyph_names": row.get("fontB_target_glyph_names", ""),
                    "reason": str(e),
                })

                complete_rows.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "complete_glyph_name": "",
                    "source_glyph_a": "|".join(names_a),
                    "source_glyph_b": "|".join(names_b),
                    "contour_count": "",
                    "synthetic": "",
                    "status": "skipped: " + str(e),
                })

        if not prepared:
            print("[GB-COMPLETE-OYUN][ERROR] no prepared runtime items")
            return

        # 生成 complete 版本字体
        for old in out_dir.glob("complete_oyun_gb_step_*.ttf"):
            old.unlink()

        generated_files = []

        for step in range(1, steps + 1):
            t = step / (steps + 1)

            out_font = copy.deepcopy(font_a)
            glyf = out_font["glyf"]
            glyph_order = out_font.getGlyphOrder()

            for item in prepared:
                cname = item["complete_glyph_name"]

                if cname not in glyph_order:
                    glyph_order.append(cname)

                if "contours_a" not in item or "contours_b" not in item:
                    print("[GB-COMPLETE-OYUN-V2][SKIP] item has no contours_a/contours_b; V2 requires in-memory contours")
                    return None
                contours = interpolate_contours(item["contours_a"], item["contours_b"], t)
                glyf[cname] = contours_to_glyf(contours, glyf)

                if "hmtx" in out_font:
                    # 安全估算 advance width，避免 numpy/list 结构不一致导致 bbox 报错。
                    aw = 800
                    try:
                        xs = []
                        ys = []

                        def walk(obj):
                            if obj is None:
                                return

                            # numpy array
                            if hasattr(obj, "tolist"):
                                obj = obj.tolist()

                            # 点：[x, y] 或 [x, y, flag]
                            if isinstance(obj, (list, tuple)) and len(obj) >= 2:
                                if isinstance(obj[0], (int, float)) and isinstance(obj[1], (int, float)):
                                    xs.append(float(obj[0]))
                                    ys.append(float(obj[1]))
                                    return

                            # 递归处理嵌套 contour / point
                            if isinstance(obj, (list, tuple)):
                                for it in obj:
                                    walk(it)

                        walk(contours)

                        if xs:
                            aw = max(300, int((max(xs) - min(xs)) + 120))
                    except Exception:
                        aw = 800

                    out_font["hmtx"].metrics[cname] = (aw, 0)

            out_font.setGlyphOrder(glyph_order)

            # 补齐所有新增 glyph 的 hmtx 宽度，避免保存 TTF 时 KeyError。
            if "hmtx" in out_font:
                hmtx = out_font["hmtx"]
                for _gname in out_font.getGlyphOrder():
                    if _gname not in hmtx.metrics:
                        hmtx.metrics[_gname] = (800, 0)

            family = f"oyun_gb_complete_Step_{step:02d}"
            set_names(out_font, family)

            out_file = out_dir / f"complete_oyun_gb_step_{step:02d}.ttf"
            # ===== GB-COMPLETE-OYUN-SAFE-PRESAVE-HMTX =====
            # 在保存 complete 字体前，强制补齐 glyphOrder / glyf / hmtx 的一致性。
            try:
                _order = list(out_font.getGlyphOrder())

                # glyf 中存在但 glyphOrder 没有的，也补进 glyphOrder
                if "glyf" in out_font:
                    for _gname in out_font["glyf"].glyphs.keys():
                        if _gname not in _order:
                            _order.append(_gname)

                # prepared 中新增的 complete glyph 必须在 glyphOrder 中
                for _item in prepared:
                    _cname = _item.get("complete_glyph_name")
                    if _cname and _cname not in _order:
                        _order.append(_cname)

                out_font.setGlyphOrder(_order)

                if "hmtx" in out_font:
                    _hmtx = out_font["hmtx"]

                    # 先给所有 glyphOrder 项补默认宽度
                    for _gname in out_font.getGlyphOrder():
                        if _gname not in _hmtx.metrics:
                            _hmtx.metrics[_gname] = (800, 0)

                    # 再给所有 prepared 新 glyph 强制覆盖一次，防止遗漏
                    for _item in prepared:
                        _cname = _item.get("complete_glyph_name")
                        if _cname:
                            _hmtx.metrics[_cname] = _hmtx.metrics.get(_cname, (800, 0))

                if "maxp" in out_font:
                    out_font["maxp"].numGlyphs = len(out_font.getGlyphOrder())

            except Exception as _e:
                print("[GB-COMPLETE-OYUN][WARN] presave hmtx fix failed:", _e)
            # ===== GB-COMPLETE-OYUN-SAFE-PRESAVE-HMTX-END =====

            _gb_repair_hmtx_for_all_glyphs(out_font)

            out_font.save(str(out_file))
            generated_files.append(out_file.name)

            print(
                f"[GB-COMPLETE-OYUN] step {step:02d}/{steps}, "
                f"runtime_glyphs={len(prepared)}"
            )

        report = {
            "algorithm": "oyun_complete_gb_runtime_item_generation",
            "rule": "one visible GB/runtime item -> one independent generated glyph; missing ligatures synthesized from components",
            "font_a": str(font_a_path),
            "font_b": str(font_b_path),
            "runtime_rows": len(rows),
            "generated_runtime_glyphs": len(prepared),
            "skipped": len(skipped),
            "steps": steps,
            "generated_files": generated_files,
            "prepared": [
                {
                    "runtime_id": x["runtime_id"],
                    "display_group": x["display_group"],
                    "gb_code": x["gb_code"],
                    "base_unicode": x["base_unicode"],
                    "complete_glyph_name": x["complete_glyph_name"],
                    "source_glyph_a": x["glyph_a"],
                    "source_glyph_b": x["glyph_b"],
                    "contour_count": x["contour_count"],
                    "synthetic": x["synthetic"],
                }
                for x in prepared
            ],
            "skipped_items": skipped[:1000],
        }

        report_path = out_dir / "gb_morph_complete_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        complete_csv = out_dir / "oyun_gb_complete_runtime_table.csv"
        with complete_csv.open("w", encoding="utf-8-sig", newline="") as f:
            fields = [
                "runtime_id",
                "display_group",
                "gb_code",
                "base_unicode",
                "complete_glyph_name",
                "source_glyph_a",
                "source_glyph_b",
                "contour_count",
                "synthetic",
                "status",
            ]
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(complete_rows)

        zip_path = root / "output" / "oyun_gb_complete_ttf_steps.zip"
        if zip_path.exists():
            zip_path.unlink()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for fp in sorted(out_dir.glob("complete_oyun_gb_step_*.ttf")):
                z.write(fp, fp.name)
            z.write(report_path, report_path.name)
            z.write(complete_csv, complete_csv.name)

        print(
            f"[GB-COMPLETE-OYUN] done: "
            f"runtime_rows={len(rows)}, generated_runtime_glyphs={len(prepared)}, "
            f"skipped={len(skipped)}, zip={zip_path}"
        )

    except Exception as e:
        import traceback
        print("[GB-COMPLETE-OYUN][ERROR]", e)
        print(traceback.format_exc())


_force_oyun_gb_complete_runtime_items_patch()
# ================= FORCE_OYUN_GB_COMPLETE_RUNTIME_ITEMS_END =================

# ================= FORCE_OYUN_GB_COMPLETE_RUNTIME_ITEMS_V2_START =================
# V2：修复强制性合体字 synthetic 拼合失败的问题。
# 目标：尽量补齐到 475 个可见国标 runtime 项。
# 说明：前面的 complete patch 若生成 438，本 V2 会在最后覆盖 complete_oyun_gb_step_XX.ttf 和 gb_morph_complete_report.json。

def _force_oyun_gb_complete_runtime_items_v2():
    # GB_DISABLE_OYUN_COMPLETE_V2_TEMP_V1
    print("[GB-COMPLETE-OYUN-V2][DISABLED] V2 synthetic fallback disabled; keep stable V1 output")
    return None

    try:
        import os
        import sys
        import csv
        import json
        import copy
        import re
        import zipfile
        from pathlib import Path

        try:
            import numpy as np
        except Exception:
            np = None

        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        from fontTools.ttLib import TTFont

        from gb_morph_algorithm import (
            glyph_to_sampled_contours,
            match_contours,
            interpolate_contours,
            contours_to_glyf,
            set_names,
        )

        out_dir = root / "output" / "oyun_gb_ttf_steps"
        runtime_csv = out_dir / "oyun_gb_runtime.csv"

        if not runtime_csv.exists():
            print("[GB-COMPLETE-OYUN-V2][ERROR] runtime csv not found:", runtime_csv)
            return

        with runtime_csv.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            rows = list(csv.DictReader(f))

        steps = int(os.environ.get("MGB_STEPS", "20"))
        points_per_contour = 320

        font_dir = root / "input" / "foundry_oyun"

        def find_font(name):
            name = str(name or "").strip()
            if name:
                for d in [font_dir, root / "input" / "fonts"]:
                    p = d / name
                    if p.exists():
                        return p
                for p in root.rglob(name):
                    if p.is_file() and "/output/" not in str(p):
                        return p
            return None

        font_a_name = rows[0].get("fontA", "")
        font_b_name = rows[0].get("fontB", "")

        font_a_path = find_font(font_a_name)
        font_b_path = find_font(font_b_name)

        if not font_a_path or not font_b_path:
            fonts = []
            for ext in ["*.ttf", "*.otf", "*.TTF", "*.OTF"]:
                fonts.extend(font_dir.glob(ext))
            fonts = sorted(fonts)
            if len(fonts) >= 2:
                font_a_path = fonts[0]
                font_b_path = fonts[1]

        if not font_a_path or not font_b_path:
            print("[GB-COMPLETE-OYUN-V2][ERROR] cannot find fontA/fontB")
            return

        font_a = TTFont(str(font_a_path), recalcBBoxes=True, recalcTimestamp=False)
        font_b = TTFont(str(font_b_path), recalcBBoxes=True, recalcTimestamp=False)

        glyphs_a = set(font_a.getGlyphOrder())
        glyphs_b = set(font_b.getGlyphOrder())

        bad_names = {
            ".notdef", "notdef", "gid0", "null", "NULL",
            "zerowidth", "zeroWidth", "ZeroWidth",
            "space", "uni0020",
        }

        control_nominal_ids = {
            "OYUN_NOM_U180B",
            "OYUN_NOM_U180C",
            "OYUN_NOM_U180D",
            "OYUN_NOM_U180E",
        }

        def usable(row):
            return str(row.get("usable_for_ttf", "")).strip() in ["1", "true", "True", "yes", "YES"]

        def split_names(v):
            v = str(v or "").strip()
            if not v:
                return []
            return [x.strip() for x in v.split("|") if x.strip()]

        def safe_name(s):
            s = str(s or "")
            s = re.sub(r"[^0-9A-Za-z_.-]+", "_", s)
            s = s.strip("._-")
            return s or "unnamed"

        def is_num(x):
            return isinstance(x, (int, float)) and not isinstance(x, bool)

        def is_point(obj):
            if hasattr(obj, "tolist"):
                obj = obj.tolist()
            return (
                isinstance(obj, (list, tuple))
                and len(obj) >= 2
                and is_num(obj[0])
                and is_num(obj[1])
            )

        def normalize_contours(contours):
            """
            把 glyph_to_sampled_contours 可能返回的各种结构统一成：
            [ numpy_array(N,2), numpy_array(N,2), ... ]
            """
            if contours is None:
                return []

            if hasattr(contours, "tolist"):
                contours = contours.tolist()

            out = []

            def make_arr(points):
                pts = []
                for p in points:
                    if hasattr(p, "tolist"):
                        p = p.tolist()
                    if is_point(p):
                        pts.append([float(p[0]), float(p[1])])
                if len(pts) == 0:
                    return None
                if np is not None:
                    return np.array(pts, dtype=float)
                return pts

            # 单点
            if is_point(contours):
                arr = make_arr([contours])
                return [arr] if arr is not None else []

            # 一个 contour：[[x,y], [x,y]...]
            if isinstance(contours, (list, tuple)) and contours and all(is_point(p) for p in contours):
                arr = make_arr(contours)
                return [arr] if arr is not None else []

            def collect_points(obj):
                pts = []

                def walk(x):
                    if hasattr(x, "tolist"):
                        x = x.tolist()

                    if is_point(x):
                        pts.append([float(x[0]), float(x[1])])
                        return

                    if isinstance(x, (list, tuple)):
                        for it in x:
                            walk(it)

                walk(obj)
                return pts

            if isinstance(contours, (list, tuple)):
                for c in contours:
                    if hasattr(c, "tolist"):
                        c = c.tolist()

                    # c 是一个 contour
                    if isinstance(c, (list, tuple)) and c and all(is_point(p) for p in c):
                        arr = make_arr(c)
                        if arr is not None:
                            out.append(arr)
                    else:
                        pts = collect_points(c)
                        arr = make_arr(pts)
                        if arr is not None:
                            out.append(arr)

            return out

        def contour_bbox(contours):
            xs = []
            ys = []

            for c in normalize_contours(contours):
                pts = c.tolist() if hasattr(c, "tolist") else c
                for p in pts:
                    if is_point(p):
                        xs.append(float(p[0]))
                        ys.append(float(p[1]))

            if not xs:
                return None

            return min(xs), min(ys), max(xs), max(ys)

        def translate_contours(contours, dx=0, dy=0):
            result = []

            for c in normalize_contours(contours):
                pts = c.tolist() if hasattr(c, "tolist") else c
                shifted = []

                for p in pts:
                    if is_point(p):
                        shifted.append([float(p[0]) + dx, float(p[1]) + dy])

                if shifted:
                    if np is not None:
                        result.append(np.array(shifted, dtype=float))
                    else:
                        result.append(shifted)

            return result

        def get_glyph_contours(font, glyph_name):
            if glyph_name in bad_names:
                return []
            if glyph_name not in font.getGlyphOrder():
                return []

            try:
                c = glyph_to_sampled_contours(font, glyph_name, points_per_contour)
                return normalize_contours(c)
            except Exception:
                return []

        def component_sequence_to_contours(font, names):
            """
            用已有组件 glyph 纵向拼合出一个 synthetic 合体字轮廓。
            只过滤 .notdef / zerowidth / space，不再因为其中一个无效组件导致整项失败。
            """
            valid_parts = []

            for name in names:
                if name in bad_names:
                    continue

                c = get_glyph_contours(font, name)
                if c:
                    valid_parts.append((name, c))

            if not valid_parts:
                raise ValueError("no visible component glyphs for synthetic ligature")

            combined = []
            y_cursor = 0

            for name, contours in valid_parts:
                box = contour_bbox(contours)
                if not box:
                    continue

                x0, y0, x1, y1 = box
                h = max(1, y1 - y0)

                shifted = translate_contours(contours, dx=0, dy=y_cursor - y0)
                combined.extend(shifted)

                # 竖排拼合，适当重叠，避免间距太散
                y_cursor -= int(h * 0.78)

            combined = normalize_contours(combined)

            if not combined:
                raise ValueError("synthetic contours empty")

            return combined

        def direct_sequence_to_contours(font, names):
            """
            正常 glyph 序列也拼成一个完整国标项。
            如果只是 zerowidth / space，忽略；
            如果有 .notdef，则返回空，让外层走 synthetic。
            """
            combined = []
            y_cursor = 0
            saw_notdef = False

            for name in names:
                if name in [".notdef", "notdef", "gid0", "null", "NULL"]:
                    saw_notdef = True
                    continue

                if name in ["zerowidth", "zeroWidth", "ZeroWidth", "space", "uni0020"]:
                    continue

                contours = get_glyph_contours(font, name)
                if not contours:
                    continue

                box = contour_bbox(contours)
                if not box:
                    continue

                x0, y0, x1, y1 = box
                h = max(1, y1 - y0)

                shifted = translate_contours(contours, dx=0, dy=y_cursor - y0)
                combined.extend(shifted)
                y_cursor -= int(h * 0.78)

            combined = normalize_contours(combined)

            if saw_notdef:
                return []

            return combined

        prepared = []
        skipped = []
        complete_rows = []

        for row in rows:
            rid = row.get("runtime_id", "")
            group = row.get("display_group", "")
            gb_code = row.get("gb_code", "")
            base_unicode = row.get("base_unicode", "")

            if rid in control_nominal_ids:
                skipped.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "reason": "control character, intentionally no visible outline",
                })
                complete_rows.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "complete_glyph_name": "",
                    "source_glyph_a": "",
                    "source_glyph_b": "",
                    "contour_count": "",
                    "synthetic": "",
                    "status": "skipped: control character",
                })
                continue

            if group not in [
                "传统蒙古文35基础字母",
                "名义字符",
                "单个变形显现字符",
                "强制性合体字",
                "非强制性合体字（附录F示例）",
            ]:
                continue

            if not usable(row):
                skipped.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "reason": "usable_for_ttf != 1",
                })
                continue

            names_a = split_names(row.get("fontA_target_glyph_names", ""))
            names_b = split_names(row.get("fontB_target_glyph_names", ""))

            synthetic = False

            try:
                ca = direct_sequence_to_contours(font_a, names_a)
                cb = direct_sequence_to_contours(font_b, names_b)

                # 强制性合体字如果直接失败，使用可见组件拼合兜底。
                if group == "强制性合体字" and (not ca or not cb):
                    ca = component_sequence_to_contours(font_a, names_a)
                    cb = component_sequence_to_contours(font_b, names_b)
                    synthetic = True

                if not ca or not cb:
                    raise ValueError("empty contours after direct/synthetic generation")

                ca, cb = match_contours(
                    ca,
                    cb,
                    glyph_name_a=base_unicode or rid,
                    glyph_name_b=base_unicode or rid,
                )

                cname = "gb_" + safe_name(rid)

                prepared.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "complete_glyph_name": cname,
                    "source_glyph_a": "|".join(names_a),
                    "source_glyph_b": "|".join(names_b),
                    "contours_a": ca,
                    "contours_b": cb,
                    "contour_count": len(ca),
                    "synthetic": synthetic,
                })

                complete_rows.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "complete_glyph_name": cname,
                    "source_glyph_a": "|".join(names_a),
                    "source_glyph_b": "|".join(names_b),
                    "contour_count": len(ca),
                    "synthetic": synthetic,
                    "status": "generated",
                })

            except Exception as e:
                skipped.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "fontA_target_glyph_names": row.get("fontA_target_glyph_names", ""),
                    "fontB_target_glyph_names": row.get("fontB_target_glyph_names", ""),
                    "reason": str(e),
                })

                complete_rows.append({
                    "runtime_id": rid,
                    "display_group": group,
                    "gb_code": gb_code,
                    "base_unicode": base_unicode,
                    "complete_glyph_name": "",
                    "source_glyph_a": "|".join(names_a),
                    "source_glyph_b": "|".join(names_b),
                    "contour_count": "",
                    "synthetic": "",
                    "status": "skipped: " + str(e),
                })

        if not prepared:
            prepared = _gb_complete_v2_load_prepared_from_v1_report()
        if not prepared:
            print("[GB-COMPLETE-OYUN-V2][SKIP] no in-memory contour prepared glyphs; keep V1 complete output")
            return None

        for old in out_dir.glob("complete_oyun_gb_step_*.ttf"):
            try:
                old.unlink()
            except Exception:
                pass

        generated_files = []

        for step in range(1, steps + 1):
            t = step / (steps + 1)
            out_font = copy.deepcopy(font_a)

            glyf = out_font["glyf"]
            order = list(out_font.getGlyphOrder())

            for item in prepared:
                cname = item["complete_glyph_name"]

                if cname not in order:
                    order.append(cname)

                if "contours_a" not in item or "contours_b" not in item:
                    print("[GB-COMPLETE-OYUN-V2][SKIP] item has no contours_a/contours_b; V2 requires in-memory contours")
                    return None
                contours = interpolate_contours(item["contours_a"], item["contours_b"], t)
                contours = normalize_contours(contours)

                glyf[cname] = contours_to_glyf(contours, glyf)

            out_font.setGlyphOrder(order)

            if "hmtx" in out_font:
                hmtx = out_font["hmtx"]

                for gname in out_font.getGlyphOrder():
                    if gname not in hmtx.metrics:
                        hmtx.metrics[gname] = (800, 0)

                for item in prepared:
                    cname = item["complete_glyph_name"]
                    hmtx.metrics[cname] = hmtx.metrics.get(cname, (800, 0))

            if "maxp" in out_font:
                out_font["maxp"].numGlyphs = len(out_font.getGlyphOrder())

            set_names(out_font, f"oyun_gb_complete_Step_{step:02d}")

            out_file = out_dir / f"complete_oyun_gb_step_{step:02d}.ttf"
            out_font.save(str(out_file))
            generated_files.append(out_file.name)

            print(f"[GB-COMPLETE-OYUN-V2] step {step:02d}/{steps}, runtime_glyphs={len(prepared)}")

        report = {
            "algorithm": "oyun_complete_gb_runtime_item_generation_v2_safe_synthetic",
            "rule": "one visible GB/runtime item -> one independent glyph; missing ligatures synthesized from visible components",
            "font_a": str(font_a_path),
            "font_b": str(font_b_path),
            "runtime_rows": len(rows),
            "generated_runtime_glyphs": len(prepared),
            "skipped": len(skipped),
            "steps": steps,
            "generated_files": generated_files,
            "prepared": [
                {
                    "runtime_id": x["runtime_id"],
                    "display_group": x["display_group"],
                    "gb_code": x["gb_code"],
                    "base_unicode": x["base_unicode"],
                    "complete_glyph_name": x["complete_glyph_name"],
                    "source_glyph_a": x["source_glyph_a"],
                    "source_glyph_b": x["source_glyph_b"],
                    "contour_count": x["contour_count"],
                    "synthetic": x["synthetic"],
                }
                for x in prepared
            ],
            "skipped_items": skipped[:2000],
        }

        report_path = out_dir / "gb_morph_complete_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        complete_csv = out_dir / "oyun_gb_complete_runtime_table.csv"
        with complete_csv.open("w", encoding="utf-8-sig", newline="") as f:
            fields = [
                "runtime_id",
                "display_group",
                "gb_code",
                "base_unicode",
                "complete_glyph_name",
                "source_glyph_a",
                "source_glyph_b",
                "contour_count",
                "synthetic",
                "status",
            ]
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(complete_rows)

        zip_path = root / "output" / "oyun_gb_complete_ttf_steps.zip"
        if zip_path.exists():
            zip_path.unlink()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for fp in sorted(out_dir.glob("complete_oyun_gb_step_*.ttf")):
                z.write(fp, fp.name)
            z.write(report_path, report_path.name)
            z.write(complete_csv, complete_csv.name)

        print(
            f"[GB-COMPLETE-OYUN-V2] done: "
            f"runtime_rows={len(rows)}, generated_runtime_glyphs={len(prepared)}, "
            f"skipped={len(skipped)}, synthetic={sum(1 for x in prepared if x['synthetic'])}, "
            f"zip={zip_path}"
        )

    except Exception as e:
        import traceback
        print("[GB-COMPLETE-OYUN-V2][ERROR]", e)
        print(traceback.format_exc())


_force_oyun_gb_complete_runtime_items_v2()
# ================= FORCE_OYUN_GB_COMPLETE_RUNTIME_ITEMS_V2_END =================

# GB_COMPLETE_V2_DISABLE_BAD_JSON_FALLBACK_V1
