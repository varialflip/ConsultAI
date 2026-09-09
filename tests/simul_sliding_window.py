"""Simulation : fenêtre glissante vs tranches 10 s vs transcription complète.

Rejoue une dictée SANS interruption sur un enregistrement réel (fichier
stable, endpoint STT de production) selon trois stratégies, puis compare
chaque résultat à la transcription complète (référence) :

  1. ``tranches``  — comportement actuel : tranches ~10 s, fusion par
     simple accumulation (le transcript live d'hier) ;
  2. ``fenetres``  — fenêtre glissante 45 s / pas 15 s / recouvrement 30 s,
     fusion par alignement textuel du recouvrement (``dictation
     ._decoupe_fusion``, le même code que la dictée live utilisera) ;
  3. ``complete``  — le fichier entier en une passe (ce que produit
     « Retranscrire »).

Exécuter DANS le conteneur (endpoint + config nécessaires) :

    docker cp tests/simul_sliding_window.py consultai-test:/tmp/
    docker exec consultai-test python3 /tmp/simul_sliding_window.py 46

Sortie : rapport de similarité (jetons), coût audio par stratégie, extraits
des différences ; les trois textes sont écrits dans ``/data/simul_sliding/``.
"""

import difflib
import os
import re
import sys
import time

from app import dictation, recordings, stt
from app.database import Consultation, SessionLocal, Template
from app.stt import extract_segment, find_cut_point, probe_duration, transcribe_payload


def classifier(candidat: str, reference: str):
    """Catégorise les écarts : forme (inoffensif) vs clinique (chiffres, noms)."""
    tf = candidat.split()
    tc = reference.replace("\n", " ").split()
    sm = difflib.SequenceMatcher(None, tf, tc)
    cats = {"ponctuation/casse": [], "chiffres": [],
            "noms/majuscules": [], "autres": []}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a = " ".join(tf[i1:i2])
        b = " ".join(tc[j1:j2])
        na = re.sub(r"[^a-z0-9]", "", a.lower())
        nb = re.sub(r"[^a-z0-9]", "", b.lower())
        if na == nb:
            cats["ponctuation/casse"].append((a, b))
        elif re.search(r"[0-9]", a + b):
            cats["chiffres"].append((a, b))
        elif re.search(r"[A-ZÀ-Ý]", a + b):
            cats["noms/majuscules"].append((a, b))
        else:
            cats["autres"].append((a, b))
    cliniques = cats["chiffres"] + cats["noms/majuscules"]
    return sm.ratio(), cats, cliniques


def _contexte(texte: str, caracteres: int = 1500):
    if not texte:
        return None
    corps = texte[-(caracteres + 1):]
    idx = max(corps.rfind(". "), corps.rfind("! "), corps.rfind("? "))
    if idx > 0:
        corps = corps[idx + 1:]
    retour = corps.strip()
    return retour or None


def _attendu(prov, prov_start, texte, debut, offset, fin):
    """Estimation du recouvrement attendu (même calcul que la dictée live)."""
    span_provisoire = max(1e-6, offset - prov_start)
    span_fenetre = max(1e-6, fin - debut)
    recouvrement = min(offset, fin) - debut
    if recouvrement <= 0:
        return 0
    return max(0, min(
        int(len(dictation._jetons_norm(prov)) * recouvrement / span_provisoire),
        int(len(dictation._jetons_norm(texte)) * recouvrement / span_fenetre)))


def sim_tranches(chemin, hints, duree):
    """Stratégie actuelle : tranches ~10 s coupées au silence, accumulation."""
    low, target, high = dictation._window()
    offset, parts, contexte = 0.0, [], None
    envoye = 0.0
    while offset < duree - 0.05:
        longueur, real = find_cut_point(chemin, offset, target, low, high)
        payload = extract_segment(chemin, offset, longueur, real)
        if payload.duration_seconds < 0.7:
            break
        offset += payload.duration_seconds
        envoye += payload.duration_seconds
        resultat = transcribe_payload(payload, hints, contexte_precedent=contexte)
        texte = (resultat.get("transcript") or "").strip()
        if texte:
            parts.append(texte)
            contexte = _contexte(" ".join(parts))
    return " ".join(parts), envoye


