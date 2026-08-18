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
import unittest
from unittest import mock

from app.default_templates import LOCKED_TEMPLATES
from app import llm
from app.note_extraction import build_expected_json_skeleton, validate_and_repair
from app.note_renderer import OWN_CONTENT_KEY, render
from app.note_schema import ElementAValider, ExtractedNote, GroundedField, parse_layout
from app.note_validator import validate

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


if __name__ == "__main__":
    unittest.main()
