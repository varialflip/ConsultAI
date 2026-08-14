# Changelog

Changements livrés, entrées datées. À maintenir à chaque version publiée —
voir `/opt/dictai/AGENTS.md` (cycle de déploiement).

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
