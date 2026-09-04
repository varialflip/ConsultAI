"""
geriatric_terms.py — Termes gériatriques québécois corrigés avant le LLM.
================================================================================

POURQUOI UN MODULE SÉPARÉ DE MÉDICAMENTS
----------------------------------------
Les médicaments déformés par la reconnaissance vocale sont corrigés par
``med_grounding`` (moteur déterministe : base DPD, liste curatée
``common_meds.json``, correction inline + suggestions). Les TERMES gériatriques
— établissements, conditions, tests cognitifs, abréviations (Hôtel-Dieu,
HTO/CHSLD, MMSE, AVQ…) — n'y ont pas leur place : ce ne sont pas des
médicaments, et les chercher dans la base DPD serait faux.

Ce module vit donc À PART, alimenté par ``geriatric_terms.json``. Il offre
DEUX canaux, comme ``med_grounding`` mais pour des termes non médicamenteux :

  * ``apply_inline_replacements`` — remplacements DÉTERMINISTES dans le texte
    (canonicalisation d'abréviations, orthographe d'établissement), appliqués
    AVANT le LLM : l'erreur est corrigée dans le texte, zéro attention du
    modèle. Chaque remplacement est explicitement curaté dans le JSON.
  * ``pertinent_hints`` — candidats AMBIGUS laissés au jugement clinique du
    LLM (homophonies à lecture possible multiple), injectés dans le bloc
    <<<HOMOPHONIES_CE_CALL>>>. Un terme sans ambiguïté n'y figure pas : s'il
    a une lecture unique sûre, il est remplacé (canal précédent), pas suggéré.

COLLISIONS AVEC LES MÉDICAMENTS
-------------------------------
Le JSON ne porte AUCUN nom de médicament (règle de tenue, cf. en-tête). En
doublure, ``apply_inline_replacements`` accepte un jeu ``protect`` : tout
jeton présent dans ce jeu (les jetons déjà corrigés par l'inline de
``med_grounding`` à la même génération) ne sera JAMAIS retiré ni réécrit ici —
le médicament gagne sur la collision. Voir ``main.py`` (api_generate).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

#: Chemin du JSON curaté, chargé au démarrage de chaque appel (léger).
JSON_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                         "geriatric_terms.json")


def _normaliser(texte: str) -> str:
    """Minuscules, accents retirés, espaces resserrés — clé de rapprochement."""
    if not texte:
        return ""
    nfkd = unicodedata.normalize("NFKD", texte.lower())
    sans = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(sans.split())


# ---------------------------------------------------------------------------
# Chargement du JSON (curaté, petit — on le relit à la volée)
# ---------------------------------------------------------------------------
def _charger() -> Dict[str, list]:
    try:
        with open(JSON_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # pragma: no cover — fichier livré ; on ne fait pas tomber l'app
        return {"deterministic_replacements": [], "prompt_hints": []}


def _filtre_langue(donnees: list, langue: str) -> list:
    cible = "en" if str(langue).lower().startswith("en") else "fr"
    return [
        d for d in donnees
        if str(d.get("langue", "fr")).lower().startswith(cible)
    ]


def liste_remplacements(langue: str = "fr") -> List[dict]:
    """Remplacements déterministes pour la langue demandée."""
    return _filtre_langue(_charger().get("deterministic_replacements") or [], langue)


def liste_hints(langue: str = "fr") -> List[dict]:
    """Candidats ambigus (bloc <<<HOMOPHONIES_CE_CALL>>>) pour la langue."""
    return _filtre_langue(_charger().get("prompt_hints") or [], langue)


# ---------------------------------------------------------------------------
# Canal 1 — remplacements inline déterministes
# ---------------------------------------------------------------------------
#: Plis d'accents pour construire des classes de caractères insensibles aux
#: accents : `a` matche `a`, `à`, `â`, `ä`… Indispensable pour frapper
#: « Hôtel-Dieu », « mini mental »… quel que soit l'accent livré par le STT.
_FOLD_ACCENTS = {
    "a": "aàâäáãå", "c": "cç", "e": "eéèêë", "i": "iîïíì",
    "n": "nñ", "o": "oôöóòõ", "u": "uùûüú", "y": "yÿ",
}


def _classe(car: str) -> str:
    """Classe regex insensible aux accents pour ``car`` (une lettre)."""
    if car in _FOLD_ACCENTS:
        # Une classe de caractères : chaque lettre n'est qu'un caractère.
        return f"[{_FOLD_ACCENTS[car]}]"
    return re.escape(car)


def _motif(cible: str) -> str:
    """Regex (casse + accents insensibles) correspondant à ``cible``."""
    return "".join(_classe(c) for c in cible)


def _prep(texte: str):
    """Minuscules sans toucher aux accents — pour un ``find`` fidèle.

    Ne sert qu'à retrouver l'emplacement des correspondances ; le remplacement
    s'appuie sur ``_motif`` (insensible aux accents), donc pas besoin de la
    classe combinante de NFKD ici.
    """
    return (texte or "").lower()


def gerble_collision(garble: str, protect: Optional[set]) -> bool:
    """Vrai si un jeton du garble est déjà corrigé par med_grounding."""
    if not protect:
        return False
    return any(_normaliser(mot) in protect for mot in garble.split())


def _garble_low_conf(garble: str, conf: dict) -> bool:
    """Vrai si au moins un jeton du garble est entendu avec doute (< 0.98)."""
    for mot in garble.split():
        cle = _normaliser(mot)
        valeur = conf.get(cle)
        if isinstance(valeur, (int, float)) and valeur < 0.98:
            return True
    return False


def apply_inline_replacements(
    texte: str,
    langue: str = "fr",
    protect: Optional[set] = None,
    conf: Optional[dict] = None,
) -> Tuple[str, List[dict]]:
    """Remplace un terme gériatrique dans ``texte`` → ``(texte, changements)``.

    ``protect`` : jetons déjà corrigés par l'inline de ``med_grounding`` —
    collision : le médicament gagne, le terme est ignoré.

    ``conf`` : mapping ``norm_phon → confiance``. Fourni à la génération,
    un remplacement n'est appliqué que si un jeton du garble a une confiance
    STT < 0.98 (probablement déformé) — on ne réécrit pas un mot bien entendu.
    Absent en dictée, on réécrit (les curations sont sûres).

    ``changements`` : ``[{"garble", "correct"}]`` — paires réellement
    appliquées, pour le surlignage front-end (le client retrouve ``correct``
    dans le texte retourné).

    Les garbles sont essayés DU PLUS LONG AU PLUS COURT : « mini mental
    status » prime sur « mini mental » (multi-désignations convergées vers un
    même terme canonique). Chaque remplacement s'applique au texte COURANT,
    donc une occurrence déjà réécrite n'est jamais frappée deux fois.
    """
    if not texte:
        return texte, []
    proteger = set() if protect is None else set(protect)
    entrees = liste_remplacements(langue)
    entrees.sort(key=lambda e: len(e.get("garble") or ""), reverse=True)
    texte_courant = texte
    changements: List[dict] = []
    vus = set()
    for entree in entrees:
        garble = entree.get("garble")
        correct = entree.get("correct")
        if not garble or not correct or _normaliser(garble) == _normaliser(correct):
            continue
        if gerble_collision(garble, proteger):
            continue
        if conf is not None and not _garble_low_conf(garble, conf):
            continue
        texte_courant, nb = _remplacer_phrase(texte_courant, garble, correct)
        if nb and correct not in vus:
            vus.add(correct)
            changements.append({"garble": garble, "correct": correct})
    return texte_courant, changements


def _remplacer_phrase(texte: str, garble: str, correct: str) -> Tuple[str, int]:
    """Remplace toutes les occurrences de ``garble`` (casse + accents insensibles).

    Le motif (regex de classes d'accents) est appliqué sur le texte MINUSCULE
    (``lower()`` ne change pas la longueur). Retourne ``(texte, nb_rempl)``.
    """
    if not texte:
        return texte, 0
    cible = _normaliser(garble)
    if not cible:
        return texte, 0
    motif = re.compile(_motif(cible))
    bas = texte.lower()
    decouvertes = []  # (debut, fin)
    for m in motif.finditer(bas):
        d, f = m.start(), m.end()
        # Frontières de mot : ni avant ni après un caractère alphanumérique
        # (ou accentué) — ne pas frapper « xmini mentaly ».
        avant = bas[d - 1] if d > 0 else ""
        apres = bas[f] if f < len(bas) else ""
        if avant and (avant.isalnum() or _est_accent(avant)):
            continue
        if apres and (apres.isalnum() or _est_accent(apres)):
            continue
        decouvertes.append((d, f))
    if not decouvertes:
        return texte, 0
    resultat = texte
    for d, f in sorted(decouvertes, reverse=True):
        resultat = resultat[:d] + correct + resultat[f:]
    return resultat, len(decouvertes)


def _est_accent(car: str) -> bool:
    return car in "àâäéèêëîïôöùûüçñÿ"


# ---------------------------------------------------------------------------
# Canal 2 — candidats ambigus pour le LLM (bloc HOMOPHONIES_CE_CALL)
# ---------------------------------------------------------------------------
def _confiance_combinée(fragment: str, lecture: str, conf_map: Optional[dict]) -> Optional[float]:
    """Confiance combinée d'un fragment GARBLE : ``sqrt(min_stt × sim)``.

    Même convention que les suggestions phonétiques des médicaments
    (``med_grounding.suggestions_texte``) : PLUS BASSE = piste plus forte
    (le STT hésitait ET la correspondance phonétique est proche → garble
    probable). ``conf_map`` : mapping ``norm_phon → confiance`` ; ``min_stt``
    = minima sur les jetons du fragment présents dans le mapping (1.0 si
    aucun jeton y figure). ``sim`` = similarité phonémique G2P entre la forme
    fautive et la lecture correcte (via ``med_grounding``). Retourne ``None``
    si la similarité n'est pas calculable.
    """
    if not fragment or not lecture:
        return None
    try:
        from app import med_grounding
    except Exception:
        return None
    try:
        sim = med_grounding.sim_phon_w(
            med_grounding.phonetic_fr(fragment),
            med_grounding.phonetic_fr(lecture),
        )
    except Exception:
        return None
    if not sim or sim <= 0:
        return None
    stt_min = 1.0
    if conf_map:
        vals = [
            float(conf_map[t])
            for m in fragment.split()
            if isinstance(conf_map.get(t := med_grounding.norm_phon(m)), (int, float))
        ]
        if vals:
            stt_min = min(vals)
    return round((stt_min * sim) ** 0.5, 3)


def pertinent_hints(
    texte: str,
    langue: str = "fr",
    maxi: int = 6,
    conf_map: Optional[dict] = None,
) -> List[dict]:
    """Lignes du canal hints dont le fragment fautif figure dans ``texte``.

    Seules ces lignes voyagent dans le prompt ; la liste complète ne sort
    jamais. ``maxi`` borne le message utilisateur. ``conf_map`` (STT) alimente
    la confiance combinée des fragments flaggés ``phonetic`` (garble-type) :
    plus elle est BASSE, plus la piste est forte. Les entrées sans drapeau
    ``phonetic`` (équivalences autoritaires) ne portent AUCUNE confiance.
    """
    if not texte:
        return []
    texte_norm = _normaliser(texte)
    resultats: List[dict] = []
    for entree in liste_hints(langue):
        fragment = _normaliser(entree.get("fragment") or "")
        if fragment and fragment in texte_norm:
            item = {
                "erreur": entree.get("fragment"),
                "lecture": entree.get("lecture"),
                "contexte": entree.get("contexte"),
            }
            if entree.get("phonetic"):
                conf = _confiance_combinée(
                    entree.get("fragment") or "", entree.get("lecture") or "", conf_map,
                )
                if conf is not None:
                    item["conf"] = conf
            resultats.append(item)
            if len(resultats) >= maxi:
                break
    return resultats