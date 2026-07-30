# ConsultAI — Dictée de consultations cliniques (français / anglais)

Application web auto-hébergée permettant à un médecin de **dicter** une
consultation, de la faire **transcrire** (Google Speech-to-Text, Deepgram,
AssemblyAI — ce dernier avec un module de terminologie médicale francophone —
ou Soniox) puis **structurer** par un modèle de langage (Gemini, Claude
ou OpenAI) selon un **gabarit personnalisable**, avec relecture, correction,
copie et export PDF. L'audio reste attaché au brouillon pour lever un doute, et
s'efface avec lui.

L'application n'est propre à **aucune spécialité** : ce qui l'est vit dans les
gabarits et dans la consigne générale, que le médecin écrit et modifie
lui-même. L'interface existe en **français et en anglais**, et la langue
choisie traverse toute la chaîne — voir § 5 quater.

Conçue pour tourner sur un NAS Synology. Elle **authentifie elle-même**, par
OpenID Connect (§ 4) : le reverse proxy n'est qu'un relais.

---

## 1. Architecture

```
Navigateur (iPad, portable)
   │  HTTPS
   ▼
Reverse proxy ── RELAIS SEULEMENT (son authentification est désactivée)
   │  HTTP interne
   ▼
Conteneur ConsultAI (FastAPI + uvicorn)
   ├── app/oidc.py           flux OpenID Connect (state, nonce, PKCE)
   ├── app/auth.py           session signée → compte → groupes → permissions
   ├── app/users.py          comptes, groupes, règles d'entrée
   ├── app/runtime_config.py réglages du panneau d'administration (base)
   ├── app/dictation.py      réception de la dictée au fil de l'eau, découpage
   ├── app/stt.py            ffmpeg → OGG/Opus → Google / Deepgram / AssemblyAI
   ├── app/llm.py            gabarit + transcription → Gemini/Claude/OpenAI
   ├── app/recordings.py     audio conservé avec le brouillon
   └── app/database.py       SQLite (/data/consultai.db)
```

| Fichier | Rôle |
|---|---|
| `docker-compose.yml` | Service, volumes, variables d'environnement |
| `Dockerfile` | Image multi-étages (amd64 + arm64), ffmpeg inclus |
| `app/config.py` | Lecture et validation de la configuration |
| `app/database.py` | Schéma SQLite et gabarits par défaut |
| `app/auth.py` | Session, identité et permissions |
| `app/oidc.py` | Flux OpenID Connect et lecture des revendications |
| `app/users.py` | Comptes, groupes et règles d'entrée |
| `app/i18n.py` | Textes de l'interface (français / anglais) |
| `app/preferences.py` | Préférences par usager (langue) |
| `app/runtime_config.py` | Réglages modifiables depuis le panneau d'administration |
| `app/dictation.py` | Sessions de dictée : fragments reçus, tranches transcrites |
| `app/recordings.py` | Enregistrements audio attachés aux brouillons |
| `app/stt.py` | Transcodage, découpage, Google / Deepgram / AssemblyAI |
| `app/llm.py` | Prompts et appel du modèle (Gemini, Claude, OpenAI) |
| `app/main.py` | API FastAPI |
| `app/templates/index.html` | Interface (Tailwind) et feuille de style d'impression |
| `app/static/app.js` | Enregistrement, rendu Markdown, sauvegarde auto, export |

---

## 2. Prérequis Google Cloud

> Les services utilisés — reconnaissance vocale et modèle de langage — se
> choisissent ensuite dans le **panneau d'administration** (§ 5 bis). Cette
> section décrit la configuration Google, qui est celle livrée par défaut.

1. Créer un projet Google Cloud et **activer deux API** :
   - `speech.googleapis.com` (Cloud Speech-to-Text)
   - `aiplatform.googleapis.com` (si vous utilisez Vertex AI pour Gemini)

2. Créer un **compte de service** avec les rôles :
   - `Cloud Speech Client` — obligatoire
   - `Vertex AI User` — seulement en mode Vertex AI
   - `Storage Object Admin` — seulement si vous configurez `STT_GCS_BUCKET`

3. Télécharger la clé JSON et la déposer dans `./secrets/gcp-sa.json`.

### Choix du mode Gemini

| Mode | Configuration | Remarque |
|---|---|---|
| Clé API (AI Studio) | `GEMINI_API_KEY=…` | Le plus simple ; traitement hors du Canada |
| **Vertex AI** | `GEMINI_API_KEY` vide + `GOOGLE_CLOUD_PROJECT=…` | **Recommandé** : `GOOGLE_CLOUD_LOCATION=northamerica-northeast1` garde le traitement à Montréal, ce qui simplifie la conformité pour des renseignements de santé québécois |

> **Nom du modèle.** Le cahier des charges mentionnait `gemini-3.5-flash`, qui
> n'existe pas au catalogue Google. La valeur livrée est `gemini-2.5-flash`.
> Pour connaître les modèles réellement accessibles à votre compte, ouvrez
> `https://votre-domaine/api/models` une fois l'application démarrée, puis
> ajustez `GEMINI_MODEL` dans `.env`.

---

## 3. Installation sur le NAS

```bash
cd /volume1/docker/ConsultAI

# 1. Configuration
cp .env.example .env

# 2. ⚠️ ÉTAPE CRITIQUE : votre UID/GID
id votre_utilisateur      # ex. uid=1026(fred) gid=100(users)
# Reportez ces valeurs dans .env : APP_UID=1026 et APP_GID=100

# 3. Renseignez au minimum la section OIDC (§ 4), SESSION_SECRET
#    et la configuration Gemini

# 4. Clé Google
mkdir -p secrets data
cp /chemin/vers/votre-cle.json secrets/gcp-sa.json
chmod 600 secrets/gcp-sa.json

# 5. Démarrage
docker compose up -d --build
docker compose logs -f consultai
```

