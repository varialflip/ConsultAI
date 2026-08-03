"""
audio_server.py — serveur OpenAI-compatible pour l'ASR et les LLM audio.

Tourne sur la machine louée (port 8002), PAS dans le conteneur ConsultAI.
Sert deux routes :
  POST /v1/audio/transcriptions   — contrat de app.stt._transcribe_custom,
                                     PLUS un champ ``prompt`` (mots-clés) que
                                     l'app n'envoie jamais elle-même (voir le
                                     plan) mais que tools/model_bench.py, lui,
                                     envoie directement.
  POST /v1/chat/completions       — sous-ensemble du format OpenAI, avec
                                     acceptation de blocs ``input_audio``
                                     (convention gpt-4o-audio) dans le
                                     contenu du message utilisateur — c'est
                                     ce que tools/model_bench.py envoie pour
                                     les modes B/C.

AVERTISSEMENT HONNÊTE : la couche FastAPI + le registre ci-dessous sont la
partie qu'on peut garantir correcte (c'est le contrat déjà vérifié dans le
code de l'appli). Les chargeurs par modèle, eux, sont un premier jet écrit
SANS accès à un GPU pour les tester — Whisper est un usage standard de
``transformers`` et devrait marcher tel quel ; VibeVoice-ASR et Qwen3-Omni
suivent les exemples publiés de leurs fiches HF au moment d'écrire ceci
(2026-08-02) ; Canary (NeMo, pas ``transformers``), Voxtral et
Phi-4-multimodal sont des ébauches à vérifier/corriger UNE FOIS sur la
machine, contre les vraies traces d'erreur plutôt que par déduction.

Chargement paresseux, un modèle reste en mémoire une fois chargé — pas
d'éviction : c'est un banc d'essai, pas un service ; si la VRAM sature,
relancer le processus (voir bootstrap.sh) plutôt que compliquer ce fichier
avec une logique de purge qu'on ne pourra pas non plus tester à l'avance.

Lancer :
  source ~/consultai-bench/venv/bin/activate
  python3 audio_server.py            # écoute 0.0.0.0:8002
"""
from __future__ import annotations

import base64
import io
import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("audio_server")

app = FastAPI(title="ConsultAI eval — audio server")

_load_lock = threading.Lock()
_backends: Dict[str, "ASRBackend"] = {}


# ===========================================================================
# Interface commune
# ===========================================================================
class ASRBackend:
    """Un backend transcrit toujours. ``supports_chat`` dit s'il peut aussi
    servir /v1/chat/completions avec de l'audio en entrée (modes B/C)."""

    supports_chat = False

    def transcribe(self, audio_path: str, language: str, prompt: str) -> str:
        raise NotImplementedError

    def chat(self, system: str, user_text: str, audio_paths: List[str],
              max_tokens: int, temperature: float) -> str:
        raise NotImplementedError(f"{type(self).__name__} ne fait pas de chat audio")


class WhisperBackend(ASRBackend):
    """Usage standard de ``transformers`` — c'est la partie la plus sûre de
    ce fichier, Whisper via ``pipeline()`` est un chemin très rodé."""

    def __init__(self, model_id: str):
        import torch
        from transformers import pipeline

        device = 0 if torch.cuda.is_available() else -1
        dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        self.pipe = pipeline(
            "automatic-speech-recognition", model=model_id,
            torch_dtype=dtype, device=device,
        )

    def transcribe(self, audio_path: str, language: str, prompt: str) -> str:
        generate_kwargs: dict = {}
        if language:
            generate_kwargs["language"] = language
        if prompt:
            prompt_ids = self.pipe.tokenizer.encode(prompt, add_special_tokens=False)
            generate_kwargs["prompt_ids"] = prompt_ids
        result = self.pipe(audio_path, return_timestamps=True, generate_kwargs=generate_kwargs if generate_kwargs else None)
        return str(result.get("text") or "").strip()


