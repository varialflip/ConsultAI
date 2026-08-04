"""
preferences.py — Préférences propres à chaque usager.
===========================================================================

Pour l'instant une seule : la langue de l'interface. Elle mérite pourtant son
module, parce qu'elle ne se lit pas comme un réglage d'instance.

DEUX PORTÉES, DEUX ENDROITS
---------------------------
``runtime_config`` décrit l'installation : quel service vocal, quel modèle,
quelles clés. Ces réglages sont ceux d'un administrateur et valent pour tout le
monde.

La langue, elle, regarde la personne qui lit l'écran. Deux médecins partageant
la même installation doivent pouvoir travailler l'un en français et l'autre en
anglais, sans se marcher sur les pieds et sans passer par un administrateur.

    langue effective  =  préférence de l'usager  sinon  APP_LANGUAGE du .env

COMMENT LA LANGUE ATTEINT LE FOND DE L'APPLICATION
--------------------------------------------------
``stt.py`` et ``llm.py`` ont besoin de la langue très loin du point d'entrée
HTTP — au moment de composer une requête à un service vocal, ou une consigne
pour le modèle. Faire descendre un paramètre ``username`` à travers toute cette
pile aurait touché des dizaines de signatures pour une valeur qui ne varie pas
au cours d'une requête.

On emploie donc une variable de contexte (``contextvars``), fixée une fois par
le middleware d'authentification et lue partout ailleurs. C'est le mécanisme
prévu pour cela en Python asynchrone : chaque requête a sa propre valeur, et le
contexte est **recopié** par ``asyncio.create_task`` comme par
``run_in_threadpool``. La passe de découpage d'une dictée, lancée en tâche de
fond et qui survit à la réponse HTTP, garde donc la langue de l'usager qui l'a
déclenchée.

Hors requête (démarrage, tâche de purge), la variable est vide et la langue
retombe sur celle du ``.env``.
"""

from __future__ import annotations

import logging
import threading
from contextvars import ContextVar
from typing import Dict, Optional

from app import i18n
from app.config import settings
from app.database import SessionLocal, UserPreference

