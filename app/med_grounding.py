#!/usr/bin/env python3
"""Medicine grounding engine — ConsultAI (module applicatif).

Port déterministe et auditable de ``med_grounding/match_meds.py`` (le dépôt
garde les scripts de régénération de la base). Normalise les noms de
médicaments déformés par la reconnaissance vocale contre la base canadienne
BDP (`meds.sqlite`, livrée dans l'image).

Signal primaire  : correspondance orthographique floue (Levenshtein à repli
                   d'accents) sur les noms de marques/génériques DPD.
Signal secondaire: G2P français → arbre BK de phonèmes (optionnel, bruité).
Scoring          : exact 100 ; sinon PHONETIC + ANCHOR + POSOLOGY, demande un
                   signal de contexte dans la prose narrative, seuil S ≥ 65.

Ce module n'a AUCUNE dépendance ORM/réseau : il est importable partout
(dictée, génération, tâches de fond) et thread-safe (un seul ``Matcher``
singleton, en lecture seule après construction).
"""
from __future__ import annotations

import functools
import os
import re
import sqlite3
import threading
import unicodedata

#: Levenshtein accéléré en C (rapidfuzz). Optionnel au démarrage : l'image doit
#: être reconstruite avec `rapidfuzz` (voir requirements.txt) pour que la
#: correction des médicaments fonctionne, mais une instance non reconstruite
#: doit continuer de DÉMARRER (fonctionnalité simplement désactivée, voir
#: ``matcher()``).
try:
    from rapidfuzz.distance import Levenshtein
    _RAPIDFUZZ_OK = True
except Exception:  # pragma: no cover — dépendance manquante (image à reconstruire)
    Levenshtein = None
    _RAPIDFUZZ_OK = False

#: Base BDP livrée avec l'application. ``meds.sqlite`` est au côté du module
#: (Dockerfile: ``COPY app/ /app/app/``) ; le faire pointer vers le répertoire
#: courant du module (et non le CWD) le rend invariable quel que soit l'endroit
#: d'où le serveur est lancé.
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meds.sqlite")
PHONETIC_WEIGHT = 40
ANCHOR_WEIGHT   = 25
POSOLOGY_WEIGHT = 25
THRESHOLD = 65
ORTHO_FLOOR = 0.62     # ortho similarity required before signals may push over
MAX_LEN_DIFF = 4       # only compare aliases within +/-4 chars of the token
PROACTIVE_ORTHO_FLOOR = 0.68  # min ortho sim for a proactive multi-token join
MIN_FUZZY_LEN = 5      # tokens shorter than this are exact-match only (no fuzzy):
                       # 3-4 char prose collisions (nose/pen/sen/dose) burn the
                       # surrounding narrative; short real drug names are rare and
                       # their garble is unrecoverable by ortho anyway.
HIGH_SIM = 0.80       # near-certain fuzzy matches; exempt from context signals

#: Seuil de confiance mot-à-mot (STT ``words[].confidence``) : au-dessus, une
#: substitution orthographique *floue* d'un token est refusée quand il n'est
#: porté ni par une dose ni par un ancre ni par une région médicament
#: confirmée. Mesuré sur 8 dictées réelles (Cohere MLX `words[]`) : 0.92
#: bloque les faux positifs de prose flous (alcool 1.00, laisse 1.00, diabète
#: 0.996, d'autres 1.00) tout en conservant les vrais garbles de la zone
#: 0.55–0.92 (pivale, neurontain, tilénol, dexilan, l'épival 0.917) et ceux
#: portés par une dose ou un ancre (l'aldol+PRN, aspirine+80, oxycontin+210).
#: En dessous de 0.92 les vrais garbles hors contexte (l'épival à 0.917,
#: ketapine 0.78, Dapaglyflosine 0.76) passeraient, mais rejeter dès 0.90 les
#: ferait tomber. 0.92 est le point de bascule mesuré.
CONF_HARD_FLOOR = 0.92

#: Seuil de « prose sûre » : en deçà, un jeton est réputé DOUTEUX (il mérite
#: une piste phonétique même sans dose) ; au-delà, il est réputé CONFIANT pour
#: le STT. Combindé à une résolution EXACTE (le mot est déjà bien écrit, aucune
#: corréction à proposer) et hors région de liste confirmée, un confident
#: n'est pas suggéré comme médicament — c'est de la prose sûre (« Air Canada »
#: → pas de suggestion « air »).
#:
#: ⚠ CRITÈRE ARBITRAIRE (calibré sur les transcripts réels disponibles) :
#:   * max. observé d'un vrai nom déformé méritant correction = 0.928
#:     (lanzapine, consult 23) ;
#:   * min. observé d'un vrai médicament non déformé en liste = 0.952
#:     (Hydrocortisone 10 mg BID, consult 22) ;
#:   * faux positif cible « Air Canada » = 0.998.
#: Le creux 0.928↔0.952 retient 0.95, mais il n'est PAS issu d'une étude
#: statistique : à re-caliibrer dès que le corpus grossit (cf. README § 7.8).
CONF_PROSE_SURE = 0.95

#: Prose structurante à ne JAMAIS proposer comme candidat phonétique (léxique
#: réduit, réservé à la DÉTECTION de hints — ne modifie pas ``FRENCH_STOP``,
#: qui reste le garde de la réécriture inline).
_HINTS_PROSE = {
    "droite", "gauche", "piles", "vide", "doigt", "mamelle", "epaule",
    "hanche", "genou", "cheville", "poignet", "coude", "colonne", "poitrine",
    "ventre", "abdomen", "groupe", "taches",
}

# ---------------------------------------------------------------- normalization
def norm_orth(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z]+", " ", s.lower()).strip()

def norm_phon(s):
    return re.sub(r"[^a-z]", "", norm_orth(s))

def _norm_lead(s):
    """Normalisation qui CONSERVE les chiffres (lettres + chiffres concaténés).

    Sert aux garbles STT dont le nom commence par un nombre (« 13 iba ») :
    ``norm_phon`` perdrait le « 13 », or ce chiffre est l'amorce phonétique du
    nom et non une dose — il doit rester dans la clé de correspondance.
    """
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s.lower())

# ------------------------------- ISMP Canada TALLman lettering (LASA names)
# Mixed-case capitalization recommended for look-alike/sound-alike drug names.
# Key  = accent-folded lowercase name (as produced by norm_orth within a token).
# Value= the ISMP-recommended display casing. Names ISMP leaves as plain lower
#        case in Canada (the 2015 PDF *) map to their plain form (no change).
TALLMAN = {
    "afatinib": "AFAtinib", "axitinib": "aXitinib",
    "amlodipine": "amLODIPine", "amiodarone": "amiodarone",
    "azacitidine": "azaCITIDine", "azathioprine": "azaTHIOprine",
    "azithromycin": "azithromycin",
    "carboplatin": "CARBOplatin", "cisplatin": "CISplatin",
    "cycloserine": "cycloSERINE", "cyclosporine": "cycloSPORINE",
    "cyclophosphamide": "cyclophosphamide",
    "dabrafenib": "daBRAFenib", "dasatinib": "daSATinib",
    "dactinomycin": "DACTINomycin", "daptomycin": "daptomycin",
    "daunorubicin": "DAUNOrubicin", "doxorubicin": "DOXOrubicin",
    "dexamethasone": "dexamethasone", "dexmedetomidine": "dexmedeTOMidine",
    "diltiazem": "dilTIAZem", "diazepam": "diazepam",
    "dimenhydrinate": "dimenhyDRINATE", "diphenhydramine": "diphenhydrAMINE",
    "dobutamine": "DOBUTamine", "dopamine": "DOPamine",
    "docetaxel": "DOCEtaxel", "paclitaxel": "PACLitaxel",
    "idarubicin": "IDArubicin",
    "epinephrine": "epinephrine", "ephedrine": "ePHEDrine",
    "epirubicin": "epirubicin", "eribulin": "eriBULin",
    "fentanyl": "fentanyl", "sufentanil": "SUFentanil",
    "hydromorphone": "HYDROmorphone", "morphine": "morphine",
    "hydroxyzine": "hydroxyzine", "hydroxyurea": "hydroxyUREA",
    "ibrutinib": "iBRUtinib", "imatinib": "iMAtinib",
    "infliximab": "inFLIXimab", "rituximab": "riTUXimab",
    "lamivudine": "lamiVUDine", "lamotrigine": "lamoTRIgine",
    "mitoxantrone": "mitoXANTRONE",
    "nilotinib": "niLOtinib", "nilutamide": "niLUTAmide",
    "obinutuzumab": "oBINutuzumab", "ofatumumab": "oFAtumumab",
    "panitumumab": "PANitumumab", "pertuzumab": "PERTuzumab",
    "quinidine": "quiNIDine", "quinine": "quiNINE",
    "saxagliptin": "sAXagliptin", "sitagliptin": "sitagliptin",
    "sorafenib": "SORAfenib", "sunitinib": "SUNItinib",
    "vandetanib": "vanDETanib", "vemurafenib": "vemURAFenib",
    "vinblastine": "vinBLAStine", "vincristine": "vinCRIStine",
}

def tallman(s):
    """Return `s` with ISMP TALLman lettering applied to a trailing drug name,
    matched case-insensitively; names not on the list are returned unchanged."""
    key = norm_orth(s).replace(" ", "")
    if not key:
        return s
    return TALLMAN.get(key, s)


def title_brand(s):
    """Capitalize each word of a brand name, de-ALL-CAPS'ing DB-stored brands
    (e.g. 'TRESIBA' -> 'Tresiba', 'ELIQUIS' -> 'Eliquis'). Preserves the words
    that already read like Trademark-styled labels; intended solely to stop
    shouting ALL-CAPS output."""
    return " ".join(
        w[:1].upper() + w[1:].lower() if w and w.isupper() else w
        for w in s.split()
    )

# Canonical outputs that must never be substituted (they are ordinary prose /
# lab-value words that collide with a DPD generic or brand name). Keyed on the
# accent-folded, space-free orthography of the canonical name.
BAN_ORTH = {
    "proteines",     # "électrophorèse des protéines" (lab), not the generic Protein S
    "proteine",      # singular of the above
    # false-positive replacements observed on live audio (notes 4/10/12/15) :
    # these DPD rows are cosmetic/rare/insecticidal and must never be written
    # into a geriatric med list — they only ever fire via prose fuzzy matches.
    "colprone",          # « comprimé(s) » (tablet) -> Colprone
    "pyrethrines",       # « prescrites » (verb) -> pyrethrins
    "sylvant",           # « savant » (prose) -> Sylvant
    "acidealginique",    # « l'avion » (prose) -> acide alginique
    "alcool",            # « l'alcool » (substance de vie) -> alcool
}

# Electrolytes / lab-ion single-word generics. These appear constantly in the
# lab section of a dictation ("Sodium 141", "Calcium 2,35") AND, for a few (e.g.
# calcium), as a genuine oral supplement in the medication list. They are only
# substituted as meds when a real posology signal (mg / dose+unit / route) is
# present nearby; otherwise they are treated as lab values.
LAB_ION = {
    "sodium", "potassium", "calcium", "magnesium", "magnésium", "phosphate",
    "chlorure", "chloride", "proteine", "proteines", "creatinine", "créatinine",
    "glucose", "fer", "cholesterol", "cholestérol", "bilirubine", "albumine",
    "uree", "urée", "acide urique", "glycemie", "glycémie", "tsh", "ferritine",
    "vitamine b12", "vitamine d",
}

# ---------------------------------------------------------------- French G2P (optional)
G2P_RULES = [
    ("eau", "o"), ("eux", "ø"), ("œu", "ø"), ("eu", "ø"),
    ("ain", "ɛ̃"), ("ein", "ɛ̃"), ("oi", "wa"), ("ou", "u"), ("au", "o"),
    ("ai", "ɛ"), ("ei", "ɛ"), ("ay", "ɛj"), ("oy", "waj"), ("uy", "ɥi"),
    ("ien", "jɛ̃"), ("ian", "jɑ̃"), ("ion", "jɔ̃"), ("ie", "i"),
    ("ill", "ij"), ("ail", "aj"), ("eil", "ɛj"), ("euil", "œj"),
    ("gn", "ɲ"), ("ch", "ʃ"), ("ph", "f"), ("th", "t"), ("sh", "ʃ"),
    ("qu", "k"),
    # gu+voyelle : le « u » de « gu » n'est muet que devant e/i/é (il durcit
    # le g : « admelogue » = /admelɔg/, pas /admelɔgye/) ; devant a/o il forme
    # /gw/ (« proguanil » = /pʁɔgwanil/). Règle d'orthographe française
    # standard (indépendante de l'accent régional).
    ("gué", "g"), ("gue", "g"), ("gui", "g"), ("gü", "g"),
    ("gua", "gwa"), ("guo", "gwɔ"),
    ("ç", "s"), ("c", "k"), ("g", "g"), ("j", "ʒ"), ("h", ""),
    ("q", "k"), ("x", "ks"), ("y", "i"), ("w", "w"), ("z", "z"), ("s", "s"),
    ("f", "f"), ("v", "v"), ("p", "p"), ("b", "b"), ("t", "t"), ("d", "d"),
    ("k", "k"), ("l", "l"), ("m", "m"), ("n", "n"), ("r", "ʁ"),
    ("a", "a"), ("e", "e"), ("i", "i"), ("o", "ɔ"), ("u", "y"),
    ("é", "e"), ("è", "ɛ"), ("ê", "ɛ"), ("à", "a"), ("ô", "o"), ("û", "y"), ("î", "i"),
]
_NOSALT = {"hydrochloride","hydrobromide","besylate","tartrate","maleate","sodium",
           "potassium","calcium","magnesium","succinate","dihydrate","monohydrate",
           "sulfate","citrate","mesylate","tosylate","chlorhydrate","bromhydrate",
           "calcique","sodique","potassique","propyleneglycol","solvate","anhydrous"}

def _g2p_word(word):
    w = word.lower()
    i, out = 0, []
    while i < len(w):
        for gr in sorted(G2P_RULES, key=lambda r: -len(r[0])):
            g, p = gr
            if w.startswith(g, i):
                if p:
                    out.append(p)
                i += len(g)
                break
        else:
            out.append(w[i]); i += 1
    return "".join(out)

def phonetic_fr(phrase):
    return " ".join(_g2p_word(w) for w in norm_orth(phrase).split())


#: Cache de la conversion G2P française : les mêmes tokens/alias sont
#: phonétisés de façon répétée (construction des arbres BK du ``Matcher``,
#: ``phonetiques_texte`` à chaque grounding). La fonction est pure et
#: déterministe — le cache est sûr en multi-thread (vérifié : ``Matcher``
#: partagé en lecture seule après construction). Borne de 4096 entrées suffit
#: largement aux ~50 000 alias : on ne cache que la hot-path de la phonétique
#: (appels répétés sur les mêmes tokens), pas la totalité du lexique.
phonetic_fr = functools.lru_cache(maxsize=4096)(phonetic_fr)

