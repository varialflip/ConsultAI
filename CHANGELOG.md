# Changelog

Changements livrés, entrées datées. À maintenir à chaque version publiée —
voir `/opt/dictai/AGENTS.md` (cycle de déploiement).

## 2026-08-19 — v2.0.0-beta.75

- **Consolidation des consignes retirée** (beta.75) — la v2.0.0-beta.74 a
  produit une régression de style à la génération (voix dictée non
  respectée) et est **revertée intégralement** : la consigne générale
  (français/anglais) et les quatre gabarits livrés retrouvent leur texte
  d'origine, la section « Éléments à valider » du message utilisateur est
  rétablie. La valeur en base est remise sur l'ancien défaut livré par une
  migration d'annulation (une consigne personnalisée par le médecin n'est
  pas touchée).

## 2026-08-19 — v2.0.0-beta.73

- **Toast desktop relevé de 6 px de plus** (beta.73).

## 2026-08-19 — v2.0.0-beta.72

- **Cohere : budget de raisonnement réglable dans le panneau** (beta.72).
  Nouveau réglage **« Budget de raisonnement (jetons) »** (`cohere_llm_
  thinking_budget`, défaut **1024**), sous Modèle de langage → Cohere. Il est
  envoyé comme `thinking.token_budget` à la mise en forme de la note (validé à
  l'API : la famille command-a l'accepte, à condition de rester sous le budget
  de sortie — l'application y ramène la valeur). 0 = défaut du modèle. JAMAIS
  envoyé à la relecture des métadonnées (tâche mécanique, même règle que
  DeepSeek/Qwen). Un modèle ancien refusant le champ est rejoué sans lui, la
  note est produite quand même.

## 2026-08-19 — v2.0.0-beta.71

- **Cohere : note vide « MAX_TOKENS » corrigée** (beta.71). Les modèles de la
  famille command-a raisonnent et consommaient tout leur budget de sortie
  (plafonné à 8192 par l'ancienne limite codée en dur) avant de produire le
  moindre texte — la génération échouait avec « réponse vide (motif :
  MAX_TOKENS) », constaté en production sur `command-a-plus-05-2026`. Cohere
  bénéficie désormais du traitement réservé au point de terminaison
  personnalisé : budget de sortie propre de **32000 jetons**, relance
  automatique au budget doublé (plafond 64000, la limite réelle annoncée par
  l'API) si le raisonnement a tout consommé sans texte. La limite codée en dur
  est retirée : la vraie limite par modèle est apprise de l'API à l'exécution,
  comme pour `custom`.

## 2026-08-19 — v2.0.0-beta.70

- **Toast mobile collé au bord** (beta.70). La zone de toasts touche le bord
  inférieur de l'écran (plus de marge de 8 px) ; la safe-area de l'iPhone est
  toujours respectée (`env(safe-area-inset-bottom)`).

## 2026-08-19 — v2.0.0-beta.69

- **Tous les toasts tiennent sur une ligne sur mobile** (beta.69). Le message
  est tronqué avec des points de suspension plutôt que de passer sur deux
  lignes — y compris le toast « Note générée avec {model}. Relisez-la avant
  utilisation. », trop long pour une ligne pleine largeur. (Desktop inchangé :
  les messages longs continuent de s'afficher en entier.)

## 2026-08-19 — v2.0.0-beta.68

- **Toasts — position et compacité ajustées** (beta.68). Sur **mobile**, la
  zone de toasts redescend au plus près du bord : le toast recouvre au pire la
  barre de confidentialité, jamais les boutons « Retranscrire » / « Mettre en
  forme » qui lui sont au-dessus. Le **toast de progression devient une seule
  ligne compacte** : la piste fine est alignée à droite du texte au lieu
  d'être en dessous — hauteur réduite. Sur **desktop**, la zone reste relevée
  pour dégager le pied de la note structurée, la barre d'info et le pied de
  page. Le toast « Brouillon chargé » tient désormais sur une ligne (titre
  tronqué avec points de suspension, texte complet en infobulle).
- **`generation_started` plus tôt pour Gemini** (beta.68). L'événement est
  désormais publié **dès que la requête a été finie d'envoyer à l'API** — pour
  Gemini au lancement du flux (`generate_content_stream`), au lieu d'attendre
  le premier contenu reçu. Le toast de génération bascule donc en « La note
  se génère… » sans la latence du premier jeton. (OpenAI-compatible et
  Anthropic le publiaient déjà à la création du flux.)

## 2026-08-19 — v2.0.0-beta.67

- **Harmonisation de tous les états « en cours » en un toast unique**
  (beta.67). Génération (`genIndicator`/`genBar`), transcription et
  retranscription, fin de dictée, reprise et uploads partageaient des
  apparences divergentes (pastille-spinner, barre balayante mobile, voile
  plein écran `busyOverlay`, barre avec pourcentage pour la transcription).
  Tout est désormais un **toast de progression identique** : une ligne (spinner
  harmonisé 16 px + message + pourcentage à droite s'il est connu) et une
  **piste fine** — déterministe pour la transcription/upload (avancement réel
  du serveur), indéterminée sinon, sans jamais afficher de faux pourcentage.
  Le voile plein écran bloquant est supprimé. Sur desktop, la zone de toasts
  est légèrement relevée pour ne plus couvrir le pied de la note structurée ;
  sur mobile, la barre cinq fois aller-retour est remplacée par ce toast
  compact d'une ligne.
- **Événement SSE `generation_started`** : le serveur ne le publie QUE
  lorsqu'il sait que le fournisseur LLM a bien reçu la requête (jamais au
  lancement interne — ConsultAI n'exécute pas le modèle). Le toast de
  génération passe de « Connexion au modèle… » à « La note se génère… » à la
  réception de ce signal (ou dès le premier morceau `generation_chunk`, si
  l'événement s'est perdu). Point d'acquittement par fournisseur :
  OpenAI-compatible et Anthropic dès que `create(stream=True)` revient sans
  erreur, Gemini au premier contenu reçu.

## 2026-08-17 — v2.0.0-beta.66

- **Consigne générale : règles de structure rendues explicites** (beta.66).
  Constat en production sur `mistralai/voxtral-small-24b-2507` (OpenRouter,
  audio seul) : la note gérait mal la mise en forme malgré des règles déjà
  présentes — HMA et sections narratives en liste à puces au lieu d'un récit,
  rubriques entières vides (Allergies) ou lignes d'en-tête sans valeur
  (médecin de famille) survécues par le marqueur `[inaudible]`. Le § 3 exige
  désormais que les sections narratives (HMA, histoire sociale,
  investigations) soient rédigées en **paragraphes courts et suivis**, jamais
  en liste à puces — Impression et Plan restant en liste numérotée — et le § 1
  précise que `[inaudible]` ne couvre qu'un passage inintelligible À
  L'INTÉRIEUR d'une rubrique qui a du contenu : une rubrique ENTIÈRE sans
  contenu dicté, ou une ligne d'en-tête sans valeur, est supprimée, jamais
  remplie par `[inaudible]`. Une migration porte la règle en base si la
  consigne en place est encore le défaut livré (laissée intacte sinon, le
  médecin l'ajoute depuis le panneau).

## 2026-08-17 — v2.0.0-beta.65

- **Extraction des métadonnées : le raisonnement n'est plus envoyé en mode
  JSON** (beta.65). L'extraction (date, raison, demandeur, accompagné) est une
  tâche mécanique en `json_mode` ; sur le point de terminaison personnalisé,
  le réglage « Raisonnement » était pourtant transmis à cette étape et un
  modèle reflexif (DeepSeek v4 Flash) y renvoyait du texte hors JSON
  (vérifié en production : « Expecting property name… » sur 179 caractères),
  laissant l'interface en attente — la note, elle, était bien générée et
  conservée. Le raisonnement n'est désormais demandé que pour la **mise en
  forme de la note**, jamais pour l'extraction, comme Qwen le fait déjà avec
  `enable_thinking=False`. Le choix d'un modèle rapide **non raisonneur**
  (ministral) pour l'extraction complète le réglage.

## 2026-08-17 — v2.0.0-beta.64

- **Format audio configurable pour le point de terminaison personnalisé**
  (beta.64). « Joindre aussi l'audio » demande désormais le format de l'extrait
  joint (`custom_send_audio_format` : OGG/Opus par défaut, ou MP3/WAV). Le
  constat : un modèle comme **Mistral Voxtral** exposé via OpenRouter exige un
  fichier **MP3 ou WAV** et refuse l'OGG — l'audio était pourtant envoyé en
  OGG, voire en WebM brut mal étiqueté quand la langue STT était aussi
  « custom » et désactivait alors le rognage des silences. Le fournisseur
  « custom » transcodait désormais réellement l'audio dans le format demandé
  (`stt.transcode_to`, mono 48 kHz — MP3/WAV sans rognage de silence, l'audio
  du modèle conservant la dictée telle quelle) et le champ `format` censé par
  OpenRouter est normalisé (`audio/mpeg` → `mp3`). Gemini et Qwen restent sur
  OGG, leur format connu.

## 2026-08-17 — v2.0.0-beta.63

- **Section finale « Éléments à valider » rendue structurellement obligatoire**
  (beta.63). La section était exigée par la consigne générale (§ 4.1) mais
  contredite par la règle « n'ajoute aucune rubrique absente du gabarit » (§ 4) :
  des modèles qui reproduisent fidèlement la structure du gabarit (ex. Gemini)
  pouvaient alors l'omettre en fin de note. La contradiction est levée — la
  consigne générale déclare « Éléments à valider » comme l'unique rubrique
  supplémentaire autorisée, toujours en toute fin de note (même levée de
  contradiction côté anglais, « Items to verify ») — et le gabarit l'inscrit
  désormais dans sa structure (`## ÉLÉMENTS À VALIDER` en dernier bloc des
  gabarits français livrés et des copies en service). La consigne générale en
  base est migrée sans écraser une version personnalisée.

## 2026-08-16 — v2.0.0-beta.62

- **Fournisseur Augure retiré, retour à Vertex AI (Gemini 2.5 Pro)** (beta.57–62).
  Augure (fournisseur OpenAI-compatible `augure_*`, tarifs `ossington-5` en
  CAD, badge ToS « Propulsé par Augure » sur la connexion et dans le pied de
  page, hotfix streaming beta.58) est entièrement retiré de l'application et de
  la documentation : le constat a établi que le traitement annoncé « en sol
  canadien » passait en réalité par des fournisseurs européens. La mise en forme
  revient sur **Google Gemini via Vertex AI** (`northamerica-northeast1`,
  Montréal) avec **l'audio envoyé directement au modèle multimodal** ; la
  transcription locale Parakeet reste configurée en secours. Les entrées beta.57
  à beta.61 restent conservées comme trace.
- **Politique de confidentialité et ÉFVP (regroupées, Loi 25)** (beta.60–62).
  Énoncés factuels consolidés : reconnaissance vocale au Québec sur serveur
  local, **aucune voix de patient attendue** (dictée post-consultation), mise en
  forme via Vertex AI Montréal (remplace les mentions « partenaire canadien /
  fournisseurs européens »), absence d'entraînement des modèles, droits des
  personnes (accès, rectification, suppression, portabilité, explication —
  nouvelle question « Quels sont vos droits ? » de la politique de
  confidentialité), incidents de confidentialité. L'ÉFVP passe en v1.6.
- **Divers hérités du chantier Augure, conservés.** Libellés de modèles
  raccourcis (Gemini → « gemini-2.5-pro », Parakeet → « Parakeet v3 »), pieds de
  note/dictée au nom du modèle, onglets « Tarifs » par fournisseur dans
  Statistiques, table de libellés fournisseur avec repli générique
  (anti-`KeyError` → 502).

## 2026-08-15 — v2.0.0-beta.56

- **Modèle de langage — point de terminaison personnalisé : raisonnement
  contrôlable** (beta.53–56). Streaming réparé (lit `choice.delta`, `reasoning`
  transmis via `extra_body`), réglage « Raisonnement » corrigé (couples de
  choix, plus de 500) avec options « Aucun »/« Minimal » (« Aucun » force 0,
  vérifié sur DeepSeek v4), budget de sortie propre (`custom_llm_max_tokens`,
  32768 par défaut) et relance automatique à budget doublé (plafonné) sur
  réponse vide « length » ; `reasoning_tokens` capturés dans l'usage.
- **Modèle de langage — Gemini : raisonnement (thinking) et reprise**
  (beta.46–49). Budget de raisonnement réglable (`GEMINI_THINKING_BUDGET`, 128
  par défaut — minimum accepté par gemini-2.5-pro sur Vertex, relevé des
  valeurs 0–127 refusées), repli automatique sans `thinking_config` en cas de
  refus, bascule « Raisonnement » désactivée = vraie coupure (0 sur
  gemini-2.5-flash ; gemini-2.5-pro refuse 0 → message invitant à passer à
  128). Reprise sur quota (429) : jusqu'à 3 tentatives avec recul 30 s/60 s et
  respect de `Retry-After` (beta.43, livrée le 08-14).
- **Reconnaissance vocale — endpoint personnalisé** (beta.44–51). Modèle de
  repli sur erreur 5xx et routage par durée (Parakeet ↔ Whisper), retrait des
  silences suspendu pour cet endpoint (mélangeait les langues sur
  Parakeet/ONNX), découpage en **tranches de 60 s** (coupe dans les silences,
  repli tranche par tranche, texte partiel conservé), barre de progression de la
  retranscription et de l'import (le toast expirait après 60 s).
- **Brouillons et consignes.** Statut des brouillons affiché en français
  (« Générée », « Finalisée »…), fin de consigne « jamais **Confirmé** » (deux
  mentions : « → correction apportée » / « → à confirmer »), section
  « Éléments à valider » obligatoire et **aucun médicament ignoré** — migrations
  par empreinte pour les installations restées au défaut.
- **Audio et documentation.** WebM sans durée réparés (décodage complet en
  secours quand ffprobe renvoie `N/A`), cible de tranche de dictée documentée à
  10 s.

## 2026-08-14 — v2.0.0-beta.43

- **Génération en direct** (beta.39–42). La note se dévoile au fil du modèle :
  fragments diffusés dès réception, lissage « token par token », deltas +
  point de référence complet chaque seconde (auto-réparation), SSE sans tampon
  (flush ~100 ms). Vues « Aperçu » (desktop : pastille sur le panneau ; mobile :
  témoin horizontal), régénération qui efface la note affichée puis la restitue
  en cas d'échec, correction d'un crash à l'ouverture d'un brouillon
  (« Parameter 1 not node »).
- **Reprise sur quota Gemini dépassé** (beta.43). Les refus 429
  (RESOURCE_EXHAUSTED) sont transitoires : jusqu'à 3 essais avec 30 s puis 60 s,
  en respectant `Retry-After`. Sur le flux, la reprise ne s'applique que tant
  qu'aucun texte n'a été diffusé (éviter de dupliquer la note).
- **Politique de confidentialité.** Énoncé de portée — ConsultAI n'est **pas**
  un « scribe IA » du Collège des médecins du Québec, dictée post-consultation
  par le clinicien seul — repris en tête de l'ÉFVP ; politique reformulée en
  constats factuels.
- **Mode dictaphone (téléphone retourné)** (beta.27–37). Détection par vecteur
  de gravité (iPhone), une seule demande de permission capteurs (re-tentative) avec
  aide « Empêcher le suivi intersites », bouton « Mode retourné » en accès
  direct pour iOS, calque stabilisé (emplacement réservé des boutons, stop sans
  envoyer retiré), retour au comportement d'origine, manifest PWA en portrait,
  diagnostic `debug=sensors` retiré.
- **Confidentialité et rétention** (beta.25–26). Pied de page avec politique de
  confidentialité (FAQ modale), rétention en heures (défaut 12 h) harmonisée
  entre dossiers et dictées abandonnées, sauvegardes sanitisées (ni audio ni
  données patient), dénominalisation (nom et dossier effacés à la mise à jour),
  panneau latéral d'information avec version logicielle et nouveautés des 7
  derniers jours (déplacées sur la connexion, beta.38).

## 2026-08-13 — v2.0.0-beta.24

- Statistiques **durables à la purge** et heures en heure locale (ISO 8601, Z).
- **Gabarits personnels** ; gabarits livrés (général FR/EN restructuré,
  gériatrie verrouillée).
- La note **préserve le raisonnement clinique dicté**.
- Sauvegardes : **rotation par couverture temporelle** et dates courtes.

## 2026-08-12 — v2.0.0-beta.18

- **Second fournisseur OIDC** (app/login.loki.casa) — client dual host-aware.
- **Audio multimodal pour le point de terminaison personnalisé** (OpenRouter).
- Panneau admin élargi (onglet Statistiques repensé : journal des générations
  paginé en $, notes par usager, mobile).
- « Mettre en forme » **conclut la dictée** avant de générer.
- Nettoyage des marqueurs de prompt et raisonnement Qwen coupé.
- Gabarits verrouillés alignés sur la règle anti-remplissage ; consignes de
  fiabilité et style déclaratif.

## 2026-08-11 — v2.0.0-beta.8

- Le numéro de version affiché suit enfin l'étiquette publiée.

## 2026-08-10 — v2.0.0-beta.7

- Refonte de la page de connexion, typographie Gloock, marque dynamique.