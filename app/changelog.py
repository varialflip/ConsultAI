"""
changelog.py — Lecture du CHANGELOG.md embarqué dans l'image.

Le fichier ``/app/CHANGELOG.md`` (copié au build, racine du dépôt) liste les
versions datées. Ce module le parse et n'expose que les entrées des derniers
jours : de quoi alimenter la section informative du panneau de droite sans
envoyer tout l'historique au navigateur.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List

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


@dataclass
class ChangelogEntry:
    date: date
    title: str
    items: List[str]


def _read_source() -> str:
    for path in _CHANGELOG_PATHS:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read()
    return ""


def recent_entries(days: int = 7) -> List[ChangelogEntry]:
    """Entrées du CHANGELOG datées de moins de ``days`` jours (date du jour
    incluse), les plus récentes d'abord."""
    source = _read_source()
    if not source:
        return []

    matches = list(_ENTRY_RE.finditer(source))
    if not matches:
        return []

    cutoff = date.today() - timedelta(days=max(0, days - 1))
    result: List[ChangelogEntry] = []
    for index, match in enumerate(matches):
        entry_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        if entry_date < cutoff:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        items = [
            line.strip("- ").strip()
            for line in source[start:end].splitlines()
            if line.strip().startswith("-")
        ]
        result.append(
            ChangelogEntry(date=entry_date, title=match.group(2).strip(), items=items)
        )
    return result
