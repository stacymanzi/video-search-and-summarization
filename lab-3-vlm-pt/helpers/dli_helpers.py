"""Helpers for the Cosmos 3 Nano LoRA post-training lab.

Plumbing only — dataset acquisition, video cutting, the docker invocation, and
scoring. The parts a student should actually read (task definition, dataset
design, hyperparameters, results) stay inline in the notebook cells.

Nothing in this module is specific to a use case. The label space is passed in
from the notebook's TASK config, so swapping the lab to a different video
classification problem means editing one cell, not this file.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# TAO 7.2.0 Cosmos-RL. This single image covers checkpoint preparation,
# training and evaluation: it packages the TAO-owned conversion entrypoint
# `cosmos_rl.model_preparation.vlm_safetensors` alongside an isolated
# Cosmos-Framework converter venv, attested by
# /opt/tao/framework-converter-runtime.json.
IMAGE = os.environ.get("DLI_IMAGE", "nvcr.io/nvidia/tao/tao-toolkit:7.2.0-cosmos-rl")

# Answers are emitted as a single line, `LABEL=<class>`, where <class> is one of
# the task's positive classes or `none`. One token to parse, no free text.
ANSWER_RE = re.compile(r"label\s*=\s*([a-z0-9_]+)", re.I)


# ------------------------------------------------------------- credentials

def load_env_file(path, keys=None, override=False):
    """Read KEY=VALUE lines from a .env file into os.environ.

    The environment wins by default: a value already exported stays. Accepts the
    `export KEY=value` form and strips matching quotes. Returns the names it set,
    never the values, so a caller can report what happened without leaking a
    secret into notebook output.
    """
    path = Path(path)
    if not path.is_file():
        return []
    loaded = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if keys is not None and key not in keys:
            continue
        if not value:
            continue          # an unfilled template entry is not a credential
        if override or not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded


def credential_status(names, env_file=None):
    """Resolve credentials from the environment, falling back to a .env file.

    Prints where each one came from, and never prints a value.
    """
    before = {n: bool(os.environ.get(n)) for n in names}
    from_file = load_env_file(env_file, keys=names) if env_file else []
    ok = True
    for n in names:
        if before[n]:
            source = "environment"
        elif n in from_file:
            source = f"{Path(env_file).name}"
        else:
            source = None
        ok &= check(f"{n}", source is not None,
                    f"from {source}" if source else "not set")
    return ok


# ----------------------------------------------------------------- preflight

def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    return ok


def sh(cmd, **kw):
    """Run a shell command, echoing it first. Returns CompletedProcess."""
    print(f"  $ {cmd if isinstance(cmd, str) else ' '.join(map(str, cmd))}")
    return subprocess.run(cmd, shell=isinstance(cmd, str), **kw)


def gpu_table() -> list[dict]:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used",
         "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    gpus = []
    for line in out.strip().splitlines():
        i, name, tot, used = [x.strip() for x in line.split(",")]
        gpus.append({"index": int(i), "name": name,
                     "total_gb": round(int(tot) / 1024, 1),
                     "used_gb": round(int(used) / 1024, 1)})
    return gpus


def pip_install(packages: list[str], quiet=True) -> bool:
    """Install into the running kernel's interpreter.

    Debian/Ubuntu images mark the system interpreter as externally managed
    (PEP 668), which makes a plain `pip install` fail; `--break-system-packages`
    is the documented escape hatch and is safe inside a disposable lab image.
    """
    base = [sys.executable, "-m", "pip", "install"] + (["-q"] if quiet else [])
    r = subprocess.run(base + packages, capture_output=True, text=True)
    if r.returncode != 0 and "externally-managed-environment" in (r.stdout + r.stderr):
        r = subprocess.run(base + ["--break-system-packages"] + packages,
                           capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
    return r.returncode == 0


SKILL_BANK_URL = "https://github.com/NVIDIA-TAO/tao-skill-bank.git"
# Pin the Skill Bank to the release matching the container image. It ships the
# single-GPU video helpers, which must line up with the image they run inside.
SKILL_BANK_REF = os.environ.get("DLI_SKILL_BANK_REF", "7.2.0")
HELPER_SUBDIR = "skills/models/tao-finetune-cosmos-reason/scripts"


def clone_skill_bank(dest, url=SKILL_BANK_URL, ref=SKILL_BANK_REF, depth=1):
    """Clone the TAO Skill Bank at a pinned ref over https (no key or PAT)."""
    dest = Path(dest)
    if (dest / HELPER_SUBDIR).is_dir():
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc = sh(["git", "-c", "advice.detachedHead=false", "clone", "--quiet",
             "--depth", str(depth), "--branch", ref, url, str(dest)]).returncode
    if rc != 0:
        print("  clone failed; set DLI_SKILL_BANK to an existing checkout")
    return (dest / HELPER_SUBDIR).is_dir()


def skill_bank_complete(path):
    """A real 7.2.0 checkout: manifest present and the model skills directory populated.
    A partial S3 copy passes an is_dir() test, so look for the files that matter."""
    p = Path(path)
    return (p / "versions.yaml").is_file() and any(p.glob("skills/models/*/SKILL.md"))


def ensure_skill_bank(path, ref=SKILL_BANK_REF):
    """Return a complete Skill Bank checkout, cloning the pinned tag if the staged copy
    is missing or partial. The partial copy is left untouched; the clone goes next to it."""
    path = Path(path)
    if skill_bank_complete(path):
        return path
    alt = path.with_name(f"{path.name}-{ref}")
    if skill_bank_complete(alt):
        return alt
    print(f"  staged Skill Bank at {path} is missing or incomplete; cloning tag {ref} -> {alt}")
    if alt.exists():
        shutil.rmtree(alt)
    if clone_skill_bank(alt, ref=ref) and skill_bank_complete(alt):
        return alt
    return path


def verify_checkpoint(ptm):
    """Check that a converted checkpoint is complete and readable, and print its shape.

    The loader copies files one by one, so "some shards exist" is not "the checkpoint
    is here": every shard named by the index must be present. Raises with guidance
    when it is not, because nothing later in the lab can run without it."""
    ptm = Path(ptm)
    shards = sorted(ptm.glob("*.safetensors"))
    index = ptm / "model.safetensors.index.json"
    expected = sorted(set(json.loads(index.read_text())["weight_map"].values())) if index.exists() else []
    missing = [x for x in expected if x not in {q.name for q in shards}]
    complete = bool(shards) and not missing and (ptm / "config.json").exists()
    detail = f"{len(shards)} shards" + (f", {len(missing)} still missing" if missing else "")
    if not check(f"base checkpoint at {ptm}", complete, detail):
        raise RuntimeError(
            f"No complete base checkpoint under {ptm}. The course loader stages it before the lab "
            "starts - wait for it to finish or ask a TA. (Elsewhere: set BUILD_PTM = True with HF_TOKEN exported.)")
    cfg = json.loads((ptm / "config.json").read_text())
    text = cfg.get("text_config", cfg)
    print(f"       model_type={cfg['model_type']}  arch={cfg['architectures'][0]}  "
          f"hidden={text.get('hidden_size')}  layers={text.get('num_hidden_layers')}")
    # The container runs as this user; an unreadable shard surfaces much later as a
    # confusing "No such file or directory", so check readability now.
    try:
        shards[0].open("rb").read(8)
        check("checkpoint readable by this user", True)
    except PermissionError:
        check("checkpoint readable by this user", False, "run: chmod -R u+r <ptm>")


def helper_dir(skill_bank):
    """Directory holding the packaged single-GPU train/evaluate helpers."""
    d = Path(skill_bank) / HELPER_SUBDIR
    if not d.is_dir():
        raise FileNotFoundError(
            f"single-GPU helpers not found under {d}; clone the Skill Bank first")
    return d


def load_toml(path):
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def apply_settings(config: dict, settings: dict) -> dict:
    """Overlay dotted-key settings onto a nested config.

    `{"model.enable_lora": True}` sets config["model"]["enable_lora"]. Keys must
    already exist, so a typo fails here rather than being silently ignored by the
    container hours later.
    """
    import copy
    out = copy.deepcopy(config)
    for dotted, value in settings.items():
        parts = dotted.split(".")
        node = out
        for part in parts[:-1]:
            if part not in node:
                raise KeyError(f"{dotted}: no section '{part}' in the config file")
            node = node[part]
        if parts[-1] not in node:
            raise KeyError(f"{dotted}: no key '{parts[-1]}' in the config file")
        node[parts[-1]] = value
    return out


def write_toml(config: dict, path):
    import tomli_w
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(tomli_w.dumps(config).encode())
    return Path(path)


def show_settings(settings: dict, title=""):
    if title:
        print(title)
    width = max(len(k) for k in settings)
    for k, v in settings.items():
        print(f"  {k:<{width}}  {v}")


# ------------------------------------------------------------------ dataset

def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


_CLIP_CACHE = {}


def _clip_bytes(video_path, seconds=10):
    """Return the first `seconds` of a clip as mp4 bytes.

    Full clips run to 33 s and every player is embedded in the notebook as
    base64, so an untrimmed page becomes very large and slow to open. Ten
    seconds is enough to see what a clip contains. Falls back to the original
    file if ffmpeg is unavailable.
    """
    video_path = Path(video_path)
    key = (str(video_path), seconds)
    if key in _CLIP_CACHE:
        return _CLIP_CACHE[key]
    data = None
    if seconds and shutil.which("ffmpeg"):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "clip.mp4"
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(video_path), "-t", str(seconds),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                 "-vf", "scale=640:-2", "-an", "-movflags", "+faststart",
                 str(dst), "-y"], capture_output=True)
            if r.returncode == 0 and dst.exists():
                data = dst.read_bytes()
    if data is None:
        data = video_path.read_bytes()
    _CLIP_CACHE[key] = data
    return data


def show_video(video_path, width=640, seconds=10):
    """Inline player for the first `seconds` of a clip."""
    from IPython.display import HTML
    data = base64.b64encode(_clip_bytes(video_path, seconds)).decode()
    return HTML(f'<video width="{width}" controls>'
                f'<source src="data:video/mp4;base64,{data}" type="video/mp4"></video>')


def video_grid(paths, labels=None, width=330, seconds=10):
    """Side-by-side players — used to show one clip under each condition."""
    from IPython.display import HTML
    labels = labels or [Path(p).stem for p in paths]
    cells = []
    for p, lab in zip(paths, labels):
        data = base64.b64encode(_clip_bytes(p, seconds)).decode()
        cells.append(
            f'<div style="display:inline-block;margin:4px;text-align:center">'
            f'<div style="font-family:system-ui;font-size:13px;font-weight:600;'
            f'padding:2px">{lab}</div>'
            f'<video width="{width}" controls>'
            f'<source src="data:video/mp4;base64,{data}" type="video/mp4"></video></div>')
    return HTML("".join(cells))


def _rule(width=64, char="-"):
    print(char * width)


def dataset_table(train_orig, train_aug, val_orig, val_aug, classes):
    """Per-label counts for both splits and both annotation files."""
    from collections import Counter
    labels = list(classes) + ["none"]
    to = Counter(r["label"] for r in train_orig)
    ta = Counter(r["label"] for r in train_aug)
    vo = Counter(r["label"] for r in val_orig)
    va = Counter(r["label"] for r in val_aug)

    print(f"{'':<12}{'TRAIN':^20}  {'VALIDATION':^20}")
    print(f"{'label':<12}{'original':>10}{'augmented':>10}  "
          f"{'original':>10}{'augmented':>10}")
    _rule(54)
    for lab in labels:
        print(f"{lab:<12}{to[lab]:>10}{ta[lab]:>10}  {vo[lab]:>10}{va[lab]:>10}")
    _rule(54)
    print(f"{'total':<12}{len(train_orig):>10}{len(train_aug):>10}  "
          f"{len(val_orig):>10}{len(val_aug):>10}")
    print(f"{'clips':<12}{len({r['source_clip'] for r in train_orig}):>10}"
          f"{len({r['source_clip'] for r in train_aug}):>10}  "
          f"{len({r['source_clip'] for r in val_orig}):>10}"
          f"{len({r['source_clip'] for r in val_aug}):>10}")
    var = Counter(r.get("variant", "original") for r in val_aug)
    print(f"\nconditions in the augmented files: "
          f"{', '.join(f'{k} ({v})' for k, v in sorted(var.items()))}")
    print("The augmented file contains the originals as well, so it is a superset.")


def prompt_table(task, classes, negative):
    """Show the answer space and the exact prompts the model receives."""
    print("answers the model may give")
    _rule(64)
    described = {"collision": "two or more vehicles collide",
                 "stalled": "a vehicle is stopped in a travel lane"}
    for c in classes:
        print(f"  LABEL={c:<12} {described.get(c, c)}")
    print(f"  LABEL={'none':<12} anything else: normal traffic, an empty road")
    _rule(64)
    print("\nsystem prompt")
    print(f"  {task['system']}")
    print("\nquestion")
    for line in _wrap(task["question"], 76):
        print(f"  {line}")


def _wrap(text, width):
    import textwrap
    return textwrap.wrap(text, width)


def metrics_table(rows, title="", note=""):
    """rows: list of (name, metrics dict). Prints one aligned table."""
    if title:
        print(title)
    _rule(64, "=")
    print(f"{'model':<26}{'macro-F1':>10}{'precision':>11}{'recall':>9}{'acc':>8}")
    _rule(64)
    for name, m in rows:
        print(f"{name:<26}{m['macro_f1']:>10.3f}{m['precision']:>11.3f}"
              f"{m['recall']:>9.3f}{m['accuracy']:>8.3f}")
    _rule(64, "=")
    per = [m for _, m in rows if m.get("per_class")]
    if per:
        keys = sorted({k for m in per for k in m["per_class"]})
        w = max(20, max(len(n) for n, _ in rows) + 2)
        print(f"\n{'clips detected':<20}" + "".join(f"{n:>{w}}" for n, _ in rows))
        for k in keys:
            print(f"  {k:<18}" + "".join(
                f"{m['per_class'].get(k, '-'):>{w}}" for _, m in rows))
    if note:
        print(f"\n{note}")


def condition_table(rows, title=""):
    """rows: {condition: {'n':..,'ok':..,'pos':..,'pos_hit':..}}"""
    if title:
        print(title)
    print(f"{'condition':<12}{'clips':>7}{'accuracy':>10}{'recall':>9}")
    _rule(38)
    for c in sorted(rows):
        d = rows[c]
        rec = d["pos_hit"] / d["pos"] if d["pos"] else float("nan")
        print(f"{c:<12}{d['n']:>7}{d['ok']/d['n']:>10.3f}{rec:>9.3f}")


def per_clip(results_json, annotations, classes):
    """clip id -> {'gold','pred','label','variant','video'} for one eval run."""
    def stem(x):
        return Path(str(x or "")).name.removesuffix(".mp4")
    recs = {stem(r["video_id"]): r for r in annotations}
    out = {}
    for r in load_json(results_json):
        key = stem(r.get("video_id") or r.get("video"))
        meta = recs.get(key)
        if not meta:
            continue
        pred, _ = parse_answer(r.get("response") or "", classes)
        gold, _ = parse_answer(r.get("gt") or "", classes)
        out[key] = {"gold": gold, "pred": pred, "label": meta["label"],
                    "variant": meta.get("variant", "original"), "video": meta["video"]}
    return out


def find_improvements(base_clips, post_clips, limit=3):
    """Clips the base model got wrong and the tuned model gets right.

    Prefers one example per label so the examples are not all the same class.
    """
    gained = [(cid, b) for cid, b in base_clips.items()
              if cid in post_clips
              and b["pred"] != b["gold"]
              and post_clips[cid]["pred"] == post_clips[cid]["gold"]]
    picked, seen_label, seen_cond = [], set(), set()
    for cid, b in gained:                      # first pass: a new label each time
        if b["label"] not in seen_label:
            picked.append(cid); seen_label.add(b["label"]); seen_cond.add(b["variant"])
        if len(picked) == limit:
            return picked
    for cid, b in gained:                      # second: vary the capture condition
        if cid not in picked and b["variant"] not in seen_cond:
            picked.append(cid); seen_cond.add(b["variant"])
        if len(picked) == limit:
            return picked
    for cid, _ in gained:                      # last resort: fill up
        if cid not in picked:
            picked.append(cid)
        if len(picked) == limit:
            break
    return picked


def _said(pred, label):
    """Render a yes/no prediction as the answer a reader expects to see."""
    return f"LABEL={label}" if pred == "yes" else "LABEL=none"


def improvement_report(clip_id, base, tuned, val_dir, show=True):
    """Print expected / before / after for one clip, and play it."""
    from IPython.display import display
    expected = f"LABEL={base['label']}" if base["gold"] == "yes" else "LABEL=none"
    before   = _said(base["pred"], base["label"])
    after    = _said(tuned["pred"], tuned["label"])
    cond = base["variant"]
    print(f"{clip_id}   ({cond})")
    print(f"   {'expected':<22}{expected:<18}")
    print(f"   {'zero-shot model':<22}{before:<18}{'CORRECT' if before == expected else 'WRONG'}")
    print(f"   {'post-trained model':<22}{after:<18}{'CORRECT' if after == expected else 'WRONG'}")
    if show:
        display(show_video(Path(val_dir) / base["video"]))


def per_condition(results_json, annotations, classes):
    """Join evaluator predictions back to annotations and group by condition."""
    def stem(x):
        return Path(str(x or "")).name.removesuffix(".mp4")
    recs = {stem(r["video_id"]): r for r in annotations}
    rows, wrong, unmatched = {}, [], 0
    for r in load_json(results_json):
        meta = recs.get(stem(r.get("video_id") or r.get("video")))
        if not meta:
            unmatched += 1
            continue
        pred, _ = parse_answer(r.get("response") or "", classes)
        gold, _ = parse_answer(r.get("gt") or "", classes)
        cond = meta.get("variant", "original")
        d = rows.setdefault(cond, {"n": 0, "ok": 0, "pos": 0, "pos_hit": 0})
        d["n"] += 1
        d["ok"] += int(pred == gold)
        if gold == "yes":
            d["pos"] += 1
            d["pos_hit"] += int(pred == "yes")
        if pred != gold:
            wrong.append((cond, meta["label"], meta["id"], pred))
    return rows, wrong, unmatched


# -------------------------------------------------------------------- docker

_CONTAINER_LD_LIBRARY_PATH = ":".join([
    "/usr/local/lib",
    "/usr/local/lib/python3.12/dist-packages/torch/lib",
    "/usr/local/lib/python3.12/dist-packages/torch_tensorrt/lib",
    "/usr/local/nixlbench/lib",
    "/opt/amazon/efa/lib",
    "/usr/lib",
    "/usr/local/nvidia/lib",
    "/usr/local/nvidia/lib64",
    "/usr/local/nixl/lib/x86_64-linux-gnu",
])


def _docker_env(job_name: str, results_in_container="/results") -> list[str]:
    """Env every TAO cosmos-rl container needs when run as a non-root host uid.

    Each of these fixes a real failure:
      USER/LOGNAME        torch's cache_dir() calls getpass.getuser(); the host
                          uid has no /etc/passwd entry -> KeyError getpwuid
      TAO_API_JOB_ID      _get_results_dir() otherwise returns a RELATIVE
                          "./results" and fails with PermissionError
      *_CACHE_DIR/HOME    keep every cache inside the writable results mount
      LD_LIBRARY_PATH     the image puts /usr/local/cuda/compat first; that older libcuda
                          shadows the host driver when the host is newer, and torch fails
                          with cuInit CUDA_ERROR_SYSTEM_DRIVER_MISMATCH (803). Dropping the
                          compat entries lets the host driver (injected by the nvidia
                          runtime) be used. Same paths as the image default, minus compat.
    """
    return [
        "-e", "USER=taouser", "-e", "LOGNAME=taouser", "-e", f"HOME={results_in_container}",
        "-e", f"TAO_API_JOB_ID={job_name}", "-e", f"TAO_API_RESULTS_DIR={results_in_container}",
        "-e", f"TORCHINDUCTOR_CACHE_DIR={results_in_container}/.inductor",
        "-e", f"TRITON_CACHE_DIR={results_in_container}/.triton",
        "-e", f"XDG_CACHE_HOME={results_in_container}/.cache",
        "-e", f"HF_HOME={results_in_container}/.hf",
        # The lab has no network: any incidental hub lookup must fail fast instead of hanging on DNS.
        "-e", "HF_HUB_OFFLINE=1", "-e", "TRANSFORMERS_OFFLINE=1",
        "-e", "LD_LIBRARY_PATH=" + _CONTAINER_LD_LIBRARY_PATH,
    ]


# Container logs are extremely noisy (vLLM engine banners, Triton/pyarmor import
# warnings). Show only lines a student should read.
# Milestones only. The containers emit several log lines per batch; showing them
# all buries the run in thousands of near-identical lines.
_KEEP = re.compile(r"avg_loss|Validation at step|Exported safetensors"
                   r"|Checkpoint will be saved|checkpoint saved"
                   r"|Job FAILED|Traceback|AssertionError|Error:", re.I)
# Progress lines are throttled rather than dropped, so a long stage still moves.
_PROGRESS = re.compile(r"(\d+)/(\d+) requests completed \((\d+\.?\d*)%\)")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_DROP = re.compile(r"pyarmor|Triton kernels|Telemetry|EngineCore|WARNING|UserWarning"
                   r"|Failed to import|deprecat", re.I)


def _interesting(line: str) -> bool:
    return bool(_KEEP.search(line)) and not _DROP.search(line)


def _clean(line: str) -> str:
    line = _ANSI.sub("", line)
    # drop the logger prefix: "2026-.. - name - INFO - message"
    parts = line.split(" - ")
    return (parts[-1] if len(parts) > 2 else line).strip()


def docker_ok() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def image_present(image: str = IMAGE) -> bool:
    out = subprocess.run(["docker", "images", "-q", image],
                         capture_output=True, text=True).stdout.strip()
    return bool(out)


def pull_image(image: str = IMAGE, ngc_key: str | None = None) -> bool:
    """Log in to nvcr.io and pull. ~28 GB — normally pre-staged on a DLI node."""
    ngc_key = ngc_key or os.environ.get("NGC_KEY", "")
    if not ngc_key:
        print("  NGC_KEY not set — cannot pull")
        return False
    login = subprocess.run(["docker", "login", "nvcr.io", "-u", "$oauthtoken",
                            "--password-stdin"], input=ngc_key, text=True,
                           capture_output=True)
    if login.returncode != 0:
        print("  docker login failed:", login.stderr[-500:])
        return False
    print(f"  pulling {image} (~28 GB, several minutes) ...")
    return subprocess.run(["docker", "pull", image]).returncode == 0


def as_gpu_list(gpus):
    """Normalise 0 | "0" | [0, 1] | "0,1" to a list of ints."""
    if isinstance(gpus, (list, tuple)):
        return [int(g) for g in gpus]
    if isinstance(gpus, str):
        return [int(g) for g in gpus.replace(" ", "").split(",") if g != ""]
    return [int(gpus)]


def gpu_device_arg(gpus):
    """Docker --gpus value, e.g. 'device=0' or 'device=0,1'."""
    return '"device=' + ",".join(str(g) for g in as_gpu_list(gpus)) + '"'


def docker_command(job_name, gpus, mounts, command):
    """Build the exact `docker run` argv this helper would execute.

    Exposed so a notebook can show the command instead of describing it.
    """
    cmd = ["docker", "run", "--rm", "--name", job_name,
           "--gpus", gpu_device_arg(gpus), "--shm-size=32g", "--ipc=host",
           "-u", f"{os.getuid()}:{os.getgid()}", "-w", "/results"]
    cmd += _docker_env(job_name)
    for host, cont in mounts:
        cmd += ["-v", f"{host}:{cont}"]
    return cmd + [IMAGE] + list(command)


def show_docker_command(job_name, gpus, mounts, command):
    """Print the docker command in a readable, copy-pasteable form."""
    argv = docker_command(job_name, gpus, mounts, command)
    line, out = "docker", []
    for tok in argv[1:]:
        if tok in ("-v", "-e", "--gpus", "-u", "-w", "--name", "--rm",
                   "--shm-size=32g", "--ipc=host") or tok == IMAGE:
            out.append(line); line = "  " + tok
        else:
            line += " " + tok
    out.append(line)
    print(" \\\n".join(l for l in out if l.strip()))


_DRIVER_NOTICE = re.compile(r"built for NVIDIA Driver Release", re.I)


def run_container(job_name, gpus, mounts, command, log_path, timeout=7200, stream=True):
    """Run one TAO container, streaming a filtered view of its log.

    `gpus` accepts a single index or a list; it becomes the container's
    --gpus device list. The training topology must agree with its length.
    """
    cmd = ["docker", "run", "--rm", "--name", job_name,
           "--gpus", gpu_device_arg(gpus), "--shm-size=32g", "--ipc=host",
           "-u", f"{os.getuid()}:{os.getgid()}", "-w", "/results"]
    cmd += _docker_env(job_name)
    for host, cont in mounts:
        cmd += ["-v", f"{host}:{cont}"]
    cmd += [IMAGE] + command

    subprocess.run(["docker", "rm", "-f", job_name], capture_output=True)
    t0 = time.time()
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as lf:
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
        last, spoke, last_pct, prev = 0, 0.0, -1.0, ""
        while p.poll() is None:
            time.sleep(5)
            if not stream:
                continue
            lines = Path(log_path).read_text(errors="ignore").splitlines()
            newest_progress = None
            for l in lines[last:]:
                m = _PROGRESS.search(l)
                if m:
                    newest_progress = (int(m.group(1)), int(m.group(2)), float(m.group(3)))
                elif _interesting(l):
                    msg = _clean(l)[:120]
                    if msg != prev:          # every rank logs the same milestone
                        print(f"  [{(time.time()-t0)/60:5.1f}m] {msg}", flush=True)
                        # The driver-version notice is printed as "ERROR" by the
                        # container entrypoint but is not one. Say so on the spot -
                        # it is the first thing every run prints and it looks fatal.
                        if _DRIVER_NOTICE.search(l):
                            print("           ^ expected: a version notice, not a failure. "
                                  "The run continues - keep waiting.", flush=True)
                        prev, spoke = msg, time.time()
            last = len(lines)
            # one progress line at most every 20s, and only if it actually moved
            if newest_progress and time.time() - spoke > 20:
                done, total, pct = newest_progress
                if pct - last_pct >= 10 or done == total:
                    print(f"  [{(time.time()-t0)/60:5.1f}m] {done}/{total} ({pct:.0f}%)", flush=True)
                    last_pct, spoke = pct, time.time()
            elif time.time() - spoke > 90:
                print(f"  [{(time.time()-t0)/60:5.1f}m] running ...", flush=True)
                spoke = time.time()
    dt = time.time() - t0
    print(f"\n[{job_name}] exit={p.returncode} in {dt/60:.1f} min  (log: {log_path})")
    return p.returncode, dt


# ------------------------------------------------------------------- scoring

def confusion_table(pairs, positive="anomaly", negative="none"):
    """Side-by-side confusion matrices for [(name, metrics), ...].

    `score()` already returns tp/fp/fn/tn, so this only lays them out. Rows are
    what the clip actually was; columns are what the model said.
    """
    w = 22
    print(" " * 16 + "".join(f"{n[:w-2]:^{w}}" for n, _ in pairs))
    print(" " * 16 + "".join(f"{'predicted':^{w}}" for _ in pairs))
    print(f"{'actual':<16}" + "".join(f"{positive:>10}{negative:>10}  " for _ in pairs))
    print("-" * (16 + w * len(pairs)))
    for label, keys in ((positive, ("tp", "fn")), (negative, ("fp", "tn"))):
        row = f"{label:<16}"
        for _, m in pairs:
            row += f"{m[keys[0]]:>10}{m[keys[1]]:>10}  "
        print(row)
    print("-" * (16 + w * len(pairs)))
    row = f"{'said ' + positive:<16}"
    for _, m in pairs:
        row += f"{m['tp'] + m['fp']:>10}{'':>10}  "
    print(row)


def parse_answer(text: str, classes: list[str], negative: str = "none"):
    """'LABEL=collision' -> ('yes', 'collision'). Unknown/absent -> negative."""
    m = ANSWER_RE.search(text or "")
    if m:
        raw = m.group(1).lower()
        for c in classes:
            if raw == c:
                return "yes", c
        return "no", negative
    # Fall back to a substring read so a chatty reply still scores rather than
    # silently becoming a negative.
    t = (text or "").lower()
    for c in classes:
        if c in t:
            return "yes", c
    return "no", negative


def score(items):
    """items: [{'answer','pred','label'}] -> metrics dict.

    macro-F1 averages the positive-class and negative-class F1 equally, so a
    model that just answers the negative class to everything cannot score well
    on an imbalanced set.
    """
    tp = sum(1 for i in items if i["answer"] == "yes" and i["pred"] == "yes")
    fp = sum(1 for i in items if i["answer"] == "no" and i["pred"] == "yes")
    fn = sum(1 for i in items if i["answer"] == "yes" and i["pred"] == "no")
    tn = sum(1 for i in items if i["answer"] == "no" and i["pred"] == "no")

    def f1(t, f_p, f_n):
        pr = t / (t + f_p) if (t + f_p) else 0.0
        rc = t / (t + f_n) if (t + f_n) else 0.0
        return (2 * pr * rc / (pr + rc)) if (pr + rc) else 0.0, pr, rc

    f1_pos, prec, rec = f1(tp, fp, fn)
    f1_neg, _, _ = f1(tn, fn, fp)

    per = defaultdict(lambda: [0, 0])
    for i in items:
        if i["answer"] == "yes":
            per[i["label"]][1] += 1
            per[i["label"]][0] += i["pred"] == "yes"

    return {"macro_f1": (f1_pos + f1_neg) / 2, "precision": prec, "recall": rec,
            "accuracy": (tp + tn) / len(items) if items else 0.0,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "per_class": {k: f"{v[0]}/{v[1]}" for k, v in sorted(per.items())}}


def show(name, m):
    print(f"{name:<28} macro-F1={m['macro_f1']:.3f}  P={m['precision']:.3f}  "
          f"R={m['recall']:.3f}  acc={m['accuracy']:.3f}")
    print(f"{'':<28} per-class detected: {m['per_class']}")


def load_eval_results(results_dir: Path, classes: list[str]):
    """Read the evaluator's per-sample predictions and score them."""
    files = list(Path(results_dir).rglob("results.json"))
    if not files:
        raise FileNotFoundError(f"no results.json under {results_dir}")
    data = json.loads(files[0].read_text())
    items = []
    for r in data:
        pred, _ = parse_answer(r.get("response") or "", classes)
        gold, gcls = parse_answer(r.get("gt") or "", classes)
        items.append({"pred": pred, "answer": gold, "label": gcls})
    return items, files[0]

