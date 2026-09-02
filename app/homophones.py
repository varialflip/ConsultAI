"""
homophones.py — Table des homophonies et livres oralement, injectée à la volée.
================================================================================

POURQUOI SORTIR LA TABLE DE LA CONSIGNE GÉNÉRALE
------------------------------------------------
L'ancien tableau § 2.1 de la consigne générale grandissait sans borne :
chaque erreur de reconnaissance vocale ajoutait une ligne au prompt fourni
à TOUTES les générations, qu'elle concerne la consultation ou non. C'est le
même problème que la liste des médicaments : la solution est la même —
la faire vivre par donnée, pas par texte permanent.

Ici vit donc la table, sous forme de données. À la génération, on n'injecte
dans la requête QUE les lignes dont le fragment fautif apparaît réellement
dans la transcription de CETTE consultation (voir ``candidates_pertinents``).
La consigne générale ne garde que la MÉTHODOLOGIE (reconstruire du contexte
clinique, jamais du son isolé) : voir § 2.1 de ``default_prompts.py``.

RÈGLE DE TENUE -- CHAQUE AJOUT VA DANS UNE LIGNE ICI
----------------------------------------------------
Toute nouvelle homophonie observée sur une note réelle s'ajoute comme
entrée de ``TABLE_FR`` / ``TABLE_EN``, jamais comme phrase d'une consigne.
La clé de lecture reste la même : la reconnaissance vocale confond du
vocabulaire médical avec des mots courants, et la correction doit passer par
le contexte clinique.
"""

from __future__ import annotations

import unicodedata
from typing import List, Optional

# ---------------------------------------------------------------------------
# Table française : (erreur entendue/transcrite, lecture correcte, indice).
# ``erreur`` est cherché tel quel (normalisé sans accents, insensible à la
# casse) dans la transcription : insérer un fragment trop court produirait
# des faux déclenchements à chaque consultation.
# ---------------------------------------------------------------------------
TABLE_FR: List[dict] = [
    {
        "erreur": "casseur de saint droit",
        "lecture": "cancer du sein droit",
        "contexte": "latéralité + oncologie",
    },
    {
        "erreur": "casseur de saint gauche",
        "lecture": "cancer du sein gauche",
        "contexte": "latéralité + oncologie",
    },
    {
        "erreur": "amy parisi",
        "lecture": "hémiparésie",
        "contexte": "examen neurologique",
    },
    {
        "erreur": "amy parisie",
        "lecture": "hémiparésie",
        "contexte": "examen neurologique",
    },
    {
        "erreur": "parisie drotte",
        "lecture": "hémiparésie droite",
        "contexte": "latéralité + neurologie",
    },
    {
        "erreur": "parisie droit",
        "lecture": "hémiparésie droite",
        "contexte": "latéralité + neurologie",
    },
    {
        "erreur": "dix annexes",
        "lecture": "Xanax",
        "contexte": "anxiété / sleep",
    },
    {
        "erreur": "dix anatomies",
        "lecture": "Xanax",
        "contexte": "anxiété / sleep",
    },
    {
        "erreur": "dit étrozol",
        "lecture": "létrozole",
        "contexte": "cancer du sein hormono-dépendant",
    },
    {
        "erreur": "dit l'étrozol",
        "lecture": "létrozole",
        "contexte": "cancer du sein hormono-dépendant",
    },
    {
        "erreur": "antisystémique",
        "lecture": "antihistaminique",
        "contexte": "allergies",
    },
    {
        "erreur": "hôtel du québec",
        "lecture": "Hôtel-Dieu de Québec",
        "contexte": "établissement hospitalier",
    },
    {
        "erreur": "hôtel dieu du québec",
        "lecture": "Hôtel-Dieu de Québec",
        "contexte": "établissement hospitalier",
    },
    {
        "erreur": "aide au tovertan",
        "lecture": "HTO / hypotension orthostatique",
        "contexte": "vertiges, chutes",
    },
    {
        "erreur": "son droite",
        "lecture": "saint droit",
        "contexte": "orthographe d'usage (saint) — rare",
    },
    {
        "erreur": "pantoloque",
        "lecture": "Pantoloc",
        "contexte": "gastrite / RGO",
    },
    {
        "erreur": "monocore",
        "lecture": "Monocor (bisoprolol)",
        "contexte": "cardiovasculaire",
    },
    {
        "erreur": "pestor",
        "lecture": "Crestor",
        "contexte": "statine — dyslipidémie, coronaropathie",
    },
    {
        "erreur": "restore 5",
        "lecture": "Crestor 5",
        "contexte": "statine — dyslipidémie, coronaropathie",
    },
    {
        "erreur": "sélexa",
        "lecture": "Celexa",
        "contexte": "trouble dépressif",
    },
    {
        "erreur": "donné pézil",
        "lecture": "donépézil",
        "contexte": "démence / cognition",
    },
    {
        "erreur": "donné pézile",
        "lecture": "donépézil",
        "contexte": "démence / cognition",
    },
    {
        "erreur": "admelogue",
        "lecture": "Admelog",
        "contexte": "diabète",
    },
    {
        "erreur": "soixante dix huit",
        "lecture": "78",
        "contexte": "âge",
    },
    {
        "erreur": "soixante dix-huit",
        "lecture": "78",
        "contexte": "âge",
    },
]

