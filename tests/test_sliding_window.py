"""Régressions de la fenêtre glissante (mode ``stt_sliding_window``).

Le service vocal est MOQUÉ : aucun réseau, aucun ffmpeg ; la base n'est pas
requise (``Consultation`` absent → branchement protégé). Exécuter dans le
conteneur :

    docker cp tests/test_sliding_window.py consultai-test:/tmp/
    docker exec -e PYTHONPATH=/app consultai-test python3 /tmp/test_sliding_window.py

Contrats vérifiés :
  * premier texte dès ``pas`` secondes (la fenêtre croît, puis se sature) ;
  * en régime établi, ~une part confirmée par pas ; conservation EXACTE des
    jetons à l'engagement final ;
  * alignement marginal (pic 0.75-0.87) → plage marquée ``verify_ranges`` ;
  * alignement rompu → fenêtre jetée + rattrapage en mini-tranche AU
    provisoire (jamais de part directe) ;
  * ``_verifier_residuel`` : cible les plages marquées via ``_reecouter_plage``
    ; ``stt_verify_max_seconds=0`` → aucune re-écoute.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from app import dictation


class _Payload:
    def __init__(self, duree: float, debut: float):
        self.duration_seconds = duree
        self.debut = debut


def _script(secondes: int) -> list:
    return [f"w{n}" for n in range(secondes)]


def _mots(debut: float, fin: float, script: list) -> list:
    """Jetons attendus pour [debut, fin] — bornes entières dans les tests."""
    premier = int(round(debut))
    dernier = min(int(round(fin)), len(script))
    return [script[t] for t in range(max(0, premier), max(0, dernier))]


def _altere(mots: list, pas: int, phase: int = 0) -> list:
    """Change un mot sur ``pas`` — lecture altérée du MÊME audio."""
    return [("zz" + m[1:]) if (i + phase) % pas == 0 else m
            for i, m in enumerate(mots)]


class FenetreTests(unittest.TestCase):

    def setUp(self):
        self.dossier = tempfile.mkdtemp()
        self._dir_orig = dictation.DictationSession.directory
        self._root_orig = dictation._root
        dictation.DictationSession.directory = property(
            lambda s: f"{self.dossier}/{s.id}")
        dictation._root = lambda: self.dossier
        self.addCleanup(self._detacher)
        self._rc_orig = dictation.runtime_config.value
        self.config_values = {}

        def faux_value(cle):
            if cle in self.config_values:
                return self.config_values[cle]
            return self._rc_orig(cle)

        p = patch.object(dictation.runtime_config, "value", faux_value)
        p.start()
        self.addCleanup(p.stop)
        self.session = dictation.DictationSession(
            id="a" * 32, username="test", consultation_id=1)
        os.makedirs(self.session.directory, exist_ok=True)

    def _detacher(self):
        dictation.DictationSession.directory = self._dir_orig
        dictation._root = self._root_orig

    def _reglages(self, fenetre=90, pas=15, verify=60):
        self.config_values.update({
            "stt_sliding_window": "true",
            "stt_window_seconds": str(fenetre),
            "stt_window_step_seconds": str(pas),
            "stt_verify_max_seconds": str(verify),
        })

    def _moquer_stt(self, oracle):
        """Patch extract/transcribe/_store_part/_usage/_session_owner.

        ``oracle(payload) -> texte`` pour la fenêtre ``[debut, fin[``.
        """
        def faux_extract(path, debut, longueur, real=None):
            return _Payload(round(longueur, 3), debut)

        def faux_transcribe(payload, hints, boost=0, on_progress=None,
                            contexte_precedent=None):
            return {"transcript": oracle(payload) or "",
                    "provider": "fake", "model": "m", "words": None}

        def faux_store_part(session, texte, moteur=("", ""),
                            duration_seconds=0.0, words=None):
            if texte:
                session.parts.append(texte)
                session.parts_conf.append({})

        for module, nom, imu in (
            (dictation, "extract_segment", faux_extract),
            (dictation, "transcribe_payload", faux_transcribe),
            (dictation, "_store_part", faux_store_part),
            (dictation, "_usage_fenetre", lambda *a, **k: None),
            (dictation, "_session_owner", lambda s: "test"),
        ):
            p = patch.object(module, nom, imu)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _oracle_fidele(script):
        def oracle(payload):
            return " ".join(_mots(payload.debut,
                                  payload.debut + payload.duration_seconds,
                                  script))
        return oracle

    # ------------------------------------------------------------------
    def test_croissance_puis_regime_sans_perte(self):
        """Premier texte à ~15 s ; conservation EXACTE des jetons, de 0 à 450 s."""
        script = _script(450)
        self._reglages()
        self._moquer_stt(self._oracle_fidele(script))
        session = self.session

        session.received_seconds = 15
        self.assertIsNotNone(dictation._transcribe_fenetre(session, "hints"))
        self.assertEqual(
            session.window_text, " ".join(_mots(0, 15, script)),
            "premier texte dès pas=15 s (fenêtre en croissance)")

        for i in range(2, 7):                      # croissance → [0, 90]
            session.received_seconds = 15 * i
            self.assertIsNotNone(dictation._transcribe_fenetre(session, "hints"))
        self.assertEqual(session.parts, [], "croissance : rien d'engagé")
        self.assertEqual(session.window_text, " ".join(_mots(0, 90, script)))

        for i in range(7, 31):                     # régime : 15 s confirmées par passe
            session.received_seconds = 15 * i
            self.assertIsNotNone(dictation._transcribe_fenetre(session, "hints"))
        self.assertGreaterEqual(len(session.parts), 15)

        dictation._engager_provisoire(session)
        jetons = dictation._jetons_norm(" ".join(session.parts))
        self.assertEqual(jetons, dictation._jetons_norm(" ".join(script)),
                         "conservation EXACTE de tout le script sans perte")
        self.assertEqual(session.window_text, "")

    # ------------------------------------------------------------------
    def test_frontiere_marginale_marquee(self):
        """Pic de ratio dans (0.75, 0.87) → plage marquée, pas de rattrapage."""
        script = _script(300)
        self._reglages()
        etat = {"appels": 0}

        def oracle(payload):
            etat["appels"] += 1
            mots = _mots(payload.debut, payload.debut + payload.duration_seconds,
                         script)
            # Appel 6 = première passe SATURÉE ([15, 90]) : frontière altérée
            # (1 mot sur 5 → pic ~0.80), puis retour au fidèle.
            if etat["appels"] == 6:
                return " ".join(_altere(mots, 5))
            return " ".join(mots)

        self._moquer_stt(oracle)
        session = self.session
        for i in range(1, 11):
            session.received_seconds = 15 * i
            self.assertIsNotNone(dictation._transcribe_fenetre(session, "hints"))
        self.assertEqual(len(session.verify_ranges), 1,
                         "la frontière altérée est marquée une fois")
        self.assertGreater(len(session.parts), 0,
                           "l'alignement reste accepté (texte confirmé)")

    # ------------------------------------------------------------------
    def test_alignement_rompu_rattrapage(self):
        """Fenêtres illisibles d'ensemble : jetées, rattrapage, aucune perte."""
        script = _script(300)
        self._reglages()
        etat = {"appels": 0}

        def oracle(payload):
            etat["appels"] += 1
            # Toute fenêtre PLEINE (> pas+1 s), SAUF la première : lecture où
            # CHAQUE jeton embarque le début de fenêtre — deux fenêtres se
            # recouvrant partagent donc AUCUN jeton → alignement impossible.
            if payload.duration_seconds > 21 and etat["appels"] > 1:
                return " ".join(
                    f"q{int(payload.debut) + int(t)}_{t}"
                    for t in range(int(payload.duration_seconds)))
            # Première fenêtre et mini-tranches (≤ pas+~5 s) : fidèle.
            return " ".join(_mots(payload.debut,
                                  payload.debut + payload.duration_seconds, script))

        self._moquer_stt(oracle)
        session = self.session
        session.received_seconds = 97
        self.assertIsNotNone(dictation._transcribe_fenetre(session, "hints"))
        self.assertTrue(session.window_text)
        session.received_seconds = 112
        self.assertIsNotNone(dictation._transcribe_fenetre(session, "hints"))
        # La fenêtre illisible est JETÉE ; le rattrapage fidèle a rejoint le
        # provisoire, et la plage est marquée pour la vérification.
        self.assertGreater(len(session.verify_ranges), 0)
        jetons = dictation._jetons_norm(session.window_text)
        for t in range(0, 90):
            self.assertIn(dictation._jeton_norm(f"w{t}"), jetons,
                          "la base fidèle reste dans le provisoire")

    # ------------------------------------------------------------------
    def test_verifier_residuel_marque_et_budget(self):
        script = _script(300)
        self._reglages(verify=120)
        self._moquer_stt(self._oracle_fidele(script))
        session = self.session
        for i in range(1, 21):
            session.received_seconds = 15 * i
            self.assertIsNotNone(dictation._transcribe_fenetre(session, "hints"))
        dictation._engager_provisoire(session)
        self.assertGreater(len(session.parts), 0)

        appels: list = []

        def faux_reecouter(sess, a, b, hints):
            appels.append((a, b))
            return False

        session.verify_ranges = [(40.0, 50.0)]
        with patch.object(dictation, "_reecouter_plage", faux_reecouter):
            dictation._verifier_residuel(session, "hints")
        self.assertEqual(session.verify_ranges, [], "plages consommées")
        self.assertGreaterEqual(len(appels), 1, "la plage marquée a été visitée")

        # Budget 0 → aucune re-écoute.
        self._reglages(verify=0)
        session.verify_ranges = [(40.0, 50.0)]
        with patch.object(dictation, "_reecouter_plage",
                          lambda *a, **k: self.fail("budget 0 → pas de re-écoute")):
            dictation._verifier_residuel(session, "hints")
        self.assertEqual(session.verify_ranges, [])


if __name__ == "__main__":
    unittest.main()