def sim_fenetres(chemin, hints, duree, taille=45.0, pas=15.0, debug=False):
    """Fenêtre glissante : fusion par alignement du recouvrement."""
    recouvrement = taille - pas
    offset, prov, parts = 0.0, "", []
    envoye = 0.0
    alignes = ratés = 0
    while offset < duree - 0.05:
        if not prov:
            if duree - offset < taille:
                break
            debut, fin_prevu = offset, offset + taille
        else:
            debut = max(0.0, offset - recouvrement)
            fin_prevu = offset + pas
        longueur = fin_prevu - debut
        payload = extract_segment(chemin, debut, longueur, longueur)
        if payload.duration_seconds < 0.7:
            break
        fin = debut + payload.duration_seconds
        envoye += payload.duration_seconds
        resultat = transcribe_payload(
            payload, hints, contexte_precedent=_contexte(prov))
        texte = (resultat.get("transcript") or "").strip()
        if texte:
            if not prov:
                prov, prov_start = texte, debut
            else:
                confirme, m, ratio = dictation._decoupe_fusion(
                    prov, texte,
                    m_attendu=_attendu(prov, prov_start, texte,
                                       debut, offset, fin))
                if debug:
                    ta = dictation._jetons_norm(prov)
                    tb = dictation._jetons_norm(texte)
                    ratio = difflib.SequenceMatcher(
                        None, ta[-m:] if m else [], tb[:m] if m else []).ratio() if m else 0.0
                    print(f"  [dbg offset={offset:.0f}] m={m} "
                          f"len(prov_jetons)={len(prov.split())} "
                          f"len(ta)={len(ta)} len(tb)={len(tb)} "
                          f"ratio={ratio:.2f} confirmé={len(confirme.split())} jetons")
                if m >= dictation._FUSION_MIN_JETONS:
                    if confirme:
                        parts.append(confirme)
                    prov, prov_start = texte, debut
                    alignes += 1
                else:
                    # Rattrapage : mini-tranche de l'audio neuf, ajoutée au
                    # provisoire (même repli que la dictée live).
                    mini = extract_segment(chemin, offset, pas, pas)
                    if mini.duration_seconds >= 0.7:
                        r2 = transcribe_payload(
                            mini, hints, contexte_precedent=_contexte(prov))
                        t2 = (r2.get("transcript") or "").strip()
                        envoye += mini.duration_seconds
                        if t2:
                            prov = f"{prov} {t2}".strip()
                        fin = offset + mini.duration_seconds
                    ratés += 1
        offset = fin
    if prov:
        parts.append(prov)
    print(f"  fenêtres alignées: {alignes}, rattrapages: {ratés}")
    return " ".join(parts), envoye


def sim_complete(chemin, mime, hints):
    with open(chemin, "rb") as handle:
        brut = handle.read()
    resultat = stt.transcribe(brut, mime, hints)
    return (resultat.get("transcript") or "").strip(), float(
        resultat.get("duration_seconds") or 0)


def comparer(nom, candidat, reference):
    tc = candidat.split()
    tr = reference.replace("\n", " ").split()
    ratio = difflib.SequenceMatcher(None, tc, tr).ratio()
    print(f"  {nom:<10} : {len(candidat)} caractères, {len(tc)} jetons, "
          f"similarité {100 * ratio:.1f} %")
    return ratio


