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
      org.opencontainers.image.description="Dictée de consultations gériatriques (fr-CA) — STT Google + Gemini" \
      org.opencontainers.image.source="https://github.com/local/consultai"

# ffmpeg : indispensable pour normaliser l'audio du navigateur.
#   - Chrome/Android produit du audio/webm;codecs=opus
#   - Safari/iOS (iPad du médecin) produit du audio/mp4 (AAC) que Google STT
#     NE SAIT PAS lire nativement.
# On transcode donc systématiquement vers OGG/Opus mono 48 kHz (voir app/stt.py).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Utilisateur non-root. UID/GID 1000 correspond au premier utilisateur créé
# sur un NAS Synology ; ajustez avec --build-arg si vos dossiers partagés
# appartiennent à un autre UID (voir `id votre_user` en SSH sur le NAS).
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g "${APP_GID}" appuser 2>/dev/null || true \
    && useradd -m -u "${APP_UID}" -g "${APP_GID}" -s /usr/sbin/nologin appuser 2>/dev/null || true

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app/ /app/app/

# /data = volume persistant (base SQLite). Créé et possédé par appuser pour
# que SQLite puisse y écrire son fichier -wal/-shm.
RUN mkdir -p /data && chown -R "${APP_UID}:${APP_GID}" /data /app

USER appuser

EXPOSE 8000

# Healthcheck sans curl (absent de l'image slim) : on utilise urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else sys.exit(1)"

# Un seul worker : SQLite + état applicatif simple. Pour plus de charge,
# augmentez --workers ET passez la base sur PostgreSQL.
#
# --no-proxy-headers est VOLONTAIRE ET IMPORTANT : sans cela, uvicorn remplace
# request.client.host par la valeur de X-Forwarded-For, qui est falsifiable.
# La vérification TRUSTED_PROXIES (app/auth.py) doit voir l'IP réelle du pair
# TCP, sinon elle ne protège plus rien.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
