"""
drug_lookup.py — client pour la Base de données sur les produits
pharmaceutiques (BDPP/DPD) de Santé Canada.
===========================================================================

API publique, sans authentification : https://health-products.canada.ca/api/drug/
Utilisée pour vérifier qu'un nom de médicament (marque ou ingrédient actif)
correspond à un produit réellement approuvé au Canada — voir
``note_extraction._extract_note_with_dpd_tool`` (branche selfhosted,
expérimental), qui donne au modèle un outil appelant cette fonction pendant
l'extraction.

Une absence de correspondance N'EST PAS une preuve d'erreur : un médicament
étranger, composé en pharmacie ou retiré du marché peut légitimement être
absent de cette base. C'est un signal, jamais une décision automatique — voir
``note_validator.check_drug_lookups``.

Convention : requête brute via ``urllib.request`` (stdlib), comme
``llm._mistral_request``/``_cohere_request`` — pas de nouvelle dépendance
pour un point de terminaison qui ne prend qu'un seul appel HTTP.

REPLI FLOU (2026-08-18)
------------------------
Confirmé empiriquement contre l'API réelle : la recherche ``brandname`` est
un simple filtre préfixe/sous-chaîne, PAS une correspondance floue ni
phonétique — ``Norvask`` (k) ne retrouve rien pour ``NORVASC`` (c), qui
diverge avant la fin d'un préfixe commun, alors qu'un préfixe plus court
comme ``Norva`` retrouve bien les 3 DIN de NORVASC. Plutôt que d'espérer que
le modèle réessaie lui-même avec un préfixe plus court (fragile — la leçon
déjà tirée deux fois cette session, listes numérotées puis fusion de
médicaments), ``search_drug`` le fait lui-même : si la recherche exacte est
vide, elle raccourcit le terme depuis la fin jusqu'à obtenir des candidats,
puis les classe par similarité au terme ORIGINAL (``difflib.SequenceMatcher``
— même technique que ``note_validator._best_match_ratio`` pour le
grounding). Un résultat trouvé ainsi porte ``source="dpd_fuzzy"`` plutôt que
``"dpd"``, pour rester distinguable dans les journaux/``note_generations``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from typing import Dict, Optional, Tuple

from app.note_schema import DrugLookup
from app.note_validator import _normalize_text

logger = logging.getLogger(__name__)

_DPD_API = "https://health-products.canada.ca/api/drug"
#: Court : cet appel se produit À L'INTÉRIEUR d'un tour d'appel d'outil que
#: le médecin attend déjà (génération non diffusée en direct, voir
#: main._generate_json_pipeline) — pas question d'y ajouter le délai qu'on
#: tolère pour un appel modèle complet.
_TIMEOUT_SECONDS = 5

#: En dessous de cette longueur de préfixe, le repli flou abandonne — un
#: préfixe trop court (« Nor ») retrouverait un flot de médicaments sans
#: rapport plutôt qu'une correction plausible.
_FUZZY_MIN_PREFIX = 4
#: "norvask" vs "norvasc" (ratio ≈ 0.86) doit passer ; un candidat sans
#: rapport doit échouer — 0.75 sépare confortablement les deux dans les cas
#: observés.
_FUZZY_THRESHOLD = 0.75


#: Cache mémoire, durée de vie du processus — clé (genre, terme normalisé).
#: Les résultats en erreur ne sont JAMAIS mis en cache : une panne réseau
#: passagère de Santé Canada ne doit pas rester collée pour le reste du
#: processus (voir plus bas, dans search_drug).
_cache: Dict[Tuple[str, str], DrugLookup] = {}

_ENDPOINTS = {
    "brand": ("drugproduct/", "brandname"),
    "ingredient": ("activeingredient/", "ingredientname"),
}


class _DpdQueryError(Exception):
    """Panne réseau/HTTP/JSON d'un appel brut — jamais laissée remonter
    au-delà de ``search_drug``, qui la transforme en ``DrugLookup(error=...)``."""


def _dpd_query(term: str, kind: str, language: str) -> list:
    """Un seul appel HTTP brut. Retourne toujours une liste (vide si la BDPP
    n'a rien trouvé) ; lève ``_DpdQueryError`` sur tout échec — distingue
    ainsi « recherché, rien trouvé » de « la recherche elle-même a échoué »,
    ce qu'une liste vide seule ne permettrait pas."""
    path, param = _ENDPOINTS[kind]
    query = urllib.parse.urlencode({param: term, "lang": language, "type": "json"})
    url = f"{_DPD_API}/{path}?{query}"
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as reponse:
            payload = json.loads(reponse.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _DpdQueryError(f"HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 — réseau/JSON/temps mort
        raise _DpdQueryError(str(exc)) from exc
    return payload if isinstance(payload, list) else []


def _candidate_name(candidate: dict) -> str:
    return str(candidate.get("brand_name") or candidate.get("ingredient_name") or "").strip()


def _build_result(term: str, candidate: dict, *, source: str) -> DrugLookup:
    din = str(candidate.get("drug_identification_number") or "").strip() or None
    return DrugLookup(term=term, found=True, matched_name=_candidate_name(candidate) or None, din=din, source=source)


def search_drug(term: str, *, kind: str = "brand", language: str = "fr") -> DrugLookup:
    """Cherche ``term`` dans la BDPP. Ne lève JAMAIS d'exception : tout échec
    (réseau, temps mort, JSON illisible, genre inconnu) devient
    ``DrugLookup(found=False, error=...)`` — une recherche ratée ne doit
    jamais faire échouer une génération de note."""
    term = (term or "").strip()
    if not term:
        return DrugLookup(term=term, found=False, error="terme vide")
    if kind not in _ENDPOINTS:
        kind = "brand"

    cache_key = (kind, _normalize_text(term))
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        candidates = _dpd_query(term, kind, language)
    except _DpdQueryError as exc:
        logger.warning("Recherche BDPP échouée pour « %s » (%s) : %s", term, kind, exc)
        return DrugLookup(term=term, found=False, error=str(exc))

    if candidates:
        result = _build_result(term, candidates[0], source="dpd")
        _cache[cache_key] = result
        return result

    result = _fuzzy_fallback(term, kind, language)
    _cache[cache_key] = result
    return result


def _fuzzy_fallback(term: str, kind: str, language: str) -> DrugLookup:
    """Voir le commentaire de module. Raccourcit ``term`` depuis la fin
    jusqu'au premier préfixe qui rend des candidats, les classe par
    similarité au terme ORIGINAL, retourne le meilleur si assez proche."""
    normalized_term = _normalize_text(term)
    best: Optional[Tuple[float, dict]] = None

    cut = len(term)
    while cut > _FUZZY_MIN_PREFIX:
        cut -= 1
        prefix = term[:cut]
        try:
            prefix_candidates = _dpd_query(prefix, kind, language)
        except _DpdQueryError as exc:
            # La recherche exacte a déjà réussi (liste vide, pas une panne) —
            # une panne PENDANT le repli n'annule pas ce "non trouvé" légitime,
            # elle interrompt juste la tentative d'amélioration.
            logger.warning("Repli flou BDPP interrompu pour « %s » (%s) : %s", term, kind, exc)
            break
        if not prefix_candidates:
            continue
        for candidate in prefix_candidates:
            name = _candidate_name(candidate)
            if not name:
                continue
            ratio = SequenceMatcher(None, normalized_term, _normalize_text(name)).ratio()
            if best is None or ratio > best[0]:
                best = (ratio, candidate)
        break  # premier préfixe qui rend des candidats : on s'arrête là

    if best is not None and best[0] >= _FUZZY_THRESHOLD:
        return _build_result(term, best[1], source="dpd_fuzzy")
    return DrugLookup(term=term, found=False)
