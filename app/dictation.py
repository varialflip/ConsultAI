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

from app import audio_cache, live, llm, med_grounding, recordings, runtime_config, stt, usage
from app.config import settings
from app.database import Consultation, SessionLocal, utcnow
from sqlalchemy.orm import Session
from app.stt import (
    _MISTRAL_REALTIME_MODEL_DEFAUT,
    MistralRealtimeTranscription,
    TranscriptionError,
    _decode_pcm16,
    detect_speech_ranges,
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
# Temps réel de la dictée (mode « vad » / « sse »)
# ---------------------------------------------------------------------------
#: Longueur minimale d'un énoncé avant qu'une coupe au silence ne soit
#: tentée. En deçà, on ne peut pas distinguer un vrai énoncé d'un bruit de
#: bouche : la fenêtre de recherche de ``find_cut_point`` commence ici.
_FLUSH_MIN = 1.5

#: En deçà de cette quantité d'audio EN ATTENTE, la fin d'énoncé signalée par
#: le navigateur ne déclenche rien : il faut de la matière à transcrire.
_FLUSH_MIN_PENDING = 2.0

#: Tolérance de chevauchement couverture/parole du filet de fin : une région
#: est réputée couverte quand elle est transcrite à moins de cette marge.
#: Absorbe les écarts de mesure (ffprobe vs silencedetect) sans laisser de
#: blancs audibles.
_SWEEP_OVERLAP_SECONDS = 0.3

#: En deçà, une région non couverte ne mérite pas un appel de plus — elle
#: tomberait de toute façon sous ``_MIN_SPEECH_SECONDS`` de ``stt``.
_SWEEP_MIN_REGION = 0.7


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
    #: Couverture de l'audio par les transcriptions réussies, dans l'horloge
    #: MESURÉE : intervalles [début, fin] du fichier brut déjà transcrits.
    #: Sert au filet de fin (``_sweep_uncovered``) à retrouver les trous
    #: laissés par un VAD trop strict ou une tranche échouée.
    covered_ranges: List[Tuple[float, float]] = field(default_factory=list)
    #: Un énoncé vient de se terminer côté navigateur (signal VAD) : la
    #: prochaine passe découpe et transcrit immédiatement, sans attendre le
    #: cadencement batch.
    flush_requested: bool = False
    #: Compteur des énoncés transcrits en streaming : chaque énoncé porte un
    #: identifiant, pour que la ligne provisoire des onglets puisse être
    #: retirée au commit.
    utterance_seq: int = 0

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
            "covered_ranges": list(self.covered_ranges),
            "flush_requested": self.flush_requested,
            "utterance_seq": self.utterance_seq,
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
        covered_ranges=[
            (float(a), float(b)) for a, b in data.get("covered_ranges", [])
        ],
        flush_requested=bool(data.get("flush_requested", False)),
        utterance_seq=int(data.get("utterance_seq", 0)),
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
    _forget_realtime(session.id)
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
            _forget_realtime(entry)
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
            _forget_realtime(entry)
            removed += 1
    if removed:
        logger.info("Purge des dictées : %d session(s) de plus de %g h supprimée(s)",
                    removed, hours)
    return removed


def cleanup_abandoned(username: str, db: Session, origin_tab: str = "") -> None:
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
        _archive_abandoned(session, db, origin_tab)
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


def _archive_abandoned(
    session: DictationSession, db: Session, origin_tab: str = "",
) -> None:
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
            "origin_tab": origin_tab,
        })
        logger.info(
            "Dictée %s abandonnée : audio conservé avec le brouillon %s "
            "(%.1f Mo, %s s), brouillon marqué « abandonnée »",
            session.id, consultation.id, stored.size_bytes / 1048576,
            int(round(session.received_seconds)),
        )
    # Les autres onglets/appareils ouverts sont prévenus en direct : un
    # brouillon abandonné vient d'apparaître dans la liste.
    live.publish(consultation.owner, "consultation_abandoned", {
        "consultation_id": consultation.id,
        "title": consultation.title,
        "origin_tab": origin_tab,
    })


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


# ===========================================================================
# Points de contrôle de l'audio préparé (pour la génération)
# ===========================================================================
#
# L'audio joint au modèle de langage est plafonné (silences) puis encodé :
# ~0,9× le temps réel de l'audio (mesuré 2026-08-26 — 4,2 s pour 5 min,
# 32 s pour 35 min). Payés AU CLIC « Mettre en forme », ces secondes
# retardaient les premiers mots de la note.
#
# Pendant la dictée, une passe bornée (-to) reconstruit régulièrement un
# « checkpoint » : l'audio préparé de [0, couvert]. À la conclusion, il ne
# reste qu'à préparer la queue (seek d'entrée jamais tardif + atrim exacte,
# cf. stt.first_packet_after) et concaténer sans réencoder — l'artefact
# complet est prêt en ~1 s même après une longue dictée.
#
# Tout échec dégrade vers la voie historique : passe complète au moment de
# la conclusion (finish_audio_artifact), puis préparation à la demande à la
# génération (audio_cache). Rien de ce qui suit ne peut faire échouer une
# dictée ni appauvrir l'audio envoyé au modèle.

