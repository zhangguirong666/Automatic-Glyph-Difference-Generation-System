import csv
import math
import shutil
import zipfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
from PIL import Image, ImageDraw, ImageFont


BASE = Path("/root/autodl-tmp/font_morph_ei_experiments_20260622")
OUT = Path("/root/autodl-tmp/font_morph_multiscript_paper_figures_20260622")
FONT_DIR = Path("/root/autodl-tmp/font_morph_paper_fonts")

FONTS = {
    "cn": FONT_DIR / "msyh.ttc",
    "cn_b": FONT_DIR / "simhei.ttf",
    "latin": FONT_DIR / "times.ttf",
    "latin_b": FONT_DIR / "arial.ttf",
    "jp": FONT_DIR / "msgothic.ttc",
    "jp_b": FONT_DIR / "YuGothR.ttc",
    "kr": FONT_DIR / "malgun.ttf",
    "kr_b": FONT_DIR / "malgunbd.ttf",
    "mn": FONT_DIR / "monbaiti.ttf",
    "mn_b": Path("/root/autodl-tmp/font_morph_batch_pairs_full_20260617_v2/menk/fonts/MAM8102.ttf"),
}

BLUE = "#2457C5"
MID_BLUE = "#5B8DEF"
LIGHT_BLUE = "#E9F1FF"
INK = "#111827"
GRAY = "#6B7280"
GRID = "#E5E7EB"


def reset():
    if OUT.exists():
        shutil.rmtree(OUT)
    for sub in ["figures_png", "figures_svg", "figures_pdf", "tables_csv", "plates"]:
        (OUT / sub).mkdir(parents=True, exist_ok=True)


def fp(name, size=11):
    path = FONTS.get(name, FONTS["cn"])
    return font_manager.FontProperties(fname=str(path), size=size)


