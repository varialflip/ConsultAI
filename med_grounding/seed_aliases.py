#!/usr/bin/env python3
"""Seed STT_GARBLE aliases linking observed/phonetic garbles to DPD meds."""
import sqlite3, unicodedata, re

DB = "./meds.sqlite"

# Map of garble -> (resolve brand, resolve base_generic). Prefer brand if present.
# These are the phonetic STT confusions we actually observed on the test instance
# (consultations 6/8/9) plus common French-language ASR confusions for
# cardiac/psych/geriatric meds. A garble row makes the token an EXACT match
# (S=100) in match_meds.Matcher, so it always resolves deterministically.
GARBLES = {
    # dictee-1 observed
    "l'a6":        ("lasix", "furosemide"),
    "l'asix":      ("lasix", "furosemide"),
    "asix":        ("lasix", "furosemide"),
    "laxic":       ("lasix", "furosemide"),
    "laxil":       ("lasix", "furosemide"),
    "lasiks":      ("lasix", "furosemide"),
    "celle-la":    ("celexa", "citalopram"),
    "selec ca":    ("celexa", "citalopram"),
    "selexa":      ("celexa", "citalopram"),
    "celixa":      ("celexa", "citalopram"),
    "trendal":     ("trandate", "labetalol"),
    "trandate 10": ("trandate", "labetalol"),
    "trandat":     ("trandate", "labetalol"),
    "datte":       ("trandate", "labetalol"),
    "dipitor":     ("lipitor", "atorvastatin"),
    "lipitax":     ("lipitor", "atorvastatin"),
    "pitax":       ("lipitor", "atorvastatin"),
    "dipipanone":  ("lipitor", "atorvastatin"),
    "tyrénol":     ("tylenol", "acetaminophen"),
    "tylénol":     ("tylenol", "acetaminophen"),
    "tirilnol":    ("tylenol", "acetaminophen"),
    "utilinole":   ("tylenol", "acetaminophen"),
    # dictee-6 observed
    "aricept":     ("aricept", None),
    "arisepte":    ("aricept", "donepezil"),
    "aricepte":    ("aricept", None),
    "rivastigmine":(None, "rivastigmine"),
    "l'aceto":     (None, "acetaminophen"),
    # --- consultation 6 (délirium agité, soins intensifs) ---
    "oxycontin":   ("oxycontin", "oxycodone"),
    "laldol":      ("haldol", "haloperidol"),
    "tilenol":     ("tylenol", "acetaminophen"),
    "lipitar":     ("lipitor", "atorvastatin"),
    # --- consultation 8 (trouble marche / confusion, polypharmacie) ---
    "spadex":      ("cipralex", "escitalopram"),
    "pival":       ("epival", "divalproex"),
    "espival":     ("epival", "divalproex"),
    "peridone":    (None, "risperidone"),
    "risperidone": (None, "risperidone"),
    # --- consultation 9 (démence Alzheimer, comportement) ---
    "ketiapine":   ("quetiapine", "quetiapine"),
    "dexilan":     ("dexilant", "dexilant"),
    # --- consultations 4/5/10/11 (troubles mnésiques / neurocognitifs) ---
    # « Hamelot d'épine » = amlodipine (bêta-bloquant), pas « adenine ».
    "hamelot d'épine": (None, "amlodipine"),
    "hamelot d epine": (None, "amlodipine"),
    "d'épine":     (None, "amlodipine"),
    "depine":      (None, "amlodipine"),
    "hydrochloroxia": (None, "hydrochlorothiazide"),
    "hydrochloroxyl": (None, "hydrochlorothiazide"),
    "val sartan":  (None, "valsartan"),
    "val sartans": (None, "valsartan"),
    "sartan":      (None, "valsartan"),
    "cipravex":    ("cipralex", "escitalopram"),
    "ciprabex":    ("cipralex", "escitalopram"),
    # « sous Eliquissé » = Eliquis (F.A.), pas « Latisse ».
    "liquissé":    ("eliquis", "apixaban"),
    "liquisse":    ("eliquis", "apixaban"),
    "eliquissé":   ("eliquis", "apixaban"),
    "eliquisse":   ("eliquis", "apixaban"),
    # generic French confusions
    "rosuvastatine http": (None, "rosuvastatin"),
    "rozu va statine":    (None, "rosuvastatin"),
    "simvastatine":       (None, "simvastatin"),
    "atrovastatine":      (None, "atorvastatin"),
    "metoprolol":         (None, "metoprolol"),
    "bisoprolol":         (None, "bisoprolol"),
    "perindopril":        (None, "perindopril"),
    "ramipril":           (None, "ramipril"),
    "amlodipine":         (None, "amlodipine"),
    "eliquis":            ("eliquis", None),
    "synthroid":          ("synthroid", "levothyroxine"),
    "levothyroxine":      (None, "levothyroxine"),
    "pantoloc":           ("pantoloc", "pantoprazole"),
    "pantolot":           ("pantoloc", "pantoprazole"),
    "pantoloque":         ("pantoloc", "pantoprazole"),
    "omeprazole":         (None, "omeprazole"),
    "apixaban":           (None, "apixaban"),
    "xarelto":            ("xarelto", None),
    "citalopram":         (None, "citalopram"),
    "seroplex":           ("seroplex", "escitalopram"),
    "escitalopram":       (None, "escitalopram"),
    "lexapro":            ("lexapro", "escitalopram"),
    "fluoxetine":         (None, "fluoxetine"),
    "prozac":             ("prozac", "fluoxetine"),
    "venlafaxine":        (None, "venlafaxine"),
    "effexor":            ("effexor", "venlafaxine"),
    "metformin":          (None, "metformin"),
    "glucophage":         ("glucophage", "metformin"),
    "insulin":            (None, "insulin"),
    "lantus":             ("lantus", None),
    "lipitor":            ("lipitor", "atorvastatin"),
    "atorvastatine":      (None, "atorvastatin"),
    "tramadol":           (None, "tramadol"),
    "tramacet":           ("tramacet", None),
    "hydromorphone":      (None, "hydromorphone"),
    "morphine":           (None, "morphine"),
    "codeine":            (None, "codeine"),
    "gabapentin":         (None, "gabapentin"),
    "pregabalin":         (None, "pregabalin"),
    "lyrica":             ("lyrica", "pregabalin"),
    "acetaminophen":      (None, "acetaminophen"),
    "diclofenac":         (None, "diclofenac"),
    "ibuprofen":          (None, "ibuprofen"),
    "advil":              ("advil", "ibuprofen"),
    "naproxen":           (None, "naproxen"),
    "celecoxib":          (None, "celecoxib"),
    "celebrex":           ("celebrex", "celecoxib"),
    "amoxicillin":        (None, "amoxicillin"),
    "clarithromycin":     (None, "clarithromycin"),
    "azithromycin":       (None, "azithromycin"),
    "levofloxacin":       (None, "levofloxacin"),
    "sertraline":         (None, "sertraline"),
    "zoloft":             ("zoloft", "sertraline"),
    "paroxetine":         (None, "paroxetine"),
    "paxil":              ("paxil", "paroxetine"),
    "bupropion":          (None, "bupropion"),
    "wellbutrin":         ("wellbutrin", "bupropion"),
    "trazodone":          (None, "trazodone"),
    "mirtazapine":        (None, "mirtazapine"),
    "quetiapine":         (None, "quetiapine"),
    "seroquel":           ("seroquel", "quetiapine"),
    "olanzapine":         (None, "olanzapine"),
    "aripiprazole":       (None, "aripiprazole"),
    "haloperidol":        (None, "haloperidol"),
    "clonazepam":         (None, "clonazepam"),
    "lorazepam":          (None, "lorazepam"),
    "ativan":             ("ativan", "lorazepam"),
    "diazepam":           (None, "diazepam"),
    "alprazolam":         (None, "alprazolam"),
    "xanax":              ("xanax", "alprazolam"),
    "zopiclone":          (None, "zopiclone"),
    "imovan":             ("imovan", "zopiclone"),
    "ramelteon":          (None, "ramelteon"),
    "melatonine":         (None, "melatonin"),
    "pantoprazole":       (None, "pantoprazole"),
    "rabeprazole":        (None, "rabeprazole"),
    "esomeprazole":       (None, "esomeprazole"),
    "lan soprazole":      (None, "lansoprazole"),
    "lansoprazole":       (None, "lansoprazole"),
    "famotidine":         (None, "famotidine"),
    "ranitidine":         (None, "ranitidine"),
    "domperidone":        (None, "domperidone"),
    "metoclopramide":     (None, "metoclopramide"),
    "maxeran":            ("maxeran", "metoclopramide"),
    "ondansetron":        (None, "ondansetron"),
    "zofran":             ("zofran", "ondansetron"),
    "gravol":             ("gravol", None),
    "dimenhydrinate":     (None, "dimenhydrinate"),
    "bisacodyl":          (None, "bisacodyl"),
    "lactulose":          (None, "lactulose"),
    "polyethylene glycol": ("restoralax", None),
    "polyethyleneglycol": ("restoralax", None),
    "peg":                ("restoralax", None),
    "peg 3350":           ("restoralax", None),
    "lax a day":          ("lax-a-day", None),
    "senna":              (None, "sennosides"),
    "sena":               (None, "sennosides"),
    "sénna":              (None, "sennosides"),
    "senokot":            (None, "sennosides"),
    "sénokot":            (None, "sennosides"),
    "docusate":           (None, "docusate"),
    # « 13 IBA » = homonyme phonétique de « Tresiba » (« treize » ≈ « trési »)
    # : le nombre n'est PAS la dose — c'est l'amorce du nom. Le garble est le
    # BIGRAMME complet « 13 iba », consommé comme un seul nom (jamais « iba »
    # seul qui deviendrait Tresiba en prose).
    "13 iba":             ("tresiba", "insulin degludec"),
    # extended Canadian ASR confusions (brand-oriented)
    "cymbalta":           ("cymbalta", "duloxetine"),
    "duloxetine":         (None, "duloxetine"),
    "remron":             ("remeron", "mirtazapine"),
    "reméron":            ("remeron", "mirtazapine"),
    "avanza":             ("avanza", "mirtazapine"),
    "lopressor":          ("lopressor", "metoprolol"),
    "coversyl":           ("coversyl", "perindopril"),
    "dioxan":             ("dioxan", "valsartan"),
    "cozaar":             ("cozaar", "losartan"),
    "hyzaar":             ("hyzaar", "losartan"),
    "januvia":            ("januvia", "sitagliptin"),
    "aldactone":          ("aldactone", "spironolactone"),
    "spironolactone":     (None, "spironolactone"),
    "cordarone":          ("cordarone", "amiodarone"),
    "amiodarone":         (None, "amiodarone"),
    "digoxin":            (None, "digoxin"),
    "eltroxin":           ("eltroxin", "levothyroxine"),
    "percocet":           ("percocet", "oxycodone"),
    "endocet":            ("endocet", "oxycodone"),
    "dilaudid":           ("dilaudid", "hydromorphone"),
    "ms contin":          ("ms contin", "morphine"),
    "duragesic":          ("duragesic", "fentanyl"),
    "fentanyl":           (None, "fentanyl"),
    "aleve":              ("aleve", "naproxen"),
    "mobic":              ("mobic", "meloxicam"),
    "tegretol":           ("tegretol", "carbamazepine"),
    "carbamazepine":      (None, "carbamazepine"),
    "depakene":           ("depakene", "valproic acid"),
    "lithium":            (None, "lithium"),
    "lamictal":           ("lamictal", "lamotrigine"),
    "keppra":             ("keppra", "levetiracetam"),
    "lioresal":           ("lioresal", "baclofen"),
    "baclofen":           (None, "baclofen"),
    "plavix":             ("plavix", "clopidogrel"),
    "clopidogrel":        (None, "clopidogrel"),
    "coumadin":           ("coumadin", "warfarin"),
    "warfarin":           (None, "warfarin"),
    "pradaxa":            ("pradaxa", "dabigatran"),
}