Au démarrage, les journaux affichent en clair chaque problème de
configuration détecté (`CONFIGURATION : …`). L'état est aussi visible sur
`/healthz`.

L'application écoute sur `127.0.0.1:8787` — **volontairement inaccessible
depuis l'extérieur du NAS**. Seul le reverse proxy doit l'atteindre.

---

## 4. Authentification (OpenID Connect)

L'application **authentifie elle-même**, par OpenID Connect. Le reverse proxy
n'est plus qu'un relais.

```
navigateur ──► /auth/login ──► fournisseur ──► /auth/callback ──► témoin signé
                                                     │
                                              compte en base
```

### 4.1 Créer le client chez le fournisseur

Chez votre fournisseur (Pocket ID, Authentik, Keycloak…), créez un client OIDC
**confidentiel** et déclarez comme adresse de retour, à l'identique :

```
https://consultai.exemple.com/auth/callback
```

Puis dans `.env` :

```ini
OIDC_PROVIDER_URL=https://login.exemple.com
OIDC_CLIENT_ID=…
OIDC_CLIENT_SECRET=…
OIDC_REDIRECT_URI=https://consultai.exemple.com/auth/callback
BASE_URL=https://consultai.exemple.com
OIDC_SCOPES=openid,profile,email,groups
SESSION_SECRET=$(openssl rand -base64 48)
```

`SESSION_SECRET` n'est pas optionnelle en pratique : sans elle, une clé
aléatoire est tirée à chaque démarrage et **tout le monde est déconnecté** à
chaque `docker compose up`.

### 4.2 ⚠️ Le proxy doit RELAYER, pas authentifier

**Désactivez l'authentification de Pangolin sur cette ressource.** Ce n'est pas
une préférence : quand Pangolin authentifie, il **retire l'en-tête `Cookie`**
des requêtes qu'il relaie au conteneur. L'application ne verrait jamais revenir
son propre témoin de session, et la connexion bouclerait indéfiniment — le flux
aboutit chez le fournisseur, puis retombe sur une session vide.

Le symptôme est reconnaissable : la page de connexion réapparaît sans message,
ou `/auth/callback` affiche « Connexion expirée ou témoin de session absent ».

Conséquence de ce changement : `SSO_HEADER_KEY` et `TRUSTED_PROXIES` **ne sont
plus des contrôles de sécurité** et peuvent être retirées du `.env`.
L'application le signale au démarrage si elles y sont encore. Gardez néanmoins
le port du conteneur non exposé et la règle de pare-feu du § 4.4 : un appelant
direct ne peut plus rien forger, mais il n'a aucune raison d'y accéder.

### 4.3 Qui a le droit d'entrer

| Situation | Résultat |
|---|---|
| Compte déjà connu, actif | entre |
| Compte déjà connu, désactivé | refusé |
| **Aucun compte n'existe encore** | entre et **devient administrateur** |
| Inconnu, `ALLOW_SIGNUP=true` | créé dans `users` |
| Inconnu, `ALLOW_SIGNUP=false` | refusé |

> **Le premier usager qui se connecte devient administrateur**, quel que soit
> `ALLOW_SIGNUP`. C'est l'amorçage : sans cette exception, une installation
> neuve n'aurait personne pour ouvrir le panneau. Cela suppose que
> l'inscription soit **fermée** chez votre fournisseur — ouverte, le premier
> venu prendrait l'installation.

Deux groupes sont livrés :

| Groupe | Panneau d'administration | Gabarits | Ses consultations |
|---|---|---|---|
| `admins` | oui | oui | oui |
| `users` | non | non | oui |

Les groupes annoncés par le fournisseur (portée `groups`) sont reportés **par
nom** sur ceux de l'application, et seulement de façon **additive** : un groupe
absent de la réponse du fournisseur n'est jamais retiré. Beaucoup de
fournisseurs n'envoient la revendication que sous conditions, et la traiter
comme la vérité complète ferait perdre ses droits au dernier administrateur le
jour où elle manque. Le retrait d'un droit se fait explicitement, depuis la
gestion des comptes.

### 4.4 Migration depuis l'authentification par en-têtes

Au premier démarrage, l'application crée un compte pour **chaque propriétaire
de consultation existant** (la valeur que portait `Remote-User`), et le premier
entre dans `admins`. Vos brouillons restent donc les vôtres.

À la première connexion, l'identité du fournisseur est rattachée à ce compte en
cascade : `sub`, puis nom d'usager, puis courriel. Le journal l'indique :

```
Compte « fred » créé à partir des consultations existantes. Il conserve ses 8 brouillon(s).
Compte « fred » rattaché à l'identité du fournisseur
```

> **Si vos brouillons n'apparaissent pas après la première connexion**, c'est
> que le nom d'usager retenu diffère de l'ancien propriétaire. Le compte visible
> dans **Réglages → Comptes** affiche le nombre de consultations qu'il porte :
> comparez-le. La colonne `owner` de la table `consultations` est la clé de
> propriété.

### 4.5 Porte de secours

Si plus personne ne peut entrer (client OIDC supprimé, fournisseur
injoignable, dernier compte désactivé à la main dans la base) :

```ini
AUTH_DISABLED=true
```

puis `docker compose up -d`. L'application s'ouvre alors sans authentification,
sous l'identité `DEV_USER`, avec les droits d'administrateur — le temps de
réparer les comptes. **Remettez-la à `false` immédiatement après**, et ne
l'utilisez jamais sur une installation joignable depuis l'extérieur.

### 4.6 Règle de pare-feu Synology

Le NAS publie le port via le proxy userland de Docker (`docker-proxy`), qui
réécrit l'IP source. Cela n'a plus d'incidence sur l'authentification, mais le
port n'a aucune raison d'être joignable au-delà du proxy :

