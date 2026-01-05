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

### 1) 最简交互（推荐）

```bash
python3 img2heic_sips_tuned_final.py
# 运行后会提示输入源目录，输出默认：<SRC>_out
```

### 2) 显式指定输入/输出

```bash
python3 img2heic_sips_tuned_final.py "/path/to/SRC" "/path/to/OUT"
```

### 3) 严格透明度保护

If you store lots of transparent PNG assets (icons, stickers, design exports):

```bash
python3 img2heic_sips_tuned_final.py SRC OUT --disallow-alpha-drop
```

### 4) 质量保障：SSIM（更慢）

```bash
python3 img2heic_sips_tuned_final.py SRC OUT \
  --disallow-alpha-drop \  # 默认已开启
  --verify-ssim \
  --ssim-threshold 0.97 \
  --quality-ladder 55,65,75,85,90
```

### 5) 外置盘加速（预拷到本地）

外置盘路径（在 `/Volumes/...`）会自动启用预拷，默认批次：8G / 400 张，预拷目录默认 `~/Downloads/img2heic_temp`。

```bash
python3 img2heic_sips_tuned_final.py "/Volumes/T7/pic"
# 如需自定义：
# --threads 2              # 默认 2，更友好外置盘
# --precache-bytes 8G      # 每批大小
# --precache-files 400     # 每批文件数
# --precache-dir ~/Downloads/img2heic_temp_run2
# --no-precache            # 禁用预拷
```

### 6) 非图片文件

- 默认会处理非图片（保留到输出目录），同卷 APFS 下优先 clone（`cp -c`），失败再复制。
- 若只想处理图片，显式加 `--skip-non-images`。

---

## Defaults & paths

- 并行度：默认 `--threads 2`（外置盘友好），可自行调高。
- 预拷：源在 `/Volumes/...` 时自动开启，默认批次 8G / 400 张，临时目录 `~/Downloads/img2heic_temp`。
- 日志：`~/Downloads/img2heic_log/heic_conversion.log`（可用环境变量 `IMG2HEIC_LOG_DIR` 覆盖）。
- 输出：未指定时为 `<SRC>_out`，保留原目录结构。
- 进度文件：`<OUT>/.conversion_progress.json`，便于断点续传。

> 并行跑多份脚本时，请为每份指定独立的 `--out` / `--precache-dir` / `IMG2HEIC_LOG_DIR`，避免互相覆盖。

---

## Extra utilities

- `bench_heic_compare_fixed.py`：基准对比 sips 与 heif-enc 的 HEIC 输出，可生成 CSV/JSON/HTML；支持“标定模式”按 SSIM 阈值寻找最优 quality。
  - 示例：`python3 bench_heic_compare_fixed.py --src ./testset --out ./bench_out --methods sips,heif-enc --csv result.csv --html report.html`
  - 标定：`python3 bench_heic_compare_fixed.py --src ./testset --out ./bench_out --calibrate sips --qualities 55 65 75 85 90 95 --ssim 0.97 --csv cal.csv --json cal.json`
- `compare_stems.py`：对比两个目录的“文件名主干”（忽略 jpg/jpeg/png/heic 后缀），列出只存在于一侧的文件列表。
  - 示例：`python3 compare_stems.py dirA dirB [--no-recursive] [--ignore-case]`
- `clone_missing.py`：基于 `compare_stems` 结果，把缺失的一侧文件 clone/copy 到目标目录（保留目录结构，优先 APFS clone，失败回退复制）。
  - 示例：`python3 clone_missing.py --which B dirA dirB /path/to/target [--no-recursive] [--ignore-case] [--dry-run] [--verbose]`

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
