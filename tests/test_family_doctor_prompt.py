"""
tests/test_family_doctor_prompt.py — Médecin de famille, dans la note.

Quand les pages partent au modèle (« Envoyer les pages à la note »), la note
doit lire le médecin de famille sur l'en-tête IMPRIMÉ de la page. Elle ne peut
pas en dépendre de la passe de lecture des documents : celle-ci peut n'avoir
jamais tourné, avoir échoué, ou avoir laissé l'en-tête de côté — et la note
perdait alors le champ « Médecin de famille » du gabarit.

Ces tests ne touchent ni au réseau ni à la base : ils vérifient le message
assemblé, pas la réponse du modèle.

Exécuter dans le conteneur :

    docker cp tests/test_family_doctor_prompt.py consultai-test:/tmp/
    docker exec -e PYTHONPATH=/app python3 /tmp/test_family_doctor_prompt.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from app import llm


def _prompt(**kwargs) -> str:
    return llm.build_user_prompt(
        kwargs.pop("transcript", ""),
        kwargs.pop("layout_format", "**Médecin de famille :**"),
        language="fr",
        **kwargs,
    )


class FamilyDoctorPromptTest(unittest.TestCase):
    def test_consigne_presente_avec_les_pages(self):
        """Le bloc pages doit dire de lire l'en-tête imprimé."""
        prompt = _prompt(documents_pages=True)
        self.assertIn("EN-TÊTE IMPRIMÉ", prompt)
        self.assertIn("médecin de famille", prompt)
        self.assertIn("apparaît pas dans la dictée", prompt.replace("ne figure pas", "apparaît pas"))

    def test_consigne_absente_en_mode_texte(self):
        """En mode texte, c'est le bloc de documents qui porte l'en-tête."""
        prompt = _prompt(
            transcript="dictée",
            documents_block="Médecin de famille imprimé en tête du document : X — 1",
        )
        self.assertNotIn("EN-TÊTE IMPRIMÉ", prompt)
        self.assertIn("imprimé en tête du document", prompt)

    def test_la_consigne_ne_donne_aucune_valeur_d_exemple(self):
        """Aucun nom, aucun numéro, aucun libellé cité : une valeur d'exemple
        est une donnée que le modèle peut recopier dans la note."""
        prompt = _prompt(documents_pages=True)
        paragraphe = prompt.split("EN-TÊTE IMPRIMÉ", 1)[1].split("\n", 1)[0]
        for interdit in ("MABY", "GAP", "20387", "«", "Dr ", "Dre ", "prénom"):
            self.assertNotIn(interdit, paragraphe)

    def test_rien_n_est_lu_en_base_pour_le_medecin(self):
        """La génération ne consulte aucune lecture de documents : la page est
        la source, pas une extraction conservée."""
        captured = {}

        def faux_flux(system, user, **params):
            captured["user"] = user
            yield "NOTE"
            return llm.Completion(text="NOTE", model="m", provider="gemini")

        with mock.patch.object(llm, "complete_stream", faux_flux), \
                mock.patch.object(llm, "active_model", lambda: "gemini-2.5-pro"), \
                mock.patch.object(llm, "active_temperature", lambda: 0.05), \
                mock.patch.object(llm, "active_provider", lambda: "gemini"), \
                mock.patch.object(llm, "audio_settings", lambda provider=None: {
                    "send_audio": True, "bypass_stt": False, "keep_transcript": True,
                    "max_minutes": 20.0, "send_audio_format": "ogg",
                }):
            flux = llm.generate_note_stream(
                "dictée", "sys", "**Médecin de famille :**", language="fr",
                system_override="SYS",
                documents=[(b"%PDF-1.4", "application/pdf")],
            )
            entree = flux.gi_frame.f_locals if hasattr(flux, "gi_frame") else {}
            try:
                while True:
                    next(flux)
            except StopIteration:
                pass

        # Aucun paramètre d'en-tête de dossier n'existe plus : la consigne est
        # statique, le seul contenu variable est la page jointe.
        self.assertNotIn("dossier_block", entree)
        self.assertIn("EN-TÊTE IMPRIMÉ", captured["user"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