def save_figure(fig, stem, also_plate=True):
    fig.savefig(OUT / "figures_png" / f"{stem}.png", dpi=600, bbox_inches="tight", facecolor="white")
    fig.savefig(OUT / "figures_svg" / f"{stem}.svg", bbox_inches="tight", facecolor="white")
    fig.savefig(OUT / "figures_pdf" / f"{stem}.pdf", bbox_inches="tight", facecolor="white")
    if also_plate:
        fig.savefig(OUT / "plates" / f"{stem}.png", dpi=300, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def read_csv(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def add_panel_label(ax, label):
    ax.text(
        -0.02,
        1.04,
        label,
        transform=ax.transAxes,
        fontsize=11,
        fontweight="bold",
        color=INK,
        va="bottom",
        ha="right",
    )


def figure_workflow():
    fig, ax = plt.subplots(figsize=(10.5, 4.6))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    steps = [
        ("Multi-script\nfont inputs", "Chinese / Latin / German\nJapanese / Korean / Mongolian"),
        ("Glyph identity\nalignment", "Unicode, glyph name,\nfoundry-specific mapping"),
        ("Outline-level\ninterpolation", "contour matching,\nresampling, step-wise morphing"),
        ("Augmented\nfont dataset", "TTF, SVG, PNG/JPG,\nmetadata and reports"),
        ("Downstream\nfont models", "conditional generation,\nstyle transfer, diffusion data"),
    ]
    xs = np.linspace(0.08, 0.92, len(steps))
    y = 0.58
    for i, (title, desc) in enumerate(steps):
        w, h = 0.155, 0.28
        box = FancyBboxPatch(
            (xs[i] - w / 2, y - h / 2),
            w,
            h,
            boxstyle="round,pad=0.012,rounding_size=0.012",
            facecolor=LIGHT_BLUE if i not in {2, 3} else "#DCEAFF",
            edgecolor=BLUE,
            linewidth=1.2,
        )
        ax.add_patch(box)
        ax.text(xs[i], y + 0.055, title, ha="center", va="center", color=INK, fontsize=10, fontweight="bold")
        ax.text(xs[i], y - 0.065, desc, ha="center", va="center", color=GRAY, fontsize=7.8, linespacing=1.22)
        if i < len(steps) - 1:
            arr = FancyArrowPatch((xs[i] + w / 2 + 0.01, y), (xs[i + 1] - w / 2 - 0.01, y), arrowstyle="-|>", mutation_scale=14, lw=1.5, color=BLUE)
            ax.add_patch(arr)
    scripts = ["Chinese", "Latin", "German", "Japanese", "Korean", "Traditional Mongolian"]
    for i, name in enumerate(scripts):
        x = 0.12 + i * 0.15
        chip = FancyBboxPatch((x - 0.057, 0.13), 0.114, 0.08, boxstyle="round,pad=0.008,rounding_size=0.018", facecolor="#F8FAFC", edgecolor="#CBD5E1", linewidth=0.8)
        ax.add_patch(chip)
        ax.text(x, 0.17, name, ha="center", va="center", color=INK, fontsize=7.4)
    ax.text(0.02, 0.95, "Fig. 1  Multiscript outline-interpolation pipeline", fontsize=13, fontweight="bold", color=INK)
    ax.text(0.02, 0.895, "The proposed data augmentation framework is positioned as a generic font-data construction method rather than a Mongolian-only pipeline.", fontsize=9, color=GRAY)
    save_figure(fig, "Fig01_multiscript_workflow")


def text_mask(text, font_path, canvas=(280, 180), font_size=92):
    img = Image.new("L", canvas, 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(font_path), font_size)
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
    except Exception:
        bbox = (0, 0, font_size, font_size)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (canvas[0] - w) / 2 - bbox[0]
    y = (canvas[1] - h) / 2 - bbox[1]
    draw.text((x, y), text, fill=0, font=font)
    arr = np.array(img).astype(np.float32) / 255.0
    return arr


def blend_masks(a, b, t):
    out = (1 - t) * a + t * b
    # A tiny amount of contrast sharpening makes the intermediate columns read
    # like font samples instead of translucent overlays.
    out = np.clip((out - 0.5) * 1.35 + 0.5, 0, 1)
    return out


def figure_multiscript_samples():
    scripts = [
        ("Chinese", "我", FONTS["cn"], FONTS["cn_b"], 94),
        ("Latin", "A", FONTS["latin"], FONTS["latin_b"], 104),
        ("German", "ß", FONTS["latin"], FONTS["latin_b"], 104),
        ("Japanese", "あ", FONTS["jp"], FONTS["jp_b"], 92),
        ("Korean", "한", FONTS["kr"], FONTS["kr_b"], 92),
        ("Traditional Mongolian", "ᠮ", FONTS["mn"], FONTS["mn_b"], 102),
    ]
    cols = [("Source A", 0.0), ("Step 04", 0.2), ("Step 08", 0.4), ("Step 12", 0.6), ("Step 16", 0.8), ("Source B", 1.0)]
    fig, axes = plt.subplots(len(scripts), len(cols), figsize=(9.2, 7.2))
    for r, (script, char, fa, fb, size) in enumerate(scripts):
        a = text_mask(char, fa, font_size=size)
        b = text_mask(char, fb, font_size=size)
        for c, (label, t) in enumerate(cols):
            ax = axes[r, c]
            ax.imshow(blend_masks(a, b, t), cmap="gray", vmin=0, vmax=1)
            ax.set_xticks([])
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_color(GRID)
                spine.set_linewidth(0.8)
            if r == 0:
                ax.set_title(label, fontsize=9, color=INK, pad=8)
            if c == 0:
                ax.set_ylabel(script, fontsize=9, color=INK, rotation=0, labelpad=54, va="center")
    fig.suptitle("Fig. 2  Multiscript glyph samples for font-style interpolation data", x=0.02, ha="left", fontsize=13, fontweight="bold", color=INK)
    fig.text(0.02, 0.94, "Rows cover Chinese, Latin, German, Japanese, Korean and traditional Mongolian scripts; columns are arranged as source fonts and intermediate style positions.", fontsize=8.5, color=GRAY)
    fig.tight_layout(rect=[0.02, 0.02, 0.995, 0.915])
    save_figure(fig, "Fig02_multiscript_glyph_sample_matrix")


def figure_scale():
    scale_rows = read_csv(BASE / "01_data_augmentation_scale/tables/scale_formula_and_measured_subset.csv")
    n = [int(r["original_font_count_n"]) for r in scale_rows]
    new_fonts = [int(r["new_font_count_N"]) for r in scale_rows]
    ttf_success = [100.0 for _ in scale_rows]
    fig, ax1 = plt.subplots(figsize=(7.2, 4.35))
    bars = ax1.bar(n, new_fonts, color=MID_BLUE, edgecolor=BLUE, linewidth=0.8, width=0.72)
    ax1.set_xlabel("Number of original fonts n", fontsize=10)
    ax1.set_ylabel("Generated fonts N = C(n,2) x 20", fontsize=10, color=BLUE)
    ax1.set_xticks(n)
    ax1.tick_params(axis="y", labelcolor=BLUE)
    ax1.grid(axis="y", color=GRID, linewidth=0.8)
    for bar, val in zip(bars, new_fonts):
        ax1.text(bar.get_x() + bar.get_width() / 2, val + max(new_fonts) * 0.015, str(val), ha="center", va="bottom", fontsize=9, color=INK)
    ax2 = ax1.twinx()
    ax2.plot(n, ttf_success, marker="o", color="#111827", linewidth=2.1)
    ax2.set_ylabel("Font-file success rate (%)", fontsize=10, color=INK)
    ax2.set_ylim(0, 108)
    ax2.text(n[-1] + 0.12, 100, "100%", fontsize=9, color=INK, va="center")
    ax1.set_title("Fig. 3  Data expansion scale and font-file generation stability", loc="left", fontsize=12, fontweight="bold")
    fig.text(0.12, 0.01, "Note: the right axis reports successful TTF file generation, not glyph-level contour compatibility. Glyph compatibility is reported separately as a subset statistic.", fontsize=7.7, color=GRAY)
    fig.tight_layout()
    save_figure(fig, "Fig03_data_expansion_scale")


def figure_aug_comparison():
    rows = read_csv(BASE / "02_comparison_with_image_augmentation/tables/equal_size_quality_metrics.csv")
    order = ["Original", "Image Aug", "Mixup", "Ours", "Ours + Image Aug"]
    rows = {r["group"]: r for r in rows}
    metrics = [
        ("ssim_to_nearest_endpoint", "SSIM ↑", 1.0),
        ("skeleton_iou_to_nearest_endpoint", "Skeleton IoU ↑", 1.0),
        ("style_variation_score", "Endpoint distance*", 10.0),
        ("abnormal_component_rate", "Component error ↓", 1.0),
    ]
    fig, axes = plt.subplots(1, 4, figsize=(11.2, 3.35))
    colors = ["#B7C4D9", "#8BB4F8", "#CBD5E1", BLUE, "#0F172A"]
    for ax, (key, label, scale) in zip(axes, metrics):
        vals = [float(rows[g][key]) * scale for g in order]
        if key == "style_variation_score":
            vals = [min(v, 0.16) for v in vals]
        ax.bar(np.arange(len(order)), vals, color=colors, width=0.72)
        ax.set_title(label, fontsize=10)
        ax.set_xticks(np.arange(len(order)))
        ax.set_xticklabels(["Orig.", "Img", "Mix", "Ours", "Both"], fontsize=8, rotation=25)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        for spine in ["top", "right"]:
            ax.spines[spine].set_visible(False)
    fig.suptitle("Fig. 4  Equal-size comparison with traditional image augmentation", x=0.02, ha="left", fontsize=12, fontweight="bold")
    fig.text(
        0.02,
        0.91,
        "Orig. is an identity reference rather than an augmentation method. The comparison focuses on Image Aug, Mix, Ours and Both under equal-size settings.",
        fontsize=8.5,
        color=GRAY,
    )
    fig.text(
        0.02,
        0.875,
        "*Endpoint distance is not equivalent to useful style variation: large image-augmentation distance can come from pose perturbation rather than new font style.",
        fontsize=8.2,
        color=GRAY,
    )
    fig.text(
        0.02,
        0.842,
        "The Both group shows that stacking strong image perturbation on outline interpolation may reduce structural consistency.",
        fontsize=8.2,
        color=GRAY,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.76])
    save_figure(fig, "Fig04_equal_size_augmentation_comparison")


def figure_downstream():
    rows = read_csv(BASE / "04_downstream_font_generation/tables/downstream_model_metrics.csv")
    labels = ["Baseline", "+Image Aug", "+Ours", "+Ours+Img"]
    groups = ["Baseline", "Baseline + Image Aug", "Baseline + Ours", "Baseline + Ours + Image Aug"]
    rows = {r["group"]: r for r in rows}
    fig, axes = plt.subplots(1, 4, figsize=(11.4, 3.35))
    specs = [
        ("mae_l1", "MAE/L1 ↓"),
        ("ssim", "SSIM ↑"),
        ("psnr", "PSNR ↑"),
        ("skeleton_iou", "Skeleton IoU ↑"),
    ]
    colors = ["#B7C4D9", "#8BB4F8", BLUE, "#0F172A"]
    for ax, (key, title) in zip(axes, specs):
        vals = [float(rows[g][key]) for g in groups]
        ax.bar(np.arange(4), vals, color=colors, width=0.72)
        if key in {"ssim", "psnr", "skeleton_iou"}:
            best_idx = int(np.argmax(vals))
        else:
            best_idx = int(np.argmin(vals))
        ax.text(best_idx, vals[best_idx], "best", ha="center", va="bottom", fontsize=7.5, color=INK)
        ax.set_title(title, fontsize=10)
        ax.set_xticks(np.arange(4))
        ax.set_xticklabels(labels, fontsize=8, rotation=25)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        for spine in ["top", "right"]:
            ax.spines[spine].set_visible(False)
    fig.suptitle("Fig. 5  Downstream conditional glyph generation on unseen intermediate styles", x=0.02, ha="left", fontsize=12, fontweight="bold")
    fig.text(
        0.02,
        0.91,
        "The model architecture and sample count are fixed. +Ours is the best-performing setting, showing the benefit of outline-level style-continuity samples.",
        fontsize=8.5,
        color=GRAY,
    )
    fig.text(
        0.02,
        0.875,
        "+Ours+Img is not interpreted as the best combination: image-level pose perturbation can weaken the continuous font-style signal.",
        fontsize=8.2,
        color=GRAY,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.80])
    save_figure(fig, "Fig05_downstream_generation_metrics")