class VibeVoiceAsrBackend(ASRBackend):
    """Suit l'exemple de la fiche HF (microsoft/VibeVoice-ASR-HF, vérifié
    par recherche le 2026-08-02) : ``AutoProcessor`` +
    ``VibeVoiceAsrForConditionalGeneration``, mots-clés via
    ``apply_transcription_request(audio, prompt=...)``. NON TESTÉ sur GPU
    réel — la forme exacte de l'appel peut avoir bougé depuis."""

    def __init__(self, model_id: str = "microsoft/VibeVoice-ASR-HF"):
        from transformers import AutoProcessor, VibeVoiceAsrForConditionalGeneration

        self.processor = AutoProcessor.from_pretrained(model_id)
        self.model = VibeVoiceAsrForConditionalGeneration.from_pretrained(
            model_id, device_map="auto",
        )

    def transcribe(self, audio_path: str, language: str, prompt: str) -> str:
        import subprocess, tempfile, soundfile as sf, torch, json

        wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        wav.close()
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ar", "24000", "-ac", "1", wav.name],
            capture_output=True, check=True,
        )
        audio_data, _sr = sf.read(wav.name)
        Path(wav.name).unlink(missing_ok=True)
        audio_data = audio_data.astype("float32")

        inputs = self.processor.apply_transcription_request(
            audio_data, prompt="Transcris cet audio en français, mot pour mot, sans rien ajouter.",
        )
        inputs = {k: v.to(self.model.device, dtype=self.model.dtype if v.dtype == torch.float32 else v.dtype) for k, v in inputs.items()}
        output_ids = self.model.generate(**inputs, max_new_tokens=4096)
        trimmed = output_ids[:, inputs["input_ids"].shape[1]:]
        raw = self.processor.batch_decode(
            trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False,
        )[0].strip()

        # VibeVoice peut sortir du JSON avec un préfixe "assistant\n".
        raw = raw.split("\n", 1)[-1] if raw.startswith(("system\n", "user\n", "assistant\n")) else raw
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                raw = " ".join(item.get("Content", "") for item in data)
            elif isinstance(data, dict):
                raw = data.get("text", raw)
        except (json.JSONDecodeError, TypeError):
            pass
        return raw.strip()


