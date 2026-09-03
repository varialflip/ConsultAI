#!/usr/bin/env python3
"""Renseigne la table `common_meds` de la base avec la liste curatée des
médicaments « courants » (ordonnance géronto/gériatrique & ambulatoire).

Ces médicaments sont les plus dictés en consultation, donc les plus fréquemment
déformés par la reconnaissance vocale. Le moteur de grounding leur accorde un
bonus de score et abaisse légèrement ses barrières d'admission (constantes
COMMON_* dans ``app/med_grounding.py``) — voir README § Med Grounding.

Usage (depuis ``med_grounding/``) :
    python3 seed_common.py [chemin_vers_meds.sqlite ...]

Par défaut régénère `./meds.sqlite` et `../app/meds.sqlite` (les deux copies
livrées). La vêrification d'existence est douce : un nom absent du périmètre
DPD actuel est signalé et ignoré (la liste refuse d'inventer un médicament).
Les rangées se font par ordre de la liste (rank = position).
"""
import os
import sqlite3

#: Noms génériques de la liste curatée = ``base_generic`` de la table
#: ``medications`` (niveau BASE_GENERIC). L'ordre de la liste définit `rank`.
#: Les noms absents de la base (périmètre DPD / geriatrique) sont ignorés.
COMMON_GENERICS = [
    # Mémoire
    "donepezil", "rivastigmine", "galantamine", "memantine",
    # Antipsychotiques
    "quetiapine", "risperidone", "olanzapine", "aripiprazole", "clozapine",
    "paliperidone", "ziprasidone", "haloperidol", "loxapine", "chlorpromazine",
    # Antidépresseurs
    "sertraline", "escitalopram", "citalopram", "fluoxetine", "paroxetine",
    "fluvoxamine", "venlafaxine", "desvenlafaxine", "duloxetine", "bupropion",
    "mirtazapine", "trazodone", "vortioxetine", "amitriptyline", "nortriptyline",
    # Statines
    "rosuvastatin", "atorvastatin", "simvastatin", "pravastatin", "fluvastatin",
    # Antihypertenseurs
    "amlodipine", "bisoprolol", "candesartan cilexetil", "ramipril",
    "perindopril erbumine", "losartan", "valsartan", "telmisartan", "lisinopril",
    "metoprolol", "atenolol", "diltiazem", "hydrochlorothiazide", "indapamide",
    "furosemide",
    # Diabète / insulines
    "metformin", "empagliflozin", "dapagliflozin", "canagliflozin",
    "sitagliptin", "linagliptin", "saxagliptin", "gliclazide", "glyburide",
    "liraglutide", "dulaglutide", "pioglitazone",
    "insulin glargine", "insulin aspart", "insulin lispro", "insulin detemir",
    "insulin degludec",
    # Sommeil / benzodiazépines
    "zopiclone", "zolpidem", "lorazepam", "temazepam", "oxazepam", "clonazepam",
    "doxylamine",
    # Parkinson
    "levodopa", "pramipexole dihydrochloride", "ropinirole", "rasagiline",
    "amantadine",
    # Antiemétiques / allergies
    "ondansetron", "metoclopramide", "dimenhydrinate",
    "cetirizine", "loratadine", "fexofenadine", "desloratadine",
    "diphenhydramine", "chlorpheniramine",
    # IPP
    "pantoprazole", "omeprazole", "rabeprazole", "lansoprazole", "esomeprazole",
    # Anticoagulants / antiplaquettaires
    "apixaban", "rivaroxaban", "edoxaban", "warfarin", "clopidogrel",
    "ticagrelor",
    # Opioïdes
    "hydromorphone", "morphine", "oxycodone", "fentanyl", "tramadol",
    # Vessie hyperactive / HBP
    "oxybutynin chloride", "solifenacin", "tolterodine", "mirabegron",
    "darifenacin", "tamsulosin", "finasteride", "dutasteride", "alfuzosin",
    # Os / transit / rythme
    "alendronic acid alendronate sodium", "risedronate", "docusate", "digoxin",
    "levothyroxine", "acetaminophen", "ibuprofen",
]


def seed(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS common_meds ("
                 "medication_id INTEGER PRIMARY KEY REFERENCES medications(id),"
                 "rank INTEGER NOT NULL DEFAULT 0)")
    conn.execute("DELETE FROM common_meds")
    inserted = 0
    missed = []
    for rank, name in enumerate(COMMON_GENERICS):
        row = conn.execute(
            "SELECT id FROM medications WHERE base_generic=? "
            "AND level='BASE_GENERIC' ORDER BY id LIMIT 1", (name,)).fetchone()
        if not row:
            missed.append(name)
            continue
        conn.execute(
            "INSERT OR REPLACE INTO common_meds(medication_id, rank) VALUES(?,?)",
            (row[0], rank))
        inserted += 1
    conn.commit()
    conn.close()
    print(f"{os.path.basename(db_path)}: {inserted} médicaments courants")
    if missed:
        print(f"  ignorés (absents du périmètre) : {', '.join(missed)}")
    return inserted


def main():
    targets = []
    if len(os.sys.argv) > 1:
        targets = os.sys.argv[1:]
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        targets = [os.path.join(here, "meds.sqlite"),
                   os.path.join(here, "..", "app", "meds.sqlite")]
    for t in targets:
        if os.path.exists(t):
            seed(t)
        else:
            print(f"(skip: {t} absent)")


if __name__ == "__main__":
    main()
