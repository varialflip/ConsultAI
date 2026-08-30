#!/usr/bin/env python3
"""Normalize Cohere ASR French medical transcripts using a local LLM.

Generalizable: passes the raw transcript + a reference drug glossary to a
local instruct model, which corrects drug names/medical terms in context
without changing anything else. Handles unseen drug names via the LLM's
knowledge plus the glossary as context.
"""
import argparse
import sys

from mlx_lm import generate, load

SYSTEM = (
    "Tu es un assistant médical qui nettoie des dictées de consultation en "
    "français produites par un logiciel de reconnaissance vocale.\n\n"
    "Corrige les noms de médicaments et termes médicaux mal transcrits par la "
    "reconnaissance vocale, en utilisant tes connaissances médicales "
    "(français). Conserve exactement le reste : la syntaxe, les autres mots, "
    "la ponctuation et le style parlé. Ne reformule pas, ne résume pas, ne "
    "rachecourt pas les phrases.\n\n"
    "RÈGLE DE SÉCURITÉ : un nom de médicament DÉJÀ correctement orthographié "
    "doit rester INCHANGÉ. Ne remplace JAMAIS un nom de médicament valide par "
    "un autre. Ne corrige que les mots manifestement déformés par la "
    "reconnaissance vocale.\n\n"
    "INDICATION UTILE : dans une dictée médicale, la liste des médicaments est "
    "généralement donnée d'un seul tenant. Si, dans cette liste de "
    "médicaments, un mot ou une suite de mots ne correspond à aucun nom ou "
    "posologie plausible, considère fortement qu'il s'agit d'une erreur de "
    "transcription phonétique du nom d'un médicament, et corrige-le en "
    "conséquence.\n\n"
    "S'il te manque des médicaments, garde le texte tel quel. Renvoie "
    "uniquement le texte corrigé, sans explication ni encadrement."
)


def normalize(model, tokenizer, text, max_tokens):
    prompt = (
        "Voici la dictée à corriger (transcription automatique brute) :\n"
        "<dictée>\n"
        + text +
        "\n</dictée>\n"
        "Donne-moi la version corrigée."
    )
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]
    out = generate(
        model, tokenizer,
        prompt=tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        ),
        max_tokens=max_tokens,
        verbose=False,
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model",
                    default="mlx-community/Qwen2.5-7B-Instruct-4bit")
    ap.add_argument("--input", required=True, help="raw transcript file")
    ap.add_argument("--output", required=True, help="output file")
    ap.add_argument("--max-tokens", type=int, default=1500)
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        text = f.read().strip()
    model, tokenizer = load(args.model)
    result = normalize(model, tokenizer, text, args.max_tokens)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(result.strip() + "\n")
    print("wrote:", args.output)


if __name__ == "__main__":
    main()
