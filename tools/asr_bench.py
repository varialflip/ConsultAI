"""
asr_bench.py — Comparaison ponctuelle de trois pipelines ASR -> Gemini 2.5 Flash.

Variantes, pour chaque enregistrement :
  A. AssemblyAI (universal-3-5-pro) -> transcription texte -> Flash
  B. Whisper (point de terminaison personnalisé déjà en service) -> transcription
     texte + audio (silences plafonnés) envoyés ENSEMBLE -> Flash
  C. Audio seul (silences plafonnés), sans transcription -> Flash

Ne modifie rien dans l'application : lit `app.llm` / `app.stt` tels quels et
n'écrit que dans /tmp. Conçu pour tourner DANS le conteneur (`docker exec`),
qui a les identifiants, `ffmpeg` et `/data` déjà en place.

Usage : python3 /app/asr_bench.py
"""
import json
import sqlite3
import sys
import time

sys.path.insert(0, "/app")

from app import llm, runtime_config, stt  # noqa: E402
from google.genai import types  # noqa: E402

DB_PATH = "/data/consultai.db"

CASES = [
    {
        "consultation_id": 8,
        "label": "Georges Simen — Évaluation d'un trouble cognitif",
        "audio_path": "/data/audio/8/c5debb277cee42eca733d1db5a0a3629.webm",
        "template_id": 4,
    },
    {
        "consultation_id": 9,
        "label": "Michel Beaulieu — Suivi médical après hospitalisation",
        "audio_path": "/data/audio/9/75c0bbce741b4522844c58d6f092d359.webm",
        "template_id": 8,
    },
    {
        "consultation_id": 12,
        "label": "Monique Saint-Arnaud — Chute et évaluation cognitive",
        "audio_path": "/data/audio/12/ed6475b9137c4e1cbc66383aba26fc13.webm",
        "template_id": 8,
    },
]

# Tarifs vérifiés dans le brief (2026-07-31).
RATE_ASSEMBLYAI_PRO_PER_SEC = 0.21 / 3600.0
RATE_GEMINI_AUDIO_IN = 1.00 / 1_000_000
RATE_GEMINI_TEXT_IN = 0.30 / 1_000_000
RATE_GEMINI_OUT = 2.50 / 1_000_000


def load_template(template_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT system_instructions, layout_format, language FROM templates WHERE id=?",
        (template_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise SystemExit(f"Gabarit {template_id} introuvable.")
    return {"system_instructions": row[0], "layout_format": row[1], "language": row[2] or "fr"}


def load_existing_transcript(consultation_id: int) -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT raw_transcript FROM consultations WHERE id=?", (consultation_id,)
    ).fetchone()
    conn.close()
    return (row[0] if row else "") or ""


def usage_breakdown(response) -> dict:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {}
    out = {
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
    }
    by_modality = {}
    for detail in getattr(usage, "prompt_tokens_details", None) or []:
        # ``modality`` est un enum du SDK ; ``str()`` renvoie
        # ``MediaModality.AUDIO``, pas ``AUDIO`` — sans le split ci-dessous,
        # la clé ne correspond jamais à celle utilisée par ``cost_gemini``,
        # qui retombe silencieusement sur 0 et facture l'audio au tarif texte.
        modality = str(getattr(detail, "modality", "")).upper().rsplit(".", 1)[-1]
        by_modality[modality] = getattr(detail, "token_count", None)
    out["prompt_by_modality"] = by_modality
    return out


def gemini_call(system_prompt: str, contents, model: str, temperature: float, max_tokens: int):
    client = llm.get_client("gemini")
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=temperature,
        max_output_tokens=max_tokens,
        safety_settings=llm._safety_settings(),
        top_p=0.95,
    )
    t0 = time.monotonic()
    response = client.models.generate_content(model=model, contents=contents, config=config)
    elapsed = time.monotonic() - t0
    return {
        "text": llm._strip_code_fence(getattr(response, "text", None) or ""),
        "usage": usage_breakdown(response),
        "elapsed_seconds": round(elapsed, 2),
    }


def cost_gemini(usage: dict) -> float:
    modality = usage.get("prompt_by_modality") or {}
    audio_tok = modality.get("AUDIO", 0) or 0
    total_prompt = usage.get("prompt_tokens") or 0
    text_tok = max(0, total_prompt - audio_tok)
    out_tok = usage.get("output_tokens") or 0
    return (
        audio_tok * RATE_GEMINI_AUDIO_IN
        + text_tok * RATE_GEMINI_TEXT_IN
        + out_tok * RATE_GEMINI_OUT
    )