# ---------------------------------------------------------------- distance
def lev(a, b):
    """Levenshtein distance (edit distance), C-accelerated by rapidfuzz.
    Identical values to the previous pure-Python DP, verified on random strings."""
    return Levenshtein.distance(a, b)

def sim(a, b):
    m = max(len(a), len(b))
    return 0.0 if m == 0 else 1.0 - lev(a, b) / m

# ------------------------------------------- métriques pondérées (lettres / phonèmes)
#
# Le STT produit des fautes surtout auditives : les substitutions entre
# phonèmes (ou lettres) articulatoirement proches — /s/↔/z/, /f/↔/v/… —
# sont plus plausibles qu'une insertion d'un phonème entier. Or la distance
# de Levenshtein unitaire en fait des coûts identiques (1), et le normalise
# `sim = 1 - lev/max(len)` favorise même le plus long des deux. C'est ce qui
# faisait proposer l'hormone « estetrol » (insertion d'un /t/) au détriment de
# « ezetrol » = ézétimibe (simple /s/→/z/, `esétrol` dicté).
#
# On ajoute une distance pondérée : la substitution n'est pénalisée qu'à
# *fraction* du coût d'une insertion/suppression selon sa proximité. Deux
# tables : l'une pour les chaînes de LETTRES (chemin orthographique, tie-break
# de `_resolve_single`), l'autre pour les chaînes de PHONÈMES G2P (chemin
# phonétique des hints).

def _dp_subst(a, b, sub_cost):
    """Distance de Levenshtein pondérée : le coût de substitution vaut
    ``sub_cost(pa, pb)`` (0 si identiques), sinon 1.0 par insertion/suppr."""
    n, m = len(a), len(b)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0.0 if a[i - 1] == b[j - 1] else sub_cost(a[i - 1], b[j - 1])
            d[i][j] = min(d[i - 1][j] + 1.0, d[i][j - 1] + 1.0,
                          d[i - 1][j - 1] + cost)
    return d[n][m]

# Substitutions de lettres proches (orthographique). Paires phonétiquement
# équivalentes en français → coût réduit.
_ORTHO_SUB_PAIRS = ("sz", "fv", "ck", "cs", "gj", "bd", "pt", "iy")
_ORTHO_SUB = {frozenset(p): 0.4 for p in _ORTHO_SUB_PAIRS}

def _ortho_sub_cost(a, b):
    return _ORTHO_SUB.get(frozenset((a, b)), 1.0)

def sim_ortho_w(a, b):
    """Similarité orthographique pondérée (lettres), pour départager à
    distance de Levenshtein égale des candidats aux lettres proches."""
    m = max(len(a), len(b))
    if m == 0:
        return 0.0
    return 1.0 - _dp_subst(a, b, _ortho_sub_cost) / m

# Substitutions de phonèmes articulatoirement proches (G2P).
_PLOSIVE = set("ptkbdg")
_FRICATIVE = set("fsvzʃʒ")

def _phon_sub_cost(a, b):
    if {a, b} in ({'s', 'z'}, {'f', 'v'}, {'ʃ', 'ʒ'}):
        return 0.3                  # fricatives sonore/sourde
    if {a, b} in ({'p', 'b'}, {'t', 'd'}, {'k', 'g'}):
        return 0.4                  # plosives sonore/sourde
    if (a in _FRICATIVE and b in _FRICATIVE) or (a in _PLOSIVE and b in _PLOSIVE):
        return 0.6                  # même classe d'articulation
    if {a, b} in ({'r', 'l'}, {'l', 'ʁ'}):
        return 0.7                  # approximantes proches
    return 1.0

def sim_phon_w(a, b):
    """Similarité phonémique pondérée (G2P), pour les hints phonétiques."""
    m = max(len(a), len(b))
    if m == 0:
        return 0.0
    return 1.0 - _dp_subst(a, b, _phon_sub_cost) / m

class BKTree:
    def __init__(self, distance):
        self._d = distance; self.tree = None
    def add(self, node):
        if self.tree is None:
            self.tree = (node, {}); return
        t = self.tree
        while True:
            parent, children = t; dist = self._d(node, parent)
            if dist == 0: return
            child = children.get(dist)
            if child is None:
                children[dist] = (node, {}); return
            t = child
    def search(self, query, max_dist):
        if self.tree is None: return []
        results, stack = [], [self.tree]
        while stack:
            node, children = stack.pop(); dist = self._d(query, node)
            if dist <= max_dist: results.append((dist, node))
            lo, hi = dist - max_dist, dist + max_dist
            for d in range(max(0, lo), hi + 1):
                child = children.get(d)
                if child is not None: stack.append(child)
        return sorted(results, key=lambda x: x[0])

# ---------------------------------------------------------------- context signals
# Verb anchors: the prescribing verb directly introduces the drug name that
# follows ("prescrit...", "prend du...", "administre..."). Word-bounded so a
# substring can never leak ("prend" inside "comprendre", "prescrit" inside
# "prescrite").
STRONG_ANCHORS = [
    "prend comme medicament", "prend du", "prend de", "prends", "prend",
    "prendre", "sous traitement", "sous", "traite par", "medicament",
    "medication", "posologie", "ordonne", "prescrit", "prescrite",
    "prescrire", "prescription", "remplace par", "remplace", "cesse",
    "cesser", "administre", "administrer", "comme medicament",
    "liste de medicaments", "sur ordonnance",
]
# List-introducer nouns: a generic noun ("dans ses médicaments") opens a wide
# medication context but must NOT credit every word in its wake like a verb
# anchor does -- they only count at near-certain similarity (HighSim) or when a
# real dose sits nearby. These get a separate, sentence-scoped flag.
ANCHOR_RE = re.compile(
    r"\b(" + "|".join(sorted(STRONG_ANCHORS, key=len, reverse=True)) + r")\b", re.I)
NOUN_CTX_WORDS = {"medicament", "medicaments", "medication", "medications",
                  "liste", "posologie", "polypharmacie"}
POSOLOGY_RE = re.compile(
    r"(?:\d+(?:[,.]\d+)?)\s*(?:mg|mcg|µg|g|ml|unités|unites|ui|%|die|bid|tid|hs|prn|qid|"
    r"po|per os|par jour|q\.?d)", re.I)
DOSE_RE = re.compile(r"(\d+(?:[,.]\d+)?)\s*(mg|mcg|µg|g|ml)", re.I)

ANCHOR_WORDS = set(re.findall(r"[a-z]+", " ".join(STRONG_ANCHORS) + " comme medicament"))

# Dose/protocol/unit abbreviations that are drug-token look-alikes (e.g. "BID" is a
# protocol token yet also a brand-leaf of BIAXIN BID). Never treat these AS meds.
PROTOCOL_WORDS = {"bid","tid","qid","qd","qod","prn","hs","die","po","peros","q",
                  "die","am","pm","per","os","par","jour","mg","mcg","µg","g","ml",
                  "unités","unites","ui","ui/j","once","semaine","ann"}

# ----------------------------------------------------------------- latin dosing
# Canonical route/frequency output for the medication list (per Wikimedica).
# Latin abbreviations render in CAPS (PO / DIE / BID / TID / QID / HS / AM /
# PM / PRN); frequencies / routes use the standard forms: `Q2 jours` -> Q2J,
# `Q semaine` -> Q1SEM, `unités`/`unites`/`ui` -> UI.

def _norm_dose(w):
    """Dose-token key: accent-folded, lowercase, keep digits (so "Q2" != "q")."""
    s = unicodedata.normalize("NFD", w)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s.lower())

DOSE_FREQ_SINGLE = {
    "po": "PO", "peros": "PO", "os": "PO", "per": "PO",
    "perotidien": "PO DIE", "perosquotidien": "PO DIE",
    "sc": "S/C", "souscut": "S/C", "souscutane": "S/C", "souscutanee": "S/C",
    "souscu": "S/C", "subcut": "S/C",
    "die": "DIE", "dye": "DIE", "dieam": "DIE AM", "dieam": "DIE AM",
    # « d'yeux » = déformation STT de « DIE » (10 mg d'yeux → 10 mg DIE) ;
    # « déjeunés » = « AM » (5 mg d'yeux déjeunés → 5 mg DIE AM). Observés sur
    # la consultation 11 (STT custom, confirmés à l'audience).
    "dyeux": "DIE", "dejeunes": "AM",
    "quotidien": "DIE", "quotidienne": "DIE",
    "bd": "BID", "bid": "BID", "tid": "TID", "qid": "QID",
    "prn": "PRN", "besoin": "PRN",
    "hs": "HS", "am": "AM", "pm": "PM",
    "matin": "AM", "soir": "PM", "coucher": "HS",
    "q": "Q", "q2": "Q2",
    "q2j": "Q2J", "q1sem": "Q1SEM", "q1h": "Q1H", "q2h": "Q2H",
    "jour": "jour", "jours": "jours", "semaine": "semaine",
    "unites": "UI", "unite": "UI", "ui": "UI",
}
# Multi-token phrases (each entry: tuple of _norm_dose tokens -> canonical list).
DOSE_FREQ_PHRASES = [
    (("per", "os"), ["PO"]),
    (("sous", "cut"), ["S/C"]),
    (("sous", "cutane"), ["S/C"]),
    (("sous", "cutanee"), ["S/C"]),
    (("au", "besoin"), ["PRN"]),
    (("deux", "fois", "par", "jour"), ["BID"]),
    (("trois", "fois", "par", "jour"), ["TID"]),
    (("quatre", "fois", "par", "jour"), ["QID"]),
    (("une", "fois", "par", "jour"), ["DIE"]),
    # Le STT dit la fréquence en chiffre (« 3 fois par jour ») : même canon que
    # l'orthographe en lettres — 1→DIE, 2→BID, 3→TID, 4→QID. Placés AVANT le
    # motif générique (« fois par jour »→DIE) pour que le chiffre soit absorbé
    # dans le run et non laissé seul devant un « 3 » nu.
    (("1", "fois", "par", "jour"), ["DIE"]),
    (("2", "fois", "par", "jour"), ["BID"]),
    (("3", "fois", "par", "jour"), ["TID"]),
    (("4", "fois", "par", "jour"), ["QID"]),
    (("fois", "par", "jour"), ["DIE"]),
    (("par", "jour"), ["DIE"]),
    (("par", "semaine"), ["Q1SEM"]),
    (("q", "semaine"), ["Q1SEM"]),
    (("le", "matin"), ["AM"]),
    (("le", "soir"), ["HS"]),
    (("au", "coucher"), ["HS"]),
    (("die", "am"), ["DIE", "AM"]),
    (("q2", "jours"), ["Q2J"]),
    (("q", "2", "jours"), ["Q2J"]),
]
# Route markers open a phrase unconditionally; quant markers (`q`/`q2`) and
# Latin frequency markers open on a preceding dose; French prose frequency
# words additionally need a confirmed med-list region.
DOSE_ROUTE_START = {"po", "peros", "per", "os",
                    "perotidien", "perosquotidien",
                    "souscut", "souscutane", "souscutanee", "souscu", "subcut"}
DOSE_QUANT_START = {"q", "q2"}
DOSE_LATIN_START = {"die", "dye", "dieam", "quotidien", "quotidienne",
                    "bd", "bid", "tid", "qid", "prn", "besoin", "hs", "am",
                    "pm", "unites", "unite", "ui", "sc",
                    "dyeux", "dejeunes"}
DOSE_FRENCH_START = {"jour", "jours", "semaine", "matin", "soir", "coucher",
                     "par", "fois", "deux", "trois", "quatre", "une", "au",
                     "le", "de", "sous"}
#: Fréquence dictée en CHIFFRE (« 3 fois par jour ») : amorce identique à la
#: variante en lettres, exige le run complet (1–4 × « fois par jour »). Un
#: chiffre nu (« …il prend 3 comprimés… ») ne s'absorbe jamais : la boucle de
#: consommation ne remonte que les runs reconnus.
DOSE_NUM_START = {"1", "2", "3", "4"}

