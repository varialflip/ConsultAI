"""
default_templates.py — Les quatre gabarits livrés.
===========================================================================

DEUX CATÉGORIES, ET LA DIFFÉRENCE COMPTE
----------------------------------------
**Verrouillés** (``is_locked``) — « Consultation Médicale Générale » et
« General Medical Consultation ». Ni modifiables ni supprimables depuis
l'interface. Ce sont les points de départ : on les DUPLIQUE pour obtenir une
copie indépendante et modifiable. Le refus est appliqué côté serveur, pas
seulement masqué dans l'écran.

Étant intouchables, ils sont RAFRAÎCHIS à chaque démarrage : une amélioration
apportée ici profite aux installations existantes, sans risque d'écraser le
travail de quiconque — personne ne peut les avoir modifiés.

**Modifiables** — « Consultation - Gériatrie » et « Suivi ». Amorcés une seule
fois, puis ils appartiennent au médecin : modifiables, supprimables, et jamais
réécrits. Leur contenu vient de l'installation d'origine, conservé tel quel.

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
        "system_instructions": "Tu structures une CONSULTATION MÉDICALE GÉNÉRALE dictée par un médecin.\n\n1. Restitue l'histoire de la maladie actuelle dans l'ordre chronologique, en\n   distinguant ce que rapporte le patient de ce qu'observe le médecin.\n2. Sépare nettement les données objectives (signes vitaux, examen physique,\n   résultats) de l'interprétation.\n3. N'inscris un antécédent, un médicament ou une allergie que s'il a été dicté.\n   Une rubrique sans contenu dicté est supprimée ; une ligne d'en-tête sans\n   valeur (patient, dossier, demandeur, accompagnateur) perd sa ligne.\n   N'utilise jamais un texte de remplissage (« Non servi », « Non abordé », « — »)\n   et n'invente aucun nom propre.\n4. L'impression est une liste numérotée de problèmes, du plus actif au moins\n   actif. Le plan reprend la même numérotation lorsque c'est possible.\n5. Ne conclus rien que la dictée ne dise pas : ni diagnostic, ni posologie\n   manquante, ni suivi non demandé.\n",
        "layout_format": '# CONSULTATION MÉDICALE\n\n**Patient :** {{PATIENT}}\n**Dossier :** {{DOSSIER}}\n**Date :** {{DATE}}\n**Demande de :** {{DEMANDEUR}}\n**Accompagné de :** {{ACCOMPAGNATEUR}}\n\n## MOTIF DE CONSULTATION\n\n## HISTOIRE DE LA MALADIE ACTUELLE\n\n## ANTÉCÉDENTS\n\n## MÉDICATION\n\n## ALLERGIES\n\n## EXAMEN PHYSIQUE\n\n## EXAMENS COMPLÉMENTAIRES\n\n## IMPRESSION\n\n## PLAN\n\nRédigé à l\'aide de la reconnaissance vocale.',
        "phrase_hints": "",
        "sort_order": 10,
        "is_default": True,
        "is_locked": True,
    },
    {
        "name": "General Medical Consultation",
        "description": "General consultation: history, examination, impression and plan.",
        "language": "en",
        "system_instructions": 'You are structuring a GENERAL MEDICAL CONSULTATION dictated by a physician.\n\n1. Present the history of present illness in chronological order, distinguishing\n   what the patient reports from what the physician observes.\n2. Keep objective data (vital signs, physical examination, results) clearly\n   separate from interpretation.\n3. Record a past history item, a medication or an allergy only if it was\n   dictated. A section with no dictated content is removed; an empty header\n   line (patient, record, requester, companion) loses its line. Never use\n   placeholder filler ("Not addressed", "N/A", a dash) and never invent a\n   proper noun.\n4. The impression is a numbered problem list, from most to least active. The\n   plan follows the same numbering wherever possible.\n5. Conclude nothing the dictation does not state: no diagnosis, no missing\n   dosage, no follow-up that was not requested.\n',
        "layout_format": '# MEDICAL CONSULTATION\n\n**Patient:** {{PATIENT}}\n**Record:** {{DOSSIER}}\n**Date:** {{DATE}}\n**Referred by:** {{DEMANDEUR}}\n**Accompanied by:** {{ACCOMPAGNATEUR}}\n\n## REASON FOR CONSULTATION\n\n## HISTORY OF PRESENT ILLNESS\n\n## PAST MEDICAL HISTORY\n\n## MEDICATIONS\n\n## ALLERGIES\n\n## PHYSICAL EXAMINATION\n\n## INVESTIGATIONS\n\n## IMPRESSION\n\n## PLAN\n\nWritten using speech recognition.',
        "phrase_hints": "",
        "sort_order": 11,
        "is_default": True,
        "is_locked": True,
    },
)

#: Gabarits modifiables : amorcés UNE SEULE FOIS, puis laissés au médecin.
EDITABLE_TEMPLATES = (
    {
        "name": 'Consultation - Gériatrie',
        "description": 'Consultation médicale gériatrique standardisée',
        "language": 'fr',
        "system_instructions": '# STRUCTURATION CLINIQUE\n\nTu structures une CONSULTATION MÉDICALE GÉRIATRIQUE à partir de la transcription dictée.\n\nRegroupe l\'information sous la bonne rubrique, même si le médecin l\'a dictée dans le désordre ou y est revenu plus tard.\n\nEn-tête — Lieu, Date, Médecin référent, Médecin de famille : ne renseigne que les valeurs dictées. Toute ligne d\'en-tête dont la valeur n\'a pas été dictée est retirée. N\'invente jamais un nom, une date ou un lieu, et n\'utilise jamais un texte de remplissage (« Non servi », « Non abordé », « — »).\n\nRaison de consultation : dégage la raison principale, clairement et brièvement.\n\nAntécédents médicaux et chirurgicaux : n\'inscris que les antécédents dictés.\n\nMédication actuelle : liste pointée, nom + dose. Regroupe un même traitement en une ligne lorsqu\'il sert la même indication (ex. « Metformine 500 mg PO bid, Diamicron MR 90 mg PO die »). Sans titres ni colonnes. Ordonne : 1) médicaments de la cognition, 2) médicaments à impact SNC, 3) autres. Vérifie que les médicaments et les doses sont plausibles.\n\nAllergies : n\'inscris que les allergies dictées.\n\nHabitudes de vie : tabac, alcool, cannabis, activité physique — uniquement si dictés.\n\nHistoire sociale et milieu de vie : rapporte de façon factuelle — milieu de vie et réseau de soutien (domicile, RPA, RI, CHSLD; conjoint, proches; services en place), scolarité et emploi, aspects médicolégaux (niveau de soins, directives anticipées, aptitude, mandat, procuration).\n\nAutonomie fonctionnelle : décris les AVQ, AVD et la mobilité uniquement lorsqu\'ils sont dictés.\n\nHMA : mentionne si le patient a été rencontré seul ou accompagné. Récit chronologique concis : début, évolution, facteurs déclenchants, traitements essayés. Style déclaratif : n\'ouvre pas chaque phrase par « il/elle » ; énonce le sujet une fois, puis poursuis sans pronom.\n\nExamen objectif : liste pointée sans titres — apparence générale, signes vitaux et poids, cognition et langage (si décrits), état mental (si décrit), et le reste.\n\nInvestigations : laboratoires et imagerie — résultats dictés uniquement.\n\nImpression : l\'impression diagnostique telle que dictée, en liste numérotée. Si elle est dictée à la première personne, transcris-la telle quelle (ex. « Je crois qu\'il s\'agit d\'une maladie d\'Alzheimer »), jamais « Maladie d\'Alzheimer » ni « Le médecin croit… ».\n\nPlan : la conduite à tenir telle que dictée, en liste numérotée. Si elle est dictée à la première personne, transcris-la telle quelle (ex. « Je lui donne congé de la clinique »), jamais « Congé de la clinique » ni « Il lui donne congé ».',
        "layout_format": "# CONSULTATION EN GÉRIATRIE\n**Lieu de la consultation :**\n**Date de l'évaluation :**\n**Médecin référent :**\n**Médecin de famille :**\n\n## RAISON DE CONSULTATION\n\n## ANTÉCÉDENTS MÉDICAUX ET CHIRURGICAUX\n\n## MÉDICATION ACTUELLE\n\n### ALLERGIES\n\n## HABITUDES DE VIE\n\n## HISTOIRE SOCIALE ET MILIEU DE VIE\n### Autonomie fonctionnelle\n**AVQ :**\n**AVD :**\n**Mobilité :**\n\n## HMA\n\n## EXAMEN OBJECTIF\n\n## INVESTIGATIONS\n### Laboratoires\n### Imagerie\n\n## IMPRESSION\n\n## PLAN\n\nRédigé à l'aide de la reconnaissance vocale.",
        "phrase_hints": '',
        "sort_order": 1,
        "is_default": False,
        "is_locked": False,
    },
    {
        "name": 'Suivi - Gériatrie',
        "description": 'Note de suivi médical - Gériatrie',
        "language": 'fr',
        "system_instructions": 'Règle audio : le numéro de dossier et le nom de l\'usager sont dictés pour repérer l\'audio — ne les transcris jamais dans la note.\n\nTu structures une NOTE DE SUIVI EN GÉRIATRIE à partir de la transcription dictée.\n\nRegroupe l\'information sous la bonne rubrique, même si le médecin l\'a dictée dans le désordre ou y est revenu plus tard. N\'invente jamais un nom, une date, un chiffre ou un résultat, et n\'utilise jamais un texte de remplissage (« Non servi », « Non abordé », « — ») : une rubrique sans contenu dicté est retirée.\n\nRésumé : les faits saillants, les antécédents importants, ce qui est nouveau depuis la dernière visite, et l\'autonomie fonctionnelle antérieure à l\'hospitalisation si elle est décrite. En phrases courtes. Style déclaratif : n\'ouvre pas chaque phrase par « il/elle » ; énonce le sujet une fois, puis poursuis sans pronom. Ne mets ici aucune information qui figurera en HMA ou en examen objectif.\n\nHMA : ce qui est subjectif — ce qui a été discuté avec le patient ou avec ses proches. Souvent introduite par « M. / Mme rencontré(e) seul(e) / avec son fils… ». Courts paragraphes, phrases courtes. Style déclaratif : n\'ouvre pas chaque phrase par « il/elle » ; énonce le sujet une fois, puis poursuis sans pronom. Ne répète ni le résumé, ni l\'examen objectif.\n\nExamen objectif : l\'examen physique, les signes vitaux, l\'apparence (calme ou agitée), la cognition, l\'examen mental, et le reste. Liste pointée sans titres : « Calme, collabore et orientée », jamais « État général : Calme, collabore, orientée ».\n\nInvestigations : résultats et examens complémentaires dictés.\n\nImpression : l\'impression diagnostique telle que dictée, en liste numérotée. Si elle est dictée à la première personne, transcris-la telle quelle (ex. « Je crois qu\'il s\'agit d\'une maladie d\'Alzheimer »), jamais « Maladie d\'Alzheimer » ni « Le médecin croit… ».\n\nPlan : la conduite à tenir telle que dictée, en liste numérotée. Si elle est dictée à la première personne, transcris-la telle quelle (ex. « Je lui donne congé de la clinique »), jamais « Congé de la clinique » ni « Il lui donne congé ».',
        "layout_format": "# Note de suivi - Gériatrie\n\n## RÉSUMÉ\n\n## HMA\n\n## EXAMEN OBJECTIF\n\n## INVESTIGATIONS\n\n## IMPRESSION\n\n## PLAN\n\nRédigé à l'aide de la reconnaissance vocale.",
        "phrase_hints": '',
        "sort_order": 2,
        "is_default": False,
        "is_locked": False,
    },
)

#: Les quatre noms qui doivent subsister. Tout autre gabarit livré par une
#: version antérieure est supprimé par la migration — voir
#: ``database.purge_legacy_templates``.
KEEP_NAMES = tuple(
    t["name"] for t in LOCKED_TEMPLATES + EDITABLE_TEMPLATES
)
