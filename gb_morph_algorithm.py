from pathlib import Path
from array import array
import copy
import json
import re
import zipfile

import numpy as np
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphCoordinates
from fontTools.ttLib.tables.ttProgram import Program
from svgpathtools import parse_path


def best_cmap(font):
    best = None
    best_score = -1

    if "cmap" not in font:
        return {}

    for table in font["cmap"].tables:
        if not table.isUnicode():
            continue

        score = len(table.cmap)
        if table.platformID == 3:
            score += 1000000

        if score > best_score:
            best = table.cmap
            best_score = score

    return dict(best or {})


def safe_name(text):
    text = re.sub(r"[^0-9A-Za-z_\-]+", "", str(text))
    return text[:58] or "MorphFont"


def set_names(font, family):
    if "name" not in font:
        return

    records = {
        1: family,
        2: "Regular",
        4: family + " Regular",
        6: safe_name(family + "-Regular"),
        16: family,
        17: "Regular",
    }

    for name_id, value in records.items():
        for platform_id, enc_id, lang_id in [(3, 1, 0x409), (1, 0, 0)]:
            try:
                font["name"].setName(value, name_id, platform_id, enc_id, lang_id)
            except Exception:
                pass


def glyph_path_d(font, glyph_name):
    glyph_set = font.getGlyphSet()
    pen = SVGPathPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    return pen.getCommands()


def signed_area(points):
    x = points.real
    y = points.imag
    return 0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)


def abs_area(points):
    return abs(signed_area(points))


def sample_subpath(subpath, n_points):
    length = subpath.length(error=1e-4)

    if length <= 1e-6:
        return None

    # Keep a denser representation for long strokes.  Fixed sparse sampling was
    # one reason that narrow Mongolian strokes became jagged in middle steps.
    n_points = max(int(n_points), int(np.ceil(length / 8.0)))
    n_points = min(max(n_points, 32), 640)

    pts = []

    for i in range(n_points):
        s = length * i / n_points
        try:
            t = subpath.ilength(s)
        except Exception:
            t = i / n_points
        pts.append(subpath.point(t))

    return np.array(pts, dtype=np.complex128)


def glyph_to_sampled_contours(font, glyph_name, points_per_contour):
    d = glyph_path_d(font, glyph_name)

    if not d or not d.strip():
        return []

    path = parse_path(d)
    subpaths = path.continuous_subpaths()

    contours = []

    for sp in subpaths:
        pts = sample_subpath(sp, points_per_contour)
        if pts is not None and len(pts) >= 4:
            contours.append(pts)

    contours = sorted(contours, key=abs_area, reverse=True)
    return contours


def contour_center(points):
    if points is None or len(points) == 0:
        return 0 + 0j
    return complex(float(np.mean(points.real)), float(np.mean(points.imag)))


def contour_bbox(points):
    if points is None or len(points) == 0:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        float(np.min(points.real)),
        float(np.min(points.imag)),
        float(np.max(points.real)),
        float(np.max(points.imag)),
    )


def contour_diag(points):
    x0, y0, x1, y1 = contour_bbox(points)
    return float(np.hypot(x1 - x0, y1 - y0))


def contour_length(points):
    if points is None or len(points) < 2:
        return 0.0
    return float(np.sum(np.abs(np.roll(points, -1) - points)))


def glyph_bbox(contours):
    pts = _all_points(contours) if contours else np.array([], dtype=np.complex128)
    if len(pts) == 0:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        float(np.min(pts.real)),
        float(np.min(pts.imag)),
        float(np.max(pts.real)),
        float(np.max(pts.imag)),
    )


def bbox_size(bbox):
    x0, y0, x1, y1 = bbox
    return max(float(x1 - x0), 1.0), max(float(y1 - y0), 1.0)


def infer_codepoint_from_name(name):
    if not name:
        return None
    match = re.search(r"(?:^|[^0-9A-Fa-f])U\+?([0-9A-Fa-f]{4,6})(?:$|[^0-9A-Fa-f])", str(name))
    if not match:
        return None
    try:
        return int(match.group(1), 16)
    except Exception:
        return None


