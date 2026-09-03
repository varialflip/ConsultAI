#!/usr/bin/env python3
"""Build the Canadian medication SQLite DB from Health Canada DPD + garble aliases."""
import re, sqlite3, unicodedata

DPD_DIR = "./dpd"
DPD_IA_DIR = "./dpd_ia"   # cancelled/inactive extract (drug_ia, ingred_ia, ...)
DB_PATH = "./meds.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS medications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_name        TEXT,
    base_generic      TEXT NOT NULL,
    full_chemical_name TEXT,
    level             TEXT NOT NULL CHECK(level IN ('BRAND','BASE_GENERIC','FULL_GENERIC')),
    is_active         BOOLEAN NOT NULL DEFAULT 1,
    is_otc            BOOLEAN NOT NULL DEFAULT 0,
    source            TEXT NOT NULL,
    phonetic_fr       TEXT
);
CREATE TABLE IF NOT EXISTS medication_aliases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    medication_id INTEGER REFERENCES medications(id),
    alias_name    TEXT NOT NULL,
    alias_type    TEXT NOT NULL CHECK(alias_type IN ('BRAND','BASE_GENERIC','FULL_GENERIC','STT_GARBLE','BRAND_LEAF')),
    phonetic_fr   TEXT
);
CREATE INDEX IF NOT EXISTS idx_meds_alias  ON medication_aliases(alias_name);
CREATE INDEX IF NOT EXISTS idx_meds_phonetic ON medication_aliases(phonetic_fr);
CREATE TABLE IF NOT EXISTS common_meds (
    medication_id INTEGER PRIMARY KEY REFERENCES medications(id),
    rank INTEGER NOT NULL DEFAULT 0
);
"""

# Salt / dosage-form suffixes to strip to derive the base generic name.
# ``acetate``/``valerate``/``carbonate``/``nitrate``/``gluconate``/``lactate``
# manquaient : « FLUDROCORTISONE 21-ACETATE » produisait le générique
# « fludrocortisone 21 acetate » au lieu de « fludrocortisone » (2026-09-03).
# Du coup 15 racines n'avaient que leur forme salifiée (fludrocortisone,
# formoterol, lithium, miconazole, cortisone, medroxyprogesterone, …) et la
# plus dictée d'elles résolvait en phonétique vers hydrocortisone.
SALT_SUFFIX_EN = [
    "hydrochloride", "hydrobromide", "besylate", "tartrate", "maleate",
    "sodium", "potassium", "calcium", "magnesium", "succinate", "dihydrate",
    "monohydrate", "anhydrous", "phosphate", "sulfate", "sulphate", "fumarate",
    "citrate", "mesylate", "tosylate", "camsylate", "niphedipine",
    "acetate", "valerate", "gluconate", "lactate", "carbonate", "nitrate",
    "bicarbonate", "propionate", "dipropionate", "butyrate", "enanthate",
    "estolate", "palmitate", "pivalate", "stearate", "undecylenate",
]
SALT_SUFFIX_FR = [
    "chlorhydrate", "bromhydrate", "besylate", "tartrate", "maleate",
    "sodique", "potassique", "calcique", "magnesique", "succinate", "dihydrate",
    "monohydrate", "anhydre", "phosphate", "sulfate", "sulphate", "fumarate",
    "citrate", "mesylate", "tosylate", "camsylate",
    "acetate", "valerate", "gluconate", "lactate", "carbonate", "nitrate",
    "bicarbonate", "propionate", "dipropionate", "butyrate", "enanthate",
    "estolate", "palmitate", "pivalate", "stearate", "undecylenate",
]
# Brand-name frequency suffixes / strength tokens to drop from brand names.
BRAND_STOP = re.compile(
    r"(\b(?:tablet|tab|caplet|capsule|cap|oral solution|oral suspension|injection|"
    r"cream|ointment|gel|spray|patch|suppository|syrup|elixir|drop|drops|solution|"
    r"tablets|injectable|syringe|regular|extra|extra strength|non|no\.?|etc)\b"
    r"|\s+\d+\s*(?:mg|mcg|g|ml|%)\b|\s+xl\b|\s+hs\b|\s+np\b|\s*qd\b|\s*tab\b|\s+co\b)",
    re.IGNORECASE,
)
# Token suffixes that indicate a strength / presentation to strip for cleanliness.
BRAND_TRAILING = re.compile(r"[\s\-]+(\d+\s*(mg|mcg|g|ml|%)\b|xl\b|hs\b|np\b)", re.IGNORECASE)

# Generic presentation words that must NOT become brand-leaf aliases.
# (Le jeu de mots « floratiles » observés sur les marques OTC/disparues :
# SET, AND, WHITE, MIN... — voir prune_scope.py, et les mots de prose qui
# survivent au retrait ci-dessous.)
BRAND_LEAF_STOP = set("""
strength regular extra children enfants junior adult adults complete cold cough flu
plus mucus relief liquid gels rapid release arthritis pain ultra ultimate kids
maximum max forte forte extra nouvelle new original advance advanced non dosed
surround etc co cr er xr sr odt tab caplet capsule tablet cap easy swallow spray
""".split())

# Mots de prose qui survivent au retrait de périmètre (marques légitimes à
# tous les mots comme DEPO-MEDROL WITH LIDOCAINE, VOLTAREN RAPIDE...). Leur
# feuille de marque est un mot français/anglais courant : risque de faux
# positif en prose, ne deviennent jamais des alias BRAND_LEAF.
BRAND_LEAF_STOP.update("""
and with one without joint application action all restore first low preparation
total time depot back day internal treatment immediate
""".split())

# Very common French words that must never be treated as a brand leaf (prevents
# false hits like `mini` -> ADVIL MINI-GELS, `plan` -> PLAN B). Shared with the
# matcher's stoplist.
FRENCH_COMMON_WORDS = set("""
un une des le la les du de d a à au aux et ou ni ne se ce sa son ses leur leurs
elle il ils elles on nous vous je tu moi toi notre vos ton ta tes mon ma mes
en dans pour par avec sans sous sur chez vers entre pendant depuis autour comme
tout toute tous toutes rien quelque quelques quel quelle quels quelles ceci cela
celui celle meme si car mais donc or quand que qui quoi dont ou est sont etait ete
fait font faire avoir etre suis es avais avait avons avez ont sera aucune aucun
autre pas non plus tres bien bon bonne mauvais tres peu beaucoup encore toujours
jamais deja puis avant apres chez vers y en cela cette cet ces jour mois an ans
semaine docteur medecin madame monsieur dame patient patienter clinique hopital
examen prise sang normale normal numero note aller dit dite dire voir vu vue veux
voulait voulez sait sais savoir passe passee present depuis revue revoir rien yeux
oeil annee prochain derniere premier premiere suivant suivante actuel actuelle
mini mental plan aide soin soins dose douleur douleurs pain life live daily active
plus pro max ultra kids child adult senior forte relief reliefs cold flu cough
sinus allergy aller apres avant toujours jamais site situ situe bas haut grand
petit moyen faible fort fortra new nouveau nouvelle original classique moderne
complet complete special speciale unique com prendre type ligne moment diabete
proteine proteines phosphate magnesium dispille dispil prepare preparer prescrite
# prose / lab-value guards (they must never become brand leaves)
proteines proteine type ligne moment diabete prendre prerare preparer preparee
# collisions from the wider inactive-brand pool
geriatrique geriatriques note notes repet repete ferritine ferritines avc autres
""".split())
FRENCH_COMMON_WORDS |= {str(n) for n in range(1000)}


def norm(s):
    """Lowercase, accent-fold, collapse punctuation to spaces for matching."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def strip_salts(name, fr=False):
    """Return base generic by removing a leading/trailing chemical salt token."""
    # Operate on the RAW name BEFORE accent/paren normalization:
    # "Citalopram (citalopram hydrobromide)" -> "citalopram".
    # If no parenthetical, also handle "chlorhydrate de labétalol" (salt + "de X").
    m = re.match(r"^\s*([A-Za-z][A-Za-z' -]+?)\s*\([^)]*\)\s*$", name)
    if m:
        base_raw = m.group(1)
    else:
        base_raw = name
    n = norm(base_raw)
    words = n.split()
    # Remove ONE trailing salt token (e.g. "diphenhydramine hydrochloride")
    salts = SALT_SUFFIX_FR if fr else SALT_SUFFIX_EN
    stripped_salt = False
    if len(words) >= 2 and words[-1] in salts:
        words = words[:-1]
        stripped_salt = True
    # Stéroïdes « FLUDROCORTISONE 21-ACETATE » : le marqueur de position (21)
    # précède le sel. On ne l'enlève que si un sel vient d'être retiré, pour ne
    # jamais dépouiller un vrai nombre de dose (« VITAMIN B12 » reste intact).
    if stripped_salt and len(words) >= 2 and words[-1].isdigit():
        words = words[:-1]
    # Handle explicit French "X de Y" salt patterns -> keep "Y"
    if len(words) >= 3 and words[-2] == "de" and words[-1] in {"citalopram"}:
        pass
    return " ".join(words)