class Qwen3OmniBackend(ASRBackend):
    """Suit l'exemple de la fiche HF (Qwen/Qwen3-Omni-30B-A3B-Instruct) :
    ``Qwen3OmniMoeForConditionalGeneration`` + ``Qwen3OmniMoeProcessor`` +
    ``qwen_omni_utils.process_mm_info``. Sert AUSSI /v1/chat/completions —
    c'est un des deux candidats audio-LLM du plan. NON TESTÉ sur GPU réel."""

    supports_chat = True

    def __init__(self, model_id: str = "Qwen/Qwen3-Omni-30B-A3B-Instruct"):
        import torch
        from transformers import Qwen3OmniMoeForConditionalGeneration, Qwen3OmniMoeProcessor

        self.model = Qwen3OmniMoeForConditionalGeneration.from_pretrained(
            model_id, torch_dtype=torch.bfloat16, device_map="auto",
        )
        self.processor = Qwen3OmniMoeProcessor.from_pretrained(model_id)

    def _generate(self, conversation: list, max_tokens: int, temperature: float) -> str:
        from qwen_omni_utils import process_mm_info

        text_in = self.processor.apply_chat_template(
            conversation, tokenize=False, add_generation_prompt=True,
        )
        # ``use_audio_in_video=False`` : on n'envoie jamais de blocs vidéo,
        # seulement de l'audio pur — argument positionnel désormais requis
        # par qwen_omni_utils (absent des versions plus anciennes du paquet).
        audios, images, videos = process_mm_info(conversation, use_audio_in_video=False)
        # ``dtype=self.model.dtype`` : la tour audio (bf16) rejette les
        # ``input_features`` en float32 renvoyés par défaut par le processor
        # (« Input type (float) and bias type (c10::BFloat16) »). ``.to()``
        # avec un dtype ne caste que les tenseurs flottants (BatchFeature
        # laisse ``input_ids``/``attention_mask`` intacts) — idiome standard
        # des exemples HF pour les modèles multimodaux.
        inputs = self.processor(
            text=text_in, audio=audios, images=images, videos=videos,
            return_tensors="pt", padding=True,
        ).to(self.model.device, dtype=self.model.dtype)
        # ``return_audio=False`` : sans ça, generate() renvoie un tuple
        # (texte, audio) — ce backend ne sert que du texte. ``thinker_``
        # (pas ``max_new_tokens`` nu) : generate() ne route la longueur que
        # via ce préfixe, un ``max_new_tokens`` sans préfixe est absorbé
        # silencieusement par la valeur par défaut (1024) de thinker_kwargs.
        output_ids = self.model.generate(
            **inputs, return_audio=False, thinker_max_new_tokens=max_tokens,
            temperature=temperature if temperature > 0 else None,
            do_sample=temperature > 0,
        )
        # Le modèle renvoie prompt + génération concaténés — ne garder que
        # ce qui suit le prompt, comme pour tout modèle causal.
        trimmed = output_ids[:, inputs["input_ids"].shape[1]:]
        text = self.processor.batch_decode(
            trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False,
        )
        return str(text[0] if text else "").strip()

    def transcribe(self, audio_path: str, language: str, prompt: str) -> str:
        instruction = "Transcris cet audio mot pour mot, sans rien ajouter."
        if prompt:
            instruction += f" Vocabulaire attendu : {prompt}."
        conversation = [{
            "role": "user",
            "content": [{"type": "audio", "audio": audio_path}, {"type": "text", "text": instruction}],
        }]
        return self._generate(conversation, max_tokens=4096, temperature=0.0)

    def chat(self, system: str, user_text: str, audio_paths: List[str],
              max_tokens: int, temperature: float) -> str:
        content = [{"type": "text", "text": user_text}]
        for path in audio_paths:
            content.append({"type": "audio", "audio": path})
        conversation = [
            {"role": "system", "content": [{"type": "text", "text": system}]},
            {"role": "user", "content": content},
        ]
        return self._generate(conversation, max_tokens, temperature)


class NotYetImplementedBackend(ASRBackend):
    """Canary (NeMo, pas transformers), Voxtral et Phi-4-multimodal ont
    chacun leur propre API et n'ont pas été vérifiés faute de GPU pour les
    tester. Échoue explicitement plutôt que de prétendre marcher — un combo
    qui échoue clairement dans model_bench.py est réparable ; un combo qui
    renvoie un texte silencieusement faux ne l'est pas."""

    def __init__(self, model_id: str, hint: str):
        self.model_id = model_id
        self.hint = hint

    def transcribe(self, audio_path: str, language: str, prompt: str) -> str:
        raise HTTPException(
            status_code=501,
            detail=f"{self.model_id} : chargeur non vérifié sur GPU. {self.hint}",
        )


# Nom de modèle (tel qu'envoyé par model_bench.py --asr/--audio-llm) -> chargeur.
BACKEND_LOADERS = {
    "whisper-large-v3": lambda: WhisperBackend("openai/whisper-large-v3"),
    "whisper-large-v3-turbo": lambda: WhisperBackend("openai/whisper-large-v3-turbo"),
    "vibevoice-asr": VibeVoiceAsrBackend,
    "Qwen3-Omni-30B-A3B-Instruct": Qwen3OmniBackend,
    "canary-1b-v2": lambda: NotYetImplementedBackend(
        "canary-1b-v2", "NeMo (nemo_toolkit.asr.models.ASRModel), pas transformers.",
    ),
    "canary-1b-flash": lambda: NotYetImplementedBackend(
        "canary-1b-flash", "NeMo (nemo_toolkit.asr.models.ASRModel), pas transformers.",
    ),
    "voxtral-mini-3b": lambda: NotYetImplementedBackend(
        "voxtral-mini-3b", "Voir mistralai/Voxtral-Mini-3B-2507 sur HF pour la classe exacte.",
    ),
    "Voxtral-Small-24B": lambda: NotYetImplementedBackend(
        "Voxtral-Small-24B", "Voir mistralai/Voxtral-Small-24B-2507 sur HF pour la classe exacte.",
    ),
    "Phi-4-multimodal": lambda: NotYetImplementedBackend(
        "Phi-4-multimodal", "Nécessite probablement trust_remote_code=True — à vérifier.",
    ),
}


