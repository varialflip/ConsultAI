"""
scheduler.py — Le seul mécanisme récurrent de l'application.
==============================================================

Avant ce module, la seule chose « périodique » de ConsultAI tournait une
fois, au démarrage du conteneur (purge des dictées abandonnées, purge des
consultations expirées). Ça suffisait tant qu'aucune tâche n'avait besoin de
revenir chaque jour sans redémarrage — la sauvegarde quotidienne et le
compactage des statistiques d'usage en ont besoin.

Plutôt qu'une dépendance de planification (APScheduler, Celery…), une seule
boucle asyncio démarrée dans ``lifespan()`` : elle se réveille toutes les
heures, et pour chaque tâche enregistrée vérifie si elle a déjà tourné
aujourd'hui (date locale). Un conteneur resté éteint au moment habituel de
déclenchement rattrape donc la tâche à la prochaine heure de réveil, y
compris tout de suite après un redémarrage — sans logique supplémentaire.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Callable, Dict

from starlette.concurrency import run_in_threadpool

from app.database import SchedulerState, SessionLocal, utcnow

logger = logging.getLogger(__name__)

JobFn = Callable[[], None]

_JOBS: Dict[str, JobFn] = {}

#: Intervalle entre deux réveils. Une heure suffit très largement pour une
#: tâche qui ne doit courir qu'une fois par jour — inutile de descendre plus
#: bas, ça ne ferait que réveiller le processus pour rien.
_POLL_SECONDS = 3600


def register_daily_job(name: str, fn: JobFn) -> None:
    _JOBS[name] = fn


def _today() -> str:
    return date.today().isoformat()


def _run_job_once(name: str, fn: JobFn) -> None:
    """Exécute ``fn`` puis journalise le résultat dans ``scheduler_state``.
    Une tâche en échec ne doit ni bloquer les autres ni faire planter la
    boucle : l'erreur est journalisée, la tâche sera retentée au prochain
    réveil (son marqueur ``last_run_date`` n'aura pas été mis à jour)."""
    with SessionLocal() as db:
        state = db.get(SchedulerState, name)
        if state and state.last_run_date == _today():
            return
        try:
            fn()
        except Exception as exc:  # une tâche qui plante ne doit rien emporter d'autre
            logger.exception("Tâche planifiée « %s » en échec", name)
            if state is None:
                state = SchedulerState(job_name=name)
                db.add(state)
            state.last_status = "error"
            state.last_error = str(exc)[:2000]
            db.commit()
            return
        if state is None:
            state = SchedulerState(job_name=name)
            db.add(state)
        state.last_run_date = _today()
        state.last_run_at = utcnow()
        state.last_status = "ok"
        state.last_error = ""
        db.commit()
        logger.info("Tâche planifiée « %s » exécutée avec succès", name)


async def run_daily_loop() -> None:
    """Démarrée une fois depuis ``lifespan()``. Tourne jusqu'à annulation."""
    while True:
        for name, fn in list(_JOBS.items()):
            await run_in_threadpool(_run_job_once, name, fn)
        await asyncio.sleep(_POLL_SECONDS)
