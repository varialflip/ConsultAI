# Changelog

Changements livrés, entrées datées. À maintenir à chaque version publiée —
voir `/opt/dictai/AGENTS.md` (cycle de déploiement).

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
