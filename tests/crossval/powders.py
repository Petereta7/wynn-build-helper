import os
import json, random, sys
sys.path.insert(0, os.environ.get("HELPER_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
import wynn_helper as wh
random.seed(5)
d = wh.load_game_data(offline=True, data_dir=os.environ.get("WB_DATA_ROOT", wh.DATA_DIR))
weps = [it for it in d.items.values() if it.category == "weapon" and it.slots > 0]
cases = []
for _ in range(500):
    w = random.choice(weps)
    n = random.randint(0, w.slots)
    pw = [wh.Powder(random.randrange(5), random.randint(1, 7)) for _ in range(n)]
    pw_w = wh.weapon_with_powders(w, pw)
    cases.append({"name": w.raw["name"], "powders": [p.elem * 7 + p.tier - 1 for p in pw],
                  "py": [list(x) for x in pw_w.damage]})
json.dump(cases, open("powder_cases.json", "w")); print(len(cases), "cases")
# parser checks
for s in ["t6 t6", "T5 thunder x2", "e4,e4", "weapon water 6", "a7 f3"]:
    print(repr(s), "->", wh.parse_powders(s))
print(wh.parse_powder_arg("weapon=t6 t6; chest=w4 w4; legs=fire 5"))