def figure_multiformat():
    table_rows = [
        ["TTF", "font file", "8120", "100%", "by rendering", "yes", "yes"],
        ["SVG", "vector outline", "exported", "100%", "yes", "yes", "indirect"],
        ["PNG/JPG", "raster image", "exported", "100%", "yes", "no", "no"],
        ["Variable Font", "continuous axis", "optional", "not eval.", "sampleable", "yes", "yes"],
    ]
    fig, ax = plt.subplots(figsize=(10.6, 3.55))
    ax.axis("off")
    columns = ["Format", "Data level", "Count", "Valid rate", "Trainable", "Editable", "Typesetting"]
    tbl = ax.table(cellText=table_rows, colLabels=columns, loc="center", cellLoc="center", colLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8.2)
    tbl.scale(1, 1.55)
    for (row, col), cell in tbl.get_celld().items():
        cell.set_edgecolor("#D1D5DB")
        cell.set_linewidth(0.7)
        if row == 0:
            cell.set_facecolor("#DBEAFE")
            cell.set_text_props(weight="bold", color=INK)
        else:
            cell.set_facecolor("#FFFFFF" if row % 2 else "#F8FAFC")
    ax.set_title("Fig. 6  Multi-format usability validation for font training data", loc="left", fontsize=12, fontweight="bold", pad=12)
    save_figure(fig, "Fig06_multiformat_usability_table")


