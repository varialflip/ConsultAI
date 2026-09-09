#!/usr/bin/env python3
"""Side-by-side : inline / suggestions du matcher vs la région médicaments (LLM).

Pour 3 consultations tirées au hasard, on exécute le flow complet :
  * ``detect_med_region`` (LLM actif — Gemma 4 31b) → la zone médicaments ;
  * ``matcher().normalize`` → corrections INLINE (réécritures déterministes) ;
  * ``extract_validation_items`` → suggestions (déterministes + phonétiques) ;
  * ``extract_med_items`` → la liste pointée.

Produit un HTML dans ``app/../tests/med_regions/side_by_side_<ts>.html`` montrant,
pour chaque consultation, la région originale avec contexte avant/après en regard
des corrections inline et des suggestions du moteur.

Usage (dans le conteneur) :
    python3 /tmp/side_by_side_med.py
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import random
import sqlite3
import sys

sys.path.insert(0, "/app")

from app.med_grounding import (  # noqa: E402
    detect_med_region,
    extract_med_items,
    extract_validation_items,
    matcher,
    norm_phon,
)

DB_PATH = "/data/consultai.db"
OUT_DIR = "/tmp/med_side"
N = 3
CONTEXT = 12  # tokens avant/après la région


def pick_consultations(conn, n: int) -> list:
    rows = conn.execute(
        "SELECT id, title FROM consultations "
        "WHERE transcript_conf IS NOT NULL AND transcript_conf != '' "
        "AND raw_transcript IS NOT NULL AND raw_transcript != '' "
        "AND normalized_transcript IS NOT NULL AND normalized_transcript != '' "
        "ORDER BY id"
    ).fetchall()
    ids = [r[0] for r in rows]
    random.shuffle(ids)
    return ids[:n]


def conf_for(conf_map, w):
    if not conf_map:
        return None
    return conf_map.get(norm_phon(w))


def load_consultation(conn, cid):
    row = conn.execute(
        "SELECT id, title, raw_transcript, transcript_conf, "
        "normalized_transcript, med_grounding_json, inline_fixed_json "
        "FROM consultations WHERE id=?", (cid,)
    ).fetchone()
    return {
        "id": row[0],
        "title": row[1],
        "raw": row[2],
        "conf_map": json.loads(row[3]) if row[3] else {},
        "normalized": row[4],
        "med_items_persisted": json.loads(row[5]) if row[5] else [],
        "inline_fixed": json.loads(row[6]) if row[6] else [],
    }


def token_highlight(text):
    """Découpe le texte en mots avec ponctuation préservée."""
    import re
    return re.findall(r"\s+|\S+", text)


def build_report(c, region):
    """Construit le rapport d'une consultation."""
    raw = c["raw"]
    conf_map = c["conf_map"]

    # 1. Région LLM + contexte
    tok_llm = region if region else None

    # 2. Inline (matcher().normalize)
    m = matcher()
    fixed, changes = m.normalize(raw, conf=conf_map or None)

    # 3. Suggestions (validation items) + liste pointée
    items = extract_validation_items(raw, conf=conf_map or None)
    med_items = extract_med_items(raw, conf=conf_map or None)

    # Contexte autour de la région LLM
    context = None
    if tok_llm:
        words = raw.split()
        ts, te = tok_llm.get("token_start"), tok_llm.get("token_end")
        if ts is not None and te is not None:
            start = max(0, ts - CONTEXT)
            end = min(len(words), te + 1 + CONTEXT)
            ctx_before = words[start:ts]
            ctx_after = words[te + 1:end]
            region_words = words[ts:te + 1]
            context = {
                "before": " ".join(ctx_before),
                "region": " ".join(region_words),
                "after": " ".join(ctx_after),
                "ts": ts,
                "te": te,
            }

    return {
        "region_llm": tok_llm,
        "context": context,
        "inline_changes": changes,
        "fixed": fixed,
        "items": items,
        "med_items": med_items,
        "raw": raw,
    }


def esc(s):
    if s is None:
        return "&mdash;"
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_context_html(context):
    if not context:
        return '<div class="ctx-error">Pas de région LLM</div>'
    before = esc(context["before"])
    region = esc(context["region"])
    after = esc(context["after"])
    return (
        '<div class="ctx-line">'
        '<span class="ctx-before">' + before + "</span> "
        '<span class="ctx-region">' + region + "</span> "
        '<span class="ctx-after">' + after + "</span>"
        "</div>"
    )


