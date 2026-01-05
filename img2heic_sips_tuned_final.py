#!/usr/bin/env python3
"""
批量图片转 HEIC 存档工具 v2.2
- 支持大规模文件夹（50k+ 图片）
- 断点续传：中断后可继续
- 保留非图片文件到目标目录
- 使用生成器减少内存占用
- 可视化报告 + 智能质量建议
- SSIM 质量评估（随机抽样）
"""

import subprocess
import shutil
import tempfile
import os
import sys
import argparse
import re
from datetime import datetime
from pathlib import Path
from multiprocessing import Pool, cpu_count
from PIL import Image
import logging
from tqdm import tqdm
from collections import Counter
import json

def _fast_png_info(path: Path) -> tuple[int, int, bool] | None:
    """
    解析 PNG 头部拿到 (w, h, has_alpha_or_transparency)。
    - IHDR color type 4/6 直接含 alpha
    - 若存在 tRNS chunk（调色板/灰度透明），也视为“有透明”（用于质量策略）
    """
    try:
        with open(path, "rb") as f:
            sig = f.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                return None
            # length(4) + type(4) + data(13) = 21
            chunk_len = int.from_bytes(f.read(4), "big")
            ctype = f.read(4)
            if ctype != b"IHDR" or chunk_len < 13:
                return None
            data = f.read(13)
            w = int.from_bytes(data[0:4], "big")
            h = int.from_bytes(data[4:8], "big")
            color_type = data[9]
            has_alpha = color_type in (4, 6)

        # 额外快速扫描一小段寻找 tRNS（避免全文件解析）
        if not has_alpha:
            with open(path, "rb") as f:
                blob = f.read(65536)  # 64KB 足够覆盖大部分小 PNG 的前几个 chunk
            if b"tRNS" in blob:
                has_alpha = True

        return w, h, has_alpha
    except Exception:
        return None


def _fast_jpeg_size(path: Path) -> tuple[int, int] | None:
    """解析 JPEG SOF 段得到 (w, h)。失败则返回 None。"""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xFF\xD8":
                return None
            while True:
                # 找 marker：跳过填充 FF
                b = f.read(1)
                if not b:
                    return None
                while b != b"\xFF":
                    b = f.read(1)
                    if not b:
                        return None
                # 跳过连续 FF
                while True:
                    m = f.read(1)
                    if not m:
                        return None
                    if m != b"\xFF":
                        break

                marker = m[0]
                # stand-alone markers
                if marker in (0xD9, 0xDA):  # EOI / SOS
                    return None
                # 读段长度
                seg_len_bytes = f.read(2)
                if len(seg_len_bytes) != 2:
                    return None
                seg_len = int.from_bytes(seg_len_bytes, "big")
                if seg_len < 2:
                    return None

                # SOF markers（包括 baseline/progressive 等）
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    seg = f.read(5)  # precision(1) + height(2) + width(2)
                    if len(seg) != 5:
                        return None
                    h = int.from_bytes(seg[1:3], "big")
                    w = int.from_bytes(seg[3:5], "big")
                    return w, h

                # 跳过当前段剩余字节（seg_len 包括这 2 字节长度本身）
                f.seek(seg_len - 2, 1)
    except Exception:
        return None


def fast_image_info(path: Path, real_format: str) -> tuple[int, int, bool] | None:
    """
    返回 (w, h, has_alpha_or_transparency)。
    先走快速头解析，失败再回退 PIL（兼容性更强但更慢）。
    """
    if real_format == "png":
        info = _fast_png_info(path)
        if info is not None:
            w, h, a = info
            return w, h, a
    elif real_format == "jpeg":
        sz = _fast_jpeg_size(path)
        if sz is not None:
            w, h = sz
            return w, h, False

    # fallback
    try:
        with Image.open(path) as img:
            w, h = img.size
            a = ("A" in img.getbands()) or ("transparency" in img.info)
            return w, h, a
    except Exception:
        return None

import random
import numpy as np
from typing import Any, Callable, Optional

# SSIM 支持（可选）
ssim: Optional[Callable[..., Any]] = None
try:
    from skimage.metrics import structural_similarity as ssim
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        SSIM_AVAILABLE = True
    except ImportError:
        SSIM_AVAILABLE = False
        print("⚠️ pillow-heif 未安装，SSIM 测试功能将被禁用")
        print("   安装方法: pip install pillow-heif scikit-image")