> *DSM → Panneau de configuration → Sécurité → Pare-feu → Modifier les règles*
> → **Créer** : Ports = **TCP 8787**, IP source = celle du proxy,
> Action = **Autoriser**. Puis une règle **Refuser** pour TCP 8787 depuis
> « Toutes », placée **après**.

---

## 5. Utilisation

1. Choisir un **gabarit** dans le menu déroulant.
2. **Enregistrer** la dictée, ou **Importer** un fichier audio existant. Le
   texte apparaît au fur et à mesure, par tranches d'environ trente secondes
   (voir « La dictée » ci-dessous), et s'ajoute à la suite du texte déjà
   présent — on peut donc dicter en plusieurs fois.
3. Corriger la transcription brute si nécessaire, puis **Mettre en forme**.
4. Relire la note dans le panneau droit ; l'onglet **Éditer** donne accès au
   Markdown source.
5. **Copier** (avec mise en forme, pour Word ou le DME), **Markdown** (texte
   brut) ou **PDF** (impression du navigateur).

Tout est sauvegardé automatiquement dans SQLite ; « Mes brouillons » permet de
reprendre un document.

**Raccourcis :** `Ctrl/⌘ + Entrée` met en forme · `Ctrl/⌘ + S` sauvegarde ·
`Échap` ferme les fenêtres.

### La dictée

Quatre boutons, dans cet ordre :

| Bouton | Effet |
|---|---|
| **Enregistrer** | Ouvre le micro et la session de dictée |
| **Pause** ⏸ | Suspend et reprend, sans rien envoyer ni perdre |
| **Terminer** ⏹ | Transcrit le reliquat et conclut la dictée |
| **Arrêter** ✕ | Abandonne : l'enregistrement est **supprimé**, sans transcription |

« Arrêter » ne défait pas ce qui a déjà été transcrit — ce texte est dans la
transcription, à vider à la main si on n'en veut pas. Le bouton n'apparaît que
pendant l'enregistrement, où il prend la place de celui d'import.

#### Ce qui se passe pendant que vous parlez

```
micro ──▶ fragment de 5 s ──▶ copie locale (IndexedDB)
                            └▶ serveur : fichier audio de la session
                                            │
                                            ├─▶ tranche de ~30 s (ffmpeg)
                                            └─▶ Google STT ─▶ brouillon
```

La dictée n'attend plus « Terminer » pour quitter le navigateur : elle part
par fragments de cinq secondes, et le serveur la transcrit par tranches d'une
trentaine de secondes qui s'affichent au fil de l'eau. Trois conséquences :

- **une coupure ne coûte plus la consultation.** Le serveur détient l'audio à
  quelques secondes près, et le brouillon reçoit chaque tranche de texte dès
  qu'elle est prête ;
- **un envoi raté se rejoue.** Tant que la dictée n'est pas conclue, le
  navigateur garde sa propre copie de l'audio. Une ligne sous les boutons
  indique combien de fragments attendent, et la file se vide seule au retour
  du réseau ;
- **le texte n'arrive plus d'un bloc à la fin**, ce qui permet de vérifier
  que le micro capte réellement la bonne chose.

Si le serveur est injoignable au moment d'appuyer sur « Enregistrer », la
dictée se fait quand même : tout est conservé dans le navigateur et envoyé à
« Terminer ».

Les tranches ne sont **pas coupées à la seconde fixe** : ffmpeg cherche un
silence autour de la trentième seconde (`silencedetect`) et coupe là. Couper au
milieu d'un mot le rendrait inintelligible des deux côtés de la frontière, ce
qu'une note médicale ne peut pas se permettre. Le curseur n'avance que de la
durée réellement mesurée sur la tranche produite : aucune dérive ne peut faire
sauter un passage.

#### Dictée interrompue

Un onglet fermé en pleine consultation — ou une application iOS tuée en
arrière-plan — laisse la dictée en plan. Au chargement suivant, une bannière
ambre la propose :

- **Reprendre et transcrire** : complète le serveur avec les fragments qui lui
  manquent, conclut la dictée et ouvre la consultation d'origine. Le texte
  déjà transcrit n'est pas redemandé ;
- **Télécharger l'audio** : récupère l'enregistrement complet en fichier, pour
  le garder ou l'importer ailleurs ;
- **Supprimer**.

Le serveur conserve une dictée inachevée pendant `DICTATION_RETENTION_HOURS`
(72 h par défaut), puis la purge au démarrage suivant. Une dictée conclue est
effacée immédiatement : le brouillon porte désormais le texte, garder l'audio
d'une consultation ne ferait qu'ajouter un risque.

### Métadonnées de la consultation

Le repli **Métadonnées de la consultation** contient le nom du patient, son
numéro de dossier, la date, la raison de consultation, le demandeur et
l'accompagnateur. **Ces champs n'ont pas à être saisis :** ils sont relus dans
la dictée par Gemini juste après la mise en forme (`app/llm.py →
extract_metadata`, un appel distinct de celui qui produit la note), puis
affichés pour vérification.

Ils remplissent deux rôles :

- ils alimentent les `{{PLACEHOLDERS}}` de l'en-tête du document ;
- ils identifient la consultation dans **Mes brouillons** — c'est le seul
  moyen d'y reconnaître un document, la liste n'affichant aucun extrait du
  contenu clinique.

> Une valeur saisie à la main fait toujours autorité : l'extraction ne
> complète que les champs restés vides, y compris après une régénération. Un
> numéro de dossier vérifié à l'écran ne sera jamais remplacé par ce que le
> moteur de reconnaissance vocale a cru entendre.

La **scolarité** ne figure pas ici : ce n'est pas un élément d'identification
mais une donnée clinique, qui appartient à l'histoire sociale de la note (et
qui conditionne l'interprétation du MoCA).