FRENCH_STOP = set("""
un une des le la les du de d a à au aux et ou ni ne se ce sa son ses leur leurs
elle il ils elles on nous vous je tu moi toi votre nos ton ta tes mon ma mes
en dans pour par avec sans sous sur chez vers entre contre pendant depuis autour
comme tout toute tous toutes rien quelque quelques quel quelle quels quelles ceci
cela celui celle meme si car mais donc or quand que qui quoi dont ou est sont etait
ete fait font faire avoir etre suis es avais avait avons avez ont sera aucune aucun
autre pas non plus tres bien bon bonne mauvais tres peu beaucoup encore toujours
jamais deja puis avant apres pendant chez vers y en cela cette cet ces tout jour mois
an ans semaine docteur medecin madame monsieur dame patient patienter clinique hopital
consultation test examen prise sang normale normal a un numero votre note aller dit
dite dire voir vu vue veux voulait voulez sait sais savoir passe passee present depuis
revue revoir rien yeux oeil annee prochain derniere premier premiere suivant suivante
actuel actuelle fait arefaite pourtant ensuite encore assez environ presque
alzheimer reisberg congestion congrue hypothyroi dislip hemie maladie coronarienne
lombalgies chroniques cognitifs cognitive cognitif evolutif changement psychotique
patron marche inchange calme collaborant collaborante mental scan cerebral bilan
analyses revue revoirAGE partie donne donnes histoire antecedent antecedents problem
probleme suivi suivie cong egeriatrie cgeriatrie geriatrie famille
""".split())
FRENCH_STOP |= {str(n) for n in range(1000)}
FRENCH_STOP |= {
    "mini", "plan", "mental", "aide", "soin", "soins", "douleur", "douleurs",
    "pain", "life", "live", "daily", "active", "plus", "pro", "kids", "child",
    "adult", "senior", "new", "nov", "original", "modern", "complet", "complete",
    "special", "unique", "bas", "haut", "grand", "petit", "moyen", "fort", "forte",
    "jour", "matin", "soir", "nuit", "cong", "famille", "donne", "donnes",
    "histoire", "probleme", "antecedents", "suivi", "suivie", "congeg",
    # collisions observed from DPD fuzzy matches
    "unite", "unites", "diéam", "dieam", "prille", "aide", "proches", "proche",
    "demande", "demandes", "faites", "faite", "fais", "os", "per", "indice",
    "indices", "cardiaque", "avril", "mai", "juin", "juillet", "mars", "janvier",
    "fevrier", "septembre", "octobre", "novembre", "decembre", "aod", "hta",
    "timbre", "timbr", "scande", "film", "band", "vitamine", "vitamines", "age",
    "ages", "partie", "question", "questions", "probleme", "probl", "freq",
    "frequence", "fréquence", "tension", "signe", "signes", "donnees", "données",
    "histoire", "antecedentes", "couture", "tricote", "tricot", "sortie", "entrer",
    # prose / lab-value collisions from dictée 4 (consult ai 4)
    "prendre", "prend", "diabete", "diabète", "ligne", "lignes", "moment",
    "type", "types", "proteine", "proteines", "protéine", "protéines",
    "phosphate", "magnesium", "magnésium", "dispile", "dispill", "prescrite",
    "prescrit", "prepare", "preparer", "preparée", "préparée", "préparé",
    "concombre", "analyser", "analyse", "analyse", "electrophorese",
    # prose / lab / abbreviation collisions from the wider inactive-brand pool
    "geriatrique", "gériatrique", "geriatriques", "note", "notes", "repet",
    "répète", "répéter", "repe", "ferritine", "ferritines", "avc", "autres",
    # accent-folded norm_phon forms the gate actually sees
    "feritine", "feritines", "repete", "dautres", "geriatrie", "soins",
    # prose / verb-form collisions (notes 8 & 9) that are NOT medication names
    "dose", "doses", "pourra", "pourrai", "pourraient", "pourrait", "pourrons",
    "pourront", "symptome", "symptomes", "symptômes", "symptom", "symptoms",
    "relief", "strength", "cold", "flu", "lemon", "nose", "reparler",
    "repariera", "reparlera", "prescrite", "prescrire", "prescrits",
    # demonstratives / prose nouns that fuzzy-match leave brands. "celle-là"
    # is a REAL observed STT_GARBLE for Celexa (seeded -> exact match), so its
    # combined form "cellela" must NOT be blocked; bare "celle" stays guarded.
    "celle", "celles", "celui", "ceux", "conge", "conges",
    "reste", "restants", "prises", "famille", "date", "dates",
    "echelle", "échelle", "echelles", "selon", "depression", "essais",
    # la marque contraceptive JASMIN a été retirée du périmètre (prune_scope) :
    # sans lui, « jasmin » retomberait sur FASTIN par le flou orthographique
    "jasmin",
    # le tokenizer garde l'article soudé : « l'hopital » -> norm "lhopital",
    # qui floue sur ADRENALIN TOPICAL (faux positif historique, base git-id)
    "lhopital",
    # note 6 (délirium ICU) prose collisions
    "heure", "heures", "laisse", "laissee", "laisser", "laissait",
    "médicaments", "medicaments", "médication", "medication", "polypharmacie",
    # prose des consultations 3/4/10/11 qui floue sur des marques medically
    "sinusal", "sinusale", "semaine", "semaines", "alcool", "savant",
    "cestadire", "eviere", "évière", "pothyroide", "pothyroïdie",
    "hypothyroide", "hypothyroïdie", "sartanicals",
    # formes avec article soudé (tokenizer garde « l' ») ou accent replié :
    # « l'alcool » -> lalcool, « l'évière » -> leviere, « pothyroïdie » ->
    # pothyroidie
    "lalcool", "leviere", "lhypothyroide", "pothyroidie", "lasynthe",
    "lalcohol", "lesinusal",
    # polyethylene glycol is written with a space: "glycol" alone must never
    # fuzzy-match a brand (Baycol); the full phrase only resolves via the
    # multi-token garble / brand path, never per-token.
    "glycol", "glycole", "polyethylene", "polyethyleneglycol", "polyethylène",
    # adjectifs/adverbes de fréquence et de sévérité : la prose « de façon
    # régulière HS » ne doit jamais se faire coller la posologie suivante
    # (régulière -> REGULEX/docusate en raflant le HS de la quétiapine,
    # note 13). « aiguë »/« couchée » gardent la même ceinture.
    "regulier", "reguliere", "regulierement", "reguliers", "regulieres",
    "aigue", "aigues", "ague", "agues", "aigu",
    "couchee", "recouchee",
    # la conjonction anglaise « and » (le STT lâche parfois des tokens EN :
    # « on and off ») frappe EXACTEMENT la marque BDP « AND » (naproxène,
    # OTC inactif) : on la bannit, comme « jasmin »/« lhopital ».
    "and",
    # prose / dose-unit colliding with DPD fuzzy matches (notes 4/10/12/15) :
    # « prescrites » (verbe, pluriel féminin — « …prescrites le 26 juin »)
    # frappe PYRETHRINES ; « comprimé(s) » (unité de dose) frappe COLRONE ;
    # « l'avion »/« savant » (prose) frappent ACIDE ALGINIQUE/SYLVANT ;
    # « alcool » est une substance de vie, jamais une médication prescrite.
    "prescrites", "prescrits",
    "comprimes", "comprime",
    "avion", "lavion", "savant",
    "alcool", "lalcool",
}

