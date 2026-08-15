"""
runtime_config.py — Réglages modifiables depuis le panneau d'administration.
===========================================================================

POURQUOI UNE SECONDE SOURCE DE CONFIGURATION
--------------------------------------------
``app/config.py`` lit l'environnement une fois pour toutes au démarrage : c'est
ce qu'il faut pour ce qui touche à la sécurité (liste blanche d'usagers, plages
de proxy de confiance), qui ne doit pas pouvoir changer depuis le navigateur.

Mais changer de fournisseur de reconnaissance vocale, essayer un autre modèle
ou corriger une consigne n'a rien à voir avec la sécurité, et demander un
``docker compose up --build`` entre deux consultations n'est pas raisonnable.
Ces réglages-là vivent donc en base, où ils **surchargent** l'environnement :

    valeur effective  =  ligne dans app_settings  sinon  valeur du .env

Vider un champ dans le panneau supprime la ligne : le réglage revient à ce que
dit le ``.env``. C'est la façon la plus simple de revenir en arrière.

CE QUI N'EST PAS ICI
--------------------
Rien de ce qui gouverne l'accès. Un panneau d'administration accessible depuis
le navigateur ne doit jamais pouvoir élargir la liste des usagers autorisés, ni
les plages IP de confiance : ces réglages restent dans le fichier ``.env``, hors
d'atteinte de quiconque n'a pas accès au NAS.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import select

from app import default_prompts, i18n, preferences
from app.config import COHERE_DEFAULT_LLM_MODEL, MISTRAL_DEFAULT_LLM_MODEL, settings
from app.database import AppSetting, SessionLocal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Description des réglages
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Setting:
    """
    Description d'un réglage.

    Les textes ne sont pas ici : ``label`` et ``help`` sont cherchés dans le
    catalogue de traduction sous ``set.<clé>.label`` et ``set.<clé>.help``. Le
    panneau existe en deux langues, garder les libellés dans cette structure
    aurait obligé à en tenir deux copies.
    """

    key: str
    kind: str                     # text | secret | choice | textarea | number
    group: str                    # clé i18n : « group.stt »…
    default: Callable[[], str] = lambda: ""
    #: (valeur, libellé). Le libellé passe par ``i18n.t`` : une clé connue est
    #: traduite, un nom propre (« Deepgram ») traverse inchangé.
    choices: Tuple[Tuple[str, str], ...] = ()
    placeholder: str = ""

    def default_value(self) -> str:
        return str(self.default() or "")

    def label(self, language: str) -> str:
        return i18n.t(f"set.{self.key}.label", language)

    def help(self, language: str) -> str:
        return i18n.t(f"set.{self.key}.help", language)


STT_PROVIDERS = (
    ("google", "Google Speech-to-Text"),
    ("deepgram", "Deepgram"),
    ("assemblyai", "AssemblyAI"),
    ("soniox", "Soniox"),
    ("cohere", "Cohere Transcribe"),
    ("mistral", "Mistral Voxtral"),
    ("openai", "OpenAI Whisper"),
    ("custom", "provider.custom_endpoint"),
)

#: Un booléen présenté comme un choix : le panneau sait déjà afficher un
#: « choice », et cela évite un type de champ de plus pour deux réglages.
ON_OFF = (("true", "choice.on"), ("false", "choice.off"))

LLM_PROVIDERS = (
    ("gemini", "Google Gemini"),
    ("anthropic", "Anthropic Claude"),
    ("openai", "OpenAI"),
    # Cohere et Mistral ne reçoivent PAS de réglage de clé propre :
    # ``llm._api_key(...)`` lit « cohere_api_key » / « mistral_api_key », ceux
    # du service vocal. Une seule clé pour les deux usages chez chacun, et un
    # seul champ à remplir dans le panneau.
    ("cohere", "Cohere"),
    ("mistral", "Mistral AI"),
    ("qwen_omni", "Qwen Omni"),
    ("custom", "provider.custom_endpoint"),
)


# NOTE — la langue n'est PAS ici.
#
# Elle a d'abord été un réglage de ce panneau, ce qui était une erreur de
# portée : le panneau est réservé aux administrateurs, si bien qu'un usager
# ordinaire ne pouvait pas changer la langue de sa propre interface, et qu'un
# administrateur la changeait pour tout le monde à la fois. Elle vit maintenant
# dans le menu d'identité, par usager — voir ``app/preferences.py``.
#
# Le défaut de l'installation reste ``APP_LANGUAGE`` dans le ``.env``.
SETTINGS: Tuple[Setting, ...] = (
    # --- Système -------------------------------------------------------------
    Setting(
        "allow_signup", "choice", "group.system",
        default=lambda: "true" if settings.allow_signup else "false", choices=ON_OFF,
    ),
    Setting(
        "consultation_retention_hours", "number", "group.system",
        default=lambda: "12",
    ),

    # --- Reconnaissance vocale ---------------------------------------------
    Setting(
        "stt_provider", "choice", "group.stt",
        default=lambda: "google", choices=STT_PROVIDERS,
    ),
    Setting(
        "deepgram_api_key", "secret", "group.stt",
        default=lambda: settings.deepgram_api_key,
    ),
    Setting(
        "deepgram_model", "text", "group.stt",
        default=lambda: settings.deepgram_model, placeholder="nova-2",
    ),
    # Les trois réglages de langue ci-dessous sont vides par défaut : ils
    # suivent alors la langue de l'application. Y inscrire une valeur est un
    # forçage, utile pour un dialecte précis, mais qui survit au changement de
    # langue — c'est pourquoi ce n'est pas le défaut.
    Setting(
        "deepgram_language", "text", "group.stt",
        default=lambda: settings.stt_language_code, placeholder="fr-CA / en-CA",
    ),
    Setting(
        "assemblyai_api_key", "secret", "group.stt",
        default=lambda: settings.assemblyai_api_key,
    ),
    Setting(
        "assemblyai_model", "text", "group.stt",
        default=lambda: settings.assemblyai_model, placeholder="universal-3-5-pro",
    ),
    Setting(
        "assemblyai_language", "text", "group.stt",
        default=lambda: "", placeholder="fr / en / auto",
    ),
    Setting(
        "assemblyai_medical", "choice", "group.stt",
        default=lambda: "true", choices=ON_OFF,
    ),

    Setting(
        "stt_trim_silence", "choice", "group.stt",
        default=lambda: "true" if settings.stt_trim_silence else "false", choices=ON_OFF,
    ),
    Setting(
        "stt_silence_keep_seconds", "number", "group.stt",
        default=lambda: str(settings.stt_silence_keep_seconds),
    ),

    Setting(
        "soniox_api_key", "secret", "group.stt",
        default=lambda: settings.soniox_api_key,
    ),
    Setting(
        "soniox_model", "text", "group.stt",
        default=lambda: settings.soniox_model, placeholder="stt-async-v5",
    ),
    Setting(
        "soniox_language", "text", "group.stt",
        default=lambda: "", placeholder="fr / en / auto",
    ),
    Setting(
        "soniox_send_context", "choice", "group.stt",
        default=lambda: "true" if settings.soniox_send_context else "false",
        choices=ON_OFF,
    ),

    Setting(
        "cohere_api_key", "secret", "group.stt",
        default=lambda: settings.cohere_api_key,
    ),
    Setting(
        "cohere_model", "text", "group.stt",
        default=lambda: settings.cohere_model,
        placeholder="cohere-transcribe-03-2026",
    ),
    Setting(
        "cohere_language", "text", "group.stt",
        default=lambda: "", placeholder="fr / en",
    ),

    Setting(
        "mistral_api_key", "secret", "group.stt",
        default=lambda: settings.mistral_api_key,
    ),
    Setting(
        "mistral_model", "text", "group.stt",
        default=lambda: settings.mistral_model,
        placeholder="voxtral-mini-latest",
    ),
    Setting(
        "mistral_language", "text", "group.stt",
        default=lambda: "", placeholder="fr / en",
    ),

    # Pas de clé propre : « openai_api_key » (sous Modèle de langage) sert aux
    # deux usages, même compte — comme Cohere et Mistral, mais dans l'autre
    # sens puisque la clé LLM existait déjà.
    Setting(
        "openai_stt_model", "text", "group.stt",
        default=lambda: "", placeholder="whisper-1",
    ),
    Setting(
        "openai_stt_language", "text", "group.stt",
        default=lambda: "", placeholder="fr / en",
    ),

    Setting(
        "custom_stt_api_key", "secret", "group.stt",
        default=lambda: "",
    ),
    Setting(
        "custom_stt_base_url", "text", "group.stt",
        default=lambda: "", placeholder="https://exemple.tld/v1",
    ),
    Setting(
        "custom_stt_model", "text", "group.stt",
        default=lambda: "", placeholder="whisper-1",
    ),
    Setting(
        "custom_stt_language", "text", "group.stt",
        default=lambda: "", placeholder="fr / en / auto",
    ),
    Setting(
        "custom_stt_fallback_model", "text", "group.stt",
        default=lambda: "", placeholder="Systran/faster-whisper-small",
    ),
    Setting(
        "custom_stt_fallback_base_url", "text", "group.stt",
        default=lambda: "", placeholder="(vide = même adresse que le principal)",
    ),
    Setting(
        "custom_stt_max_seconds", "text", "group.stt",
        default=lambda: "", placeholder="ex. 380 (vide = pas de routage par durée)",
    ),

    # --- Modèle de langage ---------------------------------------------------
    # AUCUN réglage commun ici, volontairement : un champ « Modèle » unique
    # partagé par les cinq fournisseurs affichait le nom d'un modèle Gemini
    # même quand Anthropic ou OpenAI était actif, et changer de fournisseur
    # écrasait silencieusement ce que l'autre avait de configuré. Chaque
    # fournisseur a donc désormais SES PROPRES réglages modèle/rapide/
    # température ; ils s'enregistrent tous ensemble au clic sur
    # « Enregistrer », comme n'importe quel autre réglage.
    Setting(
        "llm_provider", "choice", "group.llm",
        default=lambda: "gemini", choices=LLM_PROVIDERS,
    ),

    Setting(
        "gemini_api_key", "secret", "group.llm",
        default=lambda: settings.gemini_api_key,
    ),
    Setting(
        "gemini_model", "text", "group.llm",
        default=lambda: settings.active_gemini_model,
        placeholder="gemini-2.5-flash",
    ),
    Setting(
        "gemini_model_fast", "text", "group.llm",
        default=lambda: settings.gemini_model,
        placeholder="gemini-2.5-flash",
    ),
    Setting(
        "gemini_temperature", "number", "group.llm",
        default=lambda: str(settings.gemini_temperature),
    ),
    Setting(
        "gemini_thinking_budget", "number", "group.llm",
        default=lambda: str(settings.gemini_thinking_budget),
        placeholder="128",
    ),
    Setting(
        "gemini_send_audio", "choice", "group.llm",
        default=lambda: "false", choices=ON_OFF,
    ),
    Setting(
        "gemini_send_audio_max_minutes", "number", "group.llm",
        default=lambda: "20",
    ),
    Setting(
        "gemini_bypass_stt", "choice", "group.llm",
        default=lambda: "false", choices=ON_OFF,
    ),
    Setting(
        "gemini_bypass_stt_keep_transcript", "choice", "group.llm",
        default=lambda: "false", choices=ON_OFF,
    ),

    Setting(
        "anthropic_api_key", "secret", "group.llm",
        default=lambda: settings.anthropic_api_key,
    ),
    Setting("anthropic_model", "text", "group.llm", default=lambda: ""),
    Setting("anthropic_model_fast", "text", "group.llm", default=lambda: ""),
    Setting(
        "anthropic_temperature", "number", "group.llm",
        default=lambda: str(settings.gemini_temperature),
    ),

    Setting(
        "openai_api_key", "secret", "group.llm",
        default=lambda: settings.openai_api_key,
    ),
    Setting("openai_model", "text", "group.llm", default=lambda: ""),
    Setting("openai_model_fast", "text", "group.llm", default=lambda: ""),
    Setting(
        "openai_temperature", "number", "group.llm",
        default=lambda: str(settings.gemini_temperature),
    ),

    # Cohere et Mistral n'ont pas de clé propre ici : elle se règle sous
    # Reconnaissance vocale (cohere_api_key / mistral_api_key) et le champ y
    # est répété — voir app.js, partitionFields(). Suffixe « _llm » pour ne
    # pas entrer en collision avec cohere_model / mistral_model, qui désignent
    # le modèle de TRANSCRIPTION.
    Setting(
        "cohere_llm_model", "text", "group.llm",
        default=lambda: COHERE_DEFAULT_LLM_MODEL,
        placeholder=COHERE_DEFAULT_LLM_MODEL,
    ),
    Setting("cohere_llm_model_fast", "text", "group.llm", default=lambda: ""),
    Setting(
        "cohere_llm_temperature", "number", "group.llm",
        default=lambda: str(settings.gemini_temperature),
    ),

    Setting(
        "mistral_llm_model", "text", "group.llm",
        default=lambda: MISTRAL_DEFAULT_LLM_MODEL,
        placeholder=MISTRAL_DEFAULT_LLM_MODEL,
    ),
    Setting("mistral_llm_model_fast", "text", "group.llm", default=lambda: ""),
    Setting(
        "mistral_llm_temperature", "number", "group.llm",
        default=lambda: str(settings.gemini_temperature),
    ),

    # Qwen Omni (Alibaba Cloud DashScope, mode compatible OpenAI). Clé et
    # adresse propres, comme « custom » : la région (internationale ou Chine
    # continentale) détermine l'adresse exacte, laissée à saisir plutôt que
    # devinée.
    Setting(
        "qwen_omni_api_key", "secret", "group.llm",
        default=lambda: settings.qwen_omni_api_key,
    ),
    Setting(
        "qwen_omni_base_url", "text", "group.llm",
        default=lambda: settings.qwen_omni_base_url,
        placeholder="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    ),
    Setting("qwen_omni_model", "text", "group.llm", default=lambda: ""),
    Setting("qwen_omni_model_fast", "text", "group.llm", default=lambda: ""),
    Setting(
        "qwen_omni_temperature", "number", "group.llm",
        default=lambda: str(settings.gemini_temperature),
    ),
    Setting(
        "qwen_omni_send_audio", "choice", "group.llm",
        default=lambda: "false", choices=ON_OFF,
    ),
    Setting(
        "qwen_omni_send_audio_max_minutes", "number", "group.llm",
        default=lambda: "20",
    ),
    Setting(
        "qwen_omni_bypass_stt", "choice", "group.llm",
        default=lambda: "false", choices=ON_OFF,
    ),
    Setting(
        "qwen_omni_bypass_stt_keep_transcript", "choice", "group.llm",
        default=lambda: "false", choices=ON_OFF,
    ),

    # Point de terminaison personnalisé, compatible OpenAI (serveur
    # auto-hébergé ou service tiers exposant /v1/chat/completions). Clé et
    # adresse propres : rien ne garantit que ce soit le même compte qu'un
    # autre fournisseur.
    Setting(
        "custom_llm_api_key", "secret", "group.llm",
        default=lambda: "",
    ),
    Setting(
        "custom_llm_base_url", "text", "group.llm",
        default=lambda: "", placeholder="https://exemple.tld/v1",
    ),
    Setting("custom_llm_model", "text", "group.llm", default=lambda: ""),
    Setting("custom_llm_model_fast", "text", "group.llm", default=lambda: ""),
    Setting(
        "custom_llm_temperature", "number", "group.llm",
        default=lambda: str(settings.gemini_temperature),
    ),
    Setting(
        "custom_send_audio", "choice", "group.llm",
        default=lambda: "false", choices=ON_OFF,
    ),
    Setting(
        "custom_send_audio_max_minutes", "number", "group.llm",
        default=lambda: "20",
    ),
    Setting(
        "custom_bypass_stt", "choice", "group.llm",
        default=lambda: "false", choices=ON_OFF,
    ),
    Setting(
        "custom_bypass_stt_keep_transcript", "choice", "group.llm",
        default=lambda: "false", choices=ON_OFF,
    ),

    # --- Identité affichée ---------------------------------------------------
    # Quelle revendication du fournisseur porte le nom et l'avatar. Rangées
    # dans le groupe des comptes : c'est là qu'on regarde quand un nom
    # s'affiche mal.
    Setting(
        "oidc_name_claim", "text", "group.users",
        default=lambda: settings.oidc_name_claim,
        placeholder="name / preferred_username / nickname",
    ),
    Setting(
        "oidc_picture_claim", "text", "group.users",
        default=lambda: settings.oidc_picture_claim, placeholder="picture",
    ),

    # --- Consignes ----------------------------------------------------------
    # Une consigne par langue. C'est la LANGUE DU GABARIT qui décide laquelle
    # est employée — voir llm.build_system_prompt. Les valeurs livrées viennent
    # de app/default_prompts.py, sous contrôle de version.
    Setting(
        "general_prompt_fr", "textarea", "group.prompts",
        default=lambda: default_prompts.GENERAL_PROMPT_FR,
        placeholder="set.general_prompt.placeholder",
    ),
    Setting(
        "general_prompt_en", "textarea", "group.prompts",
        default=lambda: default_prompts.GENERAL_PROMPT_EN,
        placeholder="set.general_prompt.placeholder",
    ),

    # --- Sauvegarde -----------------------------------------------------------
    Setting(
        "backup_retention_count", "number", "group.backup",
        default=lambda: "7",
    ),
)

#: Ordre d'affichage des groupes dans le panneau.
GROUPS: Tuple[str, ...] = (
    "group.system", "group.stt", "group.llm", "group.prompts", "group.users",
    "group.backup", "group.stats",
)

BY_KEY: Dict[str, Setting] = {item.key: item for item in SETTINGS}


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------
# Ces valeurs sont lues à chaque transcription et à chaque génération : on les
# garde en mémoire plutôt que d'ouvrir une session SQLAlchemy à chaque fois.
# Le cache n'est valable que dans le processus courant, ce qui suffit —
# ConsultAI tourne en un seul worker uvicorn — et il est vidé à chaque
# écriture, donc jamais périmé.
_cache: Optional[Dict[str, str]] = None
_cache_lock = threading.Lock()


def _load() -> Dict[str, str]:
    global _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        overrides: Dict[str, str] = {}
        try:
            with SessionLocal() as db:
                for row in db.scalars(select(AppSetting)):
                    if row.key in BY_KEY:
                        overrides[row.key] = row.value
        except Exception as exc:  # base pas encore créée, disque plein…
            logger.warning("Réglages non lus, valeurs du .env utilisées : %s", exc)
        _cache = overrides
        return _cache


def invalidate() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def value(key: str) -> str:
    """Valeur effective : surcharge en base, sinon défaut du ``.env``."""
    setting = BY_KEY.get(key)
    if setting is None:
        raise KeyError(f"Réglage inconnu : {key}")
    override = _load().get(key)
    if override is not None and override != "":
        return override
    return setting.default_value()


def value_float(key: str, fallback: float) -> float:
    try:
        return float(str(value(key)).replace(",", "."))
    except (TypeError, ValueError):
        return fallback


def is_overridden(key: str) -> bool:
    """Le réglage vient-il du panneau plutôt que du ``.env`` ?"""
    return bool(_load().get(key))


# ---------------------------------------------------------------------------
# Langue
# ---------------------------------------------------------------------------
def language() -> str:
    """
    Langue effective : « fr » ou « en ».

    Point d'entrée unique de tout ce qui dépend de la langue — l'interface, le
    code envoyé au service vocal, la langue de rédaction de la note.

    Ce n'est **pas** un réglage d'instance : la langue appartient à l'usager qui
    lit l'écran, et elle se choisit depuis son menu d'identité. La valeur est
    donc portée par la requête en cours (voir ``preferences``), avec
    ``APP_LANGUAGE`` du ``.env`` comme défaut pour qui n'a jamais choisi.

    La fonction reste ici parce que c'est l'adresse que connaissent ``stt.py``
    et ``llm.py`` ; elle ne fait que déléguer.
    """
    return preferences.current_language()


def general_prompt(language: str) -> str:
    """
    Consigne générale pour la langue demandée.

    La langue vient du GABARIT et de nulle part ailleurs : c'est la seule source
    prévue, sans détection automatique depuis l'audio ni depuis le texte.
    """
    cle = "general_prompt_en" if i18n.normalize(language) == "en" else "general_prompt_fr"
    return value(cle)


def stt_language(provider: str) -> str:
    """
    Code de langue à envoyer à ``provider``.

    Trois cas, et il fallait les distinguer :

    * champ **vide** → la langue de l'application décide, traduite dans la
      convention du service. C'est le défaut, et ce qui fait qu'un passage à
      l'anglais emporte toute la chaîne ;
    * champ à ``auto`` → chaîne vide renvoyée, ce que Soniox et AssemblyAI
      interprètent comme « détecte la langue toi-même ». Utile pour une
      consultation qui alterne deux langues ;
    * toute autre valeur → forçage explicite, tel quel.

    Le sentinelle ``auto`` existe parce que « vide » était déjà pris : sans
    elle, demander la détection automatique et laisser l'application choisir
    s'écriraient de la même façon.
    """
    cle = {
        "deepgram": "deepgram_language",
        "assemblyai": "assemblyai_language",
        "soniox": "soniox_language",
        "cohere": "cohere_language",
        "mistral": "mistral_language",
        "openai": "openai_stt_language",
        "custom": "custom_stt_language",
    }.get(provider)

    if cle is not None:
        forcage = value(cle).strip()
    else:
        # Google n'a pas de champ dans le panneau : seul le .env peut forcer.
        forcage = settings.stt_language_code.strip()

    if forcage.lower() == "auto":
        return ""
    if forcage:
        return forcage

    # La langue du DOCUMENT — donc celle du gabarit — et non celle de l'écran :
    # c'est la dictée qu'on transcrit, pas l'interface qu'on lit.
    return i18n.stt_language_code(preferences.document_language(), provider)


def stt_model(provider: Optional[str] = None) -> str:
    """
    Modèle STT actuel pour le fournisseur donné (ou l'actif).
    """
    if provider is None:
        provider = value("stt_provider")
    cle = {
        "deepgram": "deepgram_model",
        "assemblyai": "assemblyai_model",
        "soniox": "soniox_model",
        "cohere": "cohere_model",
        "mistral": "mistral_model",
        "openai": "openai_stt_model",
        "custom": "custom_stt_model",
    }.get(provider)
    if cle is not None:
        return value(cle) or ""
    return ""


# ---------------------------------------------------------------------------
# Vue destinée au panneau d'administration
# ---------------------------------------------------------------------------
def _mask(secret: str) -> dict:
    """
    Une clé d'API ne ressort jamais du serveur.

    On renvoie de quoi la reconnaître — sa longueur et ses quatre derniers
    caractères — et rien de plus : c'est assez pour vérifier qu'on a collé la
    bonne clé, et inutile pour s'en servir.
    """
    if not secret:
        return {"configured": False, "hint": ""}
    return {"configured": True, "hint": f"…{secret[-4:]}" if len(secret) > 4 else "…"}


def describe(language_code: Optional[str] = None) -> List[dict]:
    """
    Schéma + valeurs courantes, prêt à être affiché par le panneau.

    Les libellés sont rendus dans ``language_code``, ou dans la langue
    effective de l'application si l'appelant ne précise rien.
    """
    langue = i18n.normalize(language_code or language())
    result = []
    for setting in SETTINGS:
        entry = {
            "key": setting.key,
            "label": setting.label(langue),
            "kind": setting.kind,
            # La clé ET le libellé : le client identifie un onglet par sa clé,
            # jamais par un texte traduit — voir l'onglet des comptes, qui a un
            # comportement propre.
            "group": setting.group,
            "group_label": i18n.t(setting.group, langue),
            "help": setting.help(langue),
            # Un nom propre ou une valeur technique traverse ``t`` inchangé :
            # seules les clés connues du catalogue sont remplacées.
            "placeholder": i18n.t(setting.placeholder, langue) if setting.placeholder else "",
            "choices": [
                {"value": v, "label": i18n.t(label, langue)} for v, label in setting.choices
            ],
            "overridden": is_overridden(setting.key),
        }
        if setting.kind == "secret":
            entry.update(_mask(value(setting.key)))
            # D'où vient la clé : utile quand rien ne marche et qu'on cherche
            # si c'est le panneau ou le .env qui fait foi.
            entry["from_env"] = bool(setting.default_value()) and not is_overridden(setting.key)
        else:
            entry["value"] = value(setting.key)
            entry["default"] = setting.default_value()
        result.append(entry)
    return result


def update(values: Dict[str, str], username: str) -> List[str]:
    """
    Applique les réglages reçus. Retourne la liste des clés modifiées.

    Toute clé présente dans ``values`` est appliquée telle quelle ; une chaîne
    vide **supprime** la surcharge, et le réglage revient à ce que dit le
    ``.env``. Les clés absentes ne sont pas touchées : le panneau peut donc
    n'envoyer que ce qui a bougé, ce qui évite de réécrire une clé d'API
    qu'il n'a de toute façon jamais reçue en clair.
    """
    # Langue lue avant d'appliquer : si c'est justement elle qui change, un
    # message d'erreur doit sortir dans la langue que le médecin lit en ce
    # moment, pas dans celle qu'il vient de demander.
    langue = language()
    changed: List[str] = []
    with SessionLocal() as db:
        for key, raw in values.items():
            setting = BY_KEY.get(key)
            if setting is None:
                continue
            new_value = "" if raw is None else str(raw).strip()

            if setting.kind == "choice" and new_value:
                allowed = {choice for choice, _ in setting.choices}
                if new_value not in allowed:
                    raise ValueError(
                        i18n.t(
                            "err.setting_rejected", langue,
                            label=setting.label(langue), value=new_value,
                        )
                    )
            if setting.kind == "number" and new_value:
                try:
                    float(new_value.replace(",", "."))
                except ValueError as exc:
                    raise ValueError(
                        i18n.t(
                            "err.setting_number", langue, label=setting.label(langue)
                        )
                    ) from exc

            row = db.get(AppSetting, key)
            if not new_value:
                if row is not None:
                    db.delete(row)
                    changed.append(key)
                continue
            if row is None:
                db.add(AppSetting(key=key, value=new_value, updated_by=username))
                changed.append(key)
            elif row.value != new_value:
                row.value = new_value
                row.updated_by = username
                changed.append(key)
        db.commit()

    invalidate()
    if changed:
        # Jamais les valeurs : une clé d'API n'a rien à faire dans un journal.
        logger.info("Réglages modifiés par %s : %s", username, ", ".join(changed))
    return changed
