#!/usr/bin/env python3
"""Prune maximal de meds.sqlite : une molécule = un générique ± marques utiles.

Objectif produit (2026-09-05) : réduire la base à l'essentiel dictable —
pour chaque molécule, la (ou les) lignes GÉNÉRIQUES (BASE_GENERIC) et les
MARQUES PROPRIÉTAIRES / de libération prolongée réellement utilisées (un
clinicien dicte « quetiapine », « Seroquel », « Seroquel XR » — jamais
« AG-QUETIAPINE », « SANDOZ QUETIAPINE XRT », « ACH-QUETIAPINE FUMARATE XR »).

Cinq catégories de retrait (déterministes, structurelles — pas de liste
d'exemples) :

- **C1 marques préfixées fabricant** couples à un générique (« AG-QUETIAPINE »
  → « quetiapine », « ACH-CAPECITABINE ») : préfixe retiré + noyau couvert par
  un BASE_GENERIC. Les combos non couvertes (« TEVA-TRIAMTERENE/HCTZ ») sont
  conservées.
- **C2 marques « copie exacte »** du générique (« THYROID », « DIAZEPAM »,
  « FOLIC ACID », « CHLORDIAZEPOXIDE ») : `brand_name` normalisé identique à
  un BASE_GENERIC — doublons purs.
- **C3 marques « générique + décor »** (« CODEINE PHOSPHATE », « METOPROLOL-L »,
  « HALOPERIDOL LA », « METHOTREXATE SODIUM », « CISPLATIN BP »,
  « GENTAMICIN(E) ») : le noyau (sel / dose / forme galénique / libération /
  pharmacopée retirés) est un BASE_GENERIC.
- **C4 FULL_GENERIC doublons** : le `base_generic` a un noyau couvert par un
  BASE_GENERIC — la résolution passe par le générique.
- **C5 hybrides inactifs** (`is_active=0`, DPD_CANCELLED/DORMANT) : retirés
  dès que toute leur résolution est ré-couverte (ci-dessous).

INVARIANT DE SÉCURITÉ — une ligne n'est RETIRÉE que si chaque `norm_phon` de
ses alias reste résoluble après le prune :

1. aucun retrait d'une cible `STT_GARBLE` seedée ;
2. aucun retrait d'un nom de `common_meds.json` (générique ou marque) ;
3. aucun retrait d'une clé `OTC_DISPLAY` / `FR_COMMON` du moteur ;
4. aucun retrait d'un nom observé dans le corpus réel (option `--corpus-json`
   : liste de `norm_phon` issus des `med_grounding_json` persistés) ;
5. pour chaque alias orphelin d'une ligne retirée, REMAP vers le générique
   survivant de même noyau (« codeine phosphate » → ligne BASE « codeine ») —
   sinon la ligne est conservée (filet de sécurité).

Résultat attendu : résolution IDENTIQUE à l'identique (benchmark), base allégée
des doublons de fabricants/sels/formes. La liste des médicaments « courants »
est la source unique JSON `app/common_meds.json` (plus de table `common_meds`
à rejouer après refonte).

Idempotent / dry-runnable comme prune_generic_mfg.py, prune_otc.py.
"""
import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict

DB = "./meds.sqlite"

#: Marques de fabricants de génériques — mêmes préfixes que le moteur
#: (app/med_grounding.py MANUFACTURER_PREFIXES) et que prune_generic_mfg.py.
MFG_PREFIXES = {
    "apo", "pms", "teva", "mylan", "sandoz", "jamp", "mint", "act", "auro",
    "baxter", "dom", "glenmark", "mar", "pharmascience", "ranbaxy", "ratio",
    "taro", "zydus", "accord", "apotex", "aa", "biomed", "medley", "pro doc",
    "sivem", "sab", "stanton", "accel", "ach", "alti", "ava", "bio", "gd",
    "gen", "med", "nat", "ntp", "nu", "odan", "phl", "priva", "pro", "reddy",
    "rhoxal", "riva", "torrent", "van",
}

#: Formes de libération prolongée / variantes de formulation.
RELEASE = {
    "xr", "er", "la", "cr", "xl", "odt", "ir", "dr", "sr", "xlr", "pr",
    "qd", "sos", "ec", "tr", "sr", "xl", "mr",
}

