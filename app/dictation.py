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

import difflib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app import audio_cache, geriatric_terms, live, llm, med_grounding, recordings, runtime_config, stt, usage
from app.config import settings
from app.database import Consultation, SessionLocal, merge_compute_stats, utcnow
from sqlalchemy.orm import Session
from app.stt import (
    _MISTRAL_REALTIME_MODEL_DEFAUT,
    MistralRealtimeTranscription,
    TranscriptionError,
    _decode_pcm16,
    _run_ffmpeg,
    _SILENCE_RE,
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
    #: Confiance mot-à-mot (``norm_phon → conf``) de chaque part, en miroir de
    #: ``parts``. Alimentée quand le STT fournit ``words[].confidence`` (endpoint
    #: custom) : sert au gate anti-faux-positifs au moment du grounding des
    #: VIEILLES parts (on ne retranscrit pas pour re-naître la confiance) et à
    #: l'extraction de la liste de médicaments sans resurgir les faux positifs.
    parts_conf: List[dict] = field(default_factory=list)
    #: Couverture de l'audio par les transcriptions réussies, dans l'horloge
    #: MESURÉE : intervalles [début, fin] du fichier brut déjà transcrits.
    #: Sert au filet de fin (``_sweep_uncovered``) à retrouver les trous
    #: laissés par un VAD trop strict ou une tranche échouée.
    covered_ranges: List[Tuple[float, float]] = field(default_factory=list)
    #: Fenêtres qu'une passe a classées « sans parole » en plein fichier —
    #: donc suspectes. Sur un WebM encore en croissance, ffmpeg peut lire une
    #: fenêtre comme silencieuse (cluster partiel, course lecture/écriture)
    #: alors que l'audio contient de la parole : on re-vérifie ces plages à la
    #: passe suivante, une fois l'audio arrivé, avant de les accepter comme
    #: silencieuses. Intervalles [début, fin] dans l'horloge mesurée.
    unverified: List[Tuple[float, float]] = field(default_factory=list)
    #: Fenêtre glissante (mode ``stt_sliding_window``) : texte PROVISOIRE de la
    #: fenêtre en cours, couvrant ``[window_start, offset_seconds]``. Chaque
    #: nouvelle fenêtre (45 s, pas 15 s) re-transcrit les 30 dernières secondes
    #: : l'alignement textuel du recouvrement confirme le préfixe (il rejoint
    #: ``parts``) et le provisoire est REMPLACÉ par la lecture la plus récente
    #: — plus de contexte, meilleure lecture des nombres et des accords. Vide =
    #: mode par tranches historique.
    window_text: str = ""
    #: Confiance mot-à-mot accumulée du provisoire (``norm_phon → conf``, min).
    #: Fusionnée dans ``transcript_conf`` à chaque engagement du provisoire.
    window_conf: dict = field(default_factory=dict)
    #: Début AUDIO du provisoire (horloge mesurée du fichier brut).
    window_start: float = 0.0
    #: Plages audio à re-vérifier à la fin de dictée (frontières d'alignement
    #: marginales, plages rattrapées en mini-tranche, région médicaments) —
    #: la vérification résiduelle ne réécoute QUE ces plages, jamais tout.
    verify_ranges: List[Tuple[float, float]] = field(default_factory=list)
    #: Un énoncé vient de se terminer côté navigateur (signal VAD) : la
    #: prochaine passe découpe et transcrit immédiatement, sans attendre le
    #: cadencement batch.
    flush_requested: bool = False
    #: Compteur des énoncés transcrits en streaming : chaque énoncé porte un
    #: identifiant, pour que la ligne provisoire des onglets puisse être
    #: retirée au commit.
    utterance_seq: int = 0
    #: Disponibilité du service STT sondée au démarrage (None = pas encore
    #: sondée ; booléen ensuite). Transmise au navigateur via ``to_public``.
    stt_available: Optional[bool] = None

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
            "parts_conf": self.parts_conf,
            "status": self.status,
            "last_error": self.last_error,
            "covered_ranges": list(self.covered_ranges),
            "unverified": [list(p) for p in self.unverified],
            "window_text": self.window_text,
            "window_conf": self.window_conf,
            "window_start": self.window_start,
            "verify_ranges": [list(p) for p in self.verify_ranges],
            "flush_requested": self.flush_requested,
            "utterance_seq": self.utterance_seq,
            "stt_available": self.stt_available,
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
            # Fenêtre glissante : texte provisoire (dernières secondes, encore
            # révisable par la fenêtre suivante). Le navigateur l'affiche À LA
            # SUITE des parts confirmées et le remplace à chaque passe.
            "window_text": self.window_text,
            "stt_available": self.stt_available,
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
            # Disponibilité immédiate du service STT (sonde légère au démarrage
            # de dictée) : renseigne le navigateur AVANT le premier segment pour
            # qu'il puisse prévenir tout de suite si l'endpoint est injoignable.
            "stt_available": self.stt_available,
        }

    def save(self) -> None:
        # Les scrutations du navigateur, les uploads et la transcription de
        # fond peuvent sauver la même session en parallèle. Un nom temporaire
        # partagé permettait à deux écrivains de se mélanger avant ``replace``
        # et de produire un state.json invalide au fragment suivant.
        with _lock_for(self.id):
            self.updated_at = time.time()
            directory = os.path.dirname(self.state_path)
            fd, temporary = tempfile.mkstemp(
                dir=directory, prefix=".state-", suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self.to_state(), handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.state_path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# Verrous
# ---------------------------------------------------------------------------
# Deux requêtes de la même session ne doivent jamais écrire dans « raw » en
# même temps, et une seule passe de découpage doit tourner à la fois. Les
# verrous vivent en mémoire : ils ne protègent qu'à l'intérieur d'un
# processus, ce qui suffit — ConsultAI tourne en un seul worker uvicorn.
_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_processing: set = set()


def _lock_for(session_id: str) -> threading.RLock:
    with _locks_guard:
        # ``save()`` peut être appelé depuis une section qui tient déjà le
        # verrou de session : il doit donc être réentrant.
        return _locks.setdefault(session_id, threading.RLock())


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
        stt_available=data.get("stt_available"),
        last_error=data.get("last_error", ""),
        covered_ranges=[
            (float(a), float(b)) for a, b in data.get("covered_ranges", [])
        ],
        unverified=[
            (float(a), float(b)) for a, b in data.get("unverified", [])
        ],
        window_text=str(data.get("window_text", "") or ""),
        window_conf=data.get("window_conf") or {},
        window_start=float(data.get("window_start", 0.0) or 0.0),
        verify_ranges=[
            (float(a), float(b)) for a, b in data.get("verify_ranges", [])
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


def _merge_conf_into(consultation, conf_map: Optional[dict]) -> None:
    """Fusionne ``conf_map`` (``norm_phon → confiance``) dans le brouillon.

    La confiance mot-à-mot est accumulée tranche par tranche dans
    ``transcript_conf`` (JSON) pour servir à la génération : sans audio, le
    LLM a besoin de savoir quels mots le STT a mal reconnus. Clé duplicatée
    dans deux tranches → on garde la confiance la PLUS BASSE (le mot fut
    ambigu au moins une fois, la prudence commande de le signaler).
    """
    if not conf_map:
        return
    try:
        stocke = json.loads(consultation.transcript_conf) if consultation.transcript_conf else {}
    except (ValueError, TypeError):
        stocke = {}
    if not isinstance(stocke, dict):
        stocke = {}
    for cle, valeur in conf_map.items():
        try:
            valeur = float(valeur)
        except (TypeError, ValueError):
            continue
        if cle not in stocke or valeur < float(stocke[cle]):
            stocke[cle] = valeur
    consultation.transcript_conf = json.dumps(stocke, ensure_ascii=False)


def _store_part(
    session: DictationSession, text: str, moteur: tuple = ("", ""),
    duration_seconds: float = 0.0, words: Optional[list] = None,
) -> None:
    """
    Reporte la tranche dans le brouillon — c'est lui, la copie durable. Le
    texte y est écrit dès qu'il existe, sans attendre la fin de la dictée :
    c'est ce qui fait qu'un onglet fermé ne coûte plus la consultation.

    Appelée même pour une tranche muette, afin que la durée d'audio traitée
    reste juste : elle sert de repère au médecin dans la liste des brouillons.
    """
    conf_part = None
    if text:
        if words:
            try:
                conf_part = med_grounding.conf_par_token(text, words)
            except Exception:
                conf_part = None
    if text:
        session.parts.append(text)
        session.parts_conf.append(conf_part or {})
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
        _merge_conf_into(consultation, conf_part)
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
        # Liste de médicaments ACCUMULÉE (``_grounding_meds``) persistée au même
        # rythme que le transcrit : un Refresh en cours de dictée restaure
        # l'onglet Validation au lieu de le vider. Le transcrite est écrit dès
        # qu'il existe (ce que fait le bloc au-dessus) ; on fait de même pour le
        # med-matcher, sans attendre le « Terminer » (``_finalize_grounding``
        # demeure la source définitive et écrasera ce brouillon). Un accumulateur
        # vide ou une dictée sans grounding sont laissés tels quels.
        med_accum = _grounding_meds.get(session.id)
        if _grounding_enabled() and med_accum:
            consultation.med_grounding_json = json.dumps(med_accum, ensure_ascii=False)
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
#: Plafond de candidats PHONÉTIQUES par passage de grounding incrémental.
#: L'exhaustivité (``maxi_phon=40``) est réservée au grounding final
#: (``_finalize_grounding``) : en cours de dictée, quelques pistes par
#: frontière suffisent à alimenter la liste pointée sans exploser la queue.
_GROUNDING_PHON_MAX = 8
_grounding_lock = threading.Lock()
_grounding_course: set = set()
_grounding_cooldown: Dict[str, float] = {}
_grounding_upto: Dict[str, int] = {}
_grounding_meds: Dict[str, list] = {}
#: Dernier ``tail`` de transcript sur lequel ``extract_validation_items`` a
#: été exécuté pour cette session — la queue n'évoluant que par frontières
#: stabilisées, on court-circuite la phonétique (coûteuse) tant qu'elle n'a
#: pas bougé.
_grounding_tail: Dict[str, str] = {}
#: Événements de FIN du scan final plein texte, par consultation : armés par
#: ``schedule_final_grounding`` au « Terminer », posés par ``_finalize_grounding``
#: en fin de job (qu'il aboutisse ou non). ``api_generate`` s'y appuie pour
#: ATTENDRE le scan de fond (borné) plutôt que d'en lancer un second dans la
#: fenêtre « Terminer → Générer » — le LLM n'a jamais une liste partielle et
#: on n'exécute pas deux fois ~10-17 s de phonétique au pire moment.
_grounding_events: Dict[int, threading.Event] = {}
_grounding_events_lock = threading.Lock()


def grounding_event(consultation_id: int) -> Optional[threading.Event]:
    """Événement de fin du scan final EN COURS pour ``consultation_id``.

    ``None`` si aucun scan de fond n'est armé (pas de dictée récente, ou job
    déjà terminé) : l'appelant retombe alors sur son propre calcul synchrone.
    """
    with _grounding_events_lock:
        return _grounding_events.get(consultation_id)


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


def _contexte_tranche(session: DictationSession, caracteres: int = 1500) -> Optional[str]:
    """Queue du transcript déjà stable, passée en contexte à la tranche suivante.

    La dictée découpe l'audio en tranches sans chevauchement ; une tranche
    transcrit la fin d'une phrase sans savoir son début. On renvoie la fin du
    texte ASSEMBLÉ (``window_text`` = provisoire de la fenêtre glissante, le
    plus récent ; sinon la dernière tranche stable) pour servir de ``prompt``
    au modèle ASR — il maintient la continuité au lieu de réinventer la
    phrase coupée.
    """
    texte = session.window_text or (session.parts[-1] if session.parts else "") or ""
    if not texte:
        return None
    corps = texte[-(caracteres + 1):]
    # Couper proprement : on ne reprend que le dernier début de phrase
    # (~dernier point) pour éviter de donner au modèle un fragment.
    idx = max(corps.rfind(". "), corps.rfind(".\n"), corps.rfind("! "), corps.rfind("? "))
    if idx > 0:
        corps = corps[idx + 1:]
    retour = corps.strip()
    return retour or None


# ---------------------------------------------------------------------------
# Doublons de phrases du transcript (artefact STT)
# ---------------------------------------------------------------------------
# La reconnaissance vocale ressort parfois la MÊME phrase deux fois de suite,
# au second passage légèrement reformulée : « Donc, j'ai augmenté l'hyprexa à
# 2 %. / Donc, j'ai augmenté l'hyper-exa à 2 %. » ou une subordonnée répétée
# seule (« ...quoique ces atteintes cognitives me semblaient plus importantes
# que cela. / Que ces atteintes cognitives me semblaient plus importantes que
# cela. »). Ces redites polluent le transcript brut (observé en note 40,
# dictée live). On les retire en FIN de dictée, prudemment : seulement les
# paires ADJACENTES clairement redondantes, et la première occurrence est
# toujours conservée.
_PHRASE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def _norm_propre(s: str) -> str:
    """Clé de comparaison d'une phrase : minuscules, espaces aplanis."""
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _mots_sans_punct(s: str) -> List[str]:
    """Mots d'une phrase, ponctuation finale et attachée retirée."""
    return [w.lower().strip(".,;:!?…()'’\"") for w in s.split()]


#: Ratio de similarité minimal (séquence, caractères) d'une vraie redite.
#: Calibré sur note 40 : la paire MMSE « Sur 30, puis a augmenté à 23 sur 30. »
#: / « Et puis, il a augmenté à 23 sur 30. » ressort à ~0.82, la reformulée
#: hyprexa/hyper-exa à ~0.89. AU-DESSOUS de 0.85, on exige en plus l'absence de
#: mot CONTENU distinct d'un seul côté (cf. ``_uniques_contenu``) : l'anaphore
#: légitime « Elle est suivie pour une HTA. » / « … pour un diabète. » (ratio
#: ~0.82) porte « hta » / « diabète », deux données cliniques distinctes — elle
#: est préservée.
_DOUBLON_RATIO = 0.80


def _mots_contenu(mots: List[str]) -> List[str]:
    """Mots « porteurs de sens » : >= 5 lettres alpha, hors chiffres."""
    return [m for m in mots
            if len(re.sub(r"[^a-zà-ÿœ]", "", m)) >= 5 and not m.isdigit()]


def _mots_proches(x: str, y: str) -> bool:
    """Deux mots dénotent-ils la même réalité ? Égalité, préfixe long, ou
    similarité de séquence élevée (une déformation STT, hyprexa/hyper-exa)."""
    if not x or not y:
        return False
    if x == y:
        return True
    if (len(x) >= 4 and y.startswith(x)) or (len(y) >= 4 and x.startswith(y)):
        return True
    return difflib.SequenceMatcher(None, x, y).ratio() >= 0.70


def _uniques_contenu(wa: List[str], wb: List[str]) -> List[str]:
    """Mots CONTENUS présents d'un seul côté — la preuve d'une donnée clinique
    distincte (HTA vs diabète), donc de deux phrases LÉGITIMES."""
    ca, cb = _mots_contenu(wa), _mots_contenu(wb)
    uniques = []
    for x in ca:
        if not any(_mots_proches(x, y) for y in cb):
            uniques.append(x)
    for x in cb:
        if not any(_mots_proches(x, y) for y in ca):
            uniques.append(x)
    return uniques


def _est_doublon(a: str, b: str) -> bool:
    """Vrai si ``b`` re-répète ``a`` (paire ADJACENTE du transcript).

    Un doublon de dictée réapporte le MÊME énoncé : à l'identique, tronqué,
    ou reformulé (hyprexa / hyper-exa). On compare la séquence de caractères
    normalisée ; le seuil haut (>= 0.85) suffit pour l'identique et la
    reformulée. Dans la bande 0.80-0.85, on exige en plus qu'aucune DONNÉE
    clinique ne soit d'un seul côté (``_uniques_contenu``) — c'est elle qui
    sépare la vraie redite (même fait, amorce différente : la paire MMSE de
    note 40) de l'anaphore légitime (HTA / diabète). Un énoncé de moins de
    3 mots n'est jamais traité.
    """
    na, nb = _norm_propre(a), _norm_propre(b)
    if not na or not nb:
        return False
    wa, wb = _mots_sans_punct(na), _mots_sans_punct(nb)
    if min(len(wa), len(wb)) < 3:
        return False
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    if ratio >= 0.85:
        return True
    if ratio >= _DOUBLON_RATIO:
        return not _uniques_contenu(wa, wb)
    return False


def _dedupe_adjacent(texte: str) -> str:
    """Retire les redites du transcript, première occurrence la plus complète
    conservée.

    Quatre formes de redites, toutes observées quand deux tranches se
    succèdent sans contexte (découpage du live, note 40) :

      * **Écho tronqué** : ``b`` n'est que le DÉBUT de ``a`` (le modèle ASR a
        « commis » la phrase entière en N puis la re-récite tronquée en N+1)
        → on écarte ``b`` ;
      * **Extension** : ``b`` commence par ``a`` et la PROLONGE (la même
        phrase, plus complète, au second passage) → on garde ``b`` ;
      * **Redite pure / reformulée** : ``b`` répète ``a`` (identique ou
        réécrite, hyprexa/hyper-exa) → on écarte ``b`` ;
      * **Reprise de frontière** : la QUEUE de ``a`` est redite en TÊTE de
        ``b`` par le chevauchement mot à mot → on replie les deux.

    Aucun autre nettoyage : la ponctuation, les fragments courts et les
    retours à la ligne sont laissés tels quels ; les phrases sont re-joignées
    par un espace.
    """
    phrases = [p.strip() for p in re.split(_PHRASE_SPLIT_RE, texte or "") if p and p.strip()]
    out: List[str] = []
    for ph in phrases:
        if not out:
            out.append(ph)
            continue
        n_out = _mots_sans_punct(_norm_propre(out[-1]))
        n_ph = _mots_sans_punct(_norm_propre(ph))
        if min(len(n_out), len(n_ph)) >= 3:
            # Écho tronqué : ph = début strict de la phrase précédente.
            if len(n_ph) < len(n_out) and n_ph == n_out[:len(n_ph)]:
                continue
            # Extension : ph commence par la précédente et la prolonge.
            if len(n_out) < len(n_ph) and n_out == n_ph[:len(n_out)]:
                out[-1] = ph
                continue
        # Redite pure / reformulée.
        if _est_doublon(out[-1], ph):
            continue
        # Reprise de frontière (queue/tête).
        fusion = _dedupe_frontiere(out[-1], ph)
        if fusion is not None:
            out[-1] = fusion
            continue
        out.append(ph)
    return " ".join(out)


def _dedupe_frontiere(a: str, b: str) -> Optional[str]:
    """Replie ``b`` sur ``a`` quand ``b`` re-dit la FIN de ``a`` (frontière).

    On cherche le plus long chevauchement ENTRE la fin de ``a`` et le début de
    ``b`` (alignement de mots). Si la queue de ``a`` et la tête de ``b``
    partagent un suffixe/préfixe commun (>= 5 mots), ``b`` est un rappel de
    la frontière : on les fusionne en ``a + (b sans le chevauchement)``.
    Retourne la phrase fusionnée, ou ``None`` si rien à replier.

    Exemple (note 40) : ``a`` = « ...on a cessé les traitements. » et ``b`` =
    « on a cessé les traitements et on a introduit un nouveau. » → on replie
    en « ...on a cessé les traitements et on a introduit un nouveau. ».
    """
    # Mots normalisés de chaque phrase (la ponctuation est gardée séparément).
    ma = re.findall(r"[\wÀ-ÿ'’0-9-]+", a.lower())
    mb = re.findall(r"[\wÀ-ÿ'’0-9-]+", b.lower())
    if min(len(ma), len(mb)) < 5:
        return None
    # Plus long préfixe de ``b`` qui est aussi un suffixe de ``a``.
    chevauchement = 0
    for k in range(min(len(ma), len(mb)), 4, -1):
        if ma[-k:] == mb[:k]:
            chevauchement = k
            break
    if not chevauchement:
        return None
    # ``b`` commence par le rappel ; on en retire le préfixe redondant.
    mots_b = b.split()
    if len(mots_b) <= chevauchement:
        return a       # ``b`` n'est QUE le rappel → on ne garde qu'``a``
    reste = " ".join(mots_b[chevauchement:])
    return (a.rstrip() + " " + reste.lstrip()).strip()


def _assainir_doublons(session: DictationSession) -> None:
    """Nettoie les redites du transcript complet en fin de dictée.

    Déduplique le texte final, replie la session sur UNE part propre (les
    confiances mot-à-mot sont fusionnées), impose le texte nettoyé dans la
    consultation (``raw_transcript``) et le re-diffuse aux onglets ouverts.
    Appelé APRÈS le filet de fin, juste avant la finalisation du grounding.
    """
    texte = " ".join(session.parts).strip()
    if not texte:
        return
    propre = _dedupe_adjacent(texte)
    if propre == texte:
        return
    conflits: dict = {}
    for cmap in session.parts_conf:
        if isinstance(cmap, dict):
            conflits.update(cmap)
    session.parts = [propre]
    session.parts_conf = [conflits]
    session.save()
    with _lock_for_consultation(session.consultation_id), SessionLocal() as db:
        consultation = db.get(Consultation, session.consultation_id)
        if consultation is not None:
            consultation.raw_transcript = propre
            consultation.updated_at = utcnow()
            db.commit()
    try:
        live.publish(_session_owner(session), "transcript_correct", {
            "consultation_id": session.consultation_id,
            "session_id": session.id,
            "parts": list(session.parts),
        })
    except Exception:
        logger.exception("Re-diffusion du transcript nettoyé impossible")


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
    nouveaux_conf: List[dict] = []
    nouvelles_couvertures: List[Tuple[float, float]] = []
    curseur = start
    while end - curseur > 0.05:
        restant = end - curseur
        if restant <= high:
            longueur = restant
            real = restant
        else:
            longueur, real = find_cut_point(
                session.audio_path, curseur, target, low, high)
        try:
            payload = extract_segment(session.audio_path, curseur, longueur, real)
        except TranscriptionError:
            break
        if payload.duration_seconds < _MIN_SEGMENT_SECONDS:
            break
        curseur += payload.duration_seconds
        try:
            result = transcribe_payload(
                payload, hints, contexte_precedent=_contexte_tranche(session),
            )
        except TranscriptionError:
            continue
        texte = (result.get("transcript") or "").strip()
        if texte:
            words = result.get("words") or None
            try:
                conf_map = med_grounding.conf_par_token(texte, words) if words else {}
            except Exception:
                conf_map = {}
            nouveaux.append(texte)
            nouveaux_conf.append(conf_map)
            # Une couverture PAR morceau, comme en cours de dictée : les
            # parts et ``covered_ranges`` restent en verrou (indices alignés),
            # ce que l'insertion positionnelle de ``_inserer_part`` exige.
            nouvelles_couvertures.append(
                (curseur - payload.duration_seconds, curseur)
            )
        if texte:
            logger.info(
                "Dictée %s : stabilisation [%.1f-%.1f s] (%d caractères)",
                session.id, curseur - payload.duration_seconds, curseur, len(texte),
            )
    if not nouveaux:
        return None

    # Redites dans la tranche réécrite : le STT ressort parfois la même phrase
    # deux fois à la suite (reformulée au second passage). On déduplique le
    # bloc réécrit sur le texte ORIGINAL (casse conservée) et on replie sur
    # une part propre — sa couverture couvre tout le bloc réécrit.
    texte_bloc = " ".join(nouveaux)
    nouveau = _norm_spaces(texte_bloc)
    dedup = _dedupe_adjacent(texte_bloc)
    if _norm_spaces(dedup) != nouveau:
        conflits: dict = {}
        for c in nouveaux_conf:
            if isinstance(c, dict):
                conflits.update(c)
        nouveaux = [dedup]
        nouveaux_conf = [conflits]
        nouvelles_couvertures = [(start, curseur)]
        nouveau = _norm_spaces(" ".join(nouveaux))

    ancien = _norm_spaces(" ".join(session.parts[a:b + 1]))
    if ancien and ancien == nouveau:
        return session                     # déjà stable, rien à faire

    session.parts[a:b + 1] = nouveaux
    session.parts_conf[a:b + 1] = nouveaux_conf
    session.covered_ranges[a:b + 1] = nouvelles_couvertures
    session.save()

    # Persistance durable du transcript remplacé.
    owner = _session_owner(session)
    with _lock_for_consultation(session.consultation_id), SessionLocal() as db:
        consultation = db.get(Consultation, session.consultation_id)
        if consultation is not None:
            consultation.raw_transcript = " ".join(session.parts).strip()
            # La confiance est CONSERVÉE tant que le transcript existe : la
            # stabilisation remplace une tranche de texte, mais la confiance des
            # tranches inchangées reste valide. On fusionne donc les nouvelles
            # confiances dans le mapping existant au lieu de repartir de zéro
            # (``_merge_conf_into`` garde la valeur la plus basse, le mot ayant
            # été ambigu au moins une fois — prudence conservée).
            for cmap in session.parts_conf:
                _merge_conf_into(consultation, cmap)
            consultation.updated_at = utcnow()
            db.commit()

    live.publish(owner, "transcript_correct", {
        "consultation_id": session.consultation_id,
        "session_id": session.id,
        "parts": list(session.parts),
    })
    return session


def _conf_tail(session: DictationSession, tail: str) -> Optional[dict]:
    """Confiance mot-à-mot agrégée des parts couvrant ``tail`` (liste seule).

    ``tail`` = ``" ".join(parts)[-3000:]``. On remonte les parts depuis la fin
    jusqu'à couvrir la longueur du tail, on fusionne leurs ``parts_conf``
    (la clé ``norm_phon`` suffit, le gate ne lit qu'à la clé). Retourne ``None``
    si aucune part portait de confiance.
    """
    if not session.parts_conf:
        return None
    conf: dict = {}
    restant = len(tail)
    for part, cmap in reversed(list(zip(session.parts, session.parts_conf))):
        if not cmap:
            continue
        conf.update(cmap)
        restant -= len(part)
        if restant <= 0:
            break
    return conf or None


def _merge_and_publish_meds(session: DictationSession) -> None:
    """Met à jour la liste pointée (moteur déterministe) et la diffuse.

    Calcule sur la QUEUE du transcript (derniers ~600 mots) puis fusionne dans
    l'accumulateur de la session (clé = nom normalisé, première posologie
    non vide conservée) : pas de re-normalisation complète à chaque frontière,
    ce qui garde la passe compatible même avec une dictée de 30 min.

    La résolution phonétique (BK-tree, facturée en CPU) n'est relancée que si
    la queue a effectivement changé depuis la dernière fusion — une frontière
    réécrite identique ne doit pas repayer cette passe, ni n'envoie un SSE
    inutile.
    """
    owner = _session_owner(session)
    texte = " ".join(session.parts)
    tail = texte[-3000:].lstrip()              # ≈ derniers ~600 mots
    if _grounding_tail.get(session.id) == tail:
        return                                 # queue inchangée : rien à recalculer
    _grounding_tail[session.id] = tail
    conf_tail = _conf_tail(session, tail)
    # En cours de dictée, un plafond phonétique bas suffit à alimenter la liste
    # pointée : l'exhaustivité (maxi_phon=40) est réservée au grounding final
    # (``_finalize_grounding``). Évite d'exploser la queue sur chaque frontière.
    nouveaux = med_grounding.extract_validation_items(tail, conf=conf_tail,
                                                      maxi_phon=_GROUNDING_PHON_MAX)
    accum = _grounding_meds.get(session.id, [])
    par_cle = {med_grounding.norm_phon(i.get("base") or i["name"]): i for i in accum}
    for item in nouveaux:
        cle = med_grounding.norm_phon(item.get("base") or item["name"])
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
        # Termes gériatriques réécrits inline dans le texte de CETTE dictée
        # (module À PART de med_grounding) : paires {garble, correct} pour le
        # surlignage + rollover du front-end. Même langue que le gabarit.
        "geriatric": _geriatric_corrections(texte),
    })


