# Médication Grounding — « dictée → médicaments normalisés »

Module autonome et **déterministe** de normalisation des médicaments pour le
français médical transcrit par reconnaissance vocale (STT). Il prend un
transcript **brut** (la sortie du modèle ASR, avec ses déformations
phonétiques : `Trendate`, `Aricepte`, `pantoloque`, `tyrénol`, `perotidien` …)
et réécrit les noms de médicaments dans leur forme valide (nom commun français,
marque, posologie canonique), sans toucher au reste du texte.

Créé pour `ConsultAI` (dictée de consultations) mais **standalone** : il ne
dépend d'aucune partie de l'application. Ce dossier est livré dans la branche
`selfhosted` (instance `test.dictai.ca`) pour être repris/configuré depuis la
machine.

---

## 1. Vue d'ensemble

```
[ audio ] --STT--> transcript brut (*-cohere.txt)
                       |
                       v
               fix_medical.py  (corrections phonétiques ciblées: tyrénol→Tylenol…)
                       |
                       v
               match_meds.py   (moteur de grounding : orthographique + phonétique)
                       |
                       v
              transcript normalisé (liste de médicaments en CAPS/po/…)
```

Le cœur est **`match_meds.py`**, un moteur déterministe et auditable :

- **Signal primaire** : correspondance floue orthographique (Levenshtein à
  repli d'accents) sur les noms de marques/génériques de la **Base de données
  sur les produits pharmaceutiques (BDP/D PD)** canadienne. Il colle au motif
  de substitution de lettres des vraies erreurs ASR (`Trendate→TRANDATE`,
  `Aricepte→ARICEPT`, `Pantolot→PANTOLOC`).
- **Signal secondaire (optionnel `--phonetic`)** : G2P français règle → arbre
  BK de phonèmes. Gardé derrière un drapeau car le G2P naïf est bruité ;
  l'orthographe est le signal principal.
- **Scoring** : ortho/exact = 100 ; sinon PHONETIC (seuil ortho) + ANCHOR +
  POSOLOGY, exige un signal de contexte dans la prose narrative, seuil S ≥ 65.
- **Posologie** : phrases « per os »→`PO`, « dy »→`DIE`, « Q2 jours »→`Q2J`,
  « Q semaine »→`Q1SEM`, « unités »→`UI`, « sous-cut »→`S/C` … (voir plus bas).

Il est **reproductible** : `meds.sqlite` se reconstruit depuis `dpd/` +
`dpd_ia/` (extraits BDP Canada) via `build_db.py` + `seed_aliases.py` +
`prune_db.py`.

---

## 2. Arborescence

| Fichier/dossier | Rôle |
|---|---|
| `match_meds.py` | **Moteur de grounding** (le script principal). |
| `fix_medical.py` | Corrections phonétiques ciblées *avant* le grounding (glossaire de substitutions). |
| `build_db.py` | Construit `meds.sqlite` à partir de `dpd/` + `dpd_ia/` (données BDP Canada). |
| `seed_aliases.py` | Rajoute des alias « STT_GARBLE » (garbles phonétiques observés → médicament BDP). |
| `prune_db.py` | Nettoie `meds.sqlite` (alias de présentation/dose parasites) pour un matching rapide. |
| `audit_medical.py` | Affiche les différences ligne à ligne entre transcript brut et corrigé (contrôle qualité). |
| `meds.sqlite` | Base de données médicaments (marques + génériques + alias) **préconstruite et prête à l'emploi**. |
| `dpd/`, `dpd_ia/` | Extraits texte de la BDP (Drug, Ingredient, Schedule… ; `_ia` = produits annulés/inactifs). |
| `meds_grounded/` | Exemples de sortie de référence (`*-grounded.txt`). |
| `*_cohere.txt` | Transcripts **bruts** de référence (entrées de test). |
| `transcribe.py` | (Contexte, **Apple Silicon uniquement**) Produit un transcript brut `*-cohere.txt` via le modèle Cohere. |
| `normalize_medical.py` | (Alternative) Nettoyage par LLM local (7B) — autre approche, non incluse dans le cœur. |
| `venv/` | Environnement virtuel (créé sur la machine ; **gitignoré**, ne pas committer). |

> `transcribe.py` et `normalize_medical.py` dépendent de paquets **Apple
> uniquement** (`mlx_audio`, `mlx_lm`) — ils ne fonctionnent **pas** sur ce
> serveur Linux. Ils servent à produire des transcripts bruts sur un Mac si
> besoin. Le grounding (`match_meds.py`) n'a besoin que de `rapidfuzz`.

---

## 3. Prérequis & installation (une seule fois)

Le serveur (Linux) a **Python 3.9** ; il manquait `rapidfuzz`. Un `venv` a déjà
été créé dans ce dossier. Sinon, le recréer :

```bash
cd ~/ConsultAI-selfhosted/med_grounding
python3 -m venv venv
./venv/bin/pip install rapidfuzz          # seule dépendance du moteur
```

Vérifier : `./venv/bin/python -c "import rapidfuzz"` ne doit rien afficher
d'erreur.

---

## 4. Utilisation — grounding d'un transcript

Entrée = un transcript **brut** STT (fichier texte UTF-8). Sortie = le même
texte avec les médicaments normalisés (affichée sur la sortie standard, avec la
liste des changements audités).

Avec correction phonétique (recommandé) :

```bash
cd ~/ConsultAI-selfhosted/med_grounding
./venv/bin/python match_meds.py --phonetic dictee-1-cohere.txt
```

Sans la passe phonétique (plus sûr, un peu moins couvrant) :

```bash
./venv/bin/python match_meds.py dictee-1-cohere.txt
```

**Important :** les données sont référencées par des chemins **relatifs**
(`DB = "./meds.sqlite"`). Il faut donc lancer la commande **depuis ce dossier**
(`cd ~/ConsultAI-selfhosted/med_grounding`), sinon le script ne trouvera pas la
base.

Pour obtenir un fichier de sortie réutilisable :

```bash
./venv/bin/python match_meds.py --phonetic dictee-1-cohere.txt > dictee-1-grounded-new.txt
```

### Comparer brut vs corrigé (contrôle qualité)

```bash
./venv/bin/python audit_medical.py dictee-1-cohere.txt dictee-1-grounded-new.txt
```

Les exemples de référence sont dans `meds_grounded/` (par ex.
`meds_grounded/dictee-1-grounded.txt`).

---

## 5. Refondre la base `meds.sqlite` (voir / à régénérer)

Le moteur utilise `meds.sqlite` tel quel. Ce fichier est **prêt à l'emploi** —
aucune étape n'est requise pour simplement faire tourner le grounding. Les
commandes ci-dessous ne servent que si l'on veut **reconstruire ou retoucher**
la base (nouvel extrait BDP, nouveaux alias, nettoyage).

Pipeline complet, **à lancer depuis ce dossier** (chemins relatifs) :

```bash
cd ~/ConsultAI-selfhosted/med_grounding

# 1) Construire la base depuis dpd/ + dpd_ia/ (marques + génériques + inactifs)
./venv/bin/python build_db.py

# 2) Rajouter les alias phonétiques « STT_GARBLE » observés
./venv/bin/python seed_aliases.py

# 3) Nettoyer (alias de présentation/dose parasites). D'abord en dry-run :
./venv/bin/python prune_db.py            # affiche ce qui serait retiré
./venv/bin/python prune_db.py --apply    # applique réellement

# 4) Vérifier la base
./venv/bin/python -c "import sqlite3; c=sqlite3.connect('meds.sqlite'); \
print('meds', c.execute('select count(*) from medications').fetchone()[0], \
'alias', c.execute('select count(*) from medication_aliases').fetchone()[0])"
```

Ordre logique : `build_db` → `seed_aliases` → `prune_db` (apply). Aucun de ces
trois n'accepte d'argument (ils travaillent sur `./dpd`, `./dpd_ia`,
`./meds.sqlite`).

> ⚠ Toute reprise manuelle d'un transcript doit se faire **sur une copie** de
> la sortie, et toute reprise de données cliniques respecte la politique de
> dénominalisation du projet (jamais de nom de patient/dossier).

---

## 6. Types de correspondance & conventions de sortie

### Niveaux de correspondance
- `BRAND` : nom de marque (ex. `TRANDATE`) — rendu en majuscules.
- `BASE_GENERIC` / `FULL_GENERIC` : nom commun de la substance active (ex.
  `labetalol`).
- `BRAND_LEAF` : mot unique de tête d'une marque multi-mots (ex. `TYLENOL`).
- `STT_GARBLE` : aliases phonétiques observés (ex. `pantoloque`, `tirilnol`) —
  injectés par `seed_aliases.py`.
- Produits OTC : rendu du principe actif véritable (par ex. `aspirine`) sauf
  exception marque conservée (`Tylenol`).

### Conventions de posologie (fréquences de prise, alignées sur la pratique
Wikimedica / formulation québécoise)