#: Intervalle minimum entre deux points de contrôle (audio neuf requis).
_CHECKPOINT_INTERVAL_S = 60.0

#: En deçà de cette durée reçue, aucun point de contrôle : la passe complète
#: de conclusion est déjà négligeable, inutile dépenser du CPU.
_CHECKPOINT_MIN_SECONDS = 45.0

#: Marge de recul du seek de queue devant la frontière du point de contrôle :
#: absorbe la granularité des clusters WebM (jamais après la cible, jusqu'à
#: ~2 s avant — l'excédent est retranché par ``atrim`` à l'échantillon près).
_TAIL_SEEK_MARGIN_S = 2.5

#: Attente d'un point de contrôle encore en course à la conclusion.
_CHECKPOINT_WAIT_S = 45.0

_checkpoint_lock = threading.Lock()
_checkpoints_en_course: set = set()


def _checkpoint_meta_path(session: DictationSession) -> str:
    return os.path.join(session.directory, "checkpoint.json")


def _read_checkpoint(session: DictationSession) -> Optional[dict]:
    """État du point de contrôle existant, ou ``None``."""
    try:
        with open(_checkpoint_meta_path(session), encoding="utf-8") as handle:
            infos = json.load(handle)
    except (OSError, ValueError):
        return None
    media = os.path.join(
        session.directory,
        "checkpoint." + str(infos.get("fmt") or "ogg").strip().lower(),
    )
    if not os.path.exists(media) or not infos.get("covered"):
        return None
    infos["media"] = media
    return infos


def _write_ready_pair(session: DictationSession, fmt: str,
                      content: bytes, mime: str, duration: float) -> Optional[str]:
    """Écrit l'artefact complet de la dictée (« ready.* » + méta), atomique."""
    ext = {"ogg": ".ogg", "mp3": ".mp3", "wav": ".wav"}.get(fmt, ".ogg")
    media = os.path.join(session.directory, "ready" + ext)
    meta = os.path.join(session.directory, "ready.json")
    tmp_media, tmp_meta = media + ".tmp", meta + ".tmp"
    try:
        with open(tmp_media, "wb") as handle:
            handle.write(content)
        with open(tmp_meta, "w", encoding="utf-8") as handle:
            json.dump({"mime": mime, "duration": duration,
                       "mode": audio_cache.mode_signature(fmt)}, handle)
        os.replace(tmp_media, media)
        # La méta en dernier : sa présence vaut engagement de validité.
        os.replace(tmp_meta, meta)
        return media
    except OSError as exc:
        logger.warning("Dictée %s : artefact audio non écrit — %s", session.id, exc)
        return None


def maybe_schedule_checkpoint(session_id: str, username: str) -> bool:
    """
    Lance si besoin une passe de point de contrôle après un fragment reçu.

    Cadencée par l'audio NOUVEAU depuis le dernier contrôle (une dictée en
    pauses longues ne déclenche rien) ; une seule course par session. Les
    exceptions sont volontairement muettes : c'est une optimisation pure.
    """
    try:
        session = load_session(session_id, username)
    except DictationError:
        return False
    if session.status != "recording":
        return False
    if session.received_seconds < _CHECKPOINT_MIN_SECONDS:
        return False
    deja_couvert = (_read_checkpoint(session) or {}).get("covered") or 0.0
    if session.received_seconds - float(deja_couvert) < _CHECKPOINT_INTERVAL_S:
        return False

    fmt = llm.audio_settings(llm.active_provider())["send_audio_format"]
    with _checkpoint_lock:
        if session_id in _checkpoints_en_course:
            return False
        _checkpoints_en_course.add(session_id)
    threading.Thread(
        target=_run_checkpoint,
        args=(session_id, username, fmt),
        daemon=True,
        name=f"audio-checkpoint-{session_id}",
    ).start()
    return True


