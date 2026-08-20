"""
dictation.py — Dictée téléversée au fil de l'eau.
=================================================

POURQUOI
--------
L'ancienne trajectoire gardait toute la dictée dans la mémoire de l'onglet et
ne l'envoyait qu'au moment de « Terminer ». Une coupure Wi-Fi, un onglet tué
par iOS ou une erreur serveur à cet instant précis effaçaient vingt minutes de
consultation, sans aucun moyen de réessayer. C'est le pire moment possible
pour une panne : le patient est reparti.

La trajectoire actuelle :

    navigateur ──(fragments de ~5 s)──▶  fichier « raw » sur le serveur
        │                                        │
        └── copie locale (IndexedDB)             ├─▶ tranche de ~10 s (ffmpeg)
            gardée jusqu'à la fin réussie        └─▶ Google STT ─▶ brouillon

Trois garanties en découlent :
  1. le serveur détient l'audio à quelques secondes près en permanence ;
  2. le navigateur en garde une copie tant que la dictée n'est pas conclue,
     donc un envoi raté peut être rejoué ;
  3. le texte apparaît pendant la dictée, tranche par tranche.

DÉCOUPAGE
---------
Les fragments sont concaténés tels quels : le résultat est un conteneur
tronqué, illisible par Google mais parfaitement décodable par ffmpeg. On en
extrait des tranches autonomes, en cherchant un silence autour de la durée
cible (`dictation_segment_seconds`, 10 s par défaut) plutôt qu'en coupant à la
seconde fixe (voir ``stt.find_cut_point``).

Le curseur ``offset_seconds`` n'avance que de la durée **mesurée** sur la
tranche produite : aucune dérive ne peut faire sauter un passage.

ÉTAT
----
Une session = un dossier sous ``DICTATION_DIR`` contenant ``raw`` (l'audio
brut) et ``state.json``. Rien en base : une dictée abandonnée n'a pas à
polluer le schéma, et un redémarrage du conteneur en pleine dictée laisse le
dossier intact — « Terminer » fonctionne encore après.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app import live, llm, recordings, runtime_config, usage
from app.config import settings
from app.database import Consultation, SessionLocal, utcnow
from sqlalchemy.orm import Session
from app.stt import (
    TranscriptionError,
    extract_segment,
    find_cut_point,
    transcribe,
    transcribe_payload,
)

logger = logging.getLogger(__name__)


class DictationError(RuntimeError):
    """Erreur métier, avec un message affichable à l'écran."""


class SessionNotFound(DictationError):
    pass


class SequenceMismatch(DictationError):
    """Le client a sauté un fragment : il doit se resynchroniser."""

    def __init__(self, expected: int):
        super().__init__(
            f"Fragment hors séquence : le serveur attend le numéro {expected}."
        )
        self.expected = expected


# ---------------------------------------------------------------------------
# Réglages dérivés
# ---------------------------------------------------------------------------
def _target_seconds() -> float:
    return max(10.0, float(settings.dictation_segment_seconds))


def _window() -> tuple:
    """(minimum, cible, maximum) d'une tranche, en secondes."""
    target = _target_seconds()
    return target * 0.6, target, target * 1.15


#: Marge au-delà du maximum avant de tenter une coupe : le silence retenu doit
#: pouvoir se situer *après* la durée visée, donc toute la fenêtre de recherche
#: doit déjà être arrivée sur le serveur.
_HEADROOM_SECONDS = 2.0

#: En deçà, la tranche extraite est considérée comme la fin du fichier.
_MIN_SEGMENT_SECONDS = 0.7

#: Délai sans AUCUNE activité (fragment reçu, ou scrutation de l'onglet qui
#: enregistre) après lequel une dictée est réputée abandonnée : l'onglet est
#: mort (navigateur fermé), le brouillon doit être marqué et son audio
#: conservé. Le client rafraîchit ``updated_at`` à chaque scrutation (~7 s)
#: tant que la page est ouverte, donc une dictée en pause ne devient jamais
#: « abandonnée ». Marge volontairement confortable pour tolérer la mise en
#: veille des minuteurs dans un onglet d'arrière-plan. Voir
#: ``cleanup_abandoned``.
_STALE_AFTER = 300.0

