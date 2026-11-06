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

    def test_entree_non_couverte_refusee(self):
        """Le mode d'échec qui compte : une page oubliée en silence."""
        with self.assertRaises(extraction.ExtractionError) as ctx:
            extraction._normalize(_payload(), 2)
        self.assertIn("2", str(ctx.exception))

    def test_entree_illisible_compte_comme_couverte(self):
        charge = _payload(unreadable=[{"input": 2, "reason": "page tournée"}])
        resultat = extraction._normalize(charge, 2)
        self.assertEqual(resultat["unreadable"], [{"input": 2, "reason": "page tournée"}])

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

    def test_ligne_medicament_quatre_emplacements(self):
        """Les deux exemples demandés, produits par le normaliseur."""
        champs = {
            "short_name": "PEG 3350",
            "dose": "",
            "route": "",
            "frequency": "",
            "strength": "100%",
            "directions": "DISSOUDRE 17 GRAMMES DANS PLUS OU MOINS 250 ML DE "
                          "LIQUIDE ET BOIRE 1 FOIS PAR JOUR",
        }
        ligne, ok = extraction._ligne_medicament(champs)
        self.assertEqual(ligne, "PEG 3350 17g po die")
        self.assertTrue(ok)

        champs = {
            "short_name": "Acétaminophène",
            "directions": "PRENDRE 1 COMPRIMÉ AUX 6 HEURES SI BESOIN",
            "dose": "650mg",
        }
        ligne, ok = extraction._ligne_medicament(champs)
        self.assertEqual(ligne, "Acétaminophène 650mg po q6h prn")
        self.assertTrue(ok)

    def test_frequences_et_voies(self):
        cas = [
            ("1 fois par jour", "die"), ("deux fois par jour", "bid"),
            ("3 fois par jour", "tid"), ("4 fois par jour", "qid"),
            ("au coucher", "hs"), ("1 fois par semaine", "qsem"),
            ("aux 6 heures", "q6h"), ("aux 4 à 6 heures", "q4-6h"),
        ]
        for phrase, attendu in cas:
            self.assertEqual(extraction._frequence(phrase), attendu, phrase)
        # « si besoin » est un MODE, pas une fréquence : il se combine.
        self.assertTrue(extraction._prn("si besoin"))
        self.assertTrue(extraction._prn("aux 6 heures au besoin"))
        self.assertEqual(extraction._voie("par voie orale"), "po")
        self.assertEqual(extraction._voie("sous-cutanée"), "sc")
        self.assertEqual(extraction._voie("inhalée"), "inh")

    def test_dose_ignore_le_volume_de_dilution(self):
        self.assertEqual(
            extraction._dose("dissoudre 17 grammes dans 250 ml de liquide"), "17g"
        )
        self.assertEqual(extraction._dose("prendre 2 comprimés de 500 mg"), "500mg")
        self.assertEqual(extraction._dose("250 ml"), "")

    def test_ligne_hors_moule_signalee(self):
        """Sans dose chiffrée, la ligne n'est pas « normalisée » — et le dit."""
        ligne, ok = extraction._ligne_medicament({"short_name": "Ramipril",
                                                  "directions": "au besoin"})
        self.assertFalse(ok)
        self.assertEqual(ligne, "Ramipril prn")

    def test_molecule_remplace_le_prefixe_fabricant(self):
        charge = _payload(items=[{
            "document_id": "d1",
            "kind": "medications",
            "text": "JAMP-CITALOPRAM 20 mg comp 1 co PO DIE  Cessé le 2026-03-02",
            "fields": {
                "molecule": "CITALOPRAM",
                "strength": "20 mg",
                "directions": "1 co PO DIE",
                "status_printed": "Cessé le 2026-03-02",
            },
            "source_page": 1,
        }])
        item = extraction._normalize(charge, 1)["items"][0]
        self.assertEqual(item["text"], "Citalopram 20mg po die (Cessé le 2026-03-02)")
        self.assertTrue(item["fields"]["as_printed"].startswith("JAMP-CITALOPRAM"))

    def test_bilan_normal_decoché_bilan_marque_inclus(self):
        """La note ne recopie pas un bilan entier : le drapeau IMPRIMÉ décide."""
        charge = _payload(items=[
            # Chlore : hors de la liste du médecin, normal → pas versé.
            {"document_id": "d1", "kind": "labs", "text": "Chlore 101 mmol/L",
             "fields": {"analyte": "Chlore", "flag": None}},
            # Acide urique : hors liste, mais le laboratoire l'a marqué.
            {"document_id": "d1", "kind": "labs", "text": "Acide urique 512",
             "fields": {"analyte": "Acide urique", "flag": "H"}},
            {"document_id": "d1", "kind": "labs", "text": "Bilirubine 22",
             "fields": {"analyte": "Bilirubine", "flag": "↓"}},
        ])
        items = extraction._normalize(charge, 1)["items"]
        self.assertFalse(items[0]["included"])   # disponible, pas versé
        self.assertTrue(items[1]["included"])    # H imprimé = garde-fou
        self.assertTrue(items[2]["included"])    # ↓ imprimé = garde-fou

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
        self.assertFalse(items[0]["included"])   # routine, non versé
        self.assertTrue(items[1]["included"])    # pertinent
        self.assertTrue(items[2]["included"])    # drapeau imprimé = garde-fou

    def test_liste_du_medecin_reconnue_par_abreviation(self):
        """« na », « creat », « vih » doivent reconnaître Sodium, Créatinine, VIH."""
        self.assertTrue(extraction._analytes_retenus("Sodium", "na, k, creat"))
        self.assertTrue(extraction._analytes_retenus("Créatinine", "na, k, creat"))
        self.assertTrue(extraction._analytes_retenus("Créatinine (DFGe)", "creat"))
        self.assertTrue(extraction._analytes_retenus("VIH 1/2", "vih"))
        self.assertTrue(extraction._analytes_retenus("VGM", "vgm"))
        self.assertFalse(extraction._analytes_retenus("TIBC", "na, k, creat"))
        self.assertFalse(extraction._analytes_retenus("Sodium", ""))

    def test_liste_du_medecin_prime_sur_le_jugement(self):
        """Marqué « routine » par le modèle, mais réclamé par le médecin."""
        charge = _payload(items=[
            {"document_id": "d1", "kind": "labs", "text": "Sodium 137 mmol/L",
             "fields": {"analyte": "Sodium", "pertinence": "routine"}},
            {"document_id": "d1", "kind": "labs", "text": "TIBC 62 umol/L",
             "fields": {"analyte": "TIBC", "pertinence": "routine"}},
        ])
        items = extraction._normalize(charge, 1)["items"]
        self.assertTrue(items[0]["included"])   # dans la liste du médecin
        self.assertFalse(items[1]["included"])  # routine, hors liste

    def test_imagerie_conclusion_incluse_corps_decoché(self):
        corps = "Les champs pulmonaires montrent un infiltrat basal droit. " * 3
        charge = _payload(items=[{
            "document_id": "d1", "kind": "imaging",
            "text": "Infiltrat lobaire inférieur droit. Cardiomégalie légère.",
            "fields": {"study": "Radiographie du thorax", "body": corps},
        }])
        items = extraction._normalize(charge, 1)["items"]
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]["included"])    # la conclusion entre
        self.assertFalse(items[1]["included"])   # le corps accompagne
        self.assertEqual(items[1]["text"].strip(), corps.strip())

    def test_observation_decochée_par_defaut(self):
        charge = _payload(observations=[{"text": "Le bilan précède la liste", "pages": [1]}])
        observation = extraction._normalize(charge, 1)["observations"][0]
        self.assertFalse(observation["included"])

    def test_pages_deduites_des_lignes(self):
        """Le récapitulatif du modèle se trompe parfois ; les lignes font foi."""
        charge = _payload(
            documents=[{"id": "d1", "kind": "labs", "pages": [1]}],
            items=[{"document_id": "d1", "kind": "labs", "text": "Créatinine 132",
                    "source_page": 2}],
        )
        self.assertEqual(extraction._normalize(charge, 1)["documents"][0]["pages"], [2])


if __name__ == "__main__":
    unittest.main()