def _run_checkpoint(session_id: str, username: str, fmt: str) -> None:
    """Passe bornée [-to reçu] sur le brut → checkpoint.* dans la session."""
    try:
        session = load_session(session_id, username)
        if session.status != "recording":
            return
        fin = max(0.0, float(session.received_seconds))
        result = stt.trim_segment(session.audio_path, fmt, end_seconds=fin)
        if result is None:
            return
        content, mime, duree = result
        ext = {"ogg": ".ogg", "mp3": ".mp3", "wav": ".wav"}.get(fmt, ".ogg")
        media_tmp = os.path.join(session.directory, "checkpoint.tmp")
        media = os.path.join(session.directory, "checkpoint." + fmt.strip().lower())
        meta = _checkpoint_meta_path(session)
        with open(media_tmp, "wb") as handle:
            handle.write(content)
        os.replace(media_tmp, media)
        meta_tmp = meta + ".tmp"
        with open(meta_tmp, "w", encoding="utf-8") as handle:
            json.dump({"mode": audio_cache.mode_signature(fmt), "fmt": fmt,
                       "covered": fin, "mime": mime, "duration": duree}, handle)
        # La méta en dernier : sa présence engage la validité de la paire.
        os.replace(meta_tmp, meta)
        logger.info(
            "Dictée %s : point de contrôle audio à %.0f s (%.1f Mo)",
            session_id, fin, len(content) / 1048576,
        )
    except Exception:  # tâche de fond : jamais fatale, toujours visible
        logger.exception("Point de contrôle audio impossible (dictée %s)", session_id)
    finally:
        with _checkpoint_lock:
            _checkpoints_en_course.discard(session_id)


def finish_audio_artifact(session_id: str, username: str) -> Optional[str]:
    """
    Produit l'artefact audio COMPLET de la dictée dans son dossier
    (« ready.<ext> », méta « ready.json ») ; renvoie le chemin du média ou
    ``None``.

    Point de contrôle exploitable → queue préparée puis concaténation sans
    réencodage (~1 s même en dictée longue) ; sinon passe complète historique
    (plafonnement, repli transcodage). Appelé APRÈS le dernier traitement de
    transcription, AVANT le déplacement du brut (store_path) — c'est pourquoi
    l'appel est bloquant : il sérialise avec le move.
    """
    session = load_session(session_id, username)

    # Un contrôle encore en course écrit sous nous : attendre borné.
    deadline = time.monotonic() + _CHECKPOINT_WAIT_S
    while True:
        with _checkpoint_lock:
            if session_id not in _checkpoints_en_course:
                break
        if time.monotonic() >= deadline:
            logger.warning("Dictée %s : point de contrôle toujours en course, ignoré", session_id)
            break
        time.sleep(0.25)

    fmt = llm.audio_settings(llm.active_provider())["send_audio_format"]
    mode = audio_cache.mode_signature(fmt)
    brut = session.audio_path
    t0 = time.monotonic()
    resultat: Optional[Tuple[bytes, str, float]] = None

    controle = _read_checkpoint(session)
    if controle and controle.get("mode") == mode and controle.get("fmt") == fmt \
            and os.path.exists(brut):
        couvert = float(controle["covered"])
        ecart = max(0.0, float(session.received_seconds)) - couvert
        if ecart > 2 * _CHECKPOINT_INTERVAL_S:
            # Contrôle trop en course retard (passe longue interrompue,
            # machine chargée) : la queue coûterait presque une passe
            # complète — on rend la main et la préparation partira en tâche
            # de fond (start_build côté conclusion), sans bloquer « Terminer ».
            logger.info(
                "Dictée %s : point de contrôle en retard de %.0f s, "
                "préparation complète déléguée en tâche de fond",
                session_id, ecart,
            )
            return None
        seek = max(0.0, couvert - _TAIL_SEEK_MARGIN_S)
        premier = stt.first_packet_after(brut, seek)
        if premier is None or premier > couvert:
            # Seek incertain : voie complète, sans risque.
            logger.info("Dictée %s : seek de queue incertain, passe complète", session_id)
        else:
            retrancher = max(0.0, couvert - premier)
            queue = stt.trim_segment(
                brut, fmt, seek_seconds=seek, cut_relative_seconds=retrancher,
            )
            if queue is not None:
                contenu_q, _mime_q, _duree_q = queue
                travail = os.path.join(session.directory, "queue.tmp")
                with open(travail, "wb") as handle:
                    handle.write(contenu_q)
                try:
                    resultat = stt.concat_copies([controle["media"], travail], fmt)
                finally:
                    try:
                        os.remove(travail)
                    except OSError:
                        pass
            # ``queue`` indéterminable (échec ffmpeg ou silence pur) : on ne
            # tente JAMAIS de servir le point de contrôle seul — une vraie
            # parole manquante serait une perte clinique. Voie complète.

    if resultat is None:
        if not os.path.exists(brut):
            return None
        if float(session.received_seconds) > (
            2 * _CHECKPOINT_INTERVAL_S + _CHECKPOINT_MIN_SECONDS
        ):
            # Passe complète trop chère pour bloquer « Terminer » : la
            # conclusion déléguera à start_build (tâche de fond), et la
            # génération attendra borné ou retombera sur sa voie classique.
            logger.info(
                "Dictée %s : préparation complète déléguée en tâche de fond",
                session_id,
            )
            return None
        # Passe complète historique — plafonnement, repli transcodage tel quel.
        resultat = stt.cap_silence_to(brut, fmt)
        if resultat is None:
            resultat = stt.transcode_to(brut, fmt)
    if resultat is None:
        logger.info("Dictée %s : pas d'artefact audio (vide ou échec)", session_id)
        return None

    contenu, mime, duree = resultat
    chemin = _write_ready_pair(session, fmt, contenu, mime, duree)
    if chemin:
        logger.info(
            "Dictée %s : artefact audio prêt en %.1f s (%.1f Mo, %.1f s)",
            session_id, time.monotonic() - t0, len(contenu) / 1048576, duree,
        )
    return chemin