#: En deçà de cette durée d'audio reçue, une dictée abandonnée n'a rien à
#: conserver : la session et le brouillon vide sont supprimés. L'audio est le
#: critère (pas la transcription) : un fournisseur en audio direct ne produit
#: jamais de transcription, seul l'audio compte.
_MIN_AUDIO_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Structure d'une session
# ---------------------------------------------------------------------------
@dataclass
class DictationSession:
    id: str
    username: str
    consultation_id: int
    template_id: Optional[int] = None
    mime_type: str = "audio/webm"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    next_seq: int = 0
    bytes_received: int = 0
    received_seconds: float = 0.0     # estimation fournie par le navigateur
    offset_seconds: float = 0.0       # audio déjà transcrit
    parts: List[str] = field(default_factory=list)
    status: str = "recording"         # recording | finished | error
    last_error: str = ""

    # -- Chemins ------------------------------------------------------------
    @property
    def directory(self) -> str:
        return os.path.join(settings.dictation_dir, self.id)

    @property
    def audio_path(self) -> str:
        return os.path.join(self.directory, "raw")

    @property
    def state_path(self) -> str:
        return os.path.join(self.directory, "state.json")

    # -- Sérialisation ------------------------------------------------------
    def to_state(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "consultation_id": self.consultation_id,
            "template_id": self.template_id,
            "mime_type": self.mime_type,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_seq": self.next_seq,
            "bytes_received": self.bytes_received,
            "received_seconds": self.received_seconds,
            "offset_seconds": self.offset_seconds,
            "parts": self.parts,
            "status": self.status,
            "last_error": self.last_error,
        }

    def to_public(self) -> dict:
        """Vue transmise au navigateur (sans le nom d'utilisateur)."""
        return {
            "session_id": self.id,
            "consultation_id": self.consultation_id,
            "status": self.status,
            "next_seq": self.next_seq,
            "parts": self.parts,
            "part_count": len(self.parts),
            "transcribed_seconds": int(round(self.offset_seconds)),
            "received_seconds": int(round(self.received_seconds)),
            "bytes_received": self.bytes_received,
            "created_at": self.created_at,
            # Dernière écriture (dernier fragment reçu) : c'est ce qui permet
            # au navigateur de distinguer une session vraiment abandonnée
            # d'une autre encore active sur un autre appareil (voir
            # refreshRecoveryBanner côté JS, et list_sessions ci-dessous).
            "updated_at": self.updated_at,
            "last_error": self.last_error,
        }

    def save(self) -> None:
        self.updated_at = time.time()
        temporary = f"{self.state_path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.to_state(), handle, ensure_ascii=False)
        os.replace(temporary, self.state_path)


# ---------------------------------------------------------------------------
# Verrous
# ---------------------------------------------------------------------------
# Deux requêtes de la même session ne doivent jamais écrire dans « raw » en
# même temps, et une seule passe de découpage doit tourner à la fois. Les
# verrous vivent en mémoire : ils ne protègent qu'à l'intérieur d'un
# processus, ce qui suffit — ConsultAI tourne en un seul worker uvicorn.
_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_processing: set = set()


