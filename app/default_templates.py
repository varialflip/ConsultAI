"""
default_templates.py — Les quatre gabarits livrés.
===========================================================================

TOUS VERROUILLÉS
-----------------
Les quatre gabarits livrés (« Consultation Médicale Générale », « General
Medical Consultation », « Consultation - Gériatrie » et « Suivi - Gériatrie »)
sont verrouillés (``is_locked``) : ni modifiables ni supprimables depuis
l'interface. Ce sont les points de départ : on les DUPLIQUE pour obtenir une
copie indépendante et modifiable. Le refus est appliqué côté serveur, pas
seulement masqué dans l'écran.

Étant intouchables, ils sont RAFRAÎCHIS à chaque démarrage : une amélioration
apportée ici profite aux installations existantes, sans risque d'écraser le
travail de quiconque — personne ne peut les avoir modifiés. Une copie créée
par le médecin (via « Dupliquer ») est une ligne neuve et indépendante,
jamais réécrite.

LA LANGUE DU GABARIT PILOTE TOUTE LA CHAÎNE
-------------------------------------------
``language`` n'est pas une étiquette d'affichage : c'est elle qui décide de la
langue des consignes de base, de la consigne générale employée, du code envoyé
au service vocal et de la langue de rédaction de la note. Voir
``llm.build_system_prompt`` et ``runtime_config.stt_language``.
"""

