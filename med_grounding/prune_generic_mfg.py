#!/usr/bin/env python3
"""Déduplique les marques de fabricants génériques dans meds.sqlite.

Les marques préfixées de fabricants (APO-, TEVA-, PMS-, SANDOZ-, MYLAN-,
JAMP-…) encombrent la base : pour une même molécule (« furosemide ») on
retrouve souvent plusieurs lignes (APO-FUROSEMIDE, TEVA-FUROSEMIDE, …), alors
qu'un clinicien dicte le nom de **principale activ**, pas le suffixe du
fabricant — et que la molécule dispose déjà d'une ligne générique propre
(`BASE_GENERIC` / `FULL_GENERIC`) qui résout la dictée.

Ce script ne touche que les marques **préfixées de fabricant**
(`level='BRAND'`, premier mot dans `MFG_PREFIXES`). Pour chaque molécule
redondante (dont la substance épurée du préfixe est couverte par une ligne
générique), on ne garde qu'**une ligne représentative**. Les lignes qui sont
l'**unique représentant de leur produit** (combinaisons type OXYCOCET,
TECNAL, TRIAZIDE — sans ligne générique de secours) sont **laissées intactes**,
ainsi que toute cible d'un alias `STT_GARBLE` seedé (jamais retirée).

Une **garde** vérifie avant application que :
- aucune cible de garble seedé (même préfixée) n'est retirée ;
- chaque molécule redondante conserve sa résolution via sa ligne générique
  (garantie par construction : on ne retire jamais les génériques).

Idempotent / dry-runnable comme prune_otc.py, prune_scope.py, ban_terms.py.
"""
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict

DB = "./meds.sqlite"

#: Marques de fabricants de génériques (premier mot du nom de marque).
MFG_PREFIXES = {
    "apo", "pms", "teva", "mylan", "sandoz", "jamp", "mint", "act", "auro",
    "baxter", "dom", "glenmark", "mar", "pharmascience", "ranbaxy", "ratio",
    "taro", "zydus", "accord", "apotex", "aa", "biomed", "medley", "pro doc",
    "sivem", "sab", "stanton",
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def first_word(n: str) -> str:
    x = norm(n)
    return x.split()[0] if x else ""


def is_mfg(brand: str) -> bool:
    return first_word(brand).strip("-") in MFG_PREFIXES


def strip_mfg(name: str) -> str:
    """Retire le préfixe fabricant d'un nom normalisé (« teva furosemide » →
    « furosemide », « TEVA-FUROSEMIDE » → « furosemide »). La normalisation
    tourne tout séparateur (tiret, espace) en espace, donc un seul cas à traiter.
    """
    n = norm(name)
    first = first_word(n)
    if not first:
        return ""
    if n.startswith(first + " "):
        return n[len(first) + 1:].strip()
    if n == first:
        return ""
    return n


def main() -> None:
    dry = "--apply" not in sys.argv
    conn = sqlite3.connect(DB)

    rows = conn.execute(
        "SELECT id, brand_name, base_generic FROM medications "
        "WHERE level='BRAND'"
    ).fetchall()
    seeds = {m for (m,) in conn.execute(
        "SELECT medication_id FROM medication_aliases "
        "WHERE alias_type='STT_GARBLE'"
    )}

    mfg = [r for r in rows if is_mfg(r[1])]

    # base_generic normalisé des lignes génériques (résolution par substance)
    generic_norm = {
        norm(b)
        for (b,) in conn.execute(
            "SELECT base_generic FROM medications "
            "WHERE level IN ('BASE_GENERIC', 'FULL_GENERIC')"
        )
    }
    generic_norm.discard("")

    # ---------- molécule propre de chaque marque préfixée ----------
    # la molécule se lit préférentiellement dans brand épuré (plus fiable),
    # sinon dans base_generic épuré.
    info = {}  # id -> (molécule, brand, base)
    for mid, brand, base in mfg:
        mol = strip_mfg(brand) or strip_mfg(base)
        info[mid] = (mol, brand, base)

    # group by molécule (normalisée)
    by_mol = defaultdict(list)
    for mid, (mol, brand, base) in info.items():
        by_mol[mol].append((mid, brand, base))

    # redondant = la molécule est couverte par une ligne générique propre
    redundant = {}
    unique = set()
    for mol, items in by_mol.items():
        if mol in generic_norm and len(items) >= 1:
            redundant[mol] = items
        else:
            unique.update(m[0] for m in items)

    # ---------- choix du représentant par molécule redondante ----------
    drop = set()
    for mol, items in redundant.items():
        items.sort(key=lambda r: (
            0 if r[0] in seeds else 1,   # cibles des grables d'abord
            0 if strip_mfg(r[1]) == mol else 1,  # marque épurée == molécule
            len(norm(r[1])),              # puis nom le plus court
        ))
        representants = [r for r in items if r[0] in seeds] or [items[0]]
        keep = {r[0] for r in representants}
        drop.update(r[0] for r in items if r[0] not in keep)

    # ---------- garde ----------
    mfg_ids = {r[0] for r in mfg}
    lost_seed = seeds & mfg_ids & drop
    if lost_seed:
        print(f"[garde] ÉCHEC : grables seedés préfixés perdus "
              f"{sorted(map(str, lost_seed))}")
        conn.close()
        sys.exit(2)

    total_before = conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0]
    alias_before = conn.execute("SELECT COUNT(*) FROM medication_aliases").fetchone()[0]

    n_mfg = len(mfg)
    n_red_mol = len(redundant)
    print(f"marques préfixées fabricant : {n_mfg}")
    print(f"  redondantes (molécule couverte par un générique) : {len(redundant)}")
    print(f"  conservées (intactes)                             : {len(unique)}")
    print(f"  dédupliquées en 1 représentant/molécule           : {len(drop)}")
    print(f"  → médicaments {total_before} → {total_before - len(drop)} (estimé)")

    if dry:
        print("[dry-run] garde OK, passe --apply pour appliquer")
        conn.close()
        return

    conn.execute("BEGIN")
    ph = ",".join("?" * len(drop))
    conn.execute(
        "DELETE FROM medication_aliases WHERE medication_id IN (%s)" % ph,
        sorted(drop),
    )
    conn.execute(
        "DELETE FROM medications WHERE id IN (%s)" % ph, sorted(drop),
    )
    conn.commit()
    vm = conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0]
    va = conn.execute("SELECT COUNT(*) FROM medication_aliases").fetchone()[0]
    print(f"après : medications={vm} (avant {total_before}), "
          f"aliases={va} (avant {alias_before})")
    conn.close()


if __name__ == "__main__":
    main()
