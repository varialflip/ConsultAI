#!/usr/bin/env python3
"""Deterministic, auditable medication-normalization engine.

Primary signal : orthographic fuzzy match (accent-folded Levenshtein) over the
                 DPD brand/generic names -- matches the letter-substitution
                 pattern of real ASR drug-name garble (Trendate->TRANDATE,
                 Aricepte->ARICEPT, Pantolot->PANTOLOC).
Secondary      : rule-based French G2P -> phoneme BK-tree, optional (--phonetic).
                 Kept behind a flag because naive G2P is noisy; ortho is primary.
Scoring        : ortho/exact 100; else PHONETIC(ortho floor) + ANCHOR + POSOLOGY,
                 requires a context signal in narrative prose, gate S>=65.
"""
import re, sqlite3, unicodedata
from rapidfuzz.distance import Levenshtein

DB = "./meds.sqlite"
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

# ---------------------------------------------------------------- normalization
def norm_orth(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z]+", " ", s.lower()).strip()

def norm_phon(s):
    return re.sub(r"[^a-z]", "", norm_orth(s))

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
    # gu+voyelle : le « u » de « gu » est muet devant e/i/é (durcit le g :
    # « admelogue » = /admelɔg/, pas /admelɔgye/) ; devant a/o il forme /gw/
    # (« proguanil » = /pʁɔgwanil/). Règle d'orthographe française standard.
    ("gué", "g"), ("gue", "g"), ("gui", "g"), ("gü", "g"),
    ("gua", "gwa"), ("guo", "gwɔ"),
    # c DOUX devant e/i/y : la règle naïve « c »→/k/ force « cinémète » →
    # /kinemete/, collision à égalité avec KINERET (anakinra) — un candidat
    # absurde pour une maladie de Parkinson. En français le « c » devant
    # e/i/y se prononce /s/ : « cinémète » → /sinemete/ remonte alors
    # Sinemet (lévodopa) comme candidat UNIQUE. La tri par longueur place
    # d'office ces règles 2-caractères avant le « c » dur — un « c » devant
    # a/o/u (ka, ko, ku) reste /k/.
    ("cia", "sja"), ("cie", "sje"), ("cio", "sjo"), ("ceu", "so"),
    ("ce", "se"), ("ci", "si"), ("cy", "si"),
    ("cé", "se"), ("cè", "sɛ"), ("cê", "sɛ"), ("cë", "se"),
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

# ---------------------------------------------------------------- distance
def lev(a, b):
    """Levenshtein distance (edit distance), C-accelerated by rapidfuzz.
    Identical values to the previous pure-Python DP, verified on random strings."""
    return Levenshtein.distance(a, b)

def sim(a, b):
    m = max(len(a), len(b))
    return 0.0 if m == 0 else 1.0 - lev(a, b) / m

# ---------------- similarité orthographique pondérée (miroir app/med_grounding)
#
# Le STT produit surtout des fautes auditives : la substitution entre lettres
# proches (/s/↔/z/, /f/↔/v/…) est plus plausible qu'une insertion. Or sim
# (1 - lev/max_len) départage mal deux candidats à distance de Levenshtein
# égale (il favorise le plus long). On départage à distance égale par une
# distance pondérée qui pénalise moins les lettres articulatoirement proches
# (ex. « esétrol » → « ezetrol » = ézétimibe, au lieu de la seule insertion
# réussie « estetrol »).
_ORTHO_SUB_PAIRS = ("sz", "fv", "ck", "cs", "gj", "bd", "pt", "iy")
_ORTHO_SUB = {frozenset(p): 0.4 for p in _ORTHO_SUB_PAIRS}

def _ortho_sub_cost(a, b):
    return _ORTHO_SUB.get(frozenset((a, b)), 1.0)

def _dp_subst(a, b, sub_cost):
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

def sim_ortho_w(a, b):
    m = max(len(a), len(b))
    if m == 0:
        return 0.0
    return 1.0 - _dp_subst(a, b, _ortho_sub_cost) / m

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
                    "pm", "unites", "unite", "ui", "sc"}
DOSE_FRENCH_START = {"jour", "jours", "semaine", "matin", "soir", "coucher",
                     "par", "fois", "deux", "trois", "quatre", "une", "au",
                     "le", "de", "sous"}
