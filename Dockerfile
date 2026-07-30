# =============================================================================
# ConsultAI - Image Docker multi-étages
# =============================================================================
# Compatible amd64 (Synology x86) ET arm64 (Synology DS/RS ARM, Raspberry Pi).
# L'étape « builder » compile les roues Python dans un venv jetable ; l'étape
# finale ne copie que le venv, ce qui évite d'embarquer gcc & co dans l'image
# de production (~200 Mo économisés).
# =============================================================================

# -----------------------------------------------------------------------------
# ÉTAPE 1 — Construction des dépendances
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Outils de compilation requis par certaines roues (grpcio, cryptography...)
# lorsqu'aucune roue précompilée n'existe pour l'architecture cible (cas ARM).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Environnement virtuel isolé : facile à copier tel quel dans l'image finale.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt


# -----------------------------------------------------------------------------
# ÉTAPE 2 — Image d'exécution
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="ConsultAI" \
      org.opencontainers.image.description="Dictée de consultations cliniques (fr / en) — STT + LLM au choix" \
      org.opencontainers.image.source="https://github.com/varialflip/ConsultAI"

# ffmpeg : indispensable pour normaliser l'audio du navigateur.
#   - Chrome/Android produit du audio/webm;codecs=opus
#   - Safari/iOS (iPad du médecin) produit du audio/mp4 (AAC) que Google STT
#     NE SAIT PAS lire nativement.
# On transcode donc systématiquement vers OGG/Opus mono 48 kHz (voir app/stt.py).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Utilisateur non-root, UID/GID fixes (1000:1000) — c'est une image publiée
# une fois et réutilisée partout, donc plus question de la reconstruire avec
# un UID sur mesure. Un déploiement qui a besoin d'un UID différent (ACL
# Synology, par ex. — voir docker-compose.yml : "user:") le fait au lancement
# du conteneur, pas à sa construction : Docker autorise n'importe quel UID à
# l'exécution, même absent de /etc/passwd.
RUN groupadd -g 1000 appuser \
    && useradd -m -u 1000 -g 1000 -s /usr/sbin/nologin appuser

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # HOME=/tmp (monde-inscriptible) plutôt que /home/appuser : un conteneur
    # lancé avec un UID différent de 1000 (voir "user:" dans
    # docker-compose.yml) n'a pas accès à /home/appuser, propriété du build.
    # Rien dans l'application n'écrit sous HOME, mais une bibliothèque tierce
    # pourrait vouloir y déposer un cache — /tmp le lui permet sans exiger un
    # UID précis.
    HOME=/tmp

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app/ /app/app/

# /data = volume persistant (base SQLite), monté par-dessus au lancement : ce
# chown ne sert qu'en dehors de tout montage (ex. test de l'image seule).
# L'écriture réelle dépend de la propriété du dossier hôte ./data — voir
# APP_UID/APP_GID dans .env.example.
RUN mkdir -p /data && chown -R appuser:appuser /data /app

USER appuser

EXPOSE 8000

# Healthcheck sans curl (absent de l'image slim) : on utilise urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else sys.exit(1)"

# Un seul worker : SQLite + état applicatif simple. Pour plus de charge,
# augmentez --workers ET passez la base sur PostgreSQL.
#
# --no-proxy-headers est VOLONTAIRE : sans cela, uvicorn remplacerait
# request.client.host par la valeur d'X-Forwarded-For, falsifiable par
# n'importe quel appelant. Ce n'est plus un contrôle de sécurité (l'app
# authentifie par OIDC, pas par IP source — voir app/auth.py) mais l'IP
# journalisée doit rester celle du pair TCP réel, pas une valeur que
# l'appelant peut choisir.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