# Brand keys that have no clean DPD brand row (only presentation variants like
# "CIPRALEX -10MG" or OTC "TYLENOL STRENGTH ..."): the seed creates one clean
# BRAND row for them (brand_name=KEY, base_generic=base, is_otc=otc) so a garble
# resolves to the tidy canonical brand name ("Cipralex", "Tylenol").
CLEAN_BRANDS = {
    "cipralex": ("escitalopram", 0),
    "tylenol":  ("acetaminophen", 1),
}


def norm(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _build_indexes(conn):
    """Brand_name and base_generic -> medication id, keyed on norm() form."""
    brands = {}
    generics = {}
    for mid, bn, bg in conn.execute(
            "SELECT id, brand_name, base_generic FROM medications"):
        if bn:
            key = norm(bn)
            if key:
                brands.setdefault(key, mid)
        if bg:
            key = norm(bg)
            if key:
                generics.setdefault(key, mid)
    return brands, generics


def _ensure_clean_brand(conn, key, base, is_otc, brands, generics):
    if key in brands:
        return brands[key]
    # Insert one clean BRAND row (e.g. CIPRALEX -> escitalopram).
    cur = conn.execute(
        "INSERT INTO medications(brand_name, base_generic, full_chemical_name, "
        "level, is_active, is_otc, source) VALUES(?,?,?,?,1,?,'MANUAL_ALIAS')",
        (key.upper(), base, key, "BRAND", is_otc))
    mid = cur.lastrowid
    conn.execute(
        "INSERT INTO medication_aliases(medication_id, alias_name, alias_type) "
        "VALUES(?,?, 'BRAND')", (mid, key.upper()))
    brands[key] = mid
    print(f"  [clean brand created] {key.upper()} -> {base} (otc={is_otc})")
    return mid


def find_med_id(conn, brand, generic, brands, generics):
    """Resolve (brand, generic) to a medication id, preferring the brand."""
    if brand:
        key = norm(brand)
        if key in CLEAN_BRANDS:
            base, otc = CLEAN_BRANDS[key]
            return _ensure_clean_brand(conn, key, base, otc, brands, generics)
        if key in brands:
            return brands[key]
        # Fallback: exact alias-name match (covers leaves like 'cipralex').
        r = conn.execute(
            "SELECT medication_id FROM medication_aliases WHERE UPPER(alias_name)=? "
            "LIMIT 1", (brand.upper(),)).fetchone()
        if r:
            return r[0]
    if generic:
        key = norm(generic)
        if key in generics:
            return generics[key]
        r = conn.execute(
            "SELECT id FROM medications WHERE level='BASE_GENERIC' AND "
            "base_generic LIKE ? LIMIT 1",
            (f"%{key}%",)).fetchone()
        if r:
            return r[0]
    return None


def main():
    conn = sqlite3.connect(DB)
    brands, generics = _build_indexes(conn)
    missing = set()
    added = 0
    for garble, (brand, generic) in GARBLES.items():
        mid = find_med_id(conn, brand, generic, brands, generics)
        if mid is None:
            missing.add((garble, brand, generic))
            continue
        g_n = norm(garble)
        if not g_n:
            continue
        exists = conn.execute(
            "SELECT 1 FROM medication_aliases WHERE medication_id=? AND "
            "alias_name=? AND alias_type='STT_GARBLE'",
            (mid, g_n)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO medication_aliases(medication_id, alias_name, alias_type) "
            "VALUES(?,?, 'STT_GARBLE')",
            (mid, g_n))
        added += 1
    conn.commit()
    print(f"STT_GARBLE aliases added: {added}")
    if missing:
        print("  unresolved entries:")
        for garble, brand, generic in sorted(missing):
            print(f"    '{garble}' -> brand={brand} generic={generic}")
    total = conn.execute("SELECT COUNT(*) FROM medication_aliases").fetchone()[0]
    print(f"total aliases in DB: {total}")
    conn.close()


if __name__ == "__main__":
    main()