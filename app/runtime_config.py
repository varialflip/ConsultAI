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
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import select

from app.config import settings
from app.database import AppSetting, SessionLocal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Description des réglages
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str                     # text | secret | choice | textarea | number
    group: str
    default: Callable[[], str] = lambda: ""
    choices: Tuple[Tuple[str, str], ...] = ()
    help: str = ""
    placeholder: str = ""

    def default_value(self) -> str:
        return str(self.default() or "")


STT_PROVIDERS = (
    ("google", "Google Speech-to-Text"),
    ("deepgram", "Deepgram"),
    ("assemblyai", "AssemblyAI"),
    ("soniox", "Soniox"),
)

#: Un booléen présenté comme un choix : le panneau sait déjà afficher un
#: « choice », et cela évite un type de champ de plus pour deux réglages.
ON_OFF = (("true", "Activé"), ("false", "Désactivé"))

LLM_PROVIDERS = (
    ("gemini", "Google Gemini"),
    ("anthropic", "Anthropic Claude"),
    ("openai", "OpenAI"),
)


SETTINGS: Tuple[Setting, ...] = (
    # --- Reconnaissance vocale ---------------------------------------------
    Setting(
        "stt_provider", "Service de reconnaissance vocale", "choice", "Reconnaissance vocale",
        default=lambda: "google", choices=STT_PROVIDERS,
        help="Le découpage de la dictée en tranches est identique dans les deux cas : "
             "seul l'envoi final change.",
    ),
    Setting(
        "deepgram_api_key", "Clé API Deepgram", "secret", "Reconnaissance vocale",
        default=lambda: settings.deepgram_api_key,
        help="console.deepgram.com → API Keys. Requise si Deepgram est sélectionné.",
    ),
    Setting(
        "deepgram_model", "Modèle Deepgram", "text", "Reconnaissance vocale",
        default=lambda: settings.deepgram_model, placeholder="nova-2",
        help="nova-2 pour le français canadien (l'adaptation par mots-clés n'existe "
             "que sur cette génération). nova-3 est plus récent mais ignore les "
             "mots-clés hors anglais.",
    ),
    Setting(
        "deepgram_language", "Langue Deepgram", "text", "Reconnaissance vocale",
        default=lambda: settings.stt_language_code, placeholder="fr-CA",
    ),
    Setting(
        "assemblyai_api_key", "Clé API AssemblyAI", "secret", "Reconnaissance vocale",
        default=lambda: settings.assemblyai_api_key,
        help="assemblyai.com → Dashboard → API Keys.",
    ),
    Setting(
        "assemblyai_model", "Modèle AssemblyAI", "text", "Reconnaissance vocale",
        default=lambda: settings.assemblyai_model, placeholder="universal-3-5-pro",
        help="universal-3-5-pro (défaut) ou universal-2. Le premier reconnaît "
             "explicitement le français québécois et accepte 1000 termes "
             "d'adaptation, contre 200 pour le second.",
    ),
    Setting(
        "assemblyai_language", "Langue AssemblyAI", "text", "Reconnaissance vocale",
        default=lambda: "fr", placeholder="fr",
        help="« fr » couvre le français québécois : AssemblyAI ne demande pas de "
             "code de dialecte. Laisser vide pour la détection automatique.",
    ),
    Setting(
        "assemblyai_medical", "Mode médical AssemblyAI", "choice", "Reconnaissance vocale",
        default=lambda: "true", choices=ON_OFF,
        help="Module « medical-v1 » : améliore les noms de médicaments, de "
             "procédures, les diagnostics et les posologies. Le français en fait "
             "partie. Facturé en supplément (~0,15 $US/h) ; sur une langue non "
             "prise en charge, l'option est simplement ignorée, sans frais.",
    ),

    Setting(
        "stt_trim_silence", "Retirer les longues pauses", "choice", "Reconnaissance vocale",
        default=lambda: "true" if settings.stt_trim_silence else "false", choices=ON_OFF,
        help="Les trois services facturent à la durée d'audio. Seule la copie envoyée "
             "est raccourcie : l'enregistrement conservé avec le brouillon reste "
             "intact, et la durée affichée reste celle de la dictée réelle.",
    ),
    Setting(
        "stt_silence_keep_seconds", "Pause conservée (secondes)", "number",
        "Reconnaissance vocale",
        default=lambda: str(settings.stt_silence_keep_seconds),
        help="Toute pause plus courte est gardée telle quelle ; les plus longues sont "
             "ramenées à cette durée. Ne pas descendre à 0 : les moteurs se servent "
             "des pauses pour placer la ponctuation et séparer les phrases — sur une "
             "liste de médicaments, cela compte.",
    ),

    Setting(
        "soniox_api_key", "Clé API Soniox", "secret", "Reconnaissance vocale",
        default=lambda: settings.soniox_api_key,
        help="console.soniox.com → API Keys.",
    ),
    Setting(
        "soniox_model", "Modèle Soniox", "text", "Reconnaissance vocale",
        default=lambda: settings.soniox_model, placeholder="stt-async-v5",
        help="Modèle asynchrone (fichier). Le tarif annoncé est d'environ "
             "0,10 $US/h, soit le quart d'AssemblyAI avec ses modules.",
    ),
    Setting(
        "soniox_language", "Langue Soniox", "text", "Reconnaissance vocale",
        default=lambda: "fr", placeholder="fr",
        help="Indice de langue. Soniox est multilingue par conception : laisser "
             "vide active la détection automatique, utile si la consultation "
             "alterne français et anglais.",
    ),

    # --- Modèle de langage --------------------------------------------------
    Setting(
        "llm_provider", "Fournisseur", "choice", "Modèle de langage",
        default=lambda: "gemini", choices=LLM_PROVIDERS,
    ),
    Setting(
        "llm_model", "Modèle", "text", "Modèle de langage",
        default=lambda: settings.active_gemini_model,
        placeholder="gemini-2.5-flash",
        help="Le bouton « Modèles disponibles » interroge le fournisseur avec la clé "
             "configurée et affiche ce à quoi ce compte a réellement droit.",
    ),
    Setting(
        "llm_model_fast", "Modèle rapide (métadonnées)", "text", "Modèle de langage",
        default=lambda: settings.gemini_model,
        help="Utilisé pour la seule relecture des métadonnées, une tâche triviale "
             "payée au jeton. Laisser vide pour employer le modèle principal.",
    ),
    Setting(
        "llm_temperature", "Température", "number", "Modèle de langage",
        default=lambda: str(settings.gemini_temperature),
        help="0 = déterministe. Au-delà de 0,4 le modèle commence à broder, ce qui "
             "n'a pas sa place dans une note clinique. Les modèles les plus "
             "récents ne l'acceptent plus : le réglage est alors ignoré, la note "
             "est produite quand même.",
    ),
    Setting(
        "gemini_api_key", "Clé API Google Gemini", "secret", "Modèle de langage",
        default=lambda: settings.gemini_api_key,
    ),
    Setting(
        "anthropic_api_key", "Clé API Anthropic", "secret", "Modèle de langage",
        default=lambda: settings.anthropic_api_key,
    ),
    Setting(
        "openai_api_key", "Clé API OpenAI", "secret", "Modèle de langage",
        default=lambda: settings.openai_api_key,
    ),

    # --- Consignes ----------------------------------------------------------
    Setting(
        "general_prompt", "Consigne générale", "textarea", "Consignes",
        default=lambda: "",
        placeholder="Ex. : Utiliser systématiquement le vouvoiement. Ne jamais "
                    "abréger les noms de médicaments.",
        help="Ajoutée aux consignes de TOUS les gabarits et appliquée quel que soit "
             "le modèle choisi. Elle passe après celles du gabarit : en cas de "
             "contradiction, c'est elle qui l'emporte.",
    ),
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


def describe() -> List[dict]:
    """Schéma + valeurs courantes, prêt à être affiché par le panneau."""
    result = []
    for setting in SETTINGS:
        entry = {
            "key": setting.key,
            "label": setting.label,
            "kind": setting.kind,
            "group": setting.group,
            "help": setting.help,
            "placeholder": setting.placeholder,
            "choices": [{"value": v, "label": label} for v, label in setting.choices],
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
                        f"Valeur refusée pour « {setting.label} » : {new_value}"
                    )
            if setting.kind == "number" and new_value:
                try:
                    float(new_value.replace(",", "."))
                except ValueError as exc:
                    raise ValueError(f"« {setting.label} » doit être un nombre.") from exc

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