except ImportError:
    SSIM_AVAILABLE = False
    print("⚠️ scikit-image 未安装，SSIM 测试功能将被禁用")
    print("   安装方法: pip install scikit-image pillow-heif")

# ------------------------------
# 全局配置（在 main 中初始化）
# ------------------------------
SRC: Optional[Path] = None
ARCHIVE: Optional[Path] = None
THREADS: Optional[int] = None
LOG_FILE = Path.cwd() / "heic_conversion.log"
PROGRESS_FILE: Optional[Path] = None
REPORT_FILE: Optional[Path] = None

# 支持的图片格式
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}

# SSIM 测试配置
SSIM_SAMPLE_SIZE = 20  # 随机抽样数量
SSIM_MIN_THRESHOLD = 0.95  # SSIM 最低阈值

# ------------------------------
# 日志配置
# ------------------------------
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ------------------------------
# 工具检测（macOS：优先使用 sips）
# ------------------------------
SIPS = shutil.which("sips")
# 不在 import 时硬失败，便于 --help / 静态检查；运行时再检查
if not SIPS:
    SIPS = None

# ------------------------------
# 编码质量策略（基于标定：大多数图片在 55 即可达到 SSIM≈0.97）
# 说明：若启用 --verify-ssim，会按 QUALITY_LADDER 逐档尝试并以 SSIM_THRESHOLD 作为门槛自动升档。
# ------------------------------
DEFAULT_QUALITY = 55
QUALITY_LADDER = [55, 65, 75, 85, 90]
SSIM_THRESHOLD = 0.97
VERIFY_SSIM = False
ALLOW_ALPHA_DROP_IF_OPAQUE = True

def tuned_default_quality(real_format: str, has_alpha: bool, pixels: int, filesize: int) -> int:
    """基于你的标定集拟合出的默认质量策略（不开 --verify-ssim 时使用）。
    - 全局默认 55
    - PNG + alpha 且 (像素>=25MP 或 文件>=20MB) → 90
    - JPEG 文件>=15MB → 90；>=8MB → 75
    说明：如启用 --verify-ssim，会从该默认档位起步并按 QUALITY_LADDER 逐档升档。
    """
    MB = 1024 * 1024
    q = 55
    rf = (real_format or "").lower()
    if rf == "png":
        if has_alpha and (pixels >= 25_000_000 or filesize >= 20 * MB):
            q = 90
    elif rf in ("jpeg", "jpg"):
        if filesize >= 15 * MB:
            q = 90
        elif filesize >= 8 * MB:
            q = 75
    return q


# ------------------------------
# 文件头 Magic Bytes 校验
# ------------------------------
MAGIC_BYTES = {
    b'\xff\xd8\xff': 'jpeg',      # JPEG
    b'\x89PNG\r\n\x1a\n': 'png',  # PNG
}


def alpha_actually_used(path: Path) -> bool:
    """粗略判断 PNG/JPEG（主要是 PNG）是否真的用到了透明（alpha<255）。
    - 为了安全：如果无法判断，返回 True（当作用到了透明）。
    - 只对带 alpha 的输入调用；内部会缩略到小尺寸以降低开销。
    """
    try:
        with Image.open(path) as im:
            # 仅对含 alpha 的模式检查
            if 'A' not in im.getbands():
                return False
            # 缩略以降低解码成本（对大图更重要）
            im = im.copy()
            im.thumbnail((256, 256))
            alpha = im.getchannel('A')
            lo, hi = alpha.getextrema()
            return lo < 255
    except Exception:
        return True

def heic_has_alpha(path: Path) -> bool | None:
    """尝试读取 HEIC 并判断是否带 alpha。失败返回 None。"""
    if not SSIM_AVAILABLE:
        return None
    try:
        with Image.open(path) as im:
            return 'A' in im.getbands()
    except Exception:
        return None
def detect_real_format(filepath: Path) -> str | None:
    """通过文件头 magic bytes + PIL 兜底检测真实文件格式"""
    try:
        with open(filepath, 'rb') as f:
            header = f.read(12)
        for magic, fmt in MAGIC_BYTES.items():
            if header.startswith(magic):
                return fmt
    except Exception:
        return None

    # magic bytes 无法识别时，使用 PIL 兜底
    try:
        with Image.open(filepath) as img:
            fmt = (img.format or "").lower()
        if fmt in ("jpeg", "png"):
            return fmt
        return None
    except Exception:
        return None

