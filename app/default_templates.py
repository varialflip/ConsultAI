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
        "system_instructions": "Tu structures une CONSULTATION MÉDICALE GÉNÉRALE dictée par un médecin.\n\n1. Restitue l'histoire de la maladie actuelle dans l'ordre chronologique, en\n   distinguant ce que rapporte le patient de ce qu'observe le médecin.\n2. Sépare nettement les données objectives (signes vitaux, examen physique,\n   résultats) de l'interprétation.\n3. N'inscris un antécédent, un médicament ou une allergie que s'il a été dicté.\n   Une rubrique sans contenu dicté reçoit « Non abordé lors de la dictée. »\n4. L'impression est une liste numérotée de problèmes, du plus actif au moins\n   actif. Le plan reprend la même numérotation lorsque c'est possible.\n5. Ne conclus rien que la dictée ne dise pas : ni diagnostic, ni posologie\n   manquante, ni suivi non demandé.\n",
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
        "system_instructions": 'You are structuring a GENERAL MEDICAL CONSULTATION dictated by a physician.\n\n1. Present the history of present illness in chronological order, distinguishing\n   what the patient reports from what the physician observes.\n2. Keep objective data (vital signs, physical examination, results) clearly\n   separate from interpretation.\n3. Record a past history item, a medication or an allergy only if it was\n   dictated. A section with no dictated content receives\n   "Not addressed during dictation."\n4. The impression is a numbered problem list, from most to least active. The\n   plan follows the same numbering wherever possible.\n5. Conclude nothing the dictation does not state: no diagnosis, no missing\n   dosage, no follow-up that was not requested.\n',
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
        "system_instructions": '# STRUCTURATION CLINIQUE\n\nRegroupe l\'information sous la bonne rubrique même si le médecin l\'a dictée dans le désordre ou y est revenu plus tard.\n## 0. Informations\nInscrire les informations retrouvées à l\'entête de la mise en page\nSi le médecin de famille n\'est pas mentionné, retirer la rubrique du même nom\n\n## 1. Raison de consultation\nDégagez clairement la raison de la consultation\n\n## 2. Histoire sociale et milieu de vie\n\nRegroupez de façon factuelle et sans développement narratif :\n\n- **Milieu de vie et réseau de soutien** : domicile, RPA, RI, CHSLD; présence du conjoint ou des proches; services en place (SAD du CLSC, popote roulante, aide domestique, centre de jour); épuisement du proche aidant\n- **Scolarité et emploi** : performance à l\'école si décrit, emploi antérieur\n- **Aspects médicolégaux** : niveau de soins, directives médicales anticipées, aptitude, mandat, testament, procuration\n\n## 3. Autonomie fonctionnelle\n\nDécrivez les AVQ, AVD et la mobilité :\n\n- **AVQ** : hygiène, habillage, continence, alimentation\n- **AVD** : gestion des rendez-vous, des médicaments, des finances, conduite automobile, entretien de la maison\n- **Mobilité** : usage ou non d\'aide technique à la marche; présence de chutes\n\n## 4. Histoire de la maladie actuelle (HMA)\n\nMentionnez si le patient a été rencontré seul ou accompagné. Récit chronologique concis : début des symptômes, évolution, facteurs déclenchants, traitements essayés.\n\n## Examen objectif\n\nPrésentez en **liste pointée** l\'ensemble de l\'examen incluant:\nApparence générale, Signes vitaux et poids, Cognition et langage (si décrit), État mental (si décrit) ainsi que le reste. Ne pas titrer chaque point\n\n## Médication actuelle\n\nPrésenter en liste pointée. il est possible de regrouper certains médicaments ensembles lorsqu\'ils sont pour la même indication, par exemple : "Metformine 500mg PO bid, Diamicron MR 90mg PO die". Ne jamais mettre de titres pour chaque ligne de médicament. \nOn doit réorganiser la liste dans cet ordre (toujours sans titre):\n1. Médicaments de la cognition (rivastigmine, donepezil, ...)\n2. Médicaments à impact SNC (antipsychotiques, antidépresseurs, opiacés, gabapentinoïdes, anticholinergiques)\n3. Autres médicaments\n**Format** : nom du médicament et dose (sans commentaires ni colonnes séparées). Assurez-vous que les médicaments existent et que les doses sont compatibles.\n\n## Impression \n\nL\'impression diagnostique telle que dictée. Si dicté à la première personne du singulier, transcrire idem. Par exemple "Je crois qu\'il s\'agit d\'une maladie d\'Alzheimer", pas "Maladie d\'Alzheimer". Présenter en une liste numérotée.\n\n## Plan\n\nLa conduite à tenir, telle que dictée. Si dicté à la première personne du singulier, transcrire idem. Par exemple "Je lui donne congé de la clinique", pas "Congé de la clinique". Présenter en une liste numérotée.',
        "layout_format": "# CONSULTATION EN GÉRIATRIE\n**Lieu de la consultation :**\n**Date de l'évaluation :**\n**Médecin référent :**\n**Médecin de famille :**\n\n## RAISON DE CONSULTATION\n\n## ANTÉCÉDENTS MÉDICAUX ET CHIRURGICAUX\n\n## MÉDICATION ACTUELLE\n\n### ALLERGIES\n\n## HABITUDES DE VIE\n(Tabac, alcool, cannabis, activité physique.)\n\n## HISTOIRE SOCIALE ET MILIEU DE VIE\n### Autonomie fonctionnelle\n**AVQ :**\n**AVD :**\n**Mobilité :**\n\n## HMA\n\n## EXAMEN OBJECTIF\n\n## INVESTIGATIONS\n### Laboratoires\n### Imagerie\n\n## IMPRESSION\n\n## PLAN\n\nRédigé à l'aide de la reconnaissance vocale.",
        "phrase_hints": '',
        "sort_order": 1,
        "is_default": False,
        "is_locked": False,
    },
    {
        "name": 'Suivi - Gériatrie',
        "description": 'Note de suivi médical - Gériatrie',
        "language": 'fr',
        "system_instructions": 'Ne pas transcrire le numéro de dossier ou le nom de l\'usager, c\'est uniquement pour l\'audio.\n\n3. STRUCTURATION CLINIQUE - voici quelques précisions pour les sections de la mise en page\n\nRegroupe l\'information sous la bonne rubrique même si le médecin l\'a dictée dans le désordre ou y est revenu plus tard.\n\n3.1 Résumé : Le clinicien dicte ici les faits saillants, les antécédants importants, ce qui est nouveau depuis la dernière visite. En phrases courtes / paragraphes courts. Si on parle d\'autonomie fonctionnelle pré-hospit, c\'est ici qu\'on le retrouve. ne pas mettre d\'information ici qui se retrouvera dans hma ou examen objectif.\n\n3.2 HMA : On restranscrit ici ce qui est dicté de "subjectif" : ce qui est discuté avec la patient, ce qui est discuté avec les autres. Attention de ne pas re-décrire ce qui est déjà dans le résumé. Présnter sous formes de courts paragraphes, en phrases courtes. Souvent cette section commence par "M / Mme rencontré seul / rencontré avec son fils" etc. Attention de ne pas répéter ce qui sera à l\'examen objectif.\n\n3.2 Examen Objectif : c\'est l\'examen physique incluant les signes vitaux, l\'apparence (incluant être calme ou agité), la cognition, l\'examen mental, ainsi que tout le reste. Présenter en liste pointée, par exemple "- Calme, collabore et orientée". Ne pas présenter avec des titres, par exemple "- État général : Calme, collabore, orientée".\n\n3.3 Investigation\n\n3.4 Impression: L\'impression diagnostique telle que dictée. Si dicté à la première personne du singulier, transcrire idem. Par exemple "Je crois qu\'il s\'agit d\'une maladie d\'Alzheimer", pas "Maladie d\'Alzheimer". Présenter en une liste numérotée.\n\n3.5 Plan: La conduite à tenir, telle que dictée. Si dicté à la première personne du singulier, transcrire idem. Par exemple "Je lui donne congé de la clinique", pas "Congé de la clinique". Présenter en une liste numérotée.',
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
