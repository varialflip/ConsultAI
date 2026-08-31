# Changelog

Changements livrés, entrées datées. À maintenir à chaque version publiée —
voir `/opt/dictai/AGENTS.md` (cycle de déploiement).

## 2026-08-31 — Grounding : approche « donner des outils au LLM »

*Le moteur orthographique réécrivait des noms bien transcrits en voisins
orthographiques lointains de la base BDP (« Monocore » → « nitrate de
miconazole »). Ajouter une exception par faux positif devenait intenable. Le
moteur ne réécrit plus inline que ce qu'il sait déterministiquement ; les
noms déformés restants sont résolus par le modèle de langage, qui reçoit les
candidats sûrs *et phonétiques* comme hints.*

- **Fini la correction inline inventive** : `normalize(..., inline_safe=True)`
  ne réécrit plus que les correspondances **exactes** (nom réel, non-feuille)
  et les garbles STT **seedés**. Toute résolution orthographique floue
  (« Monocore » → nitrate de miconazole, « comprimé » → acétaminophène) est
  laissée telle quelle dans le texte envoyé au modèle.
- **Le modèle de langage devient le résolveur** : la consigne générale (§ 4
  MÉDICAMENTS, fr/en) lui demande explicitement de reconnaître le
  médicament réel derrière le nom déformé (posologie + contexte clinique) et
  d'écrire le nom correct, en signalant en « Corrections et éléments à
  valider » ce qu'il ne peut pas trancher. Application : « Monocore 1,25 mg »
  → Monocor (bisoprolol) 1,25 mg ; « pantoloque 40 » → Pantoloc 40.
- **Hints structurés au modèle** :
  - la liste **sûre** du moteur (extraction `inline_safe`) accompagne la
    dictée dans un bloc `MEDICAMENTS_SOUPCONNES` ;
  - **candidats phonétiques** : le G2P français du moteur (arbre BK,
    `phonetiques_texte`) remonte pour chaque jeton non résolu et « dosé » le
    meilleur voisin phonétique (dist ≤ 3, sim ≥ 0,72, non feuille, non
    cosmétique), injecté dans un bloc `MEDICAMENTS_PHONETIQUES` étiqueté
    « à confirmer ». « dilote » → Dilaudid, « kitsapine » → quetiapine,
    « Antoloque » → Pantoloc, « phézothérodine » → fesoterodine.
- **Correctif arbre BK** : `phonetic_fr(" ".join(n))` (éclaté en caractères)
  rendait la phonétique inopérante — remplacé par `phonetic_fr(n)` + index
  inverse `bk_node`. L'index ortho est balayé une fois à la construction
  (~3 s au démarrage, une seule fois).
- **Liste Validation alignée** : `extract_med_items` passe aussi en mode sûr
  pour qu'aucune inventon du moteur n'apparaisse dans l'onglet Validation ni
  dans les hints — fini les fantômes du STT canonisés (diclofenac
  diethylamine, nitrate de miconazole).
- **Migration consigne en base** (ancrage « Liste pointée, nom + dose »,
  idempotente, consigne personnalisée intacte).
- **Régression** : sortie `normalize()` **non** `inline_safe` (mode de
  compat) inchangée sur les 4 transcripts de référence ; le mode sûr ne
  conserve que les corrections déterministes. Le transcript corrompu de la
  note 15 (« nitrate de miconazole » déjà écrit) se nettoie par
  **retranscription** (le code de retranscription passe aussi en mode sûr).

## 2026-08-31 — Grounding : fin des faux médicaments de prose et des équivoques de dose

*Suite à l'analyse de la note 13 (dictée réelle) : des mots de prose étaient
réécrits en noms de médicaments en raflant une posologie voisine, et des noms
hallucinés par le STT étaient canonisés dans la liste de l'onglet
« Validation » puis repris par le LLM.*

- **Posologie directionnelle** : un marqueur de dose placé **après** un nom
  ne suffit plus, seul ou presque, à valider sa substitution — un nom est
  réécrit si la dose vient d'**avant** (la preuve la plus probante), d'un
  chiffre collé, ou d'une région de liste confirmée. La prose « de façon
  régulière HS » ne transforme plus « régulière » en REGULEX (docusate), le
  « HS » restant au vrai médicament qui précède (quétiapine).
- **La fenêtre de dose arrière s'arrête à la ponctuation de phrase** :
  « … au coucher régulièrement. Prochain point. … » — le « coucher » d'avant
  le point ne crédite plus « point. » (fini le faux « point. → diclofenac
  diethylamine »).
- **Gate de confiance mot-à-mot renforcé** : la haute confiance STT d'un mot
  le protège d'une substitution floue quand elle n'est portée que par une
  dose **en avant** ; seules une preuve arrière, une ancre verbale ou une
  région confirmée la lèvent.
- **Liste anti-fantôme (Validation / import)** : un nom de médicament n'entre
  dans la liste que muni d'un signal de dosage (posologie captée, chiffre de
  dose adjacent, région de liste confirmée, nom composé). Un nom nu
  halluciné par le STT et canonisé (« diclofenac diethylamine »,
  « naproxene ») n'apparaît plus dans la liste — le texte inline reste
  intact.
- **Consigne générale (§ Impression et Plan, fr/en)** : un nom de médicament
  dicté sans verbe d'action, sans dose ni voie n'est jamais écrit comme une
  ligne de Plan affirmée — il passe en « Corrections et éléments à valider ».
  Migré en base (ancienne puce intacte exigée — une consigne personnalisée
  n'est pas écrasée).
- **Collisions exactes bannies** : la conjonction anglaise « and » (le STT
  sort parfois des tokens EN, « on and off ») frappait exactement la marque
  BDP « AND » (naproxène) ; ajoutée à `FRENCH_STOP`, comme les adjectifs de
  fréquence (« régulière », « aiguë », « couchée »).
- **Régression** : aucun changement sur les 4 transcripts de référence
  (`dictee-1`, `dictee-6`, `consult7`, `consultai4-gemini`) à l'exception
  voulue du retrait d'un nom nu de prose hors liste (« apixaban à vie » dans
  les antécédents). Port appliqué aux deux modules (`app/med_grounding.py` et
  `med_grounding/match_meds.py`).

## 2026-08-31 — Endpoint personnalisé : envoi en un seul bloc par défaut

- **Plus de découpage en tranches par défaut** : `custom_stt_chunk_seconds`
  est désormais vide (auparavant 60 s), l'audio part en une seule requête —
  le découpage n'était utile que pour les endpoints plafonnant la durée par
  passe (Parakeet/ONNX) et se règle toujours explicitement dans le panneau.

## 2026-08-31 — Confiance STT mot-à-mot envoyée au LLM (génération sans audio)

- **Mieux corriger sans inventer** : quand le fournisseur de note ne reçoit pas
  l'audio, la confiance `words[].confidence` du STT (persistée tranche par
  tranche) est transmise au modèle — seuls les mots entendus avec incertitude
  (< seuil) sont signalés, et le modèle y concentre son effort de correction,
  en marquant « à confirmer » les doutes persistants. Le reste de la
  transcription (mots sûrs) est préservé fidèlement, prévenant la sur-correction.
- Les noms de médicaments déjà corrigés par le grounding déterministe sont
  exclus du signalement : le modèle ne re-discusse pas une correction auditable.
- Sans `words[]` (fournisseur STT qui n'en fournit pas) ou sans confiance
  persistée, le comportement historique s'applique (aucun signal, sans
  ralentissement).

## 2026-08-31 — « Validation » : 2e passe de validation pour tous les fournisseurs LLM

*La 2e passe de validation n'était utilisable qu'avec Gemini (seul chemin qui
recevait l'audio). Elle s'applique désormais à n'importe quel fournisseur de
modèle de langage, avec un modèle de 2e passe choisi par fournisseur dans le
panneau d'administration.*

- **Disponible pour tous les modèles** : fin de la restriction Gemini. Pour les
  fournisseurs qui reçoivent l'audio (Gemini, Qwen Omni, point de terminaison
  personnalisé, OpenRouter), l'audit croise toujours la note avec l'AUDIO
  (source de vérité) ; pour les autres (Anthropic, OpenAI, Cohere, Mistral), il
  croise la note avec la **transcription**, sous des consignes volontairement
  plus permissives (la transcription du moteur vocal peut se tromper : seuls
  les écarts certainement prouvés sont signalés).
- **Modèle de 2e passe par fournisseur** : nouveau champ « Modèle de 2e passe
  (Validation) » sous chaque fournisseur de Note (Note → *fournisseur*). Laissé
  vide, c'est le **même modèle que la mise en forme** qui audite ; il alimente
  le sous-menu « Modèles disponibles ».
- **Journalisation de l'usage corrigée** : la consommation de la 2e passe est
  dorénavant imputée au fournisseur et au modèle réellement utilisés (et non
  plus systématiquement « gemini »).
- Sans audio **ni** transcription à croiser, la bascule affiche toujours un
  « rien à signaler » immédiat (pas de roue qui tourne dans le vide).

## 2026-08-31 — Correction des médicaments : gate de confiance mot-à-mot (STT custom)

*L'endpoint STT personnalisé (`response_format=json`) renvoie désormais la
confiance de chaque mot (`words[].confidence`). Elle devient un levier pour
distinguer les vrais noms de médicaments déformés des mots de prose à
l'orthographe voisine : un mot dicté avec une confiance très élevée n'est
jamais « corrigé » par simple proximité orthographique, sauf si une dose, un
verbe d'administration ou une liste de médicaments le porte.*

- **`CONF_HARD_FLOOR = 0.92`** (mesuré sur 8 dictées réelles, corpus Cohere
  MLX) : substitution orthographique *floue* d'un token confiant refusée hors
  contexte fort (dose/ancre/région de liste). Élimine les faux positifs de
  prose `laisse → Latisse`, `diabète → Diabeta`, `d'autres → Dobutrex` (conf
  ~1,00) sans perdre un seul vrai nom déformé du corpus (`pivale → Epival`,
  `neurontain → Neurontin`, `l'aldol PRN → Haldol`, `aspirine 80`…). Sans
  confiance disponible (fournisseurs sans `words[]`), comportement historique
  inchangé.