# Presentation / strength remnants to strip from brand names so an injectable
# inactive like "HALDOL INJECTION 5MG/ML" lands as the plain name "HALDOL".
# Without this the alias becomes "HALDOL /ML", whose unit token makes
# prune_db.py delete it as presentation junk and orphan the med.
SLASH_STRENGTH = re.compile(r"\s*/\s*\d*(?:[.,]\d*)?\s*(?:mg|mcg|g|ml|%)\b", re.I)
INLINE_STRENGTH = re.compile(
    r"\s+\d+(?:[.,]\d+)?\s*(?:mg|mcg|g|ml|%)\b(?:\s*/\s*\d+(?:[.,]\d+)?\s*(?:mg|mcg|g|ml|%)\b)?", re.I)
FORM_TAIL = re.compile(r"\s+(?:la|inj|injection)\s*$", re.I)


def clean_presentation(name):
    """Reduce a raw brand name to its name-only core: drop dosage-form words
    (BRAND_STOP), slash strengths ("5MG/ML", "/ML", "50 MG/ML", "0.1MG") and
    trailing LA/INJ form tokens.
    """
    n = BRAND_STOP.sub(" ", name)
    n = SLASH_STRENGTH.sub(" ", n)
    n = INLINE_STRENGTH.sub(" ", n)
    while True:
        m = FORM_TAIL.sub(" ", n)
        if m == n:
            break
        n = m
    return re.sub(r"\s+", " ", n).strip().rstrip(".-")


