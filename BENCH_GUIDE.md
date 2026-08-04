# Guide de benchmark — location GPU A100

## Résumé de la session 2026-08-03

| ASR | Statut | Temps moy/cas |
|---|---|---|
| whisper-large-v3 | 28/28 ✓ | 58s |
| whisper-large-v3-turbo | 28/28 ✓ | 36s |
| vibevoice-asr | 28/28 ✓ (après 4 correctifs) | ~?s |
| canary-1b*, voxtral*, phi-4 | non testés (ébauches) | — |

| LLM (mode A) | Statut | Temps moy/cas |
|---|---|---|
| mistral-small:24b | 21/21 ✓ | 36s |
| gemma3:27b | 21/21 ✓ | 35s |
| command-r:35b | 21/21 ✓ | 42s |
| qwen3:30b | 14 puis 14 ✓ (après fix thinking) | 75s → ~40s |
| deepseek-r1:70b | timeout (300s) | — |
| llama3.3:70b | 1/1 (276s) | trop lent |

**Verdict** : whisper-turbo + mistral-small est le meilleur rapport qualité/vitesse pour le français québécois clinique (36s ASR + 36s LLM = ~72s par consultation).

---

## Problèmes rencontrés et solutions

| Problème | Solution | Temps perdu |
|---|---|---|
| **SSH tunnel instable** : `-N -L` meurt après 1 requête sur Synology | Remplacer par `-N` + `while sleep 60; do :; done` comme commande distante | ~1h |
| **Docker ne voit pas 127.0.0.1** : `--network host` inefficace sur Synology | Lancer `model_bench.py` directement sur la machine GPU, pas via Docker | ~1h |
| **pip3 lent** : 6 Ko/s vers PyPI | La machine a déjà `torch`+CUDA. Créer un venv `--system-site-packages` ou utiliser le Python système. Installer SEULEMENT `transformers fastapi uvicorn python-multipart openai google-generativeai` + `requirements.txt` | ~30 min |
| **ffmpeg absent** | `apt-get install -y ffmpeg` | 5 min |
| **`return_timestamps`** : Whisper exige `return_timestamps=True` dans le `pipeline()` (pas `generate_kwargs`) pour l'audio >30s | Paramètre direct du pipeline, pas dans `generate_kwargs` | 20 min |
| **VibeVoice `device_map`** : exige `accelerate` | `pip3 install accelerate` | 10 min |
| **VibeVoice OGG** : `soundfile` ne lit pas l'OGG | Convertir en WAV 16 kHz via ffmpeg avant `soundfile.read()` | 15 min |
| **VibeVoice dtype** : mismatch float32/bf16 | Cast des inputs : `{k: v.to(device, dtype=model.dtype)}` | 15 min |
| **VibeVoice gcc/triton** : Triton compile du code CUDA à chaud | `apt-get install build-essential python3-dev` | 10 min |
| **VibeVoice format sortie** : sort du JSON avec timestamps au lieu de texte brut | Parser JSON, extraire `Content`, ou split sur `\n` si préfixé | 20 min |
| **Qwen3 thinking** : génère 6000+ tokens de raisonnement silencieux, rend le modèle 2× plus lent. `extra_body={"enable_thinking": False}` ne fonctionne PAS (ignoré par la couche OpenAI-compat d'Ollama) | Ajouter `/no_think` à la fin du message utilisateur — parsé directement par le template Jinja de Qwen3, traverse toutes les couches API. Appliqué dans `call_llm_text()` de model_bench.py. | 15 min |
| **VRAM zombie** : `llama-server` survivant après timeout, 74 Go bloqués | `kill -9` du processus `llama-server` spécifique | 15 min |
| **Ollama 70B** : trop lent sur A100 seule (>4 min/cas) | Exclure les modèles ≥70B du benchmark | — |

---

## Procédure express pour la prochaine location

### 1. Préparer les fichiers locaux (NAS)
```bash
# Tout est déjà dans tools/gpu/ — vérifier que audio_server.py et bootstrap.sh sont à jour
```

### 2. Copier et bootstrapper la machine GPU
```bash
SSH="ssh -i ~/.ssh/consultai_gpu -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p PORT root@HOST"

# Copier les scripts
scp -i ... tools/gpu/audio_server.py tools/gpu/bootstrap.sh root@HOST:~/

# Installer les dépendances (30-60 min)
$SSH "bash ~/bootstrap.sh"
```

### 3. Installer les dépendances critiques (si bootstrap incomplet)
```bash
$SSH "apt-get install -y -qq ffmpeg build-essential python3-dev zstd rsync"
$SSH "pip3 install transformers fastapi uvicorn python-multipart openai google-generativeai soundfile accelerate"
$SSH "pip3 install -r ~/consultai-bench/requirements.txt"
```

### 4. Préparer les données (depuis le NAS)
```bash
$SSH "mkdir -p ~/consultai-bench"
scp -r -i ... app/ tools/ data/ requirements.txt root@HOST:~/consultai-bench/
$SSH "ln -sf ~/consultai-bench /app && ln -sf ~/consultai-bench/data /data"
```

### 5. Lancer audio_server
```bash
# Démarrer (sans pkill pour éviter de tuer le SSH)
$SSH "nohup python3 ~/consultai-bench/audio_server.py &>~/consultai-bench/logs/srv.log &"
# Vérifier
$SSH "curl -s http://127.0.0.1:8002/healthz"
```

### 6. Lancer le benchmark (depuis la machine GPU, pas Docker !)
```bash
$SSH "cd ~/consultai-bench && nohup python3 tools/model_bench.py \
  --run-id NOM_DU_RUN \
  --cases 7,8,9,10,11,12,13 \
  --mode A \
  --asr whisper=http://127.0.0.1:8002/v1,whisper-large-v3 \
  --asr whisper-turbo=http://127.0.0.1:8002/v1,whisper-large-v3-turbo \
  --asr vibevoice=http://127.0.0.1:8002/v1,vibevoice-asr \
  --llm mistral-small=http://127.0.0.1:11434/v1,mistral-small:24b \
  --llm command-r=http://127.0.0.1:11434/v1,command-r:35b \
  --llm qwen3=http://127.0.0.1:11434/v1,qwen3:30b \
  --llm gemma3=http://127.0.0.1:11434/v1,gemma3:27b \
  > logs/bench.log 2>&1 &"
```

### 7. Surveiller
```bash
$SSH 'wc -l < /data/bench_runs/NOM_DU_RUN/results.jsonl'
$SSH 'ps aux | grep model_bench | grep -v grep | wc -l'
```

### 8. Rapatrier les résultats
```bash
rsync -avz -e "ssh -i ..." root@HOST:/data/bench_runs/NOM_DU_RUN/ ./data/bench_runs/NOM_DU_RUN/
```

---

## Points clés à retenir

1. **Ne pas utiliser Docker** pour le benchmark — la connectivité réseau est un cauchemar. Lancer `model_bench.py` directement sur la GPU.
2. **Ne pas utiliser `-N` seul** pour les tunnels SSH — utiliser `while sleep 60; do :; done` comme commande distante.
3. **Installer ffmpeg, build-essential, python3-dev** avant toute chose.
4. **La machine a déjà torch + CUDA** — utiliser le Python système, pas de venv.
5. **Exclure les modèles ≥70B** — trop lents sur A100 seule.
6. **Qwen3** : toujours passer `extra_body={"enable_thinking": False}`.
7. **Ollama garde les modèles en VRAM** — tuer `llama-server` zombies si besoin.
8. **VibeVoice** : le chargeur fonctionne mais exige 4 prérequis (accelerate, build-essential, python3-dev, conversion WAV).
