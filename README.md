# ConsultAI — Guide de déploiement

Application web auto-hébergée de **dictée de consultations cliniques**. Le
médecin dicte, l'application transcrit, un modèle de langage met en forme selon
un gabarit, le médecin relit et exporte.

* Interface **française ou anglaise**, au choix de chaque usager.
* **Huit** services de reconnaissance vocale et **sept** fournisseurs de
  modèle de langage, commutables depuis le panneau d'administration sans
  reconstruire l'image. Plusieurs modèles (Gemini, Qwen Omni, point de
  terminaison personnalisé) peuvent aussi recevoir **l'audio directement**,
  sans transcription séparée.
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
| Une clé de modèle de langage | Gemini, Anthropic, OpenAI, Cohere, Mistral ou Qwen Omni — au moins une (peut attendre le premier démarrage, voir §2) |
| Une clé de service vocal | Google, Deepgram, AssemblyAI, Soniox, Cohere, Mistral ou OpenAI (Whisper) — au moins une (idem) |
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

# Modèle de langage — au moins un
GEMINI_API_KEY=                # ou GOOGLE_CLOUD_PROJECT pour Vertex AI
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
QWEN_OMNI_API_KEY=
QWEN_OMNI_BASE_URL=            # documentation DashScope
# Cohere et Mistral : pas de variable propre, COHERE_API_KEY / MISTRAL_API_KEY
# ci-dessus servent aux deux usages
```

> **Cohere, Mistral et OpenAI n'ont qu'une clé pour deux usages** :
> `COHERE_API_KEY`, `MISTRAL_API_KEY` et `OPENAI_API_KEY` alimentent chacun le
> service vocal *et* le modèle de langage (OpenAI Whisper d'un côté, les
> modèles gpt-4o de l'autre). Le champ n'apparaît donc qu'une fois dans le
> panneau, sous Reconnaissance vocale, et le panneau du modèle y renvoie.
>
> Un **point de terminaison personnalisé** (ex. Whisper auto-hébergé,
> compatible API OpenAI) se configure uniquement depuis le panneau, sans
> variable de clé propre : `custom_stt_base_url`, `custom_stt_model` et une clé
> éventuelle. L'audio y reste sur votre machine (§ 7.2, § 11.2). Il accepte
> aussi un **modèle de repli** (`custom_stt_fallback_model` et, au besoin,
> `custom_stt_fallback_base_url`) : en cas d'erreur HTTP 5xx de l'endpoint
> principal, la transcription est retentée une fois avec le modèle de repli.
> Un **découpage en tranches** (`custom_stt_chunk_seconds`, 60 s par défaut)
> découpe l'audio au-delà de cette durée et envoie chaque tranche au modèle
> principal, en coupant de préférence dans un silence : même un endpoint qui
> plafonne en longueur d'audio par passe (ex. un Parakeet/ONNX plafonnant
> autour de 6-7 min) garde le modèle principal sur toute la dictée. Le
> découpage prime sur le **seuil de durée** (`custom_stt_max_seconds`), qui
> reste disponible sans découpage pour envoyer directement au modèle de repli
> les dictées trop longues. Le **retrait des silences est suspendu** pour cet
> endpoint (pas de facturation à la durée, et le plafonnement des pauses
> dégrade certains modèles locaux multilingues) : l'audio lui est envoyé tel
> quel.

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

### Pare-feu

Le port du conteneur n'a aucune raison d'être joignable au-delà du proxy :

> *DSM → Panneau de configuration → Sécurité → Pare-feu* → **Autoriser**
> TCP 8787 depuis l'IP du proxy, puis **Refuser** TCP 8787 depuis « Toutes »,
> règle placée **après**.

---

## 5. Premier démarrage

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

**Réglages**, visible des seuls administrateurs. Sept onglets :

| Onglet | Contenu |
|---|---|
| Système | Inscription automatique, rétention des consultations |
| Reconnaissance vocale | Service, clés, modèles, retrait des longues pauses |
| Modèle de langage | Fournisseur, modèle, modèle rapide, température, clés |
| Consignes | Consigne générale, en français et en anglais |
| Comptes et groupes | Revendications d'identité, comptes, groupes, permissions |
| Sauvegarde | Sauvegardes manuelles, rotation, restauration (§ 9) |
| Statistiques | Usage et coûts par fournisseur |

Dans le panneau **Statistiques**, la liste des **Tarifs** est regroupée par
des onglets-fournisseur : un clic filtre le tableau. Les tarifs du fournisseur
actif y sont préremplis au premier démarrage (voir § 8 pour les décisions de
conformité à tout changement de fournisseur).

Ces valeurs sont stockées en base et **surchargent le `.env`** : effet immédiat,
sans reconstruction. Vider un champ le remet à la valeur du `.env`. Chaque champ
indique sa provenance (`panneau` ou `.env`).

Dans Reconnaissance vocale et Modèle de langage, un **sous-menu** ouvre les
réglages de chaque service, actif ou non : on peut y coller une clé ou un
modèle sans mettre le service en production — il n'y a plus de case à cocher
pour dévoiler les fournisseurs non sélectionnés.

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
| **Personnalisé** | **aucune** | Endpoint compatible API OpenAI (ex. Whisper auto-hébergé) ; l'audio reste sur votre machine |

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
> accepté par Gemini et Qwen Omni. Le **point de terminaison personnalisé**
> expose en plus un réglage **Format audio envoyé** (`custom_send_audio_format` :
> OGG/MP3/WAV) : un modèle comme **Mistral Voxtral** derrière OpenRouter **exige
> MP3 ou WAV** et rejette l'OGG (`400 Failed to load audio file — valid mp3 or
> wav`). Choisissez alors `mp3` ; le fichier est transcodé en mono 48 kHz avant
> l'envoi. Gemini et Qwen ignorent ce réglage.

### 7.4 Gabarits

Quatre sont livrés, tous verrouillés :

| Gabarit | Langue | |
|---|---|---|
| Consultation Médicale Générale | fr | 🔒 protégé |
| General Medical Consultation | en | 🔒 protégé |
| Consultation - Gériatrie | fr | 🔒 protégé |
| Suivi - Gériatrie | fr | 🔒 protégé |

Les quatre sont ni modifiables ni supprimables — le refus est appliqué côté
serveur. **Dupliquez-les** pour obtenir une copie indépendante et entièrement
modifiable ; c'est le chemin prévu, et un bouton du formulaire le propose.
Étant verrouillés, ils sont rafraîchis à chaque démarrage : une amélioration
livrée avec l'application profite aux installations existantes.

Chaque gabarit comporte : **Instructions cliniques** (ce sur quoi le modèle se
concentre), **Mise en page** (le squelette Markdown, qui fixe la structure
exacte), **Vocabulaire additionnel** et **Langue**.

**Rubrique finale obligatoire.** Quelle que soit la mise en page, la note se
termine toujours par la section **`Éléments à valider`** — corrections apportées
et éléments à confirmer, en liste télégraphique (au-delà de 8 éléments, ils sont
regroupés par catégorie). La consigne générale l'exige (§ 4.1) et les gabarits
français livrés l'inscrivent (`## ÉLÉMENTS À VALIDER`) pour que tous les modèles
la produisent, sans exception.