#: Gabarits verrouillés : rafraîchis à chaque démarrage.
LOCKED_TEMPLATES = (
    {
        "name": "Consultation Médicale Générale",
        "description": "Consultation générale : anamnèse, examen, impression et plan.",
        "language": "fr",
        "system_instructions": "Ces consignes précisent le contenu attendu dans chaque rubrique du gabarit pour une consultation médicale générale. Les règles transversales — aucune invention, style déclaratif, ellipse du sujet, regroupement des médicaments, corrections et éléments à valider — viennent de la consigne générale et ne sont pas répétées ici.\n\n## RÈGLES PAR RUBRIQUE\n\n**En-tête.** Ne renseigne que les valeurs dictées ; toute ligne d'en-tête sans valeur dictée est supprimée, y compris la date. Si le patient a été référé par son médecin de famille, transforme la ligne « Médecin référent : » en « Médecin référent / Médecin de famille : ».\n\n**Raison de consultation.** Une phrase courte : la raison principale telle que dictée.\n\n**Antécédents.** Liste pointée, une ligne par antécédent dicté (médical, chirurgical, familial) — uniquement ce qui est dicté.\n\n**Médicaments.** Liste pointée selon les règles générales des médicaments (nom + dose, regroupement par indication partagée, indication entre parenthèses en fin de ligne). Ordre propre à ce gabarit — simple ordre, aucun titre de catégorie ne s'écrit : 1) cognition, 2) impact SNC, 3) autres ; laxatifs, vitamines, pompes et gouttes en dernier.\n\n**Allergies.** Allergies dictées uniquement.\n\n**Habitudes de vie.** Tabac, alcool, drogues — uniquement si dictés.\n\n**Histoire sociale.** Paragraphes suivis : milieu de vie et réseau de soutien (domicile, RPA, RI, CHSLD ; conjoint, proches ; services en place), scolarité et emploi. Termine la rubrique par les aspects médicolégaux dictés en liste pointée, sans libellé (niveau de soins, directives anticipées, aptitude, mandat, procuration). Aucun aspect médicolégal dicté → pas de liste.\n\n**HMA.** Mentionne si le patient a été rencontré seul ou accompagné. Récit chronologique concis : début, évolution, facteurs déclenchants, traitements essayés. Paragraphes suivis, selon le style général.\n\n**Examen.** Liste pointée sans libellé interne (« Calme, collabore et orientée », jamais « État général : … »), dans cet ordre : apparence générale ; signes vitaux et poids ; cognition et langage (si décrits) ; état mental (si décrit) ; le reste ensuite.\n\n**Investigation.** Deux listes pointées, une par sous-section :\n- **Laboratoires** — une ligne par résultat ou regroupement dicté ;\n- **Imagerie** — une ligne par examen dicté (type, site et date si dictés, résultat).\n\n**Impression et Plan.** Listes numérotées selon le style général — problèmes du plus actif au moins actif, conduites concrètes pour le Plan ; le Plan reprend la numérotation de l'Impression lorsque c'est possible. Conserve intégralement le raisonnement clinique dicté.",
        "layout_format": "# Consultation médicale\n**Lieu de la consultation :**\n**Date de l'évaluation :**\n**Médecin référent :**\n**Médecin de famille :**\n\n## Raison de consultation\n{{Raison de consultation}}\n\n## Antécédents\n- {{Antécédent 1}}\n- {{Antécédent 2}}...\n\n## Médicaments\n- {{Médicament ou groupe de médicaments 1}}\n- {{Médicament ou groupe de médicaments 2}}...\n\n### Allergies\n\n## Habitudes de vie\n\n## Histoire sociale\n\n## HMA\n{{Paragraphe 1}}\n\n{{Paragraphe 2}}...\n\n## Examen\n\n## Investigation\n### Laboratoires\n### Imagerie\n\n## Impression\n1. {{Problème 1}}\n2. {{Problème 2}}...\n\n## Plan\n1. {{Plan 1}}\n2. {{Plan 2}}...\n\nRédigé à l'aide de la reconnaissance vocale.\n\n## Corrections et éléments à valider",
        "phrase_hints": "",
        "sort_order": 101,
        "is_default": True,
        "is_locked": True,
    },
    {
        "name": "General Medical Consultation",
        "description": "General consultation: history, examination, impression and plan.",
        "language": "en",
        "system_instructions": 'These instructions specify the expected content of each template section for a general medical consultation. The cross-cutting rules — never invent, declarative style, subject ellipsis, medication grouping, corrections and items to verify — come from the general instruction and are not repeated here.\n\n## RULES BY SECTION\n\n**Header.** Fill in only the values that were dictated; any header line whose value was not dictated is removed, including the date. If the patient was referred by their family physician, turn the "Referring physician:" line into "Referring physician / Family physician:".\n\n**Reason for consultation.** One short sentence: the main reason as dictated.\n\n**Past medical history.** Bulleted list, one line per dictated item (medical, surgical, family) — only what was dictated.\n\n**Medications.** Bulleted list following the general medication rules (name + dose, grouping by shared indication, indication in parentheses at the end of the line). Order specific to this template — ordering only, no category heading is ever written: 1) cognition, 2) CNS-acting, 3) others; laxatives, vitamins, pumps and eye drops last.\n\n**Allergies.** Dictated allergies only.\n\n**Lifestyle habits.** Tobacco, alcohol, drugs — only if dictated.\n\n**Social history.** Flowing paragraphs: living situation and support network (home, assisted-living residence, intermediate resource, long-term care; spouse, relatives; services in place), education and employment. Close the section with the dictated medicolegal aspects as an unlabelled bulleted list (level of care, advance directives, capacity, mandate, power of attorney). None dictated → no list.\n\n**HPI.** State whether the patient was seen alone or accompanied. Concise chronological account: onset, evolution, triggers, treatments tried. Flowing paragraphs, per the general style.\n\n**Physical examination.** Bulleted list with no internal labels ("Calm, cooperative and oriented", never "General appearance: ..."), in this order: general appearance; vital signs and weight; cognition and speech (if described); mental status (if described); everything else afterwards.\n\n**Investigations.** Two bulleted lists, one per subsection:\n- **Laboratory** — one line per dictated result or grouped result;\n- **Imaging** — one line per dictated study (type, site and date if dictated, result).\n\n**Impression and Plan.** Numbered lists per the general style — problems from most to least active, concrete actions for the Plan; the Plan reuses the Impression numbering whenever possible. Preserve in full the dictated clinical reasoning.',
        "layout_format": '# Medical consultation\n**Location of the consultation:**\n**Date of the assessment:**\n**Referring physician:**\n**Family physician:**\n\n## Reason for consultation\n{{Reason for consultation}}\n\n## Past medical history\n- {{History item 1}}\n- {{History item 2}}...\n\n## Medications\n- {{Medication or group of medications 1}}\n- {{Medication or group of medications 2}}...\n\n### Allergies\n\n## Lifestyle habits\n\n## Social history\n\n## HPI\n{{Paragraph 1}}\n\n{{Paragraph 2}}...\n\n## Physical examination\n\n## Investigations\n### Laboratory\n### Imaging\n\n## Impression\n1. {{Problem 1}}\n2. {{Problem 2}}...\n\n## Plan\n1. {{Plan item 1}}\n2. {{Plan item 2}}...\n\nWritten using speech recognition.\n\n## Corrections and items to verify',
        "phrase_hints": "",
        "sort_order": 102,
        "is_default": True,
        "is_locked": True,
    },
    {
        "name": 'Consultation - Gériatrie',
        "description": 'Consultation médicale gériatrique standardisée',
        "language": 'fr',
        "system_instructions": '# STRUCTURATION CLINIQUE\n\nTu structures une CONSULTATION MÉDICALE GÉRIATRIQUE à partir de la transcription dictée.\n\nRegroupe l\'information sous la bonne rubrique, même si le médecin l\'a dictée dans le désordre ou y est revenu plus tard.\n\nEn-tête — Lieu, Date, Médecin référent, Médecin de famille : ne renseigne que les valeurs dictées. Toute ligne d\'en-tête dont la valeur n\'a pas été dictée est retirée. N\'invente jamais un nom, une date ou un lieu, et n\'utilise jamais un texte de remplissage (« Non servi », « Non abordé », « — »).\n\nRaison de consultation : dégage la raison principale, clairement et brièvement.\n\nAntécédents médicaux et chirurgicaux : n\'inscris que les antécédents dictés.\n\nMédication actuelle : liste pointée, nom + dose. Regroupe sur une même ligne les médicaments qui servent la même indication lorsque celle-ci est dictée ou cliniquement évidente — deux ou trois antalgiques, deux laxatifs, deux hypoglycémiants, le couple calcium + vitamine D par exemple — en plaçant l\'indication entre parenthèses en fin de ligne (« Senokot 1 comprimé PO HS, Lax-A-Day 17 g PO die (constipation) »). Écrire l\'indication sur une ligne ne dispense pas du regroupement : dès que deux médicaments partagent la même indication, ils DOIVENT figurer sur la même ligne. En cas de doute sur une indication commune, conserve une ligne par médicament. Sans titres ni colonnes. Ordonne : 1) médicaments de la cognition, 2) médicaments à impact SNC, 3) autres. Vérifie que les médicaments et les doses sont plausibles.\n\nAllergies : n\'inscris que les allergies dictées.\n\nHabitudes de vie : tabac, alcool, drogues — uniquement si dictés.\n\nHistoire sociale et milieu de vie : rapporte de façon factuelle — milieu de vie et réseau de soutien (domicile, RPA, RI, CHSLD; conjoint, proches; services en place), scolarité et emploi, aspects médicolégaux (niveau de soins, directives anticipées, aptitude, mandat, procuration).\n\nAutonomie fonctionnelle : décris les AVQ, AVD et la mobilité uniquement lorsqu\'ils sont dictés.\n\nHMA : mentionne si le patient a été rencontré seul ou accompagné. Récit chronologique concis : début, évolution, facteurs déclenchants, traitements essayés. Style déclaratif : n\'ouvre pas chaque phrase par « il/elle » ; énonce le sujet une fois, puis poursuis sans pronom.\n\nExamen objectif : liste pointée sans titres — apparence générale, signes vitaux et poids, cognition et langage (si décrits), état mental (si décrit), et le reste.\n\nInvestigations : laboratoires et imagerie — résultats dictés uniquement.\n\nImpression : l\'impression diagnostique telle que dictée, en liste numérotée. Si elle est dictée à la première personne, transcris-la telle quelle (ex. « Je crois qu\'il s\'agit d\'une maladie d\'Alzheimer »), jamais « Maladie d\'Alzheimer » ni « Le médecin croit… ». Conserve aussi intégralement le raisonnement clinique dicté — revue des effets secondaires d\'un traitement, écart ou rétention d\'une cause — sans le résumer ni le supprimer.\n\nPlan : la conduite à tenir telle que dictée, en liste numérotée. Si elle est dictée à la première personne, transcris-la telle quelle (ex. « Je lui donne congé de la clinique »), jamais « Congé de la clinique » ni « Il lui donne congé ».',
        "layout_format": "# CONSULTATION EN GÉRIATRIE\n**Lieu de la consultation :**\n**Date de l'évaluation :**\n**Médecin référent :**\n**Médecin de famille :**\n\n## RAISON DE CONSULTATION\n\n## ANTÉCÉDENTS MÉDICAUX ET CHIRURGICAUX\n\n## MÉDICATION ACTUELLE\n\n### ALLERGIES\n\n## HABITUDES DE VIE\n\n## HISTOIRE SOCIALE ET MILIEU DE VIE\n### Autonomie fonctionnelle\n**AVQ :**\n**AVD :**\n**Mobilité :**\n\n## HMA\n\n## EXAMEN OBJECTIF\n\n## INVESTIGATIONS\n### Laboratoires\n### Imagerie\n\n## IMPRESSION\n\n## PLAN\n\nRédigé à l'aide de la reconnaissance vocale.\n\n## ÉLÉMENTS À VALIDER",
        "phrase_hints": '',
        "sort_order": 103,
        "is_default": False,
        "is_locked": True,
    },
    {
        "name": 'Suivi - Gériatrie',
        "description": 'Note de suivi médical - Gériatrie',
        "language": 'fr',
        "system_instructions": 'Règle audio : le numéro de dossier et le nom de l\'usager sont dictés pour repérer l\'audio — ne les transcris jamais dans la note.\n\nTu structures une NOTE DE SUIVI EN GÉRIATRIE à partir de la transcription dictée.\n\nRegroupe l\'information sous la bonne rubrique, même si le médecin l\'a dictée dans le désordre ou y est revenu plus tard. N\'invente jamais un nom, une date, un chiffre ou un résultat, et n\'utilise jamais un texte de remplissage (« Non servi », « Non abordé », « — ») : une rubrique sans contenu dicté est retirée.\n\nRésumé : les faits saillants, les antécédents importants, ce qui est nouveau depuis la dernière visite, et l\'autonomie fonctionnelle antérieure à l\'hospitalisation si elle est décrite. En phrases courtes. Style déclaratif : n\'ouvre pas chaque phrase par « il/elle » ; énonce le sujet une fois, puis poursuis sans pronom. Ne mets ici aucune information qui figurera en HMA ou en examen objectif.\n\nHMA : ce qui est subjectif — ce qui a été discuté avec le patient ou avec ses proches. Souvent introduite par « M. / Mme rencontré(e) seul(e) / avec son fils… ». Courts paragraphes, phrases courtes. Style déclaratif : n\'ouvre pas chaque phrase par « il/elle » ; énonce le sujet une fois, puis poursuis sans pronom. Ne répète ni le résumé, ni l\'examen objectif.\n\nExamen objectif : l\'examen physique, les signes vitaux, l\'apparence (calme ou agitée), la cognition, l\'examen mental, et le reste. Liste pointée sans titres : « Calme, collabore et orientée », jamais « État général : Calme, collabore, orientée ».\n\nInvestigations : résultats et examens complémentaires dictés.\n\nImpression : l\'impression diagnostique telle que dictée, en liste numérotée. Si elle est dictée à la première personne, transcris-la telle quelle (ex. « Je crois qu\'il s\'agit d\'une maladie d\'Alzheimer »), jamais « Maladie d\'Alzheimer » ni « Le médecin croit… ». Conserve aussi intégralement le raisonnement clinique dicté — revue des effets secondaires d\'un traitement, écart ou rétention d\'une cause — sans le résumer ni le supprimer.\n\nPlan : la conduite à tenir telle que dictée, en liste numérotée. Si elle est dictée à la première personne, transcris-la telle quelle (ex. « Je lui donne congé de la clinique »), jamais « Congé de la clinique » ni « Il lui donne congé ».',
        "layout_format": "# Note de suivi - Gériatrie\n\n## RÉSUMÉ\n\n## HMA\n\n## EXAMEN OBJECTIF\n\n## INVESTIGATIONS\n\n## IMPRESSION\n\n## PLAN\n\nRédigé à l'aide de la reconnaissance vocale.\n\n## ÉLÉMENTS À VALIDER",
        "phrase_hints": '',
        "sort_order": 104,
        "is_default": False,
        "is_locked": True,
    },
)

#: Plus aucun gabarit modifiable livré : les quatre gabarits sont verrouillés
#: (voir le docstring du module). La constante demeure pour l'amorçage d'un
#: éventuel futur gabarit éditable — ``database.seed_editable_templates``
#: n'a alors qu'à la remplir à nouveau.
EDITABLE_TEMPLATES = ()

#: Les quatre noms qui doivent subsister. Tout autre gabarit livré par une
#: version antérieure est supprimé par la migration — voir
#: ``database.purge_legacy_templates``.
KEEP_NAMES = tuple(
    t["name"] for t in LOCKED_TEMPLATES + EDITABLE_TEMPLATES
)
