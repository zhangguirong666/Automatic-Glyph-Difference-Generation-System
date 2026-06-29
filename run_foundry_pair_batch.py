import argparse
import csv
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def safe_stem(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"\s+\(\d+\)$", "", stem)
    stem = re.sub(r"[^0-9A-Za-z_.-]+", "_", stem).strip("._-")
    return stem or "font"


def unzip_fonts(zip_path: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    fonts = sorted({p.resolve() for p in dest.rglob("*") if p.suffix.lower() in {".ttf", ".otf"}})
    return fonts


def parse_report(output_dir: Path) -> dict:
    report = {}
    build_report = output_dir / "build_report.csv"
    if build_report.exists():
        try:
            with build_report.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            if rows:
                report.update(rows[0])
        except Exception as exc:
            report["build_report_error"] = str(exc)
    for name in ["gb_morph_report.json", "gb_morph_complete_report.json"]:
        p = output_dir / name
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                for key in ["algorithm", "runtime_rows", "handled_runtime_rows", "interpolated_unique_glyph_pairs", "prepared_count", "skipped_count"]:
                    if key in data:
                        report[key] = data[key]
            except Exception as exc:
                report[name + "_error"] = str(exc)
    return report


def zip_dir(src: Path, dest_zip: Path):
    if dest_zip.exists():
        dest_zip.unlink()
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(src))


def ensure_link_or_copy(src: Path, dst: Path):
    if dst.exists() or dst.is_symlink():
        return
    try:
        os.symlink(src, dst, target_is_directory=src.is_dir())
    except Exception:
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def make_worker_project(project: Path, work: Path, company: str, worker_id: int) -> Path:
    worker = work / company / "workers" / f"worker_{worker_id:02d}"
    if worker.exists():
        shutil.rmtree(worker)
    (worker / "scripts").mkdir(parents=True, exist_ok=True)
    (worker / "input").mkdir(parents=True, exist_ok=True)
    (worker / "output").mkdir(parents=True, exist_ok=True)
    ensure_link_or_copy(project / "data", worker / "data")
    if (project / "input" / "fonts").exists():
        ensure_link_or_copy(project / "input" / "fonts", worker / "input" / "fonts")
    shutil.copy2(project / "scripts" / "build_menk_gb_version.py", worker / "scripts" / "build_menk_gb_version.py")
    shutil.copy2(project / "scripts" / "build_oyun_gb_version.py", worker / "scripts" / "build_oyun_gb_version.py")
    return worker


