import os
import json, random, sys
sys.path.insert(0, os.environ.get("HELPER_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
import wynn_helper as wh
random.seed(9)
d = wh.load_game_data(offline=True, data_dir=os.environ.get("WB_DATA_ROOT", wh.DATA_DIR))
enc = json.load(open("encoding_consts.json"))
by_type = {}
for it in d.items.values():
    by_type.setdefault(it.type, []).append(it)
cases = []
for n in range(300):
    cls = random.choice(wh.CLASSES)
    tree = wh.Tree(d, cls)
    armor = []
    for s, t in enumerate(wh.SLOT_TYPES):
        it = random.choice(by_type[t]) if random.random() < 0.8 else None
        if it is not None and s < 4 and it.slots and random.random() < 0.7:
            it = wh.armor_with_powders(it, [wh.Powder(random.randrange(5), random.randint(1, 7)) for _ in range(random.randint(1, it.slots))])
        armor.append(it)
    w = random.choice(by_type[wh.CLASS_WEAPON[cls]])
    pw = [wh.Powder(random.randrange(5), random.randint(1, 7)) for _ in range(random.randint(0, w.slots))] if w.slots else []
    weapon = wh.weapon_with_powders(w, pw)
    tomes = random.sample(d.tomes, random.randint(0, 4))
    # keep at most the slots available per type
    kept, cnt = [], {}
    for t in tomes:
        cap = wh.TOME_SLOT_TYPES.count(t.type)
        if cnt.get(t.type, 0) < cap:
            kept.append(t); cnt[t.type] = cnt.get(t.type, 0) + 1
    asp_pool = d.aspects[wh.CLASS_TITLE[cls]]
    aspects = [(a, random.randint(1, len(a["tiers"]))) for a in random.sample(asp_pool, random.randint(0, 5))]
    root, children = wh._tree_children(tree)
    owned = {root}
    frontier = list(children[root])
    while frontier and random.random() < 0.97:
        c = frontier.pop(random.randrange(len(frontier)))
        if c not in owned and random.random() < 0.6:
            owned.add(c); frontier.extend(children[c])
    level = random.choice([random.randint(1, 120), 121])
    auto = [random.randint(0, 120) for _ in range(5)]
    final = [a + (random.randint(-5, 30) if random.random() < 0.4 else 0) for a in auto]
    link = wh.encode_build_link(d, tree, enc, 35, level, armor, weapon, kept, aspects, owned, auto, final)
    # tree order for JS: list of nodes; children per node in Wynnbuilder order
    cases.append({
        "cls": cls, "link": link,
        "equipment": [None if a is None else int(a.raw["id"]) for a in armor] + [int(weapon.raw["id"])],
        "powders": [[p.elem * 7 + p.tier - 1 for p in (getattr(armor[i], "powders", []) if armor[i] is not None else [])] for i in range(4)]
                   + [[p.elem * 7 + p.tier - 1 for p in weapon.powders]],
        "tomes": [None] * 14, "level": level, "auto": auto, "final": final,
        "aspects": [[int(a["id"]), t] for a, t in aspects],
        "root": root, "children": {str(k): v for k, v in children.items()}, "owned": sorted(owned),
    })
    slots = [None] * 14
    for t in kept:
        for i, tt in enumerate(wh.TOME_SLOT_TYPES):
            if tt == t.type and slots[i] is None:
                slots[i] = int(t.raw["id"]); break
    cases[-1]["tomes"] = slots
    # round trip through the Python decoder
    dec = wh.decode_build_link(d, enc, link)
    ok = ([None if a is None else int(a.raw["id"]) for a in dec["armor"]] == cases[-1]["equipment"][:8]
          and int(dec["weapon"].raw["id"]) == cases[-1]["equipment"][8]
          and dec["level"] == level and sorted(dec["tree"]) == sorted(owned)
          and [[int(a["id"]), t] for a, t in dec["aspects"]] == cases[-1]["aspects"]
          and sorted(int(t.raw["id"]) for t in dec["tomes"]) == sorted(int(t.raw["id"]) for t in kept))
    pw_ok = all(sorted(p.elem * 7 + p.tier - 1 for p in dec["powders"].get(i, [])) == sorted(cases[-1]["powders"][wh.POWDERABLE_INDEX[i]]) for i in wh.POWDERABLE_INDEX)
    sp_ok = (dec["sp"] is None and auto == final) or (dec["sp"] is not None and all((s is None and a == f) or s == f for s, a, f in zip(dec["sp"], auto, final)))
    cases[-1]["roundtrip"] = ok and pw_ok and sp_ok
json.dump(cases, open("link_cases.json", "w"))
print(len(cases), "cases; python round trip ok:", sum(c["roundtrip"] for c in cases))