### Mes brouillons

La liste est **groupée par jour de dictée**, du plus récent au plus ancien, et
chaque groupe est trié de l'heure la plus récente à la plus ancienne. Une
ligne porte l'heure, le nom, le numéro de dossier, la raison de consultation,
le gabarit et l'état.

Le tri repose sur la date de **création** et non de dernière modification :
rouvrir un vieux dossier pour y corriger une virgule ne doit pas le faire
remonter au-dessus de la consultation du matin même.

### Enregistrements conservés

L'audio d'une consultation **reste attaché à son brouillon**, réécoutable dans
le repli « Enregistrements » sous les boutons de dictée. La reconnaissance
vocale se trompe, et généralement sur ce qui compte — une posologie, un score,
un nom de molécule. Réécouter le passage est le seul moyen de trancher sans
refaire l'entrevue.

Les fichiers importés y figurent au même titre que les dictées. Chacun peut
être écouté, téléchargé ou supprimé individuellement.

> **Supprimer le brouillon efface tout** : la transcription, la note **et**
> les enregistrements audio, fichiers compris. C'est la contrepartie de leur
> conservation — un seul geste doit suffire à ne rien laisser derrière.

---

## 5 bis. Panneau d'administration (« Réglages »)

Bouton **Réglages** dans l'en-tête, visible pour les comptes listés dans
`TEMPLATE_ADMINS` (par défaut : tous les usagers autorisés).

Ces réglages sont enregistrés en base et **surchargent le fichier `.env`** :
ils prennent effet immédiatement, sans `docker compose up --build`. Vider un
champ supprime la surcharge et le réglage revient à ce que dit le `.env` —
c'est la façon la plus simple de revenir en arrière. Chaque champ indique sa
provenance : `panneau` ou `.env`.

> **Ce qui n'y est pas :** rien de ce qui gouverne l'accès. `AUTHORIZED_USERS`,
> `TRUSTED_PROXIES` et `TEMPLATE_ADMINS` restent dans le `.env`, hors d'atteinte
> du navigateur. Un panneau accessible en ligne ne doit pas pouvoir élargir la
> liste des personnes autorisées à lire les consultations.

> **La langue n'est pas ici.** C'est une préférence personnelle, choisie dans
> le menu de la pastille d'identité — voir § 5 quater.

### Reconnaissance vocale

Quatre services au choix. Le changement ne touche que la dernière étape : le
transcodage en OGG/Opus, le découpage en tranches de trente secondes et le
lexique d'adaptation leur sont communs. Basculer en cours d'usage est sans
effet sur les dictées déjà transcrites.

| Service | Adaptation au vocabulaire | Terminologie clinique |
|---|---|---|
| **Google Speech-to-Text** | `speech_contexts`, ~300 expressions | aucun modèle médical francophone (`medical_dictation` est anglophone) |
| **Deepgram** | `keywords`, **nova-2 seulement** | aucun |
| **AssemblyAI** | `keyterms_prompt`, 1000 termes | **module « medical-v1 », français pris en charge** |
| **Soniox** | `context.terms`, 60 termes | aucun, mais multilingue par conception |

> Le lexique d'adaptation livré avec l'application est **francophone** : il n'est
> pas envoyé en mode anglais. Voir § 5 quater.

#### AssemblyAI et son module médical

C'est le seul des trois à proposer un modèle spécialisé en terminologie
clinique qui **couvre le français**. Le module `medical-v1` (« Medical Mode »)
vise précisément ce que les deux autres ratent le plus ici : noms de
médicaments, procédures, diagnostics et posologies. Il est activé par défaut
dans le panneau et fonctionne en pré-enregistré, donc avec le découpage en
tranches de ConsultAI.

| Réglage | Remarque |
|---|---|
| Clé API | `assemblyai.com` → Dashboard → API Keys |
| Modèle | `universal-3-5-pro` (défaut) ou `universal-2`. Le premier reconnaît explicitement le **français québécois** et accepte 1000 termes d'adaptation, contre 200 pour le second |
| Langue | `fr` — AssemblyAI ne demande pas de code de dialecte, `fr` couvre le québécois. Vide = détection automatique |
| Mode médical | `medical-v1`, activé par défaut |

À savoir :

- **Facturation supplémentaire.** Le module est un supplément (~0,15 $US/h
  au tarif public, au-dessus du prix du modèle). Sur une langue non prise en
  charge, AssemblyAI l'ignore et le signale — les journaux du conteneur
  reprennent l'avertissement — sans le facturer.
- **Trois allers-retours au lieu d'un.** L'API n'est pas synchrone :
  téléversement, création de la tâche, puis interrogation jusqu'à la fin.
  Quelques secondes de plus par tranche, invisibles en pratique puisque le
  découpage tourne en tâche de fond pendant que le médecin continue de parler.
- Les expressions du lexique de plus de six mots sont écartées : l'API les
  refuse. Ce sont des phrases entières, que l'adaptation n'aide pas.

#### Retrait des longues pauses

Les trois services facturent **à la durée d'audio** : les pauses d'une
consultation — le médecin examine le patient, le patient cherche ses mots —
sont payées plein tarif pour rien. ConsultAI les plafonne avant l'envoi.

| Réglage (panneau) | Défaut |
|---|---|
| Retirer les longues pauses | **Activé** |
| Pause conservée | 0,5 s |

Ce n'est **pas** une suppression du silence, c'est un plafonnement. Toute pause
plus courte que le réglage est conservée telle quelle ; les plus longues sont
ramenées à cette durée. La raison est clinique : les moteurs se servent des
pauses pour placer la ponctuation et séparer les phrases. Tout supprimer
transformerait « *arrêter le lisinopril. Débuter l'amlodipine* » en une seule
phrase — sur une liste de médicaments, ce n'est pas un détail de mise en forme.
**Ne mettez pas 0.**

