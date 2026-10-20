#!/usr/bin/env python3
"""
Side-by-side check of the helper against Wynnbuilder's own JavaScript.

    python3 tests/crossval/run_all.py [--refresh-golden] [--no-update]

Needs Node.js 18+ and git. Clones (or updates) Wynnbuilder into tests/crossval/wynnbuilder, generates
random cases with the helper, runs the same cases through Wynnbuilder's code and prints the results.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
REPO = os.path.join(HERE, "wynnbuilder")
WORK = os.path.join(HERE, "work")
GOLDEN = os.path.join(ROOT, "tests", "golden")
WB_GIT = "https://github.com/wynnbuilder/wynnbuilder.github.io.git"

# (label, python generator, node checker)
CHECKS = [
    ("damage", "gen.py", "run.js"),
    ("skill points", "gen_sp.py", "run_sp.js"),
    ("item rolls", "rolls.py", "rolls.js"),
    ("weapon powders", "powders.py", "powders.js"),
    ("defenses / EHP", "ehp.py", "ehp.js"),
    ("links", "links.py", "links.js"),
    ("crafted items", "gen_craft.py", "craft.js"),
    ("custom items", "custom_gen.js", "custom_check.py"),      # Wynnbuilder encodes, the helper decodes
    ("links with crafted/custom", "links2.py", "links2.js"),
]
GOLDEN_FILES = ["cases.json", "sp_cases.json", "powder_cases.json", "ehp_cases.json", "link_cases.json",
                "craft_cases.json", "custom_cases.json", "link2_cases.json"]


def sh(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def ensure_repo(update: bool):
    if shutil.which("git") is None:
        sys.exit("git is needed (https://git-scm.com).")
    if not os.path.isdir(os.path.join(REPO, ".git")):
        print("Cloning Wynnbuilder (first run only)...")
        r = sh(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", WB_GIT, REPO])
        if r.returncode:
            sys.exit("git clone failed:\n" + r.stderr)
    elif update:
        sh(["git", "pull", "--depth", "1"], cwd=REPO)
    listing = sh(["git", "ls-tree", "-d", "--name-only", "HEAD", "data/"], cwd=REPO).stdout.split()
    versions = sorted([os.path.basename(p) for p in listing if re.fullmatch(r"data/\d+(\.\d+)+", p)],
                      key=lambda v: tuple(int(x) for x in v.split(".")))
    if not versions:
        sys.exit("Couldn't find Wynnbuilder's data folders.")
    ver = versions[-1]
    sh(["git", "sparse-checkout", "set", "js", f"data/{ver}"], cwd=REPO)
    return ver


def result_ok(line: str) -> bool:
    """Each check ends with 'identical: A / B ...' or '... worst ... difference X'."""
    if "ERROR" in line:
        return False
    m = re.search(r"mismatches:\s*(\d+)", line)
    if m:                                   # skill points: ties (equal total) are fine
        return m.group(1) == "0"
    m = re.search(r"difference\s+([0-9.eE+-]+)", line)
    if m:
        return float(m.group(1)) < 1e-6
    m = re.findall(r"(\d+)\s*/\s*(\d+)", line)
    return bool(m) and m[0][0] == m[0][1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-golden", action="store_true", help="copy the new cases into tests/golden")
    ap.add_argument("--no-update", action="store_true", help="don't git pull Wynnbuilder")
    args = ap.parse_args()
    if shutil.which("node") is None:
        sys.exit("Node.js 18+ is needed (https://nodejs.org).")
    ver = ensure_repo(not args.no_update)
    data = os.path.join(REPO, "data", ver)
    print(f"Wynnbuilder data version {ver}")
    os.makedirs(WORK, exist_ok=True)
    shutil.copy(os.path.join(data, "encoding_consts.json"), os.path.join(WORK, "encoding_consts.json"))
    env = dict(os.environ, WB_REPO=REPO, WB_DATA=data + os.sep, WB_DATA_ROOT=os.path.join(REPO, "data"),
               HELPER_ROOT=ROOT, WB_VERSION=ver)
    all_ok = True
    for label, gen, check in CHECKS:
        outputs = []
        for script in (gen, check):
            path = os.path.join(HERE, script)
            cmd = ["node", path] if script.endswith(".js") else [sys.executable, path]
            r = sh(cmd, cwd=WORK, env=env)
            if r.returncode:
                outputs.append("ERROR: " + (r.stderr.strip().splitlines() or ["?"])[-1])
                all_ok = False
                break
            outputs.append(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")
        result = outputs[-1]
        print(f"  {label:<26} {result}")
        if not result_ok(result):
            all_ok = False
    if args.refresh_golden:
        if not all_ok:
            sys.exit("Not refreshing tests/golden: some checks differ.")
        os.makedirs(GOLDEN, exist_ok=True)
        for f in GOLDEN_FILES:
            src = os.path.join(WORK, f)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(GOLDEN, f))
        with open(os.path.join(GOLDEN, "version.json"), "w") as fh:
            json.dump({"wynnbuilder_data": ver}, fh)
        print("tests/golden updated.")
    print("All identical." if all_ok else "Some checks differ; see above.")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
