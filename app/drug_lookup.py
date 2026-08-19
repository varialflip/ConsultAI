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

COUCHE PHONÉTIQUE FRANÇAISE (2026-08-18)
----------------------------------------
La tâche réelle n'est pas de rapprocher deux ORTHOGRAPHES mais deux
PRONONCIATIONS : le terme vient d'un moteur de reconnaissance vocale et ses
erreurs sont phonétiques (« Norvask » pour NORVASC, « Monochore » pour
MONOCOR…). La similarité de caractères seule échoue sur les confusions qui
changent des lettres entières : cas réel, ``Ensoprazole`` obtient le MÊME
ratio (0,870) contre LANSOPRAZOLE et ESOMEPRAZOLE — l'ancien code gardait
alors la première entrée de l'index, donc un choix arbitraire — alors que
la comparaison des clés phonétiques départe immédiatement (0,952 vs 0,762).

Deuxième signal ajouté au repli flou : la clé **Soundex FR** (algorithme
phonétique français, voir ``app/phonetic_fr.py`` — porté de la lib
``phonetic-fr``, MIT) comparée avec le même ``SequenceMatcher``. La clé
est PRÉCALCULÉE à la construction de l'index (``_dedupe_by_name``,
quelques secondes une seule fois par index) et journalisée dans un fichier
compagnon sur disque pour ne pas être rejouée à chaque redémarrage du
processus : la recherche garde ensuite quasiment le même coût que l'ancienne
(deux ``SequenceMatcher`` sur des chaînes courtes, ~0,8 et ~0,9 s).

Sécurité inchangée et volontaire : le phonétique ne peut JAMAIS remonter
un candidat au palier « confirmé ». Les paliers restent décidés sur le
ratio de caractères (comme avant) :
  - ``char >= 0,83`` → ``source="dpd_fuzzy"`` (confiance élevée) ;
  - ``char >= 0,75`` → ``source="dpd_fuzzy_weak"`` (candidat à confirmer) ;
  - ``char < 0,75 mais >= 0,50`` avec phonétique >= 0,83 → le candidat
    devient TROUVABLE en ``dpd_fuzzy_weak`` (jamais fort).

