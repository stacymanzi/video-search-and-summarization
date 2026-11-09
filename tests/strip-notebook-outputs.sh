#!/usr/bin/env bash
#
# Clear executed output from every shipped notebook, or (--check) fail if any
# notebook still carries output without touching it.
#
#   tests/strip-notebook-outputs.sh            # rewrite dirty notebooks in place
#   tests/strip-notebook-outputs.sh --check    # CI gate: list them, exit 1, change nothing
#
# Covers every *.ipynb under task1/lab/task: 00_Home.ipynb and
# part1/*.ipynb today, plus anything added later, skipping
# .ipynb_checkpoints/ and work/ (learner output on a devbox).
#
# Run this BEFORE committing anything under task1/lab/task/. That directory is
# bind-mounted into the lab container at /dli/task, so any notebook a learner
# (or you) runs in JupyterLab writes its output straight back into this repo.
# `git add` then sweeps up megabytes of base64 images and video, and ships
# notebooks pre-filled with someone else's results.
#
# A dirty notebook is rewritten exactly as JupyterLab saves it (indent 1,
# UTF-8, trailing newline); a clean one is never rewritten. Cell ids, metadata
# and nbformat 4.5 pass through untouched.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../lab/task" && pwd)"
MODE="${1:-fix}"
case "${MODE}" in
    fix) ;;
    --check|check) MODE=check ;;
    *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

python3 -I - "${ROOT}" "${MODE}" <<'PY'
import json, pathlib, sys
root, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
seen, dirty = 0, []
for path in sorted(root.rglob("*.ipynb")):
    parts = path.relative_to(root).parts
    if ".ipynb_checkpoints" in parts or "work" in parts:
        continue
    seen += 1
    nb = json.loads(path.read_text(encoding="utf-8"))
    cells = [c for c in nb["cells"]
             if c["cell_type"] == "code" and (c.get("outputs") or c.get("execution_count") is not None)]
    if not cells:
        continue
    dirty.append(path)
    rel = path.relative_to(root)
    if mode == "check":
        ids = ", ".join(str(c.get("id")) for c in cells[:5])
        print(f"{rel}: {len(cells)} code cell(s) with outputs or execution counts (first: {ids})")
        continue
    for c in cells:
        c["outputs"], c["execution_count"] = [], None
    path.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"cleared {rel}")
if seen == 0:
    print(f"no notebooks found under {root}")
    sys.exit(2)
if mode == "check" and dirty:
    print(f"{len(dirty)} of {seen} notebooks carry outputs; run task1/tests/strip-notebook-outputs.sh and commit")
    sys.exit(1)
print(f"{seen} notebooks clean" if mode == "check" else f"{seen} notebooks checked, {len(dirty)} cleared")
PY