def ingest_inactive(conn, seen, dpd_ia_dir=DPD_IA_DIR):
    """Ingest Health Canada 'cancelled / inactive' products (dpd_ia extract) as
    BRAND rows mapped to their active-ingredient base generic. is_active=0 so a
    still-marketed brand of the same name (already ingested) takes precedence."""
    import os
    def read(fn):
        path = os.path.join(dpd_ia_dir, fn)
        if not os.path.exists(path):
            print(f"  (skip: no {fn})")
            return []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                inner = line[1:-1] if line.startswith('"') and line.endswith('"') else line
                yield inner.split('","')

    # schedule_ia: DRUG_CODE -> otc flag. Contrairement à schedule.txt
    # (valeur "OTC"), l'extrait des produits inactifs étiquette les produits
    # sans ordonnance "NON-PRESCRIPTION DRUGS" (libellé historique). Sans cette
    # traduction, tous les OTC annulés étaient chargés avec is_otc=0 et le
    # moteur sortait leur nom de marque au lieu du principe actif.
    otc_codes = set()
    for r in read("schedule_ia.txt"):
        if len(r) >= 2 and r[1].strip() in {"OTC", "NON-PRESCRIPTION DRUGS"}:
            otc_codes.add(r[0].strip())

    # ingred_ia: DRUG_CODE -> active ingredient tokens (EN, FR)
    ing_by_drug = {}
    for r in read("ingred_ia.txt"):
        if len(r) < 3 or (len(r) > 3 and r[3] != "I"):
            continue  # 'I' = active ingredient
        code = r[0]
        en = r[2].strip()
        fr = r[11].strip() if len(r) > 11 else ""
        if not en and not fr:
            continue
        ing_by_drug.setdefault(code, []).append((en, fr))

    n_brand = 0
    seen_brand = seen
    # norm -> canonical base_generic, for resolving ingredient names to the
    # exact generic string already stored by the marketed ingestion.
    base_by_norm = {
        norm(b): b
        for (b,) in conn.execute("SELECT base_generic FROM medications WHERE level='BASE_GENERIC'")
    }
    for r in read("drug_ia.txt"):
        if len(r) < 5:
            continue
        if r[2].strip() != "Human":
            continue
        code = r[0].strip()
        brand_en = r[4].strip()
        # dpd_ia layout puts brand_fr at index 5 (the marketed layout uses 11).
        brand_fr = r[5].strip() if len(r) > 5 else ""
        for bname in {brand_en, brand_fr}:
            bname = bname.strip()
            if not bname:
                continue
            clean = clean_presentation(bname)
            if not clean or clean.upper() in {"HUMAN", "N/A", ""}:
                continue
            key = ("BRAND", norm(clean))
            if key in seen_brand:
                continue          # marketed brand of same name already ingested
            seen_brand.add(key)
            otc = 1 if code in otc_codes else 0
            # Active-ingredient base generic (French preferred, salt-stripped).
            base = ""
            ais = ing_by_drug.get(code, [])
            for en, fr in ais:
                if fr and strip_salts(fr, fr=True):
                    base = strip_salts(fr, fr=True)
                    break
            if not base:
                for en, fr in ais:
                    if en and strip_salts(en, fr=False):
                        base = strip_salts(en, fr=False)
                        break
            if not base:
                base = norm(clean)
            # Resolve to an existing canonical base_generic string if available.
            base = base_by_norm.get(norm(base), base)
            cur = conn.execute(
                "INSERT INTO medications(brand_name,base_generic,full_chemical_name,"
                "level,is_active,is_otc,source) VALUES(?,?,?,?,0,?,'DPD_CANCELLED')",
                (clean, base, norm(clean), "BRAND", otc),
            )
            conn.execute(
                "INSERT INTO medication_aliases(medication_id,alias_name,alias_type) "
                "VALUES(?,?, 'BRAND')",
                (cur.lastrowid, clean),
            )
            n_brand += 1
    print(f"inactive DPD ingested   -> brands={n_brand} (cancelled/dormant, is_active=0)")
    return n_brand