def _geriatric_corrections(texte: str) -> list:
    """Candidats gériatriques pour le rollover pendant la dictée.

    Réécritures inline sûres (``{garble, correct}``) + candidats phonétiques
    du profil (``{garble, correct, confidence}``, ex. MMS→MMSE,
    isosnaphe→ISO-SMAF) sur le texte de CETTE dictée.
    """
    if not (texte or "").strip():
        return []
    from app import preferences
    try:
        return geriatric_terms.corrections_et_hints(
            texte, langue=preferences.document_language(),
        )
    except Exception:
        return []


def schedule_final_grounding(session_id: str, username: str, consultation_id: int) -> None:
    """Lance (tâche de fond) la liste de médicaments DÉFINITIVE d'une dictée.

    Exécutée au « Terminer », une seule fois, sur le transcript complet : le
    résultat est persisté dans ``consultation.med_grounding_json`` et diffusé
    via ``med_grounding_result``. Ne bloque pas la clôture de la dictée.

    Arme aussi (``_grounding_events``) l'événement de fin que ``api_generate``
    attend en cas de course « Terminer → Générer » : la génération n'exécute
    jamais un second scan plein texte (voir ``dictation.grounding_event``).
    """
    if not _grounding_enabled():
        return
    with _grounding_events_lock:
        _grounding_events[consultation_id] = threading.Event()
    threading.Thread(
        target=_finalize_grounding,
        args=(session_id, username),
        daemon=True,
        name=f"grounding-final-{session_id}",
    ).start()


