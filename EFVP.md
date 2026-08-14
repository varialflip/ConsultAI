# Évaluation des facteurs relatifs à la vie privée (ÉFVP)

**Système** : ConsultAI (DictAI.ca) — dictée et rédaction de notes de consultations cliniques
**Version du document** : 1.1
**Date** : 2026-08-14
**Base légale** : *Loi sur la protection des renseignements personnels dans le secteur privé* (RLRQ, c. P-39.1), notamment ses articles 3.1 à 3.5 (Loi 25).

---

## 1. Identification du responsable et des parties prenantes

| Rôle | Personne / entité |
|---|---|
| Responsable de la protection des renseignements personnels | Dr Frederick Duong, médecin |
| Personne en charge du suivi | Dr Frederick Duong |
| Titulaire des renseignements | Les patients dont les consultations sont dictées |
| Utilisateurs du système | Médecins et cliniciennes de la pratique (actuellement : `frederick.duong`, `genevieve.belanger`) |
| Fournisseurs de services (tiers) | Alphabet (Google Cloud — Vertex AI, région `northamerica-northeast1`, Montréal), Pocket ID (auto-hébergé), SMTP2GO (courriels), Cloudflare (Turnstile), GitHub Container Registry (distribution de l'image) |

---

## 2. Description du système d'information

### 2.1 Généralités

ConsultAI est une application web auto-hébergée qui permet à un clinicien de dicter
une consultation à l'aide d'un microphone, d'en obtenir la transcription puis la mise
en forme structurée en note clinique par un modèle de langage, de relire et de
corriger la note, puis de l'exporter.

Déploiement en production (2026-08-14) :

| Élément | Valeur |
|---|---|
| Infrastructure | VM Oracle Cloud (OCI), hébergement auto-géré |
| Emplacement | /opt/dictai — pile Docker Compose (Caddy, consultai, pocket-id, pocket-id-loki, crowdsec, turnstile-gate) |
| Accès public | `app.dictai.ca` / `app.loki.casa` (HTTPS TLS) |
| Fournisseur d'identité | Pocket ID, auto-hébergé : `login.dictai.ca` et `login.loki.casa` (2 instances) |
| Modèle de langage | Gemini via Vertex AI — région `northamerica-northeast1` (Montréal, Québec) |
| Reconnaissance vocale | Traitement direct de l'audio par le modèle (Vertex AI, Montréal) — aucun envoi à un service STT externe |
| Base de données | SQLite (`/data/consultai.db`), WAL |

### 2.2 Environnement technique

- Image conteneurisée `ghcr.io/varialflip/consultai`, UID/GID 1000, un seul worker uvicorn.
- Volume de données persistantes `/opt/dictai/data/consultai` : base SQLite, audio,
  dictées en cours, sauvegardes.
- Reverse proxy Caddy avec TLS (Let's Encrypt), réponses **403** aux chemins de
  scan connus (`.env`, `.git`, `.aws`, `wp-*`, …).
- CrowdSec (détection/bannissement d'IP), règles géographiques **fail-closed** :
  seules les adresses IP canadiennes explicites passent.
- Secrets et clés hors image : `/etc/dictai/.env`, `/etc/dictai/secrets`.

---

## 3. Renseignements personnels traités

### 3.1 Catégories

| Catégorie | Nature | Sensibilité |
|---|---|---|
| **Renseignements de santé** | Contenu des consultations : anamnèse, motifs, examens, diagnostics, plans de traitement (champs `reason`, `raw_transcript`, `generated_markdown`, `edited_markdown`) | Très élevée |
| **Identité des patients** | **Non collectée** : le nom (`patient_name`) et le numéro de dossier (`patient_ref`) ne sont plus saisis ni stockés (dénominalisation, § 7.7). Date de consultation, demandeur (`requester`) et accompagnateur (`accompanied_by`) restent conservés | Très élevée (ce qui reste) |
| **Voix / enregistrements audio** | Enregistrement brut de la dictée (fichiers sous `/data/audio/<consultation_id>/`) | Très élevée — voix non anonymisable, inclut la voix du patient et toute personne présente dans la pièce |
| **Identité des utilisateurs** | `username`, `email`, `display_name`, `avatar_url`, groupes, dates de connexion | Élevée (données d'identité de professionnels de la santé) |
| **Données d'usage** | `usage_events` : durées audio, fournisseur/modèle utilisés, tokens, coûts, horodatages | Faible à moyenne (révèle l'activité clinique) |
| **Données de facturation d'usage** | `pricing_rates`, `usage_daily` | Faible |
| **Préférences** | Langue d'interface, gabarits personnels | Faible |
| **Données de supervision** | Journaux réseau Caddy (adresse IP, hôte demandé, chemin URI, code), journaux CrowdSec (IP, ASN, scénarios déclenchés), rapports quotidiens/hebdo dans `/opt/dictai/threat_reports/` | Moyenne — les adresses IP des usagers légitimes apparaissent dans les journaux ; le contenu clinique en est exclu (voir § 3.3, finalité 7 et § 7.6) |

### 3.2 Sources

- Collecte directe auprès des cliniciens : dictée orale (audio), saisie manuelle
  des métadonnées non identifiantes (date, raison, demandeur, accompagnateur).
  Le nom et le numéro de dossier du patient ne sont **pas** collectés.
- Transmission par le fournisseur OIDC (Pocket ID) : identité (`openid`, `profile`,
  `email`, `groups`).
- Données générées par le système : transcriptions, notes générées, journaux d'usage.
- Données de supervision : journaux générés par Caddy et CrowdSec (adresses IP,
  chemins demandés), agrégés par les scripts de `/opt/dictai/threat_tools/`.

### 3.3 Finalités

1. Transcrire la dictée vocale d'une consultation clinique.
2. Structurer la transcription en note clinique conforme à un gabarit choisi.
3. Permettre la relecture, la correction et l'export de la note (ex. vers le dossier médical).
4. Synchroniser une dictée en cours entre plusieurs appareils du même clinicien.
5. Authentifier les utilisateurs et contrôler les accès (OIDC).
6. Facturer l'usage des fournisseurs et suivre les coûts.
7. Assurer la sécurité et la supervision du système : détection et blocage des
   menaces réseau (CrowdSec), supervision continue des accès et des tentatives
   d'intrusion (voir § 7.6).

---

## 4. Justification de la collecte (nécessité et proportionnalité)

| Collecte | Justification | Nécessité | Proportionnalité |
|---|---|---|---|
| Enregistrement audio | La voix est l'entrée même du service : impossible de dicter sans capturer l'audio. | Oui | Le clinicien choisit le moment et le contexte de la dictée ; l'identification du patient est laissée à l'étape du versement au dossier, en dehors de l'application. |
| Métadonnées de consultation | Date, raison, demandeur, accompagnateur — reconnaître la consultation dans la liste des brouillons. | Oui | L'identité du patient (nom, numéro de dossier) n'est **pas** collectée (dénominalisation) ; seules des métadonnées non identifiantes sont conservées. |
| Transcription et note | Produit du service, conservé pour relecture/export. | Oui | La note doit toujours être relue par le clinicien avant versement au dossier (jamais versée automatiquement). |
| Identité des utilisateurs | Contrôle d'accès et traçabilité. | Oui | Collecte minimale (`username`, courriel, nom, groupes) ; les revendications se limitent aux `scopes` déclarés. |
| Données d'usage | Facturation des fournisseurs (coût par modèle/durée) et supervision. | Oui | Agrégeables ; conservées en base sans nom de patient (liées au propriétaire de la consultation). |
| Données de supervision | Détection des menaces réseau, surveillance continue et traçabilité des accès (CrowdSec, journaux Caddy). | Oui | Minimisées : en-têtes Cookie/Authorization/Set-Cookie et valeurs OIDC (`code`/`state`/`nonce`/`token`) **jamais** transmises aux modèles d'analyse ; rapports agrégés et courts (~30 lignes). |

**Conservation** :

| Donnée | Durée | Mécanisme |
|---|---|---|
| Audio, transcription et note d'une consultation | **12 h par défaut** sans modification (`consultation_retention_hours`, réglable au panneau en heures ; 0 = désactivé) | Purge automatique au démarrage et à l'ouverture de la liste des brouillons, basée sur `updated_at`. Suppression immédiate (fichier compris) à la suppression du brouillon |
| Dictée en cours, jamais conclue | 72 h (`DICTATION_RETENTION_HOURS`) | Purge au démarrage |
| Copies temporaires du navigateur (IndexedDB) | Jusqu'à l'envoi réussi, puis effacées | Automatique |
| Sauvegardes | Rotation automatique quotidienne, `backup_retention_count` (défaut 7) — couverture quotidienne/hebdo/mensuelle | **Sanitisées** : les archives ne contiennent **ni audio, ni données patient** (config, comptes, gabarits et statistiques seulement) |
| Journaux Caddy / CrowdSec | Fenêtre glissante couvrant les dernières 24 h pour l'analyse ; journaux bruts conservés selon le volume | Rotation des journaux |
| Rapports de supervision (`threat_reports/`) | Rapports quotidiens : **7 jours** ; rapports hebdomadaires : conservés | Prune automatique (> 7 j) |

---

## 5. Flux des renseignements personnels

```
[Micro du clinicien]
        │  audio (HTTPS/WSS)
        ▼
[Caddy (TLS) ─ CrowdSec / Turnstile]
        ▼
[ConsultAI — conteneur]
   ├─ SQLite  consultai.db   (transcriptions, notes, métadonnées non identifiantes, usagers, usage)
   ├─ /data/audio/           (enregistrements, fichier par consultation, purge 12 h par défaut)
   ├─ /data/dictations/      (dictées en cours, purge 72 h)
   └─ /data/backups/         (sauvegardes sanitisées : ni audio, ni données patient)
        │  audio + note (Vertex AI, région Montréal)         ← traitement hors périmètre local
        ▼
[Vertex AI — Gemini — northamerica-northeast1]
   (transcription + mise en forme, API Google Cloud)
```

> ℹ️ Les sauvegardes ZIP ne contiennent plus d'audio ni de données cliniques
> (config, comptes, gabarits, statistiques) : une restauration ne ramène pas
> les données patient — les fichiers audio existants sont laissés intacts.

Autres flux :

| Flux | Données | Destination | Résidence |
|---|---|---|---|
| Audio + note → Vertex AI | Audio brut, transcription, note, gabarit | `northamerica-northeast1` (Montréal) | **Québec** |
| OIDC → Pocket ID | Identité, groupes | `login.dictai.ca` / `login.loki.casa` (auto-hébergé) | Locale |
| Courriels (notifications compte) | Courriel, lien | SMTP2GO | Traitement américain (vérifier l'entente) |
| Turnstile (captcha) | Données du navigateur, adresse IP | Cloudflare | Hors Canada (données non cliniques) |
| Image conteneur | — (aucune donnée) | GitHub Container Registry | — |

**Supervision (flux séparé)** :

```
[Caddy / CrowdSec / journaux SSH]
        │  journaux réseau (IP, hôtes, chemins — sans en-têtes d'authentification)
        ▼
[threat_tools/ — agrégation locale (geo.sh, web_window.sh, ssh_window.sh, cs.sh)]
        │  agrégats compactés (~30 lignes), en-têtes masqués
        ▼
[OpenChamber — agent deepseek (API DeepSeek)   ← analyse cloud, données non cliniques]
        ▼
[threat_reports/ (7 j) + ajout ASN à ban-known-bad-asn-ssh.yaml]
```

> ⚠️ **Décision documentée (2026-07-31, confirmée au 2026-08-13)** : l'audio et le
> texte sont envoyés au **modèle Gemini via Vertex AI en région Montréal** — le seul
> fournisseur retenu gardant le traitement au Québec. Le panneau permet de changer
> de fournisseur STT/LLM en deux clics : **chaque changement est une décision de
> conformité** (résidence des données, entente de service) et doit être revalidé
> avant toute bascule.

---

## 6. Évaluation des risques

### 6.1 Grille

| Niveau | Gravité | Vraisemblance |
|---|---|---|
| Élevé (E) | Atteinte grave à la vie privée (renseignements de santé divulgués, voix) | Probable / fréquent |
| Moyen (M) | Atteinte notable, périmètre limité | Possible |
| Faible (F) | Atteinte mineure ou nulle | Improbable |

### 6.2 Registre des risques

| # | Risque | Gravité | Vraisemblance | Niveau |
|---|---|---|---|---|
| R1 | **Accès non autorisé** à l'application ou à la base (mot de passe, session volée, compte compromis) | E | M | **Élevé** |
| R2 | **Divulgation des renseignements de santé** par un tiers de traitement (fournisseur de modèle, courriel) | E | F | Moyen |
| R3 | **Surconservation** de l'audio ou des notes (pas de rétention automatique, sauvegardes longues) | M | M | Moyen |
| R4 | **Erreur humaine** : note générée inexacte ou tronquée versée au dossier sans relecture | E | M | **Élevé** |
| R5 | **Interception en transit** (réseau) | E | F | Moyen |
| R6 | **Perte / vol d'appareil** (PWA : données en IndexedDB, session persistante 30 j) | M | M | Moyen |
| R7 | **Compromission du serveur** (défaillance de la pile, image malveillante, vulnérabilité Caddy/CrowdSec/ConsultAI) | E | F | Moyen |
| R8 | **Traitement hors Québec** suite à une bascule de fournisseur dans le panneau, sans revalidation | E | M | Moyen |
| R9 | **Compte d'usager non désiré** si l'inscription Pocket ID est rouverte (`ALLOW_SIGNUP=true`) | M | F | Faible |

### 6.3 Analyse des menaces principales

- **R1 (accès non autorisé)** — Surface : sessions (cookie `SESSION_HTTPS_ONLY`, durée
  normale 4 h / « rester connecté » 30 j), comptes Pocket ID (passkeys + code), groupes
  `admins`/`users`. Menaces : vol de session, phishing, rejeu. La session est glissante
  (repoussée à chaque requête) — risque d'une session longue sur un poste partagé.
- **R2/R8 (tiers)** — Le traitement est confié à Vertex AI (Montréal) pour l'audio et la
  note. Un changement de fournisseur depuis le panneau d'administration (par ex. vers
  OpenAI, Anthropic, Cohere, Mistral, AssemblyAI, Deepgram, Google STT global, Qwen Omni)
  **déplace le traitement hors Québec** immédiatement et sans revalidation.
- **R4 (erreur humaine)** — La transcription et la mise en forme sont imparfaites ;
  la note peut contenir des mots faux (molécules, acronymes) ou être coupée.
- **R3 (surconservation)** — L'audio est la donnée la plus sensible (voix non
  anonymisable). Une rétention automatique de **12 h** par défaut la borne ;
  une durée plus longue est un choix délibéré (réglage au panneau).

---

## 7. Mesures d'atténuation et de protection

### 7.1 Organisationnelles et de gouvernance

- Responsable RPD nommé : Dr Frederick Duong.
- Consigne explicite : **l'identité du patient (nom, numéro de dossier) n'est pas
  saisie dans l'application** — la note est produite dénominalisée et l'identification
  est rattachée au moment du versement au dossier, hors de l'application (voir § 7.7).
- Consigne explicite : **la note générée doit toujours être relue** avant versement au
  dossier (aucun versement automatique).
- Les fournisseurs STT/LLM sont validés avant mise en service ; tout changement depuis
  le panneau est revalidé (cf. § 5).
- Sauvegardes régulières **sanitisées** (ni audio, ni données patient — voir § 7.5) ;
  `.env` et `secrets/` **exclus**.
- Purge automatique de l'audio et des consultations au-delà de la rétention
  (`consultation_retention_hours`, défaut **12 h**).

### 7.2 Techniques — accès et authentification

- Authentification **OIDC unique** (Pocket ID, auto-hébergé) : pas de mot de passe local.
- Deux instances Pocket ID par domaine ; usernames/groups **identiques** entre les deux
  pour éviter doublons de comptes.
- Passkeys par domaine (WebAuthn, RP ID distincts) + code.
- Sessions : cookie `HttpOnly` + `Secure` (`SESSION_HTTPS_ONLY=true`), clés signées par
  `SESSION_SECRET` (unique, stable).
- Contrôle par groupes : `admins` (panneau, gabarits, toutes consultations) vs `users`
  (ses consultations seulement). Consultations cloisonnées par propriétaire (`owner`).
- `AUTH_DISABLED` toujours `false` en production (porte de secours uniquement).

### 7.3 Techniques — réseau et infrastructure

- **HTTPS/TLS** partout (Caddy, Let's Encrypt) ; pas de CDN pour les données cliniques
  (CDN chargé uniquement de ressources statiques anonymes, remplaçable en mode hors-ligne).
- **CrowdSec** actif : scénarios géo **fail-closed** (seules les IP canadiennes
  explicites passent), brute-force SSH/Web, bannissement des ASN d'hébergeurs connus.
- Caddy renvoie **403** aux chemins de scan ; Turnstile sur les pages de connexion.
- Secrets hors image, en lecture seule (`/secrets/gcp-sa.json`), `SESSION_SECRET` dans `/etc/dictai/.env`.
- Pare-feu : le port du conteneur n'est pas joignable au-delà du proxy.
- Le service worker ne met en cache que des ressources statiques et anonymes ; ni la
  page ni les `/api/` (ils contiennent des renseignements de santé).

### 7.4 Traitement et résidence des données

- LLM + audio sur **Vertex AI, région `northamerica-northeast1` (Montréal)**.
- `GOOGLE_CLOUD_LOCATION` explicite ; vérification que `GEMINI_API_KEY` est **vide**
  (une clé renseignée ferait retomber silencieusement sur l'API grand public, hors région).
- Les réglages effectifs du panneau priment sur le `.env` : un contrôle périodique
  (`app_settings` → `stt_provider`, `llm_provider`) vérifie qu'aucun fournisseur non
  validé n'a été activé.

### 7.5 Protection de la base et des sauvegardes

- Fichier `consultai.db` propriétaire UID/GID 1000 ; volume monté sur la VM.
- Transcriptions et notes stockées **en clair** dans SQLite (pas de chiffrement natif) :
  le volume n'est pas chiffré au repos actuellement — **action recommandée** : stocker
  `/opt/dictai/data/consultai` sur un volume chiffré.
- **Sauvegardes sanitisées (2026-08-14)** : les archives ZIP contiennent la base
  **vidée de toute donnée clinique** (tables `consultations` et `recordings` purgées)
  et **aucun fichier audio**. Elles ne renferment que la configuration, les comptes,
  les gabarits et les statistiques d'usage. Conséquence assumée : une restauration ne
  ramène pas les données patient ; l'audio existant est laissé intact. Les sauvegardes
  ne sont donc **plus** des vecteurs de fuite de renseignements de santé — le
  chiffrement des archives au repos reste recommandé (secrets d'API compris).

### 7.6 Surveillance continue (supervision)

La supervision constitue une **mesure d'atténuation active** des risques R1 (accès non
autorisé) et R7 (compromission du serveur). Elle est distincte du traitement clinique
et n'y accède jamais :

- **Collecte** : journaux réseau de Caddy (les quatre hôtes : `app`/`login` sur
  `dictai.ca` et `loki.casa`), CrowdSec (IP, ASN, scénarios), journaux SSH.
- **Minimisation avant analyse cloud** : les scripts `web_window.sh` **n'émettent jamais**
  les en-têtes `Cookie`/`Authorization`/`Set-Cookie` et **masquent** les valeurs
  `code`/`state`/`nonce`/`token` des URIs OIDC. Les agrégats transmis sont courts
  (~30 lignes) et **sans contenu clinique** (chemins et hôtes, jamais le corps des
  requêtes, jamais les réponses).
- **Traitement cloud** : l'analyse quotidienne est confiée à un agent DeepSeek
  (API DeepSeek) via OpenChamber ; le modèle ne reçoit que les agrégats ci-dessus.
  Les accès de l'agent sont limités à `/opt/dictai/**`, `/tmp/**`, `/var/log/**` —
  `/etc` (secrets) est **entièrement interdit**.
- **Réaction** : au verdict `ANOMALY`, un rapport détaillé est rédigé et, le cas échéant,
  un ASN malveillant est ajouté à `ban-known-bad-asn-ssh.yaml` (durcissement CrowdSec).
- **Rétention** : rapports quotidiens 7 jours, rapport hebdomadaire conservé (prune automatique).
- **Confidentialité** : les adresses IP des usagers légitimes peuvent apparaître dans les
  journaux — les rapports sont agrégés, conservés 7 jours et non partagés hors de la VM.

### 7.7 Dénominalisation (2026-08-14)

L'application ne collecte plus l'identité du patient :

- Les champs `patient_name` (nom) et `patient_ref` (numéro de dossier) ont été retirés
  de l'interface, de l'extraction automatique (métadonnées lues dans la dictée) et des
  en-têtes de note. Les gabarits livrés ne contiennent plus les champs `{{PATIENT}}` /
  `{{DOSSIER}}` (les gabarits personnels qui les gardent produisent une ligne retirée).
- Les valeurs déjà stockées ont été **effacées** à la migration (les colonnes sont
  conservées, vides).
- Les notes générées sont donc dénominalisées à la source ; l'identification du patient
  se fait au moment du versement au dossier médical, en dehors de l'application.
- Les métadonnées restantes (`consultation_date`, `reason`, `requester`,
  `accompanied_by`) ne permettent pas d'identifier un patient.

---

## 8. Registre des incidents

| # | Date | Description | Mesures prises |
|---|---|---|---|
| — | — | Aucun incident déclaré à ce jour | — |

Tout incident impliquant des renseignements de santé fera l'objet d'une évaluation de
la gravité (préjudice sérieux) et, le cas échéant, d'une **déclaration à la CAI** et
d'une **notification aux personnes concernées** conformément à la Loi 25.

---

## 9. Suivi et réévaluation

Le présent document est réévalué :
- à chaque **changement de fournisseur** STT ou LLM (même de test) ;
- à chaque **migration de plateforme** ou de région d'hébergement ;
- lors de l'ajout **d'utilisateurs** ou de groupes ;
- à chaque **changement du dispositif de supervision** (nouvel agent, nouveau
  fournisseur d'analyse, élargissement des journaux collectés) ;
- à tout changement de code touchant **aux données personnelles ou de santé** :
  rétention, sauvegardes, dénominalisation, collecte de métadonnées (cf. § 7.7) ;
- au moins **annuellement**, et à chaque révision majeure du code ou de la pile.

Le suivi continu s'appuie sur la supervision quotidienne (§ 7.6) : un incident
détecté alimente le registre des incidents (§ 8) et peut déclencher une
réévaluation du présent document.

---

## 10. Approbation

| Rôle | Nom | Signature | Date |
|---|---|---|---|
| Responsable de la protection des renseignements personnels | Dr Frederick Duong | | 2026-08-14 |
