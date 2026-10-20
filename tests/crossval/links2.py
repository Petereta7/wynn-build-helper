import os
# Builds with crafted and custom items: Python encodes, Wynnbuilder encodes the same build, links must match.
import json, random, sys
sys.path.insert(0, os.environ.get("HELPER_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
import wynn_helper as wh
random.seed(21)
d = wh.load_game_data(offline=True, data_dir=os.environ.get("WB_DATA_ROOT", wh.DATA_DIR))
enc = json.load(open("encoding_consts.json"))
customs = json.load(open("custom_cases.json"))
by_type = {}
for it in d.items.values():
    by_type.setdefault(it.type, []).append(it)
rec_by_type = {}
for r in d.recipes.values():
    rec_by_type.setdefault(r["type"].lower(), []).append(r)
ings = [i for i in d.ingredients.values() if i["id"] < 4000]

def rand_craft(t):
    r = random.choice(rec_by_type[t])
    pool = [i for i in ings if r["skill"] in i.get("skills", [])]
    ids = [random.choice(pool)["id"] if random.random() < 0.7 else 4000 for _ in range(6)]
    spec = {"recipe": r["name"], "mats": [random.randint(1, 3), random.randint(1, 3)], "ings": ids,
            "atk": random.choice(wh.CRAFT_ATK_SPDS)}
    return wh.make_crafted(d, int(r["id"]), spec["mats"], ids, spec["atk"]), {"craft": spec}

def rand_custom(t):
    cands = [c for c in customs if c["spec"]["type"] == t]
    if not cands:
        return None, None
    c = random.choice(cands)
    return wh.custom_from_code("CI-" + c["hash"]), {"custom": c["hash"]}

cases, rt = [], 0
for n in range(200):
    cls = random.choice(wh.CLASSES)
    tree = wh.Tree(d, cls)
    armor, spec = [], []
    for s, t in enumerate(wh.SLOT_TYPES):
        x = random.random()
        if x < 0.3:
            it, sp = rand_craft(t)
        elif x < 0.45:
            it, sp = rand_custom(t)
            if it is None:
                it = random.choice(by_type[t]); sp = {"id": int(it.raw["id"])}
        elif x < 0.9:
            it = random.choice(by_type[t]); sp = {"id": int(it.raw["id"])}
        else:
            it, sp = None, {"id": None}
        if it is not None and s < 4 and it.slots and random.random() < 0.6:
            it = wh.armor_with_powders(it, [wh.Powder(random.randrange(5), random.randint(1, 7)) for _ in range(random.randint(1, it.slots))])
        armor.append(it); spec.append(sp)
    wt = wh.CLASS_WEAPON[cls]
    if random.random() < 0.5:
        w, sp = rand_craft(wt)
    else:
        w = random.choice(by_type[wt]); sp = {"id": int(w.raw["id"])}
    pw = [wh.Powder(random.randrange(5), random.randint(1, 7)) for _ in range(random.randint(0, w.slots))] if w.slots else []
    weapon = wh.weapon_with_powders(w, pw)
    spec.append(sp)
    root, children = wh._tree_children(tree)
    owned = {root}
    level = random.randint(1, 121)
    auto = [random.randint(0, 100) for _ in range(5)]
    link = wh.encode_build_link(d, tree, enc, 35, level, armor, weapon, [], [], owned, auto, auto)
    dec = wh.decode_build_link(d, enc, link)
    same = all((a is None and b is None) or (a is not None and b is not None and a.name == b.name and a.reqs == b.reqs
               and a.bonus == b.bonus) for a, b in zip(list(armor) + [weapon], dec["armor"] + [dec["weapon"]]))
    rt += same and not dec["crafted_slots"]
    cases.append({"cls": cls, "link": link, "equipment": spec, "level": level, "auto": auto, "root": root,
                  "children": {str(k): v for k, v in children.items()},
                  "powders": [[p.elem * 7 + p.tier - 1 for p in (getattr(armor[i], "powders", []) if armor[i] is not None else [])] for i in range(4)]
                             + [[p.elem * 7 + p.tier - 1 for p in weapon.powders]]})
json.dump(cases, open("link2_cases.json", "w"))
print(len(cases), "builds; python decode round trip ok:", rt)
