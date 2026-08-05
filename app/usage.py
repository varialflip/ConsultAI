"""
usage.py — Journal d'usage (jetons LLM, durée audio STT) et son compactage.
==============================================================================

Écrit un ``UsageEvent`` à chaque appel facturé. Les événements bruts sont
conservés ``RAW_RETENTION_DAYS`` jours (utile pour comprendre pourquoi UNE
consultation a coûté cher), puis ``compact_old_events`` les regroupe en
lignes quotidiennes ``UsageDaily`` — jamais purgées, c'est la base des
statistiques à long terme de l'onglet admin.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import pricing
from app.database import SessionLocal, UsageDaily, UsageEvent, utcnow

logger = logging.getLogger(__name__)

#: Au-delà de ce délai, un événement brut n'a plus qu'un intérêt statistique
#: (pas de dépannage au cas par cas) — voir la doc de UsageEvent.
RAW_RETENTION_DAYS = 45


def log_llm_usage(
    db: Session,
    *,
    owner: str,
    consultation_id: Optional[int],
    provider: str,
    model: str,
    prompt_tokens: Optional[int],
    output_tokens: Optional[int],
    audio_prompt_tokens: Optional[int] = None,
) -> None:
    cost, currency = pricing.compute_cost(
        db, "llm", provider, model,
        prompt_tokens=prompt_tokens, output_tokens=output_tokens,
        audio_prompt_tokens=audio_prompt_tokens,
    )
    db.add(UsageEvent(
        owner=owner, consultation_id=consultation_id, kind="llm",
        provider=provider, model=model,
        prompt_tokens=prompt_tokens, output_tokens=output_tokens,
        audio_prompt_tokens=audio_prompt_tokens,
        cost=cost, currency=currency,
    ))


def log_stt_usage(
    db: Session,
    *,
    owner: str,
    consultation_id: Optional[int],
    provider: str,
    model: str,
    audio_seconds: Optional[int],
) -> None:
    if not provider or not audio_seconds:
        return
    cost, currency = pricing.compute_cost(
        db, "stt", provider, model, audio_seconds=audio_seconds,
    )
    db.add(UsageEvent(
        owner=owner, consultation_id=consultation_id, kind="stt",
        provider=provider, model=model,
        audio_seconds=audio_seconds,
        cost=cost, currency=currency,
    ))


def compact_old_events() -> None:
    """Tâche planifiée quotidienne (voir app/scheduler.py). Regroupe tout
    événement plus vieux que RAW_RETENTION_DAYS en lignes UsageDaily, puis
    efface les événements bruts compactés."""
    cutoff = utcnow() - timedelta(days=RAW_RETENTION_DAYS)
    with SessionLocal() as db:
        rows = db.scalars(select(UsageEvent).where(UsageEvent.created_at < cutoff)).all()
        if not rows:
            return

        buckets: dict[tuple, dict] = defaultdict(lambda: {
            "prompt_tokens": 0, "output_tokens": 0, "audio_seconds": 0,
            "audio_prompt_tokens": 0,
            "cost": 0.0, "currency": "USD", "event_count": 0,
        })
        for row in rows:
            day = row.created_at.date().isoformat()
            key = (day, row.owner, row.kind, row.provider, row.model)
            bucket = buckets[key]
            bucket["prompt_tokens"] += row.prompt_tokens or 0
            bucket["output_tokens"] += row.output_tokens or 0
            bucket["audio_prompt_tokens"] += row.audio_prompt_tokens or 0
            bucket["audio_seconds"] += row.audio_seconds or 0
            bucket["cost"] += row.cost or 0.0
            bucket["currency"] = row.currency or "USD"
            bucket["event_count"] += 1

        for (day, owner, kind, provider, model), agg in buckets.items():
            daily = db.scalar(
                select(UsageDaily).where(
                    UsageDaily.date == day, UsageDaily.owner == owner,
                    UsageDaily.kind == kind, UsageDaily.provider == provider,
                    UsageDaily.model == model,
                )
            )
            if daily is None:
                daily = UsageDaily(date=day, owner=owner, kind=kind, provider=provider, model=model)
                db.add(daily)
            daily.prompt_tokens += agg["prompt_tokens"]
            daily.output_tokens += agg["output_tokens"]
            daily.audio_prompt_tokens += agg["audio_prompt_tokens"]
            daily.audio_seconds += agg["audio_seconds"]
            daily.cost += agg["cost"]
            daily.currency = agg["currency"]
            daily.event_count += agg["event_count"]

        for row in rows:
            db.delete(row)
        db.commit()
        logger.info(
            "Compactage de l'usage : %d événement(s) regroupé(s) en %d ligne(s) quotidienne(s)",
            len(rows), len(buckets),
        )


def _empty_summary() -> dict:
    return {"prompt_tokens": 0, "output_tokens": 0, "audio_seconds": 0, "audio_prompt_tokens": 0,
            "cost": 0.0, "currency": "USD",
            "by_provider": []}


def summary_for_owner(
    db: Session, owner: str, since: datetime, until: Optional[datetime] = None,
) -> dict:
    """Alimente GET /api/me/usage : cumul sur [`since`, `until`[ (sans borne
    haute si `until` est absent), sur les événements bruts ET les lignes déjà
    compactées (un mois peut chevaucher les deux si le compactage a tourné
    entre-temps)."""
    since_day = since.date().isoformat()
    until_day = until.date().isoformat() if until else None
    totals: dict[tuple, dict] = defaultdict(lambda: {
        "prompt_tokens": 0, "output_tokens": 0, "audio_seconds": 0,
        "audio_prompt_tokens": 0, "cost": 0.0,
    })

    event_query = select(UsageEvent).where(UsageEvent.owner == owner, UsageEvent.created_at >= since)
    if until is not None:
        event_query = event_query.where(UsageEvent.created_at < until)
    for row in db.scalars(event_query):
        key = (row.kind, row.provider, row.model)
        totals[key]["prompt_tokens"] += row.prompt_tokens or 0
        totals[key]["output_tokens"] += row.output_tokens or 0
        totals[key]["audio_prompt_tokens"] += row.audio_prompt_tokens or 0
        totals[key]["audio_seconds"] += row.audio_seconds or 0
        totals[key]["cost"] += row.cost or 0.0

    daily_query = select(UsageDaily).where(UsageDaily.owner == owner, UsageDaily.date >= since_day)
    if until_day is not None:
        daily_query = daily_query.where(UsageDaily.date < until_day)
    for row in db.scalars(daily_query):
        key = (row.kind, row.provider, row.model)
        totals[key]["prompt_tokens"] += row.prompt_tokens
        totals[key]["output_tokens"] += row.output_tokens
        totals[key]["audio_prompt_tokens"] += row.audio_prompt_tokens
        totals[key]["audio_seconds"] += row.audio_seconds
        totals[key]["cost"] += row.cost

    result = _empty_summary()
    for (kind, provider, model), agg in totals.items():
        result["prompt_tokens"] += agg["prompt_tokens"]
        result["output_tokens"] += agg["output_tokens"]
        result["audio_prompt_tokens"] += agg["audio_prompt_tokens"]
        result["audio_seconds"] += agg["audio_seconds"]
        result["cost"] += agg["cost"]
        result["by_provider"].append({
            "kind": kind, "provider": provider, "model": model, **agg,
        })
    result["by_provider"].sort(key=lambda item: item["cost"], reverse=True)
    return result


def admin_cost_overview(db: Session) -> dict:
    """
    Coût par usager sur des périodes calendaires fixes : les trois derniers
    mois (courant compris), l'année en cours et l'année précédente. Alimente
    le tableau récapitulatif en tête de l'onglet admin « Statistiques ».

    Additionne, comme partout ici, les événements bruts récents ET les lignes
    quotidiennes compactées — une année calendaire couvre largement les deux.
    Le nom des mois est mis en forme côté navigateur (langue de l'interface) ;
    on ne renvoie que des numéros.
    """
    maintenant = utcnow()
    mois = []  # (année, mois), courant d'abord
    annee, m = maintenant.year, maintenant.month
    for _ in range(3):
        mois.append((annee, m))
        annee, m = (annee - 1, 12) if m == 1 else (annee, m - 1)
    annees = [maintenant.year, maintenant.year - 1]

    par_usager: dict[str, dict] = defaultdict(lambda: {
        "month_costs": [0.0, 0.0, 0.0], "year_costs": [0.0, 0.0],
    })

    def cumule(owner: str, cout: Optional[float], annee: int, mois_num: int) -> None:
        if not cout:
            return
        agregat = par_usager[owner]
        for i, (a, mm) in enumerate(mois):
            if (a, mm) == (annee, mois_num):
                agregat["month_costs"][i] += cout
        for i, a in enumerate(annees):
            if a == annee:
                agregat["year_costs"][i] += cout

    for row in db.scalars(select(UsageEvent)):
        cumule(row.owner, row.cost, row.created_at.year, row.created_at.month)
    for row in db.scalars(select(UsageDaily)):
        # date = « YYYY-MM-DD » : le découpage de chaîne évite toute
        # conversion de fuseau.
        cumule(row.owner, row.cost, int(row.date[:4]), int(row.date[5:7]))

    rows = [
        {"owner": owner, **agregat}
        for owner, agregat in par_usager.items()
        if any(agregat["month_costs"]) or any(agregat["year_costs"])
    ]
    rows.sort(key=lambda item: item["month_costs"][0], reverse=True)
    return {
        "months": [{"year": a, "month": m} for a, m in mois],
        "years": annees,
        "rows": rows,
        "currency": "USD",
    }


def admin_breakdown(
    db: Session, date_from: str, date_to: str, owner: Optional[str] = None,
) -> dict:
    """Alimente GET /api/admin/usage : même principe que summary_for_owner
    mais tous usagers confondus (ou un seul si `owner` est fourni), filtré
    sur une plage de dates (chaînes YYYY-MM-DD, bornes incluses)."""
    from_dt = datetime.fromisoformat(date_from)
    to_dt = datetime.fromisoformat(date_to) + timedelta(days=1)  # borne haute incluse

    totals: dict[tuple, dict] = defaultdict(lambda: {
        "prompt_tokens": 0, "output_tokens": 0, "audio_seconds": 0,
        "audio_prompt_tokens": 0, "cost": 0.0,
    })

    event_query = select(UsageEvent).where(UsageEvent.created_at >= from_dt, UsageEvent.created_at < to_dt)
    if owner:
        event_query = event_query.where(UsageEvent.owner == owner)
    for row in db.scalars(event_query):
        key = (row.owner, row.kind, row.provider, row.model)
        totals[key]["prompt_tokens"] += row.prompt_tokens or 0
        totals[key]["output_tokens"] += row.output_tokens or 0
        totals[key]["audio_prompt_tokens"] += row.audio_prompt_tokens or 0
        totals[key]["audio_seconds"] += row.audio_seconds or 0
        totals[key]["cost"] += row.cost or 0.0

    daily_query = select(UsageDaily).where(UsageDaily.date >= date_from, UsageDaily.date <= date_to)
    if owner:
        daily_query = daily_query.where(UsageDaily.owner == owner)
    for row in db.scalars(daily_query):
        key = (row.owner, row.kind, row.provider, row.model)
        totals[key]["prompt_tokens"] += row.prompt_tokens
        totals[key]["output_tokens"] += row.output_tokens
        totals[key]["audio_prompt_tokens"] += row.audio_prompt_tokens
        totals[key]["audio_seconds"] += row.audio_seconds
        totals[key]["cost"] += row.cost

    rows_out = []
    grand_total_cost = 0.0
    for (owner_key, kind, provider, model), agg in totals.items():
        grand_total_cost += agg["cost"]
        rows_out.append({"owner": owner_key, "kind": kind, "provider": provider, "model": model, **agg})
    rows_out.sort(key=lambda item: item["cost"], reverse=True)
    return {"rows": rows_out, "total_cost": grand_total_cost, "currency": "USD"}
