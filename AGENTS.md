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
  curatée des **médicaments courants** — source UNIQUE, noms nus et marques ;
  l'ancienne table BDP `common_meds` / `med_grounding/seed_common.py` a été
  supprimée) : toute modification de ce
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
- **Fusion des candidats phonétiques partageant une posologie** (2026-09-06,
  cf. `Matcher._fusionner_candidats_posologie` en fin de
  `suggestions_texte`) : quand le STT éclate un nom unique en plusieurs
  jetons (« apixaban » → « Applique, ça bande »), deux candidats phonetic
  peuvent être suggérés pour la MÊME dose adjacente — le LLM verrait deux
  médicaments distincts. Deux candidats phonetic de molécules différentes,
  portant la même `posology` (strictement identique, non vide, positions à
  écart ≤ 2 jetons), sont FUSIONNÉS : le MÉDICAMENT COURANT l'emporte
  (sinon la sim la plus haute), et la `name` concatène les fragments.
  Deux médicaments légitimes à même dose mais à positions éloignées
  (gap > 2) ne sont JAMAIS fusionnés, ni les items déterministes
  (`source` absent). Toute évolution de ce regroupement doit être
  re-validée sur la consultation 38 (apixaban) et les 4 transcripts
  de référence.
- **`phonetic_profiles` gériatriques** (2026-09-06) : `_FENETRES = (1, 2, 3)`,
  `require_score` à 3 jetons suivant, `min_sim` 0.75 par défaut sur les
  nouveaux profils (MMSE/MoCA/ISO-SMAF conservent leurs seuils). Le
  gate « no-op » ne déclenche via `desigs_plain` QUE sur le canon (s'il
  porte un séparateur non-espace) et sur les garbles inline — un canon
  multi-mots à espaces (« Maison Aloïs », « bradykinétique ») ne bloque
  plus sa propre fenêtre déformée. Le dedup par canonique privilégie
  la fenêtre la plus proche phonétiquement (la locution pleine bat la
  troncature), la plus courte en sim égale. Tout ajout dans
  `geriatric_terms.json` (canal, sonde, `min_sim`) doit être re-validé
  sur les transcripts de référence `med_grounding/*-cohere.txt` et la
    consultation n° 37, et `compute_stats_json` doit confirmer le budget
    de scan (≤ ~500 ms sur 12 k caractères).
- **Zone médicaments (LLM)** (2026-09-07, cf. `detect_med_region` dans
  `med_grounding.py`) : le modèle LLM actif (`llm.active_model()`) identifie
  la région médicaments dans la transcription brute. Le résultat est persisté
  (`med_region_json` sur `Consultation`), affiché dans l'onglet Validation
  (carte violette), et utilisé pour cibler le scan phonétique. Si le modèle a
  le thinking activé (`_openrouter_reasoning_effort() != "none"`), le chemin
  LLM est annulé et le fallback local `_medlist_regions` s'exécute. Timeout
  HTTP : 30 s. Toute erreur → `None` → fallback local. La région est effacée
  sur retranscription/import/édition manuelle ; redétectée paresseusement par
  `_apply_grounding()`. `clearSecondPassView()` appelle `renderMedRegion(null)`
  au début de chaque génération.
- **`_RELEASE_FUSED_RE`** (2026-09-07) : le STT fusionne parfois les codes de
  formulation (MR, SR, XR, XL, CD, PB) avec le chiffre de dose (« MR90 »).
  Le regex `_RELEASE_FUSED_RE` sépare ces combinaisons (« MR 90 ») dans
  `normalize()`, `phonetiques_texte()` et `suggestions_texte()`. Les vrais
  suffixes numériques (« B12 », « D5W », « Q1SEM ») ne sont pas touchés.
  Tout ajout de code de formulation doit vérifier ce regex.
- **`_lookup_exact` composés de marque** (2026-09-07) : les noms multi-mots
  de marque dans `exact` (clé `norm_phon`, concatené sans espace) sont
  résolus via `norm_phon(nspace)` dans le chemin bigramme. Avant ce fix,
  `m.exact[nspace]` (clé avec espace) ne trouvait jamais un composé de
  marque. Toute modification de ce chemin doit être testée sur les
  consultations 38 (apixaban) et 12 (diamicron MR 90).
- **Bloc `CONFIANCE_MOTS` en spans de prose** (2026-09-07, cf. `med_grounding.grouper_doutes_pour_prompt`) : le bloc ne liste plus chaque mot douteux un à un. Les doutes sont regrouper par proximité dans le texte, écart de 2 jetons maximum, le même seuil que la fusion des candidats de posologie. Un span sans aucun mot non-courant est supprimé ; un span mono-jeton garde `mot → XX %` ; un span multi-jetons devient un **extrait verbatim** du texte avec un mot de contexte de chaque côté, les mots douteux marqués de `*astérisques*`, et la confiance du minimum du span. L'extrait localise le doute exactement dans la dictée, sans index numérique fragile — un multi-mots garble comme « Applique, ça bande » vers apixaban reste identifiable d'un bloc. Le formatage vit dans `med_grounding`; `llm._bloc_confiance` reçoit des lignes déjà prêtes. Toute évolution de ce regroupement doit être re-validée sur la consultation 38 (apixaban) et sur les transcripts de référence.


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
