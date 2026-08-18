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

REPLI FLOU SUR L'EXTRAIT COMPLET, PAS SUR DES PRÉFIXES (2026-08-18)
--------------------------------------------------------------------
Première version : en cas d'échec de la recherche exacte, raccourcir le
terme depuis la fin et réinterroger la BDPP à chaque longueur. Fonctionnait
pour ``Norvask`` → NORVASC (préfixe commun ``Norva``), mais s'est avéré
structurellement incapable de retrouver un médicament dont le début diffère
— cas réel, consultation #9 : ``Activant`` (probablement Ativan/lorazépam,
un médicament DISTINCT confondu par le modèle avec zopiclone) et ``Ativan``
divergent dès la 2ᵉ lettre (``c`` vs ``t``), donc AUCUN préfixe de l'un n'est
un préfixe de l'autre — la recherche par préfixe ne pouvait jamais le
proposer comme candidat, même si ``SequenceMatcher(None, "activant",
"ativan").ratio()`` vaut 0,857, largement au-dessus du seuil.

Corrigé en téléchargeant l'extrait COMPLET (pas de fichier ZIP à parser : le
même point de terminaison ``drugproduct``/``activeingredient``, appelé SANS
paramètre de nom, rend l'ensemble de la base — confirmé empiriquement :
~58 000 produits / ~121 000 lignes d'ingrédients, ~15-16 Mo chacun en JSON)
et en classant le terme par similarité contre CHAQUE nom, pas seulement ceux
partageant un préfixe. Mis en cache localement (``_DPD_CACHE_DIR``,
rafraîchi si périmé) : le téléchargement ne se produit qu'une fois par
semaine par processus, jamais dans le chemin critique d'une génération.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from app.note_schema import DrugLookup
from app.note_validator import _normalize_text

logger = logging.getLogger(__name__)

_DPD_API = "https://health-products.canada.ca/api/drug"
#: Court : cet appel se produit À L'INTÉRIEUR d'un tour d'appel d'outil que
#: le médecin attend déjà (génération non diffusée en direct, voir
#: main._generate_json_pipeline) — pas question d'y ajouter le délai qu'on
#: tolère pour un appel modèle complet. Le téléchargement de l'extrait
#: complet (rare, mis en cache) a son propre délai, plus généreux.
_TIMEOUT_SECONDS = 5
_BULK_TIMEOUT_SECONDS = 60

#: "norvask" vs "norvasc" (ratio ≈ 0.86), "activant" vs "ativan" (≈ 0.86)
#: doivent passer ; un candidat sans rapport doit échouer — 0.75 sépare
#: confortablement les cas observés.
_FUZZY_THRESHOLD = 0.75
#: Deuxième palier (vu réellement, consultation #9) : « respirone » vs
#: « REPRONEX » (un médicament de fertilité SANS rapport) obtient 0,824 —
#: au-dessus de 0,75, mais le modèle l'a alors présenté comme une
#: correction CONFIRMÉE dans Éléments à valider, alors que la vraie réponse
#: (rispéridone) n'était même pas le meilleur candidat par similarité pure
#: de caractères. Les cas vérifiés comme fiables (Norvask/Norvasc,
#: Activant/Ativan, Monochore/Monocor, Ensoprazole/Lansoprazole) sont tous
#: ≥ 0,857 ; Repronex reste à 0,824 — ce palier sépare confortablement les
#: deux. En dessous, un match reste renvoyé (``found=True``) mais avec
#: ``source="dpd_fuzzy_weak"`` : un candidat à considérer, jamais une
#: correction à affirmer avec confiance (voir la consigne de l'outil).
_FUZZY_STRONG_THRESHOLD = 0.83

#: Où vivent les extraits complets mis en cache — /data est déjà le volume
#: persistant de la base SQLite (voir app/database.py), donc déjà monté et
#: sauvegardé.
_DPD_CACHE_DIR = os.environ.get("DPD_CACHE_DIR", "/data/dpd_cache")
#: Rafraîchi si le cache a plus de 7 jours — Santé Canada met à jour
#: l'extrait périodiquement (pas quotidiennement), pas la peine de
#: retélécharger 15 Mo à chaque redémarrage du conteneur.
_CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600


#: Cache mémoire, durée de vie du processus — clé (genre, terme normalisé).
#: Les résultats en erreur ne sont JAMAIS mis en cache : une panne réseau
#: passagère de Santé Canada ne doit pas rester collée pour le reste du
#: processus (voir plus bas, dans search_drug).
_cache: Dict[Tuple[str, str], DrugLookup] = {}

#: Index flou en mémoire (liste de lignes dédupliquées par nom normalisé),
#: chargé une fois par ``kind`` puis réutilisé — voir _load_local_index.
_local_index: Dict[str, List[dict]] = {}