# ---------------------------------------------------------------- matcher
class Matcher:
    def __init__(self, db=DB, use_phonetic=False):
        self.use_phonetic = use_phonetic
        self.conn = sqlite3.connect(db)
        rows = self.conn.execute("""
            SELECT a.alias_name, a.alias_type, m.level, m.base_generic, m.brand_name, m.is_otc
            FROM medication_aliases a JOIN medications m ON m.id = a.medication_id
        """).fetchall()
        self.exact = {}          # norm_phon -> (level, base, brand, is_leaf, is_otc)
        self.exact_garble = {}   # norm_phon -> same tuple, seeded STT_GARBLE rows
                                 # (seeded garbles WIN any exact collision)
        # Garbles STT dont le NOM commence par un nombre (« 13 iba » = Tresiba,
        # où « 13 » est l'amorce phonétique « trési » et NON une dose). Leur
        # ``norm_phon`` (« 13iba » → « iba ») perdrait le chiffre : ils sont
        # rangés à part, clé = ``_norm_lead`` (lettres + chiffres concaténés),
        # et ``normalize`` les consomme comme un bigramme nombre+mot unique.
        self.exact_garble_num = {}   # _norm_lead(num+word) -> tuple
        self.ortho = []          # (norm_phon, level, base, brand, is_leaf, is_otc) deduped
        self.ortho_by_len = {}   # len(norm_phon) -> [(n, ...)] for fast phrase lookup
        self._generic_names = set()   # genuine generic orthography names
        self._generic_compound = set()   # composés multi-mots (espaces préservés)
        # À l'orthographique comme à l'exact (l.579-582), un niveau générique
        # écrase une feuille de marque de même ``norm_phon`` : sans quoi un alias
        # BRAND_LEAF (« trazodone » → PMS TRAZODONE HCL) shadowait son générique
        # (id 207475 < 207476) et les hints phonétiques, qui excluent les
        # feuilles, retombaient sur une marque manufacturier parasite
        # (« trasodone » → NU-TRAZODONE au lieu de « trazodone »).
        seen = {}
        for alias, atype, level, base, brand, is_otc in rows:
            n = norm_phon(alias)
            is_leaf = (atype == "BRAND_LEAF")
            if not n:
                continue
            if atype == "STT_GARBLE":
                # Garbles STT dont le nom commence par un nombre (« 13 iba » =
                # Tresiba) : rangés à part, JAMAIS populés dans exact_garble /
                # exact (leur norm_phon « 13 iba » → « iba » ferait de « iba »
                # seul un Tresiba S=100 en prose).
                if re.match(r"^\d", alias.strip()):
                    k = _norm_lead(alias)
                    if k:
                        self.exact_garble_num.setdefault(
                            k, (level, base, brand, bool(is_otc)))
                    continue
                self.exact_garble.setdefault(n, (level, base, brand, False, bool(is_otc)))
            if level in ("BASE_GENERIC", "FULL_GENERIC") and base:
                self._generic_names.add(norm_orth(base))
                if " " in norm_orth(base):
                    self._generic_compound.add(norm_orth(base))
            # Generic-level aliases win over brand leaves on exact collisions
            prev = self.exact.get(n)
            if n not in self.exact or (prev is not None and prev[3] and not is_leaf):
                self.exact[n] = (level, base, brand, is_leaf, bool(is_otc))
            if re.search(r"\d|%|mg|mcg|µg|ml|tablet|tab|caplet|capsule|cream|gel|"
                         r"patch|syringe|injection|solution|suppository", alias, re.I):
                continue                       # junk aliases: exact only
            prev = seen.get(n)
            if prev is None or (prev[3] and not is_leaf):
                seen[n] = (n, level, base, brand, is_leaf, bool(is_otc))
        for n, level, base, brand, is_leaf, is_otc in seen.values():
            self.ortho.append((n, level, base, brand, is_leaf, bool(is_otc)))
            self.ortho_by_len.setdefault(len(n), []).append(
                (n, level, base, brand, is_leaf, bool(is_otc)))
        self.bk_ortho = BKTree(lev)
        self.ortho_node = {}
        for n, level, base, brand, is_leaf, is_otc in self.ortho:
            self.bk_ortho.add(n)
            self.ortho_node[n] = (level, base, brand, is_leaf, is_otc)
        if self.use_phonetic:
            self.bk = BKTree(lev)
            self.bk_node = {}            # node phonétique -> (level, base, brand, is_leaf, is_otc)
            for n, level, base, brand, is_leaf, is_otc in self.ortho:
                node = phonetic_fr(n)
                self.bk.add(node)
                prev = self.bk_node.get(node)
                if prev is None or (prev[3] and not is_leaf):
                    self.bk_node[node] = (level, base, brand, is_leaf, is_otc)

        # Canonicalization data
        self.generics = self._generic_names
        # Canadian generic manufacturer prefixes (strip these from brands)
        self.MANUFACTURER_PREFIXES = (
            "apo-", "pms-", "ag-", "taro-", "nra-", "aa-", "jamp-", "teva-",
            "sandoz-", "ran-", "lupin-", "mylan-", "accord-", "auro-",
            "bex-", "ufc-", "pendopharm-", "sivem-", "pharmascience-", "pro-",
            "act-", "mint-", "co-", "dom-", "mar-", "bex-", "bms-", "pfizer-",
            "sanis-", "abbott-", "gsk-", "sanofi-", "merck-", "novo-",
            "servier-", "bausch-", "alcon-", "actavis-", "amneal-", "auvio-",
            "aam-", "bpg-", "cpl-", "drm-", "dpt-", "eph-", "frm-", "gmp-",
            "hik-", "ipr-", "jav-", "krm-", "lph-", "map-", "med-", "mhr-",
            "mph-", "mxi-", "nph-", "npi-", "nva-", "ocl-", "omr-", "orx-",
            "par-", "pch-", "pdi-", "pdr-", "pfg-", "pms-", "prx-", "psi-",
            "qpi-", "rph-", "rxd-", "sag-", "sap-", "sbx-", "sci-", "sep-",
            "sgm-", "shl-", "smi-", "spc-", "spt-", "stp-", "srx-", "sun-",
            "swx-", "tgx-", "the-", "tpi-", "tpg-", "trm-", "tst-", "upi-",
            "vxl-", "wht-", "xix-", "xph-", "zph-", "zyd-"
        )
        # Presentation/salt tokens to strip from brand names
        self.PRESENTATION_SALT_TOKENS = {
            "usp", "strength", "regular", "extra", "forte", "maximum", "max",
            "tablet", "tablets", "tab", "caplet", "caplets", "capsule", "capsules",
            "cap", "injection", "injectable", "syringe", "cream", "ointment",
            "gel", "patch", "spray", "solution", "suspension", "syrup", "elixir",
            "drop", "drops", "suppository", "chews", "quick", "chewable",
            "coated", "delayed", "release", "extended", "sustained",
            "chloride", "hydrochloride", "hydrobromide", "besylate", "tartrate",
            "maleate", "sodium", "potassium", "calcium", "magnesium", "succinate",
            "dihydrate", "monohydrate", "anhydrous", "phosphate", "sulfate",
            "sulphate", "fumarate", "citrate", "mesylate", "tosylate", "camsylate",
            "chlorhydrate", "bromhydrate", "calcique", "sodique", "potassique",
            "magnesique", "calcium", "chlorure", "sulfate", "phosphate",
            "acetate", "acetates", "gluconate", "lactate", "carbonate",
            "nitrate", "bicarbonate", "bisulfate", "bisulphate", "edisylate",
            "glucoheptonate", "lactobionate", "mandelate", "methylbromide",
            "methylsulfate", "methylsulphate", "naphthoate", "nitrobenzoate",
            "pamoate", "pentothenate", "salicylate", "stearate", "subproate",
            "teoclate", "tartrate", "theophyllinate", "triethiodide",
            "undecylenate", "valerate", "xinafoate", "chloride", "bromide",
        }
        # Small French common-name map for well-known English common names
        self.FR_COMMON = {
            "aspirin": "aspirine",
            "acetaminophen": "acétaminophène",
            "ibuprofen": "ibuprofène",
            "naproxen": "naproxène",
            "acetazolamide": "acétazolamide",
            "allopurinol": "allopurinol",
            "amitriptyline": "amitriptyline",
            "amlodipine": "amlodipine",
            "atenolol": "aténolol",
            "atorvastatin": "atorvastatine",
            "bisoprolol": "bisoprolol",
            "citalopram": "citalopram",
            "clopidogrel": "clopidogrel",
            "diltiazem": "diltiazem",
            "donepezil": "donépézil",
            "escitalopram": "escitalopram",
            "furosemide": "furosémide",
            "gabapentin": "gabapentine",
            "hydrochlorothiazide": "hydrochlorothiazide",
            "levothyroxine": "lévothyroxine",
            "losartan": "losartan",
            "metformin": "metformine",
            "metoprolol": "métoprolol",
            "olanzapine": "olanzapine",
            "omeprazole": "oméprazole",
            "pantoprazole": "pantoprazole",
            "perindopril": "perindopril",
            "pregabalin": "prégabaline",
            "quetiapine": "quétiapine",
            "ramipril": "ramipril",
            "rivastigmine": "rivastigmine",
            "rosuvastatin": "rosuvastatine",
            "sertraline": "sertraline",
            "simvastatin": "simvastatine",
            "trazodone": "trazodone",
            "valsartan": "valsartan",
            "venlafaxine": "venlafaxine",
        }
        # OTC base_generic -> natural French display name (a few common ones).
        self.OTC_DISPLAY = {
            "acide acetylsalicylique": "aspirine",
            "acetylsalicylic acid": "aspirine",
        }

    def _resolve_phrase(self, words, start, n, dose_unit, num_token, anchor_hit,
                        fragile, garble_seed_only=False):
        """Proactive multi-token med-phrase resolution (medication-list regions
        only). Joins up to 3 content tokens bridged by French stop-words (e.g.
        "Perrin de prille" -> perindopril) and accepts a match only when a
        dose follows immediately and the hit is high-confidence and unique, so
        ordinary prose / anchored narrative is never absorbed.

        ``garble_seed_only=True`` restreint le flou orthographique et ne garde
        que les garbles multi-mots SEEDÉS (exacts, déterministes) — usage hors
        zone proactive, où un score flou pourrait absorber de la prose.
        """
        BRIDGE = {"de", "du", "des", "d", "à", "au", "aux", "le", "la",
                  "les", "l", "un", "une", "et"}
        def clean(w):
            return w.strip(" \t,;:.()\"'’")
        # Never open a phrase on a stop / anchor / protocol / number token.
        if (fragile[start] or dose_unit[start] or num_token[start]
                or not clean(words[start])):
            return None
        parts, idxs, content = [], [], 0
        k = start
        while k < n and len(parts) < 4:
            cw = clean(words[k])
            if not cw or dose_unit[k] or num_token[k]:
                break                  # stop at dose / number / strong boundary
            low = cw.lower()
            p = norm_phon(cw)
            if p in BRIDGE or low in BRIDGE:
                if not content:
                    return None        # a med cannot start on a stop word
                parts.append(cw); idxs.append(k); k += 1
                continue               # bridge stop words are consumed, not broken
            if p in ANCHOR_WORDS or p in PROTOCOL_WORDS:
                break                  # structural anchor/protocol word ends phrase
            parts.append(cw); idxs.append(k); content += 1; k += 1
            if content >= 3:
                break
        if content < 2:
            return None
        # The whole point: batch dictations pair a med name with a dose right
        # after the phrase. Require a dose/number to follow within a few tokens.
        e = idxs[-1]
        if not any(dose_unit[a] or num_token[a] for a in range(e, min(e + 4, n))):
            return None
        for ln in range(len(parts), 1, -1):
            joined = "".join(parts[:ln])
            t = norm_phon(joined)
            if not t or len(t) < 6:
                continue
            # Un garble STT multi-mots connu (seed) prime sur le flou :
            # « Hamelot d'épine » -> amlodipine, « Périn de prille » -> …
            if t in self.exact_garble:
                level, base, brand, _leaf, _otc = self.exact_garble[t]
                can = self._canonicalize(level, base, brand, bool(_otc))
                if can:
                    return (idxs[ln - 1] + 1, can, 100)
            if garble_seed_only:
                continue              # hors proactive : pas de score flou
            best, second = None, 0.0
            L = len(t)
            for bln in range(max(1, L - MAX_LEN_DIFF - 2), L + MAX_LEN_DIFF + 3):
                for entry in self.ortho_by_len.get(bln, ()):
                    orth, level, base, brand, is_leaf, is_otc = entry
                    if re.search(r"\d|%|mg|mcg|tablet|injection", orth):
                        continue          # skip junk presentation aliases
                    s = sim(t, orth)
                    if s >= PROACTIVE_ORTHO_FLOOR:
                        if best is None or s > best[0]:
                            second = best[0] if best else 0.0
                            best = (s, level, base, brand, is_otc)
                        elif s > second:
                            second = s
            if best is None or best[0] < 0.70 or (best[0] - second) < 0.12:
                continue              # weak or ambiguous -> stay conservative
            can = self._canonicalize(best[1], best[2], best[3], best[4])
            if can:
                return (idxs[ln - 1] + 1, can, 100)
        return None

    def _medlist_regions(self, words, fragile, num_token, dose_unit):
        """Detect confirmed medication-list regions.

        A dictation med list has an unmistakable rhythm: several drug names
        each immediately followed by a dose (bare number, or number+unit). We
        find runs of `name -> dose` pairs and confirm a run is a true med list
        only when a majority of its names actually resolve to a real medication
        (exact or high-confidence fuzzy, non-leaf, and not a lab ion). This
        separates a real list ("aspirine 80, pantoloque 40, ...") from vitals /
        lab panels / MMSE-subscore narration ("tension 132, Hdl 1,61, Moins 6 à
        l'orientation"), whose 'names' are stopwords or non-med laboratory
        values. Returns a per-token bool array marking the confirmed spans.
        """
        n = len(words)
        BRIDGE = {"de", "du", "des", "d", "à", "au", "aux", "le", "la",
                  "les", "l", "un", "une", "et"}
        isname = [False] * n
        for i in range(n):
            if fragile[i] or num_token[i] or dose_unit[i]:
                continue
            # Names must be substantive drug-like tokens (>=5 letters):
            # short abbreviations ("C", "GAD", "Hdl", "Moca", "MMSE") belong
            # to lab panels / cognitive scores, not dictation med lists.
            if len(norm_phon(words[i])) < 5:
                continue
            j = i + 1
            if j < n and norm_phon(words[j]) in BRIDGE:
                j += 1
            if j < n and num_token[j]:
                isname[i] = True
        idxs = [i for i in range(n) if isname[i]]
        medlist = [False] * n
        k = 0
        while k < len(idxs):
            start = idxs[k]; j = k
            while j + 1 < len(idxs) and idxs[j + 1] - idxs[j] <= 6:
                j += 1
            cnt = j - k + 1
            strong = 0
            for t in range(k, j + 1):
                i = idxs[t]
                cand = self._resolve_single(words[i])
                if (cand and not cand[4]
                        and norm_orth(words[i]).replace(" ", "") not in LAB_ION):
                    strong += 1
            if cnt >= 2 and strong >= 2 and strong / max(1, cnt) >= 0.5:
                for t in range(start, idxs[j] + 1):
                    medlist[t] = True
            k = j + 1
        # Passe PERMISSIVE « transfert de dossier » : une liste confirmée (au
        # moins 2 noms résolus munis d'une dose) étend sa fenêtre aux NOMS NUS
        # avoisinants qui se résolvent en vrai médicament (non feuille, non
        # lab) dans un court intervalle. Les listes de transfert dictent
        # souvent un nom sans dose (« Doxazosin. », « Serpaline 50 » gardés
        # sans unité) : tant que le bloc a de vraies doses ailleurs, un nom
        # résolu en prose 2 mots plus loin reste du médicament, pas du bruit.
        medlist = _extend_medlist_bare(words, medlist, self._resolve_single)
        return medlist

    def _dose_suffix_phrase(self, words, i, n, dose_unit, num_token, proactive,
                            medlist):
        """Normalize a Latin/French route-frequency suffix in a medication list.

        Starting at `i`, if the token opens a dosing phrase (per os / die / bid
        / tid / hs / prn / am / pm / Q2 jours / Q semaine / unités ...)
        following a dose (or as an unqualified route marker), consume the whole
        contiguous run and return (canonical_phrase, end_index). Otherwise
        (None, i). Any recognized fragment is rendered in its canonical CAPS /
        lowercase French form (PO, DIE, BID, Q2jours, Q semaine, UI ...).
        """
        region = bool(proactive[i] or medlist[i])
        p0 = _norm_dose(words[i])
        prev_dose = any(dose_unit[k] or num_token[k]
                        for k in range(max(0, i - 3), min(i, n)))
        if p0 in DOSE_ROUTE_START:
            pass                                   # route marker: always opens
        elif p0 in (DOSE_QUANT_START | DOSE_LATIN_START) and (prev_dose or region):
            pass
        elif p0 in DOSE_FRENCH_START and (prev_dose or region):
            pass
        elif p0 in DOSE_NUM_START:
            # Un chiffre 1–4 devant « fois par jour » est à coup sûr une
            # fréquence de prise, même hors d'une région de liste dense (un
            # seul médicament dicté avec sa posologie) : la boucle ci-dessous
            # n'absorbe que le run exact (« 3 fois par jour » → TID), un
            # chiffre nu ou suivi d'autre chose reste intact.
            pass
        else:
            return None, i
        def is_dosing(p):
            return (p in DOSE_FREQ_SINGLE or p in DOSE_ROUTE_START
                    or any(pat[0] == p for pat, _ in DOSE_FREQ_PHRASES))
        if not is_dosing(p0):
            return None, i
        pieces, k = [], i
        while k < n:
            p = _norm_dose(words[k])
            matched = False
            for pat, canon in DOSE_FREQ_PHRASES:
                lp = len(pat)
                if k + lp <= n and all(_norm_dose(words[k + t]) == pat[t]
                                       for t in range(lp)):
                    pieces.extend(canon); k += lp; matched = True; break
            if matched:
                continue
            if p in DOSE_FREQ_SINGLE:
                pieces.append(DOSE_FREQ_SINGLE[p]); k += 1; continue
            break
        if not pieces:
            return None, i
        # Carry the trailing punctuation of the last consumed token so a
        # sentence never collapses ("deux fois par jour." -> "TID.").
        trail = "".join(re.findall(r"[^\w]+$", words[k - 1]))
        return " ".join(pieces) + trail, k

    def _canonicalize(self, level, base, brand, is_otc):
        """Return the canonical output name for a matched medication.

        OTC brands drop their brand and output the true active-ingredient
        generic (e.g. ASPIRIN STRENGTH -> aspirine). Exception: the TYLENOL
        line keeps its trade brand (tyrénol -> Tylenol), and a few other OTC
        brands render under their natural French common name.
        """
        if is_otc and base:
            bname = (brand or "").strip().upper()
            if bname.startswith("TYLENOL"):
                return tallman("Tylenol")   # keep the trade brand
            return tallman(self.OTC_DISPLAY.get(base, base))
        if level == "BASE_GENERIC":
            return base
        if level == "FULL_GENERIC":
            return base
        if level != "BRAND" or not brand:
            return base or brand
        # Rule 1: strip generic manufacturer prefix from brand
        b = norm_orth(brand)
        first = b.split()[:1][0] if b.split() else ""
        for pref in self.MANUFACTURER_PREFIXES:
            p_norm = norm_orth(pref)
            if b.startswith(p_norm):
                remainder = b[len(p_norm):].strip()
                if remainder in self.generics:
                    return self.FR_COMMON.get(remainder, remainder)
                # prefix is a stand-alone leading token like "APO" (e.g. APO X)
                if first == p_norm.split()[:1][0] if first else False:
                    remainder = b[len(p_norm.split()[:1][0]):].strip()
                    if remainder and remainder in self.generics:
                        return self.FR_COMMON.get(remainder, remainder)
        # Rule 1b: brand and base both carry the same manufacturer prefix
        # (e.g. APO-PERINDOPRIL, base "apo perindopril") -> bare generic core.
        bb = norm_orth(base) if base else ""
        for pref in self.MANUFACTURER_PREFIXES:
            p_norm = norm_orth(pref)
            if bb.startswith(p_norm):
                core = bb[len(p_norm):].strip()
                b_rest = b[len(p_norm):].strip() if b.startswith(p_norm) else ""
                if core and core in self.generics:
                    return self.FR_COMMON.get(core, core)
                if core and b_rest and core == b_rest:
                    return self.FR_COMMON.get(core, core)
        # Rule 2: strip presentation/salt tokens from brand; keep the leading
        # generic core when the remainder is a genuine generic AND non-empty.
        tokens = b.split()
        filtered = [t for t in tokens if t not in self.PRESENTATION_SALT_TOKENS]
        if 0 < len(filtered) < len(tokens):
            stripped = " ".join(filtered)
            if stripped in self.generics:
                return self.FR_COMMON.get(stripped, stripped)
        # Rule 3: brand name itself is a distinct base generic (genuine, since
        # self.generics excludes brand self-names)
        if b in self.generics:
            return self.FR_COMMON.get(b, b)
        # Default: return original brand, simply capitalized (no screaming caps)
        return title_brand(brand)

    def _resolve_single(self, token, fuzzy=True):
        """Return (level, base, brand, ortho_sim01, is_leaf, is_otc) or None.
        fuzzy=False => exact lookup only (O(1)); used outside medication-list regions."""
        t = norm_phon(token)
        if not t:
            return None
        # Article soudé (« l'Aldactone », « d'abitrate ») : le STT colle
        # l'article au nom SANS espace. On ne décape la clé que si la forme
        # complète ne résout déjà pas (jamais de « lasix » → « six » en
        # découpant « la ») et qu'on cible le NOM NU derrière l'article.
        # ``t_alt`` n'est essayé que si ``t`` ne frappe aucune table exacte.
        if t in self.exact_garble:
            level, base, brand, is_leaf, is_otc = self.exact_garble[t]
            return (level, base, brand, 1.0, is_leaf, is_otc)
        if t in self.exact:
            level, base, brand, is_leaf, is_otc = self.exact[t]
            return (level, base, brand, 1.0, is_leaf, is_otc)
        t_alt = _strip_glued_article(t, t)
        if t_alt != t:
            if t_alt in self.exact_garble:
                level, base, brand, is_leaf, is_otc = self.exact_garble[t_alt]
                return (level, base, brand, 1.0, is_leaf, is_otc)
            if t_alt in self.exact:
                level, base, brand, is_leaf, is_otc = self.exact[t_alt]
                return (level, base, brand, 1.0, is_leaf, is_otc)
        if not fuzzy:
            return None
        if len(t) < MIN_FUZZY_LEN:
            return None                     # short tokens: exact only, no fuzzy
        # Pour le FLOU, préférer la forme découpée (la plus proche du vrai nom).
        if t_alt != t and len(t_alt) >= MIN_FUZZY_LEN:
            t = t_alt
        best = None
        best_sim = 0.0
        L = len(t)
        # Candidats au-dessus du seuil, avec leur distance de Levenshtein brute
        # (rapidfuzz, rapide). On retient aussi la plus petite distance.
        cands = []
        lev_min = None
        for bln in range(max(1, L - MAX_LEN_DIFF), L + MAX_LEN_DIFF + 1):
            for n, level, base, brand, is_leaf, is_otc in self.ortho_by_len.get(bln, ()):
                s = sim(t, n)
                if s >= ORTHO_FLOOR:
                    d = lev(t, n)
                    if lev_min is None or d < lev_min:
                        lev_min = d
                    cands.append((n, level, base, brand, s, is_leaf, is_otc))
                    if s > best_sim:
                        best_sim = s
                        best = (level, base, brand, s, is_leaf, is_otc)
        if best is None:
            return None
        # Tie-break des substitutions proches : quand plusieurs candidats
        # partagent la plus petite distance de Levenshtein (typiquement des
        # paires /s/↔/z/, /f/↔/v/), on les départage par la distance pondérée
        # qui favorise les lettres articulatoirement proches. Sans ce rééqui-
        # librage, `sim` (1 - lev/max_len) fait gagner le candidat le plus
        # long — « esétrol » devenait « estetrol » (insertion d'un t) au lieu
        # d'« ezetrol » (ézétimibe, simple s→z).
        if len(cands) > 1:
            grp = [c for c in cands if lev(t, c[0]) == lev_min]
            if len(grp) > 1:
                g_best = None
                g_sim = 0.0
                for n, level, base, brand, _s, is_leaf, is_otc in grp:
                    sw = sim_ortho_w(t, n)
                    if g_best is None or sw > g_sim:
                        g_sim = sw
                        g_best = (level, base, brand, sw, is_leaf, is_otc)
                if g_best is not None:
                    best = g_best
        return best

    def _resolve_phonetic(self, token):
        """Secondary phonetic fallback (optional)."""
        if not self.use_phonetic:
            return None
        q = phonetic_fr(token)
        hits = self.bk.search(q, max_dist=3)
        # Filtre par `sim` (non pondéré) au seuil 0.6 pour ne pas laisser la
        # pondération faire franchir le seuil à des mots de prose (ex. « l'ivire »
        # → « l lysine », sim 0.571 < 0.6 mais sim_phon_w 0.629) ; on départage
        # ensuite les candidats retenus par `sim_phon_w` pour favoriser les
        # substitutions de lettres proches.
        best = None
        for dist, node in hits[:8]:
            if sim(q, node) < 0.6:
                continue
            s = sim_phon_w(q, node)
            if best is not None and s <= best[3]:
                continue
            row = self.bk_node.get(node)
            if row is not None:
                best = (row[0], row[1], row[2], s, row[3], row[4])
        return best

    def phonetiques_texte(self, texte, maxi=10, conf_keys=None):
        """Candidats PHONÉTIQUES pour le modèle de langage (bloc hints).

        Pour chaque jeton « ressemblant à un médicament » (ni français courant,
        ni ancre/protocole, pas un chiffre, longueur >= 5) NON déjà résolu
        exactement/par garble seedé, on interroge l'arbre BK phonétique (G2P
        français, dist <= 3) et on remonte le meilleur voisin (>= 0,60),
        non-feuille, non cosmétique. Le candidat est remis au LLM avec la
        posologie voisine et l'étiquette ``source: "phonetic"`` : c'est une
        PISTE à confirmer par le modèle, jamais une réécriture.

        ``maxi`` borne le nombre de candidats total (la phonétique brute est
        bruitée ; on ne donne que les pistes les mieux notées, les autres
        retombent sur le modèle seul).

        ``conf_keys``: itérable de clés ``norm_phon`` dont la transcription est
        DOUTEUSE (mots entendus avec incertitude par le STT). Pour ces tokens, le
        contexte « dose à portée » n'est pas exigé — le doute lui-même est la
        preuve qu'on est face à un nom susceptible d'avoir été déformé, et la
        proximité phonétique élevée (>= 0,80, non ensuite filtrée par la
        posologie) suffit à proposer la piste au modèle. Sans ce signal, une
        énumération de médicaments sans dose (« du ziprexa ») resterait muette.

        Retourne une liste de dicts compatibles ``extract_med_items`` (+ la clé
        ``source``) pour le rendu unique des hints.
        """
        if not self.use_phonetic:
            return []
        words = texte.split()
        n = len(words)
        dose_unit = [False] * n
        num_token = [False] * n
        for i, w in enumerate(words):
            if (POSOLOGY_RE.search(w) or DOSE_RE.search(w)
                    or w.strip(",;:.()").lower() in
                    {"mg", "mcg", "µg", "g", "ml", "unités", "unites", "ui",
                     "bid", "tid", "hs", "prn", "qid", "die", "po", "peros"}):
                dose_unit[i] = True
            if re.fullmatch(r"\d+(?:[,.]\d+)?", w.strip(",;:.()")):
                num_token[i] = True
        # Régions « liste de médicaments » confirmées : on y exige une dose à
        # portée pour admettre un débile/candidat phonétique ; hors région, un
        # nombre nu de lab/dossier ne doit jamais autoriser une piste.
        fragile = [False] * n
        for i, w in enumerate(words):
            pp = norm_phon(w)
            if (pp in FRENCH_STOP or pp in ANCHOR_WORDS or pp in PROTOCOL_WORDS
                    or words[i].isdigit()):
                fragile[i] = True
        region = self._medlist_regions(words, fragile, num_token, dose_unit)
        result: list = []
        vus = set()
        for i, w in enumerate(words):
            jet = w.strip(" \t,;:.()\"'’")
            p = norm_phon(jet)
            if not p or len(p) < 5:
                continue
            if p in FRENCH_STOP or p in ANCHOR_WORDS or p in PROTOCOL_WORDS:
                continue
            if p in _HINTS_PROSE:
                continue                  # prose structurante (« droite »,
                                          # « piles ») — jamais un med
            if p in self.exact_garble or p in self.exact:
                continue                  # déjà résolu déterministiquement
            # Contexte : une dose/ancre à portée ou un voisin chiffre petit —
            # sinon ça spatit de la prose sur la phonétique même bruitée. Un gros
            # nombre (>999) est une date/n° de dosier, jamais une doze.
            j = min(i + 3, n)
            d1 = _dose_nb(words[i + 1]) if i + 1 < n else None
            d2 = _dose_nb(words[i + 2]) if i + 2 < n else None
            dm = _dose_nb(words[i - 1]) if i > 0 else None
            num_apres = (i + 1 < n and num_token[i + 1] and d1 is not None
                         and d1 <= 999) or \
                        (i + 2 < n and num_token[i + 2] and d2 is not None
                         and d2 <= 999)
            contexte = (
                any(dose_unit[k] for k in range(i, j))
                or num_apres
                or (i > 0 and num_token[i - 1] and dm is not None and dm <= 999)
            )
            if not contexte:
                # Un jeton DOUTEUX (conf_keys) est admis sans dose à portée MAIS
                # uniquement au sein d'une région « liste de médicaments »
                # confirmée (``region[i]``) : le doute STT est la preuve d'un nom
                # possiblement déformé. Hors liste, la prose douteuse
                # (« Lontin », « continent », « visuelle ») ne doit jamais devenir
                # une piste médicament.
                if not (region[i] and conf_keys and p in conf_keys):
                    continue
            cand = self._phonetic_candidats(jet)
            if not cand:
                continue
            can, base, brand, s = cand
            poso = _dose_posology(texte, jet) or ""
            # Hors liste confirmée, la POSOLOGIE doit être crédible pour porter
            # la piste phonétique : un simple voisin numérique (valeur de lab
            # « 5,8 », n° de dossier) ou un « gouttes » isolé ne sont pas des
            # doses de médicament. Dans une région de liste, un nombre nu
            # suffit (dictée « …, Doxazocin. 4, … »).
            if not region[i] and not _poso_credible(poso):
                continue
            # Un candidat SANS TROUVE posologie crédible (vide, ou uniquement des
            # chiffres sans unité — n° de dossier/date) n'est admis que s'il est
            # très proche phonétiquement (>= 0,72, sépare les vrais garbles
            # kitsapine→quetiapine 0,78 du bruit de prose droite/piles 0,67).
            if poso and not re.search(
                    r"\b(mg|mcg|µg|g|ml|ui|unité|unites|comprimé|tid|bid|hs|prn|po|die)\b",
                    poso, re.I) and s < 0.72:
                continue
            # Le token douteux (conf_keys) sans posologie crédible est admis à
            # partir de 0,80 — le doute vient du STT, pas de la capacité du moteur
            # à trouver la dose. Un token NON douteux sans dose exige 0,72 (voir
            # ci-dessous) ; un douteux à 0,72-0,79 reste filtré (bruit de prose).
            if not poso and s < (0.80 if (conf_keys and p in conf_keys) else 0.72):
                continue
            # (canonical, base, brand, sim)
            if can is None or norm_phon(can) in vus:
                continue
            vus.add(norm_phon(can))
            result.append({
                "name": jet,              # le token déformé tel quel
                "base": base or brand or can,
                "brand": brand,
                "posology": poso,
                "score": int(round(s * 100)),
                "level": "BASE_GENERIC",
                "source": "phonetic",
            })
            if len(result) >= maxi:
                break
        # Passe des PAIRES adjacentes : un nom déformé peut éclater en deux
        # mots français courts que le STT épelle séparément (« très bas » →
        # TRESIBA, « la kro » → LYRICA). Chaque token seul (< 5 lettres) est
        # filtré par la boucle ci-dessus ; on re-sonde alors la paire collée.
        # RÉSERVÉE aux paires en contexte de dose (ou douteuses) : sans dose ni
        # doute, « très bien », « tout bas » ne deviennent pas un médicament.
        if len(result) < maxi:
            for i in range(n - 1):
                a = words[i].strip(" \t,;:.()\"'’")
                b = words[i + 1].strip(" \t,;:.()\"'’")
                pa = norm_phon(a)
                pb = norm_phon(b)
                # Au moins un des deux est court et « ressemble » au début de la
                # déformation ; le tout doit rester raisonnable en taille.
                paire = f"{a} {b}"
                paire_p = norm_phon(paire)
                if not paire_p:
                    continue
                if len(paire_p) < 5 or len(paire_p) > 14:
                    continue
                if paire_p in FRENCH_STOP or paire_p in self.exact or paire_p in self.exact_garble:
                    continue
                if len(pa) >= 5 or len(pb) >= 5:
                    continue                  # déjà couvert par l'unigramme
                # Contexte : une dose/unité à portée de la paire, ou un voisin
                # chiffre petit, ou un doute STT sur l'un des deux mots.
                j2 = min(i + 4, n)
                d2a = _dose_nb(words[i + 2]) if i + 2 < n else None
                d2b = _dose_nb(words[i + 3]) if i + 3 < n else None
                d2m = _dose_nb(words[i - 1]) if i > 0 else None
                contexte2 = (
                    any(dose_unit[k] for k in range(i, j2))
                    or (i + 2 < n and num_token[i + 2] and d2a is not None
                        and d2a <= 999)
                    or (i + 3 < n and num_token[i + 3] and d2b is not None
                        and d2b <= 999)
                    or (i > 0 and num_token[i - 1] and d2m is not None
                        and d2m <= 999)
                )
                if not contexte2 and not (region[i] and region[i + 1] and conf_keys and (pa in conf_keys or pb in conf_keys)):
                    continue
                cand2 = self._phonetic_candidats(paire)
                if not cand2:
                    continue
                can2, base2, brand2, s2 = cand2
                # Sans dose crédible, une paire doit être douteuse ET très
                # proche pour ne pas faire de la prose un médicament.
                poso2 = _dose_posology(texte, paire) or ""
                cred2 = _poso_credible(poso2)
                seuil2 = 0.72 if (cred2 or (conf_keys and (pa in conf_keys or pb in conf_keys))) else 0.80
                if s2 < seuil2:
                    continue
                if can2 is None or norm_phon(can2) in vus:
                    continue
                vus.add(norm_phon(can2))
                result.append({
                    "name": f"{a} {b}",
                    "base": base2 or brand2 or can2,
                    "brand": brand2,
                    "posology": poso2,
                    "score": int(round(s2 * 100)),
                    "level": "BASE_GENERIC",
                    "source": "phonetic",
                })
                if len(result) >= maxi:
                    break
        return result

    def _phonetic_candidats(self, token):
        """Meilleur candidat phonétique d'un token, ou ``None``.

        Retourne ``(canonical_display, base_generic, brand_name, sim)``. On
        exclut les feuilles de marques et les cosmétiques/UV, dont la
        phonétique rapproche souvent la prose.
        """
        q = phonetic_fr(token)
        if not q:
            return None
        voie = self.bk.search(q, max_dist=3)
        # Filtre par `sim` (non pondéré) au seuil 0.72 : garantit qu'un mot de
        # prose sans vraie parenté phonétique ne devient jamais un hint (la
        # pondération abaissant les coûts s/z, f/v… ferait franchir le seuil à
        # des mots courants : droite→thyroide, corps→Corax…). Parmi les
        # candidats retenus, on départage par `sim_phon_w` pour que la substi-
        # tution de lettres proches l'emporte (ex. « esétrol » → ezetrol, pas
        # estetrol).
        best = None
        for dist, node in voie[:12]:
            s = sim(q, node)
            if s < 0.72:
                continue
            sw = sim_phon_w(q, node)
            if best is not None and sw <= best[0]:
                continue
            row = self.bk_node.get(node)
            if row is None:
                continue
            level, base, brand, is_leaf, is_otc = row
            if is_leaf:
                continue
            if _is_cosmetic(base or brand):
                continue
            can = self._canonicalize(level, base, brand, bool(is_otc))
            if can is None:
                continue
            best = (sw, can, base or brand, brand)
        if best is None:
            return None
        return (best[1], best[2], best[3], best[0])

    def normalize(self, text, conf=None, inline_safe=False):
        # ``inline_safe`` — mode « texte envoyé au LLM » : on ne réécrit que les
        # substitutions déterministes et auditées (exact, alias STT seedés) ;
        # toute substitution orthographique FLOUE est laissée au modèle de
        # langage, qui la résout avec le contexte clinique (approche A —
        # « donner des outils au LLM »). Sans cela, le moteur remplace un mot
        # bien transcrit par un voisin orthographique lointain de la base BDP
        # (« Monocore » -> « nitrate de miconazole », note 15) : ajouter une
        # exception par faux positif devient intenable.
        # accent-folded copy for signal detection so "médicament" matches "medicament"
        flat = unicodedata.normalize("NFD", text)
        flat = "".join(c for c in flat if unicodedata.category(c) != "Mn").lower()
        words = text.split()
        n = len(words)
        dose_unit = [False] * n     # this token is dose+unit, a unit word, or a protocol
        num_token = [False] * n     # this token is a bare number (potential dose)
        for i, w in enumerate(words):
            if (POSOLOGY_RE.search(w) or DOSE_RE.search(w)
                    or w.strip(",;:.()").lower() in
                    {"mg","mcg","µg","g","ml","unités","unites","ui","bid","tid",
                     "hs","prn","qid","die","po","peros"}):
                dose_unit[i] = True
            if re.fullmatch(r"\d+(?:[,.]\d+)?", w.strip(",;:.()")):
                num_token[i] = True
        # ---- posology signal ----
        # A med token counts as dosed if a real dose+unit/protocol sits nearby,
        # or a bare number is glued directly after it AND the orthographic match is
        # near-certain. Dates like "10 juillet 2026" / prose numbers are excluded
        # because their ortho similarity to any med is too low.
        def posology(i, sim, in_region):
            j = min(i + 3, n)                 # allow dose+unit a little fuzz away
            # La preuve de posologie est DIRECTIONNELLE : elle peut venir APRÈS
            # le nom (« régulière HS »), d'un chiffre collé après le nom
            # (« aspirine 80 »), ou AVANT le nom (« administrer 25 mg HS de
            # kétiapine »). Une preuve par un marqueur de dose APRÈS un nom
            # FLOU n'est créditée que si le nom est quasi-certain (HIGH_SIM)
            # ou sert dans une région de liste confirmée : sans cela, la prose
            # « …de façon régulière HS… » ferait régulière -> REGULEX/docusate
            # en raflant le HS qui revient au vrai médicament précédent
            # (quétiapine, note 13). Retourne la direction ("avant"/"nombre"/
            # "arriere") ou "" — la direction sert au gate de confiance mot-à-mot.
            if any(dose_unit[k] for k in range(i, j)):
                if in_region or sim >= HIGH_SIM:
                    return "avant"
            if i + 1 < n and num_token[i + 1]:
                if sim >= 0.85:
                    return "nombre"
                # Inside a confirmed med-list region a name followed by a bare
                # dose number is a list entry (e.g. "pantoloque 40"), so a
                # moderate fuzzy match suffices there; outside the region the
                # near-certain 0.85 bar is kept to avoid prose / lab false hits.
                if in_region and sim >= ORTHO_FLOOR:
                    return "nombre"
            # Small backward window for dose-BEFORE-name phrasings
            # ("administrer 25 mg HS de kétiapine"): the dose marker sits up to
            # three tokens earlier. On n'utilise que les marqueurs de la MÊME
            # phrase que le token courant : la limite repousse APRÈS la dernière
            # ponctuation de phrase de la fenêtre (« …au coucher régulièrement.
            # Prochain point. » — le « coucher » d'avant la ponctuation ne
            # crédite pas « point. », note 13). Unit-only markers (mg/ml/g) must
            # see their number; protocol markers (HS/BID/PO/DIE/PRN) carry the
            # dose by themselves.
            borne = max(0, i - 3)
            for k in range(i - 1, borne - 1, -1):
                if words[k].endswith((".", "!", "?")):
                    borne = k + 1
                    break
            for k in range(borne, i):
                base_unit = words[k].strip(",;:.()").lower()
                # only real dose markers count; PROTOCOL_WORDS also holds
                # everyday particles (par/per/os/de) that must never credit
                if not (dose_unit[k] or base_unit in {"matin", "soir", "coucher"}):
                    continue
                if base_unit in {"mg", "mcg", "µg", "g", "ml", "unités", "unites"}:
                    if any(num_token[t] for t in range(k, i)):
                        return "arriere"
                    continue
                return "arriere"
            return ""
        anchor_hit = [False] * n
        token_start, idx = [], 0
        for w in words:
            st = text.find(w, idx)
            token_start.append(st); idx = st + len(w) + 1
        for am in ANCHOR_RE.finditer(flat):
            ae = am.end()
            for i, st in enumerate(token_start):
                if st >= ae and st <= ae + 14:
                    anchor_hit[i] = True

        # ---- medication-list regions (proactive) ----
        # Batch dictations pack many dose/anchor signals together; a token in a
        # region of dense dose/anchor signals is a medication list, where we run
        # aggressive matching (multi-token phrase joins). Narrative prose away
        # from these regions stays conservative.
        sig = [bool(a or d) for a, d in zip(anchor_hit, dose_unit)]
        proactive = [False] * n
        for i in range(n):
            if sum(sig[max(0, i - 5):i + 6]) >= 2:
                proactive[i] = True

        # tokens we must never use to open or absorb into a med phrase
        fragile = [False] * n
        for i, w in enumerate(words):
            p = norm_phon(w)
            if (p in FRENCH_STOP or p in ANCHOR_WORDS or p in PROTOCOL_WORDS
                    or words[i].isdigit()):
                fragile[i] = True

        # Confirmed medication-list regions (name+dose rhythm where the names
        # resolve to real meds). These catch bare comma-lists that the dense
        # dose/anchor window above misses ("... pantoloque 40, dipitar 40, ...").
        medlist = self._medlist_regions(words, fragile, num_token, dose_unit)
        for i in range(n):
            if medlist[i]:
                proactive[i] = True

        # Sentence-scoped "medication context": a list-introducer noun
        # ("dans ses médicaments", "médication", "posologie", "polypharmacie")
        # sets a wide but weak context -- it only credits near-certain matches
        # or tokens with a real dose, never the whole prose tail (so "pourra"
        # after "... ces médicaments." cannot float in on it).
        noun_seen = False
        noun_ctx = [False] * n
        for i in range(n):
            noun_ctx[i] = noun_seen
            if norm_phon(words[i]) in NOUN_CTX_WORDS:
                noun_seen = True
            if words[i].endswith((".", "!", "?")):
                noun_seen = False

        # ---- borne visuelle de la liste de médicaments ----
        # Dans le texte transcrit, on isole la zone normalisée par une ligne
        # vide avant et après : chaque run maximal de tokens ``proactive``
        # Qui contient au moins un token de liste confirmée (``medlist``) est
        # le bloc « liste de médicaments ». La prose dense des antécédents
        # (proactive mais AUCUN medlist, ex. « anémie, FA sous AOD, HTA… »)
        # n'est pas encadrée.
        encadrer = [False] * n
        # Rassemble d'abord les run « proactive contenant un medlist », puis
        # fusionne ceux séparés par peu de prose (<= 6 jetons) : une liste
        # dicte en prose (« ... sous Risperdal 2 et Epival 500 mg BID, ainsi
        # que trazodone 150 HS et Cipralex 20 ... ») ne doit pas produire une
        # ligne vide à chaque groupe de deux noms.
        blocs = []
        run_debut = None
        for i in range(n + 1):
            dans = i < n and proactive[i]
            if dans and run_debut is None:
                run_debut = i
            elif not dans and run_debut is not None:
                if any(medlist[k] for k in range(run_debut, i)):
                    blocs.append((run_debut, i - 1))
                run_debut = None
        if blocs:
            fusionnes = [list(blocs[0])]
            for s, e in blocs[1:]:
                if s - fusionnes[-1][1] <= 6:
                    fusionnes[-1][1] = e
                else:
                    fusionnes.append([s, e])
            for s, e in fusionnes:
                for k in range(s, e + 1):
                    encadrer[k] = True

        result, changes = [], []
        i = 0
        dans_liste = False
        while i < n:
            # Ligne vide avant/après la zone de liste de médicaments : quand on
            # entre dans la zone marquée, on isole sauf si c'est le tout début ;
            # quand on en sort, on ferme. Garde-fou : on ne cumule jamais deux
            # sauts de ligne consécutifs.
            if encadrer[i] and not dans_liste:
                if result and result[-1] != "\n\n":
                    result.append("\n\n")
                dans_liste = True
            elif not encadrer[i] and dans_liste:
                if result and result[-1] != "\n\n":
                    result.append("\n\n")
                dans_liste = False
            # Garble STT dont le nom commence par un nombre : « 13 IBA » =
            # Tresiba (le « 13 » est l'amorce phonétique « trési », PAS une
            # dose). Le couple nombre+mot est consommé comme un seul nom,
            # exact, déterministe — jamais le mot isolé (« IBA » seul en prose
            # reste IBA).
            if self.exact_garble_num and i + 1 < n:
                k = _norm_lead(f"{words[i]} {words[i + 1]}")
                if k in self.exact_garble_num:
                    level, base, brand, is_otc = self.exact_garble_num[k]
                    can = self._canonicalize(level, base, brand, is_otc)
                    if can:
                        trail = "".join(re.findall(r"[^\w]+$", words[i + 1]))
                        result.append(can + trail)
                        changes.append((" ".join(words[i:i + 2]), can, 100, 0.0))
                        i += 2
                        continue
            # Latin/French route-frequency suffix in a med list: normalize the
            # dosing part (per os -> PO, d'ye -> DIE, Q2 jours -> Q2jours ...)
            # before any med resolution, since these are protocol tokens.
            ds, de = self._dose_suffix_phrase(
                words, i, n, dose_unit, num_token, proactive, medlist)
            if ds is not None:
                result.append(ds)
                i = de
                continue
            # Proactive region: try to resolve a multi-token med phrase first.
            if proactive[i]:
                ph = self._resolve_phrase(words, i, n, dose_unit, num_token,
                                          anchor_hit, fragile,
                                          garble_seed_only=inline_safe)
                if ph is not None:
                    end, can, ph_score = ph
                    result.append(can)
                    changes.append((" ".join(words[i:end]), can, ph_score, 0.0))
                    i = end
                    continue
            # Hors zone proactive, on tente UNAÉ pas le garble multi-mots
            # seedé (exact, déterministe — « Hamelot d'épine » -> amlodipine
            # même hors d'une liste dense). Le gabarit phrase exige une dose
            # juste après, donc aucune prose ne peut être absorbée.
            else:
                ph = self._resolve_phrase(words, i, n, dose_unit, num_token,
                                          anchor_hit, fragile,
                                          garble_seed_only=True)
                if ph is not None:
                    end, can, ph_score = ph
                    result.append(can)
                    changes.append((" ".join(words[i:end]), can, ph_score, 0.0))
                    i = end
                    continue
            j = min(i + 2, n)
            j = min(i + 2, n)
            has_anchor = any(anchor_hit[k] for k in range(i, j))
            in_region = medlist[i]

            cand = self._resolve_single(words[i])
            if cand is None and self.use_phonetic:
                cand = self._resolve_phonetic(words[i])
            base_score = cand[3] if cand else 0.0
            is_leaf = cand[4] if cand else False
            is_otc = cand[5] if cand else False
            poso_dir = posology(i, base_score, in_region)
            has_poso = bool(poso_dir)

            w_phon = norm_phon(words[i])
            if (not cand or w_phon in ANCHOR_WORDS or
                    w_phon in PROTOCOL_WORDS or words[i].isdigit() or
                    len(w_phon) < 3):
                result.append(words[i]); i += 1; continue
            # Un garble STT seedé peut franchir ``FRENCH_STOP`` (« faire » ->
            # « fer », consult 10) MAIS uniquement quand une dose est voisine :
            # sans dose, le verbe « faire » de la prose reste ignoré — c'est le
            # même garde que ``is_leaf`` (un nom à part entière exige une dose
            # ou un ancre fort, jamais seule en prose).
            if w_phon in FRENCH_STOP and not (w_phon in self.exact_garble and has_poso):
                result.append(words[i]); i += 1; continue

            level, base, brand = cand[0], cand[1], cand[2]
            if base_score >= 0.99 and not is_leaf:
                score = 100                     # genuine drug name, exact
            elif base_score < ORTHO_FLOOR:
                score = 0
            else:
                # Being inside a confirmed med-list region is itself strong
                # anchor context, but only counts when the token is dosed (has
                # a following bare dose number) -- so undosed words that merely
                # sit inside a numeric run never get credit.
                region_credit = in_region and has_poso
                # Noun-introducer context ("dans ses médicaments ...") is weak:
                # it credits the 25 anchor points only for near-certain fuzzy
                # matches (HighSim) or tokens with a real dose nearby.
                has_noun = noun_ctx[i] and (base_score >= HIGH_SIM or has_poso)
                score = (PHONETIC_WEIGHT
                         + (ANCHOR_WEIGHT if (has_anchor or region_credit or has_noun) else 0)
                         + (POSOLOGY_WEIGHT if has_poso else 0))
                if not (has_anchor or has_poso or has_noun or region_credit):
                    score = 0                   # narrative gate
                elif is_leaf and not has_poso and not (has_anchor and base_score >= HIGH_SIM):
                    score = 0                   # leaves need a real dose, or a
                                                # direct high-confidence verb
                                                # anchor (tilénol -> Tylenol)

            replacement = self._canonicalize(level, base, brand, is_otc)
            replacement = tallman(replacement)
            # Lab-ion / electrolyte terms are only meds when they carry a real
            # posology (mg / dose+unit / route) nearby; in the lab section they
            # are plain values and must not be substituted.
            if norm_orth(replacement).replace(" ", "") in LAB_ION and not has_poso:
                score = 0
            # ---- confiance mot-à-mot (STT words[].confidence) ----
            # Une substitution FLOUE (ortho pas exacte — jamais l'exact ni les
            # garbles seedés) d'un token TRÈS CONFIDENT est refusée quand rien de
            # fort ne la porte — dose, ancre verbale, région médicament confirmée.
            # Le contexte NOMINAL (le simple fait d'être dans une phrase qui
            # parle de « médicaments ») n'est PAS compté : c'est précisément lui
            # qui fait passer les faux positifs de prose (alcool/laisse/diabète
            # à conf ~1.00). Seules les preuves physiques (dose, ancre, région)
            # épargnent un token à haute confiance du rejet. La confiance STT est
            # le meilleur séparateur prose/méd : les FP de prose flous ont une
            # conf ~1.00 et ne portent aucune dose ; les vrais garbles portés par
            # une dose (l'aldol+PRN, aspirine+80, oxycontin+210 mg) sont épargnés
            # par le contexte physique. Les collisions EXACTES (vitamine→vitamin
            # e, ces→C.e.s, magnésium→magnesium) ont la même confiance que les
            # vrais noms : elles relèvent de ``FRENCH_STOP``, pas de ce seuil.
            # Une preuve de posologie ne gratifie un token confiant du rejet que
            # lorsqu'elle vient d'AVANT le nom (dose posée derrière, la plus
            # probante : « administrer 25 mg HS de kétiapine ») — jamais d'une
            # simple preuve APRÈS (« régulière HS », note 13) ni d'un chiffre
            # collé : la haute confiance STT du mot suffit alors à le protéger.
            if (score >= THRESHOLD and base_score < 0.99 and conf is not None
                    and not (has_anchor or region_credit)
                    and not (has_poso and poso_dir == "arriere")):
                c = (conf.get(w_phon, 0.0) if isinstance(conf, dict) else
                     (conf[i] if i < len(conf) else 0.0))
                if isinstance(c, (int, float)) and c >= CONF_HARD_FLOOR:
                    score = 0
            # ---- inline_safe : refuser toute substitution non certaine (cf. plus haut) ----
            # Le token seul n'est réécrit que (a) si c'est un garble STT SEEDÉ
            # (auditable, déterministe : « sélexa » -> Celexa) ou (b) si la
            # correspondance est EXACTE d'un vrai nom non-feuille
            # (base_score ≈ 1,0, pas ``is_leaf``). Toute résolution orthographique
            # floue — y compris un BRAND_LEAF exact toléré (« comprimé » ->
            # acétaminophène, « Monocore » -> nitrate de miconazole) — est laissée
            # au modèle de langage, qui la tranche avec le contexte clinique.
            if inline_safe and w_phon not in self.exact_garble:
                if is_leaf or base_score < 0.99:
                    score = 0
            if replacement and score >= THRESHOLD and norm_orth(replacement).replace(" ", "") not in BAN_ORTH:
                trail = "".join(re.findall(r"[^\w]+$", words[i]))
                result.append(replacement + trail)
                changes.append((words[i], replacement, score, round(base_score, 3)))
            else:
                result.append(words[i])
            i += 1
        texte = " ".join(result)
        # Les sauts de ligne de la borne de liste portent les espaces du join :
        # on les reformate propres (ligne vide = deux \n).
        texte = re.sub(r"[ \t]+\n\n[ \t]+", "\n\n", texte)
        texte = re.sub(r"\n{3,}", "\n\n", texte)
        return texte.strip("\n") if texte else texte, changes