def read_dpd_table(fn):
    rows = []
    with open(fn) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            inner = line[1:-1] if line.startswith('"') and line.endswith('"') else line
            rows.append(inner.split('","'))
    return rows


def primary_active_generic(code, ing_by_drug):
    """Best base-generic name for an OTC product: its primary active ingredient.
    Prefers the French name (matches the matcher's FR_COMMON convention), falls
    back to English, skipping 'Historic Freeform' / empty entries."""
    items = ing_by_drug.get(code, set())
    cands = []
    for en, fr in items:
        if "HISTORIC FREEFORM" in (en or "").upper():
            continue
        base_fr = strip_salts(fr, fr=True) if fr else ""
        base_en = strip_salts(en, fr=False) if en else ""
        cands.append((base_fr, base_en))
    # Prefer any French base, else first English base.
    for base_fr, base_en in cands:
        if base_fr:
            return base_fr
    for base_fr, base_en in cands:
        if base_en:
            return base_en
    return ""


def main():
    drug = read_dpd_table(f"{DPD_DIR}/drug.txt")
    ingred = read_dpd_table(f"{DPD_DIR}/ingred.txt")

    # schedule.txt: DRUG_CODE -> schedule; OTC marks over-the-counter products.
    otc_codes = set()
    for r in read_dpd_table(f"{DPD_DIR}/schedule.txt"):
        if len(r) >= 2 and r[1].strip() == "OTC":
            otc_codes.add(r[0].strip())

    # ingred: DRUG_CODE -> set of (en_name, fr_name)
    ing_by_drug = {}
    for r in ingred:
        code = r[0]
        en = r[2].strip()
        fr = r[11].strip() if len(r) > 11 else ""
        ing_by_drug.setdefault(code, set()).add((en, fr))

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # Clear any rows from a previous build (dedupe `seen` is per-run only).
    conn.execute("DELETE FROM medication_aliases")
    conn.execute("DELETE FROM medications")

    seen = set()  # (level, normname) dedupe
    n_brand = n_generic = n_full = 0

    for r in drug:
        if len(r) < 12:
            continue
        category = r[2].strip()
        if category != "Human":
            continue  # drop Vet / Disinfectant / Radiopharm
        din = r[3].strip()
        brand_en = r[4].strip()
        brand_fr = r[11].strip() if len(r) > 11 else ""
        code = r[0].strip()

        ingredients = ing_by_drug.get(code, set())

        # --- Brand-name entry (level-preserve: brand stays brand) ---
        for bname in {brand_en, brand_fr}:
            bname = bname.strip()
            if not bname:
                continue
            # Clean weight/presentation suffixes for matching
            clean = BRAND_STOP.sub(" ", bname)
            clean = re.sub(r"\s+", " ", clean).strip().rstrip(".-")
            if not clean or clean.upper() in {"HUMAN", "N/A", ""}:
                continue
            key = ("BRAND", norm(clean))
            if key in seen:
                continue
            seen.add(key)
            otc = 1 if code in otc_codes else 0
            # OTC products output their true active-ingredient generic (so a
            # TYLENOL brand yields "acétaminophène", not the brand name).
            brand_base = norm(primary_active_generic(code, ing_by_drug)) if otc else norm(clean)
            if otc and not brand_base:
                brand_base = norm(clean)
            cur = conn.execute(
                "INSERT INTO medications(brand_name,base_generic,full_chemical_name,level,is_active,is_otc,source) "
                "VALUES(?,?,?,?,1,?,'DPD_MARKETED')",
                (clean, brand_base, norm(clean), "BRAND", otc),
            )
            conn.execute(
                "INSERT INTO medication_aliases(medication_id,alias_name,alias_type) VALUES(?,?,?)",
                (cur.lastrowid, clean, "BRAND"),
            )
            n_brand += 1
            # Brand-leaf aliases: the single clean leading word so bare-brand tokens
            # like "TYLENOL" match a multi-word brand (TYLENOL STRENGTH CAPLETS).
            # Marked BRAND_LEAF and guarded so common French words never become leaves.
            for leaf in norm(clean).split():
                if len(leaf) < 3 or leaf in BRAND_LEAF_STOP or leaf in FRENCH_COMMON_WORDS:
                    continue
                lkey = ("BRAND_LEAF", leaf)
                if lkey in seen:
                    continue
                seen.add(lkey)
                conn.execute(
                    "INSERT INTO medication_aliases(medication_id,alias_name,alias_type) "
                    "VALUES(?,?, 'BRAND_LEAF')", (cur.lastrowid, leaf),
                )

        # --- Generic / full-chemical entries from ingredients ---
        for (en, fr) in ingredients:
            base_en = strip_salts(en, fr=False)
            base_fr = strip_salts(fr, fr=True) if not fr.lower().startswith("historic") else base_en
            for base, is_fr in ((base_en, False), (base_fr, True)):
                base = base.strip()
                if not base:
                    continue
                key = ("BASE_GENERIC", base)
                if key not in seen:
                    seen.add(key)
                    cur = conn.execute(
                        "INSERT INTO medications(brand_name,base_generic,full_chemical_name,level,is_active,is_otc,source) "
                        "VALUES(?,?,?,?,1,?,'DPD_MARKETED')",
                        (None, base, base, "BASE_GENERIC", otc),
                    )
                    conn.execute(
                        "INSERT INTO medication_aliases(medication_id,alias_name,alias_type) VALUES(?,?,?)",
                        (cur.lastrowid, base, "BASE_GENERIC"),
                    )
                    n_generic += 1
            # full chemical = the raw ingredient name (EN + FR)
            for chem, is_fr in ((en, False), (fr, True)):
                if not chem or "HISTORIC FREEFORM" in chem.upper():
                    continue
                chem = chem.strip()
                key = ("FULL_GENERIC", chem.lower())
                if key not in seen:
                    seen.add(key)
                    cur = conn.execute(
                        "INSERT INTO medications(brand_name,base_generic,full_chemical_name,level,is_active,is_otc,source) "
                        "VALUES(?,?,?,?,1,?,'DPD_MARKETED')",
                        (None, norm(chem), chem, "FULL_GENERIC", otc),
                    )
                    conn.execute(
                        "INSERT INTO medication_aliases(medication_id,alias_name,alias_type) VALUES(?,?,?)",
                        (cur.lastrowid, chem, "FULL_GENERIC"),
                    )
                    n_full += 1

    conn.commit()
    print(f"marketed DPD ingested  -> brands={n_brand} base_generic={n_generic} full_chemical={n_full}")
    for lvl in ("BRAND", "BASE_GENERIC", "FULL_GENERIC"):
        c = conn.execute("SELECT COUNT(*) FROM medications WHERE level=?", (lvl,)).fetchone()[0]
        print(f"  medications[{lvl}] total = {c}")

    # Cancelled / inactive (discontinued & dormant) Canadian products, mapped to
    # their active-ingredient generic. is_active=0; marketed brands win dedup.
    ingest_inactive(conn, seen)

    # Manual brand -> generic aliases for the rare discontinued drugs whose DIN
    # is absent even from the inactive extract. These should no longer be needed
    # once the inactive extract is ingested (MAXERAN/LOPRESSOR are covered), but
    # kept as an auditable safety net for any future gaps.
    MANUAL_ALIASES = {}
    for alias, base in MANUAL_ALIASES.items():
        row = conn.execute(
            "SELECT id FROM medications WHERE base_generic=? AND level='BASE_GENERIC' LIMIT 1",
            (base,),
        ).fetchone()
        if row:
            conn.execute(
                "INSERT INTO medication_aliases(medication_id,alias_name,alias_type) "
                "VALUES(?,?, 'BRAND_LEAF')",
                (row[0], alias),
            )
            print(f"  manual alias -> {alias} = {base}")
        else:
            print(f"  !! manual alias target not found: {base}")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