def main():
    consultation_id = int(sys.argv[1]) if len(sys.argv) > 1 else 46
    taille = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    pas = float(sys.argv[3]) if len(sys.argv) > 3 else 15.0
    # Baseline des tranches refaite une seule fois (indépendante de la config) :
    # env SIMUL_SKIP_TRANCHES=1 pour les runs suivants.
    skip_tranches = os.environ.get("SIMUL_SKIP_TRANCHES") == "1"

    with SessionLocal() as db:
        consultation = db.get(Consultation, consultation_id)
        if consultation is None:
            raise SystemExit(f"consultation {consultation_id} introuvable")
        pistes = recordings.for_consultation(db, consultation_id)
        if not pistes:
            raise SystemExit("aucun enregistrement")
        template = db.get(Template, consultation.template_id)
        hints = template.phrase_hints if template else ""
        # Un seul enregistrement pour une simulation propre (une dictée).
        piste = pistes[-1]
        chemin = recordings.absolute_path(piste)

    duree = probe_duration(chemin) or 0.0
    suffixe = f"{int(taille)}x{int(pas)}"
    print(f"Consultation {consultation_id}, enregistrement {piste.id} "
          f"({duree:.0f} s, {piste.mime_type}) — config {suffixe}")
    print(f"Fenêtre: {taille:.0f} s, pas: {pas:.0f} s, "
          f"recouvrement: {taille - pas:.0f} s, multiple audio: {taille/pas:.1f}x\n")

    sortie = "/data/simul_sliding"
    os.makedirs(sortie, exist_ok=True)

    texte_tranches, audio_tranches, t_tranches = "", 0.0, 0.0
    if not skip_tranches:
        t0 = time.monotonic()
        texte_tranches, audio_tranches = sim_tranches(chemin, hints, duree)
        t_tranches = time.monotonic() - t0
        print(f"[tranches]  {t_tranches:.0f} s, {audio_tranches:.0f} s d'audio envoyé")

    t0 = time.monotonic()
    texte_fenetres, audio_fenetres = sim_fenetres(
        chemin, hints, duree, taille, pas)
    t_fenetres = time.monotonic() - t0
    print(f"[fenêtres]  {t_fenetres:.0f} s, {audio_fenetres:.0f} s d'audio envoyé")

    if skip_tranches:
        chemin_tranches = os.path.join(sortie, f"tranches_c{consultation_id}.txt")
        if os.path.exists(chemin_tranches):
            with open(chemin_tranches, encoding="utf-8") as handle:
                texte_tranches = handle.read()

    t0 = time.monotonic()
    texte_complete, audio_complete = sim_complete(chemin, piste.mime_type, hints)
    t_complete = time.monotonic() - t0
    print(f"[complète]  {t_complete:.0f} s, {audio_complete:.0f} s d'audio envoyé\n")

    print(f"Similarité contre la transcription complète (config {suffixe}) :")
    if texte_tranches:
        r0, cats0, clin0 = classifier(texte_tranches, texte_complete)
        print(f"  tranches   : {100*r0:.1f} % — cliniques: {len(clin0)} "
              f"(chiffres {len(cats0['chiffres'])}, noms {len(cats0['noms/majuscules'])})")
    rf, cats, clin = classifier(texte_fenetres, texte_complete)
    print(f"  fenêtres   : {100*rf:.1f} % — cliniques: {len(clin)} "
          f"(chiffres {len(cats['chiffres'])}, noms {len(cats['noms/majuscules'])})")
    print(f"  catégorie 'autres': {len(cats['autres'])} écart(s), "
          f"'ponctuation/casse': {len(cats['ponctuation/casse'])} écart(s)")
    for a, b in clin[:12]:
        print(f"    CLINIQUE  fenêtres: {a[:60]!r}  ->  complète: {b[:60]!r}")

    for nom, texte in (("tranches", texte_tranches),
                       (f"fenetres_{suffixe}", texte_fenetres),
                       (f"complete_{suffixe}", texte_complete)):
        with open(os.path.join(sortie, f"{nom}_c{consultation_id}.txt"),
                  "w", encoding="utf-8") as handle:
            handle.write(texte)


if __name__ == "__main__":
    main()