# ---------------------------------------------------------------------------
# Table anglaise, mêmes règles.
# ---------------------------------------------------------------------------
TABLE_EN: List[dict] = [
    {
        "erreur": "cancer of the sane right",
        "lecture": "right breast cancer",
        "contexte": "oncologie, latéralité",
    },
    {
        "erreur": "sane right",
        "lecture": "right breast",
        "contexte": "oncologie, latéralité",
    },
    {
        "erreur": "right hemi thirty",
        "lecture": "right hemiparesis",
        "contexte": "examen neurologique",
    },
    {
        "erreur": "then acts",
        "lecture": "Xanax",
        "contexte": "anxiété / sleep",
    },
    {
        "erreur": "ten annexes",
        "lecture": "Xanax",
        "contexte": "anxiété / sleep",
    },
    {
        "erreur": "let throw zole",
        "lecture": "letrozole",
        "contexte": "cancer du sein hormono-dépendant",
    },
    {
        "erreur": "anti systemic",
        "lecture": "antihistamine",
        "contexte": "allergies",
    },
    {
        "erreur": "the hotel du quebec",
        "lecture": "Hôtel-Dieu de Québec",
        "contexte": "établissement hospitalier",
    },
    {
        "erreur": "ortho static hypo tension",
        "lecture": "orthostatic hypotension",
        "contexte": "vertiges, chutes",
    },
    {
        "erreur": "pantoloque",
        "lecture": "Pantoloc",
        "contexte": "gastrite / RGO",
    },
]


def _normaliser(texte: str) -> str:
    """Minuscules, accents retirés, espaces resserrés — pour le rapprochement."""
    if not texte:
        return ""
    nfkd = unicodedata.normalize("NFKD", texte.lower())
    sans = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(sans.split())


def candidates_pertinents(
    texte: str,
    langue: str = "fr",
    maxi: int = 6,
) -> List[dict]:
    """
    Lignes de la table dont le fragment fautif figure dans la transcription.

    Seules ces lignes sont injectées dans la requête du modèle : la table
    entière ne voyage jamais dans le prompt. ``maxi`` borne la liste pour
    qu'une consultation truffée d'homophonies n'étende pas le message
    utilisateur.
    """
    if not texte:
        return []
    table = TABLE_EN if str(langue).lower().startswith("en") else TABLE_FR
    texte_norm = _normaliser(texte)
    resultats: List[dict] = []
    for entree in table:
        motif = _normaliser(entree["erreur"])
        if motif and motif in texte_norm:
            resultats.append(dict(entree))
            if len(resultats) >= maxi:
                break
    return resultats