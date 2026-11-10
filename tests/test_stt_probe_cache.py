"""La sonde de disponibilité du STT ne se contredit pas d'un appel à l'autre.

Régression (instance de test, 2026-11-10) : le cache rendait le résultat BRUT
de la sonde là où la fonction renvoie « indisponible » — donc l'inverse. Un
service parfaitement joignable était annoncé injoignable pendant tout le TTL
(15 s), et le navigateur affichait « service vocal indisponible » au milieu
d'une dictée qui transcrivait normalement. Comme la passe de dictée sonde à
chaque tour, l'avis clignotait toutes les deux passes.
"""

import pytest

from app import runtime_config, stt


@pytest.fixture(autouse=True)
def _environnement_isole(monkeypatch):
    """Pas de base de réglages ni de réseau : la sonde est remplacée."""
    monkeypatch.setattr(runtime_config, "value", lambda cle, *a, **k: "")
    monkeypatch.setattr(runtime_config, "stt_language", lambda fournisseur: "fr")
    stt._STT_PROBE_CACHE.clear()
    yield
    stt._STT_PROBE_CACHE.clear()


def _sonde_factice(monkeypatch, joignable):
    """Remplace la sonde réseau et retourne la liste de ses appels réels."""
    appels = []

    def _faux(base_url, api_key, model, langue):
        appels.append(base_url)
        return joignable

    monkeypatch.setattr(stt, "_probe_custom", _faux)
    return appels


def test_service_joignable_reste_disponible_avec_le_cache(monkeypatch):
    appels = _sonde_factice(monkeypatch, True)

    assert stt.stt_unavailable("http://stt/v1") is False
    # Deuxième appel servi par le cache : il doit rendre LE MÊME verdict.
    assert stt.stt_unavailable("http://stt/v1") is False
    assert stt.stt_available("http://stt/v1") is True
    assert len(appels) == 1, "le cache doit éviter une seconde sonde réseau"


def test_service_injoignable_reste_indisponible_avec_le_cache(monkeypatch):
    appels = _sonde_factice(monkeypatch, False)

    assert stt.stt_unavailable("http://stt/v1") is True
    assert stt.stt_unavailable("http://stt/v1") is True
    assert stt.stt_available("http://stt/v1") is False
    assert len(appels) == 1
