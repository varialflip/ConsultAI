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
    # Le titre n'annonce aucune spécialité : ce qui est propre à une pratique
    # vit dans les gabarits et dans la consigne générale, pas ici.
    app_title: str = "ConsultAI"
    #: Langue de départ de l'interface ET de la chaîne de traitement (« fr » ou
    #: « en »). Le panneau d'administration la surcharge — c'est donc une
    #: valeur initiale, pas la valeur effective : voir runtime_config.language().
    app_language: str = "fr"
    log_level: str = "INFO"
    database_url: str = "sqlite:////data/consultai.db"

    # --- Authentification OIDC ---
    # L'application authentifie elle-même, par OpenID Connect. Le reverse proxy
    # n'est plus qu'un relais : il ne décide plus qui entre.
    oidc_provider_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    base_url: str = ""
    oidc_scopes: List[str] = field(default_factory=list)
    #: Nom de la revendication portant les groupes. Aucune norme ne l'impose :
    #: Pocket ID et Authentik disent « groups », Keycloak « roles » selon la
    #: configuration du client.
    oidc_groups_claim: str = "groups"
    #: Revendication portant le nom à afficher, et celle portant l'avatar.
    #: Configurables parce que les fournisseurs ne s'accordent pas : « name »,
    #: « preferred_username », « nickname », « given_name »… Surchargeables
    #: depuis le panneau d'administration.
    oidc_name_claim: str = "name"
    oidc_picture_claim: str = "picture"

    #: Nom du fournisseur d'identité tel qu'il apparaît à l'écran (pages
    #: d'erreur). Configurable plutôt que codé en dur : l'application doit
    #: pouvoir être redéployée derrière un autre fournisseur sans qu'un nom
    #: propre traîne dans son catalogue de traductions.
    sso_display_name: str = ""

    #: Clé de signature du témoin de session. **Doit être fixée** : sans elle,
    #: une clé aléatoire est tirée au démarrage et tout le monde est déconnecté
    #: à chaque redémarrage du conteneur.
    session_secret: str = ""
    session_max_age_seconds: int = 60 * 60 * 12
    #: Le témoin n'est émis qu'en HTTPS. À ne passer à false que pour un essai
    #: local en http://localhost, jamais en production.
    session_https_only: bool = True

    #: Tout usager authentifié par le fournisseur est-il accepté ?
    #: Surchargeable depuis le panneau d'administration.
    allow_signup: bool = False

    #: Reste utilisé au tout premier démarrage : ces comptes sont créés en base
    #: pour que l'installation ne se retrouve pas sans personne pouvant entrer.
    authorized_users: List[str] = field(default_factory=list)
    template_admins: List[str] = field(default_factory=list)

    auth_disabled: bool = False
    dev_user: str = "dev@local"

    # --- Anciens en-têtes de proxy (obsolètes) ---
    # Conservés pour la seule journalisation d'un avertissement au démarrage :
    # une installation qui les a encore renseignés croit être protégée par son
    # proxy alors que ce n'est plus l'application qui s'y fie.
    sso_header_key: str = ""
    trusted_proxies: List[ipaddress._BaseNetwork] = field(default_factory=list)

    # --- Speech-to-Text ---
    google_credentials: str = ""
    #: Forçage du code de langue envoyé au service vocal. **Vide = suit la
    #: langue de l'application**, ce qui est le comportement voulu : une valeur
    #: fixée ici l'emporterait sur le réglage de langue et une dictée en
    #: anglais partirait avec « fr-CA ».
    stt_language_code: str = ""
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

    # --- Cohere Transcribe (cinquième) ---
    # ⚠️ 5 requêtes/minute sur une clé d'essai : voir stt._transcribe_cohere.
    cohere_api_key: str = ""
    cohere_model: str = "cohere-transcribe-03-2026"

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
    def sso_label(self) -> str:
        """
        Nom du fournisseur à afficher.

        À défaut de ``SSO_DISPLAY_NAME``, on montre l'hôte du fournisseur OIDC :
        moins élégant, mais toujours exact et jamais vide, alors qu'un libellé
        générique n'aiderait personne à savoir où il vient de se faire refuser.
        """
        if self.sso_display_name:
            return self.sso_display_name
        if self.oidc_provider_url:
            from urllib.parse import urlparse

            return urlparse(self.oidc_provider_url).netloc or self.oidc_provider_url
        return "OIDC"

    @property
    def oidc_configured(self) -> bool:
        """Y a-t-il de quoi lancer un flux OIDC ?"""
        return bool(
            self.oidc_provider_url and self.oidc_client_id and self.oidc_client_secret
        )

    @property
    def oidc_scopes_effective(self) -> List[str]:
        """
        Portées demandées, « openid » garantie présente.

        « openid » n'est pas une portée comme les autres : sans elle le
        fournisseur exécute un flux OAuth2 ordinaire et ne renvoie aucun
        jeton d'identité. L'oublier dans OIDC_SCOPES donnerait une erreur
        obscure côté fournisseur ; on la remet donc en tête.
        """
        portees = [p for p in self.oidc_scopes if p]
        if "openid" not in portees:
            portees.insert(0, "openid")
        return portees

    @property
    def effective_redirect_uri(self) -> str:
        """
        Adresse de retour du fournisseur.

        Déduite de BASE_URL si OIDC_REDIRECT_URI n'est pas renseignée : les deux
        doivent concorder, et les laisser saisir séparément est la première
        source d'erreur « redirect_uri_mismatch ».
        """
        if self.oidc_redirect_uri:
            return self.oidc_redirect_uri
        if self.base_url:
            return f"{self.base_url.rstrip('/')}/auth/callback"
        return ""

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
            app_title=_env("APP_TITLE", "ConsultAI"),
            app_language=_env("APP_LANGUAGE", "fr"),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            database_url=_env("DATABASE_URL", "sqlite:////data/consultai.db"),

            oidc_provider_url=_env("OIDC_PROVIDER_URL").rstrip("/"),
            oidc_client_id=_env("OIDC_CLIENT_ID"),
            oidc_client_secret=_env("OIDC_CLIENT_SECRET"),
            oidc_redirect_uri=_env("OIDC_REDIRECT_URI"),
            base_url=_env("BASE_URL").rstrip("/"),
            oidc_scopes=_env_list("OIDC_SCOPES", "openid,profile,email,groups"),
            oidc_groups_claim=_env("OIDC_GROUPS_CLAIM", "groups"),
            oidc_name_claim=_env("OIDC_NAME_CLAIM", "name"),
            oidc_picture_claim=_env("OIDC_PICTURE_CLAIM", "picture"),
            sso_display_name=_env("SSO_DISPLAY_NAME"),

            session_secret=_env("SESSION_SECRET"),
            session_max_age_seconds=_env_int("SESSION_MAX_AGE_SECONDS", 60 * 60 * 12),
            session_https_only=_env_bool("SESSION_HTTPS_ONLY", True),

            allow_signup=_env_bool("ALLOW_SIGNUP", False),
            authorized_users=_env_list("AUTHORIZED_USERS"),
            template_admins=_env_list("TEMPLATE_ADMINS", "*"),

            auth_disabled=_env_bool("AUTH_DISABLED", False),
            dev_user=_env("DEV_USER", "dev@local"),

            # Obsolètes : lues uniquement pour pouvoir avertir qu'elles ne
            # servent plus (voir warnings()).
            sso_header_key=_env("SSO_HEADER_KEY"),
            trusted_proxies=_parse_networks(_env_list("TRUSTED_PROXIES")),

            google_credentials=_env("GOOGLE_APPLICATION_CREDENTIALS"),
            # Pas de défaut : vide signifie « suis la langue de l'application ».
            stt_language_code=_env("STT_LANGUAGE_CODE"),
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
            cohere_api_key=_env("COHERE_API_KEY"),
            cohere_model=_env("COHERE_MODEL", "cohere-transcribe-03-2026"),

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
        else:
            if not self.oidc_provider_url:
                problems.append(
                    "OIDC_PROVIDER_URL est vide — aucune connexion n'est possible."
                )
            if not self.oidc_client_id or not self.oidc_client_secret:
                problems.append(
                    "OIDC_CLIENT_ID ou OIDC_CLIENT_SECRET est vide — la connexion "
                    "échouera à l'échange du code."
                )
            if not self.effective_redirect_uri:
                problems.append(
                    "Ni OIDC_REDIRECT_URI ni BASE_URL n'est renseignée — impossible "
                    "de construire l'adresse de retour du fournisseur."
                )
            elif not self.effective_redirect_uri.startswith("https://") \
                    and "localhost" not in self.effective_redirect_uri \
                    and "127.0.0.1" not in self.effective_redirect_uri:
                problems.append(
                    "L'adresse de retour OIDC n'est pas en HTTPS "
                    f"({self.effective_redirect_uri}) — le fournisseur la refusera "
                    "et le témoin de session ne serait pas protégé."
                )
            if not self.session_secret:
                problems.append(
                    "SESSION_SECRET est vide — une clé aléatoire est tirée au "
                    "démarrage, donc TOUT LE MONDE EST DÉCONNECTÉ à chaque "
                    "redémarrage du conteneur. Fixez-la."
                )
            elif len(self.session_secret) < 32:
                problems.append(
                    "SESSION_SECRET fait moins de 32 caractères — trop court pour "
                    "signer un témoin de session."
                )
            if not self.session_https_only:
                problems.append(
                    "SESSION_HTTPS_ONLY=false — le témoin de session circulerait "
                    "en clair. À ne faire qu'en essai local."
                )
            if self.allow_signup:
                problems.append(
                    "ALLOW_SIGNUP=true — tout compte du fournisseur d'identité est "
                    "créé et autorisé automatiquement. À ne laisser ainsi que si "
                    "l'inscription chez le fournisseur est fermée."
                )

        # L'application n'authentifie plus par en-têtes : le dire, sinon une
        # installation migrée croit encore être protégée par son proxy.
        if self.sso_header_key or self.trusted_proxies:
            problems.append(
                "SSO_HEADER_KEY / TRUSTED_PROXIES sont encore renseignées mais ne "
                "servent plus : l'authentification se fait par OIDC. Le proxy doit "
                "désormais RELAYER sans authentifier, et ces variables peuvent être "
                "retirées du .env."
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
