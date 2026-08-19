"""
default_templates.py — Les quatre gabarits livrés.
===========================================================================

TOUS VERROUILLÉS
----------------
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

DEPUIS LA CONSOLIDATION (2026-08-19)
------------------------------------
Les règles transversales (aucune invention, pas de texte de remplissage,
rubrique vide supprimée, style déclaratif, raisonnement clinique préservé,
première personne d'Impression/Plan, vérification des médicaments, voix
dictée) ne vivent PLUS ici : elles sont énoncées une seule fois dans la
consigne générale (``default_prompts.py``), placée APRÈS ces gabarits dans le
prompt système — c'est elle qui l'emporte en cas de conflit. Ces gabarits ne
portent donc que leur **carte de rubriques** : ce que contient chaque section,
son ordre, son niveau de titre. Le layout (``layout_format``) porte la
structure exacte de titres à reproduire, jusqu'à la section finale
« Éléments à valider » (gabarits français) ou « Items to verify »
(gabarit anglais).
"""

#: Gabarits verrouillés : rafraîchis à chaque démarrage.
LOCKED_TEMPLATES = (
    {
        "name": "Consultation Médicale Générale",
        "description": "Consultation générale : anamnèse, examen, impression et plan.",
        "language": "fr",
        "system_instructions": '# STRUCTURATION CLINIQUE\n\nTu structures une CONSULTATION MÉDICALE GÉNÉRALE dictée par un médecin.\n\nEn-tête — Lieu, Date, Médecin référent, Médecin de famille.\n\nRaison de consultation : dégage la raison principale, clairement et brièvement.\n\nAntécédents médicaux et chirurgicaux.\n\nMédication actuelle : liste pointée, nom + dose. Regroupe un même traitement en une ligne lorsqu\'il sert la même indication (ex. « Metformine 500 mg PO bid, Diamicron MR 90 mg PO die »). Sans titres ni colonnes. Ordonne : 1) médicaments de la cognition, 2) médicaments à impact SNC, 3) autres.\n\nAllergies.\n\nHabitudes de vie : tabac, alcool, drogues.\n\nHistoire sociale (optionnelle) : rapporte de façon factuelle — milieu de vie et réseau de soutien (domicile, RPA, RI, CHSLD; conjoint, proches; services en place), scolarité et emploi, aspects médicolégaux (niveau de soins, directives anticipées, aptitude, mandat, procuration). Profite de cette section pour décrire les AVQ, AVD et la mobilité uniquement lorsqu\'ils sont dictés.\n\nHistoire de la maladie actuelle : mentionne si le patient a été rencontré seul ou accompagné. Récit chronologique concis : début, évolution, facteurs déclenchants, traitements essayés.\n\nExamen physique : liste pointée sans titres — apparence générale, signes vitaux et poids, cognition et langage (si décrits), état mental (si décrit), et le reste.\n\nInvestigations : laboratoires et imagerie.\n\nImpression : liste numérotée de problèmes, du plus actif au moins actif. Le plan reprend la même numérotation lorsque c\'est possible.',
        "layout_format": '# CONSULTATION MÉDICALE\n\n**Lieu de la consultation :**\n**Date de l\'évaluation :**\n**Médecin référent :**\n**Médecin de famille :**\n\n## RAISON DE CONSULTATION\n\n## ANTÉCÉDENTS MÉDICAUX ET CHIRURGICAUX\n\n## MÉDICATION ACTUELLE ET ALLERGIES\n\n## HABITUDES DE VIE\n\n## HISTOIRE SOCIALE\n\n## HISTOIRE DE LA MALADIE ACTUELLE\n\n## EXAMEN PHYSIQUE\n\n## EXAMENS COMPLÉMENTAIRES\n\n## IMPRESSION\n\n## PLAN\n\nRédigé à l\'aide de la reconnaissance vocale.\n\n## ÉLÉMENTS À VALIDER',
        "phrase_hints": "",
        "sort_order": 10,
        "is_default": True,
        "is_locked": True,
    },
    {
        "name": "General Medical Consultation",
        "description": "General consultation: history, examination, impression and plan.",
        "language": "en",
        "system_instructions": '# CLINICAL STRUCTURE\n\nYou structure a GENERAL MEDICAL CONSULTATION dictated by a physician.\n\nHeader — Location, Date, Referring physician, Family physician.\n\nReason for consultation: extract the main reason, clearly and briefly.\n\nPast medical and surgical history.\n\nCurrent medications: bulleted list, name + dose. Group a single treatment on one line when it serves the same indication (e.g. "Metformin 500 mg PO BID, Diamicron MR 90 mg PO daily"). No headings or columns. Order: 1) cognition medications, 2) CNS-acting medications, 3) others.\n\nAllergies.\n\nLifestyle habits: tobacco, alcohol, drugs.\n\nSocial history (optional): report factually — living situation and support network (home, assisted-living residence, intermediate resource, long-term care; spouse, relatives; services in place), education and employment, medicolegal aspects (level of care, advance directives, capacity, mandate, power of attorney). Use this section to describe ADLs, IADLs and mobility only when they are dictated.\n\nHistory of present illness: state whether the patient was seen alone or accompanied. Concise chronological account: onset, evolution, precipitating factors, treatments tried.\n\nPhysical examination: bulleted list without headings — general appearance, vital signs and weight, cognition and language (if described), mental state (if described), and the rest.\n\nInvestigations: laboratory tests and imaging.\n\nImpression: numbered list of problems, from most to least active. The plan uses the same numbering where possible.',
        "layout_format": '# MEDICAL CONSULTATION\n\n**Location of the consultation:**\n**Date of the assessment:**\n**Referring physician:**\n**Family physician:**\n\n## REASON FOR CONSULTATION\n\n## PAST MEDICAL AND SURGICAL HISTORY\n\n## CURRENT MEDICATIONS AND ALLERGIES\n\n## LIFESTYLE HABITS\n\n## SOCIAL HISTORY\n\n## HISTORY OF PRESENT ILLNESS\n\n## PHYSICAL EXAMINATION\n\n## INVESTIGATIONS\n\n## IMPRESSION\n\n## PLAN\n\nWritten using speech recognition.\n\n## ITEMS TO VERIFY',
        "phrase_hints": "",
        "sort_order": 11,
        "is_default": True,
        "is_locked": True,
    },
    {
        "name": 'Consultation - Gériatrie',
        "description": 'Consultation médicale gériatrique standardisée',
        "language": 'fr',
        "system_instructions": '# STRUCTURATION CLINIQUE\n\nTu structures une CONSULTATION MÉDICALE GÉRIATRIQUE à partir de la transcription dictée.\n\nEn-tête — Lieu, Date, Médecin référent, Médecin de famille.\n\nRaison de consultation : dégage la raison principale, clairement et brièvement.\n\nAntécédents médicaux et chirurgicaux.\n\nMédication actuelle : liste pointée, nom + dose. Regroupe un même traitement en une ligne lorsqu\'il sert la même indication (ex. « Metformine 500 mg PO bid, Diamicron MR 90 mg PO die »). Sans titres ni colonnes. Ordonne : 1) médicaments de la cognition, 2) médicaments à impact SNC, 3) autres.\n\nAllergies.\n\nHabitudes de vie : tabac, alcool, drogues.\n\nHistoire sociale et milieu de vie : rapporte de façon factuelle — milieu de vie et réseau de soutien (domicile, RPA, RI, CHSLD; conjoint, proches; services en place), scolarité et emploi, aspects médicolégaux (niveau de soins, directives anticipées, aptitude, mandat, procuration).\n\nAutonomie fonctionnelle : décris les AVQ, AVD et la mobilité uniquement lorsqu\'ils sont dictés.\n\nHMA : mentionne si le patient a été rencontré seul ou accompagné. Récit chronologique concis : début, évolution, facteurs déclenchants, traitements essayés.\n\nExamen objectif : liste pointée sans titres — apparence générale, signes vitaux et poids, cognition et langage (si décrits), état mental (si décrit), et le reste.\n\nInvestigations : laboratoires et imagerie.\n\nImpression : l\'impression diagnostique telle que dictée, en liste numérotée.\n\nPlan : la conduite à tenir telle que dictée, en liste numérotée.',
        "layout_format": "# CONSULTATION EN GÉRIATRIE\n**Lieu de la consultation :**\n**Date de l'évaluation :**\n**Médecin référent :**\n**Médecin de famille :**\n\n## RAISON DE CONSULTATION\n\n## ANTÉCÉDENTS MÉDICAUX ET CHIRURGICAUX\n\n## MÉDICATION ACTUELLE\n\n### ALLERGIES\n\n## HABITUDES DE VIE\n\n## HISTOIRE SOCIALE ET MILIEU DE VIE\n### Autonomie fonctionnelle\n**AVQ :**\n**AVD :**\n**Mobilité :**\n\n## HMA\n\n## EXAMEN OBJECTIF\n\n## INVESTIGATIONS\n### Laboratoires\n### Imagerie\n\n## IMPRESSION\n\n## PLAN\n\nRédigé à l'aide de la reconnaissance vocale.\n\n## ÉLÉMENTS À VALIDER",
        "phrase_hints": '',
        "sort_order": 1,
        "is_default": False,
        "is_locked": True,
    },
    {
        "name": 'Suivi - Gériatrie',
        "description": 'Note de suivi médical - Gériatrie',
        "language": 'fr',
        "system_instructions": 'Tu structures une NOTE DE SUIVI EN GÉRIATRIE à partir de la transcription dictée.\n\nRésumé : les faits saillants, les antécédents importants, ce qui est nouveau depuis la dernière visite, et l\'autonomie fonctionnelle antérieure à l\'hospitalisation si elle est décrite. En phrases courtes. Ne mets ici aucune information qui figurera en HMA ou en examen objectif.\n\nHMA : ce qui est subjectif — ce qui a été discuté avec le patient ou avec ses proches. Souvent introduite par « M. / Mme rencontré(e) seul(e) / avec son fils… ». Courts paragraphes, phrases courtes. Ne répète ni le résumé, ni l\'examen objectif.\n\nExamen objectif : l\'examen physique, les signes vitaux, l\'apparence (calme ou agitée), la cognition, l\'examen mental, et le reste. Liste pointée sans titres : « Calme, collabore et orientée », jamais « État général : Calme, collabore, orientée ».\n\nInvestigations : résultats et examens complémentaires dictés.\n\nImpression : l\'impression diagnostique telle que dictée, en liste numérotée.\n\nPlan : la conduite à tenir telle que dictée, en liste numérotée.',
        "layout_format": "# Note de suivi - Gériatrie\n\n## RÉSUMÉ\n\n## HMA\n\n## EXAMEN OBJECTIF\n\n## INVESTIGATIONS\n\n## IMPRESSION\n\n## PLAN\n\nRédigé à l'aide de la reconnaissance vocale.\n\n## ÉLÉMENTS À VALIDER",
        "phrase_hints": '',
        "sort_order": 2,
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
