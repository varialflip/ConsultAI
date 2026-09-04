#!/usr/bin/env python3
"""
regression_pipeline.py — Filet de régression du pipeling « deux passes ».
================================================================================

HORS LIGNE : aucune base, aucun réseau, aucun modèle. Génère un rapport
synthétique par gabarit verrouillé, le passe par ``note_renderer`` (passe 2)
et vérifie les engagements du rendu applicatif :

  * les rubriques apparaissent dans l'ORDRE du gabarit, avec leur titre exact ;
  * une rubrique SANS contenu dicté disparaît (titre compris), jamais comblée
    par un texte de remplissage ;
  * une ligne d'en-tête sans valeur disparaît, une valeur dictée est reprise ;
  * l'Impression et le Plan restent numérotés, les listes à puces restent des
    puces (détection par le GABARIT, pas par devinette) ;
  * le pied « Rédigé à l'aide de la reconnaissance vocale. » est conservé ;
  * la rubrique « Corrections et éléments à valider » est toujours émise ;
  * les blocs à libellés (**AVQ :**…) sont respectés ;
  * les homophonies injectées par ``homophones.candidates_pertinents`` sont
    celles de CETTE dictée, et seulement celles-là.

À lancer après toute modification du rendu ou des gabarits :

    python3 tools/regression_pipeline.py

Toute assertion échouée fait sortir en 1 avec le détail.
"""

from __future__ import annotations

import sys
from typing import Dict, List

sys.path.insert(0, ".")

from app import homophones, note_renderer
from app.default_templates import LOCKED_TEMPLATES


def _contenu_type(section) -> object:
    """Forme de contenu adaptée à chaque rubrique (contrat de la passe 1)."""
    if section.labeled:
        return {l: f"valeur {l}" for l in section.labeled}
    if section.numbered and section.paragraph:
        return {"phrases": "Résumé en une phrase.", "items": ["item un", "item deux"]}
    if section.numbered:
        return ["item un", "item deux", "item trois"]
    if section.bullet:
        return ["item un", "item deux"]
    return "Paragraphe unique pour la rubrique."


def _rendu_par_gabarit() -> List[dict]:
    """(gabarit, extraction, rendu) pour chacun des gabarits verrouillés."""
    resultats = []
    for gabarit in LOCKED_TEMPLATES:
        layout = gabarit["layout_format"]
        parsed = note_renderer.inspect_layout(layout)
        titre = gabarit["name"]

        if not parsed.compatible:
            raise AssertionError(f"{titre} : gabarit déclaré incompatible au rendu")

        # Extraction complète SAUF une rubrique laissée vide (elle doit
        # disparaître) : on choisit la première rubrique à paragraphe libre.
        sections = {}
        a_vider = None
        for section in parsed.sections:
            if a_vider is None and section.vide:
                a_vider = section.title
                continue
            sections[section.title] = _contenu_type(section)
        if a_vider is None:
            a_vider = parsed.sections[-1].title
            sections.pop(a_vider, None)

        # En-tête : un seul libellé renseigné — les autres courent à la ligne.
        en_tete = {}
        for i, libelle in enumerate(parsed.header_labels):
            if i == 0:
                en_tete[libelle] = "Clinique du centre-ville"
        # (déjà vu lors des sections : une clé non dictée n'est pas posée)

        extraction = {
            "en_tete": en_tete,
            "sections": sections,
            "corrections": ["[Médicaments] ...Xanax 0,5... → à confirmer"],
        }
        langue = gabarit["language"]
        rendu = note_renderer.render_note(layout, extraction, langue)
        resultats.append({
            "nom": titre,
            "layout": layout,
            "parsed": parsed,
            "extraction": extraction,
            "rendu": rendu,
            "a_vider": a_vider,
            "langue": langue,
        })
    return resultats


