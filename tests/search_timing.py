"""
How long a build takes, part by part, and a fingerprint of the build it finds.

    python tests/search_timing.py                 deep search (the app's default) for one case per class
    python tests/search_timing.py --quick         the quicker search instead
    python tests/search_timing.py --cases build   the build check's cases (tests/build_check.py)
    python tests/search_timing.py --json out.json also save the results as JSON
    python tests/search_timing.py --only "mage L106 spells"   just that case

For each case it prints the total time, the time in each part of the whole-build search, the time outside it
(tree planning, the report's other sections), and a fingerprint: every item with its powders, the weapon, the
ability tree, the skill points and the score. A speed-up must leave every fingerprint exactly the same.
"""
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.environ["WYNN_HELPER_NO_POPUP"] = "1"

CLASS_CASES = [
    {"name": "warrior L106 melee", "class": "warrior", "level": 106, "goal": "melee", "crafted": True},
    {"name": "mage L106 spells", "class": "mage", "level": 106, "goal": "spells", "crafted": True},
    {"name": "archer L100 melee", "class": "archer", "level": 100, "goal": "melee", "crafted": True},
    {"name": "shaman L80 spells", "class": "shaman", "level": 80, "goal": "spells", "crafted": True},
    {"name": "assassin L106 balanced", "class": "assassin", "level": 106, "goal": "balanced", "crafted": True},
]


def fingerprint(best) -> dict:
    def item(x):
        if x is None:
            return None
        code = getattr(x, "link_code", None)
        return [x.name, [repr(p) for p in (getattr(x, "powders", None) or [])], repr(code) if code else None]
    fp = {"armor": [item(x) for x in best["armor"]], "weapon": item(best["weapon"]),
          "tree": sorted(best["owned"]), "extra": list(best["extra"]), "score": repr(best["eval"]["main"])}
    fp["hash"] = hashlib.sha1(json.dumps(fp, sort_keys=True).encode()).hexdigest()[:16]
    return fp


def main():
    import wynn_app as app
    import wynn_helper as wh
    import wynn_search as ws
    argv = sys.argv[1:]
    quick = "--quick" in argv
    cases = CLASS_CASES
    if "--cases" in argv and argv[argv.index("--cases") + 1] == "build":
        import build_check
        cases = build_check.CASES
    out_path = argv[argv.index("--json") + 1] if "--json" in argv else None
    if "--only" in argv:                         # just these cases (names, comma-separated)
        want = {x.strip() for x in argv[argv.index("--only") + 1].split(",")}
        cases = [c for c in cases if c["name"] in want]

    ws.remembered_starts = lambda *a, **k: []      # the same answer every time: no remembered builds
    ws.remember = lambda *a, **k: None
    app.save_history = lambda *a, **k: None
    app.STATE["data_checked"] = True
    print(f"{'Quick' if quick else 'Deep'} search, {ws.cpu_cores()} CPU cores", flush=True)
    results = []
    t_all = time.time()
    for case in cases:
        params = {k: v for k, v in case.items() if k != "name"}
        params.update(deep_search=not quick, include_level_plan=False)
        captured = {}
        real = ws.search_best_build

        def spy(*a, **k):
            r = real(*a, **k)
            captured["search"] = r
            return r
        ws.search_best_build = spy
        t = time.time()
        try:
            res = app.run_report(params)
        finally:
            ws.search_best_build = real
        total = time.time() - t
        best = wh.LAST_RESULT.get("best")
        sr = captured.get("search") or {}
        row = {"case": case["name"], "ok": res["ok"], "seconds": round(total, 1),
               "search": round(sr.get("seconds", 0), 1), "outside_search": round(total - sr.get("seconds", 0), 1),
               "stages": sr.get("timings", {}), "fingerprint": fingerprint(best) if best else None}
        results.append(row)
        stages = "  ".join(f"{k} {v:.0f}s" for k, v in row["stages"].items())
        print(f"{case['name']:<34} {total:6.0f}s total ({row['search']:.0f}s search, {row['outside_search']:.0f}s other)  "
              f"build {row['fingerprint']['hash'] if best else '-'}  score {best['eval']['main'] if best else 0:,.1f}", flush=True)
        print(f"    {stages}", flush=True)
    print(f"\nAll cases: {time.time() - t_all:.0f}s", flush=True)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=1)


if __name__ == "__main__":
    main()
