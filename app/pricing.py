"""
pricing.py — Tarifs fournisseur et calcul du coût d'un appel.
================================================================

Aucun tarif n'était suivi nulle part avant ce module. ``DEFAULT_RATES``
n'est qu'un point de départ approximatif : à corriger depuis l'onglet
« Statistiques » du panneau admin dès que les tarifs réels sont connus — les
générations passées gardent le coût calculé avec le tarif en vigueur au
moment de l'appel (voir ``UsageEvent.cost`` dans ``app/database.py``), une
correction ultérieure ne réécrit jamais l'historique.

Unités : ``token_input_1m``/``token_output_1m`` (prix pour 1 million de
jetons texte), ``token_audio_input_1m`` (prix pour 1 million de jetons
d'entrée AUDIO — Gemini 2.5 Flash et Qwen Omni facturent l'audio entrant
à un tarif distinct du texte et le ventilent dans la réponse) et
``audio_minute`` (prix pour 1 minute d'audio) — jamais un prix
par jeton unique, illisible en décimal.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import PricingRate

logger = logging.getLogger(__name__)

# provider, model ("" = tarif par défaut du fournisseur), kind, unit, rate (USD)
# Toute entrée peut porter une 6e valeur optionnelle : la devise (défaut « USD ») —
# la facturation du point de terminaison personnalisé peut être en CAD (ex. Augure).
# Placeholders approximatifs — à vérifier/corriger depuis le panneau admin.
DEFAULT_RATES: Tuple[Tuple[str, str, str, str, float, str], ...] = (
    # --- LLM (texte → note), $ / 1M jetons ---------------------------------
    ("gemini", "", "llm", "token_input_1m", 1.25),
    ("gemini", "", "llm", "token_output_1m", 5.00),
    # Jetons d'entrée audio (bypass STT : la dictée part directement au
    # LLM). Tarif distinct du texte chez Gemini 2.5 Flash comme chez Qwen
    # Omni — placeholder, à corriger depuis le panneau admin.
    ("gemini", "", "llm", "token_audio_input_1m", 3.00),
    ("anthropic", "", "llm", "token_input_1m", 3.00),
    ("anthropic", "", "llm", "token_output_1m", 15.00),
    ("openai", "", "llm", "token_input_1m", 2.50),
    ("openai", "", "llm", "token_output_1m", 10.00),
    ("cohere", "", "llm", "token_input_1m", 0.50),
    ("cohere", "", "llm", "token_output_1m", 1.50),
    ("mistral", "", "llm", "token_input_1m", 2.00),
    ("mistral", "", "llm", "token_output_1m", 6.00),
    ("qwen_omni", "", "llm", "token_input_1m", 0.50),
    ("qwen_omni", "", "llm", "token_output_1m", 1.50),
    ("qwen_omni", "", "llm", "token_audio_input_1m", 1.50),

    # --- STT (audio → texte) -------------------------------------------------
    # Facturés à la durée : google, deepgram, assemblyai, soniox.
    ("google", "", "stt", "audio_minute", 0.024),
    ("deepgram", "", "stt", "audio_minute", 0.0043),
    ("assemblyai", "", "stt", "audio_minute", 0.0062),
    ("soniox", "", "stt", "audio_minute", 0.006),
    # Facturés au jeton (audio envoyé comme entrée multimodale d'un LLM) :
    # cohere/mistral partagent la clé API LLM, qwen_omni et openai idem selon
    # le modèle — à corriger si le modèle réellement utilisé facture autrement.
    ("cohere", "", "stt", "token_input_1m", 0.50),
    ("mistral", "", "stt", "token_input_1m", 2.00),
    ("openai", "", "stt", "audio_minute", 0.006),

    # --- Augure (AI souveraine canadienne, API OpenAI-compatible). Fournisseur
    # ``augure`` dédié (augure_base_url / augure_api_key) ; texte seul, facturé
    # en CAD (augureai.ca) — seule ligne à devise non-USD ici. Tarifs officiels
    # au 2026-08-16 (model 5) : ossington-5 input 1.50 CAD / output 3.00 CAD
    # par 1M jetons.
    ("augure", "ossington-5", "llm", "token_input_1m", 1.50, "CAD"),
    ("augure", "ossington-5", "llm", "token_output_1m", 3.00, "CAD"),
)


def seed_default_rates(db: Session) -> int:
    """Insère les tarifs par défaut absents. Idempotent — n'écrase jamais un
    tarif déjà présent (potentiellement corrigé par un admin)."""
    existing = {
        (row.provider, row.model, row.kind, row.unit)
        for row in db.scalars(select(PricingRate))
    }
    added = 0
    for entry in DEFAULT_RATES:
        provider, model, kind, unit, rate, *devise = entry
        if (provider, model, kind, unit) in existing:
            continue
        db.add(PricingRate(
            provider=provider, model=model, kind=kind, unit=unit, rate=rate,
            currency=devise[0] if devise else "USD",
        ))
        added += 1
    if added:
        db.commit()
        logger.info("Tarifs par défaut amorcés : %d ligne(s)", added)
    return added


def rate_for(db: Session, provider: str, model: str, kind: str, unit: str) -> Optional[PricingRate]:
    """Tarif exact (fournisseur, modèle, type, unité) sinon tarif par défaut
    du fournisseur (``model=""``)."""
    exact = db.scalar(
        select(PricingRate).where(
            PricingRate.provider == provider,
            PricingRate.model == model,
            PricingRate.kind == kind,
            PricingRate.unit == unit,
        )
    )
    if exact is not None:
        return exact
    return db.scalar(
        select(PricingRate).where(
            PricingRate.provider == provider,
            PricingRate.model == "",
            PricingRate.kind == kind,
            PricingRate.unit == unit,
        )
    )


def compute_cost(
    db: Session,
    kind: str,
    provider: str,
    model: str,
    *,
    prompt_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    audio_prompt_tokens: Optional[int] = None,
    audio_seconds: Optional[int] = None,
) -> Tuple[Optional[float], str]:
    """
    Coût estimé en USD, ou ``(None, "USD")`` si aucun tarif ne correspond —
    un tarif manquant ne doit jamais empêcher l'enregistrement d'une
    consultation, seulement laisser le coût vide.

    ``prompt_tokens`` ne compte que le texte d'entrée quand le fournisseur
    ventile par modalité (Gemini, Qwen Omni) — l'audio entrant voyage dans
    ``audio_prompt_tokens`` et se paie au tarif ``token_audio_input_1m``.
    Sans ventilation, ``prompt_tokens`` reste le total d'entrée.
    """
    total = 0.0
    matched = False

    if prompt_tokens:
        rate = rate_for(db, provider, model, kind, "token_input_1m")
        if rate is not None:
            total += (prompt_tokens / 1_000_000) * rate.rate
            matched = True
    if output_tokens:
        rate = rate_for(db, provider, model, kind, "token_output_1m")
        if rate is not None:
            total += (output_tokens / 1_000_000) * rate.rate
            matched = True
    if audio_prompt_tokens:
        rate = rate_for(db, provider, model, kind, "token_audio_input_1m")
        if rate is not None:
            total += (audio_prompt_tokens / 1_000_000) * rate.rate
            matched = True
    if audio_seconds:
        rate = rate_for(db, provider, model, kind, "audio_minute")
        if rate is not None:
            total += (audio_seconds / 60) * rate.rate
            matched = True

    if not matched:
        logger.debug("Aucun tarif pour %s/%s (%s) — coût non calculé", provider, model, kind)
        return None, "USD"
    return total, "USD"
