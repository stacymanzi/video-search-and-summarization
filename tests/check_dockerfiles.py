#!/usr/bin/env python3
"""Keep task1/Dockerfile.assessment in step with task1/lab/Dockerfile.

Pebble's builder ignores compose `build.target` and builds the LAST stage of the
Dockerfile a service names (production launch of v0.0.3, 2026-10-22: the
assessment service got the lab stage and failed to start). So each image has its
own Dockerfile whose last stage is the image wanted, and the assessment file
carries a verbatim copy of the lab file between two marker lines.

Checks (exit 1 on the first failure):
  1. Dockerfile.assessment contains lab/Dockerfile byte-identical between the markers.
  2. The last `FROM` of lab/Dockerfile names stage `lab`; the last `FROM` of
     Dockerfile.assessment names stage `assessment`.
  3. docker-compose.override.yml has no `target:` key, builds `lab` and
     `course-prep` from lab/Dockerfile and `assessment` from Dockerfile.assessment.
`--fix` rewrites the copied section from lab/Dockerfile and re-runs the checks.
Standard library only; CI runs it in the checks:content job.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TASK1 = Path(__file__).resolve().parents[1]
LAB = TASK1 / "lab" / "Dockerfile"
ASSESS = TASK1 / "Dockerfile.assessment"
OVERRIDE = TASK1 / "docker-compose.override.yml"
BEGIN = "# ---- BEGIN verbatim copy of lab/Dockerfile (tests/check_dockerfiles.py --fix regenerates it) ----\n"
END = "# ---- END verbatim copy of lab/Dockerfile ----\n"


FROM_LINE = re.compile(r"^\s*FROM(?:\s+--\S+)*\s+\S+(?:\s+AS\s+(\S+))?\s*$", re.I | re.M)


def last_stage(text: str) -> str | None:
    """Alias of the LAST `FROM` instruction of any kind (flags and lowercase `as`
    included), or None when that stage is unnamed. A builder that ignores
    `target` builds exactly that stage, so an unnamed trailing FROM must fail."""
    stages = FROM_LINE.findall(text)
    return (stages[-1] or None) if stages else None


def split_assessment(text: str) -> tuple[str, str, str]:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise SystemExit(f"{ASSESS}: expected exactly one BEGIN and one END marker line")
    head, rest = text.split(BEGIN, 1)
    copied, tail = rest.split(END, 1)
    return head, copied, tail


def compose_builds() -> dict[str, dict[str, str]]:
    """Minimal reader for the `build:` blocks of the override (no PyYAML needed)."""
    builds: dict[str, dict[str, str]] = {}
    service = None
    in_build = False
    for line in OVERRIDE.read_text().splitlines():
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            service = line.strip()[:-1]
            in_build = False
            continue
        if service is None:
            continue
        if re.match(r"^    build:\s*$", line):
            in_build = True
            builds[service] = {}
            continue
        if in_build:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= 4:          # back at the service level: the build block is over
                in_build = False
                continue
            m = re.match(r"^      ([a-z_]+):\s*(.*?)\s*$", line)
            if m:                    # six-space keys; deeper lines (args: values) are skipped
                builds[service][m.group(1)] = m.group(2)
    return builds


def check() -> list[str]:
    problems: list[str] = []
    lab_text = LAB.read_text()
    assess_text = ASSESS.read_text()
    _, copied, _ = split_assessment(assess_text)
    if copied != lab_text:
        problems.append(f"{ASSESS.name}: the copied section differs from lab/Dockerfile (run with --fix)")
    if last_stage(lab_text) != "lab":
        problems.append(f"lab/Dockerfile: the LAST FROM must be the named stage `lab`, found {last_stage(lab_text)!r}")
    if last_stage(assess_text) != "assessment":
        problems.append(f"{ASSESS.name}: the LAST FROM must be the named stage `assessment`, found {last_stage(assess_text)!r}")
    if "/dli/entrypoint.assessment.sh" not in assess_text.split(END, 1)[1]:
        problems.append(f"{ASSESS.name}: the assessment stage must install /dli/entrypoint.assessment.sh")
    builds = compose_builds()
    for svc, b in builds.items():
        if "target" in b:
            problems.append(f"docker-compose.override.yml: {svc} uses build.target, which the platform builder ignores; give the image its own Dockerfile")
    want = {"lab": "lab/Dockerfile", "course-prep": "lab/Dockerfile", "assessment": "Dockerfile.assessment"}
    for svc, dockerfile in want.items():
        got = builds.get(svc, {})
        if got.get("context") != "." or got.get("dockerfile") != dockerfile:
            problems.append(f"docker-compose.override.yml: {svc} must build with context `.` and dockerfile `{dockerfile}`, found {got}")
        if not (TASK1 / dockerfile).exists():
            problems.append(f"{dockerfile} does not exist")
    return problems


def fix() -> None:
    head, _, tail = split_assessment(ASSESS.read_text())
    lab_text = LAB.read_text().rstrip("\n") + "\n"   # a lost trailing newline must not glue END onto ENTRYPOINT
    if LAB.read_text() != lab_text:
        LAB.write_text(lab_text)
    ASSESS.write_text(head + BEGIN + lab_text + END + tail)


def main(argv: list[str]) -> int:
    if "--fix" in argv:
        fix()
    problems = check()
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        print("Dockerfiles consistent: lab/Dockerfile ends in `lab`, Dockerfile.assessment ends in `assessment`, override builds without `target`")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