def render_inline_html(changes):
    if not changes:
        return '<div class="dim">Aucune correction inline</div>'
    rows = []
    for orig, repl, score, sim in changes:
        rows.append(
            "<tr>"
            f'<td class="mono">{esc(orig)}</td>'
            f'<td class="mono">→</td>'
            f'<td class="mono">{esc(repl)}</td>'
            f'<td>{score}</td>'
            f'<td>{sim:.3f}</td>'
            "</tr>"
        )
    return '<table class="kv"><thead><tr><th>Original</th><th></th>' \
           '<th>Corrigé</th><th>Score</th><th>Similarité</th></tr></thead>' \
           "<tbody>" + "".join(rows) + "</tbody></table>"


def render_items_html(items):
    if not items:
        return '<div class="dim">Aucune suggestion</div>'
    rows = []
    for it in items:
        src = it.get("source", "det")
        name = it.get("name", "")
        base = it.get("base", "")
        poso = it.get("posology", "")
        score = it.get("score", "")
        garble = it.get("garble")
        badge = '<span class="badge ' + ("phon" if src == "phonetic" else "det") + '">' + src + "</span>"
        g = f'<span class="garble">{esc(garble)}</span>' if garble else "&mdash;"
        rows.append(
            "<tr>"
            f"<td>{badge}</td>"
            f'<td class="mono strong">{esc(name)}</td>'
            f'<td class="mono">{esc(base)}</td>'
            f'<td class="mono">{esc(poso)}</td>'
            f"<td>{score}</td>"
            f"<td>{g}</td>"
            "</tr>"
        )
    return '<table class="kv"><thead><tr><th>Source</th><th>Nom</th>' \
           '<th>Base</th><th>Posologie</th><th>Score</th>' \
           '<th>Garble</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>"