def _finalize_grounding(session_id: str, username: str) -> None:
    consultation_id: Optional[int] = None
    try:
        with _lock_for(session_id):
            session = load_session(session_id, username)
            consultation_id = session.consultation_id
            owner = _session_owner(session)
            if session.status not in ("recording", "finished"):
                return
            text = " ".join(session.parts).strip()
            if not text:
                return
            # Liste DÉFINITIVE = scan PLEIN TEXTE, la même source que les
            # hints LLM (main.py ``_apply_grounding``). La liste accumulée
            # en continu (``_merge_and_publish_meds``, fenêtre de queue
            # ~600 mots, maxi_phon=8) laisse échapper les garbles dictés en
            # début de note — « sérocoïl » → Seroquel n° 42 — hors de la
            # fenêtre au « Terminer ». Le scan complet (~10-17 s en tâche
            # de fond) retrouve TOUS les candidats ; on le persiste avec la
            # marque de finalisation (``grounding_finalized_at``), et la
            # génération ne lit QUE cette version. La confiance mot-à-mot
            # complète vient de ``transcript_conf``, accumulée tranche par
            # tranche par ``_merge_conf_into``.
            # 1) Lecture courte (confiance + marque éventuelle) SANS tenir la
            #    session pendant le scan.
            conf_map: dict = {}
            with _lock_for_consultation(session.consultation_id), SessionLocal() as db:
                consultation = db.get(Consultation, session.consultation_id)
                if consultation is None:
                    return
                if consultation.grounding_finalized_at is not None:
                    # La génération (garde synchrone) a déjà recalculé pendant
                    # la course « Terminer » → « Générer » : liste autoritaire
                    # en place, on ne refait pas le travail.
                    items = json.loads(consultation.med_grounding_json or "[]")
                else:
                    try:
                        conf_map = (json.loads(consultation.transcript_conf)
                                    if consultation.transcript_conf else {})
                    except (ValueError, TypeError):
                        conf_map = {}
                    items = None
            if items is None:
                # 0) Détection de la zone médicaments par le LLM configuré.
                #    Le résultat pré-remplace ``_medlist_regions`` pour que le
                #    pipeline de grounding se concentre sur cette zone.
                t_region = time.monotonic()
                med_region = med_grounding.detect_med_region(text)
                region_ms = round((time.monotonic() - t_region) * 1000, 1)
                m = med_grounding.matcher()
                if med_region and med_region.get("region_text"):
                    m.set_med_region(med_region["region_text"], text)
                try:
                    # 1) Scan plein texte — chronométré pour les statistiques de la
                    #    consultation (``compute_stats_json``) : c'est la mesure de la
                    #    fenêtre « Terminer → Générer » qui disparaît de l'attente.
                    #    Depuis 2026-09-07, ``_detail`` renvoie en plus la dictée
                    #    DÉJÀ normalisée (les deux phases de la chaîne partageaient
                    #    la MÊME passe ``normalize(inline_safe=True)`` ~3-6 s sur les
                    #    longues dictées — une seule suffit, voir plus bas).
                    t_scan = time.monotonic()
                    items, fixed_inline, inline_med = (
                        med_grounding.extract_validation_items(
                            text, conf=conf_map or None, _detail=True))
                    grounding_scan_ms = round(
                        (time.monotonic() - t_scan) * 1000, 1)
                    # Pré-calcul du texte DÉJÀ normalisé (inline sûr médicamenteux) :
                    # on ne re-normalise PAS le texte — ``fixed_inline`` vient de la
                    # passe du scan — seuls les termes gériatriques restent à
                    # appliquer (``deja_normalise`` saute la passe 1 de
                    # ``precompute_normalization``). La langue du gabarit est celle
                    # de la dictée (document) ; la génération réutilisera le cache
                    # au lieu de re-résoudre ~5-14 s dans sa fenêtre d'attente.
                    t_norm = time.monotonic()
                    from app import preferences
                    n_transcript, inline_fixed = (
                        geriatric_terms.precompute_normalization(
                            text, conf=conf_map or None,
                            langue=preferences.document_language(),
                            deja_normalise=fixed_inline,
                            inline_med=inline_med,
                        ))
                    precompute_ms = round(
                        (time.monotonic() - t_norm) * 1000, 1)
                finally:
                    m.clear_med_region()
                # 2) Persistance (court verrou de nouveau ; reverrouillage au
                #    commit au cas où la garde synchrone aurait gagné entre-
                #    temps — le calcul est idempotent et déterministe).
                with _lock_for_consultation(session.consultation_id), SessionLocal() as db:
                    consultation = db.get(Consultation, session.consultation_id)
                    if consultation is None:
                        return
                    if consultation.grounding_finalized_at is None:
                        consultation.med_grounding_json = (
                            json.dumps(items, ensure_ascii=False))
                        consultation.grounding_finalized_at = utcnow()
                        consultation.normalized_transcript = n_transcript
                        consultation.inline_fixed_json = (
                            json.dumps(sorted(inline_fixed), ensure_ascii=False))
                        consultation.med_region_json = (
                            json.dumps(med_region, ensure_ascii=False)
                            if med_region else None)
                        # Titre du brouillon ramené par la même détection de
                        # région (libellé court demandé au modèle). Une raison
                        # tapée au clavier fait autorité ; sinon le libellé
                        # sert à retrouver le brouillon dans la liste.
                        if (med_region is not None and med_region.get("titre")
                                and not (consultation.reason or "").strip()
                                and str(med_region["titre"]).strip()[:300]
                                != consultation.title):
                            consultation.title = (
                                str(med_region["titre"]).strip()[:300])
                        consultation.compute_stats_json = merge_compute_stats(
                            consultation.compute_stats_json,
                            {"grounding_scan_ms": grounding_scan_ms,
                             "precompute_normalize_ms": precompute_ms,
                             "region_detect_ms": region_ms,
                             "region_model": (med_region or {}).get("model", ""),
                             "precompute_lang": preferences.document_language()},
                        )
                        db.commit()
            else:
                med_region = None
            # Charger la zone persistée si le scan n'a pas été refait
            # (grounding_finalized_at était déjà posé).
            if med_region is None:
                try:
                    mr = consultation.med_region_json if consultation else None
                    if mr:
                        med_region = json.loads(mr)
                except Exception:
                    pass
            live.publish(owner, "med_grounding_result", {
                "consultation_id": session.consultation_id,
                "session_id": session.id,
                "items": items,
                "geriatric": _geriatric_corrections(text),
                "med_region": med_region,
            })
    except Exception:
        logger.exception("Grounding final impossible (dictée %s)", session_id)
    finally:
        # Fin du job de fond (abouti ou non) : libère la course « Terminer →
        # Générer ». Une génération en attente sur ``grounding_event`` repart
        # aussitôt — la liste (ou le filet synchrone, si rien n'a été produit)
        # décidera de la suite.
        with _grounding_events_lock:
            event = _grounding_events.pop(consultation_id, None) if consultation_id else None
        if event is not None:
            event.set()
        with _grounding_lock:
            _grounding_course.discard(session_id)
            _grounding_upto.pop(session_id, None)
            _grounding_meds.pop(session_id, None)
            _grounding_tail.pop(session_id, None)


