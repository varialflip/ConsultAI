#!/usr/bin/env python3
"""Seed STT_GARBLE aliases linking observed/phonetic garbles to DPD meds."""
import sqlite3, unicodedata, re

DB = "./meds.sqlite"

# Map of garble -> (resolve brand, resolve base_generic). Prefer brand if present.
# These are the phonetic STT confusions we actually observed this session plus
# common French-language ASR confusions for cardiac/psych meds.
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
    "lipitor 20":  ("lipitor", "atorvastatin"),
    "dipipanone":  ("lipitor", "atorvastatin"),
    "tylenol":     ("tylenol",None),        # already correct brand
    "tyrénol":     ("tylenol", "acetaminophen"),
    "tylénol":     ("tylenol", "acetaminophen"),
    "tyrénol 20":  ("tylenol",None),
    "tirilnol":    ("tylenol", "acetaminophen"),
    "utilinole":   ("tylenol", "acetaminophen"),
    # dictee-6 observed
    "aricept":     ("aricept", None),
    "arisepte":    ("aricept", "donepezil"),
    "aricepte":    ("aricept", None),
    "rivastigmine":(None, "rivastigmine"),
    "l'aceto":     (None,"acetaminophen"),
    # generic French confusions
    "rosuvastatine http":(None,"rosuvastatin"),
    "rozu va statine":(None,"rosuvastatin"),
    "simvastatine": (None,"simvastatin"),
    "atrovastatine":(None,"atorvastatin"),
    "metoprolol":   (None,"metoprolol"),
    "bisoprolol":   (None,"bisoprolol"),
    "perindopril":  (None,"perindopril"),
    "ramipril":     (None,"ramipril"),
    "amlodipine":   (None,"amlodipine"),
    "eliquis":      ("eliquis",None),
    "synthroid":    ("synthroid","levothyroxine"),
    "levothyroxine":(None,"levothyroxine"),
    "pantoloc":     ("pantoloc","pantoprazole"),
    "pantolot":     ("pantoloc","pantoprazole"),
    "pantoloque":   ("pantoloc","pantoprazole"),
    "omeprazole":   (None,"omeprazole"),
    "apixaban":     (None,"apixaban"),
    "xarelto":      ("xarelto",None),
    "citalopram":   (None,"citalopram"),
    "seroplex":     ("seroplex","escitalopram"),
    "escitalopram": (None,"escitalopram"),
    "lexapro":      ("lexapro","escitalopram"),
    "fluoxetine":   (None,"fluoxetine"),
    "prozac":       ("prozac","fluoxetine"),
    "venlafaxine":  (None,"venlafaxine"),
    "effexor":      ("effexor","venlafaxine"),
    "metformin":    (None,"metformin"),
    "glucophage":   ("glucophage","metformin"),
    "insulin":      (None,"insulin"),
    "lantus":       ("lantus",None),
    "lipitor":      ("lipitor","atorvastatin"),
    "atorvastatine":(None,"atorvastatin"),
    "tramadol":     (None,"tramadol"),
    "tramacet":     ("tramacet",None),
    "hydromorphone":(None,"hydromorphone"),
    "morphine":     (None,"morphine"),
    "codeine":      (None,"codeine"),
    "gabapentin":   (None,"gabapentin"),
    "pregabalin":   (None,"pregabalin"),
    "lyrica":       ("lyrica","pregabalin"),
    "acetaminophen":(None,"acetaminophen"),
    "tylenol extra":("tylenol","acetaminophen"),
    "diclofenac":   (None,"diclofenac"),
    "ibuprofen":    (None,"ibuprofen"),
    "advil":        ("advil","ibuprofen"),
    "naproxen":     (None,"naproxen"),
    "celecoxib":    (None,"celecoxib"),
    "celebrex":     ("celebrex","celecoxib"),
    "amoxicillin":  (None,"amoxicillin"),
    "clarithromycin":(None,"clarithromycin"),
    "azithromycin": (None,"azithromycin"),
    "levofloxacin": (None,"levofloxacin"),
    "sertraline":   (None,"sertraline"),
    "zoloft":       ("zoloft","sertraline"),
    "paroxetine":   (None,"paroxetine"),
    "paxil":        ("paxil","paroxetine"),
    "bupropion":    (None,"bupropion"),
    "wellbutrin":   ("wellbutrin","bupropion"),
    "trazodone":    (None,"trazodone"),
    "mirtazapine":  (None,"mirtazapine"),
    "quetiapine":   (None,"quetiapine"),
    "seroquel":     ("seroquel","quetiapine"),
    "olanzapine":   (None,"olanzapine"),
    "aripiprazole": (None,"aripiprazole"),
    "haloperidol":  (None,"haloperidol"),
    "clonazepam":   (None,"clonazepam"),
    "lorazepam":    (None,"lorazepam"),
    "ativan":       ("ativan","lorazepam"),
    "diazepam":     (None,"diazepam"),
    "alprazolam":   (None,"alprazolam"),
    "xanax":        ("xanax","alprazolam"),
    "zopiclone":    (None,"zopiclone"),
    "imovan":       ("imovan","zopiclone"),
    "ramelteon":    (None,"ramelteon"),
    "melatonine":   (None,"melatonin"),
    "pantoprazole": (None,"pantoprazole"),
    "omeprazole":   (None,"omeprazole"),
    "rabeprazole":  (None,"rabeprazole"),
    "esomeprazole": (None,"esomeprazole"),
    "lan soprazole":(None,"lansoprazole"),
    "lansoprazole": (None,"lansoprazole"),
    "famotidine":   (None,"famotidine"),
    "ranitidine":   (None,"ranitidine"),
    "domperidone":  (None,"domperidone"),
    "metoclopramide":(None,"metoclopramide"),
    "maxeran":      ("maxeran","metoclopramide"),
    "ondansetron":  (None,"ondansetron"),
    "zofran":       ("zofran","ondansetron"),
    "gravol":       ("gravol",None),
    "dimenhydrinate":(None,"dimenhydrinate"),
    "bisacodyl":    (None,"bisacodyl"),
    "lactulose":    (None,"lactulose"),
    "polyethylene glycol":(None,"polyethylene glycol"),
    "senna":        (None,"senna"),
    "docusate":     (None,"docusate"),
    "pantoloc":     ("pantoloc","pantoprazole"),
    "tm ":"",
}

