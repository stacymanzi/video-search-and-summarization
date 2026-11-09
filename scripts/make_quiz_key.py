#!/usr/bin/env python3
"""Write the hashed multiple-choice key that the Part 1 assessment notebook reads.

Input, the grader's plaintext key (never copied into the lab image):
    task1/assessment/grader/answer_keys/part1.json
    {"version": 1, "questions": {"p1-q1": "B", ...}, "min_correct": 6,
     "explanations": {"p1-q1": "...", ...}}

Output, shipped next to the notebook:
    task1/lab/task/part1/assessment_key.json
    {"version": 1, "questions": {"p1-q1": [sha256 of "p1-q1:B", explanation encrypted with "B", 4], ...}}

quiz.make_key does the hashing, so the notebook and this script cannot disagree.
Every question has four options, A to D. Standard library only.

    python3 -I task1/scripts/make_quiz_key.py           # write the hashed key
    python3 -I task1/scripts/make_quiz_key.py --check   # exit 1 if the hashed key does not match the plaintext key
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

TASK1 = Path(__file__).resolve().parents[1]
PLAIN_KEY = TASK1 / "assessment" / "grader" / "answer_keys" / "part1.json"
HASHED_KEY = TASK1 / "lab" / "task" / "part1" / "assessment_key.json"
QUIZ = TASK1 / "lab" / "task" / "part1" / "quiz.py"
OPTIONS = 4
LETTERS = "ABCD"


def load_quiz(path: Path = QUIZ):
    """Import quiz.py by path, without loading the key file it would otherwise read at import."""
    saved = os.environ.get("ASSESSMENT_KEY")
    saved_bytecode = sys.dont_write_bytecode
    os.environ["ASSESSMENT_KEY"] = str(path.with_name("no-key-while-generating.json"))
    sys.dont_write_bytecode = True    # leave no __pycache__ behind in the lab content
    try:
        spec = importlib.util.spec_from_file_location("quiz_for_key", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved_bytecode
        if saved is None:
            os.environ.pop("ASSESSMENT_KEY", None)
        else:
            os.environ["ASSESSMENT_KEY"] = saved
    return module


def read_plain(path: Path = PLAIN_KEY) -> dict:
    """The plaintext key, checked: every question has a letter A to D and an explanation."""
    data = json.loads(path.read_text())
    questions, explanations = data.get("questions"), data.get("explanations")
    if not isinstance(questions, dict) or not questions:
        raise ValueError(f"{path}: 'questions' must map question ids to letters")
    if not isinstance(explanations, dict):
        raise ValueError(f"{path}: 'explanations' must map question ids to text")
    for qid, letter in questions.items():
        if not isinstance(letter, str) or len(letter) != 1 or letter not in LETTERS:
            raise ValueError(f"{path}: {qid} has answer {letter!r}; use one letter from {LETTERS}")
        text = explanations.get(qid)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{path}: {qid} has no explanation")
    extra = sorted(set(explanations) - set(questions))
    if extra:
        raise ValueError(f"{path}: explanations for unknown questions {extra}")
    min_correct = data.get("min_correct")
    if not isinstance(min_correct, int) or not 0 < min_correct <= len(questions):
        raise ValueError(f"{path}: min_correct must be a whole number from 1 to {len(questions)}")
    return data


def build(plain: dict, quiz) -> dict:
    """The hashed key file content for a checked plaintext key."""
    entries = {}
    for qid in sorted(plain["questions"]):
        digest, cipher, options = quiz.make_key(qid, plain["questions"][qid], plain["explanations"][qid], OPTIONS)
        entries[qid] = [digest, cipher, options]
    return {"version": int(plain.get("version", 1)), "questions": entries}


def render(content: dict) -> str:
    return json.dumps(content, indent=2) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key", type=Path, default=PLAIN_KEY, help="plaintext key (default: %(default)s)")
    parser.add_argument("--out", type=Path, default=HASHED_KEY, help="hashed key to write (default: %(default)s)")
    parser.add_argument("--check", action="store_true", help="compare instead of writing; exit 1 on a difference")
    args = parser.parse_args(argv)

    text = render(build(read_plain(args.key), load_quiz()))
    if args.check:
        current = args.out.read_text() if args.out.is_file() else ""
        if current != text:
            print(f"{args.out} does not match {args.key}. Run: python3 -I task1/scripts/make_quiz_key.py")
            return 1
        print(f"{args.out} matches {args.key}")
        return 0
    args.out.write_text(text)
    print(f"wrote {args.out} ({len(json.loads(text)['questions'])} questions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
