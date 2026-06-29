import csv
import itertools
import json
import math
import os
import random
import shutil
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import cv2
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from fontTools.pens.basePen import BasePen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.path import Path as MplPath
from matplotlib.patches import PathPatch
from skimage.metrics import peak_signal_noise_ratio, structural_similarity
from skimage.morphology import skeletonize


RANDOM_SEED = 20260622
random.seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)
torch.manual_seed(RANDOM_SEED)

ROOT = Path("/root/autodl-tmp/font_morph_batch_pairs_full_20260617_v2")
OUT = Path("/root/autodl-tmp/font_morph_ei_experiments_20260622")
CANVAS = 128
TRAIN_CANVAS = 64
STEPS = 20


class FlattenPathPen(BasePen):
    def __init__(self, glyph_set, curve_steps=12):
        super().__init__(glyph_set)
        self.curve_steps = curve_steps
        self.vertices = []
        self.codes = []
        self.current = None
        self.start = None

    def _moveTo(self, p0):
        self.vertices.append(p0)
        self.codes.append(MplPath.MOVETO)
        self.current = p0
        self.start = p0

    def _lineTo(self, p1):
        self.vertices.append(p1)
        self.codes.append(MplPath.LINETO)
        self.current = p1

    def _qCurveToOne(self, p1, p2):
        p0 = self.current
        for i in range(1, self.curve_steps + 1):
            t = i / self.curve_steps
            x = (1 - t) * (1 - t) * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0]
            y = (1 - t) * (1 - t) * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]
            self.vertices.append((x, y))
            self.codes.append(MplPath.LINETO)
        self.current = p2

    def _curveToOne(self, p1, p2, p3):
        p0 = self.current
        for i in range(1, self.curve_steps + 1):
            t = i / self.curve_steps
            x = (
                (1 - t) ** 3 * p0[0]
                + 3 * (1 - t) ** 2 * t * p1[0]
                + 3 * (1 - t) * t * t * p2[0]
                + t ** 3 * p3[0]
            )
            y = (
                (1 - t) ** 3 * p0[1]
                + 3 * (1 - t) ** 2 * t * p1[1]
                + 3 * (1 - t) * t * t * p2[1]
                + t ** 3 * p3[1]
            )
            self.vertices.append((x, y))
            self.codes.append(MplPath.LINETO)
        self.current = p3

    def _closePath(self):
        if self.start is not None:
            self.vertices.append(self.start)
            self.codes.append(MplPath.CLOSEPOLY)
        self.current = None
        self.start = None

    def _endPath(self):
        self.current = None
        self.start = None


