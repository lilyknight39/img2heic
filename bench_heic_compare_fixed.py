#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_heic_compare.py
对同一批图片分别用 heif-enc 与 sips 转成 HEIC，并输出逐文件对比结果（CSV/JSON/HTML）。

用法示例：
  # 1) 直接对比（按脚本同款“智能质量”规则）
  python3 bench_heic_compare.py --src ./testset --out ./bench_out --methods sips,heif-enc --csv result.csv --html report.html

  # 2) 针对 sips 做质量标定：在多个 formatOptions 中找“最小且满足 SSIM 阈值”的值
  python3 bench_heic_compare.py --src ./testset --out ./bench_out --calibrate sips --qualities 55 65 75 85 90 95 --ssim 0.97 --csv cal.csv --json cal.json

说明：
- SSIM 需要安装：pip install scikit-image pillow-heif
- 该脚本不会修改你的原始文件；必要时只会在临时目录复制一个“修正后缀名”的副本。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image

# 可选：HEIC 解码 + SSIM
try:
    import pillow_heif  # noqa: F401
    pillow_heif.register_heif_opener()  # ensure PIL can open HEIC/AVIF
    from skimage.metrics import structural_similarity as ssim  # type: ignore
    import numpy as np
    SSIM_AVAILABLE = True
except Exception:
    SSIM_AVAILABLE = False
    ssim = None
    np = None  # type: ignore