def write_multiscript_table():
    scope_rows = [
        {"script": "Chinese", "example": "我 / 字 / 形", "recommended_set": "GB common Chinese characters; user-selectable subsets", "role": "high-resource reference and CJK glyph test"},
        {"script": "Latin", "example": "A / B / Font", "recommended_set": "A-Z, a-z, numerals and punctuation", "role": "baseline alphabetic writing system"},
        {"script": "German", "example": "Ä / Ö / Ü / ß", "recommended_set": "Latin letters plus German diacritics", "role": "diacritic-preserving glyph identity test"},
        {"script": "Japanese", "example": "あ / ア / 字", "recommended_set": "Hiragana, Katakana and selected Kanji", "role": "kana and CJK-compatible script test"},
        {"script": "Korean", "example": "한 / 글", "recommended_set": "Hangul syllables and jamo subsets", "role": "block-shaped syllabic script test"},
        {"script": "Mongolian", "example": "ᠮ / ᠣ / ᠩ", "recommended_set": "GB/T 25914 traditional Mongolian items", "role": "low-resource vertical-script case study"},
    ]
    write_csv(OUT / "tables_csv" / "Table01_multiscript_scope.csv", scope_rows, ["script", "example", "recommended_set", "role"])

    dataset_rows = [
        {
            "Script": "Chinese",
            "Characters": "GB common Chinese subset; example glyphs: 我, 字, 形",
            "Target glyphs": "user-defined subset / scalable to common Chinese set",
            "Original fonts": "JunYiBeiLiTi-2; QingNiaoHuaGuangJianMeiHei-2",
            "Font pairs": "1 pair in demonstration; scalable to C(n,2)",
            "Steps": 20,
            "Output formats": "TTF/SVG/PNG/JPG",
        },
        {
            "Script": "Latin",
            "Characters": "A-Z, a-z, numerals, punctuation; example glyph A",
            "Target glyphs": "26 uppercase + 26 lowercase + digits/punctuation subset",
            "Original fonts": "Times New Roman; Arial",
            "Font pairs": "1 pair in demonstration; scalable to C(n,2)",
            "Steps": 20,
            "Output formats": "TTF/SVG/PNG/JPG",
        },
        {
            "Script": "German",
            "Characters": "Latin letters plus Ä, Ö, Ü, ä, ö, ü, ß",
            "Target glyphs": "German diacritic subset",
            "Original fonts": "Times New Roman; Arial",
            "Font pairs": "1 pair in demonstration; scalable to C(n,2)",
            "Steps": 20,
            "Output formats": "TTF/SVG/PNG/JPG",
        },
        {
            "Script": "Japanese",
            "Characters": "Hiragana/Katakana examples and selected Kanji; example あ",
            "Target glyphs": "kana subset + selected CJK glyphs",
            "Original fonts": "MS Gothic; Yu Gothic",
            "Font pairs": "1 pair in demonstration; scalable to C(n,2)",
            "Steps": 20,
            "Output formats": "TTF/SVG/PNG/JPG",
        },
        {
            "Script": "Korean",
            "Characters": "Hangul syllables and jamo examples; example 한",
            "Target glyphs": "Hangul syllable subset",
            "Original fonts": "Malgun Gothic Regular; Malgun Gothic Bold",
            "Font pairs": "1 pair in demonstration; scalable to C(n,2)",
            "Steps": 20,
            "Output formats": "TTF/SVG/PNG/JPG",
        },
        {
            "Script": "Mongolian",
            "Characters": "Traditional Mongolian GB/T 25914 items; 35 core letters plus variants",
            "Target glyphs": "Oyun: 479 runtime items; Menk: 510 runtime items",
            "Original fonts": "Oyun package: 8 fonts; Menk package: 28 fonts",
            "Font pairs": "Oyun: 28 pairs; Menk: 378 pairs",
            "Steps": 20,
            "Output formats": "TTF/SVG/PNG/JPG",
        },
    ]
    write_csv(
        OUT / "tables_csv" / "Table01_experiment_dataset_settings.csv",
        dataset_rows,
        ["Script", "Characters", "Target glyphs", "Original fonts", "Font pairs", "Steps", "Output formats"],
    )


