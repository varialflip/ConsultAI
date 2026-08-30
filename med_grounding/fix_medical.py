#!/usr/bin/env python3
"""Post-process Cohere Transcribe output: fix phonetic medical terms."""
import re
import sys
from pathlib import Path

GLOSSARY = [
    (r"tyrénol l'A6 20 mg perotidien, puis celle-là, 10 mg perotidien",
     "Tylenol, Lasix 20 mg po die, Celexa 10 mg po die"),
    (r"tyrénol", "Tylenol"),
    (r"l'A6", "(acétaminophène)"),
    (r"celle-là", "Celexa"),
    (r"perotidien", "per os die"),
    (r"sous-diète", "type 2"),
    (r"Trendate", "Trandate"),
    (r"Aricepte", "Aricept"),
    (r"l'excellent", "l'Exelon"),
    (r"excellente", "Exelon"),
    (r"maxérant", "Maxeran"),
    (r"maxéran", "Maxeran"),
    (r"méthoprolol", "métoprolol"),
    (r"pantholoque", "pantoloc"),
    (r"pantoloque", "pantoloc"),
    (r"dipitar", "Lipitor"),
    (r"démence mixte Reisberg quatre", "démence mixte de stade 4 de Reisberg"),
    (r"Rivastigmine", "rivastigmine"),
]


def fix(text: str) -> str:
    for pattern, repl in GLOSSARY:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return text


def main() -> None:
    for path in sys.argv[1:]:
        p = Path(path)
        if not p.exists():
            print(f"skip (missing): {p}")
            continue
        original = p.read_text(encoding="utf-8")
        corrected = fix(original)
        out = p.with_name(p.stem + "-fixed.txt")
        out.write_text(corrected, encoding="utf-8")
        print(f"wrote: {out}")


if __name__ == "__main__":
    main()
