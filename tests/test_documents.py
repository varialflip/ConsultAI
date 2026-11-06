"""
tests/test_documents.py — Documents photographiés (assemblage + couverture).

Ces tests ne touchent PAS le réseau : ils vérifient l'assemblage PDF et la
validation de la réponse du modèle. Ce sont les deux endroits où une erreur
coûte cher — un PDF mal formé, et une page oubliée en silence.

Exécuter dans le conteneur :

    docker cp tests/test_documents.py consultai-test:/tmp/
    docker exec consultai-test python3 /tmp/test_documents.py
"""

from __future__ import annotations

import struct
import unittest

from app import documents, extraction


def _jpeg(width: int, height: int, components: int = 3) -> bytes:
    """JPEG synthétique : SOI + APP0 + SOF0 + SOS + EOI (en-tête suffisant)."""
    app0 = (
        b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00"
        + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    )
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 8 + 3 * components)
        + bytes([8])
        + struct.pack(">HH", height, width)
        + bytes([components])
        + b"".join(bytes([i + 1, 0x11, 0]) for i in range(components))
    )
    sos = b"\xff\xda" + struct.pack(">H", 8) + bytes([1, 1, 0, 0, 63, 0]) + b"\x00" * 32
    return b"\xff\xd8" + app0 + sof + sos + b"\xff\xd9"


def _payload(**extra) -> dict:
    base = {
        "documents": [{"id": "d1", "kind": "medications", "pages": [1]}],
        "items": [{"document_id": "d1", "kind": "medications", "text": "Ramipril 5 mg"}],
        "coverage": [{"input": 1, "document_id": "d1"}],
        "unreadable": [],
        "observations": [],
    }
    base.update(extra)
    return base


class TestAssemblagePdf(unittest.TestCase):
    def test_dimensions_lues_dans_le_sof(self):
        self.assertEqual(documents._jpeg_size(_jpeg(1200, 1600)), (1200, 1600, 3))

    def test_une_page_par_image(self):
        pdf = documents.assemble_pdf([_jpeg(100, 200), _jpeg(300, 400)])
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        self.assertEqual(pdf.count(b"/Type /Page "), 2)
        self.assertIn(b"/Count 2", pdf)
        # Les octets JPEG sont embarqués tels quels, sans ré-encodage.
        self.assertEqual(pdf.count(b"/Filter /DCTDecode"), 2)

    def test_media_box_suit_l_image(self):
        pdf = documents.assemble_pdf([_jpeg(640, 480)])
        self.assertIn(b"/MediaBox [0 0 640 480]", pdf)

    def test_type_reel_prime_sur_le_type_declare(self):
        self.assertEqual(documents.normalize_mime("", _jpeg(10, 10)), "image/jpeg")
        self.assertEqual(
            documents.normalize_mime("application/pdf", b"%PDF-1.4 ..."), "application/pdf"
        )

    def test_svg_refuse(self):
        # Un SVG servi en ligne exécute du script : jamais un document patient.
        self.assertIsNone(documents.normalize_mime("image/svg+xml", b"<svg onload=alert(1)>"))
        self.assertIsNone(documents.normalize_mime("image/png", b"n'importe quoi"))


class TestCouverture(unittest.TestCase):
    def test_couverture_complete_acceptee(self):
        resultat = extraction._normalize(_payload(), 1)
        self.assertEqual(len(resultat["items"]), 1)
        self.assertTrue(resultat["items"][0]["included"])

    def test_page_non_couverte_signalee_sans_bloquer(self):
        """Une page oubliée se voit, mais n'emporte plus toute la lecture."""
        resultat = extraction._normalize(_payload(), 2)
        self.assertEqual(resultat["missing"], [2])
        self.assertTrue(resultat["items"])

    def test_entree_illisible_compte_comme_couverte(self):
        charge = _payload(unreadable=[{"input": 2, "reason": "page tournée"}])
        resultat = extraction._normalize(charge, 2)
        self.assertEqual(resultat["unreadable"], [{"page": 2, "reason": "page tournée"}])

    def test_aucun_document_refuse(self):
        with self.assertRaises(extraction.ExtractionError):
            extraction._normalize(_payload(documents=[]), 1)