Le phonétique est un TIE-BREAKER au sein d'un même palier (deux candidats
au même ratio de caractères, on garde le phonétiquement le plus proche —
cas Ensoprazole) et un moyen de RETROUVER un candidat que les caractères
seuls trouvaient « introuvable » (jamais pour l'affirmer avec confiance).
Vérifié contre le cas réel « Respirone » vs « Repronex » : les deux métriques
favorisent Repronex et la note reste correctement « à confirmer » — c'est
à cela que sert ce palier ; corriger ce cas exigerait un signal clinique
tiers (le nom de l'ingrédient actif, par exemple).
"""

from __future__ import annotations

import functools
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from app import phonetic_fr
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

#: Couche phonétique française (voir le docstring de module) — le phonétique
#: sert de TIE-BREAKER au sein d'un palier et de RAMPE DE RETROUVAILLE pour
#: les candidats que les caractères seuls classeraient introuvables. Il ne
#: peut JAMAIS remonter un candidat au palier « confirmé » : ces deux seuils
#: bornent une résurrection « faible » seulement (char >= 0,50 ET
#: phonétique >= 0,83 → ``found=True``, ``source="dpd_fuzzy_weak"``).
_PHON_RESCUE_THRESHOLD = 0.83
_PHON_CHAR_FLOOR = 0.50

#: Où vivent les extraits complets mis en cache — /data est déjà le volume
#: persistant de la base SQLite (voir app/database.py), donc déjà monté et
#: sauvegardé.
_DPD_CACHE_DIR = os.environ.get("DPD_CACHE_DIR", "/data/dpd_cache")
#: Rafraîchi si le cache a plus de 7 jours — Santé Canada met à jour
#: l'extrait périodiquement (pas quotidiennement), pas la peine de
#: retélécharger 15 Mo à chaque redémarrage du conteneur.
_CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600

# --- Source historique RxNorm (marques retirées/internationales) -----------
#: La BDPP n'expose que les produits COURANTS (un produit retiré du marché —
#: ex. LOPRESSOR/métoprolol — en est absent, aucun filtre ``status`` ne le
#: ramène). RxNorm (NLM/US) garde l'historique et les synonymes ; release
#: « Current Prescribable Content, no license required » — vérifiée : contient
#: Lopressor, Ativan, Prevacid. On la télécharge UNE FOIS et on l'indexe
#: localement (Soundex FR + caractères), comme l'extrait BDPP : AUCUN nom de
#: médicament ne quitte la machine pendant l'exploitation (pas d'envoi
#: runtime vers les USA → pas de flux de données à déclarer en EFVP).
_RX_URL = "https://download.nlm.nih.gov/rxnorm/RxNorm_full_prescribe_current.zip"
_RX_CACHE_DIR = os.environ.get("RX_CACHE_DIR", "/data/rxnorm_cache")
_RX_BULK_TIMEOUT_SECONDS = 180
#: Palier « à confirmer » de la source historique : une marque ancienne reste
#: TOUJOURS faible (jamais une « correction apportée » — pas de DIN canadien,
#: source internationale). Seuil phonétique >= 0,85 avec char >= 0,50, ou
#: char >= 0,75 seul (même barre que le FAIBLE du DPD).
_LEGACY_PHON = 0.85
_LEGACY_CHAR = 0.50
_LEGACY_CHAR_HI = 0.75
#: Index mémoire (durée de vie du processus).
_legacy_index: Optional[List[dict]] = None


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


#: La BDPP fusionne souvent la force/forme dans ``brand_name`` lui-même —
#: ``MONOCOR   -(5MG)``, ``PLACIDYL CAP 200MG``, ``COLCHICINE TAB 0.6MG`` —
#: alors que le terme transcrit ne porte que le nom (la dose est dictée à
#: part). Comparé tel quel, ce suffixe dilue le ratio de caractères : cas
#: réel, ``Monochore`` contre ``MONOCOR   -(5MG)`` ne fait que 0,609 (sous le
#: seuil), alors que ``ONCCOR`` (nom propre, sans suffixe, DIN 02230207 —
#: un médicament SANS RAPPORT) obtient 0,667 et l'emporte par défaut. Contre
#: le nom nettoyé ``MONOCOR``, le ratio grimpe à 0,875 — au-dessus du palier
#: fort. Le nom AFFICHÉ (``matched_name``, via ``_candidate_name`` sur la
#: ligne brute) reste intact ; seul l'index de comparaison utilise le nom
#: nettoyé.
_DOSE_SUFFIX_RE = re.compile(
    r"\s*[-(]*\s*"
    r"(?:CAP|TAB|INJ|LIQ|SOLN|SYRUP|SR|MR|XR|ER|CR|CHEWABLE(?:\s+TBS)?|FILMTAB|DPS)?\s*"
    r"\d+(?:[.,]\d+)?\s*(?:MG|MCG|G|ML|MEQ|UI|IU|%)(?:/ML)?\)?\s*$",
    re.IGNORECASE,
)


def _strip_dose_suffix(name: str) -> str:
    cleaned = _DOSE_SUFFIX_RE.sub("", name).strip()
    return cleaned or name


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


#: Clé phonétique Soundex FR en cache (durée de vie du processus) — sert aux
#: lignes sans ``_phonetic`` précalculé (tests, plausibles migrations) ; dans
#: l'index réel la clé est précalculée dans ``_dedupe_by_name``.
@functools.lru_cache(maxsize=2 ** 16)
def _phonetic_key(text: str) -> str:
    """Clé phonétique Soundex FR d'un nom — voir ``app/phonetic_fr.py`` et le
    docstring de module. ``phonetic_fr.phonetic`` gère lui-même casse, accents
    et ponctuation ; une entrée vide donne une clé vide (ratio 0, jamais
    retenue)."""
    return phonetic_fr.phonetic(text or "")


def _fuzzy_fallback(term: str, kind: str, language: str) -> DrugLookup:
    """Voir le commentaire de module. Classe TOUT l'extrait local par
    similarité (caractères ET clé phonétique française) au terme original —
    pas seulement les entrées partageant un préfixe — et retourne le meilleur
    si assez proche. Un extrait local indisponible (jamais téléchargé avec
    succès) dégrade en ``found=False``, jamais en exception.

    Le palier de confiance est décidé sur les CARACTÈRES, comme avant : le
    phonétique ne peut pas remonter un candidat ; il départe les égalités
    (cas réel ``Ensoprazole`` → LANSOPRAZOLE plutôt qu'ESOMEPRAZOLE) et
    ressuscite « faible » un candidat que seuls les caractères classaient
    introuvable (char >= ``_PHON_CHAR_FLOOR`` ET clé phonétique >=
    ``_PHON_RESCUE_THRESHOLD``)."""
    index = _load_local_index(kind, language)
    if not index:
        return DrugLookup(term=term, found=False)

    normalized_term = _normalize_text(term)
    term_phonetic = _phonetic_key(term)

    # score = (palier, ratio_caractères, ratio_phonétique), comparable
    # directement : le palier prime, puis les caractères, puis le phonétique.
    best_key: Optional[Tuple[float, float, float]] = None
    best_row: Optional[dict] = None
    for row in index:
        name_norm = row.get("_normalized_name") or ""
        if not name_norm:
            continue
        char_ratio = SequenceMatcher(None, normalized_term, name_norm).ratio()
        row_phonetic = row.get("_phonetic") or _phonetic_key(_strip_dose_suffix(_candidate_name(row)))
        phon_ratio = SequenceMatcher(None, term_phonetic, row_phonetic).ratio()

        if char_ratio >= _FUZZY_STRONG_THRESHOLD:
            tier = 3  # confiance élevée — décidé aux caractères, comme avant
        elif char_ratio >= _FUZZY_THRESHOLD:
            tier = 2  # candidat faible (à confirmer)
        elif char_ratio >= _PHON_CHAR_FLOOR and phon_ratio >= _PHON_RESCUE_THRESHOLD:
            tier = 2  # ressuscité « faible » par le phonétique — jamais fort
        else:
            continue

        key = (tier, char_ratio, phon_ratio)
        if best_key is None or key > best_key:
            best_key = key
            best_row = row

    if best_row is None:
        return DrugLookup(term=term, found=False)
    source = "dpd_fuzzy" if best_key[0] >= 3 else "dpd_fuzzy_weak"
    return _build_result(term, best_row, source=source)


# ---------------------------------------------------------------------------
# Extrait complet local — téléchargement, cache disque, index en mémoire.
# ---------------------------------------------------------------------------


def _cache_path(kind: str) -> str:
    return os.path.join(_DPD_CACHE_DIR, f"{kind}_extract.json")


def _phonetic_index_path(kind: str) -> str:
    """Fichier compagnon de l'extrait : l'index DÉDUPLIQUÉ et PRÉTRAITÉ
    (``_normalized_name`` + ``_phonetic``), qui évite de rejouer les quelques
    secondes d'encodage phonétique à chaque redémarrage du processus (voir
    le docstring de module). Reparti par ``kind`` — la clé phonétique est
    indépendante de la langue du téléchargement."""
    return os.path.join(_DPD_CACHE_DIR, f"{kind}_index.json")


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
    précalcule ``_normalized_name`` (caractères) ET ``_phonetic`` (clé Soundex
    FR, voir ``app/phonetic_fr.py``) — évite de renormaliser/recoder à chaque
    comparaison de similarité, ~58 000 fois par recherche floue sinon. Le
    prétraité phonétique coûte quelques secondes UNE fois par index, pas à
    chaque génération ; il est persisté par ``_load_local_index``."""
    seen: Dict[str, dict] = {}
    for row in rows:
        name = _candidate_name(row) if isinstance(row, dict) else ""
        if not name:
            continue
        key = _normalize_text(name)
        if key not in seen:
            comparable = _strip_dose_suffix(name)
            row = dict(row)
            row["_normalized_name"] = _normalize_text(comparable)
            row["_phonetic"] = _phonetic_key(comparable)
            seen[key] = row
    return list(seen.values())


def _load_local_index(kind: str, language: str) -> List[dict]:
    """Charge l'index flou pour ``kind`` — mémoire du processus d'abord, puis
    le fichier compagnon précalculé (``_phonetic_index_path``) s'il n'a pas
    plus de 7 jours, puis cache disque (``_DPD_CACHE_DIR``) + construction de
    l'index, puis téléchargement complet en dernier recours. Un cache périmé
    mais présent est préféré à une panne de téléchargement ; aucun index
    disponible dégrade en liste vide (voir ``_fuzzy_fallback``), jamais en
    exception."""
    if kind in _local_index:
        return _local_index[kind]

    index = _phonetic_index_from_disk(kind)
    if index is None:
        rows = _load_raw_rows(kind, language)
        index = _dedupe_by_name(rows or [])
        _phonetic_index_to_disk(kind, index)

    _local_index[kind] = index
    return index


def _load_raw_rows(kind: str, language: str) -> list:
    """Extrait brut (non dédupliqué) : cache disque puis téléchargement."""
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
    return rows or []


def _phonetic_index_from_disk(kind: str) -> Optional[list]:
    """Le fichier compagnon précalculé, s'il existe et est frais (7 jours,
    aligné sur l'extrait). Jamais une exception : tout échec retombe sur la
    construction complète (``_dedupe_by_name`` + encodage phonétique)."""
    path = _phonetic_index_path(kind)
    try:
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < _CACHE_MAX_AGE_SECONDS:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, list):
                return payload
    except Exception as exc:  # noqa: BLE001 — compagnon corrompu, pas fatal
        logger.warning("Index BDPP phonétique local illisible (%s) : %s", path, exc)
    return None


def _phonetic_index_to_disk(kind: str, index: List[dict]) -> None:
    """Persiste l'index prétraité — un travail de quelques secondes qu'on ne
    veut pas reprocher au redémarrage suivant. Jamais fatal en cas d'échec
    disque : le calcul sera simplement rejoué au prochain démarrage."""
    try:
        os.makedirs(_DPD_CACHE_DIR, exist_ok=True)
        with open(_phonetic_index_path(kind), "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 — disque plein/lecture seule, pas fatal
        logger.warning("Enregistrement de l'index BDPP phonétique échoué (%s) : %s", _phonetic_index_path(kind), exc)


# ---------------------------------------------------------------------------
# Source historique RxNorm — marques retirées / internationales (voir le
# docstring de module et les constantes _RX_* / _LEGACY_*).
# ---------------------------------------------------------------------------


def _legacy_index_path() -> str:
    return os.path.join(_RX_CACHE_DIR, "rxnorm_index.json")


def _fetch_rxnorm_names() -> List[str]:
    """Télécharge la release RxNorm prescribable et en extrait les noms de
    médication « propres » (alphabétiques, 3-40 lettres, sans forme posologique
    « 25 MG [...] », pour ne garder que des noms exploitables — marques comme
    ingrédients : Lopressor, Ativan, donepezil, sertraline…). Ne lève pas :
    l'appelant dégrade en liste vide (voir ``_load_legacy_index``)."""
    request = urllib.request.Request(_RX_URL, headers={"Accept": "application/zip"})
    with urllib.request.urlopen(request, timeout=_RX_BULK_TIMEOUT_SECONDS) as reponse:
        archive = reponse.read()
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        blob = zf.read("rrf/RXNCONSO.RRF").decode("utf-8")
    names: set = set()
    for line in blob.splitlines():
        cols = line.split("|")
        if len(cols) <= 14:
            continue
        name = cols[14].strip()
        if len(name) < 3 or len(name) > 40 or any(c.isdigit() for c in name):
            continue
        if "[" in name or "]" in name:
            continue
        names.add(name)
    return sorted(names)


def _load_legacy_index() -> List[dict]:
    """Index local RxNorm : mémoire du processus d'abord, puis compagnon
    disque (+ prétraitement phonétique), puis téléchargement en dernier
    recours. Tout échec dégrade en liste vide — la source historique est
    OPTIONNELLE, jamais un point de blocage."""
    global _legacy_index
    if _legacy_index is not None:
        return _legacy_index

    path = _legacy_index_path()
    rows: Optional[list] = None
    try:
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < _CACHE_MAX_AGE_SECONDS:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, list):
                rows = payload
    except Exception as exc:  # noqa: BLE001 — compagnon corrompu, pas fatal
        logger.warning("Index RxNorm local illisible (%s) : %s", path, exc)

    if rows is None:
        try:
            names = _fetch_rxnorm_names()
            rows = []
            for name in names:
                key = _normalize_text(name)
                rows.append({"name": name, "_normalized_name": key, "_phonetic": _phonetic_key(name)})
            os.makedirs(_RX_CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 — réseau/disque, jamais fatal
            logger.warning("Construction de l'index RxNorm échouée (%s) : %s", path, exc)
            try:
                with open(path, "r", encoding="utf-8") as f:  # cache périmé, mieux que rien
                    payload = json.load(f)
                rows = payload if isinstance(payload, list) else []
            except Exception:
                rows = []

    _legacy_index = rows or []
    return _legacy_index


def legacy_match(term: str) -> Optional[DrugLookup]:
    """Rapproche un terme transcrit contre les marques historiques RxNorm
    (retirées du marché, internationales) — source LOCALE, aucun egress.
    Retourne ``None`` si aucun candidat ne franchit le palier « à confirmer »,
    sinon un ``DrugLookup`` avec ``source="rxnorm"`` (DIN vide). Cette source
    reste TOUJOURS « faible » (jamais une correction apportée) : voir
    ``note_extraction._maybe_legacy`` et la consigne de l'outil."""
    term = (term or "").strip()
    if not term:
        return None
    index = _load_legacy_index()
    if not index:
        return None

    tc, tp = _normalize_text(term), _phonetic_key(term)
    best_key: Optional[Tuple[float, float]] = None  # (phon, char) — phon d'abord
    best_name: Optional[str] = None
    for row in index:
        ch = SequenceMatcher(None, tc, row["_normalized_name"]).ratio()
        ph = SequenceMatcher(None, tp, row["_phonetic"]).ratio()
        if not ((ph >= _LEGACY_PHON and ch >= _LEGACY_CHAR) or ch >= _LEGACY_CHAR_HI):
            continue
        key = (ph, ch)
        if best_key is None or key > best_key:
            best_key = key
            best_name = row.get("name")
    if best_name is None:
        return None
    return DrugLookup(term=term, found=True, matched_name=best_name, source="rxnorm")
