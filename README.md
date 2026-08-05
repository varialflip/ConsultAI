# ConsultAI — Guide de déploiement

Application web auto-hébergée de **dictée de consultations cliniques**. Le
médecin dicte, l'application transcrit, un modèle de langage met en forme selon
un gabarit, le médecin relit et exporte.

* Interface **française ou anglaise**, au choix de chaque usager.
* **Cinq** services de reconnaissance vocale et **quatre** fournisseurs de
  modèle de langage, commutables depuis le panneau d'administration sans
  reconstruire l'image.
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
| Une clé de modèle de langage | Gemini, Anthropic, OpenAI, Cohere ou Mistral — au moins une (peut attendre le premier démarrage, voir §2) |
| Une clé de service vocal | Google, Deepgram, AssemblyAI, Soniox, Cohere ou Mistral — au moins une (idem) |
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
# Cohere et Mistral : pas de variable propre, COHERE_API_KEY / MISTRAL_API_KEY
# ci-dessus servent aux deux usages
```

> **Cohere et Mistral n'ont qu'une clé pour deux usages chacun** :
> `COHERE_API_KEY` / `MISTRAL_API_KEY` alimentent le service vocal *et* le
> modèle de langage. Le champ n'apparaît donc qu'une fois dans le panneau,
> sous Reconnaissance vocale, et le panneau du modèle y renvoie.

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

**Réglages**, visible des seuls administrateurs. Cinq onglets :

| Onglet | Contenu |
|---|---|
| Système | Inscription automatique |
| Reconnaissance vocale | Service, clés, modèles, retrait des longues pauses |
| Modèle de langage | Fournisseur, modèle, modèle rapide, température, clés |
| Consignes | Consigne générale, en français et en anglais |
| Comptes et groupes | Revendications d'identité, comptes, groupes, permissions |

Ces valeurs sont stockées en base et **surchargent le `.env`** : effet immédiat,
sans reconstruction. Vider un champ le remet à la valeur du `.env`. Chaque champ
indique sa provenance (`panneau` ou `.env`).

Les champs d'un fournisseur non sélectionné sont masqués. Pour **saisir** une clé
avant de basculer, cochez *Afficher les champs des fournisseurs non
sélectionnés*.

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

### 7.4 Gabarits

Quatre sont livrés :

| Gabarit | Langue | |
|---|---|---|
| Consultation Médicale Générale | fr | 🔒 protégé |
| General Medical Consultation | en | 🔒 protégé |
| Consultation - Gériatrie | fr | modifiable |
| Suivi | fr | modifiable |

Les deux **protégés** ne sont ni modifiables ni supprimables — le refus est
appliqué côté serveur. **Dupliquez-les** pour obtenir une copie indépendante et
entièrement modifiable ; c'est le chemin prévu, et un bouton du formulaire le
propose.

Les deux autres sont amorcés une seule fois et se comportent comme vos propres
gabarits : modifiables, supprimables, jamais recréés.

Chaque gabarit comporte : **Instructions cliniques** (ce sur quoi le modèle se
concentre), **Mise en page** (le squelette Markdown, qui fixe la structure
exacte), **Vocabulaire additionnel** et **Langue**.

Champs de substitution disponibles dans la mise en page : `{{PATIENT}}`,
`{{DOSSIER}}`, `{{DATE}}`, `{{DEMANDEUR}}`, `{{ACCOMPAGNATEUR}}`. Une ligne dont
le champ reste inconnu est retirée du document.

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
par défaut (§ 11).

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

Après toute modification d'un fichier de `app/static/`, **incrémentez `VERSION`
dans `app/static/sw.js`** : sans cela, les appareils ayant installé
l'application continuent de servir l'ancienne version depuis leur cache.

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

Tout tient dans `./data` : base SQLite, audio conservé, dictées en cours.

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
| L'interface ne se met pas à jour sur mobile | Service worker en cache : incrémentez `VERSION` dans `app/static/sw.js`. |
| L'enregistrement s'arrête écran éteint | Verrou d'écran non supporté. Gardez l'application au premier plan. |
| Erreur 413 sur un long enregistrement | Augmentez la taille de corps autorisée au proxy. |
| « Enregistrement trop long pour un envoi direct » | Au-delà de ~55 min. Configurez `STT_GCS_BUCKET` ou dictez en plusieurs parties. |
| La note est coupée à la fin | Augmentez `GEMINI_MAX_OUTPUT_TOKENS` ; l'interface le signale. Malgré son nom, ce plafond vaut pour les cinq fournisseurs, et chacun a sa propre limite — l'application ramène la valeur sous celle du fournisseur retenu. |
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

* ⚠️ **Les enregistrements audio sont conservés** avec leur brouillon, sous
  `AUDIO_DIR`. C'est la donnée la plus sensible produite : la voix du patient,
  non anonymisable. Elle disparaît quand le brouillon est supprimé — fichier
  compris — et de nulle autre façon. Purgez ce que vous n'avez plus à garder.
* Pendant la dictée, deux copies temporaires existent, effacées à la conclusion :
  sur le serveur sous `DICTATION_DIR` (purgée après
  `DICTATION_RETENTION_HOURS` si la dictée n'est jamais conclue), et dans le
  navigateur (IndexedDB) pour rejouer un envoi raté.
* Transcriptions et notes sont stockées **en clair** dans SQLite. Placez `./data`
  sur un partage chiffré si votre analyse de risque l'exige.
* Le service vocal **et** le fournisseur de modèle traitent des renseignements de
  santé. Le panneau permet de changer l'un ou l'autre en deux clics : **chaque
  changement est une décision de conformité**, pas un réglage. Faites valider les
  fournisseurs, signez les ententes, et privilégiez Vertex AI en région
  `northamerica-northeast1` — le seul choix de la liste qui garde le traitement
  au Québec.
* Préférez une identification indirecte du patient (initiales, numéro de dossier)
  plutôt qu'un nom complet.
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

Vérification en une commande — ce qui est **réellement** en service, et non ce que
dit le `.env` (le panneau le surcharge) :

```bash
docker exec consultai python3 -c "from app import llm, runtime_config; from app.config import settings; \
print('vertex:', settings.gemini_use_vertex, '| llm:', llm.active_provider(), '| stt:', runtime_config.value('stt_provider'))"
```

> ⚠️ `gemini_use_vertex` n'est vrai que si `GEMINI_API_KEY` est **vide** *et*
> `GOOGLE_CLOUD_PROJECT` renseignée. Une clé oubliée fait retomber silencieusement
> sur l'API grand public, hors région — sans aucun message.

**Décision de ce déploiement (2026-07-31)** : modèle de langage sur Vertex AI à
Montréal ; reconnaissance vocale maintenue chez **AssemblyAI (États-Unis)**, en
connaissance de cause. Le module `medical-v1` est le seul de la liste à couvrir la
terminologie clinique **en français**, et l'écart de justesse sur les noms de
molécules et les acronymes du réseau québécois a été jugé plus déterminant pour la
sécurité du patient que la résidence de l'audio. À réévaluer si AssemblyAI ouvre
une région canadienne, ou si un service hébergé au Canada atteint une justesse
comparable en français clinique.

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
`/static/vendor/…`, incrémentez `VERSION` dans `sw.js`, reconstruisez.

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
├── oidc.py               flux OpenID Connect
├── users.py              comptes, groupes, règles d'entrée
├── preferences.py        préférences par usager (langue)
├── config.py             lecture et validation du .env
├── runtime_config.py     réglages du panneau (base de données)
├── database.py           schéma SQLite et migrations
├── default_templates.py  les quatre gabarits livrés
├── default_prompts.py    consignes générales fr / en
├── dictation.py          dictée par tranches
├── stt.py                transcodage, découpage, cinq services vocaux
├── llm.py                consignes et appel du modèle
├── recordings.py         audio attaché aux brouillons
├── i18n.py               textes de l'interface (fr / en)
├── templates/index.html  interface et feuille de style d'impression
└── static/app.js         logique du navigateur
```
