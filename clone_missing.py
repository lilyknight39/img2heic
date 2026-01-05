#!/usr/bin/env python3
"""
clone_missing.py

从 compare_stems 的差异结果中，选择一侧的所有差异文件并将它们 clone 到目标目录，
保留原始目录结构。尽量使用 macOS 的 clonefile syscall（APFS 克隆），若失败则回退到复制并保留元数据。

用法示例：
  python clone_missing.py --which B dirA dirB /path/to/target

参数：
  --which {A,B}   选择从哪一侧取差异文件并进行克隆：
                  - A: 克隆那些仅存在于 dirA（即缺失于 dirB）的文件
                  - B: 克隆那些仅存在于 dirB（即缺失于 dirA）的文件
  dirA dirB        要比较的两个目录
  target_dir       将文件克隆到此目录下，保留相对路径
  --no-recursive   仅检查顶层文件
  --ignore-case    忽略文件名大小写
  --dry-run        仅显示将要操作的文件，不做实际复制
  --verbose        输出过程信息

"""
from __future__ import annotations
import argparse
import ctypes
import errno
import hashlib
import os
import shutil
from pathlib import Path
from typing import Iterable

try:
    from compare_stems import stems
except Exception:
    # 如果模块路径不同，尝试相对导入（脚本与 compare_stems.py 同目录时正常）
    from .compare_stems import stems  # type: ignore


def sha1(path: Path, chunk=1 << 20) -> str:
    h = hashlib.sha1()
    with path.open('rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def try_clonefile(src: str, dst: str) -> bool:
    """尝试使用 macOS clonefile syscall 进行克隆。返回 True 表示已成功使用 clone。"""
    try:
        libc = ctypes.CDLL('libc.dylib', use_errno=True)
        # int clonefile(const char *src, const char *dst, clonefile_flags_t flags);
        clone = libc.clonefile
        clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        clone.restype = ctypes.c_int
        res = clone(src.encode('utf-8'), dst.encode('utf-8'), 0)
        if res == 0:
            return True
        # 若失败，errno 可用
        err = ctypes.get_errno()
        if err == errno.ENOSYS:
            return False
        return False
    except Exception:
        return False


def make_unique(dst: Path) -> Path:
    """如果目标文件已存在，生成一个不会覆盖现有文件的唯一路径（在文件名末尾添加 -clone-N）。"""
    if not dst.exists():
        return dst
    parent = dst.parent
    name = dst.stem
    suff = dst.suffix
    n = 1
    while True:
        candidate = parent / f"{name}-clone-{n}{suff}"
        if not candidate.exists():
            return candidate
        n += 1


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def clone_or_copy(src: Path, dst: Path, dry_run=False, verbose=False) -> Path:
    """尝试 clonefile，失败则 copy2。若 dst 存在且内容相同则跳过。返回实际写入的目标路径或原有路径。"""
    if dst.exists():
        try:
            if src.stat().st_size == dst.stat().st_size and sha1(src) == sha1(dst):
                if verbose:
                    print(f"SKIP identical: {dst}")
                return dst
        except Exception:
            pass

    ensure_dir(dst.parent)

    if dry_run:
        print(f"DRY RUN copy: {src} -> {dst}")
        return dst

    # 先尝试 clone
    if try_clonefile(str(src), str(dst)):
        if verbose:
            print(f"CLONED: {src} -> {dst}")
        return dst

    # clone 失败，使用 copy2
    tmp = dst
    if dst.exists():
        tmp = make_unique(dst)

    shutil.copy2(str(src), str(tmp))
    if verbose:
        if tmp == dst:
            print(f"COPIED: {src} -> {dst}")
        else:
            print(f"COPIED -> (unique): {src} -> {tmp}")
    return tmp


def gather_targets(which: str, dirA: Path, dirB: Path, recursive=True, case_sensitive=True):
    A = stems(dirA, recursive=recursive, case_sensitive=case_sensitive)
    B = stems(dirB, recursive=recursive, case_sensitive=case_sensitive)
    keysA = set(A.keys())
    keysB = set(B.keys())
    if which == 'A':
        # 选择那些在 A 中但不在 B 中 -> 从 A 克隆
        missing_keys = sorted(keysA - keysB)
        # 返回 list of (src_path, src_root)
        items = []
        for k in missing_keys:
            for p in A.get(k, []):
                items.append((p, dirA))
        return items
    else:
        missing_keys = sorted(keysB - keysA)
        items = []
        for k in missing_keys:
            for p in B.get(k, []):
                items.append((p, dirB))
        return items


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--which', choices=['A', 'B'], required=True, help='从哪一侧克隆差异文件 (A or B)')
    p.add_argument('dirA', type=Path)
    p.add_argument('dirB', type=Path)
    p.add_argument('target', type=Path)
    p.add_argument('--no-recursive', dest='recursive', action='store_false')
    p.add_argument('--ignore-case', dest='case_sensitive', action='store_false')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--verbose', '-v', action='store_true')
    args = p.parse_args(list(argv) if argv is not None else None)

    dirA = args.dirA.expanduser().resolve()
    dirB = args.dirB.expanduser().resolve()
    target = args.target.expanduser().resolve()

    if not dirA.exists() or not dirB.exists():
        print('dirA 或 dirB 不存在')
        return 2

    items = gather_targets(args.which, dirA, dirB, recursive=args.recursive, case_sensitive=args.case_sensitive)
    if not items:
        print('没有找到差异文件，退出。')
        return 0

    for src_path, src_root in items:
        # 计算相对于源根的相对路径，并将其放入 target 下
        try:
            rel = src_path.relative_to(src_root)
        except Exception:
            # 如果无法 relative_to，则用文件名直接放到 target 根下
            rel = Path(src_path.name)

        dst = target.joinpath(rel)
        # 若目标路径与源所在目录有重合（例如 target 在 dirA/dirB 子树中），也按相对路径写入
        clone_or_copy(src_path, dst, dry_run=args.dry_run, verbose=args.verbose)

    print('完成。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
