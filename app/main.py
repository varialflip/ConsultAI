"""
main.py — API FastAPI de ConsultAI.
====================================

Routes
------
  Public (non authentifié) :
    GET    /healthz                      sonde de santé Docker

  Protégé (authentification OIDC, voir app/auth.py) :
    GET    /                             interface web
    GET    /api/me                       identité de l'utilisateur courant
    PUT    /api/me/language              langue de l'utilisateur courant
    GET    /api/config                   configuration visible côté client
    GET    /api/models                   modèles du fournisseur LLM accessibles (diagnostic)
    GET    /api/stt/models                modèles de transcription accessibles (diagnostic)

    GET    /api/templates                liste des gabarits (partagés + les miens)
    POST   /api/templates                création d'un gabarit personnel
    GET    /api/templates/{id}           détail
    PUT    /api/templates/{id}           modification (propriétaire / administrateur)
    POST   /api/templates/{id}/duplicate copie personnelle (tout utilisateur)
    DELETE /api/templates/{id}           suppression (propriétaire / administrateur)

    POST   /api/transcribe               audio → texte (Google STT), en un bloc
    POST   /api/generate                 texte + gabarit → note (Gemini)

    GET    /api/dictation                dictées inachevées de l'utilisateur
    POST   /api/dictation                ouvre une dictée par segments
    POST   /api/dictation/{id}/chunk     fragment audio (~5 s)
    GET    /api/dictation/{id}           avancement et texte déjà transcrit
    POST   /api/dictation/{id}/finish    conclut et transcrit le reliquat
    POST   /api/dictation/{id}/cancel    abandonne sans transcrire

    GET    /api/consultations/{id}/recordings   enregistrements du brouillon
    GET    /api/recordings/{id}/audio           lecture du fichier audio
    DELETE /api/recordings/{id}                 suppression

    GET    /api/admin/settings           réglages courants (administrateur)
    PUT    /api/admin/settings           modification      (administrateur)

    GET    /api/consultations            brouillons de l'utilisateur
    POST   /api/consultations            création
    GET    /api/consultations/{id}       détail
    PATCH  /api/consultations/{id}       sauvegarde automatique (partielle)
    DELETE /api/consultations/{id}       suppression
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from markupsafe import Markup
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse
from app import __version__

from app import audio_cache, backup, changelog, dictation, i18n, live, llm, med_grounding, oidc, preferences, recordings, runtime_config, scheduler, stt, usage
from app import users as users_service
from app.auth import (
    AuthMiddleware,
    Principal,
    clear_identity,
    current_user,
    require_admin,
    require_template_admin,
    store_identity,
)
from app.config import configure_logging, settings
from app.database import (
    Consultation,
    PricingRate,
    SchedulerState,
    SessionLocal,
    Recording,
    Template as TemplateModel,
    _iso,
    get_db,
    init_db,
    utcnow,
)
from app.dictation import DictationError, SequenceMismatch, SessionNotFound, _merge_conf_into
from app.llm import GenerationError, extract_metadata, list_available_models
from app.stt import TranscriptionError, list_available_stt_models, transcribe

configure_logging()
logger = logging.getLogger("consultai")

BASE_DIR = Path(__file__).resolve().parent

#: Longueur maximale du jeton d'identité conservé en session. La session est un
#: témoin signé, et un témoin dépassant ~4 ko est écarté sans un mot par le
#: navigateur : on garde de la marge pour le reste de son contenu et pour la
#: signature.
_MAX_SESSION_ID_TOKEN = 2800


# ---------------------------------------------------------------------------
# Cycle de vie de l'application
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Démarrage de ConsultAI v%s", __version__)
    init_db()

    # Un sentinel encore présent ici signifie que CE démarrage est le
    # redémarrage demandé après une restauration (voir app/backup.py et le
    # middleware _block_writes_after_restore ci-dessous) : le processus qui a
    # remplacé les fichiers est mort, un nouveau vient de prendre le relais
    # avec un moteur SQLAlchemy neuf sur les données restaurées. Le blocage a
    # rempli son rôle, il doit disparaître — sinon plus aucune écriture ne
    # serait jamais possible après la toute première restauration.
    cleared = backup.clear_restart_sentinel()
    if cleared:
        logger.info("Redémarrage post-restauration détecté (sauvegarde du %s) — écritures débloquées", cleared)

    dictation.purge_expired()
    with SessionLocal() as _purge_db:
        purge_expired_consultations(_purge_db)
    # Amorce le compteur de notes produites (NotesDaily) depuis les dossiers
    # encore en base — une seule fois, la table reste ensuite alimentée à la
    # première génération de chaque consultation.
    usage.backfill_notes_daily()
    # Capturée une fois : c'est elle que live.publish() utilise pour remettre
    # un évènement en main propre depuis n'importe quel fil d'exécution
    # (threadpool inclus — voir app/live.py).
    live.bind_loop(asyncio.get_running_loop())

    # Les erreurs de configuration sont la première cause de « ça ne marche
    # pas » sur un NAS : on les affiche bien en évidence dans les journaux.
    for warning in settings.warnings():
        logger.warning("CONFIGURATION : %s", warning)

    if settings.auth_disabled:
        logger.warning(
            "Authentification DÉSACTIVÉE (AUTH_DISABLED) — usager « %s », "
            "administrateur. À n'utiliser qu'en développement local.",
            settings.dev_user,
        )
    else:
        with SessionLocal() as _db:
            _comptes = users_service.count_users(_db)
        logger.info(
            "Authentification : OIDC chez %s | retour %s | %d compte(s) connu(s) | "
            "inscription automatique : %s%s",
            settings.oidc_provider_url or "(non configuré)",
            settings.effective_redirect_uri or "(non configurée)",
            _comptes,
            "oui" if users_service.allow_signup() else "non",
            f" | second fournisseur : {settings.oidc_alt_provider_url}"
            if settings.oidc_alt_provider_url else "",
        )
        if _comptes == 0:
            logger.warning(
                "Aucun compte en base : le PREMIER usager qui se connectera "
                "deviendra administrateur."
            )
    # Les fournisseurs effectifs viennent du panneau d'administration, pas du
    # .env : c'est ce couple-là qu'il faut voir dans les journaux quand une
    # génération échoue.
    # Hors requête HTTP, la langue est celle du .env : les préférences par
    # usager n'ont de sens qu'une fois qu'on sait de qui il s'agit. C'est donc
    # le défaut de l'installation qui est journalisé ici, avec le code de langue
    # qui en découle — et non le contenu éventuel de STT_LANGUAGE_CODE.
    _stt_provider = runtime_config.value("stt_provider")
    logger.info(
        "Modèle : %s / %s | Reconnaissance vocale : %s (%s) | Langue : %s",
        runtime_config.value("llm_provider"), llm.active_model(),
        _stt_provider, runtime_config.stt_language(_stt_provider) or "détection auto",
        runtime_config.language(),
    )
    os.makedirs(settings.audio_dir, exist_ok=True)

    scheduler.register_daily_job("backup", backup.run_scheduled_backup)
    scheduler.register_daily_job("usage_compaction", usage.compact_old_events)
    _scheduler_task = asyncio.create_task(scheduler.run_daily_loop())

    yield

    _scheduler_task.cancel()
    logger.info("Arrêt de ConsultAI")


app = FastAPI(
    title="ConsultAI",
    description="Dictée et mise en forme de consultations cliniques (fr / en)",
    version=__version__,
    lifespan=lifespan,
    # La documentation interactive reste accessible aux utilisateurs
    # authentifiés ; mettez ces valeurs à None pour la désactiver.
    docs_url="/api/docs",
    redoc_url=None,
)

# ⚠️ Le middleware protège TOUT sauf les chemins listés ici.
#
# Les ressources PWA sont publiques à dessein : le navigateur récupère le
# manifeste et les icônes SANS cookies (credentials omis par défaut), et met
# à jour le service worker hors du contexte d'une page. Protégées, elles
# renverraient 403 et l'installation sur l'écran d'accueil échouerait
# silencieusement. Aucune de ces ressources ne contient de donnée clinique :
# ce sont le nom de l'application, ses couleurs, ses icônes et du code de
# mise en cache. Toutes les données restent derrière l'authentification.
app.add_middleware(
    AuthMiddleware,
    public_paths={
        "/healthz",
        "/sw.js",
        "/static/manifest.webmanifest",
        # Le flux de connexion doit rester atteignable sans être connecté :
        # sans cela, la redirection vers la page de connexion boucle sur
        # elle-même.
        "/auth/login",
        "/auth/callback",
        "/auth/logout",
    },
    public_prefixes=("/static/icons/",),
)

# ⚠️ ORDRE DES MIDDLEWARES
#
# Starlette exécute les middlewares dans l'ordre INVERSE de leur ajout : celui
# déclaré en dernier s'exécute en premier. La session doit donc être ajoutée
# APRÈS l'authentification pour être disponible AVANT elle — sans quoi
# `request.session` lèverait au moment où le middleware d'authentification
# cherche à la lire.
#
# La clé de signature : si SESSION_SECRET est absente, on en tire une au hasard
# pour que l'application démarre quand même. Conséquence assumée et signalée au
# démarrage : tout le monde est déconnecté à chaque redémarrage.
_session_secret = settings.session_secret or oidc.new_secret()
if not settings.session_secret:
    logger.warning(
        "SESSION_SECRET absente : clé de session aléatoire. Les sessions ne "
        "survivront pas à un redémarrage du conteneur."
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    session_cookie="consultai_session",
    # Le témoin signé doit rester valide le temps de la PLUS LONGUE des deux
    # durées (« Rester connecté ») : la signature ne doit pas expirer avant la
    # session qu'elle garantit. La durée réelle, elle, est appliquée côté
    # serveur par l'échéance portée dans la session (auth.session_identity) —
    # sans cela, un témoin de session « normale » resterait rejouable pendant
    # la durée étendue.
    max_age=max(
        settings.session_max_age_seconds,
        settings.session_stay_max_age_seconds,
    ),
    # « lax » et non « strict » : le fournisseur d'identité nous renvoie par une
    # navigation venue d'un autre site, et « strict » ferait perdre le témoin
    # portant l'état du flux — la connexion échouerait systématiquement.
    same_site="lax",
    https_only=settings.session_https_only,
)


@app.middleware("http")
async def _block_writes_after_restore(request: Request, call_next):
    """
    Après une restauration (voir app/backup.py:restore_backup), le fichier
    SQLite sur disque a été remplacé sous les pieds du moteur SQLAlchemy en
    cours d'exécution — un redémarrage manuel du conteneur est requis avant
    toute nouvelle écriture. Ajoutée en dernier (donc exécutée en premier,
    avant même la session/l'authentification) : aucune requête d'écriture ne
    doit atteindre une route le temps que ce sentinel existe.
    """
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
        pending = backup.restore_required()
        if pending is not None:
            return JSONResponse(
                status_code=503,
                content={"detail": _t("err.restart_required"), "restore": pending},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Traduction
# ---------------------------------------------------------------------------
def _t(key: str, **fields) -> str:
    """
    Texte dans la langue effective de l'application.

    Les messages d'erreur renvoyés par l'API passent par ici : ils s'affichent
    tels quels dans le navigateur (champ « detail »), et ils doivent donc être
    dans la langue de l'écran que le médecin regarde.
    """
    return i18n.t(key, runtime_config.language(), **fields)


def _template_translator(language: str):
    """
    Traducteur destiné au gabarit Jinja : ``{{ t('cle') }}``.

    Une fonction nommée plutôt qu'une lambda, et des paramètres positionnels
    seulement, pour la même raison que dans ``i18n.t`` : un champ de traduction
    appelé « key » ou « language » ne doit pas pouvoir heurter la signature.
    """
    def traduire(key, /, **fields) -> str:
        return i18n.t(key, language, **fields)

    return traduire


def _apply_grounding(db: Session, consultation, origin_tab: str = "") -> list:
    """
    Liste pointée des médicaments d'une transcription (texte complet), persistée
    dans ``consultation.med_grounding_json`` et diffusée aux onglets suiveurs.
    Comprend les noms normalisés et les candidats phonétiques à confirmer
    (``source: "phonetic"``). Déterministe et local.
    Retourne les items pour la réponse HTTP locale.
    """
    if consultation is None:
        return []
    text = (consultation.raw_transcript or "").strip()
    if not text:
        return []
    # Même confiance qu'à la génération : le contenu de l'onglet Validation
    # est EXACTEMENT ce qui sera envoyé au LLM (med_hints), pas une seconde
    # lecture divergente. Sans conf (pas de transcript_conf), ``conf=None``
    # désactive simplement le gate « prose sûre » — comportement historique.
    try:
        conf_map = json.loads(consultation.transcript_conf) if consultation.transcript_conf else {}
    except (ValueError, TypeError):
        conf_map = {}
    try:
        items = med_grounding.extract_validation_items(text, conf=conf_map or None)
    except Exception:
        logger.exception("Grounding méds impossible (consultation %s)", consultation.id)
        return []
    consultation.med_grounding_json = json.dumps(items, ensure_ascii=False)
    db.commit()
    live.publish(consultation.owner, "med_grounding_result", {
        "consultation_id": consultation.id,
        "origin_tab": origin_tab,
        "items": items,
    })
    return items


def _med_grounding_on() -> bool:
    try:
        if not med_grounding.is_available():
            return False
        return runtime_config.value("dictation_grounding") != "false"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Manifeste PWA — servi dynamiquement pour suivre la langue
# ---------------------------------------------------------------------------
# Déclaré AVANT le montage de /static : Starlette parcourt les routes dans
# l'ordre d'enregistrement, et le montage attraperait sinon cette adresse. Le
# chemin ne change pas, ce qui évite de retoucher la liste des chemins publics
# d'AuthMiddleware (public_paths, plus haut).
@app.get("/static/manifest.webmanifest", include_in_schema=False)
async def web_manifest() -> JSONResponse:
    langue = runtime_config.language()
    return JSONResponse(
        {
            "name": settings.app_title,
            "short_name": settings.app_title,
            "description": i18n.t("app.description", langue),
            "lang": i18n.stt_language_code(langue, "google"),
            "dir": "ltr",
            "start_url": "/",
            "scope": "/",
            # « id » identifie l'application installée : l'omettre ou le
            # changer ferait voir une AUTRE application au navigateur, qui
            # proposerait une seconde installation à côté de la première.
            "id": "/",
            "display": "standalone",
            "display_override": ["standalone", "minimal-ui"],
            "orientation": "portrait",
            "background_color": "#f1f5f9",
            "theme_color": "#ffffff",
            "categories": ["medical", "health", "productivity"],
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "maskable"},
            ],
            "shortcuts": [
                {
                    "name": i18n.t("header.new_title", langue),
                    "short_name": i18n.t("header.new", langue),
                    "url": "/?nouvelle=1",
                    "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}],
                }
            ],
        },
        media_type="application/manifest+json",
        # Le manifeste dépend d'un réglage : le mettre en cache longtemps
        # laisserait l'écran d'accueil dans l'ancienne langue.
        headers={"Cache-Control": "no-cache, max-age=0"},
    )


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
jinja_templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _title_ai_italic(title: str) -> Markup:
    """Met « AI » du titre en italique : Dict<span class="ai">AI</span>.ca."""
    return Markup("<span class=\"ai\">AI</span>".join(esc_html(p) for p in title.split("AI")))


def _md_inline(value: str) -> Markup:
    """Rend le sous-ensemble de Markdown utilisé dans le CHANGELOG
    (l'échappement précède la conversion : aucun contenu n'est injectable).
    L'inline code est traité en premier pour protéger son contenu du gras."""
    texte = esc_html(value)
    texte = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", texte)
    texte = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", texte)
    return Markup(texte)


jinja_templates.env.filters["ai_italic"] = _title_ai_italic
jinja_templates.env.filters["md_inline"] = _md_inline


# ---------------------------------------------------------------------------
# Schémas d'entrée/sortie
# ---------------------------------------------------------------------------
class TemplateIn(BaseModel):
    """Corps de requête pour la création/modification d'un gabarit."""

    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    system_instructions: str = Field(..., min_length=1)
    layout_format: str = Field(..., min_length=1)
    phrase_hints: str = Field("", max_length=20000)
    sort_order: int = Field(100, ge=0, le=9999)
    #: « fr » ou « en ». Décide de TOUTE la chaîne pour ce gabarit : consignes,
    #: consigne générale employée, code de langue du service vocal, langue de
    #: rédaction. Voir database.Template.language.
    language: str = Field("fr", max_length=8)

    @field_validator("language")
    @classmethod
    def _langue(cls, value: str) -> str:
        # Une langue inconnue est refusée plutôt que ramenée au français : elle
        # ne peut venir que d'un appel fabriqué, et la corriger en silence
        # produirait une note dans une langue que personne n'a demandée.
        code = (value or "fr").strip().lower()
        if code not in dict(i18n.LANGUAGES):
            raise ValueError(f"Langue inconnue : {value}")
        return code

    @field_validator("name", "description", "system_instructions", "layout_format", "phrase_hints")
    @classmethod
    def _strip(cls, value: str) -> str:
        return (value or "").strip()


class ConsultationIn(BaseModel):
    """Création d'un brouillon."""

    title: str = Field("Consultation sans titre", max_length=300)
    reason: str = Field("", max_length=300)
    template_id: Optional[int] = None
    raw_transcript: str = ""


class ConsultationPatch(BaseModel):
    """Sauvegarde automatique : tous les champs sont optionnels."""

    title: Optional[str] = Field(None, max_length=300)
    reason: Optional[str] = Field(None, max_length=300)
    requester: Optional[str] = Field(None, max_length=200)
    accompanied_by: Optional[str] = Field(None, max_length=200)
    consultation_date: Optional[str] = Field(None, max_length=40)
    template_id: Optional[int] = None
    raw_transcript: Optional[str] = None
    generated_markdown: Optional[str] = None
    edited_markdown: Optional[str] = None
    status: Optional[str] = Field(None, max_length=30)
    audio_seconds: Optional[int] = Field(None, ge=0)


class GenerateIn(BaseModel):
    """Demande de mise en forme par Gemini."""

    template_id: int
    # Pas de ``min_length`` : une transcription vide est valide quand le
    # fournisseur actif contourne le STT (audio envoyé seul) — c'est
    # ``generate_note`` qui tranche, seule source de vérité sur cette règle.
    transcript: str = ""
    consultation_id: Optional[int] = None
    # Métadonnées de la consultation (hors identité du patient, volontairement
    # non collectée). Le médecin peut les saisir avant la dictée, mais le cas
    # normal est qu'elles restent vides ici et soient relues dans la dictée
    # après la mise en forme (voir plus bas).
    reason: str = Field("", max_length=300)
    consultation_date: str = Field("", max_length=60)
    requester: str = Field("", max_length=200)
    accompanied_by: str = Field("", max_length=200)
    extra_instructions: str = Field("", max_length=4000)
    # Bascule ponctuelle vers le modèle « pro » pour une dictée difficile.
    use_pro: bool = False
    # Jeton propre à CETTE demande de génération, généré par l'onglet qui
    # clique sur « Mettre en forme ». Repris tel quel dans les évènements
    # ``generation_chunk`` diffusés en direct, il permet à l'onglet émetteur de
    # ne s'appliquer que SES propres morceaux et d'ignorer ceux d'un flux
    # supplanté (voir _generate_and_publish et connectLiveEvents côté JS).
    generation_token: str = ""
    # « Validation » : après la note, auditer audio↔note (listes d'écarts).
    # Préférence par usager, reflétée par la bascule à côté du bouton.
    second_pass: bool = False


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
def _get_owned_consultation(db: Session, consultation_id: int, user: Principal) -> Consultation:
    """
    Récupère un brouillon en vérifiant qu'il appartient bien à l'utilisateur.

    On renvoie 404 (et non 403) pour un document appartenant à un autre
    médecin : cela évite de révéler l'existence du dossier.
    """
    consultation = db.get(Consultation, consultation_id)
    if consultation is None or consultation.owner != user.owner_key:
        raise HTTPException(status_code=404, detail=_t("err.consultation_not_found"))
    return consultation


def _build_context_lines(payload: GenerateIn) -> List[str]:
    """
    Transforme les métadonnées déjà connues en lignes lisibles pour le prompt.

    Elles ne sont qu'une aide : ce qui n'est pas fourni ici est de toute façon
    dicté, et le modèle le tire de la transcription pour remplir les champs
    entre accolades du gabarit.
    """
    date_value = payload.consultation_date.strip() or datetime.now().strftime("%Y-%m-%d")
    lines = [f"Date de la consultation : {date_value}"]
    if payload.reason.strip():
        lines.append(f"Raison de consultation : {payload.reason.strip()}")
    if payload.requester.strip():
        lines.append(f"Demande de : {payload.requester.strip()}")
    if payload.accompanied_by.strip():
        lines.append(f"Accompagné de : {payload.accompanied_by.strip()}")
    return lines


def _concat_audio(chemins: List[str]) -> Optional[Tuple[bytes, float]]:
    """
    Fusionne plusieurs enregistrements en un seul OGG/Opus, dans l'ordre.

    Une consultation dictée en plusieurs parties garde un enregistrement par
    partie. Sans fusion, seul le dernier atteindrait le modèle et le début de
    la consultation serait perdu. Le réencodage est volontaire : les parties
    peuvent venir de navigateurs différents (WebM/Opus de Chrome, MP4/AAC de
    Safari) et le concaténateur brut d'ffmpeg exigerait des codecs identiques.

    Retourne ``(contenu, durée)``, ou ``None`` si la fusion échoue — l'appelant
    renonce alors à l'audio, comme s'il n'y avait aucun enregistrement.
    """
    if not chemins:
        return None
    workdir = tempfile.mkdtemp(prefix="consultai-concat-")
    try:
        liste = os.path.join(workdir, "liste.txt")
        with open(liste, "w", encoding="utf-8") as handle:
            for chemin in chemins:
                # Échappement pour le format « file '…' » du démosélecteur.
                handle.write(f"file '{chemin.replace(chr(39), chr(92) + chr(39))}'\n")
        sortie = os.path.join(workdir, "concat.ogg")
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "concat", "-safe", "0", "-i", liste,
                "-vn", "-map_metadata", "-1",
                "-ac", "1", "-ar", "48000",
                "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
                "-f", "ogg", "-y", sortie,
            ],
            capture_output=True, timeout=300, check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", "replace")
            logger.warning("Fusion des enregistrements impossible : %s", stderr[-400:])
            return None
        with open(sortie, "rb") as handle:
            contenu = handle.read()
        duree = stt.probe_duration(sortie)
        if not contenu or duree <= 0:
            logger.warning(
                "Fusion des enregistrements : sortie vide (%d octets, %.1f s)",
                len(contenu), duree,
            )
            return None
        logger.info(
            "Enregistrements fusionnés : %d piste(s), %.1f s", len(chemins), duree,
        )
        return contenu, duree
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Fusion des enregistrements impossible : %s", exc)
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _prepare_audio_for_generation(
    db: Session, consultation_id: int, max_minutes: float = 20.0,
    audio_format: str = "ogg",
) -> Optional[Tuple[bytes, str]]:
    """
    Extrait audio (silences plafonnés) à joindre à la génération, ou ``None``.

    Toutes les pistes conservées sont envoyées : lorsqu'une consultation est
    dictée en plusieurs parties, chaque partie a produit son propre
    enregistrement, et toutes doivent atteindre le modèle — pas seulement la
    dernière.

    ``audio_format`` (``ogg`` / ``mp3`` / ``wav``) décide du conteneur de
    sortie. Le plafonnement des silences (bascule globale ``stt_trim_silence``)
    s'applique à toutes les pistes et à tous les formats ; quand il est éteint
    ou en échec, l'audio est joint tel quel (``transcode_to``).

    Best-effort à dessein : aucun enregistrement, fusion ou rognage impossible,
    ou dictée trop longue (``<fournisseur>_send_audio_max_minutes``)
    retombent sur ``None`` — la note se génère alors comme avant, sur la seule
    transcription. Ce n'est jamais une raison de faire échouer la génération,
    mais chaque repli est journalisé : silencieux pour l'usager, visible dans
    les journaux, sinon un audio manquant est indiscernable d'un bogue.
    """
    fmt = (audio_format or "ogg").strip().lower()

    def _traiter(source: str, provenance: str) -> Optional[Tuple[bytes, str]]:
        """Prépare l'audio de ``source`` dans le format demandé."""
        plafond = max_minutes * 60
        contenu, mime, duree = None, None, None
        rogné = False

        # Plafonnement des silences — bascule GLOBALE ``stt_trim_silence``,
        # appliquée à toutes les pistes et à tous les formats (ogg/mp3/wav),
        # y compris l'audio envoyé au modèle de langage.
        # ``cap_silence_to`` renvoie ``None`` quand la bascule est éteinte ou
        # que le filtre échoue : on envoie alors l'audio tel quel (transcodé).
        try:
            capped = stt.cap_silence_to(source, fmt)
        except OSError as exc:
            logger.warning(
                "Audio non joint (consultation %s, %s) : %s",
                consultation_id, provenance, exc,
            )
            capped = None
        if capped is not None:
            contenu, mime, duree = capped
            rogné = True
        else:
            transcodé = stt.transcode_to(source, fmt)
            if transcodé is None:
                logger.warning(
                    "Audio non joint (consultation %s, %s) : transcodage %s impossible",
                    consultation_id, provenance, fmt,
                )
                return None
            contenu, mime, duree = transcodé

        if duree <= 0 or duree > plafond:
            logger.info(
                "Audio non joint (consultation %s, %s) : %.1f s hors bornes "
                "(plafond %.0f s)",
                consultation_id, provenance, duree, plafond,
            )
            return None
        logger.info(
            "Audio joint (consultation %s, %s) : %.1f s (%s)%s",
            consultation_id, provenance, duree, mime,
            ", silences plafonnés" if rogné else "",
        )
        return contenu, mime

    pistes = recordings.for_consultation(db, consultation_id)
    if not pistes:
        logger.info(
            "Audio non joint (consultation %s) : aucun enregistrement conservé",
            consultation_id,
        )
        return None

    # Voie rapide : artefacts déjà préparés par le cache (points de contrôle
    # pendant la dictée, préparation à la conclusion, ou une génération
    # antérieure). Une piste manquante est lancée puis attendue bornée ;
    # au-delà — ou en échec — la voie historique reprend ci-dessous, à
    # l'identique, et remplit le cache pour la fois suivante.
    clefs = [
        (piste, audio_cache.key_for(piste.id, recordings.absolute_path(piste), fmt))
        for piste in pistes
    ]
    manquantes = [
        (piste, clef) for piste, clef in clefs
        if not audio_cache.ready(clef, fmt)
    ]
    for piste, _clef in manquantes:
        audio_cache.start_build(piste.id, recordings.absolute_path(piste), fmt)
    for piste, _clef in manquantes:
        if not audio_cache.ensure_ready(piste.id, recordings.absolute_path(piste), fmt):
            logger.info(
                "Cache audio absent (enregistrement %s) : voie classique",
                piste.id,
            )

    chemins = audio_cache.all_paths([clef for _p, clef in clefs], fmt)
    if chemins is not None:
        plafond = max_minutes * 60
        if len(chemins) == 1:
            charge = audio_cache.load(clefs[0][1], fmt)
            if charge is not None:
                contenu, mime, duree = charge
                if 0 < duree <= plafond:
                    logger.info(
                        "Audio joint (consultation %s, cache) : %.1f s (%s)",
                        consultation_id, duree, mime,
                    )
                    return contenu, mime
                logger.info(
                    "Audio non joint (consultation %s, cache) : %.1f s hors "
                    "bornes (plafond %.0f s)",
                    consultation_id, duree, plafond,
                )
                return None
        else:
            fusion_cache = stt.concat_copies(chemins, fmt)
            if fusion_cache is not None:
                contenu, mime, duree = fusion_cache
                if 0 < duree <= plafond:
                    logger.info(
                        "Audio joint (consultation %s, cache fusionné) : "
                        "%.1f s (%s)",
                        consultation_id, duree, mime,
                    )
                    return contenu, mime

    if len(pistes) == 1:
        piste = pistes[0]
        resultat_classique = _traiter(
            recordings.absolute_path(piste), f"enregistrement {piste.id}",
        )
        if resultat_classique is not None:
            # Remplit le cache pour la génération suivante (régénération,
            # reprise) : même source, mêmes réglages — contenu équivalent.
            try:
                contenu_c, mime_c, duree_c = resultat_classique
                audio_cache.store(
                    audio_cache.key_for(
                        piste.id, recordings.absolute_path(piste), fmt,
                    ),
                    fmt, contenu_c, mime_c, duree_c,
                )
            except Exception:  # pragma: no cover — remplissage au mieux
                logger.exception("Cache audio non rempli (enregistrement %s)", piste.id)
        return resultat_classique

    try:
        fusion = _concat_audio([recordings.absolute_path(p) for p in pistes])
    except OSError as exc:
        logger.warning(
            "Audio non joint (consultation %s) : %s", consultation_id, exc,
        )
        return None
    if fusion is None:
        logger.warning(
            "Audio non joint (consultation %s) : fusion des %d enregistrements impossible",
            consultation_id, len(pistes),
        )
        return None

    contenu, _ = fusion
    workdir = tempfile.mkdtemp(prefix="consultai-gen-audio-")
    chemin = os.path.join(workdir, "fusion.ogg")
    try:
        with open(chemin, "wb") as handle:
            handle.write(contenu)
        provenance = "fusion des " + " + ".join(
            f"enregistrement {p.id}" for p in pistes
        )
        return _traiter(chemin, provenance)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Garde contre les appels qui se chevauchent (génération, retranscription,
# transcription d'un import)
# ---------------------------------------------------------------------------
# Un clic pendant qu'un appel précédent tourne encore (point de terminaison
# personnalisé lent, ou simplement impatience) l'annule CÔTÉ NAVIGATEUR —
# mais rien n'arrête le thread serveur en cours, qui a toutes les chances de
# se terminer quand même avec un client HTTP synchrone. Sans ce compteur,
# celui des deux appels qui finit en dernier écraserait le brouillon en base
# sans qu'on sache lequel des deux résultats on regarde. Même principe que
# ``dictation._processing`` : un entier par ressource, incrémenté à chaque
# tentative, comparé après l'appel bloquant — s'il a bougé entre-temps, une
# tentative plus récente a pris le relais et celle-ci n'écrit rien.
#
# Une instance par TYPE d'appel (et non une seule partagée) : une
# régénération de note et une retranscription en cours pour la MÊME
# consultation sont deux opérations indépendantes, ni l'une ni l'autre ne
# doit invalider le compteur de l'autre.
class _SequenceGuard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq: dict[int, int] = {}

    def begin(self, key: Optional[int]) -> int:
        """Réserve un numéro de tentative. 0 sans identifiant (brouillon pas
        encore créé) — il n'existe alors qu'une seule requête possible."""
        if key is None:
            return 0
        with self._lock:
            seq = self._seq.get(key, 0) + 1
            self._seq[key] = seq
            return seq

    def is_current(self, key: Optional[int], seq: int) -> bool:
        """Cette tentative est-elle toujours la plus récente pour cette clé ?"""
        if key is None:
            return True
        with self._lock:
            return self._seq.get(key) == seq


_generation_guard = _SequenceGuard()    # /api/generate
_retranscribe_guard = _SequenceGuard()  # /api/consultations/{id}/retranscribe
_transcribe_guard = _SequenceGuard()    # /api/transcribe (import de fichier)


# Correspondance entre les champs renvoyés par l'extraction et les colonnes.
# Volontairement sans « patient_name » ni « record_number » : l'identité du
# patient (nom, numéro de dossier) n'est plus collectée ni stockée.
_METADATA_TO_COLUMN = {
    "consultation_date": "consultation_date",
    "reason": "reason",
    "requester": "requester",
    "accompanied_by": "accompanied_by",
}


def _apply_metadata(consultation: Consultation, extracted: dict) -> dict:
    """
    Écrit les métadonnées relues dans la dictée, SANS écraser une saisie.

    Une valeur tapée au clavier par le médecin fait toujours autorité sur une
    valeur reconnue à l'oreille : un numéro de dossier mal entendu par le
    moteur de reconnaissance vocale ne doit pas remplacer celui qu'il a
    lui-même vérifié. Retourne l'état final des champs, tel que l'interface
    doit l'afficher.
    """
    for source, column in _METADATA_TO_COLUMN.items():
        value = (extracted.get(source) or "").strip()
        if value and not (getattr(consultation, column) or "").strip():
            setattr(consultation, column, value)

    return {
        source: getattr(consultation, column) or ""
        for source, column in _METADATA_TO_COLUMN.items()
    }


def _build_title(consultation: Consultation, template_name: str) -> str:
    """Libellé lisible du brouillon, reconstruit après extraction."""
    parts = [consultation.reason or template_name]
    return " — ".join(part for part in parts if part)[:300]


# ===========================================================================
# Routes publiques
# ===========================================================================
@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """
    Sonde de santé (Docker HEALTHCHECK). Volontairement non authentifiée et
    ne divulguant aucune donnée clinique.
    """
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "config_warnings": settings.warnings(),
        }
    )


