"""
note_renderer.py — Passe 2 : rendu applicatif déterministe de la note clinique.
===============================================================================

POURQUOI CET ÉTAPE EXISTE
-------------------------
Dans un pipeling « deux passes », la PASSE 1 (LLM) extrait et corrige la
dictée en un objet structuré (medicaments, historique, examen, plan…) ; la
PASSE 2 — ici — reçoit cet objet et le met en page dans le gabarit choisi,
SANS modèle : c'est du code. Le LLM ne dépense donc plus aucun jeton à
reproduire la structure du gabarit, et ne peut ni inventer une rubrique ni
« remplir » une section vide : une rubrique sans contenu est retirée par ce
module, avec son titre, comme l'exige la consigne générale. La latence d'une
génération passe de « chaîne de Markdown générée » à « structure compacte ».

C'est exactement la division voulue : le modèle fait la partie médicalement
difficile (comprendre et corriger la dictée), le code répond mécaniquement
du squelette.

CE QUE LE MODULE GARANTIT
-------------------------
* les intitulés, l'ordre et le niveau de titre viennent TELS QUELS du gabarit ;
* une rubrique sans contenu disparaît (titre compris) ;
* une ligne d'en-tête sans valeur disparaît ;
* les listes de médicaments / antécédents / examen restent des listes, l'Impression
  et le Plan restent numérotés (détection par le gabarit, pas par devinette) ;
* chaque item de liste est normalisé : tout marqueur de tête que la passe 1
  aurait déjà émis (« - … », « 1. … ») est retiré avant re-marquage selon le
  gabarit — le rendu ne produit jamais « - - … » ni « 1. 1. … »
  (``_nettoyer_item``, idempotent) ;
* la ligne « Rédigé à l'aide de la reconnaissance vocale. » est conservée ;
* la rubrique finale « Corrections et éléments à valider » est toujours émise
  (vide → « Aucun élément à signaler. »).

CONTRAT DE LA PASSE 1
---------------------
``extraction`` est un objet avec trois clés :

  * ``en_tete``    — dict ``{libellé exact du gabarit : valeur}`` ; une clé
    absente ou vide retire la ligne correspondante ;
  * ``sections``   — dict ``{titre exact du gabarit : contenu}``, où contenu
    est l'une des formes suivantes :
       - ``str``               → paragraphe(s) (séparés par une ligne vide) ;
       - ``list[str]``         → liste à puces, OU numérotée pour les rubriques
         dont le gabarit porte un plan numéroté (Impression / Plan) ;
       - ``{"phrases": str, "items": list[str]}`` → rubrique mixte (résumé en
         paragraphes + éléments numérotés) — p. ex. Impression de la Gériatrie ;
       - ``{libellé : str, …}`` → bloc étiqueté (**AVQ :** …), p. ex. Autonomie
         fonctionnelle.
    Le titre de section est comparé INSENSIBLEMENT à la casse et aux accents,
    pour tolérer une petite variation du modèle sans jamais déraper.
  * ``corrections`` — liste de lignes (rubrique finale). Vide ou absente →
    « Aucun élément à signaler. ».

Une rubrique de la mise en page absente d'``extraction`` (clé inconnue, ``""``,
``None``, liste vide, dict vide) est SUPPRIMÉE : le rendu n'a jamais de contenu
inventé ni de texte de remplissage.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: Rubrique « Corrections… » : dernier titre de la mise en page (fr / en).
_CORRECTIONS_RE = re.compile(r"valid|vérif|verify|corrections", re.IGNORECASE)

#: Ligne d'en-tête ou libellé étiqueté : ``**Lieu de la consultation :**``.
#: Une succès éventuel ``{{DATE}}`` (forme historique) est ignoré : la valeur
#: vient de la passe 1, jamais d'un champ du gabarit.
_LABEL_RE = re.compile(r"^\*\*(.+?):\*\*\s*(?:\{\{[^}]*\}\})?\s*$")
#: Sous-titre de niveau 2+ : ``## Rubrique``. Le niveau 1 (`# Titre`) est le
#: titre du document, conservé tel quel par le rendu.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
#: Placeholder de liste à puces ``- {{…}}``.
_BULLET_RE = re.compile(r"^-\s*\{\{")
#: Placeholder de liste numérotée ``1. {{…}}``.
_NUMBERED_RE = re.compile(r"^\d+\.?\s*\{\{")
#: Placeholder de paragraphe ``{{Paragraphe 1}}``.
_PARAGRAPH_RE = re.compile(r"^\s*\{\{")
#: Ligne vide.
_EMPTY_RE = re.compile(r"^\s*$")
#: Tableau Markdown (les gabarits à tableaux ne sont pas rendus ici).
_TABLE_RE = re.compile(r"\|")
#: Marqueur de liste numérotée en tête d'item émis par le modèle (``1. …``).
_ORD_MARK_RE = re.compile(r"^\d+[.)]\s+")
#: Marqueur de liste à puces en tête d'item émis par le modèle (``- …``).
_BUL_MARK_RE = re.compile(r"^[-•*]\s+")


@dataclass
class Section:
    """Rubrique du gabarit, avec le style que sa mise en page impose."""

    title: str
    level: int
    bullet: bool = False
    numbered: bool = False
    paragraph: bool = False
    labeled: List[str] = field(default_factory=list)

    @property
    def vide(self) -> bool:
        return not (self.bullet or self.numbered or self.paragraph or self.labeled)


@dataclass
class Layout:
    """Vue parsée d'une mise en page : tout ce que le rendu a besoin de savoir."""

    title: Optional[str] = None
    header_labels: List[str] = field(default_factory=list)
    sections: List[Section] = field(default_factory=list)
    footers: List[str] = field(default_factory=list)
    corrections_header: Optional[str] = None
    compatible: bool = True