def _should_transcribe(session: DictationSession) -> bool:
    """Vrai si le service STT est (vraisemblablement) joignable.

    Pose ``session.stt_available`` pour le signaler au navigateur
    (informations). Ne publie PAS de SSE et ne bloque jamais : la vraie alarme
    « service indisponible » ne part qu'à l'échec réel d'une transcription (où
    l'on sait que le STT n'a pas répondu), pas sur une sonde qui peut avoir un
    faux négatif transitoire.
    """
    try:
        endpoint = stt.active_stt_endpoint()
        if not endpoint:
            return True
        joignable = stt.stt_available(endpoint)
    except Exception:
        joignable = True
    session.stt_available = joignable
    return joignable


def _is_transport_error(message: str) -> bool:
    """Vrai si l'erreur de transcription signale un stt injoignable (réseau/
    timeout/endpoint) et non un refus métier (aucune parole, clé, 4xx)."""
    m = (message or "").lower()
    return ("point de terminaison" in m or "urllib" in m
            or "timed out" in m or "timeout" in m
            or "connection refused" in m or "unreachable" in m)


def _signal_stt_unavailable(session: DictationSession, message: str) -> None:
    """Diffuse l'avis d'indisponibilité du STT (échec RÉEL de transcription)."""
    try:
        owner = _session_owner(session)
        live.publish(owner, "stt_unavailable", {
            "consultation_id": session.consultation_id,
            "session_id": session.id,
            "message": "Le service de reconnaissance vocale ne répond pas.",
        })
        logger.warning(
            "Dictée %s : service STT injoignable à la transcription — %s",
            session.id, message,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fenêtre glissante (mode ``stt_sliding_window``)
# ---------------------------------------------------------------------------
# Motivation : le transcript LIVE divergeait fort de la retranscription
# complète (76 % de similarité mesuré sur la consultation 46) — tranches de
# 10 s transcrites isolément, chiffres de dossier éclatés token par token,
# accords perdus faute de contexte, audio partiel aux frontières. Une fenêtre
# glissante de 45 s (pas 15 s, recouvrement 30 s) donne au modèle ASR le
# contexte qui lui manque : chaque fenêtre re-transcrit les 30 dernières
# secondes et le provisoire est remplacé par la lecture la plus récente.
#
# Sans timestamps (Cohere Transcribe n'en fournit pas), la fusion est
# TEXTUELLE : la queue du provisoire et la tête de la nouvelle fenêtre
# décrivent le MÊME audio — on aligne les deux en jetons normalisés (casse,
# accents, ponctuation ignorés) ; le préfixe du provisoire avant
# l'alignement est CONFIRMÉ (il rejoint ``parts``), et le provisoire devient
# la nouvelle fenêtre EN ENTIER (sa tête re-transcrite remplace l'ancienne
# lecture du recouvrement). En cas d'alignement ambigu : rien n'est
# confirmé, l'audio neuf est ajouté au provisoire en mini-tranche, et le
# filet de fin (``_sweep_uncovered``) reste la sécurité ultime.

#: Jetons minimum d'un recouvrement pour oser confirmer le préfixe.
_FUSION_MIN_JETONS = 8

#: Similarité minimale (jetons normalisés) d'un recouvrement fiable.
_FUSION_SIM_MIN = 0.75

#: Au-delà de ce ratio de pic, la frontière d'alignement est jugée solide ; en
#: deçà (mais au-dessus du seuil d'acceptation), la plage est marquée pour la
#: re-vérification résiduelle de fin de dictée.
_FUSION_RATIO_FIABLE = 0.87

#: Plafond de jetons comparés d'un côté (la queue du provisoire suffit).
_FUSION_PLAFOND_JETONS = 160

#: Au-delà de ce nombre de jetons, un provisoire qui n'arrive plus à s'aligner
#: est engagé en bloc (soupape anti-croissance, ~2 fenêtres et demie).
_FUSION_ENGAGEMENT_MAX_JETONS = 280


def fenetrage_actif() -> bool:
    """Fenêtre glissante active ? Réglage + compatibilité temps réel.

    Le mode ``sse`` (Mistral, énoncé par énoncé en streaming) possède son
    propre chemin : la fenêtre glissante ne s'y applique pas.
    """
    if runtime_config.value("stt_sliding_window") != "true":
        return False
    return realtime_mode() != "sse"


def _fenetre_secondes() -> float:
    """Durée de la fenêtre glissante (``stt_window_seconds``, 45 s)."""
    try:
        valeur = float(runtime_config.value("stt_window_seconds") or 45)
    except (TypeError, ValueError):
        valeur = 45.0
    return max(20.0, min(120.0, valeur))


def _fenetre_pas() -> float:
    """Nouvel audio ajouté par passe (``stt_window_step_seconds``, 15 s).

    Le recouvrement vaut ``fenêtre - pas`` (30 s par défaut) : c'est la plage
    re-transcrite à chaque passe, bornée pour laisser toujours du neuf.
    """
    try:
        valeur = float(runtime_config.value("stt_window_step_seconds") or 15)
    except (TypeError, ValueError):
        valeur = 15.0
    return max(5.0, min(_fenetre_secondes() - 5.0, valeur))


def should_process_fenetres(session: DictationSession) -> bool:
    """Assez d'audio neuf pour une nouvelle fenêtre ?"""
    disponible = session.received_seconds - session.offset_seconds
    if not session.window_text:
        return disponible >= _fenetre_secondes()
    return disponible >= _fenetre_pas()


def _jeton_norm(s: str) -> str:
    """Jeton de comparaison : casse, accents et ponctuation ignorés."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-z]", "", s.lower())


def _jetons_norm(texte: str) -> List[str]:
    return [j for j in (_jeton_norm(m) for m in (texte or "").split()) if j]


def _recouvrement_attendu(session: DictationSession, texte: str,
                          debut: float, fin: float) -> int:
    """Taille attendue du recouvrement, en jetons, depuis la géométrie audio.

    Le recouvrement AUDIO est connu exactement : ``[debut, offset]`` (la
    fenêtre commence ``taille - pas`` avant le curseur). Sa part dans le
    provisoire (``[window_start, offset]``) et dans la fenêtre (``[debut,
    fin]``) donne deux estimations du nombre de jetons à appareiller ; leur
    minimum borne la bande de recherche de ``_aligner_chevauchement``.
    """
    span_provisoire = max(1e-6, session.offset_seconds - session.window_start)
    span_fenetre = max(1e-6, fin - debut)
    recouvrement = min(session.offset_seconds, fin) - debut
    if recouvrement <= 0:
        return 0
    est_provisoire = int(len(_jetons_norm(session.window_text))
                         * recouvrement / span_provisoire)
    est_fenetre = int(len(_jetons_norm(texte)) * recouvrement / span_fenetre)
    return max(0, min(est_provisoire, est_fenetre))


def _aligner_chevauchement(ancien: str, nouveau: str,
                           m_attendu: Optional[int] = None
                           ) -> Tuple[int, float]:
    """Jetons de la QUEUE d'``ancien`` retrouvés en TÊTE de ``nouveau``.

    Les deux textes décrivent le même audio (recouvrement de fenêtres) : la
    similarité de jetons normalisés (casse, accents, ponctuation ignorés)
    présente un PIC net au vrai recouvrement — en deçà, les deux tranches
    décrivent des portions audio différentes ; au-delà, elles s'étendent
    chacune vers de l'audio distinct. On cherche donc l'ARGMAX du ratio dans
    une bande autour du recouvrement ATTENDU (``m_attendu``, dérivé de la
    géométrie audio par l'appelant) : un glouton « plus grand m d'abord »
    s'arrêtait sur des correspondances étirées (ratio 0.75 tout juste) qui
    confirmaient presque tout le provisoire et perdaient le texte neuf.
    Retourne ``(m, ratio_du_pic)`` — ``(0, meilleur_ratio)`` si rien n'est
    fiable : le pic sous le seuil signale une frontière incertaine (plage à
    re-vérifier à la fin).
    """
    ta = _jetons_norm(ancien)
    tb = _jetons_norm(nouveau)
    if not ta or not tb:
        return 0, 0.0
    if m_attendu and m_attendu > 0:
        maximum = min(len(ta), len(tb), int(m_attendu * 1.4) + 4,
                      _FUSION_PLAFOND_JETONS)
        minimum = max(_FUSION_MIN_JETONS, int(m_attendu * 0.45))
    else:
        maximum = min(len(ta), len(tb), _FUSION_PLAFOND_JETONS)
        minimum = _FUSION_MIN_JETONS
    if maximum < minimum:
        return 0, 0.0
    meilleur, meilleur_ratio = 0, 0.0
    for m in range(maximum, minimum - 1, -1):
        ratio = difflib.SequenceMatcher(None, ta[-m:], tb[:m]).ratio()
        if ratio > meilleur_ratio:
            meilleur, meilleur_ratio = m, ratio
    if meilleur and meilleur_ratio >= _FUSION_SIM_MIN:
        return meilleur, meilleur_ratio
    return 0, meilleur_ratio


def _decoupe_fusion(ancien: str, nouveau: str,
                    m_attendu: Optional[int] = None) -> Tuple[str, int, float]:
    """Scinde l'ancien provisoire en (confirmé, jetons recouverts, ratio).

    ``confirmé`` = texte de l'ancien provisoire AVANT le recouvrement — il
    rejoint les parts durables. ``ratio`` : qualité du pic d'alignement ; un
    pic marginal (< ``_FUSION_RATIO_FIABLE``) signale une frontière
    incertaine — l'appelant marque alors la plage pour re-vérification.
    Recouvrement insuffisant → ("", 0, ratio) : rien n'est confirmé.
    """
    m, ratio = _aligner_chevauchement(ancien, nouveau, m_attendu)
    jetons = (ancien or "").split()
    if m < _FUSION_MIN_JETONS or len(jetons) <= m:
        return "", 0, ratio
    return " ".join(jetons[:len(jetons) - m]).strip(), m, ratio


def _fusionner_fenetre(session: DictationSession, texte: str, conf: dict,
                       debut: float, fin: float, moteur: tuple,
                       confirme: Optional[str] = None) -> None:
    """Intègre la transcription d'une fenêtre ``[debut, fin]`` au transcript.

    ``confirme`` : préfixe du provisoire AVANT le recouvrement, déjà
    déterminé par l'appelant (``_decoupe_fusion``) — il rejoint ``parts``
    (avec sa plage audio interpolée au prorata des jetons — pas de
    timestamps), et le provisoire devient le texte nouveau EN ENTIER. Le
    confirmé porte la confiance accumulée de la fenêtre (clé-dépendante, min)
    — fusionnée en base par ``_store_part`` via ``_merge_conf_into``.
    """
    if not session.window_text:
        # Première fenêtre : tout reste provisoire, rien à confirmer.
        session.window_text = texte
        session.window_start = debut
        for cle, valeur in (conf or {}).items():
            try:
                valeur = float(valeur)
            except (TypeError, ValueError):
                continue
            precedent = session.window_conf.get(cle)
            if precedent is None or valeur < float(precedent):
                session.window_conf[cle] = valeur
        return

    if confirme is None:
        confirme, m, _ratio = _decoupe_fusion(session.window_text, texte)
    else:
        m = len(session.window_text.split()) - len(confirme.split())
    if confirme:
        jetons = session.window_text.split()
        total = len(jetons)
        duree = max(0.0, session.offset_seconds - session.window_start)
        ratio = (len(confirme.split()) / total) if total else 0.0
        fin_confirmee = session.window_start + duree * ratio
        _store_part(session, confirme, moteur, duration_seconds=0.0, words=None)
        session.covered_ranges.append((session.window_start, fin_confirmee))
        logger.info(
            "Dictée %s : fenêtre fusionnée — %d jetons confirmés "
            "[%.1f-%.1f s], recouvrement %d jetons",
            session.id, len(confirme.split()), session.window_start,
            fin_confirmee, m,
        )

    # Le provisoire devient la lecture la plus récente, recouvrement inclus.
    session.window_text = texte
    session.window_start = debut
    for cle, valeur in (conf or {}).items():
        try:
            valeur = float(valeur)
        except (TypeError, ValueError):
            continue
        precedent = session.window_conf.get(cle)
        if precedent is None or valeur < float(precedent):
            session.window_conf[cle] = valeur


def _engager_provisoire(session: DictationSession) -> None:
    """Engage le provisoire en part durable (fin de dictée, ou soupape).

    La plage audio est exacte : le provisoire couvre ``[window_start,
    offset_seconds]`` par construction (tout l'audio intermédiaire a été
    inclus dans une fenêtre). La confiance accumulée est fusionnée en base.
    """
    if not session.window_text:
        return
    texte = session.window_text
    debut, fin = session.window_start, session.offset_seconds
    conf = session.window_conf
    session.window_text = ""
    session.window_start = 0.0
    session.window_conf = {}
    _store_part(session, texte, ("", ""), duration_seconds=0.0, words=None)
    session.covered_ranges.append((debut, fin))
    try:
        with _lock_for_consultation(session.consultation_id), SessionLocal() as db:
            consultation = db.get(Consultation, session.consultation_id)
            if consultation is not None:
                _merge_conf_into(consultation, conf)
                db.commit()
    except Exception:
        logger.exception("Dictée %s : confiance du provisoire non fusionnée", session.id)
    logger.info(
        "Dictée %s : provisoire engagé (%d caractères, [%.1f-%.1f s])",
        session.id, len(texte), debut, fin,
    )


def _usage_fenetre(session: DictationSession, moteur: tuple,
                   secondes: float) -> None:
    """Facture l'audio réellement envoyé pour CETTE fenêtre (une ligne)."""
    if not moteur[0] or secondes <= 0:
        return
    try:
        owner = _session_owner(session)
        with SessionLocal() as db:
            usage.log_stt_usage(
                db, owner=owner, consultation_id=session.consultation_id,
                provider=moteur[0], model=moteur[1],
                audio_seconds=int(round(secondes)),
            )
            db.commit()
    except Exception:
        logger.exception("Dictée %s : usage STT (fenêtre) non journalisé", session.id)


def _transcribe_fenetre(session: DictationSession, hints: str,
                        flush: bool = False) -> Optional[float]:
    """Transcrit une fenêtre glissante et fusionne le résultat.

    Retourne la durée d'audio consommée (curseur avancé), ou ``None`` s'il
    n'y a pas assez d'audio neuf ou si la passe n'a rien pu consommer.
    Fenêtre muette : le curseur avance, rien n'est écrit — le filet de fin
    couvrira la plage si elle contient de la parole. Alignement raté : la
    fenêtre est JETÉE (pas de frontière sûre pour découper le texte), l'audio
    neuf est rattrapé en mini-tranche ajoutée au provisoire, et le curseur
    n'avance que du rattrapage — la prochaine fenêtre (recouvrement porté à
    ~45 s) retentera l'alignement.
    """
    taille = _fenetre_secondes()
    pas = _fenetre_pas()
    recouvrement = taille - pas

    if not session.window_text:
        # Première fenêtre en CROISSANCE : déclenchée dès ``pas`` secondes
        # d'audio (premier texte à ~15 s), la fenêtre grandit à chaque passe
        # (elle recouvre alors tout le provisoire, qui ne fausse rien) jusqu'à
        # atteindre la taille de confirmation.
        debut = session.offset_seconds
        if session.received_seconds - debut < pas:
            return None
        fin_prevu = min(session.received_seconds, debut + taille)
    else:
        debut = max(0.0, session.offset_seconds - recouvrement)
        if session.received_seconds - session.offset_seconds < pas:
            return None
        fin_prevu = (session.received_seconds if flush
                     else session.offset_seconds + pas)

    longueur = fin_prevu - debut
    if longueur < _MIN_SEGMENT_SECONDS:
        return None
    try:
        payload = extract_segment(session.audio_path, debut, longueur, longueur)
    except TranscriptionError as exc:
        logger.debug("Dictée %s : fenêtre non extractible (%s)", session.id, exc)
        return None
    if payload.duration_seconds < _MIN_SEGMENT_SECONDS:
        return None
    fin = debut + payload.duration_seconds

    contexte = _contexte_tranche(session)
    resultat = transcribe_payload(payload, hints, contexte_precedent=contexte)
    texte = (resultat.get("transcript") or "").strip()
    moteur = (resultat.get("provider") or "", resultat.get("model") or "")
    words = resultat.get("words") or None
    _usage_fenetre(session, moteur, payload.duration_seconds)

    if texte:
        conf = {}
        if words:
            try:
                conf = med_grounding.conf_par_token(texte, words)
            except Exception:
                conf = {}
        if not session.window_text:
            # Croissance (premières fenêtres) : tout reste provisoire.
            _fusionner_fenetre(session, texte, conf, debut, fin, moteur)
            session.offset_seconds = fin
            session.save()
            return payload.duration_seconds
        if debut <= session.window_start + 0.01:
            # La fenêtre couvre ENTIÈREMENT le provisoire existant (phase de
            # croissance) : le provisoire devient simplement la nouvelle
            # lecture, sans alignement (rien à confirmer, rien à perdre).
            _fusionner_fenetre(session, texte, conf, debut, fin, moteur,
                               confirme=None)
            session.offset_seconds = fin
            session.save()
            return payload.duration_seconds
        confirme, m, ratio = _decoupe_fusion(
            session.window_text, texte,
            m_attendu=_recouvrement_attendu(session, texte, debut, fin))
        if m >= _FUSION_MIN_JETONS:
            _fusionner_fenetre(session, texte, conf, debut, fin, moteur,
                               confirme=confirme)
            if ratio < _FUSION_RATIO_FIABLE:
                # Frontière incertaine (pic faible) : plage à re-vérifier.
                session.verify_ranges.append((session.offset_seconds, fin))
                logger.info(
                    "Dictée %s : alignement marginal (%.2f) sur [%.1f-%.1f s] "
                    "— plage marquée pour vérification",
                    session.id, ratio, session.offset_seconds, fin,
                )
            session.offset_seconds = fin
            session.save()
            return payload.duration_seconds
        logger.warning(
            "Dictée %s : alignement de fenêtre raté (recouvrement < %d jetons) "
            "— fenêtre jetée, rattrapage de [%.1f-%.1f s] en mini-tranche",
            session.id, _FUSION_MIN_JETONS, session.offset_seconds, fin_prevu,
        )
        # Le curseur NE bouge pas : le rattrapage consomme l'audio neuf depuis
        # l'ancien curseur. La fenêtre jetée a coûté son audio (journalisé
        # ci-dessus) — le prix d'une frontière incertaine, rare et borné par
        # la soupape d'engagement du provisoire.
        if _rattraper_fenetre(session, hints):
            return payload.duration_seconds
        return None

    logger.info(
        "Dictée %s : fenêtre [%.1f-%.1f s] sans parole (reçu %.1f s)",
        session.id, debut, fin, session.received_seconds,
    )
    session.offset_seconds = fin
    session.save()
    return payload.duration_seconds


def _rattraper_fenetre(session: DictationSession, hints: str) -> bool:
    """Alignement impossible : ajoute l'audio neuf au provisoire en mini-tranche.

    Jamais d'engagement direct : le provisoire couvre déjà ``[window_start,
    offset]`` — une part engagée maintenant se retrouverait AVANT le
    provisoire dans l'ordre final. On étend donc le provisoire ; la prochaine
    fenêtre (recouvrement porté à ~45 s) retentera l'alignement. Retourne
    ``False`` si l'audio n'a pas pu être consommé (fin de fichier) —
    l'appelant stoppe la passe sans avancer le curseur. Une erreur de
    transport se propage : c'est un échec réel de transcription.
    """
    pas = _fenetre_pas()
    try:
        payload = extract_segment(session.audio_path, session.offset_seconds,
                                  pas, pas)
    except TranscriptionError as exc:
        logger.debug("Dictée %s : rattrapage non extractible (%s)", session.id, exc)
        return False
    if payload.duration_seconds < _MIN_SEGMENT_SECONDS:
        return False
    fin = session.offset_seconds + payload.duration_seconds
    resultat = transcribe_payload(
        payload, hints, contexte_precedent=_contexte_tranche(session))
    texte = (resultat.get("transcript") or "").strip()
    moteur = (resultat.get("provider") or "", resultat.get("model") or "")
    words = resultat.get("words") or None
    _usage_fenetre(session, moteur, payload.duration_seconds)
    if texte:
        # Plage rattrapée (frontière ratée) : à re-vérifier à la fin.
        session.verify_ranges.append((fin - payload.duration_seconds, fin))
        conf = {}
        if words:
            try:
                conf = med_grounding.conf_par_token(texte, words)
            except Exception:
                conf = {}
        if not session.window_text:
            session.window_start = fin - payload.duration_seconds
        session.window_text = (
            f"{session.window_text} {texte}".strip() if session.window_text else texte)
        for cle, valeur in conf.items():
            try:
                valeur = float(valeur)
            except (TypeError, ValueError):
                continue
            precedent = session.window_conf.get(cle)
            if precedent is None or valeur < float(precedent):
                session.window_conf[cle] = valeur
        logger.info(
            "Dictée %s : rattrapage +%d jetons (provisoire %d jetons, "
            "[%.1f-%.1f s])",
            session.id, len(texte.split()), len(session.window_text.split()),
            session.window_start, fin,
        )
    if len(session.window_text.split()) > _FUSION_ENGAGEMENT_MAX_JETONS:
        logger.warning(
            "Dictée %s : provisoire non alignable (%d jetons) — engagement forcé",
            session.id, len(session.window_text.split()),
        )
        _engager_provisoire(session)
    session.offset_seconds = fin
    session.save()
    return True


def _finir_fenetre(session: DictationSession, hints: str) -> Optional[float]:
    """Queue finale de la dictée (mode fenêtres) : UNE passe, plein contexte.

    À la fin, il ne reste que ``[offset, reçu]`` — quelques secondes à ~1,5
    fois ``pas``. La transcriter en UNE fenêtre (jamais en tranches de 10 s :
    la fin de dictée est la partie la plus sensible — plan, prescriptions) et
    engager le texte directement : le provisoire est déjà engagé, l'ordre des
    parts reste chronologique. Retourne la durée consommée, ``None`` quand
    tout l'audio est traité.
    """
    debut = session.offset_seconds
    longueur = min(session.received_seconds - debut, _fenetre_secondes())
    if longueur < _MIN_SEGMENT_SECONDS:
        return None
    try:
        payload = extract_segment(session.audio_path, debut, longueur, longueur)
    except TranscriptionError as exc:
        logger.debug("Dictée %s : queue non extractible (%s)", session.id, exc)
        return None
    if payload.duration_seconds < _MIN_SEGMENT_SECONDS:
        return None
    fin = debut + payload.duration_seconds
    resultat = transcribe_payload(
        payload, hints, contexte_precedent=_contexte_tranche(session))
    texte = (resultat.get("transcript") or "").strip()
    moteur = (resultat.get("provider") or "", resultat.get("model") or "")
    words = resultat.get("words") or None
    _usage_fenetre(session, moteur, payload.duration_seconds)
    session.offset_seconds = fin
    if texte:
        try:
            conf = med_grounding.conf_par_token(texte, words) if words else {}
        except Exception:
            conf = {}
        _store_part(session, texte, moteur,
                    duration_seconds=0.0, words=None)
        session.covered_ranges.append((debut, fin))
        if conf:
            try:
                with _lock_for_consultation(session.consultation_id), SessionLocal() as db:
                    consultation = db.get(Consultation, session.consultation_id)
                    if consultation is not None:
                        _merge_conf_into(consultation, conf)
                        db.commit()
            except Exception:
                logger.exception("Dictée %s : confiance de queue non fusionnée", session.id)
    session.save()
    return payload.duration_seconds


def _parts_pour_plage(session: DictationSession, r0: float, r1: float,
                      contexte_parts: int = 1) -> Optional[Tuple[int, int]]:
    """Indices (a, b) des parts couvrant la plage audio ``[r0, r1]`` (±contexte).

    ``None`` si parts et ``covered_ranges`` ne sont pas alignés (le fold de
    fin de dictée peut désaxer) — on renonce alors à cibler cette plage.
    """
    if not session.parts or len(session.parts) != len(session.covered_ranges):
        return None
    idx = [i for i, (c0, c1) in enumerate(session.covered_ranges)
           if c0 < r1 and c1 > r0]
    if not idx:
        return None
    return (max(0, min(idx) - contexte_parts),
            min(len(session.parts) - 1, max(idx) + contexte_parts))


def _reecouter_plage(session: DictationSession, a: int, b: int,
                     hints: str) -> bool:
    """Re-transcrit la plage des parts ``[a, b]`` en UNE passe, puis remplace.

    À l'inverse du découpage 10 s de ``_rewrite_boundary`` : l'audio part en
    UN SEUL appel (contexte maximal — c'est précisément ce que la dictée par
    tranches lui a manqué) et le remplacement porte les parts entières. Le
    texte d'origine est conservé si le service ne renvoie rien ou renvoie le
    même texte. Persistance + rediffusion ``transcript_correct`` incluses.
    """
    if a < 0 or b < a or b >= len(session.parts):
        return False
    if len(session.parts) != len(session.covered_ranges):
        return False
    debut = session.covered_ranges[a][0]
    end = session.covered_ranges[b][1]
    longueur = end - debut
    if longueur < _MIN_SEGMENT_SECONDS:
        return False
    try:
        payload = extract_segment(session.audio_path, debut, longueur, longueur)
    except TranscriptionError as exc:
        logger.debug("Dictée %s : plage [%d-%d] non extractible (%s)",
                     session.id, a, b, exc)
        return False
    resultat = transcribe_payload(
        payload, hints,
        contexte_precedent=" ".join(session.parts[:a]).strip() or None)
    texte = (resultat.get("transcript") or "").strip()
    moteur = (resultat.get("provider") or "", resultat.get("model") or "")
    words = resultat.get("words") or None
    _usage_fenetre(session, moteur, payload.duration_seconds)
    if not texte:
        return False
    texte = _norm_spaces(texte)
    ancien = _norm_spaces(" ".join(session.parts[a:b + 1]))
    if ancien == texte:
        logger.info(
            "Dictée %s : re-écoute [%.1f-%.1f s] identique — rien à changer",
            session.id, debut, end,
        )
        return False
    logger.info(
        "Dictée %s : re-écoute [%.1f-%.1f s] (%d jetons remplacés par %d) — "
        "ancien %r, nouveau %r",
        session.id, debut, end, len(ancien.split()), len(texte.split()),
        ancien[:80], texte[:80],
    )
    conf: dict = {}
    if words:
        try:
            conf = med_grounding.conf_par_token(texte, words)
        except Exception:
            conf = {}
    session.parts[a:b + 1] = [texte]
    session.parts_conf[a:b + 1] = [conf]
    session.covered_ranges[a:b + 1] = [(debut, end)]
    session.save()
    # Persistance durable + rediffusion aux onglets (mêmes canaux que la
    # stabilisation de frontière).
    try:
        owner = _session_owner(session)
        with _lock_for_consultation(session.consultation_id), SessionLocal() as db:
            consultation = db.get(Consultation, session.consultation_id)
            if consultation is not None:
                consultation.raw_transcript = " ".join(session.parts).strip()
                for cmap in session.parts_conf:
                    _merge_conf_into(consultation, cmap)
                consultation.updated_at = utcnow()
                db.commit()
    except Exception:
        logger.exception("Dictée %s : persistance de la re-écoute impossible", session.id)
    try:
        live.publish(_session_owner(session), "transcript_correct", {
            "consultation_id": session.consultation_id,
            "session_id": session.id,
            "parts": list(session.parts),
        })
    except Exception:
        logger.exception("Dictée %s : rediffusion de la re-écoute impossible", session.id)
    return True


def _verifier_residuel(session: DictationSession, hints: str) -> None:
    """Re-vérification RÉSIDUELLE de fin — jamais de retranscription complète.

    Au « Terminer » (mode fenêtres), ne sont re-écoutées que les zones qui
    ont raisonnablement pu se tromper :
      1. les parts portant un garble de médicament (terme dicté douteux,
         candidat phonétique) — priorité clinique ;
      2. les plages marquées pendant la dictée : frontières d'alignement
         marginales (pic de ratio < ``_FUSION_RATIO_FIABLE``) et plages
         rattrapées en mini-tranche ;
    chacune avec une part de contexte de chaque côté, en UNE passe à plein
    contexte (``_reecouter_plage``). Budget ``stt_verify_max_seconds`` secondes
    d'audio (0 = désactivé) ; les dépassements sont journalisés. Le texte
    d'origine reste en place si la re-écoute n'apporte rien.
    """
    cibles: List[Tuple[int, int]] = []

    def _cible(a: Optional[int], b: Optional[int]) -> None:
        if a is None or b is None or a > b:
            return
        if b >= len(session.parts):
            return
        for (fa, fb) in cibles:
            if not (b < fa or a > fb):
                return  # chevauche une cible déjà retenue
        cibles.append((a, b))

    def _pour_part(i: int) -> None:
        if 0 <= i < len(session.parts) \
                and len(session.parts) == len(session.covered_ranges):
            _cible(max(0, i - 1), min(len(session.parts) - 1, i + 1))

    # 1) Garbles de médicaments : la part qui porte le terme douteux.
    items = _grounding_meds.get(session.id) or []
    for item in items:
        jetons = [t for t in str(item.get("garble") or "").split() if t]
        if not jetons and item.get("source") == "phonetic":
            jetons = [t for t in str(item.get("name") or "").split() if t]
        cles = [_jeton_norm(j) for j in jetons]
        cles = [c for c in cles if c]
        if not cles:
            continue
        for i, part in enumerate(session.parts):
            mots = set(_jetons_norm(part))
            if all(c in mots for c in cles):
                _pour_part(i)
                break

    # 2) Plages marquées pendant la dictée (frontières marginales, rattrapage).
    for (r0, r1) in list(session.verify_ranges):
        if r1 - r0 < _SWEEP_MIN_REGION:
            continue
        plage = _parts_pour_plage(session, r0, r1)
        if plage is not None:
            _cible(*plage)

    if not cibles:
        session.verify_ranges = []
        return

    budget = _verifier_budget()
    restant = budget
    for (a, b) in cibles:
        if len(session.covered_ranges) < b + 1:
            continue
        duree = session.covered_ranges[b][1] - session.covered_ranges[a][0]
        if duree > restant:
            logger.warning(
                "Dictée %s : vérification des parts [%d-%d] hors budget "
                "(%.0f s > %.0f s restants) — retranscription manuelle si besoin",
                session.id, a, b, duree, restant,
            )
            continue
        restant -= duree
        logger.info(
            "Dictée %s : vérification résiduelle des parts %d-%d "
            "(%.0f s, %d s restants)",
            session.id, a, b, duree, restant,
        )
        try:
            _reecouter_plage(session, a, b, hints)
        except Exception:
            logger.exception("Dictée %s : re-écoute [%d-%d] impossible",
                             session.id, a, b)
    session.verify_ranges = []


def _verifier_budget() -> float:
    """Secondes d'audio maximaux dépensés par la vérification résiduelle."""
    try:
        return max(0.0, runtime_config.value_float("stt_verify_max_seconds", 60.0))
    except Exception:
        return 60.0


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
    real_duration = None
    if final:
        length = high
        real_duration = length
    elif flush:
        length, real_duration = find_cut_point(
            session.audio_path, session.offset_seconds,
            _FLUSH_MIN, _FLUSH_MIN, high)
    else:
        length, real_duration = find_cut_point(
            session.audio_path, session.offset_seconds, target, low, high)

    try:
        payload = extract_segment(
            session.audio_path, session.offset_seconds, length, real_duration)
    except TranscriptionError as exc:
        # Fin de fichier atteinte : ffmpeg ne produit plus rien.
        logger.debug("Dictée %s : plus de tranche extractible (%s)", session.id, exc)
        return None

    if payload.duration_seconds < _MIN_SEGMENT_SECONDS:
        return None

    if realtime_mode() == "sse" and flush:
        result = _transcribe_one_sse(session, payload, hints)
    else:
        # Contexte de la tranche précédente : la queue du transcript déjà
        # stable. Sans lui, chaque tranche ~10-11 s est transcrite isolément
        # et le modèle réinvente la fin de la phrase coupée (doublons de
        # frontière, note 40) — le rappel le garde sur la continuité.
        contexte = _contexte_tranche(session)
        result = transcribe_payload(
            payload, hints, contexte_precedent=contexte,
        )
    session.offset_seconds += payload.duration_seconds

    text = (result.get("transcript") or "").strip()
    if text:
        # Couverture : cette plage est transcrite — le filet de fin n'y
        # repassera pas. Une tranche muette reste volontairement non couverte
        # : elle est soit confirmée silencieuse, soit récupérée plus tard.
        session.covered_ranges.append(
            (session.offset_seconds - payload.duration_seconds, session.offset_seconds)
        )
    if not text:
        # Tranche muette : le curseur avance quand même, sinon la boucle
        # repasserait indéfiniment sur le même silence. Mais une fenêtre
        # « sans parole » décidée pendant la dictée est SUSPECTE : sur un
        # WebM encore en croissance, ffmpeg peut lire un cluster partiel
        # comme du silence alors que l'audio contient de la parole (course
        # lecture/écriture, observée sur l'instance de test — la moitié du
        # texte d'une dictée partait ainsi en fumée). On la marque pour
        # re-vérification à la passe suivante (``_reverify_silences``) : si
        # elle redevient « parlante » une fois l'audio arrivé, elle est
        # retranscrite et insérée à sa place, au lieu de rester perdue.
        if not final:
            session.unverified.append(
                (session.offset_seconds - payload.duration_seconds,
                 session.offset_seconds)
            )
        try:
            taille_brut = os.path.getsize(session.audio_path)
        except OSError:
            taille_brut = -1
        logger.info(
            "Dictée %s : tranche de %.1f s sans parole "
            "(reçu %.1f s, brut %d octets, %d fenêtre(s) suspecte(s))",
            session.id, payload.duration_seconds,
            session.received_seconds,
            taille_brut,
            len(session.unverified),
        )
    _store_part(session, text,
                (result.get("provider") or "", result.get("model") or ""),
                duration_seconds=payload.duration_seconds,
                words=result.get("words") or None)

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
            resultat = transcribe_payload(
                payload, hints, contexte_precedent=_contexte_tranche(session),
            )
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


# ---------------------------------------------------------------------------
# Récupération des fenêtres « sans parole » (WebM en croissance)
# ---------------------------------------------------------------------------
# Une tranche découpée dans un WebM encore en cours d'écriture peut être lue
# comme silencieuse alors que l'audio contient de la parole : ffmpeg se fie
# aux graines/bordures de clusters, et la course lecture/écriture sur le
# fichier brut rend la découpe non déterministe (observé sur l'instance de
# test : 32 fenêtres de 10 s perdues sur 644 s de dictée, ~3 700 caractères
# avalés en silence, puis retrouvés intégralement par la retranscription du
# fichier complet). Trois lignes de défense :
#   1. ``_reverify_silences`` — chaque fenêtre « sans parole » est re-vérifiée
#      à la passe suivante, une fois l'audio arrivé, via silencedetect local
#      (aucun appel STT) : parole présente → retranscription et insertion à
#      sa place chronologique (``_inserer_part``), silence réel → acceptée ;
#   2. ``_sweep_uncovered`` — le filet de fin re-transcrit au « Terminer »
#      tout ce qui reste non couvert, dans la limite de
#      ``stt_sweep_max_seconds`` ;
#   3. la retranscription manuelle, filet ultime inchangé.
# L'insertion se fait TOUJOURS à la position chronologique : une part
# récupérée rejoint le transcript entre les parts adjacentes, jamais en fin
# de liste (le filet historique appendait les trous à la fin, ce qui
# mélangeait l'ordre de la note).


def _inserer_part(session: DictationSession, texte: str, conf: dict,
                  start: float, end: float) -> None:
    """Insère une part récupérée à sa position CHRONOLOGIQUE.

    ``parts``, ``parts_conf`` et ``covered_ranges`` avancent en verrou (une
    part transcrite a toujours sa couverture, et une fenêtre muette aucune).
    La position = nombre de plages dont la fin précède le début de la fenêtre.
    """
    position = 0
    for (c0, c1) in session.covered_ranges:
        if c1 <= start + 0.05:
            position += 1
        else:
            break
    session.parts.insert(position, texte)
    session.parts_conf.insert(position, conf or {})
    session.covered_ranges.insert(position, (start, end))


def _persiste_et_diffuse(session: DictationSession) -> None:
    """Écrit le transcript assemblé (parts) en base et le re-diffuse."""
    try:
        with _lock_for_consultation(session.consultation_id), SessionLocal() as db:
            consultation = db.get(Consultation, session.consultation_id)
            if consultation is not None:
                consultation.raw_transcript = " ".join(session.parts).strip()
                for cmap in session.parts_conf:
                    _merge_conf_into(consultation, cmap)
                consultation.updated_at = utcnow()
                db.commit()
        live.publish(_session_owner(session), "transcript_correct", {
            "consultation_id": session.consultation_id,
            "session_id": session.id,
            "parts": list(session.parts),
        })
    except Exception:
        logger.exception("Persistance du transcript récupéré impossible")


def _range_a_parole(path: str, start: float, duree: float) -> bool:
    """Y a-t-il une vraie parole dans ``[start, start+duree[`` du brut ?

    Détection LOCALE (silencedetect ffmpeg, aucun appel STT — gratuit), sur
    la même sensibilité que le reste de la chaîne. Une région de parole ≥
    ``_SWEEP_MIN_REGION`` compte. Indécis (échec réseau/ffmpeg) → ``True`` :
    on préfère payer une re-transcription plutôt que de perdre du contenu.
    """
    if duree <= 0:
        return False
    if not stt._ffmpeg_available():
        return True
    try:
        log = _run_ffmpeg(
            [
                "-loglevel", "info", "-ss", f"{start:.3f}", "-t", f"{duree:.3f}",
                "-i", path, "-vn",
                "-af", (
                    f"silencedetect=noise={settings.stt_silence_threshold_db}dB"
                    f":duration=0.25"
                ),
                "-f", "null", "-",
            ],
            timeout=60,
        )
    except Exception:
        return True
    events = [(kind, float(value)) for kind, value in _SILENCE_RE.findall(log)]
    curseur = 0.0
    silence_en_cours = False
    for kind, value in events:
        if kind == "start":
            if value - curseur >= _SWEEP_MIN_REGION:
                return True
            silence_en_cours = True
        else:
            curseur = value
            silence_en_cours = False
    if not silence_en_cours and duree - curseur >= _SWEEP_MIN_REGION:
        return True
    return False


def _recuperer_span(session: DictationSession, start: float, end: float,
                    hints: str) -> bool:
    """Re-transcrit ``[start, end[`` et insère le texte à sa position.

    Retourne ``True`` si au moins un morceau a été récupéré. La passe de
    lecture est fraîche : le fichier a grandi depuis la fenêtre perdue, la
    gravure est stable (course lecture/écriture résolue).
    """
    low, target, high = _window()
    curseur = start
    recupere = False
    while end - curseur > 0.05:
        restant = end - curseur
        if restant <= high:
            longueur = restant
            real = restant
        else:
            longueur, real = find_cut_point(
                session.audio_path, curseur, target, low, high)
        try:
            payload = extract_segment(session.audio_path, curseur, longueur, real)
        except TranscriptionError:
            break
        if payload.duration_seconds < _MIN_SEGMENT_SECONDS:
            break
        curseur += payload.duration_seconds
        try:
            resultat = transcribe_payload(
                payload, hints, contexte_precedent=_contexte_tranche(session),
            )
        except TranscriptionError as exc:
            logger.warning(
                "Dictée %s : récupération [%.1f-%.1f s] écartée — %s",
                session.id, curseur - payload.duration_seconds, curseur, exc,
            )
            continue
        texte = (resultat.get("transcript") or "").strip()
        if not texte:
            continue
        conf = {}
        if resultat.get("words"):
            try:
                conf = med_grounding.conf_par_token(
                    texte, resultat.get("words") or [],
                )
            except Exception:
                conf = {}
        _inserer_part(session, texte, conf,
                      curseur - payload.duration_seconds, curseur)
        recupere = True
        logger.info(
            "Dictée %s : fenêtre [%.1f-%.1f s] récupérée (%d caractères)",
            session.id, curseur - payload.duration_seconds, curseur, len(texte),
        )
    return recupere


def _reverify_silences(session: DictationSession, hints: str) -> None:
    """Re-vérifie les fenêtres « sans parole » une fois l'audio arrivé.

    Appelée au début de chaque passe non finale (sous ``_lock_for``). Une
    fenêtre encore près du bord reçu est reportée (l'audio peut n'être pas
    encore là) ; les autres sont tranchées localement et gratuitement
    (silencedetect) : parole présente → retranscrite et insérée à sa place,
    silence réel → acceptée et oubliée. Les lointaines qui restent (never
    confirmé) sont laissées au filet de fin (``_sweep_uncovered``).
    """
    if not session.unverified:
        return
    marge = _SWEEP_OVERLAP_SECONDS + 1.0
    restants: List[Tuple[float, float]] = []
    a_persiste = False
    for start, end in session.unverified:
        if end > session.received_seconds - marge:
            restants.append((start, end))
            continue
        if _range_a_parole(session.audio_path, start, end - start):
            if _recuperer_span(session, start, end, hints):
                a_persiste = True
        else:
            logger.info(
                "Dictée %s : fenêtre [%.1f-%.1f s] confirmée silencieuse",
                session.id, start, end,
            )
    session.unverified = restants
    session.save()
    if a_persiste:
        _persiste_et_diffuse(session)
        maybe_schedule_grounding(session.id, session.username)


def _sweep_budget() -> float:
    """Secondes d'audio maximaux re-transcrits par le filet de fin.

    ``stt_sweep_max_seconds`` (0 = illimité). Une dictée à longues pauses peut
    révéler des dizaines de trous : le balayage complet retarderait le
    « Terminer » de minutes. Passé le budget, les trous restants sont signalés
    au journal — la retranscription manuelle (ou un second passage) les
    récupérera.
    """
    try:
        return max(0.0, runtime_config.value_float(
            "stt_sweep_max_seconds", 300.0,
        ))
    except Exception:
        return 300.0


def _sweep_uncovered(session: DictationSession, hints: str) -> None:
    """
    Filet de fin : re-transcrit les zones de parole non couvertes.

    Au « Terminer », on re-parcourt le fichier brut avec silencedetect
    (détection SERVEUR, indépendante du VAD du navigateur) et on compare aux
    plages déjà transcrites (``covered_ranges``). Tout trou — énoncé que le
    VAD a manqué, tranche qui avait échoué ou fenêtre lue comme silencieuse
    pendant la dictée — est re-extraite et re-transcrite, insérée à sa
    position chronologique (``_transcribe_region``), dans la limite de
    ``stt_sweep_budget``.

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
    budget = _sweep_budget()
    depense = 0.0
    trous = _subtract_ranges(regions, couvert)
    pour_suite = 0.0
    for start, end in trous:
        if end - start < _SWEEP_MIN_REGION:
            continue
        depense += end - start
        if budget and depense > budget:
            pour_suite += end - start
            continue
        logger.info(
            "Dictée %s : trou de %.1f s détecté en fin (%.1f-%.1f s), re-transcription",
            session.id, end - start, start, end,
        )
        _transcribe_region(session, start, end, hints)
    if pour_suite > 0:
        logger.warning(
            "Dictée %s : %d trou(s) hors budget du filet (%.0f s d'audio) — "
            "retranscrivez manuellement ou relancez la passe",
            session.id, pour_suite,
        )


def _transcribe_region(session: DictationSession, start: float, end: float, hints: str) -> None:
    """Transcrit l'intervalle [start, end[ du fichier brut, en découpant si
    nécessaire (un trou long repasse par les coupes au silence, comme le
    découpage en cours de dictée).

    Chaque morceau est INSÉRÉ à sa position chronologique (``_inserer_part``),
    jamais appendé : un trou au milieu de la dictée doit retrouver sa place
    dans la note, pas sa fin.
    """
    low, target, high = _window()
    curseur = start
    while end - curseur > 0.05:
        restant = end - curseur
        # Dernier morceau : entier, sans coupe donc sans risque.
        if restant <= high:
            longueur = restant
            real = restant
        else:
            longueur, real = find_cut_point(
                session.audio_path, curseur, target, low, high)
        try:
            payload = extract_segment(session.audio_path, curseur, longueur, real)
        except TranscriptionError:
            break
        if payload.duration_seconds < _MIN_SEGMENT_SECONDS:
            break
        curseur += payload.duration_seconds
        try:
            result = transcribe_payload(
                payload, hints, contexte_precedent=_contexte_tranche(session),
            )
        except TranscriptionError as exc:
            logger.warning(
                "Dictée %s : trou [%.1f-%.1f s] écarté — %s",
                session.id, curseur - payload.duration_seconds, curseur, exc,
            )
            continue
        text = (result.get("transcript") or "").strip()
        if not text:
            continue
        conf = {}
        if result.get("words"):
            try:
                conf = med_grounding.conf_par_token(
                    text, result.get("words") or [],
                )
            except Exception:
                conf = {}
        _inserer_part(session, text, conf,
                      curseur - payload.duration_seconds, curseur)
    session.save()
    _persiste_et_diffuse(session)


def process_pending(session_id: str, username: str, final: bool = False) -> DictationSession:
    """
    Découpe et transcrit tout ce qui peut l'être.

    Appelée en tâche de fond après réception d'un fragment, et une dernière
    fois — avec ``final`` — quand le médecin appuie sur « Terminer ».
    """
    with _lock_for(session_id):
        session = load_session(session_id, username)

        # Vérifier tôt la disponibilité du STT (sonde légère, cache court) :
        # le navigateur est prévenu dès l'initiation de la dictée si l'endpoint
        # est injoignable, plutôt qu'après un backlog de segments.
        _should_transcribe(session)
        # Un échec ci-dessus doit marquer la session (persisté) pour que le
        # premier get renvoie stt_available=False.
        session.save()

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
        fenetres = fenetrage_actif()

        # Les fenêtres « sans parole » de la passe précédente sont re-vérifiées
        # maintenant que l'audio a grandi (voix récupérée et insérée à sa
        # place, silence confirmé accepté) — avant de découper la suite.
        # EN MODE FENÊTRES, la re-vérification est écartée : une insertion
        # positionnelle au milieu d'un provisoire non encore engagé briserait
        # l'ordre des parts ; les plages muettes sont couvertes par le filet
        # de fin (``_sweep_uncovered``), qui travaille sur le fichier complet.
        if not final and not fenetres:
            _reverify_silences(session, hints)

        # Fin d'énoncé signalée par le navigateur : la première tranche de
        # cette passe part immédiatement, coupée au premier silence après un
        # minimum de parole. Le drapeau n'est consommé qu'une fois la tranche
        # réellement transcrite — s'il n'y a pas encore assez d'audio reçu
        # (le fragment portant la fin de l'énoncé n'est pas arrivé), il
        # reste posé pour la prochaine passe.
        if final:
            # Le provisoire rejoint les parts AVANT la passe finale : la
            # queue restante (``[offset, reçu]``) est ensuite transcrite par
            # le chemin historique, dans l'ordre.
            _engager_provisoire(session)
        flush = session.flush_requested

        while True:
            if not final:
                if flush:
                    if not should_flush(session):
                        break
                elif fenetres:
                    if not should_process_fenetres(session):
                        break
                elif not should_process(session):
                    break
            try:
                if not final and fenetres:
                    duration = _transcribe_fenetre(session, hints, flush)
                    if duration is not None and flush:
                        flush = False
                        session.flush_requested = False
                        session.save()
                elif final and fenetres:
                    # Queue finale en UNE passe à plein contexte (la partie
                    # la plus sensible — plan et prescriptions — ne passe pas
                    # par le découpage 10 s).
                    duration = _finir_fenetre(session, hints)
                else:
                    duration = _transcribe_one(session, hints, final, flush)
            except TranscriptionError as exc:
                session.last_error = str(exc)
                session.save()
                logger.warning("Dictée %s : transcription refusée — %s", session.id, exc)
                # Échec RÉEL de transport = le STT ne répond pas : c'est ICI
                # qu'on prévient (pas sur une sonde). Un refus métier (aucune
                # parole, clé, 4xx) ne déclenche pas l'avis.
                if _is_transport_error(str(exc)):
                    _signal_stt_unavailable(session, str(exc))
                raise
            if duration is None:
                break
            # Une tranche plus courte que demandé signifie qu'on a atteint la
            # fin du fichier : inutile de refaire un tour pour rien.
            if final and duration < high - 0.5:
                break
            if flush and not fenetres:
                # Un énoncé suffit : le cadencement batch reprend ensuite. Le
                # drapeau est persisté — ``_transcribe_one`` a déjà sauvé la
                # session AVANT ce point, il faut repersister l'effacement.
                flush = False
                session.flush_requested = False
                session.save()

        if final:
            # Vérification résiduelle AVANT le filet : elle réécrit des parts
            # et leur couverture ; le filet ne couvre ensuite que les trous
            # restants. Zéro retranscription complète par construction.
            if fenetres:
                _verifier_residuel(session, hints)
            if runtime_config.value("stt_vad_finish_sweep") != "false":
                _sweep_uncovered(session, hints)
            # Redites de la reconnaissance vocale retirées du transcript final
            # avant persistance du grounding (artefact STT : même phrase
            # entendue deux fois à la suite, cf. ``_dedupe_adjacent``).
            _assainir_doublons(session)
            _finalise(session)
            schedule_final_grounding(session.id, session.username,
                                     session.consultation_id)
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
                    duration_seconds=session.offset_seconds,
                    words=result.get("words") or None)
    session.status = "finished"
    session.save()
