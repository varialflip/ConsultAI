"""
geriatric_terms.py — Termes gériatriques québécois corrigés avant le LLM.
================================================================================

POURQUOI UN MODULE SÉPARÉ DE MÉDICAMENTS
----------------------------------------
Les médicaments déformés par la reconnaissance vocale sont corrigés par
``med_grounding`` (moteur déterministe : base DPD, liste curatée
``common_meds.json``, correction inline + suggestions). Les TERMES gériatriques
— établissements, conditions, tests cognitifs, abréviations (MMSE, MoCA,
ISO-SMAF, Maison Aloïs, TEP-Scan, corps de Lewy, brady/hypo kinétique…) —
n'y ont pas leur place : ce ne sont pas des médicaments, et les chercher
dans la base DPD serait faux.

Ce module vit donc À PART, alimenté par ``geriatric_terms.json``. Il offre
TROIS canaux, comme ``med_grounding`` mais pour des termes non
médicamenteux :

  * ``apply_inline_replacements`` — remplacements DÉTERMINISTES dans le
    texte (formes qu'un profil phonétique ne peut pas capturer :
    acronymes au canon collé comme ``mms``/``mo ca``/``iso smaf``, ou
    locutions à plus de trois jetons), appliqués AVANT le LLM :
    l'erreur est corrigée dans le texte, zéro attention du modèle.
    Chaque remplacement est explicitement curaté dans le JSON.
  * ``pertinent_hints`` — candidats AMBIGUS laissés au jugement clinique
    du LLM (homophonies à lecture possible multiple), injectés dans le
    bloc <<<HOMOPHONIES_CE_CALL>>>. Un terme sans ambiguïté n'y figure
    pas : s'il a une lecture unique sûre, il est remplacé (canal
    précédent), pas suggéré.
  * ``matcher_profils`` — matching phonétique FLOU (G2P) par profil
    (``phonetic_profiles``) : suggère au LLM les variantes d'un terme
    SANS réécrire le texte. Sert l'onglet « Termes gériatriques à
    valider » de la consultation.

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


#: Seuil de similarité phonémique G2P par défaut d'un probe (remplaçable par
#: le champ ``min_sim`` de chaque probe de ``phonetic_profiles``).
_MIN_SIM_DEF = 0.60


def liste_profils(langue: str = "fr") -> List[dict]:
    """Profils phonétiques flous (G2P) pour la langue demandée.

    Chaque profil porte ``canonical`` (forme cible), ``probes`` (lectures à
    matcher phonétiquement), et l'option ``require_score`` (le match ne compte
    que s'il est suivi d'une cote — voir ``_a_une_cote``).
    """
    return _filtre_langue(_charger().get("phonetic_profiles") or [], langue)


#: Fenêtres texte (en jetons) sondées pour un probe phonétique : 1 → 3 jetons.
#: « moca », « iso smaf », « corps de louis », « clinique d'évaluation »… couvre
#: les acronymes, leurs variantes en toutes lettres et les locutions à 3 jetons
#: sans jamais charger le scan (le G2P pré-filtré ramène chaque appel à ~0,2 ms).
_FENETRES = (1, 2, 3)

#: Motifs synonymes d'une cote : un mot de cote (« cote », « score »,
#: « code ») précédant/formant la cible d'une échelle d'autonomie tient lieu
#: de preuve quand aucun chiffre n'accompagne (ISO-SMAF dicté sans nombre).
_SCORE = re.compile(r"\d{1,3}")
_COTE_MOTS = ("cote", "cotè", "coté", "score", "code")


def _a_une_cote(valeurs: List[str]) -> bool:
    """Vrai si une cote (entier <= 30) ou un mot de cote figure dans ``valeurs``.

    Les scores s'écrivent « MMS 28 », « MOCA 24 sur 30 », « MMSE à 30 »… : on
    cherche un entier dans les ~2 jetons suivant le match. Les mots de cote
    (« cote isosnaphe », « code ISOSNAF ») comptent aussi, pour les échelles
    d'autonomie dictées sans chiffre.
    """
    for v in valeurs:
        for m in _SCORE.finditer(v):
            if m.group().isdigit() and int(m.group()) <= 30:
                return True
        if _normaliser(v).split() and _normaliser(v).split()[-1] in _COTE_MOTS:
            return True
    return False


def _bigrammes(s: str) -> set:
    """Bigrammes de caractères de ``s`` (pré-filtre orthographique)."""
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s or ""}


def _purge_ponct(mot: str) -> str:
    """Retire la ponctuation collée d'un jeton (« isosnaphe. » → « isosnaphe »)."""
    return re.sub(r"[^\w]+", "", mot) if mot else mot


