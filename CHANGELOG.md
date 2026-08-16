# Changelog

Changements livrés, entrées datées. À maintenir à chaque version publiée —
voir `/opt/dictai/AGENTS.md` (cycle de déploiement).

## 2026-08-16 — v2.0.0-beta.61

- **ÉFVP complété.** L'ÉFVP précise désormais qu'en tant qu'outil de dictée
  post-consultation par le clinicien seul, **aucune voix de patient n'est
  attendue dans l'audio** ; une voix tierce captée par erreur demeure traitée
  comme très sensible sous rétention automatique de 12 h. Le fournisseur de
  modèle de langage est nommé : **Augure AI** (IA souveraine canadienne,
  traitement en sol canadien, texte seul, attribution ToS « Propulsé par
  Augure », facturation CAD ; cas exceptionnel de requêtes anonymisées vers des
  fournisseurs européens sous accords de non-conservation).

## 2026-08-16 — v2.0.0-beta.60

- **Politique de confidentialité et ÉFVP actualisées (Loi 25).** Le texte complet
  des enjeux couverts (STT au Québec sur serveur local, partenaire d'inférence
  canadien en sol canadien, cas exceptionnel de requêtes anonymisées vers des
  fournisseurs européens sous accords de non-conservation, absence
  d'entraînement des modèles, droits des personnes, incidents de confidentialité,
  transferts hors Québec) est détaillé dans la section dédiée du dépôt.
- La page de politique de confidentialité gagne une question « Quels sont vos
  droits ? » (accès, rectification, suppression, portabilité, explication du
  traitement), en français et en anglais.

## 2026-08-16 — v2.0.0-beta.59

- **Badge « Propulsé par Augure » sur la page de connexion.** Dès qu'Augure est
  le fournisseur actif, le badge officiel (lien augureai.ca, ≥ 12 px) apparaît
  sur la page d'accueil, **dans le même bloc que le choix de durée de session**,
  sous « La session expire après la durée choisie sans activité. » — sur les
  deux variantes (bureau et mobile). Le layout mobile défile désormais si le
  contenu dépasse l'écran (`overflow-y:auto`, carte centrée par `margin:auto`).
- **Libellé du badge corrigé.** « Propulsé par **Augure** » (au lieu de
  « Propulsé par Augure Augure ») — la clé i18n porte désormais « Propulsé par »,
  le nom « Augure » venant de la marque.
- **Pieds de note/dictée : nom du modèle, pas l'attribution.** Le pied de la
  note structurée revient à la mention par défaut (**`ossington-5`**) pour un
  modèle Augure ; l'attribution ToS reste portée par les badges (connexion et
  pied de page).

## 2026-08-16 — v2.0.0-beta.58

- **Hotfix — génération Augure (streaming) réparée.** La variante en continu
  de l'appel OpenAI-compatible (`_stream_openai_like`) résolvait le libellé
  du fournisseur par une table indexée qui ignorait « augure », provoquant
  une `KeyError` → 502 dès qu'Augure était actif. La table accepte désormais
  « augure » (et retombe sur un libellé générique si un futur fournisseur en
  échappait une fois de plus).

## 2026-08-16 — (benchmark Augure)

- **Nouveau fournisseur de modèle de langage : Augure AI.** L'onglet
  « Modèle de langage » gagne un fournisseur **Augure** dédié
  (`augure_base_url`, `augure_api_key`, `augure_model`, température, budget
  de sortie, raisonnement), routeur OpenAI-compatible comme les autres. **Texte
  seul** : la voix est transcrite en amont par le STT local (Parakeet) et ne
  quitte jamais la machine. Mesures du benchmark du jour : temps et coût par
  consultation mesurés (ossington-5 ~ 14 s, ~ 0,035 CAD lissé 15 min).
- **Attribution ToS Augure.** Dès qu'Augure est le fournisseur actif : les
  pieds de note et de dictée présentent le modèle sous le libellé
  « **Infrastructure IA canadienne par Augure** », et un badge officiel
  « **Propulsé par Augure** » (lien vers augureai.ca) apparaît dans le pied de
  page de l'application.