**Structure des rubriques.** La consigne générale impose deux règles de mise en
forme (§ 1 et § 3), renforcées en 2026-08-17 pour les modèles plus sensibles :
les sections **narratives** (HMA, histoire sociale, investigations) se rédigent
en **paragraphes courts et suivis** — jamais en liste à puces — tandis que
**Impression** et **Plan** restent en **liste numérotée**. Une rubrique ENTIÈRE
sans contenu dicté est **supprimée** (titre compris), de même qu'une ligne
d'en-tête sans valeur dictée (médecin de famille, lieu) ; le marqueur
`[inaudible]` ne sert qu'à un passage inintelligible situé À L'INTÉRIEUR d'une
rubrique qui produit par ailleurs du contenu — il ne remplace jamais une rubrique
ou une ligne vide.

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
réponse — mais une **barre de progression** (événements SSE
`transcription_progress`, alimentée pendant le découpage du point de
terminaison personnalisé) montre l'avancement en temps réel ; l'import de
fichier (§ 7.4) en bénéficie aussi.

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
| La note est coupée à la fin | Augmentez `GEMINI_MAX_OUTPUT_TOKENS` ; l'interface le signale. Malgré son nom, ce plafond vaut pour les sept fournisseurs, et chacun a sa propre limite — l'application ramène la valeur sous celle du fournisseur retenu. |
| Acronymes mal transcrits | Ajoutez-les au **Vocabulaire additionnel** du gabarit. |
| Dictée transcrite dans la mauvaise langue | Choisissez le gabarit de la bonne langue : l'application propose de retranscrire l'enregistrement (§ 7.5). Sans enregistrement conservé, elle refuse — il n'y a plus de source. |
| Tranches retardées avec Cohere | Limite de 5 req/min atteinte. Changez de service (§ 7.2). |

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
**multimodaux** (Gemini, Qwen Omni, point de terminaison personnalisé) peuvent
recevoir **l'audio directement** (option propre à chaque fournisseur dans le
panneau). Dans ce cas le service vocal n'est pas appelé (`stt_provider` inactif)
et toute la résidence se décide du côté du fournisseur de modèle — ce qui peut
ramener le trajet audio au même endroit que le texte.

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
├── note_schema.py         gabarit -> LayoutSpec ; structure de la note extraite (JSON)
├── note_extraction.py     transcription -> JSON structuré, réparation ciblée (§13)
├── note_validator.py      vérifications mécaniques sur le JSON extrait (§13)
├── note_renderer.py       JSON validé -> markdown final, en code pur (§13)
├── drug_lookup.py         client BDPP Santé Canada, vérification médicament (§13)
├── templates/index.html  interface et feuille de style d'impression
├── templates/login.html  page de connexion (version + nouveautés)
└── static/app.js         logique du navigateur
```

## 13. Pipeline de structuration en JSON (branche `selfhosted`, expérimental)

Chemin de structuration alternatif à `llm.generate_note_stream` (markdown
directement). Objectif : rendre mécaniquement vérifiable ce qui, avant,
dépendait entièrement du respect des consignes par le modèle — utile en
particulier avec des modèles auto-hébergés plus petits que Gemini 2.5 Pro,
moins fiables sur le respect strict d'un format.

**Branché sur `/api/generate`**, derrière le réglage panneau (Modèle de
langage) « Pipeline JSON structuré (test) » (`note_pipeline_json`,
désactivé par défaut — voir `app/runtime_config.py`) : `api_generate`
choisit `_generate_json_pipeline` ou l'ancien `_generate_and_publish`
selon ce réglage (`app/main.py`). Pas de diffusion en direct pour ce
chemin (un seul appel bloquant, pas de flux caractère par caractère —
le front-end s'en accommode déjà). Persistance, `extract_metadata` et
journalisation d'usage restent inchangés pour les deux chemins. La
réponse gagne un champ `validator_issues` (ce que le validateur a relevé)
pour observer le pipeline pendant les tests.

Limites connues de ce branchement (pas un défaut caché — juste hors
périmètre pour l'instant) : pas d'audio seul (`bypass_stt`/`send_audio`),
pas de contexte/instructions ponctuelles (`extra_instructions`), pas de
jetons d'usage remontés (`usage: {}` côté réponse). Une consultation qui
dépend de l'un de ces chemins doit rester sur le réglage par défaut.

Trois étapes, chacune dans son module :

1. **`note_extraction.extract_note`** — même consigne générale et même
   consigne de gabarit qu'aujourd'hui (`default_prompts.py`,
   `default_templates.py`, inchangées), mais la sortie demandée au modèle est
   un objet JSON structuré (`app.llm.complete(..., json_mode=True)`, déjà
   utilisé par `extract_metadata`), pas du markdown. La forme attendue du
   JSON est dérivée de `note_schema.parse_layout(gabarit.layout_format)` —
   aucune liste de rubriques codée en dur, un gabarit dupliqué/modifié par un
   médecin est donc supporté sans changement de code.
2. **`note_validator.validate`** — vérifie mécaniquement ce que le format
   JSON permet de vérifier sans modèle : texte de remplissage interdit
   (auto-retiré), accolades `{{...}}` oubliées, balises HTML, présence et
   cohérence d'Éléments à valider, préservation de la voix à la première
   personne en Impression/Plan (« je crois que... »), et ancrage
   (« grounding ») de chaque valeur critique contre un extrait exact de la
   transcription.
3. **`note_extraction.validate_and_repair`** — pour ce qui nécessite un
   jugement, un appel de réparation CIBLÉ (juste le champ en cause, pas une
   régénération), plafonné à 2 tentatives ; au-delà, repli déterministe —
   jamais une nouvelle boucle, jamais un passage silencieux. Un problème
   d'ancrage (médicament/dose) n'est JAMAIS renvoyé au modèle pour
   « correction » : il est signalé dans Éléments à valider dès le premier
   passage (sécurité du patient — voir `_REPAIRABLE_CODES` dans
   `note_extraction.py`).
4. **`note_renderer.render`** — JSON validé -> markdown, en code pur, sans
   appel modèle : la grammaire d'Éléments à valider (« terme dicté →
   **correction apportée : X** » / « → **à confirmer** ») et le texte fixe du
   gabarit (ex. « Rédigé à l'aide de la reconnaissance vocale. ») sont
   produits par ce code, jamais par une consigne que le modèle pourrait mal
   suivre.

Tests : `tests/test_note_pipeline.py` (`unittest`, sans dépendance
supplémentaire) — couvre le parsing des quatre gabarits livrés (y compris les
sous-rubriques imbriquées de « Consultation - Gériatrie »), chaque
vérification du validateur, le rendu, et `validate_and_repair` avec un
`app.llm.complete` simulé (aucune clé de fournisseur requise). Lancer :
```
python3 -m unittest tests.test_note_pipeline -v
```

Branché sur `/api/generate` sans diffusion en direct (voir plus haut) : un
seul appel bloquant, décision assumée plutôt que laissée en suspens.
L'ancrage médicament/DIN contre la Banque de données des produits
pharmaceutiques de Santé Canada est maintenant implémenté (voir « Vérification
de médicament par appel d'outil » plus bas) — `grounded_fields` couvre
l'ancrage générique contre la transcription, la vérification BDPP couvre en
plus l'existence du nom lui-même dans un référentiel de médicaments. La Liste
RAMQ (couverture d'assurance, distincte de « ce nom est-il un vrai
médicament ») reste hors périmètre — son format de diffusion (PDF, pas d'API
propre) rend l'accès programmatique nettement plus coûteux que la BDPP.

Correctifs trouvés en testant deux générations réelles avec un modèle self-hosted-like (`mistral-small-latest`, pas Gemini), 2026-08-18 :
une rubrique peut désormais avoir SON PROPRE contenu direct EN PLUS de sous-rubriques imbriquées (clé réservée `__contenu__`, `note_renderer.OWN_CONTENT_KEY`) — nécessaire pour que MÉDICATION ACTUELLE + `### ALLERGIES` rende les deux, pas seulement l'un des deux ; les gabarits « Consultation Médicale Générale » et « General Medical Consultation » utilisent maintenant ce même schéma imbriqué (au lieu de « MÉDICATION ACTUELLE ET ALLERGIES » fusionné) ; le filtre de texte de remplissage couvre une famille de formulations plutôt qu'une liste fermée d'exemples ; les corrections aberrantes dans Éléments à valider (identique au terme dicté, ou « à confirmer » écrit comme lecture) sont auto-corrigées.

