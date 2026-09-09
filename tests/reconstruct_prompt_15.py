#!/usr/bin/env python3
"""Reconstruit le prompt complet de GÉNÉRATION DE NOTE pour consultation #15.

Reproduit EXACTEMENT le chemin de ``api_generate`` → ``_generate_and_publish`` →
``generate_note_stream`` → ``complete_stream`` → ``_stream_openai_like`` en
réutilisant les fonctions réelles de l'application (mêmes consignes système,
mêmes blocs utilisateur, mêmes hints, même confiance). Sortie : un fichier
``tests/med_regions/prompt_note_consult15.txt`` avec, en clair :

  1. la requête HTTP (URL + curl avec l'en-tête Authorization),
  2. le corps JSON complet,
  3. la consigne système,
  4. le prompt utilisateur,
  5. les métadonnées (layout, contexte, hints, confiance).

Usage (dans le conteneur) :
    python3 /tmp/reconstruct_prompt_15.py
"""
from __future__ import annotations

import json
import sqlite3
import sys

sys.path.insert(0, "/app")

from app import geriatric_terms, i18n, llm, med_grounding, runtime_config  # noqa: E402

DB_PATH = "/data/consultai.db"
CID = 15
OUT = "/tmp/prompt_note_consult15.txt"

# Reproduire les constantes de budget OpenRouter (voir llm.py)
from app.llm import (  # noqa: E402
    _OPENROUTER_MAX_TOKENS_DEFAUT,
    _openrouter_max_tokens,
    _openrouter_reasoning_effort,
    _reasoning_param,
    active_temperature,
)


def load_consultation(conn, cid):
    conn.row_factory = sqlite3.Row
    c = conn.execute(
        "SELECT template_id, template_name, raw_transcript, transcript_conf, "
        "inline_fixed_json, med_grounding_json, med_region_json, "
        "reason, requester, accompanied_by, consultation_date, stt_language, "
        "audio_seconds "
        "FROM consultations WHERE id=?", (cid,)
    ).fetchone()
    tpl = conn.execute(
        "SELECT system_instructions, layout_format, language "
        "FROM templates WHERE id=?", (c["template_id"],)
    ).fetchone()
    return c, tpl


def build_context_lines(c):
    date_value = (c["consultation_date"] or "").strip() or "2026-09-07"
    lines = [f"Date de la consultation : {date_value}"]
    if (c["reason"] or "").strip():
        lines.append(f"Raison de consultation : {(c['reason'] or '').strip()}")
    if (c["requester"] or "").strip():
        lines.append(f"Demande de : {(c['requester'] or '').strip()}")
    if (c["accompanied_by"] or "").strip():
        lines.append(f"Accompagné de : {(c['accompanied_by'] or '').strip()}")
    return lines