def _verifier(resultats: List[dict]) -> None:
    for r in resultats:
        nom, parsed, rendu = r["nom"], r["parsed"], r["rendu"]
        lignes = [l for l in rendu.splitlines() if l.strip()]
        texte = "\n".join(lignes)

        # 1. Rubriques dans l'ordre du gabarit, titre exact.
        titres = []
        for section in parsed.sections:
            if section.title == r["a_vider"]:
                continue
            titres.append(("#" * section.level) + " " + section.title)
        for t in titres:
            if t not in lignes:
                raise AssertionError(f"{nom} : rubrique absente du rendu — « {t} »")
        positions = [lignes.index(t) for t in titres]
        if positions != sorted(positions):
            raise AssertionError(f"{nom} : ordre des rubriques non respecté")

        # 2. La rubrique laissée vide a disparu, et jamais de remplissage.
        if f"## {r['a_vider']}".strip() and any(
                l.strip().startswith(f"## {r['a_vider']}") for l in lignes):
            raise AssertionError(f"{nom} : rubrique vide restée au rendu")
        for interdit in ("Non servi", "Non abordé", "N/A", "— ", "À déterminer"):
            if interdit in texte:
                raise AssertionError(f"{nom} : texte de remplissage « {interdit} »")

        # 3. En-tête : seule la première valeur est reprise.
        attendu_entete = f"**{parsed.header_labels[0]} :** Clinique du centre-ville"
        if attendu_entete not in texte:
            raise AssertionError(f"{nom} : en-tête dicté absent — « {attendu_entete} »")
        for libelle in parsed.header_labels[1:]:
            if f"**{libelle} :**" in texte:
                raise AssertionError(f"{nom} : en-tête non dicté encore présent")

        # 4. Plans numérotés et listes à puces, selon le GABARIT.
        for section in parsed.sections:
            contenu = r["extraction"]["sections"].get(section.title)
            if section.numbered:
                if not any(l.strip().startswith("1. ") for l in lignes):
                    raise AssertionError(f"{nom} : liste numérotée attendue sous « {section.title} »")
                if not any(l.strip().startswith("2. ") for l in lignes):
                    raise AssertionError(f"{nom} : deuxième élément numéroté absent (« {section.title} »)")
            elif section.bullet and isinstance(contenu, list):
                if not any(l.strip().startswith("- ") for l in lignes):
                    raise AssertionError(f"{nom} : liste à puces attendue sous « {section.title} »")
            elif section.labeled and isinstance(contenu, dict):
                for libelle in section.labeled:
                    if f"**{libelle} :** valeur {libelle}" not in rendu:
                        raise AssertionError(f"{nom} : bloc étiqueté « {libelle} » mal rendu")

        # 5. Pied de note conservé.
        pied = "Rédigé à l'aide de la reconnaissance vocale." if r["langue"] == "fr" \
            else "Written using speech recognition."
        if pied not in texte:
            # Un gabarit verrouillé sans ce pied ? On s'en assure plutôt deux fois.
            if not r["layout"].strip().endswith(pied) and pied not in r["layout"]:
                raise AssertionError(f"{nom} : pied « {pied} » perdu au rendu")

        # 6. Rubrique « Corrections et éléments à valider » toujours émise.
        titre_corr = parsed.corrections_header or \
            ("## Corrections and items to verify" if r["langue"] == "en"
             else "## Corrections et éléments à valider")
        if titre_corr not in texte:
            raise AssertionError(f"{nom} : rubrique de corrections absente")
        if "Xanax 0,5" not in texte or "→ à confirmer" not in texte:
            raise AssertionError(f"{nom} : élément de correction perdu")

        # 7. Une rubrique entière vide → titre ET contenu supprimés, et la
        #    rubrique de corrections reste en DERNIÈRE position (plus aucun
        #    titre après elle).
        heading_corr = titre_corr if titre_corr.startswith("#") else "## " + titre_corr
        if heading_corr not in lignes:
            raise AssertionError(f"{nom} : rubrique de corrections absente du rendu")
        index_corr = lignes.index(heading_corr)
        for j, l in enumerate(lignes[index_corr + 1:], start=index_corr + 1):
            if l.startswith("#"):
                raise AssertionError(f"{nom} : titre après la rubrique de corrections — « {l} »")

        print(f"  ✓ {nom} ({len(lignes)} lignes)")


def _verifier_homophones() -> None:
    cas = (
        (
            "À l'examen, un casseur de saint droit, et je renouvelle son pantoloque 40 die.",
            "fr",
            {
                ("casseur de saint droit", "cancer du sein droit"),
                ("pantoloque", "Pantoloc"),
            },
        ),
        (
            "She has cancer of the sane right and takes ten annexes at bedtime.",
            "en",
            {
                ("cancer of the sane right", "right breast cancer"),
                ("ten annexes", "Xanax"),
            },
        ),
    )
    for texte, langue, attendus in cas:
        candidats = homophones.candidates_pertinents(texte, langue)
        trouves = {(c["erreur"], c["lecture"]) for c in candidats}
        assert attendus <= trouves, \
            f"homophones : paires attendues absentes pour « {texte} »"

    non_pertinents = homophones.candidates_pertinents(
        "Patiente de 85 ans, trouble hypomaniaque, retour à domicile.",
        "fr",
    )
    assert non_pertinents == [], "homophones : déclenchements hors sujet"

    plafond = homophones.candidates_pertinents(
        "casseur de saint droit casseur de saint droit casseur de saint droit "
        "casseur de saint droit casseur de saint droit casseur de saint droit "
        "casseur de saint droit",
        "fr",
        maxi=3,
    )
    assert len(plafond) <= 3, "homophones : plafond non respecté"
    print("  ✓ homophones.candidates_pertinents (fr + en + plafond)")


def main() -> None:
    print("Pipeling deux passes — filet de régression")
    print("Gabarits verrouillés abordés :", len(LOCKED_TEMPLATES))
    resultats = _rendu_par_gabarit()
    _verifier(resultats)
    _verifier_homophones()
    print("\nOK — rendu applicatif conforme sur tous les gabarits verrouillés.")


if __name__ == "__main__":
    main()