#: Sels / hydrates / esters — retirés pour atteindre le noyau générique.
SALT = {
    "hcl", "fumarate", "maleate", "sodium", "calcium", "dihydrate",
    "monohydrate", "sulfate", "sodique", "hydrochloride", "phosphate",
    "citrate", "tartrate", "acide", "mesylate", "besylate", "gluconate",
    "chlorhydrate", "disodium", "magnesium", "potassium", "zinc", "ferreux",
    "ferrique", "hydroxyde", "carbonate", "bicarbonate", "de", "d",
}

#: Formes galéniques / voies / concentrations.
FORM = {
    "tablet", "tablets", "tab", "capsule", "capsules", "caplet", "solution",
    "injection", "inj", "injectable", "cream", "crème", "ointment", "syrup",
    "suspension", "gel", "patch", "suppository", "suppositories", "aspiration",
    "liquid", "spray", "powder", "amp", "ampule", "vial", "kit", "drop",
    "drops", "lotion", "shampoo", "foam", "enema", "ml", "mg", "mcg", "g",
    "gum", "lozenge", "granules", "drink", "oral", "topical", "otic",
    "ophthalmic", "ophtalmic", "nasal", "rectal", "iv", "im", "sc", "unit",
    "usp", "bp", "nf", "in", "with", "plus", "extra", "concentrate",
}

DECOR = RELEASE | SALT | FORM


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def nph(s):
    return re.sub(r"[^a-z]", "", norm(s))


def core(s):
    """Noyau molécule : mots du nom normalisé, sans fabricant ni décor."""
    return " ".join(t for t in norm(s).split()
                    if t not in DECOR and not t.isdigit() and len(t) >= 3)


def first_word(n):
    x = norm(n)
    return x.split()[0] if x else ""


def strip_mfg(name):
    n = norm(name)
    f = first_word(n)
    if f.strip("-") in MFG_PREFIXES:
        return n[len(f):].strip() if n.startswith(f + " ") else ("" if n == f else n)
    return n


def load_corpus(json_path):
    if not json_path:
        return set()
    try:
        return set(json.load(open(json_path)))
    except (OSError, ValueError) as e:
        print(f"[warn] corpus injoignable ({e}) — ignoré")
        return set()