def should_process(session: DictationSession) -> bool:
    """Assez d'audio en attente pour découper une tranche de qualité ?"""
    _, _, high = _window()
    pending = session.received_seconds - session.offset_seconds
    return pending >= high + _HEADROOM_SECONDS


def realtime_mode() -> str:
    """
    Mode temps réel EFFECTIF de la dictée (``off`` | ``vad`` | ``sse``).

    La valeur du panneau est validée contre le fournisseur actif : un mode
    inapplicable retombe silencieusement sur ``off`` plutôt que de casser la
    dictée.

      * ``sse`` n'a de sens qu'avec Mistral (le streaming est un contrat
        Voxtral) ;
      * ``vad`` (énoncé-granularité) est incompatible avec Cohere, plafonné à
        5 requêtes/minute : une dictée hachée épuiserait le quota et le
        texte en direct serait systématiquement retardé par l'étalement.
    """
    mode = runtime_config.value("stt_realtime_mode")
    provider = runtime_config.value("stt_provider")
    if mode == "vad" and provider == "cohere":
        return "off"
    if mode == "sse" and provider != "mistral":
        return "off"
    return mode


def should_flush(session: DictationSession) -> bool:
    """Assez d'audio en attente pour traiter immédiatement la fin d'énoncé ?"""
    pending = session.received_seconds - session.offset_seconds
    return pending >= _FLUSH_MIN_PENDING


def request_flush(session_id: str, username: str) -> DictationSession:
    """
    Le navigateur signale qu'un énoncé vient de se terminer (VAD client).

    Pose le drapeau ``flush_requested`` : la prochaine passe découpera et
    transcrira immédiatement, en coupant au premier silence exploitable —
    c'est ce qui fait apparaître le texte quelques secondes après chaque
    pause au lieu d'attendre le cadencement batch. Le drapeau est un simple
    signal, jamais un repère de coupe : la frontière reste ffmpeg
    (``find_cut_point``), qui travaille dans l'horloge mesurée du fichier.
    """
    with _lock_for(session_id):
        session = load_session(session_id, username)
        if session.status == "finished":
            raise DictationError("Cette dictée est déjà conclue.")
        if realtime_mode() == "off":
            # Le mode a été désactivé (ou est inapplicable au fournisseur
            # actif) depuis le début de la dictée : le signal n'a plus d'objet.
            return session
        session.flush_requested = True
        session.save()
        return session


def _session_owner(session: DictationSession) -> str:
    """Nom du propriétaire du brouillon — l'adresse de diffusion en direct."""
    with SessionLocal() as db:
        row = db.get(Consultation, session.consultation_id)
        return row.owner if row is not None else session.username


# ---------------------------------------------------------------------------
# Canal temps réel Mistral persistant (mode « sse »)
# ---------------------------------------------------------------------------
# Une session WebSocket par dictée, conservée ouverte pour que le modèle de
# streaming garde le contexte des énoncés successifs (voir
# stt.MistralRealtimeTranscription). Fermée et retirée dès que la dictée se
# conclut, s'abandonne ou est purgée. Comme les verrous du module, le registre
# vit en mémoire : cela ne fonctionne QUE parce que ConsultAI tourne en un
# seul worker uvicorn.
_realtime_sessions: Dict[str, MistralRealtimeTranscription] = {}
_realtime_guard = threading.Lock()


def _realtime_session(session: DictationSession) -> MistralRealtimeTranscription:
    """Canal temps réel PERSISTANT de cette dictée (créé à la demande)."""
    api_key = runtime_config.value("mistral_api_key")
    model = runtime_config.value("mistral_realtime_model") or _MISTRAL_REALTIME_MODEL_DEFAUT
    boucle = live.event_loop()
    with _realtime_guard:
        inst = _realtime_sessions.get(session.id)
        if inst is None or inst.is_closed:
            inst = MistralRealtimeTranscription(model, api_key, boucle)
            _realtime_sessions[session.id] = inst
        return inst


