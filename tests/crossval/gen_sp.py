import os
import json, random, sys
sys.path.insert(0, os.environ.get("HELPER_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
import wynn_helper as wh
random.seed(2026)
d = wh.load_game_data(offline=True, data_dir=os.environ.get("WB_DATA_ROOT", wh.DATA_DIR))
by_type = {}
for it in d.items.values():
    by_type.setdefault(it.type, []).append(it)
neg = [it for it in d.items.values() if any(b < 0 for b in it.bonus) and it.category != "weapon"]
cases = []
for _ in range(1500):
    lvl = random.randint(20, 106)
    armor = []
    for t in wh.SLOT_TYPES:
        pool = [i for i in by_type[t] if abs(i.level - lvl) <= 15] or by_type[t]
        if random.random() < 0.85:
            armor.append(random.choice(pool))
    if neg and random.random() < 0.5:   # make sure negative-bonus items get exercised
        armor[random.randrange(len(armor))] = random.choice(neg)
    if random.random() < 0.4:            # crafted pieces: their skill point bonus only counts at the end
        for _k in range(random.randint(1, 2)):
            idx = random.randrange(len(armor))
            rtype = armor[idx].type
            recs = [r for r in d.recipes.values() if r["type"].lower() == rtype]
            r = random.choice(recs)
            pool = [g for g in d.ingredients.values() if g["id"] < 4000 and r["skill"] in g.get("skills", [])]
            sp_pool = [g for g in pool if any(k in g.get("ids", {}) for k in wh.SKILLS)] or pool
            ids = [random.choice(sp_pool if random.random() < 0.6 else pool)["id"] for _ in range(6)]
            armor[idx] = wh.make_crafted(d, int(r["id"]), [random.randint(1, 3), random.randint(1, 3)], ids)
    wpool = [i for i in d.items.values() if i.category == "weapon" and abs(i.level - lvl) <= 15]
    weapon = random.choice(wpool)
    armor = wh._wynn_sorted(armor)       # Wynnbuilder's page (and the game) go through gear boots-first
    assigned, final, order = wh.solve_skill_points(armor, weapon)
    cases.append({"armor": [{"reqs": a.reqs, "bonus": a.bonus, "crafted": a.crafted} for a in armor],
                  "weapon": {"reqs": weapon.reqs, "bonus": weapon.bonus},
                  "py_total": sum(assigned), "py_under": all(x <= 100 for x in assigned), "py_final": final})
json.dump(cases, open("sp_cases.json", "w"))
print(len(cases), "cases,", sum(1 for c in cases if any(b < 0 for a in c["armor"] for b in a["bonus"])), "with negative bonuses,",
      sum(1 for c in cases if any(a["crafted"] for a in c["armor"])), "with crafted items")
