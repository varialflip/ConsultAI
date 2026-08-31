#!/usr/bin/env python3
"""Prune les lignes de marques OTC de comptoir hors contexte clinique dans
meds.sqlite.

Un clinicien qui dicte des médicaments peut dire « Tylenol », « Advil »,
« Gravol », mais jamais « TUMS CHEWY BITES WITH GAS RELIEF », « BILEX »,
« TUMS CHERRY » ou les 25 variantes marketing d'une même famille (force,
parfum, format, gel, plus, extra…). Ces lignes sont du bruit qui alourdit la
base d'ancrage sans enrichir la résolution.

Le retrait ne touche que les marques **OTC** (`level='BRAND' AND is_otc=1`).
Les marques Rx, les génériques (BASE_GENERIC / FULL_GENERIC) et les alias
STT_GARBLE ne sont jamais modifiés. Une marque OTC est CONSERVÉE si :

- **R1** son principe actif (`base_generic`) entre dans une liste de
  substances cliniquement dictables (acétaminophène, ibuprofène, docusate,
  diphénhydramine, siméticone…) ; ou
- **R0** elle est la cible d'au moins un alias `STT_GARBLE` seedé (on ne casse
  jamais un garble manuel déjà observé).

Puis on **déduplique par famille de marque** (premier mot du nom) : un nom de
tête (TYLENOL, BENADRYL, ADVIL) ne garde qu'**une ligne représentative** (celle
qui résout le nom court dicté) + toutes ses lignes seedées — « une fois que
Tylenol apparaît, inutile qu'il réapparaisse vingt fois ».

Un **benchmark de garde** (Tylenol, Advil, Gravol, Benadryl, Voltaren) vérifie,
avant application, que chaque nom court résout encore après le prunage (via une
ligne OTC conservée ou une ligne Rx intacte) et qu'aucun garble seedé n'est
perdu. `--apply` refuse d'écrire si le benchmark échoue.

Idempotent / dry-runnable comme prune_scope.py, ban_terms.py, prune_db.py.
"""
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict

DB = "./meds.sqlite"

#: Principe actif cliniquement dictable en gériatrie (R1). Recherche par
#: SOUS-CHAÎNE normalisée sur `base_generic`: une entrée courte comme « fer »
#: matche « sulfate ferreux », « calcium » matche « carbonate de calcium ».
KEEP_BASES = [
    # antalgiques / AINS
    "acetaminophen", "acide acetylsalicylique", "salicylate", "ibuprofen",
    "naproxen", "diclofenac",
    # laxatifs / régulateurs du transit
    "docusate", "bisacodyl", "lactulose", "polyethylene glycol", "macrogol",
    "sennosides", "senna", "huile minerale", "glycerine", "glycerol",
    "psyllium", "methylcellulose", "polycarbophile",
    # suppléments / carences
    "fer", "sulfate ferreux", "fumarate ferreux", "gluconate de fer",
    "acide folique", "folate", "vitamine b12", "cyanocobalamine", "magnesium",
    "zinc", "sulfate de zinc", "potassium", "thiamine", "nicotinamide",
    "acide ascorbique", "vitamine c", "vitamine d", "calcium",
    "carbonate de calcium", "citrate de calcium", "gluconate de calcium",
    "lactate de calcium", "omeprazole",
    # antiémétiques / gastro
    "dimenhydrinate", "loperamide", "simeticone", "famotidine",
    "omeprazole", "rantidine", "ranitidine", "meclizine", "alginique",
    "hydroxyde", "bismuth",
    # antiallergiques (ne pas confondre avec décongestionnants)
    "diphenhydramine", "cetirizine", "loratadine", "fexofenadine",
    "desloratadine", "cromolyne",
    # dermatologie OTC
    "hydrocortisone", "clotrimazole", "miconazole", "ketoconazole",
    "permethrine", "nystatine", "bacitracine", "chlorhexidine",
    # ophtalmo / ORL doux
    "hypromellose", "carboxymethylcellulose", "cromoglycate", "lidocaine",
    # autres
    "methocarbamol", "nicotine",
]

#: Mots de présentation / parfum / force marketing (R2) : un nom de marque qui
#: en porte un est un produit de comptoir non dicté tel quel (force, parfum,
#: format, « avec gaz »…). TUMS CHERRY = parfum → retiré.
FLUFF_TOKENS = [
    "chew", "chewable", "gas relief", "anti gas", "antigas", "flavour",
    "flavor", "berry", "mint", "sherbert", "sherbet", "tropical", "fruit",
    "cherry", "wintergreen", "assorted", "strength", "ultra", "liquid",
    "liqui", "complete", "rapid", "smooth", "cool", "fresh", "mini",
    "caplet", "bite", "bites", "gel", "softgel", "soft gel", "plus",
    "extra", "maximum", "maximum strength",
]

#: Allégations marketing de comptoir (R2 bis) : durée/nuit/rhume combiné qui
#: signent un produit grand public non dicté — « 24 HOUR ALLERGY REMEDY »,
#: « DIMETAPP NIGHTTIME COLD », « TUMS DUAL ACTION ». GAVISCON (acide
#: alginique) reste car son nom ne porte aucune allégation.
FLUFF_CLAIMS = re.compile(
    r"\b(dual action|advanced|extra strength|maximum strength|"
    r"\d+\s*hr|24[ -]hour|fast[ -]?acting|long[ -]?acting|"
    r"multi[ -]?symptom|allergy relief|night[ -]?time|"
    r"nighttime|daytime|pm|am)\b",
    re.IGNORECASE,
)