**Style de liste (numéroté vs à puces), 2026-08-18.** Une valeur de
`sections` peut être un tableau JSON (plusieurs items distincts). Le style
d'affichage — « 1. », « 2. »... ou « - » — est déclaré dans le GABARIT, pas
deviné du texte des consignes : une instruction `{{liste numérotée}}` (ou
`{{numbered list}}`) placée juste sous un titre de rubrique
(`note_schema.LIST_STYLE_MARKERS`, `LayoutSpec.list_style`) marque cette
rubrique ; par défaut, une liste reste à puces. `note_renderer` fait TOUJOURS
le numérotage lui-même, et retire d'abord tout marqueur (`1.`, `1)`, `-`,
`•`) que le modèle aurait écrit lui-même en tête d'item malgré la consigne
contraire — un modèle plus faible le fait parfois quand même, et sans ce
nettoyage la note affiche une double numérotation. Les quatre gabarits
verrouillés portent ce marqueur sous IMPRESSION et PLAN, et (depuis le
correctif ci-dessous) `{{liste à puces}}` sous ANTÉCÉDENTS MÉDICAUX ET
CHIRURGICAUX, MÉDICATION ACTUELLE et EXAMEN OBJECTIF/PHYSIQUE.

Le marqueur ne sert pas qu'au RENDU (numéroté/puces) : `note_extraction`
s'en sert aussi pour NUDGER le modèle vers un encodage en tableau JSON
(`_list_style_nudge`, `LayoutSpec.explicit_list_style` — distinct de
`list_style`, qui retombe sur « bulleted » par défaut pour le rendu
seulement). Sans ce nudge explicite, un modèle plus faible rend une rubrique
« liste pointée » en PROSE À VIRGULES au lieu d'un tableau (vu réellement :
Médication, Antécédents et Examen rendus en une seule phrase malgré la
consigne du gabarit) — la seule mention « liste pointée » dans la consigne de
gabarit ne suffit pas, il faut aussi que le schéma JSON transmis au modèle le
demande explicitement pour CETTE rubrique précise.