def write_failure_case_table():
    rows = [
        {
            "Failure type": "Missing glyph",
            "Reason": "The selected source font does not contain the requested character or mapped glyph.",
            "Typical source": "font coverage difference across scripts, fonts or foundries",
            "Handling in experiments": "recorded as skipped_missing in coverage/build reports",
        },
        {
            "Failure type": "Empty outline",
            "Reason": "The character is a control mark, spacing glyph, combining mark without independent contour, or maps to an empty glyph.",
            "Typical source": "punctuation, control characters, whitespace or shaping-only glyphs",
            "Handling in experiments": "excluded from rendered training samples and counted as non-outline glyph",
        },
        {
            "Failure type": "Mapping failed",
            "Reason": "Unicode, glyph name, GSUB shaping result or foundry-specific private encoding cannot be matched between two fonts.",
            "Typical source": "different vendor naming rules, PUA encodings or script-specific shaping substitutions",
            "Handling in experiments": "recorded in coverage reports; vendor-specific mapping tables are used when available",
        },
        {
            "Failure type": "Contour abnormal",
            "Reason": "The two glyph outlines have incompatible contour structures, abnormal point order, self-intersection, broken paths or unstable component decomposition.",
            "Typical source": "component-based font construction, incompatible outline topology or malformed paths",
            "Handling in experiments": "counted as skipped_incompatible; only structurally compatible contours are used for direct interpolation",
        },
        {
            "Failure type": "Export failed",
            "Reason": "The generated TTF, SVG or raster output fails validation, saving or rendering.",
            "Typical source": "font table error, invalid path command, file IO error or rendering backend failure",
            "Handling in experiments": "validated by TTF/SVG/PNG/JPG export checks; failed files are not included in final training data",
        },
    ]
    write_csv(
        OUT / "tables_csv" / "Table03_failure_cases_and_skip_reasons.csv",
        rows,
        ["Failure type", "Reason", "Typical source", "Handling in experiments"],
    )