#: Ligne « const VERSION = '…' » en tête de sw.js. Voir service_worker().
_SW_VERSION_RE = re.compile(r"^const VERSION = '[^']*';", re.MULTILINE)


def _empreinte_actifs_navigateur() -> str:
    """
    Empreinte courte des actifs du shell navigateur (app.js, tailwind.css).

    Substituée dans /sw.js avec la version : le service worker ne se met à
    jour que si LE TEXTE de /sw.js change, et __version__ seul ne bouge pas
    lors des déploiements « commit simple » — l'interface navigait alors sur
    un JavaScript périmé devant un serveur à jour (trois incidents à ce jour).
    L'empreinte change dès qu'un actif modifié est servi : purge garantie,
    release ou pas.
    """
    empreinte = hashlib.sha1()
    for nom in ("static/app.js", "static/tailwind.css", "static/sw.js"):
        chemin = BASE_DIR / nom
        try:
            stat = chemin.stat()
            empreinte.update(
                f"{nom}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
            )
        except OSError:
            empreinte.update(nom.encode("utf-8"))
    return empreinte.hexdigest()[:8]


_SW_EMPREINTE = _empreinte_actifs_navigateur()


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    """
    Service worker servi depuis la RACINE, et non depuis /static/.

    La portée d'un service worker est limitée au dossier d'où il est servi :
    depuis /static/sw.js il ne contrôlerait que /static/. Il lui faut la
    racine pour que l'application soit installable en tant que PWA.

    « no-cache » garantit qu'une nouvelle version est détectée immédiatement
    au lieu d'être servie depuis le cache HTTP du navigateur.

    POURQUOI LA VERSION EST RÉÉCRITE ICI
    ------------------------------------
    ``VERSION`` gouverne la purge du cache : le service worker supprime à
    l'activation tout cache dont le nom ne commence pas par elle. Tant qu'elle
    ne bouge pas, le navigateur continue de servir l'``app.js`` qu'il a en
    réserve — et une interface périmée devant un serveur à jour ne ressemble
    pas à un cache oublié, elle ressemble à un bogue : des réglages rangés au
    mauvais endroit, un fournisseur qui manque. C'est arrivé deux fois, les
    deux fois en ajoutant un fournisseur.

    L'incrémenter à la main était donc une étape qu'il ne fallait jamais
    oublier alors que rien ne la rappelait. Elle suit maintenant la version de
    l'application, qui change à chaque publication : le cache se purge parfois
    sans nécessité — 231 ko — et c'est tout ce que coûte l'impossibilité de
    l'oublier. La valeur écrite dans le fichier ne sert plus qu'au cas où la
    substitution échouerait.
    """
    source = (BASE_DIR / "static" / "sw.js").read_text(encoding="utf-8")
    source, remplacees = _SW_VERSION_RE.subn(
        f"const VERSION = 'consultai-v{__version__}-{_SW_EMPREINTE}';",
        source, count=1,
    )
    if not remplacees:
        # Le fichier reste servi tel quel : un service worker qui garde
        # l'ancienne version vaut mieux qu'une PWA qui ne s'installe plus.
        logger.warning(
            "sw.js : ligne « const VERSION » introuvable, version non "
            "substituée. Le cache ne se purgera qu'au prochain changement "
            "manuel de cette ligne.",
        )

    return Response(
        source,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, max-age=0",
            "Service-Worker-Allowed": "/",
        },
    )


