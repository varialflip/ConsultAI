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
    GET    /api/models                   modèles Gemini accessibles (diagnostic)

    GET    /api/templates                liste des gabarits
    POST   /api/templates                création          (administrateur)
    GET    /api/templates/{id}           détail
    PUT    /api/templates/{id}           modification      (administrateur)
    POST   /api/templates/{id}/duplicate copie             (administrateur)
    DELETE /api/templates/{id}           suppression       (administrateur)

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
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app import __version__
from app import dictation, i18n, llm, oidc, preferences, recordings, runtime_config, stt
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
    Group,
    SessionLocal,
    Recording,
    Template as TemplateModel,
    User,
    get_db,
    init_db,
    utcnow,
)
from app.dictation import DictationError, SequenceMismatch, SessionNotFound
from app.llm import GenerationError, extract_metadata, generate_note, list_available_models
from app.stt import TranscriptionError, transcribe

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
    dictation.purge_expired()

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
            "inscription automatique : %s",
            settings.oidc_provider_url or "(non configuré)",
            settings.effective_redirect_uri or "(non configurée)",
            _comptes,
            "oui" if users_service.allow_signup() else "non",
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
    yield
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
    max_age=settings.session_max_age_seconds,
    # « lax » et non « strict » : le fournisseur d'identité nous renvoie par une
    # navigation venue d'un autre site, et « strict » ferait perdre le témoin
    # portant l'état du flux — la connexion échouerait systématiquement.
    same_site="lax",
    https_only=settings.session_https_only,
)


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
            "short_name": "ConsultAI",
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
            "orientation": "any",
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
    patient_name: str = Field("", max_length=200)
    patient_ref: str = Field("", max_length=120)
    reason: str = Field("", max_length=300)
    template_id: Optional[int] = None
    raw_transcript: str = ""


class ConsultationPatch(BaseModel):
    """Sauvegarde automatique : tous les champs sont optionnels."""

    title: Optional[str] = Field(None, max_length=300)
    patient_name: Optional[str] = Field(None, max_length=200)
    patient_ref: Optional[str] = Field(None, max_length=120)
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
    transcript: str = Field(..., min_length=1)
    consultation_id: Optional[int] = None
    # Métadonnées d'identification. Le médecin peut les saisir avant la
    # dictée, mais le cas normal est qu'elles restent vides ici et soient
    # relues dans la dictée après la mise en forme (voir plus bas).
    patient_name: str = Field("", max_length=200)
    patient_ref: str = Field("", max_length=200)          # numéro de dossier
    reason: str = Field("", max_length=300)
    consultation_date: str = Field("", max_length=60)
    requester: str = Field("", max_length=200)
    accompanied_by: str = Field("", max_length=200)
    extra_instructions: str = Field("", max_length=4000)
    # Bascule ponctuelle vers le modèle « pro » pour une dictée difficile.
    use_pro: bool = False


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
    if payload.patient_name.strip():
        lines.append(f"Patient : {payload.patient_name.strip()}")
    if payload.patient_ref.strip():
        lines.append(f"Numéro de dossier : {payload.patient_ref.strip()}")
    if payload.reason.strip():
        lines.append(f"Raison de consultation : {payload.reason.strip()}")
    if payload.requester.strip():
        lines.append(f"Demande de : {payload.requester.strip()}")
    if payload.accompanied_by.strip():
        lines.append(f"Accompagné de : {payload.accompanied_by.strip()}")
    return lines