def matcher_profils(
    texte: str,
    langue: str = "fr",
    conf_map: Optional[dict] = None,
    maxi: int = 10,
) -> List[dict]:
    """Candidats des ``phonetic_profiles`` présents (flous) dans ``texte``.

    Découpe le texte en fenêtres de 1–2 jetons et les compare phonétiquement
    (G2P, ``med_grounding.sim_phon_w``) aux probes de chaque profil. Un match
    n'est retenu que si :
      * la similarité >= ``min_sim`` (défaut ``_MIN_SIM_DEF``) ;
      * le profil ``require_score`` n'exige pas de cote, OU une cote suit ;
      * la fenêtre n'est pas déjà égale à la forme canonique (pas de no-op).
    Triple pré-filtre orthographique avant le G2P (sur les garbles reste de la
    même taille que la cible) : écart de longueur <= 2, premier caractère
    identique, et au moins un bigramme commun avec le probe (aucun → skip).
    Ramène le scan à ~quelques dizaines de ms sur un transcrit de ~2000 mots
    (~4000 appels G2P, ~0,2 ms chacun).

    Les candidats sont dédupliqués PAR CANONIQUE : pour chaque profil, seule la
    fenêtre la plus courte (1 jeton d'abord) qui a matché est retenue — le
    garble signalé reste précis, sans englober la cote ou la ponctuation qui
    suit. Retourne ``[{"erreur", "lecture", "contexte", "conf"}]`` — même forme
    que ``pertinent_hints`` (alimente le bloc HOMOPHONIES_CE_CALL et le rollover).
    """
    if not texte:
        return []
    try:
        from app import med_grounding as mg
    except Exception:
        return []
    tokens = texte.split()
    profils = liste_profils(langue)
    # Pré-filtres par probe, calculés une fois (le G2P est le vrai goulot :
    # un appel ~0,2 ms — on n'y soumet que les fenêtres PLAUSIBLES).
    # Désignations valides du profil : le canonique ET les réécritures sûres du
    # canal inline qui convergent vers lui (le fichier listé dans
    # ``deterministic_replacements``). Une fenêtre qui colle à l'une de ces
    # formes n'est PAS un garble (pas de suggestion) — les SONDES phonétiques
    # sont des déformations à capturer, pas des façons valides d'écrire.
    inline_par_canon: dict = {}
    for entree in liste_remplacements(langue) or []:
        if entree.get("correct"):
            inline_par_canon.setdefault(entree.get("correct"), []).append(entree)
    filtres = []  # (canonical, contexte, require_score, désignations valides, [(fn, seuil, bigrammes)])
    for profil in profils:
        canon = profil.get("canonical") or ""
        canon_norm = _normaliser(canon)
        if not canon_norm:
            continue
        desigs_plain = {
            _normaliser("".join(_purge_ponct(m) for m in s.split()))
            for s in [e.get("garble") or "" for e in inline_par_canon.get(canon, [])]
            if _normaliser("".join(_purge_ponct(m) for m in s.split()))
        }
        # Le canon lui-même ne rejoint les désignations valides QUE s'il porte
        # un séparateur non-espace (tiret apostrophe) — « ISO-SMAF » → «
        # isosmaf » : une fenêtre collée équivalente est déjà la bonne forme.
        # Un canon MONO-MOT (« bradykinétique ») ou À ESPACES (« Maison
        # Aloïs ») ne se colle PAS : « brady kinétique » est une déformation
        # à suggérer, pas une façon valide d'écrire.
        if re.search(r"[^\w\s]", canon):
            desigs_plain.add(
                _normaliser("".join(_purge_ponct(m) for m in canon.split()))
            )
        for probe in profil.get("probes") or []:
            forme = probe.get("forme") or ""
            fn = _normaliser(_purge_ponct(forme))
            if fn:
                filtres.append((
                    canon, profil.get("contexte"),
                    profil.get("require_score"), canon_norm,
                    tuple(sorted(desigs_plain)),
                    fn, float(probe.get("min_sim") or _MIN_SIM_DEF),
                    _bigrammes(fn),
                ))
    resultats: List[dict] = []
    retenus: dict = {}  # canonical → (n, best_sim, candidat)
    for k in range(len(tokens)):
        for n in _FENETRES:
            fen = tokens[k:k + n]
            if not fen:
                continue
            win = " ".join(_purge_ponct(t) for t in fen)
            win_norm = _normaliser(win)
            if not win_norm:
                continue
            larg = len(win_norm)
            win_plain = _normaliser("".join(e for e in win if e != " "))
            for (canon, contexte, req_score, canon_norm,
                 desigs_plain, fn, seuil, fn_bigr) in filtres:
                # La fenêtre écrit déjà le canonique ou UNE désignation valide
                # du profil (sans sa ponctuation interne — « ISO-SMAF » ≈
                # « isosmaf », « mini mental » ≈ « minimental ») : pas de no-op,
                # pas de suggestion.
                if canon_norm in win_norm or win_plain in desigs_plain:
                    continue
                # Triple filtre orthographique avant le G2P :
                #   1) longueur proche (un garble garde la taille de la cible) ;
                #   2) premier caractère identique (les vrais variants aussi) ;
                #   3) au moins un bigramme commun (même reçoit du probe).
                if len(fn) > 2 and len(win_norm) > 2 and (
                        abs(larg - len(fn)) > 2
                        or win_norm[0] != fn[0]
                        or not (_bigrammes(win_norm) & fn_bigr)):
                    continue
                try:
                    sim = mg.sim_phon_w(mg.phonetic_fr(win), mg.phonetic_fr(fn))
                except Exception:
                    continue
                if sim < seuil:
                    continue
                # Fenêtre la plus précise pour le canonique : en priorité celle
                # qui matche le MIEUX phonétiquement (la locution pleine « maison
                # à lois » bat la troncature « maison à »), à sim égale la plus
                # courte d'abord (« mms » bat « mms a »).
                retenu = retenus.get(canon)
                if retenu is None or sim > retenu[1] or (
                        sim == retenu[1] and n < retenu[0]):
                    if req_score and not _a_une_cote(tokens[k + n:k + n + 3]):
                        continue
                    retenus[canon] = (n, sim, {
                        "erreur": " ".join(fen),
                        "lecture": canon,
                        "contexte": contexte,
                        "conf": _confiance_combinée(win, canon, conf_map)
                                or round(sim, 3),
                    })
    resultats = [retenu[2] for retenu in retenus.values()]
    return resultats[:maxi]


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
        if (
            not entree.get("force")
            and conf is not None
            and not _garble_low_conf(garble, conf)
        ):
            # ``force`` : réécriture toujours appliquée, même quand le STT a
            # entendu le jeton avec confiance (>= 0.98). Réservé aux échelles /
            # acronymes à lecture univoque dont la reconnaissance est
            # systématiquement erronée (« mms » → MMSE) : sans ce drapeau, la
            # garde < 0.98 bloquerait une correction pourtant sûre.
            continue
        texte_courant, nb = _remplacer_phrase(texte_courant, garble, correct)
        if nb and correct not in vus:
            vus.add(correct)
            changements.append({"garble": garble, "correct": correct})
    return texte_courant, changements