def _forget_realtime(session_id: str) -> None:
    """Ferme et retire le canal temps réel d'une dictée (fin, abandon, purge)."""
    with _realtime_guard:
        inst = _realtime_sessions.pop(session_id, None)
    if inst is not None:
        inst.close()


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
        if text:
            maybe_schedule_grounding(session.id, session.username)


# ---------------------------------------------------------------------------
# Grounding — stabilisation des frontières + liste de médicaments
# ---------------------------------------------------------------------------
# La transcription par tranches ~10 s coupe parfois un mot à la jointure de
# deux segments (aucun silence à portée de la fenêtre). Quand la correction
# des médicaments est active, chaque frontière est ré-écoutée en continu
# (petit délai « par l'arrière », compatible Cohere custom auto-hébergé) puis
# le texte des deux parts concernées est remplacé. Le nom de médicaments
# déformé est ensuite normalisé (moteur déterministe, base BDP) et la liste
# pointée mise à jour (SSE ``med_grounding`` / ``med_grounding_result``).

#: Intervalle minimal entre deux passages de stabilisation d'une même dictée.
_GROUNDING_GAP = 4.0
_GROUNDING_MEDS_CAP = 3
_grounding_lock = threading.Lock()
_grounding_course: set = set()
_grounding_cooldown: Dict[str, float] = {}
_grounding_upto: Dict[str, int] = {}
_grounding_meds: Dict[str, list] = {}


def _grounding_enabled() -> bool:
    try:
        if not med_grounding.is_available():
            return False
        return runtime_config.value("dictation_grounding") != "false"
    except Exception:
        return False


def _norm_spaces(s: str) -> str:
    """Comparateur de contenu insensible à la casse/aux blancs multiples."""
    return " ".join((s or "").lower().split())


def maybe_schedule_grounding(session_id: str, username: str) -> None:
    """Arme (si besoin) la stabilisation d'une frontière de la dictée.

    Nécessite le réglage, une session en cours, et le respect d'un faible
    intervalle entre deux passages (endpoint custom auto-hébergé : aucune
    limite de taux cloud, on garde juste une marge anti-boucle).
    """
    if not _grounding_enabled():
        return
    try:
        session = load_session(session_id, username)
    except DictationError:
        return
    if session.status != "recording":
        return
    if len(session.parts) < 2:
        return
    now = time.time()
    with _grounding_lock:
        if session_id in _grounding_course:
            return
        if now - _grounding_cooldown.get(session_id, 0.0) < _GROUNDING_GAP:
            return
        _grounding_course.add(session_id)
        _grounding_cooldown[session_id] = now
    threading.Thread(
        target=_run_grounding,
        args=(session_id, username),
        daemon=True,
        name=f"grounding-{session_id}",
    ).start()


def _run_grounding(session_id: str, username: str) -> None:
    """Réécoute la frontière non encore stabilisée et remplace en place."""
    try:
        with _lock_for(session_id):
            session = load_session(session_id, username)
            if session.status != "recording":
                return
            n = len(session.parts)
            if n < 2:
                return
            a = _grounding_upto.get(session_id, -1) + 1   # 1re frontière instable
            if a < 0:
                a = 0
            if a >= n - 1:
                return
            b = a + 1
            session = _rewrite_boundary(session, a, b)
            if session is None:
                return
            _grounding_upto[session_id] = b
            _merge_and_publish_meds(session)
    except Exception:
        logger.exception("Grounding impossible (dictée %s)", session_id)
    finally:
        with _grounding_lock:
            _grounding_course.discard(session_id)