def variant_a_assemblyai(case, tpl, model_name, temperature, max_tokens):
    with open(case["audio_path"], "rb") as fh:
        raw = fh.read()
    prev_provider = runtime_config.value("stt_provider")
    assert prev_provider == "assemblyai", f"stt_provider inattendu : {prev_provider}"

    t0 = time.monotonic()
    stt_result = stt.transcribe(raw, content_type="audio/webm")
    stt_elapsed = time.monotonic() - t0

    transcript = stt_result["transcript"]
    billed_seconds = stt_result["duration_seconds"]
    stt_cost = billed_seconds * RATE_ASSEMBLYAI_PRO_PER_SEC

    system_prompt = llm.build_system_prompt(
        tpl["system_instructions"], runtime_config.general_prompt(tpl["language"]), tpl["language"]
    )
    user_prompt = llm.build_user_prompt(transcript, tpl["layout_format"], language=tpl["language"])

    gen = gemini_call(system_prompt, user_prompt, model_name, temperature, max_tokens)
    return {
        "variant": "A_assemblyai_to_flash",
        "transcript": transcript,
        "stt_confidence": stt_result.get("confidence"),
        "stt_billed_seconds": billed_seconds,
        "stt_elapsed_seconds": round(stt_elapsed, 2),
        "stt_cost_usd": round(stt_cost, 5),
        "note_markdown": gen["text"],
        "gemini_usage": gen["usage"],
        "gemini_elapsed_seconds": gen["elapsed_seconds"],
        "gemini_cost_usd": round(cost_gemini(gen["usage"]), 5),
        "total_cost_usd": round(stt_cost + cost_gemini(gen["usage"]), 5),
    }


def variant_b_whisper_plus_audio(case, tpl, model_name, temperature, max_tokens):
    whisper_transcript = load_existing_transcript(case["consultation_id"])
    trimmed = stt.compress_silence(case["audio_path"])
    if trimmed is None:
        raise SystemExit("Retrait des silences impossible (ffmpeg absent ou désactivé).")
    audio_bytes, trimmed_seconds = trimmed

    system_prompt = llm.build_system_prompt(
        tpl["system_instructions"], runtime_config.general_prompt(tpl["language"]), tpl["language"]
    )
    user_prompt = llm.build_user_prompt(whisper_transcript, tpl["layout_format"], language=tpl["language"])
    user_prompt += (
        "\n\nUN EXTRAIT AUDIO DE LA DICTÉE EST JOINT À CETTE REQUÊTE. Utilise-le "
        "pour vérifier ou corriger la transcription ci-dessus en cas de doute "
        "(terme médical incertain, mot mal reconnu), sans jamais inventer ce que "
        "tu n'entends pas clairement."
    )
    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")
    contents = [user_prompt, audio_part]

    gen = gemini_call(system_prompt, contents, model_name, temperature, max_tokens)
    return {
        "variant": "B_whisper_transcript_plus_audio_to_flash",
        "whisper_transcript": whisper_transcript,
        "trimmed_audio_seconds": round(trimmed_seconds, 1),
        "note_markdown": gen["text"],
        "gemini_usage": gen["usage"],
        "gemini_elapsed_seconds": gen["elapsed_seconds"],
        "gemini_cost_usd": round(cost_gemini(gen["usage"]), 5),
        "total_cost_usd": round(cost_gemini(gen["usage"]), 5),
    }


def variant_c_direct_audio(case, tpl, model_name, temperature, max_tokens):
    trimmed = stt.compress_silence(case["audio_path"])
    if trimmed is None:
        raise SystemExit("Retrait des silences impossible (ffmpeg absent ou désactivé).")
    audio_bytes, trimmed_seconds = trimmed

    system_prompt = llm.build_system_prompt(
        tpl["system_instructions"], runtime_config.general_prompt(tpl["language"]), tpl["language"]
    )
    labels = llm._USER_PROMPT_LABELS[tpl["language"]]
    user_prompt = "\n\n".join([
        f"{labels['layout']}\n<<<MISE_EN_PAGE\n{tpl['layout_format'].strip()}\nMISE_EN_PAGE>>>",
        f"{labels['transcript']}\n<<<DICTEE\n[AUCUNE TRANSCRIPTION FOURNIE — transcris et structure "
        "directement à partir du fichier audio ci-joint.]\nDICTEE>>>",
        labels["closing"],
    ])
    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")
    contents = [user_prompt, audio_part]

    gen = gemini_call(system_prompt, contents, model_name, temperature, max_tokens)
    return {
        "variant": "C_direct_audio_to_flash",
        "trimmed_audio_seconds": round(trimmed_seconds, 1),
        "note_markdown": gen["text"],
        "gemini_usage": gen["usage"],
        "gemini_elapsed_seconds": gen["elapsed_seconds"],
        "gemini_cost_usd": round(cost_gemini(gen["usage"]), 5),
        "total_cost_usd": round(cost_gemini(gen["usage"]), 5),
    }


def main():
    model_name = llm.active_model("gemini")
    temperature = llm.active_temperature()
    max_tokens = llm.settings.gemini_max_output_tokens
    only_ids = {int(a) for a in sys.argv[1:]} or None
    cases = [c for c in CASES if only_ids is None or c["consultation_id"] in only_ids]
    results = []
    for case in cases:
        tpl = load_template(case["template_id"])
        print(f"=== {case['label']} (consultation {case['consultation_id']}) ===", file=sys.stderr)

        print("  -> A. AssemblyAI -> Flash", file=sys.stderr)
        a = variant_a_assemblyai(case, tpl, model_name, temperature, max_tokens)

        print("  -> B. Whisper + audio -> Flash", file=sys.stderr)
        b = variant_b_whisper_plus_audio(case, tpl, model_name, temperature, max_tokens)

        print("  -> C. Audio seul -> Flash", file=sys.stderr)
        c = variant_c_direct_audio(case, tpl, model_name, temperature, max_tokens)

        results.append({
            "consultation_id": case["consultation_id"],
            "label": case["label"],
            "results": [a, b, c],
        })

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