def precompute_normalization(
    texte: str,
    conf: Optional[dict] = None,
    langue: str = "fr",
    deja_normalise: Optional[str] = None,
    inline_med: Optional[set] = None,
) -> Tuple[str, set]:
    """Normalisation déterministe COMPLÈTE d'un transcrit → ``(texte, inline_fixed)``.

    Chaîne exacte des deux passes inline appliquées avant le LLM (le module
    ``geriatric_terms`` est importable des deux côtés — ``med_grounding`` en
    dépend déjà — , d'où son atterrissage ici) :
    1. ``med_grounding.normalize(..., inline_safe=True)`` — substitutions
       déterministes/auditées (exact + garbles seedés) des médicaments ;
    2. ``apply_inline_replacements`` — termes gériatriques québécois, avec
       ``protect`` = formes déjà corrigées par la passe 1 (le médicament gagne).

    Depuis 2026-09-07, le « Terminer » fournit ``deja_normalise`` (texte déjà
    corrigé par la passe ``normalize(inline_safe=True)`` qui a servi aux items
    de la Validation, avec ``inline_med`` = protection ``norm_phon`` de ses
    corrections) : la passe 1 coûteuse est alors SAUTÉE, seuls les termes
    gériatriques restent à appliquer. Hors de ce chemin (génération sans cache,
    import), ``deja_normalise`` reste ``None`` et les deux passes s'exécutent
    comme avant.

    ``inline_fixed`` : clés ``norm_phon`` des formes corrigées (médicaments ET
    termes gériatriques) — le LLM doit rester aveugle à ces corrections, et les
    hints ne doivent pas les re-suggérer.

    Utilisée par le « Terminer » (``dictation._finalize_grounding``) pour
    PRÉ-CALCULER le cache ``normalized_transcript`` hors fenêtre d'attente de
    l'usager, et par ``main.api_generate`` quand le cache est manquant (texte
    édité, import, retranscription).
    """
    from app import med_grounding
    fixed = texte or ""
    inline_fixed: set = set()
    if deja_normalise is None:
        try:
            lowercase, changes = med_grounding.normalize(fixed, conf=conf, inline_safe=True)
            if lowercase and lowercase.strip():
                fixed = lowercase
            inline_fixed = {
                med_grounding.norm_phon(repl)
                for _span, repl, _score, _sim in changes if repl
            }
        except Exception:
            inline_fixed = set()
    else:
        fixed = deja_normalise
        inline_fixed = set(inline_med or ())
    try:
        fixed, changements = apply_inline_replacements(
            fixed, langue=langue, protect=inline_fixed, conf=conf,
        )
        for changement in changements:
            if changement.get("correct"):
                inline_fixed.add(med_grounding.norm_phon(changement["correct"]))
    except Exception:
        pass
    return fixed, inline_fixed


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
        # (ou accentué) — ne pas frapper « xmini mentaly ». Un tiret collé à
        # une lettre (« iso-smaf », « hôtel-dieu ») fait partie du mot, pas
        # une frontière ; de même une apostrophe collée (« d'smaf »).
        def _dans_mot(pos: int) -> bool:
            """Vrai si ``bas[pos]`` est la première lettre d'un mot qui déborde
            la frontière testée (le caractère de chevauchement est une lettre
            directe, ou un tiret/apostrophe lui-même collé à une lettre)."""
            c = bas[pos]
            if c.isalnum() or _est_accent(c):
                return True
            if c in ("-", "'") and 0 < pos < len(bas) - 1:
                suiv = bas[pos + 1]
                return suiv.isalnum() or _est_accent(suiv)
            return False

        if d > 0 and _dans_mot(d - 1):
            continue
        if f < len(bas) and _dans_mot(f):
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
    # Profils phonétiques flous (``phonetic_profiles``) : lecteurs de variantes
    # non énumérées (isosnaphe, izosnaf, mms…), suggérés au LLM comme les hints.
    if len(resultats) < maxi:
        resultats.extend(matcher_profils(texte, langue, conf_map=conf_map,
                                          maxi=maxi - len(resultats)))
    return resultats


