"""Régressions de ``Matcher._fusionner_candidats_posologie``.

La fusion regroupe deux candidats PHONÉTIQUES qui se disputent une MÊME
posologie adjacente (écart <= 2 jetons) quand le STT a scindé un nom unique
(« Applique, ça bande » → apixaban). Depuis 2026-09-09, elle ne fusionne QUE
les paires dont AUCUN fragment n'est fort (``_FUSION_FRAG_FORT`` = 0.80) : un
fragment qui colle à >= 0.80 est un nom de médicament AUTONOME (le STT l'a
presque écrit), et deux voisins forts à même dose sont deux médicaments
légitimes dictés à la suite (« télénol, l'irrita 100 mg tid » → Tylenol +
Lyrica, jamais fusionnés — consult 48).

Exécuter dans le conteneur (rapidfuzz requis — NE PAS poser PYTHONPATH=/app,
qui masquerait ``/opt/consultai-extras`` où vit rapidfuzz) :

    docker cp tests/test_fusion_candidats.py consultai-test:/tmp/
    docker exec consultai-test python3 /tmp/test_fusion_candidats.py

Calculé sur la base : apixaban = {Eliquis 0.71, Banzel 0.67} ;
Tylenol/Lyrica = {0.857, 0.675} ; Dexilant/Prevacid = {0.875, 0.778}.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

from app.med_grounding import Matcher, _FUSION_FRAG_FORT


def _it(name, base, poso, score, i):
    return {"name": name, "base": base, "posology": poso, "score": score,
            "source": "phonetic", "_i": i}


class FusionCandidatsTests(unittest.TestCase):

    def setUp(self):
        self.m = Matcher(use_phonetic=True)

    def test_seuil_configure(self):
        self.assertEqual(_FUSION_FRAG_FORT, 0.80)

    def test_nom_scinde_fusionne(self):
        """« Applique, ça bande » → apixaban : DEUX fragments faibles."""
        items = [_it("Applique", "eliquis", "5 mg 2 fois par jour", 71, 0),
                 _it("bande", "banzel", "5 mg 2 fois par jour", 67, 2)]
        out = self.m._fusionner_candidats_posologie(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["base"], "eliquis")
        self.assertEqual(out[0]["name"], "Applique bande")

    def test_voisins_forts_non_fusionnes(self):
        """« télénol, l'irrita 100 tid » : Tylenol (fort) + Lyrica — 2 meds."""
        items = [_it("télénol", "acetaminophen", "100 mg 3 fois par jour", 86, 0),
                 _it("l'irrita", "lyrica", "100 mg 3 fois par jour", 68, 1)]
        out = self.m._fusionner_candidats_posologie(items)
        self.assertEqual(len(out), 2)
        bases = {o["base"] for o in out}
        self.assertEqual(bases, {"acetaminophen", "lyrica"})

    def test_voisins_forts_non_fusionnes_2(self):
        """« Lexilan, non, Privacide 30 mg » : Dexilant (fort) + Prevacid."""
        items = [_it("Lexilan", "dexilant", "30 mg", 88, 0),
                 _it("Privacide", "prevacid", "30 mg", 78, 2)]
        out = self.m._fusionner_candidats_posologie(items)
        self.assertEqual(len(out), 2)
        bases = {o["base"] for o in out}
        self.assertEqual(bases, {"dexilant", "prevacid"})

    def test_posologies_distinctes_jamais_fusionnees(self):
        items = [_it("a", "med1", "5 mg", 70, 0),
                 _it("b", "med2", "10 mg", 65, 1)]
        out = self.m._fusionner_candidats_posologie(items)
        self.assertEqual(len(out), 2)

    def test_positions_eloignees_jamais_fusionnees(self):
        items = [_it("a", "med1", "5 mg", 70, 0),
                 _it("b", "med2", "5 mg", 65, 5)]
        out = self.m._fusionner_candidats_posologie(items)
        self.assertEqual(len(out), 2)

    def test_region_consult_48_lyrica_respire(self):
        """Fin à fin sur la région de la consult 48 : Lyrica ne disparaît pas
        derrière Tylenol (régression du 2026-09-09, note « Tylenol, Lyrica 100
        tid »)."""
        region = ("il prend du nicoderme, hydralazine, monocore, dilodide au "
                  "besoin, télénol, l'irrita, 100 mg trois fois par jour, "
                  "Effector, 225 mg une fois par jour.")
        conf = {"lirrita": 0.781, "telenol": 0.835, "monocore": 0.89,
                "dilodide": 0.831, "effector": 0.806, "nicoderme": 0.995,
                "hydralazine": 0.9}
        self.m.set_med_region(region, region)
        try:
            out = self.m.suggestions_texte(region, conf=conf, maxi=40)
        finally:
            self.m.clear_med_region()
        lyrica = [o for o in out if o["base"] == "lyrica"]
        self.assertEqual(len(lyrica), 1)
        self.assertEqual(lyrica[0]["name"], "l'irrita")
        # Aucune fusion « l'irrita télénol ».
        self.assertFalse(any(" " in (o["name"] or "") for o in out
                             if o["base"] == "lyrica"))


if __name__ == "__main__":
    unittest.main(verbosity=2)