def render_consult_html(c, r, idx):
    meta = f'#{c["id"]} — {esc(c["title"])}'
    llm_line = ""
    if r["region_llm"]:
        rl = r["region_llm"]
        llm_line = (
            f'<div class="llm-meta">LLM : <b>{esc(rl.get("model", ""))}</b> · '
            f'{rl.get("time_s")}s · {rl.get("prompt_tokens", 0)}/'
            f'{rl.get("completion_tokens", 0)} tokens · '
            f'<span class="region-coord">[{rl.get("token_start")}–{rl.get("token_end")}]</span></div>'
        )
    else:
        llm_line = '<div class="llm-meta warn">LLM : aucune région détectée</div>'

    inline_h = render_inline_html(r["inline_changes"])
    items_h = render_items_html(r["items"])
    ctx_h = render_context_html(r["context"])

    fixed_preview = ""
    if r["fixed"] and r["fixed"].strip() != r["raw"].strip():
        fixed_preview = ('<details class="fixed-details"><summary>Texte normalisé '
                         '(envoi au LLM)</summary><div class="fixed-block">'
                         + esc(r["fixed"]) + "</div></details>")

    return (
        '<div class="consult">'
        f'<div class="consult-header"><div class="consult-title">{idx}. {meta}</div></div>'
        '<div class="consult-body">'
        f"{llm_line}"
        '<div class="grid-2">'
        '<div class="col"><div class="col-title">Région originale (with context ±' + str(CONTEXT) +
        ')</div>' + ctx_h + "</div>"
        '<div class="col"><div class="col-title">Inline (normalize)</div>' + inline_h +
        '<div class="col-title" style="margin-top:14px">Suggestions</div>' + items_h + "</div>"
        "</div>"
        f"{fixed_preview}"
        "</div></div>"
    )


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    picked = pick_consultations(conn, N)
    os.makedirs(OUT_DIR, exist_ok=True)

    reports = []
    for cid in picked:
        c = load_consultation(conn, cid)
        region = None
        try:
            region = detect_med_region(c["raw"])
        except Exception as e:
            print(f"  #{cid}: detect_med_region échec {e!r}")
        rep = build_report(c, region)
        rep["_consult"] = c
        reports.append(rep)
        print(f"  #{cid}: région LLM {'OK:' + str(rep['region_llm'] and rep['context'])[:80] if rep['region_llm'] else 'aucune'} · "
              f"{len(rep['inline_changes'])} inline · {len(rep['items'])} suggestions")

    conn.close()

    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(OUT_DIR, f"side_by_side_{ts}.html")
    body = "".join(render_consult_html(r["_consult"], r, i + 1)
                   for i, r in enumerate(reports))
    html = PAGE_TEMPLATE.format(ts=ts, n=len(reports), body=body, context=CONTEXT)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nÉcrit : {out_path}")


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ConsultAI — Med matcher vs région LLM</title>
<style>
:root{{--bg:#0d1117;--fg:#c9d1d9;--dim:#8b949e;--border:#30363d;--accent:#58a6ff;
--green:#3fb950;--yellow:#d29922;--red:#f85149;--orange:#db6d28;--bg2:#161b22;
--bg3:#1c2128;--bg4:#21262d;--region-bg:#1a3a1a;--violet:#c084fc}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'SF Mono','Cascadia Code','Consolas',monospace;background:var(--bg);
color:var(--fg);font-size:13px;line-height:1.5}}
.container{{max-width:1600px;margin:0 auto;padding:16px}}
.header{{background:var(--bg2);border:1px solid var(--border);border-radius:8px;
padding:18px 22px;margin-bottom:16px}}
.header h1{{font-size:18px;color:var(--accent)}} 
.header .subtitle{{color:var(--dim);font-size:12px;margin-top:4px}}
.consult{{background:var(--bg2);border:1px solid var(--border);border-radius:8px;
margin:16px 0;overflow:hidden}}
.consult-header{{padding:13px 18px;background:var(--bg3);border-bottom:1px solid var(--border)}}
.consult-title{{font-size:14px;font-weight:700}}
.consult-body{{padding:16px 18px}}
.llm-meta{{color:var(--violet);font-size:11px;margin-bottom:12px}}
.llm-meta.warn{{color:var(--orange)}}
.region-coord{{color:var(--dim)}}
.grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
@media(max-width:1000px){{.grid-2{{grid-template-columns:1fr}}}}
.col{{min-width:0}}
.col-title{{font-size:11px;color:var(--accent);font-weight:600;
text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px}}
.ctx-line{{font-size:12.5px;line-height:2;background:var(--bg);border:1px solid var(--border);
border-radius:6px;padding:10px 12px}}
.ctx-before{{color:var(--dim)}}
.ctx-region{{background:var(--region-bg);color:var(--green);padding:1px 4px;
border-radius:2px;font-weight:700;border:1px solid #2f6f2f}}
.ctx-after{{color:var(--dim)}}
.ctx-error{{color:var(--orange);font-size:12px}}
table.kv{{width:100%;border-collapse:collapse;font-size:11px;background:var(--bg);
border:1px solid var(--border);border-radius:6px;overflow:hidden}}
table.kv th{{background:var(--bg4);color:var(--dim);text-align:left;padding:5px 8px;
font-size:10px;text-transform:uppercase;letter-spacing:0.5px}}
table.kv td{{padding:4px 8px;border-top:1px solid var(--border);vertical-align:top}}
table.kv tr{{background:var(--bg)}}
table.kv tr.phonetic{{background:#1a1a2e}}
.mono{{font-family:inherit}}
.strong{{font-weight:700;color:var(--fg)}}
.badge{{display:inline-block;padding:1px 6px;border-radius:9px;font-size:9px;font-weight:700}}
.badge.det{{background:#1f3d5c;color:var(--accent)}}
.badge.phon{{background:#2a1a3a;color:var(--violet)}}
.garble{{color:var(--yellow);font-style:italic}}
.dim{{color:var(--dim);font-size:12px}}
.fixed-details{{margin-top:16px}}
.fixed-details summary{{cursor:pointer;color:var(--accent);font-size:12px}}
.fixed-block{{background:var(--bg);border:1px solid var(--border);border-radius:6px;
padding:12px 14px;margin-top:8px;font-size:12px;line-height:1.7;white-space:pre-wrap}}
::-webkit-scrollbar{{width:8px}}::-webkit-scrollbar-track{{background:var(--bg)}}
::-webkit-scrollbar-thumb{{background:var(--border);border-radius:4px}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>ConsultAI — Med matcher vs région LLM</h1>
    <div class="subtitle">
      {n} consultation(s) — région détectée par le LLM (Gemma 4 31b) avec contexte ±{context}
      tokens, en regard des corrections inline et des suggestions du moteur de grounding.
      Généré le {ts}.
    </div>
  </div>
  {body}
</div>
</body>
</html>
"""


if __name__ == "__main__":
    main()