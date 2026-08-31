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

COMMENT LE PANNEAU EST DÉCRIT
-----------------------------
Chaque ``Setting`` porte, en plus de sa valeur par défaut, les métadonnées de
présentation que le navigateur lit telles quelles dans ``describe()`` — le
client ne redéclare PLUS RIEN :

* ``only_for``     — le réglage n'appartient qu'au service choisi d'un
  sélecteur (ex. ``("llm_provider", "gemini")``) ; il devient « propre » à ce
  service dans la carte du fournisseur ;
* ``also_in``      — répétition du MÊME réglage sous un autre onglet, pour une
  clé partagée entre deux services (Cohere, Mistral, OpenAI) : une seule valeur
  en base, deux champs à l'écran synchronisés par leur ``data-key`` ;
* ``visible_if``   — conditions (et) sur d'autres réglages : le champ est
  masqué tant qu'elles ne sont pas réunies (le VAD ne se règle qu'en mode
  « énoncé », la transcription conservée seulement quand on ignore le STT…) ;
  masquer n'est pas effacer ;
* ``section``      — sous-titre à l'intérieur d'un onglet (« Retrait des
  pauses », « Temps réel »…), pour regrouper ce qui se règle ensemble ;
* ``advanced``     — rangé sous le bloc repliable « Avancé » ;
* ``label_key`` / ``help_key`` — textes partagés entre fournisseurs : les
  capacités identiques (modèle rapide, température, audio joint…) sont
  déclarées UNE fois et instanciées par fournisseur via les fabriques
  ``_cap_*`` ci-dessous. Les valeurs restent propres à chaque fournisseur en
  base — seul le texte est mutualisé.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import select

from app import default_prompts, i18n, preferences
from app.config import (
    COHERE_DEFAULT_LLM_MODEL,
    MISTRAL_DEFAULT_LLM_MODEL,
    OPENROUTER_DEFAULT_LLM_MODEL,
    OPENROUTER_DEFAULT_STT_MODEL,
    settings,
)
from app.database import AppSetting, SessionLocal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Description des réglages
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Setting:
    """
    Description d'un réglage, textes compris.

    Par défaut, ``label`` et ``help`` sont cherchés dans le catalogue de
    traduction sous ``set.<clé>.label`` et ``set.<clé>.help`` ; ``label_key``
    et ``help_key`` permettent de pointer vers des textes PARTAGÉS entre
    fournisseurs (voir les fabriques ``_cap_*``). Le panneau existe en deux
    langues, garder les libellés dans cette structure aurait obligé à en tenir
    deux copies.
    """

    key: str
    kind: str                     # text | secret | choice | textarea | number
    group: str                    # clé i18n : « group.dictation »…
    default: Callable[[], str] = lambda: ""
    #: (valeur, libellé). Le libellé passe par ``i18n.t`` : une clé connue est
    #: traduite, un nom propre (« Deepgram ») traverse inchangé.
    choices: Tuple[Tuple[str, str], ...] = ()
    placeholder: str = ""
    #: Textes partagés : None = ``set.<clé>.label`` / ``set.<clé>.help``.
    label_key: Optional[str] = None
    help_key: Optional[str] = None
    #: (sélecteur, valeur) : le réglage ne concerne que ce service.
    only_for: Optional[Tuple[str, str]] = None
    #: Onglets où le champ est RÉPÉTÉ (clé partagée entre deux services) :
    #: ``(onglet, valeur_du_sélecteur_de_cet_onglet)``.
    also_in: Tuple[Tuple[str, str], ...] = ()
    #: Conditions (et) de visibilité : ``(autre_réglage, valeur_attendue)``.
    visible_if: Tuple[Tuple[str, str], ...] = ()
    #: Sous-titre interne à l'onglet, clé i18n « sect.… ».
    section: str = ""
    advanced: bool = False
    #: Proposer la liste « Modèles disponibles » (datalist partagé).
    datalist: bool = False

    def default_value(self) -> str:
        return str(self.default() or "")

    def label(self, language: str) -> str:
        return i18n.t(self.label_key or f"set.{self.key}.label", language)

    def help(self, language: str) -> str:
        return i18n.t(self.help_key or f"set.{self.key}.help", language)


