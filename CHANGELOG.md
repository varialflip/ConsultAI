# Changelog

Changements livrés, entrées datées. À maintenir à chaque version publiée —
voir `/opt/dictai/AGENTS.md` (cycle de déploiement).

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
