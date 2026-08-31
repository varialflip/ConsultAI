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
        self._generic_compound = set()   # composés multi-mots (espaces préservés)
        seen = set()
        for alias, atype, level, base, brand, is_otc in rows:
            n = norm_phon(alias)
            is_leaf = (atype == "BRAND_LEAF")
            if not n:
                continue
            if atype == "STT_GARBLE":
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
            if n not in seen:
                seen.add(n)
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
                self.bk.add(phonetic_fr(" ".join(n)))

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
        L = len(t)
        for bln in range(max(1, L - MAX_LEN_DIFF), L + MAX_LEN_DIFF + 1):
            for n, level, base, brand, is_leaf, is_otc in self.ortho_by_len.get(bln, ()):
                s = sim(t, n)
                if s >= ORTHO_FLOOR and (best is None or s > best[3]):
                    best = (level, base, brand, s, is_leaf, is_otc)
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
                    if phonetic_fr(" ".join(n)) == node:
                        best = (level, base, brand, s, is_leaf, is_otc); break
        return best

    def normalize(self, text, conf=None):
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
            if any(dose_unit[k] for k in range(i, j)):
                return True
            if i + 1 < n and num_token[i + 1]:
                if sim >= 0.85:
                    return True
                # Inside a confirmed med-list region a name followed by a bare
                # dose number is a list entry (e.g. "pantoloque 40"), so a
                # moderate fuzzy match suffices there; outside the region the
                # near-certain 0.85 bar is kept to avoid prose / lab false hits.
                if in_region and sim >= ORTHO_FLOOR:
                    return True
            # Small backward window for dose-BEFORE-name phrasings
            # ("administrer 25 mg HS de kétiapine"): the dose marker sits up to
            # three tokens earlier. A marker attached to sentence punctuation
            # belongs to the previous sentence and never counts ("8,1%. Le
            # reste ..."). Unit-only markers (mg/ml/g) must see their number;
            # protocol markers (HS/BID/PO/DIE/PRN) carry the dose by themselves.
            for k in range(max(0, i - 3), i):
                if words[k].endswith((".", "!", "?")):
                    continue
                base_unit = words[k].strip(",;:.()").lower()
                # only real dose markers count; PROTOCOL_WORDS also holds
                # everyday particles (par/per/os/de) that must never credit
                if not (dose_unit[k] or base_unit in {"matin", "soir", "coucher"}):
                    continue
                if base_unit in {"mg", "mcg", "µg", "g", "ml", "unités", "unites"}:
                    if any(num_token[t] for t in range(k, i)):
                        return True
                    continue
                return True
            return False
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
            has_poso = posology(i, base_score, in_region)

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
            if (score >= THRESHOLD and base_score < 0.99 and conf is not None
                    and not (has_poso or has_anchor or region_credit)):
                c = (conf.get(w_phon, 0.0) if isinstance(conf, dict) else
                     (conf[i] if i < len(conf) else 0.0))
                if isinstance(c, (int, float)) and c >= CONF_HARD_FLOOR:
                    score = 0
            if replacement and score >= THRESHOLD and norm_orth(replacement).replace(" ", "") not in BAN_ORTH:
                trail = "".join(re.findall(r"[^\w]+$", words[i]))
                result.append(replacement + trail)
                changes.append((words[i], replacement, score, round(base_score, 3)))
            else:
                result.append(words[i])
            i += 1
        return " ".join(result), changes


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
                _moteur = Matcher(db=DB)
    return _moteur


def normalize(text: str, conf=None) -> tuple:
    """Normalise les médicaments du texte → ``(texte_corrigé, changements)``.

    ``conf`` optionnel : mapping ``token → confiance`` (clé ``norm_phon``) ou
    liste parallèle aux mots du texte (``text.split()``) — active le refus de
    substitution floue pour les tokens très confiants sans contexte (voir
    ``CONF_HARD_FLOOR``).

    ``changements`` = liste de ``(span, remplacement, score, ortho_sim)``,
    filtrée des auto-correspondances (``span == remplacement``) pour la
    lisibilité — consultez ``matcher().normalize`` pour la liste brute.
    """
    fixed, changes = matcher().normalize(text, conf=conf)
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