| Entrée (transcript) | Sortie canonique |
|---|---|
| `per os`, `per`, `os` | `PO` |
| `per os quotidien` (dicté `perotidien`) | `PO DIE` |
| `par jour`, `quotidien(ne)`, `dy` | `DIE` |
| `deux fois par jour` / `BD` | `BID` |
| `trois fois par jour` | `TID` |
| `quatre fois par jour` | `QID` |
| `hs`, `au coucher` | `HS` |
| `le matin` | `AM` |
| `le soir` | `PM` |
| `au besoin` | `PRN` |
| `Q2 jours` métaphone `q2j` | `Q2J` |
| `Q semaine`, `par semaine` | `Q1SEM` |
| `unités` / `unites` / `ui` | `UI` |
| `sous-cutané` (`sous-cut`, `souscut…`, `sc`) | `S/C` |

### Sécurité sur la prose
Le moteur est volontairement **conservateur hors des listes de médicaments** :
des expressions comme « une fois par semaine », « quotidiennement », « deux
jours », « une prise matinale », « sous AOD », « au plus deux jours » restent
inchangées. Un mot n'est remplacé que si un signal de contexte (posologie,
ancre de liste, similarité élevée) l'étaye.

---

## 7. Référence rapide des entrées de test

| Transcript brut | Contenu |
|---|---|
| `dictee-1-cohere.txt` | consultation test — `perotidien`→`PO DIE`, Tyrénol/Lasix = Tylenol/Lasix, Celexa, Trandate, Lipitor, Avapro, Aricept. |
| `dictee-6-cohere.txt` | note de suivi prose lourde — vérifie qu'aucune prose n'est touchée. |
| `consult7-cohere.txt` | liste médicaments riche (PO Q2J HS, Vitamine D 10 000 UI Q1SEM, perindopril PO DIE AM). |
| `consultai4-gemini-cohere.txt` | dictée longue Gemini — Tresiba 9 UI HS, Vitamine D Q1SEM. |

Sorties attendues (référence) dans `meds_grounded/` (à noter : ces fichiers de
référence ont été produits avant la règle `perotidien→PO DIE` ; les regénérer
avec `match_meds.py` courant pour la version à jour).

---

## 8. Dépanner

- `ModuleNotFoundError: No module named 'rapidfuzz'` → recréer le venv (section 3).
- « aucun changement » sur un nom pourtant faux → le seuil de similarité ortho
  n'est pas atteint ; vérifier les alias dans `seed_aliases.py` ou la base.
- Sortie identique à l'entrée sur une diction → confirmer qu'on a bien lancé
  depuis `~/ConsultAI-selfhosted/med_grounding` (chemins relatifs `./meds.sqlite`).
- Changement de la base → relancer `build_db.py` + `seed_aliases.py` +
  `prune_db.py --apply` puis retester sur `meds_grounded/*`.
```
