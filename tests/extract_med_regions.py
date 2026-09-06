#!/usr/bin/env python3
"""Extrait les régions médicaments de chaque consultation ayant une conf_map.

Pour chaque consultation, identifie les régions « liste de médicaments » via
``_region_medlist``, étend de ±10 mots de contexte, et produit un fichier
JSON avec les tokens, leur statut (médicament / contexte) et la confiance STT.

Usage (dans le conteneur) :
    python3 tests/extract_med_regions.py
"""
import json
import os
import sqlite3
import sys

# Ajouter le répertoire app au path
sys.path.insert(0, "/app")

from app.med_grounding import _region_medlist, norm_phon

DB_PATH = "/data/consultai.db"
OUT_DIR = "/tmp/med_regions"
CONTEXT = 10  # mots avant/après


def conf_for_token(token, conf_map):
    """Trouve la confiance STT d'un token via norm_phon."""
    if not conf_map:
        return None
    key = norm_phon(token)
    if not key:
        return None
    return conf_map.get(key)


def extract_regions(text, conf_map_json):
    """Extrait les régions médicaments avec contexte ±10 mots."""
    conf_map = json.loads(conf_map_json) if conf_map_json else {}
    words = text.split()
    n = len(words)

    # Indices des tokens dans les régions médicaments confirmées
    region_idxs = _region_medlist(text)
    if not region_idxs:
        return None

    # Grouper les indices consécutifs en runs
    sorted_idxs = sorted(region_idxs)
    runs = []
    current_run = [sorted_idxs[0]]
    for idx in sorted_idxs[1:]:
        if idx == current_run[-1] + 1:
            current_run.append(idx)
        else:
            runs.append(current_run)
            current_run = [idx]
    runs.append(current_run)

    # Pour chaque run, étendre de ±CONTEXT mots
    results = []
    for run in runs:
        start = max(0, run[0] - CONTEXT)
        end = min(n, run[-1] + CONTEXT + 1)
        region_set = set(run)

        tokens = []
        for i in range(start, end):
            w = words[i]
            tokens.append({
                "idx": i,
                "token": w,
                "in_region": i in region_set,
                "conf": conf_for_token(w, conf_map),
            })

        results.append({
            "token_start": run[0],
            "token_end": run[-1],
            "context_start": start,
            "context_end": end - 1,
            "region_text": " ".join(words[run[0]:run[-1] + 1]),
            "full_text": " ".join(words[start:end]),
            "tokens": tokens,
        })

    return results


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    rows = conn.execute(
        "SELECT id, raw_transcript, normalized_transcript, transcript_conf, "
        "title, inline_fixed_json "
        "FROM consultations "
        "WHERE transcript_conf IS NOT NULL AND transcript_conf != '' "
        "ORDER BY id"
    ).fetchall()

    print(f"Consultations avec conf_map : {len(rows)}")
    count = 0

    for cid, raw, normalized, conf_json, title, inline_json in rows:
        # Utiliser normalized_transcript (celui envoyé au LLM)
        text = normalized or raw
        if not text:
            continue

        regions = extract_regions(text, conf_json)
        if not regions:
            print(f"  #{cid}: pas de région médicaments")
            continue

        # Charger les items méd_grounding pour référence
        conn2 = sqlite3.connect(DB_PATH)
        mg_row = conn2.execute(
            "SELECT med_grounding_json FROM consultations WHERE id=?", (cid,)
        ).fetchone()
        conn2.close()
        mg_items = json.loads(mg_row[0]) if mg_row and mg_row[0] else []

        output = {
            "consultation_id": cid,
            "title": title or "",
            "inline_fixed": json.loads(inline_json) if inline_json else [],
            "med_grounding_items": mg_items,
            "nb_regions": len(regions),
            "regions": regions,
        }

        out_path = os.path.join(OUT_DIR, f"consult{cid}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        region_sizes = [r["token_end"] - r["token_start"] + 1 for r in regions]
        print(f"  #{cid}: {len(regions)} région(s), tailles {region_sizes}")
        count += 1

    conn.close()
    print(f"\n{count} fichiers écrits dans {OUT_DIR}")


if __name__ == "__main__":
    main()