def get_backend(model_name: str) -> ASRBackend:
    with _load_lock:
        if model_name not in _backends:
            loader = BACKEND_LOADERS.get(model_name)
            if loader is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Modèle inconnu : {model_name!r}. Connus : {sorted(BACKEND_LOADERS)}",
                )
            logger.info("Chargement de %s (premier appel)...", model_name)
            t0 = time.monotonic()
            _backends[model_name] = loader()
            logger.info("  -> chargé en %.1f s", time.monotonic() - t0)
        return _backends[model_name]


def _audio_bytes_to_tempfile(data: bytes, cleanup_list: list | None = None) -> str:
    """``transformers``/NeMo veulent un chemin de fichier, pas des octets en
    mémoire dans la plupart des cas — un fichier temporaire évite de
    dupliquer la logique de décodage par backend."""
    fh = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    fh.write(data)
    fh.close()
    path = fh.name
    if cleanup_list is not None:
        cleanup_list.append(path)
    return path


# ===========================================================================
# POST /v1/audio/transcriptions — contrat de app.stt._transcribe_custom
# ===========================================================================
@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(...),
    language: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
):
    backend = get_backend(model)
    raw = await file.read()
    temp_files: list = []
    path = _audio_bytes_to_tempfile(raw, cleanup_list=temp_files)
    try:
        text = backend.transcribe(path, language or "", prompt or "")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Échec de transcription (%s)", model)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        for f in temp_files:
            try:
                Path(f).unlink(missing_ok=True)
            except OSError:
                pass
    return JSONResponse({"text": text})


# ===========================================================================
# POST /v1/chat/completions — sous-ensemble OpenAI, blocs input_audio inclus
# ===========================================================================
class ChatRequest(BaseModel):
    model: str
    messages: list
    max_completion_tokens: int = 4096
    temperature: float = 0.2


def _extract_system_user_audio(messages: list):
    system = ""
    user_text_parts: List[str] = []
    audio_paths: List[str] = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content")
            system = content if isinstance(content, str) else str(content)
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            user_text_parts.append(content)
            continue
        for block in content or []:
            btype = block.get("type")
            if btype == "text":
                user_text_parts.append(block.get("text", ""))
            elif btype == "input_audio":
                raw = base64.b64decode(block["input_audio"]["data"])
                audio_paths.append(_audio_bytes_to_tempfile(raw))
    return system, "\n\n".join(user_text_parts), audio_paths


@app.post("/v1/chat/completions")
async def chat_completions(payload: ChatRequest):
    backend = get_backend(payload.model)
    if not backend.supports_chat:
        raise HTTPException(
            status_code=400,
            detail=f"{payload.model} ne prend pas d'audio en entrée de conversation.",
        )
    system, user_text, audio_paths = _extract_system_user_audio(payload.messages)
    try:
        text = backend.chat(
            system, user_text, audio_paths, payload.max_completion_tokens, payload.temperature,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Échec de chat audio (%s)", payload.model)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        for f in audio_paths:
            try:
                Path(f).unlink(missing_ok=True)
            except OSError:
                pass

    # Forme OpenAI minimale — model_bench.py ne lit que ceci.
    return JSONResponse({
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": None, "completion_tokens": None},
    })


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "loaded": sorted(_backends)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
