"""
Build check: runs the whole app (the same path as asking the bot for a build) once for every class, then checks
that what it hands back is something you can really wear, and that it's never worse than before.

For every class it checks:

  * the app finished and gave a report and a results screen
  * every item is in level range, the weapon is the class's own type
  * skill points, worked out from scratch: gear needs + spare points fit the level, no skill over 100
  * the ability tree is legal and fits the ability points for the level
  * any minimums asked for are met
  * crafted pieces (designed by default) can be made in game, and their crafter codes read back the same
  * the build as a share code opens as exactly the same build (items, powders, crafts, tree, skill points)
  * the score is at least the score recorded in tests/golden/build_baseline.json (builds never get worse)

    python3 tests/build_check.py              (all cases; a few minutes)
    python3 tests/build_check.py mage shaman  (only cases for those classes)
    python3 tests/build_check.py --record     (after an improvement: save the new, higher scores)

--record never lowers a recorded score. Every line should say PASS.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["WYNN_HELPER_NO_POPUP"] = "1"

import wynn_app as app          # noqa: E402
import wynn_helper as wh        # noqa: E402
import wynn_search as ws        # noqa: E402

BASELINE = os.path.join(HERE, "golden", "build_baseline.json")

# One case per class (each with a different goal), plus one with minimums and one that uses an ability buff.
CASES = [
    {"name": "warrior L50 melee", "class": "warrior", "level": 50, "goal": "melee"},
    {"name": "mage L40 spells", "class": "mage", "level": 40, "goal": "spells"},
    {"name": "archer L60 melee", "class": "archer", "level": 60, "goal": "melee"},
    {"name": "shaman L60 spells", "class": "shaman", "level": 60, "goal": "spells"},
    {"name": "assassin L50 balanced", "class": "assassin", "level": 50, "goal": "balanced"},
    {"name": "mage L45 survival, min 2500 health", "class": "mage", "level": 45, "goal": "survival",
     "minimums": {"health": 2500}},
    {"name": "assassin L55 spells, Aerial Ace 50%", "class": "assassin", "level": 55, "goal": "spells",
     "toggles": ["Aerial Ace=50%"]},
    {"name": "warrior L50 melee, no crafted items", "class": "warrior", "level": 50, "goal": "melee", "crafted": False},
    {"name": "shaman L70 spells 70% + survival 30%", "class": "shaman", "level": 70, "goal": "spells",
     "second_goal": "survival", "second_goal_weight": 30},
]

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<46} {detail}", flush=True)


def problems_with(case, res):
    """Everything wrong with the build the app returned (empty list = fine)."""
    out = []
    if not res.get("ok"):
        return ["the app didn't finish: " + res.get("text", "")[-300:].strip()]
    if not res.get("summary"):
        out.append("no results screen data")
    best = wh.LAST_RESULT.get("best")
    if not best:
        return out + ["no whole build in the result"]
    lvl, cls = case["level"], case["class"]
    armor, weapon, extra = [a for a in best["armor"] if a is not None], best["weapon"], best["extra"]
    for it in armor + [weapon]:
        if it.level > lvl:
            out.append(f"{it.name} needs level {it.level}")
    if weapon.type != wh.CLASS_WEAPON[cls]:
        out.append(f"weapon {weapon.name} is a {weapon.type}, not a {wh.CLASS_WEAPON[cls]}")
    assigned, _, _ = wh._solve_skill_points_uncached(armor, weapon)
    if sum(assigned) + sum(extra) > wh.skill_points_available(lvl):
        out.append(f"skill points over the limit ({sum(assigned)} for gear + {sum(extra)} spare "
                   f"> {wh.skill_points_available(lvl)})")
    if any(a + e > wh.MAX_ASSIGNED_PER_SKILL for a, e in zip(assigned, extra)) or min(extra) < 0:
        out.append(f"a skill is over {wh.MAX_ASSIGNED_PER_SKILL} or below 0 ({assigned} + {extra})")
    tree = wh.LAST_RESULT.get("tree")
    v = tree.validate(best["owned"], lvl)
    if not v["ok"]:
        out.append("ability tree isn't legal: " + "; ".join(map(str, v.get("errors", [])[:3])))
    elif v.get("ap", 0) > wh.ability_points(lvl):
        out.append(f"ability tree uses {v['ap']} points, only {wh.ability_points(lvl)} at this level")
    for it in armor + [weapon]:                  # crafted pieces: can be made in game, and the code reads back
        if not getattr(it, "crafted", False) or not getattr(it, "link_code", None):
            continue
        if case.get("crafted") is False:
            out.append(f"crafted {it.type} with crafted items turned off")
        if it.craft_problems or it.missing_durability is not None:
            out.append(f"crafted {it.type} can't be made: {it.craft_problems}")
        back = wh.crafted_from_code(wh.LAST_RESULT["build"].data, "CR-" + wh.craft_code(it))
        if getattr(it, "powders", None) and it.category == "armor":     # powders aren't part of a craft's code
            back = wh.armor_with_powders(back, it.powders)
        if back.max_stats != it.max_stats or back.reqs != it.reqs or back.bonus != it.bonus:
            out.append(f"crafted {it.type}'s code doesn't read back the same")
    fin = best["eval"]
    if case.get("minimums") and not fin.get("floors_met", True):
        out.append("misses the minimums: " + "; ".join(wh.floor_misses(wh.parse_floors(
            ", ".join(f"{k}={v}" for k, v in case["minimums"].items())), fin.get("floor_values", {}))))
    return out


def main():
    argv = sys.argv[1:]
    record = "--record" in argv
    one = None
    if "--case" in argv:                      # one case by its name (tests/run_all.py runs cases side by side)
        i = argv.index("--case")
        one = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    only = {a.lower() for a in argv if not a.startswith("--")}
    cases = [c for c in CASES if (one is None or c["name"] == one) and (not only or c["class"] in only)]
    if one is not None and not cases:
        sys.exit(f"No case named {one!r}.")
    base = {}
    if os.path.exists(BASELINE):
        with open(BASELINE, encoding="utf-8") as fh:
            base = json.load(fh)

    # Same answer on every computer: no remembered builds from earlier runs, nothing saved to history.
    ws.remembered_starts = lambda *a, **k: []
    ws.remember = lambda *a, **k: None
    app.save_history = lambda *a, **k: None
    app.STATE["data_checked"] = True          # use the game data in the repository

    t_all = time.time()
    for case in cases:
        params = {k: v for k, v in case.items() if k != "name"}
        params.update(deep_search=False, include_level_plan=False)
        t = time.time()
        res = app.run_report(params)
        secs = time.time() - t
        bad = problems_with(case, res)
        check(f"{case['name']}: legal build", not bad, f"({secs:.0f}s)" + (" " + "; ".join(bad) if bad else ""))
        best = wh.LAST_RESULT.get("best")
        if not best or not res.get("ok"):
            continue
        S = res.get("summary") or {}
        if S.get("best"):                     # the build as a share code opens as exactly the same build
            e = dict({k: S.get(k) for k in ("class", "class_title", "level", "goal", "label", "focus", "floors",
                                             "toggles_on", "toggle_share", "settings", "rolls")},
                     numbers=S["best"]["numbers"], items=S["best"]["items"], sp=S["best"]["sp"],
                     owned=S["best"]["owned"], order=S["best"]["order"], link=S.get("best_link"))
            code = app.share_code(e, "Friend")
            back, err = app.decode_share(code)
            same = back is not None and all(back[k] == e[k] for k in ("items", "owned", "order", "sp", "label")) \
                and not back["shared_from"]["missing"]
            check(f"{case['name']}: share code opens the same build", same, f"({len(code)} characters)" + (f" {err}" if err else ""))
        score = best["eval"]["main"]
        print(f"SCORE\t{case['name']}\t{score!r}", flush=True)
        was = base.get(case["name"])
        if was is None:
            check(f"{case['name']}: not worse than before", True, f"({score:,.1f}; no score recorded yet)")
        else:
            check(f"{case['name']}: not worse than before", score >= was * (1 - 1e-9),
                  f"({score:,.1f} vs {was:,.1f} recorded" + (", better: run with --record)" if score > was * (1 + 1e-9)
                                                              else ")"))
        if record and (was is None or score > was):
            base[case["name"]] = score

    if record:
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump(base, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"\nRecorded scores saved to {os.path.relpath(BASELINE, ROOT)}.")
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed in {time.time() - t_all:.0f}s.")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
