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
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Tuple

from app.note_schema import DrugLookup
from app.note_validator import _normalize_text

logger = logging.getLogger(__name__)

_DPD_API = "https://health-products.canada.ca/api/drug"
#: Court : cet appel se produit À L'INTÉRIEUR d'un tour d'appel d'outil que
#: le médecin attend déjà (génération non diffusée en direct, voir
#: main._generate_json_pipeline) — pas question d'y ajouter le délai qu'on
#: tolère pour un appel modèle complet.
_TIMEOUT_SECONDS = 5


#: Cache mémoire, durée de vie du processus — clé (genre, terme normalisé).
#: Les résultats en erreur ne sont JAMAIS mis en cache : une panne réseau
#: passagère de Santé Canada ne doit pas rester collée pour le reste du
#: processus (voir plus bas, dans search_drug).
_cache: Dict[Tuple[str, str], DrugLookup] = {}

_ENDPOINTS = {
    "brand": ("drugproduct/", "brandname"),
    "ingredient": ("activeingredient/", "ingredientname"),
}


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

    path, param = _ENDPOINTS[kind]
    query = urllib.parse.urlencode({param: term, "lang": language, "type": "json"})
    url = f"{_DPD_API}/{path}?{query}"

    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as reponse:
            payload = json.loads(reponse.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("Recherche BDPP échouée pour « %s » (%s) : HTTP %s", term, kind, exc.code)
        return DrugLookup(term=term, found=False, error=f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 — réseau/JSON/temps mort, jamais fatal ici
        logger.warning("Recherche BDPP échouée pour « %s » (%s) : %s", term, kind, exc)
        return DrugLookup(term=term, found=False, error=str(exc))

    if not isinstance(payload, list) or not payload:
        result = DrugLookup(term=term, found=False)
        _cache[cache_key] = result
        return result

    premier = payload[0] if isinstance(payload[0], dict) else {}
    matched_name = str(
        premier.get("brand_name") or premier.get("ingredient_name") or ""
    ).strip() or None
    din = str(premier.get("drug_identification_number") or "").strip() or None
    result = DrugLookup(term=term, found=True, matched_name=matched_name, din=din)
    _cache[cache_key] = result
    return result
