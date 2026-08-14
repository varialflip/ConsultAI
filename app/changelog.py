"""
changelog.py — Lecture du CHANGELOG.md embarqué dans l'image.

Le fichier ``/app/CHANGELOG.md`` (copié au build, racine du dépôt) liste les
versions datées. Ce module le parse et n'expose que les entrées des derniers
jours : de quoi alimenter la page de connexion sans envoyer tout l'historique
au navigateur.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Tuple

logger = logging.getLogger(__name__)

#: Emplacements possibles du fichier — l'image le pose à ``/app/CHANGELOG.md``,
#: mais on accepte aussi la racine du dépôt (exécution hors conteneur).
_CHANGELOG_PATHS = (
    "/app/CHANGELOG.md",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "CHANGELOG.md"),
)

#: « ## AAAA-MM-JJ — vX.Y.Z-beta.N » en tête d'entrée.
_ENTRY_RE = re.compile(
    r"^##\s+(\d{4}-\d{2}-\d{2})\s*—\s*(\S[^\n]*)", re.MULTILINE
)

#: Le numéro de version est le dernier nombre de l'intitulé (« v2.0.0-beta.38 »).
_VERSION_RE = re.compile(r"(\d+)\s*$")


@dataclass
class ChangelogEntry:
    date: date
    title: str
    items: List[str]


@dataclass
class ChangelogDay:
    """Un jour de nouveautés : date et items fusionnés de toutes les versions."""

    date: date
    items: List[str]


def _read_source() -> str:
    for path in _CHANGELOG_PATHS:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read()
    return ""


def _parse() -> List[ChangelogEntry]:
    """Toutes les entrées du fichier, dans l'ordre où elles y apparaissent."""
    source = _read_source()
    if not source:
        return []

    matches = list(_ENTRY_RE.finditer(source))
    if not matches:
        return []

    result: List[ChangelogEntry] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        items = [
            line.strip("- ").strip()
            for line in source[start:end].splitlines()
            if line.strip().startswith("-")
        ]
        result.append(
            ChangelogEntry(
                date=datetime.strptime(match.group(1), "%Y-%m-%d").date(),
                title=match.group(2).strip(),
                items=items,
            )
        )
    return result


def _version_key(title: str) -> Tuple[int, ...]:
    """
    Ordre décroissant des intitulés « v2.0.0-beta.N » par numéro de version.

    L'ordre du fichier n'est pas fiable — une version peut y être insérée avant
    une autre du même jour (ex. beta.34 avant beta.37). Extraire le dernier
    nombre permet de présenter le jour du plus récent au plus ancien.
    """
    nombre = _VERSION_RE.search(title)
    return (int(nombre.group(1)) if nombre else 0,)


def recent_entries(days: int = 7) -> List[ChangelogEntry]:
    """Entrées du CHANGELOG datées de moins de ``days`` jours (date du jour
    incluse), les plus récentes d'abord."""
    cutoff = date.today() - timedelta(days=max(0, days - 1))
    entries = [e for e in _parse() if e.date >= cutoff]
    entries.sort(key=lambda e: (e.date, _version_key(e.title)), reverse=True)
    return entries


def recent_by_day(days: int = 7) -> List[ChangelogDay]:
    """Nouveautés des ``days`` derniers jours, regroupées par date.

    Un jour, un sous-titre : les items des différentes versions publiées ce
    jour-là sont fusionnés, du plus récent au plus ancien. Les dates sont
    présentées de la plus récente à la plus ancienne.
    """
    entries = recent_entries(days=days)
    jours: List[ChangelogDay] = []
    for entry in entries:
        if jours and jours[-1].date == entry.date:
            jours[-1].items.extend(entry.items)
        else:
            jours.append(ChangelogDay(date=entry.date, items=list(entry.items)))
    return jours
