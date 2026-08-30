#!/usr/bin/env python3
"""Diff raw Cohere transcript vs normalized file, sentence by sentence."""
import re
import sys
from difflib import SequenceMatcher


def sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def main():
    raw_path, norm_path = sys.argv[1], sys.argv[2]
    raw = open(raw_path, encoding="utf-8").read()
    norm = open(norm_path, encoding="utf-8").read()
    print(f"### {raw_path}  vs  {norm_path}\n")
    for r, n in zip(sentences(raw), sentences(norm)):
        if r != n:
            sm = SequenceMatcher(None, r, n)
            print("CHANGED SENTENCE:")
            print(f"  RAW : {r}")
            print(f"  NEW : {n}")
            print("  ops:")
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag != "equal":
                    print(f"    {tag:8s} {r[i1:i2]!r} -> {n[j1:j2]!r}")
            print()


if __name__ == "__main__":
    main()