def run_pair(project: Path, company: str, script: Path, pair_dir: Path, out_dir_name: str, font_a: Path, font_b: Path, steps: int, final_root: Path, worker_id: int = 0, isolated_project: Path | None = None) -> dict:
    pair_name = f"{safe_stem(font_a)}__TO__{safe_stem(font_b)}"
    pair_font_dir = pair_dir / pair_name
    if pair_font_dir.exists():
        shutil.rmtree(pair_font_dir)
    pair_font_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(font_a, pair_font_dir / font_a.name)
    shutil.copy2(font_b, pair_font_dir / font_b.name)

    run_project = isolated_project or project
    run_script = run_project / "scripts" / script.name if isolated_project else script
    output_dir = run_project / "output" / out_dir_name
    output_zip = run_project / "output" / f"{out_dir_name}.zip"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    if output_zip.exists():
        output_zip.unlink()

    env = os.environ.copy()
    env["MGB_STEPS"] = str(steps)
    if company == "menk":
        env["MENK_FONT_DIR"] = str(pair_font_dir)
    else:
        env["OYUN_FONT_DIR"] = str(pair_font_dir)

    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, str(run_script)],
        cwd=str(run_project),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    elapsed = round(time.time() - t0, 2)
    pair_out = final_root / pair_name
    if pair_out.exists():
        shutil.rmtree(pair_out)
    pair_out.mkdir(parents=True, exist_ok=True)

    copied_ttf = 0
    report = {}
    if output_dir.exists():
        for p in sorted(output_dir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(output_dir)
                target = pair_out / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
                if p.suffix.lower() == ".ttf":
                    copied_ttf += 1
        report = parse_report(output_dir)
    (pair_out / "run_stdout.txt").write_text(proc.stdout, encoding="utf-8", errors="ignore")
    (pair_out / "run_stderr.txt").write_text(proc.stderr, encoding="utf-8", errors="ignore")
    meta = {
        "company": company,
        "font_a": font_a.name,
        "font_b": font_b.name,
        "pair": pair_name,
        "steps_requested": steps,
        "ttf_count": copied_ttf,
        "ok": proc.returncode == 0 and copied_ttf == steps,
        "returncode": proc.returncode,
        "elapsed_seconds": elapsed,
        "worker_id": worker_id,
        "report": report,
    }
    (pair_out / "pair_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(pair_font_dir, ignore_errors=True)
    return meta


def write_summary(summary: dict, summary_path: Path, csv_path: Path):
    summary["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["company", "pair", "font_a", "font_b", "ok", "returncode", "ttf_count", "elapsed_seconds", "worker_id"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in summary["items"]:
            w.writerow({k: row.get(k) for k in fields})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--company", choices=["menk", "oyun"], required=True)
    ap.add_argument("--zip", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    project = Path(args.project).resolve()
    work = Path(args.work).resolve()
    company = args.company
    fonts_dir = work / company / "fonts"
    pair_tmp = work / company / "pair_fonts"
    final_root = work / company / "pairs"
    final_root.mkdir(parents=True, exist_ok=True)
    fonts = unzip_fonts(Path(args.zip).resolve(), fonts_dir)
    if len(fonts) < 2:
        raise SystemExit(f"not enough fonts for {company}: {len(fonts)}")

    if company == "menk":
        script = project / "scripts" / "build_menk_gb_version.py"
        out_dir_name = "menk_gb_ttf_steps"
    else:
        script = project / "scripts" / "build_oyun_gb_version.py"
        out_dir_name = "oyun_gb_ttf_steps"

    pairs = list(itertools.combinations(fonts, 2))
    if args.limit:
        pairs = pairs[: args.limit]

    summary = {
        "company": company,
        "font_count": len(fonts),
        "pair_count": len(pairs),
        "steps": args.steps,
        "workers": max(1, int(args.workers or 1)),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": [],
    }
    summary_path = work / company / "summary.json"
    csv_path = work / company / "summary.csv"

    workers = max(1, int(args.workers or 1))
    if workers == 1:
        for index, (font_a, font_b) in enumerate(pairs, 1):
            print(f"[{company}] {index}/{len(pairs)} {font_a.name} -> {font_b.name}", flush=True)
            meta = run_pair(project, company, script, pair_tmp, out_dir_name, font_a, font_b, args.steps, final_root)
            summary["items"].append(meta)
            summary["finished_pairs"] = index
            write_summary(summary, summary_path, csv_path)
    else:
        worker_projects = [make_worker_project(project, work, company, i) for i in range(workers)]
        worker_tasks = [[] for _ in range(workers)]
        for index, pair in enumerate(pairs, 1):
            worker_tasks[(index - 1) % workers].append((index, pair[0], pair[1]))

        def worker_loop(worker_id: int, tasks: list[tuple[int, Path, Path]]) -> list[dict]:
            out = []
            worker_pair_tmp = work / company / "worker_pair_fonts" / f"worker_{worker_id:02d}"
            for index, font_a, font_b in tasks:
                try:
                    meta = run_pair(
                        project,
                        company,
                        script,
                        worker_pair_tmp,
                        out_dir_name,
                        font_a,
                        font_b,
                        args.steps,
                        final_root,
                        worker_id,
                        worker_projects[worker_id],
                    )
                except Exception as exc:
                    meta = {
                        "company": company,
                        "pair": f"{safe_stem(font_a)}__TO__{safe_stem(font_b)}",
                        "font_a": font_a.name,
                        "font_b": font_b.name,
                        "ok": False,
                        "returncode": -999,
                        "ttf_count": 0,
                        "elapsed_seconds": 0,
                        "worker_id": worker_id,
                        "error": str(exc),
                    }
                meta["pair_index"] = index
                out.append(meta)
            return out

        futures = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for worker_id, tasks in enumerate(worker_tasks):
                future = ex.submit(worker_loop, worker_id, tasks)
                futures[future] = worker_id
            completed = 0
            for future in as_completed(futures):
                worker_id = futures[future]
                try:
                    worker_results = future.result()
                except Exception as exc:
                    worker_results = [{
                        "company": company,
                        "pair": f"worker_{worker_id:02d}_failed",
                        "font_a": "",
                        "font_b": "",
                        "ok": False,
                        "returncode": -999,
                        "ttf_count": 0,
                        "elapsed_seconds": 0,
                        "worker_id": worker_id,
                        "error": str(exc),
                    }]
                for meta in worker_results:
                    completed += 1
                    summary["items"].append(meta)
                    print(f"[{company}] done {completed}/{len(pairs)} ok={meta.get('ok')} {meta.get('pair')} ttf={meta.get('ttf_count')} sec={meta.get('elapsed_seconds')} worker={meta.get('worker_id')}", flush=True)
                summary["finished_pairs"] = completed
                write_summary(summary, summary_path, csv_path)

    zip_path = work / f"{company}_all_pair_20step_ttf.zip"
    zip_dir(work / company, zip_path)
    summary["zip_path"] = str(zip_path)
    summary["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[DONE]", zip_path)


if __name__ == "__main__":
    main()
