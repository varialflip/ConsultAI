"""
note_schema.py — Gabarit -> structure attendue, et structure de la note extraite.
===================================================================================

Un ``layout_format`` de gabarit (voir ``default_templates.py``) est un texte
markdown : un titre ``#``, des champs d'en-tête ``**Libellé :**``, des
rubriques ``##``/``###`` (parfois imbriquées, parfois elles-mêmes composées de
champs ``**Libellé :**`` plutôt que de prose), des lignes de texte fixe
(ex. « Rédigé à l'aide de la reconnaissance vocale. »), et la rubrique finale
``## ÉLÉMENTS À VALIDER``.

``parse_layout`` lit ce texte une fois et produit une ``LayoutSpec`` : la
liste ordonnée des éléments attendus, avec leur parent. C'est la SEULE source
de vérité sur « quelles rubriques/champs existent » — ni l'extraction, ni le
rendu, ni la validation ne codent en dur une liste de rubriques. Un gabarit
dupliqué/modifié par un médecin (``EDITABLE_TEMPLATES``) est donc supporté
sans changement de code ici.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

# ---------------------------------------------------------------------------
# Gabarit -> LayoutSpec
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r"^#\s+(.+)$")
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+)$")
_BOLD_FIELD_RE = re.compile(r"^\*\*(.+?)\s*:\*\*\s*$")
_ELEMENTS_A_VALIDER_LABELS = {"ÉLÉMENTS À VALIDER", "ELEMENTS A VALIDER"}

#: Marqueurs de style de liste reconnus comme instruction {{...}} placée juste
#: sous un titre de rubrique (ex. « ## PLAN » suivi de « {{liste numérotée}} »).
#: Décision de RENDU déterministe (voir LayoutSpec.list_style/note_renderer) —
#: jamais déduite du texte libre de system_instructions, qui n'est pas fiable
#: pour une décision de code : l'auteur du gabarit la déclare explicitement,
#: au même endroit que les autres indices de forme {{...}}.
LIST_STYLE_MARKERS = {
    "{{liste numérotée}}": "numbered",
    "{{numbered list}}": "numbered",
    "{{liste à puces}}": "bulleted",
    "{{bulleted list}}": "bulleted",
}


@dataclass
class LayoutEntry:
    kind: str  # "bold_field" | "heading" | "literal" | "instruction"
    label: str
    level: int = 0  # 2 ou 3 pour "heading" ; 0 sinon
    parent: Optional[str] = None  # libellé de la rubrique englobante, ou None


@dataclass
class LayoutSpec:
    title: str
    entries: List[LayoutEntry] = field(default_factory=list)
    has_elements_a_valider: bool = False

    def top_level_fields(self) -> List[str]:
        """Champs d'en-tête (avant toute rubrique) — ex. Lieu, Date."""
        return [e.label for e in self.entries if e.kind == "bold_field" and e.parent is None]

    def headings(self) -> List[LayoutEntry]:
        return [e for e in self.entries if e.kind == "heading"]

    def literals_of(self, label: Optional[str]) -> List[str]:
        return [e.label for e in self.entries if e.kind == "literal" and e.parent == label]

    def explicit_list_style(self, label: str) -> Optional[str]:
        """« numbered »/« bulleted » SEULEMENT si le gabarit porte un
        marqueur {{...}} explicite sous cette rubrique (voir
        LIST_STYLE_MARKERS) ; ``None`` sinon — distinct de ``list_style``,
        qui retombe sur « bulleted » par défaut pour le RENDU. Cette
        distinction sert à ``note_extraction`` : ne pousser le modèle vers un
        encodage en tableau JSON que pour les rubriques où l'auteur du
        gabarit l'a demandé explicitement, jamais par défaut."""
        for e in self.entries:
            if e.kind == "instruction" and e.parent == label and e.label in LIST_STYLE_MARKERS:
                return LIST_STYLE_MARKERS[e.label]
        return None

    def list_style(self, label: str) -> str:
        """« numbered » si le gabarit porte un marqueur {{liste numérotée}}
        sous cette rubrique, sinon « bulleted » (comportement historique) —
        voir LIST_STYLE_MARKERS. Le numérotage lui-même reste toujours fait
        par note_renderer, jamais par le modèle : ça évite qu'un item sur
        deux perde son numéro ou que la numérotation reparte à 1."""
        return self.explicit_list_style(label) or "bulleted"