Une tranche où personne n'a parlé est réduite à rien : l'appel au service n'est
alors pas émis du tout.

> **Seule la copie envoyée est raccourcie.** L'enregistrement conservé avec le
> brouillon garde sa chronologie intacte — il sert à réécouter un passage dont
> on doute, une bande trafiquée le rendrait inutilisable. Et la durée affichée
> reste celle de la dictée réelle.

Sur un fichier de test contenant 40 s de parole et 60 s de silence, l'audio
facturé passe de 100 s à 46 s. Le gain réel dépend entièrement de la proportion
de silence de vos dictées — les journaux du conteneur l'indiquent tranche par
tranche :

```
Silences retirés : 34.5 s → 18.1 s envoyées (48 % de moins)
```

Le seuil de détection (`STT_SILENCE_THRESHOLD_DB`, −40 dB) reste dans le
`.env` : remontez-le vers −32 si le local est bruyant, descendez-le pour être
plus prudent.

#### Deepgram

| Réglage | Remarque |
|---|---|
| Clé API | `console.deepgram.com` → API Keys |
| Modèle | `nova-2` recommandé en français canadien |
| Langue | **laisser vide** : suit la langue de l'application (§ 5 quater) |

> **Modèle Deepgram :** l'adaptation par mots-clés (le lexique du réseau de la
> santé québécois — CHSLD, CIUSSS, les échelles cliniques, les molécules)
> passe par le paramètre `keywords`, qui n'existe plus sur nova-3. Ce dernier
> lui substitue `keyterm`, réservé à l'anglais. En français, l'adaptation n'est
> donc réellement disponible que sur **nova-2**, d'où la recommandation.

### Modèle de langage

| Réglage | Remarque |
|---|---|
| Fournisseur | **Google Gemini**, **Anthropic Claude** ou **OpenAI** |
| Modèle | Nom exact. Le bouton **Modèles disponibles** interroge le fournisseur avec la clé configurée et remplit la liste de suggestions du champ |
| Modèle rapide | Sert uniquement à la relecture des métadonnées (voir § Métadonnées). Tâche triviale payée au jeton : inutile d'y mettre un modèle « pro ». Vide = même modèle que ci-dessus |
| Température | 0 = déterministe. Au-delà de 0,4 le modèle brode, ce qui n'a pas sa place dans une note clinique |
| Clés API | Une par fournisseur. Seul celui qui est sélectionné a besoin de la sienne |

Les clés **ne ressortent jamais du serveur** : le panneau n'en affiche que les
quatre derniers caractères, assez pour vérifier qu'on a collé la bonne, inutile
pour s'en servir. Laisser le champ vide conserve la clé en place ; le bouton
**Effacer** la supprime.

Le mode **Vertex AI** de Gemini (traitement en région de Montréal) reste piloté
par le `.env` : il s'active dès qu'aucune clé Gemini n'est configurée et que
`GOOGLE_CLOUD_PROJECT` est renseigné.

### Consigne générale

Un texte libre ajouté aux consignes de **tous** les gabarits, appliqué quel que
soit le modèle choisi. Il sert aux préférences durables du médecin :

```
Toujours employer le vouvoiement.
Ne jamais abréger les noms de molécules.
Exprimer toutes les doses en milligrammes.
```

L'ordre des consignes est : garde-fous de l'application (non modifiables) →
consignes du gabarit → consigne générale. **Elle passe en dernier, donc elle
l'emporte** en cas de contradiction avec un gabarit qu'on n'a pas pensé à
mettre à jour.

---

## 5 ter. Utilisation sur mobile et installation (PWA)

L'application est une **PWA** : elle s'installe sur l'écran d'accueil et
s'ouvre en plein écran, sans barre d'adresse.

| Appareil | Installation |
|---|---|
| **iPhone / iPad** | Ouvrir l'adresse dans **Safari** (Chrome iOS ne sait pas installer), bouton *Partager* → **Sur l'écran d'accueil** |
| **Android** | Chrome → menu ⋮ → **Installer l'application** (ou la bannière proposée automatiquement) |
| **Bureau** | Chrome/Edge → icône d'installation dans la barre d'adresse |

Adaptations spécifiques au mobile :

- **Un panneau à la fois**, avec un sélecteur *Dictée / Note structurée* :
  les deux colonnes du bureau ne sont lisibles que sur grand écran. Après la
  mise en forme, l'application bascule automatiquement sur la note.
- **Barre d'action basse** avec « Mettre en forme » toujours accessible, même
  depuis l'onglet Note, et respectant la barre d'accueil de l'iPhone.
- **Bouton d'enregistrement large**, commandes Pause / Terminer / Importer en
  cibles tactiles de 44 px minimum.
- **Verrou d'écran** (*Wake Lock*) actif pendant la dictée : sans lui, la mise
  en veille du téléphone suspend l'onglet et interrompt l'enregistrement. Le
  verrou est repris automatiquement au retour dans l'application.
- **Champs à 16 px** sur mobile : en dessous, Safari iOS zoome
  automatiquement à chaque focus et décale la mise en page.
