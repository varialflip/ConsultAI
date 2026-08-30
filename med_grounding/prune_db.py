#!/usr/bin/env python3
"""Prune the medication DB for leaner, faster matching.

Removes the presentation / strength / dose-form clutter that the matcher's
fuzzy path already excludes (BRAND_STOP junk: "5% DEXTROSE AND 0.9% SODIUM
CHLORIDE", "XYLOCAINE 2%", "25mg", "100000", suspensions, creams, etc.) and
drops the medication rows that were ONLY represented by such junk. These are
IV-fluid / local-anaesthetic-strength / dosage-form rows, not spoken dictation
drug names, so their removal is invisible to med grounding.

This only ever shrinks the search space; it never removes a name the fuzzy
matcher could reach (those already live in the non-junk aliases). Medication
rows that keep at least one clean alias are retained untouched.
"""
import re, sqlite3, sys, unicodedata

DB = "./meds.sqlite"
# True presentation / strength / noise intimately bound to a specific dose or
# form, NOT a spoken drug name. Boundary-aware so a clean generic like
# "AMLODIPINE" (which merely contains "ml"/"mg" as substrings) is NEVER caught
# -- only a strength value, a percent, or a standalone unit/dosage-form token.
JUNK_RE = re.compile(
    r"\d|%|"                                  # strength digit or percent
    r"\b(mg|mcg|µg|ml|g|gm|gram|unit|units|ui|iu|meq|mmol)\b|"  # standalone unit
    r"\b(tablet|tablets|tab|caplet|caplets|capsule|capsules|cap|cream|gel|"
    r"ointment|patch|syringe|injection|injectable|solution|suspension|elixir|"
    r"syrup|drop|drops|lotion|shampoo|kit|pack|suppository|infusion|vial|"
    r"ampoule|ampul|prefilled|pre-filled)\b", re.IGNORECASE)


def norm_phon(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z]", "", s.lower())


def main():
    dry = "--apply" not in sys.argv
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT id, alias_name FROM medication_aliases").fetchall()

    junk_ids = set()
    for aid, alias in rows:
        n = norm_phon(alias)
        if not n:
            junk_ids.add(aid)
        elif JUNK_RE.search(alias):
            junk_ids.add(aid)

    keep_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT medication_id FROM medication_aliases WHERE id NOT IN (%s)"
        % ",".join(map(str, junk_ids)) if junk_ids else "SELECT 0 WHERE 0")]
    keep_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT medication_id FROM medication_aliases WHERE id NOT IN (%s)"
        % ",".join(map(str, junk_ids)))]

    orphan_mid = [r[0] for r in conn.execute(
        "SELECT DISTINCT medication_id FROM medication_aliases")
        if r[0] not in set(keep_ids)]

    n_junk = len(junk_ids)
    n_orphan = len(orphan_mid)
    print(f"junk aliases to remove : {n_junk}")
    print(f"orphaned medications    : {n_orphan}")

    if dry:
        print("[dry-run] pass --apply to actually prune")
        conn.close()
        return

    conn.execute("BEGIN")
    if junk_ids:
        conn.execute("DELETE FROM medication_aliases WHERE id IN (%s)"
                     % ",".join(map(str, junk_ids)))
    if orphan_mid:
        conn.execute("DELETE FROM medications WHERE id IN (%s)"
                     % ",".join(map(str, orphan_mid)))
    conn.commit()
    va = conn.execute("SELECT COUNT(*) FROM medication_aliases").fetchone()[0]
    vm = conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0]
    print(f"after: medications={vm}, aliases={va}")
    conn.close()


if __name__ == "__main__":
    main()