**Historique de génération (branche `selfhosted` uniquement), 2026-08-18.**
Table additive `note_generations` (`app.database.NoteGeneration`) : une ligne
par tentative de génération (pipeline, fournisseur, modèle, variante de
consigne — `prompt_variant` —, markdown produit, problèmes du validateur),
insérée EN PLUS de l'écrasement habituel de
`consultations.generated_markdown`/`edited_markdown` (qui reste inchangé —
voir le commentaire dans `main.py`). But : pouvoir comparer plusieurs
itérations de gabarit/consigne sur la même dictée sans qu'une régénération
n'efface la précédente. Jamais lu par le pipeline de génération lui-même,
jamais activé en production (la table n'existe que sur les bases où ce code
tourne).

**Consigne dédiée au pipeline JSON, 2026-08-18.** `general_prompt_json_fr`/`_en`
(réglage à part de « Consigne générale » — jamais un remplacement) : version
condensée qui ne garde que les décisions de contenu clinique, sans les règles
de mise en forme désormais imposées par `note_renderer`. Voir
`default_prompts.JSON_GENERAL_PROMPT_FR/EN` pour le détail et le
raisonnement. Testée par 3 générations réelles contre la consultation #5
(mistral-small-latest) comparées via `note_generations` : a corrigé une
fabrication de diagnostics à partir de valeurs de laboratoire brutes
(« Hypothyroïdie subclinique », « Anémie légère » — absentes avec la consigne
d'origine) en ajoutant une règle explicite contre l'inférence diagnostique à
partir d'un chiffre isolé. **Risque résiduel connu** : un item d'Impression
non dicté, dérivé d'un chiffre (« nécessite optimisation »), passe encore —
le validateur ne couvre pas aujourd'hui les items d'Impression/Plan par
`grounded_fields`, seulement les valeurs identifiées comme critiques
(médicament, dose, date, nom). Piste de suivi, pas encore implémentée :
étendre le grounding à Impression/Plan plutôt que de compter sur la seule
formulation de la consigne pour un modèle plus faible.

**Vérification de médicament par appel d'outil (BDPP Santé Canada), 2026-08-18.**
Réglage `note_lookup_dpd` (désactivé par défaut, sans effet sauf si le
pipeline JSON est actif ET le fournisseur est Mistral) : le modèle reçoit un
outil d'appel de fonction, `verifier_medicament_dpd`
(`note_extraction._DPD_TOOL_SCHEMA`), qu'il peut invoquer pendant
l'extraction pour vérifier un nom de médicament incertain contre la Base de
données sur les produits pharmaceutiques de Santé Canada — API publique, sans
authentification (`app/drug_lookup.py`, requête `urllib` brute, même
convention que les fournisseurs Mistral/Cohere existants dans `llm.py`, ne
lève jamais).

Choix de conception délibéré, décidé par Fred contre la suggestion initiale
(un contrôle purement déterministe après extraction, sans intervention du
modèle) : l'appel d'outils permet au modèle de consulter la base **pendant**
qu'il décide, pas seulement après coup. `llm.py` gagne un point d'entrée SŒUR
de `complete()` — `complete_with_tools()` — plutôt qu'une extension de sa
signature : aucun autre fournisseur ne sait aujourd'hui appeler des outils,
et `complete()` a un contrat simple dont dépendent six branches ; un nouveau
point d'entrée qui refuse proprement les autres fournisseurs a un impact nul
sur eux. `note_extraction._extract_note_with_dpd_tool` orchestre une boucle
bornée (2 tours, 6 appels maximum — le modèle peut ignorer l'outil, l'appeler
plusieurs fois, ou ne jamais conclure dans le budget imparti, auquel cas un
dernier tour SANS outil force une réponse).

**La preuve de vérification est écrite par le CODE, jamais par le modèle** :
`ExtractedNote.drug_lookups` est peuplé par la boucle d'orchestration
elle-même à partir de ce qu'elle a réellement exécuté — `from_dict()` refuse
délibérément de lire cette clé depuis la réponse JSON du modèle (même
principe que `_REPAIRABLE_CODES` excluant les codes de grounding : un modèle
qui s'auto-déclarerait « vérifié » sans que l'appel ait eu lieu rendrait ce
champ inutile comme garantie). `note_validator.check_drug_lookups` est
informatif seulement (`auto_fixed`, journalisé par
`main._generate_json_pipeline`) — volontairement PAS câblé dans `validate()`,
faute de classifieur fiable « ceci est un médicament » sur le texte libre
d'Éléments à valider ; un heuristique faible câblé en dur produirait un flux
de faux positifs déguisé en vérification sérieuse.

Une absence de correspondance BDPP n'est PAS une preuve d'erreur (médicament
étranger, composé en pharmacie, retiré du marché) : un signal de plus, jamais
une décision automatique, jamais renvoyé au modèle pour « correction » —
même principe de sécurité déjà appliqué à `check_grounding`.

Testé par génération réelle contre la consultation #5 : le modèle a appelé
l'outil 6 fois (une fois par médicament dicté), 5 correspondances trouvées
avec DIN, 1 « non trouvé » (Norvask) traité correctement comme un signal —
pas une erreur, pas un blocage.
