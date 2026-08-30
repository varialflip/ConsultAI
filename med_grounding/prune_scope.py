#!/usr/bin/env python3
"""Retire de meds.sqlite les classes thérapeutiques hors périmètre clinique
gériatrique, pour une base d'ancrage plus rapide et moins bruyante.

Un grounder consult geriatrique ne dicte jamais :
- les vaccins / extraits allergéniques / immunoglobulines (J07, V01, J06) ;
- les produits de contraste et agents diagnostiques (V04, V07, V08, V09) ;
- les gaz médicaux (V03AN) et solutés IV / dialyse / irrigation (B05) ;
- l'hygiène et la cosmétique : désinfectants mains (D08A), émollients/huiles
  (D02A), écrans solaires résiduels (D02B), anti-acné (D10), antisudorifiques
  (D11AA), shampoings (D11AC), verrues (D11AF), alopécie/cosmétique (D11AX),
  soins dentaires/fluor (A01A), pastilles/rince-bouche (D-gorge R02A),
  antiprurigineux/anesthésiques topiques OTC (D04A), rubéfiants OTC (M02A) ;
- les anesthésiques (N01A généraux, N01B locaux/dentaires) ;
- les sirops toux/rhume OTC (R05C/F/X, R05DA09/20, R05DB) et les
  décongestionnants nasaux (R01AA/AB/AC/AX) ;
- la « soupe » de suppléments (A11A/B/E/G/J, A12C), la contraception (G03A),
  l'obstétrique/gynéco (G02), la fertilité (G03G), les anthelminthiques (P02) ;
- l'homéopathie (annexe BDP schedule = HOMEOPATHIC) ;
- les marques cosmétiques/pédiatriques repérables au nom (SCHAMPOO, DEODORANT,
  HAND SANITIZER, CHILDREN'S, PRENATAL...) ;
- les marques annulées sans aucune classe ATC (produits de santé naturels d'une
  époque où le PSN n'existait pas, souvent porteurs d'alias courts dangereux :
  ART, PUL, LC, SET...).

Une **liste de sauvegarde explicite** force la conservation de exceptions
cliniquement parlantes au sein de ces classes (diclofénac topique, Xylocaïne,
Zincofax, Peridex, vaccins gériatriques, codéine...). Retirer une marque ne
retire jamais le générique (lignes BASE_GENERIC distinctes) : le risque d'un
retrait est « nom non normalisé », jamais « nom normalisé faussement ».

Idempotent / dry-runnable comme prune_db.py et ban_terms.py.
"""
import re
import sqlite3
import sys
import unicodedata

from build_db import clean_presentation, norm, read_dpd_table

DB = "./meds.sqlite"

#: Classes ATC entièrement hors périmètre clinique gériatrique. Un préfixe
#: ATC4/ATC3 (les 4 premières lettres) cible le groupe ; une ligne n'est
#: retirée que si TOUTES ses classes ATC sont dans le périmètre retiré.
DROP_ATC_PREFIXES = (
    "J07",    # vaccins (sauf liste de sauvegarde)
    "V01",    # extraits allergéniques
    "J06",    # immunoglobulines / sérums
    "V04",    # agents diagnostiques
    "V07",    # non-thérapeutiques (eau stérile, conservateurs...)
    "V08",    # produits de contraste
    "V09",    # radiopharmaceutiques diagnostiques
    "V03AN",  # gaz médicaux (oxygène, air médical, azote...)
    "B05",    # solutés IV / irrigation / dialyse / nutrition parentérale
    "D02A",   # émollients / protecteurs cutanés, huiles
    "D02B",   # restes d'écrans solaires non couverts par ban_terms
    "D04A",   # antiprurigineux / anesthésiques topiques OTC
    "D08A",   # désinfectants / antiseptiques mains et surfaces
    "D10",    # anti-acné
    "D11AA",  # antisudorifiques
    "D11AC",  # shampoings médicamenteux / antipelliculaires
    "D11AF",  # verrues / cors
    "D11AX",  # alopécie, dépilation cosmétique
    "A01A",   # hygiène dentaire / fluor
    "R02A",   # gorge / pastilles / rince-bouche
    "N01A",   # anesthésiques généraux
    "N01B",   # anesthésiques locaux (dentaires, ORL, régionales)
    "M02A",   # topiques antalgiques / rubéfiants OTC
    "R05C",   # expectorants
    "R05F",   # antitusifs+expectorants « combinés »
    "R05X",   # préparations « rhume » diverses
    "R05DA09", "R05DA20", "R05DB",  # dextrométhorphane/combinés, clofédanol
    "R01AA",  # sympathomimétiques nasaux (oxymétazoline, xylométazoline...)
    "R01AB",  # décongestionnants nasaux + anti-allergiques
    "R01AC",  # cromoglycate intranasal OTC
    "R01AX",  # divers nasaux OTC
    "A11A",   # multivitamines + minéraux
    "A11B",   # multivitamines simples
    "A11E",   # complexe B
    "A11G",   # vitamine C
    "A11J",   # vitamines + autres produits
    "A12C",   # oligo-éléments
    "G02",    # produits gynécologiques / obstétriques
    "G03A",   # contraceptifs hormonaux
    "G03G",   # gonadotrophines / fertilité
    "P02",    # anthelminthiques (hors listes)
)