def infer_script_hint(codepoint=None, glyph_name=None):
    cp = codepoint if codepoint is not None else infer_codepoint_from_name(glyph_name)
    if cp is None:
        return "generic"
    if 0x1800 <= cp <= 0x18AF or 0x11660 <= cp <= 0x1167F:
        return "mongolian"
    if (
        0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0x20000 <= cp <= 0x2FA1F
    ):
        return "cjk"
    if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
        return "kana"
    if 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F:
        return "hangul"
    if 0x0041 <= cp <= 0x024F or 0x1E00 <= cp <= 0x1EFF:
        return "latin"
    if 0x0400 <= cp <= 0x052F:
        return "cyrillic"
    return "generic"


def contour_structure_role(points, glyph_bbox_value=None, script_hint="generic"):
    if points is None or len(points) == 0:
        return {
            "role": "empty",
            "x_zone": 1,
            "y_zone": 1,
            "area_ratio": 0.0,
            "diag_ratio": 0.0,
            "aspect": 1.0,
        }

    bbox = glyph_bbox_value or contour_bbox(points)
    gx0, gy0, gx1, gy1 = bbox
    gw, gh = bbox_size(bbox)
    x0, y0, x1, y1 = contour_bbox(points)
    cw = max(float(x1 - x0), 1.0)
    ch = max(float(y1 - y0), 1.0)
    center = contour_center(points)
    area_ratio = abs_area(points) / max(gw * gh, 1.0)
    diag_ratio = contour_diag(points) / max(float(np.hypot(gw, gh)), 1.0)
    aspect = cw / ch
    x_norm = (center.real - gx0) / gw
    y_norm = (center.imag - gy0) / gh
    x_zone = int(np.clip(np.floor(x_norm * 3.0), 0, 2))
    y_zone = int(np.clip(np.floor(y_norm * 3.0), 0, 2))

    role = "main"
    if area_ratio < 0.012 and diag_ratio < 0.18:
        role = "dot"
    elif area_ratio < 0.035 and diag_ratio < 0.28:
        role = "mark"

    if script_hint == "mongolian":
        # Traditional Mongolian glyphs are strongly vertical.  A long narrow
        # contour is usually structural spine/tail information, not a random
        # blob; keep it matched to a similarly placed contour.
        if ch / gh > 0.42 and cw / gw < 0.58 and area_ratio >= 0.018:
            role = "spine"
        elif y_norm < 0.22 and role == "main":
            role = "tail"
        elif y_norm > 0.78 and role == "main":
            role = "crown"
    elif script_hint == "latin":
        if y_norm > 0.72 and role in {"dot", "mark"}:
            role = "diacritic"
        elif ch / gh > 0.52 and cw / gw < 0.36:
            role = "stem"
        elif cw / gw > 0.40 and ch / gh > 0.35:
            role = "bowl"
    elif script_hint == "cjk":
        if cw / gw > 0.78 and ch / gh > 0.78 and area_ratio < 0.36:
            role = "enclosure"
        elif role == "main":
            role = "component"

    return {
        "role": role,
        "x_zone": x_zone,
        "y_zone": y_zone,
        "area_ratio": float(area_ratio),
        "diag_ratio": float(diag_ratio),
        "aspect": float(aspect),
        "x_norm": float(x_norm),
        "y_norm": float(y_norm),
        "width_ratio": float(cw / gw),
        "height_ratio": float(ch / gh),
    }


def contour_role_distance(role_a, role_b):
    role_penalty = 0.0 if role_a["role"] == role_b["role"] else 0.75
    if {role_a["role"], role_b["role"]} <= {"mark", "dot", "diacritic"}:
        role_penalty *= 0.45
    if {role_a["role"], role_b["role"]} <= {"main", "component", "spine", "stem", "bowl", "tail", "crown"}:
        role_penalty *= 0.65

    zone_score = (
        abs(role_a["x_zone"] - role_b["x_zone"]) * 0.22
        + abs(role_a["y_zone"] - role_b["y_zone"]) * 0.30
    )
    norm_score = abs(role_a.get("x_norm", 0.5) - role_b.get("x_norm", 0.5)) * 0.65
    norm_score += abs(role_a.get("y_norm", 0.5) - role_b.get("y_norm", 0.5)) * 0.85
    aspect_score = abs(np.log((role_a["aspect"] + 0.05) / (role_b["aspect"] + 0.05))) * 0.18
    return role_penalty + zone_score + norm_score + aspect_score