# ---------------------------------------------------------------------------
# Analyse de la mise en page
# ---------------------------------------------------------------------------
def _normaliser(texte: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (texte or "").lower())
    return " ".join("".join(c for c in nfkd if not unicodedata.combining(c)).split())


def _est_corrections(titre: str) -> bool:
    return bool(_CORRECTIONS_RE.search(titre or ""))


def inspect_layout(layout: str) -> Layout:
    """
    Analyse une mise en page une fois pour toutes (clés de la passe 1 + rendu).

    ``compatible`` devient False dès qu'un tableau Markdown apparaît ou qu'une
    ligne du gabarit ne se range dans aucune catégorie connue : le module ne
    sait pas la rendre fidèlement, l'appelant doit alors retomber sur la passe
    unique (LLM) plutôt que de produire une note tronquée.
    """
    resultat = Layout()
    lignes = (layout or "").splitlines()
    en_tete = True
    section_actuelle: Optional[Section] = None
    for ligne in lignes:
        if _TABLE_RE.search(ligne):
            resultat.compatible = False
            return resultat
        m_titre = _HEADING_RE.match(ligne)
        if m_titre:
            titre = m_titre.group(2).strip()
            if len(m_titre.group(1)) == 1 and resultat.title is None:
                # Titre du document (`# …`) : conservé tel quel, l'en-tête
                # étiqueté suit.
                resultat.title = titre
                continue
            if _est_corrections(titre) and resultat.corrections_header is None:
                resultat.corrections_header = titre
                # La rubrique des corrections clôt la mise en page : tout ce
                # qui suit (elle-même comprise) n'est pas du corps de note.
                section_actuelle = None
                en_tete = False
                continue
            section_actuelle = Section(
                title=titre, level=len(m_titre.group(1)),
            )
            resultat.sections.append(section_actuelle)
            en_tete = False
            continue
        if _EMPTY_RE.match(ligne):
            continue
        if en_tete:
            m_label = _LABEL_RE.match(ligne)
            if m_label:
                resultat.header_labels.append(m_label.group(1).strip())
            else:
                resultat.compatible = False
                return resultat
            continue
        if section_actuelle is None:
            # Après la rubrique des corrections, seul le vide est attendu.
            resultat.compatible = False
            return resultat
        m_label = _LABEL_RE.match(ligne)
        if m_label:
            section_actuelle.labeled.append(m_label.group(1).strip())
            continue
        if _BULLET_RE.match(ligne):
            section_actuelle.bullet = True
            continue
        if _NUMBERED_RE.match(ligne):
            section_actuelle.numbered = True
            continue
        if _PARAGRAPH_RE.match(ligne):
            section_actuelle.paragraph = True
            continue
        # Ligne libre (p. ex. « Rédigé à l'aide de la reconnaissance vocale. ») :
        # elle est conservée comme pied de note, pas comme contenu de rubrique.
        resultat.footers.append(ligne.strip())
    return resultat