# ===========================================================================
# Connexion (OpenID Connect)
# ===========================================================================
# Ces trois routes sont publiques : elles constituent le mécanisme de défi.
# Voir app/oidc.py pour ce que la bibliothèque prend en charge (state, nonce,
# PKCE, validation de signature) et ce qui reste à notre charge.
# ===========================================================================
@app.api_route("/auth/login", methods=["GET", "POST"], include_in_schema=False)
async def auth_login(request: Request):
    """
    Page de connexion, puis amorce du flux OIDC.

    Un seul chemin porte les deux rôles. La page (GET) affiche la case
    « Rester connecté » — l'application n'a pas d'autre page de connexion qui
    lui appartienne, le middleware renvoie d'ailleurs ici toutes les
    navigations non authentifiées. Sa soumission (POST) range la préférence
    dans la session avant de rediriger vers le fournisseur d'identité.
    """
    from app.auth import session_identity

    suite = oidc.safe_next_path(request.query_params.get("next", "/"))

    if session_identity(request):
        return RedirectResponse(suite, status_code=302)

    if not settings.oidc_configured:
        return _auth_error_page(
            _t("auth.not_configured"),
            status_code=503,
        )

    if request.method == "POST":
        form = await request.form()
        suite = oidc.safe_next_path(str(form.get("next") or suite))
        rester_connecte = str(form.get("stay_logged_in") or "") in ("true", "on", "1")

        # Mémorisé côté session plutôt que passé au fournisseur : ce dernier ne
        # renvoie que « state » et « code », et un paramètre supplémentaire dans
        # l'adresse de retour serait refusé pour non-concordance.
        request.session["consultai_next"] = suite
        request.session["consultai_stay_logged_in"] = rester_connecte
        try:
            return await oidc.authorization_redirect(request)
        except oidc.OidcError as exc:
            return _auth_error_page(str(exc), status_code=503)

    langue = i18n.normalize(settings.app_language)
    return jinja_templates.TemplateResponse(
        request,
        "login.html",
        {
            "t": _template_translator(langue),
            "lang": langue,
            "next": suite,
            "logged_out": request.query_params.get("logged_out") == "1",
            "sso_name": settings.sso_label,
            "default_hours": max(1, settings.session_max_age_seconds // 3600),
            "stay_days": max(1, settings.session_stay_max_age_seconds // 86400),
            # Version logicielle et nouveautés récentes, affichées avant la
            # connexion — la page de login est la seule surface publique.
            "app_version": __version__,
            "changelog_days": [
                {"date": day.date.isoformat(), "items": day.items}
                for day in changelog.recent_by_day(days=7)
            ],
        },
    )


@app.get("/auth/callback", include_in_schema=False)
async def auth_callback(request: Request):
    """
    Retour du fournisseur : valide, rattache le compte, ouvre la session.

    Toute erreur est rendue en page lisible plutôt que propagée : l'usager
    arrive ici par une navigation, une trace d'exception ne lui apprendrait
    rien.
    """
    # Le fournisseur signale un refus par des paramètres, pas par un code HTTP.
    if request.query_params.get("error"):
        detail = request.query_params.get("error_description") or request.query_params["error"]
        logger.warning("Connexion refusée par le fournisseur : %s", detail)
        return _auth_error_page(_t("auth.provider_refused", detail=detail))

    try:
        identite = await oidc.fetch_identity(request)
    except oidc.OidcError as exc:
        return _auth_error_page(str(exc))

    claims = identite.claims

    username = oidc.username_from(claims)
    groupes_fournisseur = oidc.groups_from(claims)

    # Nom affiché et avatar : la revendication à lire est un réglage, les
    # fournisseurs ne s'accordant pas sur son nom.
    nom_affiche = oidc.display_name_from(claims, runtime_config.value("oidc_name_claim"))
    avatar = oidc.picture_from(claims, runtime_config.value("oidc_picture_claim"))

    try:
        user, groupes = await run_in_threadpool(
            _link_account,
            str(claims.get("sub") or ""),
            username,
            str(claims.get("email") or ""),
            nom_affiche,
            groupes_fournisseur,
            avatar,
        )
    except users_service.SignupRefused as exc:
        logger.warning("Inscription refusée : %s", exc)
        return _auth_error_page(_t("denied.signup_closed", username=str(exc)), status_code=403)
    except users_service.AccountDisabled as exc:
        logger.warning("Compte désactivé : %s", exc)
        return _auth_error_page(_t("denied.account_disabled"), status_code=403)

    # La session ne porte que l'identité. Les droits sont relus en base à chaque
    # requête — voir auth._principal_from_db.
    #
    # La durée du témoin est décidée ICI : la case « Rester connecté » cochée
    # sur la page de connexion donne 30 jours (ou la valeur configurée),
    # sinon la durée normale. Elle est portée PAR le témoin (max_age_seconds
    # et expires_at) et repoussée à chaque requête authentifiée — voir
    # auth.authenticate — de sorte que la session glisse tant qu'on s'en sert.
    rester_connecte = bool(request.session.pop("consultai_stay_logged_in", False))
    duree = (
        settings.session_stay_max_age_seconds
        if rester_connecte
        else settings.session_max_age_seconds
    )
    store_identity(request, {
        "sub": user.subject,
        "username": user.username,
        "max_age_seconds": duree,
        "expires_at": int(time.time()) + duree,
    })
    # Conservé pour « id_token_hint » à la déconnexion. Sans lui, le
    # fournisseur ignore notre adresse de retour et l'usager reste sur sa page.
    #
    # BORNE DE TAILLE : la session vit dans un témoin, et un navigateur écarte
    # SILENCIEUSEMENT un témoin trop gros (~4 ko). Un jeton exceptionnellement
    # long — beaucoup de groupes, revendications volumineuses — coûterait alors
    # la session entière, donc la connexion. Perdre la redirection de
    # déconnexion est un moindre mal : on renonce au jeton plutôt qu'à la
    # session.
    if len(identite.id_token) <= _MAX_SESSION_ID_TOKEN:
        request.session["consultai_id_token"] = identite.id_token
    else:
        logger.warning(
            "Jeton d'identité trop long pour la session (%d octets) : il n'est "
            "pas conservé. La déconnexion ne reviendra pas à l'application.",
            len(identite.id_token),
        )

    logger.info(
        "Connexion de « %s » (groupes : %s)",
        user.username, ", ".join(g.name for g in groupes) or "aucun",
    )
    # Quelles revendications le fournisseur a réellement envoyées. Les NOMS
    # seulement, pas les valeurs : c'est ce qu'il faut pour régler les deux
    # réglages de revendication, et cela évite de recopier des données
    # personnelles dans les journaux du conteneur.
    logger.info(
        "Revendications reçues : %s | nom retenu : %s | avatar : %s",
        ", ".join(sorted(claims)) or "aucune",
        "oui" if nom_affiche else "non",
        "oui" if avatar else "non",
    )

    suite = oidc.safe_next_path(request.session.pop("consultai_next", "/"))
    return RedirectResponse(suite, status_code=302)


def _link_account(subject, username, email, display_name, provider_groups, avatar_url=""):
    """Partie synchrone du rattachement, exécutée hors de la boucle asyncio."""
    with SessionLocal() as db:
        return users_service.link_or_create(
            db, subject, username, email, display_name, provider_groups, avatar_url
        )


@app.get("/auth/logout", include_in_schema=False)
async def auth_logout(request: Request):
    """
    Ferme la session locale, puis celle du fournisseur.

    Dans cet ordre, et sans dépendre de la seconde : si le fournisseur n'annonce
    pas de point de déconnexion, la session locale doit tout de même être close.
    L'inverse — rediriger d'abord — laisserait un témoin valide derrière si
    l'usager n'allait pas au bout.
    """
    id_token = request.session.get("consultai_id_token", "")
    clear_identity(request)
    request.session.clear()

    # Retour sur la page de connexion de l'application, qui affichera
    # l'annonce de déconnexion. Le fournisseur doit l'avoir déclarée comme
    # adresse de retour de déconnexion (Pocket ID : champs « logout
    # callback URLs » du client OIDC). La base dépend du domaine d'accès :
    # app.loki.casa revient vers son propre login, dictai.ca vers le sien.
    retour = f"{oidc.base_url_for(request.url.hostname) or ''}/auth/login?logged_out=1"

    cible = ""
    if settings.oidc_configured:
        try:
            cible = await oidc.end_session_url(
                id_token, retour=retour, host=request.url.hostname
            )
        except Exception as exc:  # la déconnexion locale a déjà eu lieu
            logger.info("Déconnexion du fournisseur impossible : %s", exc)

    # Même si le fournisseur est injoignable ou n'annonce pas de point de
    # terminaison, l'usager doit revenir sur notre page de connexion, pas
    # rester sur un écran du fournisseur.
    return RedirectResponse(cible or retour, status_code=302)


def _auth_error_page(message: str, status_code: int = 400) -> HTMLResponse:
    """
    Page d'erreur du flux de connexion.

    Reprend la mise en forme de la page de refus du middleware plutôt que
    d'introduire un troisième gabarit, et propose toujours de réessayer : la
    cause la plus fréquente est un témoin d'état expiré, que recommencer suffit
    à corriger.
    """
    from app.auth import _ERROR_PAGE

    langue = i18n.normalize(settings.app_language)
    corps = (
        f"{esc_html(message)}</p>"
        f"<p><a style=\"color:#5eead4\" href=\"/auth/login\">"
        f"{esc_html(i18n.t('auth.retry', langue))}</a>"
    )
    return HTMLResponse(
        _ERROR_PAGE.format(
            lang=langue,
            title=i18n.t("auth.error_title", langue),
            heading=i18n.t("auth.error_title", langue),
            footer=i18n.t("denied.footer", langue, sso=settings.sso_label),
            detail=corps,
        ),
        status_code=status_code,
    )


def esc_html(value: str) -> str:
    """Échappement minimal : ce texte peut venir du fournisseur d'identité."""
    import html

    return html.escape(str(value or ""), quote=True)


# ===========================================================================
# Interface
# ===========================================================================
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request):
    user = current_user(request)
    langue = runtime_config.language()
    return jinja_templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_title": settings.app_title,
            "user": user.to_dict(),
            "version": __version__,
            # « t » est une fonction et non un dictionnaire : le gabarit écrit
            # t('cle') plutôt que t['cle'], ce qui permet de passer des champs
            # à remplir et de ne pas lever sur une clé absente.
            "t": _template_translator(langue),
            "lang": langue,
            # Catalogue complet inclus dans la page : le navigateur en a besoin
            # avant le premier affichage, et un aller-retour de plus ferait
            # apparaître l'interface en clés brutes le temps du chargement.
            "i18n_catalog": i18n.catalog(langue),
            # Pour le sélecteur de langue du formulaire de gabarit.
            "languages": i18n.LANGUAGES,
        },
    )


@app.get("/test", response_class=HTMLResponse, include_in_schema=False)
async def test_page(request: Request):
    """Index « test » réservé : liste des dictées, sous OIDC."""
    user = current_user(request)
    index = _read_test_index()
    return jinja_templates.TemplateResponse(
        request,
        "test.html",
        {
            "app_title": settings.app_title,
            "user": user.to_dict(),
            "index": index,
        },
    )


@app.get("/test/{dictation_id}", response_class=HTMLResponse, include_in_schema=False)
async def test_dictation_page(request: Request, dictation_id: int):
    """Page dictée : notes Gemini / Qwen Omni côte à côte, stats en haut."""
    user = current_user(request)
    index = _read_test_index()
    found = next(
        (d for d in index.get("dictations", []) if d.get("id") == dictation_id), None
    )
    if found is None:
        raise HTTPException(status_code=404, detail="Dictée introuvable")
    return jinja_templates.TemplateResponse(
        request,
        "test_dictation.html",
        {
            "app_title": settings.app_title,
            "user": user.to_dict(),
            "index": index,
            "dictation": found,
        },
    )


def _read_test_index() -> dict:
    chemin = "/data/test_index.json"
    try:
        with open(chemin, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        logger.warning("Page /test : %s illisible (%s)", chemin, exc)
        return {"generated_at": "", "dictations": []}


# ===========================================================================
# Identité et configuration
# ===========================================================================
@app.get("/api/me")
async def api_me(request: Request):
    user = current_user(request)
    return {"user": user.to_dict(), "auth_header": user.source_header}


class LanguageIn(BaseModel):
    #: Vide = revenir au défaut de l'installation (``APP_LANGUAGE``).
    language: str = Field("", max_length=8)


class ThemeIn(BaseModel):
    #: Clef du thème (« teal », « blue », …), vide = défaut « teal ».
    theme: str = Field("", max_length=32)


class SecondPassIn(BaseModel):
    #: « Validation » : auditer chaque note après génération (audio↔note).
    enabled: bool


@app.put("/api/me/language")
def put_my_language(payload: LanguageIn, request: Request):
    """
    Change la langue de l'usager courant.

    Volontairement PAS sous ``/api/admin`` et sans exiger de droits : la langue
    n'est pas un réglage d'installation mais une préférence personnelle. Chacun
    change la sienne, personne ne change celle des autres — l'écriture est
    toujours faite sous l'identité authentifiée, jamais sous un identifiant
    reçu dans le corps de la requête.
    """
    user = current_user(request)
    try:
        retenue = preferences.set_language(user.owner_key, payload.language)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=_t("err.unknown_language", language=payload.language),
        ) from exc

    return {"language": retenue, "languages": [
        {"value": code, "label": label} for code, label in i18n.LANGUAGES
    ]}


@app.put("/api/me/theme")
def put_my_theme(payload: ThemeIn, request: Request):
    """Change le thème de couleur de l'usager courant."""
    user = current_user(request)
    try:
        retenu = preferences.set_theme(user.owner_key, payload.theme)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "theme": retenu,
        "themes": [
            {"value": tid, "label_fr": label_fr, "label_en": label_en, "hex": hex_color}
            for tid, label_fr, label_en, hex_color in preferences.THEMES
        ],
    }


@app.put("/api/me/second_pass")
def put_my_second_pass(payload: SecondPassIn, request: Request):
    """Enregistre la préférence « Validation » de l'usager courant."""
    user = current_user(request)
    retenu = preferences.set_second_pass(user.owner_key, payload.enabled)
    return {"second_pass": retenu}


@app.get("/api/me/usage")
def get_my_usage(request: Request, db: Session = Depends(get_db)):
    """
    Récapitulatif d'usage de l'usager courant, mois calendaire en cours et
    mois précédent — jamais celui d'un autre, ``owner`` vient toujours de
    l'identité authentifiée, jamais d'un paramètre. Les bornes sont des
    dates serveur ; le nom du mois est mis en forme côté navigateur, dans la
    langue de l'interface.
    """
    user = current_user(request)
    now = utcnow()
    debut_mois = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    debut_mois_precedent = (debut_mois - timedelta(days=1)).replace(day=1)
    return {
        "current": {
            **usage.summary_for_owner(db, user.owner_key, debut_mois),
            "year": now.year, "month": now.month,
        },
        "previous": {
            **usage.summary_for_owner(db, user.owner_key, debut_mois_precedent, debut_mois),
            "year": debut_mois_precedent.year, "month": debut_mois_precedent.month,
        },
    }