- **Propagation de la confiance** : endpoint custom demande
  `response_format=json` et remonte `words[]` ; `dictation` stocke une carte
  `norm_phon → conf` par part (persistée en session) et l'applique au
  grounding des frontières, à la retranscription et à l'extraction de la
  liste (`extract_med_items(text, conf=…)`) — un faux positif bloqué dans le
  texte ne resurgit donc pas dans la liste de l'onglet Validation.
- **Seuil réglable** : constante unique `CONF_HARD_FLOOR` dans
  `app/med_grounding.py` (pas de champ admin : réglé au code, comme le reste
  du moteur).
- **Garbles multi-mots commençant par un nombre** : « 13 IBA » (homonyme
  phonétique de **Tresiba**, « treize » ≈ « trési ») est désormais consommé
  comme un **nom unique** (`exact_garble_num`, clé conservant les chiffres) —
  le « 13 » n'est pas une dose mais l'amorce du nom. Le mot isolé « IBA » en
  prose reste inchangé. Posologie captée (« 13 IBA, 9 UI HS » → Tresiba, 9 UI
  HS).
- **Garble seedé franchissant `FRENCH_STOP` à condition de dose** : « faire »
  (consult 10, « Médication, faire 300 mg PO Q2J HS » = sulfate ferreux) est
  corrigé en **« fer »** uniquement quand une dose est voisine — en prose
  (« va faire des courses ») le verbe reste ignoré. Même garde que les noms
  courts (`is_leaf`).
- **Liste de Validation : noms sans chiffre de dose** — fini « bisoprolol 2 —
  5 mg DIE » / « calcium 500 — mg PO » : `_lookup_exact` ne reconnaît plus un
  bigramme « nom + chiffre » comme un composé (le chiffre est la posologie,
  jamais le nom). La posologie conserve la décimale entière (« bisoprolol 2,5
  mg DIE »), les items sont propres dans Validation et à l'import.
- **Liste de médicaments isolée par des lignes vides dans le texte** : le
  transcript corrigé met une ligne vide avant et après le bloc de la liste
  (région `proactive` contenant un `medlist`, blocs denses séparés par ≤ 6
  jetons fusionnés) — on voit d'un coup d'œil où la normalisation a agi.
  Reste hors encadrement : la prose des antécédents (« anémie, FA sous AOD,
  HTA… ») et les consultations sans liste.

## 2026-08-30 — Correction des médicaments pendant la dictée (grounding, liste pointée)

*Le STT par tranches coupe parfois un mot à la jointure de deux segments, et
les noms de médicaments déformés arrivent telles quels. Avec le nouveau
réglage admin « Correction des médicaments » (groupe Dictée,
`DICTATION_GROUNDING`), l'application réécoute la frontière et normalise les
noms contre la base canadienne BDP livrée dans l'image (moteur déterministe,
sans appel réseau).*

- **Stabilisation audio par l'arrière** : chaque frontière non encore stable
  est ré-écoutée en continu (découpage aux silences), le texte des segments
  concernés est remplacé (SSE `transcript_correct`, « le texte se corrige par
  l'arrière » en ~3-6 s). Prévu pour un point de terminaison STT custom
  auto-hébergé (aucune limite de taux).
- **Liste pointée des médicaments** : noms normalisés + posologie (puces,
  jamais de pointillés) sous le transcrit et **en haut de l'onglet
  Validation** (SSE `med_grounding` puis `med_grounding_result`, restaurée au
  rechargement via `med_grounding_json`).
- **Fonctionne aussi sur l'audio importé et la retranscription** (liste
  recalculée sur le texte complet, réponse `med_items` + SSE).
- **Moteur** : `app/med_grounding.py` (port du matcher de `med_grounding/`),
  base `app/meds.sqlite` livrée dans l'image, dépendance `rapidfuzz`.
- **Base assainie** : bannissement de la base (`ban_terms.py`) du mot
  « gériatrique » et de **tous les écrans solaires** (SPF/FPS, ÉCRAN, principes
  UV : octisalate, avobenzone, octocrilene, dioxyde de titane, oxyde de zinc…) —
  ces feuilles de marques (MINUTES, BASE, RAPIDE, SUPPORT…) provoquaient des
  faux positifs en prose ; longs verbes corrects conservés (sunitinib,
  salbutamol, furosemide…).
- Bascule automatique vers l'onglet Validation sur grand écran après
  génération de la note (étendue aux médicaments).
- **Déploiement** : nouvelle dépendance (`rapidfuzz`) et nouvelle donnée de
  référence → **reconstruire l'image** (pas un simple redéploiement) :
  `docker compose build consultai-test` puis
  `up -d --force-recreate consultai-test`.

**Complément — base de médicaments resserrée sur le périmètre gériatrique (-31 %).**
La base BDP était pleine de catalogue non pertinent (cosmétiques, vaccins,
solutions IV, homéopathie…). Nouveau script `med_grounding/prune_scope.py` :
à partir des classes ATC et des annexes BDP, il retire ~7 243 marques
(23 034 → 15 791 lignes) — désinfectants mains, émollients/huiles, anti-acné,
antisudorifiques, shampoings, verrues, soins dentaires/fluor, pastilles et
rince-bouche, antiprurigineux et rubéfiants OTC, sirops toux/rhume OTC,
décongestionnants nasaux, multivitamines/suppléments, contraception,
obstétrique, fertilité, anthelminthiques, anesthésiques, gaz médicaux,
solutés IV/dialyse, diagnostics et produits de contraste, extraits
allergéniques, immunoglobulines, homéopathie, et marques cosmétiques/
pédiatriques ou annulées sans classe ATC. Une liste de sauvegarde préserve ce
qui se dicte réellement en gériatrie (vaccins gériatriques, diclofénac
topique, Xylocaïne/EMLA, Zincofax, Peridex/chlorhexidine, codéine,
Nix-Stromectol…), et **toutes les anciennes marques** (MAXERAN, ARICEPT,
LASIX, PANTOLOC, LOSEC, COUMADIN, ELTROXIN, SYNTHROID…) restent matchées. Les
marques retirées cessent seulement d'être normalisées — le générique reste
toujours catalogué (lignes distinctes). Effets : initialisation du moteur et
normalisation ~3× plus rapides (base chargée en mémoire à chaque démarrage),
735 alias de marques parasites en moins (SET, PEOPLE, MIN, MINUTES…), zéro
changement sur les 4 transcripts de référence. Corrections associées :
**`fix_inactive_otc.py`** répare le drapeau `is_otc` des marques annulées
(l'extrait BDP inactif écrit « NON-PRESCRIPTION DRUGS », `build_db.py` ne
testait que « OTC » → les OTC annulés sortaient leur nom de marque au lieu du
principe actif ; correction définitive dans `build_db.py`) ; base
`ZINCOFAX` réglée sur « oxyde de zinc » ; gardes de prose ajoutées au moteur
(`jasmin`, `lhopital`).

## 2026-08-30 — STT custom : accepte un transcript en texte brut (endpoint OpenAI-compatible)