- **Libellés des moteurs raccourcis (en-tête et pieds de note/dictée).** Les
  modèles affichés ne montrent plus le chemin complet : seule la partie finale
  du modèle est affichée (Gemini → « gemini-2.5-pro »), et le modèle
  **Parakeet** (« istupakov/parakeet-tdt-0.6b-v3-onnx ») devient simplement
  « Parakeet v3 ». Détail preservé pour l'inspection.
- **Panneau admin — onglets « Tarifs » par fournisseur.** La liste des tarifs
  (Statistiques) est regroupée par des pilules-fournisseur ; la graine
  `pricing.py` porte désormais les tarifs sous le provider **`augure`**
  (`ossington-5` : 1,50 CAD / 3,00 CAD par 1M jetons entrée / sortie, devise
  `CAD`), préremplis au premier démarrage. Simple saisie de livre de comptes :
  aucun changement de fournisseur actif.

## 2026-08-15 — v2.0.0-beta.56

- **Modèle de langage — point de terminaison personnalisé : « Raisonnement »
  peut être désactivé.** Le réglage gagne les options **« Aucun »** (none →
  `reasoning.effort: none`, raisonnement totalement désactivé, vérifié à
  `reasoning_tokens = 0` sur DeepSeek v4) et **« Minimal »**. Sur DeepSeek
  v4-flash, le défaut (« Automatique ») ne raisonne déjà quasiment pas ;
  curieusement, « Faible »/« Minimal » y *augmentent* la pensée (contraire à
  l'intuition) — « Aucun » est la valeur pour forcer 0.

## 2026-08-15 — v2.0.0-beta.55

- **Modèle de langage — point de terminaison personnalisé : streaming réparé
  et paramètre de raisonnement correctement transmis**. Deux bugs rendaient la
  génération impossible dès que le réglage « Raisonnement » passait à Faible /
  Moyen / Élevé :
  - le flux OpenAI-compatible lisait `choice.message` alors que le SDK expose
    le texte dans `choice.delta` (un `ChoiceDelta`) : le contenu était **toujours
    vide** pour tout fournisseur OpenAI-compatible (OpenAI, OpenRouter, Qwen
    Omni). Le correctif lit `choice.delta` ;
  - `reasoning` était passé en argument direct du SDK, qui le refuse
    (`TypeError: Completions.create() got an unexpected keyword argument
    'reasoning'`). Il passe désormais par `extra_body`, que le SDK fusionne
    dans le corps JSON (OpenRouter l'accepte).
  Le réglage « Raisonnement » du point de terminaison personnalisé est donc
  pleinement opérationnel.

## 2026-08-15 — v2.0.0-beta.54

- **Panneau Réglages réparé (régression de la beta.53)**. Le réglage
  « Raisonnement » du point de terminaison personnalisé déclarait ses choix
  comme de simples chaînes au lieu de couples « valeur, libellé » : le
  panneau d'administration renvoyait une erreur 500. Les choix sont désormais
  des couples (`auto/low/medium/high`, libellés fr/en « Automatique /
  Faible / Moyen / Élevé »).

## 2026-08-15 — v2.0.0-beta.53

- **Modèle de langage — point de terminaison personnalisé : modèles à
  raisonnement (DeepSeek) réparés et contrôlables**. La régénération vers un
  modèle à raisonnement (ex. `deepseek/deepseek-v4-pro` via OpenRouter) sortait
  une note vide (« réponse vide, motif : length ») : le raisonnement consommait
  tout le budget de sortie — hérité du plafond de Gemini (8192 jetons) — avant
  le moindre texte. Deux nouveaux réglages propres au fournisseur custom :
  **Budget de sortie** (`custom_llm_max_tokens`, 32768 par défaut) et
  **Raisonnement** (`custom_llm_reasoning_effort`, « Auto » par défaut, qui
  envoie `reasoning.effort` au point de terminaison). De plus, si la réponse
  revient vide avec un motif « length », l'application **relance
  automatiquement** avec un budget doublé (plafonné). Les jetons de
  raisonnement (`reasoning_tokens`) sont désormais capturés dans l'usage pour
  le diagnostic. Côté panneau, le fournisseur custom a son propre budget, sans
  toucher à celui de Gemini.

## 2026-08-15 — v2.0.0-beta.52

- **Liste des brouillons — statut lisible**. Le statut était affiché brut en
  majuscules (« GENERE », « FINALISE »), les valeurs en base étant sans accent.
  Il passe par un libellé localisé (« Générée », « Finalisée », « Transcrit »,
  « Brouillon », « Erreur ») — `statusLabel()` côté client, clés `status.*` en
  fr / en.
- **Consigne générale — section finale : plus jamais « Confirmé »**. Le modèle
  écrivait « terme dicté → Confirmé », contradictoire dans une section destinée
  à la vérification par le clinicien. La consigne impose désormais **deux
  mentions possibles** : « → correction apportée : <lecture> » pour une
  correction retenue avec confiance, « → à confirmer » pour une lecture encore
  incertaine. Porté automatiquement dans les installations dont la consigne est
  restée au défaut livré (migration par empreinte, `database.py`) ; une
  consigne personnalisée dans le panneau n'est pas touchée.

## 2026-08-15 — v2.0.0-beta.51

- **Consigne générale — « Éléments à valider » rendue prioritaire, aucun
  médicament ignoré**. La section finale de la consigne est présentée comme
  **obligatoire** : elle ne doit jamais être omise ni vidée, et reste énumérée
  même quand plus de 8 éléments sont regroupés par catégorie (aucun doute valide
  ne disparaît). Nouvelle règle absolue en tête de consigne : **aucun
  médicament n'est jamais ignoré** — un nom de médicament incertain, mal
  entendu ou inaudible, une dose inconnue ou douteuse est toujours reporté en
  « Éléments à valider », jamais retiré du rapport sans trace. Porté
  automatiquement dans les installations dont la consigne est restée au défaut
  livré (migration par empreinte, `database.py`) ; une consigne personnalisée
  dans le panneau n'est pas touchée.
- **Reconnaissance vocale — barre de progression pour la retranscription et
  l'import**. L'appel HTTP reste bloquant (le texte complet revient en une
  réponse), mais le serveur publie son avancement en direct par événements
  SSE (`transcription_progress`) pendant le découpage du point de terminaison
  personnalisé : le navigateur affiche une **barre de progression déterministe**
  (pourcentage de l'audio déjà traité, indéterminée en pulsation avant le
  premier événement). Corrige la retranscription d'un long enregistrement :
  l'ancien toast « Retranscription… » expirait après 60 s alors que l'opération
  pouvait durer plusieurs minutes, ce qui poussait à relancer — et à jeter —
  des transcriptions entières. `toast()` accepte désormais une durée nulle
  (persistant) comme `toastWithAction()` ; le voile plein écran de l'import est
  remplacé par la barre.

## 2026-08-15 — v2.0.0-beta.50

- **Reconnaissance vocale — endpoint personnalisé : découpage en tranches de
  1 minute**. L'audio envoyé au point de terminaison personnalisé est découpé
  en tranches de 60 s (`custom_stt_chunk_seconds`, réglable dans le panneau)
  dès qu'il dépasse cette durée, en coupant de préférence dans un silence pour
  ne pas trancher un mot. Chaque tranche part au **modèle principal** (ex.
  Parakeet/ONNX), même au-delà de son plafond d'une passe (~6-7 min) : une
  retranscription ou un import de plusieurs minutes reste transcrite par le
  bon modèle plutôt que routée en bloc vers le modèle de repli Whisper. Le
  repli sur erreur 5xx s'applique tranche par tranche ; une tranche en échec
  est sautée, le texte partiel est conservé. Le découpage prime sur le seuil
  de durée (`custom_stt_max_seconds`), qui reste disponible sans découpage.
- **Documentation harmonisée** : la cible de tranche de dictée est bien de
  **10 s** (`DICTATION_SEGMENT_SECONDS`, fenêtre de coupe 6-11,5 s), et non
  « ~30 s » comme l'affirmaient plusieurs textes périmés (diagramme de
  `dictation.py`, avertissement Cohere, commentaire AssemblyAI) — corrigés.

## 2026-08-15 — v2.0.0-beta.49

- **Modèle de langage — Gemini : « Raisonnement » désactivé = vraie coupure**.
  La bascule passe désormais le budget de raisonnement à 0 : coupure réelle
  (pensée `None`), supportée par gemini-2.5-flash. gemini-2.5-pro refuse le
  budget 0 : un message le signale alors et invite à passer « Raisonnement »
  sur « Oui » avec un budget de 128 (plus de repli silencieux vers un
  raisonnement à plein régime).
- **Audio réparé pour les WebM sans durée**. Certains enregistrements produits
  par le navigateur (MediaRecorder, muxeur « live ») n'ont aucune durée dans
  leurs métadonnées : ffprobe renvoyait `N/A` et l'audio était jugé
  « illisible » et jamais envoyé au modèle. La mesure de durée retombe
  maintenant sur un décodage complet (quelques secondes, uniquement quand la
  durée manque) : ces enregistrements repartent réellement à Gemini.

## 2026-08-15 — v2.0.0-beta.48

- **Modèle de langage — Gemini : bascule « Raisonnement (thinking) »**. Nouveau
  réglage dans l'onglet Gemini : « Raisonnement » activé/désactivé. Désactivé
  (par défaut) : raisonnement quasi nul (budget 128 — le minimum que
  gemini-2.5-pro sur Vertex accepte, 0 étant refusé, et l'absence de réglage
  laissant le modèle penser à plein régime). Activé : le « Budget de
  raisonnement » s'applique pour laisser le modèle réfléchir avant de répondre.

## 2026-08-15 — v2.0.0-beta.47

- **Modèle de langage — Gemini : budget de raisonnement réglable**. Nouveau
  champ « Budget de raisonnement (thinking) » dans l'onglet Gemini du panneau
  (variable `GEMINI_THINKING_BUDGET`). 128 par défaut — le minimum accepté par
  gemini-2.5-pro sur Vertex, raisonnement quasi nul —, modulable à la hausse
  pour laisser le modèle réfléchir avant de répondre. Toute valeur sous 128 est
  relevée au minimum (0 et 1-127 sont refusés par le modèle).

## 2026-08-15 — v2.0.0-beta.46

- **Modèle de langage — Gemini : budget de raisonnement réglé et repli en cas
  de refus**. `thinking_budget` passe de 0 à 128 : gemini-2.5-pro (Vertex)
  refuse 0 et 1-127 (« thinking_budget is out of range; supported values are
  integers from 128 to 32768 »), ce qui faisait échouer toute génération avec
  ce modèle (400 INVALID_ARGUMENT). 128 est le minimum accepté tout en laissant
  un raisonnement quasi nul. Au cas où un modèle refuserait entièrement le
  champ `thinking_config`, une nouvelle tentative sans lui est faite
  automatiquement (une seule, avant toute diffusion) pour les appels en flux et
  hors flux.

## 2026-08-15 — v2.0.0-beta.45

- **Reconnaissance vocale — retrait des silences suspendu pour l'endpoint
  personnalisé**. Le plafonnement des pauses (qui concatène les paroles)
  dégradait les modèles locaux multilingues : sur Parakeet/ONNX, il faisait
  mélanger les langues (français/anglais). Comme cet endpoint n'est pas
  facturé à la durée, l'audio y est maintenant envoyé tel quel. Corrige la
  bascule Parakeet du 2.0.0-beta.44.

## 2026-08-15 — v2.0.0-beta.44

- **Reconnaissance vocale — point de terminaison personnalisé : repli et
  routage**. Le modèle de repli (`custom_stt_fallback_model`) prend le relais
  en cas d'erreur HTTP 5xx du modèle principal (une seule tentative), et un
  seuil de durée (`custom_stt_max_seconds`) envoie directement au modèle de
  repli les dictées trop longues pour l'endpoint principal. En pratique, le
  secours local (speaches) bascule entre **Parakeet TDT 0.6B v3** (dictées
  courtes) et **Whisper small** (dictées longues) sans intervention.

## 2026-08-14

- **Politique de confidentialité** : ajout en tête de la liste d'un énoncé de
  portée — ConsultAI n'est **pas** un « scribe IA » au sens du Collège des
  médecins du Québec : l'application n'est pas faite pour l'enregistrement
  d'une conversation entre un médecin et ses patients, mais pour la dictée
  post-consultation par le clinicien seul. Énoncé repris en tête de l'ÉFVP.

## 2026-08-14 — v2.0.0-beta.43

- **Reprise automatique sur quota Gemini dépassé** : un refus 429
  (RESOURCE_EXHAUSTED) de Vertex AI — plafond par minute ou capacité régionale —
  est transitoire. La génération réessaie maintenant jusqu'à 3 fois, avec un
  recul de 30 s puis 60 s, en respectant « Retry-After » quand le fournisseur le
  fournit. Un retard d'une minute ne se transforme plus en erreur à l'écran.
- La reprise s'applique aussi au flux de génération en continu, mais seulement
  tant qu'aucun texte n'a encore été diffusé : reprendre après aurait dupliqué
  la note. Coût et facturation inchangés (les tentatives refusées ne sont pas
  facturées).

## 2026-08-14 — v2.0.0-beta.42

- Génération en continu **lissée « token par token »** : le texte se dévoile
  par petits incréments continus au rythme du modèle, quelle que soit la taille
  des morceaux émis par le fournisseur — l'affichage coule au lieu de sauter.
- Le flux Gemini diffuse chaque partie séparément (granularité plus fine), et
  le proxy relâche les évènements immédiatement (flush SSE sans tampon).
- Fiabilité inchangée : la note finale reste celle renvoyée par le serveur ;
  l'affichage reste auto-réparé par les points de référence complets.

## 2026-08-14 — v2.0.0-beta.41

- Génération en continu plus fluide : les fragments du modèle sont diffusés au
  navigateur **dès leur arrivée** (au lieu d'être regroupés toutes les 250 ms),
  et l'affichage est rafraîchi à cadence fixe pour éviter les à-coups. Le texte
  coule maintenant au fil de la génération au lieu de sauter par grands blocs.
- Diffusion par deltas + auto-réparation : chaque morceau ne porte que le texte
  nouveau, avec un point de référence complet chaque seconde — un morceau perdu
  (réseau, file SSE saturée) est corrigé par la suite. La note finale reste
  celle renvoyée par le serveur, inchangée et fiable.
- Latence de diffusion réduite côté proxy (flush SSE ~100 ms).

## 2026-08-14 — v2.0.0-beta.40

- Correction d'un crash à l'ouverture d'un brouillon (« Parameter 1 not node ») :
  le témoin horizontal de génération est recapturé avant chaque rendu de
  l'aperçu au lieu d'être cherché après son effacement du DOM.
- Régénération : la note déjà affichée est effacée dès le clic sur
  « Mettre en forme », laissant la place à la nouvelle qui arrive en streaming
  (l'ancienne est restituée si la génération échoue).
- Voile de chargement sans effet de flou (`backdrop-blur` retiré) pendant les
  opérations en arrière-plan.

## 2026-08-14 — v2.0.0-beta.39

- Génération en direct : la note s'affiche **au fur et à mesure** que le modèle
  la rédige, au lieu d'apparaître d'un bloc à la fin. Le texte défile
  automatiquement pour toujours montrer la fin en cours.
- Desktop : le témoin de génération quitte le voile plein écran (qui cachait le
  texte) pour une pastille posée sur le panneau de transcription ; la vue passe
  automatiquement en « Aperçu ».
- Mobile : bascule directe sur la vue « Aperçu » pendant la génération ; un
  témoin horizontal animé, centré sur la portion encore vide de la note, balaie
  de gauche à droite.
- Le fond du texte en cours de génération est temporairement gris pâle,
  signalant que la note n'est pas encore définitive.
- Streaming pris en charge par Gemini, Anthropic, OpenAI, Qwen Omni et le point
  de terminaison personnalisé ; Cohere et Mistral conservent l'affichage en un
  bloc (comportement d'origine). Coût et facturation inchangés (le nombre de
  jetons ne varie pas).

## 2026-08-14 — v2.0.0-beta.38

- Nouveautés déplacées sur la page de connexion : version logicielle et
  changelogs des 7 derniers jours s'affichent avant l'authentification. Le
  panneau de droite de l'application redevient vierge (l'aperçu de la note
  apparaît seul, sans note ouverte).

## 2026-08-14 — v2.0.0-beta.34

- Politique de confidentialité reformulée en constats factuels : les questions
  restent en titres, les corps énoncent des faits.

## 2026-08-14 — v2.0.0-beta.37

- Calque dictaphone : bouton « Arrêter sans envoyer » (delete) retiré, sur iOS
  et Android. Seul le bouton « Terminer et transcrire » reste pendant la
  dictée ; l'arrêt sans envoi reste disponible sur la barre d'outils.

## 2026-08-14 — v2.0.0-beta.36

- Calque dictaphone : boutons stop/terminer rendus en ligne (`flex` rétabli sur
  leur emplacement réservé) — plus d'empilement vertical ni de chevauchement
  du texte d'aide.

## 2026-08-14 — v2.0.0-beta.35

- Calque dictaphone : le gros bouton reste fixe quand la dictée démarre. Les
  boutons stop/terminer apparaissent dans un emplacement de hauteur réservée
  (visibilité seule, plus `display:none`) — la colonne centrée ne se décale
  plus, notamment sur le calque renversé de l'iPhone.

## 2026-08-14 — v2.0.0-beta.33

- Mode dictaphone (téléphone retourné) : retour au comportement d'origine,
  plus simple et stable, sur toutes les plateformes (détection par rotation
  d'affichage à 180° sur Android et par `deviceorientation` avec anti-rebond
  400 ms).
- iOS : la demande de permission des capteurs est supprimée (aucune popup,
  aucun toast). Le mode retourné s'y active par le bouton « Mode retourné »,
  qui affiche le calque micro renversé de 180° ; sortie par le bouton ✕.
- Android : comportement d'origine seul, sans option de revirement manuel
  (boutons masqués).
- Panneau de diagnostic `?debug=sensors` retiré.

## 2026-08-14 — v2.0.0-beta.32

- Mode retourné manuel corrigé sur Android : la rotation de 180° à l'ouverture
  n'est plus appliquée que sur iOS. Ailleurs, le bouton « Mode retourné »
  ouvre le calque à l'endroit et celui-ci reste ouvert jusqu'à la sortie
  explicite (il n'est plus refermé par l'auto-détection) ; la rotation suit
  le retournement, et le retour à l'écran normal fonctionne comme avant.

## 2026-08-14 — v2.0.0-beta.31

- iOS : détection automatique du retournement retirée (l'accès aux capteurs y
  est souvent refusé et sa fiabilité variable). Le mode retourné ne s'y
  active plus que par le bouton « Mode retourné ».
- Bouton « Mode retourné » : le calque s'ouvre désormais tourné de 180° (tête
  en bas) pour se lire à l'endroit une fois le téléphone retourné ; rotation
  re-réglée si iOS bascule l'interface en paysage. Aide « Bouton ✕ pour
  quitter ».

## 2026-08-14 — v2.0.0-beta.30

- Mode retourné en accès direct : bouton « Mode retourné » dans la barre
  d'enregistrement pour activer/désactiver le grand bouton de dictée sans
  dépendre des capteurs — secours quand iOS refuse l'accès au mouvement et à
  l'orientation (permission refusée, « Empêcher le suivi intersites » actif).
  Bouton de sortie ajouté au calque (suspend l'auto-détection tant que le
  téléphone reste retourné).

## 2026-08-14 — v2.0.0-beta.29

- Aide permission des capteurs : le message de refus indique en premier le
  réglage iOS 18 qui supprime la demande (« Empêcher le suivi intersites »),
  et le panneau `?debug=sensors` affiche la même consigne en cas de refus.

## 2026-08-14 — v2.0.0-beta.28

- Permission des capteurs : une seule demande (`DeviceMotionEvent.
  requestPermission()`, qui couvre aussi `deviceorientation`) au lieu de deux
  simultanées, qui pouvaient rester en suspens sur iOS (état « pending »
  permanent) ; retentative à chaque geste tant que la décision n'est pas
  mémorisée par le système.

## 2026-08-14 — v2.0.0-beta.27

- Mode dictaphone (téléphone retourné) réparé sur iPhone : détection par
  vecteur de gravité, indépendante des conventions de signe des capteurs
  (`beta`/`gamma` s'inversent selon les versions d'iOS/WKWebKit) ; permission
  explicite pour les capteurs de mouvement et d'orientation avec message
  d'aide si refusée ; rotation du calque adaptée à l'orientation d'affichage
  (y compris la bascule en paysage que produit parfois iOS).
- Manifest PWA : orientation verrouillée portrait pour l'application installée
  (s'applique aux installations existantes, sans réinstallation).
- Diagnostic `?debug=sensors` : état de la permission, compteur et fraîcheur
  des événements capteurs, composantes de gravité, état du mode dictaphone.

## 2026-08-14 — v2.0.0-beta.26

- Pied de page sur toutes les pages avec politique de confidentialité (FAQ
  modale, fondée sur l'ÉFVP) — traitement et hébergement au Québec, un seul
  cookie de session, aucun cookie de tracking.
- Rétention des dictées abandonnées harmonisée sur celle des consultations
  (`consultation_retention_hours`, 12 h par défaut) — variable
  `DICTATION_RETENTION_HOURS` retirée ; purge aussi à l'accès à la liste des
  brouillons.

## 2026-08-14 — v2.0.0-beta.25

- Rétention des dossiers en heures (défaut 12 h) au lieu de jours.
- Sauvegardes sans contenu clinique : les archives n'emportent plus ni audio
  ni données patient (config, comptes, gabarits et statistiques seulement).
- Dénominalisation : le nom et le numéro de dossier du patient ne sont plus
  collectés ni stockés (les valeurs existantes sont effacées à la mise à jour).
- Panneau de droite : section informative avec version logicielle et
  changelogs des 7 derniers jours.

## 2026-08-13 — v2.0.0-beta.24

- Statistiques durables à la purge.
- Gabarits personnels.

## 2026-08-13 — v2.0.0-beta.23

- Préserver le raisonnement clinique dicté dans la note.

## 2026-08-13 — v2.0.0-beta.22

- Gabarits livrés — général FR/EN restructuré, gériatrie verrouillée.

## 2026-08-13 — v2.0.0-beta.21

- Dates courtes dans la liste des sauvegardes.

## 2026-08-13 — v2.0.0-beta.20

- Rotation des sauvegardes par couverture temporelle.

## 2026-08-13 — v2.0.0-beta.19

- Heures des statistiques en heure locale (ISO 8601 avec Z).

## 2026-08-12 — v2.0.0-beta.18

- Second fournisseur OIDC (app/login.loki.casa) — client dual host-aware.

## 2026-08-12 — v2.0.0-beta.17

- Audio multimodal pour le point de terminaison personnalisé (OpenRouter).

## 2026-08-12 — v2.0.0-beta.16

- Panneau admin en max-w-6xl permanent.

## 2026-08-12 — v2.0.0-beta.15

- Panneau admin élargi sur l'onglet Statistiques.

## 2026-08-12 — v2.0.0-beta.14

- « Mettre en forme » conclut la dictée avant de générer.

## 2026-08-12 — v2.0.0-beta.13

- Journal des générations paginé et montant affiché en $.

## 2026-08-12 — v2.0.0-beta.12

- Onglet Statistiques repensé (journal des générations, notes par usager, mobile).

## 2026-08-12 — v2.0.0-beta.11

- Nettoyage des marqueurs de prompt et raisonnement Qwen coupé.

## 2026-08-12 — v2.0.0-beta.10

- Gabarits verrouillés alignés sur la règle anti-remplissage.

## 2026-08-12 — v2.0.0-beta.9

- Consignes de fiabilité et style déclaratif.

## 2026-08-11 — v2.0.0-beta.8

- Le numéro de version affiché suit enfin l'étiquette publiée.

## 2026-08-10 — v2.0.0-beta.7

- Refonte de la page de connexion, typographie Gloock, marque dynamique.