@app.get("/api/config")
async def api_config(request: Request):
    """Configuration non sensible, consommée par le frontend."""
    user = current_user(request)
    langue = runtime_config.language()
    stt_provider = runtime_config.value("stt_provider")
    audio_opts = llm.audio_settings()
    return {
        "app_title": settings.app_title,
        "version": __version__,
        # Langue de l'interface. Le client s'en sert pour le formatage des
        # dates et pour savoir s'il doit recharger la page après un
        # changement de réglage.
        "language": langue,
        "theme": preferences.current_theme(),
        "themes": [
            {"value": tid, "label_fr": label_fr, "label_en": label_en, "hex": hex_color}
            for tid, label_fr, label_en, hex_color in preferences.THEMES
        ],
        "stt_language": runtime_config.stt_language(stt_provider),
        "stt_provider": stt_provider,
        "stt_model": runtime_config.stt_model(stt_provider),
        "llm_provider": runtime_config.value("llm_provider"),
        "llm_model": llm.active_model(),
        # Le fournisseur actif contourne-t-il le STT (audio envoyé seul) ?
        # Consommé par le client pour activer « Générer » sans transcription
        # quand un enregistrement existe — voir updateActionButtons/
        # generateNote côté app.js.
        "llm_bypass_stt": audio_opts["bypass_stt"],
        "llm_bypass_stt_keep_transcript": audio_opts["keep_transcript"],
        "gemini_backend": "vertex" if settings.gemini_use_vertex else "api_key",
        # « Validation » : capable (fournisseur audio) et préférence de l'usager.
        "verification_capable": llm.verification_capable(),
        "second_pass": preferences.second_pass_for(user.owner_key),
        # « Correction des médicaments » : réglage admin global.
        "dictation_grounding": _med_grounding_on(),
        "max_audio_mb": settings.max_audio_mb,
        "is_template_admin": user.is_template_admin,
        #: L'utilisateur courant : le client en a besoin pour reconnaître ses
        #: gabarits personnels (``owner``) parmi la liste des gabarits.
        "username": user.username,
        # Cadence de téléversement de la dictée : c'est le navigateur qui la
        # règle sur MediaRecorder, mais le serveur qui en décide.
        "dictation_chunk_seconds": settings.dictation_chunk_seconds,
        "dictation_segment_seconds": settings.dictation_segment_seconds,
        # Temps réel de la dictée. ``stt_realtime_mode`` est le mode EFFECTIF
        # (validé contre le fournisseur actif — voir dictation.realtime_mode) :
        # c'est lui que le navigateur doit suivre pour savoir s'il tourne le
        # VAD et signale les fins d'énoncé. ``vad``/``sse`` exigent des
        # réglages côté client (seuil, cadences), transmis tels quels.
        "stt_realtime_mode": dictation.realtime_mode(),
        "stt_vad_sensitivity": runtime_config.value("stt_vad_sensitivity"),
        "stt_vad_speech_ms": runtime_config.value_float(
            "stt_vad_speech_ms", settings.stt_vad_speech_ms
        ),
        "stt_vad_silence_ms": runtime_config.value_float(
            "stt_vad_silence_ms", settings.stt_vad_silence_ms
        ),
        # Une seule session désormais : celle de l'application. La déconnexion
        # ferme la session locale puis celle du fournisseur, dans cet ordre —
        # voir la route /auth/logout.
        "logout_url": "/auth/logout",
        "auth_mode": "disabled" if settings.auth_disabled else "oidc",
        "is_admin": user.is_admin,
        # Langues offertes, pour le menu d'identité. Le serveur en est la seule
        # source : ajouter une langue ne demande de toucher qu'à app/i18n.py.
        "languages": [
            {"value": code, "label": label} for code, label in i18n.LANGUAGES
        ],
    }


@app.get("/api/models")
async def api_models(request: Request, provider: Optional[str] = None):
    """
    Modèles réellement accessibles avec la clé configurée.

    Outil de diagnostic, et source du bouton « Modèles disponibles » du
    panneau : si le modèle configuré ne figure pas dans cette liste, la
    génération échouera par un 404 du côté du fournisseur.
    """
    current_user(request)
    target = provider or llm.active_provider()
    try:
        models = await run_in_threadpool(list_available_models, target)
    except GenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # Le modèle du fournisseur INTERROGÉ (``target``), pas nécessairement de
    # celui actif : consulter l'onglet d'un service non activé ne doit pas
    # comparer son modèle configuré à la liste d'un autre.
    configured = llm.active_model(target)
    # Le modèle rapide est renvoyé lui aussi : c'en est un second, réglable
    # séparément, et « Modèles disponibles » ne renseignait que le principal —
    # on ne pouvait donc pas vérifier qu'il existait sans lancer une génération.
    rapide = llm.raw_fast_model(target)
    return {
        "provider": target,
        "configured": configured,
        "configured_available": configured in models,
        "fast_model": rapide,
        "fast_model_available": (not rapide) or rapide in models,
        "models": models,
    }


@app.get("/api/stt/models")
async def api_stt_models(request: Request, provider: Optional[str] = None):
    """
    Modèles de transcription réellement accessibles avec la clé configurée.

    Jumeau de /api/models pour l'onglet Dictée : c'est la source du bouton
    « Modèles disponibles » quand il interroge un fournisseur de
    reconnaissance vocale. ``supported`` est false pour un fournisseur sans
    API de liste (Google, Soniox, AssemblyAI, Modulate) — le nom du modèle se
    saisit alors à la main.
    """
    current_user(request)
    target = provider or runtime_config.value("stt_provider")
    try:
        models = await run_in_threadpool(list_available_stt_models, target)
    except TranscriptionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if models is None:
        return {
            "provider": target,
            "supported": False,
            "models": [],
        }
    configured = runtime_config.stt_model(target)
    return {
        "provider": target,
        "supported": True,
        "configured": configured,
        "configured_available": (not configured) or configured in models,
        "models": models,
    }


# ===========================================================================
# Panneau d'administration
# ===========================================================================
#
# Ces réglages surchargent le fichier .env (voir app/runtime_config.py). Rien
# de ce qui gouverne l'accès n'y figure : la liste des usagers autorisés et
# les plages de proxy de confiance restent hors d'atteinte du navigateur.


class AdminSettingsIn(BaseModel):
    """
    Seules les clés présentes sont appliquées ; une chaîne vide remet le
    réglage à la valeur du ``.env``. Le panneau n'envoie donc que ce qui a
    changé, ce qui lui évite d'avoir à renvoyer une clé d'API qu'il n'a de
    toute façon jamais reçue en clair.
    """

    values: dict


@app.get("/api/admin/settings")
def get_admin_settings(request: Request, admin: Principal = Depends(require_template_admin)):
    langue = runtime_config.language()
    return {
        "settings": runtime_config.describe(langue),
        # Clé ET libellé, dans l'ordre voulu : le navigateur affiche le
        # libellé mais raisonne sur la clé.
        "groups": [
            {"key": groupe, "label": i18n.t(groupe, langue)}
            for groupe in runtime_config.GROUPS
        ],
        # Avertissements par onglet (Cohere…) : le client les filtre selon le
        # service CONSULTÉ, pas seulement l'actif.
        "warnings": runtime_config.group_warnings(langue),
    }


@app.put("/api/admin/settings")
def put_admin_settings(
    payload: AdminSettingsIn,
    request: Request,
    admin: Principal = Depends(require_template_admin),
):
    try:
        changed = runtime_config.update(payload.values, admin.username)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # La langue a pu changer à l'instant : on redécrit dans la NOUVELLE langue,
    # de sorte que le panneau se réaffiche traduit sans recharger la page.
    langue = runtime_config.language()
    return {
        "changed": changed,
        "settings": runtime_config.describe(langue),
        "groups": [
            {"key": groupe, "label": i18n.t(groupe, langue)}
            for groupe in runtime_config.GROUPS
        ],
        "warnings": runtime_config.group_warnings(langue),
        "language": langue,
    }


# ===========================================================================
# Statistiques d'usage et tarifs (administrateur)
# ===========================================================================
# require_template_admin, comme le reste du panneau de réglages — consulter
# des statistiques n'a pas le même poids que gérer comptes/groupes.
@app.get("/api/admin/usage")
def get_admin_usage(
    request: Request,
    date_from: str,
    date_to: str,
    owner: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: Principal = Depends(require_template_admin),
):
    return {
        **usage.admin_breakdown(db, date_from, date_to, owner),
        # Tableau récapitulatif en tête d'onglet : périodes calendaires fixes,
        # indépendantes de la plage date_from/date_to choisie pour le détail.
        "overview": usage.admin_cost_overview(db),
    }


@app.get("/api/admin/usage/log")
def get_admin_usage_log(
    request: Request,
    date_from: str,
    date_to: str,
    owner: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    admin: Principal = Depends(require_template_admin),
):
    """Une page du journal des générations (STT consolidé par dictée).
    Paginé côté serveur : le journal complet peut être long, le navigateur
    n'en charge qu'une page à la fois (``offset``/``limit``)."""
    return usage.admin_log(
        db, date_from, date_to, owner,
        offset=max(0, offset), limit=max(1, min(limit, 200)),
    )


class PricingRateIn(BaseModel):
    provider: str = Field(..., max_length=40)
    model: str = Field("", max_length=120)
    kind: str = Field(..., pattern="^(llm|stt)$")
    unit: str = Field(..., max_length=20)
    rate: float
    currency: str = Field("USD", max_length=8)


@app.get("/api/admin/pricing")
def list_admin_pricing(request: Request, db: Session = Depends(get_db), admin: Principal = Depends(require_template_admin)):
    rows = db.scalars(select(PricingRate).order_by(PricingRate.provider, PricingRate.model, PricingRate.kind)).all()
    return {"rates": [
        {"id": r.id, "provider": r.provider, "model": r.model, "kind": r.kind,
         "unit": r.unit, "rate": r.rate, "currency": r.currency}
        for r in rows
    ]}


@app.post("/api/admin/pricing", status_code=status.HTTP_201_CREATED)
def create_admin_pricing(
    payload: PricingRateIn, request: Request,
    db: Session = Depends(get_db), admin: Principal = Depends(require_template_admin),
):
    row = PricingRate(**payload.model_dump())
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_t("err.pricing_duplicate")) from exc
    return {"id": row.id}


@app.put("/api/admin/pricing/{rate_id}")
def update_admin_pricing(
    rate_id: int, payload: PricingRateIn, request: Request,
    db: Session = Depends(get_db), admin: Principal = Depends(require_template_admin),
):
    row = db.get(PricingRate, rate_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_t("err.pricing_not_found"))
    for field, value in payload.model_dump().items():
        setattr(row, field, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_t("err.pricing_duplicate")) from exc
    return {"id": row.id}


@app.delete("/api/admin/pricing/{rate_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_admin_pricing(
    rate_id: int, request: Request,
    db: Session = Depends(get_db), admin: Principal = Depends(require_template_admin),
):
    row = db.get(PricingRate, rate_id)
    if row is not None:
        db.delete(row)
        db.commit()
    return None


# ===========================================================================
# Sauvegarde / restauration (administrateur)
# ===========================================================================
# require_admin, et non require_template_admin : action destructive à large
# rayon d'action (remplace TOUTES les données patient), même bar que la
# gestion des comptes/groupes.
@app.get("/api/admin/backup")
def list_admin_backups(request: Request, db: Session = Depends(get_db), admin: Principal = Depends(require_admin)):
    state = db.get(SchedulerState, "backup")
    return {
        "backups": [b.to_dict() for b in backup.list_backups()],
        "retention_count": int(runtime_config.value_float("backup_retention_count", 7.0)),
        "last_run": {
            "at": _iso(state.last_run_at) if state and state.last_run_at else None,
            "status": state.last_status if state else "",
            "error": state.last_error if state else "",
        },
        "restore_pending": backup.restore_required(),
    }


@app.post("/api/admin/backup", status_code=status.HTTP_201_CREATED)
async def create_admin_backup(request: Request, admin: Principal = Depends(require_admin)):
    info = await run_in_threadpool(backup.create_backup, "manual")
    logger.info("Sauvegarde manuelle créée par %s : %s", admin.username, info.filename)
    return info.to_dict()


@app.get("/api/admin/backup/{filename}")
def download_admin_backup(filename: str, request: Request, admin: Principal = Depends(require_admin)):
    try:
        path = backup.get_backup_path(filename)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=_t("err.backup_not_found")) from exc
    return FileResponse(path, media_type="application/zip",
                         headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.delete("/api/admin/backup/{filename}", status_code=status.HTTP_204_NO_CONTENT)
def delete_admin_backup(filename: str, request: Request, admin: Principal = Depends(require_admin)):
    try:
        backup.delete_backup(filename)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=_t("err.backup_not_found")) from exc
    logger.warning("Sauvegarde supprimée par %s : %s", admin.username, filename)
    return None