# ------------------------------
# 元数据时间处理
# ------------------------------
def preserve_times(src: Path, dst: Path):
    """尽量保留修改/创建时间"""
    try:
        st = src.stat()
        os.utime(dst, (st.st_atime, st.st_mtime))
    except Exception as e:
        logging.warning(f"TIME - {dst} 修改时间写入失败: {e}")

    setfile = shutil.which("SetFile")
    if not setfile:
        return

    try:
        birth = getattr(st, "st_birthtime", None)
        if birth is None:
            birth = st.st_ctime
        create_time = datetime.fromtimestamp(birth)
        formatted = create_time.strftime("%m/%d/%Y %H:%M:%S")
        res = subprocess.run(
            [setfile, "-d", formatted, str(dst)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if res.returncode != 0:
            err = res.stderr.strip() or "unknown error"
            logging.warning(f"TIME - {dst} 创建时间写入失败: {err}")
    except Exception as e:
        logging.warning(f"TIME - {dst} 创建时间写入失败: {e}")

# ------------------------------
# 断点续传：加载/保存进度
# ------------------------------
def load_progress() -> set:
    """加载已处理文件列表"""
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, 'r') as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_progress(processed: set):
    """保存已处理文件列表"""
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(list(processed), f)
    except Exception:
        pass

# ------------------------------
# SSIM 质量评估
# ------------------------------
def calculate_ssim(original_path: Path, heic_path: Path) -> float | None:
    """计算原图和 HEIC 之间的 SSIM 分数"""
    if not SSIM_AVAILABLE or ssim is None:
        return None
    
    try:
        # 加载并转换为相同格式
        img1 = np.array(Image.open(original_path).convert("RGB"))
        img2 = np.array(Image.open(heic_path).convert("RGB"))
        
        # 确保尺寸一致
        if img1.shape != img2.shape:
            return None
        
        # 计算 SSIM，确保返回 float（避免返回 (score, diff) 的情况）
        score = ssim(img1, img2, channel_axis=2, full=False)
        if isinstance(score, tuple):
            score = score[0]
        return float(score)
    except Exception as e:
        logging.error(f"SSIM 计算失败: {original_path} - {e}")
        return None

def test_quality_samples(sample_pairs: list) -> dict:
    """对抽样的图片对进行 SSIM 质量测试"""
    if not SSIM_AVAILABLE or not sample_pairs:
        return {"available": False, "scores": [], "avg_ssim": 0}
    
    scores = []
    print(f"\n正在进行 SSIM 质量测试（抽样 {len(sample_pairs)} 张图片）...")
    
    for original, heic in tqdm(sample_pairs, desc="SSIM 测试", ncols=80):
        score = calculate_ssim(original, heic)
        if score is not None:
            scores.append(score)
    
    avg_ssim = sum(scores) / len(scores) if scores else 0
    return {
        "available": True,
        "scores": scores,
        "avg_ssim": avg_ssim,
        "sample_count": len(scores)
    }

# ------------------------------
# 文件处理函数
# ------------------------------
def process_file(f: Path):
    stats = {
        "total": 1, 
        "converted": 0, 
        "heic_larger": 0, 
        "copied": 0, 
        "skipped_symlink": 0, 
        "compression_ratios": [],
        "sample_pair": None  # (original_path, heic_path) for SSIM testing
    }
    ext = f.suffix.lower()
    rel_path = f.relative_to(SRC)

    # 跳过软链接
    if f.is_symlink():
        logging.info(f"SKIP_SYMLINK - {f}")
        stats["skipped_symlink"] = 1
        return stats, None, str(f)

    # 非图片文件：直接复制保留
    if ext not in IMAGE_EXTENSIONS:
        out_file = ARCHIVE / rel_path
        out_file.parent.mkdir(parents=True, exist_ok=True)
        if not out_file.exists():
            shutil.copy2(f, out_file)
            preserve_times(f, out_file)
            stats["copied"] = 1
            logging.info(f"COPY - {f} → {out_file}")
        return stats, None, str(f)

    # 文件头校验：防止后缀名被误改
    # 文件头校验：防止后缀名被误改；若可识别则自动修复后重试
    real_format = detect_real_format(f)
    if real_format is None:
        logging.warning(f"SKIP - {f} (无法识别文件头，可能不是有效图片)")
        return stats, None, str(f)

    # 统一后缀（便于命令行转换工具识别）：jpeg → .jpg, png → .png；若后缀不匹配则临时修复并继续处理
    tmp_path: Optional[Path] = None
    input_path: Path = f
    want_suffix = ".jpg" if real_format == "jpeg" else ".png"
    is_suffix_ok = (real_format == "jpeg" and ext in [".jpg", ".jpeg"]) or (real_format == "png" and ext == ".png")
    needs_temp = (not is_suffix_ok) or (ext == ".jpeg")

    if needs_temp:
        try:
            with tempfile.NamedTemporaryFile(prefix=f.stem + "_", suffix=want_suffix, delete=False) as tmp:
                tmp_path = Path(tmp.name)
            shutil.copy2(f, tmp_path)
            input_path = tmp_path
            logging.info(f"FIX_SUFFIX - {f} (后缀 {ext} / 实际 {real_format} → 临时 {want_suffix})")
        except Exception as e:
            logging.error(f"ERROR - {f} 临时修复后缀失败: {e}")
            return stats, f, str(f)

    # 归一化 ext 供后续质量/透明度逻辑使用
    ext = want_suffix


    out_file = ARCHIVE / rel_path.with_name(f"{rel_path.stem}{rel_path.suffix}.heic")
    i = 1
    while out_file.exists():
        out_file = ARCHIVE / rel_path.with_name(f"{rel_path.stem}_{i}.heic")
        i += 1
    out_file.parent.mkdir(parents=True, exist_ok=True)
    
    # 断点续传：已存在则跳过
    if out_file.exists():
        logging.info(f"SKIP_EXISTS - {f} (已转换)")
        return stats, None, str(f)
    
    filesize = f.stat().st_size

    info = fast_image_info(f, real_format)
    if info is None:
        logging.error(f"ERROR - {f} 无法打开/解析尺寸")
        return stats, f, str(f)
    w, h, has_alpha = info
    pixels = w * h

    # 编码质量：按（格式 / alpha / 像素 / 文件大小）选择默认档位；如启用 SSIM 校验则从该档位起步自动升档。
    heuristic_quality = tuned_default_quality(real_format, has_alpha, pixels, filesize)

    if VERIFY_SSIM:
        # 从默认档位起步，减少无谓的低档尝试
        quality_candidates = [q for q in QUALITY_LADDER if q >= heuristic_quality] or [heuristic_quality]
    else:
        quality_candidates = [heuristic_quality]
    chosen_quality = quality_candidates[-1] if quality_candidates else heuristic_quality
    last_err = None
    last_ssim = None

    for q in quality_candidates:
        chosen_quality = q
        cmd = [SIPS, '-s', 'format', 'heic', '-s', 'formatOptions', str(q), str(input_path), '--out', str(out_file)]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            last_err = (res.stderr.strip() or 'unknown error')
            break
        if VERIFY_SSIM and SSIM_AVAILABLE:
            s = calculate_ssim(input_path, out_file)
            last_ssim = s
            if s is not None and s >= SSIM_THRESHOLD:
                break
    if last_err:
        logging.error(f"ERROR - {f} 转换失败: {last_err}")
        return stats, f, str(f)
    # 清理临时文件（用于修正后缀名的输入副本）
    if tmp_path and tmp_path.exists():
        try:
            tmp_path.unlink()
        except Exception:
            pass

    # 透明度保护：如果输入真的用到了 alpha，但输出不带 alpha，则视为失败并保留原图
    if has_alpha:
        alpha_used = True
        if ALLOW_ALPHA_DROP_IF_OPAQUE:
            alpha_used = alpha_actually_used(input_path)
        out_alpha = heic_has_alpha(out_file)
        if alpha_used and out_alpha is False:
            try:
                out_file.unlink()
            except Exception:
                pass
            shutil.copy2(f, ARCHIVE / rel_path)
            preserve_times(f, ARCHIVE / rel_path)
            stats['heic_larger'] += 1  # 复用计数：最终仍保留原图
            logging.info(f"KEEP_ORIGINAL - {f} (输入含透明但输出丢失 alpha，保留原文件)")
            return stats, None, str(f)

    heic_size = out_file.stat().st_size
    compression_ratio = (filesize - heic_size) / filesize * 100

    if heic_size >= filesize:
        out_file.unlink()
        # HEIC 比原文件大时，复制原文件保留
        shutil.copy2(f, ARCHIVE / rel_path)
        preserve_times(f, ARCHIVE / rel_path)
        stats["heic_larger"] += 1
        logging.info(f"KEEP_ORIGINAL - {f} (HEIC 更大，保留原文件)")
    else:
        stats["converted"] = 1
        stats["compression_ratios"].append(compression_ratio)
        preserve_times(f, out_file)
        # 记录样本对用于 SSIM 测试
        stats["sample_pair"] = (str(f), str(out_file))
        logging.info(f"SUCCESS - {f} → {out_file} (节省: {compression_ratio:.2f}%)")

    return stats, None, str(f)

# ------------------------------
# 生成器：遍历文件夹（减少内存占用）
# ------------------------------
def gather_files_generator(src_dir: Path, processed: set):
    """使用生成器遍历，跳过已处理文件"""
    for f in src_dir.rglob("*"):
        if f.is_file() and str(f) not in processed:
            yield f

# ------------------------------
# 生成可视化 HTML 报告
# ------------------------------
def generate_html_report(stats: dict, ratios: list, ssim_results: dict):
    """生成包含图表的 HTML 报告"""
    
    # 压缩比分布统计
    bins_counter = Counter(min(int(r//10), 9) for r in ratios) if ratios else {}
    total_images = len(ratios) if ratios else 1
    
    # 计算分布数据
    bar_data = []
    for i in range(10):
        count = bins_counter.get(i, 0)
        percentage = count / total_images * 100 if total_images > 0 else 0
        bar_data.append({
            "label": f"{i*10}-{(i+1)*10}%",
            "count": count,
            "percentage": percentage
        })
    
    # 智能分析建议
    if ratios:
        avg_ratio = sum(ratios) / len(ratios)
        low_ratio_count = sum(1 for r in ratios if r < 10)
        negative_count = stats.get("heic_larger", 0)
        high_ratio_count = sum(1 for r in ratios if r > 50)
        
        suggestions = []
        
        # SSIM 相关建议
        if ssim_results.get("available"):
            avg_ssim = ssim_results.get("avg_ssim", 0)
            sample_count = ssim_results.get("sample_count", 0)
            if avg_ssim >= 0.98:
                suggestions.append({
                    "type": "success",
                    "text": f"✅ 平均 SSIM {avg_ssim:.4f}（抽样 {sample_count} 张），画质几乎无损"
                })
            elif avg_ssim >= SSIM_MIN_THRESHOLD:
                suggestions.append({
                    "type": "success",
                    "text": f"✅ 平均 SSIM {avg_ssim:.4f}（抽样 {sample_count} 张），画质良好"
                })
            else:
                suggestions.append({
                    "type": "warning",
                    "text": f"⚠️ 平均 SSIM {avg_ssim:.4f}（抽样 {sample_count} 张），低于阈值 {SSIM_MIN_THRESHOLD}，建议提高质量参数"
                })
        
        if negative_count > total_images * 0.3:
            suggestions.append({
                "type": "warning",
                "text": f"⚠️ {negative_count} 个文件 HEIC 比原文件更大，建议降低质量参数或检查源文件是否已高度压缩"
            })
        if low_ratio_count > total_images * 0.5:
            suggestions.append({
                "type": "info",
                "text": f"ℹ️ 超过一半文件压缩比 <10%，考虑适当提高质量参数以获得更好画质"
            })
        if high_ratio_count > total_images * 0.5:
            suggestions.append({
                "type": "success",
                "text": f"✅ 超过一半文件压缩比 >50%，转换效果优秀！"
            })
        if 20 <= avg_ratio <= 50:
            suggestions.append({
                "type": "success",
                "text": f"✅ 平均压缩比 {avg_ratio:.1f}%，处于理想区间（20-50%）"
            })
    else:
        avg_ratio = 0
        suggestions = [{"type": "info", "text": "没有成功转换的图片"}]
    
    ssim_card = ""
    if ssim_results.get("available"):
        ssim_card = f"""<div class="stat-card">
                <h3>{ssim_results.get("avg_ssim", 0):.4f}</h3>
                <p>平均 SSIM</p>
            </div>"""

    # 生成 HTML
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HEIC 转换报告</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #eee;
            min-height: 100vh;
            padding: 2rem;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{ 
            text-align: center; 
            margin-bottom: 2rem;
            font-size: 2.5rem;
            background: linear-gradient(90deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }}
        .stat-card {{
            background: rgba(255,255,255,0.1);
            border-radius: 1rem;
            padding: 1.5rem;
            text-align: center;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.1);
        }}
        .stat-card h3 {{ font-size: 2rem; color: #667eea; }}
        .stat-card p {{ color: #aaa; margin-top: 0.5rem; }}
        .chart-container {{
            background: rgba(255,255,255,0.05);
            border-radius: 1rem;
            padding: 2rem;
            margin-bottom: 2rem;
            backdrop-filter: blur(10px);
        }}
        .suggestions {{
            background: rgba(255,255,255,0.05);
            border-radius: 1rem;
            padding: 1.5rem;
            backdrop-filter: blur(10px);
        }}
        .suggestion {{
            padding: 1rem;
            margin: 0.5rem 0;
            border-radius: 0.5rem;
            font-size: 1.1rem;
        }}
        .suggestion.success {{ background: rgba(46, 204, 113, 0.2); border-left: 4px solid #2ecc71; }}
        .suggestion.warning {{ background: rgba(241, 196, 15, 0.2); border-left: 4px solid #f1c40f; }}
        .suggestion.info {{ background: rgba(52, 152, 219, 0.2); border-left: 4px solid #3498db; }}
        canvas {{ max-height: 400px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 HEIC 转换报告</h1>
        
        <div class="stats-grid">
            <div class="stat-card">
                <h3>{stats.get("total", 0)}</h3>
                <p>总文件数</p>
            </div>
            <div class="stat-card">
                <h3>{stats.get("converted", 0)}</h3>
                <p>成功转换</p>
            </div>
            <div class="stat-card">
                <h3>{stats.get("heic_larger", 0)}</h3>
                <p>保留原文件</p>
            </div>
            <div class="stat-card">
                <h3>{stats.get("copied", 0)}</h3>
                <p>复制非图片</p>
            </div>
            <div class="stat-card">
                <h3>{avg_ratio:.1f}%</h3>
                <p>平均压缩比</p>
            </div>
            <div class="stat-card">
                <h3>{stats.get("skipped_symlink", 0)}</h3>
                <p>跳过软链接</p>
            </div>
            {ssim_card}
        </div>
        
        <div class="chart-container">
            <h2 style="margin-bottom: 1rem;">压缩比分布</h2>
            <canvas id="barChart"></canvas>
        </div>
        
        <div class="chart-container">
            <h2 style="margin-bottom: 1rem;">处理结果分布</h2>
            <canvas id="pieChart"></canvas>
        </div>
        
        <div class="suggestions">
            <h2 style="margin-bottom: 1rem;">💡 智能分析建议</h2>
            {"".join(f'<div class="suggestion {s["type"]}">{s["text"]}</div>' for s in suggestions)}
        </div>
    </div>
    
    <script>
        // 柱状图：压缩比分布
        new Chart(document.getElementById('barChart'), {{
            type: 'bar',
            data: {{
                labels: {json.dumps([d["label"] for d in bar_data])},
                datasets: [{{
                    label: '图片数量',
                    data: {json.dumps([d["count"] for d in bar_data])},
                    backgroundColor: 'rgba(102, 126, 234, 0.8)',
                    borderColor: 'rgba(102, 126, 234, 1)',
                    borderWidth: 1,
                    borderRadius: 8
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{
                    legend: {{ display: false }}
                }},
                scales: {{
                    y: {{ 
                        beginAtZero: true,
                        grid: {{ color: 'rgba(255,255,255,0.1)' }},
                        ticks: {{ color: '#aaa' }}
                    }},
                    x: {{
                        grid: {{ display: false }},
                        ticks: {{ color: '#aaa' }}
                    }}
                }}
            }}
        }});
        
        // 饼图：处理结果分布
        new Chart(document.getElementById('pieChart'), {{
            type: 'doughnut',
            data: {{
                labels: ['成功转换', '保留原文件', '复制非图片', '跳过软链接'],
                datasets: [{{
                    data: [{stats.get("converted", 0)}, {stats.get("heic_larger", 0)}, {stats.get("copied", 0)}, {stats.get("skipped_symlink", 0)}],
                    backgroundColor: [
                        'rgba(46, 204, 113, 0.8)',
                        'rgba(241, 196, 15, 0.8)',
                        'rgba(52, 152, 219, 0.8)',
                        'rgba(155, 89, 182, 0.8)'
                    ],
                    borderWidth: 0
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{
                    legend: {{
                        position: 'right',
                        labels: {{ color: '#eee', padding: 20, font: {{ size: 14 }} }}
                    }}
                }}
            }}
        }});
    </script>
</body>
</html>'''
    
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write(html)
    
    return REPORT_FILE

# ------------------------------
# 多进程初始化（避免子进程重复输入）
# ------------------------------
def init_worker(src: str, archive: str, progress_file: str, report_file: str):
    global SRC, ARCHIVE, PROGRESS_FILE, REPORT_FILE
    SRC = Path(src)
    ARCHIVE = Path(archive)
    PROGRESS_FILE = Path(progress_file)
    REPORT_FILE = Path(report_file)

# ------------------------------
# 主程序
# ------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='批量图片转 HEIC（macOS sips 版）')
    parser.add_argument('src', nargs='?', help='源文件夹路径（不传则交互输入）')
    parser.add_argument('out', nargs='?', help='输出文件夹路径（不传则默认：源同级 + _archived）')
    parser.add_argument('--threads', type=int, default=None, help='并行进程数（默认：4~8 之间自适应）')
    parser.add_argument('--default-quality', type=int, default=DEFAULT_QUALITY, help='默认 sips formatOptions（不启用 SSIM 校验时使用）')
    parser.add_argument('--quality-ladder', type=str, default=",".join(str(x) for x in QUALITY_LADDER), help='SSIM 校验时的质量阶梯，例如 55,65,75,85,90')
    parser.add_argument('--verify-ssim', action='store_true', help='对每张图进行 SSIM 校验，不达标则自动升档（更慢但更稳）')
    parser.add_argument('--ssim-threshold', type=float, default=SSIM_THRESHOLD, help='SSIM 门槛（配合 --verify-ssim）')
    parser.add_argument('--disallow-alpha-drop', action='store_true', help='只要输入带 alpha，就要求输出也带 alpha（即使 alpha 实际全不透明）')
    args = parser.parse_args()

    # ------------------------------
    # 解析参数 / 兼容交互输入
    # ------------------------------
    src_input = args.src
    while not src_input:
        src_input = input('请输入源文件夹路径: ').strip()
    SRC = Path(src_input).resolve()
    if not SRC.is_dir():
        raise SystemExit('输入路径不存在或不是文件夹')

    ARCHIVE = Path(args.out).resolve() if args.out else (SRC.parent / f'{SRC.name}_archived')
    ARCHIVE.mkdir(parents=True, exist_ok=True)

    if SIPS is None:
        raise SystemExit('未找到 sips（macOS 自带）。请确认系统环境或 PATH。')

    # 并行度
    THREADS = args.threads if args.threads else max(4, min(8, cpu_count()))

    # 质量策略全局覆盖
    DEFAULT_QUALITY = int(args.default_quality)
    QUALITY_LADDER = [int(x) for x in re.split(r'\s*,\s*', args.quality_ladder.strip()) if x]
    VERIFY_SSIM = bool(args.verify_ssim)
    SSIM_THRESHOLD = float(args.ssim_threshold)
    ALLOW_ALPHA_DROP_IF_OPAQUE = (not args.disallow_alpha_drop)

    PROGRESS_FILE = ARCHIVE / '.conversion_progress.json'
    REPORT_FILE = ARCHIVE / 'conversion_report.html'
    # ------------------------------
    # 扫描待处理文件（支持断点续传）
    # ------------------------------
    processed_files = load_progress()
    failed_files: list[Path] = []
    sample_pairs: list[tuple[Path, Path]] = []
    total_stats = {
        "total": 0,
        "converted": 0,
        "heic_larger": 0,
        "copied": 0,
        "skipped_symlink": 0,
        "compression_ratios": [],
    }
    save_interval = 50

    # 收集文件列表（仅文件；若输出目录在源目录内则自动排除）
    all_files: list[Path] = []
    archive_prefix = str(ARCHIVE) + os.sep
    for f in SRC.rglob('*'):
        if not f.is_file():
            continue
        if str(f).startswith(archive_prefix):
            continue
        if str(f) in processed_files:
            continue
        all_files.append(f)

    total_count = len(all_files)
    if total_count == 0:
        print("✅ 没有发现需要处理的新文件（可能都已在进度文件中记录）。")
        print(f"输出目录: {ARCHIVE}")
        print(f"进度文件: {PROGRESS_FILE}")
        sys.exit(0)

    print(f"将处理 {total_count} 个文件（已跳过 {len(processed_files)} 个已处理文件）。")


    with Pool(
        THREADS,
        initializer=init_worker,
        initargs=(str(SRC), str(ARCHIVE), str(PROGRESS_FILE), str(REPORT_FILE))
    ) as pool:
        for i, (stats_result, failed, processed_path) in enumerate(
            tqdm(pool.imap_unordered(process_file, all_files), total=total_count, desc="转换进度", ncols=80)
        ):
            total_stats["total"] += stats_result["total"]
            total_stats["converted"] += stats_result["converted"]
            total_stats["heic_larger"] += stats_result["heic_larger"]
            total_stats["copied"] += stats_result.get("copied", 0)
            total_stats["skipped_symlink"] += stats_result.get("skipped_symlink", 0)
            total_stats["compression_ratios"].extend(stats_result["compression_ratios"])
            
            # 收集 SSIM 测试样本
            if stats_result.get("sample_pair") and SSIM_AVAILABLE:
                sample_pairs.append(stats_result["sample_pair"])
            
            if failed:
                failed_files.append(failed)
            
            # 记录已处理文件
            processed_files.add(processed_path)
            
            # 定期保存进度
            if (i + 1) % save_interval == 0:
                save_progress(processed_files)

    # 最终保存进度
    save_progress(processed_files)

    # ------------------------------
    # SSIM 质量测试（随机抽样）
    # ------------------------------
    ssim_results = {"available": False}
    if SSIM_AVAILABLE and sample_pairs:
        # 随机抽样
        sample_count = min(SSIM_SAMPLE_SIZE, len(sample_pairs))
        sampled = random.sample(sample_pairs, sample_count)
        ssim_results = test_quality_samples([(Path(orig), Path(heic)) for orig, heic in sampled])
        
        if ssim_results.get("available"):
            avg_ssim = ssim_results.get("avg_ssim", 0)
            print(f"\n📊 SSIM 质量评估: {avg_ssim:.4f} (抽样 {ssim_results.get('sample_count')} 张)")
            if avg_ssim < SSIM_MIN_THRESHOLD:
                print(f"⚠️  平均 SSIM 低于阈值 {SSIM_MIN_THRESHOLD}，建议提高质量参数")

    # ------------------------------
    # 统计报告（控制台）
    # ------------------------------
    ratios = total_stats["compression_ratios"]
    print("\n==== 转换统计报告 ====")
    print(f"总文件数: {total_stats['total']}")
    print(f"成功转换 HEIC: {total_stats['converted']}")
    print(f"HEIC 比原文件大/保留原文件: {total_stats['heic_larger']}")
    print(f"非图片文件/已复制: {total_stats['copied']}")
    print(f"跳过软链接: {total_stats['skipped_symlink']}")
    
    if ratios:
        total_images = len(ratios)
        bins_counter = Counter(min(int(r//10), 9) for r in ratios)
        print("压缩比分布:")
        for i in range(10):
            count = bins_counter.get(i, 0)
            print(f" {i*10:.0f}% - {(i+1)*10:.0f}%: {count} 张 ({count/total_images*100:.2f}%)")
        avg_ratio = sum(ratios)/len(ratios)
        print(f"平均压缩比例: {avg_ratio:.2f}%")
        print("理想参考压缩比例（Pixiv 绘画原图经验）: 20~50%")

    # ------------------------------
    # 生成可视化 HTML 报告
    # ------------------------------
    report_path = generate_html_report(total_stats, ratios, ssim_results)
    print(f"\n📊 可视化报告已生成: {report_path}")

    # ------------------------------
    # 保存失败文件列表
    # ------------------------------
    if failed_files:
        fail_log = Path.cwd() / "failed_files.txt"
        with open(fail_log, "w") as f:
            for ff in failed_files:
                f.write(f"{ff}\n")
        print(f"\n部分文件转换失败，已记录到 {fail_log}")

    # 完成后提示
    print(f"\n✅ 处理完成！输出目录: {ARCHIVE}")
    print(f"📄 进度文件: {PROGRESS_FILE}")
    print("   (如需重新处理全部文件，请删除进度文件)")