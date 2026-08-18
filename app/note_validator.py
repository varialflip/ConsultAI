"""
note_validator.py — vérifications mécaniques sur la note extraite (JSON).
===================================================================================

Tourne sur ``ExtractedNote`` (avant rendu), jamais sur le markdown final —
voir la brique de rendu (``note_renderer``), qui applique déjà mécaniquement
la plupart des règles de mise en forme (grammaire d'Éléments à valider,
suppression des champs vides, etc.) : ce module vérifie ce que le RENDU ne
peut pas garantir de lui-même, parce que ça dépend du CONTENU produit par le
modèle, pas de la structure.

Chaque vérification retourne des ``ValidationIssue`` :
  - ``auto_fixed``   : corrigé ici même, sans appel modèle (ex. valeur de
    remplissage retirée) — informatif, ne bloque rien.
  - ``needs_repair``  : nécessite un appel de réparation ciblé (voir le
    plafond de tentatives dans note_extraction) ou, à défaut, un repli
    déterministe (« à confirmer », ou champ retiré).
  - ``blocked``       : ne doit jamais atteindre le médecin tel quel si la
    réparation échoue (ex. Éléments à valider vide).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import List, Tuple

from app.note_schema import ExtractedNote, LayoutSpec

# ---------------------------------------------------------------------------
# Résultat
# ---------------------------------------------------------------------------


@dataclass
class ValidationIssue:
    severity: str  # "auto_fixed" | "needs_repair" | "blocked"
    code: str
    message: str
    path: str


@dataclass
class ValidationResult:
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def needs_repair(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "needs_repair"]

    @property
    def blocked(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "blocked"]

    @property
    def ok(self) -> bool:
        return not self.needs_repair and not self.blocked


# ---------------------------------------------------------------------------
# Parcours générique des valeurs de section (str imbriquées dans des dict)
# ---------------------------------------------------------------------------


def _iter_leaf_strings(value: object, path: str) -> List[Tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        out: List[Tuple[str, str]] = []
        for k, v in value.items():
            out.extend(_iter_leaf_strings(v, f"{path}.{k}"))
        return out
    if isinstance(value, list):
        out = []
        for i, item in enumerate(value):
            if isinstance(item, str):
                out.append((f"{path}[{i}]", item))
        return out
    return []


def _all_leaf_strings(note: ExtractedNote) -> List[Tuple[str, str]]:
    out = [(f"header_fields.{k}", v) for k, v in note.header_fields.items()]
    for k, v in note.sections.items():
        out.extend(_iter_leaf_strings(v, f"sections.{k}"))
    return out


_INDEX_RE = re.compile(r"^(.*)\[(\d+)\]$")


def _get_container(note: ExtractedNote, dotted_path: str) -> object:
    """Résout un chemin ``sections.A.B`` (ou ``sections``) vers le
    conteneur (dict/list) à cet endroit. Ne gère pas ``header_fields``."""
    parts = dotted_path.split(".")
    container: object = note.sections
    for part in parts[1:]:
        if not isinstance(container, dict):
            return None
        container = container.get(part)
    return container


def _set_leaf(note: ExtractedNote, path: str) -> None:
    """Retire la valeur au chemin donné (``header_fields.X``,
    ``sections.A.B`` ou ``sections.A[2]``) — l'auto-fix « champ sans valeur
    perd sa ligne »."""
    m = _INDEX_RE.match(path)
    if m:
        container = _get_container(note, m.group(1))
        idx = int(m.group(2))
        if isinstance(container, list) and 0 <= idx < len(container):
            container.pop(idx)
        return
    parts = path.split(".")
    if parts[0] == "header_fields":
        note.header_fields.pop(parts[1], None)
        return
    parent = _get_container(note, ".".join(parts[:-1]))
    if isinstance(parent, dict):
        parent.pop(parts[-1], None)


# ---------------------------------------------------------------------------
# 1bis. Type de valeur inattendu (ni str, ni liste de str, ni dict) — jamais
#       fait disparaître silencieusement, toujours signalé.
# ---------------------------------------------------------------------------


def _iter_section_values(value: object, path: str) -> List[Tuple[str, object]]:
    out: List[Tuple[str, object]] = [(path, value)]
    if isinstance(value, dict):
        for k, v in value.items():
            out.extend(_iter_section_values(v, f"{path}.{k}"))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            out.append((f"{path}[{i}]", item))
    return out


def check_section_types(note: ExtractedNote) -> List[ValidationIssue]:
    issues = []
    for key, value in note.sections.items():
        for path, v in _iter_section_values(value, f"sections.{key}"):
            if isinstance(v, (dict, list)) or v is None:
                continue
            if not isinstance(v, str):
                issues.append(
                    ValidationIssue(
                        "needs_repair", "unexpected_value_type",
                        f"Valeur de type inattendu ({type(v).__name__} : {v!r}) — une chaîne était attendue.",
                        path,
                    )
                )
    return issues


# ---------------------------------------------------------------------------
# 1. Texte de remplissage interdit — auto-fixé (retrait du champ)
# ---------------------------------------------------------------------------

# Liste ouverte : un modèle plus faible invente volontiers sa propre
# formulation plutôt que de reprendre exactement les exemples de la consigne
# (« Non servi », « Non abordé »...) — élargi après un cas réel où le modèle
# a écrit « non dictée » en valeur de champ (test.dictai.ca, 2026-08-18).
_FILLER_RE = re.compile(
    r"^("
    r"non[\s-]+(servi|abord[ée]e?|dict[ée]e?|pr[ée]cis[ée]e?|renseign[ée]e?|"
    r"sp[ée]cifi[ée]e?|mentionn[ée]e?|fourni[ée]?|disponible|applicable|"
    r"communiqu[ée]e?)s?"
    r"|inconnue?s?|non[\s-]+connue?s?|[àa]\s+d[ée]terminer|sans\s+objet"
    r"|aucune?\s+information|non[\s-]+applicable|n/?[ad]"
    r"|not[\s-]+(stated|provided|specified|addressed|applicable|available|dictated|mentioned)"
    r"|unknown|n/?a|none\s+provided|not\s+given"
    r"|—|-"
    r")$",
    re.IGNORECASE,
)


def check_forbidden_filler(note: ExtractedNote) -> List[ValidationIssue]:
    """Mute ``note`` (retire les champs de remplissage) et retourne les
    problèmes trouvés, en ``auto_fixed``."""
    issues: List[ValidationIssue] = []
    for path, value in _all_leaf_strings(note):
        if _FILLER_RE.match(value.strip()):
            _set_leaf(note, path)
            issues.append(
                ValidationIssue(
                    "auto_fixed", "filler_value",
                    f"Valeur de remplissage « {value.strip()} » retirée (règle : champ sans valeur dictée = ligne absente).",
                    path,
                )
            )
    return issues


# ---------------------------------------------------------------------------
# 2. Accolades de gabarit oubliées / balises HTML — jamais auto-fixé
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{\{.*?\}\}")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def check_placeholder_leftover(note: ExtractedNote) -> List[ValidationIssue]:
    issues = []
    for path, value in _all_leaf_strings(note):
        if _PLACEHOLDER_RE.search(value):
            issues.append(ValidationIssue("needs_repair", "placeholder_leftover", "Accolade de gabarit non remplacée ({{...}}).", path))
    return issues


def check_html_tags(note: ExtractedNote) -> List[ValidationIssue]:
    issues = []
    for path, value in _all_leaf_strings(note):
        if _HTML_TAG_RE.search(value):
            issues.append(ValidationIssue("needs_repair", "html_markup", "Balise HTML détectée (interdite — markdown simple uniquement).", path))
    return issues


# ---------------------------------------------------------------------------
# 2bis. Liste numérotée/à puces écrasée sur une seule ligne — jamais auto-fixé
#       (regrouper les items nécessite de comprendre où ils se coupent).
# ---------------------------------------------------------------------------

# Deux marqueurs « N. » (ou plus) sur la MÊME ligne : signe qu'un modèle a
# écrit « 1. X. 2. Y. 3. Z. » à la suite sans saut de ligne entre les items,
# au lieu d'encoder chaque item séparément (voir note_extraction.py, la
# consigne de mise en forme JSON). Vu réellement, test.dictai.ca 2026-08-18,
# mistral-small-latest : Impression/Plan rendus en un seul bloc au lieu
# d'une liste numérotée.
_CRAMPED_NUMBERED_LIST_RE = re.compile(r"(?:^|[.!?]\s+)\d+\.\s+\S.*?[.!?]\s+\d+\.\s")


def check_cramped_lists(note: ExtractedNote) -> List[ValidationIssue]:
    issues = []
    for path, value in _all_leaf_strings(note):
        for line in value.split("\n"):
            if _CRAMPED_NUMBERED_LIST_RE.search(line):
                issues.append(
                    ValidationIssue(
                        "needs_repair", "cramped_numbered_list",
                        "Plusieurs items numérotés (« 1. ... 2. ... ») trouvés sur une seule ligne — "
                        "doivent être séparés par un vrai saut de ligne, un par item.",
                        path,
                    )
                )
                break
    return issues


# ---------------------------------------------------------------------------
# 3. Éléments à valider — présence + cohérence des items
# ---------------------------------------------------------------------------

# Le modèle écrit parfois « à confirmer » (ou une variante) DANS le champ
# ``correction`` lui-même, au lieu de le laisser vide — ce qui produit la
# phrase absurde « → correction apportée : à confirmer » (vu réellement,
# test.dictai.ca, 2026-08-18, mistral-small-latest). Auto-fixé : équivalent à
# ne pas avoir renseigné de correction.
_META_UNCONFIRMED_RE = re.compile(
    r"^([àa]\s+confirmer|unconfirmed|inconnue?|tbd|\?+|non[\s-]+confirm[ée]e?)$",
    re.IGNORECASE,
)


def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().rstrip(".")).casefold()


def fix_elements_a_valider_corrections(note: ExtractedNote) -> List[ValidationIssue]:
    """Mute ``note.elements_a_valider`` : retire les auto-corrections « no-op »
    (correction identique au terme dicté — aucune information, seulement du
    bruit) et démet en « à confirmer » les corrections dont la valeur est
    elle-même un mot-clé d'incertitude plutôt qu'une lecture réelle."""
    issues: List[ValidationIssue] = []
    kept: List[ElementAValider] = []
    for i, e in enumerate(note.elements_a_valider):
        if e.kind == "item" and e.correction:
            if _META_UNCONFIRMED_RE.match(e.correction.strip()):
                issues.append(
                    ValidationIssue(
                        "auto_fixed", "correction_is_meta_word",
                        f"« {e.correction} » utilisé comme correction au lieu d'une lecture — traité comme à confirmer.",
                        f"elements_a_valider[{i}]",
                    )
                )
                e.correction = None
            elif _normalize_text(e.correction) == _normalize_text(e.terme_dicte):
                issues.append(
                    ValidationIssue(
                        "auto_fixed", "correction_is_noop",
                        f"« {e.terme_dicte} » signalé « corrigé » vers un texte identique — retiré (aucune information).",
                        f"elements_a_valider[{i}]",
                    )
                )
                continue  # ne pas conserver cet élément
        kept.append(e)
    note.elements_a_valider = kept
    return issues


def check_elements_a_valider(note: ExtractedNote, layout: LayoutSpec) -> List[ValidationIssue]:
    if not layout.has_elements_a_valider:
        return []
    issues = []
    if not note.elements_a_valider:
        issues.append(
            ValidationIssue(
                "blocked", "elements_a_valider_empty",
                "Éléments à valider est vide — cette rubrique ne doit jamais être omise ni vidée.",
                "elements_a_valider",
            )
        )
        return issues
    for i, e in enumerate(note.elements_a_valider):
        if e.kind == "item" and not e.terme_dicte.strip():
            issues.append(ValidationIssue("needs_repair", "elements_a_valider_malformed", "Élément sans terme dicté.", f"elements_a_valider[{i}]"))
        if e.kind == "group" and not e.texte_groupe.strip():
            issues.append(ValidationIssue("needs_repair", "elements_a_valider_malformed", "Regroupement sans texte.", f"elements_a_valider[{i}]"))
    return issues


# ---------------------------------------------------------------------------
# 4. Clause épistémique (Impression / Plan) — préservation du « je crois »
# ---------------------------------------------------------------------------

_EPISTEMIC_MARKERS_FR = [
    "je crois", "je pense", "je considère", "j'estime", "je soupçonne",
    "il me semble", "à mon avis", "je retiens que", "je privilégie", "je doute que",
]
_EPISTEMIC_MARKERS_EN = [
    "i believe", "i think", "i consider", "i suspect", "it seems to me",
    "in my opinion", "i favor", "i doubt that",
]
_EPISTEMIC_SCOPE_SECTIONS = ("IMPRESSION", "PLAN")


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _split_sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _has_marker(sentence: str, markers: List[str]) -> bool:
    normalized = _strip_accents(sentence.lower())
    return any(m in normalized for m in markers)


def _jaccard(a: str, b: str) -> float:
    ta = set(_strip_accents(a.lower()).split())
    tb = set(_strip_accents(b.lower()).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def check_epistemic_clauses(note: ExtractedNote, transcript: str, language: str = "fr") -> List[ValidationIssue]:
    markers = _EPISTEMIC_MARKERS_EN if language == "en" else _EPISTEMIC_MARKERS_FR
    hedge_sentences = [s for s in _split_sentences(transcript) if _has_marker(s, markers)]
    if not hedge_sentences:
        return []

    rendered_sentences: List[str] = []
    for section_label in _EPISTEMIC_SCOPE_SECTIONS:
        value = note.sections.get(section_label)
        for _, text in _iter_leaf_strings(value, section_label):
            rendered_sentences.extend(_split_sentences(text))

    issues: List[ValidationIssue] = []
    for hedge in hedge_sentences:
        best_sentence, best_score = None, 0.0
        for candidate in rendered_sentences:
            score = _jaccard(hedge, candidate)
            if score > best_score:
                best_sentence, best_score = candidate, score
        if best_sentence is not None and best_score > 0.3 and not _has_marker(best_sentence, markers):
            issues.append(
                ValidationIssue(
                    "needs_repair", "epistemic_clause_dropped",
                    f"Le médecin dicte une opinion à la première personne (« {hedge[:80]} ») mais la phrase "
                    f"correspondante dans Impression/Plan (« {best_sentence[:80]} ») ne la préserve pas.",
                    "sections.IMPRESSION|PLAN",
                )
            )
    return issues


# ---------------------------------------------------------------------------
# 5. Ancrage (grounding) des champs critiques
# ---------------------------------------------------------------------------


def _best_match_ratio(needle: str, haystack: str) -> float:
    needle = needle.strip()
    if not needle:
        return 0.0
    if needle.lower() in haystack.lower():
        return 1.0
    window = max(len(needle), 8)
    step = max(1, window // 2)
    best = 0.0
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(needle.lower())
    for start in range(0, max(1, len(haystack) - window + 1), step):
        matcher.set_seq1(haystack[start:start + window].lower())
        best = max(best, matcher.ratio())
        if best > 0.92:
            break
    return best


def check_grounding(note: ExtractedNote, transcript: str, threshold: float = 0.72) -> List[ValidationIssue]:
    issues = []
    for i, gf in enumerate(note.grounded_fields):
        if not gf.value:
            continue  # rien affirmé, rien à ancrer
        if not gf.source_span:
            issues.append(
                ValidationIssue("needs_repair", "grounding_missing_span", f"Champ « {gf.field} » = « {gf.value} » sans source_span.", f"grounded_fields[{i}]")
            )
            continue
        ratio = _best_match_ratio(gf.source_span, transcript)
        if ratio < threshold:
            issues.append(
                ValidationIssue(
                    "needs_repair", "grounding_mismatch",
                    f"Champ « {gf.field} » : source_span « {gf.source_span} » introuvable dans la transcription (similarité {ratio:.2f}).",
                    f"grounded_fields[{i}]",
                )
            )
    return issues


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------


def validate(note: ExtractedNote, layout: LayoutSpec, transcript: str, language: str = "fr") -> ValidationResult:
    """Applique les auto-fix (mute ``note``) puis retourne tous les problèmes,
    fixes compris (pour traçabilité) — les non « auto_fixed » restent à
    traiter par un appel de réparation ciblé ou un repli déterministe."""
    issues: List[ValidationIssue] = []
    issues += check_section_types(note)
    issues += check_forbidden_filler(note)  # auto-fix, mute `note`
    issues += fix_elements_a_valider_corrections(note)  # auto-fix, mute `note`
    issues += check_placeholder_leftover(note)
    issues += check_html_tags(note)
    issues += check_cramped_lists(note)
    issues += check_elements_a_valider(note, layout)
    issues += check_epistemic_clauses(note, transcript, language)
    issues += check_grounding(note, transcript)
    return ValidationResult(issues=issues)
