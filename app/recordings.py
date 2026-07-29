"""
recordings.py — Enregistrements audio conservés avec leur brouillon.
====================================================================

L'audio d'une consultation n'est plus détruit dès la transcription faite : il
reste attaché au brouillon, réécoutable, et disparaît avec lui.

POURQUOI LES GARDER
-------------------
La transcription automatique se trompe, et c'est en général sur ce qui compte :
une posologie, un chiffre de score, un nom de molécule. Pouvoir réécouter le
passage est le seul moyen de trancher — sinon il faut refaire l'entrevue.

POURQUOI LES SUPPRIMER AVEC LE BROUILLON
----------------------------------------
Un enregistrement de consultation est la donnée la plus sensible que produise
l'application : la voix du patient, non anonymisable. Il ne doit exister qu'un
seul geste à faire pour tout effacer. Supprimer le brouillon efface donc la
transcription, la note **et** l'audio, sans que rien ne survive quelque part.

ORGANISATION SUR DISQUE
-----------------------
    AUDIO_DIR/<consultation_id>/<uuid>.<ext>

Le fichier garde le format d'origine du navigateur (WebM/Opus, ou MP4/AAC sur
Safari) : c'est celui que ce même navigateur sait relire sans transcodage.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Consultation, Recording

logger = logging.getLogger(__name__)


#: Extension retenue par type MIME. Un format inconnu tombe sur « .bin » : le
#: fichier reste téléchargeable, seule la lecture dans la page peut échouer.
_EXTENSIONS = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
}


def _extension(mime_type: str) -> str:
    base = (mime_type or "").split(";")[0].strip().lower()
    return _EXTENSIONS.get(base, ".bin")


def _directory(consultation_id: int) -> str:
    path = os.path.join(settings.audio_dir, str(consultation_id))
    os.makedirs(path, exist_ok=True)
    return path


def absolute_path(recording: Recording) -> str:
    return os.path.join(settings.audio_dir, recording.filename)


def _register(
    db: Session,
    consultation: Consultation,
    relative: str,
    mime_type: str,
    size_bytes: int,
    duration_seconds: int,
    source: str,
) -> Recording:
    recording = Recording(
        consultation_id=consultation.id,
        owner=consultation.owner,
        filename=relative,
        mime_type=(mime_type or "audio/webm")[:100],
        size_bytes=size_bytes,
        duration_seconds=max(0, int(duration_seconds or 0)),
        source=source,
    )
    db.add(recording)
    db.commit()
    db.refresh(recording)
    logger.info(
        "Enregistrement %s conservé avec la consultation %s (%.1f Mo, %s s, %s)",
        recording.id, consultation.id, size_bytes / 1048576,
        recording.duration_seconds, source,
    )
    return recording


def store_bytes(
    db: Session,
    consultation: Consultation,
    data: bytes,
    mime_type: str,
    duration_seconds: int = 0,
    source: str = "import",
) -> Recording:
    """Écrit un enregistrement reçu en mémoire (import d'un fichier)."""
    name = f"{uuid.uuid4().hex}{_extension(mime_type)}"
    destination = os.path.join(_directory(consultation.id), name)
    with open(destination, "wb") as handle:
        handle.write(data)
    return _register(
        db, consultation, os.path.join(str(consultation.id), name),
        mime_type, len(data), duration_seconds, source,
    )


def store_path(
    db: Session,
    consultation: Consultation,
    source_path: str,
    mime_type: str,
    duration_seconds: int = 0,
    source: str = "dictee",
) -> Optional[Recording]:
    """
    Déplace un fichier déjà sur disque (l'audio accumulé pendant la dictée).

    Un déplacement plutôt qu'une copie : le fichier peut faire plusieurs
    dizaines de mégaoctets, et le dupliquer sur un NAS n'apporte rien.
    """
    if not os.path.exists(source_path) or os.path.getsize(source_path) == 0:
        return None
    name = f"{uuid.uuid4().hex}{_extension(mime_type)}"
    destination = os.path.join(_directory(consultation.id), name)
    size = os.path.getsize(source_path)
    shutil.move(source_path, destination)
    return _register(
        db, consultation, os.path.join(str(consultation.id), name),
        mime_type, size, duration_seconds, source,
    )


def for_consultation(db: Session, consultation_id: int) -> List[Recording]:
    return list(
        db.scalars(
            select(Recording)
            .where(Recording.consultation_id == consultation_id)
            .order_by(Recording.created_at)
        )
    )


def delete(db: Session, recording: Recording) -> None:
    """Efface le fichier puis la ligne. L'ordre importe peu, l'absence des
    deux est ce qui compte ; un fichier orphelin serait pire qu'une ligne
    orpheline, qui ne contient aucune donnée clinique."""
    try:
        os.remove(absolute_path(recording))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Enregistrement %s : fichier non supprimé — %s", recording.id, exc)
    db.delete(recording)
    db.commit()


def delete_for_consultation(db: Session, consultation_id: int) -> int:
    """Appelée à la suppression d'un brouillon : rien ne doit survivre."""
    rows = for_consultation(db, consultation_id)
    for recording in rows:
        try:
            os.remove(absolute_path(recording))
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Enregistrement %s : fichier non supprimé — %s", recording.id, exc)
        db.delete(recording)
    # Le dossier de la consultation n'a plus de raison d'être.
    shutil.rmtree(os.path.join(settings.audio_dir, str(consultation_id)), ignore_errors=True)
    if rows:
        logger.info("Consultation %s : %d enregistrement(s) supprimé(s)",
                    consultation_id, len(rows))
    return len(rows)