# ---------------------------------------------------------------------------
# API applicative — singleton thread-safe + extraction de la liste de méds
# ---------------------------------------------------------------------------
#: Verrou de construction : le ``Matcher`` est retardé au premier usage (l'init
#: charge la base en mémoire, ~0,5 s) et partagé ensuite. Après construction
#: l'objet est en lecture seule : ``normalize`` est thread-safe (vérifié).
_moteur: Matcher | None = None
_verrou_moteur = threading.Lock()


def is_available() -> bool:
    """La correction des médicaments peut-elle tourner (rapidfuzz + base) ?"""
    return _RAPIDFUZZ_OK and os.path.exists(DB)


def matcher() -> Matcher:
    """Le ``Matcher`` singleton du processus (construit une seule fois).

    Lève ``RuntimeError`` si ``rapidfuzz`` est absent du conteneur (image non
    reconstruite) : les appelants traitent l'absence en désactivant la
    fonctionnalité, jamais en faisant échouer un appel applicatif.
    """
    global _moteur
    if _moteur is None:
        if not _RAPIDFUZZ_OK:
            raise RuntimeError(
                "rapidfuzz est absent — correction des médicaments indisponible "
                "(reconstruire l'image)."
            )
        with _verrou_moteur:
            if _moteur is None:
                # L'arbre phonétique (G2P français) sert désormais à produire
                # les HINTS remis au modèle de langage (« candidats phonétiques
                # à confirmer ») : on le construit côté application. La
                # réécriture inline, elle, reste sur l'orthographe + seeds.
                _moteur = Matcher(db=DB, use_phonetic=True)
    return _moteur