#: Benchmark de garde : ces noms courts doivent encore résoudre après le
#: prunage (via une ligne OTC conservée ou une ligne Rx intacte). Ce sont des
#: VÉRIFICATIONS, pas une liste d'exceptions de conservation.
BENCHMARK = ["tylenol", "advil", "gravol", "benadryl", "voltaren"]


def norm(s: str) -> str:
    """Normalise un nom : minuscules, sans accents, mots séparés par espaces."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def first_word(n: str) -> str:
    x = norm(n)
    return x.split()[0] if x else ""


def has_fluff(brand: str) -> bool:
    b = (brand or "").lower()
    return any(re.search(r"\b" + re.escape(t.lower()), b) for t in FLUFF_TOKENS)


def base_clinical(base: str) -> bool:
    n = norm(base)
    return any(t in n for t in KEEP_BASES)


def main() -> None:
    dry = "--apply" not in sys.argv
    conn = sqlite3.connect(DB)

    otc = conn.execute(
        "SELECT id, brand_name, base_generic FROM medications "
        "WHERE level='BRAND' AND is_otc=1"
    ).fetchall()
    seeds = {m for (m,) in conn.execute(
        "SELECT medication_id FROM medication_aliases "
        "WHERE alias_type='STT_GARBLE'"
    )}

    # alias norm -> set de (medication_id, alias_type)
    aliases = defaultdict(set)
    for mid, al, typ in conn.execute(
        "SELECT medication_id, alias_name, alias_type FROM medication_aliases"
    ):
        aliases[norm(al)].add((mid, typ))

    def resolves(tok: str, alive: set, rx_ids: set) -> bool:
        """True si `tok` résout vers une ligne OTC vivante ou une ligne Rx."""
        for mid, typ in aliases.get(norm(tok), ()):
            if typ in ("BRAND", "BRAND_LEAF") and (mid in alive or mid in rx_ids):
                return True
        return False

    # ------------------------------------------------------------- décision
    kept = []  # (id, brand, base) conservés par R1/R2/R0
    for mid, brand, base in otc:
        if mid in seeds or (
            base_clinical(base)
            and not has_fluff(brand)
            and not FLUFF_CLAIMS.search(brand or "")
        ):
            kept.append((mid, brand, base))

    dropped_rules = {r[0] for r in otc if r not in kept}

    # -------------------------------------------------- déduplication famille
    fam = defaultdict(list)
    for mid, brand, base in kept:
        fam[first_word(brand)].append((mid, brand, base))

    final = set()
    for tok, items in fam.items():
        items.sort(key=lambda r: (
            0 if r[0] in seeds else 1,      # les cibles des grables d'abord
            0 if norm(r[1]) == tok else 1,  # ensuite le nom de tête exact
            0 if any(m == r[0] and t in ("BRAND", "BRAND_LEAF")
                     for m, t in aliases.get(tok, ())) else 1,
            len(norm(r[1])),                # puis le nom le plus court
        ))
        for rep in items:
            if rep[0] in seeds:
                final.add(rep[0])
        if not any(r[0] in seeds for r in items):
            final.add(items[0][0])

    deduped = {r[0] for r in otc} - final  # OTC entrées retirées au total

    # ------------------------------------------------------------- benchmark
    rx_ids = {mid for mid, *_ in conn.execute(
        "SELECT id FROM medications WHERE is_otc=0 AND level='BRAND'"
    )}
    seeds_otc = seeds & {r[0] for r in otc}
    ok = True
    for ex in BENCHMARK:
        if not resolves(ex, final, rx_ids):
            ok = False
            print(f"[benchmark] ÉCHEC : « {ex} » ne résout plus (0 lignes)")
    if seeds_otc - final:
        ok = False
        print(f"[benchmark] ÉCHEC : grables viables perdus "
              f"{sorted(map(str, seeds_otc - final))}")

    total_before = conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0]
    alias_before = conn.execute("SELECT COUNT(*) FROM medication_aliases").fetchone()[0]

    print(f"OTC BRAND : {len(otc)} lignes (dont {len(seeds_otc)} cibles de grables)")
    print(f"conservées (R1/R2/R0)     : {len(kept)}")
    print(f"après dédup. par famille  : {len(final)}  (−{len(kept)-len(final)})")
    print(f"OTC totales retirées      : {len(deduped)} → "
          f"médicaments {total_before} → {total_before - len(deduped)} "
          f"(estimé)")

    if not ok:
        print("\n[benchmark] ÉCHEC — refus d'appliquer")
        conn.close()
        sys.exit(2)

    if dry:
        print("[dry-run] benchmark OK, passe --apply pour appliquer")
        conn.close()
        return

    conn.execute("BEGIN")
    ph = ",".join("?" * len(deduped))
    conn.execute(
        "DELETE FROM medication_aliases WHERE medication_id IN (%s)" % ph,
        sorted(deduped),
    )
    conn.execute(
        "DELETE FROM medications WHERE id IN (%s)" % ph, sorted(deduped),
    )
    conn.commit()
    vm = conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0]
    va = conn.execute("SELECT COUNT(*) FROM medication_aliases").fetchone()[0]
    print(f"après : medications={vm} (avant {total_before}), "
          f"aliases={va} (avant {alias_before})")
    conn.close()


if __name__ == "__main__":
    main()
