#!/usr/bin/env python3
"""Corrige le drapeau `is_otc` des marques annulées dans meds.sqlite.

Historique du bogue : `build_db.py` chargeait les produits BDP inactifs
(`dpd_ia`) en testant `schedule == "OTC"`, alors que l'extrait des produits
annulés écrit `"NON-PRESCRIPTION DRUGS"` pour les produits sans ordonnance.
Résultat : toutes les marques OTC annulées avaient `is_otc=0` et le moteur
sortait leur nom de marque au lieu du principe actif (contrairement aux OTC
commercialisés).

`fix_inactive_otc.py` répare la base **déjà construite** (la correction
définitive vit dans `build_db.py`, pour les reconstructions futures). Il est
idempotent et dry-runnable comme `prune_db.py` / `ban_terms.py`.

Règle : pour chaque marque `level='BRAND'` d'origine `DPD_CANCELLED`, on lit
ses codes de drogue BDP via les noms EN/FR nettoyés ; si **tous** ses codes
sont étiquetés sans-ordonnance (`OTC` / `NON-PRESCRIPTION DRUGS`), `is_otc=1`,
si **aucun** ne l'est, `is_otc=0` (marques à codes mixtes : intactes).
"""
import re
import sqlite3
import sys
import unicodedata

from build_db import clean_presentation, read_dpd_table

DB = "./meds.sqlite"
DPD_IA = "dpd_ia"

OTC_SCHEDULES = {"OTC", "NON-PRESCRIPTION DRUGS"}


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def main() -> None:
    dry = "--apply" not in sys.argv
    conn = sqlite3.connect(DB)

    # code de drogue -> étiquettes de schedule (BDP produits annulés/inactifs)
    sched_by_code = {}
    for r in read_dpd_table(f"{DPD_IA}/schedule_ia.txt"):
        if len(r) >= 2:
            sched_by_code.setdefault(r[0].strip(), set()).add(r[1].strip())

    # nom normalisé (EN/FR nettoyés) -> codes de drogue
    name_to_codes = {}
    for r in read_dpd_table(f"{DPD_IA}/drug_ia.txt"):
        if len(r) < 6 or r[2].strip() != "Human":
            continue
        code = r[0].strip()
        names = {r[4].strip()}
        if len(r) > 5:
            names.add(r[5].strip())
        for nm in names:
            if not nm:
                continue
            for key in {norm(nm), norm(clean_presentation(nm))}:
                key = re.sub(r"\s+", " ", key).strip()
                if key:
                    name_to_codes.setdefault(key, set()).add(code)

    # marques DPD_CANCELLED -> (id, brand_name) et leur is_otc courant
    rows = conn.execute(
        "SELECT id, brand_name, is_otc FROM medications "
        "WHERE level='BRAND' AND source='DPD_CANCELLED'"
    ).fetchall()

    todo = []          # (id, brand, is_otc_cible)
    mixed = 0
    no_code = 0
    for mid, brand, cur_otc in rows:
        codes = name_to_codes.get(norm(brand), set())
        if not codes:
            no_code += 1
            continue
        flags = []
        for c in codes:
            if c in sched_by_code:
                flags.append(bool(sched_by_code[c] & OTC_SCHEDULES))
        if not flags:
            continue
        is_otc = 1 if all(flags) else 0 if not any(flags) else None
        if is_otc is not None and is_otc != cur_otc:
            todo.append((mid, brand, is_otc))

    n_otc = sum(1 for _, _, v in todo if v == 1)
    n_non = sum(1 for _, _, v in todo if v == 0)
    print(f"marques DPD_CANCELLED à corriger : {len(todo)} "
          f"(dont {n_otc} -> is_otc=1, {n_non} -> is_otc=0)")
    print(f"  (codes mixtes laissés intacts : {mixed}; sans code BDP rattaché : {no_code})")
    for mid, brand, v in todo[:15]:
        print(f"  - {brand[:44]:44} -> is_otc={v}")

    if dry:
        print("[dry-run] passe --apply pour appliquer")
        conn.close()
        return

    conn.execute("BEGIN")
    conn.executemany(
        "UPDATE medications SET is_otc=? WHERE id=?", [(v, m) for m, _, v in todo]
    )
    conn.commit()
    vo = conn.execute("SELECT COUNT(*) FROM medications WHERE is_otc=1").fetchone()[0]
    vm = conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0]
    print(f"après : medications={vm}, is_otc=1 : {vo}")
    conn.close()


if __name__ == "__main__":
    main()