def normalize(text: str, conf=None, inline_safe: bool = False) -> tuple:
    """Normalise les médicaments du texte → ``(texte_corrigé, changements)``.

    ``conf`` optionnel : mapping ``token → confiance`` (clé ``norm_phon``) ou
    liste parallèle aux mots du texte (``text.split()``) — active le refus de
    substitution floue pour les tokens très confiants sans contexte (voir
    ``CONF_HARD_FLOOR``).

    ``inline_safe`` : mode « résolution sûre » — seules les correspondances
    EXACTES et les garbles STT SEEDÉS sont réécrits ; les résolutions
    orthographiques floues sont laissées telles quelles. Le texte envoyé au
    modèle de langage est désormais BRUT (approche <CONFIANCE_MOTS> +
    suggestions) ; ce mode sert à l'extraction (``extract_med_items`` →
    hints des prompts et liste Validation), qui reste entière pour alimenter
    la liste pointée.

    ``changements`` = liste de ``(span, remplacement, score, ortho_sim)``,
    filtrée des auto-correspondances (``span == remplacement``) pour la
    lisibilité — consultez ``matcher().normalize`` pour la liste brute.
    """
    fixed, changes = matcher().normalize(text, conf=conf, inline_safe=inline_safe)
    changes = [
        (span, repl, score, ortho)
        for span, repl, score, ortho in changes
        if span and repl and norm_phon(span) != norm_phon(repl)
    ]
    return fixed, changes


