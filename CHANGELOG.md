# Changelog

Changements livrés, entrées datées. À maintenir à chaque version publiée —
voir `/opt/dictai/AGENTS.md` (cycle de déploiement).

## 2026-08-19 — branche `selfhosted`, vérification médicament BDPP : pipeline en 2 temps

Le chemin `note_lookup_dpd` (vérification par appel d'outil pendant
l'extraction) est remplacé par un **pipeline en 2 temps**, plus robuste aux
candidats auto-hébergés (Qwen, Lagunda, Mistral-small… qui n'émettent pas de
tool_call fiable sous le prompt d'extraction complet — voir le banc
d'essai/session Claude Code du 2026-08-19).

- **Passe 1 — repérage & vérification** (`note_extraction._DPD_PASS1_SYSTEM_*`,
  `tool_choice="required"`) : un appel étroit force la liste COMPLÈTE des
  médicaments de la dictée, transmis tels que transcrits, en UN SEUL appel à
  `verifier_medicaments_dpd`. En code pur : les correspondances fiables
  (`dpd`/`dpd_fuzzy`, sauf « seulement plus précis ») sont substituées dans la
  transcription (termes les plus longs d'abord) ; les candidats faibles
  (`dpd_fuzzy_weak`/RxNorm) sont isolés.
- **Passe 2 — rédaction** : l'extraction JSON normale tourne sur la
  transcription corrigée, avec une consigne annexe (`_weak_note`) laissant les
  candidats faibles en Éléments à valider (« à confirmer »), jamais corrigés.
- **Résilience** : une panne ou une absence de tool_call en passe 1 ne bloque
  jamais la passe 2 (note sur la transcription brute). Les `drug_lookups`
  restent écrits par le code, jamais par le modèle.
- `llm.complete_with_tools` gagne un paramètre `tool_choice` (`"auto"` défaut,
  `"required"`) porté par `_complete_mistral_tools` et `_complete_openai_tools`.
- Suppression de la boucle bornée (`_DPD_TOOL_MAX_ROUNDS/CALLS`) et des
  consignes `_DPD_TOOL_GUIDANCE_FR/EN` (devenues caduques).

Tests : `DpdToolTests` réécrits pour le 2 temps (substitution forte, faible →
consigne annexe, « seulement plus précis » non substitué, usage sommé sur les
deux passes, panne/absence de tool_call → passe 2 intacte, fournisseur custom).

## 2026-08-19 — branche `selfhosted`, liste des brouillons en 500 : purge bloquée par l'historique de générations

`GET /api/consultations` répondait 500 (vu réellement, test.dictai.ca
2026-08-19). L'endpoint purge d'abord les consultations périmées
(`purge_expired_consultations`), et cette purge échouait sur une
`sqlite3.IntegrityError` : la table `note_generations` porte une contrainte de
clé étrangère vers `consultations.id`, mais ni la purge de rétention ni la
suppression manuelle d'un brouillon ne supprimaient cet historique append-only
des générations. Le DELETE de la consultation arrivait donc avant celui de
l'historique et le COMMIT cassait — sur 10 consultations générées d'un coup,
toute la liste des brouillons tombait.

- Nouveau `_delete_consultation_cascade` (`app/main.py`) : supprime les
  enregistrements audio, puis l'historique de générations, puis la
  consultation. L'effacement de `note_generations` est exécuté IMMÉDIATEMENT
  (`db.execute(delete(...))`) — l'unité de travail de SQLAlchemy n'ordonnait
  pas « enfants avant parent » dans ce cas, le DELETE de la consultation
  partait avant celui de l'historique et la contrainte sautait.
- Branche sur les deux chemins : purge de rétention et
  `DELETE /api/consultations/{id}`.

## 2026-08-19 — branche `selfhosted`, Plan/Impression : la voix dictée au « je » s'impose aussi aux actions factuelles

Le rendu du PLAN basculait parfois à l'impersonnel (« Augmentation de la
rispéridone à 0,60 mg PO au coucher. ») alors que la dictée était à la
première personne (« J'augmente la rispéridone à 0,60 ») — malgré une consigne
qui demandait déjà de transcrire telle quelle. Vu réellement sur
test.dictai.ca 2026-08-19 : l'action factuelle au « je » reformulée en énoncé
nominal, et l'action « Je traite de façon symptomatique » supprimée du plan.

- **Consigne** : contre-exemples explicites ajoutés dans les six emplacements
  qui portent la règle de voix — `app/default_prompts.py` (`GENERAL_PROMPT_FR/EN`
  au § 3 Plan et `JSON_GENERAL_PROMPT_FR/EN`) et les gabarits verrouillés
  Gériatrie / Suivi - Gériatrie (`app/default_templates.py`) : « J'augmente la
  rispéridone à 0,60 » reste tel quel, jamais « Augmentation de la
  rispéridone… » ni « Il augmente… ». Une action dictée au « je » ne doit être
  ni reformulée en infinitif/nominal/impératif ni supprimée.
- **Contrôle fusionné** : `check_epistemic_clauses` devient
  `check_dictated_first_person` (`app/note_validator.py`) — la vérification
  s'étend de la seule clause épistémique à toute phrase dictée à la première
  personne dont l'équivalent rendu dans Impression/Plan a perdu le « je/j' »
  (code `dictated_first_person_dropped`).
- Migration en base : `migrate_general_prompt_plan_voice`
  (`app/database.py`, `_OLD_GENERAL_PROMPT_SHA6`) pour porter le nouveau texte
  dans les copies en base des consignes (générales + JSON), sans toucher aux
  consignes personnalisées.

Tests : 2 nouveaux cas dans `ValidatorTests` (action factuelle au « je » déclarée
puis perdue → flaggé ; conservée → non flaggé), le test épistémique existant
est conservé (code mis à jour).

## 2026-08-19 — branche `selfhosted`, correction non appliquée au corps + médecin référent confondu

Deux bugs réels trouvés en testant des modèles candidats pour un futur
self-hosting (mistral-small-latest, indépendant du modèle — reproduit sur
transcription texte propre, sans bruit STT) :

- **Correction annoncée, jamais appliquée au corps.** Le modèle produisait
  un élément dans Éléments à valider (« ange droite → correction apportée :
  hanche droite ») sans jamais réécrire le terme dans la section concernée
  (ANTÉCÉDENTS gardait « ange droite »). Vu deux fois dans une même note
  (aussi « 11 livres » → « 5 kg » resté en livres dans HMA). `sections` et
  `elements_a_valider` sont deux sorties JSON indépendantes sans rien qui
  force leur accord. Nouveau `fix_elements_a_valider_not_applied`
  (`app/note_validator.py`) : pour chaque correction confirmée
  (`is_confirmed_reading`), substitue `terme_dicte` → `correction` partout
  où le terme apparaît encore tel quel dans `sections`/`header_fields`
  (limite de mots, insensible à la casse, garde-fou longueur minimale de 3
  caractères + au moins une lettre contre la corruption par terme trop
  court). Tourne APRÈS `fix_elements_a_valider_corrections` dans `validate()`
  pour ne jamais appliquer une correction déjà démise comme non fiable.
  Aucune correspondance trouvée = no-op silencieux (terme paraphrasé ou déjà
  correct dans le corps), pas une erreur.
- **Médecin référent confondu avec un spécialiste mentionné plus loin.**
  Transcription nommant plusieurs médecins (le vrai demandeur, cité
  explicitement en début de dictée : « à la demande de Catherine Folly, son
  médecin de famille » ; un neurologue cité plus loin dans le récit à propos
  d'une conclusion passée) → le champ « Médecin référent » recevait le nom
  du spécialiste, pas du demandeur réel. Le grounding ne peut pas attraper
  ce cas (le nom erroné est réellement dans la transcription — erreur
  d'attribution de rôle, pas une invention). Correctif de consigne
  uniquement, dans les quatre emplacements de `app/default_prompts.py`
  (`GENERAL_PROMPT_FR/EN`, `JSON_GENERAL_PROMPT_FR/EN`) : nouvelle règle de
  désambiguïsation explicite juste après la règle anti-invention existante.
  Premier essai en vérification réelle : le spécialiste n'était plus retenu,
  mais le champ recevait « CRDS » (sigle du service demandeur) plutôt que
  « Catherine Folly » (le médecin nommé dans la même phrase) — la consigne
  autorisait « (ou l'organisme) » sans ordre de priorité. Resserré : quand un
  service ET le nom du médecin qui a fait la demande via ce service sont
  tous deux dictés, retenir le nom du médecin, le service n'étant un repli
  que si aucun médecin n'est nommé. Reconfirmé sur deux générations
  consécutives (consultation #11, transcription réelle) : « Médecin référent »
  ET « Médecin de famille » résolvent tous deux correctement à « Catherine
  Folly ».

Tests : 6 nouveaux cas dans `ValidatorTests`
(`tests/test_note_pipeline.py`), reproduisant les deux cas réels ci-dessus
plus les garde-fous (terme trop court, limite de mots, non-interférence
avec la démotion de fusion existante). Vérification bout-en-bout sur
transcriptions réelles (`extract_note` + `validate_and_repair`,
mistral-small-latest) pour les deux bugs.

## 2026-08-19 — branche `selfhosted`, appel d'outils ouvert au point de terminaison personnalisé + banc DeepSeek V4 Flash

`llm.complete_with_tools` accepte maintenant le fournisseur `custom`
(compatible OpenAI) via un nouvel `_complete_openai_tools` : le
`response_format` « json_object » est écarté pendant les tours d'appel
d'outil (le forcer empêcherait le modèle d'émettre un `tool_call` — vérifié
contre `~deepseek/deepseek-v4-flash-latest` sur OpenRouter), et
`reasoning.effort` (réglage `custom_llm_reasoning_effort`) est transmis sur
les tours d'outil mais pas sur le tour final JSON (même règle que
`_complete_openai`). `note_extraction.extract_note` ouvre donc la
vérification de médicament par appel d'outil BDPP (`note_lookup_dpd`) à
Mistral ET au fournisseur `custom`.

Banc d'essai, instance de TEST uniquement (dictée Carrière, consultations
5/9/10) : DeepSeek V4 Flash (`~deepseek/deepseek-v4-flash-latest`),
`custom_llm_reasoning_effort=minimal`, JSON pipeline + outil DPD.

- 3/3 notes valides (0 `validator_issues`, rien de bloqué).
- Outil BDPP réellement appelé : `Activant → ATIVAN` (DIN 02041413) dans les
  trois passes — le cas critique du chantier, confondu en amont par Mistral
  — retrouvé correctement, MÉDICATION ACTUELLE avec Ativan/Lorazépam distinct
  des IPP, plus de fusion.
- Coût : ~18 400 tokens d'entrée / ~9-12 500 de sortie, ~0,01 $/consultation,
  soit ~40 % de tokens d'entrée en moins que la référence Mistral
  (29 985 / 39 337 en entrée sur les cid 9/10).

Écarts à surveiller (revue clinique requise avant décision) : dans une passe
un « Monochore » reste littéral en MÉDICATION ACTUELLE (faible confiance
BDPP, correctement non confirmé) ; cid 10 la dose « 0,60 » du Plan apparaît
dans MÉDICATION ACTUELLE. Rapport :
`/tmp/opencode/bench/out/BENCHMARK_REPORT-deepseek.md`.

## 2026-08-18 — branche `selfhosted`, médicament mentionné en narratif absent de la liste

Vu réellement, test.dictai.ca, consultation #10 : la dictée liste 7
médicaments en une phrase explicite, mais mentionne SÉPARÉMENT en HMA
l'initiation de la rispéridone et sa titration dans le Plan (« j'augmente
la rispéridone à 0,60 »). MÉDICATION ACTUELLE ne reprenait que les 7
médicaments de la phrase de liste — la rispéridone, un antipsychotique
activement ajusté, restait invisible de la liste, visible seulement noyée
dans le narratif.

- **Consigne d'extraction** (`app/default_prompts.py`,
  `JSON_GENERAL_PROMPT_FR`/`EN`) : MÉDICATION ACTUELLE doit désormais
  refléter TOUTE la dictée, pas seulement une phrase de liste explicite —
  tout médicament décrit comme pris/initié/ajusté/cessé n'importe où (HMA,
  Plan) doit y figurer avec sa dose à jour. Même ajout dans les gabarits
  verrouillés (`app/default_templates.py`, Consultation Médicale Générale
  FR/EN + Consultation - Gériatrie), rafraîchis au démarrage.
- **Backstop déterministe** (`app/note_validator.check_medication_omitted_from_list`) :
  signale (visibilité seulement, jamais bloquant, jamais d'insertion de
  contenu) un médicament que le CODE a vérifié auprès de la BDPP/RxNorm
  (`note.drug_lookups`) mais qui n'apparaît nulle part dans le texte de la
  section médication (comparaison insensible aux accents). Appelé depuis
  `main._generate_json_pipeline`, journalisé comme `check_drug_lookups`.

Vérifié par génération réelle (rejeu direct de la consultation #10,
`run_pipeline_v2.py`) : MÉDICATION ACTUELLE contient maintenant
« Rispéridone 0,5 mg PO HS », et le backstop ne remonte aucun problème
(correctement, puisqu'elle y figure). 80 tests.

## 2026-08-18 — branche `selfhosted`, marques historiques locales (RxNorm)

La BDPP (Santé Canada) n'expose que les produits **courants** : une marque
retirée du marché s'y voit introuvable et la dictée d'un tel nom retombait
sur un faux candidat (`oppressor` → `suppressor`, au lieu de **Lopressor**
= métoprolol). Ajout d'une **source historique locale RxNorm** (NLM/US) —
release « Current Prescribable Content », sans licence, vérifiée (contient
Lopressor, Ativan, Prevacid) :

- **Aucun envoi runtime** : la release est téléchargée une fois (mensuelle)
  dans `/data/rxnorm_cache/` et indexée localement avec la même clé Soundex
  FR + similarité de caractères que l'extrait BDPP → **zéro flux de données
  vers les États-Unis**, donc rien à déclarer en EFVP (§ 11).
- **Fusion « choix unique »** (`note_extraction._maybe_legacy`) : quand le
  résultat BDPP est introuvable ou « faible », `drug_lookup.legacy_match`
  remplace le candidat par la marque historique, `source="rxnorm"`. Elle
  reste **TOUJOURS** `confiance: "faible"` (« à confirmer », jamais une
  correction apportée) ; une source BDPP forte n'est jamais remplacée.
- **Réglage** `note_lookup_legacy` (panneau, défaut **oui**) — sans
  implication vie privée (index local).

Vérifié sur les vraies données : `oppressor → Lopressor`, `norvasquine →
Norvasc`, `Certraline → SERTRALINE`, `donnépésil → DONEPEZIL` (tous
« à confirmer »). Testé par `LegacySourceTests` (`tests/test_note_pipeline.py`,
75 cas au total).

## 2026-08-18 — branche `selfhosted`, couche phonétique française BDPP

Le repli flou de la vérification BDPP rapprochait deux ORTHOGRAPHES ; or le
terme comparé sort d'un moteur de reconnaissance vocale et ses erreurs sont
PHONÉTIQUES (« Norvask », « Monochore »…). `app/drug_lookup._fuzzy_fallback`
compare maintenant chaque nom de l'extrait aussi à sa clé **Soundex FR** —
`app/phonetic_fr.py`, l'algorithme phonétique français d'Édouard Bergé
(port Python « Soundex FR » de la lib `phonetic-fr`), vendu à l'identique
sous licence MIT, **sans nouvelle dépendance** (la lib est pure Python).

Ce second signal ne remplace pas le premier, il le complète, sous une
sécurité stricte :
- **tie-breaker** : deux candidats au même ratio de caractères (cas réel
  `Ensoprazole`, 0,870 contre LANSOPRAZOLE comme ESOMEPRAZOLE — l'ancien
  code gardait arbitrairement la première entrée de l'index) ; la clé
  phonétique départe (0,952 vs 0,762) et choisit LANSOPRAZOLE.
- **rampe de retrouvaille « faible »** : un candidat à faible similarité de
  caractères (≥ 0,50) mais à clé phonétique très proche (≥ 0,83) devient
  TROUVABLE en `source="dpd_fuzzy_weak"` (jamais « elevée »).
- le phonétique ne peut JAMAIS remonter un candidat au palier « confirmé » :
  « Respirone » reste correctement « à confirmer » (REPRONEX domine dans
  les deux métriques — le palier faible existe exactement pour ce cas).

Côté coût : la clé de tout l'index est calculée une seule fois à la
construction (quelques secondes) et persistée dans
`/data/dpd_cache/<kind>_index.json` — pas d'encodage rejoué à chaque
génération. La version 2 de l'index ne se migre pas automatiquement, mais
`_load_local_index` voit son compagnon absent et le reconstruit seul.

Testé par `tests/test_note_pipeline.py` (`PhoneticLayerTests`, 69 cas au
total — les 65 existants inchangés).

Deux correctifs sur le risque résiduel documenté plus tôt la même journée
(le modèle transmettait parfois sa propre correction à l'outil plutôt que
le terme réellement transcrit) :

- **Consigne renforcée** (`note_extraction._DPD_TOOL_SCHEMA`/`_DPD_TOOL_GUIDANCE_FR/EN`) :
  interdiction explicite de transmettre une hypothèse déjà corrigée à
  l'outil — le terme doit être TEL QUE TRANSCRIT (« Activant », pas
  « Ativan » même si c'est l'hypothèse du modèle), sans quoi l'outil ne
  vérifie plus rien, il confirme seulement que la propre supposition du
  modèle est un vrai médicament.
- **Palier de confiance dans le repli flou** (`app/drug_lookup.py`,
  `_FUZZY_STRONG_THRESHOLD = 0.83`) : en rejouant la consultation #9 avec le
  correctif précédent, un NOUVEAU problème est apparu — « Respirone » a été
  rapproché de « REPRONEX » (médicament de fertilité SANS rapport, ratio
  0,824), et le modèle a écrit « correction apportée : Repronex » avec
  confiance. Les cas vérifiés fiables (Norvask/Norvasc, Activant/Ativan,
  Monochore/Monocor, Ensoprazole/Lansoprazole) sont tous ≥ 0,857 ; Repronex
  reste à 0,824 — marge confortable pour séparer les deux. En dessous de ce
  second seuil, un match reste renvoyé (`found=True`, un candidat reste
  utile) mais avec `source="dpd_fuzzy_weak"`, communiqué au modèle comme
  `confiance: "faible"` avec consigne explicite de ne jamais l'écrire comme
  une correction confirmée.

Vérifié par génération réelle contre la consultation #9 après les deux
correctifs : `Ativan 0.5 mg PO HS PRN` apparaît maintenant correctement
dans MÉDICATION ACTUELLE, et `Respirone → à confirmer` (plus de fausse
correction vers Repronex).

Testé par `tests/test_note_pipeline.py` (65 cas).

## 2026-08-18 — branche `selfhosted`, repli flou BDPP sur l'extrait complet (pas des préfixes)

Le repli flou livré plus tôt la même journée (raccourcir le terme depuis la
fin, réinterroger la BDPP à chaque longueur de préfixe) résolvait
`Norvask → NORVASC`, mais s'est avéré structurellement incapable de
retrouver un médicament dont le début diffère : cas réel, consultation #9,
`Activant` (probablement Ativan/lorazépam, confondu par le modèle avec
zopiclone) et `Ativan` divergent dès la 2ᵉ lettre (`c` vs `t`) — aucun
préfixe de l'un n'est un préfixe de l'autre, donc la recherche par préfixe
ne pouvait JAMAIS le proposer comme candidat, même si leur similarité
(`SequenceMatcher` ≈ 0,857) est largement au-dessus du seuil de 0,75.

Corrigé en téléchargeant l'extrait COMPLET plutôt que des préfixes : le même
point de terminaison `drugproduct`/`activeingredient`, appelé SANS
paramètre de nom, rend l'ENSEMBLE de la base (confirmé empiriquement :
~58 000 produits / ~121 000 lignes d'ingrédients, ~15-16 Mo chacun en JSON)
— pas de fichier ZIP à télécharger ni parser. Mis en cache localement sous
`/data/dpd_cache/` (même volume persistant que la base SQLite), rafraîchi
si périmé (7 jours), avec repli sur un cache périmé plutôt que rien si le
téléchargement échoue. Le repli flou compare maintenant le terme contre
CHAQUE nom de l'extrait (`difflib.SequenceMatcher`), pas seulement ceux
partageant un préfixe — remplace entièrement l'ancienne boucle de
raccourcissement, sans régression (tout ce qui était trouvable par préfixe
le reste).

Vérifié contre l'API réelle : `search_drug('Activant')` retrouve maintenant
ATIVAN (DIN 02041413) — 0,9 à 2 s par recherche (téléchargement une seule
fois, puis index en mémoire pour le reste du processus).

**Constat non résolu, trouvé en rejouant la consultation #9 avec l'outil
amélioré** : le modèle ne transmet pas toujours le terme LITTÉRALEMENT
dicté à l'outil — il arrive qu'il transmette déjà SA PROPRE correction
(cette fois : « Activelle », un vrai médicament — patch hormonal —
totalement différent d'Ativan) avant l'appel, et l'outil confirme alors
que cette proposition existe, renforçant une mauvaise supposition avec une
fausse confiance plutôt que de vérifier le terme réellement entendu. Un
meilleur outil ne peut rien faire si le terme qu'on lui soumet a déjà été
« corrigé » par le modèle avant l'appel. Piste de suivi, pas encore
implémentée : consigne explicite pour transmettre le terme TEL QUE DICTÉ,
jamais une reconstruction personnelle, à l'outil.

Testé par `tests/test_note_pipeline.py` (63 cas).

## 2026-08-18 — branche `selfhosted`, Laboratoires en liste, Antécédents ≠ social, BDPP flou

Trois problèmes de plus trouvés sur la même consultation #9 :

- **`### Laboratoires` toujours rendu en un seul paragraphe**, malgré le
  correctif de la même journée pour Médication/Antécédents/Examen : le
  marqueur `{{liste à puces}}` n'avait jamais été ajouté sous CETTE
  sous-rubrique précise. Ajouté dans le gabarit verrouillé « Consultation -
  Gériatrie » (`default_templates.py`) et le gabarit « (FD) » de test.dictai.ca.
- **« Divorce » classé sous `ANTÉCÉDENTS MÉDICAUX ET CHIRURGICAUX`** : dicté
  immédiatement après des antécédents médicaux sans transition
  (« ...trauma crânien... Il a un divorcé. »), le modèle a continué
  d'alimenter la même rubrique au lieu de reclasser par contenu. Le statut
  civil/la situation familiale n'est pas un antécédent médical ou
  chirurgical. Consigne renforcée dans les gabarits « Consultation Médicale
  Générale », « General Medical Consultation », « Consultation - Gériatrie »
  et le gabarit « (FD) » : exclut explicitement le statut civil, qui va sous
  Histoire sociale même dicté sans transition marquée. Aucun correctif
  mécanique proposé ici — reclasser du contenu narratif entre rubriques
  exige de comprendre le sens, ce qu'un validateur ne fait pas.
- **La recherche BDPP est un filtre préfixe, pas une correspondance floue**
  (confirmé empiriquement contre l'API réelle) : `Norvask` (k, tel que
  dicté/mal transcrit) ne retrouve RIEN pour `NORVASC` (c, la vraie marque),
  alors qu'un préfixe plus court comme `Norva` le retrouve. `app/drug_lookup.search_drug`
  fait maintenant ce raccourcissement elle-même — repli flou qui rétrécit le
  terme depuis la fin jusqu'au premier préfixe qui rend des candidats, les
  classe par similarité au terme ORIGINAL (`difflib.SequenceMatcher`, même
  technique que `note_validator._best_match_ratio` pour le grounding), et
  retient le meilleur si le ratio dépasse 0,75. Un résultat trouvé ainsi
  porte `source="dpd_fuzzy"` plutôt que `"dpd"`, pour rester distinguable.
  Vérifié contre l'API réelle : `search_drug("Norvask")` retrouve maintenant
  NORVASC (DIN 00878901) dès le premier préfixe raccourci.

Testé par `tests/test_note_pipeline.py` (55 cas).

## 2026-08-18 — branche `selfhosted`, deux médicaments fusionnés en un (Ativan disparu), et jetons non rapportés

- **Deux médicaments dictés sans pause fusionnés en un seul** (vu réellement,
  consultation #9, test.dictai.ca, mistral-small-latest) : « ...Diamicron MR
  30 L'ensoprazole 30 Activant 0.5 au coucher au besoin... » a été fusionné
  en un seul médicament « Ésoméprazole », avec une correction mensongère
  « Activant → correction apportée : Ésoméprazole » — « Activant » est
  vraisemblablement Ativan (lorazépam), un médicament DISTINCT (benzodiazépine
  PRN au coucher, rien à voir avec un IPP). Le pipeline de vérification BDPP
  confirmait déjà le problème : seuls 6 médicaments avaient été soumis à
  l'outil, jamais 7 — la fusion avait eu lieu avant même l'appel d'outil.
  Corrigé en deux temps :
  - **Consigne** (`default_prompts.JSON_GENERAL_PROMPT_FR/EN`, `note_extraction._DPD_TOOL_GUIDANCE_FR/EN`) :
    règle explicite contre la fusion de deux noms de médicaments dictés sans
    pause claire, et consigne à l'outil de vérifier chaque segment candidat
    séparément avant de fusionner.
  - **Validateur** (`note_validator.fix_elements_a_valider_corrections`,
    nouveau cas `correction_is_duplicate`) : filet mécanique — si une
    « correction » proposée duplique EXACTEMENT un contenu déjà gardé
    ailleurs dans la note, elle est démise en « à confirmer » plutôt que
    gardée telle quelle. Ne récupère pas le médicament disparu, mais empêche
    la note de prétendre que la fusion était correcte. Exclut délibérément
    les valeurs numériques en tête (une dose comme « 75 mg » recoupe
    légitimement le contenu déjà gardé — ce n'est pas le signal recherché) et
    ne compare jamais deux éléments d'Éléments à valider entre eux (deux
    mishearings légitimes du même médicament, ex. « Respirone »/« Rispiridone »
    → rispéridone, ne doivent jamais être signalés).
  - Rejoué contre la même consultation après correctif : « Lorazépam 0,5 mg
    PO HS PRN » apparaît maintenant comme médicament distinct, plus de
    correction mensongère.
- **Jetons jamais rapportés pour le pipeline JSON** (la page statistiques
  n'affichait aucun compte de jetons pour ces générations) : `usage: {}` était
  codé en dur dans `main._generate_json_pipeline`, et `note_extraction.extract_note`
  ne remontait l'usage d'aucun appel modèle à l'appelant — ni pour le chemin
  simple, ni (a fortiori) pour la boucle d'appel d'outils, qui fait PLUSIEURS
  appels à additionner. Corrigé : `extract_note`/`_extract_note_with_dpd_tool`
  acceptent un `usage_out` optionnel, muté en place pour accumuler les jetons
  de chaque tour (`llm.ToolCompletion` gagne aussi un champ `usage`, absent
  jusqu'ici). Vérifié par appel direct de `main._generate_json_pipeline`
  contre la consultation #9 : jetons réels rapportés (29985 en entrée, 8117
  en sortie).

Testé par `tests/test_note_pipeline.py` (53 cas) et par génération/appel
réels contre la consultation #9.

## 2026-08-18 — branche `selfhosted`, vérification de médicament par appel d'outil (BDPP Santé Canada)

- **Nouveau réglage `note_lookup_dpd`** (désactivé par défaut, sans effet
  sauf si `note_pipeline_json` est activé ET le fournisseur actif est
  Mistral) : pendant l'extraction, le modèle reçoit un outil d'appel de
  fonction `verifier_medicament_dpd` qu'il peut invoquer pour vérifier un
  nom de médicament incertain contre la Base de données sur les produits
  pharmaceutiques de Santé Canada (API publique, sans authentification).
- **Nouveau module `app/drug_lookup.py`** : client `urllib` (même
  convention que les fournisseurs Mistral/Cohere existants dans `llm.py`,
  pas de nouvelle dépendance), ne lève jamais — toute panne réseau devient
  un résultat « non trouvé, en erreur », jamais un échec de génération.
- **`llm.py` gagne un point d'entrée SŒUR** de `complete()` :
  `complete_with_tools()` — réservé à Mistral (seul fournisseur dont
  l'appel d'outils est câblé aujourd'hui), zéro impact sur les six autres
  fournisseurs qui n'y touchent jamais.
- **Boucle bornée** dans `note_extraction._extract_note_with_dpd_tool`
  (2 tours, 6 appels maximum) : le modèle peut ignorer l'outil, l'appeler
  plusieurs fois, ou ne jamais conclure dans le budget imparti (repli sur
  un dernier tour sans outil).
- **La preuve de vérification est écrite par le CODE, jamais par le
  modèle** : `ExtractedNote.drug_lookups` est peuplé par la boucle
  d'orchestration elle-même ; `from_dict()` refuse délibérément de lire
  cette clé depuis la réponse JSON du modèle — un modèle ne peut donc
  jamais s'auto-déclarer « vérifié » sans que l'appel ait réellement eu
  lieu.
- **`note_validator.check_drug_lookups`** : informatif seulement
  (`severity=auto_fixed`), journalisé par `main._generate_json_pipeline`,
  volontairement PAS câblé dans `validate()` — aucun classifieur fiable
  « ceci est un médicament » n'existe pour le texte libre d'Éléments à
  valider ; un heuristique faible câblé en dur produirait un flux de faux
  positifs déguisé en vérification sérieuse.
- Une absence de correspondance BDPP N'EST PAS une preuve d'erreur
  (médicament étranger, composé en pharmacie, retiré du marché) — jamais
  bloquant, jamais renvoyé au modèle pour « correction » automatique, même
  principe de sécurité déjà appliqué à `check_grounding`.

Testé par `tests/test_note_pipeline.py` (49 cas, incluant une vérification
empirique préalable que `json_mode` et `tools` fonctionnent ensemble sur
`mistral-small-latest`) et par génération réelle contre la consultation #5 :
le modèle a appelé l'outil 6 fois (une fois par médicament), 5 correspondances
BDPP trouvées avec DIN, 1 « non trouvé » traité correctement comme un signal
et non une erreur.

## 2026-08-18 — branche `selfhosted`, Médication/Antécédents/Examen rendus en prose au lieu d'une liste

- **Rubriques « liste pointée » ignorées** : Médication actuelle, Antécédents
  médicaux et Examen (objectif/physique) demandent toutes une « liste pointée »
  dans la consigne du gabarit, mais le modèle rendait ces rubriques en une
  seule phrase à virgules (ex. « Norvask 10, Crestor 20, Monocor 2,5... »)
  plutôt qu'en tableau JSON — même défaut que le bogue Impression/Plan corrigé
  plus tôt (le numérotage), mais côté puces cette fois : rien n'indiquait
  explicitement au modèle, au niveau du schéma JSON, que CETTE rubrique voulait
  un tableau plutôt qu'une chaîne.
- **Correctif, même mécanisme que {{liste numérotée}}** : marqueur
  `{{liste à puces}}`/`{{bulleted list}}` (déjà reconnu par
  `note_schema.LIST_STYLE_MARKERS`, jusqu'ici seulement utilisé pour
  « numbered ») ajouté sous ANTÉCÉDENTS MÉDICAUX ET CHIRURGICAUX, MÉDICATION
  ACTUELLE et EXAMEN OBJECTIF/PHYSIQUE dans les quatre gabarits verrouillés et
  les deux gabarits « (FD) » de test.dictai.ca. Nouvelle distinction
  `LayoutSpec.explicit_list_style` (marqueur présent ou non) vs `list_style`
  (retombe sur « bulleted » par défaut, pour le RENDU seulement) : la consigne
  au modèle — « encode-la comme un tableau JSON » — ne s'ajoute que pour les
  rubriques explicitement marquées, jamais par défaut sur toutes les rubriques
  à contenu libre.
- Consignes des gabarits corrigées en même temps : « Antécédents médicaux et
  chirurgicaux » ne mentionnait nulle part « liste pointée » (contrairement à
  Médication et Examen) — ajouté.

Testé par `tests/test_note_pipeline.py` (39 cas) et par génération réelle
contre la consultation #5 (mistral-small-latest) : Antécédents, Médication et
Examen objectif rendent maintenant chacun en liste à puces.

## 2026-08-18 — branche `selfhosted`, consigne dédiée au pipeline JSON

- **Nouvelle consigne, réglage à part** : `general_prompt_json_fr`/`_en`
  (`app.runtime_config`, valeurs par défaut `default_prompts.JSON_GENERAL_PROMPT_FR/EN`).
  `GENERAL_PROMPT_FR/EN` a été écrite pour l'ancien pipeline (une seule passe
  LLM → markdown) : le modèle y porte seul des règles de mise en forme
  maintenant imposées par le code (reproduction exacte des titres,
  remplacement des `{{...}}`, tableaux Markdown, numérotage, grammaire
  télégraphique d'Éléments à valider). La nouvelle consigne condensée ne
  garde QUE les décisions de contenu clinique (§0 proportionnalité, §1
  aucune invention, §2 correction de la transcription, §3 style/voix
  dictée, §4 style déclaratif) — jamais un remplacement de la consigne
  existante, qui reste utilisée par l'ancien pipeline avec sa propre valeur
  en base (personnalisée sur test.dictai.ca, jamais touchée).
  `main._generate_json_pipeline` utilise désormais `general_prompt_json`
  plutôt que `general_prompt`.
- **Régression trouvée et corrigée AVANT déploiement** (3 générations
  réelles contre la consultation #5, mistral-small-latest, comparées via
  `note_generations`) : la première version condensée fabriquait des
  interprétations cliniques à partir de valeurs numériques brutes non
  dictées comme telles — « Hypothyroïdie subclinique (TSH 3.22) »,
  « Anémie légère normocytaire (Hb 129) » — absentes du rendu avec la
  consigne d'origine. Ajout d'une règle explicite dans § 1 : interdiction
  d'inférer un diagnostic à partir d'un chiffre isolé (labo, signe vital,
  score) que le médecin n'a pas lui-même nommé.
- **Risque résiduel connu, non réglé par la consigne seule** : la 3ᵉ
  génération (après correctif) ne fabrique plus de diagnostic, mais ajoute
  encore un item d'Impression non dicté dérivé d'un chiffre (« HbA1c à
  6.4 % nécessitant optimisation du diabète ») — une violation d'une règle
  déjà présente dans les DEUX consignes (§1, « aucune recommandation qui ne
  figure pas dans la dictée »). Le validateur ne l'attrape pas : les items
  d'Impression ne sont pas actuellement soumis à `grounded_fields`/
  `check_grounding`, seulement les valeurs identifiées comme critiques
  (médicament, dose, date, nom). Consigné ici plutôt que masqué — c'est
  exactement le type de lacune que l'architecture (validateur + gabarit
  déterministes) est censée combler à terme, pas quelque chose qu'une
  formulation de consigne peut garantir avec un modèle plus faible. Piste
  de suivi : étendre le grounding aux items d'Impression/Plan.

## 2026-08-18 — branche `selfhosted`, numérotage déterministe + historique de génération

- **Impression/Plan rendus avec des puces au lieu d'une liste numérotée**,
  malgré la correction du cramming la même journée (voir entrée précédente) :
  le renseignement « numéroté vs à puces » n'existait nulle part dans le
  schéma — `note_renderer` numérotait toujours en dur avec des tirets. Ajout
  d'un marqueur de gabarit explicite `{{liste numérotée}}` (voir
  `note_schema.LIST_STYLE_MARKERS`, `LayoutSpec.list_style`) placé sous
  IMPRESSION/PLAN dans les quatre gabarits verrouillés (`default_templates.py`)
  et dans les deux gabarits « (FD) » de test.dictai.ca (patch DB direct, pas
  de migration : seuls ces deux gabarits en avaient besoin) — jamais déduit du
  texte libre des consignes, qui n'est pas une source fiable pour une décision
  de rendu. `note_renderer` numérote désormais lui-même (« 1. », « 2. »...),
  le modèle n'a plus qu'à choisir « items distincts (tableau JSON) » ou
  « prose continue (chaîne) ».
- **Double numérotation** (vu réellement, test.dictai.ca, mistral-small-latest :
  « 2. 1. Trouble délirant... ») : malgré la consigne de ne jamais écrire de
  numéro/puce soi-même dans un item de tableau, le modèle le fait parfois
  quand même. `note_renderer` retire désormais tout marqueur `1.`/`1)`/`-`/`•`
  en tête d'item AVANT de renuméroter — inconditionnellement, y compris pour
  les listes à puces (un item qui s'auto-numérote dans une liste à puces est
  le même bogue).
- **Historique de génération** (branche `selfhosted` UNIQUEMENT, jamais en
  production) : nouvelle table additive `note_generations`
  (`app.database.NoteGeneration`) — une ligne par tentative de génération
  (pipeline, fournisseur, modèle, variante de consigne, markdown, problèmes
  du validateur), insérée en plus de (jamais à la place de) l'écrasement
  habituel de `consultations.generated_markdown`/`edited_markdown`. But :
  comparer des itérations de gabarit/consigne sans qu'une régénération
  n'efface la précédente. Purement un outil de mise au point, jamais lu par
  le pipeline de génération lui-même.

Testé par `tests/test_note_pipeline.py` (36 cas) et par génération réelle
contre la consultation #5 (test.dictai.ca, gabarit « Consultation - Gériatrie
(FD) », mistral-small-latest) : PLAN et IMPRESSION rendent maintenant
« 1. »/« 2. »/« 3. »... proprement, sans double numérotation.

## 2026-08-18 — branche `selfhosted`, listes numérotées écrasées sur une ligne

- **Impression/Plan rendus en un seul bloc au lieu d'une liste numérotée**
  (vu réellement, test.dictai.ca, mistral-small-latest : « 1. X. 2. Y. 3.
  Z. » sans saut de ligne). La consigne JSON (`note_extraction.py`) ne
  précisait pas comment encoder plusieurs items dans une valeur de
  `sections` — précisé : tableau JSON, ou chaîne avec de VRAIS sauts de
  ligne entre items. Ajout d'un filet mécanique côté validateur
  (`check_cramped_lists`) qui détecte plusieurs marqueurs « N. » sur une
  même ligne et déclenche une réparation ciblée.

Testé par `tests/test_note_pipeline.py` (31 cas).

## 2026-08-18 — branche `selfhosted`, correctifs post-test réel (mistral-small-latest)

- **Rubrique avec contenu propre ET sous-rubrique imbriquée** (ex. MÉDICATION
  ACTUELLE + ### ALLERGIES) : `note_renderer`/`note_extraction` supposaient
  qu'une rubrique était SOIT prose SOIT conteneur de sous-rubriques, jamais
  les deux — MÉDICATION ACTUELLE ressortait vide, seule ALLERGIES avait du
  contenu. Nouvelle clé réservée `__contenu__` (`note_renderer.OWN_CONTENT_KEY`).
  Gabarits « Consultation Médicale Générale » et « General Medical
  Consultation » alignés sur le même schéma imbriqué que Gériatrie
  (`## MÉDICATION ACTUELLE` / `### ALLERGIES`, plus « MÉDICATION ACTUELLE ET
  ALLERGIES » fusionné).
- **Texte de remplissage** : le filtre ne couvrait que les exemples cités mot
  pour mot dans la consigne (« Non servi »...) ; un modèle plus faible a
  écrit « non dictée » en valeur de champ, non détecté. Filtre élargi à une
  famille de formulations plutôt qu'à une liste fermée.
- **Éléments à valider — corrections aberrantes** : deux cas vus réellement —
  une « correction » identique au terme dicté (aucune information, bruit
  pur) et le mot « à confirmer » écrit DANS le champ `correction` lui-même
  (produisait « → correction apportée : à confirmer »). Les deux sont
  maintenant auto-corrigés (`fix_elements_a_valider_corrections`).
- **Indices de forme du gabarit transmis au modèle** : les lignes
  d'instruction entre accolades (`{{Phrase résumé}}`...), retirées du rendu
  depuis le dernier correctif, étaient jusque-là purement perdues. Elles
  alimentent maintenant le schéma JSON envoyé au modèle comme indice de
  forme pour la rubrique correspondante, au lieu d'être du poids mort.

Testé par `tests/test_note_pipeline.py` (28 cas). Trouvé en testant deux
générations réelles sur test.dictai.ca avec un modèle self-hosted-like
(`mistral-small-latest`, pas Gemini) — voir mémoire de session pour le
détail des deux consultations testées.

## 2026-08-18 — branche `selfhosted` (non publié, test.dictai.ca uniquement)

- **Pipeline JSON branché sur `/api/generate`, derrière un réglage panneau**
  (`note_pipeline_json`, Modèle de langage, désactivé par défaut).
  `api_generate` choisit entre le pipeline JSON (`_generate_json_pipeline`,
  `app/main.py`) et l'ancien pipeline markdown (`_generate_and_publish`)
  selon ce réglage — production inchangée (l'image prod ne porte pas ce
  code). Trouvé et corrigé en testant sur une vraie dictée avant ce
  branchement : une fuite des consignes de gabarit entre accolades
  (ex. `{{Phrase résumé}}`) dans le rendu (`note_schema.parse_layout`
  distingue maintenant `kind="instruction"` de `kind="literal"`). Limites
  connues, hors périmètre pour l'instant : pas d'audio seul, pas de
  contexte/instructions ponctuelles, pas de jetons d'usage remontés pour ce
  chemin (voir README §13).


## 2026-08-17 — branche `selfhosted` (non publié, non déployé sur `/api/generate`)

- **Nouveau pipeline de structuration en JSON, en parallèle de l'actuel**
  (`app/note_schema.py`, `note_extraction.py`, `note_validator.py`,
  `note_renderer.py` — voir README §13). Objectif : rendre mécaniquement
  vérifiable ce qui dépend aujourd'hui entièrement du respect des consignes
  par le modèle (grammaire d'Éléments à valider, texte de remplissage,
  rubriques vides, préservation de la voix à la première personne en
  Impression/Plan, ancrage des valeurs critiques contre la transcription) —
  préalable à l'évaluation de modèles de structuration auto-hébergés plus
  petits que Gemini 2.5 Pro. N'affecte PAS la production : `/api/generate`
  continue d'appeler `llm.generate_note_stream` sans changement. Testé par
  `tests/test_note_pipeline.py` (20 cas, aucune clé de fournisseur requise).

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