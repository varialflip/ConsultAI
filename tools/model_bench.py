"""
model_bench.py — N modèles ASR x M modèles LLM x K dictées, trois architectures.

Complément d'``asr_bench.py`` (qui reste la référence Gemini) : celui-ci parle
DIRECTEMENT aux points de terminaison OpenAI-compatibles d'un serveur loué
(Ollama pour le texte, un adaptateur maison pour l'audio) plutôt que par
``app.llm``/``app.stt`` — il ne touche donc JAMAIS ``runtime_config`` en
base, ce qui serait intenable pour une grille de cette taille.

TROIS ARCHITECTURES (mêmes lettres que le benchmark Gemini de la veille) :
  A. ASR -> texte -> LLM texte                    (forme de la production)
  B. ASR -> texte + audio -> LLM audio             (gagnant du banc Gemini)
  C. audio seul -> LLM audio                       (pas d'étape ASR)

Chaque appel écrit son résultat sur disque IMMÉDIATEMENT (JSONL vidé après
chaque ligne, fichiers texte à part) : une location horaire qui tombe à
n'importe quel moment ne doit coûter que l'appel en cours, jamais toute la
grille. Relancer la même commande REPREND là où ça s'est arrêté.

Exemples :
  # Étape 1 — un seul cas (13, le plus court), tout le monde passe une fois.
  python3 /app/model_bench.py \\
      --run-id ecran \\
      --cases 13 \\
      --asr whisper=http://192.168.96.1:8402/v1,whisper-large-v3 \\
      --asr vibevoice=http://192.168.96.1:8402/v1,vibevoice-asr \\
      --audio-llm qwen3omni=http://192.168.96.1:8402/v1,Qwen3-Omni-30B-A3B-Instruct \\
      --llm gemma4=http://192.168.96.1:8401/v1,gemma4-26b-a4b-grounded \\
      --llm mistral-small4=http://192.168.96.1:8401/v1,mistral-small-3.1-24b-instruct

  # Étape 2 — les survivants, sur les 7 cas.
  python3 /app/model_bench.py --run-id approfondi --cases 7,8,9,10,11,12,13 ...
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, "/app")

from app import llm, runtime_config, stt  # noqa: E402

DB_PATH = "/data/consultai.db"
AUDIO_ROOT = Path("/data/audio")
RUNS_ROOT = Path("/data/bench_runs")

# Vérifié directement en base le 2026-08-02 (voir la session : SELECT sur
# templates/consultations/recordings). Plusieurs fichiers = plusieurs passes
# de dictée sur le même brouillon, DANS L'ORDRE DE CRÉATION — même logique
# que ``retranscribe_consultation`` (app/main.py) : transcrire chaque
# fichier séparément, recoller le TEXTE avec « \n\n ». On ne peut pas
# concaténer les octets, deux conteneurs webm indépendants ne se recollent
# pas au niveau binaire.
#
# Cas 7 : les deux imports en « .bin » (durée 0, mime video/webm) sont
# exclus délibérément — probablement des artefacts d'import avortés, pas de
# la parole. Choix pris sans certitude absolue ; à revoir si le cas 7 donne
# des résultats bizarres.
CASES: Dict[int, dict] = {
    7: {
        "template_id": 4,
        "files": [
            "7/0c919c94e90f4f9693d1d8f60f255dd1.webm",
            "7/6079e8268c2e4713b9fdd2dcf0fe17f0.webm",
        ],
    },
    8: {"template_id": 4, "files": ["8/c5debb277cee42eca733d1db5a0a3629.webm"]},
    9: {"template_id": 8, "files": ["9/75c0bbce741b4522844c58d6f092d359.webm"]},
    10: {
        "template_id": 8,
        "files": [
            "10/622c04e6211f42ae8b6c0c814769da71.webm",
            "10/c7486a739dda4b83b014562b3e520277.webm",
            "10/35ea9886bbaf4f3bad7ed11f6a3547a0.webm",
            "10/1f2759e4dd3c447ebbacda973f0d476b.webm",
            "10/dfe0642c925046d1aa083aa4f9e194c7.webm",
            "10/084c932b64e341b680cfc52da11bc62f.webm",
        ],
    },
    11: {"template_id": 4, "files": ["11/4515cd476b9e4ff1b3b15828681a48ea.webm"]},
    12: {"template_id": 8, "files": ["12/ed6475b9137c4e1cbc66383aba26fc13.webm"]},
    13: {"template_id": 8, "files": ["13/34774ae7184f40719f3af92ebd54a587.webm"]},
}

# Cas 13 = le plus court (119 s) : c'est l'écran de la phase 1 (voir le plan).
DEFAULT_CASES = [13]


# ===========================================================================
# Modèles demandés en ligne de commande — "nom=url,modele"
# ===========================================================================
class ModelRef:
    __slots__ = ("name", "base_url", "model")

    def __init__(self, spec: str):
        try:
            name, rest = spec.split("=", 1)
            base_url, model = rest.rsplit(",", 1)
        except ValueError:
            raise SystemExit(
                f"Format attendu « nom=url,modele », reçu : {spec!r}"
            )
        self.name = name.strip()
        self.base_url = base_url.strip().rstrip("/")
        self.model = model.strip()

    def __repr__(self):
        return f"{self.name}({self.model}@{self.base_url})"


def parse_model_refs(values: Optional[List[str]]) -> List[ModelRef]:
    return [ModelRef(v) for v in (values or [])]


# ===========================================================================
# Gabarits / cas — lus dans la base, jamais écrits
# ===========================================================================
def load_template(template_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT system_instructions, layout_format, language, phrase_hints "
        "FROM templates WHERE id=?",
        (template_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise SystemExit(f"Gabarit {template_id} introuvable.")
    return {
        "system_instructions": row[0],
        "layout_format": row[1],
        "language": row[2] or "fr",
        "phrase_hints": row[3] or "",
    }


def load_case_audio(case_id: int) -> List[Tuple[bytes, float]]:
    """Chaque fichier du cas, silences plafonnés (``stt.compress_silence``,
    la même fonction que la production). Retourne (octets ogg/opus, durée)."""
    out = []
    for rel in CASES[case_id]["files"]:
        path = str(AUDIO_ROOT / rel)
        trimmed = stt.compress_silence(path)
        if trimmed is None:
            with open(path, "rb") as fh:
                content = fh.read()
            out.append((content, 0.0))
        else:
            out.append(trimmed)
    return out


# ===========================================================================
# Appels HTTP directs — jamais via runtime_config
# ===========================================================================
def _multipart(fields: dict, file_field: str, filename: str, content: bytes) -> Tuple[bytes, str]:
    """Même forme EXACTE que ``app.stt._multipart_body_fields`` — c'est le
    contrat que ``_transcribe_custom`` envoie en production, on le reproduit
    ici à la main plutôt que d'importer une fonction privée d'un autre
    module."""
    boundary = f"----modelbench{uuid.uuid4().hex}"
    parts = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def call_transcribe(asr: ModelRef, audio_bytes: bytes, language: str, prompt_hint: str) -> dict:
    """POST {base}/audio/transcriptions — contrat de ``_transcribe_custom``,
    PLUS un champ ``prompt`` (mots-clés) que l'app n'envoie jamais elle-même
    (voir le plan : trou confirmé dans ``_transcribe_custom``). Le serveur du
    banc peut choisir de l'utiliser ou de l'ignorer."""
    fields = {"model": asr.model}
    if language:
        fields["language"] = language
    if prompt_hint:
        fields["prompt"] = prompt_hint
    body, ctype = _multipart(fields, "file", "dictee.ogg", audio_bytes)
    req = urllib.request.Request(
        f"{asr.base_url}/audio/transcriptions", data=body,
        headers={"Content-Type": ctype}, method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"ASR {asr} a refusé ({exc.code}) : {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"ASR {asr} injoignable : {exc}") from exc
    elapsed = time.monotonic() - t0
    return {"text": str(data.get("text") or "").strip(), "elapsed_seconds": round(elapsed, 2)}


def _openai_chat(llm_ref: ModelRef, messages: list, max_tokens: int, temperature: float) -> dict:
    """Client OpenAI construit à la main — jamais ``llm.get_client()``, qui
    lit ``runtime_config``. Même paramètre que ``_complete_openai`` :
    ``max_completion_tokens``, pas ``max_tokens`` (voir le plan, gotcha n°2)."""
    import openai

    client = openai.OpenAI(api_key="dummy-key-unused", base_url=llm_ref.base_url, timeout=300)
    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=llm_ref.model,
        messages=messages,
        max_completion_tokens=max_tokens,
        temperature=temperature,
        # ``options.num_ctx`` : sans ça, Ollama alloue le contexte natif du
        # modèle (131072 pour command-r, 262144 pour qwen3) pour CHAQUE
        # requête, ce qui force un déchargement partiel CPU et fait chuter
        # command-r à ~1.25 tok/s. Ignoré sans erreur par les backends non-
        # Ollama (Pydantic ignore les champs inconnus par défaut).
        extra_body={"enable_thinking": False, "options": {"num_ctx": 16384}},
    )
    elapsed = time.monotonic() - t0
    choice = (response.choices or [None])[0]
    text = getattr(getattr(choice, "message", None), "content", "") or ""
    usage = getattr(response, "usage", None)
    return {
        "text": text.strip(),
        "elapsed_seconds": round(elapsed, 2),
        "finish_reason": str(getattr(choice, "finish_reason", "") or ""),
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
        } if usage else {},
    }


def call_llm_text(llm_ref: ModelRef, system: str, user: str, max_tokens: int, temperature: float) -> dict:
    # ``/no_think`` est parsé par le template Jinja de Qwen3, tous les autres
    # modèles l'ignorent. Contrairement à ``enable_thinking: False`` dans
    # ``extra_body``, ceci traverse la couche OpenAI-compat d'Ollama.
    # Voir la session « splendid-fluttering-owl » (sous-agent a5bdee69e77286926).
    user_payload = user + "\n/no_think"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_payload}]
    return _openai_chat(llm_ref, messages, max_tokens, temperature)


def call_llm_audio(
    llm_ref: ModelRef, system: str, user: str, audio_clips: List[Tuple[bytes, float]],
    max_tokens: int, temperature: float,
) -> dict:
    """Convention ``input_audio`` d'OpenAI (celle des modèles gpt-4o-audio) :
    un standard reconnu plutôt qu'un format maison, pour que le serveur du
    banc (tools/gpu/audio_server.py, écrit par nous aussi) n'ait qu'à suivre
    une spec déjà documentée ailleurs. Plusieurs extraits (cas multi-fichiers
    comme 7 et 10) partent comme autant de blocs dans le même message."""
    content = [{"type": "text", "text": user}]
    for audio_bytes, _duration in audio_clips:
        content.append({
            "type": "input_audio",
            "input_audio": {"data": base64.b64encode(audio_bytes).decode("ascii"), "format": "ogg"},
        })
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    return _openai_chat(llm_ref, messages, max_tokens, temperature)


# ===========================================================================
# Artefacts sur disque — voir le plan, section « Artefacts »
# ===========================================================================
class RunDir:
    def __init__(self, run_id: str):
        self.root = RUNS_ROOT / run_id
        self.transcripts = self.root / "transcripts"
        self.notes = self.root / "notes"
        self.raw = self.root / "raw"
        self.compare = self.root / "compare"
        for d in (self.transcripts, self.notes, self.raw, self.compare):
            d.mkdir(parents=True, exist_ok=True)
        self.results_path = self.root / "results.jsonl"

    def manifest(self, extra: dict) -> None:
        path = self.root / "manifest.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        data.update(extra)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def done_combo_ids(self) -> set:
        """Un combo n'est « fait » que s'il a RÉUSSI. Un enregistrement en
        erreur (point de terminaison injoignable, timeout…) ne doit jamais
        bloquer une reprise — sinon la reprise, censée protéger contre une
        location interrompue, se contenterait de rejouer l'échec pour
        toujours. Dernière ligne vue par combo_id qui décide, pour qu'un
        succès après une reprise efface l'échec précédent du même combo."""
        if not self.results_path.exists():
            return set()
        latest: Dict[str, dict] = {}
        with open(self.results_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    latest[rec["combo_id"]] = rec
                except (json.JSONDecodeError, KeyError):
                    continue
        return {cid for cid, rec in latest.items() if "error" not in rec}

    def append_result(self, record: dict) -> None:
        with open(self.results_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()

    def write(self, subdir: Path, stem: str, suffix: str, content: str) -> None:
        (subdir / f"{stem}{suffix}").write_text(content, encoding="utf-8")

    def regenerate_compare(self, case_id: int) -> None:
        """Reconstruit le comparatif du cas à partir de TOUT results.jsonl —
        peu coûteux (quelques Ko à quelques Mo), et garantit qu'un fichier
        interrompu au milieu d'une écriture n'est jamais celui qu'on lit.
        Dédoublonné par combo_id (dernière ligne gagne, même règle que
        ``done_combo_ids``) : un échec suivi d'une reprise réussie ne doit
        montrer que la reprise, pas les deux l'une sous l'autre."""
        if not self.results_path.exists():
            return
        latest: Dict[str, dict] = {}
        with open(self.results_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("case_id") == case_id:
                    latest[rec["combo_id"]] = rec
        rows = list(latest.values())
        if not rows:
            return
        lines = [f"# Cas {case_id}\n"]
        for rec in rows:
            lines.append(f"## {rec['combo_id']}")
            lines.append(f"*mode {rec['mode']} · {rec.get('elapsed_seconds', '?')} s*\n")
            lines.append(rec.get("note_markdown") or rec.get("transcript") or "*(vide)*")
            lines.append("")
        (self.compare / f"{case_id}.md").write_text("\n".join(lines), encoding="utf-8")


# ===========================================================================
# Combinaisons à exécuter
# ===========================================================================
def build_combos(
    case_ids: List[int], modes: List[str],
    asr_refs: List[ModelRef], llm_refs: List[ModelRef], audio_llm_refs: List[ModelRef],
) -> List[dict]:
    combos = []
    for case_id in case_ids:
        if "A" in modes:
            for asr in asr_refs:
                for llm_ref in llm_refs:
                    combos.append({
                        "case_id": case_id, "mode": "A", "asr": asr, "llm": llm_ref,
                        "combo_id": f"{case_id}__A__{asr.name}__{llm_ref.name}",
                    })
        if "B" in modes:
            for asr in asr_refs:
                for audio_llm in audio_llm_refs:
                    combos.append({
                        "case_id": case_id, "mode": "B", "asr": asr, "audio_llm": audio_llm,
                        "combo_id": f"{case_id}__B__{asr.name}__{audio_llm.name}",
                    })
        if "C" in modes:
            for audio_llm in audio_llm_refs:
                combos.append({
                    "case_id": case_id, "mode": "C", "audio_llm": audio_llm,
                    "combo_id": f"{case_id}__C__{audio_llm.name}",
                })
    return combos


def run_combo(
    combo: dict, run: RunDir, max_tokens: int, temperature: float,
    transcript_cache: Optional[Dict[Tuple[int, str], str]] = None,
    drug_grounding: str = "",
) -> dict:
    case_id = combo["case_id"]
    tpl = load_template(CASES[case_id]["template_id"])
    audio_clips = load_case_audio(case_id)
    total_seconds = sum(d for _, d in audio_clips)

    record = {"combo_id": combo["combo_id"], "case_id": case_id, "mode": combo["mode"]}

    if combo["mode"] in ("A", "B"):
        asr: ModelRef = combo["asr"]
        cache_key = (case_id, asr.name)
        if transcript_cache is not None and cache_key in transcript_cache:
            transcript = transcript_cache[cache_key]
            stt_elapsed = 0.0  # cache hit — not re-measured
        else:
            pieces = []
            stt_elapsed = 0.0
            for audio_bytes, _duration in audio_clips:
                piece = call_transcribe(asr, audio_bytes, tpl["language"], tpl["phrase_hints"])
                pieces.append(piece["text"])
                stt_elapsed += piece["elapsed_seconds"]
            transcript = "\n\n".join(p for p in pieces if p)
            run.write(run.transcripts, f"{case_id}__{asr.name}", ".txt", transcript)
            if transcript_cache is not None:
                transcript_cache[cache_key] = transcript
        record["asr"] = asr.name
        record["transcript"] = transcript
        record["stt_elapsed_seconds"] = round(stt_elapsed, 2)

    system = llm.build_system_prompt(
        tpl["system_instructions"], runtime_config.general_prompt(tpl["language"]), tpl["language"],
    )

    if combo["mode"] == "A":
        llm_ref: ModelRef = combo["llm"]
        user = llm.build_user_prompt(
            record["transcript"], tpl["layout_format"],
            extra_instructions=drug_grounding, language=tpl["language"],
        )
        gen = call_llm_text(llm_ref, system, user, max_tokens, temperature)
        record["llm"] = llm_ref.name

    elif combo["mode"] == "B":
        audio_llm: ModelRef = combo["audio_llm"]
        user = llm.build_user_prompt(
            record["transcript"], tpl["layout_format"],
            extra_instructions=drug_grounding, language=tpl["language"],
        )
        user += (
            "\n\nUN EXTRAIT AUDIO DE LA DICTÉE EST JOINT À CETTE REQUÊTE. Utilise-le "
            "pour vérifier ou corriger la transcription ci-dessus en cas de doute "
            "(terme médical incertain, mot mal reconnu), sans jamais inventer ce que "
            "tu n'entends pas clairement."
        )
        gen = call_llm_audio(audio_llm, system, user, audio_clips, max_tokens, temperature)
        record["audio_llm"] = audio_llm.name

    else:  # C
        audio_llm = combo["audio_llm"]
        labels = llm._USER_PROMPT_LABELS[tpl["language"]]
        parts = [f"{labels['layout']}\n<<<MISE_EN_PAGE\n{tpl['layout_format'].strip()}\nMISE_EN_PAGE>>>"]
        if drug_grounding.strip():
            parts.append(f"{labels['extra']}\n<<<CONSIGNES\n{drug_grounding.strip()}\nCONSIGNES>>>")
        parts.append(
            f"{labels['transcript']}\n<<<DICTEE\n[AUCUNE TRANSCRIPTION FOURNIE — transcris et "
            "structure directement à partir du fichier audio ci-joint.]\nDICTEE>>>"
        )
        parts.append(labels["closing"])
        user = "\n\n".join(parts)
        gen = call_llm_audio(audio_llm, system, user, audio_clips, max_tokens, temperature)
        record["audio_llm"] = audio_llm.name

    record["note_markdown"] = gen["text"]
    record["elapsed_seconds"] = round(
        record.get("stt_elapsed_seconds", 0.0) + gen["elapsed_seconds"], 2
    )
    record["llm_usage"] = gen.get("usage") or {}
    record["finish_reason"] = gen.get("finish_reason", "")
    record["audio_seconds"] = round(total_seconds, 1)

    run.write(run.notes, combo["combo_id"], ".md", record["note_markdown"])
    run.write(run.raw, combo["combo_id"], ".json", json.dumps(record, ensure_ascii=False, indent=2))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True, help="dossier sous data/bench_runs/")
    parser.add_argument("--cases", default=",".join(str(c) for c in DEFAULT_CASES),
                         help="ids séparés par des virgules, ex. 7,8,9,10,11,12,13")
    parser.add_argument("--mode", action="append", choices=["A", "B", "C"], dest="modes",
                         help="répétable ; défaut : les trois")
    parser.add_argument("--asr", action="append", dest="asr", default=[],
                         help="nom=url,modele — répétable")
    parser.add_argument("--llm", action="append", dest="llm_", default=[],
                         help="nom=url,modele (texte, mode A) — répétable")
    parser.add_argument("--audio-llm", action="append", dest="audio_llm", default=[],
                         help="nom=url,modele (audio, modes B/C) — répétable")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--ground-drugs", type=str, default=None,
                         help="Chemin vers un CSV brand,generic,class — injecté comme "
                              "CONSIGNES PONCTUELLES (extra_instructions) sur chaque combo "
                              "mode A de ce run. Absent par défaut : ne pas confondre les runs "
                              "groundés avec la comparaison de référence sans grounding.")
    args = parser.parse_args()

    modes = args.modes or ["A", "B", "C"]
    case_ids = [int(c) for c in args.cases.split(",") if c.strip()]
    for cid in case_ids:
        if cid not in CASES:
            raise SystemExit(f"Cas inconnu : {cid} (connus : {sorted(CASES)})")

    asr_refs = parse_model_refs(args.asr)
    llm_refs = parse_model_refs(args.llm_)
    audio_llm_refs = parse_model_refs(args.audio_llm)

    if "A" in modes and (not asr_refs or not llm_refs):
        print("Mode A demandé sans --asr et --llm : ignoré.", file=sys.stderr)
        modes = [m for m in modes if m != "A"]
    if "B" in modes and (not asr_refs or not audio_llm_refs):
        print("Mode B demandé sans --asr et --audio-llm : ignoré.", file=sys.stderr)
        modes = [m for m in modes if m != "B"]
    if "C" in modes and not audio_llm_refs:
        print("Mode C demandé sans --audio-llm : ignoré.", file=sys.stderr)
        modes = [m for m in modes if m != "C"]
    if not modes:
        raise SystemExit("Rien à exécuter : vérifiez --asr/--llm/--audio-llm selon les modes demandés.")

    run = RunDir(args.run_id)
    run.manifest({
        "run_id": args.run_id, "cases": case_ids, "modes": modes,
        "asr": [repr(r) for r in asr_refs], "llm": [repr(r) for r in llm_refs],
        "audio_llm": [repr(r) for r in audio_llm_refs],
        "ground_drugs": args.ground_drugs or None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })

    drug_grounding = ""
    if args.ground_drugs:
        import csv
        with open(args.ground_drugs, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        lines = [f"{r['brand']} ({r['generic']}) {r['dosage']} — {r['class']}" for r in rows]
        drug_grounding = (
            "Voici une liste de référence de médicaments courants au Canada "
            "(nom commercial, nom générique, dosages usuels, classe). Utilise-la pour "
            "corriger un nom de médicament mal transcrit par la reconnaissance vocale, ou "
            "une dose mal transcrite, s'il y correspond clairement — n'invente jamais un "
            "médicament ou une dose absents à la fois de cette liste et de la dictée, et ne "
            "force pas une correspondance douteuse :\n"
            + "\n".join(f"- {l}" for l in lines)
        )
        print(f"Grounding médicaments actif : {len(rows)} entrées depuis {args.ground_drugs}", file=sys.stderr)

    combos = build_combos(case_ids, modes, asr_refs, llm_refs, audio_llm_refs)
    done = run.done_combo_ids()
    todo = [c for c in combos if c["combo_id"] not in done]
    print(f"{len(combos)} combinaison(s), {len(done)} déjà faites, {len(todo)} à faire.", file=sys.stderr)

    transcript_cache: Dict[Tuple[int, str], str] = {}
    touched_cases = set()
    for i, combo in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {combo['combo_id']}", file=sys.stderr)
        try:
            record = run_combo(
                combo, run, args.max_tokens, args.temperature,
                transcript_cache=transcript_cache, drug_grounding=drug_grounding,
            )
        except Exception as exc:  # noqa: BLE001 — une combinaison ratée ne doit pas tuer la grille
            print(f"    ÉCHEC : {exc}", file=sys.stderr)
            record = {**combo, "error": str(exc)}
            record.pop("asr", None); record.pop("llm", None); record.pop("audio_llm", None)
        run.append_result(record)
        touched_cases.add(combo["case_id"])

    for case_id in touched_cases:
        run.regenerate_compare(case_id)

    print(f"Terminé. Résultats : {run.root}", file=sys.stderr)


if __name__ == "__main__":
    main()