def _dose_posology(text: str, name: str) -> str:
    """Candidate posologie après ``name`` dans ``text`` (<= 8 jetons).

    S'autorise à sauter quelques mots de prose entre le nom et sa dose
    (« … prescrite à une dose de 12,5 mg BID PRN ») mais s'arrête au premier
    autre nom de médicament.
    """
    idx = text.find(name)
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
_DOSE_WORDS = {
    "mg", "mcg", "µg", "ug", "g", "ml", "unite", "unites", "unités", "units", "ui",
    "die", "bid", "tid", "qid", "prn", "po", "peros", "am", "pm", "hs",
    "matin", "soir", "coucher", "jour", "jours", "semaine", "fois",
    "quotidien", "quotidienne", "microgramme", "microgrammes", "q1sem", "q2j",
    "souscut", "souscutane", "souscutanee", "sc",
}


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
    # (« Trandate 10 ») n'est PAS un composé générique et reste exclu.
    if " " in tok:
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


def _append_item(items, vus, fixed, jeton, res, force_name=None) -> None:
    """Ajoute un item à la liste, dédupliqué par nom canonique."""
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
            r"\b(mg|mcg|µg|g|ml|ui|unit|die|bid|tid|qid|prn|hs|po|am|pm)\b",
            poso, re.I):
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
    fixed, _ = matcher().normalize((text or "").strip(), conf=conf)
    jetons = re.findall(r"[\wÀ-ÿ'-]+", fixed)
    items = []
    vus = set()
    consommes = set()

    # --- Bigrammes : noms composés ----------------------------------------
    for i in range(len(jetons) - 1):
        if i in consommes:
            continue
        paire = f"{jetons[i]} {jetons[i + 1]}"
        res = _lookup_exact(paire)
        if res is None:
            continue
        if _append_item(items, vus, fixed, paire, res, force_name=" ".join((jetons[i], jetons[i + 1]))):
            consommes.add(i)
            consommes.add(i + 1)

    # --- Unigrammes : noms simples -----------------------------------------
    for i, jeton in enumerate(jetons):
        if i in consommes:
            continue
        res = _lookup_exact(jeton)
        if not res:
            continue
        _append_item(items, vus, fixed, jeton, res)
    return items


def conf_par_token(text: str, words: list) -> dict:
    """Aligne ``words[]`` du STT (``{word, confidence}``) aux tokens du texte.

    Renvoie un mapping ``norm_phon(token) → confiance``. Le STT découpe
    souvent différemment du ``split()`` du texte (ponctuation, articles
    soudés, variantes typographiques) : on avance dans les deux listes en
    parallèle en se calant sur les amorces normalisées, et les tokens orphelins
    portent la confiance du mot STT le plus proche. Le résultat sert au gate
    ``CONF_HARD_FLOOR`` de ``normalize(text, conf=...)`` ; un mot jamais joint
    (ou une liste vide) laisse la substitution inchangée — le gate n'est actif
    que là où la confiance est exploitable.
    """
    toks = text.split()
    result: dict = {}
    wpos = 0
    dernier = 1.0
    ph = [norm_phon((w.get("word") or "").strip()) for w in words]
    for tok in toks:
        t = norm_phon(tok)
        if not t:
            continue
        if wpos < len(words):
            w = ph[wpos]
            if w == t:
                result[t] = words[wpos].get("confidence") or 1.0
                dernier = result[t]
                wpos += 1
                continue
            # Le token STT couvre le token texte (ou l'inverse) : on le joint.
            if w.startswith(t) or t.startswith(w) or (len(t) >= 4 and (t in w or w in t)):
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
        if t not in result:
            for w, c in zip(words, ph):
                if c == t:
                    result[t] = w.get("confidence") or 1.0
                    break
    return result