#: Mots qui ne sont JAMAIS un nom de médicament même s'ils figurent dans la
#: base (protocoles, posologie, prose) — exclues de l'extraction de la liste.
_ITEM_STOP = {
    "die", "bid", "tid", "qid", "prn", "hs", "po", "peros", "q", "am", "pm",
    "mg", "mcg", "ug", "g", "ml", "ui", "sc", "dieam", "per",

}

#: Principes/filtres UV et bases cosmétiques : des noms de marques RAMASSÉS
#: (« MINUTES », « BASE », « SAGE »…) résolvent à ces substances en tant que
#: feuilles de marques cosmétiques (crème solaire, maquillage). Jamais un
#: médicament de la liste clinique — on les exclut.
_UV_COSMETICS = {
    "octisalate", "avobenzone", "octocrylene", "oxybenzone", "homosalate",
    "sulisobenzone", "ecamsule", "ensulizole", "mexoryl", "zinc oxide",
    "oxyde de zinc", "titanium dioxide", "dioxyde de titane", "enxasulfate",
    "padimate", "meradimate", "dioxybenzone", "octyl methoxycinnamate",
    "benzophenone", "phenylbenzimidazole", "avobenzone",
}


def _dose_nb(w):
    """Renvoie la valeur numérique d'un jeton-dose, ou ``None``.

    Le jeton peut porter la dose avec sa ponctuation de liste (« 6,25, »,
    « 20. ») — ``float`` brut, si :`` `` y reste, lèverait en
    ``phonetiques_texte`` et tuerait tout le hint phonétique (c'est le bug P0).
    On retire la ponctuation terminale avant conversion.
    """
    try:
        return float(w.strip(" \t,;:.()\"").replace(",", "."))
    except (TypeError, ValueError):
        return None


_DOSE_UNIT_RE = re.compile(
    r"\b(mg|mcg|µg|ug|g|ml|ui|unit|unite|unites|unité|unités|die|bid|tid|qid|"
    r"prn|hs|po|peros|am|pm)\b", re.I)
_DOSE_FORM_NUM_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(comprimé|comprimés|capsule|capsules|goutte|gouttes|"
    r"timbre|timbres|crème|pommade|ampoule|suppositoire)\b", re.I)


def _poso_credible(poso) -> bool:
    """Une posologie est-elle CRÉDIBLE (unité, fréquence, ou forme galénique
    QTEttée d'un nombre) ?

    Un électrolyte/valeur de lab (poso « 5,8 »), un symbole de syntaxe
    (« gouttes » isolé) ou un n° de dossier ne sont pas des posologies :
    sans une unité/fréquence ou un « 1 comprimé »/« 2 gouttes », ce n'est pas
    une dose de médicament, et la phonétique du token ne doit pas en porter
    la preuve.
    """
    if not poso:
        return False
    if _DOSE_UNIT_RE.search(poso):
        return True
    if _DOSE_FORM_NUM_RE.search(poso):
        return True
    return False


def _dose_posology(text: str, name: str) -> str:
    """Candidate posologie après ``name`` dans ``text`` (<= 8 jetons).

    S'autorise à sauter quelques mots de prose entre le nom et sa dose
    (« … prescrite à une dose de 12,5 mg BID PRN ») mais s'arrête au premier
    autre nom de médicament.

    Le nom est cherché comme MOT ENTIER (borné par des non-lettres) — jamais
    comme simple sous-chaîne : ``find('air')`` matcherait « aire » dans
    « métastatique ganglionnaire » et alignerait la posologie d'un autre nom
    (invention d'une dose « 2026 »). ``normalize`` réécrit le transcrit avec
    des espaces, la borne ``(?<![a-zà-ÿ])`` protège des slurres comme
    « kilomètre » pour « kilo ».
    """
    idx = -1
    pat = re.compile(
        rf"(?<![a-zà-ÿ]){re.escape(name)}(?![a-zà-ÿ])"
    )
    m = pat.search(text)
    if m:
        idx = m.start()
    if idx < 0:
        return ""
    tail = text[idx + len(name):]
    pieces: list = []
    sauts = 0
    for tok in re.split(r"\s+", tail.strip()):
        if not tok:
            break
        aplat = tok.strip(" \t,;:().")
        if not aplat:
            continue
        if aplat in (".", "!", "?"):
            break
        # Les chiffres comptent (dose !) mais norm_phon les élimine.
        if re.fullmatch(r"\d+(?:[.,]\d+)?", aplat):
            pieces.append(aplat)
            if len(pieces) >= 8:
                break
            continue
        p = norm_phon(aplat)
        if p in _ITEM_STOP or (p and p in _DOSE_WORDS):
            pieces.append(aplat)
            if len(pieces) >= 8:
                break
            continue
        # Un autre nom de médicament = fin de l'entrée courante.
        if _lookup_exact(aplat):
            break
        if not pieces:
            # Prose entre le nom et sa dose : passer outre, borné.
            sauts += 1
            if sauts > 6:
                break
            continue
        break
    return " ".join(pieces)


#: Génériques français OTC courants, non portés en alias dans la base BDP.
_FRENCH_OTC = {
    "aspirine": "aspirine",
    "acetaminophene": "acétaminophène",
    "acetaminophen": "acétaminophène",
    "paracetamol": "acétaminophène",
    "ibuprofene": "ibuprofène",
    "naproxene": "naproxène",
    "omeprazole": "oméprazole",
}

#: Mots de posologie francs (fréquence, route, unité) — à comparer en ÉGALITÉ,
#: jamais en sous-chaîne (« tachycardie » contient « die » et ne doit pas être
#: capturé comme posologie).
#: Formes galéniques (comprimé, capsule…) : le STT compte une posologie par
#: « 1 comprimé par jour » (Calcium+vitamine D) sans unité de dose — c'est une
#: VRAIE posologie, pas un chiffre orphelin.
_DOSE_FORM_WORDS = {
    "comprime", "comprimes", "capsule", "capsules", "goutte", "gouttes",
    "timbre", "timbre", "timbre", "timbres", "creme", "crème", "pommade",
    "tube", "injection", "ampoule", "suppositoire", "bouchonnette",
}
_DOSE_WORDS = {
    "mg", "mcg", "µg", "ug", "g", "ml", "unite", "unites", "unités", "units", "ui",
    "die", "bid", "tid", "qid", "prn", "po", "peros", "am", "pm", "hs",
    "matin", "soir", "coucher", "jour", "jours", "semaine", "fois",
    "quotidien", "quotidienne", "microgramme", "microgrammes", "q1sem", "q2j",
    "souscut", "souscutane", "souscutanee", "sc",
} | _DOSE_FORM_WORDS


def _strip_glued_article(t, fallback):
    """Décapite un article français collé en tête d'un nom normalisé, sinon
    renvoie ``fallback``.

    Le STT colle souvent l'article au nom sans espace (« l'Aldactone »,
    « d'erythrom »). Après ``norm_phon`` l'apostrophe a disparu : on devine les
    amorces d'articles courtes (1-2 lettres, surtout ``l``/``d`` des formes
    contractées l'/d'/la/du) suivies d'un nom plausible. On ne décape QUE ce
    qui ressemble à un article de 1-2 lettres et jamais en laissant un reste
    trop court ; et l'appelant ne passe ici que si la forme complète n'a pas
    déjà résolu (sinon « lasix » serait coupé en « la »+« six »).
    """
    if not t:
        return fallback
    kept = t
    for art in ("l", "d", "a", "au", "al", "le", "la", "les", "de", "du",
                "des", "un", "une"):
        if len(t) - len(art) < 4:          # reste trop court : pas un nom
            continue
        if t.startswith(art) and _is_medlike(t[len(art):]):
            kept = t[len(art):]
            break                           # première amorce plausible (article)
    return kept


def _is_medlike(s):
    """Heuristique : un préfixe découpé est plausiblement un nom de méd en lui-
    même (longueur raisonnable, commence souvent par une consonne aspirée)."""
    if len(s) < 3 or len(s) > 16:
        return False
    return re.match(r"^[bcdfghjklmnpqrstvwxyz]", s, re.I) is not None


def _lookup_exact(token: str) -> dict | None:
    """Résout un jeton (ou bigramme composé) en nom canonique (exact).

    Exclut les feuilles de marques simples (un mot d'une marque composite,
    ex. « LONG » de « LONG LASTING DRISTAN ») : en dehors de la correction
    elle-même, un item de liste doit être un vrai nom de médicament.
    """
    tok = token.strip(" \t,;:.()\"'")
    t = norm_phon(tok)
    if not t or len(t) < 3 or t in _ITEM_STOP:
        return None
    # Article soudé (« l'Aldactone », « d'elestrox ») : on essaie la clé telle
    # quelle, puis, si elle ne résout dans aucune table exacte, la clé découpée
    # de l'article. `_strip_glued_article` ne revient sur la forme complète que
    # si celle-ci ne matche pas (jamais « lasix » → « six »).
    t_alt = _strip_glued_article(t, t)
    ts = [t] + ([t_alt] if t_alt != t else [])
    # Mots de la langue courante / protocoles : jamais des noms de médicaments,
    # même s'ils figurent comme feuilles de marques dans la base.
    if t in FRENCH_STOP or t in ANCHOR_WORDS or t in PROTOCOL_WORDS:
        return None
    # OTC francophones courants, absents de la base BDP en tant que génériques
    # (l'EN est « acetylsalicylic acid », …) mais fréquents en dictée.
    if t in _FRENCH_OTC:
        return {"level": "BASE_GENERIC", "base": _FRENCH_OTC[t], "brand": None}
    m = matcher()
    # Composé multi-mots réel (espaces préservés) : « Vitamine D »,
    # « acide folique »… La clé normalisée garde l'espace ; un nom + dose
    # (« Trandate 10 », « bisoprolol 2,5 », « calcium 500 ») n'est PAS un
    # composé générique : le chiffre doit rester dans la posologie, jamais
    # dans le nom. On l'exclut d'emblée — sans quoi « norm_orth » éliminerait
    # le chiffre et le bigramme retomberait sur le nom seul.
    if " " in tok:
        # Un garble STT multi-mots seedé (« la six » → Lasix) : la clé
        # normalisée (``norm_phon``) garde TOUS les mots collés, contrairement
        # au composé générique ci-dessous qui teste ``norm_orth`` séparé. On le
        # vérifie avant de craquer un bigramme nom+dose (le chiffre, lui, reste
        # exclu des amORCEs et le ``norm_phon`` le perd — on ne matche donc pas
        # une dose dans l'exact garble).
        if t in m.exact_garble and not re.search(r"(?:\A|\s)\d", tok):
            level, base, brand, _leaf, _otc = m.exact_garble[t]
            if _is_cosmetic(base):
                return None
            return {"level": level, "base": base, "brand": brand}
        if re.search(r"(?:\A|\s)\d", tok):
            return None   # nom + dose : chiffre = posologie, pas le nom
        nspace = norm_orth(tok)
        if nspace in m._generic_compound:
            return {"level": "BASE_GENERIC", "base": nspace, "brand": None}
        if nspace in m.exact and m.exact[nspace][0] == "BASE_GENERIC":
            level, base, brand, _leaf, _otc = m.exact[nspace]
            return {"level": level, "base": base, "brand": brand}
        return None   # bigramme non composé : ni nom+dose, ni feuille
    if t in m.exact_garble:
        level, base, brand, _leaf, _otc = m.exact_garble[t]
        if _is_cosmetic(base):
            return None
        return {"level": level, "base": base, "brand": brand}
    if t in m.exact:
        level, base, brand, is_leaf, _otc = m.exact[t]
        if level == "FULL_GENERIC" or is_leaf:
            return None
        if _is_cosmetic(base or brand):
            return None
        return {"level": level, "base": base, "brand": brand}
    # Génériques français non portés en alias exact (aspirine, …) : la liste
    # des noms de substances actives construite à l'init.
    if t in m._generic_names:
        return {"level": "BASE_GENERIC", "base": t, "brand": None}
    return None


def _is_cosmetic(base_or_brand) -> bool:
    if not base_or_brand:
        return False
    cle = norm_orth(base_or_brand).replace(" ", "")
    return cle in {norm_orth(e).replace(" ", "") for e in _UV_COSMETICS}


