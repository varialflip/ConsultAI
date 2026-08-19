"""
tests/test_note_pipeline.py — validateur + rendu, sans appel modèle.
===================================================================================

``unittest`` (stdlib) plutôt que pytest : pas de dépendance de test dans
requirements.txt aujourd'hui (voir son commentaire sur l'image légère pour
NAS), et ce n'est pas ce chantier qui doit l'ajouter.

Lance : ``python3 -m unittest tests.test_note_pipeline -v`` depuis la racine
du dépôt.
"""

import copy
import json
import unittest
import urllib.error
from unittest import mock

from app.default_templates import LOCKED_TEMPLATES
from app import drug_lookup, llm, note_extraction, runtime_config
from app.note_extraction import build_expected_json_skeleton, extract_note, validate_and_repair, verify_medications_post_extraction
from app.note_renderer import OWN_CONTENT_KEY, render
from app.note_schema import DrugLookup, ElementAValider, ExtractedNote, GroundedField, parse_layout
from app.note_validator import check_drug_lookups, check_medication_omitted_from_list, validate

GENERAL_LAYOUT = next(t for t in LOCKED_TEMPLATES if t["name"] == "Consultation Médicale Générale")["layout_format"]
GERIATRIE_LAYOUT = next(t for t in LOCKED_TEMPLATES if t["name"] == "Consultation - Gériatrie")["layout_format"]
EN_LAYOUT = next(t for t in LOCKED_TEMPLATES if t["name"] == "General Medical Consultation")["layout_format"]

TRANSCRIPT_FR = (
    "Patiente de 78 ans. Raison de consultation : suivi de mémoire. "
    "Prégabaline 75 mg PO bid. Je crois qu'il s'agit d'une maladie d'Alzheimer débutante. "
    "Non servi pour le reste."
)


def _base_note() -> ExtractedNote:
    return ExtractedNote(
        header_fields={"Lieu de la consultation": "Clinique ABC", "Date de l'évaluation": "2026-08-17"},
        sections={
            "RAISON DE CONSULTATION": "Suivi de mémoire.",
            "MÉDICATION ACTUELLE": {OWN_CONTENT_KEY: "Prégabaline 75 mg PO bid."},
            "IMPRESSION": "1. Je crois qu'il s'agit d'une maladie d'Alzheimer débutante.",
            "PLAN": "1. Poursuite du suivi.",
        },
        elements_a_valider=[ElementAValider(kind="item", terme_dicte="dose prégabaline", correction="75 mg")],
        grounded_fields=[
            GroundedField(field="dose_pregabaline", value="75 mg", source_span="Prégabaline 75 mg PO bid"),
        ],
    )


class ParseLayoutTests(unittest.TestCase):
    def test_general_template_headings_and_boilerplate(self):
        layout = parse_layout(GENERAL_LAYOUT)
        self.assertEqual(layout.title, "CONSULTATION MÉDICALE")
        self.assertTrue(layout.has_elements_a_valider)
        self.assertIn("Lieu de la consultation", layout.top_level_fields())
        headings = [h.label for h in layout.headings()]
        self.assertIn("MÉDICATION ACTUELLE", headings)
        self.assertIn("IMPRESSION", headings)
        allergies = next(h for h in layout.headings() if h.label == "ALLERGIES")
        self.assertEqual(allergies.parent, "MÉDICATION ACTUELLE")
        self.assertIn("Rédigé à l'aide de la reconnaissance vocale.", layout.literals_of(None))

    def test_en_template_has_no_elements_a_valider(self):
        layout = parse_layout(EN_LAYOUT)
        self.assertFalse(layout.has_elements_a_valider)

    def test_geriatrie_nested_subsections(self):
        layout = parse_layout(GERIATRIE_LAYOUT)
        allergies = next(h for h in layout.headings() if h.label == "ALLERGIES")
        self.assertEqual(allergies.parent, "MÉDICATION ACTUELLE")
        avq = next(e for e in layout.entries if e.kind == "bold_field" and e.label == "AVQ")
        self.assertEqual(avq.parent, "Autonomie fonctionnelle")
        labs = next(h for h in layout.headings() if h.label == "Laboratoires")
        self.assertEqual(labs.parent, "INVESTIGATIONS")


CUSTOM_LAYOUT_WITH_INSTRUCTION_PLACEHOLDER = (
    "# CONSULTATION EN GÉRIATRIE\n"
    "**Médecin référent :**\n\n"
    "## IMPRESSION\n"
    "{{Phrase résumé}}\n"
    "1. {{Problème 1}}\n"
    "2. {{Problème 2}}\n"
    "{{etc.}}\n\n"
    "## PLAN\n\n"
    "Rédigé à l'aide de la reconnaissance vocale.\n\n"
    "## ÉLÉMENTS À VALIDER"
)


class ParseLayoutInstructionTests(unittest.TestCase):
    """Régression : un gabarit personnalisé (médecin) peut contenir des
    lignes d'exemple/consigne entre accolades DANS une rubrique — distinctes
    du texte fixe (boilerplate) qui suit la dernière rubrique de contenu.
    Voir consultation #6 sur test.dictai.ca, gabarit "Consultation -
    Gériatrie (FD)" : ces lignes fuitaient telles quelles dans le rendu avant
    le correctif (kind="instruction")."""

    def test_instruction_placeholder_not_treated_as_boilerplate(self):
        layout = parse_layout(CUSTOM_LAYOUT_WITH_INSTRUCTION_PLACEHOLDER)
        self.assertNotIn("{{Phrase résumé}}", layout.literals_of(None))
        self.assertIn("Rédigé à l'aide de la reconnaissance vocale.", layout.literals_of(None))

    def test_render_never_echoes_instruction_placeholders(self):
        layout = parse_layout(CUSTOM_LAYOUT_WITH_INSTRUCTION_PLACEHOLDER)
        note = ExtractedNote(
            header_fields={"Médecin référent": "Dr Melendez-Pena"},
            sections={"IMPRESSION": "1. Récidive de cancer pulmonaire.", "PLAN": "Poursuite du suivi."},
            elements_a_valider=[ElementAValider(kind="item", terme_dicte="x", correction="y")],
        )
        markdown = render(note, layout)
        self.assertNotIn("{{", markdown)
        self.assertIn("Rédigé à l'aide de la reconnaissance vocale.", markdown)


