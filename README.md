# ConsultAI — Guide de déploiement

Application web auto-hébergée de **dictée de consultations cliniques**. Le
médecin dicte, l'application transcrit, un modèle de langage met en forme selon
un gabarit, le médecin relit et exporte.

* Interface **française ou anglaise**, au choix de chaque usager.
* **Dix** services de reconnaissance vocale et **huit** fournisseurs de
  modèle de langage, commutables depuis le panneau d'administration sans
  reconstruire l'image. Plusieurs modèles (Gemini, Qwen Omni, point de
  terminaison personnalisé, OpenRouter) peuvent aussi recevoir **l'audio
  directement**, sans transcription séparée.
* Authentification **OpenID Connect**, assurée par l'application elle-même.
* Aucune spécialité imposée : ce qui est propre à une pratique vit dans les
  gabarits et dans la consigne générale.

L'image est publiée sur GitHub Container Registry : **deux fichiers suffisent
pour déployer**, `docker-compose.yml` et `.env` — pas besoin de cloner le
dépôt ni de construire quoi que ce soit soi-même.

Conçue à l'origine pour un NAS Synology, mais rien n'y est spécifique hormis
un point signalé comme tel.

> ### 🤖 Projet entièrement *vibe-codé* avec Claude Code
>
> L'intégralité de ce dépôt — application, Dockerfile, gabarits livrés et la
> présente documentation — a été écrite par [Claude Code](https://claude.com/claude-code)
> à partir d'échanges en langage naturel : aucune ligne n'a été tapée à la main.
>
> À prendre en compte dans votre analyse de risque : le code est ouvert et
> lisible, mais il n'a pas été relu ligne à ligne par un développeur tiers.
> Auditez-le avant tout usage clinique réel, et voir le § 11 pour ce que
> l'application fait des renseignements de santé.

---

## Sommaire

1. [Prérequis](#1-prérequis) · 2. [Installation](#2-installation) ·
3. [Configuration](#3-configuration) · 4. [Le proxy inverse](#4-le-proxy-inverse) ·
5. [Premier démarrage](#5-premier-démarrage) ·
6. [Vérification](#6-vérification-du-déploiement) · 7. [Exploitation](#7-exploitation) ·
8. [Mise à jour](#8-mise-à-jour) · 9. [Sauvegarde](#9-sauvegarde) ·
10. [Dépannage](#10-dépannage) · 11. [Confidentialité](#11-confidentialité) ·
12. [Redéployer ailleurs](#12-redéployer-ailleurs)

---

## 1. Prérequis

| Élément | Détail |
|---|---|
| Docker + Compose v2 | Fournis par DSM sur Synology ; sinon [docs.docker.com](https://docs.docker.com/engine/install/) |
| Un fournisseur OIDC | Pocket ID, Authentik, Keycloak, Entra ID… |
| Un proxy inverse en HTTPS | Obligatoire : le micro et l'installation PWA l'exigent |
| Une clé de modèle de langage | Gemini, Anthropic, OpenAI, Cohere, Mistral, Qwen Omni ou OpenRouter — au moins une (peut attendre le premier démarrage, voir §2) |
| Une clé de service vocal | Google, Deepgram, AssemblyAI, Soniox, Cohere, Mistral, OpenAI (Whisper) ou OpenRouter — au moins une (idem) |
| ~1 Go de RAM | Limite fixée dans `docker-compose.yml` |

L'image contient `ffmpeg` : rien à installer sur l'hôte.

---

## 2. Installation

Aucun `git clone` n'est nécessaire : téléchargez seulement ces deux fichiers
dans un dossier (ex. `consultai/`) et travaillez depuis là :

* [`docker-compose.yml`](docker-compose.yml)
* [`.env.example`](.env.example)

```bash
mkdir consultai && cd consultai
curl -LO https://raw.githubusercontent.com/varialflip/ConsultAI/main/docker-compose.yml
curl -LO https://raw.githubusercontent.com/varialflip/ConsultAI/main/.env.example
cp .env.example .env
```

Puis ouvrez `.env` dans un éditeur : il est rangé en 6 parties numérotées, la
première (« OBLIGATOIRE ») est la seule à remplir avant de démarrer. Chaque
variable y est commentée — pas besoin de revenir ici pour la plupart des
réglages.

### 2.1 ⚠️ Synology — l'UID du processus

**À régler avant tout démarrage, sur Synology seulement.** Les dossiers
partagés Synology portent des ACL qui refusent l'écriture aux UID inconnus
**même quand les permissions affichent 777**. Avec une mauvaise valeur,
l'application s'arrête au démarrage sur
`sqlite3.OperationalError: unable to open database file`.

```bash
id votre_utilisateur        # ex. uid=1026(fred) gid=100(users)
```

```ini
APP_UID=1026
APP_GID=100
```

Sur un hôte Docker générique, ignorez cette étape : les défauts conviennent.

### 2.2 Clé Google — seulement si vous utilisez Google

Nécessaire pour Google Speech-to-Text et pour Gemini en mode Vertex AI. Rôles du
compte de service : `Cloud Speech Client`, plus `Vertex AI User` en mode Vertex,
plus `Storage Object Admin` si vous configurez `STT_GCS_BUCKET`.

```bash
mkdir -p secrets data
cp /chemin/vers/cle.json secrets/gcp-sa.json
chmod 600 secrets/gcp-sa.json
```

Les autres fournisseurs n'utilisent qu'une clé d'API, saisie dans le panneau
d'administration ou dans `.env` — voir la note « FLEXIBILITÉ SUR LES CLÉS
D'API » en tête de la partie 1 de `.env.example`. Aucune clé n'est obligatoire
pour démarrer : sans elles, l'application démarre quand même et vous les
ajoutez plus tard depuis le panneau, une fois connecté.

### 2.3 Démarrage

```bash
docker compose up -d
docker compose logs -f consultai
```

`docker compose` télécharge l'image publiée (`ghcr.io/varialflip/consultai`),
il n'y a rien à construire. Le conteneur écoute sur `BIND_ADDRESS:BIND_PORT`
(défaut `127.0.0.1:8787`).

---

## 3. Configuration

Tout passe par `.env`, documenté variable par variable dans `.env.example`.
Cette section ne retient que ce qui bloque un déploiement.

### 3.1 Réseau

```ini
BIND_ADDRESS=192.168.20.50     # une adresse joignable par le proxy
BIND_PORT=8787
BASE_URL=https://consultai.exemple.com
```

`127.0.0.1` ne convient que si le proxy tourne sur le même hôte ; sinon il
renvoie « Bad Gateway ».

### 3.2 Authentification

```ini
OIDC_PROVIDER_URL=https://login.exemple.com
OIDC_CLIENT_ID=…
OIDC_CLIENT_SECRET=…
OIDC_REDIRECT_URI=https://consultai.exemple.com/auth/callback
OIDC_SCOPES=openid,profile,email,groups
SESSION_SECRET=…               # openssl rand -base64 48
SSO_DISPLAY_NAME=Mon SSO       # nom affiché sur les pages d'erreur
ALLOW_SIGNUP=false
```

Chez votre fournisseur, créez un client **confidentiel** et déclarez l'adresse
de retour à l'identique.

> **`SESSION_SECRET` n'est pas optionnelle.** Sans elle, une clé aléatoire est
> tirée à chaque démarrage : tout le monde est déconnecté à chaque
> `docker compose up`. L'application le signale au démarrage.

### 3.3 Langue

```ini
APP_LANGUAGE=fr                # défaut de l'installation
STT_LANGUAGE_CODE=             # LAISSER VIDE
```

`APP_LANGUAGE` ne sert qu'aux usagers qui n'ont pas encore choisi : chacun règle
sa langue depuis le menu de la pastille d'identité.

> **Laissez `STT_LANGUAGE_CODE` vide.** Une valeur y **force** la langue du
> service vocal et survit au changement de langue — souhaitable seulement pour
> épingler un dialecte précis.

### 3.4 Services

Renseignez au moins une clé de chaque famille. Le choix du service se fait
ensuite dans le panneau, sans redémarrage.

```ini
# Vocal — au moins un
GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp-sa.json
DEEPGRAM_API_KEY=
ASSEMBLYAI_API_KEY=
SONIOX_API_KEY=
COHERE_API_KEY=
MISTRAL_API_KEY=
MODULATE_API_KEY=
# OpenRouter : une seule clé pour la note ET la transcription STT (voir plus bas)

# Modèle de langage — au moins un
GEMINI_API_KEY=                # ou GOOGLE_CLOUD_PROJECT pour Vertex AI
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
QWEN_OMNI_API_KEY=
QWEN_OMNI_BASE_URL=            # documentation DashScope
OPENROUTER_API_KEY=            # openrouter.ai → Keys ; note + STT
# Cohere et Mistral : pas de variable propre, COHERE_API_KEY / MISTRAL_API_KEY
# ci-dessus servent aux deux usages
# Budget de raisonnement Cohere (thinking.token_budget) à la mise en forme —
# 0 = défaut du modèle ; non envoyé à la relecture des métadonnées
COHERE_LLM_THINKING_BUDGET=1024
```

> **Cohere, Mistral et OpenAI n'ont qu'une clé pour deux usages** :
> `COHERE_API_KEY`, `MISTRAL_API_KEY` et `OPENAI_API_KEY` alimentent chacun le
> service vocal *et* le modèle de langage (OpenAI Whisper d'un côté, les
> modèles gpt-4o de l'autre). Le réglage n'existe qu'une fois en base : le
> champ est répété sous les deux onglets concernés (Dictée et Note), et une
> saisie sous l'un se reflète aussitôt sous l'autre.
>
> **OpenRouter suit le même principe** (`OPENROUTER_API_KEY` — une seule clé) :
> le fournisseur « OpenRouter » propose à la fois un **modèle de langage**
> (ex. `thinkingmachines/inkling-small`, multimodal et open-weight) et une
> **reconnaissance vocale** fondée sur ce même modèle. Contrairement au point
> de terminaison personnalisé, OpenRouter ne sert pas inkling-small derrière
> `/audio/transcriptions` : la transcription passe par un appel « chat » avec
> une part audio. L'audio de la consultation part donc **vers le cloud
> OpenRouter** (voir § 11.2 pour la résidence des données) — c'est un choix de
> fournisseur, pas le trajet local.
>
> Un **point de terminaison personnalisé** (ex. Whisper auto-hébergé,
> compatible API OpenAI) se configure uniquement depuis le panneau, sans
> variable de clé propre : `custom_stt_base_url`, `custom_stt_model` et une clé
> éventuelle. L'audio y reste sur votre machine (§ 7.2, § 11.2). Il accepte
> aussi un **modèle de repli** (`custom_stt_fallback_model` et, au besoin,
> `custom_stt_fallback_base_url`) : en cas d'erreur HTTP 5xx de l'endpoint
> principal, la transcription est retentée une fois avec le modèle de repli.
> Un **découpage en tranches** (`custom_stt_chunk_seconds`, vide par défaut) —
> uniquement si une durée y est renseignée — découpe l'audio au-delà de cette
> durée et envoie chaque tranche au modèle principal, en coupant de préférence
> dans un silence : même un endpoint qui plafonne en longueur d'audio par
> passe (ex. un Parakeet/ONNX plafonnant autour de 6-7 min) garde le modèle
> principal sur toute la dictée. Sans découpage (défaut), le **seuil de
> durée** (`custom_stt_max_seconds`) reste disponible pour envoyer directement
> au modèle de repli les dictées trop longues. Le **retrait des silences** (plafonnement des
> pauses, bascule globale `stt_trim_silence`) s'applique désormais **à tous les
> fournisseurs, y compris cet endpoint** et l'audio joint au modèle de langage ;
> coupez la bascule s'il dégrade un modèle local multilingue.

Quand le fournisseur de **génération de note** ne reçoit pas l'audio, la
confiance mot-à-mot du STT (`words[].confidence`, si le service la fournit) est
transmise au modèle : seuls les mots entendus avec incertitude sont signalés,
et le modèle y concentre son effort de correction — en marquant « à confirmer »
les doutes persistants dans « Corrections et éléments à valider » — tout en
préservant fidèlement les mots sûrs (anti sur-correction). Les noms de
médicaments déjà corrigés par le grounding déterministe sont exclus du
signalement.

> **⚡ Préparation de l'audio pendant la dictée.** L'audio joint au modèle de
> langage (plafonnement des silences + encodage) coûte ~0,9× le temps réel —
> plusieurs secondes autrefois payées AU clic « Mettre en forme ». Désormais,
> pendant la dictée, le serveur construit régulièrement un **point de
> contrôle** (passe ffmpeg bornée sur l'audio déjà reçu) ; à « Terminer », il
> ne reste qu'à préparer la queue (seek jamais tardif, retranché à
> l'échantillon près) et concaténer sans réencoder (~1 s) — le résultat
> rejoint un **cache par enregistrement** (`AUDIO_CACHE_DIR`,
> `/data/audio-cache`). Au clic, l'artefact prêt est servi tel quel ; à
> défaut, une préparation complète part en tâche de fond dès la conclusion
> (la génération attend borné), puis retombe en dernier recours sur la voie
> historique au clic. Le cache ne contient que du dérivé régénérable : hors
> sauvegarde, purgé avec l'enregistrement, ignoré si les réglages du
> plafonnement changent. Les jetons servis depuis le **cache de préfixe**
> implicite de Gemini (`cachedContentTokenCount`) sont journalisés à chaque
> appel ; le message utilisateur place désormais la **mise en page** (stable
> par gabarit) AVANT le contexte variable de la consultation, pour que le
> préfixe partagé couvre consigne système + gabarit + structure exigée et
> non la seule consigne système. La **mise en forme place l'audio en tête**
> du message, et l'audit « Validation » tourne sous la MÊME consigne système
> (assemblée une fois, injectée aux deux passes) : le préfixe
> [consigne système + audio] est donc lu depuis le cache implicite au second
> passage — l'audio, plus grosse part du prompt, n'est re-facturé que sur la
> fin du message. Les jetons servis depuis le cache sont journalisés et
> persistés (`usage_events.cached_tokens`), et le coût applique la remise
> (tarif `token_input_cached_1m`, ~90 %).

> **🔎 « Validation » — audit factuel de la note.** Bascule à côté du bouton
> « Mettre en forme » (préférence par usager, désactivée par défaut). Quand
> elle est active, chaque génération est suivie d'un second appel — **quel que
> soit le fournisseur LLM** — qui compare la note à la référence disponible et
> renvoie deux listes : ce qui fut dicté mais manque à la note, ce que la note
> affirme sans avoir été dicté. Volontairement permissif : seuls les écarts
> certains sont signalés, les reformulations médicales sont acceptées. Le
> résultat est diffusé en
> direct (SSE `verification_chunk`, JSON brut re-rendu au fil de l'eau, puis
> `verification_result`) dans un second onglet du panneau de transcription —
> roue sur le titre à partir de la FIN de la génération (jamais avant),
> bascule automatique sur grand écran à ce même moment (sur mobile, on reste
> sur la note générée) ; il est conservé avec le brouillon
> (`consultations.verification_json`) et réaffiché au chargement. Par sécurité
> face à une chute du flux SSE, l'onglet qui a lancé la génération relit en
> parallèle la consultation persistée (~toutes les 5 s) : l'audit s'affiche
> dès qu'il y est écrit, sans dépendre du seul événement `verification_result`
> (une relecture coupée revient sur les ~5 s suivantes). À l'ouverture
> d'un brouillon, l'onglet du panneau est choisi selon l'état de la note :
> **« Transcription brute »** tant qu'aucune note n'existe (on suit la dictée
> en direct), **« Validation »** dès que la note est générée et que la
> rubrique « Corrections » ou l'audit a quelque chose à y montrer.
>
> Le même onglet « Validation » reçoit la rubrique **« Corrections et
> éléments à valider »**, retirée de la note à la génération : elle n'est
> jamais écrite dans le document clinique ni envoyée à l'audit, mais stockée
> à part (`consultations.corrections_markdown`) et affichée dans l'onglet dès
> la fin du streaming. Les deux contenus — corrections puis section
> **« Validation - 2e passe »** — se présentent en simple markdown, comme le
> reste de l'application. Les brouillons antérieurs qui portent encore la
> rubrique dans leur note la font réextraire à l'ouverture.
> Une régénération réinitialise `verification_json` au moment où la nouvelle
> note est persistée : la base ne porte jamais un audit de l'ancienne note
> pendant le contrôle en cours.
> La référence de l'audit dépend du fournisseur actif : pour ceux qui
> reçoivent l'audio (Gemini, Qwen Omni, point de terminaison personnalisé,
> OpenRouter), l'audit croise la note avec l'AUDIO de la dictée (source de
> vérité, jamais la transcription approximative) ; pour ceux qui ne le
> reçoivent pas (Anthropic, OpenAI, Cohere, Mistral), il croise la note avec
> la TRANSCRIPTION, avec des consignes volontairement plus permissives (la
> transcription du moteur vocal peut se tromper). Sans audio ni transcription
> à croiser, la bascule active produit immédiatement un « rien à signaler » :
> pas de roue qui tourne dans le vide. Le **modèle de 2e passe** se choisit
> par fournisseur dans le panneau (champ « Modèle de 2e passe (Validation) ») ;
> laissé vide, c'est le même modèle que la mise en forme qui audite.
> Coût observé (audio) : ~60 % d'un appel de génération en plus (l'audio
> domine), atténué par le cache de préfixe implicite — l'audio et la consigne
> système étant relus depuis le cache au second passage (cf. ci-dessus).
> Fiabilité : l'audit audio réutilise le budget de raisonnement configuré
> dans le panneau (`gemini_thinking_budget`) — au plancher 128 il hallucine
> des écarts inexistants — et un garde-fou déterministe écarte après coup
> toute « omission » déjà présente dans la note et toute « invention » déjà
> portée par la transcription (le seuil est conservateur : un vrai écart a
> toujours un terme absent, il n'est donc jamais effacé).

---

## 4. Le proxy inverse

Créez une ressource vers `http://<hôte>:8787`, en HTTPS côté public.

### ⚠️ Le proxy doit RELAYER, sans authentifier

**Désactivez toute authentification du proxy sur cette ressource.**

Ce n'est pas une préférence. Un proxy qui authentifie lui-même retire souvent
l'en-tête `Cookie` des requêtes qu'il relaie — Pangolin le fait. L'application
ne verrait jamais revenir son propre témoin de session, et la connexion
bouclerait indéfiniment : le flux aboutit chez le fournisseur, puis retombe sur
une session vide.

Symptôme : la page de connexion réapparaît sans message, ou `/auth/callback`
affiche « Connexion expirée ou témoin de session absent ».

### Ce que le proxy doit laisser passer

| Chemin | Raison |
|---|---|
| `/auth/login`, `/auth/callback`, `/auth/logout` | Le flux de connexion |
| `/static/manifest.webmanifest`, `/sw.js`, `/static/icons/` | Installation PWA : le navigateur les demande **sans** témoin |
| Corps de requête ≥ 200 Mo | Une dictée longue dépasse les limites par défaut |

### Flux en direct (`/api/events`)

Cette route reste ouverte en permanence : c'est elle qui synchronise deux
appareils sur la même consultation (dictée sur le téléphone visible aussitôt
sur l'écran de bureau). Un proxy aux réglages par défaut coupe souvent une
réponse HTTP inactive après 30 à 60 secondes — l'application envoie un signal
toutes les 10 s pour éviter ça, mais un proxy avec un délai d'inactivité plus
court que ce signal fermera quand même la connexion. Si la synchronisation
semble se couper puis reprendre sans cesse, augmentez ce délai côté proxy
(`proxy_read_timeout` sur nginx, par exemple) plutôt que du côté de
l'application.

Quand une dictée démarre sur un autre appareil et que le médecin clique
**Suivre** (de même qu'en rouvrant un brouillon depuis « Mes brouillons » ou
après une génération reçue en direct), le panneau de transcription s'ouvre sur
l'onglet **« Transcription brute »** s'il n'y a pas encore de note — pour voir
la dictée arriver —, sinon sur l'onglet **« Validation »** dès que la rubrique
« Corrections » ou l'audit a quelque chose à y montrer (jamais d'onglet vide).

### Pare-feu

Le port du conteneur n'a aucune raison d'être joignable au-delà du proxy :

> *DSM → Panneau de configuration → Sécurité → Pare-feu* → **Autoriser**
> TCP 8787 depuis l'IP du proxy, puis **Refuser** TCP 8787 depuis « Toutes »,
> règle placée **après**.

---

## 5. Premier démarrage

### La page de connexion

Avant de partir chez le fournisseur d'identité, `/auth/login` propose le choix
de la durée de session : usage ponctuel (quelques heures) ou « rester
connecté » 30 jours. Au clavier aussi : les flèches **←/→** changent d'option,
**Entrée** valide et enchaîne.

### Le premier usager devient administrateur

À la première connexion sur une installation vide, le compte est créé et reçoit
l'administration. C'est l'amorçage : sans cette exception, personne ne pourrait
ouvrir le panneau.

> Cela suppose que **l'inscription soit fermée chez votre fournisseur**.
> Ouverte, le premier venu prendrait l'installation.

Ensuite, `ALLOW_SIGNUP` décide :

| Situation | Résultat |
|---|---|
| Compte connu, actif | entre |
| Compte connu, désactivé | refusé |
| Inconnu, `ALLOW_SIGNUP=true` | créé dans `users` |
| Inconnu, `ALLOW_SIGNUP=false` | refusé |

### Groupes

| Groupe | Panneau | Gabarits | Ses consultations |
|---|---|---|---|
| `admins` | oui | oui | oui |
| `users` | non | non | oui |

Les groupes annoncés par le fournisseur sont rapprochés **par nom** et seulement
**en ajout** : un groupe absent de la réponse n'est jamais retiré. Beaucoup de
fournisseurs n'envoient la revendication que sous conditions, et la traiter
comme la vérité complète retirerait ses droits au dernier administrateur le jour
où elle manque. Les retraits se font depuis **Réglages → Comptes et groupes**.

### Porte de secours

Si plus personne ne peut entrer — client OIDC supprimé, fournisseur
injoignable, dernier compte désactivé :

```ini
AUTH_DISABLED=true
```

puis `docker compose up -d`. L'application s'ouvre sans authentification sous
`DEV_USER`, avec les droits d'administrateur, le temps de réparer.
**Remettez `false` immédiatement après.**

---

## 6. Vérification du déploiement

```bash
# 1. Le conteneur répond et signale ses défauts de configuration
curl -s http://127.0.0.1:8787/healthz | python3 -m json.tool

# 2. Aucun avertissement bloquant
docker compose logs consultai | grep CONFIGURATION

# 3. Une navigation non authentifiée redirige vers la connexion
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/ \
     -H 'Accept: text/html'                    # attendu : 302

# 4. Un appel d'API non authentifié renvoie du JSON, pas une redirection
curl -s http://127.0.0.1:8787/api/config       # attendu : 401 {"detail":…}
```

Puis, dans un navigateur, ouvrez `BASE_URL` : vous devez arriver chez votre
fournisseur, revenir connecté, et voir votre nom dans la pastille en haut à
droite.

Le journal de démarrage résume l'état effectif :

```
Authentification : OIDC chez https://… | retour https://…/auth/callback
                 | 1 compte(s) connu(s) | inscription automatique : non
Modèle : gemini / gemini-2.5-flash | Reconnaissance vocale : soniox (fr-CA) | Langue : fr
```

---

## 7. Exploitation

### 7.1 Panneau d'administration

**Réglages**, visible des seuls administrateurs, organisé par flux de travail —
cinq onglets :

| Onglet | Contenu |
|---|---|
| Dictée | Service de reconnaissance vocale (sous-onglet par service : clé, modèle, langue), retrait des longues pauses, temps réel de la dictée (mode, VAD) |
| Note | Modèle de langage (sous-onglet par fournisseur : clé, modèle, rapide, température, audio, raisonnement), consigne générale fr/en, affichage du raisonnement |
| Comptes et accès | Inscription automatique, attributs du nom affiché et de l'avatar, comptes et groupes |
| Données et sauvegarde | Purge des dossiers après délai, rotation des sauvegardes, export/import et restauration (§ 9) |
| Statistiques | Usage et coûts par fournisseur |

Une **recherche** au-dessus des onglets filtre tous les réglages (libellé,
aide, nom technique) tous onglets confondus ; cliquer un résultat ouvre
l'onglet — et le sous-onglet de service — où se trouve le champ, puis y amène.

Les champs ne s'affichent que quand ils ont un objet : le VAD ne se règle
qu'en mode temps réel « énoncé », le modèle et le délai de streaming Mistral
qu'en mode « streaming », la durée maximale d'audio joint seulement si l'audio
est joint, la transcription conservée pendant l'enregistrement seulement si
l'on ignore le service vocal, le budget de raisonnement seulement si le
raisonnement est activé. Masquer un champ n'efface rien : la valeur reste en
base. Les réglages fins — seuils VAD en millisecondes, repli sur erreur et
découpage en tranches du point de terminaison personnalisé — se replient sous
un bloc **Avancé**.

Dans le panneau **Statistiques**, la liste des **Tarifs** est regroupée par
des onglets-fournisseur : un clic filtre le tableau. Les tarifs du fournisseur
actif y sont préremplis au premier démarrage (voir § 8 pour les décisions de
conformité à tout changement de fournisseur).

Ces valeurs sont stockées en base et **surchargent le `.env`** : effet immédiat,
sans reconstruction. Vider un champ le remet à la valeur du `.env`. Chaque champ
indique sa provenance (`panneau` ou `.env`).

Dans **Dictée** et **Note**, un **sous-menu** ouvre les réglages de chaque
service, actif ou non : on peut y coller une clé ou un modèle sans mettre le
service en production — il n'y a plus de case à cocher pour dévoiler les
fournisseurs non sélectionnés. Une clé partagée entre deux services (Cohere et
Mistral : transcription + note ; OpenAI : Whisper + note) n'existe qu'une fois
en base : le champ est répété sous chacun des deux onglets, et toute saisie
s'y reflète aussitôt.

Le bouton **Modèles disponibles**, en pied de panneau, interroge le fournisseur
**du sous-onglet consulté** avec sa clé et propose dans le champ « Modèle » ce
à quoi ce compte a réellement droit. Il couvre le modèle de langage (Note) **et**
la reconnaissance vocale (Dictée : Deepgram, Cohere, Mistral, OpenAI, point de
terminaison personnalisé, OpenRouter). Les fournisseurs sans liste de modèles
(Google, Soniox, AssemblyAI, Modulate) le signalent : le nom se saisit alors à
la main. Si le modèle configuré n'apparaît pas dans la liste, un avertissement
prévient que la transcription (ou la mise en forme) échouera.

> Le **point de terminaison personnalisé** expose un **Budget de sortie**
> (`custom_llm_max_tokens`, 32768 jetons par défaut) propre à ce fournisseur,
> distinct du plafond de Gemini, et un réglage **Raisonnement**
> (`custom_llm_reasoning_effort`, « Auto » par défaut) qui envoie
> `reasoning.effort` aux modèles à raisonnement (DeepSeek via OpenRouter…). Un
> tel modèle consomme une large part du budget dans sa pensée : si le budget
> est trop bas, il renvoie une réponse vide (« motif : length ») — l'application
> relance alors automatiquement avec un budget doublé, et le panneau explique
> comment l'ajuster. Le raisonnement ne s'applique qu'à la **mise en forme de
> la note** : l'extraction des métadonnées (tâche mécanique en JSON) ne le
> reçoit jamais, un modèle reflexif y renvoyant du texte hors JSON. Pour
> l'extraction, un **modèle rapide non raisonneur** (field « Modèle rapide »)
> est recommandé.
>
> **OpenRouter** expose les **mêmes capacités** que le point de terminaison
> personnalisé (Budget de sortie `openrouter_llm_max_tokens`, Raisonnement
> `openrouter_llm_reasoning_effort`, format audio, audio direct), avec une clé
> dédiée (`OPENROUTER_API_KEY`). Le modèle par défaut,
> `thinkingmachines/inkling-small`, est multimodal et open-weight : il accepte
> l'audio joint et le **contournement de la reconnaissance vocale** (note
> directe), comme Gemini.

> **Afficher le raisonnement (thinking) pendant la génération.** Deux
> réglages sous Note — **Montrer le raisonnement — administrateurs**
> (`show_thinking_admin`) et **Montrer le raisonnement — utilisateurs**
> (`show_thinking_users`), **désactivés par défaut**. Lorsque
> l'un est actif pour la personne qui génère, le raisonnement du modèle défile
> dans la fenêtre de note (même dévoilement progressif que le texte, avec un
> badge « Raisonnement du modèle… »), puis est **effacé de l'écran** dès que le
> texte de la note commence : il n'est **jamais enregistré**. Supporté pour
> Gemini (parties `thought`), les endpoints OpenAI-compatibles à raisonnement
> (`reasoning_content`/`reasoning`, ex. DeepSeek, Qwen) et Anthropic (blocs
> `thinking`). Sans effet si le modèle ne produit pas de raisonnement.

Dans l'onglet Comptes, l'appartenance se règle en cliquant les pastilles de
groupe, et chaque changement s'applique immédiatement — il n'y a rien à
enregistrer.

> Ce qui gouverne l'accès reste hors d'atteinte du navigateur : `OIDC_*`,
> `SESSION_SECRET` et `AUTH_DISABLED` ne sont que dans le `.env`.

### 7.2 Choisir un service vocal

| Service | Adaptation au vocabulaire | À savoir |
|---|---|---|
| **Soniox** | 60 termes de contexte | Le moins cher (~0,10 $US/h), multilingue |
| **AssemblyAI** | 1000 termes | Module médical `medical-v1`, facturé en supplément |
| **Google** | ~300 expressions | Aucun modèle médical francophone |
| **Deepgram** | mots-clés, **nova-2 seulement** | `nova-3` ignore les mots-clés hors anglais |
| **Cohere** | **aucune** | ⚠️ voir ci-dessous |
| **Mistral Voxtral** | **aucune connue** | Clé partagée avec le modèle de langage Mistral |
| **OpenAI (Whisper)** | **aucune** | Clé partagée avec le modèle de langage OpenAI |
| **Modulate** | 100 termes (`custom_terms`) | Velma STT multilingue, détection de langue par énoncé |
| **Personnalisé** | **aucune** | Endpoint compatible API OpenAI (ex. Whisper auto-hébergé) ; l'audio reste sur votre machine |
| **OpenRouter** | liste passée en consigne (prompt) | Modèle multimodal (ex. `thinkingmachines/inkling-small`) interrogé en « chat » avec part audio — l'audio part au cloud (§ 11.2) |

> Un point de terminaison personnalisé est tout indiqué pour un **Whisper
> local** (ce déploiement s'appuie sur `speaches`/faster-whisper, interne au
> réseau Docker) : aucun envoi de l'audio hors de la machine (§ 11.2).

> ⚠️ **Cohere est déconseillé pour la dictée clinique.** Plafonné à 5
> requêtes/minute sur une clé d'essai — la dictée envoie une tranche toutes les
> 10 s **et par usager**, soit 6 requêtes/minute : **une seule dictée dépasse
> déjà le plafond**. Remontez `DICTATION_SEGMENT_SECONDS` à 30 pour rester sous
> la barre. Il n'offre aucune adaptation au vocabulaire et s'est montré
> nettement moins fiable sur les noms de médicaments à l'essai, jusqu'à en
> inventer. L'application étale les envois et réessaie sur 429, mais une tranche
> peut être retardée.

Le lexique clinique livré (~320 expressions : acronymes du réseau québécois,
échelles, molécules) est **francophone**. Il n'est pas envoyé pour un gabarit
anglais, où le champ **Vocabulaire additionnel** du gabarit devient la seule
source transmise au moteur.

### 7.3 Retrait des longues pauses

Les services facturent à la durée d'audio. Seule la **copie envoyée** est
raccourcie ; l'enregistrement conservé reste intact et la durée affichée reste
celle de la dictée réelle. Gain constaté : 30 à 40 %.

Ne descendez pas `STT_SILENCE_KEEP_SECONDS` à 0 : les moteurs se servent des
pauses pour placer la ponctuation.

> **Audio joint au modèle de langage** : quand « Joindre aussi l'audio » est
> actif (`send_audio`), l'extrait envoyé est **OGG/Opus** par défaut — petit et
> accepté par Gemini, Qwen Omni et OpenRouter (vérifié sur inkling-small). Le
> **point de terminaison personnalisé** comme **OpenRouter** exposent en plus un
> réglage **Format audio envoyé** (`custom_send_audio_format` /
> `openrouter_send_audio_format` : OGG/MP3/WAV) : un modèle comme **Mistral
> Voxtral** derrière un endpoint **exige MP3 ou WAV** et rejette l'OGG
> (`400 Failed to load audio file — valid mp3 or wav`). Choisissez alors `mp3` ;
> le fichier est transcodé en mono 48 kHz avant l'envoi. Gemini et Qwen
> ignorent ce réglage.

### 7.4 Gabarits

Quatre sont livrés, tous verrouillés :

| Gabarit | Langue | |
|---|---|---|
| Consultation Médicale Générale | fr | 🔒 protégé |
| General Medical Consultation | en | 🔒 protégé |
| Consultation - Gériatrie | fr | 🔒 protégé |
| Suivi - Gériatrie | fr | 🔒 protégé |

L'ordre ci-dessus est l'ordre d'affichage livré (`sort_order` 101 à 104 : les
deux consultations générales d'abord, la gériatrie ensuite).

Les quatre sont ni modifiables ni supprimables — le refus est appliqué côté
serveur. **Dupliquez-les** pour obtenir une copie indépendante et entièrement
modifiable ; c'est le chemin prévu, et un bouton du formulaire le propose.
Étant verrouillés, ils sont rafraîchis à chaque démarrage : une amélioration
livrée avec l'application profite aux installations existantes.

Chaque gabarit comporte : **Instructions cliniques** (ce sur quoi le modèle se
concentre), **Mise en page** (le squelette Markdown, qui fixe la structure
exacte), **Vocabulaire additionnel** et **Langue**.

**Rubrique finale obligatoire.** Quelle que soit la mise en page, la note se
termine toujours par la section des **éléments à valider**, sous l'intitulé
exact prévu par le gabarit (`## ÉLÉMENTS À VALIDER` dans les gabarits français
livrés). Liste télégraphique à deux mentions possibles seulement —
« correction apportée : … » ou « … → à confirmer » —, « Aucun élément à
signaler. » quand il n'y a rien à rapporter ; jamais « Confirmé ». Au-delà de
8 éléments, ils sont regroupés par catégorie. La consigne générale l'exige
(§ 6).

**Structure des rubriques.** La consigne générale impose deux règles de mise en
forme (§ 1 et § 3), renforcées en 2026-08-17 pour les modèles plus sensibles :
les sections **narratives** (HMA, histoire sociale, investigations) se rédigent
en **paragraphes courts et suivis** — jamais en liste à puces — tandis que
**Impression** et **Plan** restent en **liste numérotée**. Dans le **Plan**, une
action dictée à la première personne conserve son « je » tel quel (jamais
réduite à l'infinitif, au substantif ou au passif), et une action dictée sans
pronom se transcrit sans pronom — la personne grammaticale dictée est respectée
strictement (§ 3). Une rubrique ENTIÈRE
sans contenu dicté est **supprimée** (titre compris), de même qu'une ligne
d'en-tête sans valeur dictée (médecin de famille, lieu) ; le marqueur
`[inaudible]` ne sert qu'à un passage inintelligible situé À L'INTÉRIEUR d'une
rubrique qui produit par ailleurs du contenu — il ne remplace jamais une rubrique
ou une ligne vide.

**Correction de la transcription.** La consigne générale (§ 2.0, ajouté le
2026-08-28) rappelle que la transcription vient d'une reconnaissance vocale,
pas d'un texte tapé : ses erreurs sont **phonétiques** (homophones — souvent
des mots courants parfaitement orthographiés, ex. « un casseur de saint
droit »), pas des fautes de frappe, et une correction rétablit le mot dicté
sans ajouter d'information — elle n'introduit jamais un fait non dicté (doute →
règle des deux lectures, § 1).

**Fidélité au contenu dicté.** La consigne générale interdit autant l'omission
que l'invention (§ 1) : toute donnée clinique dictée figure dans la note —
condenser raccourcit la formulation d'un fait, jamais sa suppression ni sa
fusion avec un autre — et la vérification finale (§ 6) en vérifie la
réciproque. Chaque hospitalisation, visite ou séjour institutionnel mentionné
(lieu, année, motif) y figure, sans être fusionné avec le séjour ou la visite
actuelle ; le gabarit « Suivi - Gériatrie » nomme explicitement les
hospitalisations antérieures dans sa règle du Résumé. Une hospitalisation
antérieure dictée dans l'énumération des antécédents reste dans la rubrique
Antécédents (contexte et synthèse dictés compris), jamais déplacée vers
l'HMA — qui ne couvre que le motif actuel de la consultation (§ 1). Les modifications de
traitement d'une visite antérieure (médicament débuté, cessé, renouvelé, dose
modifiée) sont portées dans la note, dans leur rubrique (Résumé ou HMA selon
le gabarit), distinctes du plan de traitement actuel. Dans **l'Impression**,
toute impression ou hypothèse clinique dictée est conservée, même subjective,
même contradictoire avec un résultat objectif (« MMSE stable voire amélioré,
mais j'ai l'impression qu'il se détériore au niveau amnésique » conserve les
deux faits) ; dans le **Plan**, chaque action ou recommandation figure sur sa
propre ligne numérotée, y compris un délai de suivi (« à revoir dans 6 mois »),
même bref et même sans verbe (§ 3, renforcé le 2026-08-27 suite à des omissions
constatées à la validation).

Champs de substitution disponibles dans la mise en page : `{{DATE}}`,
`{{DEMANDEUR}}`, `{{ACCOMPAGNATEUR}}`. (`{{PATIENT}}` et `{{DOSSIER}}` sont
conservés pour la compatibilité des gabarits existants, mais ne sont **plus
alimentés** : la ligne qui les porte est retirée de la note — l'identité du
patient n'est pas collectée, voir § 11.) Une ligne dont le champ reste inconnu
est retirée du document.

### 7.5 La langue du gabarit pilote la chaîne

Le champ **Langue** d'un gabarit n'est pas une étiquette : il décide des
consignes envoyées au modèle, de la consigne générale employée, du code de
langue transmis au service vocal, de l'envoi ou non du lexique francophone, et
de la langue de rédaction de la note.

Il n'y a **aucune détection automatique** depuis l'audio ou le texte : elle se
tromperait sur une consultation bilingue.

La langue d'**interface** est distincte et propre à chaque usager : on peut lire
l'écran en français et produire une note anglaise.

#### Dicter avant d'avoir choisi son gabarit

C'est le cas courant : on démarre la dictée, on choisit le gabarit ensuite. Les
premières tranches partent alors dans la langue par défaut, et une consultation
anglaise revient transcrite en français — que la mise en forme ne peut pas
rattraper, puisque le modèle reçoit déjà des mots faux.

Choisir un gabarit d'une **autre langue que celle de la transcription** déclenche
donc une proposition :

| Moment | Ce qui se passe |
|---|---|
| **Pendant** la dictée | La session bascule sur le nouveau gabarit : les tranches à venir suivent la nouvelle langue. Celles déjà transcrites restent en l'état — l'application le dit. |
| **Après** la dictée | Une question propose de renvoyer l'enregistrement au service vocal. La transcription est alors **remplacée**, pas complétée : c'est le même audio, reconnu autrement. |

La retranscription ne touche pas la note déjà mise en forme, et n'a lieu que sur
réponse affirmative : c'est un appel facturé de plus, sur toute la durée de
l'enregistrement. Elle exige que l'audio ait été conservé — ce qui est le cas
par défaut (§ 11). L'appel reste bloquant — le texte complet revient en une
réponse — mais le **toast de progression** (événements SSE
`transcription_progress`, alimentée pendant le découpage du point de
terminaison personnalisé) montre l'avancement en temps réel ; l'import de
fichier (§ 7.4) en bénéficie aussi.

> Tous les états « en cours » de l'application — génération, transcription
> et retranscription, fin de dictée, reprise et uploads — partagent le **même
> toast de progression** (`showProgressToast`, app.js) : une ligne (spinner
> harmonisé + message + pourcentage à droite s'il est connu) et une piste fine
> — déterministe quand un avancement réel existe (`transcription_progress`),
> indéterminée sinon, sans jamais afficher de faux pourcentage. La génération
> déroule une séquence de PHASES sur ce toast : « Préparation… » (au clic),
> « Envoi au modèle… » (juste avant le POST), « Traitement en cours… » dès
> l'événement SSE `generation_started` (le serveur a fini d'envoyer la
> requête au fournisseur — ConsultAI n'exécute pas le modèle), « La note se
> génère… » dès le premier morceau `generation_chunk`, puis « Validation en
> cours… » à la fin de la génération quand la bascule « Validation » est
> active (jusqu'à l'arrivée du `verification_result`). Plus aucun voile plein
> écran bloquant. Sur mobile, tous les toasts tiennent sur une seule ligne
> (message tronqué avec des points de suspension).

> Rien de tout cela n'est automatique. Retranscrire écrase du texte que le
> médecin a pu déjà corriger à la main.

> Un gabarit n'est jamais traduit. Un gabarit français avec l'interface en
> anglais produit une note aux **titres de rubriques français** et au corps
> anglais : les consignes exigent de reproduire exactement la structure fournie,
> et cette exigence l'emporte sur la langue de rédaction. Pour une note
> entièrement anglaise, dupliquez le gabarit et traduisez ses titres.

### 7.6 Installation sur mobile (PWA)

Ouvrez `BASE_URL` puis **Partager → Sur l'écran d'accueil** (iOS, Safari
exclusivement) ou **Installer** (Android/Chrome). Exige HTTPS.

La version du service worker **suit celle de l'application**, substituée
automatiquement au service (`/sw.js`) : la purge du cache des appareils
installés est déclenchée par chaque nouvelle version publiée, sans étape
manuelle.

Le service worker ne met en cache que des ressources statiques et anonymes. Ni la
page `/`, ni les appels `/api/` — ils contiennent des renseignements de santé.

### 7.7 Temps réel de la dictée

Le batch fiable reste l'épine dorsale : l'audio est téléversé par fragments,
copié localement (IndexedDB) et conservé en entier sur le serveur — rien ne
change. Le temps réel est une **couche d'affichage par-dessus**, déclenchée par
un **détecteur de parole (VAD)** tournant dans le navigateur sur l'énergie du
micro déjà mesurée pour la waveform.

Trois modes, réglés par `STT_REALTIME_MODE` (défaut `off`) :

| Mode | Ce qui se passe | Fournisseurs |
|---|---|---|
| `off` | Comportement historique : tranche ~10 s, texte après ~10-15 s | tous |
| `vad` | À la fin de chaque énoncé, le navigateur signale au serveur de transcrire immédiatement ; coupe au silence (ffmpeg fait autorité sur la frontière) ; le texte apparaît quelques secondes après chaque pause | **tous** (dont Parakeet local) |
| `sse` | Idem `vad`, plus : le texte provisoire arrive en **deltas** (Mistral Voxtral realtime) pendant la parole, affiché en italique sous la transcription ; le commit durable suit le chemin habituel | Mistral seulement |

Le texte de la transcription s'affiche **progressivement** dans le panneau STT
(« token par token »), avec la même mécanique que la note structurée — que ce
soit les segments committés ou la ligne provisoire du mode `sse`.

**Contexte conservé en mode `sse`.** Contrairement à une session par énoncé, le
mode `sse` garde **une seule session WebSocket par dictée** : chaque énoncé y
est ajouté, et le modèle de streaming conserve le contexte des énoncés
précédents (noms de médicaments, acronymes, cohérence de l'anamnèse). Le
serveur facture par énoncé, pas par session. `MISTRAL_REALTIME_DELAY_MS`
(`target_streaming_delay_ms`) règle le compromis : attendre un peu avant de
transcrire pour rassembler du contexte (1000 ms par défaut). Si la session
meurt (réseau, pause très longue), le repli batch du même énoncé prend le
relais — rien n'est perdu, seul le contexte recommence.

Précisions importantes :

- **Le VAD ne filtre jamais l'enregistrement.** Le fichier brut reste complet
  du premier au dernier fragment : c'est ce qui permet de corriger a posteriori.
- **Filet de fin** (`STT_VAD_FINISH_SWEEP`, défaut `on`) : au « Terminer », le
  serveur re-parcourt l'audio brut (détection de parole par ffmpeg, côté
  serveur) et **re-transcrit tout passage manqué** — un énoncé que le VAD
  n'avait pas vu, une tranche qui avait échoué. Rien n'est perdu.
- **Une seule transcription par plage d'audio.** Le VAD accélère le
  déclenchement de la passe existante ; il n'ajoute pas de seconde passe.
- **`sse` envoie l'audio à l'API Mistral** (hors de la machine) : engagement
  de conformité modifié, voir § 11 et EFVP. `vad`, lui, **préserve**
  l'engagement « l'audio ne quitte jamais la machine » avec le Parakeet local.
- Contraintes appliquées automatiquement : `sse` n'est actif qu'avec Mistral ;
  `vad` est désactivé avec Cohere (5 requêtes/minute incompatibles avec la
  granularité des énoncés).
- Réglages fins : `STT_VAD_SENSITIVITY` (low/medium/high), `STT_VAD_SPEECH_MS`
  (parole reconnue après), `STT_VAD_SILENCE_MS` (fin d'énoncé après) et, pour
  le mode `sse`, `MISTRAL_REALTIME_MODEL`.

Les énoncés très courts (moins de ~0,7 s, ex. « oui », « d'accord ») sont plus
exposés qu'avant, noyés qu'ils étaient dans une tranche de 10 s : le seuil
`_MIN_SPEECH_SECONDS` de la reconnaissance les écarte. Le filet de fin les
retente au « Terminer » sans garantie — comportement assumé, à reconsidérer si
le gain de latence ne compense pas ces interjections.

### 7.8 Correction des médicaments (grounding, liste pointée)

Réglage admin `DICTATION_GROUNDING` (groupe **Dictée**, section « Correction
des médicaments », défaut `false`). Une fois activé :

- **Stabilisation audio par l'arrière.** La transcription par tranches coupe
  parfois un mot à la jointure de deux segments (aucun silence à portée de la
  fenêtre). Chaque frontière non encore stabilisée est **ré-écoutée en
  continu** (découpage aux silences réels) et le texte des segments concernés
  est remplacé en direct : le texte « se corrige par l'arrière » quelques
  secondes après la dictée (SSE `transcript_correct`). Conçu pour un **point
  de terminaison STT custom auto-hébergé** (aucune limite de taux) ; coûte des
  appels STT en arrière-plan.
- **Liste pointée des médicaments.** Les noms déformés par la reconnaissance
  vocale sont normalisés contre la **base canadienne de produits
  pharmaceutiques (BDP)** livrée dans l'image (`app/meds.sqlite`, moteur
  déterministe `app/med_grounding.py`, dépendance `rapidfuzz` — aucun appel
  réseau). La liste (nom normalisé + posologie, en **puces**, jamais de
  pointillés) s'affiche sous le transcrit et **en haut de l'onglet
  « Validation »**, mise à jour en direct (`med_grounding`) puis définitive
  (`med_grounding_result`, persistée dans `med_grounding_json`).
- **S'étend à l'audio importé et à la retranscription** : la liste est
  recalculée sur le texte complet, renvoyée dans la réponse (`med_items`) et
  diffusée par SSE.
- **Confiance mot-à-mot (endpoint STT custom)** : quand le serveur renvoie
  `words[].confidence` (`response_format=json`), une substitution
  orthographique *floue* d'un mot dicté **très confiant** est refusée sauf si
  une dose, un verbe d'administration ou une région de liste médicament la
  porte. C'est la garantie anti-faux-positifs : `laisse → Latisse`,
  `diabète → Diabeta`, `d'autres → Dobutrex` (confiance ~1.00) ne sont plus
  réécrits dans la prose, alors que les vrais noms déformés portés par une
  dose (`l'aldol PRN → Haldol`, `aspirine 80`) restent corrigés. Seuil
  mesuré sur corpus réel : **0.92** (`CONF_HARD_FLOOR` dans
  `app/med_grounding.py`). Sans confiance disponible, le comportement
  historique est conservé.
- **Garbles à nom commençant par un nombre** : « 13 IBA » (homonyme
  phonétique de « Tresiba ») est consommé en **nom unique** — le « 13 » est
  l'amorce phonétique du nom, pas une dose. Le mot isolé (« IBA » en prose)
  reste inchangé.
- **Garble seedé à condition de dose** : un déformation comme « faire » (=
  « fer », sulfate ferreux) n'est corrigée que si une **dose est voisine**
  (« faire 300 mg » → « fer 300 ») ; en prose le verbe reste intact.
- **Posologie directionnelle** : la preuve de posologie n'est créditée à un
  nom qu'en bon escient — un marqueur de dose placé **après** le nom ne
  suffit que si le nom est quasi-certain ou sert dans une région de liste
  confirmée. La prose « de façon régulière HS » ne transforme donc plus
  « régulière » en médicament (`régulière → REGULEX`) et le « HS » reste au
  vrai médicament qui précède. La fenêtre de dose **avant** le nom s'arrête à
  la ponctuation de phrase (« … au coucher régulièrement. Prochain point. » :
  le « coucher » d'avant le point ne crédite pas « point. »). Le gate de
  confiance mot-à-mot n'est levé que par une preuve arrière, une ancre ou une
  région confirmée — jamais par une simple dose en avant.
- **Liste anti-fantôme** : la liste pointée de l'onglet « Validation » et
  l'import ne retiennent un nom de médicament que muni d'un signal de dosage —
  posologie captée, chiffre de dose adjacent (« aspirine 80 », « calcium 500 »,
  « rivastigmine timbre 10 »), région de liste confirmée ou nom composé
  (« Vitamine D »). Un nom nu halluciné par le STT et canonisé (« diclofenac
  diethylamine » entre deux actions) sort de la liste — le texte inline,
  lui, n'est jamais touché.
- **Liste des médicaments : noms sans dose collée**. Un bigramme « nom +
  chiffre » (ex. « bisoprolol 2,5 », « calcium 500 ») n'est jamais traité
  comme un nom composé : le chiffre reste dans la **posologie** (« bisoprolol
  2,5 mg DIE »), jamais dans le nom de l'item.
- **La liste normalisée est isolée par des lignes vides** dans le texte
  transcrit (avant et après le bloc), pour repérer d'un coup d'œil où le
  script a corrigé les noms.
- Après génération de la note, l'onglet **Validation** s'ouvre sur grand
  écran (en plus de la rubrique « Corrections » et de l'audit existants).

La base BDP peut être régénérée depuis les extraits bruts (dossier
`med_grounding/` du dépôt, scripts `build_db.py` / `seed_aliases.py` /
`prune_db.py` / `ban_terms.py` / `fix_inactive_otc.py` / `prune_scope.py`) ;
ce dossier ne fait pas partie du conteneur. La base est **assainie pour le
périmètre gériatrique** : hors catalogue — vaccins (sauf Shingrix, Pneumovax
23, Prevnar, Capvaxive, Vaxneuvance, Fluzone, Fluad, Arexvy, Abrysvo),
produits de contraste et diagnostics, gaz médicaux, solutés IV/dialyse,
hygiène/cosmétique (désinfectants mains, émollients, anti-acné,
antisudorifiques, shampoings, soins dentaires, antiprurigineux OTC,
rubéfiants), anesthésiques, sirops toux/rhume OTC, décongestionnants nasaux,
suppléments/multivitamines, contraception/obstétrique/fertilité,
helminthiases, **homéopathie** et produits de santé naturels. Une liste de
sauvegarde conserve les exceptions réellement dictées en gériatrie
(diclofénac topique, Xylocaïne/lidocaïne/EMLA, Zincofax/cremes barrière,
Peridex/chlorhexidine/benzydamine, codéine/acétylcystéine, Nix-Stromectol,
Dostinex…). Les anciennes marques disparues continuent de matcher (MAXERAN,
ARICEPT, LOPRESSOR…) tant qu'elles ne sont pas dans une classe retirée. Les
marques retirées ne font que cesser d'être normalisées : le générique
correspondant reste toujours catalogué.

---

## 8. Mise à jour

Le code est **inclus dans l'image**, republiée à chaque changement. Seuls
`./data` et `./secrets` sont des volumes.

```bash
docker compose pull
docker compose up -d

# Modification du seul .env, sans nouvelle image :
docker compose up -d
```

> **Machine de référence (`/opt/dictai`)** : la pile y monte le code du dépôt
> en lecture seule (`/home/opc/ConsultAI/app` → `/app/app`, ainsi que
> `CHANGELOG.md` — voir `docker-compose.yml`). Le conteneur tourne donc
> directement la source, sans attendre l'image de la CI. Avant le
> redéploiement, régénérer la feuille de style Tailwind (artefact de build
> absent du dépôt) puis recréer le conteneur :
>
> ```bash
> cd /home/opc/ConsultAI
> [ -d node_modules ] || npm ci
> node_modules/.bin/tailwindcss -i app/static/tailwind-src.css -o app/static/tailwind.css --minify
> cd /opt/dictai
> sudo docker compose pull consultai
> sudo docker compose up -d --force-recreate consultai
> ```

Le schéma de la base est migré automatiquement au démarrage : colonnes ajoutées,
gabarits protégés rafraîchis, gabarits livrés obsolètes retirés. Les journaux
l'indiquent ligne par ligne.

---

## 9. Sauvegarde

Deux niveaux se complètent :

**1. Sauvegarde intégrée, depuis le panneau (onglet Sauvegarde).** Exports
à la demande, rotation automatique (`backup_retention_count`, défaut 7) et
restauration. Les archives sont **sanitisées** : ni audio, ni données cliniques
(config, comptes, gabarits et statistiques seulement). Conséquence assumée :
une restauration ne ramène pas les données patient — l'audio existant est
laissé intact (voir § 11.2).

**2. Sauvegarde à chaud de la base, côté serveur.** Tout tient dans `./data` :
base SQLite, audio conservé, dictées en cours.

```bash
# Sauvegarde à chaud, sûre pendant l'utilisation (mode WAL)
docker compose exec consultai python -c \
  "import sqlite3; s=sqlite3.connect('/data/consultai.db'); \
   d=sqlite3.connect('/data/sauvegarde.db'); s.backup(d); d.close(); s.close()"
```

Incluez `/volume1/docker/ConsultAI/data` dans Hyper Backup.

> **N'incluez jamais `./secrets` ni `.env` dans une sauvegarde non chiffrée** :
> ils contiennent les clés d'API et `SESSION_SECRET`.

---

## 10. Dépannage

### Connexion

| Symptôme | Cause et correctif |
|---|---|
| La page de connexion revient en boucle, ou « témoin de session absent » | Le proxy retire l'en-tête `Cookie`. **Désactivez son authentification** sur cette ressource (§ 4). |
| Tout le monde est déconnecté à chaque redémarrage | `SESSION_SECRET` est vide. Fixez-la. |
| « Adresse de retour refusée par le fournisseur » | `OIDC_REDIRECT_URI` ne correspond pas, au caractère près, à ce qui est déclaré chez le fournisseur. |
| « Le compte … n'est pas autorisé » | `ALLOW_SIGNUP=false` et le compte n'existe pas. Créez-le ou activez l'inscription. |
| La déconnexion ne revient pas à l'application | Le fournisseur exige `id_token_hint`, capté à la connexion. Reconnectez-vous une fois après une mise à jour. Certains fournisseurs demandent aussi que `BASE_URL` soit déclarée comme adresse de retour de déconnexion. |
| Après migration, les brouillons ont disparu | Le nom d'usager retenu diffère de l'ancien propriétaire. **Réglages → Comptes** affiche le nombre de consultations par compte ; la colonne `owner` de `consultations` est la clé. |
| Plus aucun administrateur | `AUTH_DISABLED=true`, réparer, remettre à `false` (§ 5). |

### Démarrage

| Symptôme | Cause et correctif |
|---|---|
| `unable to open database file` | `APP_UID`/`APP_GID` ne correspondent pas au propriétaire de `./data` (§ 2.1). |
| **Bad Gateway** au proxy | `BIND_ADDRESS` n'est pas joignable depuis le proxy. Testez `curl -m5 http://<hôte>:8787/healthz` depuis la machine du proxy. |
| `Le modèle « … » est introuvable` | Panneau → **Modèles disponibles**, puis reportez un identifiant de la liste. |

### Dictée

| Symptôme | Cause et correctif |
|---|---|
| Le bouton micro ne fait rien | `getUserMedia` exige HTTPS. Passez par l'adresse publique. |
| Micro refusé dans l'app installée sur iPhone | Bogue de certaines versions d'iOS en mode écran d'accueil. Dictez depuis Safari. |
| « Installer » n'apparaît pas | Exige HTTPS et, sur iOS, Safari. Vérifiez que `/static/manifest.webmanifest` et `/sw.js` répondent 200 sans authentification. |
| L'interface ne se met pas à jour sur mobile | Service worker en cache : la version suit celle de l'application (§ 7.6), elle se purge au redéploiement. |
| L'enregistrement s'arrête écran éteint | Verrou d'écran non supporté. Gardez l'application au premier plan. |
| Erreur 413 sur un long enregistrement | Augmentez la taille de corps autorisée au proxy. |
| « Enregistrement trop long pour un envoi direct » | Au-delà de ~55 min. Configurez `STT_GCS_BUCKET` ou dictez en plusieurs parties. |
| La note est coupée à la fin | Augmentez `GEMINI_MAX_OUTPUT_TOKENS` ; l'interface le signale. Malgré son nom, ce plafond vaut pour les sept fournisseurs, et chacun a sa propre limite — l'application ramène la valeur sous celle du fournisseur retenu. Cohere (famille command-a) et le point de terminaison personnalisé raisonnent : leur budget de sortie propre (32000 / 32768 jetons, relance automatique doublée) évite la note vide « raisonnement saturant ». |
| Acronymes mal transcrits | Ajoutez-les au **Vocabulaire additionnel** du gabarit. |
| Dictée transcrite dans la mauvaise langue | Choisissez le gabarit de la bonne langue : l'application propose de retranscrire l'enregistrement (§ 7.5). Sans enregistrement conservé, elle refuse — il n'y a plus de source. |
| Tranches retardées avec Cohere | Limite de 5 req/min atteinte. Changez de service (§ 7.2). |
| Navigateur fermé en plein enregistrement | La dictée n'est pas perdue : l'audio est rattaché au brouillon (marqué **« Abandonnée »** en rouge pâle) et peut encore servir à générer la note — notamment avec un fournisseur en **audio direct**. Un toast vous y renvoie au chargement, tant que le brouillon existe. Le brouillon suit la rétention globale (§ 7.1). Les dictées de moins de 10 s sont supprimées. Une dictée simplement en pause n'est jamais marquée. |

### Diagnostic

```bash
docker compose logs -f consultai
curl -s http://127.0.0.1:8787/healthz
```

Dans la console du navigateur, `consultaiDiag()` explique pourquoi l'installation
PWA échoue.

---

## 11. Confidentialité

Cette application manipule des renseignements de santé. Les points suivants ne
sont pas des recommandations de style.

### 11.1 Chaque implantation exige une ÉFVP

L'application traite des renseignements personnels et de santé au sens de la
*Loi sur la protection des renseignements personnels dans le secteur privé*
(RLRQ, c. P-39.1), modifiée par la **Loi 25** : l'article 3.3 impose de réaliser
une **évaluation des facteurs relatifs à la vie privée (ÉFVP)** pour tout
système d'information qui traite de tels renseignements. L'ÉFVP est propre à
**chaque implantation** — responsable désigné, usagers, fournisseurs STT/LLM
retenus, régions de traitement, mesures de sécurité — et ne se recopie pas d'un
déploiement à l'autre. Elle doit être réévaluée à chaque changement de
fournisseur, de plateforme ou d'usagers. Un modèle complet figure dans
[`EFVP.md`](EFVP.md) ; servez-vous-en comme point de départ, puis adaptez-le à
votre contexte avant toute utilisation clinique réelle.

### 11.2 Données et fournisseurs

* ⚠️ **Les enregistrements audio sont conservés** avec leur brouillon, sous
  `AUDIO_DIR`. C'est la donnée la plus sensible produite : la voix du patient,
  non anonymisable. Elle disparaît quand le brouillon est supprimé — fichier
  compris — et automatiquement au-delà de la rétention configurée
  (`consultation_retention_hours`, défaut **12 h**, réglable au panneau).
* 🆔 **L'identité du patient n'est pas collectée** : le nom et le numéro de
  dossier ne sont ni saisis ni stockés (dénominalisation). Les notes générées
  sont dénominalisées ; l'identification se rattache au versement au dossier,
  hors de l'application.
* 🩺 **Ce n'est pas un « scribe IA »** : l'application n'est pas faite pour
  enregistrer une conversation entre un médecin et ses patients — c'est un outil
  de dictée post-consultation utilisé par le clinicien seul (voir la politique
  de confidentialité).
* 💾 **Les sauvegardes sont sanitisées** : les archives ZIP ne contiennent ni
  audio ni données cliniques (config, comptes, gabarits, statistiques
  seulement). Une restauration ne ramène donc pas les données patient.
* Pendant la dictée, deux copies temporaires existent, effacées à la conclusion :
  sur le serveur sous `DICTATION_DIR` (purgée selon la rétention commune
  `consultation_retention_hours` si la dictée n'est jamais conclue), et dans le
  navigateur (IndexedDB) pour rejouer un envoi raté.
* Transcriptions et notes sont stockées **en clair** dans SQLite. Placez `./data`
  sur un partage chiffré si votre analyse de risque l'exige.
* Le service vocal **et** le fournisseur de modèle traitent des renseignements de
  santé. Le panneau permet de changer l'un ou l'autre en deux clics : **chaque
  changement est une décision de conformité**, pas un réglage. Faites valider les
  fournisseurs, signez les ententes, et privilégiez Vertex AI en région
  `northamerica-northeast1` — le seul choix de la liste qui garde le traitement
  au Québec.
* La note générée doit **toujours** être relue par le clinicien avant d'être
  versée au dossier.

### Résidence des données — les deux trajets se décident séparément

Piège facile à manquer : **Vertex AI ne couvre que le trajet texte**. Conclure
« tout reste au Québec » parce que le modèle de langage est sur Vertex est faux
si le service vocal, lui, est resté ailleurs. Ce sont deux décisions distinctes :

| Trajet | Donnée | Destination | Résidence |
|---|---|---|---|
| Reconnaissance vocale | **audio du patient** | selon `stt_provider` | à vérifier |
| Modèle de langage | transcription | Vertex `northamerica-northeast1` | Québec |

L'audio brut est la plus identifiante des deux : la voix elle-même, les personnes
présentes dans la pièce, les propos incidents qui n'atteignent jamais la
transcription.

Un trajet supplémentaire évite la transcription séparée : les modèles
**multimodaux** (Gemini, Qwen Omni, point de terminaison personnalisé,
OpenRouter) peuvent recevoir **l'audio directement** (option propre à chaque
fournisseur dans le panneau). Dans ce cas le service vocal n'est pas appelé
(`stt_provider` inactif) et toute la résidence se décide du côté du fournisseur
de modèle — ce qui peut ramener le trajet audio au même endroit que le texte.
⚠️ **OpenRouter est un service cloud** (traite hors du Québec) : toute bascule
vers lui — note directe comme STT — est une décision de conformité à part,
comme n'importe quel fournisseur hébergé.

> **« Conserver une transcription pendant l'enregistrement »** (activable avec
> l'audio direct) : la reconnaissance vocale continue de tourner à l'écran **et**
> la transcription accompagnée la note en **guide** — l'audio reste la source
> autoritaire, le texte sert de filet anti-omission (éléments dictés oubliés
> d'un audio seul). Désactivé (défaut) : aucun appel vocal pendant
> l'enregistrement, la note vient de l'audio seul.

Vérification en une commande — ce qui est **réellement** en service, et non ce que
dit le `.env` (le panneau le surcharge) :

```bash
docker exec consultai python3 -c "from app import llm, runtime_config; from app.config import settings; \
print('vertex:', settings.gemini_use_vertex, '| llm:', llm.active_provider(), '| stt:', runtime_config.value('stt_provider'))"
```

> ⚠️ `gemini_use_vertex` n'est vrai que si `GEMINI_API_KEY` est **vide** *et*
> `GOOGLE_CLOUD_PROJECT` renseignée. Une clé oubliée fait retomber silencieusement
> sur l'API grand public, hors région — sans aucun message.

**Décision de ce déploiement (2026-08-14)** : modèle de langage sur Vertex AI à
Montréal, et **audio envoyé directement à Gemini** (Vertex AI, `northamerica-northeast1`)
— le seul choix qui garde **les deux trajets** au Québec, couvert par l'addendum
de politique cloud de Google consenti pour les renseignements de santé. Le
service de transcription par défaut est un **Whisper local** (`speaches`,
faster-whisper, interne au réseau Docker) : l'audio n'y quitte jamais la
machine. Aucun envoi vers un service STT hébergé hors de la VM. À réévaluer à
chaque bascule de fournisseur (la précédente décision, du 2026-07-31, misait sur
AssemblyAI aux États-Unis et a été abandonnée au profit du trajet local +
Vertex).

### Fonctionnement sans CDN (facultatif)

L'interface charge Tailwind, `marked` et `DOMPurify` depuis un CDN public — aucune
donnée clinique n'y transite. Pour une autonomie complète :

```bash
mkdir -p app/static/vendor && cd app/static/vendor
curl -LO https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js
curl -LO https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js
curl -Lo tailwind.js "https://cdn.tailwindcss.com?plugins=forms,typography"
```

Remplacez les trois `<script src="https://…">` d'`app/templates/index.html` par
`/static/vendor/…`, reconstruisez. Le service worker purge alors son cache à la
prochaine version (la sienne suit celle de l'application, § 7.6).

---

## 12. Redéployer ailleurs

Aucun clonage nécessaire (§2) : redéployer, c'est reconstituer trois éléments
qui vivent délibérément hors de l'image, à côté de `docker-compose.yml`.

| Élément | À reconstituer |
|---|---|
| `.env` | Depuis `.env.example`. Toutes les clés y sont vides. |
| `secrets/gcp-sa.json` | Seulement si vous utilisez Google. |
| `./data` | Créé vide au premier démarrage ; le schéma se migre seul. |

Conservez `.env` et la clé de service dans un gestionnaire de mots de passe :
rien dans l'image ne peut les reconstituer.

Sur un hôte non Synology, deux réglages méritent un second regard :
`APP_UID`/`APP_GID` (le contournement d'ACL Synology n'a plus d'objet — les
défauts de l'image, 1000:1000, conviennent) et `BIND_ADDRESS` + la règle de
pare-feu du § 4.

Le code source complet (pour l'audit, la contribution, ou reconstruire
soi-même l'image) est sur
[github.com/varialflip/ConsultAI](https://github.com/varialflip/ConsultAI).

> **Branche `selfhosted` (instance de test)** : le dépôt porte aussi une
> branche `selfhosted` qui alimente l'instance de test `test.dictai.ca`
> (miroir de `app.dictai.ca`). Elle vit dans un git worktree dédié
> (`/home/opc/ConsultAI-selfhosted`) monté dans le conteneur `consultai-test`
> de la pile de production. Configuration, compte OIDC et redéploiement :
> `/opt/dictai/AGENTS.md`.

### Structure du dépôt

```
app/
├── main.py               API FastAPI et routes
├── auth.py               session, identité, permissions
├── oidc.py               flux OpenID Connect (mono ou double fournisseur)
├── users.py              comptes, groupes, règles d'entrée
├── preferences.py        préférences par usager (langue)
├── config.py             lecture et validation du .env
├── runtime_config.py     réglages du panneau (base de données)
├── database.py           schéma SQLite et migrations
├── default_templates.py  les quatre gabarits livrés (verrouillés)
├── default_prompts.py    consignes générales fr / en
├── dictation.py          dictée par tranches
├── audio_cache.py        cache de l'audio préparé pour la génération
├── live.py               synchronisation en direct (SSE) entre appareils
├── stt.py                transcodage, découpage, services vocaux
├── llm.py                consignes et appel du modèle (audio direct possible)
├── recordings.py         audio attaché aux brouillons
├── backup.py             sauvegardes sanitisées et rotation
├── pricing.py            tarifs des fournisseurs
├── usage.py              statistiques et coûts
├── scheduler.py          tâches quotidiennes (purge, sauvegarde, statistiques)
├── changelog.py          nouveautés des 7 derniers jours (page de connexion)
├── i18n.py               textes de l'interface (fr / en)
├── templates/index.html  interface et feuille de style d'impression
├── templates/login.html  page de connexion (version + nouveautés)
└── static/app.js         logique du navigateur
```
