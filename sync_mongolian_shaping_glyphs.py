#!/usr/bin/env python3
"""
Synchronize interpolated outlines into the glyphs actually used by Mongolian
OpenType shaping.

The GB/complete generators may add extra preview glyphs, but normal Unicode
input is rendered through GSUB/GPOS glyph names such as init/medi/fina forms.
This module asks HarfBuzz which glyphs each foundry font uses for real
Mongolian text, pairs font A and font B output glyphs for the same shaped
sequence, then rewrites those glyphs in every generated step TTF.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Iterable

import uharfbuzz as hb
from fontTools.ttLib import TTFont

from gb_morph_algorithm import (
    contours_to_glyf,
    glyph_to_sampled_contours,
    interpolate_contours,
    match_contours,
)

MONGOLIAN_CORE = list(range(0x1820, 0x1843))
CONTEXT_ANCHOR = 0x1820
FVS = [0x180B, 0x180C, 0x180D]
COMMON_WORDS = [
    "ᠮᠣᠩᠭᠣᠯ",
    "ᠰᠠᠶᠢᠨ",
    "ᠪᠠᠶᠠᠷ",
    "ᠦᠰᠦᠭ",
]


def _glyph_order(font: TTFont) -> list[str]:
    return list(font.getGlyphOrder())


def _shape(font_path: Path, text: str, direction: str) -> list[str]:
    data = font_path.read_bytes()
    face = hb.Face(data)
    hb_font = hb.Font(face)
    order = _glyph_order(TTFont(str(font_path), lazy=True))

    buf = hb.Buffer()
    buf.add_str(text)
    buf.script = "Mong"
    buf.direction = direction
    buf.language = "mn"
    hb.shape(hb_font, buf, {})

    names: list[str] = []
    for info in buf.glyph_infos:
        gid = info.codepoint
        if 0 <= gid < len(order):
            names.append(order[gid])
    return names


def _parse_codepoint_sequence(value: str) -> str:
    chars: list[str] = []
    for token in (value or "").replace(",", " ").split():
        token = token.strip()
        if not token:
            continue
        if token.startswith("U+"):
            try:
                chars.append(chr(int(token[2:], 16)))
            except ValueError:
                pass
    return "".join(chars)


def _runtime_sequences(paths: Iterable[Path]) -> set[str]:
    sequences: set[str] = set()
    for path in paths:
        if not path or not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    text = _parse_codepoint_sequence(row.get("text_codepoints", ""))
                    if not text:
                        text = _parse_codepoint_sequence(row.get("base_unicode", ""))
                    if text:
                        sequences.add(text)
        except Exception:
            continue
    return sequences


def _default_sequences(runtime_csvs: Iterable[Path]) -> list[str]:
    sequences: set[str] = set(COMMON_WORDS)

    for cp in MONGOLIAN_CORE:
        ch = chr(cp)
        anchor = chr(CONTEXT_ANCHOR)
        sequences.add(ch)
        sequences.add(ch + anchor)
        sequences.add(anchor + ch)
        sequences.add(anchor + ch + anchor)
        for fvs in FVS:
            sequences.add(ch + chr(fvs))
            sequences.add(ch + chr(fvs) + anchor)
            sequences.add(anchor + ch + chr(fvs))

    sequences.update(_runtime_sequences(runtime_csvs))
    return sorted(sequences, key=lambda s: (len(s), s))


def _pairs_from_shaping(font_a_path: Path, font_b_path: Path, runtime_csvs: Iterable[Path]) -> dict[str, str]:
    order_a = set(_glyph_order(TTFont(str(font_a_path), lazy=True)))
    order_b = set(_glyph_order(TTFont(str(font_b_path), lazy=True)))
    pairs: dict[str, str] = {}

    for text in _default_sequences(runtime_csvs):
        for direction in ("ltr", "ttb"):
            try:
                shaped_a = _shape(font_a_path, text, direction)
                shaped_b = _shape(font_b_path, text, direction)
            except Exception:
                continue
            if len(shaped_a) != len(shaped_b):
                continue
            for glyph_a, glyph_b in zip(shaped_a, shaped_b):
                if glyph_a in {".notdef", ".null", "nonmarkingreturn"}:
                    continue
                if glyph_a in order_a and glyph_b in order_b:
                    pairs.setdefault(glyph_a, glyph_b)
    return pairs


def _pairs_from_runtime_csvs(runtime_csvs: Iterable[Path]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for path in runtime_csvs:
        if not path or not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    names_a = [
                        x.strip()
                        for x in (row.get("fontA_target_glyph_names") or row.get("source_glyph_a") or "").split("|")
                        if x.strip()
                    ]
                    names_b = [
                        x.strip()
                        for x in (row.get("fontB_target_glyph_names") or row.get("source_glyph_b") or "").split("|")
                        if x.strip()
                    ]
                    if len(names_a) == 1 and len(names_b) == 1:
                        pairs.setdefault(names_a[0], names_b[0])
        except Exception:
            continue
    return pairs


def _interp_metric(font_a: TTFont, font_b: TTFont, glyph_a: str, glyph_b: str, t: float):
    if "hmtx" not in font_a or "hmtx" not in font_b:
        return None
    ma = font_a["hmtx"].metrics
    mb = font_b["hmtx"].metrics
    if glyph_a not in ma or glyph_b not in mb:
        return None
    aw_a, lsb_a = ma[glyph_a]
    aw_b, lsb_b = mb[glyph_b]
    return (round(aw_a + (aw_b - aw_a) * t), round(lsb_a + (lsb_b - lsb_a) * t))


def sync_mongolian_shaping_glyphs(
    font_a_path: str | Path,
    font_b_path: str | Path,
    step_dir: str | Path,
    pattern: str,
    steps: int,
    points_per_contour: int = 120,
    runtime_csvs: Iterable[str | Path] = (),
    report_path: str | Path | None = None,
) -> dict:
    font_a_path = Path(font_a_path)
    font_b_path = Path(font_b_path)
    step_dir = Path(step_dir)
    runtime_paths = [Path(p) for p in runtime_csvs if p]

    font_a = TTFont(str(font_a_path), recalcBBoxes=True, recalcTimestamp=False)
    font_b = TTFont(str(font_b_path), recalcBBoxes=True, recalcTimestamp=False)

    pair_map: dict[str, str] = {}
    if os.environ.get("MONGOLIAN_SHAPING_SYNC_INCLUDE_RUNTIME_DIRECT") == "1":
        pair_map.update(_pairs_from_runtime_csvs(runtime_paths))
    pair_map.update(_pairs_from_shaping(font_a_path, font_b_path, runtime_paths))

    prepared = []
    skipped = []
    for glyph_a, glyph_b in sorted(pair_map.items()):
        if glyph_a not in font_a.getGlyphOrder() or glyph_b not in font_b.getGlyphOrder():
            skipped.append({"glyph_a": glyph_a, "glyph_b": glyph_b, "reason": "missing glyph"})
            continue
        try:
            contours_a = glyph_to_sampled_contours(font_a, glyph_a, points_per_contour)
            contours_b = glyph_to_sampled_contours(font_b, glyph_b, points_per_contour)
            contours_a, contours_b = match_contours(
                contours_a,
                contours_b,
                glyph_name_a=glyph_a,
                glyph_name_b=glyph_b,
                script_hint="mongolian",
            )
            if not contours_a or not contours_b:
                raise ValueError("empty sampled contours")
            prepared.append(
                {
                    "glyph_a": glyph_a,
                    "glyph_b": glyph_b,
                    "contours_a": contours_a,
                    "contours_b": contours_b,
                }
            )
        except Exception as exc:
            skipped.append({"glyph_a": glyph_a, "glyph_b": glyph_b, "reason": str(exc)})

    updated_files = []
    for step in range(1, int(steps) + 1):
        t = step / (int(steps) + 1)
        path = step_dir / pattern.format(step=step)
        if not path.exists():
            continue
        out_font = TTFont(str(path), recalcBBoxes=True, recalcTimestamp=False)
        if "glyf" not in out_font:
            continue
        glyf = out_font["glyf"]
        for item in prepared:
            glyph_a = item["glyph_a"]
            glyph_b = item["glyph_b"]
            if glyph_a not in glyf:
                continue
            contours = interpolate_contours(item["contours_a"], item["contours_b"], t)
            glyf[glyph_a] = contours_to_glyf(contours, glyf)
            metric = _interp_metric(font_a, font_b, glyph_a, glyph_b, t)
            if metric and "hmtx" in out_font:
                out_font["hmtx"].metrics[glyph_a] = metric
        if "maxp" in out_font:
            out_font["maxp"].numGlyphs = len(out_font.getGlyphOrder())
        out_font.save(str(path))
        updated_files.append(path.name)

    report = {
        "algorithm": "mongolian_shaping_glyph_sync_v1",
        "font_a": str(font_a_path),
        "font_b": str(font_b_path),
        "step_dir": str(step_dir),
        "pattern": pattern,
        "steps": int(steps),
        "points_per_contour": points_per_contour,
        "candidate_pairs": len(pair_map),
        "synced_pairs": len(prepared),
        "skipped_pairs": len(skipped),
        "updated_files": updated_files,
        "prepared": [{"glyph_a": x["glyph_a"], "glyph_b": x["glyph_b"]} for x in prepared],
        "skipped": skipped[:1000],
    }
    if report_path:
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "[MONGOLIAN-SHAPING-SYNC] "
        f"candidate_pairs={len(pair_map)}, synced_pairs={len(prepared)}, "
        f"skipped_pairs={len(skipped)}, updated_files={len(updated_files)}"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font-a", required=True)
    parser.add_argument("--font-b", required=True)
    parser.add_argument("--step-dir", required=True)
    parser.add_argument("--pattern", required=True)
    parser.add_argument("--steps", type=int, required=True)
    parser.add_argument("--points", type=int, default=120)
    parser.add_argument("--runtime-csv", action="append", default=[])
    parser.add_argument("--report")
    args = parser.parse_args()
    sync_mongolian_shaping_glyphs(
        args.font_a,
        args.font_b,
        args.step_dir,
        args.pattern,
        args.steps,
        points_per_contour=args.points,
        runtime_csvs=args.runtime_csv,
        report_path=args.report,
    )


if __name__ == "__main__":
    main()