#: Marques à CONSERVER au sein des classes ci-dessus (raisons cliniques
#: gériatriques explicites dans le README). Testé sur le nom de marque.
KEEP_BRANDS = re.compile(
    r"SHINGRIX|PNEUMOVAX|PREVNAR|CAPVAXIVE|VAXNEUVANCE|FLUZONE|FLUAD|AREXVY|ABRYSVO"
    r"|VOLTAREN|PENNSAID|DICLOFENAC"
    r"|XYLOCAINE|LIDOCAINE|LIDODAN|EMLA|MAXILENE|ZENSA|AMETOP"
    r"|ZINCOFAX|BAZA|CAVILON|PROSHIELD|BARRIERE|CRITIC-AID|SWEEN|AQUAPHOR|COMFORT SHIELD"
    r"|PERIDEX|PERIOGARD|CHLORHEXIDINE|ORO.?CLENSE|ORO CLEAR|BENZYDAMINE|PHARIXIA"
    r"|ORACORT|ORABASE|DOXYCYCLINE|ARESTIN"
    r"|ROSIVER|ONRELTEA|FINACEA|METRONIDAZOLE"
    r"|IPRAVENT|RHINARIS"
    r"|CODEINE|HYDROCODONE|ACETYLCYSTEINE|PULMOZYME"
    r"|TYLENOL|NIX|KWELLADA|RESULTZ|NYDA|STROMECTOL|IVERMECTIN"
    r"|DOSTINEX|CABERGOLINE|BROMOCRIPTINE|PARLODEL"
    r"|TIBSOVO|CIPRALEX",
    re.IGNORECASE,
)

#: Motifs de nom indiquant un cosmétique / un produit pédiatrique, quel que
#: soit son classement ATC (mots devenant des alias de marque sources de faux
#: positifs en prose : SET, AND, FOR, HANDS, WIPES, MOTHERS...).
COSMETIC_PEDIATRIC = re.compile(
    r"\b(shampoo|shampooing|conditioner|revitalisant|deodorant|antiperspirant|"
    r"antipersp|hand\s*(soap|wash|sanitiz)|skin\s*care|sanitizer|sanitizing|"
    r"wipes?|towelette|serviette|toothpaste|dentifrice|body\s*(wash|cleanser|"
    r"lotion)|lip\s*balm|make[- ]?up|cosmetic|acne|dandruff|antipellicul|bronz|"
    r"tanning|nail\s*polish|acne\s*treatment)\b"
    r"|\b(children|childrens|child|kids|junior|jr|baby|babies|infant|"
    r"nourrisson|toddler|pediatric|pregnancy|prenatal|maternity)\b",
    re.IGNORECASE,
)

#: Schedule BDP qui identifie l'homéopathie dans l'annexe des produits inactifs.
HOMEOPATHIC_SCHEDULE = frozenset({"HOMEOPATHIC"})