#: Fréquence dictée en CHIFFRE (« 3 fois par jour ») : amorce identique à la
#: variante en lettres, exige le run complet (1–4 × « fois par jour »). Un
#: chiffre nu (« …il prend 3 comprimés… ») ne s'absorbe jamais.
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
    # note 6 (délirium ICU) prose collisions
    "heure", "heures", "laisse", "laissee", "laisser", "laissait",
    "médicaments", "medicaments", "médication", "medication", "polypharmacie",
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
        self.ortho = []          # (norm_phon, level, base, brand, is_leaf, is_otc) deduped
        self.ortho_by_len = {}   # len(norm_phon) -> [(n, ...)] for fast phrase lookup
        self._generic_names = set()   # genuine generic orthography names
        # Miroir de app/med_grounding : un niveau générique écrase une feuille de
        # marque de même ``norm_phon`` (sinon « trasodone » → marque parasite).
        seen = {}
        for alias, atype, level, base, brand, is_otc in rows:
            n = norm_phon(alias)
            is_leaf = (atype == "BRAND_LEAF")
            if not n:
                continue
            if atype == "STT_GARBLE":
                self.exact_garble.setdefault(n, (level, base, brand, False, bool(is_otc)))
            if level in ("BASE_GENERIC", "FULL_GENERIC") and base:
                self._generic_names.add(norm_orth(base))
            # Generic-level aliases win over brand leaves on exact collisions
            prev = self.exact.get(n)
            if n not in self.exact or (prev is not None and prev[3] and not is_leaf):
                self.exact[n] = (level, base, brand, is_leaf, bool(is_otc))
            if re.search(r"\d|%|mg|mcg|µg|ml|tablet|tab|caplet|capsule|cream|gel|"
                         r"patch|syringe|injection|solution|suppository", alias, re.I):
                continue                       # junk aliases: exact only
            if n not in seen or (seen[n][3] and not is_leaf):
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
            for n, level, base, brand, _, _ in self.ortho:
                self.bk.add(phonetic_fr(n))

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
                        fragile):
        """Proactive multi-token med-phrase resolution (medication-list regions
        only). Joins up to 3 content tokens bridged by French stop-words (e.g.
        "Perrin de prille" -> perindopril) and accepts a match only when a
        dose follows immediately and the hit is high-confidence and unique, so
        ordinary prose / anchored narrative is never absorbed."""
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
        if t in self.exact_garble:
            level, base, brand, is_leaf, is_otc = self.exact_garble[t]
            return (level, base, brand, 1.0, is_leaf, is_otc)
        if t in self.exact:
            level, base, brand, is_leaf, is_otc = self.exact[t]
            return (level, base, brand, 1.0, is_leaf, is_otc)
        if not fuzzy:
            return None
        if len(t) < MIN_FUZZY_LEN:
            return None                     # short tokens: exact only, no fuzzy
        best = None
        best_sim = 0.0
        L = len(t)
        # Candidats au-dessus du seuil, avec leur distance de Levenshtein brute.
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
        # Tie-break des substitutions proches, comme dans app/med_grounding.py :
        # à distance de Levenshtein égale, la distance pondérée favorise les
        # lettres proches (« esétrol » → « ezetrol » = ézétimibe).
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
        best = None
        for dist, node in hits[:8]:
            s = sim(q, node)
            if s >= 0.6 and (best is None or s > best[3]):
                for n, level, base, brand, is_leaf, is_otc in self.ortho:
                    if phonetic_fr(n) == node:
                        best = (level, base, brand, s, is_leaf, is_otc); break
        return best

    def normalize(self, text):
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

        result, changes = [], []
        i = 0
        while i < n:
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
                                          anchor_hit, fragile)
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
            has_poso = bool(posology(i, base_score, in_region))

            w_phon = norm_phon(words[i])
            if (not cand or w_phon in FRENCH_STOP or w_phon in ANCHOR_WORDS or
                    w_phon in PROTOCOL_WORDS or words[i].isdigit() or
                    len(w_phon) < 3):
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
            if replacement and score >= THRESHOLD and norm_orth(replacement).replace(" ", "") not in BAN_ORTH:
                trail = "".join(re.findall(r"[^\w]+$", words[i]))
                result.append(replacement + trail)
                changes.append((words[i], replacement, score, round(base_score, 3)))
            else:
                result.append(words[i])
            i += 1
        return " ".join(result), changes


def main():
    import sys
    args = [a for a in sys.argv[1:]]
    use_phon = "--phonetic" in args
    path = next((a for a in args if not a.startswith("--")), "dictee-1-cohere.txt")
    text = open(path).read()
    m = Matcher(use_phonetic=use_phon)
    fixed, changes = m.normalize(text)
    print(f"== output ({path}) [phonetic={use_phon}] ==")
    print(fixed)
    print("\n== audited changes ==")
    for span, repl, score, ortho in changes:
        print(f"  {span!r:26} -> {repl!r:22}  S={score} ortho={ortho}")


if __name__ == "__main__":
    main()