def _append_item(items, vus, fixed, jeton, res, force_name=None,
                 ancre_poso=False, confiant=False) -> None:
    """Ajoute un item à la liste, dédupliqué par nom canonique.

    ``confiant`` : le jeton a été entendu par le STT avec une confiance >=
    ``CONF_PROSE_SURE``. Combiné à une résolution EXACTE (toujours vraie ici —
    on n'atteint cette fonction qu'après un ``_lookup_exact`` réussi), à une
    posologie non crédible et à l'absence d'ancrage, c'est de la PROSE SÛRE :
    le mot est déjà bien écrit dans le transcrit, il n'y a rien à suggérer
    (« Air Canada » ne doit pas produire un item « air »). Ne pas trouver un
    médicament mentionné en prose est acceptable — le LLM le voit tel quel.
    """
    base = res.get("base") or res.get("brand") or jeton
    cle = norm_phon(base)
    if cle in vus:
        return False
    poso = _dose_posology(fixed, force_name or jeton)
    # Électrolytes / valeurs de laboratoire : ne sont retenus comme
    # médicaments que munis d'une vraie posologie (unité + fréquence) ;
    # « Calcium 1,26 » du bilan est lab, « Calcium 500 mg PO DIE » est un
    # supplément.
    base_cle = norm_orth(base).replace(" ", "")
    if base_cle in LAB_ION and not re.search(
            r"\b(mg|mcg|µg|g|ml|ui|unit|die|bid|tid|qid|prn|hs|po|am|pm|"
            r"comprimé|comprimés|capsule|capsules|goutte|gouttes|timbre|timbres|"
            r"crème|pommade|ampoule|suppositoire)\b",
            poso, re.I):
        return False
    # Anti-fantôme : un nom de médicament NU (sans dose captée après, sans
    # ancre de dose adjacente, hors région de liste confirmée) est un
    # « fantôme » du STT canonisé (« diclofenac diethylamine »,
    # « naproxene », note 13) — il ne figure pas dans la liste Validation /
    # import. ``ancre_poso`` vaut vrai pour un token en région de liste
    # confirmée ou côte à côte d'un chiffre de dose (aspirine 80, calcium
    # 500, rivastigmine timbre 10).
    if not poso and not ancre_poso:
        return False
    # Prose sûre : jeton bien entendu (confiance haute), déjà bien écrit
    # (résolution exacte — cf. en-tête), hors région de liste et sans vrai
    # signal de dose -> on ne suggère rien. Ne s'applique JAMAIS à un vrai
    # médicament en liste (``ancre_poso``) ni à un nom non résolu exactement
    # (déformé) : ceux-là ont confiance en général < 0.95 ou une région.
    poso_credible = bool(re.search(
        r"\b(mg|mcg|µg|g|ml|ui|unit|die|bid|tid|qid|prn|hs|po|am|pm|"
        r"comprimé|comprimés|capsule|capsules|goutte|gouttes|timbre|timbres|"
        r"crème|pommade|ampoule|suppositoire)\b",
        poso, re.I))
    if confiant and not poso_credible and not ancre_poso:
        return False
    vus.add(cle)
    items.append({
        "name": force_name or jeton,
        "base": base,
        "posology": poso,
        "score": 100 if res["level"] in ("BRAND", "BASE_GENERIC") else 65,
        "level": res["level"],
    })
    return True


def _region_medlist(fixed: str) -> set:
    """Indices (dans ``fixed.split()``) des tokens de la région « liste de
    médicaments » confirmée, telle que vue par ``Matcher.normalize``.

    Réutilisée par l'extraction de la liste : un nom nu de prose n'est pas
    dans une région confirmée, un nom d'une liste dictée avec doses l'est.
    Le recalcul est léger (drapeaux de jeton + ``_medlist_regions``, aucun
    re-matching)."""
    words = fixed.split()
    n = len(words)
    dose_unit = [False] * n
    num_token = [False] * n
    for i, w in enumerate(words):
        if (POSOLOGY_RE.search(w) or DOSE_RE.search(w)
                or w.strip(",;:.()").lower() in
                {"mg", "mcg", "µg", "g", "ml", "unités", "unites", "ui", "bid",
                 "tid", "hs", "prn", "qid", "die", "po", "peros"}):
            dose_unit[i] = True
        if re.fullmatch(r"\d+(?:[,.]\d+)?", w.strip(",;:.()")):
            num_token[i] = True
    fragile = [False] * n
    for i, w in enumerate(words):
        p = norm_phon(w)
        if (p in FRENCH_STOP or p in ANCHOR_WORDS or p in PROTOCOL_WORDS
                or w.isdigit()):
            fragile[i] = True
    medlist = matcher()._medlist_regions(words, fragile, num_token, dose_unit)
    return {i for i, v in enumerate(medlist) if v}


def extract_med_items(text: str, conf=None) -> list:
    """Liste pointée des médicaments détectés dans ``text`` (texte corrigé).

    Chaque item : ``{"name", "posology", "score", "level"}``. Déterministe et
    dédupliqué par nom canonique. Sert de source à la fois à la liste live de
    dictée, à l'onglet « Validation » et à l'import.

    ``conf`` optionnel est relayé à ``normalize`` (gate mot-à-mot) : utile
    quand ``text`` n'a pas déjà été corrigé par le gate — évite qu'un faux
    positif de prose bloqué (diabète→Diabeta) resurgisse dans la liste.

    Reconnaît d'abord les NOMS COMPOSÉS en bigrammes (« Vitamine D », « acide
    folique »…) dont la concaténation normalisée existe en base, puis repasse
    token-à-token pour les simples.
    """
    fixed, _ = matcher().normalize((text or "").strip(), conf=conf,
                                   inline_safe=True)
    jetons = re.findall(r"[\wÀ-ÿ'-]+", fixed)
    items = []
    vus = set()
    consommes = set()
    region = _region_medlist(fixed)
    # Clés dont le STT est CONFIANT (>= CONF_PROSE_SURE) : servent au gate
    # « prose sûre » de ``_append_item`` — un jeton confiant et déjà exact
    # hors région n'est pas un médicament à suggérer.
    confiant_cles = set()
    if conf:
        try:
            confiant_cles = {k for k, v in conf.items()
                             if float(v) >= CONF_PROSE_SURE}
        except (TypeError, ValueError):
            confiant_cles = set()

    def chiffre_voisin(i):
        """Un chiffre de dose est collé au nom (avant/après, à <= 2 jetons) ?"""
        for d in (-2, -1, 1, 2):
            j = i + d
            if 0 <= j < len(jetons) and re.fullmatch(
                    r"\d+(?:[.,]\d+)?", jetons[j].strip(" \t,;:.()")):
                return True
        return False

# --- Bigrammes : noms composés ----------------------------------------
    for i in range(len(jetons) - 1):
        if i in consommes:
            continue
        paire = f"{jetons[i]} {jetons[i + 1]}"
        res = _lookup_exact(paire)
        if res is None:
            continue
        confiant_paire = (norm_phon(jetons[i]) in confiant_cles
                          and norm_phon(jetons[i + 1]) in confiant_cles)
        if _append_item(items, vus, fixed, paire, res,
                        force_name=" ".join((jetons[i], jetons[i + 1])),
                        ancre_poso=(i in region) or chiffre_voisin(i),
                        confiant=confiant_paire):
            consommes.add(i)
            consommes.add(i + 1)

    # --- Unigrammes : noms simples -----------------------------------------
    for i, jeton in enumerate(jetons):
        if i in consommes:
            continue
        res = _lookup_exact(jeton)
        if not res:
            continue
        _append_item(items, vus, fixed, jeton, res,
                     ancre_poso=(i in region) or chiffre_voisin(i),
                     confiant=norm_phon(jeton) in confiant_cles)
    return items


def extract_validation_items(text: str, conf=None, maxi_phon: int = 40) -> list:
    """Items de la liste « Validation » : médicaments résolus + candidats phonétiques.

    Rejoint les items déterministes de ``extract_med_items`` (noms normalisés,
    ``source`` absent) aux candidats PHONÉTIQUES de ``phonetiques_texte``
    (« Lirica » → LYRICA, « Norvasque » → NORVASC, étiquette ``source:
    "phonetic"``) : la Validation montre ainsi au médecin les deux strates —
    les corrections sûres et les pistes que le modèle de langage devra
    confirmer. Déduplique par ``norm_phon(base)`` pour ne jamais afficher deux
    fois le même médicament (déjà résolu → le candidat est écarté).

    ``conf`` et ``maxi_phon`` sont relayés tels quels (gate mot-à-mot de
    ``extract_med_items``, borne de ``phonetiques_texte``). Les clés douteuses
    de ``conf`` (< 0.95) servent de ``conf_keys`` à ``phonetiques_texte`` : un
    nom sans dose à portée mais entendu avec incertitude se voit proposer sa
    piste phonétique au modèle (voir ``Matcher.phonetiques_texte``).
    """
    items = extract_med_items(text, conf=conf)
    if not _RAPIDFUZZ_OK:
        return items
    conf_keys = set()
    if conf:
        try:
            conf_keys = {k for k, v in conf.items()
                         if float(v) < CONF_PROSE_SURE}
        except (TypeError, ValueError):
            conf_keys = set()
    try:
        phon = matcher().phonetiques_texte(text or "", maxi=maxi_phon,
                                           conf_keys=conf_keys or None)
    except Exception:
        return items
    vus = {norm_phon(i.get("base") or i.get("name")) for i in items}
    for h in phon:
        cle = norm_phon(h.get("base") or h.get("name"))
        if cle in vus:
            continue
        vus.add(cle)
        items.append(h)
    return items


def conf_par_token(text: str, words: list) -> dict:
    """Aligne ``words[]`` du STT (``{word, confidence}``) aux tokens du texte.

    Renvoie un mapping ``norm_phon(token) → confiance``. Le STT découpe
    souvent différemment du ``split()`` du texte (ponctuation, articles
    soudés, variantes typographiques) : on avance dans les deux listes en
    parallèle en se calant sur les amorces normalisées, et les tokens orphelins
    portent la confiance du mot STT le plus proche. Les tokens non
    alphabétiques (chiffres, symboles, dont ``norm_phon`` est vide) sont eux
    consommés de façon positionnelle s'ils correspondent à un mot STT aussi
    vide — sans être ajoutés au mapping — afin que la dérive d'alignement
    (toujours vraie pour ``w.startswith("")``) ne fasse pas dérailler le
    marcheur en aval. Le résultat sert au gate ``CONF_HARD_FLOOR`` de
    ``normalize(text, conf=...)`` ; un mot jamais joint (ou une liste vide)
    laisse la substitution inchangée — le gate n'est actif que là où la
    confiance est exploitable.
    """
    toks = text.split()
    result: dict = {}
    wpos = 0
    dernier = 1.0
    ph = [norm_phon((w.get("word") or "").strip()) for w in words]
    for tok in toks:
        t = norm_phon(tok)
        if not t:
            # Jeton non alphabétique (chiffre, symbole) : ``norm_phon`` est vide.
            # On consomme l'éventuel mot STT équivalent pour garder l'alignement
            # des deux listes, sans l'ajouter au mapping (clé vide, inexploitable).
            if wpos < len(words) and not ph[wpos]:
                wpos += 1
            continue
        if wpos < len(words):
            w = ph[wpos]
            if w == t:
                result[t] = words[wpos].get("confidence") or 1.0
                dernier = result[t]
                wpos += 1
                continue
            # Le token STT couvre le token texte (ou l'inverse) : on le joint.
            # ``w`` vide exclus : un chiffre/symbole ne doit jamais avaler le
            # token suivant (``t.startswith("")`` est toujours vrai).
            if w and (w.startswith(t) or t.startswith(w)
                      or (len(t) >= 4 and (t in w or w in t))):
                result[t] = words[wpos].get("confidence") or 1.0
                dernier = result[t]
                wpos += 1
                continue
            # Sinon le STT a peut-être inséré un mot (ponctuation) : on avance.
            if wpos + 1 < len(words) and w and ph[wpos + 1] == t:
                result[t] = words[wpos + 1].get("confidence") or 1.0
                dernier = result[t]
                wpos += 2
                continue
        result.setdefault(t, dernier)
    # Compléter les tokens que le passage parallèle a pu manquer.
    for tok in toks:
        t = norm_phon(tok)
        if not t or t in result:
            continue
        for w, c in zip(words, ph):
            if c == t:
                result[t] = w.get("confidence") or 1.0
                break
    return result


#: Seuil de signalement au LLM (sans audio). Plus permissif que le gate
#: déterministe ``CONF_HARD_FLOOR`` (0.92, anti-faux-positifs de prose) :
#: ici, on préfère donner au modèle un peu plus de mots douteux — il garde le
#: jugement final, et un surplus de doutes signale une correction à tenter, pas
#: une certitude d'erreur.
CONF_LLM_SEUIL = 0.90


def doutes_pour_texte(
    texte: str,
    conf_map,
    seuil: float = CONF_LLM_SEUIL,
    ignores=None,
) -> list:
    """
    Motss du texte GROUNDÉ sous le seuil de confiance, pour le LLM.



    ``conf_map`` : mapping ``norm_phon → confiance`` accumulé tranche par tranche
    (``consultation.transcript_conf``). Aligne le texte au mapping par clé
    ``norm_phon``, exactement comme ``conf_par_token`` le fait; les tokens
    orphelins (pas dans le mapping) ne sont pas signalés.



    ``ignores`` : clés ``norm_phon`` à ne jamais signaler — les noms que le
    grounding déterministe sur les médicaments vient de corriger (on ne demande
    pas au LLM de seconde-guess une correction déjà faite et auditable).

    Retourne la liste ``(mot, confiance, position)`` par position croissante ;
    ``position`` est l'index du mot dans ``texte.split()`` (pour la lisibilité et
    les tests), non utilisé par le prompt. La confiance y figure en [0,1[,
    arrondie à 3 décimales.

    Seuil compris : une valeur invalide (None, NaN…) est ignorée. Un
    mapping vide → aucune liste (on ne signale rien, le comportement
    historique s'applique).
    """
    result = []
    if not texte or not conf_map:
        return result
    ignores = ignores or set()
    try:
        seuil = float(seuil)
    except (TypeError, ValueError):
        return result
    toks = texte.split()
    for i, tok in enumerate(toks):
        cle = norm_phon(tok)
        if not cle or cle in ignores:
            continue
        valeur = conf_map.get(cle, None)
        if valeur is None:
            continue
        try:
            valeur = float(valeur)
        except (TypeError, ValueError):
            continue
        if valeur < seuil:
            result.append(
                {"mot": tok, "conf": round(valeur, 3), "position": i}
            )
    return result


def _extend_medlist_bare(words, medlist, resolve):
    """Étend une région de liste confirmée aux noms nus avoisinants.

    Soit ``medlist`` (drapeaux par jeton) calculé par ``_medlist_regions`` sur
    des noms munIS d'une dose. On repère chaque bloc confirmé (run contigu) et
    on y rattache, à gauche et à droite dans un court intervalle (<= 4 jetons
    sans dose), tout jeton qui se résout en un vrai médicament (non feuille,
    non lab-ion, non stop/protocole). Couvre les listes de transfert de dossier
    où un nom est dicté nu (« Doxazosin. », « Serpaline 50 ») — sans dose juste
    après. La confirmation préalable par doses protège la prose : on ne marque
    que des noms RÉSOLUS à proximité immédiate d'un vrai bloc de médicaments.
    """
    n = len(words)
    runs = []
    i = 0
    while i < n:
        if medlist[i]:
            s = i
            while i + 1 < n and medlist[i + 1]:
                i += 1
            runs.append((s, i))
        i += 1
    if not runs:
        return medlist
    out = medlist[:]
    for s, e in runs:
        lo = max(0, s - 4)
        hi = min(n, e + 5)
        for i in range(lo, hi):
            if out[i]:
                continue
            if medlist[i]:            # déjà marqué
                continue
            w = words[i]
            if not w or len(norm_phon(w)) < 5:
                continue
            if not w.isalpha():
                continue
            p = norm_phon(w)
            if p in FRENCH_STOP or p in ANCHOR_WORDS or p in PROTOCOL_WORDS:
                continue
            cand = resolve(w)
            if cand and not cand[4] and not cand[3] and not cand[5]:
                if norm_orth(w).replace(" ", "") not in LAB_ION:
                    out[i] = True
    return out
