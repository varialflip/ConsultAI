# ConsultAI — règles de travail pour agents

Dépôt source de vérité de l'application **ConsultAI** (dictée de consultations
cliniques : STT + LLM, audio envoyé directement au modèle en option — Gemini,
Qwen Omni, point de terminaison personnalisé, OpenRouter). Branché sur
`github.com/varialflip/ConsultAI` (branche `main`), publié via CI sur
`ghcr.io/varialflip/consultai`, déployé par la pile `/opt/dictai` (voir
`/opt/dictai/AGENTS.md` pour le déploiement).

## Branche `selfhosted` et instance de test (`test.dictai.ca`)

Cette branche est le **code de l'instance de test `test.dictai.ca`**, miroir
de `app.dictai.ca`. On la développe **dans le worktree dédié**
**`/home/opc/ConsultAI-selfhosted`**, dont `app/` et `CHANGELOG.md` sont
montés en lecture seule dans le conteneur `consultai-test` (stack
`/opt/dictai`). Détails, déploiement et configuration : `/opt/dictai/AGENTS.md`
(§ « Instance de test »).

- Développer et commiter **dans le worktree**, puis pousser la branche ;
- après chaque push : régénérer la feuille Tailwind **dans le worktree**, puis
  `sudo docker compose up -d --force-recreate consultai-test` ;
- la branche est partie de `main` le 2026-08-30 ; l'ancienne branche
  `selfhosted` (pipeline JSON de structuration, vérification BDPP) a été
  archivée sous `selfhosted-archive-2026-08-30` (locale + origin).

## Cycle de déploiement

Deux chemins bien distincts. Une release (tag) n'est faite que lorsqu'elle est
**demandée explicitement** ; un commit simple redéploie le conteneur
immédiatement, sans tag ni pull.

### Commit simple — redéploiement immédiat, sans tag ni pull

1. Éditer le code ici.
2. `git add` / `git commit` / `git push origin main`.
3. **Redéployer systématiquement**, même sans tag et sans nouvelle image : le
   code applicatif du conteneur vient du bind mount local (`/app/app`,
   lecture seule — voir `/opt/dictai/AGENTS.md`) ; il suffit de recréer le
   conteneur :

   ```bash
   cd /opt/dictai && sudo docker compose up -d --force-recreate consultai
   ```

   `pull` seulement si une nouvelle image est attendue (dépendances changées) ;
   régénérer la feuille Tailwind avant seulement si `tailwind-src.css` a
   changé (artefact absent du dépôt).
4. Pas de bump de version ni de tag sur ce chemin. La CI publie bien
   `latest` + `sha-…` à chaque push (image de référence), mais la pile reste
   épinglée à la dernière release.

### Tag + déploiement — seulement sur demande explicite

1. Bumper la version : `app/__init__.py` (`__version__`), entrée datée dans
   `CHANGELOG.md`, documentation à jour (README, EFVP le cas échéant,
   `.env.example`) — le tout dans le même commit.