def _rewrite_boundary(session: DictationSession, a: int, b: int) -> DictationSession | None:
    """Ré-transcrit [start_a, end_b[ d'un bloc et remplace les parts a..b.

    Retourne la session à jour (None si rien à changer). Ne touche JAMAIS aux
    parts déjà stabilisées ; si le contenu réécouté est identique, on marque
    simplement la frontière stable (aucun SSE, aucune mutation).
    """
    if len(session.covered_ranges) < b + 1:
        return None
    start = session.covered_ranges[a][0]
    end = session.covered_ranges[b][1]
    if end - start <= 0.05:
        return None
    hints = _phrase_hints(session.template_id)
    low, target, high = _window()
    nouveaux: List[str] = []
    curseur = start
    while end - curseur > 0.05:
        restant = end - curseur
        longueur = restant if restant <= high else find_cut_point(
            session.audio_path, curseur, target, low, high)
        try:
            payload = extract_segment(session.audio_path, curseur, longueur)
        except TranscriptionError:
            break
        if payload.duration_seconds < _MIN_SEGMENT_SECONDS:
            break
        curseur += payload.duration_seconds
        try:
            result = transcribe_payload(payload, hints)
        except TranscriptionError:
            continue
        texte = (result.get("transcript") or "").strip()
        if texte:
            nouveaux.append(texte)
        if texte:
            logger.info(
                "Dictée %s : stabilisation [%.1f-%.1f s] (%d caractères)",
                session.id, curseur - payload.duration_seconds, curseur, len(texte),
            )
    if not nouveaux:
        return None

    ancien = _norm_spaces(" ".join(session.parts[a:b + 1]))
    nouveau = _norm_spaces(" ".join(nouveaux))
    if ancien and ancien == nouveau:
        return session                     # déjà stable, rien à faire

    session.parts[a:b + 1] = nouveaux
    session.covered_ranges[a:b + 1] = [(start, end if curseur > start else end)]
    session.save()

    # Persistance durable du transcript remplacé.
    owner = _session_owner(session)
    with _lock_for_consultation(session.consultation_id), SessionLocal() as db:
        consultation = db.get(Consultation, session.consultation_id)
        if consultation is not None:
            consultation.raw_transcript = " ".join(session.parts).strip()
            consultation.updated_at = utcnow()
            db.commit()

    live.publish(owner, "transcript_correct", {
        "consultation_id": session.consultation_id,
        "session_id": session.id,
        "parts": list(session.parts),
    })
    return session


def _merge_and_publish_meds(session: DictationSession) -> None:
    """Met à jour la liste pointée (moteur déterministe) et la diffuse.

    Calcule sur la QUEUE du transcript (derniers ~600 mots) puis fusionne dans
    l'accumulateur de la session (clé = nom normalisé, première posologie
    non vide conservée) : pas de re-normalisation complète à chaque frontière,
    ce qui garde la passe compatible même avec une dictée de 30 min.
    """
    owner = _session_owner(session)
    texte = " ".join(session.parts)
    tail = texte[-3000:].lstrip()              # ≈ derniers ~600 mots
    nouveaux = med_grounding.extract_med_items(tail)
    accum = _grounding_meds.get(session.id, [])
    par_cle = {med_grounding.norm_phon(i["name"]): i for i in accum}
    for item in nouveaux:
        cle = med_grounding.norm_phon(item["name"])
        if cle in par_cle:
            if not par_cle[cle].get("posology") and item.get("posology"):
                par_cle[cle]["posology"] = item["posology"]
        else:
            par_cle[cle] = dict(item)
    merged = list(par_cle.values())
    _grounding_meds[session.id] = merged
    live.publish(owner, "med_grounding", {
        "consultation_id": session.consultation_id,
        "session_id": session.id,
        "index": len(session.parts),
        "items": merged,
    })


def schedule_final_grounding(session_id: str, username: str) -> None:
    """Lance (tâche de fond) la liste de médicaments DÉFINITIVE d'une dictée.

    Exécutée au « Terminer », une seule fois, sur le transcript complet : le
    résultat est persisté dans ``consultation.med_grounding_json`` et diffusé
    via ``med_grounding_result``. Ne bloque pas la clôture de la dictée.
    """
    if not _grounding_enabled():
        return
    threading.Thread(
        target=_finalize_grounding,
        args=(session_id, username),
        daemon=True,
        name=f"grounding-final-{session_id}",
    ).start()


def _finalize_grounding(session_id: str, username: str) -> None:
    try:
        with _lock_for(session_id):
            session = load_session(session_id, username)
            owner = _session_owner(session)
            if session.status not in ("recording", "finished"):
                return
            text = " ".join(session.parts).strip()
            if not text:
                return
            items = med_grounding.extract_med_items(text)
            _grounding_meds[session_id] = items
            with _lock_for_consultation(session.consultation_id), SessionLocal() as db:
                consultation = db.get(Consultation, session.consultation_id)
                if consultation is not None:
                    consultation.med_grounding_json = json.dumps(items, ensure_ascii=False)
                    db.commit()
            live.publish(owner, "med_grounding_result", {
                "consultation_id": session.consultation_id,
                "session_id": session.id,
                "items": items,
            })
    except Exception:
        logger.exception("Grounding final impossible (dictée %s)", session_id)
    finally:
        with _grounding_lock:
            _grounding_course.discard(session_id)
            _grounding_upto.pop(session_id, None)
            _grounding_meds.pop(session_id, None)