def main() -> None:
    dry = "--apply" not in sys.argv
    conn = sqlite3.connect(DB)

    # ------------------------------------------------------------- parse BDP
    # nom normalisé -> classes ATC; nom normalisé -> schedules
    brand_atc, brand_sched = {}, {}
    for d, drugf, therf, schedf, fr_idx in (
        ("dpd", "drug.txt", "ther.txt", "schedule.txt", 11),
        ("dpd_ia", "drug_ia.txt", "ther_ia.txt", "schedule_ia.txt", 5),
    ):
        atc = {}
        for r in read_dpd_table(f"{d}/{therf}"):
            if len(r) >= 2 and r[1].strip():
                atc.setdefault(r[0].strip(), set()).add(r[1].strip())
        sched = {}
        for r in read_dpd_table(f"{d}/{schedf}"):
            if len(r) >= 2:
                sched.setdefault(r[0].strip(), set()).add(r[1].strip().upper())
        for r in read_dpd_table(f"{d}/{drugf}"):
            if len(r) < 6 or r[2].strip() != "Human":
                continue
            code = r[0].strip()
            names = {r[4].strip()}
            if len(r) > fr_idx:
                names.add(r[fr_idx].strip())
            for nm in names:
                if not nm:
                    continue
                for key in {norm(nm), norm(clean_presentation(nm))}:
                    key = re.sub(r"\s+", " ", key).strip()
                    if not key:
                        continue
                    brand_atc.setdefault(key, set()).update(atc.get(code, set()))
                    brand_sched.setdefault(key, set()).update(sched.get(code, set()))

    # ------------------------------------------------------------- sélection
    rows = conn.execute(
        "SELECT id, brand_name, base_generic, level, is_active, is_otc "
        "FROM medications WHERE level='BRAND'"
    ).fetchall()
    alias_n = dict(
        conn.execute(
            "SELECT medication_id, count(*) FROM medication_aliases GROUP BY 1"
        )
    )

    killed = {}  # id -> rule
    for mid, brand, base, lvl, act, otc in rows:
        if KEEP_BRANDS.search(brand or ""):
            continue
        atcs = brand_atc.get(norm(brand or ""), set())
        scheds = brand_sched.get(norm(brand or ""), set())
        if atcs and all(
            any(a.startswith(p) for p in DROP_ATC_PREFIXES) for a in atcs
        ):
            killed[mid] = "A: classes ATC hors périmètre"
            continue
        if scheds and scheds <= HOMEOPATHIC_SCHEDULE:
            killed[mid] = "B: schedule homéopathie"
            continue
        if COSMETIC_PEDIATRIC.search(brand or ""):
            killed[mid] = "C: nom cosmétique/pédiatrique"
            continue
        if not atcs and not act:
            killed[mid] = "D: marque annulée sans classe ATC (PSN / herboristerie)"

    aliases = conn.execute(
        "SELECT id, medication_id FROM medication_aliases"
    ).fetchall()

    # ------------------------------------------------------------- audit
    print(f"marques à retirer : {len(killed)} / {len(rows)}")
    by_rule = {}
    for mid, rule in killed.items():
        by_rule.setdefault(rule[:1], []).append(mid)
    groups = {
        "A": "classes ATC hors périmètre",
        "B": "schedule homéopathie",
        "C": "nom cosmétique/pédiatrique",
        "D": "annulée sans classe ATC (PSN / herboristerie)",
    }
    for r in "ABCD":
        g = by_rule.get(r, [])
        print(f"\n[{r}] {groups[r]} : {len(g)} marques")
        for mid in g[:12]:
            mid, bn, bg, *_ = next(x for x in rows if x[0] == mid)
            print(f"    {str(bn)[:38]:38} <- {bg[:30]}")
    alias_gone = sum(alias_n.get(m, 0) for m in killed)
    print(f"\naliases correspondants : {alias_gone} "
          f"(dont {len(aliases)} au total avant)")

    if dry:
        print("[dry-run] passe --apply pour appliquer")
        conn.close()
        return

    conn.execute("BEGIN")
    if killed:
        ph = ",".join("?" * len(killed))
        conn.execute(
            "DELETE FROM medication_aliases WHERE medication_id IN (%s)" % ph,
            list(killed),
        )
        conn.execute(
            "DELETE FROM medications WHERE id IN (%s)" % ph, list(killed)
        )
    conn.commit()
    vm = conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0]
    va = conn.execute("SELECT COUNT(*) FROM medication_aliases").fetchone()[0]
    print(f"après : medications={vm}, aliases={va}")
    conn.close()


if __name__ == "__main__":
    main()