def main():
    conn = sqlite3.connect(DB_PATH)
    c, tpl = load_consultation(conn, CID)

    langue = i18n.normalize(tpl["language"] or runtime_config.value("app_language"))
    transcript = (c["raw_transcript"] or "").strip()
    conf_map = json.loads(c["transcript_conf"]) if c["transcript_conf"] else {}

    # Consigne système (assemblée par api_generate → system_override)
    system_prompt = llm.build_system_prompt(
        tpl["system_instructions"],
        runtime_config.general_prompt(langue),
        langue,
    )

    # inline_fixed (corrections inline déjà appliquées : médicaments + gériatrique)
    inline_fixed = set()
    if c["inline_fixed_json"]:
        try:
            inline_fixed = set(json.loads(c["inline_fixed_json"]))
        except Exception:
            inline_fixed = set()

    # Confiance mot-à-mot (bloc <CONFIANCE_MOTS>), APRÈS inline_fixed
    confiance_mots = None
    if transcript and conf_map:
        try:
            doutes = med_grounding.doutes_pour_texte(
                transcript, conf_map, ignores=inline_fixed,
            )
            confiance_mots = med_grounding.grouper_doutes_pour_prompt(
                doutes, transcript,
            )
        except Exception as exc:
            confiance_mots = None

    # Hints médicaments : items persistés du grounding, sans ceux déjà corrigés
    med_hints = []
    if c["med_grounding_json"]:
        try:
            parsed = json.loads(c["med_grounding_json"])
            med_hints = [
                item for item in parsed
                if med_grounding.norm_phon(item.get("base") or item.get("name") or "")
                   not in inline_fixed
            ]
        except Exception:
            med_hints = []

    # Prompt utilisateur (ORDER des blocs de build_user_prompt)
    context_lines = build_context_lines(c)
    user_prompt = llm.build_user_prompt(
        transcript,
        tpl["layout_format"],
        context_lines,
        "",  # extra_instructions vide (payload)
        langue,
        confiance=confiance_mots,
        med_hints=med_hints,
        geriatric_hints=geriatric_terms.pertinent_hints(
            transcript, langue, conf_map=conf_map,
        ),
    )

    # --- Requête HTTP OpenRouter (POST https://openrouter.ai/api/v1/chat/completions)
    provider = llm.active_provider()
    model = llm.active_model()
    temperature = active_temperature()
    max_tokens = _openrouter_max_tokens()
    effort = _openrouter_reasoning_effort()
    reasoning = _reasoning_param(effort) if effort else None

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    body = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if reasoning:
        body["reasoning"] = reasoning

    # --- Écrire le fichier
    out = []
    out.append("=" * 78)
    out.append("CONSULTAI — PROMPT DE GÉNÉRATION DE NOTE")
    out.append(f"Consultation #{CID} — {c['template_name']}")
    out.append(f"Modèle : {provider} / {model}")
    out.append(f"Langue du document : {langue}")
    out.append(f"Transcript : {len(transcript)} caractères")
    out.append("=" * 78)
    out.append("")
    out.append("# REQUÊTE HTTP (OpenRouter /chat/completions)")
    out.append("URI : POST https://openrouter.ai/api/v1/chat/completions")
    out.append("En-têtes :")
    out.append("  Authorization: Bearer <OPENROUTER_API_KEY>")
    out.append("  Content-Type: application/json")
    out.append("")
    out.append("# CORPOR JSON ENVOYÉ")
    out.append(json.dumps(body, ensure_ascii=False, indent=2))
    out.append("")
    out.append("# ÉQUIVALENT curl")
    curl = (
        "curl -X POST 'https://openrouter.ai/api/v1/chat/completions' "
        "-H 'Authorization: Bearer $OPENROUTER_API_KEY' "
        "-H 'Content-Type: application/json' "
        f"-d '{json.dumps(body, ensure_ascii=False)}'"
    )
    out.append(curl)
    out.append("")
    out.append("=" * 78)
    out.append("# CONSIGNE SYSTÈME")
    out.append("=" * 78)
    out.append(system_prompt)
    out.append("")
    out.append("=" * 78)
    out.append("# PROMPT UTILISATEUR")
    out.append("=" * 78)
    out.append(user_prompt)
    out.append("")
    out.append("=" * 78)
    out.append("# MÉTADONNÉES")
    out.append(f"temperature = {temperature}")
    out.append(f"max_completion_tokens = {max_tokens}")
    out.append(f"reasoning = {json.dumps(reasoning, ensure_ascii=False)}")
    out.append(f"langue = {langue}")
    out.append("")
    out.append("## Contexte (5 _build_context_lines)")
    for line in context_lines:
        out.append(f"  - {line}")
    out.append("")
    out.append(f"## inline_fixed ({len(inline_fixed)})")
    for f in sorted(inline_fixed):
        out.append(f"  - {f}")
    out.append("")
    out.append(f"## confiance_mots ({len(confiance_mots or [])})")
    if confiance_mots:
        for d in confiance_mots:
            out.append(f"  - {d.get('mot')} → {d.get('conf')}")
    out.append("")
    out.append(f"## med_hints ({len(med_hints)})")
    for h in med_hints:
        out.append(f"  - [{h.get('source','det')}] {h.get('name')} base={h.get('base')} "
                   f"poso={h.get('posology')} conf={h.get('conf')}")
    out.append("")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out))

    print(f"Écris  : {OUT}")
    print(f"System : {len(system_prompt)} chars, User : {len(user_prompt)} chars")


if __name__ == "__main__":
    main()