"""
Run every check at once, using all of this computer's CPU cores.

    python tests/run_all.py              every check the automatic GitHub checks run (app, self-test, toggles,
                                         speed, crafts, and the build for every class)
    python tests/run_all.py --full       also the side-by-side comparison with Wynnbuilder's code (needs Node.js
                                         and git) and brute-force accuracy cases (one per core, up to 24)
    python tests/run_all.py --record     save higher build scores to tests/golden/build_baseline.json
                                         (never lowers one)
    python tests/run_all.py --cores 16   use 16 cores instead of all of them

The build cases run side by side, each on its share of the cores; the answers don't depend on how many cores
a case gets, only how fast it finishes. The summary is printed at the end and saved to tests/last_run.txt.
On Windows you can double-click "Run all checks.bat" in this folder instead.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

QUICK = [("App, updater and release files", ["app_check.py"]),
         ("Self-test (matches Wynnbuilder)", ["self_test.py"]),
         ("Ability buffs, every class", ["toggle_check.py"]),
         ("Fast skill points = reference", ["speed_check.py"]),
         ("Crafted item designer", ["craft_check.py"]),
         ("Goal mix, mana check, armor powders", ["goal_check.py"])]


def main():
    argv = sys.argv[1:]
    full, record = "--full" in argv, "--record" in argv
    total = os.cpu_count() or 1
    if "--cores" in argv:
        total = max(1, int(argv[argv.index("--cores") + 1]))
    import build_check
    cases = [c["name"] for c in build_check.CASES]

    jobs = [(name, [sys.executable, os.path.join(HERE, args[0])] + args[1:], 1) for name, args in QUICK]
    per_case = max(1, (total - len(QUICK)) // max(1, len(cases)))
    jobs += [(f"Build: {c}", [sys.executable, os.path.join(HERE, "build_check.py"), "--case", c], per_case) for c in cases]
    if full:
        jobs.append(("Side by side with Wynnbuilder", [sys.executable, os.path.join(HERE, "crossval", "run_all.py")], 1))
        n_acc = max(3, min(24, total // 2))
        jobs += [(f"Accuracy case {s}", [sys.executable, os.path.join(HERE, "accuracy_test.py"), str(s), "1"], 1)
                 for s in range(1, n_acc + 1)]

    print(f"Running {len(jobs)} checks on {total} CPU cores (each build case gets {per_case}).", flush=True)
    t0 = time.time()
    env0 = dict(os.environ, PYTHONUTF8="1", WYNN_HELPER_NO_POPUP="1")
    procs = []
    for name, cmd, n in jobs:
        env = dict(env0, WYNN_HELPER_CORES=str(n))
        p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        procs.append((name, p, time.time()))
    results = []
    # Read each check's output as it finishes (reading as they go keeps their output buffers from filling).
    import threading
    outputs = {}

    def reader(name, p):
        outputs[name] = p.stdout.read()
    threads = [threading.Thread(target=reader, args=(name, p), daemon=True) for name, p, _ in procs]
    for t in threads:
        t.start()
    pending = list(procs)
    while pending:
        for item in list(pending):
            name, p, started = item
            if p.poll() is not None:
                pending.remove(item)
                threads[[x[0] for x in procs].index(name)].join(5)
                out = outputs.get(name, "")
                ok = p.returncode == 0
                secs = time.time() - started
                results.append((name, ok, secs, out))
                print(f"{'PASS' if ok else 'FAIL'}  {name:<52} ({secs:.0f}s)", flush=True)
        time.sleep(0.5)

    scores = {}
    for name, ok, secs, out in results:
        for line in out.splitlines():
            if line.startswith("SCORE\t"):
                _t, cname, val = line.split("\t", 2)
                scores[cname] = float(val)
    if record and scores:
        path = build_check.BASELINE
        base = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                base = json.load(fh)
        for k, v in scores.items():
            if k not in base or v > base[k]:
                base[k] = v
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(base, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("Recorded higher build scores in tests/golden/build_baseline.json.")

    failed = [r for r in results if not r[1]]
    lines = [f"Ran {len(results)} checks on {total} cores in {time.time() - t0:.0f}s: "
             f"{len(results) - len(failed)} passed, {len(failed)} failed.", ""]
    for name, ok, secs, out in sorted(results, key=lambda r: (r[1], r[0])):
        lines.append(f"{'PASS' if ok else 'FAIL'}  {name}  ({secs:.0f}s)")
        if not ok:
            tail = [l for l in out.splitlines() if l.strip() and "Warning" not in l][-15:]
            lines += ["    " + l for l in tail]
    if scores:
        lines += ["", "Build scores:"] + [f"  {k}: {v:,.1f}" for k, v in sorted(scores.items())]
    text = "\n".join(lines)
    print("\n" + text)
    try:
        with open(os.path.join(HERE, "last_run.txt"), "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M") + "\n" + text + "\n")
    except OSError:
        pass
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
