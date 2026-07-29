"""
config.py — Lecture et validation de la configuration.
=======================================================

Toute la configuration passe par des variables d'environnement (12-factor),
ce qui permet de tout piloter depuis docker-compose.yml / .env sans jamais
reconstruire l'image.

L'objet ``settings`` est un singleton créé au chargement du module.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Petits utilitaires de parsing
# ---------------------------------------------------------------------------
def _env(key: str, default: str = "") -> str:
    """Retourne la variable d'environnement, nettoyée des espaces parasites."""
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    """Interprète 1/true/yes/on (insensible à la casse) comme True."""
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        logger.warning("Variable %s non numérique — valeur par défaut %s utilisée", key, default)
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        logger.warning("Variable %s non numérique — valeur par défaut %s utilisée", key, default)
        return default


def _env_list(key: str, default: str = "") -> List[str]:
    """Découpe une valeur « a, b , c » en ['a', 'b', 'c'] (vides ignorés)."""
    raw = _env(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_networks(entries: List[str]) -> List[ipaddress._BaseNetwork]:
    """
    Convertit une liste de chaînes en réseaux IP.

    Accepte aussi bien « 10.0.0.0/8 » que « 192.168.1.50 » (converti en /32).
    Les entrées invalides sont ignorées avec un avertissement plutôt que de
    faire planter le démarrage du conteneur.
    """
    networks: List[ipaddress._BaseNetwork] = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("TRUSTED_PROXIES : entrée ignorée car invalide « %s »", entry)
    return networks


# ---------------------------------------------------------------------------
# Objet de configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    # --- Général ---
    app_title: str = "ConsultAI — Gériatrie"
    log_level: str = "INFO"
    database_url: str = "sqlite:////data/consultai.db"

    # --- Authentification / SSO ---
    sso_header_key: str = "Remote-User"
    sso_header_fallbacks: List[str] = field(default_factory=list)
    sso_email_header: str = "Remote-Email"
    sso_name_header: str = "Remote-Name"
    authorized_users: List[str] = field(default_factory=list)
    template_admins: List[str] = field(default_factory=list)
    trusted_proxies: List[ipaddress._BaseNetwork] = field(default_factory=list)
    auth_disabled: bool = False
    dev_user: str = "dev@local"

    # --- Speech-to-Text ---
    google_credentials: str = ""
    stt_language_code: str = "fr-CA"
    stt_model: str = "latest_long"
    stt_use_enhanced: bool = True
    stt_api_endpoint: str = ""
    stt_gcs_bucket: str = ""
    max_audio_mb: int = 120

    # --- Retrait des silences avant envoi ---
    # Les trois services facturent à la durée d'audio : les longues pauses
    # d'une consultation sont payées plein tarif pour rien. Voir
    # stt.compress_silence — seule la copie envoyée est raccourcie.
    stt_trim_silence: bool = True
    stt_silence_keep_seconds: float = 0.5
    stt_silence_threshold_db: int = -40

    # --- Deepgram (service de reconnaissance vocale alternatif) ---
    # Ces valeurs ne sont que des défauts : le panneau d'administration les
    # surcharge en base (voir app/runtime_config.py).
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-2"

    # --- AssemblyAI (troisième service de reconnaissance vocale) ---
    assemblyai_api_key: str = ""
    assemblyai_model: str = "universal-3-5-pro"

    # --- Soniox (quatrième service de reconnaissance vocale) ---
    soniox_api_key: str = ""
    soniox_model: str = "stt-async-v5"

    # --- Dictée par segments ---
    # La dictée n'est plus envoyée en un seul bloc à la fin : le navigateur
    # téléverse l'audio au fil de l'eau et le serveur le transcrit par
    # tranches. Une coupure réseau ne coûte donc plus la consultation entière.
    dictation_dir: str = "/data/dictations"
    dictation_chunk_seconds: int = 5      # cadence de téléversement (navigateur)
    dictation_segment_seconds: int = 30   # durée visée d'une tranche transcrite
    dictation_retention_hours: int = 72   # purge des dictées abandonnées

    # --- Gemini ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_model_pro: str = "gemini-2.5-pro"
    gemini_use_pro: bool = False
    gemini_temperature: float = 0.15
    gemini_max_output_tokens: int = 8192
    google_cloud_project: str = ""
    google_cloud_location: str = "northamerica-northeast1"

    # --- Autres fournisseurs de modèle de langage ---
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # --- Enregistrements conservés avec les brouillons ---
    audio_dir: str = "/data/audio"

    # --- Déconnexion ---
    # Deux sessions à clore, et Pangolin ne propage pas la déconnexion OIDC
    # quand il ferme la sienne. Il faut donc s'adresser aux deux.
    # Interface de Pangolin, et non son API : sa déconnexion exige un jeton
    # CSRF qu'aucune autre origine ne peut obtenir (vérifié : 403 « CSRF token
    # missing or invalid »). Elle ne peut donc se faire que depuis chez lui.
    logout_pangolin_ui_url: str = ""
    logout_oidc_url: str = ""
    # Où revenir après la déconnexion. Le nom du paramètre varie d'un
    # fournisseur à l'autre : Pocket ID emploie « r », la spécification OIDC
    # « post_logout_redirect_uri ».
    logout_redirect_url: str = ""
    logout_oidc_redirect_param: str = "r"

    # -- Propriétés dérivées -------------------------------------------------
    @property
    def allow_all_users(self) -> bool:
        """True si AUTHORIZED_USERS contient « * » (tout utilisateur SSO valide)."""
        return "*" in self.authorized_users

    @property
    def authorized_users_normalized(self) -> set:
        """Liste blanche en minuscules pour une comparaison insensible à la casse."""
        return {u.lower() for u in self.authorized_users}

    @property
    def template_admins_normalized(self) -> set:
        """
        Comptes autorisés à créer/modifier/supprimer des gabarits.
        Vide ou « * » = tous les utilisateurs autorisés (défaut).
        """
        return {u.lower() for u in self.template_admins}

    @property
    def sso_headers_in_order(self) -> List[str]:
        """En-tête principal suivi des replis, sans doublon."""
        ordered = [self.sso_header_key, *self.sso_header_fallbacks]
        seen, result = set(), []
        for header in ordered:
            key = header.lower()
            if header and key not in seen:
                seen.add(key)
                result.append(header)
        return result

    @property
    def gemini_use_vertex(self) -> bool:
        """
        Mode Vertex AI si aucune clé API n'est fournie mais qu'un projet GCP
        l'est. Vertex authentifie via le compte de service (ADC).
        """
        return not self.gemini_api_key and bool(self.google_cloud_project)

    @property
    def active_gemini_model(self) -> str:
        """Modèle utilisé par défaut, selon la bascule GEMINI_USE_PRO."""
        return self.gemini_model_pro if self.gemini_use_pro else self.gemini_model

    @property
    def max_audio_bytes(self) -> int:
        return self.max_audio_mb * 1024 * 1024

    # -- Construction --------------------------------------------------------
    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            app_title=_env("APP_TITLE", "ConsultAI — Gériatrie"),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            database_url=_env("DATABASE_URL", "sqlite:////data/consultai.db"),

            sso_header_key=_env("SSO_HEADER_KEY", "Remote-User"),
            sso_header_fallbacks=_env_list(
                "SSO_HEADER_FALLBACKS",
                "X-Forwarded-User,X-Remote-User,X-Authentik-Username",
            ),
            sso_email_header=_env("SSO_EMAIL_HEADER", "Remote-Email"),
            sso_name_header=_env("SSO_NAME_HEADER", "Remote-Name"),
            authorized_users=_env_list("AUTHORIZED_USERS"),
            template_admins=_env_list("TEMPLATE_ADMINS", "*"),
            trusted_proxies=_parse_networks(
                _env_list(
                    "TRUSTED_PROXIES",
                    "127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
                )
            ),
            auth_disabled=_env_bool("AUTH_DISABLED", False),
            dev_user=_env("DEV_USER", "dev@local"),

            google_credentials=_env("GOOGLE_APPLICATION_CREDENTIALS"),
            stt_language_code=_env("STT_LANGUAGE_CODE", "fr-CA"),
            stt_model=_env("STT_MODEL", "latest_long"),
            stt_use_enhanced=_env_bool("STT_USE_ENHANCED", True),
            stt_api_endpoint=_env("STT_API_ENDPOINT"),
            stt_gcs_bucket=_env("STT_GCS_BUCKET"),
            max_audio_mb=_env_int("MAX_AUDIO_MB", 120),
            stt_trim_silence=_env_bool("STT_TRIM_SILENCE", True),
            stt_silence_keep_seconds=_env_float("STT_SILENCE_KEEP_SECONDS", 0.5),
            stt_silence_threshold_db=_env_int("STT_SILENCE_THRESHOLD_DB", -40),

            deepgram_api_key=_env("DEEPGRAM_API_KEY"),
            deepgram_model=_env("DEEPGRAM_MODEL", "nova-2"),
            assemblyai_api_key=_env("ASSEMBLYAI_API_KEY"),
            assemblyai_model=_env("ASSEMBLYAI_MODEL", "universal-3-5-pro"),
            soniox_api_key=_env("SONIOX_API_KEY"),
            soniox_model=_env("SONIOX_MODEL", "stt-async-v5"),

            audio_dir=_env("AUDIO_DIR", "/data/audio"),
            logout_pangolin_ui_url=_env("LOGOUT_PANGOLIN_UI_URL"),
            logout_oidc_url=_env("LOGOUT_OIDC_URL"),
            logout_redirect_url=_env("LOGOUT_REDIRECT_URL"),
            logout_oidc_redirect_param=_env("LOGOUT_OIDC_REDIRECT_PARAM", "r"),
            dictation_dir=_env("DICTATION_DIR", "/data/dictations"),
            dictation_chunk_seconds=_env_int("DICTATION_CHUNK_SECONDS", 5),
            dictation_segment_seconds=_env_int("DICTATION_SEGMENT_SECONDS", 30),
            dictation_retention_hours=_env_int("DICTATION_RETENTION_HOURS", 72),

            gemini_api_key=_env("GEMINI_API_KEY"),
            gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
            gemini_model_pro=_env("GEMINI_MODEL_PRO", "gemini-2.5-pro"),
            gemini_use_pro=_env_bool("GEMINI_USE_PRO", False),
            gemini_temperature=_env_float("GEMINI_TEMPERATURE", 0.15),
            gemini_max_output_tokens=_env_int("GEMINI_MAX_OUTPUT_TOKENS", 8192),
            google_cloud_project=_env("GOOGLE_CLOUD_PROJECT"),
            google_cloud_location=_env("GOOGLE_CLOUD_LOCATION", "northamerica-northeast1"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            openai_api_key=_env("OPENAI_API_KEY"),
        )

    # -- Diagnostic de démarrage --------------------------------------------
    def warnings(self) -> List[str]:
        """
        Liste des problèmes de configuration détectables sans appel réseau.
        Journalisés au démarrage et exposés dans /healthz pour le débogage.
        """
        problems: List[str] = []

        if self.auth_disabled:
            problems.append(
                "AUTH_DISABLED=true — l'authentification est DÉSACTIVÉE. "
                "À n'utiliser qu'en développement local."
            )
        elif not self.authorized_users:
            problems.append(
                "AUTHORIZED_USERS est vide — personne ne pourra accéder à l'application."
            )
        elif self.allow_all_users:
            problems.append(
                "AUTHORIZED_USERS contient « * » — tout utilisateur authentifié "
                "par Pangolin aura accès aux consultations."
            )

        if not self.trusted_proxies and not self.auth_disabled:
            problems.append(
                "TRUSTED_PROXIES est vide — toutes les requêtes seront refusées."
            )

        if self.google_credentials and not os.path.exists(self.google_credentials):
            problems.append(
                f"GOOGLE_APPLICATION_CREDENTIALS pointe vers un fichier introuvable "
                f"({self.google_credentials}) — la transcription échouera."
            )

        if not (self.gemini_api_key or self.google_cloud_project
                or self.anthropic_api_key or self.openai_api_key):
            problems.append(
                "Aucune clé de modèle de langage dans l'environnement "
                "(GEMINI_API_KEY, GOOGLE_CLOUD_PROJECT, ANTHROPIC_API_KEY, "
                "OPENAI_API_KEY) — renseignez-en une ici ou dans le panneau "
                "d'administration, sinon la mise en forme échouera."
            )

        return problems


# Singleton importé partout ailleurs : `from app.config import settings`
settings = Settings.from_env()


def configure_logging() -> None:
    """Configuration du journal, appelée une fois au démarrage de FastAPI."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Les bibliothèques Google sont très bavardes en DEBUG.
    logging.getLogger("google").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Les SDK Anthropic et OpenAI journalisent chaque requête HTTP via httpx,
    # code de retour compris. Un « 400 Bad Request » y apparaît donc même
    # quand l'application le rattrape et produit la note normalement (voir
    # llm._call_tolerant) : une ligne d'erreur sans erreur, qui donne à croire
    # que quelque chose a échoué. Les échecs réels sont journalisés par
    # l'application elle-même, avec leur contexte.
    for noisy in ("httpx", "httpcore", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