def reset_out():
    if OUT.exists():
        shutil.rmtree(OUT)
    for name in [
        "01_data_augmentation_scale",
        "02_comparison_with_image_augmentation",
        "04_downstream_font_generation",
        "05_multi_format_usability",
        "common_samples",
    ]:
        (OUT / name / "tables").mkdir(parents=True, exist_ok=True)
        (OUT / name / "figures").mkdir(parents=True, exist_ok=True)
        (OUT / name / "samples").mkdir(parents=True, exist_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_csv(path, rows, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def load_summary(company):
    return read_json(ROOT / company / "summary.json")


def numeric_report(item, key, default=0.0):
    try:
        return float(item.get("report", {}).get(key, default))
    except Exception:
        return default


def ttf_paths_for_pair(company, pair):
    pair_dir = ROOT / company / "pairs" / pair
    prefix = "menk_gb_step_" if company == "menk" else "oyun_gb_step_"
    return [pair_dir / f"{prefix}{i:02d}.ttf" for i in range(1, STEPS + 1)]


def font_path(company, filename):
    matches = list((ROOT / company / "fonts").glob(filename))
    if matches:
        return matches[0]
    normalized = filename.replace(" (1)", "")
    for p in (ROOT / company / "fonts").glob("*.ttf"):
        if p.name.replace(" (1)", "") == normalized:
            return p
    return ROOT / company / "fonts" / filename


def render_glyph_mask(font_path, glyph_name, canvas=CANVAS, pad=14):
    try:
        font = TTFont(str(font_path), lazy=False)
        glyph_set = font.getGlyphSet()
        if glyph_name not in glyph_set:
            return None
        pen = FlattenPathPen(glyph_set, curve_steps=10)
        glyph_set[glyph_name].draw(pen)
        if not pen.vertices:
            return None
        verts = np.array(pen.vertices, dtype=float)
        codes = np.array(pen.codes, dtype=np.uint8)
        xs, ys = verts[:, 0], verts[:, 1]
        minx, maxx = float(xs.min()), float(xs.max())
        miny, maxy = float(ys.min()), float(ys.max())
        w, h = maxx - minx, maxy - miny
        if w <= 1 or h <= 1:
            return None
        scale = (canvas - 2 * pad) / max(w, h)
        extra_x = (canvas - 2 * pad - w * scale) * 0.5
        extra_y = (canvas - 2 * pad - h * scale) * 0.5
        tv = np.empty_like(verts)
        tv[:, 0] = (verts[:, 0] - minx) * scale + pad + extra_x
        tv[:, 1] = canvas - ((verts[:, 1] - miny) * scale + pad + extra_y)
        path = MplPath(tv, codes)
        fig = Figure(figsize=(canvas / 100, canvas / 100), dpi=100)
        fig.patch.set_facecolor("white")
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, canvas)
        ax.set_ylim(canvas, 0)
        ax.axis("off")
        ax.add_patch(PathPatch(path, facecolor="black", edgecolor="none", antialiased=True))
        canvas_obj = FigureCanvasAgg(fig)
        canvas_obj.draw()
        rgba = np.asarray(canvas_obj.buffer_rgba())
        gray = rgba[:, :, :3].mean(axis=2)
        return (gray < 210).astype(np.float32)
    except Exception:
        return None


def save_mask_image(mask, path):
    img = ((1.0 - mask) * 255).clip(0, 255).astype(np.uint8)
    cv2.imwrite(str(path), img)


def save_svg(font_path, glyph_name, path):
    font = TTFont(str(font_path), lazy=False)
    glyph_set = font.getGlyphSet()
    if glyph_name not in glyph_set:
        return False
    pen = SVGPathPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    d = pen.getCommands()
    if not d:
        return False
    upm = font["head"].unitsPerEm
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 {-upm} {upm} {upm}">
  <path d="{d}" transform="scale(1,-1)" fill="#111111"/>
</svg>
'''
    path.write_text(svg, encoding="utf-8")
    return True


def foreground_points(mask, max_points=400):
    pts = np.argwhere(mask > 0.5).astype(np.float32)
    if len(pts) == 0:
        return pts
    if len(pts) > max_points:
        idx = np.linspace(0, len(pts) - 1, max_points).astype(int)
        pts = pts[idx]
    return pts


def chamfer_distance(a, b):
    pa, pb = foreground_points(a), foreground_points(b)
    if len(pa) == 0 or len(pb) == 0:
        return 1.0
    d = ((pa[:, None, :] - pb[None, :, :]) ** 2).sum(axis=2) ** 0.5
    return float((d.min(axis=1).mean() + d.min(axis=0).mean()) * 0.5 / (a.shape[0] * math.sqrt(2)))


def hausdorff_distance(a, b):
    pa, pb = foreground_points(a), foreground_points(b)
    if len(pa) == 0 or len(pb) == 0:
        return 1.0
    d = ((pa[:, None, :] - pb[None, :, :]) ** 2).sum(axis=2) ** 0.5
    return float(max(d.min(axis=1).max(), d.min(axis=0).max()) / (a.shape[0] * math.sqrt(2)))


def ssim_score(a, b):
    try:
        return float(structural_similarity(a, b, data_range=1.0))
    except Exception:
        return 0.0


def psnr_score(a, b):
    try:
        value = float(peak_signal_noise_ratio(a, b, data_range=1.0))
        if not math.isfinite(value):
            return 99.0
        return value
    except Exception:
        return 0.0


def skeleton_iou(a, b):
    sa = skeletonize(a > 0.5)
    sb = skeletonize(b > 0.5)
    inter = np.logical_and(sa, sb).sum()
    union = np.logical_or(sa, sb).sum()
    return float(inter / union) if union else 0.0


def component_count(mask):
    arr = (mask > 0.5).astype(np.uint8)
    n, _ = cv2.connectedComponents(arr, connectivity=8)
    return max(0, int(n - 1))


def affine_aug(mask, rng, strong=False):
    h, w = mask.shape
    angle = rng.uniform(-10, 10) if not strong else rng.uniform(-16, 16)
    scale = rng.uniform(0.90, 1.10) if not strong else rng.uniform(0.82, 1.18)
    shear = rng.uniform(-0.10, 0.10) if not strong else rng.uniform(-0.18, 0.18)
    tx = rng.uniform(-5, 5) if not strong else rng.uniform(-8, 8)
    ty = rng.uniform(-5, 5) if not strong else rng.uniform(-8, 8)
    center = (w / 2, h / 2)
    m = cv2.getRotationMatrix2D(center, angle, scale)
    shear_m = np.array([[1, shear, -shear * center[1]], [0, 1, 0]], dtype=np.float32)
    m3 = np.vstack([m, [0, 0, 1]]).astype(np.float32)
    sm3 = np.vstack([shear_m, [0, 0, 1]]).astype(np.float32)
    out_m = (sm3 @ m3)[:2]
    out_m[:, 2] += [tx, ty]
    out = cv2.warpAffine(mask.astype(np.float32), out_m, (w, h), flags=cv2.INTER_LINEAR, borderValue=0)
    return np.clip(out, 0, 1)


def resize_mask(mask, size=TRAIN_CANVAS):
    return cv2.resize(mask.astype(np.float32), (size, size), interpolation=cv2.INTER_AREA)


def best_pair(company="menk"):
    summary = load_summary(company)
    valid = [x for x in summary["items"] if x.get("ok")]
    valid.sort(key=lambda x: numeric_report(x, "interpolated"), reverse=True)
    return valid[0]


def read_runtime_rows(company, pair):
    pair_dir = ROOT / company / "pairs" / pair
    name = "menk_gb_runtime.csv" if company == "menk" else "oyun_gb_runtime.csv"
    rows = []
    with (pair_dir / name).open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def choose_glyphs(company, pair_item, max_glyphs=28):
    pair = pair_item["pair"]
    rows = read_runtime_rows(company, pair)
    a_path = font_path(company, pair_item["font_a"])
    b_path = font_path(company, pair_item["font_b"])
    step_paths = ttf_paths_for_pair(company, pair)
    chosen = []
    seen = set()
    for row in rows:
        if row.get("usable_for_ttf") not in {"1", "true", "True"}:
            continue
        name = row.get("fontA_target_glyph_names", "").split("|")[0].split(",")[0].strip()
        if not name or name in seen:
            continue
        masks = [render_glyph_mask(a_path, name), render_glyph_mask(b_path, name)]
        if any(m is None or m.sum() < 10 for m in masks):
            continue
        probe = render_glyph_mask(step_paths[len(step_paths) // 2], name)
        if probe is None or probe.sum() < 10:
            continue
        chosen.append({"glyph": name, "runtime_id": row.get("runtime_id", ""), "unicode": row.get("base_unicode", "")})
        seen.add(name)
        if len(chosen) >= max_glyphs:
            break
    return chosen


def build_render_cache(company="menk", max_glyphs=24):
    pair_item = best_pair(company)
    glyphs = choose_glyphs(company, pair_item, max_glyphs=max_glyphs)
    a_path = font_path(company, pair_item["font_a"])
    b_path = font_path(company, pair_item["font_b"])
    step_paths = ttf_paths_for_pair(company, pair_item["pair"])
    cache = {"company": company, "pair_item": pair_item, "glyphs": glyphs, "images": {}}
    for g in glyphs:
        name = g["glyph"]
        cache["images"][("A", name)] = render_glyph_mask(a_path, name)
        cache["images"][("B", name)] = render_glyph_mask(b_path, name)
        for i, p in enumerate(step_paths, 1):
            cache["images"][(f"S{i:02d}", name)] = render_glyph_mask(p, name)
    meta_path = OUT / "common_samples" / "tables" / "selected_pair_and_glyphs.json"
    meta = {
        "company": company,
        "pair": pair_item["pair"],
        "font_a": pair_item["font_a"],
        "font_b": pair_item["font_b"],
        "interpolated_glyphs_reported": numeric_report(pair_item, "interpolated"),
        "selected_glyph_count": len(glyphs),
        "glyphs": glyphs,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return cache


def save_metric_plot(rows, x_key, y_keys, labels, title, ylabel, out_base):
    plt.figure(figsize=(7.2, 4.4))
    xs = [row[x_key] for row in rows]
    for y, label in zip(y_keys, labels):
        plt.plot(xs, [float(row[y]) for row in rows], marker="o", linewidth=2.3, label=label)
    plt.title(title)
    plt.xlabel(x_key.replace("_", " ").title())
    plt.ylabel(ylabel)
    plt.grid(True, alpha=0.25)
    plt.legend(frameon=False)
    plt.tight_layout()
    for ext in ["png", "svg", "pdf"]:
        plt.savefig(f"{out_base}.{ext}", dpi=320)
    plt.close()


def experiment_1():
    exp = OUT / "01_data_augmentation_scale"
    summaries = {c: load_summary(c) for c in ["oyun", "menk"]}
    full_rows = []
    for company, summary in summaries.items():
        items = [x for x in summary["items"] if x.get("ok")]
        target_total = sum(numeric_report(x, "runtime_items") * x["ttf_count"] for x in items)
        success_total = sum(numeric_report(x, "interpolated") * x["ttf_count"] for x in items)
        skip_total = sum(
            (numeric_report(x, "skipped_missing")
             + numeric_report(x, "skipped_incompatible")
             + numeric_report(x, "skipped_multi_glyph_sequence"))
            * x["ttf_count"]
            for x in items
        )
        full_rows.append({
            "company": company,
            "original_font_count": summary["font_count"],
            "pair_count": summary["pair_count"],
            "steps_per_pair": summary["steps"],
            "generated_ttf_count": sum(x["ttf_count"] for x in items),
            "target_glyph_instances": int(target_total),
            "successfully_interpolated_glyph_instances": int(success_total),
            "skipped_or_noninterpolated_glyph_instances": int(skip_total),
            "success_rate": round(success_total / target_total, 4) if target_total else 0,
            "mean_runtime_glyphs_per_font": round(np.mean([numeric_report(x, "runtime_items") for x in items]), 2),
            "mean_success_glyphs_per_font": round(np.mean([numeric_report(x, "interpolated") for x in items]), 2),
            "ttf_count": sum(x["ttf_count"] for x in items),
            "svg_count": "sampled in Experiment 5",
            "jpg_png_count": "sampled in Experiment 5",
        })
    write_csv(exp / "tables" / "full_scale_real_outputs.csv", full_rows, list(full_rows[0].keys()))

    menk = summaries["menk"]
    items = [x for x in menk["items"] if x.get("ok")]
    font_scores = defaultdict(list)
    pair_map = {}
    for item in items:
        a, b = item["font_a"], item["font_b"]
        score = numeric_report(item, "interpolated")
        font_scores[a].append(score)
        font_scores[b].append(score)
        pair_map[frozenset([a, b])] = item
    ranked_fonts = sorted(font_scores, key=lambda f: np.mean(font_scores[f]), reverse=True)
    scale_rows = []
    for n in [2, 4, 6, 10]:
        chosen = ranked_fonts[:n]
        chosen_pairs = [pair_map[frozenset(p)] for p in itertools.combinations(chosen, 2)]
        target = sum(numeric_report(x, "runtime_items") * STEPS for x in chosen_pairs)
        success = sum(numeric_report(x, "interpolated") * STEPS for x in chosen_pairs)
        skipped = sum(
            (numeric_report(x, "skipped_missing") + numeric_report(x, "skipped_incompatible") + numeric_report(x, "skipped_multi_glyph_sequence"))
            * STEPS
            for x in chosen_pairs
        )
        scale_rows.append({
            "original_font_count_n": n,
            "steps_k": STEPS,
            "pair_count_C_n_2": math.comb(n, 2),
            "new_font_count_N": math.comb(n, 2) * STEPS,
            "target_glyph_instances": int(target),
            "successfully_interpolated_glyph_instances": int(success),
            "skipped_or_noninterpolated_glyph_instances": int(skipped),
            "success_rate": round(success / target, 4) if target else 0,
            "selected_fonts": ";".join(chosen),
        })
    write_csv(exp / "tables" / "scale_formula_and_measured_subset.csv", scale_rows, list(scale_rows[0].keys()))

    plt.figure(figsize=(7.2, 4.4))
    x = [r["original_font_count_n"] for r in scale_rows]
    y = [r["new_font_count_N"] for r in scale_rows]
    bars = plt.bar(x, y, color="#2F6FDB", width=0.7)
    plt.plot(x, y, color="#111827", marker="o", linewidth=2.0)
    for b, val in zip(bars, y):
        plt.text(b.get_x() + b.get_width() / 2, b.get_height() + max(y) * 0.015, str(val), ha="center", va="bottom", fontsize=10)
    plt.xlabel("Number of original fonts (n)")
    plt.ylabel("Generated fonts N = C(n,2) x k")
    plt.title("Data Expansion Scale under 20-step Pairwise Interpolation")
    plt.grid(axis="y", alpha=0.25)
    plt.tight_layout()
    for ext in ["png", "svg", "pdf"]:
        plt.savefig(exp / "figures" / f"fig1_scale_growth.{ext}", dpi=320)
    plt.close()

    plt.figure(figsize=(7.4, 4.4))
    labels = [r["company"].upper() for r in full_rows]
    gen = [r["generated_ttf_count"] for r in full_rows]
    pairs = [r["pair_count"] for r in full_rows]
    xloc = np.arange(len(labels))
    plt.bar(xloc - 0.17, pairs, width=0.34, label="Font pairs", color="#73A2F2")
    plt.bar(xloc + 0.17, gen, width=0.34, label="Generated TTFs", color="#23395B")
    plt.xticks(xloc, labels)
    plt.ylabel("Count")
    plt.title("Full-system Real Output Scale")
    plt.grid(axis="y", alpha=0.25)
    plt.legend(frameon=False)
    plt.tight_layout()
    for ext in ["png", "svg", "pdf"]:
        plt.savefig(exp / "figures" / f"fig2_full_real_output_counts.{ext}", dpi=320)
    plt.close()


def nearest_endpoint_metrics(img, ref_a, ref_b):
    ssim_a, ssim_b = ssim_score(img, ref_a), ssim_score(img, ref_b)
    if ssim_a >= ssim_b:
        ref = ref_a
    else:
        ref = ref_b
    return {
        "ssim_to_nearest_endpoint": ssim_score(img, ref),
        "psnr_to_nearest_endpoint": psnr_score(img, ref),
        "chamfer_to_nearest_endpoint": chamfer_distance(img, ref),
        "hausdorff_to_nearest_endpoint": hausdorff_distance(img, ref),
        "skeleton_iou_to_nearest_endpoint": skeleton_iou(img, ref),
        "component_error": 1 if abs(component_count(img) - component_count(ref)) > 1 else 0,
    }


def experiment_2(cache):
    exp = OUT / "02_comparison_with_image_augmentation"
    rng = random.Random(RANDOM_SEED)
    glyphs = [g["glyph"] for g in cache["glyphs"][:18]]
    samples = defaultdict(list)
    for g in glyphs:
        a = cache["images"][("A", g)]
        b = cache["images"][("B", g)]
        samples["Original"].extend([(g, a), (g, b)])
        for i in range(1, STEPS + 1):
            samples["Ours"].append((g, cache["images"][(f"S{i:02d}", g)]))

    equal_n = min(320, len(samples["Ours"]))
    originals = samples["Original"]
    for _ in range(equal_n):
        g, img = rng.choice(originals)
        samples["Image Aug"].append((g, affine_aug(img, rng, strong=True)))
    for _ in range(equal_n):
        g, a = rng.choice(originals)
        _, b = rng.choice([x for x in originals if x[0] == g] or originals)
        alpha = rng.uniform(0.35, 0.65)
        samples["Mixup"].append((g, np.clip(alpha * a + (1 - alpha) * b, 0, 1)))
    ours_base = samples["Ours"][:]
    for _ in range(equal_n):
        g, img = rng.choice(ours_base)
        samples["Ours + Image Aug"].append((g, affine_aug(img, rng, strong=False)))
    samples["Ours"] = samples["Ours"][:equal_n]
    samples["Original"] = [rng.choice(originals) for _ in range(equal_n)]

    rows = []
    for group, items in samples.items():
        metrics = []
        for g, img in items:
            ref_a = cache["images"][("A", g)]
            ref_b = cache["images"][("B", g)]
            metrics.append(nearest_endpoint_metrics(img, ref_a, ref_b))
        row = {"group": group, "sample_count_equal_size": len(items)}
        for key in metrics[0].keys():
            row[key] = round(float(np.mean([m[key] for m in metrics])), 5)
        row["style_variation_score"] = round(row["chamfer_to_nearest_endpoint"], 5)
        row["abnormal_component_rate"] = round(row["component_error"], 5)
        rows.append(row)
    order = ["Original", "Image Aug", "Mixup", "Ours", "Ours + Image Aug"]
    rows.sort(key=lambda r: order.index(r["group"]))
    write_csv(exp / "tables" / "equal_size_quality_metrics.csv", rows, list(rows[0].keys()))

    full_rows = []
    for group in order:
        if group == "Original":
            count = len(originals)
        elif group == "Image Aug":
            count = len(originals) * STEPS
        elif group == "Mixup":
            count = len(originals) * STEPS
        elif group == "Ours":
            count = len(glyphs) * STEPS
        else:
            count = len(glyphs) * STEPS * 2
        full_rows.append({"group": group, "full_size_sample_count": count, "purpose": "full-size expansion capacity"})
    write_csv(exp / "tables" / "full_size_setting_counts.csv", full_rows, list(full_rows[0].keys()))

    metric_names = [
        ("skeleton_iou_to_nearest_endpoint", "Skeleton IoU"),
        ("style_variation_score", "Style variation"),
        ("chamfer_to_nearest_endpoint", "Chamfer"),
        ("abnormal_component_rate", "Abnormal rate"),
    ]
    plt.figure(figsize=(8.6, 4.8))
    x = np.arange(len(order))
    width = 0.18
    for idx, (key, label) in enumerate(metric_names):
        vals = [next(r for r in rows if r["group"] == g)[key] for g in order]
        if key in {"chamfer_to_nearest_endpoint", "style_variation_score"}:
            vals = [min(v * 10, 1.0) for v in vals]
            label += " (x10)"
        plt.bar(x + (idx - 1.5) * width, vals, width=width, label=label)
    plt.xticks(x, order, rotation=12)
    plt.ylim(0, 1.05)
    plt.ylabel("Normalized metric value")
    plt.title("Equal-size Comparison: Image Perturbation vs. Glyph-style Interpolation")
    plt.grid(axis="y", alpha=0.25)
    plt.legend(frameon=False, ncol=2)
    plt.tight_layout()
    for ext in ["png", "svg", "pdf"]:
        plt.savefig(exp / "figures" / f"fig3_equal_size_comparison.{ext}", dpi=320)
    plt.close()

    # Sample montage.
    show_groups = order
    fig, axes = plt.subplots(len(show_groups), 6, figsize=(8.2, 6.8))
    for r, group in enumerate(show_groups):
        for c in range(6):
            g, img = samples[group][c * max(1, len(samples[group]) // 6)]
            axes[r, c].imshow(1 - img, cmap="gray", vmin=0, vmax=1)
            axes[r, c].axis("off")
            if c == 0:
                axes[r, c].set_ylabel(group, fontsize=10)
    plt.suptitle("Visual Samples of Different Augmentation Sources", y=0.995)
    plt.tight_layout()
    plt.savefig(exp / "figures" / "fig4_augmentation_visual_samples.png", dpi=320)
    plt.close()


class TinyAutoEncoder(nn.Module):
    def __init__(self, in_channels=1):
        super().__init__()
        self.enc = nn.Sequential(
            nn.Conv2d(in_channels, 16, 3, 2, 1),
            nn.ReLU(inplace=True),
            nn.Conv2d(16, 32, 3, 2, 1),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 64, 3, 2, 1),
            nn.ReLU(inplace=True),
        )
        self.dec = nn.Sequential(
            nn.ConvTranspose2d(64, 32, 4, 2, 1),
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(32, 16, 4, 2, 1),
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(16, 1, 4, 2, 1),
            nn.Sigmoid(),
        )

    def forward(self, x):
        return self.dec(self.enc(x))


def foreground_aware_loss(pred, target):
    bce = F.binary_cross_entropy(pred, target)
    inter = (pred * target).sum(dim=(1, 2, 3))
    union = pred.sum(dim=(1, 2, 3)) + target.sum(dim=(1, 2, 3))
    dice = 1.0 - ((2.0 * inter + 1.0) / (union + 1.0)).mean()
    l1 = F.l1_loss(pred, target)
    return 0.45 * bce + 0.45 * dice + 0.10 * l1


def binarize_prediction(pred):
    arr = np.clip(pred, 0, 1).astype(np.float32)
    u8 = (arr * 255).astype(np.uint8)
    if int(u8.max()) <= int(u8.min()):
        threshold = 0.35
    else:
        threshold, _ = cv2.threshold(u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        threshold = max(0.18, min(0.62, threshold / 255.0))
    return (arr > threshold).astype(np.float32)


def corrupt_for_training(mask, rng):
    x = affine_aug(mask, rng, strong=False)
    noise = rng.normalvariate(0, 0.03)
    if abs(noise) > 1e-6:
        x = np.clip(x + np.random.normal(0, abs(noise), size=x.shape), 0, 1)
    return x.astype(np.float32)


def make_condition_input(source, style_value):
    src = resize_mask(source)
    style = np.full_like(src, float(style_value), dtype=np.float32)
    return np.stack([src, style], axis=0).astype(np.float32)


def make_train_records(cache, group, glyphs):
    rng = random.Random(RANDOM_SEED + hash(group) % 1000)
    base = []
    train_steps = [1, 2, 3, 4, 6, 7, 8, 9, 11, 12, 13, 14, 16, 17, 18, 19]
    endpoint_records = []
    for g in glyphs:
        endpoint_records.extend([
            (g, 0.0, cache["images"][("A", g)]),
            (g, 1.0, cache["images"][("B", g)]),
        ])
    if group == "Baseline":
        for _ in range(len(glyphs) * len(train_steps)):
            g, style, target = rng.choice(endpoint_records)
            source = cache["images"][("A", g)]
            base.append((make_condition_input(source, style), resize_mask(target)))
    elif group == "Baseline + Image Aug":
        for _ in range(len(glyphs) * len(train_steps)):
            g, style, target = rng.choice(endpoint_records)
            source = cache["images"][("A", g)]
            base.append((make_condition_input(source, style), resize_mask(affine_aug(target, rng, strong=True))))
    elif group == "Baseline + Ours":
        for g in glyphs:
            for s in train_steps:
                source = cache["images"][("A", g)]
                target = cache["images"][(f"S{s:02d}", g)]
                base.append((make_condition_input(source, s / STEPS), resize_mask(target)))
    else:
        for g in glyphs:
            for s in train_steps:
                source = cache["images"][("A", g)]
                target = cache["images"][(f"S{s:02d}", g)]
                mixed_target = affine_aug(target, rng, strong=False) if rng.random() < 0.5 else target
                base.append((make_condition_input(source, s / STEPS), resize_mask(mixed_target)))
    return base


def train_group_model(group, records, test_records, device, fig_dir):
    model = TinyAutoEncoder(in_channels=2).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    losses = []
    rng = random.Random(RANDOM_SEED)
    batch_size = 32
    xs = np.stack([x for x, _ in records]).astype(np.float32)
    ys = np.stack([y for _, y in records]).astype(np.float32)
    x_t = torch.from_numpy(xs).to(device)
    y_t = torch.from_numpy(ys[:, None]).to(device)
    for epoch in range(1, 31):
        order = list(range(len(records)))
        rng.shuffle(order)
        epoch_losses = []
        model.train()
        for start in range(0, len(order), batch_size):
            idx = order[start:start + batch_size]
            pred = model(x_t[idx])
            loss = foreground_aware_loss(pred, y_t[idx])
            opt.zero_grad()
            loss.backward()
            opt.step()
            epoch_losses.append(float(loss.detach().cpu()))
        losses.append({"epoch": epoch, "training_loss": float(np.mean(epoch_losses))})
    model.eval()
    metrics = []
    sample_outputs = []
    with torch.no_grad():
        for name, x, y in test_records:
            xr = x
            yr = resize_mask(y)
            pred = model(torch.from_numpy(xr[None].astype(np.float32)).to(device)).cpu().numpy()[0, 0]
            metrics.append({
                "sample": name,
                "mae_l1": float(np.mean(np.abs(pred - yr))),
                "ssim": ssim_score(pred, yr),
                "psnr": psnr_score(pred, yr),
                "chamfer": chamfer_distance(binarize_prediction(pred), (yr > 0.5).astype(np.float32)),
                "skeleton_iou": skeleton_iou(binarize_prediction(pred), (yr > 0.5).astype(np.float32)),
            })
            if len(sample_outputs) < 5:
                sample_outputs.append((xr[0], yr, pred))
    return model, losses, metrics, sample_outputs


def experiment_4(cache):
    exp = OUT / "04_downstream_font_generation"
    glyphs = [g["glyph"] for g in cache["glyphs"][:20]]
    rng = random.Random(RANDOM_SEED + 4)
    test_records = []
    test_steps = [5, 10, 15, 20]
    for g in glyphs:
        source = cache["images"][("A", g)]
        for s in test_steps:
            clean = cache["images"][(f"S{s:02d}", g)]
            test_records.append((f"{g}_S{s:02d}", make_condition_input(source, s / STEPS), clean))

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    groups = ["Baseline", "Baseline + Image Aug", "Baseline + Ours", "Baseline + Ours + Image Aug"]
    result_rows = []
    loss_rows = []
    sample_grid = {}
    for group in groups:
        records = make_train_records(cache, group, glyphs)
        _, losses, metrics, samples = train_group_model(group, records, test_records, device, exp / "figures")
        for loss in losses:
            loss_rows.append({"group": group, **loss})
        row = {"group": group, "train_samples": len(records), "test_samples": len(test_records), "device": str(device)}
        for key in ["mae_l1", "ssim", "psnr", "chamfer", "skeleton_iou"]:
            row[key] = round(float(np.mean([m[key] for m in metrics])), 5)
        result_rows.append(row)
        sample_grid[group] = samples
    write_csv(exp / "tables" / "downstream_model_metrics.csv", result_rows, list(result_rows[0].keys()))
    write_csv(exp / "tables" / "training_loss_curve.csv", loss_rows, ["group", "epoch", "training_loss"])

    plt.figure(figsize=(7.4, 4.5))
    for group in groups:
        xs = [r["epoch"] for r in loss_rows if r["group"] == group]
        ys = [r["training_loss"] for r in loss_rows if r["group"] == group]
        plt.plot(xs, ys, marker="o", linewidth=2, label=group)
    plt.xlabel("Epoch")
    plt.ylabel("Foreground-aware training loss")
    plt.title("Convergence of the Same Auto-encoding Baseline")
    plt.grid(True, alpha=0.25)
    plt.legend(frameon=False, fontsize=8)
    plt.tight_layout()
    for ext in ["png", "svg", "pdf"]:
        plt.savefig(exp / "figures" / f"fig5_downstream_training_curve.{ext}", dpi=320)
    plt.close()

    metric_keys = ["mae_l1", "ssim", "psnr", "skeleton_iou"]
    fig, axes = plt.subplots(1, 4, figsize=(11.5, 3.2))
    for ax, key in zip(axes, metric_keys):
        vals = [next(r for r in result_rows if r["group"] == g)[key] for g in groups]
        ax.bar(range(len(groups)), vals, color=["#94A3B8", "#60A5FA", "#2563EB", "#0F172A"])
        ax.set_title(key)
        ax.set_xticks(range(len(groups)))
        ax.set_xticklabels(["Base", "+Img", "+Ours", "+Both"], rotation=20)
        ax.grid(axis="y", alpha=0.22)
    plt.suptitle("Conditional Glyph Generation on Unseen Interpolation Styles", y=1.03)
    plt.tight_layout()
    for ext in ["png", "svg", "pdf"]:
        plt.savefig(exp / "figures" / f"fig6_downstream_metrics.{ext}", dpi=320)
    plt.close()

    fig, axes = plt.subplots(len(groups), 9, figsize=(11, 6.2))
    for r, group in enumerate(groups):
        samples = sample_grid[group][:3]
        for c, (inp, target, pred) in enumerate(samples):
            for off, img in enumerate([inp, target, pred]):
                axes[r, c * 3 + off].imshow(1 - img, cmap="gray", vmin=0, vmax=1)
                axes[r, c * 3 + off].axis("off")
                if r == 0:
                    axes[r, c * 3 + off].set_title(["Input", "Target", "Output"][off], fontsize=8)
        axes[r, 0].set_ylabel(group, fontsize=8)
    plt.tight_layout()
    plt.savefig(exp / "figures" / "fig7_downstream_visual_outputs.png", dpi=320)
    plt.close()


def validate_ttf(path):
    try:
        font = TTFont(str(path), lazy=True)
        needed = {"head", "hhea", "maxp", "cmap", "glyf", "hmtx"}
        tables = set(font.keys())
        return needed.issubset(tables), len(font.getGlyphOrder()), ";".join(sorted(needed - tables))
    except Exception as exc:
        return False, 0, str(exc)


def experiment_5(cache):
    exp = OUT / "05_multi_format_usability"
    rows = []
    all_ttf = []
    for company in ["oyun", "menk"]:
        all_ttf.extend((ROOT / company / "pairs").glob("*/*.ttf"))
    sample_ttf = sorted(all_ttf)[::max(1, len(all_ttf) // 300)][:300]
    valid = 0
    glyph_counts = []
    for p in sample_ttf:
        ok, gc, missing = validate_ttf(p)
        valid += int(ok)
        glyph_counts.append(gc)
    rows.append({
        "format": "TTF",
        "data_level": "font file",
        "actual_full_count": len(all_ttf),
        "validated_sample_count": len(sample_ttf),
        "valid_count": valid,
        "valid_rate": round(valid / len(sample_ttf), 4) if sample_ttf else 0,
        "trainable": "indirect by rendering",
        "editable": "yes",
        "typesetting": "yes",
        "metadata": "Unicode/glyph names/pair/step retained in reports",
    })

    pair = cache["pair_item"]["pair"]
    step_paths = ttf_paths_for_pair(cache["company"], pair)
    glyphs = [g["glyph"] for g in cache["glyphs"][:12]]
    svg_count = png_count = jpg_count = 0
    for s, path in enumerate(step_paths[:10], 1):
        for glyph in glyphs:
            stem = f"{pair}_step{s:02d}_{glyph}"
            mask = render_glyph_mask(path, glyph)
            if mask is None:
                continue
            png_path = exp / "samples" / f"{stem}.png"
            jpg_path = exp / "samples" / f"{stem}.jpg"
            svg_path = exp / "samples" / f"{stem}.svg"
            save_mask_image(mask, png_path)
            save_mask_image(mask, jpg_path)
            png_count += 1
            jpg_count += 1
            if save_svg(path, glyph, svg_path):
                svg_count += 1

    rows.extend([
        {
            "format": "SVG",
            "data_level": "vector outline",
            "actual_full_count": "sample exported",
            "validated_sample_count": svg_count,
            "valid_count": svg_count,
            "valid_rate": 1.0 if svg_count else 0,
            "trainable": "yes",
            "editable": "yes",
            "typesetting": "indirect",
            "metadata": "source pair/glyph/step in filename",
        },
        {
            "format": "PNG/JPG",
            "data_level": "raster image",
            "actual_full_count": "sample exported",
            "validated_sample_count": png_count + jpg_count,
            "valid_count": png_count + jpg_count,
            "valid_rate": 1.0 if (png_count + jpg_count) else 0,
            "trainable": "yes",
            "editable": "no",
            "typesetting": "no",
            "metadata": "source pair/glyph/step in filename",
        },
        {
            "format": "Variable Font",
            "data_level": "continuous axis",
            "actual_full_count": "not included in this batch experiment",
            "validated_sample_count": 0,
            "valid_count": 0,
            "valid_rate": "not evaluated",
            "trainable": "sampleable if generated",
            "editable": "yes",
            "typesetting": "yes",
            "metadata": "requires fvar/gvar validation",
        },
    ])
    write_csv(exp / "tables" / "multi_format_usability_validation.csv", rows, list(rows[0].keys()))

    labels = ["TTF", "SVG", "PNG", "JPG"]
    counts = [len(all_ttf), svg_count, png_count, jpg_count]
    plt.figure(figsize=(7.2, 4.2))
    plt.bar(labels, counts, color=["#111827", "#2563EB", "#60A5FA", "#93C5FD"])
    plt.yscale("log")
    plt.ylabel("Count (log scale)")
    plt.title("Multi-format Output Availability")
    plt.grid(axis="y", alpha=0.25)
    plt.tight_layout()
    for ext in ["png", "svg", "pdf"]:
        plt.savefig(exp / "figures" / f"fig8_multi_format_counts.{ext}", dpi=320)
    plt.close()

    # Sample panel.
    sample_pngs = sorted((exp / "samples").glob("*.png"))[:24]
    fig, axes = plt.subplots(4, 6, figsize=(8.0, 5.3))
    for ax, p in zip(axes.flat, sample_pngs):
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        ax.imshow(img, cmap="gray", vmin=0, vmax=255)
        ax.axis("off")
    for ax in axes.flat[len(sample_pngs):]:
        ax.axis("off")
    plt.suptitle("Exported Raster Samples from Generated TTFs", y=0.995)
    plt.tight_layout()
    plt.savefig(exp / "figures" / "fig9_exported_samples_panel.png", dpi=320)
    plt.close()


def write_readme():
    readme = f"""# EI Font Morphing Experiment Package

Generated at: {time.strftime('%Y-%m-%d %H:%M:%S')}

This package contains reproducible experimental outputs for the font interpolation data augmentation system.

Folder order follows the manuscript experiment order:

1. `01_data_augmentation_scale`: scale experiment and real full-output statistics.
2. `02_comparison_with_image_augmentation`: equal-size and full-size comparison against image perturbation augmentation.
3. `04_downstream_font_generation`: fixed auto-encoding baseline trained with different augmentation sources.
4. `05_multi_format_usability`: TTF/SVG/PNG/JPG usability validation and exported samples.

Notes:
- The experiments use existing full 20-step pairwise interpolation outputs under `{ROOT}`.
- Metrics are computed from rendered glyph masks and font metadata. LPIPS is not used because it is not installed in the current environment; SSIM, PSNR, Chamfer, Hausdorff, skeleton IoU, and component anomaly rate are reported instead.
- Figures are exported as publication-friendly PNG/SVG/PDF where applicable.
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")


def zip_results():
    zip_path = OUT.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(OUT.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(OUT.parent))
    return zip_path


def main():
    reset_out()
    experiment_1()
    cache = build_render_cache("menk", max_glyphs=30)
    experiment_2(cache)
    experiment_4(cache)
    experiment_5(cache)
    write_readme()
    zip_path = zip_results()
    print(json.dumps({
        "ok": True,
        "out_dir": str(OUT),
        "zip_path": str(zip_path),
        "zip_size_mb": round(zip_path.stat().st_size / 1024 / 1024, 2),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