# ---------------------------------------------------------------- checkpoint

BASE_MODEL_URI = "nvidia/Cosmos3-Nano"

# Checkpoint preparation runs in the SAME image as training. TAO owns the
# entrypoint `cosmos_rl.model_preparation.vlm_safetensors`, which dispatches to
# a Cosmos-Framework converter pinned in an isolated venv inside the image
# (/opt/venv/cosmos_framework_preparation). Importing `cosmos_framework` from
# the default interpreter therefore fails by design — always go through the
# TAO entrypoint.
CONVERT_IMAGE = os.environ.get("DLI_CONVERT_IMAGE", IMAGE)
CONVERT_ENTRYPOINT = "cosmos_rl.model_preparation.vlm_safetensors"

ARCH_MODEL_URI = "Qwen/Qwen3-VL-8B-Instruct"

_CONVERT_SCRIPT = chr(10).join([
    'set -Eeuo pipefail',
    'export HF_HOME=/cache/huggingface',
    'cd /',
    'SRC="$(python - <<\'PYX\'',
    'import os',
    'from huggingface_hub import snapshot_download',
    'print(snapshot_download(os.environ["BASE_MODEL"],',
    '                        revision=os.environ["BASE_REV"] or None,',
    '                        cache_dir="/cache/huggingface"))',
    'PYX',
    ')"',
    'echo "[convert] source checkpoint: $SRC"',
    'python -m "$CONVERT_ENTRYPOINT" --checkpoint-path "$SRC" --vlm-model-name "$ARCH_MODEL" -o "/output/$OUTPUT_NAME"',
])


