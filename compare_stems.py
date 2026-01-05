#!/usr/bin/env python3
import os, sys, re
from pathlib import Path

# 匹配一个或多个以点开头且属于 jpg/jpeg/png/heic 的后缀（支持连写，如 .png.heic）
EXT_RE = re.compile(r'(?:\.(?:jpg|jpeg|png|heic))+$', re.IGNORECASE)

def stems(root: Path, recursive=True, case_sensitive=True):
    """
    返回一个映射: normalized_stem -> list[Path]
    normalized_stem 根据 case_sensitive 做小写或保持原样。值为原始 Path 对象。
    """
    d = {}
    it = root.rglob("*") if recursive else root.glob("*")
    for p in it:
        if p.is_file():
            # 使用正则去掉已知的扩展名序列（例如 .png.heic 或 .JPG）
            stem = EXT_RE.sub('', p.name)
            key = stem if case_sensitive else stem.lower()
            d.setdefault(key, []).append(p)
    return d

def main():
    if len(sys.argv) < 3:
        print("Usage: compare_stems.py <dirA> <dirB> [--no-recursive] [--ignore-case]")
        sys.exit(2)

    dirA = Path(sys.argv[1]).expanduser()
    dirB = Path(sys.argv[2]).expanduser()
    recursive = "--no-recursive" not in sys.argv
    case_sensitive = "--ignore-case" not in sys.argv

    A = stems(dirA, recursive=recursive, case_sensitive=case_sensitive)
    B = stems(dirB, recursive=recursive, case_sensitive=case_sensitive)

    keysA = set(A.keys())
    keysB = set(B.keys())

    missing_in_A = sorted(keysB - keysA)
    missing_in_B = sorted(keysA - keysB)

    print(f"Only in {dirB} (missing in {dirA}): {len(missing_in_A)}")
    for stem in missing_in_A[:200]:
        # 输出每个匹配该 stem 的完整路径（绝对路径）
        paths = sorted(str(p.resolve()) for p in B.get(stem, []))
        for p in paths:
            print(p)
    if len(missing_in_A) > 200:
        print(f"... ({len(missing_in_A)-200} more stems)")

    print()
    print(f"Only in {dirA} (missing in {dirB}): {len(missing_in_B)}")
    for stem in missing_in_B[:200]:
        paths = sorted(str(p.resolve()) for p in A.get(stem, []))
        for p in paths:
            print(p)
    if len(missing_in_B) > 200:
        print(f"... ({len(missing_in_B)-200} more stems)")

if __name__ == "__main__":
    main()