STT_PROVIDERS = (
    ("google", "Google Speech-to-Text"),
    ("deepgram", "Deepgram"),
    ("assemblyai", "AssemblyAI"),
    ("soniox", "Soniox"),
    ("cohere", "Cohere Transcribe"),
    ("mistral", "Mistral Voxtral"),
    ("openai", "OpenAI Whisper"),
    ("modulate", "Modulate"),
    ("custom", "provider.custom_endpoint"),
    ("openrouter", "OpenRouter"),
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
    ("openrouter", "OpenRouter"),
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


# ---------------------------------------------------------------------------
# Fabriques « capacités » du modèle de langage
# ---------------------------------------------------------------------------
# Modèle, modèle rapide, température, audio joint, contournement du STT : ces
# capacités se comportent pareil chez tous les fournisseurs. Les VALEURS
# restent propres à chacun en base (changer de fournisseur n'écrase rien —
# voir l'historique plus bas), mais le TEXTE est déclaré une seule fois ici,
# au lieu d'être recopié sept fois dans le catalogue de traduction.

def _cap_model(fournisseur: str, prefixe: str, *,
               default: Callable[[], str] = lambda: "",
               help_key: Optional[str] = "set.cap.model.help") -> Setting:
    """Modèle principal d'un fournisseur LLM."""
    return Setting(
        f"{prefixe}_model", "text", "group.note",
        default=default, label_key="set.cap.model.label", help_key=help_key,
        datalist=True, only_for=("llm_provider", fournisseur),
    )


def _cap_fast(fournisseur: str, prefixe: str) -> Setting:
    """Modèle rapide (relecture des métadonnées)."""
    return Setting(
        f"{prefixe}_model_fast", "text", "group.note",
        default=lambda: "", label_key="set.cap.model_fast.label",
        help_key="set.cap.model_fast.help",
        datalist=True, only_for=("llm_provider", fournisseur),
    )


def _cap_verify(fournisseur: str, prefixe: str) -> Setting:
    """Modèle de la DEUXIÈME passe de validation (« Validation »).

    Vide par défaut : la passe emploie alors le même modèle que la mise en
    forme. La valeur est résolue par ``llm.verify_model()`` — pas ici, pour
    ne pas créer de dépendance circulaire entre ``runtime_config`` et
    ``llm``.
    """
    return Setting(
        f"{prefixe}_verify_model", "text", "group.note",
        default=lambda: "", label_key="set.cap.verify_model.label",
        help_key="set.cap.verify_model.help",
        datalist=True, only_for=("llm_provider", fournisseur),
    )


def _cap_temperature(fournisseur: str, prefixe: str,
                     default: Callable[[], str]) -> Setting:
    """Température de mise en forme. Le suffixe ``_temperature`` est requis :
    le client y branche le pas 0,05 borné [0 ; 2]."""
    return Setting(
        f"{prefixe}_temperature", "number", "group.note",
        default=default, label_key="set.cap.temperature.label",
        help_key="set.cap.temperature.help",
        only_for=("llm_provider", fournisseur),
    )


def _cap_audio(fournisseur: str, prefixe: str, *,
               help_key: Optional[str] = "set.cap.send_audio.help") -> Tuple[Setting, Setting]:
    """Joindre aussi l'audio à la note, avec sa durée maximale — le champ de
    durée n'a de sens que si l'envoi est activé."""
    cle = f"{prefixe}_send_audio"
    return (
        Setting(
            cle, "choice", "group.note",
            default=lambda: "false", choices=ON_OFF,
            label_key="set.cap.send_audio.label", help_key=help_key,
            only_for=("llm_provider", fournisseur),
        ),
        Setting(
            f"{cle}_max_minutes", "number", "group.note",
            default=lambda: "20",
            label_key="set.cap.send_audio_max_minutes.label",
            help_key="set.cap.send_audio_max_minutes.help",
            only_for=("llm_provider", fournisseur),
            visible_if=((cle, "true"),),
        ),
    )


def _cap_bypass(fournisseur: str, prefixe: str, *,
                help_key: Optional[str] = "set.cap.bypass_stt.help") -> Tuple[Setting, Setting]:
    """Ignorer la reconnaissance vocale (audio direct au modèle), avec
    l'option de garder une transcription pendant l'enregistrement — sans objet
    si le contournement est désactivé."""
    cle = f"{prefixe}_bypass_stt"
    return (
        Setting(
            cle, "choice", "group.note",
            default=lambda: "false", choices=ON_OFF,
            label_key="set.cap.bypass_stt.label", help_key=help_key,
            only_for=("llm_provider", fournisseur),
        ),
        Setting(
            f"{cle}_keep_transcript", "choice", "group.note",
            default=lambda: "false", choices=ON_OFF,
            label_key="set.cap.keep_transcript.label",
            help_key="set.cap.keep_transcript.help",
            only_for=("llm_provider", fournisseur),
            visible_if=((cle, "true"),),
        ),
    )


SETTINGS: Tuple[Setting, ...] = (
    # =========================================================================
    # --- Dictée --------------------------------------------------------------
    # =========================================================================
    # AUCUN réglage commun de modèle ici : chaque service a SES réglages
    # clé/modèle/langue, présentés sous son propre sous-onglet. Masquer
    # n'est pas effacer : une clé collée sous un service non actif reste en
    # base et reparaît à son retour.
    Setting(
        "stt_provider", "choice", "group.dictation",
        default=lambda: "google", choices=STT_PROVIDERS,
    ),

    # Google n'a pas de champ dans le panneau : seul le .env peut forcer sa
    # langue (voir stt_language()).

    Setting(
        "deepgram_api_key", "secret", "group.dictation",
        default=lambda: settings.deepgram_api_key,
        only_for=("stt_provider", "deepgram"),
    ),
    Setting(
        "deepgram_model", "text", "group.dictation",
        default=lambda: settings.deepgram_model, placeholder="nova-2",
        only_for=("stt_provider", "deepgram"),
        datalist=True,
    ),
    # Les trois réglages de langue ci-dessous sont vides par défaut : ils
    # suivent alors la langue de l'application. Y inscrire une valeur est un
    # forçage, utile pour un dialecte précis, mais qui survit au changement de
    # langue — c'est pourquoi ce n'est pas le défaut.
    Setting(
        "deepgram_language", "text", "group.dictation",
        default=lambda: settings.stt_language_code, placeholder="fr-CA / en-CA",
        only_for=("stt_provider", "deepgram"),
    ),

    Setting(
        "assemblyai_api_key", "secret", "group.dictation",
        default=lambda: settings.assemblyai_api_key,
        only_for=("stt_provider", "assemblyai"),
    ),
    Setting(
        "assemblyai_model", "text", "group.dictation",
        default=lambda: settings.assemblyai_model, placeholder="universal-3-5-pro",
        only_for=("stt_provider", "assemblyai"),
    ),
    Setting(
        "assemblyai_language", "text", "group.dictation",
        default=lambda: "", placeholder="fr / en / auto",
        only_for=("stt_provider", "assemblyai"),
    ),
    Setting(
        "assemblyai_medical", "choice", "group.dictation",
        default=lambda: "true", choices=ON_OFF,
        only_for=("stt_provider", "assemblyai"),
    ),

    Setting(
        "soniox_api_key", "secret", "group.dictation",
        default=lambda: settings.soniox_api_key,
        only_for=("stt_provider", "soniox"),
    ),
    Setting(
        "soniox_model", "text", "group.dictation",
        default=lambda: settings.soniox_model, placeholder="stt-async-v5",
        only_for=("stt_provider", "soniox"),
    ),
    Setting(
        "soniox_language", "text", "group.dictation",
        default=lambda: "", placeholder="fr / en / auto",
        only_for=("stt_provider", "soniox"),
    ),
    Setting(
        "soniox_send_context", "choice", "group.dictation",
        default=lambda: "true" if settings.soniox_send_context else "false",
        choices=ON_OFF, only_for=("stt_provider", "soniox"),
    ),

    # La clé Cohere sert AUSSI au modèle de langage : elle est répétée sous
    # Note → Cohere via ``also_in``, pour une seule valeur en base.
    Setting(
        "cohere_api_key", "secret", "group.dictation",
        default=lambda: settings.cohere_api_key,
        only_for=("stt_provider", "cohere"),
        also_in=(("group.note", "cohere"),),
    ),
    Setting(
        "cohere_model", "text", "group.dictation",
        default=lambda: settings.cohere_model,
        placeholder="cohere-transcribe-03-2026",
        only_for=("stt_provider", "cohere"),
        datalist=True,
    ),
    Setting(
        "cohere_language", "text", "group.dictation",
        default=lambda: "", placeholder="fr / en",
        only_for=("stt_provider", "cohere"),
    ),

    # Même partage pour Mistral : la clé Voxtral sert au modèle de langage
    # Mistral, le champ est répété sous Note → Mistral AI.
    Setting(
        "mistral_api_key", "secret", "group.dictation",
        default=lambda: settings.mistral_api_key,
        only_for=("stt_provider", "mistral"),
        also_in=(("group.note", "mistral"),),
    ),
    Setting(
        "mistral_model", "text", "group.dictation",
        default=lambda: settings.mistral_model,
        placeholder="voxtral-mini-latest",
        only_for=("stt_provider", "mistral"),
        datalist=True,
    ),
    Setting(
        "mistral_language", "text", "group.dictation",
        default=lambda: "", placeholder="fr / en",
        only_for=("stt_provider", "mistral"),
    ),
    Setting(
        "mistral_realtime_model", "text", "group.dictation",
        default=lambda: settings.mistral_realtime_model,
        placeholder="voxtral-mini-transcribe-realtime-2602",
        only_for=("stt_provider", "mistral"),
        visible_if=(("stt_realtime_mode", "sse"),), section="sect.realtime",
    ),
    Setting(
        "mistral_realtime_delay_ms", "number", "group.dictation",
        default=lambda: str(settings.mistral_realtime_delay_ms),
        only_for=("stt_provider", "mistral"),
        visible_if=(("stt_realtime_mode", "sse"),), section="sect.realtime",
        advanced=True,
    ),

    # Pas de clé propre côté STT : « openai_api_key » (sous Note) sert aux
    # deux usages, même compte — le champ est répété ici via ``also_in``.
    Setting(
        "openai_stt_model", "text", "group.dictation",
        default=lambda: "", placeholder="whisper-1",
        only_for=("stt_provider", "openai"),
        datalist=True,
    ),
    Setting(
        "openai_stt_language", "text", "group.dictation",
        default=lambda: "", placeholder="fr / en",
        only_for=("stt_provider", "openai"),
    ),

    Setting(
        "modulate_api_key", "secret", "group.dictation",
        default=lambda: settings.modulate_api_key,
        only_for=("stt_provider", "modulate"),
    ),
    Setting(
        "modulate_model", "text", "group.dictation",
        default=lambda: "", placeholder="velma-2-stt-batch",
        only_for=("stt_provider", "modulate"),
    ),
    Setting(
        "modulate_language", "text", "group.dictation",
        default=lambda: "", placeholder="fr / en / auto",
        only_for=("stt_provider", "modulate"),
    ),

    Setting(
        "custom_stt_api_key", "secret", "group.dictation",
        default=lambda: "", only_for=("stt_provider", "custom"),
    ),
    Setting(
        "custom_stt_base_url", "text", "group.dictation",
        default=lambda: "", placeholder="https://exemple.tld/v1",
        only_for=("stt_provider", "custom"),
    ),
    Setting(
        "custom_stt_model", "text", "group.dictation",
        default=lambda: "", placeholder="whisper-1",
        only_for=("stt_provider", "custom"),
        datalist=True,
    ),
    Setting(
        "custom_stt_language", "text", "group.dictation",
        default=lambda: "", placeholder="fr / en / auto",
        only_for=("stt_provider", "custom"),
    ),
    Setting(
        "custom_stt_fallback_model", "text", "group.dictation",
        default=lambda: "", placeholder="Systran/faster-whisper-small",
        only_for=("stt_provider", "custom"), advanced=True,
    ),
    Setting(
        "custom_stt_fallback_base_url", "text", "group.dictation",
        default=lambda: "", placeholder="(vide = même adresse que le principal)",
        only_for=("stt_provider", "custom"), advanced=True,
    ),
    Setting(
        "custom_stt_max_seconds", "text", "group.dictation",
        default=lambda: "", placeholder="ex. 380 (vide = pas de routage par durée)",
        only_for=("stt_provider", "custom"), advanced=True,
    ),
    Setting(
        "custom_stt_chunk_seconds", "text", "group.dictation",
        default=lambda: "60", placeholder="ex. 60 (vide = pas de découpage)",
        only_for=("stt_provider", "custom"), advanced=True,
    ),

    # --- OpenRouter (STT) ---
    # Pas de clé propre ici : « openrouter_api_key » se règle sous Note et le
    # champ y est répété via ``also_in`` (un seul compte OpenRouter pour les
    # deux usages). La transcription passe par /chat/completions + part audio,
    # pas par /audio/transcriptions qu'OpenRouter refuse pour inkling-small.
    Setting(
        "openrouter_stt_model", "text", "group.dictation",
        default=lambda: OPENROUTER_DEFAULT_STT_MODEL,
        placeholder="thinkingmachines/inkling-small",
        only_for=("stt_provider", "openrouter"),
        datalist=True,
    ),
    Setting(
        "openrouter_stt_language", "text", "group.dictation",
        default=lambda: "", placeholder="fr / en / auto",
        only_for=("stt_provider", "openrouter"),
    ),

    # --- Retrait des pauses : global, tous services -------------------------
    Setting(
        "stt_trim_silence", "choice", "group.dictation",
        default=lambda: "true" if settings.stt_trim_silence else "false",
        choices=ON_OFF, section="sect.silence",
    ),
    Setting(
        "stt_silence_keep_seconds", "number", "group.dictation",
        default=lambda: str(settings.stt_silence_keep_seconds),
        section="sect.silence",
    ),

    # --- Temps réel de la dictée --------------------------------------------
    # Une couche d'affichage par-dessus le batch fiable. Trois modes — « off »
    # (comportement historique, l'audio ne quitte jamais la machine), « vad »
    # (le VAD du navigateur signale la fin de chaque énoncé, transcription
    # immédiate au silence — tous fournisseurs, dont le Parakeet local),
    # « sse » (deltas streaming chez Mistral Voxtral realtime pendant la
    # parole). « sse » n'est applicable qu'à Mistral et « vad » est
    # incompatible avec Cohere (5 req/min) — le serveur retombe silencieusement
    # sur « off » dans ces cas.
    Setting(
        "stt_realtime_mode", "choice", "group.dictation",
        default=lambda: settings.stt_realtime_mode,
        choices=(
            ("off", "set.stt_realtime_mode.off"),
            ("vad", "set.stt_realtime_mode.vad"),
            ("sse", "set.stt_realtime_mode.sse"),
        ),
        section="sect.realtime",
    ),
    Setting(
        "stt_vad_sensitivity", "choice", "group.dictation",
        default=lambda: settings.stt_vad_sensitivity,
        choices=(
            ("low", "set.stt_vad_sensitivity.low"),
            ("medium", "set.stt_vad_sensitivity.medium"),
            ("high", "set.stt_vad_sensitivity.high"),
        ),
        section="sect.realtime", visible_if=(("stt_realtime_mode", "vad"),),
    ),
    Setting(
        "stt_vad_speech_ms", "number", "group.dictation",
        default=lambda: str(settings.stt_vad_speech_ms),
        section="sect.realtime", visible_if=(("stt_realtime_mode", "vad"),),
        advanced=True,
    ),
    Setting(
        "stt_vad_silence_ms", "number", "group.dictation",
        default=lambda: str(settings.stt_vad_silence_ms),
        section="sect.realtime", visible_if=(("stt_realtime_mode", "vad"),),
        advanced=True,
    ),
    Setting(
        "stt_vad_finish_sweep", "choice", "group.dictation",
        default=lambda: "true" if settings.stt_vad_finish_sweep else "false",
        choices=ON_OFF,
        section="sect.realtime", visible_if=(("stt_realtime_mode", "vad"),),
    ),

    # --- Correction medicaments (liste pointée sous la dictée) ---------------
    # Active la stabilisation audio par l'arrière + le grounding des noms de
    # médicaments (moteur déterministe, base BDP livrée dans l'image). Global
    # : c'est l'installation tout entière qui active/désactive la
    # re-transcription en arrière-plan pendant la dictée.
    Setting(
        "dictation_grounding", "choice", "group.dictation",
        default=lambda: "true" if settings.dictation_grounding else "false",
        choices=ON_OFF, section="sect.med_grounding",
    ),

    # =========================================================================
    # --- Note ----------------------------------------------------------------
    # =========================================================================
    # AUCUN réglage commun de modèle ici, volontairement : un champ « Modèle »
    # unique partagé par les cinq fournisseurs affichait le nom d'un modèle
    # Gemini même quand Anthropic ou OpenAI était actif, et changer de
    # fournisseur écrasait silencieusement ce que l'autre avait de configuré.
    # Chaque fournisseur garde SES valeurs modèle/rapide/température ; seuls
    # les TEXTES sont partagés (fabriques _cap_*). Ils s'enregistrent tous
    # ensemble au clic sur « Enregistrer », comme n'importe quel autre réglage.
    Setting(
        "llm_provider", "choice", "group.note",
        default=lambda: "gemini", choices=LLM_PROVIDERS,
    ),

    # --- Gemini ---
    Setting(
        "gemini_api_key", "secret", "group.note",
        default=lambda: settings.gemini_api_key,
        only_for=("llm_provider", "gemini"),
    ),
    _cap_model("gemini", "gemini",
               default=lambda: settings.active_gemini_model,
               help_key="set.cap.model.help"),
    _cap_fast("gemini", "gemini"),
    _cap_verify("gemini", "gemini"),
    _cap_temperature("gemini", "gemini", lambda: str(settings.gemini_temperature)),
    Setting(
        "gemini_thinking", "choice", "group.note",
        default=lambda: "false", choices=ON_OFF,
        only_for=("llm_provider", "gemini"),
    ),
    Setting(
        "gemini_thinking_budget", "number", "group.note",
        default=lambda: str(settings.gemini_thinking_budget),
        placeholder="128", only_for=("llm_provider", "gemini"),
        visible_if=(("gemini_thinking", "true"),),
    ),
    *_cap_audio("gemini", "gemini"),
    *_cap_bypass("gemini", "gemini"),

    # --- Anthropic Claude ---
    Setting(
        "anthropic_api_key", "secret", "group.note",
        default=lambda: settings.anthropic_api_key,
        only_for=("llm_provider", "anthropic"),
    ),
    _cap_model("anthropic", "anthropic"),
    _cap_fast("anthropic", "anthropic"),
    _cap_verify("anthropic", "anthropic"),
    _cap_temperature("anthropic", "anthropic", lambda: str(settings.gemini_temperature)),

    # --- OpenAI ---
    # Clé unique pour les deux usages (transcription Whisper et note) : le
    # champ natif vit ici, il est répété sous Dictée → OpenAI Whisper.
    Setting(
        "openai_api_key", "secret", "group.note",
        default=lambda: settings.openai_api_key,
        only_for=("llm_provider", "openai"),
        also_in=(("group.dictation", "openai"),),
    ),
    _cap_model("openai", "openai"),
    _cap_fast("openai", "openai"),
    _cap_verify("openai", "openai"),
    _cap_temperature("openai", "openai", lambda: str(settings.gemini_temperature)),

    # --- Cohere ---
    # Pas de clé propre ici : elle se règle sous Dictée (cohere_api_key) et le
    # champ y est répété via ``also_in``. Suffixe « _llm » pour ne pas entrer
    # en collision avec cohere_model, qui désigne le modèle de TRANSCRIPTION.
    _cap_model("cohere", "cohere_llm", default=lambda: COHERE_DEFAULT_LLM_MODEL),
    _cap_fast("cohere", "cohere_llm"),
    _cap_verify("cohere", "cohere_llm"),
    _cap_temperature("cohere", "cohere_llm", lambda: str(settings.gemini_temperature)),
    Setting(
        "cohere_llm_thinking_budget", "number", "group.note",
        default=lambda: "1024", only_for=("llm_provider", "cohere"),
    ),

    # --- Mistral ---
    _cap_model("mistral", "mistral_llm", default=lambda: MISTRAL_DEFAULT_LLM_MODEL),
    _cap_fast("mistral", "mistral_llm"),
    _cap_verify("mistral", "mistral_llm"),
    _cap_temperature("mistral", "mistral_llm", lambda: str(settings.gemini_temperature)),

    # --- Qwen Omni (Alibaba Cloud DashScope, mode compatible OpenAI) ---
    # Clé et adresse propres, comme « custom » : la région (internationale ou
    # Chine continentale) détermine l'adresse exacte, laissée à saisir plutôt
    # que devinée.
    Setting(
        "qwen_omni_api_key", "secret", "group.note",
        default=lambda: settings.qwen_omni_api_key,
        only_for=("llm_provider", "qwen_omni"),
    ),
    Setting(
        "qwen_omni_base_url", "text", "group.note",
        default=lambda: settings.qwen_omni_base_url,
        placeholder="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        only_for=("llm_provider", "qwen_omni"),
    ),
    _cap_model("qwen_omni", "qwen_omni"),
    _cap_fast("qwen_omni", "qwen_omni"),
    _cap_verify("qwen_omni", "qwen_omni"),
    _cap_temperature("qwen_omni", "qwen_omni", lambda: str(settings.gemini_temperature)),
    *_cap_audio("qwen_omni", "qwen_omni"),
    *_cap_bypass("qwen_omni", "qwen_omni"),

    # --- Point de terminaison personnalisé (compatible OpenAI) ---
    # Clé et adresse propres : rien ne garantit que ce soit le même compte
    # qu'un autre fournisseur. Les textes des options audio y sont légèrement
    # différents (multimodalité incertaine, format à choisir) : ils gardent
    # leurs clés propres via ``help_key=None`` + ``dataclasses.replace``.
    Setting(
        "custom_llm_api_key", "secret", "group.note",
        default=lambda: "", only_for=("llm_provider", "custom"),
    ),
    Setting(
        "custom_llm_base_url", "text", "group.note",
        default=lambda: "", placeholder="https://exemple.tld/v1",
        only_for=("llm_provider", "custom"),
    ),
    _cap_model("custom", "custom_llm", help_key=None),
    _cap_fast("custom", "custom_llm"),
    _cap_verify("custom", "custom_llm"),
    dataclasses.replace(
        _cap_temperature("custom", "custom_llm", lambda: str(settings.gemini_temperature)),
        help_key=None,
    ),
    Setting(
        "custom_llm_max_tokens", "text", "group.note",
        default=lambda: "32768", placeholder="ex. 32768 (jetons de sortie)",
        only_for=("llm_provider", "custom"),
    ),
    Setting(
        "custom_llm_reasoning_effort", "choice", "group.note",
        default=lambda: "auto",
        choices=(
            ("auto", "choice.reasoning_auto"),
            ("none", "choice.reasoning_none"),
            ("minimal", "choice.reasoning_minimal"),
            ("low", "choice.reasoning_low"),
            ("medium", "choice.reasoning_medium"),
            ("high", "choice.reasoning_high"),
        ),
        only_for=("llm_provider", "custom"),
    ),
    *_cap_audio("custom", "custom", help_key=None),
    Setting(
        "custom_send_audio_format", "choice", "group.note",
        default=lambda: "ogg",
        choices=(("ogg", "ogg"), ("mp3", "mp3"), ("wav", "wav")),
        only_for=("llm_provider", "custom"),
        visible_if=(("custom_send_audio", "true"),),
    ),
    *_cap_bypass("custom", "custom", help_key=None),

    # --- OpenRouter (modèles multimodaux open-weight, ex. Inkling) ---
    # Clé unique pour les deux usages (note et STT) : le champ natif vit ici,
    # il est répété sous Dictée → OpenRouter.
    Setting(
        "openrouter_api_key", "secret", "group.note",
        default=lambda: settings.openrouter_api_key,
        only_for=("llm_provider", "openrouter"),
        also_in=(("group.dictation", "openrouter"),),
    ),
    _cap_model("openrouter", "openrouter", default=lambda: OPENROUTER_DEFAULT_LLM_MODEL),
    _cap_fast("openrouter", "openrouter"),
    _cap_verify("openrouter", "openrouter"),
    _cap_temperature("openrouter", "openrouter", lambda: str(settings.gemini_temperature)),
    Setting(
        "openrouter_llm_max_tokens", "text", "group.note",
        default=lambda: "32768", placeholder="ex. 32768 (jetons de sortie)",
        only_for=("llm_provider", "openrouter"),
    ),
    Setting(
        "openrouter_llm_reasoning_effort", "choice", "group.note",
        default=lambda: "auto",
        choices=(
            ("auto", "choice.reasoning_auto"),
            ("none", "choice.reasoning_none"),
            ("minimal", "choice.reasoning_minimal"),
            ("low", "choice.reasoning_low"),
            ("medium", "choice.reasoning_medium"),
            ("high", "choice.reasoning_high"),
        ),
        only_for=("llm_provider", "openrouter"),
    ),
    *_cap_audio("openrouter", "openrouter"),
    Setting(
        "openrouter_send_audio_format", "choice", "group.note",
        default=lambda: "ogg",
        choices=(("ogg", "ogg"), ("mp3", "mp3"), ("wav", "wav")),
        only_for=("llm_provider", "openrouter"),
        visible_if=(("openrouter_send_audio", "true"),),
    ),
    *_cap_bypass("openrouter", "openrouter"),

    # Affichage du raisonnement du modèle (thinking) pendant la génération :
    # deux bascules indépendantes — administrateurs d'abord, autres usagers
    # ensuite. La pensée est diffusée en direct puis effacée, jamais persistée.
    Setting(
        "show_thinking_admin", "choice", "group.note",
        default=lambda: "false", choices=ON_OFF,
    ),
    Setting(
        "show_thinking_users", "choice", "group.note",
        default=lambda: "false", choices=ON_OFF,
    ),

    # --- Consigne générale ---------------------------------------------------
    # Une consigne par langue. C'est la LANGUE DU GABARIT qui décide laquelle
    # est employée — voir llm.build_system_prompt. Les valeurs livrées viennent
    # de app/default_prompts.py, sous contrôle de version.
    Setting(
        "general_prompt_fr", "textarea", "group.note",
        default=lambda: default_prompts.GENERAL_PROMPT_FR,
        placeholder="set.general_prompt.placeholder",
        help_key="set.general_prompt.help", section="sect.prompts",
    ),
    Setting(
        "general_prompt_en", "textarea", "group.note",
        default=lambda: default_prompts.GENERAL_PROMPT_EN,
        placeholder="set.general_prompt.placeholder",
        help_key="set.general_prompt.help", section="sect.prompts",
    ),

    # =========================================================================
    # --- Comptes et accès ----------------------------------------------------
    # =========================================================================
    Setting(
        "allow_signup", "choice", "group.access",
        default=lambda: "true" if settings.allow_signup else "false", choices=ON_OFF,
    ),
    # Quelle revendication du fournisseur porte le nom et l'avatar : c'est là
    # qu'on regarde quand un nom s'affiche mal.
    Setting(
        "oidc_name_claim", "text", "group.access",
        default=lambda: settings.oidc_name_claim,
        placeholder="name / preferred_username / nickname",
    ),
    Setting(
        "oidc_picture_claim", "text", "group.access",
        default=lambda: settings.oidc_picture_claim, placeholder="picture",
    ),

    # =========================================================================
    # --- Données et sauvegarde -------------------------------------------------
    # =========================================================================
    Setting(
        "consultation_retention_hours", "number", "group.data",
        default=lambda: "12",
    ),
    Setting(
        "backup_retention_count", "number", "group.data",
        default=lambda: "7",
    ),
)

#: Ordre d'affichage des onglets du panneau — organisés par flux de travail :
#: comment la voix devient texte (Dictée), comment le texte devient note
#: (Note), qui entre (Comptes et accès), combien de temps on garde (Données),
#: ce que ça consomme (Statistiques).
GROUPS: Tuple[str, ...] = (
    "group.dictation", "group.note", "group.access", "group.data", "group.stats",
)

BY_KEY: Dict[str, Setting] = {item.key: item for item in SETTINGS}

#: Sélecteur de service porté par chaque onglet « à fournisseurs ». Le client
#: en déduit le sous-menu ; côté serveur, cela reste implicite dans les
#: ``only_for`` / ``also_in`` des réglages.
PROVIDER_SELECTORS: Dict[str, str] = {
    "group.dictation": "stt_provider",
    "group.note": "llm_provider",
}

#: Avertissements affichés en tête d'un onglet selon la valeur d'un réglage.
#: Cohere plafonne à 5 requêtes/minute : c'est une contrainte qui décide de
#: l'usage qu'on peut en faire, pas un détail à enterrer dans un texte d'aide.
WARNINGS: Tuple[dict, ...] = (
    {
        "group": "group.dictation",
        "key": "stt_provider",
        "value": "cohere",
        "messages": ("admin.cohere_warning", "admin.cohere_no_vocab"),
    },
)


def group_warnings(language_code: Optional[str] = None) -> Dict[str, List[dict]]:
    """
    Avertissements par onglet, prêts à être filtrés par le client selon le
    service CONSULTÉ (pas seulement l'actif).
    """
    langue = i18n.normalize(language_code or language())
    resultat: Dict[str, List[dict]] = {}
    for alerte in WARNINGS:
        resultat.setdefault(alerte["group"], []).append({
            "selector": alerte["key"],
            "value": alerte["value"],
            "messages": [i18n.t(message, langue) for message in alerte["messages"]],
        })
    return resultat


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
        "modulate": "modulate_language",
        "openrouter": "openrouter_stt_language",
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
        "modulate": "modulate_model",
        "openrouter": "openrouter_stt_model",
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


def _est_visible(setting: Setting, valeurs: Dict[str, str]) -> bool:
    """Les conditions ``visible_if`` sont-elles réunies avec les valeurs
    effectives courantes ?"""
    return all(valeurs.get(cle) == attendu for cle, attendu in setting.visible_if)


def describe(language_code: Optional[str] = None) -> List[dict]:
    """
    Schéma + valeurs courantes, prêt à être affiché par le panneau.

    Les libellés sont rendus dans ``language_code``, ou dans la langue
    effective de l'application si l'appelant ne précise rien. Chaque entrée
    porte ses métadonnées de présentation (``only_for``, ``also_in``,
    ``visible_if``, ``section``, ``advanced``, ``datalist``) : le client ne
    redéclare plus rien et reconstruit la visibilité localement à chaque
    frappe.
    """
    langue = i18n.normalize(language_code or language())
    valeurs = {cle: value(cle) for cle in BY_KEY}
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
            # Métadonnées de présentation — sérialisées en JSON simple.
            "only_for": (
                {"key": setting.only_for[0], "value": setting.only_for[1]}
                if setting.only_for else None
            ),
            "also_in": [[groupe, service] for groupe, service in setting.also_in],
            "visible_if": [{"key": cle, "value": attendu} for cle, attendu in setting.visible_if],
            "visible": _est_visible(setting, valeurs),
            "section": i18n.t(setting.section, langue) if setting.section else "",
            "advanced": setting.advanced,
            "datalist": setting.datalist,
        }
        if setting.kind == "secret":
            entry.update(_mask(valeurs[setting.key]))
            # D'où vient la clé : utile quand rien ne marche et qu'on cherche
            # si c'est le panneau ou le .env qui fait foi.
            entry["from_env"] = bool(setting.default_value()) and not is_overridden(setting.key)
        else:
            entry["value"] = valeurs[setting.key]
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