def section_keys(layout: Optional[Layout]) -> Tuple[List[str], List[str]]:
    """(titres de rubriques, libellés d'en-tête) à communiquer à la passe 1."""
    if layout is None:
        return [], []
    return [s.title for s in layout.sections], list(layout.header_labels)


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------
def _trouver(contenu: dict, cle_raw: str):
    """Valeur brute d'une clé, insensible aux accents et à la casse."""
    cle_norm = _normaliser(cle_raw)
    for k, v in (contenu or {}).items():
        if _normaliser(str(k)) == cle_norm:
            return v
    return None


def _chercher(contenu: dict, cle: str) -> Optional[str]:
    """Valeur textuelle d'une clé étiquetée, ``None`` si absente ou vide."""
    valeur = _trouver(contenu, cle)
    if valeur is None:
        return None
    texte = str(valeur).strip()
    return texte or None


def _nettoyer_item(item: str) -> str:
    """Item de liste sans marqueur de tête que le modèle aurait déjà émis.

    Le LLM de la passe 1 répond parfois des items DÉJÀ marqués (« - Alerte… »,
    « 1. Trouble… ») malgré la consigne du « rendu par l'application ». Or le
    renderer re-préfixe chaque item (puce ``- `` ou numéro ``N. ``) selon le
    gabarit : sans nettoyage, le rendu aboutit à « - - … » / « 1. 1. … »
    (observé sur la Note 8, modèle gemma — rubriques Examen, Investigation,
    Impression, Plan). On retire donc tout marqueur de tête (numéroté, puis
    puce — au besoin répété, un modèle peut émettre « - 1. … ») et on aplatit
    les sauts de ligne : un item est TOUJOURS une ligne unique. La
    normalisation est idempotente : nettoyer puis re-marquer donne exactement
    un marqueur, quel que soit le comportement du modèle.
    """
    texte = " ".join((item or "").split())
    while True:
        avant = texte
        texte = _ORD_MARK_RE.sub("", texte)
        texte = _BUL_MARK_RE.sub("", texte)
        if texte == avant:
            break
    return texte


def _paragraphes(texte: str) -> List[str]:
    propre = "\n".join(
        " ".join(l.split()) for l in (texte or "").splitlines() if l.strip()
    )
    blocs = [b.strip() for b in propre.split("\n\n") if b.strip()]
    return blocs


def _rendre_contenu(section: Section, contenu) -> Optional[str]:
    """Rend le contenu d'une rubrique, ``None`` s'il n'y a rien à afficher."""
    if contenu is None:
        return None
    if isinstance(contenu, dict):
        if section.labeled:
            lignes = []
            for libelle in section.labeled:
                valeur = _chercher(contenu, libelle)
                if valeur:
                    lignes.append(f"**{libelle} :** {valeur}")
            return "\n".join(lignes) if lignes else None
        # Rubrique mixte (paragraphe + éléments) — convention « phrases/items ».
        blocs: List[str] = []
        phrases = contenu.get("phrases") or contenu.get("resume") or ""
        items = contenu.get("items")
        if str(phrases).strip():
            blocs.extend(_paragraphes(str(phrases)))
        if items:
            propres = [_nettoyer_item(x) for x in items if str(x).strip()]
            if section.numbered:
                blocs.append("\n".join(f"{i}. {x}" for i, x in enumerate(propres, 1)))
            else:
                blocs.append("\n".join(f"- {x}" for x in propres))
        return _joindre(blocs) if blocs else None
    if isinstance(contenu, list):
        propres = [_nettoyer_item(x) for x in contenu if str(x).strip()]
        if not propres:
            return None
        if section.numbered:
            return "\n".join(f"{i}. {x}" for i, x in enumerate(propres, 1))
        if section.bullet or section.vide:
            return "\n".join(f"- {x}" for x in propres)
        return _joindre(_paragraphes("\n\n".join(propres)))
    texte = str(contenu).strip()
    if not texte:
        return None
    if section.bullet or section.numbered:
        items = [_nettoyer_item(x) for x in texte.splitlines() if x.strip()]
        if len(items) > 1:
            if section.numbered:
                return "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1))
            return "\n".join(f"- {x}" for x in items)
        if section.numbered:
            return f"1. {_nettoyer_item(texte)}"
        return f"- {_nettoyer_item(texte)}"
    return _joindre(_paragraphes(texte))