def hf_revision(repo_id, token=None):
    """Resolve a repo's current commit sha, so a run is reproducible."""
    import urllib.request
    token = token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    req = urllib.request.Request(
        "https://huggingface.co/api/models/" + repo_id,
        headers={"Authorization": "Bearer " + token} if token else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("sha", "")


def ensure_ptm(ptm, cache_dir, skill_bank=None,
               base_model=BASE_MODEL_URI, image=None):
    """Download Cosmos3-Nano and merge it onto the Qwen3-VL visual tower.

    Produces the `qwen3_vl` safetensors layout TAO can load. Downloads ~50 GB of
    HuggingFace cache and writes ~17 GB. Roughly 20-40 minutes cold.
    """
    image = image or CONVERT_IMAGE
    ptm, cache_dir = Path(ptm), Path(cache_dir)
    if sorted(ptm.glob("*.safetensors")):
        print("  checkpoint already present at " + str(ptm))
        return True

    if not image_present(image):
        print("  conversion image " + image + " not present locally - pulling (~41 GB on disk)")
        if not pull_image(image):
            print("  could not pull the conversion image. Point DLI_PTM at a\n"
                  "  checkpoint someone has already converted, or set DLI_CONVERT_IMAGE.")
            return False

    ptm.mkdir(parents=True, exist_ok=True)
    for sub in ("", "home", "inductor", "triton"):
        (cache_dir / sub).mkdir(parents=True, exist_ok=True)
    base_rev = os.environ.get("DLI_BASE_REV") or hf_revision(base_model)
    print("  base   " + base_model + " @ " + base_rev)
    print("  tower  " + ARCH_MODEL_URI + " (converter default for Nano)")
    print("  output " + str(ptm))

    # Same uid-has-no-passwd-entry trap as the training container: torch's
    # cache_dir() calls getpass.getuser(), which raises "No username set in the
    # environment" when the container runs as a bare numeric uid. USER/LOGNAME
    # fix it; the *_CACHE_DIR vars keep every cache inside the writable mount.
    cmd = ["docker", "run", "--rm", "--ipc=host",
           "-u", str(os.getuid()) + ":" + str(os.getgid()),
           "-e", "USER=taouser", "-e", "LOGNAME=taouser",
           "-e", "HOME=/cache/home",
           "-e", "XDG_CACHE_HOME=/cache/home/.cache",
           "-e", "TORCHINDUCTOR_CACHE_DIR=/cache/inductor",
           "-e", "TRITON_CACHE_DIR=/cache/triton",
           "-e", "BASE_MODEL=" + base_model,
           "-e", "BASE_REV=" + base_rev,
           "-e", "OUTPUT_NAME=" + ptm.name,
           "-e", "CONVERT_ENTRYPOINT=" + CONVERT_ENTRYPOINT,
           "-e", "ARCH_MODEL=" + ARCH_MODEL_URI,
           "-e", "PYTHONUNBUFFERED=1",
           "-v", str(ptm.parent) + ":/output",
           "-v", str(cache_dir) + ":/cache"]
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        if os.environ.get(name):
            cmd += ["-e", name]
    cmd += ["--entrypoint", "bash", image, "-lc", _CONVERT_SCRIPT]

    print("  converting (20-40 min) ...")
    rc = subprocess.run(cmd).returncode
    ok = rc == 0 and bool(sorted(ptm.glob("*.safetensors")))
    if not ok:
        print("  conversion failed (exit " + str(rc) + ")")
    return ok


# ---------------------------------------------------------------- entrypoint
# This module doubles as the in-container merge entrypoint, so the lab ships one
# Python file instead of a helper module plus a standalone script. The merge must
# run inside the TAO image: `cosmos_rl.utils.lora_utils.merge_lora_model` is the
# supported implementation and exists only there.

def _merge_cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Merge a LoRA adapter into its base checkpoint.")
    ap.add_argument("--base", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    base, adapter, out = Path(a.base), Path(a.adapter), Path(a.out)

    cfg = json.loads((adapter / "adapter_config.json").read_text())
    print(f"[merge] adapter r={cfg['r']} alpha={cfg['lora_alpha']} "
          f"targets={cfg.get('target_modules')}")

    from cosmos_rl.utils.lora_utils import merge_lora_model

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        shutil.rmtree(out)
    merged = Path(merge_lora_model(
        str(adapter), base_model_path=str(base), merged_model_path=str(out),
        progress_callback=lambda m: print(f"[merge] {m}", flush=True))).resolve()

    shards = sorted(merged.glob("*.safetensors"))
    if not shards:
        raise SystemExit(f"no safetensors written under {merged}")
    (merged / "merge_info.json").write_text(json.dumps({
        "base": str(base), "adapter": str(adapter),
        "r": cfg["r"], "lora_alpha": cfg["lora_alpha"],
        "target_modules": cfg.get("target_modules"),
        "implementation": "cosmos_rl.utils.lora_utils.merge_lora_model",
        "shards": [f.name for f in shards],
    }, indent=2))
    print(f"[merge] done: {len(shards)} shards -> {merged}")


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "merge":
        _merge_cli(_sys.argv[2:])
    else:
        raise SystemExit("usage: dli_helpers.py merge --base B --adapter A --out O")