def resample_complex_contour(points, n_points):
    if points is None or len(points) == 0:
        return np.zeros(int(n_points), dtype=np.complex128)

    points = np.asarray(points, dtype=np.complex128)
    n_points = int(max(8, n_points))
    closed = np.concatenate([points, points[:1]])
    seg = np.abs(np.diff(closed))
    total = float(np.sum(seg))

    if total <= 1e-9:
        return np.full(n_points, points[0], dtype=np.complex128)

    cumulative = np.concatenate([[0.0], np.cumsum(seg)])
    targets = np.linspace(0.0, total, n_points, endpoint=False)
    real = np.interp(targets, cumulative, closed.real)
    imag = np.interp(targets, cumulative, closed.imag)
    return real + 1j * imag


def contour_match_score(a, b, glyph_diag, glyph_bbox_value=None, script_hint="generic"):
    ca = contour_center(a)
    cb = contour_center(b)
    diag = max(float(glyph_diag), 1.0)

    center_score = abs(ca - cb) / diag
    area_score = abs(np.log((abs_area(a) + 1.0) / (abs_area(b) + 1.0)))
    length_score = abs(np.log((contour_length(a) + 1.0) / (contour_length(b) + 1.0)))
    size_score = abs(np.log((contour_diag(a) + 1.0) / (contour_diag(b) + 1.0)))
    direction_penalty = 0.45 if signed_area(a) * signed_area(b) < 0 else 0.0
    role_a = contour_structure_role(a, glyph_bbox_value, script_hint)
    role_b = contour_structure_role(b, glyph_bbox_value, script_hint)
    structure_score = contour_role_distance(role_a, role_b)

    return (
        center_score * 4.2
        + area_score * 0.95
        + length_score * 0.72
        + size_score * 0.72
        + direction_penalty
        + structure_score
    )


def structural_anchor_indices(points, script_hint="generic"):
    if points is None or len(points) == 0:
        return np.array([], dtype=int)

    pts = np.asarray(points, dtype=np.complex128)
    idx = [
        int(np.argmin(pts.real)),
        int(np.argmax(pts.real)),
        int(np.argmin(pts.imag)),
        int(np.argmax(pts.imag)),
    ]

    if script_hint == "mongolian":
        y0 = float(np.min(pts.imag))
        y1 = float(np.max(pts.imag))
        x_mid = float(np.mean(pts.real))
        for y in [y0 + (y1 - y0) * 0.25, y0 + (y1 - y0) * 0.50, y0 + (y1 - y0) * 0.75]:
            dist = np.abs(pts.real - x_mid) + np.abs(pts.imag - y)
            idx.append(int(np.argmin(dist)))
    elif script_hint in {"cjk", "hangul"}:
        x0 = float(np.min(pts.real))
        x1 = float(np.max(pts.real))
        y0 = float(np.min(pts.imag))
        y1 = float(np.max(pts.imag))
        for x in [x0 + (x1 - x0) * 0.25, x0 + (x1 - x0) * 0.75]:
            for y in [y0 + (y1 - y0) * 0.25, y0 + (y1 - y0) * 0.75]:
                dist = np.abs(pts.real - x) + np.abs(pts.imag - y)
                idx.append(int(np.argmin(dist)))

    return np.array(sorted(set(idx)), dtype=int)


def align_contour(a, b, script_hint="generic"):
    """
    让两个闭合轮廓方向一致，并自动寻找 B 的最佳起点。
    """
    if signed_area(a) * signed_area(b) < 0:
        b = b[::-1]

    n = max(len(a), len(b), 8)
    if len(a) != n:
        a = resample_complex_contour(a, n)
    if len(b) != n:
        b = resample_complex_contour(b, n)

    ac = a - np.mean(a)
    bc = b - np.mean(b)
    scale = max(float(np.mean(np.abs(ac))) + float(np.mean(np.abs(bc))), 1.0)
    avec = np.roll(ac, -1) - ac
    bvec = np.roll(bc, -1) - bc
    anchor_idx = structural_anchor_indices(ac, script_hint)

    best_shift = 0
    best_score = float("inf")

    for shift in range(len(b)):
        bs = np.roll(bc, shift)
        bvs = np.roll(bvec, shift)
        point_score = np.mean(np.abs(ac - bs) ** 2) / (scale * scale)
        edge_score = np.mean(np.abs(avec - bvs) ** 2) / (scale * scale)
        anchor_score = 0.0
        if len(anchor_idx):
            anchor_score = np.mean(np.abs(ac[anchor_idx] - bs[anchor_idx]) ** 2) / (scale * scale)
        score = point_score + edge_score * 0.35 + anchor_score * 0.55

        if score < best_score:
            best_score = score
            best_shift = shift

    return a, np.roll(b, best_shift)