- **Marges de sécurité** (encoche, barre d'accueil) prises en compte.

### Ce que le service worker met — et ne met pas — en cache

Il ne met en cache **que** `/static/*`, les icônes et les bibliothèques CDN.
Il ne touche **jamais** à la page `/` ni aux appels `/api/*` : ces réponses
contiennent des renseignements de santé et resteraient lisibles sur l'appareil
après la déconnexion, en plus de court-circuiter la vérification
d'autorisation faite à chaque requête. **Ne modifiez pas cette règle** — elle
est commentée en tête de `app/static/sw.js`.

Conséquence assumée : l'application **ne fonctionne pas hors ligne**. C'est
sans incidence pratique, la transcription et la mise en forme nécessitant de
toute façon les API Google.

Le manifeste, les icônes et `/sw.js` sont volontairement **publics** (voir
`app/main.py`) : le navigateur les récupère sans cookies, et les protéger
ferait échouer l'installation sans message d'erreur. Ils ne contiennent que le
nom de l'application, ses couleurs et du code de mise en cache.

### Icônes

Le motif retenu est une **page repliée portant une onde vocale** : il dit à la
fois la dictée et le document clinique, là où une onde seule évoquerait une
application de musique et un micro seul un simple dictaphone.

```bash
python3 tools/make_icons.py             # jeu complet (design par défaut)
python3 tools/make_icons.py --preview   # une vignette par design, dans tools/previews/
python3 tools/make_icons.py --design onde   # bascule sur un autre motif
```

Motifs disponibles : `note-pli` (retenu), `note`, `dictee`, `onde`, `micro`,
`pouls`. Changez `DEFAULT_DESIGN` en tête du script pour figer un autre choix.

Le script n'a **aucune dépendance** : les PNG sont encodés via `zlib` et le
dessin repose sur des champs de distance signés. Ni Pillow ni ImageMagick, dont
le décodeur SVG est désactivé par la politique de sécurité de DSM.

Deux points de conception :

- Le **favicon 32 px** utilise volontairement le motif `onde`, plus gras : à
  cette taille, les barres de l'onde dans la page ne feraient qu'un pixel de
  large et le dessin virerait à la tache blanche.
- Le **logo de l'en-tête** (`index.html`) reprend la même onde en SVG inline,
  pour la même raison de lisibilité à 20 px.

> Après toute modification des icônes, incrémentez `VERSION` dans
> `app/static/sw.js` : sinon les appareils ayant déjà installé l'application
> continueront d'afficher les anciennes depuis leur cache.

> **Mise à jour de l'interface :** après avoir modifié `app.js` ou le HTML,
> incrémentez `VERSION` dans `app/static/sw.js`. Sans cela, les appareils
> ayant déjà installé l'application continueront de servir l'ancien
> JavaScript depuis leur cache.

---

## 5 quater. Langue : français ou anglais

**Menu de la pastille d'identité, en haut à droite → Langue.** Chacun choisit la
sienne ; le choix est enregistré sous son identité et ne touche personne
d'autre. `APP_LANGUAGE` dans le `.env` (`fr` par défaut) ne sert qu'aux usagers
qui n'ont jamais choisi.

> **Pourquoi pas dans le panneau d'administration ?** Parce que ce panneau est
> réservé aux administrateurs. La langue, elle, regarde la personne qui lit
> l'écran : un usager ordinaire doit pouvoir changer la sienne, et ne doit pas
> pouvoir changer celle des autres. Deux médecins partageant l'installation
> travaillent donc l'un en français, l'autre en anglais.

> **Pourquoi en base et non dans un témoin de session ?** Parce que Pangolin
> retire l'en-tête `Cookie` des requêtes qu'il relaie au conteneur : le serveur
> ne verrait jamais la préférence. Elle est donc rangée dans la table
> `user_preferences`, sous l'identité que Pangolin transmet, elle.

Ce n'est pas qu'un habillage : la langue traverse toute la chaîne :

| Ce qui suit la langue | Détail |
|---|---|
| L'interface | Boutons, menus, messages d'erreur, panneau d'administration, page de refus d'accès, manifeste de l'application installée |
| Le service vocal | `fr` → `fr-CA` (Google, Deepgram) et `fr` (AssemblyAI, Soniox) ; `en` → `en-CA` / `en` |
| Le lexique d'adaptation intégré | Il est **francophone** : en mode anglais il n'est **pas** envoyé au moteur (voir ci-dessous) |
| La note produite | Les consignes de base existent en deux versions ; le modèle rédige en français québécois ou en anglais canadien |
| L'extraction des métadonnées | Même invite, dans la langue courante |

Le changement prend effet **immédiatement**, sans reconstruction de l'image. La
page se recharge d'elle-même, l'interface étant rendue par le serveur. Une
dictée déjà lancée conserve la langue dans laquelle elle a commencé, jusqu'à sa
transcription : le changement en cours de dictée est refusé plutôt que de
produire une transcription à cheval sur deux langues.

### Ce que la langue ne change pas : vos gabarits

Un gabarit appartient au médecin qui l'a écrit et **n'est jamais réécrit ni
traduit**. Conséquence à connaître : passer l'interface en anglais avec des
gabarits rédigés en français donne une note dont **les titres de rubriques
restent français** et le corps devient anglais. Ce n'est pas un défaut — les
consignes de base exigent de reproduire *exactement* la structure fournie, et
cette exigence l'emporte volontairement sur la langue de rédaction : un titre
de rubrique inventé serait bien plus gênant qu'un titre dans l'autre langue.

Pour une note entièrement anglaise, dupliquez le gabarit et traduisez-en les
titres (**Gérer les gabarits → Dupliquer**). Idem pour la consigne générale du
panneau d'administration, qui est également recopiée telle quelle.

### Le lexique intégré est francophone

Les ~320 expressions d'adaptation livrées avec l'application (acronymes du
réseau de la santé québécois, échelles cliniques, molécules souvent mal
entendues) sont des termes **français**. En mode anglais, les envoyer ne pourrait
rien améliorer et pousserait le moteur vers des mots qui ne seront pas
prononcés : ils sont donc **omis**.

Le champ **Vocabulaire additionnel** d'un gabarit, lui, est toujours transmis —
c'est vous qui l'écrivez, vous savez dans quelle langue vous dictez. En mode
anglais, il devient donc la seule source de vocabulaire du moteur, et le texte
d'aide sous le champ le rappelle.

### Forcer un code de langue

Les trois champs **Langue …** du panneau (Deepgram, AssemblyAI, Soniox) et la
variable `STT_LANGUAGE_CODE` du `.env` acceptent trois états :

| Valeur | Effet |
|---|---|
| **vide** (recommandé) | Suit la langue de l'application |
| `auto` | Détection automatique par le service. Utile pour une consultation qui alterne deux langues — Soniox est multilingue par conception |
| `fr-CA`, `en-GB`, … | Forçage explicite. **Survit au changement de langue** de l'application : à réserver à l'épinglage d'un dialecte précis |

> Un `STT_LANGUAGE_CODE=fr-CA` laissé dans le `.env` d'une installation
> antérieure figerait le français même après un passage de l'interface à
> l'anglais. Videz-le.

---

## 6. Gabarits

C'est **ici, et nulle part ailleurs**, que vit ce qui est propre à une pratique.
L'application ne connaît aucune spécialité : elle sait dicter, transcrire et
mettre en forme. Les gabarits fournis ci-dessous sont donc des **exemples**,
rédigés pour une pratique gériatrique québécoise — renommez-les, réécrivez-les
ou supprimez-les selon la vôtre.

> Un gabarit préchargé que vous n'avez jamais enregistré depuis l'éditeur peut
> être mis à jour par une future version de l'application. Dès que vous le
> modifiez une fois, il vous appartient et n'est plus jamais écrasé.

Trois gabarits sont préchargés au premier démarrage :

1. **Évaluation gériatrique standard** — HMA, antécédents, syndromes
   gériatriques (chutes, AVQ/AVD, cognition, humeur, nutrition, continence),
   examen physique, impression, plan, niveau de soins.
2. **Bilan cognitif / Clinique de mémoire** — histoire cognitive et
   hétéro-anamnèse distinguées, impact fonctionnel, SCPD, orientation
   étiologique, tests objectifs (MoCA/MMSE), aptitude, SAAQ, hébergement.
3. **Révision de la pharmacothérapie** — bilan comparatif, critères de
   Beers/STOPP-START, charge anticholinergique, cascades médicamenteuses,
   plan de déprescription.

Chaque gabarit comporte quatre parties, modifiables via **Gérer les gabarits** :

| Champ | Rôle |
|---|---|
| **Instructions cliniques** | Ce sur quoi Gemini doit se concentrer : éléments à chercher dans la dictée, distinctions à faire, pièges à éviter |
| **Mise en page** | Le squelette Markdown : titres exacts, ordre, tableaux. Accepte `{{PATIENT}}`, `{{DOSSIER}}`, `{{DATE}}`, `{{DEMANDEUR}}`, `{{ACCOMPAGNATEUR}}` ; une ligne dont le champ reste inconnu est retirée du document |
| **Vocabulaire** | Termes ajoutés au lexique de reconnaissance vocale pour ce type de consultation |
| **Ordre** | Position dans le menu déroulant |

Le bouton **Dupliquer** crée une copie modifiable du gabarit ouvert (`Nom
(copie)`, suffixe incrémenté si besoin). C'est la façon normale d'en créer un :
partir d'un gabarit éprouvé et en ajuster une rubrique, plutôt que de réécrire
depuis zéro plusieurs dizaines de lignes d'instructions cliniques. Une copie
n'est jamais marquée « préchargé » et n'est donc jamais réécrite au démarrage.

Les instructions anti-hallucination communes (interdiction d'inventer une
dose, un score ou un antécédent ; `« Non abordé lors de la dictée. »` pour une
rubrique vide ; `[à vérifier]` en cas de doute) sont dans
`app/llm.py → BASE_SYSTEM_PROMPT` et s'appliquent à **tous** les gabarits.
Modifiez-les là si nécessaire, pas dans chaque gabarit.

> Un gabarit préchargé supprimé est recréé au redémarrage du conteneur.
> Renommez-le ou modifiez-le plutôt que de le supprimer.

> Tant qu'un gabarit préchargé n'a **jamais** été enregistré depuis l'éditeur,
> il est remis à jour au démarrage si la version livrée avec l'application a
> changé (`database.refresh_default_templates`). Dès la première modification,
> il vous appartient et n'est plus jamais touché. Pour figer un gabarit livré
> tel quel, dupliquez-le ou enregistrez-le une fois sans rien changer.

---

## 7. Dépannage

| Symptôme | Cause et correctif |
|---|---|
| **Bad Gateway** dans Pangolin | Pangolin n'atteint pas le conteneur. Vérifiez que `BIND_ADDRESS` est une adresse du NAS joignable depuis Pangolin : avec `127.0.0.1`, seul le NAS lui-même peut se connecter, jamais un proxy distant. Test : depuis la machine Pangolin, `curl -m5 http://192.168.20.50:8787/healthz` doit renvoyer du JSON. |
| **403 sur toutes les requêtes** après avoir mis l'IP de Pangolin dans `TRUSTED_PROXIES` | Le proxy userland Synology masque l'IP source. Mettez la passerelle Docker (`172.27.0.1/32`). L'IP réellement vue est journalisée : `docker compose logs consultai \| grep "IP paire"`. |
| `sqlite3.OperationalError: unable to open database file` | `APP_UID`/`APP_GID` ne correspondent pas au propriétaire de `./data`. Les ACL Synology refusent l'écriture aux UID inconnus **même avec des permissions 777**. Faites `id votre_utilisateur`, corrigez `.env`, puis `docker compose up -d --build`. |
| `403 — Accès refusé : requête reçue en dehors du proxy de confiance` | L'IP du pair n'est pas dans `TRUSTED_PROXIES`. L'IP réelle est indiquée dans les journaux : `docker compose logs consultai \| grep "IP paire"`. |
| `403 — aucune identité transmise par le SSO` | Pangolin n'envoie pas l'en-tête attendu. Vérifiez `SSO_HEADER_KEY` ou ajoutez le bon nom à `SSO_HEADER_FALLBACKS`. |
| Le bouton micro ne fait rien | `getUserMedia` exige HTTPS. Passez par Pangolin (l'accès direct en `http://ip:8787` est bloqué par le navigateur). |
| Micro refusé dans l'app installée sur iPhone | Bogue connu de certaines versions d'iOS en mode « écran d'accueil ». L'application le détecte et le signale : dictez depuis Safari, le reste fonctionne normalement dans l'app installée. |
| L'option « Installer / Sur l'écran d'accueil » n'apparaît pas | Exige HTTPS (donc Pangolin) et, sur iOS, **Safari** exclusivement. Vérifiez que `/static/manifest.webmanifest` et `/sw.js` répondent 200 **sans** en-tête d'authentification. |
| Modification de l'interface non visible sur mobile | Le service worker sert l'ancienne version. Incrémentez `VERSION` dans `app/static/sw.js` et reconstruisez l'image. |
| L'enregistrement s'arrête quand l'écran s'éteint | Le verrou d'écran n'est pas supporté par ce navigateur. Gardez l'application au premier plan pendant la dictée. |
| `Le modèle « … » est introuvable pour ce compte` | Ouvrez `/api/models`, choisissez un identifiant de la liste, reportez-le dans `GEMINI_MODEL`. |
| `Enregistrement trop long pour un envoi direct` | Au-delà d'environ 55 minutes de dictée. Configurez `STT_GCS_BUCKET`, ou dictez en plusieurs parties (les transcriptions se concatènent). |
| Erreur 413 côté proxy sur un long enregistrement | Augmentez la taille de corps autorisée dans Pangolin/Traefik (l'équivalent de `client_max_body_size 200M`). |
| La note est coupée à la fin | Augmentez `GEMINI_MAX_OUTPUT_TOKENS`. L'interface affiche un avertissement dans ce cas. |
| Acronymes mal transcrits | Ajoutez-les au champ **Vocabulaire** du gabarit, ou au lexique global dans `app/stt.py`. |

Diagnostic rapide :

```bash
docker compose logs -f consultai          # journaux applicatifs
curl -s http://127.0.0.1:8787/healthz     # état + avertissements de configuration
```

---

## 8. Sauvegarde

Toutes les données tiennent dans `./data/consultai.db` (gabarits + brouillons).

```bash
# Sauvegarde à chaud, sûre même pendant l'utilisation (mode WAL)
docker compose exec consultai python -c \
  "import sqlite3; s=sqlite3.connect('/data/consultai.db'); \
   d=sqlite3.connect('/data/sauvegarde.db'); s.backup(d); d.close(); s.close()"
```

Incluez `/volume1/docker/ConsultAI/data` dans Hyper Backup. **N'incluez jamais
`./secrets` ni `.env` dans une sauvegarde non chiffrée.**

---

## 9. Confidentialité

- ⚠️ **Les enregistrements audio sont conservés** avec leur brouillon, sous
  `AUDIO_DIR` (`./data/audio`). C'est la donnée la plus sensible que produise
  l'application : la voix du patient, non anonymisable. Elle disparaît quand
  on supprime le brouillon — fichier compris — et de nulle autre façon.
  Purgez les brouillons que vous n'avez plus à conserver.
- Pendant la dictée, deux copies temporaires existent en plus, toutes deux
  effacées à la conclusion :
  - sur le NAS, sous `DICTATION_DIR` (`./data/dictations`) — c'est ce qui
    permet de survivre à une coupure. Une dictée jamais conclue y reste
    `DICTATION_RETENTION_HOURS` puis est purgée ;
  - dans le navigateur (IndexedDB), pour pouvoir rejouer un envoi raté.
    « Terminer » avec succès, « Arrêter » et « Supprimer » l'effacent.
- Tout cela vit sous `./data`, au même titre que la base : **c'est ce dossier
  qu'il faut placer sur un partage chiffré** si votre analyse de risque
  l'exige, et c'est lui qu'il faut sauvegarder — et protéger.
- Les fichiers intermédiaires (transcodage, découpage des tranches) sont
  écrits dans un dossier temporaire immédiatement supprimé. Si
  `STT_GCS_BUCKET` est utilisé, l'objet est supprimé dès la fin de la
  transcription.
- La transcription et la note sont stockées **en clair** dans SQLite. Placez
  `./data` sur un dossier partagé chiffré du NAS si votre analyse de risque
  l'exige.
- Le service de reconnaissance vocale **et** le fournisseur de modèle traitent
  des renseignements de santé. Le panneau d'administration permet de changer
  l'un et l'autre en deux clics : **chaque changement est une décision de
  conformité**, pas un simple réglage. Validez chaque fournisseur auprès de
  votre responsable de la protection des renseignements personnels, signez les
  ententes appropriées, et privilégiez le mode Vertex AI en région
  `northamerica-northeast1` — le seul de la liste qui garde le traitement au
  Québec.
- Préférez une identification indirecte du patient (initiales, numéro de
  dossier) plutôt qu'un nom complet dans le champ prévu.
- Le contenu généré doit **toujours** être relu par le clinicien avant d'être
  versé au dossier médical.

### Fonctionnement hors ligne (facultatif)

L'interface charge Tailwind, `marked` et `DOMPurify` depuis un CDN public
(aucune donnée clinique n'y transite). Pour un fonctionnement totalement
autonome :

```bash
mkdir -p app/static/vendor && cd app/static/vendor
curl -LO https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js
curl -LO https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js
curl -Lo tailwind.js "https://cdn.tailwindcss.com?plugins=forms,typography"
```

Puis remplacez les trois `<script src="https://…">` d'`app/templates/index.html`
par `/static/vendor/…` et reconstruisez l'image.