class SkeletonTests(unittest.TestCase):
    def test_skeleton_reflects_nesting(self):
        layout = parse_layout(GERIATRIE_LAYOUT)
        skeleton = build_expected_json_skeleton(layout)
        self.assertIn("ALLERGIES", skeleton["sections"]["MÉDICATION ACTUELLE"])
        self.assertIn("AVQ", skeleton["sections"]["HISTOIRE SOCIALE ET MILIEU DE VIE"]["Autonomie fonctionnelle"])

    def test_skeleton_offers_own_content_slot_for_container_headings(self):
        layout = parse_layout(GERIATRIE_LAYOUT)
        skeleton = build_expected_json_skeleton(layout)
        self.assertIn(OWN_CONTENT_KEY, skeleton["sections"]["MÉDICATION ACTUELLE"])

    def test_skeleton_folds_instruction_placeholder_into_hint(self):
        layout = parse_layout(CUSTOM_LAYOUT_WITH_INSTRUCTION_PLACEHOLDER)
        skeleton = build_expected_json_skeleton(layout)
        self.assertIn("{{Phrase résumé}}", skeleton["sections"]["IMPRESSION"])

    def test_skeleton_nudges_bulleted_array_for_marked_leaf_sections(self):
        """Régression : Médication/Antécédents/Examen étaient rendus en
        prose (chaîne, virgules) plutôt qu'en liste malgré la consigne
        « liste pointée » du gabarit — le modèle n'avait aucun indice
        explicite l'invitant à choisir un tableau JSON plutôt qu'une
        chaîne. Voir LayoutSpec.explicit_list_style / note_extraction._list_style_nudge."""
        layout = parse_layout(GERIATRIE_LAYOUT)
        skeleton = build_expected_json_skeleton(layout)
        self.assertIn("liste À PUCES", skeleton["sections"]["ANTÉCÉDENTS MÉDICAUX ET CHIRURGICAUX"])
        self.assertIn("liste À PUCES", skeleton["sections"]["MÉDICATION ACTUELLE"][OWN_CONTENT_KEY])
        self.assertIn("liste À PUCES", skeleton["sections"]["EXAMEN OBJECTIF"])
        self.assertIn("liste À PUCES", skeleton["sections"]["INVESTIGATIONS"]["Laboratoires"])

    def test_skeleton_does_not_nudge_unmarked_sections(self):
        layout = parse_layout(GERIATRIE_LAYOUT)
        skeleton = build_expected_json_skeleton(layout)
        self.assertNotIn("liste À PUCES", skeleton["sections"]["HABITUDES DE VIE"])
        self.assertNotIn("liste NUMÉROTÉE", skeleton["sections"]["HABITUDES DE VIE"])

    def test_explicit_list_style_distinguishes_marked_from_default(self):
        layout = parse_layout(GERIATRIE_LAYOUT)
        self.assertEqual(layout.explicit_list_style("MÉDICATION ACTUELLE"), "bulleted")
        self.assertEqual(layout.explicit_list_style("IMPRESSION"), "numbered")
        self.assertIsNone(layout.explicit_list_style("HABITUDES DE VIE"))
        # list_style() (utilisé par le RENDU) retombe quand même sur "bulleted"
        # par défaut — seul explicit_list_style() (utilisé pour la CONSIGNE)
        # fait la distinction.
        self.assertEqual(layout.list_style("HABITUDES DE VIE"), "bulleted")


