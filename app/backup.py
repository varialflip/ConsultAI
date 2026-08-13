"""
backup.py — Export / import complet de l'application.
=========================================================

Une seule fonction produit l'archive, que ce soit pour la sauvegarde
quotidienne automatique ou pour un « Exporter maintenant » manuel depuis le
panneau admin — les deux chemins doivent rester identiques, sans quoi l'un
des deux finit par dériver sans qu'on s'en aperçoive.

Contenu d'une archive : ``consultai.db`` (photo cohérente, voir
``_snapshot_db``) + l'arborescence ``audio/``. ``dictations/`` (tranches de
dictée en cours, éphémères, déjà purgées séparément — voir
``dictation.purge_expired``) et le dossier de sauvegardes lui-même en sont
exclus par construction : on ne zippe que ``settings.audio_dir``, jamais tout
``/data``.

La restauration est destructive par nature : avant de rien écraser, une
sauvegarde de l'état courant est prise (même fonction, ``kind="pre_restore"``)
puis un redémarrage manuel du conteneur est exigé — voir le sentinel
``RESTART_REQUIRED`` et le middleware dans ``app/main.py`` qui bloque les
écritures tant qu'il existe. Remplacer le fichier SQLite sous les pieds du
moteur SQLAlchemy en cours d'exécution, sans redémarrer, risquerait une
divergence silencieuse (l'ancien descripteur de fichier reste valide côté
POSIX même après que le fichier qu'il désignait a été remplacé).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.database import _iso, utcnow
from app import runtime_config

logger = logging.getLogger(__name__)

_DB_ENTRY = "consultai.db"
_MANIFEST_ENTRY = "manifest.json"
_AUDIO_PREFIX = "audio/"
_RESTART_SENTINEL = os.path.join(os.path.dirname(os.path.normpath(settings.audio_dir)), "RESTART_REQUIRED")


@dataclass
class BackupInfo:
    filename: str
    kind: str
    size_bytes: int
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "created_at": _iso(self.created_at),
        }


def _db_path() -> str:
    # database_url est de la forme sqlite:////data/consultai.db
    url = settings.database_url
    prefix = "sqlite:///"
    if url.startswith(prefix):
        return url[len(prefix):]
    raise RuntimeError(f"Sauvegarde non prise en charge pour cette base : {url}")


def _snapshot_db(dest_path: str) -> None:
    """Photo cohérente de la base via l'API de sauvegarde SQLite — jamais un
    simple ``shutil.copy``, qui risquerait de rater des pages encore dans le
    fichier ``-wal`` (mode WAL, voir app/database.py)."""
    source = sqlite3.connect(_db_path())
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def create_backup(kind: str = "manual") -> BackupInfo:
    """Fonction unique : appelée par la tâche quotidienne (``kind="scheduled"``),
    par « Exporter maintenant » (``kind="manual"``) et avant toute restauration
    (``kind="pre_restore"``)."""
    os.makedirs(settings.backup_dir, exist_ok=True)
    now = utcnow()
    filename = f"consultai-{kind}-{now.strftime('%Y%m%d-%H%M%S')}.zip"
    path = os.path.join(settings.backup_dir, filename)

    tmp_db = path + ".db.tmp"
    try:
        _snapshot_db(tmp_db)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(tmp_db, _DB_ENTRY)
            archive.writestr(_MANIFEST_ENTRY, json.dumps({
                "kind": kind,
                "created_at": now.isoformat(),
                "app": "ConsultAI",
                "format": 1,
            }))
            if os.path.isdir(settings.audio_dir):
                for root, _dirs, files in os.walk(settings.audio_dir):
                    for name in files:
                        full = os.path.join(root, name)
                        rel = _AUDIO_PREFIX + os.path.relpath(full, settings.audio_dir).replace(os.sep, "/")
                        archive.write(full, rel)
    finally:
        if os.path.exists(tmp_db):
            os.remove(tmp_db)

    size = os.path.getsize(path)
    logger.info("Sauvegarde créée : %s (%s, %.1f Mo)", filename, kind, size / 1_000_000)
    return BackupInfo(filename=filename, kind=kind, size_bytes=size, created_at=now)


def list_backups() -> List[BackupInfo]:
    if not os.path.isdir(settings.backup_dir):
        return []
    items: List[BackupInfo] = []
    for name in os.listdir(settings.backup_dir):
        if not name.endswith(".zip"):
            continue
        path = os.path.join(settings.backup_dir, name)
        kind = "manual"
        created_at = datetime.fromtimestamp(os.path.getmtime(path)).astimezone()
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read(_MANIFEST_ENTRY))
                kind = manifest.get("kind", kind)
                created_at = datetime.fromisoformat(manifest["created_at"])
        except (KeyError, ValueError, zipfile.BadZipFile, OSError):
            pass
        items.append(BackupInfo(filename=name, kind=kind, size_bytes=os.path.getsize(path), created_at=created_at))
    items.sort(key=lambda item: item.created_at, reverse=True)
    return items


def _safe_backup_path(filename: str) -> str:
    """N'accepte qu'un nom de fichier déjà listé par ``list_backups`` — un
    ``filename`` fourni par le client ne doit jamais être joint tel quel à
    ``BACKUP_DIR`` (traversée de chemin)."""
    if filename != os.path.basename(filename) or not filename.endswith(".zip"):
        raise ValueError("Nom de sauvegarde invalide")
    path = os.path.join(settings.backup_dir, filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(filename)
    return path


def get_backup_path(filename: str) -> str:
    return _safe_backup_path(filename)


def delete_backup(filename: str) -> None:
    os.remove(_safe_backup_path(filename))
    logger.info("Sauvegarde supprimée : %s", filename)


def rotate_backups(keep: int) -> int:
    """Garde les ``keep`` sauvegardes les plus récentes, tous types confondus
    (choix confirmé : pas de quota séparé pour les exports manuels/de
    sécurité). ``keep &lt;= 0`` désactive la rotation."""
    if keep <= 0:
        return 0
    items = list_backups()
    to_delete = items[keep:]
    for item in to_delete:
        try:
            delete_backup(item.filename)
        except OSError as exc:
            logger.warning("Rotation : %s non supprimée — %s", item.filename, exc)
    return len(to_delete)


def run_scheduled_backup() -> None:
    """Tâche planifiée quotidienne (voir app/scheduler.py)."""
    create_backup(kind="scheduled")
    keep = int(runtime_config.value_float("backup_retention_count", 7.0))
    rotate_backups(keep)


def restore_required() -> Optional[dict]:
    if not os.path.exists(_RESTART_SENTINEL):
        return None
    try:
        with open(_RESTART_SENTINEL, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"at": None, "source": ""}


def clear_restart_sentinel() -> Optional[dict]:
    """Appelée une fois au démarrage (voir lifespan() dans app/main.py).
    Un sentinel encore présent à ce moment signifie que le processus actuel
    EST le redémarrage attendu après une restauration — il a fait son office,
    on l'efface. Retourne son contenu (pour le journal) si un sentinel a
    effectivement été trouvé, sinon None."""
    pending = restore_required()
    if pending is not None and os.path.exists(_RESTART_SENTINEL):
        os.remove(_RESTART_SENTINEL)
    return pending


def restore_backup(source_path: str) -> BackupInfo:
    """
    Restauration destructive :
      1. sauvegarde de sécurité de l'état courant (même fonction, kind="pre_restore")
      2. validation du contenu de l'archive avant de rien toucher
      3. remplacement de consultai.db (+ -wal/-shm) et de audio/
      4. pose du sentinel qui bloque les écritures jusqu'au redémarrage manuel
    """
    with zipfile.ZipFile(source_path) as archive:
        names = set(archive.namelist())
        if _DB_ENTRY not in names:
            raise ValueError("Archive invalide : base de données absente")

        safety = create_backup(kind="pre_restore")
        logger.info("Restauration : sauvegarde de sécurité créée (%s)", safety.filename)

        tmp_db = os.path.join(settings.backup_dir, "_restore.db.tmp")
        with archive.open(_DB_ENTRY) as src, open(tmp_db, "wb") as dst:
            shutil.copyfileobj(src, dst)
        try:
            check = sqlite3.connect(tmp_db)
            try:
                (result,) = check.execute("PRAGMA integrity_check").fetchone()
            finally:
                check.close()
            if result != "ok":
                raise ValueError(f"Base restaurée corrompue : {result}")

            # Ferme les connexions du pool avant de remplacer le fichier —
            # n'élimine pas le besoin d'un redémarrage (voir docstring du
            # module) mais évite qu'une connexion déjà ouverte s'accroche à
            # l'ancien fichier plus longtemps que nécessaire.
            from app.database import engine
            engine.dispose()

            db_path = _db_path()
            for suffix in ("", "-wal", "-shm"):
                stale = db_path + suffix
                if os.path.exists(stale):
                    os.remove(stale)
            shutil.move(tmp_db, db_path)

            if os.path.isdir(settings.audio_dir):
                shutil.rmtree(settings.audio_dir)
            os.makedirs(settings.audio_dir, exist_ok=True)
            for name in names:
                if not name.startswith(_AUDIO_PREFIX) or name.endswith("/"):
                    continue
                rel = name[len(_AUDIO_PREFIX):]
                dest = os.path.join(settings.audio_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with archive.open(name) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        finally:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)

    os.makedirs(os.path.dirname(_RESTART_SENTINEL), exist_ok=True)
    with open(_RESTART_SENTINEL, "w", encoding="utf-8") as handle:
        json.dump({"at": utcnow().isoformat(), "source": os.path.basename(source_path)}, handle)

    logger.warning("Restauration terminée depuis %s — redémarrage du conteneur requis", source_path)
    return safety