*Certains serveurs STT auto-hébergés (ex. CohereLabs/cohere-transcribe-03-2026
sur le serveur de l'instance de test) répondent à
``POST /audio/transcriptions`` en ``text/plain`` avec le transcript nu, et non
en JSON ``{"text": …}`` comme Whisper. La lecture stricte du JSON faisait
échouer toute transcription (« Expecting value: line 1 column 1 »).*

- `_post_openai_compatible` tolère les deux formats : JSON si décodable, sinon
  le corps brut est pris comme transcript. Une réponse vide produit une erreur
  explicite (« Réponse vide ») plutôt qu'un échec de décodage opaque.
- Redéploiement : commit simple sur `selfhosted` + `--force-recreate
  consultai-test` (aucun tag, aucune image — source servie par le bind mount).

## 2026-08-30 — Instance de test `test.dictai.ca` (branche `selfhosted`)

*Branche `selfhosted` recréée sur `main` (l'ancienne est archivée sous
`selfhosted-archive-2026-08-30`), puis miroir de l'instance de production
déployé sur `test.dictai.ca` via un git worktree dédié
(`/home/opc/ConsultAI-selfhosted`). Configuration et déploiement :
`/opt/dictai/AGENTS.md`.*

- **Instance de test** : service `consultai-test` (même image épinglée, code
  du worktree `selfhosted` monté en lecture seule), données séparées
  `data/consultai-test` (copie de `consultai.db`, `audio/`, `dictations/`).
- **Accès** : `test.dictai.ca` → Caddy (mêmes garde-fous que l'app) →
  `turnstile-gate` → `consultai-test:8000`.
- **Connexion** : même fournisseur Pocket ID de production (`login.dictai.ca`),
  client OIDC « Dictai.ca test » dédié, redirect `test.dictai.ca/auth/callback`
  — mêmes comptes que l'app (mêmes usernames Pocket ID).
- Redéploiement : commit simple sur la branche + `--force-recreate
  consultai-test` (voir `/opt/dictai/AGENTS.md`).

## 2026-08-28 — Audit « Validation » : affichage garanti côté émetteur (relecture périodique)

*Sur une régénération réelle (consultation 1), la section « Validation - 2e
passe » n'apparaissait qu'après un rechargement de page : le serveur avait
publié et persisté l'audit, mais l'onglet émetteur ne reçoit normalement le
résultat que par un seul événement SSE ``verification_result`` — une chute de
la connexion SSE (pile proxy, file saturée) laissait la section vide jusqu'au
F5.*

- **Relecture périodique pour l'émetteur** : dès la fin de la génération
  (``showSecondPassPending``), l'onglet relit la consultation persistée toutes
  les ~5 s ; l'audit s'affiche dès qu'il y est écrit, sans saut d'onglet. Le
  chemin SSE gagnant coupe le sondage (``stopSecondPassPending``) : aucun doublon.
- **`verification_json` réinitialisée à chaque génération** : une régénération
  efface l'audit de la note précédente au moment où la nouvelle note est
  persistée — la base ne porte jamais un audit périmé pendant la fenêtre de
  vérification (relecture poll ou rechargement en cours de régénération).
- Redéploiement : commit simple (aucun tag, aucune image, source servie par le
  bind mount).

## 2026-08-28 — Examen en liste pointée : consigne générale explicite

*La rubrique « Examen » devait être une liste pointée (tous les gabarits le
prescrivent), mais la consigne générale laissait le choix à la prose : sa règle
montrait un exemple rédigé en phrase (« directement « Calme, collabore et
orientée » ») et la consigne générale est placée APRÈS celles du gabarit, donc
prioritaire. Avec un modèle rapide, la rubrique sortait en paragraphes (notes
réelles du 2026-08-28).*

- La règle exige désormais **une puce « - » par ligne** et interdit
  explicitement le paragraphe suivi, FR et EN (`app/default_prompts.py`, et
  valeur correspondante appliquée en base `general_prompt_fr` /
  `general_prompt_en` — la base fait foi à l'exécution).
- Renfort (même date) : **aucun score dicté n'est omis** — MMSE, MoCA, MIS et
  tout test/score dicté figurent dans la liste de l'Examen avec leur date, **y
  compris les scores ANCIENS dictés dans la même dictée** (pour comparer
  l'évolution). Un score douteux reste dans la liste ET est signalé « à
  confirmer » en Corrections. Appliqué à la consigne générale (FR/EN) **et** aux
  gabarits verrouillés 4, 8, 9, 10 (source `app/default_templates.py` + lignes
  en base). Motivation : note réelle du 2026-08-28 où le modèle pro (audio
  direct) a omis les MMSE/MoCA/MIS pourtant dictés, alors que l'audit
  « Validation » les signalait comme omissions.

## 2026-08-28 — Contournement du STT : la transcription conservée guide le modèle

*En contournement du STT (audio direct, Gemini/Qwen/…), la note se générait à
partir de l'audio seul — la transcription conservée pour l'affichage
(`<fournisseur>_bypass_stt_keep_transcript`) était délibérément exclue du
prompt. Sur un enregistrement long, même un modèle pro oubliait des éléments
dictés (constaté en réel : discussion génétique, TEP/p-tau217, délai de suivi,
prénom du patient omis, signalés par l'audit « Validation »), alors que la
transcription conservée les contenait.*

- Nouveau mode **guide** : quand une transcription a été conservée pendant
  l'enregistrement, elle accompagne désormais la note (`_AUDIO_GUIDED_NOTE`) —
  l'audio reste la source autoritaire (il fait foi en cas de divergence), le
  texte sert de filet anti-omission. Sans transcription conservée, le
  comportement historique (audio seul) est inchangé.
- `transcript_used` devient vrai dans ce mode (la transcription a pris part à
  la note) : `stt_used` s'affiche alors comme avant.
- Appliqué aux deux chemins (`generate_note` et `generate_note_stream`) ; texte
  d'aide du réglage « Conserver une transcription… » mis à jour.

## 2026-08-28 — Extraction des métadonnées : retentative après réponse muette

*L'extraction des champs d'identification (date, raison, demandeur…) échouait
silencieusement quand le modèle rapide renvoyait une réponse vide ou illisible
— observation réelle avec `moonshotai/kimi-k3` et `z-ai/glm-4.7` sur un appel
isolé. Une retentative unique récupère le cas transitoire ; si les deux
tentatives échouent, les champs restent vides comme avant (jamais bloquant).*

## 2026-08-28 — Génération : garde-fou anti-blocage du flux (modèle qui se fige en pleine réflexion)

*Une génération avec un modèle à raisonnement (observé : `z-ai/glm-4.7-flash`
via OpenRouter) pouvait se figer sur « Raisonnement du modèle… » : le
fournisseur cessait d'envoyer et l'écran restait bloqué jusqu'au timeout global
(5 min). Deux garde-fous.*

- **Chien de garde de blocage** : sans aucun progrès (texte ou raisonnement)
  pendant 90 s, la génération s'interrompt avec un message explicite —
  « … n'a plus rien envoyé pendant la génération, réessayez ou réduisez
  l'effort de raisonnement ».
- **Timeout de lecture borné** pour les flux OpenAI-compatibles (OpenAI,
  OpenRouter, point de terminaison personnalisé, Qwen Omni) : 120 s entre deux
  morceaux au lieu du timeout global de 300 s — une coupure silencieuse se
  manifeste en 2 min au lieu de 5.

## 2026-08-28 — « Modèles disponibles » étendu à la reconnaissance vocale

*Le bouton « Modèles disponibles » (panneau d'administration) n'était proposé
que sur l'onglet du modèle de langage. Il l'est désormais aussi sur l'onglet
Dictée : il interroge le fournisseur de transcription consulté et propose ses
modèles dans le champ « Modèle ».*

- **Fournisseurs couverts** : Deepgram (liste `type=stt`), Cohere, Mistral
  (modèles `voxtral*`), OpenAI (Whisper / `*transcribe`), point de terminaison
  personnalisé (`GET {base_url}/models`, clé facultative — Parakeet local
  compris) et OpenRouter (modèles acceptant l'audio). Google, Soniox,
  AssemblyAI et Modulate n'exposent pas de liste : le panneau le signale et le
  nom se saisit à la main.
- **Diagnostic** : si le modèle configuré ne figure pas dans la liste, un
  avertissement indique que la transcription échouera — même mécanique que le
  contrôle du modèle de langage.
- Nouvelle route `/api/stt/models` (jumeau de `/api/models`).

## 2026-08-28 — Fournisseur OpenRouter : note (audio direct possible) et STT

*Ajout d'OpenRouter comme fournisseur de modèle de langage **et** de
reconnaissance vocale, pré-populé avec la clé du compte. Modèle par défaut :
`thinkingmachines/inkling-small` (Thinking Machines), multimodal, open-weight.*

- **Modèle de langage (onglet Note → OpenRouter)** : même panoplie que le point
  de terminaison personnalisé — clé dédiée (`openrouter_api_key`, une seule clé
  pour la note et le STT), modèle (`openrouter_model`,
  `thinkingmachines/inkling-small` par défaut), modèle rapide, température,
  budget de sortie (`openrouter_llm_max_tokens`, 32768 par défaut — inkling
  raisonne, une relance automatique double le budget si la pensée le sature),
  effort de raisonnement (`openrouter_llm_reasoning_effort`), **audio joint**
  (`openrouter_send_audio`, format OGG/MP3/WAV — OGG vérifié accepté) et
  **contournement du STT / note directe** (`openrouter_bypass_stt`) : l'audio
  part seul au modèle, comme avec Gemini.
- **Reconnaissance vocale (onglet Dictée → OpenRouter)** : la transcription
  passe par `/chat/completions` avec une part audio `input_audio` (OpenRouter
  refuse inkling-small derrière `/audio/transcriptions` — constaté 2026-08-27).
  Modèle `openrouter_stt_model`, langue `openrouter_stt_language`. Le lexique
  clinique prioritaire est passé en consigne (liste bornée, faute d'API
  d'adaptation).
- **Env** : `OPENROUTER_API_KEY` (ajoutée à `/etc/dictai/.env` pour ce
  déploiement). Tarifs par défaut ajoutés à `pricing.py` (texte, cache, audio).
- ⚠ **Confidentialité** : OpenRouter est un service cloud — l'audio (note
  directe ou STT) quitte alors la machine. **Aucun mode par défaut ne
  l'utilise** : l'activer dans le panneau est une décision de conformité
  (ÉFVP § 5, 1.7).
- *Validation réelle* : transcription STT (note test #5, 189 s) jugée
  **supérieure à Parakeet** sur plusieurs points (« 146 sur 91 », « logopénique »,
  « Reisberg 4 ») ; note directe structurée fidèle. Faiblesse connue : le modèle
  **invente un numéro de dossier** s'il est inaudible — point à surveiller.

## 2026-08-28 — Consigne générale : l'erreur de reconnaissance est phonétique, pas une faute de frappe

*La consigne générale (§ 2) gagne un § 2.0 qui explicite le mode d'erreur de la
reconnaissance vocale, pour que la correction ne soit plus un compromis entre
« corriger » et « ne rien inventer ».*

- **Erreurs phonétiques, pas fautes de frappe** : la transcription vient d'une
  reconnaissance vocale, pas d'un texte tapé — le modèle doit chercher des
  homophones (souvent des mots courants parfaitement orthographiés, ex. « un
  casseur de saint droit ») et non des fautes de frappe.
- **Corriger ≠ ajouter une information** : une correction rétablit le mot dicté
  (substitution des mots mal captés par le terme voulu) ; elle n'introduit aucun
  fait jamais dicté. Doute → règle des deux lectures (lecture la plus probable
  dans le corps, autre lecture en Corrections et éléments à valider).
- Migration en base pour la consigne générale fr/en (la consigne stockée reçoit
  le nouveau § 2.0) ; une consigne personnalisée dans le panneau n'est pas
  modifiée — la règle s'ajoute alors depuis le panneau.

## 2026-08-27 — Consigne générale : aucune omission dans l'Impression ni le Plan

*Correctif suite à l'audit « Validation » d'une note réelle (suivi gériatrie,
M. Framand) : deux omissions significatives y étaient signalées — l'impression
subjective du médecin (« j'ai l'impression qu'il se détériore au niveau
amnésique », malgré un MMSE stable voire amélioré) et le délai de suivi dicté
(« à revoir à six mois ») avaient été écartés de la note générée.*

- **Consigne générale (§ 3 Impression et Plan) enrichie** d'une règle
  explicite « aucune omission dans l'Impression ni le Plan » :
  - l'Impression conserve **toute** impression, hypothèse ou jugement clinique
    dicté, même subjectif, même contradictoire avec un résultat objectif — les
    deux faits sont conservés, le contraste n'est jamais résolu ni réduit au
    seul résultat (« MMSE stable voire amélioré, mais j'ai l'impression qu'il
    se détériore au niveau amnésique ») ;
  - le Plan conserve **toute** action ou recommandation dictée sur sa propre
    ligne numérotée — délai de suivi (« à revoir dans 6 mois »), examen
    demandé, référence, renouvellement, cessation, congé — même brève, même
    sans verbe. Un délai de suivi est une décision clinique, jamais écarté,
    fusionné ni résumé.
- **Applicable aux consignes déjà en service** par migration (ancienne puce
  intacte exigée — une consigne personnalisée n'est pas écrasée). À valider
  par régénération d'une note dictée avec impression subjective et délai de
  suivi.

## 2026-08-27 — Onglet du panneau de dictée choisi selon la note à chaque ouverture

*En ouvrant une consultation (clic « Suivre » d'une dictée commencée sur un
autre appareil, « Mes brouillons », relecture après génération), l'onglet du
panneau de transcription restait sur son état précédent : suivre une dictée en
cours pouvait ouvrir sur l'onglet « Validation », et rouvrir un brouillon noté
restait sur « Transcription brute ». Le choix est désormais dérivé de l'état
de la note.*

- **Pas de note générée → onglet « Transcription brute »** : en suivant une
  dictée en cours, on voit la transcription arriver en direct ;
- **Note déjà générée → onglet « Validation »** dès que la rubrique
  « Corrections et éléments à valider » ou l'audit a quelque chose à y
  montrer (sinon on reste sur « Transcription » plutôt que d'afficher un
  onglet vide) — même règle que la bascule du post-génération sur grand
  écran ;
- Règle uniforme appliquée à **toute** ouverture d'un brouillon via
  `loadDraft` : « Suivre », liste « Mes brouillons », fin de dictée récupérée,
  événement `generated`/`consultation_patched`, rechargement après conflit de
  synchronisation. L'onglet cliqué manuellement n'est jamais forcé.

## 2026-08-27 — « Texte simple » : colonnes des médicaments en écoulement continu

*Quand un médicament se repliait sur deux lignes, la colonne opposée laissait
une « cellule » vide (visible même dans Notepad, ex. sous Lipitor). Les deux
colonnes s'écoulent désormais indépendamment : une entrée repliée ne crée plus
aucun vide, le contenu de la colonne suivante remonte dans le trou.*

- **`renderMedsColumns`** : les entrées s'empilent verticalement dans chaque
  colonne (coupée en deux moitiés, ordre du modèle préservé) puis les deux
  colonnes sont intercalées ligne par ligne — plus aucune rangée ne reste à
  moitié vide.
- **`wrapText`** : les lignes de continuation (retrait suspendu) sont repliées
  à `largeur − retrait` : aucune ligne ne dépasse la colonne, l'alignement de
  la colonne de droite reste exactement à 46 caractères même pour un repli
  « plein ». Insécables, lecture verticale et deux colonnes inchangés ; aucun
  coût en jetons.

## 2026-08-27 — Mode dictaphone repensé : fermeture étiquetée, push-to-talk, carnet

*Le calque dictaphone (téléphone retourné) gardait une colonne « gros bouton
seul » : fermeture ✕ minuscule et grise, aucun retour de transcription pendant
la dictée, un seul mode d'enregistrement (tap-tap). Il est repensé en quatre
zones, avec deux modes de conversation au choix.*

- **Fermeture étiquetée et grande** : pilule « Quitter » (fond contrasté,
  bordure, ombre) en tête de calque — impossible à rater ;
- **Push-to-talk** : contrôle segmenté « Toucher / Maintenir ». En mode
  Maintenir, presser le bouton central enregistre (ou reprend) et relâcher
  (ou glisser hors du bouton) met en pause. Relâcher ne conclut jamais — le
  ■ Terminer reste l'unique moyen de clore. Préférence persistée
  (`consultai.dictaphoneMode`), défaut Toucher ;
- **Carnet de transcription** : les 3 dernières lignes (texte committé +
  provisoire en cours de reconnaissance) s'affichent en direct sous le bouton,
  avec un placeholder quand la dictée n'a rien produit ;
- **Présentation** : fond en dégradé, bouton central harmonisé, minuteur en
  tête de calque ; nouvelles clés i18n (`dictaphone.close_label`,
  `mode_tap/mode_ptt`, `tap_hint/hold_hint`, `transcript_placeholder`,
  `mode_group_aria`), retrait de `dictaphone.hint_manual` ;
- **Fond du calque opaque** : le dégradé seul pouvait laisser le calque
  transparent (variable CSS de gradient non résolue selon le navigateur) —
  une base `bg-slate-900` solide est posée sous le dégradé, et les panneaux
  internes passent en fonds pleins ;
- **Bouton central agrandi en « squircle »** (192 → 256 px, coins arrondis
  `26 %` à la iOS) avec icônes harmonisées ; calque plus aéré (padding
  `p-5 sm:p-8`) pour éloigner les éléments des bords ;
- **Marges haut et bas du calque augmentées** : + 1,5 rem par-dessus la
  zone sécurisée (`calc(env(safe-area-inset-*) + 1.5rem)`) — l'en-tête et
  l'aide ne collent plus au bord de l'écran ;
- **Bouton central remonté** (≈ 40 px) : le corps du calque centre le bouton
  avec un fond élargi (`pb-20`), pour le rapprocher de l'en-tête ;
- **Waveform centrée entre le bouton et le carnet** : écart fixe entre le
  bouton et la courbe (au lieu d'un collage sous le bouton), le tout centré
  dans le corps du calque ;
- **Minuteur déplacé sous le gros bouton** (regroupé avec lui) ;
- **Bouton « Quitter » déplacé en bas à gauche** du calque, à côté du
  ■ Terminer (le minuteur a quitté l'en-tête, qui disparaît) ;
- **Waveform : cadence d'échantillonnage passée à 75 ms** (`WAVE_SAMPLE_MS`,
  au lieu de 55) — défilement plus posé.

## 2026-08-27 — Mode dictaphone manuel sur tous les mobiles

*Le mode dictaphone (téléphone retourné) existait en deux variantes : manuelle
sur iOS (bouton « Mode retourné » + ✕) et automatique sur Android (détection
du retournement par capteurs, boutons masqués). La détection automatique
exigeait une permission capteur sur Android, variait d'une plateforme à
l'autre et ne laissait aucun contrôle — elle est retirée. Un seul mode,
manuel, sur tous les mobiles.*

- **Bouton « Mode retourné »** affiché sur tout écran tactile : il ouvre le
  calque plein écran (gros bouton Enregistrer/Pause), tourné de 180° en CSS
  pour se lire à l'endroit une fois le téléphone retourné ;
- **Bouton ✕** toujours visible pour quitter — c'est désormais le seul moyen
  de sortir, plus de retournement auto ;
- **Fin de l'auto-détection** : écouteurs `deviceorientation` /
  `screen.orientation` / `orientationchange` et demande de permission capteur
  supprimés (app.js) ; l'aide du calque devient « Bouton ✕ pour quitter ce
  mode » ; la clé i18n `dictaphone.hint` (repli « retournez le téléphone »)
  est retirée.

## 2026-08-27 — « Texte simple » : rendu soigné (casse d'origine, glyphes Unicode)

*Le rendu texte restait « dactylo » : titres en majuscules, filets `=`/`-`,
puces `-`. Le DME accepte pourtant l'Unicode (accents, « µ ») : le rendu passe
en casse d'origine et se pare de glyphes soignés, sans rien changer à
l'alignement (toujours en espaces insécables).*

- **Titres en casse d'origine** (tels qu'écrits dans le gabarit) avec filets
  `═` (titre) et `─` (rubriques) au lieu de `=`/`-` ;
- **Puces `•`** dans les listes et dans les deux colonnes des médicaments ;
- **Filet horizontal `─`** ; tableaux et deux colonnes inchangés (NBSP).
  Conversion locale au navigateur, aucun coût en jetons.

## 2026-08-27 — « Texte simple » : alignement par espaces insécables (DME riche)

*Au collage dans le champ du DME, les suites d'espaces du rendu deux colonnes
étaient aplaties en une seule espace — le champ est un éditeur riche (HTML),
qui applique la règle CSS d'aplatissement des blancs ; un champ texte brut ne
le fait pas. Les colonnes des médicaments s'effondraient donc au collage.
L'alignement se fait désormais avec des espaces insécables (U+00A0), que le
rendu HTML préserve et qui gardent la même largeur qu'une espace en monospace.*

- **Rembourrage, écart entre colonnes et retrait suspendu en insécables** dans
  `renderMedsColumns` (médicaments) et `renderPlainTable` (tableaux de
  résultats) : les colonnes survivent au collage dans le DME. Les espaces
  ordinaires restent en place dans le texte (noms, posologies, cellules) —
  repli à 44 et recherche inchangés. Aucun coût en jetons ; s'applique au
  bouton « Texte simple » et au repli texte du copier « Mise en forme ».

## 2026-08-27 — « Texte simple » : médicaments sur deux colonnes

*Quand la note est copiée en texte simple pour le DME de l'hôpital (champ en
monospace), la rubrique Médicaments prenait beaucoup de place. Elle est
désormais rendue sur DEUX colonnes — lecture verticale : on descend la
colonne de gauche, puis celle de droite. L'ordre des médicaments, déterminé
par le modèle, est ainsi préservé à la lecture.*

- **Rendu sur deux colonnes** dans `markdownToPlainText` (conversion locale au
  navigateur, aucun coût en jetons) : la liste est coupée en deux moitiés
  (colonne de gauche = première moitié), chaque cellule est repliée à
  44 caractères en coupant de préférence sur les espaces. S'applique au bouton
  « Texte simple » et au repli texte du copier « Mise en forme ». Aucune autre
  rubrique (antécédents, examen, plan…) n'est touchée.

## 2026-08-27 — L'audit « Validation » réutilise enfin le cache de préfixe

*En pratique, l'audit « Validation » ne profitait presque jamais du cache de
préfixe implicite de Gemini : sur gemini-2.5-pro (Vertex), il se heurtait à
trois obstacles qui empêchaient la resservie du préfixe [consigne système +
audio] partagé avec la mise en forme — vérifiés un à un par des appels réels
(le compteur `cached_content_token_count` passait de 0 à ~2 000+ jetons après
chaque correctif).*

- **Le mode JSON cassait la resservie.** `response_mime_type="application/json"`
  et `response_schema` plaçaient l'audit dans une partition du cache distincte
  de celle de la mise en forme (hors mode JSON) : aucun préfixe partagé n'était
  resservi. Le JSON est désormais demandé par instruction dans
  `_AUDITOR_PROMPTS` (fr/en), et l'extraction est rendue tolérante
  (`_extraire_json` : clôtures de code markdown retirées, repli sur la tranche
  du premier « { » au dernier « } ») — le garde-fou déterministe est inchangé.
- **La température doit coïncider avec la mise en forme.** L'audit fixait 0,2 ;
  la mise en forme suit `active_temperature()` (0,0) : à 0,2 fixe, le préfixe
  n'était pas resservi. L'audit suit désormais `active_temperature()` lui aussi.
- **Le message doit avoir la même structure.** L'audit enveloppait le texte dans
  un `Content` explicite, produisant deux messages [audio] / [texte] ; il passe
  désormais la chaîne brute comme la mise en forme (`[Part(audio), texte]`), qui
  s'aplanti en un seul message [audio, texte] identique.
- Mesuré en réel : l'audit lancé juste après une mise en forme voit son préfixe
  [consigne + audio] resservi du cache (~2 000 jetons) au lieu de rien, et une
  seconde passe répétée resservie en quasi-totalité. Le coût d'entrée de la
  vérification redevient celui prévu par le tarif `token_input_cached_1m`.
- Aucun changement de fournisseur, de prix, de politique de données ni de
  stockage : ni EFVP ni README impactés.

## 2026-08-27 — La génération de la note se suit en direct sur tous les appareils

*Lancée depuis le téléphone, la génération n'était visible que sur l'appareil
émetteur : les `generation_chunk` / `generation_thought` exigeaient le jeton de
corrélation local, et le second appareil n'apercevait la note qu'au tout dernier
moment, sans aucune indication de progression.*

- **Tout onglet du même usager qui a la consultation ouverte suit désormais le
  flux en direct** : la note défile dans l'aperçu (raisonnement du modèle puis
  texte, révélés par le même mécanisme que l'émetteur), le toast de progression
  s'ouvre et traverse les mêmes phases — « Traitement en cours… »,
  « La note se génère… », puis « Validation en cours… » quand l'audit coule, et
  s'éteint au résultat final.
- L'émetteur garde exactement son comportement (jeton local) ; un onglet qui
  génère lui-même ignore les flux étrangers et referme d'abord le suivi en
  cours. Un « suiveur » qui change de consultation ou repart à zéro referme
  aussi la machinerie (spinner arrêté).
- Aucun changement de fournisseur, de prix, de politique de données ni de
  stockage : ni EFVP ni README impactés.

## 2026-08-27 — L'audit « Validation » suit la consultation sur tous les appareils

*Lancée depuis le téléphone et suivie sur un autre appareil, la vérification
ne rejoignait jamais l'onglet « suiveur » : son résultat n'était diffusé qu'à
l'onglet qui avait généré la note (le jeton de corrélation y était exigé), et
le second appareil ne voyait que la note et les corrections — l'audit
n'apparaissait qu'après un rafraîchissement.*

- **L'audit est désormais appliqué par tous les onglets du même usager** qui
  ont la consultation ouverte : le flux `verification_chunk` s'y affiche en
  direct et le `verification_result` final bascule le panneau sur l'onglet
  « Validation » comme sur l'émetteur. Un onglet qui a sa PROPRE vérification
  en attente continue d'ignorer les jetons étrangers (il attend la sienne).
- Un « skipped » n'est répercuté que sur l'émetteur (arrêt de sa roue) : un
  suiveur garde ce que la relecture du brouillon a déjà affiché.
- Aucun changement de fournisseur, de prix, de politique de données ni de
  stockage : ni EFVP ni README impactés.

## 2026-08-27 — Récupération d'un audit « Validation » manqué par le flux en direct

*Si la connexion SSE était interrompue au moment précis de la publication du
résultat (redémarrage du conteneur, coupure réseau), l'audit « Validation »
n'atteignait jamais l'onglet : la roue retombait au filet et la section restait
vide alors que le serveur avait pourtant persisté le JSON.*

- **Relance à chaque (ré)ouverture du flux en direct** : si une vérification
  est toujours en attente (`verificationPendingToken` posé) et que le serveur
  a déjà écrit l'audit, la consultation est relue et le résultat affiché sans
  saut d'onglet. Une vérification encore en vol (JSON pas encore persisté)
  n'est pas interrompue : le flux et le filet restent maîtres.
- Aucun changement de fournisseur, de prix, de politique de données ni de
  stockage : ni EFVP ni README impactés.

## 2026-08-27 — Nouvelle consultation : retour systématique à « Transcription brute »

*Après une mise en forme, le panneau de dictée restait sur l'onglet
« Validation » (bascule automatique au grand écran) même quand une nouvelle
consultation était ouverte : l'espace de travail était réinitialisé mais pas
l'onglet actif.*

- **Restauration de l'onglet « Transcription brute »** à chaque remise à zéro
  de l'espace de travail (`resetWorkspace`) : bouton « Nouvelle », consultation
  supprimée, et suppression reçue en direct depuis un autre onglet. Aucun
  changement de comportement pendant une consultation en cours — seule la
  bascule automatique du grand écran à l'arrivée de l'audit est conservée.

## 2026-08-27 — Correctif : la consommation de l'audit « Validation » était perdue

*Le commit du cache de préfixe avait remplacé `owner_key` par un identifiant
inexistant (`owner`) dans la journalisation de l'audit : chaque passage en
« Validation » levait une `NameError` avalée par le filet d'exception, et la
consommation Gemini de la seconde passe (jetons + coût) n'était jamais
persistée dans `usage_events` / le panneau admin.*

- **Restauration du comportement antérieur** : `log_llm_usage` reçoit à nouveau
  `owner_key` (le même identifiant que la mise en forme et les autres appels).
  L'audit était déjà publié et persisté normalement (aucun impact sur les
  notes) — seule la statistique de consommation manquait.
- Aucun changement de fournisseur, de prix, de politique de données ni de
  stockage : ni EFVP ni README impactés.

## 2026-08-27 — L'audio n'est envoyé qu'une fois au modèle : cache de préfixe

*L'audio — la plus grosse part du prompt — était envoyé deux fois par
consultation (mise en forme, puis « Validation »). Il est désormais relu depuis
le cache de préfixe implicite de Gemini au second passage.*

- **Préfixe [consigne système + audio] partagé entre les deux passes.** La
  mise en forme place l'audio en tête du message utilisateur, et l'audit
  « Validation » tourne sous la MÊME consigne système (assemblée une fois par
  `api_generate`, injectée aux deux passes via `system_override` /
  `system_instruction`) — les consignes d'audit ouvrent alors le message
  utilisateur, juste après l'audio. Les deux requêtes partagent donc le même
  préfixe, servi depuis le cache implicite de Gemini (~4× moins cher que
  l'entrée fraîche) : l'audio n'est re-facturé que sur la fin du message. Sans
  `system_instruction` (appelant hors pipeline), l'audit retombe sur l'auditeur
  en consigne système — comportement antérieur.
- **Mesuré avant livraison** (gemini-2.5-pro, Vertex, Montréal) : le flux de
  mise en forme remplit et lit le cache ; l'audit, lancé ~20-45 s plus tard,
  en tire la quasi-totalité de son prompt — 10 017/10 382 jetons pour une
  dictée de ~4 min, 28 425/29 427 pour ~14 min. L'économie suit la durée de
  la dictée.
- **Qualité de l'audit inchangée.** Comparaison contrôlée (prompt d'audit
  courant vs déplacé dans le message, mêmes entrées, même session) sur trois
  consultations : aucune régression systématique — l'audit garde la même
  variabilité d'un appel à l'autre qu'avant.
- **La remise « cache » devient observable.** Les jetons servis depuis le
  cache sont journalisés sur la ligne de l'audit (comme ils l'étaient déjà
  sur la mise en forme), persistés dans `usage_events.cached_tokens`
  (colonne ajoutée par migration, idem `usage_daily`), agrégés dans les
  statistiques admin et appliqués au coût : le tarif `token_input_cached_1m`
  (gemini 0,125 $/1M — ~90 % de remise) réduit la facturation d'entrée quand
  texte et audio frais paient le même tarif (gemini-2.5-pro). Le coût
  journalisé reflète désormais la remise réelle, pas le pire cas.

## 2026-08-27 — v2.0.0-rc.2

*Deuxième candidate de la version 2 : inclut tout depuis la rc.1 — les
entrées du jour ci-dessous.*

- **Le panneau « Détails » ne s'ouvre plus tout seul après la mise en
  forme.** Les métadonnées (date, raison, requérant, accompagnement)
  continuent d'être renseignées depuis la dictée et affichées, mais la
  section reste repliée — le médecin l'ouvre lui-même quand il veut les
  vérifier. L'ouverture automatique n'avait plus de sens depuis que l'onglet
  « Validation » capte l'attention au même moment.

## 2026-08-27 — « Validation » en markdown, en direct, au bon moment

*Le panneau « Validation » et le déroulement de « Mettre en forme » repensés
autour de ce que le médecin voit pendant l'attente.*

- **Indicateur de « Mettre en forme » en cinq phases.** Le toast de
  progression déroule désormais « Préparation… » → « Envoi au modèle… » →
  « Traitement en cours… » → « La note se génère… », puis « Validation en
  cours… » à la fin de la génération quand la bascule « Validation » est
  active (le toast reste alors affiché jusqu'à l'arrivée de l'audit).
- **La roue de l'onglet « Validation » ne tourne plus pendant la génération.**
  Elle ne démarre qu'à la fin de la note — au moment où la rubrique
  « Corrections et éléments à valider » est extraite — qui est aussi, sur
  grand écran, celui où l'onglet bascule sur « Validation ». Sur mobile, on
  reste sur la note générée. Un « Validation » sans audio (transcription
  seule) n'arme toujours rien.
- **Panneau « Validation » en simple markdown.** La boîte jaune disparaît :
  la rubrique « Corrections et éléments à valider » s'affiche en markdown
  (intitulé compris), comme le reste de l'application. L'audit factuel suit
  sous une section titrée **« Validation - 2e passe »**, elle aussi en
  markdown.
- **Audit diffusé en direct.** Le second appel Gemini est désormais un flux
  (`verification_chunk`, JSON brut accumulé) : la section « Validation - 2e
  passe » se remplit au fil de l'eau (préfixe JSON exploitable re-rendu à
  chaque morceau), sans rafraîchissement — même mécanique de reprise que la
  génération (429, refus `thinking_config`), même parse et même garde-fou
  déterministe à l'arrivée du résultat final. `verification_json` reste
  structuré, aucune migration.
- **Correctif — 500 sur « Mettre en forme » quand un artefact audio manque.**
  La voie rapide du cache audio (`_prepare_audio_for_generation`) appelait
  `audio_cache.ensure_ready(clef, fmt)` avec les mauvais arguments (la clé
  comme identifiant d'enregistrement, `fmt` comme chemin source) : dès qu'un
  artefact du cache manquait, la génération échouait en 500 au lieu de
  retomber sur la voie historique. Appel corrigé — `ensure_ready(identifiant,
  chemin, fmt)` — et repli restauré.

## 2026-08-26 — Les hospitalisations dictées dans les antécédents y restent

*Correctif suite à l'observation sur une note réelle : une hospitalisation
antérieure dictée pendant l'énumération des antécédents (lieu, année, motif,
avec sa synthèse) était déplacée par le modèle vers l'HMA.*

- **Consigne générale (§ 1) : placement des hospitalisations.** Une
  hospitalisation ou un séjour antérieur dicté pendant l'énumération des
  antécédents figure dans la rubrique des antécédents du gabarit, contexte et
  synthèse dictés compris — jamais déplacé vers l'HMA, dont le récit ne couvre
  que le motif actuel de la consultation.
- **Gabarits de consultation (règle Antécédents / Past medical history)** :
  les trois gabarits livrés (Consultation Médicale Générale fr/en,
  Consultation - Gériatrie) nomment explicitement les hospitalisations et
  séjours antérieurs dictés parmi les antécédents. Applicable aux gabarits et
  consignes déjà en service par migration (phrase d'origine intacte exigée —
  une consigne ou copie personnalisée n'est pas écrasée).
- Le gabarit « Suivi - Gériatrie » est inchangé : les hospitalisations
  antérieures y relèvent déjà du Résumé.

## 2026-08-26 — « Corrections et éléments à valider » déplacée dans l'onglet Validation

*La rubrique finale de la note quittait le document clinique et restait dans
la note elle-même, à relire au milieu du corps. Elle vit désormais dans
l'onglet « Validation », séparée de la note à la génération.*

- **Rubrique retirée du corps de la note à la génération** (serveur). La note
  persistée, ses métadonnées et l'audit « Validation » n'incluent plus la
  rubrique « Corrections et éléments à valider » ; elle est stockée à part
  (`consultations.corrections_markdown`, migration ajoutée au démarrage) et
  renvoyée dans la réponse de `/api/generate`. Détection de l'intitulé
  (`## Corrections et éléments à valider` fr / `## Corrections and items to
  verify` en), repli sur le corps entier si absent.
- **Onglet « Validation » alimenté dès la génération.** Pendant le streaming,
  la rubrique y défile (jamais dans la fenêtre de note) ; sur grand écran le
  panneau bascule automatiquement sur l'onglet à la fin de la génération pour
  la montrer ; la roue du titre continue de tourner jusqu'à l'arrivée de
  l'audit, dont les résultats s'affichent sous la rubrique. Sur mobile, on
  reste sur la note générée (aucune bascule forcée, ni à la génération ni à
  l'arrivée de l'audit).
- **Brouillons antérieurs** : la rubrique encore présente dans leur note est
  réextraite à l'ouverture (`corrections_markdown` ou découpe locale) et
  montrée dans l'onglet sans être écrite dans le document.
- **README / ÉFVP** mis à jour (champ `corrections_markdown` ajouté aux
  renseignements de santé, même rétention que la note).

## 2026-08-26 — Le Plan conserve le « je » dicté à la première personne

*Correctif suite à l'observation sur une note réelle : un plan dicté à la
première personne (« Je renouvelle son Exelon pour un an. Je cesse le Maxeran…
je la revois dans un an ») était généré sans pronom, les actions étant réduites
à l'infinitif ou au passif (« Renouveler son Exelon pour un an », « Cesser le
Maxeran »).*

- **Consigne générale (§ 3 Impression et Plan) resserrée** : le « je » dicté
  est **toujours** conservé dans le Plan, jamais effacé, jamais réduit à
  l'infinitif, au substantif ou à la voix passive. Contre-exemples explicites
  ajoutés (« Je renouvelle son Exelon pour un an » reste tel quel — jamais
  « Renouveler son Exelon… » ; « Je cesse le Maxeran » reste tel quel — jamais
  « Cesser le Maxeran »). Une action dictée sans pronom se transcrit sans
  pronom : la personne grammaticale dictée est respectée strictement, sans
  normaliser.
- **Applicable aux consignes déjà en service** par migration (ancienne puce
  intacte exigée — une consigne personnalisée n'est pas écrasée). À valider
  par régénération d'une note dictée à la première personne.

## 2026-08-26 — Raisonnement (thinking) de retour dans la note, sans réapparition

*Le défilement du raisonnement du modèle dans la fenêtre de note avait été
retiré (beta.84) parce que, une fois la pensée terminée, elle « flashait » :
elle réapparaissait pendant le streaming de la note. Le défilement est rétabli,
et la réapparition supprimée à sa source.*

- **Défilement du raisonnement rétabli** dans la fenêtre de note pendant la
  génération (toujours régi par `show_thinking_admin` / `show_thinking_users`,
  désactivés par défaut), puis effacé dès le premier morceau de texte de la
  note — jamais persisté.
- **Cause de la réapparition supprimée.** Le serveur re-publiait un snapshot
  de la pensée toutes les secondes pendant TOUTE la génération, même une fois la
  note commencée ; le client le rejouait en remplaçant le texte de la note. Le
  serveur n'émet plus de snapshot de raisonnement dès qu'un premier jeton de
  texte est sorti (`generation_thought` ne circule plus que pendant la phase de
  pensée), et le client verrouille la bascule : toute pensée en retard après le
  début du texte est ignorée. La pensée défile donc une fois, puis la note
  prend le relais sans retour.

## 2026-08-26 — Validation fiabilisée + fidélité des visites antérieures

*Correctifs suite à l'observation sur une note réelle : des médicaments
réellement dictés étaient déclarés « inventions », des phrases déjà présentes
dans la note « omissions », et une modification de traitement faite à une
visite antérieure était passée sous silence.*

- **Le budget de raisonnement du « Validation » suit désormais le panneau**
  (`gemini_thinking_budget`, ex. 2048) au lieu du plancher 128. Avec 128 le
  modèle hallucine des écarts (il ne croise pas réellement l'audio et la
  note) ; avec le budget du panneau les faux positifs disparaissent et les
  vraies omissions (faits dictés absents de la note) sont retrouvées.
- **Garde-fou déterministe anti-faux-positifs.** Après l'appel, chaque
  élément signalé est recoupé : une « omission » dont tous les mots
  distinctifs figurent déjà dans la note est écartée (l'info y est), une
  « invention » dont la moitié des mots distinctifs figure dans la
  transcription est écartée (le fait a été dicté). Le seuil est conservateur
  par construction : un vrai écart a toujours un terme absent, donc rien de
  réel n'est perdu.
- **Consignes de l'auditeur resserrées** : méthode obligatoire (écouter
  l'audio en entier, relire la note en entier, puis comparer), rappel que la
  liste de médicaments est énoncée dans l'audio (les écarts de prononciation
  « Lipitar ≈ Lipitor » ne sont pas des inventions), tolérance explicite aux
  reformulations. Vérifié par re-validation de la note concernée : plus
  aucun faux positif, la vraie omission (renouvellement Exelon + diminution
  métoclopramide à la visite précédente) est signalée.
- **Fidélité des modifications de traitement des visites antérieures.** Une
  dictée « lors de la visite précédente, j'avais renouvelé l'Exelon et
  diminué le métoclopramide » était passée sous silence par la génération.
  La règle du **Résumé** des gabarits « Suivi » exige désormais explicitement
  toute modification du plan de traitement d'une visite antérieure
  (médicament débuté, cessé, renouvelé, dose modifiée), distincte du plan
  actuel ; la consigne générale (§ 1) porte la même règle. Applicable aux
  gabarits existants par migration (fragment intact exigé). Vérifié : la
  régénération de la note concernée contient bien « le traitement par Exelon
  avait été renouvelé et la dose de métoclopramide avait été diminuée ».
- **« Validation » demandée sans audio (transcription seule) : arrêt immédiat
  de la roue.** La bascule active mais aucun enregistrement joint à la
  consultation (note produite à partir de la seule transcription) publie
  aussitôt un évènement SSE « skipped » : l'onglet affiche « rien à
  signaler » sans attendre le filet de 180 s. L'audit étant audio↔note, il
  ne peut de toute façon pas partir sans audio.

## 2026-08-26 — v2.0.0-rc.1

*Première candidate de la version 2 : inclut tout depuis la bêta.84 — les
entrées du jour et du 25 août ci-dessous.*

- **« Validation » : audit factuel de chaque note contre l'audio.** Nouvelle
  bascule à côté de « Mettre en forme » (préférence par usager, désactivée
  par défaut) : après la note, un second appel compare celle-ci à l'AUDIO de
  la dictée — jamais à la transcription Parakeet, trop imprécise pour servir
  de référence — et signale en deux listes plates ce qui fut dicté mais
  manque à la note, et ce que la note affirme sans avoir été dicté.
  Volontairement permissif (seuls les écarts certains, reformulations
  acceptées). Résultat diffusé en direct dans un second onglet du panneau de
  transcription (roue sur le titre pendant la vérification, bascule
  automatique à l'arrivée), conservé avec le brouillon et réaffiché au
  chargement. Coût ~60 % d'une génération en plus ; jamais bloquant.
- **Requêtes ordonnées pour le cache de préfixe de Gemini.** Le message
  utilisateur place désormais la mise en page (stable par gabarit) avant le
  contexte variable : le préfixe partagé entre consultations couvre consigne
  système + consignes du gabarit + structure exigée, au-delà de la seule
  consigne système — le cache implicite (défaut Vertex, ~90 % de remise sur
  les jetons servis) a ainsi plus de matière à réutiliser, sans rien changer
  à la priorité des consignes (qui reste tranchée côté système).

- **La note démarre plus tôt : l'audio du modèle se prépare pendant la
  dictée.** Le plafonnement des silences + l'encodage de l'audio joint à la
  mise en forme (~0,9× le temps réel : 4 s pour 5 min, 32 s pour 35 min)
  étaient payés au clic « Mettre en forme ». Désormais, pendant la dictée, un
  point de contrôle de l'audio préparé est construit régulièrement en tâche
  de fond ; « Terminer » n'a plus qu'à préparer la queue et concaténer sans
  réencoder (~1 s), et le résultat rejoint un cache par enregistrement. Au
  clic « Mettre en forme », l'artefact prêt est envoyé immédiatement — les
  replis (préparation complète en fond dès la conclusion, puis voie
  historique au clic) garantissent le même audio qu'avant, jamais moins.
  Cache dérivé régénérable (`/data/audio-cache`, `AUDIO_CACHE_DIR`) : hors
  sauvegarde, purgé avec l'enregistrement, ignoré si les réglages du
  plafonnement changent. Les jetons servis par le cache de préfixe implicite
  de Gemini sont désormais journalisés (observation des économies).

## 2026-08-25

- **Gabarits gériatriques verrouillés : les versions « FD Markdown » deviennent
  les défauts livrés.** « Consultation - Gériatrie » et « Suivi - Gériatrie »
  sont remplacés par le contenu des gabarits affinés à l'usage — mêmes règles
  du Résumé enrichies le jour même (hospitalisations antérieures). Côté
  consultation : règle de classement de l'autonomie fonctionnelle (test
  AVQ / AVD / Mobilité avec exemples tranchés), ordre des médicaments étendu
  (cognition, impact SNC, diabète, cardiovasculaire, autres, puis laxatifs,
  vitamines, pompes et gouttes), ordre de l'examen, investigations en deux
  listes (Laboratoires / Imagerie), phrase-résumé d'Impression uniquement si
  dictée ; la mise en page place « Autonomie fonctionnelle » sous l'histoire
  sociale. Côté suivi : mise en page resserrée avec lignes d'en-tête (lieu,
  date, médecin de famille) et rubrique Médicaments, titres en casse de phrase
  au lieu des MAJUSCULES. Les copies modifiables « (FD Markdown) », désormais
  redondantes, sont retirées — les gabarits verrouillés restent rafraîchis à
  chaque démarrage depuis `app/default_templates.py`.

- **Aucune omission dans les notes.** La consigne générale interdisait
  l'invention sans interdire symétriquement l'omission : un fait dicté pouvait
  être condensé jusqu'à disparaître — constaté sur une note de suivi où
  l'hospitalisation de l'an passé (lieu, année, motif), pourtant dictée, ne
  figurait nulle part. § 1 porte désormais la règle miroir (condenser
  raccourcit la formulation d'un fait, jamais sa suppression ni sa fusion avec
  un autre) et une règle dédiée aux hospitalisations et séjours : chaque
  hospitalisation, visite ou séjour institutionnel mentionné (lieu, année,
  motif) figure dans la note, les séjours antérieurs n'étant jamais fusionnés
  avec le séjour ou la visite actuelle. La vérification finale (§ 6) gagne la
  couverture inverse : chaque fait clinique dicté est recherché dans le rapport,
  un fait manquant est rétabli dans sa rubrique. Le gabarit « Suivi -
  Gériatrie » nomme explicitement les hospitalisations antérieures dans sa
  règle du Résumé. Migrations livrées pour porter les deux changements en base
  (consigne générale et copies modifiables du gabarit) sans toucher aux textes
  déjà personnalisés ; version anglaise alignée.

- **Page de connexion pilotable au clavier.** Sur l'écran de choix de la durée
  de session, les flèches **←/→** basculent entre « usage ponctuel » et
  « rester connecté », et **Entrée** valide l'option sélectionnée puis
  enchaîne vers le fournisseur d'identité — sans toucher à la souris. Les
  comportements existants sont inchangés (clic sur les cartes, bouton
  Continuer, enchaînement automatique PWA).

## 2026-08-24

- **Panneau d'administration réorganisé par flux de travail ; recherche de
  réglage.** Cinq onglets : **Dictée** (service de transcription sous son
  sous-onglet, puis retrait des pauses et temps réel), **Note** (modèle de
  langage sous son sous-onglet, consigne générale, affichage du raisonnement),
  **Comptes et accès** (inscription automatique, attributs du nom et de
  l'avatar, comptes), **Données et sauvegarde** (purge des dossiers, rotation
  des sauvegardes, restauration) et **Statistiques**. Une **recherche** au-dessus
  des onglets filtre tous les réglages et mène directement au champ (onglet,
  sous-onglet, défilement). Les champs sans objet courant sont masqués au lieu
  d'attendre une lecture attentive : le VAD ne se règle qu'en mode « énoncé »,
  le streaming Mistral qu'en mode « streaming », la durée maximale d'audio
  joint seulement si l'audio est joint, la transcription conservée seulement si
  l'on ignore le service vocal, le budget de raisonnement seulement si le
  raisonnement est activé — les réglages fins (seuils VAD en ms, repli et
  découpage du point de terminaison personnalisé) se replient sous « Avancé ».
  Les clés partagées entre deux services (Cohere, Mistral, OpenAI) restent un
  seul réglage, répété sous les deux onglets concernés. Libellés clarifiés :
  « attribut » plutôt que « revendication », seuils de repli et tranches
  d'audio nommés pour ce qu'ils font. Aucune valeur en base n'est touchée par
  la migration : mêmes clés, mêmes défauts.
- **Consigne générale réécrite ; traduction anglaise alignée.** La consigne
  générale française est restructurée en sept sections (0 proportionnalité,
  1 aucune invention, 2 correction de la transcription — tableau
  d'homophonies québécois étoffé —, 3 style de rédaction : style déclaratif,
  ellipse du sujet, listes d'examen sans libellé interne, 4 médicaments avec
  regroupement par indication partagée, 5 format de sortie, 6 vérification
  finale et « Corrections et éléments à valider » : deux mentions seulement,
  « correction apportée » ou « à confirmer », statut unique « Aucun élément à
  signaler. », intitulé exact du gabarit). La version anglaise en est la
  traduction section par section, adaptée là où une traduction littérale ne
  veut rien dire : homophonies propres à la reconnaissance anglaise, décimales
  à point, PO/daily/BID/TID/QID, équivalents des acronymes cliniques
  (ADL, IADL, COPD, HTN, CBC, PET, MRI, DTRs) — les acronymes du réseau
  québécois restés tels quels. Les défauts livrés (`app/default_prompts.py`)
  suivent ; une consigne personnalisée en base n'est pas touchée par le
  module.
- **Ordre des gabarits verrouillés : 101 à 104.** Consultation Médicale
  Générale (fr) = 101, General Medical Consultation (en) = 102,
  Consultation - Gériatrie = 103, Suivi - Gériatrie = 104 : les deux
  consultations générales s'affichent avant la gériatrie. Rafraîchi au
  démarrage comme le reste du gabarit verrouillé.

- **Gabarits généraux réécrits sur le modèle « FD ».** Les gabarits verrouillés
  « Consultation Médicale Générale » (fr) et « General Medical Consultation »
  (en) adoptent la mise en forme des gabarits personnels : capitales de phrase
  (« ## Raison de consultation », « ## Antécédents », « ## Médicaments »,
  « ## Examen », « ## Investigation »…), rubriques courtes, champs de repère
  `{{...}}` (paragraphes, items de liste, problèmes et plans numérotés), et la
  rubrique finale sous l'intitulé du gabarit (« Corrections et éléments à
  valider » / « Corrections and items to verify »). Instructions réduites à une
  règle par rubrique — les règles transversales reviennent à la consigne
  générale réécrite. Rafraîchis au démarrage comme tout gabarit verrouillé ;
  les copies personnelles ne sont pas touchées.

## 2026-08-21

- **Pages « test » réservées : index + dictées, sous OIDC.** `/test` est un
  index des dictées (hyperliens) et `/test/{id}` affiche la note **Gemini et
  la note Qwen Omni côte à côte** (rendues via marked + DOMPurify, assainies
  côté client), avec les **stats de génération en haut** (durée, tokens de
  sortie, tokens audio pour chaque modèle) et un bouton **← Index**. Les deux
  colonnes restent visibles en **orientation paysage** (mobile landscape).
  **Analyse des différences** en haut de chaque page : métriques (caractères,
  mots, coût estimé), rubriques présentes dans une seule note, contenu
  différent (lignes) et un **résumé comparatif généré par un modèle tiers**
  (DeepSeek) avec verdict sur la viabilité de Qwen Omni vs Gemini 2.5 Pro.
  Données lues depuis `/data/test_index.json`. Sans session, redirection vers
  `/auth/login?next=…`.

## 2026-08-21

- **Retrait des silences : bascule globale pour toutes les pipelines.**
  Le plafonnement des pauses (`stt_trim_silence`, « Retirer les longues
  pauses ») s'applique désormais à **toutes** les pistes et **tous** les
  fournisseurs — y compris l'endpoint personnalisé (Parakeet) et l'audio
  joint au modèle de langage (Gemini/Qwen), quel que soit le format d'envoi
  (ogg/mp3/wav). L'exception « provider custom » est supprimée. Éteindre la
  bascule restaure l'envoi de l'audio tel quel partout. Attention : le
  plafonnement avait été suspendu pour Parakeet/ONNX en beta.45 (attaques de
  mots coupées, mélange des langues) — à surveiller sur le transcript si la
  bascule est active.

## 2026-08-21

- **Affichage du raisonnement du modèle (thinking) pendant la génération.**
  Pendant la mise en forme, le raisonnement du modèle défile désormais dans la
  fenêtre de note — même dévoilement progressif que le texte, avec un badge
  « Raisonnement du modèle… » — puis est **effacé de l'écran** dès que le texte
  de la note commence. Il n'est **jamais persisté** (rien du flux en cours
  n'est auto-sauvegardé pendant une génération). Deux réglages du panneau
  (Modèle de langage), **désactivés par défaut** : `show_thinking_admin`
  (administrateurs) et `show_thinking_users` (autres utilisateurs). Supporté
  pour Gemini (parties `thought`), les endpoints OpenAI-compatibles à
  raisonnement (`reasoning_content`/`reasoning` — DeepSeek, Qwen…) et
  Anthropic (blocs `thinking`). Sans effet si le modèle ne produit pas de
  raisonnement.

## 2026-08-21

- **Règle de regroupement des médicaments renforcée dans les gabarits.**
  Le modèle ajoutait bien l'indication entre parenthèses mais laissait chaque
  médicament sur sa propre ligne. La consigne « Médication actuelle » des
  gabarits « Consultation - Gériatrie » et « Consultation Médicale Générale »
  (FR, verrouillés) ainsi que des copies (FD) précise désormais que
  « écrire l'indication sur une ligne ne dispense pas du regroupement » : dès
  que deux médicaments partagent la même indication (dictée ou cliniquement
  évidente), ils doivent figurer sur la **même ligne**. Exemples étendus :
  deux ou trois antalgiques, deux laxatifs, deux hypoglycémiants, couple
  calcium + vitamine D. La consigne générale n'est pas modifiée.

## 2026-08-21

- **Règle de regroupement des médicaments rendue actionnable dans les
  gabarits.** La consigne « Regroupe un même traitement en une ligne lorsqu'il
  sert la même indication » n'était jamais exécutée : le modèle refusait
  d'inférer une indication commune, la consigne générale prioritaire
  interdisant toute invention. Reformulation dans les gabarits verrouillés
  (Consultation Médicale Générale FR/EN, Consultation - Gériatrie) et leurs
  copies existantes (migration au démarrage) : regroupement sur une même ligne
  lorsque l'indication est **dictée ou cliniquement évidente** (deux laxatifs,
  deux opioïdes, deux hypoglycémiants…), indication entre parenthèses en fin de
  ligne, et une ligne par médicament en cas de doute. La consigne générale
  n'est pas modifiée.

## 2026-08-21

- **Correctif — le panneau « Nouveautés » de la connexion rend le Markdown du
  changelog.** Le gras (`**…**`) et le code inline (`` `…` ``) s'affichaient en
  texte brut. Nouveau filtre Jinja `md_inline` (échappement puis conversion
  `**`/backticks, aucun contenu injectable). Le parseur perdait aussi des
  entrées : un en-tête daté sans intitulé de version (`## AAAA-MM-JJ` seul,
  par ex. l'entrée Modulate) n'était pas reconnu, et les lignes de continuation
  des puces étaient jetées — items tronqués en plein milieu. Les en-têtes sans
  titre sont désormais lus et les items joints sur toutes leurs lignes. La page
  de connexion est la seule surface publique — le rendu y respecte l'apparence
  des autres documents.

## 2026-08-21

- **Nouveau service vocal : Modulate (Velma STT)**. Neuvième fournisseur de
  reconnaissance vocale, multilingue avec détection de langue par énoncé et
  vocabulaire personnalisé (`custom_terms`, jusqu'à 100 termes envoyés depuis
  le lexique intégré et le gabarit). Modèle par défaut `velma-2-stt-batch` ;
  sélectionnable dans le panneau (y compris `…-multilingual-vfast`, plus
  rapide mais sans vocabulaire, et `…-english-vfast`, anglais seul). Clé dans
  le panneau d'administration (ou `MODULATE_API_KEY`). Diarisation désactivée
  (dictée mono-locuteur, moins de latence).

## 2026-08-20

- **Temps réel de la dictée** (mode « énoncé » et « streaming »). Par-dessus
  le batch fiable inchangé (audio téléversé par fragments, copie locale,
  fichier brut complet), un détecteur de parole (VAD) tournant dans le
  navigateur signale la fin de chaque énoncé : le serveur transcrit
  immédiatement, en coupant au silence (ffmpeg fait toujours autorité sur la
  frontière). Le texte apparaît quelques secondes après chaque pause au lieu
  de ~10-15 s. Deux modes réglables (`STT_REALTIME_MODE`) : **« vad »**,
  compatible avec tous les fournisseurs y compris le Parakeet local (l'audio
  ne quitte pas la machine) ; **« sse »**, qui ajoute un affichage provisoire
  en deltas pendant la parole via Mistral Voxtral realtime (l'audio part
  alors chez Mistral — désactivé par défaut, décision de conformité EFVP).
- **Filet de fin : re-transcription des trous** (`STT_VAD_FINISH_SWEEP`). Au
  « Terminer », le serveur re-parcourt l'audio brut (détection de parole par
  ffmpeg) et re-transcrit tout passage manqué — un énoncé que le VAD n'avait
  pas vu, une tranche qui avait échoué. Rien n'est perdu ; le bénéfice profite
  aussi au batch classique.
- **Réglages du détecteur** : sensibilité (`STT_VAD_SENSITIVITY`), délais
  d'entrée/sortie de parole (`STT_VAD_SPEECH_MS`, `STT_VAD_SILENCE_MS`) et
  modèle Mistral du mode streaming (`MISTRAL_REALTIME_MODEL`).
- **Correctif** : l'identifiant du modèle Mistral realtime par défaut était
  invalide — Mistral n'accepte que le daté `voxtral-mini-transcribe-realtime-2602`
  (`-latest` n'existe pas pour ce modèle, erreur 400 `invalid_model`). Défaut
  corrigé (code + `.env.example`) et valeur effective rafraîchie.
- **Correctif** : la route `POST /api/dictation/{id}/utterance_ended` était
  synchrone — exécutée par FastAPI dans le threadpool, la planification de la
  transcription y échouait en « no running event loop » (500), le flush n'était
  jamais consommé et la dictée ne transcrivait qu'au « Terminer ». Passée en
  `async def`, la fin d'énoncé relance la transcription immédiatement.
- **Texte progressif dans le panneau STT**. La transcription apparaît en
  continu, « token par token », avec la même mécanique que la note structurée
  (rattrapage proportionnel, finition lissée) : segments committés et ligne
  provisoire du mode streaming se dévoilent au fil de l'eau.
- **Contexte conservé en mode « sse »**. Le mode streaming garde une seule
  session WebSocket Mistral par dictée : chaque énoncé y est ajouté et le
  modèle conserve le contexte des énoncés précédents (noms, posologies,
  cohérence de l'anamnèse). Nouveau réglage `MISTRAL_REALTIME_DELAY_MS`
  (attente de contexte avant transcription, 1000 ms par défaut). Repli batch
  conservé si la session meurt.

## 2026-08-20 — v2.0.0-beta.83

- **Hauteur des toasts unifiée sur mobile** (beta.83). Les toasts « live »
  (noirs) faisaient 4 px de moins que les toasts de couleur d'accent (le
  bouton ✕ force une hauteur de 2,5 rem) : une hauteur minimale commune
  (`min-height: 2.5rem`) sur tous les toasts supprime l'écart. Les messages
  sur plusieurs lignes (bureau) grandissent comme avant.

## 2026-08-20 — v2.0.0-beta.82

- **Toasts harmonisés** (beta.82). Tous les toasts non « live » (information,
  succès, avertissement, brouillon abandonné, invitations) prennent la
  couleur d'accent du thème — seuls les erreurs restent rouges. Les toasts de
  génération et de transcription restent noirs (en direct). Tous les toasts
  se ferment à la croix **ou au glissé latéral**, sur mobile comme au
  bureau (le toast à bouton d'action y a droit aussi).

## 2026-08-20 — v2.0.0-beta.81

- **Correctif — le toast « brouillon abandonné » ne s'affichait pas** (beta.81).
  L'appel au chargement avait été inséré dans le gestionnaire
  `visibilitychange` au lieu de `init()` (la beta.80) : le toast ne se
  déclenchait jamais au chargement. L'appel est déplacé dans `init()` et le
  gestionnaire `visibilitychange` est rétabli dans sa forme d'origine.

## 2026-08-20 — v2.0.0-beta.80

- **Brouillon abandonné : un toast vous y renvoie** (beta.80). À chaque
  chargement, tant qu'un brouillon marqué « Abandonnée » existe, un toast
  dismissable (« Une dictée abandonnée a laissé un brouillon — voyez la liste
  des brouillons », bouton **Voir**) s'affiche et ouvre la liste. Les autres
  appareils ouverts sont prévenus en direct dès qu'une dictée interrompue est
  détectée (SSE). La détection se fait désormais aussi au chargement de la
  page, pas seulement à l'ouverture de la liste des brouillons : l'audio est
  archivé et le brouillon marqué même si on ne l'ouvre jamais.

## 2026-08-20 — v2.0.0-beta.79

- **Dictées abandonnées : l'audio est conservé** (beta.79). Une dictée
  interrompue par un onglet fermé (navigateur quitté, application tuée) n'est
  plus perdue : à l'ouverture de la liste des brouillons, le serveur rattache
  l'audio au brouillon comme un enregistrement — c'est lui qui sert à générer
  la note, notamment avec un fournisseur en **audio direct** — et marque le
  brouillon **« Abandonnée »** en rouge pâle. Le brouillon suit ensuite la
  rétention globale (`consultation_retention_hours`, 12 h par défaut), comme
  n'importe quel autre contenu. Les dictées quasi vides (moins de 10 s d'audio)
  sont supprimées, session et brouillon vide compris. La détection repose sur
  la **scrutation du navigateur** (une dictée en pause n'est jamais marquée) et
  se fait à l'accès à la liste — aucune boucle de fond côté serveur. Aucune
  transcription supplémentaire n'est déclenchée.

## 2026-08-20 — v2.0.0-beta.78

- **Le bandeau de récupération disparaît** (beta.78). Les dictées interrompues
  par un onglet fermé n'affichent plus le bandeau et ses boutons « Reprendre /
  Terminer et transcrire / Télécharger / Supprimer », source de confusion et de
  répétition quand plusieurs appareils étaient connectés. Une dictée abandonnée
  n'est plus traitée à part : sa session est simplement purgée à la rétention
  globale (`consultation_retention_hours`, défaut 12 h) et le brouillon garde
  les tranches déjà transcrites.

## 2026-08-19 — v2.0.0-beta.77

- **Consolidation des consignes retirée, définitivement** (beta.77) — la
  régression de style à la génération est **confirmée** : la consolidation
  (beta.74, réappliquée en beta.76) est retirée du code, et l'installation
  revient à l'état antérieur. Consigne générale (français/anglais) et les
  quatre gabarits livrés retrouvent leur texte d'origine, la section
  « Éléments à valider » du message utilisateur est rétablie. La valeur en
  base est remise sur l'ancien défaut livré par la migration d'annulation
  (une consigne personnalisée par le médecin n'est pas touchée).

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