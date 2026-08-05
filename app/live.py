"""
live.py — Diffusion en direct entre onglets/appareils d'un même usager.
=========================================================================

POURQUOI
--------
Un médecin dicte parfois sur son téléphone pendant qu'un onglet de bureau
reste ouvert sur la même consultation. Sans ce module, ce second onglet ne
voit rien tant qu'on ne le rafraîchit pas à la main — c'est la confusion que
ce fichier corrige : chaque écriture durable (nouvelle tranche transcrite,
note générée, enregistrement ajouté ou retiré, brouillon modifié) est publiée
ici et rejoint aussitôt tous les onglets ouverts par le MÊME usager, via
``GET /api/events`` (voir app/main.py).

ARCHITECTURE : UN SEUL PROCESSUS, EN MÉMOIRE
----------------------------------------------
Le registre d'abonnés vit en mémoire, exactement comme les verrous de
app/dictation.py (voir son commentaire « Verrous ») : cela ne fonctionne QUE
parce que ConsultAI tourne en un seul worker uvicorn (voir Dockerfile,
``--workers 1``). Si ce nombre augmentait un jour, deux onglets pourraient
atterrir sur deux processus différents et cesseraient de se voir — sans
erreur, silencieusement. Toute évolution vers plusieurs workers devra
remplacer ce module par quelque chose de partagé entre processus (Redis
pub/sub, par exemple) plutôt que d'ajuster celui-ci.

APPEL DEPUIS N'IMPORTE QUEL FIL D'EXÉCUTION
--------------------------------------------
``publish()`` est appelée aussi bien depuis une route FastAPI ``async def``
(sur la boucle d'évènements) que depuis ``dictation._store_part()``, qui
tourne dans le threadpool via ``run_in_threadpool``. Plutôt que de distinguer
les appelants « sûrs » des autres, ``publish()`` planifie systématiquement la
remise via ``loop.call_soon_threadsafe`` : un seul chemin, jamais de piège.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

#: Boucle d'évènements capturée une fois au démarrage (voir bind_loop, appelé
#: depuis app.main.lifespan). Tant qu'elle est None — avant le démarrage
#: complet, ou en test sans lifespan — publish() journalise et abandonne
#: plutôt que d'échouer : une mise à jour en direct manquée ne doit jamais
#: faire échouer la requête HTTP qui l'a déclenchée.
_loop: Optional[asyncio.AbstractEventLoop] = None

#: Abonnés par usager (``owner_key``). Une consultation n'appartient jamais
#: qu'à un seul usager (voir Consultation.owner) : une file par usager suffit
#: à couvrir tous ses appareils et toutes ses consultations ouvertes.
_subscribers: Dict[str, Set["asyncio.Queue"]] = {}
_guard = threading.Lock()

#: Profondeur maximale d'une file. Un onglet oublié en arrière-plan (Safari
#: iOS notamment) peut ne plus jamais consommer : sans plafond, sa file
#: grossirait indéfiniment. Passé ce plafond, les évènements les plus anciens
#: sont sacrifiés — un onglet qui se réveille perd un peu d'historique plutôt
#: que de faire grossir la mémoire du conteneur sans limite.
_MAX_QUEUE = 200


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """À appeler une fois, au démarrage (voir app.main.lifespan)."""
    global _loop
    _loop = loop


def subscribe(owner_key: str) -> "asyncio.Queue":
    """Nouvel abonné pour cet usager — un par connexion SSE ouverte."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
    with _guard:
        _subscribers.setdefault(owner_key, set()).add(queue)
    return queue


def unsubscribe(owner_key: str, queue: "asyncio.Queue") -> None:
    """À appeler quand la connexion SSE se ferme (voir le ``finally`` de la route)."""
    with _guard:
        subs = _subscribers.get(owner_key)
        if subs is None:
            return
        subs.discard(queue)
        if not subs:
            _subscribers.pop(owner_key, None)


def publish(owner_key: str, event: str, data: dict) -> None:
    """
    Dépose ``(event, data)`` dans la file de chaque onglet ouvert par
    ``owner_key``. Sûre à appeler depuis n'importe quel fil d'exécution — voir
    l'en-tête du fichier. Ne lève jamais : une diffusion manquée ne doit
    jamais faire échouer la requête qui l'a déclenchée.
    """
    if _loop is None:
        logger.warning(
            "live.publish(%s, %s) ignoré : boucle non encore liée (bind_loop)",
            owner_key, event,
        )
        return

    def _deliver() -> None:
        with _guard:
            queues = list(_subscribers.get(owner_key, ()))
        for queue in queues:
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                # File pleine : l'abonné ne consomme plus (onglet en
                # sommeil). On sacrifie le plus ancien plutôt que
                # l'évènement courant — c'est la mise à jour la plus
                # récente qui compte le plus à la reprise.
                try:
                    queue.get_nowait()
                    queue.put_nowait((event, data))
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    try:
        _loop.call_soon_threadsafe(_deliver)
    except Exception:
        logger.exception("live.publish(%s, %s) : échec de planification", owner_key, event)