def _all_points(contours):
    if not contours:
        return np.array([], dtype=np.complex128)
    return np.concatenate(contours)


def _contours_center(contours_a, contours_b):
    pts = []
    if contours_a:
        pts.append(_all_points(contours_a))
    if contours_b:
        pts.append(_all_points(contours_b))

    if not pts:
        return 0 + 0j

    all_pts = np.concatenate(pts)
    if len(all_pts) == 0:
        return 0 + 0j

    return complex(float(np.mean(all_pts.real)), float(np.mean(all_pts.imag)))


def _tiny_contour(center, n_points, size=1.0):
    """
    用一个极小闭合轮廓补齐轮廓数量。
    作用：当 A 有 3 个轮廓、B 只有 1 个轮廓时，
    B 额外补 2 个极小轮廓，让它们在插值过程中逐渐长出来。
    """
    angles = np.linspace(0, 2 * np.pi, n_points, endpoint=False)
    return center + size * (np.cos(angles) + 1j * np.sin(angles))



def _all_points(contours):
    if not contours:
        return np.array([], dtype=np.complex128)
    return np.concatenate(contours)


def _contours_center(contours_a, contours_b):
    pts = []
    if contours_a:
        pts.append(_all_points(contours_a))
    if contours_b:
        pts.append(_all_points(contours_b))

    if not pts:
        return 0 + 0j

    all_pts = np.concatenate(pts)
    if len(all_pts) == 0:
        return 0 + 0j

    return complex(float(np.mean(all_pts.real)), float(np.mean(all_pts.imag)))


def _tiny_contour(center, n_points, size=1.0):
    angles = np.linspace(0, 2 * np.pi, n_points, endpoint=False)
    return center + size * (np.cos(angles) + 1j * np.sin(angles))


def _local_tiny_contour(counterpart, n_points):
    center = contour_center(counterpart)
    size = max(contour_diag(counterpart) * 0.018, 1.0)
    return _tiny_contour(center, n_points, size=size)


def match_contours(contours_a, contours_b, glyph_name_a=None, glyph_name_b=None, codepoint=None, script_hint=None):
    """
    强制匹配轮廓：
    - 不再因为轮廓数量不同而跳过
    - 以面积从大到小排序
    - 少的一方补极小轮廓
    - 然后统一方向、统一起点
    """
    if not contours_a or not contours_b:
        raise ValueError(f"Empty contour: A={len(contours_a)}, B={len(contours_b)}")

    contours_a = [np.asarray(c, dtype=np.complex128) for c in contours_a if c is not None and len(c) >= 4]
    contours_b = [np.asarray(c, dtype=np.complex128) for c in contours_b if c is not None and len(c) >= 4]

    if not script_hint:
        hint_a = infer_script_hint(codepoint, glyph_name_a)
        hint_b = infer_script_hint(codepoint, glyph_name_b)
        script_hint = hint_a if hint_a != "generic" else hint_b

    pts = []
    if contours_a:
        pts.append(_all_points(contours_a))
    if contours_b:
        pts.append(_all_points(contours_b))
    all_pts = np.concatenate(pts) if pts else np.array([0 + 0j])
    glyph_diag = max(
        float(np.hypot(np.max(all_pts.real) - np.min(all_pts.real), np.max(all_pts.imag) - np.min(all_pts.imag))),
        1.0,
    )
    glyph_bbox_value = glyph_bbox(contours_a + contours_b)

    matched_a = []
    matched_b = []
    used_b = set()

    for ia in sorted(range(len(contours_a)), key=lambda i: abs_area(contours_a[i]), reverse=True):
        ca = contours_a[ia]
        best_j = None
        best_score = float("inf")

        for jb, cb in enumerate(contours_b):
            if jb in used_b:
                continue
            score = contour_match_score(ca, cb, glyph_diag, glyph_bbox_value, script_hint)
            if score < best_score:
                best_score = score
                best_j = jb

        if best_j is None:
            cb = _local_tiny_contour(ca, len(ca))
        else:
            used_b.add(best_j)
            cb = contours_b[best_j]

        aa, bb = align_contour(ca, cb, script_hint=script_hint)
        matched_a.append(aa)
        matched_b.append(bb)

    for jb, cb in enumerate(contours_b):
        if jb in used_b:
            continue
        ca = _local_tiny_contour(cb, len(cb))
        aa, bb = align_contour(ca, cb, script_hint=script_hint)
        matched_a.append(aa)
        matched_b.append(bb)

    return matched_a, matched_b