def parse_layout(layout_format: str) -> LayoutSpec:
    title = ""
    entries: List[LayoutEntry] = []
    current_parent: Optional[str] = None
    # Pile des rubriques ouvertes, par niveau (2 -> label, 3 -> label).
    open_headings: Dict[int, str] = {}
    has_eav = False

    for raw_line in layout_format.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        m = _TITLE_RE.match(line)
        if m and not title:
            title = m.group(1).strip()
            continue

        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            label = m.group(2).strip()
            if label.upper() in _ELEMENTS_A_VALIDER_LABELS:
                has_eav = True
                # N'est pas une rubrique de contenu ordinaire : pas d'entrée,
                # gérée à part par extraction/rendu (voir note_renderer).
                current_parent = None
                open_headings = {}
                continue
            parent = open_headings.get(level - 1) if level > 2 else None
            entries.append(LayoutEntry(kind="heading", label=label, level=level, parent=parent))
            open_headings[level] = label
            # Une sous-rubrique de niveau inférieur devient invalide.
            for lvl in list(open_headings):
                if lvl > level:
                    del open_headings[lvl]
            current_parent = label
            continue

        m = _BOLD_FIELD_RE.match(line)
        if m:
            entries.append(LayoutEntry(kind="bold_field", label=m.group(1).strip(), parent=current_parent))
            continue

        if "{{" in line:
            # Consigne d'auteur de gabarit (ex. « {{Phrase résumé}} »,
            # « 1. {{Problème 1}} ») — PAS du texte fixe : c'est un exemple de
            # forme attendue pour la rubrique en cours, à remplacer par le
            # contenu clinique ou à supprimer si inconnu (règle du § 4 de la
            # consigne générale). Ne doit JAMAIS être reproduit tel quel —
            # ni par le modèle, ni par ce rendu. On le garde seulement comme
            # indice de forme (voir note_extraction.build_expected_json_skeleton) ;
            # le rendu (note_renderer) l'ignore complètement.
            entries.append(LayoutEntry(kind="instruction", label=line, parent=current_parent))
            continue

        # Ligne de texte fixe (boilerplate, ex. « Rédigé à l'aide de la
        # reconnaissance vocale. ») — reproduite telle quelle au rendu,
        # jamais demandée au modèle. Toujours rattachée au NIVEAU DOCUMENT
        # (parent=None), pas à la rubrique qui la précède : sinon une
        # rubrique vide (ex. PLAN sans contenu dicté) emporterait le
        # texte fixe avec elle en disparaissant — voir note_renderer.render,
        # qui émet les littéraux document-niveau indépendamment des
        # rubriques de contenu.
        entries.append(LayoutEntry(kind="literal", label=line, parent=None))

    return LayoutSpec(title=title, entries=entries, has_elements_a_valider=has_eav)


# ---------------------------------------------------------------------------
# Note extraite (sortie du modèle, avant rendu)
# ---------------------------------------------------------------------------

# Une valeur de rubrique est de la prose, une liste à puces (ex. médication,
# examen physique — le gabarit demande explicitement une « liste pointée »),
# ou — quand la rubrique n'est faite que de champs/sous-rubriques — un dict
# imbriqué avec les mêmes libellés que le gabarit.
SectionValue = Union[str, List[str], Dict[str, "SectionValue"]]


@dataclass
class GroundedField:
    """Une valeur critique (médicament, dose, date, nom, diagnostic...)
    avec l'extrait exact de la transcription dont elle est tirée — voir
    note_validator.check_grounding."""

    field: str
    value: Optional[str]
    source_span: Optional[str]
    note: str = ""

    def to_dict(self) -> dict:
        return {"field": self.field, "value": self.value, "source_span": self.source_span, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "GroundedField":
        return cls(
            field=str(d.get("field", "")).strip(),
            value=(str(d["value"]).strip() if d.get("value") not in (None, "") else None),
            source_span=(str(d["source_span"]).strip() if d.get("source_span") else None),
            note=str(d.get("note", "")).strip(),
        )


@dataclass
class ElementAValider:
    """Une ligne de la rubrique Éléments à valider.

    ``kind == "item"`` : un élément individuel — ``terme_dicte`` +
    (``correction`` si tranché, sinon rien = à confirmer).
    ``kind == "group"`` : le cas « plus de 8 éléments », un résumé par
    catégorie (ex. « 5 dates approximatives non confirmées »).
    """

    kind: str  # "item" | "group"
    terme_dicte: str = ""
    correction: Optional[str] = None
    texte_groupe: str = ""

    @property
    def is_confirmed_reading(self) -> bool:
        return self.kind == "item" and bool(self.correction)

    def to_dict(self) -> dict:
        if self.kind == "group":
            return {"kind": "group", "texte_groupe": self.texte_groupe}
        return {"kind": "item", "terme_dicte": self.terme_dicte, "correction": self.correction}

    @classmethod
    def from_dict(cls, d: dict) -> "ElementAValider":
        if d.get("kind") == "group":
            return cls(kind="group", texte_groupe=str(d.get("texte_groupe", "")).strip())
        correction = d.get("correction")
        correction = str(correction).strip() if correction not in (None, "") else None
        return cls(kind="item", terme_dicte=str(d.get("terme_dicte", "")).strip(), correction=correction)


@dataclass
class ExtractedNote:
    header_fields: Dict[str, str] = field(default_factory=dict)
    sections: Dict[str, SectionValue] = field(default_factory=dict)
    elements_a_valider: List[ElementAValider] = field(default_factory=list)
    grounded_fields: List[GroundedField] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "header_fields": self.header_fields,
            "sections": self.sections,
            "elements_a_valider": [e.to_dict() for e in self.elements_a_valider],
            "grounded_fields": [g.to_dict() for g in self.grounded_fields],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExtractedNote":
        if not isinstance(d, dict):
            raise ValueError("La note extraite doit être un objet JSON.")
        header_fields = {
            str(k).strip(): str(v).strip()
            for k, v in (d.get("header_fields") or {}).items()
            if isinstance(v, str) and v.strip()
        }
        sections = d.get("sections") or {}
        if not isinstance(sections, dict):
            sections = {}
        elements = [
            ElementAValider.from_dict(e) for e in (d.get("elements_a_valider") or []) if isinstance(e, dict)
        ]
        grounded = [
            GroundedField.from_dict(g) for g in (d.get("grounded_fields") or []) if isinstance(g, dict)
        ]
        return cls(header_fields=header_fields, sections=sections, elements_a_valider=elements, grounded_fields=grounded)
