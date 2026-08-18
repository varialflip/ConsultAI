"""
note_renderer.py — ExtractedNote + LayoutSpec -> markdown final, en code pur.
===================================================================================

Aucun appel modèle ici. Le rendu applique mécaniquement les règles qui, dans
l'ancien pipeline (une seule passe LLM -> markdown), dépendaient du modèle et
donc pouvaient être oubliées :

- une rubrique/un champ sans valeur dictée est simplement absent (jamais
  laissé vide, jamais rempli d'un texte de remplissage) ;
- les lignes fixes du gabarit (ex. « Rédigé à l'aide de la reconnaissance
  vocale. ») sont reproduites telles quelles, jamais reformulées par le
  modèle ;
- la grammaire d'Éléments à valider (« terme dicté → **correction apportée :
  X** » / « terme dicté → **à confirmer** ») est imposée par ce code, pas par
  une consigne que le modèle pourrait mal suivre.
"""

from __future__ import annotations

import re
from typing import List

from app.note_schema import ElementAValider, ExtractedNote, LayoutSpec

#: Un modèle plus faible écrit parfois lui-même « 1. », « 2) », « - », « • »
#: au début d'un item de tableau JSON malgré la consigne de ne pas le faire
#: (voir note_extraction._JSON_FORMAT_INSTRUCTIONS_*) — sans ce nettoyage, un
#: item numéroté par le modèle ET par ce rendu produit une double
#: numérotation (« 2. 1. Trouble délirant... »). Retiré inconditionnellement,
#: y compris pour les listes à puces : un item qui s'auto-numérote dans une
#: liste à puces est le même bogue.
_LEADING_LIST_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-•])\s+")


def render(note: ExtractedNote, layout: LayoutSpec) -> str:
    lines: List[str] = [f"# {layout.title}"]

    header_lines = [
        f"**{label} :** {note.header_fields[label]}"
        for label in layout.top_level_fields()
        if note.header_fields.get(label)
    ]
    if header_lines:
        lines.append("")
        lines.extend(header_lines)

    for heading in [e for e in layout.entries if e.kind == "heading" and e.parent is None]:
        body = _render_heading(layout, heading.label, heading.level, note.sections)
        if body:
            lines.append("")
            lines.extend(body)

    # Texte fixe du gabarit (ex. « Rédigé à l'aide de la reconnaissance
    # vocale. ») : niveau document, indépendant du contenu des rubriques —
    # une rubrique vide (PLAN sans contenu dicté, par ex.) ne doit jamais
    # l'emporter avec elle en disparaissant.
    doc_literals = layout.literals_of(None)
    if doc_literals:
        lines.append("")
        lines.extend(doc_literals)

    # Ne fabrique jamais un texte de remplissage : si l'extraction n'a
    # produit aucun élément, la rubrique est omise plutôt qu'inventée — le
    # validateur (note_validator.check_elements_a_valider) marque déjà ce cas
    # « blocked » : c'est à l'appelant de ne pas finaliser la note ainsi.
    if layout.has_elements_a_valider and note.elements_a_valider:
        lines.append("")
        lines.append("## ÉLÉMENTS À VALIDER")
        lines.append("")
        lines.extend(_render_elements_a_valider(note.elements_a_valider))

    return "\n".join(lines).rstrip() + "\n"


#: Clé réservée : le contenu dicté DIRECTEMENT sous une rubrique qui a par
#: ailleurs des sous-rubriques/champs (ex. MÉDICATION ACTUELLE contient sa
#: propre liste de médicaments ET une sous-rubrique ### ALLERGIES — les deux
#: à la fois, pas l'un OU l'autre). Improbable en collision avec un vrai
#: intitulé de rubrique/champ (double soulignement, jamais utilisé dans un
#: gabarit).
OWN_CONTENT_KEY = "__contenu__"


def _render_own_content(value: object, style: str = "bulleted") -> List[str]:
    """Rend une valeur de rubrique feuille : prose (str), liste (List[str] —
    le gabarit demande souvent une « liste pointée » ou « numérotée », ex.
    médication, plan — voir LayoutSpec.list_style), ou un type inattendu
    (nombre, booléen...) qu'on affiche tel quel plutôt que de le faire
    disparaître silencieusement — une extraction malformée doit être
    visible, jamais invisible. Le numérotage est TOUJOURS fait ici, jamais
    par le modèle : un item sur deux perdant son numéro, ou une numérotation
    qui repart à 1, sont des erreurs de modèle que le code ne peut pas
    laisser passer."""
    if isinstance(value, list):
        items = [
            _LEADING_LIST_MARKER_RE.sub("", str(item).strip()).strip()
            for item in value if str(item).strip()
        ]
        items = [item for item in items if item]
        if style == "numbered":
            return [f"{i}. {item}" for i, item in enumerate(items, start=1)]
        return [f"- {item}" for item in items]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if value not in (None, "", [], {}):
        return [str(value).strip()]
    return []


def _render_heading(layout: LayoutSpec, label: str, level: int, container: object) -> List[str]:
    """Rend une rubrique — son propre contenu ET ses sous-rubriques/champs,
    les deux à la fois si les deux sont présents ; [] si rien à dire —
    auquel cas l'appelant n'émet PAS le titre non plus (rubrique retirée)."""
    value = container.get(label) if isinstance(container, dict) else None
    style = layout.list_style(label)

    child_headings = [e for e in layout.entries if e.kind == "heading" and e.parent == label]
    child_fields = [e for e in layout.entries if e.kind == "bold_field" and e.parent == label]

    body: List[str] = []

    if child_headings or child_fields:
        sub_container = value if isinstance(value, dict) else {}
        own_value = sub_container.get(OWN_CONTENT_KEY) if isinstance(sub_container, dict) else None
        body.extend(_render_own_content(own_value, style))

        for f in child_fields:
            v = sub_container.get(f.label) if isinstance(sub_container, dict) else None
            if isinstance(v, str) and v.strip():
                body.append(f"**{f.label} :** {v.strip()}")

        for child in child_headings:
            sub_body = _render_heading(layout, child.label, child.level, sub_container)
            if sub_body:
                if body:
                    body.append("")
                body.extend(sub_body)
    else:
        body.extend(_render_own_content(value, style))

    if not body:
        return []

    heading_line = "#" * level + " " + label
    return [heading_line, ""] + body


_TELEGRAPHIC = {
    "correction": lambda text: f"**correction apportée : {text}**",
    "unconfirmed": "**à confirmer**",
}


def _render_elements_a_valider(elements: List[ElementAValider]) -> List[str]:
    # Appelée seulement quand `elements` est non vide — voir render().
    out: List[str] = []
    for e in elements:
        if e.kind == "group":
            if e.texte_groupe:
                out.append(f"- {e.texte_groupe}")
            continue
        mention = _TELEGRAPHIC["correction"](e.correction) if e.is_confirmed_reading else _TELEGRAPHIC["unconfirmed"]
        out.append(f"- {e.terme_dicte} → {mention}")
    return out
