#!/usr/bin/env python3
"""Transcribe a medical audio file to French ({stem}-cohere.txt) using the
official Cohere Transcribe model (CohereLabs/cohere-transcribe-03-2026).

The output is the RAW ASR transcript (reflecting the model's phonetic/letter
garble) -- the intended input to match_meds.py, which normalizes drug names.
"""
import sys

from mlx_audio.stt.utils import load_model

MODEL = "CohereLabs/cohere-transcribe-03-2026"


def main(path: str):
    import time

    print(f"[i] loading {MODEL} ...", flush=True)
    model = load_model(MODEL)
    print("[i] transcribing ...", flush=True)
    t0 = time.time()
    out = model.generate(audio=path, language="fr", punctuation=True)
    text = out.text
    print(f"[i] done in {time.time() - t0:.1f}s", flush=True)

    stem = path.rsplit(".", 1)[0]
    outpath = f"{stem}-cohere.txt"
    with open(outpath, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[i] wrote {outpath} ({len(text.split())} words)")


if __name__ == "__main__":
    main(sys.argv[1])
