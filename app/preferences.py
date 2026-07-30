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


def invalidate() -> None:
    """Vide le cache. Utile aux tests et après une écriture directe en base."""
    with _cache_lock:
        _cache.clear()