class ValidatorTests(unittest.TestCase):
    def test_clean_note_passes(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        result = validate(note, layout, TRANSCRIPT_FR)
        self.assertEqual(result.blocked, [])
        self.assertEqual(result.needs_repair, [])

    def test_forbidden_filler_auto_fixed(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.sections["HABITUDES DE VIE"] = "Non servi"
        result = validate(note, layout, TRANSCRIPT_FR)
        self.assertNotIn("HABITUDES DE VIE", note.sections)
        self.assertTrue(any(i.code == "filler_value" for i in result.issues))

    def test_empty_elements_a_valider_is_blocked(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.elements_a_valider = []
        result = validate(note, layout, TRANSCRIPT_FR)
        self.assertTrue(any(i.code == "elements_a_valider_empty" for i in result.blocked))

    def test_epistemic_clause_dropped_is_flagged(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.sections["IMPRESSION"] = "1. Maladie d'Alzheimer débutante."  # perd le "je crois"
        result = validate(note, layout, TRANSCRIPT_FR)
        self.assertTrue(any(i.code == "epistemic_clause_dropped" for i in result.needs_repair))

    def test_grounding_mismatch_flagged(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.grounded_fields = [GroundedField(field="dose_x", value="500 mg", source_span="ceci n'est pas dans la transcription")]
        result = validate(note, layout, TRANSCRIPT_FR)
        self.assertTrue(any(i.code == "grounding_mismatch" for i in result.needs_repair))

    def test_html_and_placeholder_flagged(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.sections["RAISON DE CONSULTATION"] = "Suivi <b>de mémoire</b> {{DATE}}."
        result = validate(note, layout, TRANSCRIPT_FR)
        codes = {i.code for i in result.needs_repair}
        self.assertIn("html_markup", codes)
        self.assertIn("placeholder_leftover", codes)

    def test_cramped_numbered_list_flagged(self):
        """Régression : le modèle a écrit une liste numérotée sans saut de
        ligne entre les items (vu réellement, test.dictai.ca 2026-08-18,
        mistral-small-latest : « 1. Augmentation... 2. Compléter... 3.
        Traiter... » tout sur une ligne au lieu d'une vraie liste numérotée)."""
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.sections["PLAN"] = (
            "1. Augmentation de la rispéridone à 0.60 mg PO HS. "
            "2. Compléter le bilan avec un scan cérébral. "
            "3. Traiter le déficit en vitamine B12."
        )
        result = validate(note, layout, TRANSCRIPT_FR)
        self.assertTrue(any(i.code == "cramped_numbered_list" for i in result.needs_repair))

    def test_properly_line_broken_numbered_list_not_flagged(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.sections["PLAN"] = "1. Augmentation de la rispéridone.\n2. Compléter le bilan.\n3. Traiter le déficit."
        result = validate(note, layout, TRANSCRIPT_FR)
        self.assertFalse(any(i.code == "cramped_numbered_list" for i in result.needs_repair))

    def test_filler_variant_not_in_original_examples_is_caught(self):
        """Régression : « non dictée » (invention d'un modèle plus faible,
        test.dictai.ca 2026-08-18) n'était pas dans la liste d'exemples
        d'origine de la consigne ; le filtre doit couvrir cette famille de
        formulations, pas seulement les exemples cités mot pour mot."""
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.header_fields["Date de l'évaluation"] = "non dictée"
        note.sections["HABITUDES DE VIE"] = "non dictées"
        result = validate(note, layout, TRANSCRIPT_FR)
        self.assertNotIn("Date de l'évaluation", note.header_fields)
        self.assertNotIn("HABITUDES DE VIE", note.sections)
        self.assertTrue(any(i.code == "filler_value" for i in result.issues))

    def test_noop_self_correction_is_dropped(self):
        """Régression : une « correction » identique au terme dicté (vu
        réellement, test.dictai.ca 2026-08-18) n'apporte aucune information —
        auto-retirée plutôt que présentée au médecin comme un changement."""
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.elements_a_valider.append(
            ElementAValider(kind="item", terme_dicte="Puis je garde le dossier", correction="Puis je garde le dossier")
        )
        result = validate(note, layout, TRANSCRIPT_FR)
        self.assertFalse(any(e.terme_dicte == "Puis je garde le dossier" for e in note.elements_a_valider))
        self.assertTrue(any(i.code == "correction_is_noop" for i in result.issues))

    def test_meta_word_as_correction_is_demoted_to_unconfirmed(self):
        """Régression : le modèle a écrit le mot « à confirmer » DANS le
        champ correction lui-même (test.dictai.ca 2026-08-18), produisant
        « → correction apportée : à confirmer ». Démis en à-confirmer réel."""
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.elements_a_valider.append(
            ElementAValider(kind="item", terme_dicte="Pré-gabalin 16C", correction="à confirmer")
        )
        result = validate(note, layout, TRANSCRIPT_FR)
        item = next(e for e in note.elements_a_valider if e.terme_dicte == "Pré-gabalin 16C")
        self.assertIsNone(item.correction)
        self.assertTrue(any(i.code == "correction_is_meta_word" for i in result.issues))

    def test_duplicate_correction_demoted_to_unconfirmed(self):
        """Régression réelle (test.dictai.ca 2026-08-18, consultation #9,
        mistral-small-latest) : « L'ensoprazole 30 Activant 0.5 au coucher au
        besoin » dicté sans pause claire a été fusionné en UN médicament —
        « Activant » a été « corrigé » vers « Ésoméprazole », qui figurait
        déjà comme médicament séparé dans MÉDICATION ACTUELLE. La correction
        mensongère est démise en à-confirmer plutôt que gardée telle quelle —
        ne récupère pas le médicament fusionné disparu, mais arrête la note
        de prétendre que la fusion était correcte."""
        layout = parse_layout(GERIATRIE_LAYOUT)
        note = _base_note()
        note.sections["MÉDICATION ACTUELLE"] = {OWN_CONTENT_KEY: "Ésoméprazole 30 mg PO die"}
        note.elements_a_valider.append(
            ElementAValider(kind="item", terme_dicte="Activant", correction="Ésoméprazole")
        )
        result = validate(note, layout, TRANSCRIPT_FR)
        item = next(e for e in note.elements_a_valider if e.terme_dicte == "Activant")
        self.assertIsNone(item.correction)
        self.assertTrue(any(i.code == "correction_is_duplicate" for i in result.issues))

    def test_two_mishearings_of_same_drug_are_not_flagged_as_duplicate(self):
        """Régression négative : « Respirone » et « Rispiridone » corrigées
        toutes deux vers « rispéridone » (vu réellement, même consultation)
        sont deux mishearings LÉGITIMES du même médicament, pas une fusion —
        ce cas ne doit JAMAIS être démis. La vérification ne compare la
        correction qu'au contenu déjà gardé dans ``sections``, jamais aux
        autres éléments d'Éléments à valider."""
        layout = parse_layout(GERIATRIE_LAYOUT)
        note = _base_note()
        note.elements_a_valider.append(ElementAValider(kind="item", terme_dicte="Respirone", correction="rispéridone"))
        note.elements_a_valider.append(ElementAValider(kind="item", terme_dicte="Rispiridone", correction="rispéridone"))
        result = validate(note, layout, TRANSCRIPT_FR)
        for terme in ("Respirone", "Rispiridone"):
            item = next(e for e in note.elements_a_valider if e.terme_dicte == terme)
            self.assertEqual(item.correction, "rispéridone")
        self.assertFalse(any(i.code == "correction_is_duplicate" for i in result.issues))


class RendererTests(unittest.TestCase):
    def test_render_drops_empty_sections_and_keeps_boilerplate(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        markdown = render(note, layout)
        self.assertIn("## RAISON DE CONSULTATION", markdown)
        self.assertNotIn("## HABITUDES DE VIE", markdown)
        self.assertIn("Rédigé à l'aide de la reconnaissance vocale.", markdown)
        self.assertIn("**Lieu de la consultation :** Clinique ABC", markdown)

    def test_render_elements_a_valider_grammar(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.elements_a_valider = [
            ElementAValider(kind="item", terme_dicte="nom du patient : Georges Thhiber", correction="Georges Tibert"),
            ElementAValider(kind="item", terme_dicte="dose : 2,5 ou 5 mg", correction=None),
        ]
        markdown = render(note, layout)
        self.assertIn("nom du patient : Georges Thhiber → **correction apportée : Georges Tibert**", markdown)
        self.assertIn("dose : 2,5 ou 5 mg → **à confirmer**", markdown)

    def test_render_geriatrie_nested_fields(self):
        layout = parse_layout(GERIATRIE_LAYOUT)
        note = ExtractedNote(
            sections={
                "RAISON DE CONSULTATION": "Évaluation cognitive.",
                "HISTOIRE SOCIALE ET MILIEU DE VIE": {"Autonomie fonctionnelle": {"AVQ": "Autonome", "Mobilité": "Marche avec canne"}},
            },
            elements_a_valider=[ElementAValider(kind="group", texte_groupe="3 dates approximatives non confirmées")],
        )
        markdown = render(note, layout)
        self.assertIn("### Autonomie fonctionnelle", markdown)
        self.assertIn("**AVQ :** Autonome", markdown)
        self.assertNotIn("**AVD", markdown)  # non dictée, absente
        self.assertIn("- 3 dates approximatives non confirmées", markdown)

    def test_render_is_idempotent_deepcopy_safe(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        before = copy.deepcopy(note)
        render(note, layout)
        self.assertEqual(note, before)

    def test_render_list_valued_section_becomes_bullets(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.sections["EXAMEN PHYSIQUE"] = ["TA 150/80", "Poids 82 kg"]
        markdown = render(note, layout)
        self.assertIn("- TA 150/80", markdown)
        self.assertIn("- Poids 82 kg", markdown)
        result = validate(note, layout, TRANSCRIPT_FR)
        self.assertFalse(any(i.code == "unexpected_value_type" for i in result.issues))

    def test_list_style_numbered_marker_detected_for_impression_and_plan(self):
        layout = parse_layout(GERIATRIE_LAYOUT)
        self.assertEqual(layout.list_style("IMPRESSION"), "numbered")
        self.assertEqual(layout.list_style("PLAN"), "numbered")
        self.assertEqual(layout.list_style("MÉDICATION ACTUELLE"), "bulleted")

    def test_render_array_valued_plan_is_numbered_not_bulleted(self):
        """Régression : le gabarit Gériatrie demande une liste NUMÉROTÉE pour
        PLAN ; le modèle qui encode PLAN comme un tableau JSON (forme
        maintenant demandée, voir note_extraction._leaf_placeholder) doit
        obtenir « 1. »/« 2. », pas des puces « - »."""
        layout = parse_layout(GERIATRIE_LAYOUT)
        note = _base_note()
        note.sections["PLAN"] = ["Augmentation de la rispéridone.", "Compléter le bilan.", "Traiter le déficit."]
        markdown = render(note, layout)
        self.assertIn("1. Augmentation de la rispéridone.", markdown)
        self.assertIn("2. Compléter le bilan.", markdown)
        self.assertIn("3. Traiter le déficit.", markdown)
        self.assertNotIn("- Augmentation de la rispéridone.", markdown)

    def test_render_strips_model_written_numbering_before_renumbering(self):
        """Régression réelle (consultation #5, test.dictai.ca, mistral-small-
        latest) : le modèle a écrit ses propres « 1. »/« 2. » DANS certains
        items d'un tableau JSON malgré la consigne de ne pas le faire, ce qui
        produisait une double numérotation (« 2. 1. Trouble délirant... »)."""
        layout = parse_layout(GERIATRIE_LAYOUT)
        note = _base_note()
        note.sections["IMPRESSION"] = [
            "Je crois qu'il s'agit d'un trouble délirant tardif.",
            "1. Trouble délirant tardif avec idées paranoïdes.",
            "2. Troubles cognitifs légers sans détérioration dégénérative évidente.",
        ]
        markdown = render(note, layout)
        self.assertIn("1. Je crois qu'il s'agit d'un trouble délirant tardif.", markdown)
        self.assertIn("2. Trouble délirant tardif avec idées paranoïdes.", markdown)
        self.assertIn("3. Troubles cognitifs légers sans détérioration dégénérative évidente.", markdown)
        self.assertNotIn("2. 1.", markdown)
        self.assertNotIn("3. 2.", markdown)

    def test_render_strips_model_written_bullets_in_bulleted_list(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.sections["EXAMEN PHYSIQUE"] = ["- TA 150/80", "• Poids 82 kg"]
        markdown = render(note, layout)
        self.assertIn("- TA 150/80", markdown)
        self.assertIn("- Poids 82 kg", markdown)
        self.assertNotIn("- - TA", markdown)
        self.assertNotIn("- • Poids", markdown)

    def test_render_array_valued_medication_stays_bulleted(self):
        layout = parse_layout(GERIATRIE_LAYOUT)
        note = _base_note()
        note.sections["MÉDICATION ACTUELLE"] = {OWN_CONTENT_KEY: ["Prégabaline 75 mg PO bid.", "Rivaroxaban 20 mg die."]}
        markdown = render(note, layout)
        self.assertIn("- Prégabaline 75 mg PO bid.", markdown)
        self.assertIn("- Rivaroxaban 20 mg die.", markdown)

    def test_boilerplate_survives_empty_plan(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        del note.sections["PLAN"]
        markdown = render(note, layout)
        self.assertNotIn("## PLAN", markdown)
        self.assertIn("Rédigé à l'aide de la reconnaissance vocale.", markdown)

    def test_unexpected_type_flagged_not_dropped(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.sections["RAISON DE CONSULTATION"] = 42  # jamais produit par un modèle correct
        result = validate(note, layout, TRANSCRIPT_FR)
        self.assertTrue(any(i.code == "unexpected_value_type" for i in result.needs_repair))
        markdown = render(note, layout)
        self.assertIn("42", markdown)  # visible, jamais disparu silencieusement

    def test_empty_elements_a_valider_omits_section_rather_than_fabricating(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.elements_a_valider = []
        markdown = render(note, layout)
        self.assertNotIn("ÉLÉMENTS À VALIDER", markdown)

    def test_render_own_content_and_subsection_together(self):
        """Régression : MÉDICATION ACTUELLE a sa propre liste de médicaments
        ET une sous-rubrique ALLERGIES imbriquée — les deux doivent apparaître,
        pas seulement l'une ou l'autre. Vu réellement sur test.dictai.ca
        (2026-08-18, mistral-small-latest) : MÉDICATION ACTUELLE ressortait
        vide, seule sa sous-rubrique ALLERGIES avait du contenu."""
        layout = parse_layout(GERIATRIE_LAYOUT)
        note = ExtractedNote(
            sections={
                "RAISON DE CONSULTATION": "Suivi.",
                "MÉDICATION ACTUELLE": {
                    OWN_CONTENT_KEY: ["Prégabaline 75 mg PO bid", "Rivaroxaban 20 mg"],
                    "ALLERGIES": "Pénicilline.",
                },
            },
        )
        markdown = render(note, layout)
        self.assertIn("## MÉDICATION ACTUELLE", markdown)
        self.assertIn("- Prégabaline 75 mg PO bid", markdown)
        self.assertIn("- Rivaroxaban 20 mg", markdown)
        self.assertIn("### ALLERGIES", markdown)
        self.assertIn("Pénicilline.", markdown)


class RepairTests(unittest.TestCase):
    def test_placeholder_leftover_repaired_via_mocked_llm(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.sections["RAISON DE CONSULTATION"] = "Suivi {{DATE}} de mémoire."
        fake = llm.Completion(text='{"new_value": "Suivi de mémoire."}', model="fake", provider="fake")
        with mock.patch.object(llm, "complete", return_value=fake) as complete_mock:
            result = validate_and_repair(note, layout, TRANSCRIPT_FR, model="fake-model")
        complete_mock.assert_called_once()
        self.assertEqual(note.sections["RAISON DE CONSULTATION"], "Suivi de mémoire.")
        self.assertFalse(any(i.code == "placeholder_leftover" for i in result.needs_repair))

    def test_cramped_numbered_list_repaired_via_mocked_llm(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.sections["PLAN"] = "1. Poursuite du suivi. 2. Contrôle dans 3 mois."
        fake = llm.Completion(
            text='{"new_value": "1. Poursuite du suivi.\\n2. Contrôle dans 3 mois."}',
            model="fake", provider="fake",
        )
        with mock.patch.object(llm, "complete", return_value=fake) as complete_mock:
            result = validate_and_repair(note, layout, TRANSCRIPT_FR, model="fake-model")
        complete_mock.assert_called_once()
        self.assertIn("\n", note.sections["PLAN"])
        self.assertFalse(any(i.code == "cramped_numbered_list" for i in result.needs_repair))

    def test_grounding_mismatch_falls_back_without_calling_llm(self):
        layout = parse_layout(GENERAL_LAYOUT)
        note = _base_note()
        note.grounded_fields = [GroundedField(field="dose_x", value="500 mg", source_span="absent de la dictée")]
        with mock.patch.object(llm, "complete", side_effect=AssertionError("ne doit jamais être appelé")):
            result = validate_and_repair(note, layout, TRANSCRIPT_FR, model="fake-model")
        self.assertTrue(any(e.terme_dicte == "dose_x" for e in note.elements_a_valider))
        self.assertFalse(any(i.code == "grounding_mismatch" for i in result.needs_repair))


_MINIMAL_FINAL_JSON = json.dumps({
    "header_fields": {},
    "sections": {"RAISON DE CONSULTATION": "Suivi de mémoire."},
    "elements_a_valider": [],
    "grounded_fields": [],
})


def _tool_call_completion(*terms: str, kind: str = "marque", call_id: str = "call1") -> llm.ToolCompletion:
    """Un seul tool_call listant TOUS les ``terms`` en un seul appel batché
    (voir note_extraction._DPD_TOOL_SCHEMA, Option A du banc d'essai)."""
    medicaments = [{"terme": t, "type": kind} for t in terms]
    args = json.dumps({"medicaments": medicaments})
    return llm.ToolCompletion(
        text="",
        tool_calls=[llm.ToolCall(id=call_id, name="verifier_medicaments_dpd", arguments_raw=args)],
        raw_message={"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "verifier_medicaments_dpd", "arguments": args},
        }]},
        model="fake", provider="mistral",
        usage={"prompt_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    )


def _final_completion(text: str = _MINIMAL_FINAL_JSON) -> llm.ToolCompletion:
    return llm.ToolCompletion(
        text=text, tool_calls=[], raw_message={"role": "assistant", "content": text},
        model="fake", provider="mistral",
        usage={"prompt_tokens": 150, "output_tokens": 80, "total_tokens": 230},
    )


class DpdToolTests(unittest.TestCase):
    """Vérification de médicament par appel d'outil (branche selfhosted,
    expérimental — réglage note_lookup_dpd) : voir
    note_extraction._extract_note_with_dpd_tool, app.drug_lookup."""

    def _dpd_flag_on(self, key):
        return "true" if key == "note_lookup_dpd" else ""

    def test_regression_no_tools_when_flag_off(self):
        """Le chemin existant ne doit RIEN changer quand le réglage est
        désactivé — même fournisseur Mistral."""
        fake = llm.Completion(text=_MINIMAL_FINAL_JSON, model="fake", provider="mistral")
        with mock.patch.object(runtime_config, "value", return_value="false"), \
             mock.patch.object(llm, "complete", return_value=fake) as complete_mock, \
             mock.patch.object(llm, "complete_with_tools", side_effect=AssertionError("ne doit jamais être appelé")):
            extract_note(TRANSCRIPT_FR, parse_layout(GENERAL_LAYOUT), "", "", model="fake-model", provider="mistral")
        complete_mock.assert_called_once()

    def test_usage_reported_on_plain_path_too(self):
        """Même régression que test_usage_accumulates_across_tool_rounds,
        mais pour le chemin SANS outil (réglage désactivé) — extract_note
        doit rapporter l'usage dans les deux cas."""
        fake = llm.Completion(
            text=_MINIMAL_FINAL_JSON, model="fake", provider="mistral",
            usage={"prompt_tokens": 300, "output_tokens": 50, "total_tokens": 350},
        )
        with mock.patch.object(runtime_config, "value", return_value="false"), \
             mock.patch.object(llm, "complete", return_value=fake):
            usage: dict = {}
            extract_note(TRANSCRIPT_FR, parse_layout(GENERAL_LAYOUT), "", "", model="fake-model", provider="mistral", usage_out=usage)
        self.assertEqual(usage, {"prompt_tokens": 300, "output_tokens": 50, "total_tokens": 350})

    def test_regression_no_tools_for_non_mistral_provider(self):
        """Même réglage activé, un fournisseur autre que Mistral doit
        continuer d'utiliser complete() — aucune infrastructure d'appel
        d'outils n'existe pour les autres fournisseurs."""
        fake = llm.Completion(text=_MINIMAL_FINAL_JSON, model="fake", provider="gemini")
        with mock.patch.object(runtime_config, "value", side_effect=self._dpd_flag_on), \
             mock.patch.object(llm, "complete", return_value=fake) as complete_mock, \
             mock.patch.object(llm, "complete_with_tools", side_effect=AssertionError("ne doit jamais être appelé")):
            extract_note(TRANSCRIPT_FR, parse_layout(GENERAL_LAYOUT), "", "", model="fake-model", provider="gemini")
        complete_mock.assert_called_once()

    def test_tool_call_executed_and_recorded(self):
        lookup = DrugLookup(term="Respirone", found=True, matched_name="RISPERDAL", din="00123456")
        with mock.patch.object(runtime_config, "value", side_effect=self._dpd_flag_on), \
             mock.patch.object(llm, "complete_with_tools", side_effect=[_tool_call_completion("Respirone"), _final_completion()]) as tools_mock, \
             mock.patch.object(note_extraction, "search_drug", return_value=lookup) as search_mock:
            note = extract_note(TRANSCRIPT_FR, parse_layout(GENERAL_LAYOUT), "", "", model="fake-model", provider="mistral")
        self.assertEqual(tools_mock.call_count, 2)
        search_mock.assert_called_once_with("Respirone", kind="brand", language="fr")
        self.assertEqual(note.drug_lookups, [lookup])
        # Le résultat de l'outil doit être renvoyé, apparié au bon tool_call_id.
        second_call_messages = tools_mock.call_args_list[1].kwargs["messages"]
        tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["tool_call_id"], "call1")

    def test_batched_call_covers_multiple_medications_in_one_round(self):
        """Option A du banc d'essai coût : le modèle doit pouvoir lister
        PLUSIEURS médicaments dans UN seul tool_call plutôt qu'un appel par
        médicament — c'est ce qui évite de réémettre toute la consigne une
        fois par médicament (voir note_extraction._DPD_TOOL_SCHEMA)."""
        lookups = {
            "Respirone": DrugLookup(term="Respirone", found=True, matched_name="RISPERDAL"),
            "Norvask": DrugLookup(term="Norvask", found=True, matched_name="NORVASC", source="dpd_fuzzy"),
        }
        with mock.patch.object(runtime_config, "value", side_effect=self._dpd_flag_on), \
             mock.patch.object(llm, "complete_with_tools", side_effect=[_tool_call_completion("Respirone", "Norvask"), _final_completion()]) as tools_mock, \
             mock.patch.object(note_extraction, "search_drug", side_effect=lambda terme, **kw: lookups[terme]) as search_mock:
            note = extract_note(TRANSCRIPT_FR, parse_layout(GENERAL_LAYOUT), "", "", model="fake-model", provider="mistral")
        self.assertEqual(tools_mock.call_count, 2)  # un seul aller-retour d'outil, pas un par médicament
        self.assertEqual(search_mock.call_count, 2)
        self.assertEqual(
            sorted(note.drug_lookups, key=lambda dl: dl.term),
            sorted(lookups.values(), key=lambda dl: dl.term),
        )
        second_call_messages = tools_mock.call_args_list[1].kwargs["messages"]
        tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)  # une seule réponse d'outil pour les deux médicaments
        resultats = json.loads(tool_messages[0]["content"])["resultats"]
        self.assertEqual({r["terme"] for r in resultats}, {"Respirone", "Norvask"})

    def test_weak_fuzzy_match_reported_as_low_confidence_to_model(self):
        """Régression réelle (consultation #9) : un match flou modéré
        (source="dpd_fuzzy_weak", voir DrugLookupTests) doit être signalé
        au modèle comme confiance "faible" — jamais comme "elevee", qui
        l'inviterait à écrire une correction confirmée pour un candidat
        aussi peu fiable que 'Respirone' → 'REPRONEX'."""
        lookup = DrugLookup(term="Respirone", found=True, matched_name="REPRONEX", source="dpd_fuzzy_weak")
        with mock.patch.object(runtime_config, "value", side_effect=self._dpd_flag_on), \
             mock.patch.object(llm, "complete_with_tools", side_effect=[_tool_call_completion("Respirone"), _final_completion()]) as tools_mock, \
             mock.patch.object(note_extraction, "search_drug", return_value=lookup):
            extract_note(TRANSCRIPT_FR, parse_layout(GENERAL_LAYOUT), "", "", model="fake-model", provider="mistral")
        second_call_messages = tools_mock.call_args_list[1].kwargs["messages"]
        tool_message = next(m for m in second_call_messages if m.get("role") == "tool")
        resultat = json.loads(tool_message["content"])["resultats"][0]
        self.assertEqual(resultat["confiance"], "faible")

    def test_usage_accumulates_across_tool_rounds(self):
        """Régression réelle (test.dictai.ca 2026-08-18) : la page
        statistiques ne montrait aucun jeton pour les générations du
        pipeline JSON — extract_note ne rapportait jamais l'usage du(des)
        appel(s) modèle à l'appelant. usage_out doit sommer les jetons de
        CHAQUE tour d'appel d'outils, pas seulement le dernier."""
        with mock.patch.object(runtime_config, "value", side_effect=self._dpd_flag_on), \
             mock.patch.object(llm, "complete_with_tools", side_effect=[_tool_call_completion("Respirone"), _final_completion()]), \
             mock.patch.object(note_extraction, "search_drug", return_value=DrugLookup(term="Respirone", found=True)):
            usage: dict = {}
            extract_note(TRANSCRIPT_FR, parse_layout(GENERAL_LAYOUT), "", "", model="fake-model", provider="mistral", usage_out=usage)
        self.assertEqual(usage, {"prompt_tokens": 250, "output_tokens": 100, "total_tokens": 350})

    def test_from_dict_never_reads_drug_lookups_from_model(self):
        """Le modèle ne peut jamais s'auto-déclarer « vérifié » — seule
        l'orchestration (note_extraction) peut peupler ce champ."""
        note = ExtractedNote.from_dict({
            "header_fields": {}, "sections": {}, "elements_a_valider": [],
            "grounded_fields": [],
            "drug_lookups": [{"term": "x", "found": True, "matched_name": "FAKE"}],
        })
        self.assertEqual(note.drug_lookups, [])

    def test_malformed_tool_arguments_skipped_gracefully(self):
        bad_call = llm.ToolCompletion(
            text="", tool_calls=[llm.ToolCall(id="bad", name="verifier_medicament_dpd", arguments_raw="not json")],
            raw_message={"role": "assistant", "content": ""}, model="fake", provider="mistral",
        )
        with mock.patch.object(runtime_config, "value", side_effect=self._dpd_flag_on), \
             mock.patch.object(llm, "complete_with_tools", side_effect=[bad_call, _final_completion()]), \
             mock.patch.object(note_extraction, "search_drug", side_effect=AssertionError("ne doit jamais être appelé")):
            note = extract_note(TRANSCRIPT_FR, parse_layout(GENERAL_LAYOUT), "", "", model="fake-model", provider="mistral")
        self.assertEqual(note.drug_lookups, [])
        self.assertEqual(note.sections.get("RAISON DE CONSULTATION"), "Suivi de mémoire.")

    def test_round_budget_exhausted_falls_back_to_plain_call(self):
        """Le modèle qui n'arrête jamais d'appeler l'outil ne doit pas faire
        boucler l'extraction indéfiniment — un dernier tour SANS outil force
        une réponse finale."""
        always_tool_call = _tool_call_completion("Respirone")
        with mock.patch.object(runtime_config, "value", side_effect=self._dpd_flag_on), \
             mock.patch.object(
                 llm, "complete_with_tools",
                 side_effect=[always_tool_call, always_tool_call, _final_completion()],
             ) as tools_mock, \
             mock.patch.object(note_extraction, "search_drug", return_value=DrugLookup(term="Respirone", found=False)):
            extract_note(TRANSCRIPT_FR, parse_layout(GENERAL_LAYOUT), "", "", model="fake-model", provider="mistral")
        self.assertEqual(tools_mock.call_count, 3)  # 2 tours (plafond) + 1 repli sans outil
        self.assertIsNone(tools_mock.call_args_list[-1].kwargs["tools"])

    def test_complete_with_tools_accepts_custom_provider(self):
        """Le point de terminaison personnalisé compatible OpenAI (DeepSeek
        via OpenRouter, serveur local…) route vers _complete_openai_tools —
        pas de refus comme pour un fournisseur sans appel d'outils."""
        sentinel = llm.ToolCompletion(
            text="", tool_calls=[], raw_message={"role": "assistant", "content": ""},
            model="m", provider="custom",
        )
        with mock.patch.object(llm, "_complete_openai_tools", return_value=sentinel) as tools_mock:
            result = llm.complete_with_tools(
                "sys", "user", model="m", temperature=0.1, max_tokens=100,
                tools=[{"type": "function", "function": {"name": "verifier_medicament_dpd", "parameters": {"type": "object", "properties": {}}}}], messages=None, json_mode=True, provider="custom",
            )
        self.assertIs(result, sentinel)
        self.assertEqual(tools_mock.call_args.kwargs["provider"], "custom")
        self.assertEqual(tools_mock.call_args[0][5][0]["function"]["name"], "verifier_medicament_dpd")

    def test_complete_with_tools_still_refuses_unsupported_provider(self):
        with self.assertRaises(llm.GenerationError):
            llm.complete_with_tools(
                "s", "u", model="m", temperature=0.1, max_tokens=100,
                tools=None, messages=None, json_mode=True, provider="cohere",
            )

    def test_custom_provider_enters_dpd_tool_loop(self):
        """Avec note_lookup_dpd activé, le fournisseur `custom` doit passer
        par l'appel d'outils (comme Mistral), pas par complete() simple."""
        lookup = DrugLookup(term="Activant", found=True, matched_name="ATIVAN", din="02041413", source="dpd_fuzzy")
        with mock.patch.object(runtime_config, "value", side_effect=self._dpd_flag_on), \
             mock.patch.object(llm, "complete_with_tools", side_effect=[_tool_call_completion("Activant"), _final_completion()]) as tools_mock, \
             mock.patch.object(note_extraction, "search_drug", return_value=lookup):
            note = extract_note(TRANSCRIPT_FR, parse_layout(GENERAL_LAYOUT), "", "", model="fake-model", provider="custom")
        self.assertEqual(tools_mock.call_count, 2)
        self.assertEqual(note.drug_lookups, [lookup])
        self.assertEqual(tools_mock.call_args_list[0].kwargs["provider"], "custom")
        self.assertEqual(tools_mock.call_args_list[0].kwargs["tools"][0]["function"]["name"], "verifier_medicaments_dpd")


class PostExtractionVerificationTests(unittest.TestCase):
    """Option B du banc d'essai (voir plan de session) : vérification
    médicament SANS appel d'outil, en code pur, à partir de
    grounded_fields[*].kind == "medication"."""

    def setUp(self):
        drug_lookup._cache.clear()

    def test_only_medication_kind_fields_are_looked_up(self):
        note = ExtractedNote(grounded_fields=[
            GroundedField(field="med1", value="Norvasc", source_span="Norvask 10", kind="medication"),
            GroundedField(field="dose1", value="10 mg", source_span="10 mg", kind="dose"),
            GroundedField(field="name1", value="Georges Carrière", source_span="Georges Carrière", kind="name"),
        ])
        with mock.patch.object(note_extraction, "search_drug", return_value=DrugLookup(term="Norvasc", found=True)) as search_mock:
            verify_medications_post_extraction(note, "fr")
        search_mock.assert_called_once_with("Norvasc", kind="brand", language="fr")
        self.assertEqual(len(note.drug_lookups), 1)

    def test_dose_stripped_when_model_still_combines_name_and_dose(self):
        """Régression réelle (banc d'essai, consultation #9) : malgré la
        consigne demandant le nom seul pour kind="medication", le modèle a
        mis « Norvask 10 mg PO die » en entier dans value — recherche
        inutile puisqu'aucune BDPP ne connaît cette chaîne complète. Le
        nom doit être isolé AVANT l'appel, pas laissé tel quel."""
        note = ExtractedNote(grounded_fields=[
            GroundedField(field="med1", value="Norvask 10 mg PO die", source_span="Norvask 10", kind="medication"),
        ])
        with mock.patch.object(note_extraction, "search_drug", return_value=DrugLookup(term="Norvask", found=True)) as search_mock:
            verify_medications_post_extraction(note, "fr")
        search_mock.assert_called_once_with("Norvask", kind="brand", language="fr")

    def test_no_medication_kind_fields_is_a_noop(self):
        note = ExtractedNote(grounded_fields=[
            GroundedField(field="date1", value="2026-08-18", source_span="18 août 2026", kind="date"),
        ])
        with mock.patch.object(note_extraction, "search_drug", side_effect=AssertionError("ne doit jamais être appelé")):
            verify_medications_post_extraction(note, "fr")
        self.assertEqual(note.drug_lookups, [])


class PhoneticLayerTests(unittest.TestCase):
    """Couche phonétique française du repli flou (app/drug_lookup.py) — la
    tâche rapproche deux PRONONCIATIONS, pas deux orthographes : le STT se
    trompe par homophonie (« Norvask », « Ensoprazole »), et la similarité
    de caractères seule échoue sur ces confusions (voir README § 13)."""

    def setUp(self):
        drug_lookup._cache.clear()
        drug_lookup._phonetic_key.cache_clear()

    def _list(self, *names):
        """Index factice au format produit par _dedupe_by_name (nom +
        ``_normalized_name`` + ``_phonetic`` précalculés)."""
        return [
            {"brand_name": n, "drug_identification_number": f"{i:08d}",
             "_normalized_name": drug_lookup._normalize_text(n),
             "_phonetic": drug_lookup._phonetic_key(n)}
            for i, n in enumerate(names)
        ]

    def test_phonetic_key_french_substitutions(self):
        """La clé Soundex FR égalise exactement les paires phonétiques du
        corpus réel — c'est le signal que les caractères seuls ne donnent pas."""
        self.assertEqual(drug_lookup._phonetic_key("Norvask"), drug_lookup._phonetic_key("NORVASC"))
        self.assertEqual(drug_lookup._phonetic_key("Monochore"), drug_lookup._phonetic_key("MONOCOR"))
        self.assertEqual(drug_lookup._phonetic_key(""), "")
        # Accents/gestion orthographique gérée par la lib, jamais d'exception.
        self.assertIsInstance(drug_lookup._phonetic_key("Ésoméprazole"), str)
        self.assertTrue(drug_lookup._phonetic_key("Ésoméprazole"))

    def test_phonetic_tiebreaker_decides_ensoprazole(self):
        """Régression réelle (consultation #9) : « Ensoprazole » obtient le
        MÊME ratio de caractères (0,870) contre LANSOPRAZOLE et ESOMEPRAZOLE
        — l'ancien code gardait la première entrée de l'index (arbitraire :
        ici ESOMEPRAZOLE, rangée avant, gagnait). Le tie-breaker phonétique
        (0,952 vs 0,762) départe et choisit LANSOPRAZOLE, quels que soient
        l'ordre de l'index."""
        with mock.patch.object(drug_lookup, "_dpd_query", return_value=[]), \
             mock.patch.object(drug_lookup, "_load_local_index",
                               return_value=self._list("ESOMEPRAZOLE", "LANSOPRAZOLE")):
            result = drug_lookup.search_drug("Ensoprazole")
        self.assertTrue(result.found)
        self.assertEqual(result.matched_name, "LANSOPRAZOLE")
        self.assertEqual(result.source, "dpd_fuzzy")

    def test_phonetic_never_upgrades_respirone_to_strong(self):
        """Régression réelle (consultation #9) : « Respirone » reste
        correctement « faible » — REPRONEX (un médicament de fertilité sans
        rapport) domine DANS LES DEUX métriques (caractères ET phonétique),
        donc ni l'une ni l'autre ne peut trancher avec confiance : le palier
        faible interdit au modèle d'écrire « correction apportée » (voir
        note_extraction : source="dpd_fuzzy_weak" -> confiance "faible")."""
        with mock.patch.object(drug_lookup, "_dpd_query", return_value=[]), \
             mock.patch.object(drug_lookup, "_load_local_index",
                               return_value=self._list("RISPERIDONE", "REPRONEX")):
            result = drug_lookup.search_drug("Respirone")
        self.assertTrue(result.found)
        self.assertEqual(result.matched_name, "REPRONEX")
        self.assertEqual(result.source, "dpd_fuzzy_weak")

    def test_phonetic_rescue_finds_weak_candidate_only(self):
        """Rampe de retrouvaille : aucun cas naturel n'a été trouvé dans le
        corpus (caractères et phonétique restent corrélés sur des vrais
        noms), donc index ARTIFICIEL — un nom faible en caractères (0,714)
        dont la clé phonétique égale celle du terme (ratio 1,0). La règle
        doit le rendre TROUVABLE en « faible », jamais en « élevé »."""
        row = {
            "brand_name": "ACTIVEL", "drug_identification_number": "00000042",
            "_normalized_name": drug_lookup._normalize_text("ACTIVEL"),
            "_phonetic": drug_lookup._phonetic_key("Actimex"),
        }
        with mock.patch.object(drug_lookup, "_dpd_query", return_value=[]), \
             mock.patch.object(drug_lookup, "_load_local_index", return_value=[row]):
            result = drug_lookup.search_drug("Actimex")
        self.assertTrue(result.found)
        self.assertEqual(result.matched_name, "ACTIVEL")
        self.assertEqual(result.source, "dpd_fuzzy_weak")


class DrugLookupTests(unittest.TestCase):
    """app.drug_lookup.search_drug ne doit jamais lever — voir son
    docstring. Vide le cache du module entre les tests, sinon un terme déjà
    recherché dans un test précédent renvoie un résultat mis en cache."""

    def setUp(self):
        drug_lookup._cache.clear()

    def test_network_error_never_raises(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = drug_lookup.search_drug("termeinexistant1")
        self.assertFalse(result.found)
        self.assertTrue(result.error)

    def test_http_error_never_raises(self):
        exc = urllib.error.HTTPError("url", 500, "erreur serveur", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            result = drug_lookup.search_drug("termeinexistant2")
        self.assertFalse(result.found)
        self.assertIn("500", result.error)

    def test_empty_result_is_not_found_without_error(self):
        response = mock.MagicMock()
        response.read.return_value = b"[]"
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response):
            result = drug_lookup.search_drug("termeinexistant3")
        self.assertFalse(result.found)
        self.assertEqual(result.error, "")

    def test_happy_path_extracts_match(self):
        payload = json.dumps([{"brand_name": "RISPERDAL", "drug_identification_number": "00123456"}]).encode()
        response = mock.MagicMock()
        response.read.return_value = payload
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response):
            result = drug_lookup.search_drug("Respirone", kind="brand")
        self.assertTrue(result.found)
        self.assertEqual(result.matched_name, "RISPERDAL")
        self.assertEqual(result.din, "00123456")
        self.assertEqual(result.source, "dpd")

    def _mock_local_index(self, *brand_names):
        """L'index flou en mémoire, comme le produirait _dedupe_by_name —
        court-circuite le téléchargement/cache disque pour ces tests."""
        rows = [{"brand_name": n, "drug_identification_number": f"{i:08d}",
                  "_normalized_name": drug_lookup._normalize_text(n)}
                for i, n in enumerate(brand_names)]
        return mock.patch.object(drug_lookup, "_load_local_index", return_value=rows)

    def test_fuzzy_fallback_resolves_norvask_to_norvasc(self):
        """Régression réelle (test.dictai.ca 2026-08-18, consultation #9) :
        confirmé contre l'API réelle que « Norvask » (k) ne retrouve rien
        pour NORVASC (c) — la recherche BDPP est un filtre préfixe, pas une
        correspondance floue. Le repli flou compare contre l'EXTRAIT LOCAL
        COMPLET, pas seulement des préfixes partagés."""
        with mock.patch("urllib.request.urlopen", return_value=self._empty_response()), \
             self._mock_local_index("NORVASC"):
            result = drug_lookup.search_drug("Norvask", kind="brand", language="en")
        self.assertTrue(result.found)
        self.assertEqual(result.matched_name, "NORVASC")
        self.assertEqual(result.source, "dpd_fuzzy")

    def test_fuzzy_fallback_finds_match_with_no_shared_prefix(self):
        """Régression réelle (consultation #9) : « Activant » (probablement
        Ativan/lorazépam) et « Ativan » divergent dès la 2ᵉ lettre — aucun
        préfixe de l'un n'est un préfixe de l'autre, donc une approche par
        préfixe ne peut JAMAIS proposer Ativan comme candidat, même si leur
        similarité (≈0,86) est largement au-dessus du seuil. L'extrait
        local complet, comparé terme à terme, le retrouve."""
        with mock.patch("urllib.request.urlopen", return_value=self._empty_response()), \
             self._mock_local_index("ATIVAN", "ACTIFED", "ACTIVATED CHARCOAL"):
            result = drug_lookup.search_drug("Activant", kind="brand", language="fr")
        self.assertTrue(result.found)
        self.assertEqual(result.matched_name, "ATIVAN")
        self.assertEqual(result.source, "dpd_fuzzy")

    def test_fuzzy_fallback_marks_moderate_match_as_weak_confidence(self):
        """Régression réelle (consultation #9) : « Respirone » a été
        rapproché de « REPRONEX » (un médicament de fertilité SANS rapport,
        ratio ≈ 0,824) et le modèle l'a présenté comme une correction
        CONFIRMÉE, alors que la vraie réponse (rispéridone) n'était même pas
        le meilleur candidat par similarité pure de caractères. Un tel match
        (au-dessus du seuil minimal mais sous le seuil de confiance) doit
        rester trouvable, mais marqué source="dpd_fuzzy_weak" plutôt que
        "dpd_fuzzy" — voir note_extraction, qui interdit au modèle de le
        présenter comme une correction confirmée."""
        with mock.patch("urllib.request.urlopen", return_value=self._empty_response()), \
             self._mock_local_index("REPRONEX"):
            result = drug_lookup.search_drug("Respirone", kind="brand")
        self.assertTrue(result.found)
        self.assertEqual(result.matched_name, "REPRONEX")
        self.assertEqual(result.source, "dpd_fuzzy_weak")

    def test_fuzzy_fallback_rejects_dissimilar_candidate(self):
        """Le meilleur candidat de l'extrait local, s'il reste trop
        dissemblable (ratio de similarité trop bas), ne doit jamais être
        accepté comme une correction plausible."""
        with mock.patch("urllib.request.urlopen", return_value=self._empty_response()), \
             self._mock_local_index("COMPLETELYUNRELATED"):
            result = drug_lookup.search_drug("Xyzqwerty", kind="brand")
        self.assertFalse(result.found)

    def test_fuzzy_fallback_degrades_gracefully_without_local_index(self):
        """Aucun extrait local disponible (jamais téléchargé avec succès) :
        repli sur found=False, jamais une exception."""
        with mock.patch("urllib.request.urlopen", return_value=self._empty_response()), \
             mock.patch.object(drug_lookup, "_load_local_index", return_value=[]):
            result = drug_lookup.search_drug("Norvask", kind="brand")
        self.assertFalse(result.found)
        self.assertEqual(result.error, "")

    @staticmethod
    def _empty_response():
        response = mock.MagicMock()
        response.read.return_value = b"[]"
        response.__enter__.return_value = response
        return response


class DrugLookupLocalIndexTests(unittest.TestCase):
    """app.drug_lookup._load_local_index : cache disque + téléchargement de
    l'extrait complet, jamais dans le chemin d'une recherche exacte réussie."""

    def setUp(self):
        drug_lookup._local_index.clear()

    def test_download_failure_falls_back_to_empty_index(self):
        with mock.patch.object(drug_lookup, "_fetch_full_dataset", side_effect=OSError("timeout")), \
             mock.patch("os.path.exists", return_value=False):
            index = drug_lookup._load_local_index("brand", "fr")
        self.assertEqual(index, [])

    def test_dedupe_by_normalized_name(self):
        rows = [
            {"brand_name": "NORVASC", "drug_identification_number": "1"},
            {"brand_name": "norvasc", "drug_identification_number": "2"},  # doublon (casse)
            {"brand_name": "CRESTOR", "drug_identification_number": "3"},
        ]
        deduped = drug_lookup._dedupe_by_name(rows)
        self.assertEqual(len(deduped), 2)


class LegacySourceTests(unittest.TestCase):
    """Source historique RxNorm (marques retirées/internationales, voir
    app/drug_lookup.legacy_match) — index LOCAL téléchargé une fois, aucun
    envoi runtime vers les USA. Une marque retrouvée ici reste TOUJOURS
    « faible » (source="rxnorm", jamais une correction apportée)."""

    def setUp(self):
        drug_lookup._cache.clear()
        drug_lookup._phonetic_key.cache_clear()
        drug_lookup._legacy_index = None

    def _rows(self, *names):
        return [{"name": n, "_normalized_name": drug_lookup._normalize_text(n),
                 "_phonetic": drug_lookup._phonetic_key(n)} for n in names]

    def test_legacy_match_finds_removed_brand_lopressor(self):
        """« oppressor » (élision de LOPRESSOR) — la BDPP ne connaît pas la
        marque retirée ; l'index RxNorm la retrouve, source="rxnorm"."""
        with mock.patch.object(drug_lookup, "_load_legacy_index", return_value=self._rows("Lopressor", "Losartan")):
            result = drug_lookup.legacy_match("oppressor")
        self.assertIsNotNone(result)
        self.assertEqual(result.matched_name, "Lopressor")
        self.assertEqual(result.source, "rxnorm")
        self.assertIsNone(result.din)

    def test_legacy_match_none_below_threshold(self):
        with mock.patch.object(drug_lookup, "_load_legacy_index", return_value=self._rows("COMPLETELYUNRELATED")):
            result = drug_lookup.legacy_match("Xyzqwerty")
        self.assertIsNone(result)

    def test_legacy_match_empty_index_returns_none(self):
        with mock.patch.object(drug_lookup, "_load_legacy_index", return_value=[]):
            self.assertIsNone(drug_lookup.legacy_match("oppressor"))

    def test_maybe_legacy_replaces_weak_dpd_candidate(self):
        """Fusion « choix unique » : BDPP « faible » (suppressor) remplacée
        par la marque historique (Lopressor) quand note_lookup_legacy est ON."""
        weak = DrugLookup(term="oppressor", found=True, matched_name="SUPPRESSOR", source="dpd_fuzzy_weak")
        with mock.patch.object(note_extraction, "search_drug", return_value=weak), \
             mock.patch.object(runtime_config, "value", side_effect=lambda k: "true" if k == "note_lookup_legacy" else ""), \
             mock.patch.object(note_extraction, "legacy_match",
                               return_value=DrugLookup(term="oppressor", found=True, matched_name="Lopressor", source="rxnorm")):
            result = note_extraction._maybe_legacy(weak, "oppressor")
        self.assertEqual(result.matched_name, "Lopressor")
        self.assertEqual(result.source, "rxnorm")

    def test_maybe_legacy_never_replaces_dpd_strong(self):
        strong = DrugLookup(term="Activant", found=True, matched_name="ATIVAN", source="dpd")
        with mock.patch.object(note_extraction, "search_drug", return_value=strong), \
             mock.patch.object(note_extraction, "legacy_match",
                               side_effect=AssertionError("ne doit jamais être appelé")):
            result = note_extraction._maybe_legacy(strong, "Activant")
        self.assertEqual(result.matched_name, "ATIVAN")
        self.assertEqual(result.source, "dpd")

    def test_maybe_legacy_off_keeps_dpd(self):
        weak = DrugLookup(term="oppressor", found=True, matched_name="SUPPRESSOR", source="dpd_fuzzy_weak")
        with mock.patch.object(runtime_config, "value", side_effect=lambda k: "false" if k == "note_lookup_legacy" else ""), \
             mock.patch.object(note_extraction, "legacy_match",
                               side_effect=AssertionError("ne doit jamais être appelé")):
            result = note_extraction._maybe_legacy(weak, "oppressor")
        self.assertEqual(result.matched_name, "SUPPRESSOR")
        self.assertEqual(result.source, "dpd_fuzzy_weak")


class MedicationOmittedFromListTests(unittest.TestCase):
    """Régression réelle (test.dictai.ca, consultation #10, 2026-08-18) : la
    rispéridone était initiée en HMA et titrée en Plan (« j'augmente la
    rispéridone à 0,60 ») mais n'a jamais été ajoutée à MÉDICATION ACTUELLE —
    seule une phrase de liste explicite y avait été reprise. Voir
    note_validator.check_medication_omitted_from_list."""

    def test_verified_drug_absent_from_list_is_flagged(self):
        note = _base_note()
        note.drug_lookups = [
            DrugLookup(term="Rispiridone", found=True, matched_name="RISPERIDONE", source="dpd_fuzzy"),
        ]
        issues = check_medication_omitted_from_list(note)
        self.assertTrue(any(i.code == "medication_missing_from_list" for i in issues))

    def test_verified_drug_present_in_list_is_not_flagged(self):
        note = _base_note()
        note.sections["MÉDICATION ACTUELLE"] = {OWN_CONTENT_KEY: "Rispéridone 0,60 mg PO HS"}
        note.drug_lookups = [
            DrugLookup(term="Rispiridone", found=True, matched_name="RISPERIDONE", source="dpd_fuzzy"),
        ]
        issues = check_medication_omitted_from_list(note)
        self.assertFalse(any(i.code == "medication_missing_from_list" for i in issues))

    def test_matched_name_present_even_if_literal_term_differs(self):
        note = _base_note()
        note.sections["MÉDICATION ACTUELLE"] = {OWN_CONTENT_KEY: "RISPERIDONE 0,60 mg PO HS"}
        note.drug_lookups = [
            DrugLookup(term="Respirone", found=True, matched_name="RISPERIDONE", source="dpd_fuzzy_weak"),
        ]
        issues = check_medication_omitted_from_list(note)
        self.assertFalse(any(i.code == "medication_missing_from_list" for i in issues))

    def test_no_medication_section_never_raises(self):
        note = _base_note()
        del note.sections["MÉDICATION ACTUELLE"]
        note.drug_lookups = [DrugLookup(term="Rispiridone", found=True, matched_name="RISPERIDONE", source="dpd_fuzzy")]
        issues = check_medication_omitted_from_list(note)
        self.assertEqual(issues, [])

    def test_english_section_key_also_checked(self):
        note = _base_note()
        del note.sections["MÉDICATION ACTUELLE"]
        note.sections["CURRENT MEDICATIONS"] = {OWN_CONTENT_KEY: "Metformin 500 mg PO BID"}
        note.drug_lookups = [DrugLookup(term="Risperidone", found=True, matched_name="RISPERIDONE", source="dpd")]
        issues = check_medication_omitted_from_list(note)
        self.assertTrue(any(i.code == "medication_missing_from_list" for i in issues))


if __name__ == "__main__":
    unittest.main()