@app.post("/api/admin/backup/restore/{filename}")
async def restore_admin_backup_existing(filename: str, request: Request, admin: Principal = Depends(require_admin)):
    try:
        source_path = backup.get_backup_path(filename)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=_t("err.backup_not_found")) from exc
    try:
        await run_in_threadpool(backup.restore_backup, source_path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.warning("Restauration déclenchée par %s depuis %s", admin.username, filename)
    return {"restart_required": True, "restore": backup.restore_required()}


@app.post("/api/admin/backup/restore")
async def restore_admin_backup_upload(
    request: Request, file: UploadFile = File(...), admin: Principal = Depends(require_admin),
):
    raw = await file.read()
    os.makedirs(settings.backup_dir, exist_ok=True)
    # Nom aléatoire, jamais dérivé d'une donnée fournie par la requête
    # (nom d'usager ou de fichier) : un composant de chemin construit à partir
    # d'une valeur externe n'a pas sa place, même quand la source est admin.
    temp_path = os.path.join(settings.backup_dir, f"_upload-{uuid.uuid4().hex}.zip.tmp")
    with open(temp_path, "wb") as handle:
        handle.write(raw)
    try:
        await run_in_threadpool(backup.restore_backup, temp_path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    logger.warning("Restauration déclenchée par %s depuis un fichier téléversé", admin.username)
    return {"restart_required": True, "restore": backup.restore_required()}


# ===========================================================================
# Comptes et groupes (administrateur)
# ===========================================================================
# Ces routes exigent ``require_admin`` et non ``require_template_admin`` :
# écrire un gabarit et changer les droits d'autrui ne sont pas le même pouvoir.
#
# Le garde-fou qui compte est dans app/users.py : on refuse toute opération qui
# laisserait l'installation sans administrateur actif. Sans lui, une mauvaise
# manœuvre ne serait réparable qu'en éditant la base à la main sur le NAS.
# ===========================================================================
class UserPatchIn(BaseModel):
    group_ids: Optional[List[int]] = None
    is_active: Optional[bool] = None
    display_name: Optional[str] = Field(None, max_length=255)


class GroupIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    description: str = Field("", max_length=300)
    is_admin: bool = False
    can_manage_templates: bool = False


class GroupPatchIn(BaseModel):
    description: Optional[str] = Field(None, max_length=300)
    is_admin: Optional[bool] = None
    can_manage_templates: Optional[bool] = None


@app.get("/api/admin/users")
def api_list_users(
    request: Request,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return {
        "users": users_service.list_users(db),
        "groups": users_service.list_groups(db),
        # Pour que l'interface puisse signaler « c'est vous » et éviter qu'on se
        # retire ses propres droits par distraction.
        "current_user_id": admin.user_id,
    }


@app.patch("/api/admin/users/{user_id}")
def api_update_user(
    user_id: int,
    payload: UserPatchIn,
    request: Request,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return {
            "user": users_service.update_user(
                db, user_id,
                group_ids=payload.group_ids,
                is_active=payload.is_active,
                display_name=payload.display_name,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_translate_user_error(exc)) from exc


@app.delete("/api/admin/users/{user_id}", status_code=204)
def api_delete_user(
    user_id: int,
    request: Request,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Supprime le compte et TOUTES ses données (consultations, audio, usage)."""
    try:
        users_service.delete_user(db, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_translate_user_error(exc)) from exc
    return JSONResponse(status_code=204, content=None)


@app.get("/api/admin/groups")
def api_list_groups(
    request: Request,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return {"groups": users_service.list_groups(db)}


@app.post("/api/admin/groups", status_code=201)
def api_create_group(
    payload: GroupIn,
    request: Request,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return {"group": users_service.create_group(
            db, payload.name, payload.description,
            payload.is_admin, payload.can_manage_templates,
        )}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_translate_user_error(exc)) from exc


@app.patch("/api/admin/groups/{group_id}")
def api_update_group(
    group_id: int,
    payload: GroupPatchIn,
    request: Request,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return {"group": users_service.update_group(
            db, group_id,
            description=payload.description,
            is_admin=payload.is_admin,
            can_manage_templates=payload.can_manage_templates,
        )}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_translate_user_error(exc)) from exc


@app.delete("/api/admin/groups/{group_id}", status_code=204)
def api_delete_group(
    group_id: int,
    request: Request,
    admin: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        users_service.delete_group(db, group_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_translate_user_error(exc)) from exc
    return JSONResponse(status_code=204, content=None)


def _translate_user_error(exc: ValueError) -> str:
    """
    Traduit les refus de app/users.py en message affichable.

    Le service lève des motifs courts et stables plutôt que des phrases : il ne
    connaît pas la langue de l'usager, et c'est ici qu'on la connaît.
    """
    motif = str(exc)
    correspondances = {
        "dernier administrateur": "denied.last_admin",
        "groupe système": "err.group_system",
        "nom déjà pris": "err.group_exists",
        "nom vide": "err.group_name_required",
        "groupe introuvable": "err.group_not_found",
        "compte introuvable": "err.user_not_found",
    }
    cle = correspondances.get(motif)
    return _t(cle) if cle else motif


# ===========================================================================
# Gabarits (CRUD)
# ===========================================================================
@app.get("/api/templates")
def list_templates(request: Request, db: Session = Depends(get_db)):
    """Liste des gabarits visibles : partagés (``owner`` nul) + les miens."""
    user = current_user(request)
    rows = db.scalars(
        select(TemplateModel)
        .where(or_(TemplateModel.owner.is_(None), TemplateModel.owner == user.username))
        .order_by(TemplateModel.sort_order, TemplateModel.name)
    ).all()
    return {"templates": [row.to_dict() for row in rows]}


def _template_visible(row: TemplateModel, user: Principal) -> bool:
    """Un gabarit personnel n'est jamais visible hors de son propriétaire."""
    return row.owner is None or row.owner == user.username


def _can_manage_template(row: TemplateModel, user: Principal) -> bool:
    """
    Qui peut réécrire/supprimer un gabarit ?

    Un gabarit partagé se gère avec le droit ``can_manage_templates`` ; un
    gabarit personnel n'appartient qu'à son propriétaire. Un administrateur
    garde la main sur tout. Le verrou est vérifié avant, par l'appelant.
    """
    if user.is_admin:
        return True
    if row.owner is None:
        return user.is_template_admin
    return row.owner == user.username


@app.get("/api/templates/{template_id}")
def get_template(template_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    row = db.get(TemplateModel, template_id)
    if row is None or not _template_visible(row, user):
        raise HTTPException(status_code=404, detail=_t("err.template_not_found"))
    return row.to_dict()


@app.post("/api/templates", status_code=status.HTTP_201_CREATED)
def create_template(
    payload: TemplateIn,
    user: Principal = Depends(current_user),
    db: Session = Depends(get_db),
):
    # Toute création est personnelle : elle n'apparaît que dans le menu de son
    # auteur. Les gabarits partagés de l'équipe restent ceux livrés, gérés par
    # les administrateurs de gabarits.
    row = TemplateModel(
        name=payload.name,
        description=payload.description,
        system_instructions=payload.system_instructions,
        layout_format=payload.layout_format,
        phrase_hints=payload.phrase_hints,
        sort_order=payload.sort_order,
        language=payload.language,
        is_default=False,
        owner=user.username,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=_t("err.template_exists", name=payload.name)
        )
    db.refresh(row)
    logger.info("Gabarit personnel créé « %s » par %s", row.name, user.username)
    return row.to_dict()


@app.put("/api/templates/{template_id}")
def update_template(
    template_id: int,
    payload: TemplateIn,
    user: Principal = Depends(current_user),
    db: Session = Depends(get_db),
):
    row = db.get(TemplateModel, template_id)
    if row is None or not _template_visible(row, user):
        raise HTTPException(status_code=404, detail=_t("err.template_not_found"))
    if row.is_locked:
        raise HTTPException(status_code=403, detail=_t("err.template_locked"))
    if not _can_manage_template(row, user):
        raise HTTPException(status_code=403, detail=_t("err.template_rights"))

    row.name = payload.name
    row.description = payload.description
    row.system_instructions = payload.system_instructions
    row.layout_format = payload.layout_format
    row.phrase_hints = payload.phrase_hints
    row.sort_order = payload.sort_order
    row.language = payload.language
    row.updated_at = utcnow()

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=_t("err.template_exists", name=payload.name)
        )
    db.refresh(row)
    logger.info("Gabarit modifié « %s » par %s", row.name, user.username)
    return row.to_dict()


@app.post("/api/templates/{template_id}/duplicate", status_code=status.HTTP_201_CREATED)
def duplicate_template(
    template_id: int,
    user: Principal = Depends(current_user),
    db: Session = Depends(get_db),
):
    """
    Copie un gabarit existant.

    C'est la façon normale d'en créer un : partir d'un gabarit éprouvé et en
    ajuster une rubrique, plutôt que de réécrire depuis la page blanche des
    instructions cliniques longues de plusieurs dizaines de lignes.

    La copie n'est jamais marquée ``is_default`` ni ``is_locked`` : sinon
    l'amorçage la considérerait comme un gabarit livré et pourrait la réécrire,
    et elle hériterait du verrou qu'on cherche précisément à quitter.

    La copie est TOUJOURS personnelle (``owner`` = auteur) : elle n'apparaît
    que dans son menu. Chaque médecin peut ainsi adapter un gabarit partagé
    sans imposer sa variante à toute l'équipe.

    C'est le SEUL chemin pour adapter un gabarit verrouillé, et il fonctionne
    sur lui : la copie est une ligne neuve et indépendante, pas une référence.
    """
    source = db.get(TemplateModel, template_id)
    if source is None or not _template_visible(source, user):
        raise HTTPException(status_code=404, detail=_t("err.template_not_found"))

    # Le nom est unique en base : on cherche le premier suffixe disponible
    # plutôt que de renvoyer une erreur que l'utilisateur devrait résoudre
    # lui-même.
    taken = set(db.scalars(select(TemplateModel.name)).all())
    base_name = f"{source.name} (copie)"[:200]
    name = base_name
    counter = 2
    while name in taken:
        suffix = f" ({counter})"
        name = f"{base_name[:200 - len(suffix)]}{suffix}"
        counter += 1

    copy = TemplateModel(
        name=name,
        description=source.description,
        system_instructions=source.system_instructions,
        layout_format=source.layout_format,
        phrase_hints=source.phrase_hints,
        # Juste après l'original dans le menu déroulant.
        sort_order=min(source.sort_order + 1, 9999),
        # La copie d'un gabarit verrouillé est modifiable : c'est tout l'objet
        # de l'opération.
        is_default=False,
        is_locked=False,
        language=source.language or "fr",
        owner=user.username,
    )
    db.add(copy)
    db.commit()
    db.refresh(copy)
    logger.info("Gabarit dupliqué « %s » → « %s » par %s", source.name, copy.name, user.username)
    return copy.to_dict()


@app.delete("/api/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: int,
    user: Principal = Depends(current_user),
    db: Session = Depends(get_db),
):
    """
    Supprime un gabarit.

    Les gabarits VERROUILLÉS sont refusés : ce sont les points de départ
    garantis de l'installation, et on les duplique pour les adapter.

    Les quatre gabarits livrés sont verrouillés (voir
    ``database.LOCKED_TEMPLATES``) : aucun ne se supprime. Les gabarits
    partagés se suppriment avec le droit ``can_manage_templates`` ; un gabarit
    personnel ne se supprime que par son propriétaire.
    """
    row = db.get(TemplateModel, template_id)
    if row is None or not _template_visible(row, user):
        raise HTTPException(status_code=404, detail=_t("err.template_not_found"))
    # Contrôlé ICI et pas seulement masqué dans l'écran.
    if row.is_locked:
        raise HTTPException(status_code=403, detail=_t("err.template_locked"))
    if not _can_manage_template(row, user):
        raise HTTPException(status_code=403, detail=_t("err.template_rights"))

    remaining = db.scalar(
        select(TemplateModel.id).where(
            TemplateModel.id != template_id,
            or_(TemplateModel.owner.is_(None), TemplateModel.owner == user.username),
        )
    )
    if remaining is None:
        raise HTTPException(
            status_code=409,
            detail=_t("err.template_last"),
        )

    name = row.name
    db.delete(row)
    db.commit()
    logger.info("Gabarit supprimé « %s » par %s", name, user.username)
    return None


# ===========================================================================
# Transcription
# ===========================================================================
@app.post("/api/transcribe")
async def api_transcribe(
    request: Request,
    file: UploadFile = File(..., description="Enregistrement audio (webm, ogg, mp4, wav…)"),
    template_id: Optional[int] = Form(None),
    consultation_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
):
    """
    Transcrit un enregistrement et, si ``consultation_id`` est fourni,
    enregistre immédiatement le texte dans le brouillon (protection contre
    une fermeture accidentelle du navigateur).
    """
    user = current_user(request)

    # Réservé avant l'appel bloquant, comme pour la génération et la
    # retranscription : si un autre import pour la même consultation démarre
    # avant que celui-ci ne revienne, c'est ce compteur qui décide lequel des
    # deux a le droit d'écrire son texte dans le brouillon.
    transcribe_seq = _transcribe_guard.begin(consultation_id)

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail=_t("err.audio_empty"))
    if len(raw) > settings.max_audio_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Enregistrement trop volumineux ({len(raw) / 1048576:.1f} Mo). "
                f"Limite actuelle : {settings.max_audio_mb} Mo."
            ),
        )

    # Vocabulaire additionnel provenant du gabarit sélectionné, et surtout sa
    # langue : sans ce lien, un fichier importé partait avec la langue de
    # l'interface — donc le code de langue et le lexique francophone d'un
    # usager français sur un enregistrement anglais. La dictée par tranches, elle,
    # faisait déjà ce lien (voir dictation._bind_template_language).
    extra_hints = ""
    if template_id:
        template_row = db.get(TemplateModel, template_id)
        if template_row is not None:
            extra_hints = template_row.phrase_hints or ""
            preferences.bind_document_language(template_row.language)

    try:
        # Avancement publié en direct (endpoint personnalisé découpé) : le
        # navigateur remplit sa barre de progression pendant l'appel bloquant.
        def _publier_progres(curseur: float, duree: float) -> None:
            percent = None if not duree else min(100.0, 100.0 * curseur / duree)
            live.publish(user.owner_key, "transcription_progress", {
                "consultation_id": consultation_id,
                "percent": percent,
                "cursor_seconds": round(curseur, 1),
                "duration_seconds": round(duree, 1),
                "recording_index": 0,
                "recordings_total": 1,
                "origin_tab": request.headers.get("x-consultai-tab", ""),
            })

        # Appel bloquant (réseau + ffmpeg) : exécuté hors de la boucle asyncio.
        result = await run_in_threadpool(
            transcribe, raw, file.content_type or "", extra_hints,
            on_progress=_publier_progres,
        )
    except TranscriptionError as exc:
        logger.warning("Transcription refusée pour %s : %s", user.username, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Erreur inattendue pendant la transcription")
        raise HTTPException(status_code=502, detail=_t("err.transcription", error=exc)) from exc

    if consultation_id:
        consultation = _get_owned_consultation(db, consultation_id, user)

        # Périmé par un import plus récent pour la même consultation : on
        # n'ajoute pas ce texte, dans le désordre, à la suite d'un texte
        # qu'une tentative plus récente a peut-être déjà remplacé. L'audio
        # lui-même reste conservé plus bas — un fichier réellement envoyé n'a
        # pas de raison d'être perdu à cause d'une course sur le texte.
        if not _transcribe_guard.is_current(consultation_id, transcribe_seq):
            logger.info(
                "Transcription abandonnée pour la consultation %s : supplantée "
                "par une tentative plus récente",
                consultation_id,
            )
            result["superseded"] = True
        else:
            # On ajoute à la suite : le médecin peut dicter en plusieurs fois.
            existing = (consultation.raw_transcript or "").strip()
            consultation.raw_transcript = (
                f"{existing}\n\n{result['transcript']}" if existing else result["transcript"]
            )
            # Confiance mot-à-mot du SEGMENT importé, fusionnée dans le brouillon
            # comme le fait la dictée par tranches (``_merge_conf_into``) : le
            # LLM reçoit de la même façon le bloc <CONFIANCE_MOTS> pour un
            # fichier importé que pour une dictée en direct. ``words`` est absent
            # quand l'endpoint n'émet pas de confiance par mot — on ne signale
            # alors rien, exactement comme en dictée.
            if result.get("words"):
                try:
                    conf_train = med_grounding.conf_par_token(
                        result["transcript"], result["words"]
                    )
                    _merge_conf_into(consultation, conf_train)
                except Exception:
                    # Une panne de confiance ne doit pas faire perdre la
                    # transcription, déjà écrite ci-dessus.
                    logger.exception("Confiance import indisponible — rien à fusionner")
            consultation.audio_seconds = (consultation.audio_seconds or 0) + result["duration_seconds"]
            consultation.status = "transcrit"
            if result.get("provider"):
                consultation.stt_provider = result["provider"]
                consultation.stt_model = result.get("model") or ""
                usage.log_stt_usage(
                    db, owner=user.owner_key, consultation_id=consultation.id,
                    provider=result["provider"], model=result.get("model") or "",
                    audio_seconds=int(result["duration_seconds"]),
                )
            consultation.stt_language = preferences.document_language()
            consultation.updated_at = utcnow()
            db.commit()
            result["consultation_id"] = consultation.id
            # L'interface la garde pour comparer, plus tard, à la langue du
            # gabarit choisi (proposition de retranscription).
            result["stt_language"] = consultation.stt_language
            result["stt_used"] = " / ".join(
                p for p in (consultation.stt_provider, consultation.stt_model) if p
            )
            if _med_grounding_on():
                result["med_items"] = _apply_grounding(
                    db, consultation,
                    origin_tab=request.headers.get("x-consultai-tab", ""),
                )

        # Le fichier importé est conservé au même titre qu'une dictée, qu'il
        # ait ou non gagné la course ci-dessus : il sert à trancher un doute
        # sur une posologie, et il s'effacera avec le brouillon.
        try:
            stored = await run_in_threadpool(
                recordings.store_bytes, db, consultation, raw,
                file.content_type or "audio/webm", result["duration_seconds"], "import",
            )
            result["recording_id"] = stored.id
        except OSError as exc:
            # Le disque plein ne doit pas faire perdre la transcription, qui
            # est déjà en base à ce stade.
            logger.warning("Enregistrement importé non conservé : %s", exc)

    logger.info(
        "Transcription réussie pour %s : %d caractères, confiance %.2f",
        user.username, len(result["transcript"]), result["confidence"],
    )
    return result


# ===========================================================================
# Dictée par segments
# ===========================================================================
#
# Voir app/dictation.py pour le raisonnement. Côté API, quatre règles :
#   * le brouillon existe avant la première seconde d'audio — c'est lui qui
#     reçoit le texte, donc lui qui survit à la fermeture de l'onglet ;
#   * un fragment déjà reçu peut être renvoyé sans dommage, un fragment en
#     avance est refusé avec le numéro attendu ;
#   * le découpage tourne en tâche de fond : téléverser ne doit jamais
#     attendre Google ;
#   * « Terminer » est le seul appel bloquant, et il conclut toujours —
#     même si toutes les tranches précédentes ont échoué.


class DictationStartIn(BaseModel):
    consultation_id: int
    template_id: Optional[int] = None
    mime_type: str = Field("audio/webm", max_length=100)


def _schedule_dictation_processing(session_id: str, username: str) -> None:
    """Lance la passe de découpage sans faire attendre la réponse HTTP."""
    if not dictation.try_begin_processing(session_id):
        return

    async def runner() -> None:
        try:
            await run_in_threadpool(dictation.process_pending, session_id, username, False)
        except Exception:  # une tâche de fond ne doit jamais mourir en silence
            logger.exception("Découpage de la dictée %s interrompu", session_id)
        finally:
            dictation.end_processing(session_id)

    asyncio.create_task(runner())


def _dictation_session(session_id: str, user: Principal):
    try:
        return dictation.load_session(session_id, user.username)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/dictation")
def list_dictations(request: Request, db: Session = Depends(get_db)):
    """
    Dictées ouvertes restées sur le serveur, et brouillons actuellement
    marqués « abandonnée ». La passe de traitement des dictées orphelines
    (audio archivé, brouillon marqué, sessions sans contenu purgées) tourne
    ici, à chaque chargement de page — c'est ce qui nourrit le toast
    « brouillon abandonné » côté client.
    """
    user = current_user(request)
    dictation.cleanup_abandoned(
        user.username, db, request.headers.get("x-consultai-tab", ""),
    )
    abandoned = db.scalars(
        select(Consultation.id).where(
            Consultation.owner == user.owner_key,
            Consultation.status == "abandonnee",
        )
    ).all()
    return {
        "sessions": dictation.list_sessions(user.username),
        "abandoned": list(abandoned),
    }


@app.post("/api/dictation", status_code=status.HTTP_201_CREATED)
def start_dictation(payload: DictationStartIn, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    consultation = _get_owned_consultation(db, payload.consultation_id, user)
    session = dictation.create_session(
        username=user.username,
        consultation_id=consultation.id,
        template_id=payload.template_id,
        mime_type=payload.mime_type,
    )
    # Sonde précoce du service STT : le navigateur sait IMMÉDIATEMENT si
    # l'endpoint de reconnaissance est injoignable (au lieu d'attendre un
    # échec de transcription, des dizaines de secondes plus tard). La sonde
    # est légère (timeout court, cache ~15 s) et ne bloque jamais la dictée.
    endpoint = stt.active_stt_endpoint()
    if endpoint:
        session.stt_available = stt.stt_available(endpoint)
        session.save()
    live.publish(user.owner_key, "dictation_started", {
        "consultation_id": consultation.id,
        "session_id": session.id,
        "title": consultation.title,
        "origin_tab": request.headers.get("x-consultai-tab", ""),
    })
    return session.to_public()


@app.post("/api/dictation/{session_id}/chunk")
async def upload_dictation_chunk(
    session_id: str,
    request: Request,
    seq: int = Form(..., ge=0),
    duration_ms: int = Form(0, ge=0),
    file: UploadFile = File(..., description="Fragment audio brut"),
):
    user = current_user(request)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail=_t("err.chunk_empty"))

    try:
        session = await run_in_threadpool(
            dictation.append_chunk,
            session_id, user.username, seq, data, duration_ms / 1000.0,
        )
    except SequenceMismatch as exc:
        # 409 plutôt que 400 : le client sait alors qu'il doit repartir du
        # numéro indiqué, et non abandonner le fragment.
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-Expected-Seq": str(exc.expected)},
        ) from exc
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DictationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Optimisation pure : préparer l'audio de la génération PENDANT la dictée
    # (points de contrôle bornés, cf. dictation.maybe_schedule_checkpoint).
    try:
        dictation.maybe_schedule_checkpoint(session_id, user.username)
    except Exception:  # pragma: no cover — ne doit jamais casser un fragment
        logger.exception("Point de contrôle audio non programmé (%s)", session_id)

    if dictation.should_process(session):
        _schedule_dictation_processing(session_id, user.username)

    return session.to_public()


@app.post("/api/dictation/{session_id}/utterance_ended")
async def dictation_utterance_ended(session_id: str, request: Request):
    """
    Fin d'énoncé signalée par le VAD du navigateur (mode temps réel).

    C'est un DÉCLENCHEUR, pas un repère de coupe : le serveur pose le
    drapeau de flush et lance une passe qui découpe au premier silence
    exploitable (ffmpeg fait autorité sur la frontière). Rien n'est envoyé
    ici — seul l'ordre « traite maintenant » voyage.

    La route est ``async def`` à dessein : ``_schedule_dictation_processing``
    crée une tâche via ``asyncio.create_task``, qui exige la boucle
    d'événements. Une route synchrone (exécutée dans le threadpool par
    FastAPI) ferait échouer la planification en « no running event loop » —
    et le flush ne serait jamais consommé, la dictée ne transcrivant plus
    qu'au « Terminer ».
    """
    user = current_user(request)
    _dictation_session(session_id, user)  # 404 avant de toucher l'état

    try:
        session = await run_in_threadpool(
            dictation.request_flush, session_id, user.username
        )
    except DictationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if dictation.should_flush(session):
        _schedule_dictation_processing(session_id, user.username)
    return session.to_public()


@app.get("/api/dictation/{session_id}")
def get_dictation(session_id: str, request: Request):
    """Avancement de la dictée : le navigateur y lit les tranches transcrites."""
    user = current_user(request)
    session = _dictation_session(session_id, user)
    # Scrutation de l'onglet qui enregistre (~7 s) : c'est le signal de vie qui
    # distingue une dictée simplement en pause d'une dictée abandonnée par un
    # onglet mort. Sans cette mise à jour, la moindre pause de plus de
    # ``_STALE_AFTER`` secondes ferait marquer le brouillon « abandonnée » et
    # archiver l'audio par ``dictation.cleanup_abandoned`` alors que le
    # médecin compte bien reprendre.
    session.save()
    return session.to_public()


class DictationTemplateIn(BaseModel):
    template_id: Optional[int] = None


@app.patch("/api/dictation/{session_id}")
def update_dictation_template(session_id: str, payload: DictationTemplateIn, request: Request):
    """
    Rattache la dictée en cours à un autre gabarit.

    Le médecin choisit souvent son gabarit une fois la dictée lancée. Sans cet
    appel, la session gardait celui de son ouverture et toutes les tranches
    suivantes partaient dans l'ancienne langue, avec l'ancien vocabulaire —
    l'écart se creusait au lieu de se refermer.

    Ne retouche rien de ce qui est déjà transcrit : c'est le rôle de
    ``/api/consultations/{id}/retranscribe``, qui, lui, repart de l'audio.
    """
    user = current_user(request)
    session = _dictation_session(session_id, user)
    session.template_id = payload.template_id
    session.save()
    logger.info("Dictée %s : gabarit passé à %s", session_id, payload.template_id)
    return session.to_public()


@app.post("/api/dictation/{session_id}/finish")
async def finish_dictation(session_id: str, request: Request, db: Session = Depends(get_db)):
    """
    Transcrit le reliquat et clôt la session.

    Appel volontairement bloquant : le médecin attend le texte complet avant
    de lancer la mise en forme. Il reste court — seules les dernières dizaines
    de secondes restent à traiter, le reste l'a été pendant la dictée.
    """
    user = current_user(request)
    _dictation_session(session_id, user)  # 404 avant de mobiliser un thread

    try:
        session = await run_in_threadpool(
            dictation.process_pending, session_id, user.username, True
        )
    except TranscriptionError as exc:
        logger.warning("Fin de dictée %s refusée : %s", session_id, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DictationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Erreur inattendue à la clôture de la dictée %s", session_id)
        raise HTTPException(status_code=502, detail=_t("err.transcription", error=exc)) from exc

    result = session.to_public()
    result["transcript"] = " ".join(session.parts).strip()

    # L'audio rejoint le brouillon : il reste réécoutable pour trancher un
    # doute sur une posologie, et sera effacé avec lui.
    consultation = db.get(Consultation, session.consultation_id)
    if consultation is not None and consultation.owner == user.username:
        # Langue réellement employée par les tranches : l'interface s'en sert
        # pour proposer une retranscription si le gabarit choisi diverge.
        result["stt_language"] = consultation.stt_language
        result["stt_used"] = " / ".join(
            p for p in (consultation.stt_provider, consultation.stt_model) if p
        )
        # Artefact audio prêt pour la génération — AVANT le déplacement du
        # brut : la passe (queue du point de contrôle + concat, ou complète)
        # lit ``raw`` ; le blocage est borné (~1 s avec un contrôle à jour,
        # quelques secondes sinon) et sérialise avec le move de store_path.
        try:
            artefact = await run_in_threadpool(
                dictation.finish_audio_artifact, session.id, user.username,
            )
        except Exception:  # pragma: no cover — optimisation pure
            logger.exception("Artefact audio non préparé (dictée %s)", session_id)
            artefact = None
        try:
            stored = await run_in_threadpool(
                recordings.store_path, db, consultation, session.audio_path,
                session.mime_type, result["transcribed_seconds"], "dictee",
            )
            result["recording_id"] = stored.id if stored else None
            if stored:
                # L'artefact rejoint le cache sous la clé définitive ;
                # sans artefact, la préparation complète part en tâche de
                # fond — la génération saura attendre ou retombera sur sa
                # voie historique.
                fmt = llm.audio_settings(llm.active_provider())["send_audio_format"]
                adopte = False
                if artefact is not None:
                    meta_artefact = os.path.splitext(artefact)[0] + ".json"
                    adopte = audio_cache.adopt_pair(
                        artefact, meta_artefact,
                        stored.id, recordings.absolute_path(stored), fmt,
                    )
                if not adopte:
                    audio_cache.start_build(
                        stored.id, recordings.absolute_path(stored), fmt,
                    )
                live.publish(user.owner_key, "recording_added", {
                    "consultation_id": consultation.id,
                    "recording_id": stored.id,
                    "origin_tab": request.headers.get("x-consultai-tab", ""),
                })
        except OSError as exc:
            logger.warning("Dictée %s : audio non conservé — %s", session_id, exc)

    dictation.delete_session(session)
    # Les autres onglets/appareils qui suivaient (ou hébergeaient l'invitation
    # « Suivre » de) cette dictée rafraîchissent leur bandeau : la session a
    # disparu, « Suivre » n'a plus d'objet. delete_session n'efface que les
    # fichiers — l'objet reste lisible pour le payload.
    live.publish(user.owner_key, "dictation_stopped", {
        "consultation_id": session.consultation_id,
        "session_id": session.id,
        "origin_tab": request.headers.get("x-consultai-tab", ""),
    })
    logger.info(
        "Dictée %s conclue pour %s : %d tranche(s), %s s d'audio",
        session_id, user.username, len(session.parts), result["transcribed_seconds"],
    )
    return result


@app.post("/api/dictation/{session_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_dictation(session_id: str, request: Request):
    """Abandonne la dictée et efface son audio du serveur."""
    user = current_user(request)
    session = _dictation_session(session_id, user)
    dictation.delete_session(session)
    # Même raison que finish_dictation : prévenir les autres écrans.
    live.publish(user.owner_key, "dictation_stopped", {
        "consultation_id": session.consultation_id,
        "session_id": session.id,
        "origin_tab": request.headers.get("x-consultai-tab", ""),
    })
    logger.info("Dictée %s abandonnée par %s", session_id, user.username)
    return None


# ===========================================================================
# Génération de la note
# ===========================================================================

#: Rubrique « Corrections et éléments à valider » : dernier titre de la note,
#: détecté par son intitulé (fr « … à valider », en « … items to verify »).
#: La note conserve le corps ; la rubrique elle-même est déplacée vers
#: l'onglet « Validation » (stockée dans ``corrections_markdown``) — elle ne
#: fait jamais partie du document clinique persisté.
_CORRECTIONS_TITRE_RE = re.compile(
    r"^#{1,6}\s+[^\n]*(?:valid|vérif|verify|corrections)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)


def split_corrections(markdown: str) -> Tuple[str, str]:
    """
    Sépare la note de sa rubrique finale « Corrections et éléments à valider ».

    Retourne ``(note, corrections)`` : ``note`` est le corps sans la rubrique,
    ``corrections`` le contenu extrait (intitulé compris), ou ``""`` si la
    rubrique est absente. On coupe au DERNIER titre correspondant — la
    rubrique est toujours en fin de note ; une reformulation du libellé ne
    doit pas la faire manquer.
    """
    if not markdown:
        return markdown, ""
    coupures = [m.start() for m in _CORRECTIONS_TITRE_RE.finditer(markdown)]
    if not coupures:
        return markdown, ""
    coupe = coupures[-1]
    note = markdown[:coupe].rstrip() + "\n"
    corrections = markdown[coupe:].strip()
    return note, corrections


def _generate_and_publish(
    user: Principal,
    payload: GenerateIn,
    template_row: TemplateModel,
    model_name: Optional[str],
    audio_payload: Optional[Tuple[bytes, str]],
    generation_seq: int,
    origin_tab: str,
    system_prompt: str,
    confiance_mots: Optional[List[dict]] = None,
    med_hints: Optional[List[dict]] = None,
) -> dict:
    """
    Génère la note en continu et diffuse les morceaux en direct (SSE).

    Tourne dans le threadpool : ``live.publish`` est sûr d'être appelé depuis
    n'importe quel fil d'exécution (voir app/live.py, même schéma que
    ``dictation._store_part``). La diffusion s'interrompt dès que cette
    tentative n'est plus la plus récente pour la consultation (garde
    ``_generation_guard``) : un clic de régénération supplante l'ancien flux,
    qui cesse aussitôt de consommer des jetons chez le fournisseur.

    Le texte brut (non encore nettoyé) est envoyé tel quel dans chaque morceau :
    l'écran est sans état, un morceau perdu (file SSE saturée) est comblé par le
    suivant, et la réponse JSON finale remplace de toute façon le tout par le
    texte définitif.
    """
    # Signal « generation_started » : N'EST publié QUE lorsque le fournisseur
    # LLM accuse réception de la requête (transmis par ``on_stream_started``,
    # voir llm) — jamais au lancement interne. ConsultAI n'exécute pas le
    # modèle : le fait d'avoir commencé à préparer l'appel ne prouve rien. Le
    # garde ``is_current`` évite d'annoncer un départ pour un flux supplanté,
    # et le drapeau borne à un seul départ par tentative.
    started = {"done": False}

    def _publish_started() -> None:
        if started["done"] or not _generation_guard.is_current(
            payload.consultation_id, generation_seq
        ):
            return
        started["done"] = True
        live.publish(user.owner_key, "generation_started", {
            "consultation_id": payload.consultation_id,
            "generation_token": payload.generation_token,
            "origin_tab": origin_tab,
        })

    # --- Raisonnement du modèle (thinking) : affichage transitoire ---------
    # Décidé côté serveur selon le rôle de l'appelant et les deux bascules du
    # panneau (admin / autres utilisateurs). La pensée est diffusée en direct
    # pendant la génération puis effacée : elle n'est JAMAIS écrite en base —
    # seule ``result["markdown"]`` est persisté plus bas.
    show_thinking = (
        runtime_config.value("show_thinking_admin") == "true"
        if user.is_admin
        else runtime_config.value("show_thinking_users") == "true"
    )
    thought_accum = {"raw": "", "seq": 0}

    def _on_thought(fragment: str) -> None:
        if not _generation_guard.is_current(payload.consultation_id, generation_seq):
            return
        thought_accum["raw"] += fragment
        thought_accum["seq"] += 1
        live.publish(user.owner_key, "generation_thought", {
            "type": "delta", "seq": thought_accum["seq"],
            "delta": fragment,
            "consultation_id": payload.consultation_id,
            "generation_token": payload.generation_token,
            "origin_tab": origin_tab,
        })

    # --- Pipeling « deux passes » --------------------------------------------
    # note_pipeline = two_pass : passe 1 (extraction/correction par le LLM) +
    # passe 2 (mise en page déterministe par note_renderer, sans modèle). Toute
    # circonstance où la passe 1 échoue ou le gabarit n'est pas compatible
    # retombe sur la passe unique classique — la dictée n'est jamais perdue.
    #
    # La passe 1 reçoit le transcript PRÉ-CORRIGÉ inline (mêmes substitutions
    # déterministes et auditées que la passe unique : garbles seedés, exacts —
    # plus la réécriture agressive des MÉDICAMENTS COURANTS gagnée par
    # similarité + confiance STT, voir COMMON_INLINE_*). Le LLM n'a plus à
    # deviner les noms de médicaments déformés connus (« Restore 5 » →
    # « Crestor 5 », « la Six » → « Lasix », « ketapine » → « quetiapine ») :
    # charge cognitive en moins, et l'obéissance aux hints n'est plus le seul
    # recours. « payload.transcript » (brut) reste la source pour la
    # persistance, les hints et la confiance mot-à-mot.
    #
    # La confiance STT mot-à-mot (transcript_conf) est nécessaire à la
    # réécriture agressive des courants (le plafond COMMON_INLINE_STT écarte la
    # prose) : on la recharge ici à partir de la consultation, indépendamment
    # du charger du corridor de génération (conf_map n'est pas passée dans
    # cette fonction).
    conf_for_inline: dict = {}
    if payload.consultation_id:
        try:
            with SessionLocal() as _db_inline:
                _consult_inline = _get_owned_consultation(
                    _db_inline, payload.consultation_id, user)
                conf_for_inline = (json.loads(_consult_inline.transcript_conf)
                                   if _consult_inline.transcript_conf else {})
        except Exception:
            conf_for_inline = {}
    if runtime_config.value("note_pipeline") == "two_pass":
        deux_pass_transcript = payload.transcript
        if _med_grounding_on() and payload.transcript:
            try:
                corrige, _ = med_grounding.normalize(
                    payload.transcript, inline_safe=True,
                    conf=conf_for_inline or None,
                )
                if corrige and corrige.strip():
                    deux_pass_transcript = corrige
            except Exception:
                logger.exception("Pré-correction inline indisponible — passe 1 sur transcript brut")
        deux_pass = llm.generate_note_two_pass(
            deux_pass_transcript,
            template_row.system_instructions,
            template_row.layout_format,
            _build_context_lines(payload),
            payload.extra_instructions,
            model_name,
            template_row.language,
            audio_payload,
            system_override=system_prompt,
            confiance=confiance_mots,
            med_hints=med_hints or None,
            on_stream_started=_publish_started,
        )
        if deux_pass is not None:
            logger.info(
                "Note générée en deux passes (%s / %s) — %d caractères, "
                "extraction appliquée",
                deux_pass["provider"], deux_pass["model"],
                len(deux_pass["markdown"]),
            )
            if _generation_guard.is_current(payload.consultation_id, generation_seq):
                live.publish(user.owner_key, "generation_chunk", {
                    "type": "snapshot", "seq": 1,
                    "markdown": deux_pass["markdown"],
                    "consultation_id": payload.consultation_id,
                    "generation_token": payload.generation_token,
                    "origin_tab": origin_tab,
                })
            return deux_pass

    generator = llm.generate_note_stream(
        payload.transcript,
        template_row.system_instructions,
        template_row.layout_format,
        _build_context_lines(payload),
        payload.extra_instructions,
        model_name,
        template_row.language,
        audio_payload,
        on_stream_started=_publish_started,
        on_thought=_on_thought if show_thinking else None,
        system_override=system_prompt,
        confiance=confiance_mots,
        med_hints=med_hints,
    )

    raw = ""
    prev = ""
    seq = 0
    result = None
    last_snapshot = 0.0
    try:
        # Chaque fragment du fournisseur est publié DÈS qu'il arrive, sous
        # forme de delta (texte nouveau depuis le précédent) : la latence de
        # diffusion est minimale et le volume total reste petit (pas de
        # renvoi du texte entier à chaque fois). Un snapshot du texte complet
        # part toutes les ~1 s : le navigateur se répare lui-même si un delta
        # a été perdu en route (file SSE plafonnée, live._MAX_QUEUE). La
        # réponse JSON finale de /api/generate reste LA source de vérité.
        while _generation_guard.is_current(payload.consultation_id, generation_seq):
            raw = next(generator)
            delta = raw[len(prev):] if raw.startswith(prev) else raw
            prev = raw
            seq += 1
            now = time.monotonic()
            if delta:
                live.publish(user.owner_key, "generation_chunk", {
                    "type": "delta", "seq": seq, "delta": delta,
                    "consultation_id": payload.consultation_id,
                    "generation_token": payload.generation_token,
                    "origin_tab": origin_tab,
                })
            if now - last_snapshot >= 1.0:
                live.publish(user.owner_key, "generation_chunk", {
                    "type": "snapshot", "seq": seq, "markdown": raw,
                    "consultation_id": payload.consultation_id,
                    "generation_token": payload.generation_token,
                    "origin_tab": origin_tab,
                })
                # La pensée ne transite que tant qu'AUCUN texte de note n'a été
                # émis (``raw`` vide) : dès que la note commence, plus aucun
                # snapshot de raisonnement — sans quoi le client laisserait la
                # pensée réapparaître pendant le streaming de la note.
                if show_thinking and thought_accum["raw"] and not raw:
                    live.publish(user.owner_key, "generation_thought", {
                        "type": "snapshot", "seq": thought_accum["seq"],
                        "markdown": thought_accum["raw"],
                        "consultation_id": payload.consultation_id,
                        "generation_token": payload.generation_token,
                        "origin_tab": origin_tab,
                    })
                last_snapshot = now
        else:
            # Supplantée pendant la génération : on coupe le flux fournisseur
            # (pas de jetons gaspillés) et on laisse le garde final de
            # ``api_generate`` écarter ce résultat.
            generator.close()
            return {
                "markdown": "", "superseded": True,
                "consultation_id": payload.consultation_id,
            }
    except StopIteration as stop:
        result = stop.value
    except Exception:
        generator.close()
        raise

    # Snapshot final : l'écran montre le texte au complet juste avant que la
    # réponse JSON finale ne le remplace par la version définitive.
    if _generation_guard.is_current(payload.consultation_id, generation_seq):
        live.publish(user.owner_key, "generation_chunk", {
            "type": "snapshot", "seq": seq, "markdown": raw,
            "consultation_id": payload.consultation_id,
            "generation_token": payload.generation_token,
            "origin_tab": origin_tab,
        })
    return result


#: Références fortes aux tâches de fond : la boucle ne garde qu'une référence
#: faible aux ``asyncio.Task`` — sans ce set, une tâche peut être ramassée
#: en pleine course (ici : l'audit « Validation » disparaîtrait silencieusement).
_taches_fond: set = set()


async def _run_second_pass(
    *,
    owner_key: str,
    consultation_id: int,
    generation_seq: int,
    generation_token: str,
    origin_tab: str,
    note_markdown: str,
    transcript: str,
    langue: str,
    audio_payload,
    provider: str,
    model_name: Optional[str],
    system_instruction: str,
) -> None:
    """
    « Validation » : audit factuel (audio↔note, ou transcription seule si le
    fournisseur ne reçoit pas l'audio), en tâche de fond.

    Lancé APRÈS la persistance de la note ; ne bloque jamais l'usager. Le
    JSON brut de l'audit est diffusé en direct (évènements SSE
    ``verification_chunk``, texte accumulé) puis le résultat rejoint la
    consultation (``verification_json``) et part en évènement SSE final
    ``verification_result`` — les deux porteurs du même jeton de génération,
    pour que l'onglet émetteur seul les applique. Supplanté ou en échec :
    silencieux, la note reste telle quelle.
    """
    if not _generation_guard.is_current(consultation_id, generation_seq):
        return

    def _travail():
        # Diffusion en flux : chaque fragment JSON rejoint les onglets par
        # évènement SSE ``verification_chunk`` (texte ACCUMULÉ — le client
        # re-parse et réaffiche sans état, un morceau perdu se répare seul).
        # Écrêtée ~5/s : le navigateur re-parse le JSON à chaque morceau,
        # inutile de l'inonder (``live.publish`` est sûr depuis ce thread,
        # voir app/live.py).
        derniere = {"t": float("-inf")}

        def _publier_chunk(texte: str) -> None:
            if not _generation_guard.is_current(consultation_id, generation_seq):
                return
            maintenant = time.monotonic()
            if maintenant - derniere["t"] < 0.2:
                return
            derniere["t"] = maintenant
            live.publish(owner_key, "verification_chunk", {
                "consultation_id": consultation_id,
                "generation_token": generation_token,
                "origin_tab": origin_tab,
                "text": texte,
            })

        resultat, usage_passe = llm.verify_note_stream(
            note_markdown,
            langue=langue,
            audio=audio_payload,
            transcript=transcript or None,
            system_instruction=system_instruction,
            model=model_name,
            provider=provider,
            on_chunk=_publier_chunk,
        )
        if resultat is None:
            return None, usage_passe
        with SessionLocal() as db:
            if not _generation_guard.is_current(consultation_id, generation_seq):
                return None, usage_passe
            consultation = db.get(Consultation, consultation_id)
            if consultation is None:
                return resultat, usage_passe
            consultation.verification_json = json.dumps(resultat, ensure_ascii=False)
            try:
                usage.log_llm_usage(
                    db, owner=owner_key, consultation_id=consultation_id,
                    provider=provider, model=model_name or "",
                    prompt_tokens=usage_passe.get("prompt_tokens"),
                    output_tokens=usage_passe.get("output_tokens"),
                    audio_prompt_tokens=usage_passe.get("audio_prompt_tokens"),
                    cached_tokens=usage_passe.get("cached_tokens"),
                )
            except Exception:  # pragma: no cover — statistiques au mieux
                logger.exception("Usage du « Validation » non journalisé (%s)", consultation_id)
            db.commit()
        return resultat, usage_passe

    try:
        resultat, _usage_passe = await run_in_threadpool(_travail)
    except Exception:  # pragma: no cover — jamais bloquant
        logger.exception("« Validation » impossible (consultation %s)", consultation_id)
        return

    if resultat is None:
        # Audit sans résultat (audio absent, appel impossible, JSON invalide)
        # ou génération supplantée : on prévient quand même les onglets — sans
        # quoi la roue « Validation » tournerait jusqu'au filet du navigateur.
        live.publish(owner_key, "verification_result", {
            "consultation_id": consultation_id,
            "generation_token": generation_token,
            "origin_tab": origin_tab,
            "skipped": True,
        })
        return
    live.publish(owner_key, "verification_result", {
        "consultation_id": consultation_id,
        "generation_token": generation_token,
        "origin_tab": origin_tab,
        **resultat,
    })
    logger.info(
        "« Validation » publié (consultation %s) : %d omission(s), %d invention(s), confiance %s",
        consultation_id, len(resultat["omissions"]),
        len(resultat["inventions"]), resultat["confiance"],
    )


@app.post("/api/generate")
async def api_generate(
    payload: GenerateIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Applique le gabarit sélectionné à la transcription via Gemini."""
    user = current_user(request)

    template_row = db.get(TemplateModel, payload.template_id)
    if template_row is None:
        raise HTTPException(status_code=404, detail=_t("err.template_not_found"))

    # Réservé AVANT l'appel au modèle : si un autre clic pour cette même
    # consultation arrive pendant que celui-ci attend encore, c'est ce
    # compteur qui décidera lequel des deux a le droit d'écrire son résultat.
    generation_seq = _generation_guard.begin(payload.consultation_id)

    model_name = settings.gemini_model_pro if payload.use_pro else None

    # Consigne système partagée entre la mise en forme et l'audit « Validation »
    # (la MÊME chaîne, pour que [consigne système + audio] soit un préfixe
    # commun réutilisé par le cache implicite de Gemini — voir CHANGELOG
    # 2026-08-27).
    langue = i18n.normalize(template_row.language or runtime_config.language())
    system_prompt = llm.build_system_prompt(
        template_row.system_instructions,
        runtime_config.general_prompt(langue),
        langue,
    )

    active_provider = llm.active_provider()
    audio_opts = llm.audio_settings(active_provider)
    need_audio = audio_opts["send_audio"] or audio_opts["bypass_stt"]

    audio_payload = None
    if need_audio and payload.consultation_id:
        audio_payload = await run_in_threadpool(
            _prepare_audio_for_generation, db, payload.consultation_id,
            audio_opts["max_minutes"], audio_opts["send_audio_format"],
        )
    elif need_audio:
        logger.info(
            "Audio non tenté (consultation %s) : fournisseur « %s » ou aucune "
            "consultation associée",
            payload.consultation_id, active_provider,
        )

    # « Validation » demandée mais rien à croiser (ni audio ni transcription) :
    # prévenir aussitôt les onglets (évènement « skipped ») plutôt que de
    # laisser la roue tourner jusqu'au filet de 180 s — sans référence, l'audit
    # ne partira jamais.
    if (
        payload.second_pass
        and payload.consultation_id
        and audio_payload is None
        and not (payload.transcript or "").strip()
    ):
        live.publish(user.owner_key, "verification_result", {
            "consultation_id": payload.consultation_id,
            "generation_token": payload.generation_token,
            "origin_tab": request.headers.get("x-consultai-tab", ""),
            "skipped": True,
        })

    # Confiance mot-à-mot : sans audio (fournisseur de note qui n'écoute pas),
    # on signale au LLM les mots que le STT a entendus avec incertitude, pour
    # concentrer son effort de correction là où il est utile et prévenir toute
    # sur-correction du reste.
    conf_map: dict = {}
    confiance_mots: Optional[List[dict]] = None
    if (
        audio_payload is None
        and payload.consultation_id
        and payload.transcript
    ):
        try:
            consultation = _get_owned_consultation(db, payload.consultation_id, user)
            conf_map = json.loads(consultation.transcript_conf) if consultation.transcript_conf else {}
            if conf_map:
                confiance_mots = med_grounding.doutes_pour_texte(
                    payload.transcript, conf_map
                )
        except Exception:
            logger.exception("Confiance mot-à-mot indisponible — génération sans signal")

    # Hints structurés pour le LLM : items déterministes du moteur
    # (extraction inline_safe, candidates SAINS) + candidats PHONÉTIQUES
    # (G2P français, « dilote » → Dilaudid) — la même liste que l'onglet
    # Validation, source unique. Les phonétiques sont des PISTES à confirmer,
    # jamais des réécritures : le modèle les recoupe avec le contexte clinique.
    # ``conf_map`` est relayé aux hints : un nom sans dose mais entendu avec
    # incertitude se voit proposer sa piste phonétique (cf. phonetiques_texte).
    med_hints: list = []
    if _med_grounding_on() and payload.transcript:
        try:
            med_hints = med_grounding.extract_validation_items(
                payload.transcript, conf=conf_map or None,
            )
        except Exception:
            med_hints = []
            logger.exception("Hints méds indisponibles — génération sans candidats")

    try:
        result = await run_in_threadpool(
            _generate_and_publish,
            user,
            payload,
            template_row,
            model_name,
            audio_payload,
            generation_seq,
            request.headers.get("x-consultai-tab", ""),
            system_prompt,
            confiance_mots,
            med_hints,
        )
    except GenerationError as exc:
        logger.warning("Génération refusée pour %s : %s", user.username, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Erreur inattendue pendant la génération")
        raise HTTPException(status_code=502, detail=_t("err.generation", error=exc)) from exc

    # La rubrique « Corrections et éléments à valider » ne fait jamais partie
    # du document : on la sort du corps de la note (persistance, métadonnées,
    # audit et réponse) pour l'onglet « Validation ».
    note, corrections = split_corrections(result["markdown"])
    result["markdown"] = note
    result["corrections"] = corrections

    # Une régénération plus récente est arrivée pendant que celle-ci
    # attendait le modèle : ce résultat est périmé, on ne l'écrit jamais en
    # base. Le navigateur qui l'a demandé a de toute façon déjà annulé sa
    # requête — voir ``pendingGenerate`` côté JS — mais rien ne garantit
    # qu'un thread abandonné ne finisse pas quand même par répondre.
    if not _generation_guard.is_current(payload.consultation_id, generation_seq):
        logger.info(
            "Génération abandonnée pour la consultation %s : supplantée par "
            "une tentative plus récente",
            payload.consultation_id,
        )
        return {
            "markdown": result["markdown"],
            "superseded": True,
            "consultation_id": payload.consultation_id,
        }

    # --- Persistance ------------------------------------------------------
    if payload.consultation_id:
        consultation = _get_owned_consultation(db, payload.consultation_id, user)
    else:
        consultation = Consultation(owner=user.owner_key)
        db.add(consultation)

    consultation.template_id = template_row.id
    consultation.template_name = template_row.name
    consultation.raw_transcript = payload.transcript
    consultation.generated_markdown = result["markdown"]
    # Rubrique « Corrections et éléments à valider », retirée du corps : elle
    # suit la note et meurt avec le brouillon, comme ``verification_json``.
    consultation.corrections_markdown = result["corrections"]
    # Un audit « Validation » encore présent viendrait de la PRÉCÉDENTE note
    # (même précaution que ``corrections_markdown``) : toute régénération le
    # réinitialise ici — la base ne porte jamais un audit périmé pendant la
    # fenêtre où une relecture client (poll de complétion, réouverture) ou un
    # rechargement de page pourrait l'afficher à mauvais escient.
    consultation.verification_json = None
    # Toujours écrasée, y compris sur une régénération : l'interface ne montre
    # plus jamais cette valeur qu'à l'ouverture (voir loadDraft côté JS) et
    # prévient déjà l'usager AVANT l'appel si des modifications seraient
    # perdues — la garder ici en plus ferait diverger les deux copies
    # silencieusement, exactement le bogue que ce choix corrige.
    consultation.edited_markdown = result["markdown"]

    # Liste pointée des médicaments, recalculée sur le transcript (par défaut
    # brut, la normalisation inline n'étant plus appliquée à la génération) :
    # la liste et la note concordent, et le JSON d'audit suit la régénération.
    if _med_grounding_on():
        try:
            _apply_grounding(
                db, consultation,
                origin_tab=request.headers.get("x-consultai-tab", ""),
            )
        except Exception:
            logger.exception("Grounding liste incomplète (génération %s)", consultation.id)

    # Ce que le médecin a saisi lui-même est repris tel quel et fait autorité.
    for field, column in (
        ("reason", "reason"),
        ("requester", "requester"),
        ("accompanied_by", "accompanied_by"),
        ("consultation_date", "consultation_date"),
    ):
        value = getattr(payload, field).strip()
        if value:
            setattr(consultation, column, value)

    # Puis on complète les champs restés vides à partir de la dictée. Cet
    # appel est délibérément tolérant : la note est déjà produite et enregistrée
    # juste après, une métadonnée manquante ne justifie pas de la perdre.
    extracted = await run_in_threadpool(
        extract_metadata, payload.transcript, result["markdown"]
    )
    metadata = _apply_metadata(consultation, extracted)

    consultation.title = _build_title(consultation, template_row.name)
    consultation.model_used = result["model"]
    consultation.llm_provider = result["provider"]
    consultation.audio_used = result["audio_used"]
    consultation.transcript_used = result["transcript_used"]
    consultation.usage_prompt_tokens = result["usage"].get("prompt_tokens")
    consultation.usage_output_tokens = result["usage"].get("output_tokens")
    consultation.generation_seconds = result["elapsed_seconds"]
    # Une note réellement produite : comptée UNE fois, à sa première
    # génération — la régénération d'un brouillon déjà « genere » ne re-compte
    # pas (le compteur NotesDaily survit à la purge des dossiers).
    first_generation = consultation.status not in ("genere", "finalise")
    consultation.status = "genere"
    consultation.updated_at = utcnow()
    if first_generation:
        usage.count_note(db, owner=user.owner_key)
    usage.log_llm_usage(
        db, owner=user.owner_key, consultation_id=consultation.id,
        provider=result["provider"], model=result["model"],
        prompt_tokens=result["usage"].get("prompt_tokens"),
        output_tokens=result["usage"].get("output_tokens"),
        audio_prompt_tokens=result["usage"].get("audio_prompt_tokens"),
        cached_tokens=result["usage"].get("cached_tokens"),
    )
    db.commit()
    db.refresh(consultation)
    live.publish(user.owner_key, "generated", {
        "consultation_id": consultation.id,
        "updated_at": _iso(consultation.updated_at),
        "origin_tab": request.headers.get("x-consultai-tab", ""),
    })

    logger.info(
        "Note générée pour %s (gabarit « %s », %s / %s, %d caractères)",
        user.username, template_row.name, result["provider"], result["model"],
        len(result["markdown"]),
    )

    # « Validation » : audit factuel (audio↔note, ou transcription seule si le
    # fournisseur ne reçoit pas l'audio) en tâche de fond — jamais sur le
    # chemin de la réponse ; le résultat partira en SSE quand il sera prêt.
    if (
        payload.second_pass
        and payload.consultation_id
        and llm.verification_capable()
        and (audio_payload is not None or (payload.transcript or "").strip())
    ):
        tache_verif = asyncio.create_task(_run_second_pass(
            owner_key=user.owner_key,
            consultation_id=consultation.id,
            generation_seq=generation_seq,
            generation_token=payload.generation_token,
            origin_tab=request.headers.get("x-consultai-tab", ""),
            note_markdown=result["markdown"],
            transcript=payload.transcript,
            langue=template_row.language,
            audio_payload=audio_payload,
            provider=llm.active_provider(),
            model_name=llm.verify_model(),
            system_instruction=system_prompt,
        ))
        _taches_fond.add(tache_verif)
        tache_verif.add_done_callback(_taches_fond.discard)

    return {
        "markdown": result["markdown"],
        "corrections": result.get("corrections", ""),
        "model": result["model"],
        "provider": result["provider"],
        "stt_used": " / ".join(
            p for p in (consultation.stt_provider, consultation.stt_model) if p
        ) if result["transcript_used"] else "",
        "llm_used": " / ".join(p for p in (result["provider"], result["model"]) if p),
        "audio_used": result["audio_used"],
        "truncated": result["truncated"],
        "usage": result["usage"],
        "elapsed_seconds": result["elapsed_seconds"],
        "consultation_id": consultation.id,
        "template_name": template_row.name,
        "metadata": metadata,
    }


# ===========================================================================
# Diffusion en direct (SSE)
# ===========================================================================
@app.get("/api/events")
async def stream_events(request: Request):
    """
    Flux permanent : chaque évènement publié par live.publish() pour cet
    usager (consultation modifiée, tranche dictée, note générée,
    enregistrement ajouté/retiré) rejoint aussitôt tous ses onglets ouverts.

    Un « ping » toutes les 10 s comble les silences : sans lui, un proxy
    inverse aux réglages par défaut (souvent 30-60 s d'inactivité max) peut
    couper une réponse HTTP qui ne renvoie rien pendant plusieurs minutes,
    même si la connexion elle-même reste parfaitement valide.
    """
    user = current_user(request)
    queue = live.subscribe(user.owner_key)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=10)
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            live.unsubscribe(user.owner_key, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Sans cet en-tête, nginx tamponne la réponse par défaut et rien
            # n'arrive au navigateur avant plusieurs Ko accumulés — annulant
            # tout l'intérêt d'un flux en direct.
            "X-Accel-Buffering": "no",
        },
    )


# ===========================================================================
# Consultations (brouillons)
# ===========================================================================
@app.get("/api/consultations")
def list_consultations(
    request: Request,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """
    Brouillons de l'utilisateur courant, de la dictée la plus récente à la plus
    ancienne.

    Le tri porte sur ``created_at``, c'est-à-dire le moment de la dictée, et
    non sur ``updated_at`` : rouvrir un vieux brouillon pour en corriger une
    virgule ne doit pas le propulser en tête de liste, où le médecin cherche
    la consultation qu'il vient de faire.
    """
    user = current_user(request)
    limit = max(1, min(limit, 200))
    # Les dictées abandonnées par un onglet mort sont traitées ici — c'est le
    # moment où le médecin regarde la liste : audio archivé dans le brouillon,
    # brouillon marqué « abandonnée », sessions sans contenu (< 10 s) purgées.
    # AVANT purge_expired : une session à conserver ne doit pas être emportée
    # par la purge de rétention au même accès.
    dictation.cleanup_abandoned(
        user.username, db, request.headers.get("x-consultai-tab", ""),
    )
    dictation.purge_expired()
    purge_expired_consultations(db)
    rows = db.scalars(
        select(Consultation)
        .where(Consultation.owner == user.owner_key)
        .order_by(Consultation.created_at.desc())
        .limit(limit)
    ).all()
    retention_hours = int(runtime_config.value_float("consultation_retention_hours", 12.0))
    return {
        "consultations": [row.to_dict(include_body=False) for row in rows],
        "retention_hours": retention_hours,
    }


@app.post("/api/consultations", status_code=status.HTTP_201_CREATED)
def create_consultation(
    payload: ConsultationIn,
    request: Request,
    db: Session = Depends(get_db),
):
    user = current_user(request)

    template_name = ""
    if payload.template_id:
        template_row = db.get(TemplateModel, payload.template_id)
        template_name = template_row.name if template_row else ""

    consultation = Consultation(
        owner=user.owner_key,
        title=payload.title.strip() or "Consultation sans titre",
        reason=payload.reason.strip(),
        template_id=payload.template_id,
        template_name=template_name,
        raw_transcript=payload.raw_transcript,
        status="brouillon",
    )
    db.add(consultation)
    db.commit()
    db.refresh(consultation)
    live.publish(user.owner_key, "consultation_created", {
        "consultation_id": consultation.id,
        "title": consultation.title,
        "created_at": _iso(consultation.created_at),
        "origin_tab": request.headers.get("x-consultai-tab", ""),
    })
    return consultation.to_dict()


@app.get("/api/consultations/{consultation_id}")
def get_consultation(consultation_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    return _get_owned_consultation(db, consultation_id, user).to_dict()


@app.patch("/api/consultations/{consultation_id}")
def patch_consultation(
    consultation_id: int,
    payload: ConsultationPatch,
    request: Request,
    db: Session = Depends(get_db),
):
    """Sauvegarde automatique : seuls les champs transmis sont modifiés."""
    user = current_user(request)
    consultation = _get_owned_consultation(db, consultation_id, user)

    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in updates.items():
        if key == "template_id" and value:
            template_row = db.get(TemplateModel, value)
            consultation.template_name = template_row.name if template_row else ""
        setattr(consultation, key, value)

    # L'alignement confiance↔texte n'est rompu que par une édition RÉELLE du
    # transcript. Une retranscription vient d'écrire le texte ET la confiance
    # dans la même transaction ; le client re-sauvegarde ensuite ce même texte
    # via son debounce de sauvegarde — renvoyer le texte inchangé ne doit PAS
    # perdre la confiance. On compare après normalisation des espaces : le
    # client reformate les phrases en une ligne chacune (formatSentences), le
    # serveur stocke les blocs STT — seuls les sauts/espaces diffèrent alors.
    # La confiance est RETENUE tant que le transcript existe : elle est
    # indexée par clé phonétique (norm_phon) et ré-alignée à la lecture
    # (voir med_grounding), donc elle résiste aux éditions mineures. On ne la
    # purge que si le texte est réellement vidé — il n'y a alors plus rien à
    # cui aligner.
    if "raw_transcript" in updates:
        nouveau = " ".join((updates["raw_transcript"] or "").split())
        courant = " ".join((consultation.raw_transcript or "").split())
        if nouveau != courant and not nouveau:
            consultation.transcript_conf = None

    consultation.updated_at = utcnow()
    db.commit()
    db.refresh(consultation)
    live.publish(user.owner_key, "consultation_patched", {
        "consultation_id": consultation.id,
        "updated_at": _iso(consultation.updated_at),
        "fields": list(updates.keys()),
        "origin_tab": request.headers.get("x-consultai-tab", ""),
    })
    return consultation.to_dict(include_body=False)


def purge_expired_consultations(db: Session) -> int:
    """
    Supprime les dossiers dont la dernière modification dépasse le délai de
    rétention réglé dans le panneau admin (0 = purge désactivée). Toutes les
    consultations sont concernées, tous propriétaires confondus — c'est une
    politique de conservation des données patient, pas un filtre par usager.
    """
    hours = int(runtime_config.value_float("consultation_retention_hours", 12.0))
    if hours <= 0:
        return 0
    cutoff = utcnow() - timedelta(hours=hours)
    rows = db.scalars(select(Consultation).where(Consultation.updated_at < cutoff)).all()
    for consultation in rows:
        recordings.delete_for_consultation(db, consultation.id)
        db.delete(consultation)
    if rows:
        db.commit()
        logger.info(
            "Purge des dossiers : %d consultation(s) de plus de %d heure(s) supprimée(s)",
            len(rows), hours,
        )
    return len(rows)


@app.delete("/api/consultations/{consultation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_consultation(consultation_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Supprime le brouillon **et tout ce qui en dépend** : transcription, note et
    enregistrements audio. Un seul geste doit suffire à ne rien laisser
    derrière — c'est la contrepartie de la conservation de l'audio.
    """
    user = current_user(request)
    consultation = _get_owned_consultation(db, consultation_id, user)
    removed = recordings.delete_for_consultation(db, consultation_id)
    db.delete(consultation)
    db.commit()
    logger.info(
        "Consultation %d supprimée par %s (%d enregistrement(s))",
        consultation_id, user.username, removed,
    )
    live.publish(user.owner_key, "consultation_deleted", {
        "consultation_id": consultation_id,
        "origin_tab": request.headers.get("x-consultai-tab", ""),
    })
    return None


# ===========================================================================
# Enregistrements audio
# ===========================================================================
def _get_owned_recording(db: Session, recording_id: int, user: Principal) -> Recording:
    recording = db.get(Recording, recording_id)
    if recording is None or recording.owner != user.username:
        raise HTTPException(status_code=404, detail=_t("err.recording_not_found"))
    return recording


@app.get("/api/consultations/{consultation_id}/recordings")
def list_recordings(consultation_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    _get_owned_consultation(db, consultation_id, user)
    return {
        "recordings": [row.to_dict() for row in recordings.for_consultation(db, consultation_id)]
    }


class RetranscribeIn(BaseModel):
    template_id: Optional[int] = None


@app.post("/api/consultations/{consultation_id}/retranscribe")
async def retranscribe_consultation(
    consultation_id: int,
    payload: RetranscribeIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Renvoie l'audio conservé au service vocal, dans la langue du gabarit donné.

    Raison d'être : la dictée commence souvent avant que le gabarit soit
    choisi. La transcription part alors dans la langue par défaut, et le
    médecin se retrouve avec une consultation anglaise transcrite en français —
    illisible, et irrécupérable par la mise en forme, puisque le modèle reçoit
    déjà des mots faux.

    Remplace la transcription au lieu de s'y ajouter : c'est le MÊME audio,
    reconnu autrement. L'ajouter à la suite donnerait le texte en double.
    L'appel est bloquant — le médecin attend le résultat pour continuer.
    """
    user = current_user(request)
    consultation = _get_owned_consultation(db, consultation_id, user)

    # Réservé AVANT la boucle de transcription (potentiellement longue, un
    # ou plusieurs enregistrements) : si une autre retranscription pour cette
    # même consultation démarre pendant que celle-ci tourne encore, c'est ce
    # compteur qui décide laquelle des deux a le droit d'écrire son résultat.
    retranscribe_seq = _retranscribe_guard.begin(consultation_id)

    pistes = recordings.for_consultation(db, consultation_id)
    if not pistes:
        raise HTTPException(status_code=409, detail=_t("err.retranscribe_no_audio"))

    # Langue et vocabulaire du gabarit visé. Le gabarit commande toute la
    # chaîne — code de langue envoyé au service, lexique francophone joint ou
    # non ; c'est exactement ce qu'on cherche à changer ici.
    hints, langue = "", ""
    if payload.template_id:
        gabarit = db.get(TemplateModel, payload.template_id)
        if gabarit is None:
            raise HTTPException(status_code=404, detail=_t("err.template_not_found"))
        hints = gabarit.phrase_hints or ""
        langue = gabarit.language or ""
    preferences.bind_document_language(langue or None)

    # Plusieurs enregistrements = plusieurs passes de dictée sur le même
    # brouillon. On les reprend dans l'ordre de création, comme ils ont été
    # dictés, et on recompose le texte dans le même ordre. La barre de
    # progression du navigateur avance de la somme des durées : on la nourrit
    # après chaque tranche (endpoint custom) et à la fin de chaque
    # enregistrement, par SSE.
    total_secondes = sum(float(p.duration_seconds or 0) for p in pistes) or 0.0
    done_secondes = 0.0
    morceaux: List[str] = []
    conf_maps: List[dict] = []
    secondes = 0.0
    moteur = ("", "")
    dernier_refus = ""
    for index, piste in enumerate(pistes):
        chemin = recordings.absolute_path(piste)
        if not os.path.exists(chemin):
            logger.warning(
                "Retranscription %s : fichier absent pour l'enregistrement %s",
                consultation_id, piste.id,
            )
            continue
        done_avant = done_secondes

        def _publier_progres(curseur: float, duree: float, _idx=index, _fait=done_avant) -> None:
            if total_secondes:
                percent = min(100.0, 100.0 * (_fait + curseur) / total_secondes)
            else:
                percent = None
            live.publish(user.owner_key, "transcription_progress", {
                "consultation_id": consultation_id,
                "percent": percent,
                "cursor_seconds": round(curseur, 1),
                "duration_seconds": round(duree, 1),
                "recording_index": _idx,
                "recordings_total": len(pistes),
                "origin_tab": request.headers.get("x-consultai-tab", ""),
            })

        with open(chemin, "rb") as handle:
            brut = handle.read()
        try:
            resultat = await run_in_threadpool(
                transcribe, brut, piste.mime_type, hints, on_progress=_publier_progres,
            )
        except TranscriptionError as exc:
            # Un enregistrement muet ne doit pas emporter les autres. Une
            # consultation en compte souvent plusieurs, dont un faux départ de
            # quelques secondes : le refuser en bloc ferait perdre les minutes
            # utiles qui suivent. On note, on passe au suivant, et l'absence
            # totale de résultat est traitée après la boucle.
            dernier_refus = str(exc)
            logger.warning(
                "Retranscription %s : enregistrement %s écarté — %s",
                consultation_id, piste.id, exc,
            )
            continue
        except Exception as exc:  # pragma: no cover
            logger.exception("Erreur inattendue pendant la retranscription %s", consultation_id)
            raise HTTPException(
                status_code=502, detail=_t("err.transcription", error=exc)
            ) from exc

        texte = (resultat.get("transcript") or "").strip()
        if texte:
            conf_map = {}
            if resultat.get("words"):
                try:
                    conf_map = med_grounding.conf_par_token(
                        texte, resultat.get("words") or [],
                    )
                except Exception:
                    conf_map = {}
            morceaux.append(texte)
            conf_maps.append(conf_map)
        secondes += float(resultat.get("duration_seconds") or 0)
        done_secondes += float(resultat.get("duration_seconds") or 0)
        if resultat.get("provider"):
            moteur = (resultat["provider"], resultat.get("model") or "")

    if not morceaux:
        # Aucun fichier lisible, ou du silence partout : mieux vaut refuser que
        # remplacer une transcription existante par du vide. On remonte le
        # dernier refus du service plutôt qu'un message générique — « aucune
        # parole détectée » et « clé refusée » n'appellent pas la même suite.
        raise HTTPException(
            status_code=422, detail=dernier_refus or _t("err.retranscribe_empty")
        )

    # Une retranscription plus récente est arrivée pendant que celle-ci
    # attendait le service vocal (un ou plusieurs enregistrements, donc
    # potentiellement long) : ce résultat est périmé, on ne l'écrit jamais en
    # base. Le navigateur qui l'a demandé a de toute façon déjà annulé sa
    # requête côté client, mais rien ne garantit qu'un thread abandonné ne
    # finisse pas quand même par répondre.
    if not _retranscribe_guard.is_current(consultation_id, retranscribe_seq):
        logger.info(
            "Retranscription abandonnée pour la consultation %s : supplantée "
            "par une tentative plus récente",
            consultation_id,
        )
        return {"transcript": "\n\n".join(morceaux), "superseded": True}

    consultation.raw_transcript = "\n\n".join(morceaux)
    # La confiance est conservée (retention tant que le transcript existe) :
    # on fusionne les confiances de la retranscription dans le mapping déjà
    # présent plutôt que de le vider. Un enregistrement écarté (muet, absent)
    # ne fait ainsi pas perdre la confiance des autres. ``_merge_conf_into``
    # garde la valeur la plus basse — la prudence demeure.
    for cmap in conf_maps:
        _merge_conf_into(consultation, cmap)
    consultation.audio_seconds = int(round(secondes))
    consultation.status = "transcrit"
    if moteur[0]:
        consultation.stt_provider, consultation.stt_model = moteur
        usage.log_stt_usage(
            db, owner=user.owner_key, consultation_id=consultation.id,
            provider=moteur[0], model=moteur[1], audio_seconds=int(round(secondes)),
        )
    consultation.stt_language = preferences.document_language()
    consultation.updated_at = utcnow()
    db.commit()
    live.publish(user.owner_key, "consultation_patched", {
        "consultation_id": consultation.id,
        "updated_at": _iso(consultation.updated_at),
        "fields": ["raw_transcript", "status", "stt_language"],
        "origin_tab": request.headers.get("x-consultai-tab", ""),
    })

    logger.info(
        "Retranscription de la consultation %s pour %s : %d enregistrement(s), "
        "langue %s, %d caractères",
        consultation_id, user.username, len(morceaux),
        consultation.stt_language, len(consultation.raw_transcript),
    )
    reponse = {
        "transcript": consultation.raw_transcript,
        "stt_language": consultation.stt_language,
        "stt_used": " / ".join(p for p in moteur if p),
        "duration_seconds": consultation.audio_seconds,
        "recordings": len(morceaux),
        "recordings_total": len(pistes),
    }
    if _med_grounding_on():
        reponse["med_items"] = _apply_grounding(
            db, consultation,
            origin_tab=request.headers.get("x-consultai-tab", ""),
        )
    return reponse


@app.get("/api/recordings/{recording_id}/audio")
def stream_recording(recording_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Sert le fichier audio pour la réécoute.

    ``inline`` et non ``attachment`` : le lecteur de la page doit pouvoir le
    lire sans le télécharger d'abord. Le fichier reste derrière le SSO comme
    tout le reste — c'est la voix du patient.
    """
    user = current_user(request)
    recording = _get_owned_recording(db, recording_id, user)
    path = Path(recordings.absolute_path(recording))
    if not path.exists():
        raise HTTPException(
            status_code=410,
            detail=_t("err.recording_gone"),
        )
    return FileResponse(
        path,
        media_type=recording.mime_type,
        headers={"Content-Disposition": f'inline; filename="dictee-{recording.id}{path.suffix}"'},
    )


@app.delete("/api/recordings/{recording_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recording(recording_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    recording = _get_owned_recording(db, recording_id, user)
    consultation_id = recording.consultation_id  # lu avant delete() : la ligne est détruite ensuite
    recordings.delete(db, recording)
    logger.info("Enregistrement %d supprimé par %s", recording_id, user.username)
    live.publish(user.owner_key, "recording_deleted", {
        "consultation_id": consultation_id,
        "recording_id": recording_id,
        "origin_tab": request.headers.get("x-consultai-tab", ""),
    })
    return None
