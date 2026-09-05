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
    # préfixes manquants (audit des premiers tokens de marques, 2026-09-05)
    "novo", "nra", "ran", "penta", "gln", "pdp", "prz", "zym", "bci",
    "rho", "ftp", "pmsc", "myl", "ccp", "euro", "orb", "pat", "q", "lin",
    "lupin", "scheinpharm", "albert", "abbott", "bar",
}

#: Formes de libération prolongée / variantes de formulation.
RELEASE = {
    "xr", "er", "la", "cr", "xl", "odt", "ir", "dr", "sr", "xlr", "pr",
    "qd", "sos", "ec", "tr", "sr", "xl", "mr",
}

#: Sels / hydrates / esters — retirés pour atteindre le noyau générique.
#: Jamais dictés (0 occurrence dans les 31 transcripts réels) : une dictée
#: porte le nom nu (« perindopril ») ou la marque (« atacand »). NB : « acide »
#: n'est PAS un sel (« acide folique », « acide tranexamique » — tête du nom).
SALT = {
    "hcl", "fumarate", "maleate", "sodium", "calcium", "dihydrate",
    "monohydrate", "sulfate", "sodique", "hydrochloride", "phosphate",
    "citrate", "tartrate", "mesylate", "besylate", "gluconate",
    "chlorhydrate", "disodium", "magnesium", "potassium", "zinc", "ferreux",
    "ferrique", "hydroxyde", "carbonate", "bicarbonate", "de", "d",
    "acetate", "valerate",
    # sels/esters absents du premier vocabulaire (audit des derniers tokens)
    "chloride", "bromide", "disodique", "trihydrate", "dihydrochloride",
    "dichlorhydrate", "cilexetil", "erbumine", "xinafoate", "embonate",
    "olamine", "mesilate", "succinate", "hemisuccinate", "tosylate",
    "camsylate", "nitrate", "dinitrate", "mononitrate", "trinitrate",
    "lactate", "anhydre", "anhydrous", "hydrobromide", "propionate",
    "dipropionate", "butyrate", "enanthate", "estolate", "pivalate",
    "palmitate", "stearate", "undecylenate", "oxalate", "pamoate",
    "gluceptate", "cypionate", "decanoate", "bromhydrate", "arginine",
    "lysine",
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
    """Noyau molécule : mots du nom normalisé, sans fabricant ni décor.
    Les mots répétés sont dédoublonnés (« valacyclovir valacyclovir » →
    « valacyclovir ») : les FULL chimiques dupliquent le nom + son sel."""
    out = []
    for t in norm(s).split():
        if t in DECOR or t.isdigit() or len(t) < 3:
            continue
        if t not in out:
            out.append(t)
    return " ".join(out)


def dictable_name(base):
    """Nom DICTABLE d'un générique : ce que le clinicien prononce.

    Un seul mot porteur après retrait des sels → le nom nu
    (« perindopril erbumine » → « perindopril »). Plusieurs mots porteurs
    (« acide folique », « insulin glargine », « levodopa carbidopa ») → le nom
    d'origine, amputé de ses sels TRAILING uniquement (« acide » n'est jamais
    strippé : tête du nom, pas un sel). Vide (« sodium chloride ») → nom
    d'origine intact.
    """
    toks = norm(base).split()
    content = []
    for t in toks:
        if t in SALT or t in RELEASE or t.isdigit() or len(t) < 3:
            continue
        if t not in content:
            content.append(t)
    if not content:
        return base
    if len(content) == 1:
        return content[0]
    out = list(toks)
    while out and (out[-1] in SALT or out[-1] in RELEASE):
        out.pop()
    return " ".join(out) if out else base


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


def run_dictable(conn, cur, rows, aliases, aliases_by_mid, stt_ids, gu,
                 absous, args):
    """Mode --dictable : la base ne retient que ce qui se dicte.

    Un clinicien prononce le nom nu (« perindopril ») ou la marque
    (« atacand ») — jamais le sel (« perindopril erbumine »), le nom chimique
    complet (« valacyclovir valacyclovir hydrochloride monohydrate ») ni la
    marque fabricant (« TEVA-CANDESARTAN »). Tout le reste n'est qu'une
    surface de faux positifs (phonétique de noms longs vs prose) :

    D1. les BASE_GENERIC sont RENOMMÉS au nu (``dictable_name``) — un
        générique salifié n'est plus qu'une variante d'alias ; les lignes de
        même nom nu fusionnent (sels, doublons FR/EN) ;
    D2. les FULL_GENERIC sortent (jamais dictés) ;
    D3. les marques fabricant sortent toutes (simples ET combinaisons) ;
    D4. les copies exactes / décorées de SEL (C2/C3 sans libération) sortent —
        les variantes de libération dictables (« Seroquel XR ») restent ;
    D5. les marques propriétaires (± XR/combos, OTC, legacy inactive) restent :
        noms distinctifs, réellement dictables.

    INVARIANT : toute ligne retirée voit ses alias remappés vers le générique
    nu survivant de même noyau — sauf les alias jamais dictés (feuilles
    « teva »/« fumarate », noms chimiques sans représentation nue), supprimés.
    Aucun alias STT ne disparaît (garde post-application).
    """
    id_of = {r[0]: r for r in rows}
    drop = set()
    cat = {}
    trace = defaultdict(list)
    renames = []

    def absous_brand(rid):
        """Garde de MARQUE : les feuilles (BRAND_LEAF) ne protègent pas —
        au runtime, un générique nu écrase une feuille de même ``norm_phon``,
        donc une marque fabricant dont la feuille porte le nom du générique
        n'apporte rien que le générique n'ait déjà."""
        if rid in stt_ids:
            return False
        for _aid, aname, atype in aliases_by_mid.get(rid, ()):
            if atype == "BRAND_LEAF":
                continue
            if nph(aname) in gu:
                return False
        return True

    # ---- D1 : renommage des BASE au nu + fusion des sels / doublons FR-EN --
    base_rows0 = [r for r in rows if r[3] == "BASE_GENERIC"]
    new_name = {r[0]: dictable_name(r[2]) for r in base_rows0}

    def fold_key(name):
        """Clé de fusion FR/EN : « metformin »/« metformine », « digoxin »/
        « digoxine » — même molécule, deux orthographes. On replie le -e
        final (jamais pour les noms courts)."""
        n = norm(name)
        return n[:-1] if len(n) > 4 and n.endswith("e") else n

    by_new = defaultdict(list)
    for r in base_rows0:
        by_new[fold_key(new_name[r[0]])].append(r)
    merge_drop = set()
    for name, lst in by_new.items():
        if len(lst) < 2:
            continue
        lst2 = sorted(lst, key=lambda r: (
            0 if nph(new_name[r[0]]) in gu else 1,   # l'orthographe du JSON
            0 if norm(new_name[r[0]]) == name else 1,  # déjà au nu
            0 if r[4] else 1, r[0]))
        keeper = lst2[0]
        for extra in lst2[1:]:
            if absous(extra[0]):
                merge_drop.add(extra[0])
                trace["D1_merge"].append(extra[2])
            else:
                # gardée : conservée, mais renommée au nu comme sa sœur
                new_name[extra[0]] = new_name[keeper[0]]

    # application en mémoire du renommage
    rows2 = []
    for r in rows:
        if r[3] == "BASE_GENERIC" and r[0] in new_name:
            nn = new_name[r[0]]
            if norm(nn) != norm(r[2]):
                renames.append((r[0], r[2], nn))
            rows2.append((r[0], r[1], nn, r[3], r[4], r[5], r[6]))
        else:
            rows2.append(r)
    rows = rows2
    id_of = {r[0]: r for r in rows}
    base_rows = [r for r in rows if r[3] == "BASE_GENERIC"]
    base_names = {norm(b[2]) for b in base_rows if b[2]}

    # ---- D5/C2 : copies exactes du nu --------------------------------------
    for r in rows:
        rid, brand, base, level, act, otc, src = r
        if level != "BRAND" or rid in stt_ids or not absous_brand(rid):
            continue
        nb = norm(brand)
        if nb and nb in base_names:
            drop.add(rid); cat[rid] = "C2_exact"; trace["C2_exact"].append(brand)

    # ---- D5/C3 : décor de sel/forme SANS libération (XR/ER restent) --------
    for r in rows:
        rid, brand, base, level, act, otc, src = r
        if level != "BRAND" or rid in stt_ids or not absous_brand(rid):
            continue
        if rid in drop:
            continue
        nb = norm(brand)
        toks = [t for t in nb.split() if t not in SALT and t not in FORM
                and len(t) >= 3]
        seen = []
        for t in toks:
            if t not in seen:
                seen.append(t)
        bare = " ".join(seen)
        if bare and bare != nb and bare in base_names:
            drop.add(rid); cat[rid] = "C3_decor"; trace["C3_decor"].append(brand)

    # ---- D2 : FULL_GENERIC sortent tous (jamais dictés) --------------------
    for r in rows:
        if r[3] == "FULL_GENERIC" and r[0] not in stt_ids and absous(r[0]):
            drop.add(r[0]); cat[r[0]] = "D2_full"; trace["D2_full"].append(r[2])

    # ---- D3 : marques fabricant sortent toutes (simples ET combos) --------
    for r in rows:
        rid, brand, base, level, act, otc, src = r
        if level != "BRAND" or rid in stt_ids or not absous_brand(rid):
            continue
        if norm(strip_mfg(brand)) != norm(brand):
            drop.add(rid); cat[rid] = "D3_mfg"; trace["D3_mfg"].append(brand)

    drop |= merge_drop
    for rid in merge_drop:
        cat[rid] = "D1_merge"

    # ---- filet dictable : remap vers le générique nu survivant, sinon
    # SUPPRESSION de l'alias (jamais dicté) — anti-leaf : une feuille
    # « teva »/« fumarate » ne doit jamais devenir une résolution ------------
    survivors = {r[0] for r in rows} - drop
    rep_for_core = {}
    for b in sorted(base_rows, key=lambda r: len(norm(r[2]))):
        if b[0] in survivors:
            c = core(b[2])
            if c and c not in rep_for_core:
                rep_for_core[c] = b[0]

    def mol(r):
        if r[3] == "BRAND":
            sm = strip_mfg(r[1])
            return core(sm) if sm else core(r[2] or r[1])
        return core(r[2] or r[1])

    remap = {}
    delete_aliases = []
    rep_keys = defaultdict(set)
    for b in base_rows:
        if b[0] in survivors:
            for _aid, aname, _atype in aliases_by_mid.get(b[0], ()):
                rep_keys[b[0]].add(nph(aname))
    for rid in sorted(drop):
        r = id_of[rid]
        rep = rep_for_core.get(mol(r))
        for aid, aname, atype in aliases_by_mid.get(rid, ()):
            if atype == "BRAND_LEAF":
                # JAMAIS remapper une feuille : posée sur le générique nu elle
                # masquerait l'alias BASE de même nom (``_lookup_exact`` refuse
                # les feuilles → résolution exacte perdue, cf. galantamine) et
                # une feuille « teva »/« fumarate » créerait un faux positif.
                # Toute feuille d'une ligne retirée est supprimée.
                delete_aliases.append(aid)
                continue
            key = nph(aname)
            if rep is not None and key and key not in rep_keys.get(rep, ()):
                # clé NOUVELLE pour le rep → remap (résolution préservée)
                remap[aid] = rep
                rep_keys[rep].add(key)
            else:
                # déjà couverte par un alias propre du rep (ou aucun rep) →
                # suppression (jamais de collision de type sur le rep)
                delete_aliases.append(aid)

    # ---------- rapport ----------
    counts = defaultdict(int)
    for rid in drop:
        counts[cat.get(rid, "?")] += 1
    print(f"base {args.db} : {len(rows)} lignes, {len(aliases)} aliases")
    print(f"  renommages BASE au nu        : {len(renames):>5}")
    for _rid, old, new in sorted(renames, key=lambda x: x[2])[:12]:
        print(f"     • {old!r} → {new!r}")
    if len(renames) > 12:
        print(f"     • … (+{len(renames)-12})")
    print()
    for c in ("D1_merge", "C2_exact", "C3_decor", "D2_full", "D3_mfg"):
        if counts[c]:
            print(f"  {c:<12} {counts[c]:>5}")
    print(f"  {'TOTAL retirés':<12} {len(drop):>5}")
    print(f"  → après dictable : {len(rows) - len(drop)} lignes "
          f"({(len(rows)-len(drop))*100//len(rows)} % conservées)")
    print(f"  alias remappés   : {len(remap):>5} | alias supprimés (jamais "
          f"dictés) : {len(delete_aliases):>5}")
    for c in ("D1_merge", "C2_exact", "C3_decor", "D2_full", "D3_mfg"):
        ex = sorted(trace[c])[:8]
        if ex:
            print(f"  ex {c}: {', '.join(map(str, ex))}")

    # ---------- gardes ----------
    stt_after = stt_ids & survivors
    if stt_ids - stt_after:
        names = [id_of[i][1] for i in stt_ids - stt_after]
        print(f"\n[garde] ÉCHEC : cibles STT_GARBLE perdues : "
              f"{sorted(map(str, names))}")
        conn.close()
        sys.exit(2)
    print(f"[garde] STT_GARBLE : {len(stt_ids)} cibles, toutes conservées")
    non_traites = [a for a in aliases
                   if a[1] in drop and a[0] not in remap
                   and a[0] not in delete_aliases]
    if non_traites:
        print(f"\n[garde] ÉCHEC : {len(non_traites)} alias de lignes retirées "
              f"ni remappés ni supprimés (ex. {non_traites[:5]})")
        conn.close()
        sys.exit(2)
    print("[garde] tous les alias des lignes retirées sont remappés au nu "
          "ou supprimés (jamais dictés)")

    # purge DB-wide : feuilles dangereuses (préfixe fabricant / sel — dictation
    # « teva » ne doit jamais grounder un médicament) + alias orphelins
    purge_leaves = []
    for aid, mid, aname, atype in aliases:
        if atype != "BRAND_LEAF" or mid in drop:
            continue            # déjà traités ci-dessus
        toks = norm(aname).split()
        tok = toks[0] if toks else ""
        if tok in MFG_PREFIXES or tok in SALT:
            purge_leaves.append(aid)
    orphelins = [aid for aid, mid, _an, _at in aliases if mid not in id_of]
    print(f"  purge feuilles fabricant/sel : {len(purge_leaves):>5} | "
          f"alias orphelins : {len(orphelins):>5}")

    if not args.apply:
        print("\n[dry-run] gardes OK, passe --apply pour appliquer")
        conn.close()
        return

    # ---------- application ----------
    stt_before = set(
        cur.execute("SELECT alias_name, medication_id FROM medication_aliases "
                    "WHERE alias_type='STT_GARBLE'").fetchall())

    conn.execute("BEGIN")
    # 1) renommage des BASE au nu (ligne + alias BASE_GENERIC)
    for rid, old, new in renames:
        conn.execute("UPDATE medications SET base_generic=? WHERE id=?",
                     (new, rid))
        conn.execute("UPDATE medication_aliases SET alias_name=? "
                     "WHERE medication_id=? AND alias_type='BASE_GENERIC'",
                     (new, rid))
    # 2) remap des alias des lignes retirées
    for aid, rep in remap.items():
        conn.execute("UPDATE medication_aliases SET medication_id=? WHERE id=?",
                     (rep, aid))
    # 3) suppression des alias jamais dictés (feuilles fabricant/décor, noms
    #    chimiques sans nu)
    if delete_aliases:
        ph = ",".join("?" * len(delete_aliases))
        conn.execute(f"DELETE FROM medication_aliases WHERE id IN ({ph})",
                     sorted(delete_aliases))
    # 4) déduplication des alias non-STT (même (medication_id, nom))
    conn.execute("""
        DELETE FROM medication_aliases
        WHERE alias_type != 'STT_GARBLE'
          AND id NOT IN (
              SELECT MIN(id) FROM medication_aliases
              WHERE alias_type != 'STT_GARBLE'
              GROUP BY medication_id, LOWER(alias_name)
          )
    """)
    # 5) supprimer les lignes retirées
    ph = ",".join("?" * len(drop))
    conn.execute(f"DELETE FROM medications WHERE id IN ({ph})", sorted(drop))
    # 6) purge : feuilles fabricant/sel + alias orphelins (préexistants)
    all_purge = sorted(set(delete_aliases) | set(purge_leaves) | set(orphelins))
    if all_purge:
        ph2 = ",".join("?" * len(all_purge))
        conn.execute(f"DELETE FROM medication_aliases WHERE id IN ({ph2})",
                     all_purge)

    # 6) garde post-application : AUCUN alias STT ne doit avoir disparu
    stt_after2 = set(
        conn.execute("SELECT alias_name, medication_id FROM medication_aliases "
                     "WHERE alias_type='STT_GARBLE'").fetchall())
    lost = stt_before - stt_after2
    if lost:
        conn.rollback()
        print(f"\n[garde] ÉCHEC : {len(lost)} alias STT_GARBLE perdus "
              f"(ex. {sorted(map(str, list(lost)[:10]))}) — ROLLBACK")
        conn.close()
        sys.exit(2)
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"[apply] {len(drop)} lignes supprimées, {len(rows) - len(drop)} "
          f"conservées, {len(renames)} génériques renommés au nu")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="applique (sinon dry-run)")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--corpus-json", default=None,
                    help="fichier json : liste de norm_phon observés en corpus")
    ap.add_argument("--dictable", action="store_true",
                    help="mode dictable : génériques renommés au nu (sels "
                         "retirés), FULL_GENERIC et marques fabricant "
                         "supprimées — la base ne retient que ce qui se "
                         "dicte (nom nu ou marque propriétaire)")
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

    if args.dictable:
        run_dictable(conn, cur, rows, aliases, aliases_by_mid, stt_ids, gu,
                     absous, args)
        return

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