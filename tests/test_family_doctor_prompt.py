"""
tests/test_family_doctor_prompt.py — Sources du dossier dans le prompt.

Deux garanties, sur le texte réellement assemblé :

1. Les pages jointes portent la consigne de l'en-tête imprimé — la note lit le
   médecin de famille sur la page, sans dépendre de la passe de lecture.
2. Le prompt SYSTÈME délimite la règle « ce qui ne figure pas dans la dictée
   est retiré » quand des documents accompagnent la requête. C'est là que se
   règle le statut des faits du dossier — pas dans le message utilisateur, qui
   n'a pas à démentir trois fois la même consigne.

Le bloc de pages reste court : un test de taille empêche le retour de
l'empilement d'exceptions (il faisait 3 090 caractères le 2026-11-24).

Exécuter dans le conteneur :

    docker cp tests/test_family_doctor_prompt.py consultai-test:/tmp/
    docker exec -e PYTHONPATH=/app python3 /tmp/test_family_doctor_prompt.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from app import llm


def _user_prompt(**kwargs) -> str:
    return llm.build_user_prompt(
        kwargs.pop("transcript", ""),
        kwargs.pop("layout_format", "**Médecin de famille :**"),
        language="fr",
        **kwargs,
    )


class PagesPromptTest(unittest.TestCase):
    def test_entete_imprime_demande_avec_les_pages(self):
        prompt = _user_prompt(documents_pages=True)
        self.assertIn("EN-TÊTE IMPRIMÉ", prompt)
        self.assertIn("médecin de famille", prompt)

    def test_bloc_pages_reste_court(self):
        bloc = llm._USER_PROMPT_LABELS["fr"]["documents_pages"]
        self.assertLess(len(bloc), 2300, "le bloc de pages regonfle")

    def test_aucune_valeur_d_exemple(self):
        """Ni nom, ni numéro, ni libellé cité : une valeur d'exemple est une
        donnée que le modèle peut recopier dans la note."""
        paragraphe = llm._USER_PROMPT_LABELS["fr"]["documents_pages"]
        for interdit in ("MABY", "GAP", "20387", "« Médecin", "prénom", "Dr ", "Dre "):
            self.assertNotIn(interdit, paragraphe)

    def test_pas_d_entete_en_mode_texte(self):
        prompt = _user_prompt(
            transcript="dictée",
            documents_block="Médecin de famille imprimé en tête du document : X — 1",
        )
        self.assertNotIn("EN-TÊTE IMPRIMÉ", prompt)


class ScopeTest(unittest.TestCase):
    def test_le_systeme_declare_les_documents_comme_sources(self):
        system = llm.build_system_prompt("gabarit", "général", "fr", documents=True)
        self.assertIn("CHAMP DES CONSIGNES", system)
        self.assertIn("documents du dossier joints", system)
        self.assertIn("s'entend donc de la dictée ET des documents joints", system)

    def test_pas_de_cadrage_sans_documents(self):
        system = llm.build_system_prompt("gabarit", "général", "fr")
        self.assertNotIn("CHAMP DES CONSIGNES", system)

    def test_le_cadrage_suit_la_consigne_generale(self):
        """Le cadrage est en dernier : il précise la portée de la règle qui
        précède, sans la contredire."""
        system = llm.build_system_prompt("gabarit", "CONSIGNE GENERALE", "fr", documents=True)
        self.assertLess(system.index("CONSIGNE GENERALE"), system.index("CHAMP DES CONSIGNES"))


class GenerationPromptTest(unittest.TestCase):
    def test_les_pages_arrivent_au_modele(self):
        captured = {}

        def faux_flux(system, user, **params):
            captured["user"] = user
            captured["system"] = system
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

        self.assertNotIn("dossier_block", entree)
        self.assertIn("EN-TÊTE IMPRIMÉ", captured["user"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