def _joindre(blocs: List[str]) -> str:
    """Sépare chaque bloc par une ligne vide — jamais entre deux puces."""
    return "\n\n".join(b for b in blocs if b.strip())


def _defaut_corrections(langue: str) -> str:
    return (
        "Aucun élément à signaler."
        if not str(langue).lower().startswith("en")
        else "Nothing to report."
    )


def render_note(
    layout: str,
    extraction: dict,
    langue: str = "fr",
) -> str:
    """
    Produit la note Markdown en appliquant ``extraction`` au gabarit ``layout``.

    ``extraction`` respecte le contrat documenté en tête de module ; toute
    rubrique absente ou vide y est supprimée. La rubrique « Corrections et
    éléments à valider » est éjectée en fin de note, sous l'intitulé du gabarit
    (ou son défaut), prête à être extraite par ``main.split_corrections``.
    """
    parsed = inspect_layout(layout)
    sortie: List[str] = []

    # --- Titre du document (`# …`), tel quel ---
    if parsed.title:
        sortie.append("# " + parsed.title)

    # --- En-tête : seules les lignes dont la valeur a été dictée ---
    entete = extraction.get("en_tete") or {}
    if not isinstance(entete, dict):
        entete = {}
    lignes_entete = [
        f"**{libelle} :** {valeur}"
        for libelle in parsed.header_labels
        if (valeur := _chercher(entete, libelle))
    ]
    if lignes_entete:
        sortie.append("\n".join(lignes_entete))

    # --- Corps : rubriques du gabarit, dans l'ordre ---
    sections = extraction.get("sections") or {}
    if not isinstance(sections, dict):
        sections = {}
    for section in parsed.sections:
        contenu = _trouver(sections, section.title)
        if contenu is None and section.title in sections:
            contenu = sections[section.title]
        rendu = _rendre_contenu(section, contenu)
        if not rendu:
            continue
        sortie.append("#" * section.level + " " + section.title + "\n" + rendu)

    # --- Pied de note conservé tel quel (p. ex. la mention de reconnaissance) ---
    if parsed.footers:
        sortie.append("\n".join(parsed.footers))

    # --- Rubrique finale « Corrections et éléments à valider », jamais omise ---
    consigne = extraction.get("corrections")
    items = [x.strip() for x in consigne] if isinstance(consigne, list) else []
    titre = parsed.corrections_header or (
        "Corrections and items to verify"
        if str(langue).lower().startswith("en")
        else "Corrections et éléments à valider"
    )
    bloc_corr = "## " + titre
    if items:
        # Liste pointée : un tiret par élément de correction. Le modèle groupe
        # parfois plusieurs rubriques sur une seule entrée, séparées par « ; » ;
        # on refend chaque entrée au point-virgule qui précède une nouvelle
        # rubrique « […] » pour obtenir une puce par correction, sans jamais
        # casser un « ; » présent dans un contexte d'extrait.
        puces: List[str] = []
        for x in items:
            morceaux = re.split(r";\s*(?=\[)", x)
            puces.extend(m.strip() for m in morceaux if m.strip())
        bloc_corr += "\n" + "\n".join("- " + p for p in puces)
    else:
        bloc_corr += "\n" + _defaut_corrections(langue)
    sortie.append(bloc_corr)

    return _joindre(sortie) or ""