def _transcribe_one(session: DictationSession, hints: str, final: bool,
                    flush: bool = False) -> Optional[float]:
    """
    Extrait puis transcrit une tranche. Retourne sa durée, ou ``None`` s'il
    ne reste plus rien à découper.

    ``flush`` (fin d'énoncé signalée par le VAD du navigateur) coupe au
    PREMIER silence exploitable après un minimum de parole, plutôt qu'au
    silence le plus proche de la durée cible : c'est la pause du locuteur qui
    dicte la coupe.
    """
    low, target, high = _window()
    # En cours de dictée, on cherche un silence pour ne pas trancher un mot.
    # À la fin, il ne reste par construction qu'un reliquat plus court que la
    # fenêtre : on le prend entier, sans coupe donc sans risque.
    if final:
        length = high
    elif flush:
        length = find_cut_point(session.audio_path, session.offset_seconds,
                                _FLUSH_MIN, _FLUSH_MIN, high)
    else:
        length = find_cut_point(session.audio_path, session.offset_seconds, target, low, high)

    try:
        payload = extract_segment(session.audio_path, session.offset_seconds, length)
    except TranscriptionError as exc:
        # Fin de fichier atteinte : ffmpeg ne produit plus rien.
        logger.debug("Dictée %s : plus de tranche extractible (%s)", session.id, exc)
        return None

    if payload.duration_seconds < _MIN_SEGMENT_SECONDS:
        return None

    if realtime_mode() == "sse" and flush:
        result = _transcribe_one_sse(session, payload, hints)
    else:
        result = transcribe_payload(payload, hints)
    session.offset_seconds += payload.duration_seconds

    text = (result.get("transcript") or "").strip()
    if text:
        # Couverture : cette plage est transcrite — le filet de fin n'y
        # repassera pas. Une tranche muette ne laisse rien à rattraper (elle
        # n'apparaît de toute façon pas dans les régions de parole).
        session.covered_ranges.append(
            (session.offset_seconds - payload.duration_seconds, session.offset_seconds)
        )
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


def _transcribe_one_sse(session: DictationSession, payload, hints: str) -> dict:
    """
    Transcrit un énoncé via le canal temps réel Mistral PERSISTANT de la
    dictée, en publiant les deltas.

    La session WebSocket est ouverte au premier énoncé et conservée tant que
    la dictée dure (``_realtime_session``) : c'est elle qui permet au modèle
    de streaming de garder le contexte des énoncés précédents. Les deltas
    vont à tous les onglets (``transcript_delta``) pour composer la ligne
    provisoire ; ``transcript_final`` la retire au commit, qui suit son chemin
    habituel (``_store_part``). Si le canal est mort (réseau, session expirée),
    on retombe sur la transcription batch du même énoncé — la dictée ne perd
    jamais la parole.
    """
    uid = session.utterance_seq
    session.utterance_seq += 1
    owner = _session_owner(session)

    def on_delta(delta: str, full: str) -> None:
        live.publish(owner, "transcript_delta", {
            "consultation_id": session.consultation_id,
            "session_id": session.id,
            "utterance_id": uid,
            "delta": delta,
            "text": full,
        })

    try:
        pcm = _decode_pcm16(payload.content)
        if not pcm:
            raise TranscriptionError("Énoncé vide après transcodage PCM.")
        texte = _realtime_session(session).transcribe(pcm, on_delta)
        moteur = ("mistral",
                  runtime_config.value("mistral_realtime_model") or _MISTRAL_REALTIME_MODEL_DEFAUT)
    except TranscriptionError as exc:
        # Canal temps réel indisponible (session morte, réseau…) : on retombe
        # sur la transcription batch du même énoncé, et on jette la session
        # morte — le prochain énoncé en ouvrira une fraîche. Le texte arrive
        # alors d'un bloc, sans ligne provisoire.
        logger.warning("Dictée %s : temps réel Mistral indisponible, repli batch — %s",
                       session.id, exc)
        _forget_realtime(session.id)
        try:
            resultat = transcribe_payload(payload, hints)
        except TranscriptionError:
            raise
        texte = (resultat.get("transcript") or "").strip()
        moteur = (resultat.get("provider") or "mistral",
                  resultat.get("model") or _MISTRAL_REALTIME_MODEL_DEFAUT)
    finally:
        # Toujours publié, même sur échec : une ligne provisoire ne doit pas
        # survivre à l'énoncé qui l'a produite.
        live.publish(owner, "transcript_final", {
            "consultation_id": session.consultation_id,
            "session_id": session.id,
            "utterance_id": uid,
        })
    return {
        "transcript": texte,
        "provider": moteur[0],
        "model": moteur[1],
    }


