"""
tests/test_dossier_header.py — Médecin de famille du dossier, dans la note.

Le médecin imprimé en tête du DSQ est un FAIT que l'application résout
elle-même (``_resoudre_medecin_famille``). Il doit parvenir au modèle dans
TOUS les modes — y compris « Envoyer les pages à la note », où le bloc de
texte des documents est vidé : c'est là qu'il disparaissait, et la note
perdait le champ « Médecin de famille » du gabarit.

Ces tests ne touchent pas au réseau : ils vérifient l'assemblage du message
utilisateur et le contenu de l'en-tête, pas la réponse du modèle.

Exécuter dans le conteneur :

    docker cp tests/test_dossier_header.py consultai-test:/tmp/
    docker exec consultai-test python3 /tmp/test_dossier_header.py
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


class DossierHeaderPromptTest(unittest.TestCase):
    def test_entete_part_avec_les_pages_seules(self):
        """Mode pages : le bloc de texte est vide, l'en-tête doit rester."""
        prompt = _prompt(
            documents_pages=True,
            dossier_block="Médecin de famille : MABY, Alexandra — 20387",
        )
        self.assertIn("ENTETE_DOSSIER", prompt)
        self.assertIn("MABY, Alexandra — 20387", prompt)

    def test_entete_part_sans_document_nil_pages(self):
        """Aucune pièce jointe, bloc de texte vide : l'en-tête part quand même
        (dictée seule, dossier lu plus tôt)."""
        prompt = _prompt(
            transcript="dictée",
            dossier_block="Médecin de famille : MABY, Alexandra — 20387",
        )
        self.assertIn("MABY, Alexandra", prompt)

    def test_pas_d_entete_sans_medecin(self):
        prompt = _prompt(transcript="dictée", dossier_block="")
        self.assertNotIn("ENTETE_DOSSIER", prompt)

    def test_consigne_ne_donne_aucun_exemple(self):
        """L'instruction ne doit porter aucun libellé d'exemple : un exemple
        dans le prompt est une donnée que le modèle peut recopier."""
        prompt = _prompt(transcript="dictée", dossier_block="Médecin : X")
        consigne = prompt.split("ENTETE_DOSSIER>>>", 1)[1]
        for exemple in ("(«", "« Médecin", "Médecin référent", "GAP", "prénom nom"):
            self.assertNotIn(exemple, consigne)


class GenerateNoteDossierTest(unittest.TestCase):
    """L'en-tête traverse la génération jusqu'au message envoyé au modèle."""

    def _run(self, **kwargs) -> dict:
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
                system_override="SYS", **kwargs,
            )
            try:
                while True:
                    next(flux)
            except StopIteration:
                pass
        return captured

    def test_entete_transmis_en_mode_pages(self):
        captured = self._run(
            documents=[(b"%PDF-1.4", "application/pdf")],
            dossier_block="Médecin de famille : MABY, Alexandra — 20387",
        )
        self.assertIn("MABY, Alexandra — 20387", captured["user"])

    def test_sans_entete_le_prompt_est_inchange(self):
        captured = self._run(documents=[(b"%PDF-1.4", "application/pdf")])
        self.assertNotIn("ENTETE_DOSSIER", captured["user"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