def norm(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

def find_med_id(conn, brand, generic):
    if brand:
        r = conn.execute(
            "SELECT id FROM medications WHERE UPPER(brand_name)=? AND level='BRAND' LIMIT 1",
            brand.upper()).fetchone()
        if r: return r[0]
    if generic:
        r = conn.execute(
            "SELECT id FROM medications WHERE base_generic=? AND level='BASE_GENERIC' LIMIT 1",
            norm(generic)).fetchone()
        if r: return r[0]
        # fallback LIKE
        r = conn.execute(
            "SELECT id FROM medications WHERE base_generic LIKE ? AND level='BASE_GENERIC' LIMIT 1",
            f"%{norm(generic)}%").fetchone()
        if r: return r[0]
    return None

def main():
    conn = sqlite3.connect(DB)
    added = 0
    for garble, (brand, generic) in GARBLES.items():
        mid = find_med_id(conn, brand, generic)
        if mid is None:
            print(f"  ! no med row for garble '{garble}' (brand={brand} generic={generic})")
            continue
        g_n = norm(garble)
        exists = conn.execute(
            "SELECT 1 FROM medication_aliases WHERE medication_id=? AND alias_name=? AND alias_type='STT_GARBLE'",
            (mid, g_n)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO medication_aliases(medication_id, alias_name, alias_type) VALUES(?,?, 'STT_GARBLE')",
            (mid, g_n))
        added += 1
    conn.commit()
    print(f"STT_GARBLE aliases added: {added}")
    total = conn.execute("SELECT COUNT(*) FROM medication_aliases").fetchone()[0]
    print(f"total aliases in DB: {total}")
    conn.close()

if __name__ == "__main__":
    main()