class TestNormalisation(unittest.TestCase):
    def test_bloc_non_classe_decoché_par_defaut(self):
        """« other » est exactement là où tombent les en-têtes administratifs."""
        charge = _payload(items=[
            {"document_id": "d1", "kind": "other", "text": "Patient: Jean Tremblay NAS ..."},
            {"document_id": "d1", "kind": "medications", "text": "Ramipril 5 mg"},
        ])
        resultat = extraction._normalize(charge, 1)
        self.assertFalse(resultat["items"][0]["included"])
        self.assertTrue(resultat["items"][1]["included"])

    def test_ligne_du_modele_affichee_telle_quelle(self):
        """Aucune réécriture : la ligne du modèle est celle qui s'affiche."""
        charge = _payload(items=[{
            "document_id": "d1", "kind": "medications",
            "text": "TRAZODONE 50MG COMPRIME",
            "fields": {"line": "Trazodone 25-50mg po hs",
                       "strength": "50MG", "quantity": "0.5-1",
                       "as_printed": "TRAZODONE 50MG COMPRIME"},
        }])
        item = extraction._normalize(charge, 1)["items"][0]
        self.assertEqual(item["text"], "Trazodone 25-50mg po hs")
        self.assertEqual(item["fields"]["as_printed"], "TRAZODONE 50MG COMPRIME")

    def test_meme_medicament_sur_deux_pages_une_seule_fois(self):
        """Un doublon EXACT ne s'affiche qu'une fois ; les pages sont réunies."""
        charge = _payload(items=[
            {"document_id": "d1", "kind": "medications", "text": "Magnésium-gluconate 1g po tid",
             "fields": {"line": "Magnésium-gluconate 1g po tid"}, "source_page": 1},
            {"document_id": "d1", "kind": "medications", "text": "Magnésium-gluconate 1g po tid",
             "fields": {"line": "Magnésium-gluconate 1g po tid"}, "source_page": 2},
        ])
        items = extraction._normalize(charge, 1)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(sorted(items[0]["source_pages"]), [1, 2])

    def test_doublon_qui_differe_est_conserve(self):
        """Deux forces différentes ne sont pas un doublon : on ne tranche pas."""
        charge = _payload(items=[
            {"document_id": "d1", "kind": "medications", "text": "Metformine 500mg po bid",
             "fields": {}, "source_page": 1},
            {"document_id": "d1", "kind": "medications", "text": "Metformine 1000mg po bid",
             "fields": {}, "source_page": 2},
        ])
        self.assertEqual(len(extraction._normalize(charge, 1)["items"]), 2)

    def test_sans_ligne_on_montre_l_imprime(self):
        """Pas de ligne du modèle → la ligne imprimée, jamais une composition."""
        charge = _payload(items=[{
            "document_id": "d1", "kind": "medications",
            "text": "APO-RAMIPRIL 5 mg comp 1 co PO DIE",
            "fields": {"as_printed": "APO-RAMIPRIL 5 mg comp 1 co PO DIE"},
        }])
        item = extraction._normalize(charge, 1)["items"][0]
        self.assertEqual(item["text"], "APO-RAMIPRIL 5 mg comp 1 co PO DIE")
        self.assertNotIn("line_ok", item)

    def test_bilan_routine_decoché_pertinent_inclus(self):
        """Décocher TIBC à chaque fois serait une corvée : le modèle juge."""
        charge = _payload(items=[
            {"document_id": "d1", "kind": "labs", "text": "TIBC 62 umol/L",
             "fields": {"analyte": "TIBC", "pertinence": "routine"}},
            {"document_id": "d1", "kind": "labs", "text": "Hémoglobine 118 g/L",
             "fields": {"analyte": "Hémoglobine", "pertinence": "pertinent"}},
            {"document_id": "d1", "kind": "labs", "text": "Potassium 5,4 mmol/L",
             "fields": {"analyte": "Potassium", "pertinence": "routine",
                        "flag": "H"}},
        ])
        items = extraction._normalize(charge, 1)["items"]
        self.assertFalse(items[0]["included"])
        self.assertTrue(items[1]["included"])
        self.assertTrue(items[2]["included"])

    def test_liste_du_medecin_prime_sur_le_jugement(self):
        charge = _payload(items=[
            {"document_id": "d1", "kind": "labs", "text": "Sodium 137 mmol/L",
             "fields": {"analyte": "Sodium", "pertinence": "routine"}},
            {"document_id": "d1", "kind": "labs", "text": "TIBC 62 umol/L",
             "fields": {"analyte": "TIBC", "pertinence": "routine"}},
        ])
        items = extraction._normalize(charge, 1)["items"]
        self.assertTrue(items[0]["included"])
        self.assertFalse(items[1]["included"])

    def test_liste_du_medecin_reconnue_par_abreviation(self):
        self.assertTrue(extraction._analytes_retenus("Sodium", "na, k, creat"))
        self.assertTrue(extraction._analytes_retenus("Créatinine", "creat"))
        self.assertTrue(extraction._analytes_retenus("VIH 1/2", "vih"))
        self.assertFalse(extraction._analytes_retenus("TIBC", "na, k, creat"))

    def test_imagerie_conclusion_incluse_corps_decoché(self):
        corps = "Les champs pulmonaires montrent un infiltrat basal droit. " * 3
        charge = _payload(items=[{
            "document_id": "d1", "kind": "imaging",
            "text": "Infiltrat lobaire inférieur droit. Cardiomégalie légère.",
            "fields": {"study": "Radiographie du thorax", "body": corps},
        }])
        items = extraction._normalize(charge, 1)["items"]
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]["included"])
        self.assertFalse(items[1]["included"])

    def test_observation_decochée_par_defaut(self):
        charge = _payload(observations=[{"text": "Le bilan précède la liste", "pages": [1]}])
        observation = extraction._normalize(charge, 1)["observations"][0]
        self.assertFalse(observation["included"])

    def test_pages_deduites_des_lignes(self):
        charge = _payload(
            documents=[{"id": "d1", "kind": "labs", "pages": [1]}],
            items=[{"document_id": "d1", "kind": "labs", "text": "Créatinine 132",
                    "source_page": 2}],
        )
        self.assertEqual(extraction._normalize(charge, 1)["documents"][0]["pages"], [2])


if __name__ == "__main__":
    unittest.main()
