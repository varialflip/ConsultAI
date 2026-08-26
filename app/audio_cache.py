"""
audio_cache.py — Cache de l'audio préparé pour la génération.
==============================================================

Le plafonnement des silences puis le transcodage (``stt.cap_silence_to`` /
``transcode_to``) coûtent plusieurs secondes par piste — mesuré 4,2 s pour
5 minutes d'audio, ~32 s pour 35 minutes. Payés AU CLIC « Mettre en forme »,
ils repoussaient d'autant les premiers mots de la note.

Ce module conserve le résultat PAR ENREGISTREMENT, dans un répertoire dédié :

    AUDIO_CACHE_DIR/<clé>.<ext>   (+ <clé>.json : type MIME et durée)

La clé résume tout ce qui change le résultat : identifiant et empreinte du
fichier source (taille + mtime), format demandé, état de la bascule
``stt_trim_silence`` et réglages du plafonnement. Un changement de réglage
produit une clé nouvelle — l'ancien artefact est simplement ignoré, jamais
servi à tort ; il disparaît à la purge de l'enregistrement.

Cycle prévu :
  * dès la fin d'une dictée (``main.py``), ``start_build`` lance la
    préparation en tâche de fond ;
  * à la génération, l'appelant consomme l'artefact s'il existe, attend
    brièvement une préparation déjà en course, sinon retombe sur la
    préparation à la demande — qui remplit le cache pour la fois suivante.

Le cache ne contient que du dérivé régénérable : hors sauvegarde, purgé avec
les enregistrements (``recordings.delete`` / ``delete_for_consultation``),
et sa suppression ne coûte qu'une nouvelle passe ffmpeg.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import threading
import time
from typing import List, Optional, Tuple

from app import runtime_config, stt
from app.config import settings

logger = logging.getLogger(__name__)

#: Attente maximale d'une préparation déjà en course (typiquement lancée à la
#: fin de la dictée, quelques secondes avant le clic). Au-delà — ou en cas
#: d'échec — l'appelant retombe sur sa voie classique. Une passe complète
#: mesure ~0,9× le temps réel de l'audio (35 min → ~32 s), largement sous
#: ce plafond.
WAIT_SECONDS = 90.0

#: Extension par format demandé (cf. ``stt._SEND_AUDIO_FORMATS``).
_EXTENSIONS = {"ogg": ".ogg", "mp3": ".mp3", "wav": ".wav"}

#: Événements des préparations en course, par clé.
_lock = threading.Lock()
_events: dict[str, threading.Event] = {}


def _cache_dir() -> str:
    os.makedirs(settings.audio_cache_dir, exist_ok=True)
    return settings.audio_cache_dir


def _extension(fmt: str) -> str:
    return _EXTENSIONS.get((fmt or "").strip().lower(), ".ogg")


def _mode(fmt: str) -> str:
    """
    Résumé des réglages qui changent le résultat de la préparation.

    Mêmes lectures que ``stt.cap_silence_to`` : la bascule globale et le
    maintien de silence viennent du panneau, le seuil des réglages fixes.
    """
    trim = runtime_config.value("stt_trim_silence") != "false"
    keep = max(0.0, runtime_config.value_float(
        "stt_silence_keep_seconds", settings.stt_silence_keep_seconds
    ))
    return (
        f"{(fmt or 'ogg').strip().lower()}"
        f"-{'trim' if trim else 'brut'}-keep{keep:.2f}"
        f"-{settings.stt_silence_threshold_db}db"
    )


def key_for(recording_id: int, source_path: str, fmt: str) -> str:
    """Clé de cache pour cet enregistrement dans son état ET ces réglages."""
    try:
        stat = os.stat(source_path)
        empreinte = f"{stat.st_size}-{int(stat.st_mtime_ns)}"
    except OSError:
        # Source absente : clé sans empreinte, la préparation échouera de
        # toute façon ; aucune collision possible avec un vrai fichier.
        empreinte = "absent"
    return f"{recording_id}-{empreinte}-{_mode(fmt)}"


def _paths(key: str, fmt: str) -> Tuple[str, str]:
    base = _cache_dir()
    return (
        os.path.join(base, key + _extension(fmt)),
        os.path.join(base, key + ".json"),
    )


def ready(key: str, fmt: str) -> bool:
    media, meta = _paths(key, fmt)
    return os.path.exists(media) and os.path.exists(meta)


def media_path(key: str, fmt: str) -> str:
    return _paths(key, fmt)[0]


def load(key: str, fmt: str) -> Optional[Tuple[bytes, str, float]]:
    """Artefact ``(contenu, type_mime, durée)`` stocké, ou ``None``."""
    media, meta = _paths(key, fmt)
    try:
        with open(meta, encoding="utf-8") as handle:
            infos = json.load(handle)
        with open(media, "rb") as handle:
            content = handle.read()
    except (OSError, ValueError):
        return None
    if not content:
        return None
    return content, str(infos.get("mime") or "audio/ogg"), float(infos.get("duration") or 0)


def store(key: str, fmt: str, content: bytes, mime: str, duration: float) -> None:
    """Écriture atomique du média puis de ses métadonnées."""
    media, meta = _paths(key, fmt)
    tmp_media, tmp_meta = media + ".tmp", meta + ".tmp"
    with open(tmp_media, "wb") as handle:
        handle.write(content)
    with open(tmp_meta, "w", encoding="utf-8") as handle:
        json.dump({"mime": mime, "duration": duration}, handle)
    os.replace(tmp_media, media)
    os.replace(tmp_meta, meta)


def build_now(recording_id: int, source_path: str, fmt: str) -> bool:
    """
    Prépare l'artefact au premier plan si absent — exactement la même chaîne
    que la voie historique (plafonnement, puis transcodage en repli).

    Renvoie ``True`` si un artefact exploitable existe au retour.
    """
    key = key_for(recording_id, source_path, fmt)
    if ready(key, fmt):
        return True
    t0 = time.monotonic()
    result = stt.cap_silence_to(source_path, fmt)
    if result is None:
        result = stt.transcode_to(source_path, fmt)
    if result is None:
        logger.warning(
            "Cache audio (enregistrement %s) : préparation impossible",
            recording_id,
        )
        return False
    content, mime, duration = result
    if not content or duration <= 0:
        return False
    store(key, fmt, content, mime, duration)
    logger.info(
        "Cache audio (enregistrement %s) : prêt en %.1f s (%.1f Mo, %.1f s)",
        recording_id, time.monotonic() - t0, len(content) / 1048576, duration,
    )
    return True


def _run_build(recording_id: int, source_path: str, fmt: str, event: threading.Event) -> None:
    try:
        build_now(recording_id, source_path, fmt)
    except Exception:  # une tâche de fond ne doit jamais mourir en silence
        logger.exception("Cache audio (enregistrement %s) : échec", recording_id)
    finally:
        event.set()


def start_build(recording_id: int, source_path: str, fmt: str) -> None:
    """
    Lance la préparation en tâche de fond, sauf si elle court déjà.

    Jamais d'exception : appelée depuis la fin de dictée, elle ne doit rien
    casser si le disque refuse ou si les réglages divergent — la génération a
    de toute façon son repli.
    """
    try:
        if not os.path.exists(source_path):
            return
        key = key_for(recording_id, source_path, fmt)
        with _lock:
            if _events.get(key) is not None:
                return
            event = threading.Event()
            _events[key] = event
        threading.Thread(
            target=_run_build,
            args=(recording_id, source_path, fmt, event),
            daemon=True,
            name=f"audio-cache-{recording_id}",
        ).start()
    except Exception:
        logger.exception(
            "Lancement du cache audio impossible (enregistrement %s)", recording_id
        )
        with _lock:
            _events.pop(key_for(recording_id, source_path, fmt), None)


def ensure_ready(
    recording_id: int, source_path: str, fmt: str, attendre: bool = True,
) -> bool:
    """
    Artefact présent ? Sinon lance la préparation et — option — attend la fin
    d'une course déjà engagée (fin de dictée toute proche). Ne lance JAMAIS
    deux threads pour la même clé.
    """
    key = key_for(recording_id, source_path, fmt)
    if ready(key, fmt):
        return True
    with _lock:
        event = _events.get(key)
    if event is None:
        start_build(recording_id, source_path, fmt)
        with _lock:
            event = _events.get(key)
    if event is not None and attendre:
        event.wait(WAIT_SECONDS)
    return ready(key, fmt)


def mode_signature(fmt: str) -> str:
    """Signature publique des réglages qui changent le résultat (cf. ``_mode``)."""
    return _mode(fmt)


def adopt_pair(
    media_path: str, meta_path: str,
    recording_id: int, final_source_path: str, fmt: str,
) -> bool:
    """
    Range une paire artefact construite dans le dossier d'une session de
    dictée sous la clé définitive de l'enregistrement (les fichiers sont
    consommés par ``os.replace`` — même système de fichiers : renommage pur).

    La clé est recalculée depuis le chemin DÉFINITIF de la source :
    ``shutil.move`` préserve taille et mtime, la clé correspond donc à celle
    que recalculera la génération. Renvoie ``True`` si un artefact prêt
    existe au retour.
    """
    key = key_for(recording_id, final_source_path, fmt)
    media, meta = _paths(key, fmt)
    try:
        os.replace(media_path, media)
        os.replace(meta_path, meta)
    except OSError as exc:
        logger.warning(
            "Adoption du cache audio impossible (enregistrement %s) : %s",
            recording_id, exc,
        )
        return ready(key, fmt)
    logger.info("Cache audio (enregistrement %s) : artefact de session adopté", recording_id)
    return True


def purge(recording_id: int) -> None:
    """Supprime les artefacts d'un enregistrement (appelé avec sa source)."""
    try:
        motif = os.path.join(_cache_dir(), f"{recording_id}-*")
        for chemin in glob.glob(motif):
            try:
                os.remove(chemin)
            except OSError as exc:
                logger.warning("Cache audio %s non supprimé : %s", chemin, exc)
    except Exception:  # pragma: no cover
        logger.exception("Purge du cache audio impossible (%s)", recording_id)


def all_paths(keys: List[str], fmt: str) -> Optional[List[str]]:
    """Chemins média des clés données, ou ``None`` si une seule manque."""
    chemins: List[str] = []
    for key in keys:
        if not ready(key, fmt):
            return None
        chemins.append(media_path(key, fmt))
    return chemins
