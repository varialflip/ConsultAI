"""Régressions de persistance des sessions de dictée."""

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import dictation


def test_sauvegardes_concurrentes_restent_du_json_valide():
    """Deux flux de dictée ne doivent jamais entrelacer state.json."""
    with tempfile.TemporaryDirectory() as dossier:
        racine = dossier + "/dictations"
        original_directory = dictation.DictationSession.directory
        original_root = dictation._root
        dictation.DictationSession.directory = property(
            lambda self: f"{racine}/{self.id}"
        )
        dictation._root = lambda: racine
        try:
            session = dictation.create_session("test", 1, None, "audio/webm")
            sessions = [
                dictation.load_session(session.id, "test")
                for _ in range(8)
            ]

            def sauvegarder(index):
                sessions[index].last_error = f"essai-{index}"
                sessions[index].save()

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(sauvegarder, range(len(sessions))))

            with open(session.state_path, encoding="utf-8") as handle:
                donnees = json.load(handle)

            assert donnees["id"] == session.id
            assert donnees["last_error"].startswith("essai-")
            assert not list(Path(racine).glob("*/.state-*.tmp"))
        finally:
            if "session" in locals():
                dictation.delete_session(session)
            dictation.DictationSession.directory = original_directory
            dictation._root = original_root