def corrections_et_hints(
    texte: str,
    langue: str = "fr",
    conf_map: Optional[dict] = None,
    maxi_hints: int = 10,
) -> List[dict]:
    """Entrées complètes pour le rollover / onglet à valider.

    Réunit DEUX sources en une liste de ``{"garble", "correct", "confidence"}`` :
      * les remplacements inline déterministes réellement appliqués
        (``apply_inline_replacements``) — forme sûre, déjà dans le texte ;
      * les candidats phonétiques flous (``matcher_profils``) — variantes
        suggérées au LLM, à valider (le texte brut n'est PAS modifié).

    ``confidence`` porte la confiance combinée pour les candidats phonétiques,
    absente pour les réécritures sûres. ``conf_map`` y est propagé telle
    quelle (gate inline — ``None`` ou ``{}`` le désactive) et à
    ``matcher_profils`` pour l'étiquette de confiance combinée.
    ``maxi_hints`` borne le nombre de candidats phonétiques ajoutés.
    C'est ce que consomment le rollover du transcrit (``_geriatric_corrections``,
    ``_apply_grounding``, ``get_consultation``) pour le champ ``geriatric``.
    """
    if not (texte or "").strip():
        return []
    _, changements = apply_inline_replacements(texte, langue=langue, conf=conf_map)
    entrees: List[dict] = [
        {"garble": c.get("garble") or c.get("correct"),
         "correct": c.get("correct")}
        for c in changements
    ]
    for hint in matcher_profils(texte, langue, conf_map=conf_map, maxi=maxi_hints):
        item = {
            "garble": hint.get("erreur"),
            "correct": hint.get("lecture"),
        }
        if isinstance(hint.get("conf"), (int, float)):
            item["confidence"] = hint["conf"]
        entrees.append(item)
    return entrees