# ---------------------------------------------------------------------------
# Thèmes de couleur offerts à l'usager
# ---------------------------------------------------------------------------
# Ajouter une entrée ici suffit : les variables CSS correspondantes sont
# définies dans le <style> du gabarit, l'API /api/config les expose et le
# sélecteur dans le menu d'identité les affiche.
THEMES: list[tuple[str, str, str, str]] = [
    # (clé,     nom_fr,     nom_en,      hex_accent)
    ("teal",    "Sarcelle",  "Teal",      "#0f766e"),
    ("crimson", "Carmin",    "Crimson",   "#e11d48"),
    ("amber",   "Ambre",     "Amber",     "#d97706"),
    ("blue",    "Bleu",      "Blue",      "#1d4ed8"),
    ("green",   "Vert",      "Green",     "#16a34a"),
    ("violet",  "Violet",    "Violet",    "#7c3aed"),
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Langue de la requête en cours
# ---------------------------------------------------------------------------
#: Vide hors requête HTTP. Le défaut ``None`` est important : il distingue
#: « aucun usager » de « usager sans préférence », qui retombent tous deux sur
#: le ``.env`` mais pour des raisons différentes.
_request_language: ContextVar[Optional[str]] = ContextVar(
    "consultai_request_language", default=None
)


def bind_language(language: Optional[str]) -> None:
    """Fixe la langue de la requête en cours. Appelé par le middleware."""
    _request_language.set(i18n.normalize(language) if language else None)


#: Langue du DOCUMENT en cours de production, distincte de celle de l'écran.
#:
#: Elle vient du gabarit et de nulle part ailleurs. La séparer de la langue
#: d'interface est nécessaire : un médecin peut lire l'écran en français et
#: produire une note anglaise à partir d'un gabarit anglais. Confondre les deux
#: ferait dépendre le contenu du document d'une préférence d'affichage.
_document_language: ContextVar[Optional[str]] = ContextVar(
    "consultai_document_language", default=None
)


def bind_document_language(language: Optional[str]) -> None:
    """Fixe la langue du document pour la suite du traitement."""
    _document_language.set(i18n.normalize(language) if language else None)


def document_language() -> str:
    """
    Langue du document : celle du gabarit si elle est fixée, sinon celle de
    l'usager.

    Le repli sur la langue de l'usager sert les cas où aucun gabarit n'est en
    jeu — un fichier audio importé sans gabarit, par exemple.
    """
    portee = _document_language.get()
    if portee:
        return portee
    return current_language()


def current_language() -> str:
    """
    Langue effective : celle de l'usager de la requête, sinon celle du ``.env``.

    Ne lève jamais et n'interroge pas la base : la lecture a déjà eu lieu une
    fois, au début de la requête.
    """
    portee = _request_language.get()
    if portee:
        return portee
    return i18n.normalize(settings.app_language)


# ---------------------------------------------------------------------------
# Thème de la requête en cours
# ---------------------------------------------------------------------------
# Même mécanisme que la langue : le middleware fixe la valeur une fois, et le
# reste de l'application la lit sans repasser par la base.
_request_theme: ContextVar[Optional[str]] = ContextVar(
    "consultai_request_theme", default=None
)


def bind_theme(theme: Optional[str]) -> None:
    """Fixe le thème de la requête en cours. Appelé par le middleware."""
    _request_theme.set(theme if theme else None)


def current_theme() -> str:
    """
    Thème effectif : celui de l'usager de la requête, sinon le défaut.

    Le défaut est toujours « teal », qui est le thème historique. Une
    installation qui voudrait en changer pourrait ajouter une variable
    d'environnement, mais le besoin n'existe pas pour l'instant.
    """
    portee = _request_theme.get()
    if portee:
        return portee
    return "teal"


# ---------------------------------------------------------------------------
# Stockage
# ---------------------------------------------------------------------------
# Le middleware lit cette préférence à CHAQUE requête, y compris pour les
# fichiers statiques : on la garde donc en mémoire. Le cache est vidé à
# l'écriture, il n'est donc jamais périmé, et il ne vaut que pour le processus
# courant — ce qui suffit, ConsultAI tournant en un seul worker uvicorn.
_cache: Dict[str, str] = {}
_cache_lock = threading.Lock()


def _key(username: str) -> str:
    """Identité normalisée : la casse renvoyée par le SSO peut varier."""
    return (username or "").strip().lower()


def language_for(username: str) -> str:
    """
    Préférence enregistrée, ou chaîne vide si l'usager n'a jamais choisi.

    Une erreur de base ne doit pas empêcher l'affichage : on retourne alors une
    chaîne vide, l'appelant retombe sur le défaut de l'installation.
    """
    cle = _key(username)
    if not cle:
        return ""

    with _cache_lock:
        if cle in _cache:
            return _cache[cle]

    valeur = ""
    try:
        with SessionLocal() as db:
            row = db.get(UserPreference, cle)
            if row is not None and row.language:
                valeur = i18n.normalize(row.language)
    except Exception as exc:  # base absente, disque plein…
        logger.warning("Préférence de langue non lue pour %s : %s", cle, exc)
        return ""

    with _cache_lock:
        _cache[cle] = valeur
    return valeur


def set_language(username: str, language: str) -> str:
    """
    Enregistre la langue de cet usager et retourne la valeur retenue.

    Une valeur vide **supprime** la préférence : l'usager suit alors de nouveau
    le défaut de l'installation. C'est le même contrat que dans le panneau
    d'administration, où vider un champ rend la main au ``.env``.

    Une langue inconnue est refusée plutôt que ramenée silencieusement au
    français : elle ne peut venir que d'un appel fabriqué à la main, et
    l'accepter en la corrigeant masquerait l'erreur.
    """
    cle = _key(username)
    if not cle:
        raise ValueError("Identité manquante.")

    demande = (language or "").strip().lower()
    if demande and demande not in dict(i18n.LANGUAGES):
        raise ValueError(f"Langue inconnue : {language}")

    with SessionLocal() as db:
        row = db.get(UserPreference, cle)
        if not demande:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(UserPreference(username=cle, language=demande))
        else:
            row.language = demande
        db.commit()

    with _cache_lock:
        _cache[cle] = demande

    logger.info("Langue de %s : %s", cle, demande or "défaut de l'installation")
    return demande or i18n.normalize(settings.app_language)


# ---------------------------------------------------------------------------
# Stockage — thème
# ---------------------------------------------------------------------------
# Même stratégie que pour la langue : cache en mémoire, lecture à l'écriture.
_theme_cache: Dict[str, str] = {}
_theme_cache_lock = threading.Lock()

# Clefs valides, pratique pour les vérifications.
_THEME_KEYS = {t[0] for t in THEMES}


def theme_for(username: str) -> str:
    """
    Thème enregistré, ou chaîne vide.

    Une erreur de base retourne une chaîne vide : l'appelant retombe sur le
    défaut « teal ».
    """
    cle = _key(username)
    if not cle:
        return ""

    with _theme_cache_lock:
        if cle in _theme_cache:
            return _theme_cache[cle]

    valeur = ""
    try:
        with SessionLocal() as db:
            row = db.get(UserPreference, cle)
            if row is not None and row.theme_color:
                valeur = row.theme_color
    except Exception as exc:
        logger.warning("Thème non lu pour %s : %s", cle, exc)
        return ""

    with _theme_cache_lock:
        _theme_cache[cle] = valeur
    return valeur


def set_theme(username: str, theme: str) -> str:
    """
    Enregistre le thème et retourne la valeur retenue.

    Une valeur vide SUPPRIME la préférence — l'usager suit le défaut.
    Un thème inconnu est refusé.
    """
    cle = _key(username)
    if not cle:
        raise ValueError("Identité manquante.")

    demande = (theme or "").strip().lower()
    if demande and demande not in _THEME_KEYS:
        raise ValueError(f"Thème inconnu : {theme}")

    with SessionLocal() as db:
        row = db.get(UserPreference, cle)
        if not demande:
            if row is not None:
                row.theme_color = ""
        elif row is None:
            db.add(UserPreference(username=cle, theme_color=demande))
        else:
            row.theme_color = demande
        db.commit()

    with _theme_cache_lock:
        _theme_cache[cle] = demande

    logger.info("Thème de %s : %s", cle, demande or "défaut")
    return demande or "teal"


def invalidate() -> None:
    """Vide le cache. Utile aux tests et après une écriture directe en base."""
    with _cache_lock:
        _cache.clear()
    with _theme_cache_lock:
        _theme_cache.clear()