MAGIC_BYTES = {
    b"\xFF\xD8\xFF": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def detect_real_format(filepath: Path) -> Optional[str]:
    """通过 magic bytes 或 PIL 识别真实格式（jpeg/png），无法识别则返回 None。"""
    try:
        with open(filepath, "rb") as f:
            header = f.read(12)
        for magic, fmt in MAGIC_BYTES.items():
            if header.startswith(magic):
                return fmt
    except Exception:
        return None

    try:
        with Image.open(filepath) as img:
            fmt = (img.format or "").lower()
        if fmt in ("jpeg", "png"):
            return fmt
        return None
    except Exception:
        return None


def has_alpha_channel(p: Path) -> bool:
    try:
        with Image.open(p) as img:
            # PIL 里 PNG 透明可能在 mode 或 info['transparency']
            if "A" in img.getbands():
                return True
            if "transparency" in img.info:
                return True
            return False
    except Exception:
        return False


def get_pixels(p: Path) -> int:
    try:
        with Image.open(p) as img:
            w, h = img.size
            return int(w) * int(h)
    except Exception:
        return 0


def smart_quality(input_path: Path, real_format: str) -> int:
    """
    复刻你原脚本的“智能质量”逻辑（便于前后可比）：
      - JPEG：按 bpp 分档
      - PNG：有 alpha 用 95，否则 90
    """
    size = input_path.stat().st_size
    pixels = get_pixels(input_path)
    if real_format == "jpeg":
        bpp = (size * 8 / pixels) if pixels > 0 else 1.0
        if bpp > 1:
            return 95
        if bpp > 0.5:
            return 85
        return 75
    # png
    return 95 if has_alpha_channel(input_path) else 90


def prepare_input_copy(original: Path, real_format: str) -> tuple[Path, Optional[Path], str]:
    """
    为了兼容 heif-enc（以及统一口径），必要时复制一个临时文件修正后缀名：
      - jpeg -> .jpg
      - png  -> .png
      - 同时把 .jpeg 也统一成 .jpg
    返回：(input_path, tmp_path, normalized_ext)
    """
    ext = original.suffix.lower()
    want_suffix = ".jpg" if real_format == "jpeg" else ".png"
    is_suffix_ok = (real_format == "jpeg" and ext in [".jpg", ".jpeg"]) or (real_format == "png" and ext == ".png")
    needs_temp = (not is_suffix_ok) or (ext == ".jpeg")

    if not needs_temp:
        return original, None, ext

    tmp = tempfile.NamedTemporaryFile(prefix=original.stem + "_", suffix=want_suffix, delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    shutil.copyfile(original, tmp_path)
    return tmp_path, tmp_path, want_suffix


def run_cmd(cmd: list[str]) -> tuple[int, str]:
    """运行命令并返回 (returncode, stderr)。stdout 默认丢弃。"""
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return res.returncode, (res.stderr or "").strip()


def compute_ssim(orig_path: Path, heic_path: Path) -> Optional[float]:
    if not SSIM_AVAILABLE or ssim is None or np is None:
        return None
    try:
        img1 = np.array(Image.open(orig_path).convert("RGB"))
        img2 = np.array(Image.open(heic_path).convert("RGB"))
        if img1.shape != img2.shape:
            # 保底：尺寸不一致就不算（理论上不该发生）
            return None
        return float(ssim(img1, img2, channel_axis=2))
    except Exception:
        return None


def output_has_alpha(heic_path: Path) -> Optional[bool]:
    try:
        with Image.open(heic_path) as img:
            return ("A" in img.getbands())
    except Exception:
        return None


@dataclass
class Row:
    file: str
    size_in: int
    real_format: str
    ext_norm: str
    pixels: int
    has_alpha_in: bool

    method: str
    quality: int
    seconds: float
    ok: bool
    error: str = ""

    size_out: int = 0
    saved_pct: Optional[float] = None
    out_has_alpha: Optional[bool] = None
    ssim: Optional[float] = None
    out_path: str = ""


def ensure_tools(methods: Sequence[str]) -> dict[str, Optional[str]]:
    tool_map: dict[str, Optional[str]] = {}
    for m in methods:
        if m == "sips":
            tool_map[m] = shutil.which("sips")
        elif m == "heif-enc":
            tool_map[m] = shutil.which("heif-enc")
        else:
            tool_map[m] = None
    return tool_map


def encode_one(method: str, tool: str, input_path: Path, out_path: Path, quality: int) -> tuple[bool, str, float]:
    start = time.perf_counter()
    if method == "sips":
        cmd = [tool, "-s", "format", "heic", "-s", "formatOptions", str(quality), str(input_path), "--out", str(out_path)]
    else:
        cmd = [tool, "-q", str(quality), str(input_path), "-o", str(out_path)]
    rc, err = run_cmd(cmd)
    sec = time.perf_counter() - start
    return (rc == 0), err, sec


def iter_files(src: Path) -> list[Path]:
    files: list[Path] = []
    for p in src.rglob("*"):
        if p.is_file() and not p.is_symlink():
            files.append(p)
    return files


def write_csv(rows: list[Row], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()) if rows else [])
        if rows:
            w.writeheader()
            for r in rows:
                w.writerow(asdict(r))


def write_json(rows: list[Row], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_html(rows: list[Row], path: Path, title: str):
    # 极简 HTML（方便双击查看）
    path.parent.mkdir(parents=True, exist_ok=True)
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    # 汇总
    by_method: dict[str, dict[str, float]] = {}
    for r in rows:
        m = r.method
        by_method.setdefault(m, {"count": 0, "ok": 0, "sec": 0.0, "saved_sum": 0.0, "saved_n": 0})
        by_method[m]["count"] += 1
        by_method[m]["sec"] += r.seconds
        if r.ok:
            by_method[m]["ok"] += 1
        if r.saved_pct is not None:
            by_method[m]["saved_sum"] += r.saved_pct
            by_method[m]["saved_n"] += 1

    summary_rows = []
    for m, s in by_method.items():
        avg_sec = s["sec"] / s["count"] if s["count"] else 0
        avg_saved = (s["saved_sum"] / s["saved_n"]) if s["saved_n"] else None
        summary_rows.append((m, int(s["ok"]), int(s["count"]), avg_sec, avg_saved))

    html = []
    html.append("<!doctype html><meta charset='utf-8'/>")
    html.append(f"<title>{esc(title)}</title>")
    html.append("<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial; padding:20px;} table{border-collapse:collapse; width:100%;} th,td{border:1px solid #ddd; padding:6px; font-size:12px;} th{background:#f5f5f5; position:sticky; top:0;} .bad{background:#fff1f0;} .good{background:#f6ffed;}</style>")
    html.append(f"<h2>{esc(title)}</h2>")
    html.append("<h3>汇总</h3><table><tr><th>method</th><th>ok</th><th>count</th><th>avg_seconds</th><th>avg_saved_pct</th></tr>")
    for m, okc, cnt, avg_sec, avg_saved in summary_rows:
        html.append(f"<tr><td>{esc(m)}</td><td>{okc}</td><td>{cnt}</td><td>{avg_sec:.4f}</td><td>{'' if avg_saved is None else f'{avg_saved:.2f}'}</td></tr>")
    html.append("</table>")

    html.append("<h3>逐文件结果</h3>")
    html.append("<table><tr>"
                "<th>file</th><th>method</th><th>quality</th><th>ok</th><th>seconds</th>"
                "<th>size_in</th><th>size_out</th><th>saved_pct</th>"
                "<th>alpha_in</th><th>alpha_out</th><th>ssim</th><th>error</th>"
                "</tr>")
    for r in rows:
        cls = "good" if r.ok and (r.saved_pct is None or r.saved_pct >= 0) else "bad"
        html.append("<tr class='%s'>"
                    "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%.4f</td>"
                    "<td>%d</td><td>%d</td><td>%s</td>"
                    "<td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                    "</tr>" % (
                        cls,
                        esc(r.file),
                        esc(r.method),
                        r.quality,
                        "Y" if r.ok else "N",
                        r.seconds,
                        r.size_in,
                        r.size_out,
                        "" if r.saved_pct is None else f"{r.saved_pct:.2f}",
                        "Y" if r.has_alpha_in else "N",
                        "" if r.out_has_alpha is None else ("Y" if r.out_has_alpha else "N"),
                        "" if r.ssim is None else f"{r.ssim:.5f}",
                        esc(r.error[:200]),
                    ))
    html.append("</table>")
    Path(path).write_text("\n".join(html), encoding="utf-8")


def calibrate_for_method(
    files: list[Path],
    method: str,
    tool: str,
    out_dir: Path,
    qualities: list[int],
    ssim_threshold: float,
) -> list[Row]:
    """
    对每张图在 qualities 中找最小 quality 使 SSIM >= 阈值（且输出不比原图大时优先）。
    输出的 Row.method 仍为 method，但 quality 是“推荐值”。
    """
    rows: list[Row] = []
    for f in files:
        real = detect_real_format(f)
        if real not in ("jpeg", "png"):
            # 非图片 / 无法识别：跳过（但记录）
            rows.append(Row(
                file=str(f), size_in=f.stat().st_size, real_format=str(real), ext_norm=f.suffix.lower(),
                pixels=get_pixels(f), has_alpha_in=has_alpha_channel(f),
                method=method, quality=-1, seconds=0.0, ok=False, error="unrecognized image format"
            ))
            continue

        input_path, tmp_path, ext_norm = prepare_input_copy(f, real)
        pixels = get_pixels(input_path)
        alpha_in = has_alpha_channel(input_path)
        size_in = f.stat().st_size

        best: Optional[Row] = None
        try:
            for q in qualities:
                out_path = out_dir / method / (f"{f.stem}.q{q}.heic")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                ok, err, sec = encode_one(method, tool, input_path, out_path, q)
                if not ok or not out_path.exists():
                    continue
                size_out = out_path.stat().st_size
                saved = (size_in - size_out) / size_in * 100 if size_in > 0 else None
                s = compute_ssim(f, out_path)
                # 先满足 SSIM，再比较 size（更小更优），再比较 q（更低更优）
                if s is not None and s >= ssim_threshold:
                    candidate = Row(
                        file=str(f), size_in=size_in, real_format=real, ext_norm=ext_norm,
                        pixels=pixels, has_alpha_in=alpha_in,
                        method=method, quality=q, seconds=sec, ok=True, error="",
                        size_out=size_out, saved_pct=saved, out_has_alpha=output_has_alpha(out_path),
                        ssim=s, out_path=str(out_path),
                    )
                    if best is None:
                        best = candidate
                    else:
                        # prefer smaller output; if tie, lower q
                        bo = best.size_out
                        if size_out < bo or (size_out == bo and q < best.quality):
                            best = candidate
        finally:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

        if best is None:
            rows.append(Row(
                file=str(f), size_in=size_in, real_format=real, ext_norm=ext_norm,
                pixels=pixels, has_alpha_in=alpha_in,
                method=method, quality=-1, seconds=0.0, ok=False,
                error=f"no quality meets SSIM>={ssim_threshold} (or SSIM unavailable)"
            ))
        else:
            rows.append(best)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="测试集目录（递归扫描）")
    ap.add_argument("--out", default="./bench_out", help="输出目录（存放 heic 结果与报告）")
    ap.add_argument("--methods", default="sips,heif-enc", help="对比方法，逗号分隔：sips,heif-enc")
    ap.add_argument("--csv", default="", help="输出 CSV 路径（相对 out 或绝对）")
    ap.add_argument("--json", default="", help="输出 JSON 路径（相对 out 或绝对）")
    ap.add_argument("--html", default="", help="输出 HTML 路径（相对 out 或绝对）")
    ap.add_argument("--keep", action="store_true", help="保留所有输出 HEIC（默认会保留在 out/method 下）")
    ap.add_argument("--calibrate", default="", help="标定模式：填写 sips 或 heif-enc，仅对该方法跑多质量搜索")
    ap.add_argument("--qualities", nargs="*", type=int, default=[55, 65, 75, 85, 90, 95], help="标定模式质量候选列表")
    ap.add_argument("--ssim", type=float, default=0.97, help="标定模式 SSIM 阈值（需要 scikit-image+pillow-heif）")
    args = ap.parse_args()

    src = Path(args.src).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    files = iter_files(src)
    files.sort()

    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    if args.calibrate:
        methods = [args.calibrate.strip()]

    tools = ensure_tools(methods)
    missing = [m for m in methods if not tools.get(m)]
    if missing:
        raise SystemExit(f"缺少工具：{missing}。请确认已安装/在 PATH 中。")

    rows: list[Row] = []

    if args.calibrate:
        method = args.calibrate.strip()
        tool = tools[method] or ""
        rows = calibrate_for_method(files, method, tool, out_dir, args.qualities, args.ssim)
    else:
        # compare 模式：每张图每种方法跑一次
        for f in files:
            real = detect_real_format(f)
            if real not in ("jpeg", "png"):
                # 不是有效图片：每种方法都记录为跳过
                for method in methods:
                    rows.append(Row(
                        file=str(f), size_in=f.stat().st_size, real_format=str(real), ext_norm=f.suffix.lower(),
                        pixels=get_pixels(f), has_alpha_in=has_alpha_channel(f),
                        method=method, quality=-1, seconds=0.0, ok=False,
                        error="unrecognized image format"
                    ))
                continue

            input_path, tmp_path, ext_norm = prepare_input_copy(f, real)
            pixels = get_pixels(input_path)
            alpha_in = has_alpha_channel(input_path)
            q = smart_quality(input_path, real)
            size_in = f.stat().st_size

            try:
                for method in methods:
                    tool = tools[method] or ""
                    out_path = out_dir / method / f"{f.stem}.heic"
                    out_path.parent.mkdir(parents=True, exist_ok=True)

                    ok, err, sec = encode_one(method, tool, input_path, out_path, q)
                    r = Row(
                        file=str(f), size_in=size_in, real_format=real, ext_norm=ext_norm, pixels=pixels,
                        has_alpha_in=alpha_in,
                        method=method, quality=q, seconds=sec, ok=ok, error=err,
                        out_path=str(out_path)
                    )
                    if ok and out_path.exists():
                        r.size_out = out_path.stat().st_size
                        r.saved_pct = ((size_in - r.size_out) / size_in * 100) if size_in > 0 else None
                        r.out_has_alpha = output_has_alpha(out_path)
                        r.ssim = compute_ssim(f, out_path)
                    rows.append(r)
            finally:
                if tmp_path and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass

    # 写输出
    def resolve_out(p: str) -> Optional[Path]:
        if not p:
            return None
        pp = Path(p)
        if not pp.is_absolute():
            pp = out_dir / pp
        return pp

    csv_path = resolve_out(args.csv) if args.csv else None
    json_path = resolve_out(args.json) if args.json else None
    html_path = resolve_out(args.html) if args.html else None

    if csv_path:
        write_csv(rows, csv_path)
    if json_path:
        write_json(rows, json_path)
    if html_path:
        write_html(rows, html_path, title="HEIC 转码对比报告")

    # 控制台汇总
    by_method = {}
    for r in rows:
        m = r.method
        by_method.setdefault(m, {"count": 0, "ok": 0, "sec": 0.0, "saved": []})
        by_method[m]["count"] += 1
        if r.ok:
            by_method[m]["ok"] += 1
        by_method[m]["sec"] += r.seconds
        if r.saved_pct is not None:
            by_method[m]["saved"].append(r.saved_pct)

    print("=== summary ===")
    for m, s in by_method.items():
        avg_sec = s["sec"] / s["count"] if s["count"] else 0
        saved_list = s["saved"]
        avg_saved = (sum(saved_list) / len(saved_list)) if saved_list else None
        print(f"{m:8s} ok {s['ok']}/{s['count']}  avg_sec={avg_sec:.4f}  avg_saved_pct={'' if avg_saved is None else f'{avg_saved:.2f}'}")
    if args.calibrate:
        print("calibrate 模式：CSV/JSON 中的 quality 即该图片的推荐 quality（候选里满足 SSIM 阈值的最小值）。")
        if not SSIM_AVAILABLE:
            print("⚠️ 你的环境缺少 SSIM 依赖（scikit-image + pillow-heif），标定结果会退化为失败。")


if __name__ == "__main__":
    main()
