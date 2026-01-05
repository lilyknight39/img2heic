# img2heic (macOS) — HEIC Archive Script for Photo Cleanup

Batch-convert **PNG/JPEG → HEIC** on **macOS** using Apple’s built-in `sips`, producing an **archive-first** output folder that typically saves significant disk space.
The script is designed for **photo library cleanup**: generate a safe archive copy, review it, then delete/move originals only after you’re satisfied.

<p align="center">
  <img src="docs/images/workflow.png" alt="Recommended workflow" width="900">
</p>

---

## Highlights

- Recursive directory processing (keeps original folder structure)
- Converts **PNG/JPEG → HEIC** via **`sips`**
- Copies **non-image files** as-is (sidecars, JSON, notes, etc.)
- Tolerates **wrong file extensions** (detects by magic bytes)
- **Safety fallbacks**
  - If HEIC is larger → keep original
  - Optional strict alpha handling for transparent assets
- **Resume support**: restart safely after interruption
- Multi-process acceleration (`--threads`)
- Optional **SSIM verification** (`--verify-ssim`) with auto quality escalation
- Generates a **conversion report** (`conversion_report.html`)
- Smart defaults for external drives: small CPU concurrency, large prefetch batches, optional skip non-images

---

## Screenshot

### Running the script

![Terminal run](docs/images/terminal-run.png)

### Example report (screenshot)

![Report example](docs/images/report-example.png)

You can also include a lightweight example report in your repository:

- `examples/conversion_report_example.html`

---

## Recommended workflow (Archive-first)

1. **Export / collect** photos into a folder (e.g., export albums from Photos.app or copy from external drives).
2. **Run `img2heic`** to produce an HEIC archive copy into a separate output directory.
3. **Review** the archive:
   - Open `conversion_report.html`
   - Spot-check a few albums (especially **transparent PNG** assets).
4. **Reclaim space**: delete/move originals only after confirmation.

> The script **never deletes source files** automatically.

---

## Requirements

- macOS (uses `/usr/bin/sips`)
- Python 3.9+ (recommended: 3.10/3.11)

Optional (only for `--verify-ssim`):
- `pillow-heif`
- `scikit-image`

```bash
pip install pillow-heif scikit-image
```

---

## Quick start

### 1) Minimal interactive (recommended)

```bash
python3 img2heic_sips_tuned_final.py
# Prompts for source; output defaults to <SRC>_out
```

### 2) Explicitly set input/output

```bash
python3 img2heic_sips_tuned_final.py "/path/to/SRC" "/path/to/OUT"
```

### 3) Strict alpha protection

If you store lots of transparent PNG assets (icons, stickers, design exports):

```bash
python3 img2heic_sips_tuned_final.py SRC OUT --disallow-alpha-drop
```

### 4) Quality assurance: SSIM (slower)

```bash
python3 img2heic_sips_tuned_final.py SRC OUT \
  --disallow-alpha-drop \  # default on
  --verify-ssim \
  --ssim-threshold 0.97 \
  --quality-ladder 55,65,75,85,90
```

### 5) External drive speedup (precache to local)

When the source is under `/Volumes/...`, precache is auto-enabled. Default batch: 8G / 400 files. Temp dir: `~/Downloads/img2heic_temp`.

```bash
python3 img2heic_sips_tuned_final.py "/Volumes/T7/pic"
# Custom options:
# --threads 2              # default 2, gentler on external drives
# --precache-bytes 8G      # batch size
# --precache-files 400     # batch file count
# --precache-dir ~/Downloads/img2heic_temp_run2
# --no-precache            # disable precache
```

### 6) Non-image files

- By default non-images are preserved to output; on the same APFS volume it prefers clone (`cp -c`), falling back to copy.
- To process images only, add `--skip-non-images`.

---

## Defaults & paths

- Concurrency: default `--threads 2` (external-drive friendly); raise if local SSD.
- Precache: auto-on when source under `/Volumes/...`, default batch 8G / 400 files, temp dir `~/Downloads/img2heic_temp`.
- Logs: `~/Downloads/img2heic_log/heic_conversion.log` (override with `IMG2HEIC_LOG_DIR`).
- Output: default `<SRC>_out`, structure preserved.
- Progress: `<OUT>/.conversion_progress.json` for resume.

> Running multiple instances? Give each a distinct `--out` / `--precache-dir` / `IMG2HEIC_LOG_DIR` to avoid collisions.

---

## Extra utilities

- `bench_heic_compare_fixed.py`: benchmark sips vs heif-enc, output CSV/JSON/HTML; “calibrate” mode finds minimal quality meeting SSIM threshold.
  - Example: `python3 bench_heic_compare_fixed.py --src ./testset --out ./bench_out --methods sips,heif-enc --csv result.csv --html report.html`
  - Calibrate: `python3 bench_heic_compare_fixed.py --src ./testset --out ./bench_out --calibrate sips --qualities 55 65 75 85 90 95 --ssim 0.97 --csv cal.csv --json cal.json`
- `compare_stems.py`: compare two directories by filename stem (ignoring jpg/jpeg/png/heic suffixes), list files only in one side.
  - Example: `python3 compare_stems.py dirA dirB [--no-recursive] [--ignore-case]`
- `clone_missing.py`: using `compare_stems` results, clone/copy the missing-side files to a target directory (structure preserved; prefer APFS clone, fallback to copy).
  - Example: `python3 clone_missing.py --which B dirA dirB /path/to/target [--no-recursive] [--ignore-case] [--dry-run] [--verbose]`

---

## Output layout

The output folder mirrors the input structure.

Typical files in the output directory:

- Converted `.heic` images (for PNG/JPEG inputs)
- Copied originals for anything skipped (unsupported, larger after conversion, alpha issues, etc.)
- `.conversion_progress.json` — resume state (do not delete unless you want a full re-run)
- `conversion_report.html` — summary + per-file stats

---

## Notes on transparency (alpha)

HEIC alpha support varies between apps and pipelines. If your workflow relies heavily on transparency:

- Use `--disallow-alpha-drop` to avoid silent alpha loss
- Keep critical transparent assets as PNG if needed (the script will fall back safely)

---

## Troubleshooting

### “sips not found”
`/usr/bin/sips` is built-in on macOS. If your environment can’t find it, verify:

```bash
which sips
# should print: /usr/bin/sips
```

### SSIM is always missing / “unavailable”
Install optional deps:

```bash
pip install pillow-heif scikit-image
```

---

## License

Use and modify freely for personal archiving and cleanup workflows.
(If you plan to publish it, consider adding an explicit license file such as MIT.)

---

## Repository structure (suggested)

```text
.
├── img2heic_sips_tuned_final.py
├── README.md
├── docs/
│   └── images/
│       ├── workflow.png
│       ├── terminal-run.png
│       └── report-example.png
└── examples/
    └── conversion_report_example.html
```