2. `git add` / `git commit` / `git push origin main`.
3. Tagger : `git tag v2.0.0-beta.X && git push origin v2.0.0-beta.X` (la CI
   publie l'image épinglée, amd64 + arm64 — dépendances Python et base).
4. **Le code local EST le déploiement** : la pile `/opt/dictai` monte
   `/home/opc/ConsultAI/app` → `/app/app` et `CHANGELOG.md` en lecture seule
   (bind mount, voir `docker-compose.yml`). Le conteneur tourne directement
   la source : aucune attente de la CI.
5. Redéployer : régénérer la feuille Tailwind (artefact absent du dépôt) puis
   recréer le conteneur :

   ```bash
   cd /home/opc/ConsultAI
   [ -d node_modules ] || npm ci
   node_modules/.bin/tailwindcss -i app/static/tailwind-src.css -o app/static/tailwind.css --minify
   cd /opt/dictai
   sudo docker compose pull consultai
   sudo docker compose up -d --force-recreate consultai
   ```

   `--force-recreate` garantit la recréation même quand seule la source a
   changé (le montage est recalculé à chaque démarrage).

## Règle permanente — documentation toujours en synchronisation

Tout changement de code (release **ou pas**) doit garder à jour, **dans le
même commit** :

- **`README.md`** — fonctionnalités, fournisseurs STT/LLM (dix vocaux, huit
  modèles — audio direct aux multimodaux, dont OpenRouter), configuration,
  procédures, structure. Aucune nouveauté n'est ajoutée sans y être décrite.
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

- Version logicielle : `app/__init__.py` (`__version__`). Elle ne change
  **que lors d'une release** (tag + déploiement), jamais sur un simple push —
  elle pilote la purge du cache du service worker (`/sw.js`) et l'affichage
  de la version sur la page de connexion. Chaque tag publié doit avoir son
  `__version__` aligné dans le même commit.
- La langue des commentaires et du code est le **français** ; les textes
  d'interface passent par `app/i18n.py` (fr / en).
- L'identité du patient (nom, numéro de dossier) n'est **ni collectée ni
  stockée** (dénominalisation) : toute extraction de métadonnées ou champ de
  gabarit doit le respecter (`{{PATIENT}}` / `{{DOSSIER}}` conservés pour la
  compatibilité mais non alimentés).
- Sauvegardes **sanitisées** : jamais d'audio ni de données cliniques dans les
  archives.
- Le moteur de grounding charge au démarrage `app/common_meds.json` (liste
  curatée des **médicaments courants**, complémentaire de la table BDP
  `common_meds` / `med_grounding/seed_common.py`) : toute modification de ce
  fichier ou des constantes `COMMON_*` / `SUGGEST_*` / `_HINTS_PROSE` de
  `app/med_grounding.py`
  doit être re-validée sur les transcripts de référence (faux positifs de
  prose vs garbles réels) avant redéploiement.
- **Règles produit des candidats** (2026-09-05, cf. `_classer_candidats`) : à
  sim semblable (intervalle `COMMON_PRIVILEGE_GAP` = 0.10) le MÉDICAMENT COURANT
  l'emporte partout (rewrite phrase/mono-token, hints, suggestions) ; les
  candidats sont dédupliqués **par molécule** (générique + marques du même
  principe actif ne se concurrencent pas) ; on n'élimine jamais un candidat
  parce qu'un voisin est proche — on propose les 2 meilleures molécules
  distinctes, courant d'abord. Les doses peuvent être dictées **en toutes
  lettres** (« vingt-cinq », cap 999 via `_nb_lettres`/`_drapeaux_dose`) :
  elles comptent comme preuve de dose partout (région liste, phr, posologie) ;
  hors région confirmée elles ne créditent JAMAIS un ion de laboratoire
  (« Sodium cent quarante et un » reste une valeur de bilan).
- Le texte normalisé envoyé au LLM est **pré-calculé au « Terminer »** et mis
  en cache par consultation (`normalized_transcript` + `inline_fixed_json`).
  Toute modification de la chaîne inline (médicaments **ou** gériatrique) doit
  être vérifiée contre ce cache : invalidable (édition/retranscription/import)
  et re-persisté à chaque génération. La course « Terminer » → « Générer » est
  coordonnée par `dictation._grounding_events` (la génération attend le scan de
  fond au lieu d'en lancer un second) — ne pas contourner ce garde-fou sans
  rétablir l'équivalent. Les durées réelles se mesurent dans
  `compute_stats_json` (scan plein texte, pré-calcul, passes déterministes,
  TTFT) : y revenir avant de régler les seuils de performance.

Le déploiement de référence tourne sur la machine `/opt/dictai` : tout réglage
de production y est vérifiable par `sudo docker exec consultai python3 -c …`
(lecture des `app_settings`).
