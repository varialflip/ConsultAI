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

from typing import List

from app.note_schema import ElementAValider, ExtractedNote, LayoutSpec


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


def _render_heading(layout: LayoutSpec, label: str, level: int, container: object) -> List[str]:
    """Rend une rubrique et ses sous-rubriques/champs ; [] si rien à dire —
    auquel cas l'appelant n'émet PAS le titre non plus (rubrique retirée)."""
    value = container.get(label) if isinstance(container, dict) else None

    child_headings = [e for e in layout.entries if e.kind == "heading" and e.parent == label]
    child_fields = [e for e in layout.entries if e.kind == "bold_field" and e.parent == label]

    body: List[str] = []

    if child_headings:
        sub_container = value if isinstance(value, dict) else {}
        for child in child_headings:
            sub_body = _render_heading(layout, child.label, child.level, sub_container)
            if sub_body:
                if body:
                    body.append("")
                body.extend(sub_body)
    elif child_fields:
        sub_container = value if isinstance(value, dict) else {}
        for f in child_fields:
            v = sub_container.get(f.label) if isinstance(sub_container, dict) else None
            if isinstance(v, str) and v.strip():
                body.append(f"**{f.label} :** {v.strip()}")
    else:
        # Prose (str), liste à puces (List[str] — le gabarit demande souvent
        # une « liste pointée », ex. médication, examen physique), ou un
        # type inattendu (nombre, booléen...) qu'on affiche tel quel plutôt
        # que de le faire disparaître silencieusement : une extraction
        # malformée doit être visible, jamais invisible.
        if isinstance(value, list):
            body.extend(f"- {str(item).strip()}" for item in value if str(item).strip())
        elif isinstance(value, str) and value.strip():
            body.append(value.strip())
        elif value not in (None, "", [], {}):
            body.append(str(value).strip())

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