def _subtract_ranges(base, cuts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Retire les plages ``cuts`` de ``base`` (intervalles [début, fin])."""
    result: List[Tuple[float, float]] = []
    for start, end in base:
        curseur = start
        for c0, c1 in cuts:
            if c1 <= curseur:
                continue
            if c0 >= end:
                break
            if c0 > curseur:
                result.append((curseur, min(c0, end)))
            curseur = max(curseur, c1)
            if curseur >= end:
                break
        if curseur < end:
            result.append((curseur, end))
    return result


def _sweep_uncovered(session: DictationSession, hints: str) -> None:
    """
    Filet de fin : re-transcrit les zones de parole non couvertes.

    Au « Terminer », on re-parcourt le fichier brut avec silencedetect
    (détection SERVEUR, indépendante du VAD du navigateur) et on compare aux
    plages déjà transcrites (``covered_ranges``). Tout trou — énoncé que le
    VAD a manqué, tranche qui avait échoué — est re-extraite et re-transcrite.

    L'audio brut est resté complet tout du long : c'est ce qui rend cette
    reprise possible sans avoir rien gardé d'autre. Une région qui échoue est
    sautée (texte partiel conservé), comme la retranscription écarte un
    enregistrement muet.
    """
    regions = detect_speech_ranges(session.audio_path)
    if not regions:
        return
    # Les plages couvertes sont élargies de la tolérance : une couverture à
    # quelques centièmes de seconde près ne doit pas créer de faux trou.
    couvert = [
        (c0 - _SWEEP_OVERLAP_SECONDS, c1 + _SWEEP_OVERLAP_SECONDS)
        for c0, c1 in session.covered_ranges
    ]
    trous = _subtract_ranges(regions, couvert)
    for start, end in trous:
        if end - start < _SWEEP_MIN_REGION:
            continue
        logger.info(
            "Dictée %s : trou de %.1f s détecté en fin (%.1f-%.1f s), re-transcription",
            session.id, end - start, start, end,
        )
        _transcribe_region(session, start, end, hints)


def _transcribe_region(session: DictationSession, start: float, end: float, hints: str) -> None:
    """Transcrit l'intervalle [start, end[ du fichier brut, en découpant si
    nécessaire (un trou long repasse par les coupes au silence, comme le
    découpage en cours de dictée)."""
    low, target, high = _window()
    curseur = start
    while end - curseur > 0.05:
        restant = end - curseur
        # Dernier morceau : entier, sans coupe donc sans risque.
        longueur = (
            restant if restant <= high
            else find_cut_point(session.audio_path, curseur, target, low, high)
        )
        try:
            payload = extract_segment(session.audio_path, curseur, longueur)
        except TranscriptionError:
            break
        if payload.duration_seconds < _MIN_SEGMENT_SECONDS:
            break
        curseur += payload.duration_seconds
        try:
            result = transcribe_payload(payload, hints)
        except TranscriptionError as exc:
            logger.warning(
                "Dictée %s : trou [%.1f-%.1f s] écarté — %s",
                session.id, curseur - payload.duration_seconds, curseur, exc,
            )
            continue
        text = (result.get("transcript") or "").strip()
        if text:
            session.covered_ranges.append(
                (curseur - payload.duration_seconds, curseur)
            )
        _store_part(session, text,
                    (result.get("provider") or "", result.get("model") or ""),
                    duration_seconds=payload.duration_seconds)
    session.save()


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

        # Fin d'énoncé signalée par le navigateur : la première tranche de
        # cette passe part immédiatement, coupée au premier silence après un
        # minimum de parole. Le drapeau n'est consommé qu'une fois la tranche
        # réellement transcrite — s'il n'y a pas encore assez d'audio reçu
        # (le fragment portant la fin de l'énoncé n'est pas arrivé), il
        # reste posé pour la prochaine passe.
        flush = session.flush_requested

        while True:
            if not final:
                if flush:
                    if not should_flush(session):
                        break
                elif not should_process(session):
                    break
            try:
                duration = _transcribe_one(session, hints, final, flush)
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
            if flush:
                # Un énoncé suffit : le cadencement batch reprend ensuite. Le
                # drapeau est persisté — ``_transcribe_one`` a déjà sauvé la
                # session AVANT ce point, il faut repersister l'effacement.
                flush = False
                session.flush_requested = False
                session.save()

        if final:
            if runtime_config.value("stt_vad_finish_sweep") != "false":
                _sweep_uncovered(session, hints)
            _finalise(session)
            schedule_final_grounding(session.id, session.username)
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
    if session.offset_seconds > 0:
        session.covered_ranges[:] = [(0.0, session.offset_seconds)]
    text = (result.get("transcript") or "").strip()
    if text:
        _store_part(session, text,
                    (result.get("provider") or "", result.get("model") or ""),
                    duration_seconds=session.offset_seconds)
    session.status = "finished"
    session.save()