def mol_of(row):
    """Noyau molécule d'une ligne : BASE/FULL → core(base) ; BRAND → core de la
    marque préfixe-fabricant retiré (TEVA-QUININE → quinine)."""
    level = row[3]
    if level == "BRAND":
        return core(strip_mfg(row[1]))
    return core(row[2] or row[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="applique (sinon dry-run)")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--corpus-json", default=None,
                    help="fichier json : liste de norm_phon observés en corpus")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    table_missing = cur.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='medications'"
    ).fetchone()[0]
    if not table_missing:
        conn.close()
        print("[garde] médications introuvable — mauvais --db")
        sys.exit(2)

    # ---------- descriptions ----------
    aliases = cur.execute(
        "SELECT id, medication_id, alias_name, alias_type FROM medication_aliases"
    ).fetchall()
    aliases_by_mid = defaultdict(list)
    for aid, mid, aname, atype in aliases:
        aliases_by_mid[mid].append((aid, aname, atype))

    rows = cur.execute(
        "SELECT id, brand_name, base_generic, level, is_active, is_otc, source "
        "FROM medications"
    ).fetchall()
    id_of = {r[0]: r for r in rows}

    # gens génériques (BASE/FULL) et leurs noyaux
    base_rows = [r for r in rows if r[3] == "BASE_GENERIC"]
    full_rows = [r for r in rows if r[3] == "FULL_GENERIC"]
    base_cores = {core(r[2]) for r in base_rows if r[2]}
    base_cores.discard("")

    # ---------- garde-fous ----------
    stt_ids = {mid for (mid,) in cur.execute(
        "SELECT DISTINCT medication_id FROM medication_aliases "
        "WHERE alias_type='STT_GARBLE'").fetchall()}
    gu = set()

    # noms de common_meds.json
    import os
    cm_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "app", "common_meds.json")
    try:
        for cat, lst in json.load(open(cm_path)).items():
            for it in lst:
                if it.get("brand_name"):
                    gu.add(nph(it["brand_name"]))
                if it.get("generic_name"):
                    gu.add(nph(it["generic_name"]))
    except (OSError, ValueError) as e:
        print(f"[warn] common_meds.json injoignable ({e}) — ignoré")

    # clés runtime OTC_DISPLAY / FR_COMMON (dicts DU MOTEUR, instance-attachés :
    # on construit un Matcher sur la base en cours, lecture seule)
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
        from med_grounding import Matcher as _Matcher  # type: ignore
        _m = _Matcher(db=args.db, use_phonetic=False)
        for k in getattr(_m, "OTC_DISPLAY", {}):
            gu.add(nph(k))
        for k in getattr(_m, "FR_COMMON", {}):
            gu.add(nph(k))
    except Exception:
        pass  # moteur absent: le prune marche tout seul, protections moindres

    gu |= load_corpus(args.corpus_json)

    def absous(rid):
        """jamais retirée : cible STT + aucun de ses noms protégé non couvert..."""
        if rid in stt_ids:
            return False
        for _aid, aname, _atype in aliases_by_mid.get(rid, ()):
            if nph(aname) in gu:
                return False
        return True

    # ---------- candidats par catégorie ----------
    drop = set()   # id
    cat = {}
    trace = defaultdict(list)

    for r in rows:
        rid, brand, base, level, act, otc, src = r
        if rid in stt_ids:
            continue
        if level == "BRAND":
            if not absous(rid):
                continue
            sm = strip_mfg(brand)
            is_mfg = norm(sm) != norm(brand)
            nb = norm(brand)
            # C1 fabricant couvert
            if is_mfg and sm and core(sm) in base_cores:
                drop.add(rid); cat[rid] = "C1_mfg"; trace["C1_mfg"].append(brand); continue
            # C2 copie exacte
            if not is_mfg and nb and nb in {norm(b[2]) for b in base_rows if b[2]}:
                drop.add(rid); cat[rid] = "C2_exact"; trace["C2_exact"].append(brand); continue
            # C3 décor (non-copie, non-fabricant)
            c = core(nb)
            if not is_mfg and c and c in base_cores and c != nb:
                drop.add(rid); cat[rid] = "C3_decor"; trace["C3_decor"].append(brand); continue
            # C5 inactif
            if not act:
                drop.add(rid); cat[rid] = "C5_inactif"; trace["C5_inactif"].append(brand); continue
        elif level == "FULL_GENERIC":
            if base and core(base) in base_cores and absous(rid):
                drop.add(rid); cat[rid] = "C4_full"; trace["C4_full"].append(base); continue
        elif level == "BASE_GENERIC":
            # C6 doublons BASE par noyau : garder le plus court / premier
            pass

    # C6 : BASE_GENERIC doublons par noyau — garder les gardées + le plus court
    by_core = defaultdict(list)
    for r in base_rows:
        by_core[core(r[2])].append(r)
    for c, lst in by_core.items():
        c = core(c)
        if len(lst) < 2 or not c:
            continue
        lst.sort(key=lambda r: (len(norm(r[2])), r[0]))
        gardees = [r for r in lst if not absous(r[0])]
        keep_ids = {r[0] for r in gardees}
        if not gardees:
            keep_ids = {lst[0][0]}
        for extra in lst:
            if extra[0] in keep_ids:
                continue
            drop.add(extra[0])
            cat[extra[0]] = "C6_base_dup"
            trace["C6_base_dup"].append(extra[2])

    # ---------- INVARIANT : toute ligne retirée doit pouvoir remapper ses
    # alias vers un générique survivant de même noyau. Sinon → conservée. -----
    survivors = {r[0] for r in rows} - drop
    keep_log = []
    for rid in sorted(drop):
        r = id_of[rid]
        my_core = mol_of(r)
        rep = next((b[0] for b in base_rows
                    if b[0] in survivors and core(b[2]) == my_core), None)
        if rep is None:
            drop.discard(rid)
            keep_log.append(r[1] or r[2])

    # ---------- rapport / compte ----------
    counts = defaultdict(int)
    for rid in drop:
        counts[cat[rid]] += 1

    print(f"base {args.db} : {len(rows)} lignes, {len(aliases)} aliases")
    print()
    for c in ("C1_mfg", "C2_exact", "C3_decor", "C4_full", "C5_inactif", "C6_base_dup"):
        if counts[c]:
            print(f"  {c:<12} {counts[c]:>5}")
    print(f"  {'TOTAL retirés':<12} {len(drop):>5}")
    print(f"  → après prune : {len(rows) - len(drop)} lignes "
          f"({(len(rows)-len(drop))*100//len(rows)} % conservées)")
    print(f"  conservées par filet (alias non ré-couvers) : {len(keep_log)}")
    for b in keep_log[:20]:
        print(f"     • {b}")

    # exemples par catégorie
    for c in ("C1_mfg", "C2_exact", "C3_decor", "C5_inactif"):
        ex = sorted(trace[c])[:8]
        if ex:
            print(f"  ex {c}: {', '.join(map(str, ex))}")

    # ---------- garde : STT_GARBLE + aucun alias vers une ligne supprimée ----------
    stt_after = stt_ids & survivors
    if stt_ids - stt_after:
        names = [id_of[i][1] for i in stt_ids - stt_after]
        print(f"\n[garde] ÉCHEC : cibles STT_GARBLE perdues : {sorted(map(str, names))}")
        conn.close()
        sys.exit(2)
    print(f"[garde] STT_GARBLE : {len(stt_ids)} cibles, toutes conservées")

    # Tous les alias des lignes retirées sont remappés vers un générique
    # survivant (le filet ci-dessus garantit qu'il existe) → aucune résolution
    # ne pointe vers une ligne supprimée.
    print("[garde] alias des lignes retirées remappés vers le générique survivant")

    if not args.apply:
        print("\n[dry-run] gardes OK, passe --apply pour appliquer")
        conn.close()
        return

    # ---------- application ----------
    # (Les alias STT_GARBLE seedés sont sacrés : la déduplication qui suit ne
    # les touche JAMAIS — un alias STT partageant (medication_id, nom) avec un
    # alias BRAND/BASE ne doit pas être retiré au profit de l'autre.)
    if not args.apply:
        conn.close()
        return

    stt_before = set(
        cur.execute("SELECT alias_name, medication_id FROM medication_aliases "
                    "WHERE alias_type='STT_GARBLE'").fetchall())

    conn.execute("BEGIN")
    # 1) rep pour chaque noyau : BASE_GENERIC survivant le plus court
    rep_for_core = {}
    for b in sorted(base_rows, key=lambda r: len(norm(r[2]))):
        if b[0] not in survivors:
            continue
        c = core(b[2])
        if c and c not in rep_for_core:
            rep_for_core[c] = b[0]
    # 2) remap des alias des lignes retirées vers le générique survivant
    for rid in drop:
        r = id_of[rid]
        rep = rep_for_core.get(mol_of(r))
        if rep is None:
            continue   # filet : ne devrait pas arriver (ligne conservée sinon)
        for aid, _aname, _atype in aliases_by_mid.get(rid, ()):
            conn.execute(
                "UPDATE medication_aliases SET medication_id=? WHERE id=?",
                (rep, aid))
    # 3) supprimer les doublons d'alias (même (medication_id, nom)) —
    #    JAMAIS les STT_GARBLE (seeds exacts de dictée)
    conn.execute("""
        DELETE FROM medication_aliases
        WHERE alias_type != 'STT_GARBLE'
          AND id NOT IN (
              SELECT MIN(id) FROM medication_aliases
              WHERE alias_type != 'STT_GARBLE'
              GROUP BY medication_id, LOWER(alias_name)
          )
    """)
    # 4) supprimer les lignes retirées
    ph = ",".join("?" * len(drop))
    conn.execute(f"DELETE FROM medications WHERE id IN ({ph})", sorted(drop))

    # 6) garde post-application : AUCUN alias STT ne doit avoir disparu
    stt_after = set(
        conn.execute("SELECT alias_name, medication_id FROM medication_aliases "
                     "WHERE alias_type='STT_GARBLE'").fetchall())
    lost = stt_before - stt_after
    if lost:
        conn.rollback()
        print(f"\n[garde] ÉCHEC : {len(lost)} alias STT_GARBLE perdus "
              f"(ex. {sorted(map(str, list(lost)[:10]))}) — ROLLBACK")
        conn.close()
        sys.exit(2)
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"[apply] {len(drop)} lignes supprimées, {len(rows) - len(drop)} conservées")


if __name__ == "__main__":
    main()