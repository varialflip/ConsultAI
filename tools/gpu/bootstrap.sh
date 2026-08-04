#!/usr/bin/env bash
# bootstrap.sh — mise en route de la machine A100 louée.
#
# Idempotent : peut être relancé sans dupliquer quoi que ce soit. Pensé pour
# tourner UNE fois après rsync sur la machine louée, jamais sur le NAS.
#
# Ordre volontaire : lance TOUS les téléchargements en arrière-plan D'ABORD
# (voir le plan : ~320 Go, 45-80 min sur un lien à 1 Gbit/s — c'est le vrai
# goulot, pas l'inférence), puis installe le reste pendant que ça tire.
set -euo pipefail

BENCH_DIR="${BENCH_DIR:-$HOME/consultai-bench}"
VENV_DIR="$BENCH_DIR/venv"
LOG_DIR="$BENCH_DIR/logs"
mkdir -p "$BENCH_DIR" "$LOG_DIR"
cd "$BENCH_DIR"

echo "== 1/4 — Ollama (texte, port 11434) : install + téléchargements en fond =="
if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
fi
# `ollama serve` doit tourner pour que `pull` fonctionne.
if ! pgrep -f "ollama serve" >/dev/null 2>&1; then
    nohup ollama serve > "$LOG_DIR/ollama.log" 2>&1 &
    sleep 3
fi

# Modèles texte (mode A de model_bench.py).
# Français québécois clinique : le gabarit et les consignes apportent le
# vocabulaire du réseau (RAMQ, CISSS, CHSLD, CLSC, UCDG…), aucun modèle ne
# le connaît par défaut. Ce qui les départage, c'est le suivi de consignes
# longues et structurées en français.
OLLAMA_MODELS=(
    "mistral-small:24b"   # Mistral Small 3, meilleur rapport qualité/taille, agentic, Apache 2.0
    "command-r:35b"        # Cohere, 10 langues dont FR, RAG natif
    "qwen3:30b"            # MoE 30B, 256K ctx — utile si les consignes sont très longues
    "gemma3:27b"           # 140+ langues, 128K ctx
    "deepseek-r1:70b"      # Raisonnement explicite, base Llama 3.3 (FR), 128K ctx
    "llama3.3:70b"         # FR officiel, 128K ctx, valeur de référence
)
for model in "${OLLAMA_MODELS[@]}"; do
    echo "  pull en fond : $model"
    nohup ollama pull "$model" >> "$LOG_DIR/ollama_pull_${model//[:\/]/_}.log" 2>&1 &
done

echo "== 2/4 — venv Python (ASR + audio-LLM, port 8002) =="
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip --quiet
pip install --quiet \
    "transformers>=4.46" accelerate torch soundfile librosa \
    fastapi "uvicorn[standard]" python-multipart \
    huggingface_hub qwen-omni-utils

echo "== 3/4 — poids HF en fond (transformers les mettra en cache au 1er   =="
echo "==       chargement, mais on préfère préchauffer avant de tester)     =="
HF_MODELS=(
    "openai/whisper-large-v3"
    "openai/whisper-large-v3-turbo"
    "microsoft/VibeVoice-ASR-HF"
    "nvidia/canary-1b-v2"
    "nvidia/canary-1b-flash"
    "mistralai/Voxtral-Mini-3B-2507"
    "mistralai/Voxtral-Small-24B-2507"
    "Qwen/Qwen3-Omni-30B-A3B-Instruct"
    "microsoft/Phi-4-multimodal-instruct"
)
for model in "${HF_MODELS[@]}"; do
    echo "  download en fond : $model"
    nohup "$VENV_DIR/bin/python3" -c "
from huggingface_hub import snapshot_download
snapshot_download('$model')
" >> "$LOG_DIR/hf_download_${model//[:\/]/_}.log" 2>&1 &
done

echo "== 4/4 — audio_server.py (port 8002) =="
echo "Ne démarre PAS automatiquement ici : les modèles se chargent à la"
echo "demande (voir audio_server.py) et le premier appel pour chaque modèle"
echo "attendra la fin de son téléchargement s'il n'est pas encore arrivé."
echo ""
echo "Pour démarrer une fois les téléchargements avancés :"
echo "  source $VENV_DIR/bin/activate"
echo "  python3 $BENCH_DIR/audio_server.py"
echo ""
echo "Suivre les téléchargements : tail -f $LOG_DIR/*.log"
echo "Suivre Ollama :               ollama list"
echo ""
echo "Bootstrap lancé. Les téléchargements continuent en arrière-plan."