def remove_isolated_spikes(contour):
    if contour is None or len(contour) < 8:
        return contour

    pts = np.asarray(contour, dtype=np.complex128).copy()
    changed = False

    for _ in range(2):
        edges = np.abs(np.roll(pts, -1) - pts)
        median_edge = float(np.median(edges))
        q3_edge = float(np.percentile(edges, 75))
        diag = max(contour_diag(pts), 1.0)
        threshold = max(median_edge * 5.2, q3_edge * 3.0, diag * 0.055)
        pass_changed = False

        for i in range(len(pts)):
            prev_p = pts[i - 1]
            curr = pts[i]
            next_p = pts[(i + 1) % len(pts)]
            d1 = abs(curr - prev_p)
            d2 = abs(next_p - curr)
            chord = abs(next_p - prev_p)

            if chord <= 1e-9:
                line_distance = min(d1, d2)
            else:
                line_distance = abs(np.imag((curr - prev_p) * np.conj(next_p - prev_p))) / chord

            isolated_peak = d1 > threshold and d2 > threshold and chord < max(d1, d2) * 0.76
            needle_peak = (
                line_distance > max(median_edge * 3.8, diag * 0.038)
                and d1 + d2 > chord * 3.2
                and max(d1, d2) > threshold
            )

            if isolated_peak or needle_peak:
                pts[i] = (prev_p + next_p) * 0.5
                changed = True
                pass_changed = True

        if not pass_changed:
            break

    return pts if changed else contour


def regularize_contour_spacing(contour):
    if contour is None or len(contour) < 16:
        return contour

    pts = np.asarray(contour, dtype=np.complex128)
    edges = np.abs(np.roll(pts, -1) - pts)
    median_edge = float(np.median(edges))
    diag = max(contour_diag(pts), 1.0)

    if median_edge <= 1e-9:
        return contour

    if float(np.max(edges)) <= max(median_edge * 8.5, diag * 0.13):
        return contour

    return resample_complex_contour(pts, len(pts))


def interpolate_contours(contours_a, contours_b, t):
    results = []
    for ca, cb in zip(contours_a, contours_b):
        contour = (1.0 - t) * ca + t * cb
        contour = remove_isolated_spikes(contour)
        contour = regularize_contour_spacing(contour)
        results.append(contour)
    return results


def contours_to_glyf(contours, glyf_table):
    coords = []
    end_pts = []
    flags = []

    for contour in contours:
        pts = np.column_stack([contour.real, contour.imag])

        for x, y in pts:
            coords.append((int(round(x)), int(round(y))))
            flags.append(1)

        end_pts.append(len(coords) - 1)

    glyph = Glyph()
    glyph.numberOfContours = len(end_pts)
    glyph.coordinates = GlyphCoordinates(coords)
    glyph.endPtsOfContours = list(end_pts)
    glyph.flags = array("B", flags)

    program = Program()
    program.fromBytecode([])
    glyph.program = program

    if coords:
        glyph.recalcBounds(glyf_table)
    else:
        glyph.xMin = glyph.yMin = glyph.xMax = glyph.yMax = 0

    return glyph


def interp_metric(font_a, font_b, glyph_a, glyph_b, t):
    if "hmtx" not in font_a or "hmtx" not in font_b:
        return None

    ma = font_a["hmtx"].metrics.get(glyph_a)
    mb = font_b["hmtx"].metrics.get(glyph_b)

    if not ma or not mb:
        return None

    aw = int(round(ma[0] * (1 - t) + mb[0] * t))
    lsb = int(round(ma[1] * (1 - t) + mb[1] * t))

    return aw, lsb


def default_gb_codepoints(cmap_a, cmap_b):
    """
    对照中国国标/传统蒙古文相关编码：
    两个字体都有的 codepoint 才生成，没有就跳过。
    不重复生成。
    """
    common = sorted(set(cmap_a.keys()) & set(cmap_b.keys()))

    result = []

    for cp in common:
        if 0x1800 <= cp <= 0x18AF:
            result.append(cp)
        elif 0xE000 <= cp <= 0xF8FF:
            result.append(cp)

    return sorted(set(result))


