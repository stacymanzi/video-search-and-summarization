#!/usr/bin/env python3
"""Typography gate: the course ships with no em dashes.

Scans every text file in the repository (notebooks are scanned cell by
cell so the report names the cell) and lists each line that contains the
character U+2014. Exit status is 1 when anything is found, 0 otherwise.

Run from the repository root or from task1/:

    python3 -I task1/tests/check_typography.py
    python3 -I task1/tests/check_typography.py --fix   # replace " — " and "—" with ", " (review the diff afterwards)
"""
import json
import pathlib
import sys

EM_DASH = "—"
TEXT_SUFFIXES = {".md", ".py", ".sh", ".yml", ".yaml", ".txt", ".json", ".conf", ".cfg", ".ini", ".toml", ".env", ".html", ".rst"}
TEXT_NAMES = {"data_sources", ".env", ".launchignore", ".dockerignore", ".gitignore", "Dockerfile"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".ipynb_checkpoints", "_build", ".venv"}  # docs build output
SKIP_FILES = {"check_typography.py"}
# Vendored Singapore packages under task2: they must stay byte-identical
# (install.sh checks SHA256SUMS, part2-skills.sha256 pins Part-2), so they
# keep their em dashes. Never run --fix over them.
SKIP_TREES = (("task2", "lab", "task", "aicity_cvfix4"), ("task2", "lab", "task", "Part-2"))


def repo_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "task1").is_dir() and (parent / "BUILD.md").exists():
            return parent
    return here.parents[2]


def iter_files(root: pathlib.Path):
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root).parts
        if any(rel[:len(tree)] == tree for tree in SKIP_TREES):
            continue
        if not path.is_file() or path.name in SKIP_FILES:
            continue
        if path.suffix == ".ipynb" or path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES or path.name.startswith("Dockerfile."):
            yield path


def scan_notebook(path: pathlib.Path, fix: bool):
    findings = []
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    for index, cell in enumerate(data.get("cells", [])):
        source = cell.get("source", [])
        lines = source if isinstance(source, list) else source.splitlines(keepends=True)
        for line_no, line in enumerate(lines, start=1):
            if EM_DASH in line:
                findings.append(f"{path}: cell {index} line {line_no}: {line.strip()[:120]}")
        if fix and any(EM_DASH in line for line in lines):
            cell["source"] = [fix_line(line) for line in lines]
            changed = True
    if changed:
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return findings


def scan_text(path: pathlib.Path, fix: bool):
    findings = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return findings
    for line_no, line in enumerate(text.splitlines(), start=1):
        if EM_DASH in line:
            findings.append(f"{path}:{line_no}: {line.strip()[:120]}")
    if fix and EM_DASH in text:
        path.write_text("".join(fix_line(line) for line in text.splitlines(keepends=True)), encoding="utf-8")
    return findings


def fix_line(line: str) -> str:
    line = line.replace(" " + EM_DASH + " ", ", ")
    line = line.replace(EM_DASH + " ", ", ")
    line = line.replace(" " + EM_DASH, ", ")
    return line.replace(EM_DASH, ", ")


def main(argv):
    fix = "--fix" in argv
    root = repo_root()
    findings = []
    for path in iter_files(root):
        if path.suffix == ".ipynb":
            findings.extend(scan_notebook(path, fix))
        else:
            findings.extend(scan_text(path, fix))
    for item in findings:
        print(item)
    verb = "replaced" if fix else "found"
    print(f"{len(findings)} em dash line(s) {verb} under {root}")
    return 1 if findings and not fix else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