_ENDPOINTS = {
    "brand": ("drugproduct/", "brandname"),
    "ingredient": ("activeingredient/", "ingredientname"),
}


class _DpdQueryError(Exception):
    """Panne réseau/HTTP/JSON d'un appel brut — jamais laissée remonter
    au-delà de ``search_drug``, qui la transforme en ``DrugLookup(error=...)``."""


def _dpd_query(term: str, kind: str, language: str) -> list:
    """Un seul appel HTTP brut, recherche EXACTE (filtre préfixe côté BDPP).
    Retourne toujours une liste (vide si la BDPP n'a rien trouvé) ; lève
    ``_DpdQueryError`` sur tout échec — distingue ainsi « recherché, rien
    trouvé » de « la recherche elle-même a échoué »."""
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
    """Voir le commentaire de module. Classe TOUT l'extrait local par
    similarité au terme original — pas seulement les entrées partageant un
    préfixe — et retourne le meilleur si assez proche. Un extrait local
    indisponible (jamais téléchargé avec succès) dégrade en
    ``found=False``, jamais en exception."""
    index = _load_local_index(kind, language)
    if not index:
        return DrugLookup(term=term, found=False)

    normalized_term = _normalize_text(term)
    best: Optional[Tuple[float, dict]] = None
    for row in index:
        ratio = SequenceMatcher(None, normalized_term, row["_normalized_name"]).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, row)

    if best is not None and best[0] >= _FUZZY_STRONG_THRESHOLD:
        return _build_result(term, best[1], source="dpd_fuzzy")
    if best is not None and best[0] >= _FUZZY_THRESHOLD:
        return _build_result(term, best[1], source="dpd_fuzzy_weak")
    return DrugLookup(term=term, found=False)


# ---------------------------------------------------------------------------
# Extrait complet local — téléchargement, cache disque, index en mémoire.
# ---------------------------------------------------------------------------


def _cache_path(kind: str) -> str:
    return os.path.join(_DPD_CACHE_DIR, f"{kind}_extract.json")


def _fetch_full_dataset(kind: str, language: str) -> list:
    """Télécharge la liste COMPLÈTE (aucun paramètre de nom) — le même point
    de terminaison que ``_dpd_query``, mais sans filtre : confirmé
    empiriquement que ``drugproduct``/``activeingredient`` sans paramètre de
    nom rend l'ensemble de la base plutôt qu'une erreur ou une liste vide."""
    path, _ = _ENDPOINTS[kind]
    query = urllib.parse.urlencode({"lang": language, "type": "json"})
    url = f"{_DPD_API}/{path}?{query}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_BULK_TIMEOUT_SECONDS) as reponse:
        payload = json.loads(reponse.read().decode("utf-8"))
    return payload if isinstance(payload, list) else []


def _dedupe_by_name(rows: list) -> List[dict]:
    """Déduplique par nom normalisé (garde la première occurrence) et
    précalcule ``_normalized_name`` — évite de renormaliser à chaque
    comparaison de similarité, ~58 000 fois par recherche floue sinon."""
    seen: Dict[str, dict] = {}
    for row in rows:
        name = _candidate_name(row) if isinstance(row, dict) else ""
        if not name:
            continue
        key = _normalize_text(name)
        if key not in seen:
            row = dict(row)
            row["_normalized_name"] = key
            seen[key] = row
    return list(seen.values())


def _load_local_index(kind: str, language: str) -> List[dict]:
    """Charge l'index flou pour ``kind`` — mémoire du processus d'abord,
    puis cache disque (``_DPD_CACHE_DIR``) s'il n'a pas plus de 7 jours,
    puis téléchargement complet en dernier recours. Un cache périmé mais
    présent est préféré à une panne de téléchargement ; aucun index
    disponible dégrade en liste vide (voir ``_fuzzy_fallback``), jamais en
    exception."""
    if kind in _local_index:
        return _local_index[kind]

    path = _cache_path(kind)
    rows: Optional[list] = None
    try:
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < _CACHE_MAX_AGE_SECONDS:
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f)
    except Exception as exc:  # noqa: BLE001 — cache local corrompu, pas fatal
        logger.warning("Cache BDPP local illisible (%s) : %s", path, exc)

    if rows is None:
        try:
            rows = _fetch_full_dataset(kind, language)
            os.makedirs(_DPD_CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f)
        except Exception as exc:  # noqa: BLE001 — réseau/disque, pas fatal
            logger.warning("Téléchargement de l'extrait BDPP complet échoué (%s) : %s", kind, exc)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    rows = json.load(f)  # cache périmé, mieux que rien
            except Exception:
                rows = []

    index = _dedupe_by_name(rows or [])
    _local_index[kind] = index
    return index
