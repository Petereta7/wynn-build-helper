import os
import json, random, sys
sys.path.insert(0, os.environ.get("HELPER_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
import wynn_helper as wh
random.seed(3)
cases = []
for _ in range(1000):
    st = {"hp": random.randint(5, 20000), "hpBonus": random.randint(-3000, 8000), "def": random.randint(0, 160),
          "agi": random.randint(0, 160), "agiDef": random.choice([90, 80, 95]), "hprRaw": random.randint(-500, 1500),
          "hprPct": random.randint(-80, 200), "rDefPct": random.randint(-50, 80)}
    for e in "etwfa":
        st[e + "Def"] = random.randint(-200, 400); st[e + "DefPct"] = random.randint(-60, 120)
    wtype = random.choice(list(wh.CLASS_DEF_MULT))
    dm = {f"m{i}": random.randint(0, 40) for i in range(random.randint(0, 2))}
    class W: pass
    w = W(); w.type = wtype
    d = wh.defense_stats(st, w, dm)
    cases.append({"stats": st, "wtype": wtype, "dm": dm, "py": [d["hp"], d["ehp_agi"], d["ehp"], d["hpr"], d["elem_def"]]})
json.dump(cases, open("ehp_cases.json", "w")); print(len(cases))