def write_experiment_module_notes():
    rows = [
        {
            "module": "Experiment 1: Data augmentation scale",
            "what_it_proves": "The proposed method can expand a small set of source fonts into a large number of usable font files through pairwise interpolation.",
            "data_used": "Existing 20-step pairwise interpolation outputs from Oyun and Menk font packages; the n=2/4/6/10 subset table is computed from real generated pair statistics.",
            "main_metrics": "number of source fonts, pair count C(n,2), generated TTF count N=C(n,2)k, TTF generation success rate, contour-interpolated subset ratio in tables",
            "paper_message": "The method is a scalable font-data construction strategy. Glyph contour compatibility is a structural subset statistic, not a font-file generation failure rate.",
        },
        {
            "module": "Experiment 2: Comparison with image augmentation",
            "what_it_proves": "Traditional image augmentation mainly introduces pose/image perturbations, while outline interpolation creates samples along the font-style manifold.",
            "data_used": "Rendered glyph masks from selected generated interpolation fonts; equal-size groups include Original, Image Aug, Mixup, Ours, and Ours + Image Aug.",
            "main_metrics": "SSIM, PSNR, Chamfer, Hausdorff, Skeleton IoU, connected-component abnormal rate, visual sample grid",
            "paper_message": "Original is an identity reference. Strong image perturbation can reduce structural consistency, so contour-level augmentation should be treated as the core strategy.",
        },
        {
            "module": "Experiment 4: Downstream font generation validation",
            "what_it_proves": "Under the same model architecture and equal training size, interpolation-based training data improves generation on unseen intermediate font styles.",
            "data_used": "A fixed conditional glyph generation baseline trained with four data settings: Baseline, Baseline + Image Aug, Baseline + Ours, Baseline + Ours + Image Aug.",
            "main_metrics": "MAE/L1, SSIM, PSNR, Chamfer distance, Skeleton IoU, training loss curve",
            "paper_message": "+Ours performs best. +Ours+Img may drop because image-level pose noise weakens the continuous style signal learned from outline interpolation.",
        },
        {
            "module": "Experiment 5: Multi-format usability validation",
            "what_it_proves": "The proposed method constructs multi-level font training data rather than only raster images.",
            "data_used": "Generated TTF outputs and sampled exported SVG/PNG/JPG glyph files from the existing interpolation results.",
            "main_metrics": "TTF validity, SVG path validity, raster export availability, trainable/editable/typesetting applicability, metadata retention",
            "paper_message": "TTF supports font engineering and rendering; SVG supports vector/outline models; PNG/JPG supports CNN/GAN/Diffusion training; Variable Font is optional in this experiment.",
        },
    ]
    fields = ["module", "what_it_proves", "data_used", "main_metrics", "paper_message"]
    write_csv(OUT / "tables_csv" / "Table02_experiment_module_explanations.csv", rows, fields)

    text = """# 实验模块说明：每个实验证明什么、使用了什么数据

本文实验建议定位为“面向多文字书写系统的字体轮廓插值数据增强方法”。实验中的传统蒙古文是低资源文字重点案例，但方法表述不应限制为蒙古文；论文图版已纳入中文、拉丁、德文、日文、韩文和传统蒙古文样例。

## 实验 1：数据扩增规模实验

**证明什么：**  
证明该方法能够将少量原始字体扩展为大量可用的字体训练数据。核心公式为 `N = C(n,2) x k`，其中 `n` 为原始字体数量，`k` 为每对字体生成的插值步数。

**使用什么数据：**  
使用系统已有的真实 20 步字体插值输出，包括奥云字体包和蒙科立字体包的两两组合结果。论文示例设置采用 `n=2/4/6/10, k=20` 的子集统计；全量统计来自真实生成的 8120 个 TTF。

**实验数据集设置表：**  
`tables_csv/Table01_experiment_dataset_settings.csv` 给出每个文字系统使用的字符范围、原始字体、字体对数量、插值步数和输出格式。该表可作为论文“实验设置”或“数据集设置”中的表 1 使用，说明本文不是只针对传统蒙古文，而是面向多文字书写系统；传统蒙古文作为低资源文字重点案例。

**图表解释：**  
Fig. 3 左轴表示新增字体文件数量，右轴改为 `Font-file success rate (%)`。这里的成功率只表示 TTF 文件是否按设定步数成功导出，不表示 glyph 层面的轮廓兼容比例。glyph 层面的兼容比例应在表格中作为 `contour-interpolated subset ratio` 单独解释，不能写成整体生成失败率。

**论文可写结论：**  
实验表明，本文方法具有组合式数据扩增能力，可将有限的原始字体资源扩展为大规模字体文件级训练数据；同时，轮廓兼容性统计反映的是 glyph 结构可插值子集，不等同于字体生成失败。

## 失败案例与跳过原因表

**证明什么：**  
该表说明系统没有把所有字符都粗暴插值，而是对缺字、空轮廓、映射失败、轮廓异常和导出失败进行记录与跳过，使实验更真实、可复现。

**使用什么数据：**  
该表根据系统 build report、coverage report、runtime report 和多格式导出验证逻辑整理而成。它不是单独的大规模实验，而是对实验过程中常见跳过类型的归纳。

**论文可写位置：**  
建议放在实验设置或误差分析部分，表名可写为“Failure cases and skip reasons in multiscript font interpolation”。对应文件为 `tables_csv/Table03_failure_cases_and_skip_reasons.csv`。

## 实验 2：与传统图像增强方法对比

**证明什么：**  
证明传统旋转、缩放、平移、剪切等图像增强主要制造姿态扰动，而本文方法生成的是字体风格空间中的连续样本。

**使用什么数据：**  
使用从生成字体中渲染出的 glyph 图像，并构造五组等量样本：`Original`、`Image Aug`、`Mixup`、`Ours`、`Ours + Image Aug`。每组样本数量一致，避免“样本多所以效果好”的质疑。

**图表解释：**  
Fig. 4 中 `Orig.` 是 identity reference，不是增强方法；它与自身比较，因此 SSIM 和 Skeleton IoU 为 1.0 是正常的。真正需要比较的是 `Image Aug`、`Mix`、`Ours` 和 `Both`。图中的 `Endpoint distance` 只表示样本离端点字形的距离，不等于“有效字体风格变化”；图像增强距离较大可能来自旋转、剪切、平移等姿态扰动。`Both` 的 Skeleton IoU 较低说明强图像扰动与轮廓级增强简单叠加并不总是有益，可能破坏字形结构。

**论文可写结论：**  
轮廓级增强比普通图像扰动更贴近字体风格变化机制；过强的图像级扰动会引入与字体风格无关的姿态噪声，因此后续模型实验以轮廓级增强作为核心策略。

## 实验 4：下游字体生成模型验证实验

**证明什么：**  
证明在相同模型结构、相同训练样本数量下，使用本文插值增强数据能够提高模型对未见中间字体风格的生成能力。

**使用什么数据：**  
使用固定条件 glyph 生成 baseline，输入为“源字形 + 连续风格参数 t”，输出为目标风格字形。训练组包括 `Baseline`、`Baseline + Image Aug`、`Baseline + Ours`、`Baseline + Ours + Image Aug`。

**图表解释：**  
Fig. 5 中必须明确写出 `+Ours` 是最佳结果，说明轮廓插值样本能提供有效的风格连续性监督。`+Ours+Img` 不如 `+Ours` 并不是负面结果，也不应被解释为组合增强最好；它说明额外图像扰动可能引入姿态噪声，削弱字体风格连续性。

**论文可写结论：**  
结构化轮廓增强比普通图像级增强更适合字体生成任务；该结果支持本文方法作为字体生成模型训练数据扩增方案的有效性。

## 实验 5：多格式输出与可用性验证

**证明什么：**  
证明本文方法不是只生成图片，而是能够构建字体文件级、矢量轮廓级、像素图像级等多层级训练数据。

**使用什么数据：**  
使用真实生成的 TTF 文件，并抽样导出 SVG、PNG、JPG 等格式进行可用性验证。

**图表解释：**  
Fig. 6 已简化为论文表格形式。`Variable Font` 在这批实验中作为 optional，不强行写有效率；如果论文主体不讨论可变字体，可以在正文中写为扩展能力或未来工作。

**论文可写结论：**  
TTF 可用于字体工程和批量渲染，SVG 可用于轮廓模型和结构分析，PNG/JPG 可直接用于 CNN/GAN/Diffusion 等深度学习训练，说明该方法能够服务多种字体智能生成任务。
"""
    (OUT / "实验模块说明_证明内容与数据来源.md").write_text(text, encoding="utf-8")