def _lock_for(session_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(session_id, threading.Lock())


def _forget_lock(session_id: str) -> None:
    with _locks_guard:
        _locks.pop(session_id, None)
        _processing.discard(session_id)


#: Verrous par CONSULTATION, distincts des verrous par session ci-dessus.
#: Deux sessions différentes (par exemple, une par appareil) peuvent cibler la
#: même consultation — chacune a son propre verrou de session, qui ne les
#: empêche donc pas d'écrire dans ``raw_transcript`` en même temps. Rare tant
#: que rien ne l'encourageait, mais la diffusion en direct (voir app/live.py)
#: rend ce scénario plus tentant : un médecin qui voit ses deux appareils
#: progresser en direct peut être tenté de dicter depuis les deux à la fois.
_consultation_locks: Dict[int, threading.Lock] = {}
_consultation_locks_guard = threading.Lock()


def _lock_for_consultation(consultation_id: int) -> threading.Lock:
    with _consultation_locks_guard:
        return _consultation_locks.setdefault(consultation_id, threading.Lock())


def try_begin_processing(session_id: str) -> bool:
    """
    Réserve la passe de découpage. Renvoie False si une autre tourne déjà :
    inutile d'empiler les tâches de fond, celle en cours traitera de toute
    façon l'audio arrivé entre-temps.
    """
    with _locks_guard:
        if session_id in _processing:
            return False
        _processing.add(session_id)
        return True


def end_processing(session_id: str) -> None:
    with _locks_guard:
        _processing.discard(session_id)


# ---------------------------------------------------------------------------
# Cycle de vie
# ---------------------------------------------------------------------------
def _root() -> str:
    os.makedirs(settings.dictation_dir, exist_ok=True)
    return settings.dictation_dir


def create_session(
    username: str,
    consultation_id: int,
    template_id: Optional[int],
    mime_type: str,
) -> DictationSession:
    session = DictationSession(
        id=uuid.uuid4().hex,
        username=username,
        consultation_id=consultation_id,
        template_id=template_id,
        mime_type=(mime_type or "audio/webm")[:100],
    )
    os.makedirs(session.directory, exist_ok=True)
    open(session.audio_path, "wb").close()
    session.save()
    logger.info(
        "Dictée %s ouverte par %s (consultation %s, %s)",
        session.id, username, consultation_id, session.mime_type,
    )
    return session


def load_session(session_id: str, username: str) -> DictationSession:
    # Le nom de dossier vient du client : on refuse tout ce qui n'est pas un
    # identifiant hexadécimal, sans quoi « ../ » sortirait du répertoire.
    if not session_id or not all(c in "0123456789abcdef" for c in session_id):
        raise SessionNotFound("Identifiant de dictée invalide.")

    path = os.path.join(_root(), session_id, "state.json")
    if not os.path.exists(path):
        raise SessionNotFound("Cette dictée n'existe plus sur le serveur.")

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    session = DictationSession(
        id=data["id"],
        username=data["username"],
        consultation_id=data["consultation_id"],
        template_id=data.get("template_id"),
        mime_type=data.get("mime_type", "audio/webm"),
        created_at=data.get("created_at", time.time()),
        updated_at=data.get("updated_at", time.time()),
        next_seq=data.get("next_seq", 0),
        bytes_received=data.get("bytes_received", 0),
        received_seconds=data.get("received_seconds", 0.0),
        offset_seconds=data.get("offset_seconds", 0.0),
        parts=data.get("parts", []),
        status=data.get("status", "recording"),
        last_error=data.get("last_error", ""),
    )
    if session.username != username:
        # Message volontairement identique à l'absence : ne pas révéler
        # l'existence d'une dictée appartenant à quelqu'un d'autre.
        raise SessionNotFound("Cette dictée n'existe plus sur le serveur.")
    return session


def list_sessions(username: str) -> List[dict]:
    """Dictées encore ouvertes de l'utilisateur, la plus récente en tête."""
    sessions = []
    for entry in sorted(os.listdir(_root())):
        try:
            session = load_session(entry, username)
        except (SessionNotFound, OSError, ValueError, KeyError):
            continue
        if session.status != "finished":
            sessions.append(session.to_public())
    sessions.sort(key=lambda item: item["created_at"], reverse=True)
    return sessions


def delete_session(session: DictationSession) -> None:
    shutil.rmtree(session.directory, ignore_errors=True)
    _forget_lock(session.id)
    logger.info("Dictée %s supprimée", session.id)


def purge_for_user(username: str) -> int:
    """Supprime les dictées encore en cours d'un usager, fichiers compris.

    Appelée à la suppression du compte (``users.delete_user``) : l'audio brut
    d'une dictée est aussi sensible qu'un enregistrement conservé, il ne doit
    rien survivre d'un compte effacé.
    """
    removed = 0
    try:
        entries = os.listdir(_root())
    except OSError:
        return 0
    for entry in entries:
        directory = os.path.join(settings.dictation_dir, entry)
        state_path = os.path.join(directory, "state.json")
        try:
            with open(state_path, encoding="utf-8") as fichier:
                data = json.load(fichier)
        except (OSError, ValueError, KeyError):
            continue
        if data.get("username") == username:
            shutil.rmtree(directory, ignore_errors=True)
            _forget_lock(entry)
            removed += 1
    if removed:
        logger.info("Dictées de %s : %d session(s) supprimée(s)", username, removed)
    return removed


def purge_expired() -> int:
    """Supprime les dictées abandonnées. Appelée au démarrage et à l'accès à
    la liste des brouillons. Rétention harmonisée sur celle des consultations
    (``consultation_retention_hours``, défaut 12 h) : une seule politique.
    ``0`` désactive la purge."""
    hours = runtime_config.value_float("consultation_retention_hours", 12.0)
    if hours <= 0:
        return 0
    limit = hours * 3600
    now = time.time()
    removed = 0
    try:
        entries = os.listdir(_root())
    except OSError:
        return 0
    for entry in entries:
        directory = os.path.join(settings.dictation_dir, entry)
        state = os.path.join(directory, "state.json")
        try:
            age = now - os.path.getmtime(state if os.path.exists(state) else directory)
        except OSError:
            continue
        if age > limit:
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("Purge des dictées : %d session(s) de plus de %g h supprimée(s)",
                    removed, hours)
    return removed


def cleanup_abandoned(username: str, db: Session) -> None:
    """
    Traite les dictées abandonnées par un onglet mort — appelée à l'ouverture
    de la liste des brouillons, pas en boucle de fond (voir main.py,
    list_consultations).

    Une session sans AUCUNE activité (fragment, ou scrutation de l'onglet qui
    enregistrait) depuis ``_STALE_AFTER`` secondes est réputée orpheline. Deux
    cas :

      * rien à conserver (moins de ``_MIN_AUDIO_SECONDS`` d'audio reçus, ou —
        pour les fournisseurs qui produisent une transcription — aucune tranche
        transcrite) : la session est supprimée, et le brouillon s'il est vide ;
      * du contenu : l'audio rejoint le brouillon comme un enregistrement
        (exactement ce qu'un « Terminer » ferait — voir main.py,
        finish_dictation), le brouillon est marqué « abandonnée » (s'il n'a
        pas déjà une note générée), la session est effacée. L'audio étant
        conservé, le médecin peut encore générer la note directement depuis le
        brouillon, y compris avec un fournisseur en audio direct.

    Rien n'est transcrit ici : la transcription d'appoint est secondaire face
    à l'audio, et la récupération doit rester discrète et sans coût.
    """
    limit = _STALE_AFTER
    now = time.time()
    archived = 0
    removed = 0
    # Le fournisseur actif contourne-t-il le STT (audio envoyé seul au modèle,
    # sans transcription) ? Dans ce cas « rien de transcrit » est l'état NORMAL
    # d'une dictée : l'absence de contenu ne se juge que sur l'audio.
    opts = llm.audio_settings(llm.active_provider())
    transcript_expected = not (opts["bypass_stt"] and not opts["keep_transcript"])
    try:
        entries = os.listdir(_root())
    except OSError:
        return
    for entry in entries:
        state_path = os.path.join(settings.dictation_dir, entry, "state.json")
        try:
            with open(state_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, KeyError):
            continue
        if data.get("username") != username or data.get("status") == "finished":
            continue
        try:
            age = now - float(data.get("updated_at", now))
        except (TypeError, ValueError):
            continue
        if age <= limit:
            continue
        try:
            session = load_session(entry, username)
        except (SessionNotFound, OSError, ValueError, KeyError):
            continue
        if session.received_seconds < _MIN_AUDIO_SECONDS or (
                transcript_expected and not (session.parts or session.offset_seconds > 0)):
            _delete_empty(username, session, db)
            removed += 1
            continue
        _archive_abandoned(session, db)
        archived += 1
    if archived or removed:
        logger.info(
            "Dictées abandonnées de %s : %d audio archivé(s), %d sans contenu supprimée(s)",
            username, archived, removed,
        )


def _delete_empty(username: str, session: DictationSession, db: Session) -> None:
    """Session sans contenu : audio effacé, et brouillon si rien à y garder."""
    delete_session(session)
    consultation = db.get(Consultation, session.consultation_id)
    if consultation is not None and consultation.owner == username and not (
            consultation.raw_transcript or consultation.generated_markdown
            or consultation.edited_markdown):
        recordings.delete_for_consultation(db, consultation.id)
        db.delete(consultation)
        db.commit()
        logger.info("Brouillon %s vide supprimé (dictée sans contenu)",
                    session.consultation_id)


def _archive_abandoned(session: DictationSession, db: Session) -> None:
    """
    Conserve l'audio d'une dictée abandonnée en le rattachant au brouillon,
    marque le brouillon « abandonnée » et efface la session. Même trajectoire
    qu'un « Terminer » explicite : une seule politique d'audio.
    """
    consultation = db.get(Consultation, session.consultation_id)
    if consultation is None or consultation.owner != session.username:
        # Orphelin (brouillon supprimé entre-temps) : l'audio n'a nulle part
        # où aller, la session est simplement effacée.
        logger.info("Dictée %s : brouillon disparu, audio non conservé",
                    session.id)
        delete_session(session)
        return
    stored = recordings.store_path(
        db, consultation, session.audio_path, session.mime_type,
        int(round(session.received_seconds)), "dictee",
    )
    # L'audio est la valeur (génération directe depuis le brouillon) : c'est
    # lui qui donne la durée réelle de l'enregistrement.
    consultation.audio_seconds = int(round(session.received_seconds))
    if consultation.status not in ("genere", "finalise", "abandonnee"):
        consultation.status = "abandonnee"
    db.commit()
    delete_session(session)
    if stored:
        live.publish(consultation.owner, "recording_added", {
            "consultation_id": consultation.id,
            "recording_id": stored.id,
            "origin_tab": "",
        })
        logger.info(
            "Dictée %s abandonnée : audio conservé avec le brouillon %s "
            "(%.1f Mo, %s s), brouillon marqué « abandonnée »",
            session.id, consultation.id, stored.size_bytes / 1048576,
            int(round(session.received_seconds)),
        )


# ---------------------------------------------------------------------------
# Réception des fragments
# ---------------------------------------------------------------------------
def append_chunk(
    session_id: str,
    username: str,
    seq: int,
    data: bytes,
    duration_hint: float = 0.0,
) -> DictationSession:
    """
    Ajoute un fragment à la suite du fichier audio.

    Le renvoi d'un fragment déjà reçu est accepté sans rien écrire : le client
    qui n'a pas vu passer notre réponse doit pouvoir réessayer sans risquer de
    dupliquer une portion de la dictée. Un fragment en avance, en revanche,
    est refusé — il laisserait un trou silencieux dans l'enregistrement.
    """
    with _lock_for(session_id):
        session = load_session(session_id, username)
        if session.status == "finished":
            raise DictationError("Cette dictée est déjà conclue.")

        if seq < session.next_seq:
            return session
        if seq > session.next_seq:
            raise SequenceMismatch(session.next_seq)

        if session.bytes_received + len(data) > settings.max_audio_bytes:
            raise DictationError(
                f"Dictée trop volumineuse (limite {settings.max_audio_mb} Mo). "
                "Terminez celle-ci et poursuivez dans une nouvelle."
            )

        with open(session.audio_path, "ab") as handle:
            handle.write(data)

        session.next_seq = seq + 1
        session.bytes_received += len(data)
        session.received_seconds += max(0.0, duration_hint)
        session.status = "recording"
        session.save()
        return session


def should_process(session: DictationSession) -> bool:
    """Assez d'audio en attente pour découper une tranche de qualité ?"""
    _, _, high = _window()
    pending = session.received_seconds - session.offset_seconds
    return pending >= high + _HEADROOM_SECONDS


# ---------------------------------------------------------------------------
# Découpage et transcription
# ---------------------------------------------------------------------------
def _phrase_hints(template_id: Optional[int]) -> str:
    if not template_id:
        return ""
    from app.database import Template as TemplateModel

    with SessionLocal() as db:
        row = db.get(TemplateModel, template_id)
        return (row.phrase_hints or "") if row else ""


def _bind_template_language(template_id: Optional[int]) -> None:
    """
    Fixe la langue du document d'après le gabarit de la dictée.

    Appelé avant chaque transcription : c'est la langue du gabarit qui décide du
    code envoyé au service vocal et de l'envoi ou non du lexique francophone.
    Sans cet appel, une dictée anglaise partirait avec le code de langue de
    l'interface — et le lexique français par-dessus.
    """
    from app import preferences
    from app.database import Template as TemplateModel

    if not template_id:
        preferences.bind_document_language(None)
        return
    with SessionLocal() as db:
        row = db.get(TemplateModel, template_id)
        preferences.bind_document_language(row.language if row else None)


def _store_part(
    session: DictationSession, text: str, moteur: tuple = ("", ""),
    duration_seconds: float = 0.0,
) -> None:
    """
    Reporte la tranche dans le brouillon — c'est lui, la copie durable. Le
    texte y est écrit dès qu'il existe, sans attendre la fin de la dictée :
    c'est ce qui fait qu'un onglet fermé ne coûte plus la consultation.

    Appelée même pour une tranche muette, afin que la durée d'audio traitée
    reste juste : elle sert de repère au médecin dans la liste des brouillons.
    """
    if text:
        session.parts.append(text)
    with _lock_for_consultation(session.consultation_id), SessionLocal() as db:
        consultation = db.get(Consultation, session.consultation_id)
        if consultation is None:
            logger.warning("Dictée %s : brouillon %s disparu",
                           session.id, session.consultation_id)
            return
        if text:
            existing = (consultation.raw_transcript or "").strip()
            consultation.raw_transcript = f"{existing} {text}".strip() if existing else text
            consultation.status = "transcrit"
        consultation.audio_seconds = int(round(session.offset_seconds))
        # Dernière tranche gagnante : changer de service en pleine dictée est
        # possible, et c'est alors celui qui a fait le plus de travail qu'on
        # veut voir — pas celui de la première tranche.
        if moteur[0]:
            consultation.stt_provider, consultation.stt_model = moteur[0], moteur[1]
            if duration_seconds > 0:
                usage.log_stt_usage(
                    db, owner=consultation.owner, consultation_id=consultation.id,
                    provider=moteur[0], model=moteur[1],
                    audio_seconds=int(round(duration_seconds)),
                )
        # Langue réellement employée pour CETTE tranche. Comme le moteur, la
        # dernière gagne : c'est celle du gabarit lié à la session, et si le
        # gabarit a changé en cours de dictée, c'est la plus récente qui décrit
        # le mieux ce que contient le texte accumulé.
        from app import preferences
        consultation.stt_language = preferences.document_language()
        consultation.updated_at = utcnow()
        db.commit()
        if text:
            live.publish(consultation.owner, "transcript", {
                "consultation_id": consultation.id,
                "session_id": session.id,
                "text": text,
                "audio_seconds": consultation.audio_seconds,
            })


def _transcribe_one(session: DictationSession, hints: str, final: bool) -> Optional[float]:
    """
    Extrait puis transcrit une tranche. Retourne sa durée, ou ``None`` s'il
    ne reste plus rien à découper.
    """
    low, target, high = _window()
    # En cours de dictée, on cherche un silence pour ne pas trancher un mot.
    # À la fin, il ne reste par construction qu'un reliquat plus court que la
    # fenêtre : on le prend entier, sans coupe donc sans risque.
    length = (
        high if final
        else find_cut_point(session.audio_path, session.offset_seconds, target, low, high)
    )

    try:
        payload = extract_segment(session.audio_path, session.offset_seconds, length)
    except TranscriptionError as exc:
        # Fin de fichier atteinte : ffmpeg ne produit plus rien.
        logger.debug("Dictée %s : plus de tranche extractible (%s)", session.id, exc)
        return None

    if payload.duration_seconds < _MIN_SEGMENT_SECONDS:
        return None

    result = transcribe_payload(payload, hints)
    session.offset_seconds += payload.duration_seconds

    text = (result.get("transcript") or "").strip()
    if not text:
        # Tranche muette : le curseur avance quand même, sinon la boucle
        # repasserait indéfiniment sur le même silence.
        logger.info("Dictée %s : tranche de %.1f s sans parole",
                    session.id, payload.duration_seconds)
    _store_part(session, text,
                (result.get("provider") or "", result.get("model") or ""),
                duration_seconds=payload.duration_seconds)

    session.save()
    logger.info(
        "Dictée %s : tranche de %.1f s transcrite (%d caractères, curseur %.1f s)",
        session.id, payload.duration_seconds, len(text), session.offset_seconds,
    )
    return payload.duration_seconds


def process_pending(session_id: str, username: str, final: bool = False) -> DictationSession:
    """
    Découpe et transcrit tout ce qui peut l'être.

    Appelée en tâche de fond après réception d'un fragment, et une dernière
    fois — avec ``final`` — quand le médecin appuie sur « Terminer ».
    """
    with _lock_for(session_id):
        session = load_session(session_id, username)

        # STT contourné pour ce fournisseur (audio envoyé seul à la
        # génération) : ni transcription ni repli ``_finalise``, on se
        # contente de faire progresser le statut de la session. L'audio brut
        # est déjà sur disque (voir ``append_chunk``), c'est tout ce dont la
        # génération aura besoin.
        opts = llm.audio_settings(llm.active_provider())
        if opts["bypass_stt"] and not opts["keep_transcript"]:
            if final:
                session.status = "finished"
                session.save()
            return session

        _bind_template_language(session.template_id)
        hints = _phrase_hints(session.template_id)
        _, _, high = _window()

        while True:
            if not final and not should_process(session):
                break
            try:
                duration = _transcribe_one(session, hints, final)
            except TranscriptionError as exc:
                session.last_error = str(exc)
                session.save()
                logger.warning("Dictée %s : transcription refusée — %s", session.id, exc)
                raise
            if duration is None:
                break
            # Une tranche plus courte que demandé signifie qu'on a atteint la
            # fin du fichier : inutile de refaire un tour pour rien.
            if final and duration < high - 0.5:
                break

        if final:
            _finalise(session)
        return session


def _finalise(session: DictationSession) -> None:
    """Filet de sécurité : rien n'a été transcrit, on retente en un bloc."""
    if session.parts or session.offset_seconds > 0:
        session.status = "finished"
        session.save()
        return

    if session.bytes_received < 2000:
        session.status = "finished"
        session.last_error = "Enregistrement trop court ou silencieux."
        session.save()
        return

    logger.warning(
        "Dictée %s : le découpage n'a rien produit, envoi de l'enregistrement complet",
        session.id,
    )
    with open(session.audio_path, "rb") as handle:
        raw = handle.read()
    _bind_template_language(session.template_id)
    result = transcribe(raw, session.mime_type, _phrase_hints(session.template_id))
    session.offset_seconds = float(result.get("duration_seconds") or 0)
    text = (result.get("transcript") or "").strip()
    if text:
        _store_part(session, text,
                    (result.get("provider") or "", result.get("model") or ""),
                    duration_seconds=session.offset_seconds)
    session.status = "finished"
    session.save()
