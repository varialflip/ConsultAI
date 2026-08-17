# ConsultAI — règles de travail pour agents

Dépôt source de vérité de l'application **ConsultAI** (dictée de consultations
cliniques : STT + LLM, audio envoyé directement au modèle en option — Gemini,
Qwen Omni, point de terminaison personnalisé). Branché sur
`github.com/varialflip/ConsultAI` (branche `main`), publié via CI sur
`ghcr.io/varialflip/consultai`, déployé par la pile `/opt/dictai` (voir
`/opt/dictai/AGENTS.md` pour le déploiement).

## Cycle de déploiement

1. Éditer le code ici.
2. `git add` / `git commit` / `git push origin main`.
3. Tagger : `git tag v2.0.0-beta.X && git push origin v2.0.0-beta.X`.
4. CI construit et publie l'image (amd64 + arm64).
5. Redéployer : `cd /opt/dictai && sudo docker compose pull consultai && sudo docker compose up -d consultai`.

## Règle permanente — documentation toujours en synchronisation

Tout changement de code (release **ou pas**) doit garder à jour, **dans le
même commit** :

- **`README.md`** — fonctionnalités, fournisseurs STT/LLM (huit vocaux, sept
  modèles — audio direct aux multimodaux), configuration, procédures,
  structure. Aucune nouveauté n'est ajoutée sans y être décrite.
- **`CHANGELOG.md`** — entrée datée, copiée dans l'image et affichée sur la
  **page de connexion** (version logicielle + « Nouveautés » des 7 derniers
  jours). Les entrées sont **condensées par date quand c'est possible** : la
  page de connexion regroupe par jour les items produits par les plusieurs
  versions publiées le même jour (voir `app/changelog.py`), donc quand on
  livre plusieurs releases rapprochées, regrouper leurs notes en une entrée
  datée unique et conciser les items évite un « Nouveautés » redondant.
- **`EFVP.md`** — dès que le changement touche aux données : rétention,
  sauvegardes (sanitisées), dénominalisation, fournisseurs, collecte de
  métadonnées, résidence des données. C'est le document de conformité Loi 25.
- **`AGENTS.md`** — le présent fichier et `/opt/dictai/AGENTS.md` si le
  changement touche à la pile, au cycle ou aux règles de travail.
- **`.env.example`** — toute variable de configuration nouvelle ou modifiée.

## Contraintes du code

- Version logicielle : `app/__init__.py` (`__version__`). **Toujours aligner
  la version du code sur le tag publié** — elle pilote la purge du cache du
  service worker (`/sw.js`) et l'affichage de la version sur la page de
  connexion.
- La langue des commentaires et du code est le **français** ; les textes
  d'interface passent par `app/i18n.py` (fr / en).
- L'identité du patient (nom, numéro de dossier) n'est **ni collectée ni
  stockée** (dénominalisation) : toute extraction de métadonnées ou champ de
  gabarit doit le respecter (`{{PATIENT}}` / `{{DOSSIER}}` conservés pour la
  compatibilité mais non alimentés).
- Sauvegardes **sanitisées** : jamais d'audio ni de données cliniques dans les
  archives.

Le déploiement de référence tourne sur la machine `/opt/dictai` : tout réglage
de production y est vérifiable par `sudo docker exec consultai python3 -c …`
(lecture des `app_settings`).