def _prepare_audio_for_generation(
    db: Session, consultation_id: int
) -> Optional[Tuple[bytes, str]]:
    """
    Extrait audio (silences plafonnés) à joindre à la génération, ou ``None``.

    Best-effort à dessein : aucun enregistrement, ffmpeg indisponible, ou
    dictée trop longue (``gemini_send_audio_max_minutes``) retombent tous
    sur ``None`` — la note se génère alors comme avant, sur la seule
    transcription. Ce n'est jamais une raison de faire échouer la génération,
    mais chaque repli est journalisé : silencieux pour l'usager, visible dans
    les journaux, sinon un audio manquant est indiscernable d'un bogue.
    """
    pistes = recordings.for_consultation(db, consultation_id)
    if not pistes:
        logger.info(
            "Audio non joint (consultation %s) : aucun enregistrement conservé",
            consultation_id,
        )
        return None
    piste = pistes[-1]  # le plus récent : celui de la dictée en cours
    try:
        chemin = recordings.absolute_path(piste)
        trimmed = stt.compress_silence(chemin)
    except OSError as exc:
        logger.warning(
            "Audio non joint (consultation %s, enregistrement %s) : %s",
            consultation_id, piste.id, exc,
        )
        return None
    if trimmed is None:
        logger.info(
            "Audio non joint (consultation %s, enregistrement %s) : "
            "retrait des silences indisponible ou désactivé",
            consultation_id, piste.id,
        )
        return None
    content, duree = trimmed
    plafond = runtime_config.value_float("gemini_send_audio_max_minutes", 20.0) * 60
    if duree <= 0 or duree > plafond:
        logger.info(
            "Audio non joint (consultation %s, enregistrement %s) : "
            "%.1f s après retrait des silences, hors bornes (plafond %.0f s)",
            consultation_id, piste.id, duree, plafond,
        )
        return None
    logger.info(
        "Audio joint (consultation %s, enregistrement %s) : %.1f s après "
        "retrait des silences",
        consultation_id, piste.id, duree,
    )
    return content, "audio/ogg"


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
_METADATA_TO_COLUMN = {
    "patient_name": "patient_name",
    "record_number": "patient_ref",
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
    identity = " · ".join(
        part for part in (consultation.patient_name, consultation.patient_ref) if part
    )
    parts = [identity or "Consultation", consultation.reason or template_name]
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
        f"const VERSION = 'consultai-v{__version__}';", source, count=1,
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
@app.get("/auth/login", include_in_schema=False)
async def auth_login(request: Request):
    """Amorce le flux, ou renvoie à l'accueil si la session est déjà ouverte."""
    from app.auth import session_identity

    suite = oidc.safe_next_path(request.query_params.get("next", "/"))

    if session_identity(request):
        return RedirectResponse(suite, status_code=302)

    if not settings.oidc_configured:
        return _auth_error_page(
            _t("auth.not_configured"),
            status_code=503,
        )

    # Mémorisé côté session plutôt que passé au fournisseur : ce dernier ne
    # renvoie que « state » et « code », et un paramètre supplémentaire dans
    # l'adresse de retour serait refusé pour non-concordance.
    request.session["consultai_next"] = suite
    try:
        return await oidc.authorization_redirect(request)
    except oidc.OidcError as exc:
        return _auth_error_page(str(exc), status_code=503)


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
    store_identity(request, {"sub": user.subject, "username": user.username})
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

    cible = ""
    if settings.oidc_configured:
        try:
            cible = await oidc.end_session_url(id_token)
        except Exception as exc:  # la déconnexion locale a déjà eu lieu
            logger.info("Déconnexion du fournisseur impossible : %s", exc)

    return RedirectResponse(cible or (settings.base_url or "/"), status_code=302)


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


@app.get("/api/config")
async def api_config(request: Request):
    """Configuration non sensible, consommée par le frontend."""
    user = current_user(request)
    langue = runtime_config.language()
    return {
        "app_title": settings.app_title,
        "version": __version__,
        # Langue de l'interface. Le client s'en sert pour le formatage des
        # dates et pour savoir s'il doit recharger la page après un
        # changement de réglage.
        "language": langue,
        "stt_language": runtime_config.stt_language(runtime_config.value("stt_provider")),
        "llm_provider": runtime_config.value("llm_provider"),
        "llm_model": llm.active_model(),
        "stt_provider": runtime_config.value("stt_provider"),
        "gemini_backend": "vertex" if settings.gemini_use_vertex else "api_key",
        "max_audio_mb": settings.max_audio_mb,
        "is_template_admin": user.is_template_admin,
        # Cadence de téléversement de la dictée : c'est le navigateur qui la
        # règle sur MediaRecorder, mais le serveur qui en décide.
        "dictation_chunk_seconds": settings.dictation_chunk_seconds,
        "dictation_segment_seconds": settings.dictation_segment_seconds,
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
        "language": langue,
    }


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
    """Liste complète (corps inclus : l'éditeur en a besoin immédiatement)."""
    current_user(request)
    rows = db.scalars(
        select(TemplateModel).order_by(TemplateModel.sort_order, TemplateModel.name)
    ).all()
    return {"templates": [row.to_dict() for row in rows]}


@app.get("/api/templates/{template_id}")
def get_template(template_id: int, request: Request, db: Session = Depends(get_db)):
    current_user(request)
    row = db.get(TemplateModel, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_t("err.template_not_found"))
    return row.to_dict()


@app.post("/api/templates", status_code=status.HTTP_201_CREATED)
def create_template(
    payload: TemplateIn,
    admin: Principal = Depends(require_template_admin),
    db: Session = Depends(get_db),
):
    row = TemplateModel(
        name=payload.name,
        description=payload.description,
        system_instructions=payload.system_instructions,
        layout_format=payload.layout_format,
        phrase_hints=payload.phrase_hints,
        sort_order=payload.sort_order,
        language=payload.language,
        is_default=False,
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
    logger.info("Gabarit créé « %s » par %s", row.name, admin.username)
    return row.to_dict()


@app.put("/api/templates/{template_id}")
def update_template(
    template_id: int,
    payload: TemplateIn,
    admin: Principal = Depends(require_template_admin),
    db: Session = Depends(get_db),
):
    row = db.get(TemplateModel, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_t("err.template_not_found"))
    # Le refus est ICI et pas seulement dans l'écran : masquer un bouton n'est
    # pas un contrôle, et ces deux gabarits sont les points de départ garantis
    # de l'installation.
    if row.is_locked:
        raise HTTPException(status_code=403, detail=_t("err.template_locked"))

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
    logger.info("Gabarit modifié « %s » par %s", row.name, admin.username)
    return row.to_dict()


@app.post("/api/templates/{template_id}/duplicate", status_code=status.HTTP_201_CREATED)
def duplicate_template(
    template_id: int,
    admin: Principal = Depends(require_template_admin),
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

    C'est le SEUL chemin pour adapter un gabarit verrouillé, et il fonctionne
    sur lui : la copie est une ligne neuve et indépendante, pas une référence.
    """
    source = db.get(TemplateModel, template_id)
    if source is None:
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
    )
    db.add(copy)
    db.commit()
    db.refresh(copy)
    logger.info("Gabarit dupliqué « %s » → « %s » par %s", source.name, copy.name, admin.username)
    return copy.to_dict()


@app.delete("/api/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: int,
    admin: Principal = Depends(require_template_admin),
    db: Session = Depends(get_db),
):
    """
    Supprime un gabarit.

    Les gabarits VERROUILLÉS sont refusés : ce sont les points de départ
    garantis de l'installation, et on les duplique pour les adapter.

    Les deux gabarits modifiables livrés (« Consultation - Gériatrie »,
    « Suivi ») se suppriment normalement et ne reviennent PAS : ils ne sont
    amorcés qu'une fois, voir database.seed_editable_templates.
    """
    row = db.get(TemplateModel, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_t("err.template_not_found"))
    # Contrôlé ICI et pas seulement masqué dans l'écran.
    if row.is_locked:
        raise HTTPException(status_code=403, detail=_t("err.template_locked"))

    remaining = db.scalar(select(TemplateModel.id).where(TemplateModel.id != template_id))
    if remaining is None:
        raise HTTPException(
            status_code=409,
            detail=_t("err.template_last"),
        )

    name = row.name
    db.delete(row)
    db.commit()
    logger.info("Gabarit supprimé « %s » par %s", name, admin.username)
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
        # Appel bloquant (réseau + ffmpeg) : exécuté hors de la boucle asyncio.
        result = await run_in_threadpool(
            transcribe, raw, file.content_type or "", extra_hints
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
            consultation.audio_seconds = (consultation.audio_seconds or 0) + result["duration_seconds"]
            consultation.status = "transcrit"
            if result.get("provider"):
                consultation.stt_provider = result["provider"]
                consultation.stt_model = result.get("model") or ""
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
def list_dictations(request: Request):
    """
    Dictées ouvertes restées sur le serveur — typiquement un onglet fermé en
    plein enregistrement. Le navigateur les propose à la reprise.
    """
    user = current_user(request)
    return {"sessions": dictation.list_sessions(user.username)}


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

    if dictation.should_process(session):
        _schedule_dictation_processing(session_id, user.username)

    return session.to_public()


@app.get("/api/dictation/{session_id}")
def get_dictation(session_id: str, request: Request):
    """Avancement de la dictée : le navigateur y lit les tranches transcrites."""
    user = current_user(request)
    return _dictation_session(session_id, user).to_public()


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
        try:
            stored = await run_in_threadpool(
                recordings.store_path, db, consultation, session.audio_path,
                session.mime_type, result["transcribed_seconds"], "dictee",
            )
            result["recording_id"] = stored.id if stored else None
        except OSError as exc:
            logger.warning("Dictée %s : audio non conservé — %s", session_id, exc)

    dictation.delete_session(session)
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
    logger.info("Dictée %s abandonnée par %s", session_id, user.username)
    return None


# ===========================================================================
# Génération de la note
# ===========================================================================
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

    audio_payload = None
    audio_enabled = runtime_config.value("gemini_send_audio") == "true"
    if audio_enabled and llm.active_provider() == "gemini" and payload.consultation_id:
        audio_payload = await run_in_threadpool(
            _prepare_audio_for_generation, db, payload.consultation_id
        )
    elif audio_enabled:
        logger.info(
            "Audio non tenté (consultation %s) : fournisseur « %s » ou aucune "
            "consultation associée",
            payload.consultation_id, llm.active_provider(),
        )

    try:
        result = await run_in_threadpool(
            generate_note,
            payload.transcript,
            template_row.system_instructions,
            template_row.layout_format,
            _build_context_lines(payload),
            payload.extra_instructions,
            model_name,
            template_row.language,
            audio_payload,
        )
    except GenerationError as exc:
        logger.warning("Génération refusée pour %s : %s", user.username, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Erreur inattendue pendant la génération")
        raise HTTPException(status_code=502, detail=_t("err.generation", error=exc)) from exc

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
    # Toujours écrasée, y compris sur une régénération : l'interface ne montre
    # plus jamais cette valeur qu'à l'ouverture (voir loadDraft côté JS) et
    # prévient déjà l'usager AVANT l'appel si des modifications seraient
    # perdues — la garder ici en plus ferait diverger les deux copies
    # silencieusement, exactement le bogue que ce choix corrige.
    consultation.edited_markdown = result["markdown"]

    # Ce que le médecin a saisi lui-même est repris tel quel et fait autorité.
    for field, column in (
        ("patient_name", "patient_name"),
        ("patient_ref", "patient_ref"),
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
    consultation.usage_prompt_tokens = result["usage"].get("prompt_tokens")
    consultation.usage_output_tokens = result["usage"].get("output_tokens")
    consultation.generation_seconds = result["elapsed_seconds"]
    consultation.status = "genere"
    consultation.updated_at = utcnow()
    db.commit()
    db.refresh(consultation)

    logger.info(
        "Note générée pour %s (gabarit « %s », %s / %s, %d caractères)",
        user.username, template_row.name, result["provider"], result["model"],
        len(result["markdown"]),
    )
    return {
        "markdown": result["markdown"],
        "model": result["model"],
        "provider": result["provider"],
        "stt_used": " / ".join(
            p for p in (consultation.stt_provider, consultation.stt_model) if p
        ),
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
    rows = db.scalars(
        select(Consultation)
        .where(Consultation.owner == user.owner_key)
        .order_by(Consultation.created_at.desc())
        .limit(limit)
    ).all()
    return {"consultations": [row.to_dict(include_body=False) for row in rows]}


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
        patient_name=payload.patient_name.strip(),
        patient_ref=payload.patient_ref.strip(),
        reason=payload.reason.strip(),
        template_id=payload.template_id,
        template_name=template_name,
        raw_transcript=payload.raw_transcript,
        status="brouillon",
    )
    db.add(consultation)
    db.commit()
    db.refresh(consultation)
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

    consultation.updated_at = utcnow()
    db.commit()
    db.refresh(consultation)
    return consultation.to_dict(include_body=False)


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
    # dictés, et on recompose le texte dans le même ordre.
    morceaux: List[str] = []
    secondes = 0.0
    moteur = ("", "")
    dernier_refus = ""
    for piste in pistes:
        chemin = recordings.absolute_path(piste)
        if not os.path.exists(chemin):
            logger.warning(
                "Retranscription %s : fichier absent pour l'enregistrement %s",
                consultation_id, piste.id,
            )
            continue
        with open(chemin, "rb") as handle:
            brut = handle.read()
        try:
            resultat = await run_in_threadpool(transcribe, brut, piste.mime_type, hints)
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
            morceaux.append(texte)
        secondes += float(resultat.get("duration_seconds") or 0)
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
    consultation.audio_seconds = int(round(secondes))
    consultation.status = "transcrit"
    if moteur[0]:
        consultation.stt_provider, consultation.stt_model = moteur
    consultation.stt_language = preferences.document_language()
    consultation.updated_at = utcnow()
    db.commit()

    logger.info(
        "Retranscription de la consultation %s pour %s : %d enregistrement(s), "
        "langue %s, %d caractères",
        consultation_id, user.username, len(morceaux),
        consultation.stt_language, len(consultation.raw_transcript),
    )
    return {
        "transcript": consultation.raw_transcript,
        "stt_language": consultation.stt_language,
        "stt_used": " / ".join(p for p in moteur if p),
        "duration_seconds": consultation.audio_seconds,
        "recordings": len(morceaux),
        "recordings_total": len(pistes),
    }


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
    recordings.delete(db, recording)
    logger.info("Enregistrement %d supprimé par %s", recording_id, user.username)
    return None