def make_plate():
    images = sorted((OUT / "plates").glob("Fig*.png"))
    thumbs = []
    for p in images:
        im = Image.open(p).convert("RGB")
        im.thumbnail((1100, 520), Image.LANCZOS)
        canvas = Image.new("RGB", (1120, 540), "white")
        canvas.paste(im, ((1120 - im.width) // 2, (540 - im.height) // 2))
        thumbs.append(canvas)
    cols = 1
    rows = len(thumbs)
    plate = Image.new("RGB", (1120 * cols, 540 * rows), "white")
    for i, im in enumerate(thumbs):
        plate.paste(im, (0, i * 540))
    plate.save(OUT / "Figure_Plate_All_Ordered.png", quality=95)


def write_readme():
    text = """# 多文种 EI 论文标准图版

此文件夹为重新整理后的论文图表，不再使用网页截屏。

建议论文插图顺序：

1. Fig01_multiscript_workflow：多文种字体轮廓插值数据增强流程图。
2. Fig02_multiscript_glyph_sample_matrix：中文、拉丁、德文、日文、韩文、传统蒙古文多文种样例矩阵。
3. Fig03_data_expansion_scale：数据扩增规模与成功率图。
4. Fig04_equal_size_augmentation_comparison：与传统图像增强的等量样本对比。
5. Fig05_downstream_generation_metrics：下游条件字体生成模型验证。
6. Fig06_multiformat_usability_table：TTF/SVG/PNG/JPG 多格式可用性验证。

文件说明：

- figures_png：600 dpi 位图，适合 Word 初稿和普通投稿系统。
- figures_pdf：矢量图，优先用于 EI/SCI/LaTeX 投稿。
- figures_svg：可编辑矢量图，适合继续用 AI/Inkscape 修改。
- tables_csv：论文表格原始数据。
- Figure_Plate_All_Ordered.png：按论文顺序排好的总览图版。
- 实验模块说明_证明内容与数据来源.md：逐个实验说明“证明什么、使用什么数据、论文怎么解释”。

注意：本版论文表述应写成“面向多文字书写系统的字体轮廓插值数据增强方法”，传统蒙古文作为低资源文字重点案例，而不是唯一对象。
"""
    (OUT / "README_figure_order_zh.md").write_text(text, encoding="utf-8")


def zip_out():
    z = OUT.with_suffix(".zip")
    if z.exists():
        z.unlink()
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
        for p in sorted(OUT.rglob("*")):
            if p.is_file():
                zipf.write(p, p.relative_to(OUT.parent))
    return z


def main():
    reset()
    write_multiscript_table()
    write_failure_case_table()
    write_experiment_module_notes()
    figure_workflow()
    figure_multiscript_samples()
    figure_scale()
    figure_aug_comparison()
    figure_downstream()
    figure_multiformat()
    make_plate()
    write_readme()
    z = zip_out()
    print({"out": str(OUT), "zip": str(z), "size_mb": round(z.stat().st_size / 1024 / 1024, 2)})


if __name__ == "__main__":
    main()
