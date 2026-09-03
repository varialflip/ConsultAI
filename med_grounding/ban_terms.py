#!/usr/bin/env python3
"""Ban sunscreens and the word gériatrique from meds.sqlite.

Two classes of rows are clinical noise that must never match, neither exact
nor fuzzy:

-  ALL SUNSCREEN / UV products. Their mark lemmas are generic words that
   create prose false positives (MINUTES, BASE, SAGE, RAPIDE, SUPPORT, LONG…).
   A sunscreen is a cosmetic, never dictated in a posology list.
-  ANY name containing "gériatrique" (ex. the brand "GERIATRIQUE" -> inositol):
   clinical prose, not a medication.

Removal applies to medications AND their aliases (a removed med leaves
nothing behind in the matcher index). Idempotent/dry-runnable like
prune_db.py.
"""
import re
import sqlite3
import sys
import unicodedata

DB = "./meds.sqlite"

#: Active UV-filter substances (FR + EN) / sunscreen principals. Any medication
#: whose base_generic refers to one of these is a sunscreen product
#: (recherche par SOUS-CHAÎNE normalisée — « dioxyde de titane » doit matcher
#: une base « dioxyde de titane »).
UV_BASES = {
    "octisalate", "octinosalate", "avobenzone", "butyl methoxydibenzoylmethane",
    "octocrylene", "octocrilene", "oxybenzone", "benzophenone", "homosalate",
    "octinoxate", "octyl methoxycinnamate", "ensulizole", "phenylbenzimidazole",
    "ecamsule", "mexoryl", "sulisobenzone", "dioxybenzone", "meradimate",
    "padimate", "enxasulfate", "tinosorb", "bemotrizinol", "bisoctrizole",
    "drometrizole", "trisiloxane", "zinc oxide", "oxyde de zinc", "peroxyde de zinc",
    "titanium dioxide", "dioxyde de titane", "cinnamate",
}

#: Brand/alias text markers for sunscreen / solar products. « SPF / FPS », un
#: « ÉCRAN », la protection solaire : une marque qui porte l'un de ces mots
#: est cosmétique, jamais un médicament clinique.
SUNSCREEN_TERMS = re.compile(
    r"\bsun ?screen\b|\bsunblock\b|\bsun ?protection\b|\bsun ?stick\b|"
    r"after ?sun\b|apr[ée]s[ -]?soleil\b|"
    r"\bspf\b|\bfps\b|"
    r"\b(?:ecran|écran|ecrans|écrans)\b|"
    r"\buv ?(?:block|protect|shield)\b|"
    r"\btanning\b|sunless|bronz|"
    r"\b(?:solaire|solaires)\b|"
    r"\bbroad ?spectrum\b|\bwater ?resistant\b",
    re.IGNORECASE,
)

#: Any name containing this lemma is banned (clinical prose, not a drug).
BAN_LEMMA = re.compile(r"g[eé]riatr", re.IGNORECASE)

#: Marques bannies par leur nom EXACT (uppercased) : retirées de la base au
#: même titre que les écrans solaires. NAUSEX (dimenhydrinate, marque OTC
#: annulée) : « nausées » (pluriel de « nausée », symptôme) résolvait vers la
#: marque NAUSEX à 83 % (faux positif, note 37) — le bannissement empêche toute
#: collision exacte ou floue future.
BAN_BRANDS = frozenset({
    "NAUSEX",
})


def norm_flat(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z]+", "", s.lower())


def is_sunscreen(brand: str, base: str) -> bool:
    """True si la ligne est un produit d'écran solaire (principe UV ou marque)."""
    blob = " ".join(x for x in (brand or "", base or "") if x)
    if SUNSCREEN_TERMS.search(blob):
        return True
    base_flat = norm_flat(base or "")
    if not base_flat:
        return False
    # L'UN des principes UV (forme normalisée) est une SOUS-CHAÎNE de la base :
    # « dioxyde de titane » matche « DIOXYDE DE TITANE », « oxyde de zinc »
    # matche « peroxyde de zinc ». Pas de sous-chaîne de mot court (« cinnamate »
    # ne doit pas matcher « sunitinib »).
    for u in UV_BASES:
        uf = norm_flat(u)
        if uf and uf in base_flat and len(uf) >= 8:
            return True
    return False


def banned(brand: str, base: str) -> bool:
    if brand and BAN_LEMMA.search(brand):
        return True
    if brand and brand.strip().upper() in BAN_BRANDS:
        return True
    return is_sunscreen(brand, base)


def main() -> None:
    dry = "--apply" not in sys.argv
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id, brand_name, base_generic FROM medications"
    ).fetchall()
    drop_ids = [mid for mid, bn, bg in rows if banned(bn, bg)]
    drop_ids = list(dict.fromkeys(drop_ids))  # ordre stable

    # Les aliases des lignes retirées partent avec elles.
    alias_ids = []
    if drop_ids:
        ph = ",".join("?" * len(drop_ids))
        alias_ids = [r[0] for r in conn.execute(
            "SELECT id FROM medication_aliases WHERE medication_id IN (%s)" % ph,
            drop_ids,
        ).fetchall()]

    print(f"medications à bannir : {len(drop_ids)}")
    print(f"aliases correspondants : {len(alias_ids)}")
    if dry:
        print("[dry-run] passe --apply pour appliquer")
        conn.close()
        return

    conn.execute("BEGIN")
    if drop_ids:
        ph = ",".join("?" * len(drop_ids))
        conn.execute(
            "DELETE FROM medication_aliases WHERE medication_id IN (%s)" % ph,
            drop_ids,
        )
        conn.execute(
            "DELETE FROM medications WHERE id IN (%s)" % ph, drop_ids,
        )
    conn.commit()
    vm = conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0]
    va = conn.execute("SELECT COUNT(*) FROM medication_aliases").fetchone()[0]
    print(f"après : medications={vm}, aliases={va}")
    conn.close()


if __name__ == "__main__":
    main()