def zip_ttf_dir(out_dir, zip_path):
    out_dir = Path(out_dir)
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(out_dir.glob("*.ttf")):
            zf.write(p, arcname=p.name)

        report = out_dir / "gb_morph_report.json"
        if report.exists():
            zf.write(report, arcname="gb_morph_report.json")


def generate_gb_morph_steps(
    font_a_path,
    font_b_path,
    out_dir,
    prefix,
    steps=20,
    points_per_contour=160,
    codepoints=None,
):
    font_a_path = str(font_a_path)
    font_b_path = str(font_b_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for p in out_dir.glob("*.ttf"):
        p.unlink()

    font_a = TTFont(font_a_path, recalcBBoxes=True, recalcTimestamp=False)
    font_b = TTFont(font_b_path, recalcBBoxes=True, recalcTimestamp=False)

    if "glyf" not in font_a or "glyf" not in font_b:
        raise RuntimeError("当前算法只支持 TrueType glyf 轮廓字体。")

    cmap_a = best_cmap(font_a)
    cmap_b = best_cmap(font_b)

    if codepoints is None:
        codepoints = default_gb_codepoints(cmap_a, cmap_b)

    prepared = []
    skipped = []

    for cp in sorted(set(codepoints)):
        glyph_a = cmap_a.get(cp)
        glyph_b = cmap_b.get(cp)

        if not glyph_a or not glyph_b:
            skipped.append({
                "codepoint": f"U+{cp:04X}",
                "reason": "missing in cmap",
            })
            continue

        try:
            contours_a = glyph_to_sampled_contours(font_a, glyph_a, points_per_contour)
            contours_b = glyph_to_sampled_contours(font_b, glyph_b, points_per_contour)

            contours_a, contours_b = match_contours(
                contours_a,
                contours_b,
                glyph_name_a=glyph_a,
                glyph_name_b=glyph_b,
                codepoint=cp,
            )

            prepared.append({
                "cp": cp,
                "glyph_a": glyph_a,
                "glyph_b": glyph_b,
                "contours_a": contours_a,
                "contours_b": contours_b,
                "contour_count": len(contours_a),
            })

        except Exception as e:
            skipped.append({
                "codepoint": f"U+{cp:04X}",
                "char": chr(cp) if cp <= 0x10FFFF else "",
                "glyph_a": glyph_a,
                "glyph_b": glyph_b,
                "reason": str(e),
            })

    if not prepared:
        raise RuntimeError("没有任何可生成字形。请检查两个字体是否有共同中国国标/蒙古文 codepoint。")

    generated = []

    for step in range(1, steps + 1):
        t = step / (steps + 1)

        out_font = copy.deepcopy(font_a)
        glyf = out_font["glyf"]

        for item in prepared:
            contours = interpolate_contours(item["contours_a"], item["contours_b"], t)
            new_glyph = contours_to_glyf(contours, glyf)

            glyph_name = item["glyph_a"]
            glyf[glyph_name] = new_glyph

            metric = interp_metric(
                font_a,
                font_b,
                item["glyph_a"],
                item["glyph_b"],
                t,
            )

            if metric and "hmtx" in out_font:
                out_font["hmtx"].metrics[glyph_name] = metric

        family = f"{prefix}_Step_{step:02d}"
        set_names(out_font, family)

        out_file = out_dir / f"{prefix}_{step:02d}.ttf"
        out_font.save(str(out_file))
        generated.append(out_file.name)

        print(f"[GB-MORPH] {prefix} step {step:02d}/{steps}, t={t:.6f}, glyphs={len(prepared)}")

    report = {
        "algorithm": "structure_aware_resampled_contour_morph_v2",
        "formula": "P(t) = (1 - t) * PA + t * PB",
        "font_a": font_a_path,
        "font_b": font_b_path,
        "out_dir": str(out_dir),
        "prefix": prefix,
        "steps": steps,
        "points_per_contour": points_per_contour,
        "generated_files": generated,
        "generated_glyphs": len(prepared),
        "skipped_glyphs": len(skipped),
        "prepared": [
            {
                "codepoint": f"U+{x['cp']:04X}",
                "char": chr(x["cp"]) if x["cp"] <= 0x10FFFF else "",
                "glyph_a": x["glyph_a"],
                "glyph_b": x["glyph_b"],
                "contour_count": x["contour_count"],
            }
            for x in prepared
        ],
        "skipped": skipped,
    }

    (out_dir / "gb_morph_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return report
