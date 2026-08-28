# ConsultAI — règles de travail pour agents

Dépôt source de vérité de l'application **ConsultAI** (dictée de consultations
cliniques : STT + LLM, audio envoyé directement au modèle en option — Gemini,
Qwen Omni, point de terminaison personnalisé, OpenRouter). Branché sur
`github.com/varialflip/ConsultAI` (branche `main`), publié via CI sur
`ghcr.io/varialflip/consultai`, déployé par la pile `/opt/dictai` (voir
`/opt/dictai/AGENTS.md` pour le déploiement).

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

Le déploiement de référence tourne sur la machine `/opt/dictai` : tout réglage
de production y est vérifiable par `sudo docker exec consultai python3 -c …`
(lecture des `app_settings`).
