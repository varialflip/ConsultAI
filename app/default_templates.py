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
        "layout_format": '# CONSULTATION MÉDICALE\n\n**Patient :** {{PATIENT}}\n**Dossier :** {{DOSSIER}}\n**Date :** {{DATE}}\n**Demande de :** {{DEMANDEUR}}\n**Accompagné de :** {{ACCOMPAGNATEUR}}\n\n## MOTIF DE CONSULTATION\n\n## HISTOIRE DE LA MALADIE ACTUELLE\n\n## ANTÉCÉDENTS\n\n## MÉDICATION\n\n## ALLERGIES\n\n## EXAMEN PHYSIQUE\n\n## EXAMENS COMPLÉMENTAIRES\n\n## IMPRESSION\n\n## PLAN\n',
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
        "layout_format": '# MEDICAL CONSULTATION\n\n**Patient:** {{PATIENT}}\n**Record:** {{DOSSIER}}\n**Date:** {{DATE}}\n**Referred by:** {{DEMANDEUR}}\n**Accompanied by:** {{ACCOMPAGNATEUR}}\n\n## REASON FOR CONSULTATION\n\n## HISTORY OF PRESENT ILLNESS\n\n## PAST MEDICAL HISTORY\n\n## MEDICATIONS\n\n## ALLERGIES\n\n## PHYSICAL EXAMINATION\n\n## INVESTIGATIONS\n\n## IMPRESSION\n\n## PLAN\n',
        "phrase_hints": "",
        "sort_order": 11,
        "is_default": True,
        "is_locked": True,
    },
)

#: Gabarits modifiables : amorcés UNE SEULE FOIS, puis laissés au médecin.
EDITABLE_TEMPLATES = (
    {
        "name": "Consultation - Gériatrie",
        "description": 'Consultation médicale gériatrique standardisée',
        "language": "fr",
        "system_instructions": "3. STRUCTURATION CLINIQUE - voici quelques précisions pour les sections de la mise en page\n\nRegroupe l'information sous la bonne rubrique même si le médecin l'a dictée dans le désordre ou y est revenu plus tard.\n\n1. **Motif et demandeur** — dégage clairement la raison de la consultation et qui la demande (médecin de famille, urgence, CLSC, CHSLD, équipe traitante).\n2. **Histoire sociale et milieu de vie** — regroupe ici, de façon factuelle et sans développement narratif :\n   - milieu de vie et réseau de soutien : domicile, RPA, RI, CHSLD; présence du conjoint ou des proches; \nservices en place (SAD du CLSC, popote roulante, aide domestique, centre de jour); épuisement du proche aidant;\n   - scolarité, performance à l'école si décrit, emploi antérieur\n   - aspects médicolégaux, lorsqu'abordés : niveau de soins, directives médicales anticipées, aptitude, mandat de protection, conduite automobile.\n3. **Autonomie fonctionnelle** - on doit décrire en AVQ et AVD et la mobilité. AVQ : Hygiène, Habillage, Continence, Alimentation. AVD : Gestion des rendez-vous, des médicaments, des finances, conduite automobile, l'entretien de la maison. La mobilité : usage ou non d'aide technique à la marche. On mentionne souvent dans cette section s'il y a des chutes.\n2. **HMA** — récit chronologique concis : début des symptômes, évolution, facteurs déclenchants, traitements déjà essayés. On inclut aussi les syndromes gériatriques — accorde-leur une attention particulière même s'ils ne sont mentionnés qu'en passant, mais reste dans le registre d'une phrase ou deux par élément, jamais un paragraphe explicatif. Intègre-les au fil du texte, dans la rubrique appropriée, **sans créer un sous-titre pour chacun** :\n   - chutes : nombre, mécanisme, blessures, peur de tomber, aides à la marche;\n   - autonomie fonctionnelle, en distinguant explicitement les **AVQ** (hygiène, habillage, alimentation, transferts, continence) des **AVD/AIVQ** (finances, médication, transport, repas, ménage, téléphone, courses);\n   - cognition, humeur (dépression, anxiété), délirium;\n   - polypharmacie et médicaments potentiellement inappropriés (Beers, STOPP/START), anticholinergiques, benzodiazépines;\n   - dénutrition et perte de poids, sarcopénie, fragilité;\n   - continence, douleur, sommeil, vision, audition;\n   - plaies de pression, iatrogénie.\n\n\n---- **Médication** : présente la liste sur **deux colonnes de médicaments côte à côte** (et non une seule liste verticale) afin de réduire de moitié l'espace vertical occupé. Répartis les médicaments en deux blocs de longueur à peu près égale. Regroupe par système, en plaçant d'abord les médicaments de la cognition, puis ceux à impact SNC (antipsychotiques, antidépresseurs, opiacés, gabapentinoïdes, anticholinergiques). Ne pas mettre de commentaires ici. Ne pas mettre le nom du médicament et la posologie dans des colonnes différentes, simplement écrire le nom du médicament et la dose si dicté. Les colonnes n'ont pas besoin de titre. S'assurer que les médicaments mentionnés existent et que les doses sont compatibles avec le médicament.",
        "layout_format": "# CONSULTATION EN GÉRIATRIE\n**Lieu de la consultation :**\n**Date de l'évaluation :** {{DATE}}\n**Patient :** {{PATIENT}}\n**Demande de :** {{DEMANDEUR}}\n\n## MOTIF DE CONSULTATION\n\n## ANTÉCÉDENTS MÉDICAUX ET CHIRURGICAUX\n\n## MÉDICATION ACTUELLE\n\n### ALLERGIES\n\n## HABITUDES DE VIE\n(Tabac, alcool, cannabis, activité physique.)\n\n## HISTOIRE SOCIALE ET MILIEU DE VIE\n### Autonomie fonctionnelle\n**AVQ :**\n**AVD :**\n**Mobilité :**\n\n## HMA\n\n## EXAMEN OBJECTIF\n\n## INVESTIGATIONS\n### Laboratoires\n### Imagerie\n\n## IMPRESSION DIAGNOSTIQUE\nListe numérotée des problèmes actifs.\n\n## PLAN\n\nRédigé à l'aide de la reconnaissance vocale.",
        "phrase_hints": '',
        "sort_order": 1,
        "is_default": False,
        "is_locked": False,
    },
    {
        "name": "Suivi",
        "description": 'Note de suivi médical',
        "language": "fr",
        "system_instructions": '3. STRUCTURATION CLINIQUE - voici quelques précisions pour les sections de la mise en page\n\nRegroupe l\'information sous la bonne rubrique même si le médecin l\'a dictée dans le désordre ou y est revenu plus tard.\n\n3.1 Résumé : Le clinicien dicte ici les faits saillants, les antécédants importants, ce qui est nouveau depuis la dernière visite\n\n3.2 HMA : On restranscrit ici ce qui est dicté de "subjectif" : ce qui est discuté avec la patient, ce qui est discuté avec les autres\n\n3.2 Examen Objectif : c\'est l\'examen physique incluant les signes vitaux, l\'apparence, la cognition\n\n3.3 Impression : Retranscrire en phrases courtes ce qui est dicté, idéalement en liste numérotée\n\n3.4 Plan :  Retranscrire en phrases courtes ce qui est dicté, idéalement en liste numérotée',
        "layout_format": "# Note de suivi\n\n## RÉSUMÉ\n\n## HMA\n\n## EXAMEN OBJECTIF\n\n## INVESTIGATIONS\n\n## IMPRESSION\n\n## PLAN\n\nRédigé à l'aide de la reconnaissance vocale.",
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
