"""
Build search for Wynn Build Helper: safe item filtering, normal (fast) search and guaranteed search.

Safe filtering removes only items that provably can't be part of the best build:
  * items you can't wear even in the best case, and
  * items that another item for the same slot beats in every way that matters for the goal.
"Matters" is decided from what the calculator actually reads for each goal (see STAT GROUPS below).
Stats that ability-tree effects, aspects or major IDs read are compared for equality only, since
their effect could go either way.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import time

import wynn_helper as wh

# ============================================================================== stat groups
# Every stat the calculator reads (checked against wynn_helper.py), grouped by what it feeds.
ELEMS = ["n", "e", "t", "w", "f", "a"]
GENERAL_DAMAGE = {"damPct", "damRaw", "rDamPct", "rDamRaw", "critDamPct", "damAddMin", "damAddMax",
                  "rDamAddMin", "rDamAddMax"} | {p + s for p in ELEMS for s in ("DamPct", "DamRaw", "DamAddMin", "DamAddMax")}
SPELL_DAMAGE = {"sdPct", "sdRaw", "rSdPct", "rSdRaw"} | {p + s for p in ELEMS for s in ("SdPct", "SdRaw")}
MELEE_DAMAGE = {"mdPct", "mdRaw", "rMdPct", "rMdRaw"} | {p + s for p in ELEMS for s in ("MdPct", "MdRaw")}
MANA = {"mr", "ms"} | {f"sp{k}{i}" for k in ("Raw", "Pct") for i in range(1, 5)}
DEFENSE = {"hp", "hpBonus", "hprRaw", "hprPct", "rDefPct", "agiDef"} | {e + s for e in "etwfa" for s in ("Def", "DefPct")}
HEALING = {"healPct"}
# Stats nothing in the calculator reads.
NEVER_READ = {"xpb", "lb", "lq", "gXp", "gSpd", "spd", "sprint", "sprintReg", "jh", "eSteal", "spRegen", "ref",
              "thorns", "expd", "kb", "weakenEnemy", "slowEnemy", "maxMana", "mainAttackRange",
              "spPct1Final", "spPct2Final", "spPct3Final", "spPct4Final"}
LOWER_IS_BETTER = {f"sp{k}{i}" for k in ("Raw", "Pct") for i in range(1, 5)}


def goal_stats(goal: str):
    """(stats where more is better for this goal, stats that must be equal). Unknown stats count as relevant."""
    dmg = GENERAL_DAMAGE | SPELL_DAMAGE | MELEE_DAMAGE
    if goal == "melee":          # Wynnbuilder's Average DPS: main attack only
        rel, eq = GENERAL_DAMAGE | MELEE_DAMAGE | {"atkTier"}, set()
    elif goal == "burst":
        rel, eq = dmg, set()
    elif goal == "spells":
        rel, eq = dmg | MANA | {"poison"}, set()
    elif goal == "survival":
        rel, eq = DEFENSE | HEALING | MANA | {"ls"}, set()
    elif goal == "ability":       # one ability's damage; attack speed tiers kept equal to be safe
        rel, eq = dmg, {"atkTier"}
    elif goal == "healing":
        rel, eq = HEALING | MANA | {"hp", "hpBonus"}, set()
    else:                        # balanced: everything; faster attacks can lower poison, so atkTier must match
        rel, eq = dmg | MANA | {"poison"} | DEFENSE | HEALING | {"ls"}, {"atkTier"}
    return rel, eq


# Stats that can raise each minimum ("floor"), and stats that could push it either way (kept equal).
FLOOR_REL = {
    "hp": ({"hp", "hpBonus"}, set()),
    "ehp": ({"hp", "hpBonus", "agiDef"}, set()),
    "spd": ({"spd"}, set()),
    "mr": ({"mr"}, set()),
    "hpr": ({"hprRaw", "hprPct"}, set()),
}
for _e in "etwfa":
    FLOOR_REL[_e + "Def"] = ({_e + "Def"}, {_e + "DefPct", "rDefPct"})


def floor_stats(build):
    """(more-is-better stats, must-be-equal stats) the build's minimums read."""
    rel, eq = set(), set()
    floors = getattr(build, "floors", None) or {}
    for k in floors:
        r, e = FLOOR_REL.get(k, (set(), set()))
        rel |= r
        eq |= e
    if "ehp" in floors and build.enemy:          # against an entered hit, elemental defenses count too
        for e in "etwfa":
            if (" etwfa".index(e)) in build.enemy:
                rel.add(e + "Def")
                eq |= {e + "DefPct", "rDefPct"}
    return rel, eq


def better(v2, v, factor):
    """v2 beats v by the given factor (for positive scores exactly v2 > v * factor; also sensible for the
    negative scores of builds below a minimum)."""
    return v2 > (v * factor if v >= 0 else v * (2 - factor))


def _scalings(obj, out: list):
    """Every stat scaling (inputs, outputs, coefficients) in a JSON tree."""
    if isinstance(obj, dict):
        ins = obj.get("inputs")
        if isinstance(ins, list) and any(isinstance(i, dict) and i.get("type") == "stat" for i in ins):
            outs = obj.get("output")
            outs = outs if isinstance(outs, list) else [outs]
            coefs = obj.get("scaling") or []
            out.append(([i.get("name") if isinstance(i, dict) and i.get("type") == "stat" else None for i in ins],
                        [o.get("name") for o in outs if isinstance(o, dict)], coefs))
        for v in obj.values():
            _scalings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _scalings(v, out)


_SCALING_CACHE = {}


def class_scalings(data, cls: str) -> list:
    if cls not in _SCALING_CACHE:
        out = []
        _scalings(data.atree.get(wh.CLASS_TITLE[cls], []), out)
        _scalings(data.aspects.get(wh.CLASS_TITLE[cls], []), out)
        _scalings(data.majids, out)
        _SCALING_CACHE[cls] = out
    return _SCALING_CACHE[cls]


def _output_good(name: str, rel: set) -> str:
    """'good' if more of this output helps the goal, 'bad' if it hurts, 'none' if the goal ignores it."""
    base = name.split(".")[0]
    if base in ("damMult", "defMult", "healMult"):
        return "good"
    if base in LOWER_IS_BETTER:
        return "bad" if base in rel else "none"
    return "good" if base in rel else ("none" if base in NEVER_READ or base in _ALL_GROUPED else "unknown")


def adjust_for_scalings(data, cls: str, rel: set, eq: set):
    """
    Stats that abilities read: if every ability that reads a stat turns more of it into something
    good (or harmless) for the goal, more is still better; otherwise it must be equal to compare.
    """
    rel, eq = set(rel), set(eq)
    verdict = {}
    for ins, outs, coefs in class_scalings(data, cls):
        for j, stat in enumerate(ins):
            if stat is None:
                continue
            c = coefs[j] if j < len(coefs) and isinstance(coefs[j], (int, float)) else None
            for o in outs:
                g = _output_good(o, rel)
                if c is None or g == "unknown":
                    v = "eq"
                elif g == "none":
                    v = "none"
                elif o == stat and c < 0 and abs(c) < 1:
                    v = "good"          # e.g. lose 75% of the health you add: still more health overall
                elif (g == "good") == (c > 0):
                    v = "good"
                else:
                    v = "eq"
                prev = verdict.get(stat)
                order = {"none": 0, "good": 1, "eq": 2}
                if prev is None or order[v] > order[prev]:
                    verdict[stat] = v
    for stat, v in verdict.items():
        if v == "eq":
            eq.add(stat)
            rel.discard(stat)
        elif v == "good" and stat not in rel:
            rel.add(stat)
    return rel - eq, eq


# ============================================================================== candidate pool

def slot_kinds(cls: str):
    return [(s, wh.SLOT_TYPES[s]) for s in range(8)] + [("weapon", wh.CLASS_WEAPON[cls])]


def item_pool(build, kind: str, include_never: bool = True, min_level: int = 1):
    cls, level = build.tree.cls, build.level
    return [it for it in build.data.items.values()
            if it.type == kind and min_level <= it.level <= level and it.class_req in (None, cls)
            and (include_never or it.drop != "never")]


def max_possible_bonus(pools: dict, exclude_slot=None):
    """Most skill points other slots' items could add, per skill (for the 'can't wear' test)."""
    total = [0] * 5
    for slot, items in pools.items():
        if slot == exclude_slot:
            continue
        best = [max([0] + [it.bonus[k] for it in items]) for k in range(5)]
        mult = 1
        total = [total[k] + best[k] * mult for k in range(5)]
    return total


def wearable_at_all(it, level: int, other_bonus) -> bool:
    avail = wh.skill_points_available(level)
    need = 0
    for k in range(5):
        r = it.reqs[k]
        if r <= 0:
            continue
        assign = max(0, r - other_bonus[k])
        if assign > wh.MAX_ASSIGNED_PER_SKILL:
            return False
        need += assign
    return need <= avail


def _special(it) -> bool:
    return bool(it.set) or bool(it.major_ids) or it.crafted or it.custom


def dominates(a, b, rel: set, eq: set, is_weapon: bool) -> bool:
    """True if `a` is at least as good as `b` in every way the goal can see (so `b` can never be needed)."""
    if a is b or _special(a) or _special(b):
        return False
    for k in range(5):
        if max(0, a.reqs[k]) > max(0, b.reqs[k]) or a.bonus[k] < b.bonus[k]:
            return False
    if a.slots < b.slots:
        return False
    sa, sb = a.stats, b.stats
    for key in set(sa) | set(sb):
        if key in wh.SKILLS or (key in NEVER_READ and key not in rel and key not in eq):
            continue
        va, vb = sa.get(key, 0.0), sb.get(key, 0.0)
        if key in eq or (key not in rel and key not in _ALL_GROUPED):
            if va != vb:              # unknown or two-sided stat: must be identical
                return False
        elif key in rel:
            if key in LOWER_IS_BETTER:
                if va > vb:
                    return False
            elif va < vb:
                return False
    if is_weapon:
        if a.atk_spd != b.atk_spd:
            return False
        # Equal damage spread only: raw damage is shared between elements in proportion to the base damage,
        # so a different spread could favour another element.
        if [tuple(x) for x in a.damage] != [tuple(x) for x in b.damage]:
            ra = [x[1] for x in a.damage]
            rb = [x[1] for x in b.damage]
            if not all(x >= y for x, y in zip(ra, rb)):
                return False
            sa_ = sum(ra) or 1
            sb_ = sum(rb) or 1
            if any(abs(x / sa_ - y / sb_) > 1e-9 for x, y in zip(ra, rb)):
                return False
            if not all(x[0] >= y[0] for x, y in zip(a.damage, b.damage)):
                return False
    return True


_ALL_GROUPED = GENERAL_DAMAGE | SPELL_DAMAGE | MELEE_DAMAGE | MANA | DEFENSE | HEALING | {"poison", "ls", "atkTier"}


def safe_filter(build, include_never: bool = True, min_level: int = 1, keep=()):
    """
    Per slot: all usable items, minus items that can't be worn and items beaten in every way.
    Returns ({slot: [items]}, stats dict). `keep` items are never removed (e.g. what you wear now).
    """
    cls, goal = build.tree.cls, build.goal
    rel, eq = goal_stats(goal)
    f_rel, f_eq = floor_stats(build)
    eq = (set(eq) | f_eq)
    rel = (set(rel) | f_rel) - eq
    rel, eq = adjust_for_scalings(build.data, cls, rel, eq)
    pools = {}
    for slot, kind in slot_kinds(cls):
        if slot == 5:
            continue
        pools[slot] = item_pool(build, kind, include_never, min_level)
    stats = {"before": {k: len(v) for k, v in pools.items()}}
    keep_names = {it.name for it in keep if it is not None}
    # 1. can't be worn even in the best case
    worn = {}
    for slot, items in pools.items():
        other = max_possible_bonus(pools, exclude_slot=slot)
        if slot == 4:   # the other ring slot can hold the best ring too
            best_ring = [max([0] + [it.bonus[k] for it in items]) for k in range(5)]
            other = [other[k] + best_ring[k] for k in range(5)]
        worn[slot] = [it for it in items if it.name in keep_names or wearable_at_all(it, build.level, other)]
    # 2. beaten in every way
    out = {}
    for slot, items in worn.items():
        is_w = slot == "weapon"
        kept = []
        for b in items:
            if b.name in keep_names or not any(dominates(a, b, rel, eq, is_w) for a in items):
                kept.append(b)
        # identical items (same stats) keep one
        seen, uniq = set(), []
        for it in kept:
            sig = (tuple(it.reqs), tuple(it.bonus), it.slots, tuple(sorted(it.stats.items())),
                   tuple(map(tuple, it.damage)) if it.damage else None, it.atk_spd, it.set,
                   tuple(it.major_ids or ()))
            if sig in seen and it.name not in keep_names:
                continue
            seen.add(sig)
            uniq.append(it)
        out[slot] = uniq
    out[5] = out[4]
    stats["after"] = {k: len(v) for k, v in out.items() if k != 5}
    stats["unwearable"] = {k: len(pools[k]) - len(worn[k]) for k in pools}
    return out, stats


# ============================================================================== skill points

def refine_points(build, owned, armor, weapon, extra, fast=True):
    """
    Better free skill point split than the 2-at-a-time greedy fill: tries putting everything into each
    skill, then moves points between pairs of skills in shrinking steps until nothing improves.
    """
    assigned, _, _ = wh.solve_skill_points(armor, weapon)
    free = wh.skill_points_available(build.level) - sum(assigned)
    if free <= 0:
        return [0] * 5, free
    cap = [max(0, wh.MAX_ASSIGNED_PER_SKILL - assigned[k]) for k in range(5)]

    def val(e):
        return build.evaluate(owned, armor, weapon, e, fast=fast)["value"]

    def clamp_fill(e):
        e = [min(e[k], cap[k]) for k in range(5)]
        left = free - sum(e)
        for k in sorted(range(5), key=lambda k: -e[k]):
            add = min(left, cap[k] - e[k])
            e[k] += add
            left -= add
        return e

    starts = [list(extra) if sum(extra) else [0] * 5]
    for k in range(5):
        e = [0] * 5
        e[k] = free
        starts.append(clamp_fill(e))
    best_e, best_v = None, -1e300
    for e in starts:
        e = [max(0, x) for x in e]
        if sum(e) > free:
            e = wh._fit_extra(e, armor, weapon, build.level)
        v = val(e)
        step = max(1, free // 2)
        while step >= 1:
            improved = True
            while improved:
                improved = False
                for i in range(5):
                    for j in range(5):
                        if i == j or e[i] < step or e[j] + step > cap[j]:
                            continue
                        t = e[:]
                        t[i] -= step
                        t[j] += step
                        tv = val(t)
                        if tv > v + 1e-9:
                            e, v, improved = t, tv, True
                # spend any points left over
                left = free - sum(e)
                if left > 0:
                    for k in range(5):
                        if e[k] + left <= cap[k]:
                            t = e[:]
                            t[k] += left
                            tv = val(t)
                            if tv > v + 1e-9:
                                e, v, improved = t, tv, True
                                break
            step //= 2
        if v > best_v:
            best_e, best_v = e, v
    # Exhaustive check over the skills that matter: every split for two skills, a grid then local moves
    # for three. Makes the answer independent of where the search started.
    e = best_e
    useful = [k for k in range(5) if e[k] > 0]
    for k in range(5):
        if k in useful or cap[k] <= 0:
            continue
        donor = max(range(5), key=lambda j: e[j])
        if e[donor] <= 0:
            continue
        t = e[:]
        t[donor] -= 1
        t[k] += 1
        if val(t) > best_v - 1e-9:
            useful.append(k)
    useful = sorted(useful, key=lambda k: -e[k])[:3]
    tot = sum(e[k] for k in useful)
    if len(useful) == 2:
        i, j = useful
        for x in range(max(0, tot - cap[j]), min(tot, cap[i]) + 1):
            t = e[:]
            t[i], t[j] = x, tot - x
            tv = val(t)
            if tv > best_v + 1e-9:
                best_e, best_v = t, tv
    elif len(useful) == 3:
        i, j, k = useful
        g = max(1, tot // 16)
        for x in range(0, min(tot, cap[i]) + 1, g):
            for y in range(0, min(tot - x, cap[j]) + 1, g):
                z = tot - x - y
                if z > cap[k]:
                    continue
                t = e[:]
                t[i], t[j], t[k] = x, y, z
                tv = val(t)
                if tv > best_v + 1e-9:
                    best_e, best_v = t, tv
        # local moves of every size down to 1 around the best grid point
        step = g
        e = best_e
        while step >= 1:
            improved = True
            while improved:
                improved = False
                for a_ in useful:
                    for b_ in useful:
                        if a_ == b_ or e[a_] < step or e[b_] + step > cap[b_]:
                            continue
                        t = e[:]
                        t[a_] -= step
                        t[b_] += step
                        tv = val(t)
                        if tv > best_v + 1e-9:
                            e, best_v, improved = t, tv, True
            step //= 2
        best_e = e
    return best_e, free - sum(best_e)


# ============================================================================== normal and deep search

# Thoroughness settings (the slider in the app, --effort on the command line).
EFFORT = {
    1: {"name": "Quick", "weapons": 3, "random_starts": 0, "short": 4, "triple": 0, "kicks": 0, "polish": 1},
    2: {"name": "Normal", "weapons": 6, "random_starts": 0, "short": 6, "triple": 0, "kicks": 0, "polish": 2,
        "extra_weapons": 12, "promote": 2, "depth": 1, "depth_kicks": 3, "extra_polish": 2},
    3: {"name": "Thorough", "weapons": 10, "random_starts": 3, "short": 7, "triple": 3, "kicks": 6, "polish": 3,
        "extra_weapons": 20, "promote": 3, "depth": 2, "depth_kicks": 4, "extra_polish": 3},
    4: {"name": "Very thorough", "weapons": 16, "random_starts": 6, "short": 8, "triple": 4, "kicks": 15, "polish": 4,
        "extra_weapons": 32, "promote": 4, "depth": 2, "depth_kicks": 6, "extra_polish": 3},
    5: {"name": "Maximum", "weapons": 30, "random_starts": 10, "short": 10, "triple": 5, "kicks": 40, "polish": 6,
        "extra_weapons": 60, "promote": 6, "depth": 3, "depth_kicks": 10, "extra_polish": 4},
}

STOP = {"flag": False}          # set STOP["flag"] = True to stop a running search (keeps the best so far)
PROGRESS = {"cb": None}         # PROGRESS["cb"] = function(fraction, message) to receive progress


_PHASE = [0.0, 1.0]             # progress range of the current part of the search

# ---- live view for the app ("The Forge"): the best build so far, its score, how many builds were scored,
# and the weapons being built around. Worker processes send theirs through a queue.
LIVE = {}
PHASES = ["Throwing out items that can't win", "Ranking weapons", "Building around each weapon",
          "Fine-tuning the best builds", "Searching some more"]
_HOOK = {"best": None}


GOAL_LABELS = {"melee": "Average DPS", "spells": "Spell damage per second", "burst": "Burst combo damage",
               "survival": "Survival score", "balanced": "Damage × survival score", "healing": "Healing per second"}


def live_reset(label=""):
    LIVE.clear()
    LIVE.update(id=f"{time.time():.3f}", phase=-1, label=label, evals0=wh.EVAL_STATS["n"], worker_evals=0,
                best=None, weapons=[], pools={}, improvements=0)


def live_pools_hint(build, include_never=True, min_level=1):
    """Item lists for the reels before the search starts (the search replaces them with the filtered lists)."""
    if LIVE.get("pools"):
        return
    try:
        labels = [wh.SLOT_LABELS[k] for k in range(8)] + ["Weapon"]
        out = {}
        for i, (slot, kind) in enumerate(slot_kinds(build.tree.cls)):
            items = sorted(item_pool(build, kind, include_never, min_level), key=lambda it: -it.level)[:80]
            out[labels[i]] = [[it.name, it.tier] for it in items]
        LIVE["pools"] = out
    except Exception:
        pass


def live_snapshot(full=False) -> dict:
    out = {k: v for k, v in LIVE.items() if k not in ("pools", "evals0", "worker_evals")}
    out["evals"] = (wh.EVAL_STATS["n"] - LIVE.get("evals0", 0) + LIVE.get("worker_evals", 0)) if LIVE else 0
    out["phases"] = PHASES
    if full:
        out["pools"] = LIVE.get("pools", {})
    return out


def _names(a, wpn):
    return [[it.name, it.tier] if it is not None else None for it in a], [wpn.name, wpn.tier]


def _live_offer(value, main, armor_names, weapon_name):
    b = LIVE.get("best")
    if b is None or value > b["value"]:
        LIVE["best"] = {"value": value, "main": main, "armor": armor_names, "weapon": weapon_name}
        LIVE["improvements"] = LIVE.get("improvements", 0) + 1


def _offer(build, own, a, wp, e, wpn):
    """Show a build that may beat the best so far (straight away here, or through the queue from a worker)."""
    try:
        r = build.evaluate(own, a, wp, wh._fit_extra(e, a, wp, build.level), fast=True)
        an, wn = _names(a, wpn)
        hook = _HOOK.get("best")
        if hook is not None:
            hook(r["value"], r["main"], an, wn)
        else:
            _live_offer(r["value"], r["main"], an, wn)
    except Exception:
        pass


def _card(name, **kw):
    for c in LIVE.get("weapons", []):
        if c["name"] == name and c.get("status") != "done":
            c.update(kw)
            return


def _drain(q):
    import queue as _q
    try:
        while True:
            m = q.get_nowait()
            if m[0] == "b":
                _live_offer(*m[1:])
            elif m[0] == "e":
                LIVE["worker_evals"] = LIVE.get("worker_evals", 0) + m[1]
    except (_q.Empty, EOFError, OSError):
        pass


def _report(frac, msg):
    cb = PROGRESS.get("cb")
    if cb:
        try:
            cb(_PHASE[0] + (_PHASE[1] - _PHASE[0]) * frac, msg)
        except Exception:
            pass


def _scaled_extra(extra, armor, weapon, level):
    """Keep the same split of free points when gear changes how many are free."""
    assigned, _, _ = wh.solve_skill_points(armor, weapon)
    free = wh.skill_points_available(level) - sum(assigned)
    if free <= 0:
        return [0] * 5
    tot = sum(extra)
    if tot <= 0:
        return [0] * 5
    e = [int(free * x / tot) for x in extra]
    e[max(range(5), key=lambda k: extra[k])] += free - sum(e)
    return [max(0, min(e[k], wh.MAX_ASSIGNED_PER_SKILL - assigned[k])) for k in range(5)]


# ============================================================================== memory between scans
# The best builds each search finds are saved with the exact settings that produced them. The next search
# with the same settings starts from them (so it can only match or beat them), and a search one level up
# also starts from the previous level's best.

MEMORY_FILE = os.path.join(wh.DATA_DIR, "best_builds.json")


def _settings(build, include_never, min_level):
    return {"version": build.data.version, "class": build.tree.cls, "goal": build.goal,
            "tomes": sorted(t.name for t in build.tomes),
            "aspects": sorted(f"{a['displayName']}:{t}" for a, t in build.aspects),
            "toggles": sorted(build.toggles), "sliders": sorted(build.sliders.items()), "slider_mode": build.slider_mode,
            "buffs": sorted(build.buffs), "raid_buffs": sorted(build.raid_buffs),
            "enemy": sorted(build.enemy.items()), "rolls": wh.ROLL_MODE, "poison": wh.POISON_MODE,
            "drops_only": not include_never, "min_level": min_level, "focus": build.focus,
            **({"floors": sorted(build.floors.items())} if getattr(build, "floors", None) else {})}


def _key(settings: dict, level: int) -> str:
    return hashlib.sha1(json.dumps([settings, level], sort_keys=True, default=str).encode()).hexdigest()[:20]


def _load_memory() -> dict:
    try:
        with open(MEMORY_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def remembered_starts(build, include_never, min_level):
    """[(armor list, base weapon)] saved for these settings, plus the best from a lower level."""
    mem = _load_memory()
    st = _settings(build, include_never, min_level)
    found = list(mem.get(_key(st, build.level), {}).get("builds", []))
    family = _key(st, -1)
    lower = [v for v in mem.values() if v.get("family") == family and v.get("level", 999) < build.level]
    if lower:
        found += max(lower, key=lambda v: v["level"]).get("builds", [])[:1]
    by_name = {it.name: it for it in build.data.items.values()}
    out = []
    for b in found:
        arm = [by_name.get(n) if n else None for n in b.get("armor", [None] * 8)]
        wpn = by_name.get(b.get("weapon"))
        if wpn is None or any(n and by_name.get(n) is None for n in b.get("armor", [])):
            continue
        if wpn.level > build.level or any(a is not None and a.level > build.level for a in arm):
            continue
        out.append((arm, wpn))
    return out


def remember(build, include_never, min_level, finals):
    try:
        mem = _load_memory()
        st = _settings(build, include_never, min_level)
        builds = []
        for v, a, wp, wpn, own, e, free, fin in finals[:3]:
            if wpn.crafted or wpn.custom or any(x is not None and (x.crafted or x.custom) for x in a):
                continue
            builds.append({"armor": [x.name if x else None for x in a], "weapon": wpn.name,
                           "value": v, "extra": list(e)})
        mem[_key(st, build.level)] = {"family": _key(st, -1), "level": build.level, "builds": builds,
                                       "saved": time.strftime("%Y-%m-%d %H:%M")}
        if len(mem) > 500:                      # keep the file small: drop the oldest entries
            for k in sorted(mem, key=lambda k: mem[k].get("saved", ""))[: len(mem) - 500]:
                mem.pop(k, None)
        with open(MEMORY_FILE, "w", encoding="utf-8") as fh:
            json.dump(mem, fh)
    except Exception:
        pass


def _local_search(build, cur_own, pools, wpn, wp, starts, base_extra, P, out_of_time):
    """Improve each starting gear set around one weapon; returns [(value, armor, weapon, powdered, points)]."""
    level = build.level

    def score(a, w, e):
        return build.evaluate(cur_own, a, w, wh._fit_extra(e, a, w, level), fast=True)["value"]

    def put(a, slot, it):
        a2 = list(a)
        old = a[slot]
        a2[slot] = wh.armor_with_powders(it, old.powders) if (old is not None and old.powders and slot < 4) else it
        return a2

    out = []
    for a in starts:
        if out_of_time():
            break
        if not wh._wearable(a, wp, level):
            a = [None] * 8
        e, _ = refine_points(build, cur_own, a, wp, base_extra)
        v = score(a, wp, e)
        _offer(build, cur_own, a, wp, e, wpn)
        for _round in range(P.get("rounds", 6)):
            improved = False
            for slot in range(8):
                best = None
                for it in pools[slot]:
                    if a[slot] is not None and a[slot].name == it.name:
                        continue
                    a2 = put(a, slot, it)
                    if not wh._wearable(a2, wp, level):
                        continue
                    v2 = score(a2, wp, _scaled_extra(e, a2, wp, level))
                    if better(v2, v, 1.000001) and (best is None or v2 > best[0]):
                        best = (v2, a2)
                if best:
                    v, a = best
                    e = _scaled_extra(e, a, wp, level)
                    improved = True
                    _offer(build, cur_own, a, wp, e, wpn)
            if P.get("singles_only"):          # quick pass: one-item swaps only
                if not improved or out_of_time():
                    break
                continue
            short = {}
            for slot in range(8):
                ranked = []
                for it in pools[slot]:
                    a2 = put(a, slot, it)
                    ok = wh._wearable(a2, wp, level)
                    ranked.append((score(a2, wp, _scaled_extra(e, a2, wp, level)) if ok else -1e300, it))
                ranked.sort(key=lambda x: -x[0])
                short[slot] = [it for _v, it in ranked[: P["short"]]]
                for it in sorted(pools[slot], key=lambda it: -sum(max(0, b) for b in it.bonus))[:3]:
                    if it not in short[slot]:
                        short[slot].append(it)       # big skill point givers can unlock other items
            best = None
            for s1, s2 in itertools.combinations(range(8), 2):
                for i1 in short[s1]:
                    a1 = put(a, s1, i1)
                    for i2 in short[s2]:
                        a2 = put(a1, s2, i2)
                        if not wh._wearable(a2, wp, level):
                            continue
                        v2 = score(a2, wp, _scaled_extra(e, a2, wp, level))
                        if better(v2, v, 1.00001) and (best is None or v2 > best[0]):
                            best = (v2, a2)
            if P["triple"] and not out_of_time():
                t3 = P["triple"]
                for s1, s2, s3 in itertools.combinations(range(8), 3):
                    for i1 in short[s1][:t3]:
                        a1 = put(a, s1, i1)
                        for i2 in short[s2][:t3]:
                            a12 = put(a1, s2, i2)
                            for i3 in short[s3][:t3]:
                                a3 = put(a12, s3, i3)
                                if not wh._wearable(a3, wp, level):
                                    continue
                                v3 = score(a3, wp, _scaled_extra(e, a3, wp, level))
                                if better(v3, v, 1.00001) and (best is None or v3 > best[0]):
                                    best = (v3, a3)
            if best:
                v, a = best
                improved = True
                _offer(build, cur_own, a, wp, _scaled_extra(e, a, wp, level), wpn)
            e2, _ = refine_points(build, cur_own, a, wp, e)
            v2 = score(a, wp, e2)
            if better(v2, v, 1.0001):
                e, v = e2, v2
                improved = True
                _offer(build, cur_own, a, wp, e, wpn)
            else:
                e = _scaled_extra(e, a, wp, level)
            if not improved or out_of_time():
                break
        out.append((v, list(a), wpn, wp, e))
    return out


def _starts_for(build, cur_own, pools, wpn, wp, armor0, base_extra, memory_arms, P):
    level = build.level
    starts = [list(armor0), [None] * 8] + [list(x) for x in memory_arms]
    if P.get("greedy_only"):             # quick pass: the greedy fill only
        starts = []
    a, e = [None] * 8, base_extra
    for slot in range(8):                           # greedy fill: best item per slot given what's chosen
        best = None
        for it in pools[slot]:
            a2 = list(a)
            a2[slot] = it
            if not wh._wearable(a2, wp, level):
                continue
            v = build.evaluate(cur_own, a2, wp, wh._fit_extra(_scaled_extra(e, a2, wp, level), a2, wp, level),
                               fast=True)["value"]
            if best is None or v > best[0]:
                best = (v, a2)
        if best:
            a = best[1]
    starts.append(a)
    if P["random_starts"]:
        import random
        rng = random.Random(sum(map(ord, wpn.name)) * 131 + level)
        for _ in range(P["random_starts"]):
            r = [rng.choice(pools[sl]) if pools[sl] and rng.random() < 0.8 else None for sl in range(8)]
            if wh._wearable(r, wp, level):
                starts.append(r)
    return starts


def _refined_sweep(build, own, a, wp, pools, e, fast=True, top=4, passes=3, wpn=None):
    """Final check: for each slot, re-score the closest contenders with their own best powders and the
    exact skill point split (two items can each want different powders)."""
    level = build.level
    wpn = wpn or wp

    def full(a2):
        pw, _ = wh.best_weapon_powders(build, own, a2, wpn, e)
        w2 = wh.weapon_with_powders(wpn, pw) if wpn.slots else wp
        e2, _ = refine_points(build, own, a2, w2, e)
        return build.evaluate(own, a2, w2, e2, fast=fast)["value"], e2, w2
    v, e, wp = full(a)
    for _ in range(passes):
        changed = False
        for slot in range(8):
            ranked = []
            for it in pools[slot]:
                a2 = list(a)
                old = a[slot]
                a2[slot] = wh.armor_with_powders(it, old.powders) if (old is not None and old.powders and slot < 4) else it
                if not wh._wearable(a2, wp, level):
                    continue
                ranked.append((build.evaluate(own, a2, wp, wh._fit_extra(_scaled_extra(e, a2, wp, level), a2, wp, level),
                                              fast=True)["value"], a2))
            ranked.sort(key=lambda x: -x[0])
            for _sv, a2 in ranked[:top]:
                v2, e2, w2 = full(a2)
                if better(v2, v, 1.000001):
                    v, a, e, wp, changed = v2, a2, e2, w2, True
        # The two ring slots as a pair: two of one ring can beat any single-ring swap.
        ranked = []
        for it in pools[4]:
            a2 = list(a)
            a2[4] = a2[5] = it
            if wh._wearable(a2, wp, level):
                ranked.append((build.evaluate(own, a2, wp, wh._fit_extra(_scaled_extra(e, a2, wp, level), a2, wp, level),
                                              fast=True)["value"], it))
        ranked.sort(key=lambda x: -x[0])
        rings = [it for _v, it in ranked[:top]] + [x for x in (a[4], a[5]) if x is not None]
        for r1, r2 in itertools.combinations_with_replacement(list({r.name: r for r in rings}.values()), 2):
            a2 = list(a)
            a2[4], a2[5] = r1, r2
            if not wh._wearable(a2, wp, level):
                continue
            v2, e2, w2 = full(a2)
            if better(v2, v, 1.000001):
                v, a, e, wp, changed = v2, a2, e2, w2, True
        if not changed:
            break
    return v, a, e, wp


def _kicks(build, cur_own, pools, best, base_extra, n, out_of_time):
    """Deep search: shake up 2-3 slots of the best build at random and search again from there."""
    import random
    v0, a0, wpn, wp, e0 = best
    rng = random.Random(len(pools[0]) * 7919 + build.level)
    found = best
    for _ in range(n):
        if out_of_time():
            break
        a = list(found[1])
        for sl in rng.sample(range(8), rng.choice([2, 3])):
            if pools[sl]:
                a[sl] = rng.choice(pools[sl][: max(10, len(pools[sl]) // 3)])
        res = _local_search(build, cur_own, pools, wpn, wp, [a], found[4], EFFORT[2], out_of_time)
        if res and res[0][0] > found[0]:
            found = res[0]
    return found


# ---- worker processes (each loads the game data once, then searches around one weapon at a time)
_W = {}


def _build_spec(build):
    return {"cls": build.tree.cls, "level": build.level, "goal": build.goal,
            "tomes": [t.name for t in build.tomes], "aspects": [(a["displayName"], t) for a, t in build.aspects],
            "toggles": list(build.toggles), "sliders": dict(build.sliders), "slider_mode": build.slider_mode,
            "buffs": list(build.buffs), "raid_buffs": list(build.raid_buffs), "enemy": dict(build.enemy),
            "rolls": wh.ROLL_MODE, "poison": wh.POISON_MODE, "version": build.data.version, "data_dir": wh.DATA_DIR,
            "focus": build.focus, "floors": dict(getattr(build, "floors", None) or {})}


def _worker_init(spec, stop_event, q=None):
    wh.ROLL_MODE, wh.POISON_MODE = spec["rolls"], spec["poison"]
    data = wh.load_game_data(offline=True, data_dir=spec["data_dir"], version=spec["version"])
    tree = wh.Tree(data, spec["cls"])
    tomes = [t for n in spec["tomes"] for t in [data.find_tome(n)] if t is not None]
    pool = {a["displayName"]: a for a in data.aspects.get(wh.CLASS_TITLE[spec["cls"]], [])}
    aspects = [(pool[n], t) for n, t in spec["aspects"] if n in pool]
    _W["build"] = wh.Build(data, tree, spec["level"], spec["goal"], tomes=tomes, aspects=aspects,
                           toggles=spec["toggles"], sliders=spec["sliders"], slider_mode=spec["slider_mode"],
                           buffs=spec["buffs"], raid_buffs=spec["raid_buffs"], enemy=spec["enemy"],
                           focus=tuple(spec["focus"]) if spec.get("focus") else None, floors=spec.get("floors"))
    _W["stop"] = stop_event
    if q is not None:
        last = {"t": 0.0, "v": -1e300, "n": wh.EVAL_STATS["n"]}

        def best_hook(v, main, an, wn):
            if v > last["v"]:
                last["v"] = v
                q.put(("b", v, main, an, wn))

        def tick(force=False):
            now = time.time()
            if force or now - last["t"] > 0.3:
                n = wh.EVAL_STATS["n"]
                if n > last["n"]:
                    q.put(("e", n - last["n"]))
                last["n"], last["t"] = n, now
        _HOOK["best"] = best_hook
        wh.EVAL_STATS["tick"] = tick


def _worker_job(job):
    build = _W["build"]
    cur_own, pools, wpn, wp, armor0, base_extra, memory_arms, P, deadline = job
    stop = _W.get("stop")

    def out_of_time():
        return (stop is not None and stop.is_set()) or (deadline is not None and time.time() > deadline)
    starts = _starts_for(build, cur_own, pools, wpn, wp, armor0, base_extra, memory_arms, P)
    res = _local_search(build, cur_own, pools, wpn, wp, starts, base_extra, P, out_of_time)
    tick = wh.EVAL_STATS.get("tick")
    if tick is not None:
        tick(True)
    return res


def _run_weapon_jobs(build, cur_own, pools, jobs, armor0, base_extra, P, deadline, log):
    total = len(jobs)

    def out_of_time():
        return STOP["flag"] or (deadline is not None and time.time() > deadline)
    results = []
    workers = min(total, max(1, (os.cpu_count() or 1)))
    if workers > 1:
        try:
            import multiprocessing as mp
            from concurrent.futures import ProcessPoolExecutor, as_completed
            ctx = mp.get_context("spawn")
            stop_event = ctx.Event()
            live_q = ctx.Queue()
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_worker_init,
                                     initargs=(_build_spec(build), stop_event, live_q)) as ex:
                futs = {ex.submit(_worker_job, (cur_own, pools, wpn, wp, armor0, base_extra, mem, P, deadline)): wpn
                        for wpn, wp, mem in jobs}
                done = 0
                failures = []
                _report(0.05, f"Building around {total} weapons on {workers} CPU cores")
                pending = set(futs)
                while pending:
                    _drain(live_q)
                    for f in pending:
                        if f.running():
                            _card(futs[f].name, status="running")
                    finished = [f for f in pending if f.done()]
                    for f in finished:
                        pending.discard(f)
                        done += 1
                        try:
                            got = f.result()
                            results.extend(got)
                            _card(futs[f].name, status="done", best=max([r[0] for r in got], default=None))
                        except Exception as exc:
                            failures.append(exc)
                            _card(futs[f].name, status="done")
                        _report(0.05 + 0.75 * done / total, f"Built around {done} of {total} weapons ({futs[f].name})")
                    if out_of_time():
                        stop_event.set()
                    if pending:
                        time.sleep(0.2)
            time.sleep(0.3)
            _drain(live_q)
            if results:
                if log:
                    log(f"Used {workers} CPU cores.")
                return results
            raise RuntimeError(f"all {len(failures)} parallel jobs failed: {failures[:1]}")
        except Exception as exc:                      # fall back to one core
            if log:
                log(f"(Parallel search unavailable: {type(exc).__name__}; using one core.)")
            results = []
    for i, (wpn, wp, mem) in enumerate(jobs):
        if out_of_time():
            break
        _report(0.05 + 0.75 * i / max(1, total), f"Building around {wpn.name} ({i + 1}/{total})")
        _card(wpn.name, status="running")
        starts = _starts_for(build, cur_own, pools, wpn, wp, armor0, base_extra, mem, P)
        got = _local_search(build, cur_own, pools, wpn, wp, starts, base_extra, P, out_of_time)
        results.extend(got)
        _card(wpn.name, status="done", best=max([r[0] for r in got], default=None))
    return results


def search_best_build(build, owned, armor0, weapon0, base_weapon0, keep=(), ap_budget=0, include_never=True,
                      min_level=1, deep=False, time_limit=None, log=None, pools_override=None, effort=2):
    """
    Search for the best whole build:
      1. safe filtering (items that provably can't win are dropped),
      2. for each top weapon (several attack speeds and elements), local search from several starting points
         with one-, two- (and in deep mode three-) item swaps, scoring the whole build every time,
      3. polish the best few: re-plan the ability tree, refine skill points, best powders.
    Returns the same dict shape as optimize_whole_build.
    """
    t_start = time.time()
    STOP["flag"] = False
    effort = max(1, min(5, int(effort or 2)))
    if deep:
        effort = max(effort, 4)
    P = EFFORT[effort]
    level, cls = build.level, build.tree.cls
    slots = list(range(8)) + ["weapon"]
    LIVE["phase"] = 0
    LIVE["label"] = (build.focus[1] if build.goal == "ability" and build.focus else GOAL_LABELS.get(build.goal, "Score"))
    keep_items = [a for a in armor0 if a is not None] + ([base_weapon0] if base_weapon0 else [])
    if pools_override is not None:
        pools = dict(pools_override)
        pools[5] = pools[4]
        fstats = {"before": {k: len(v) for k, v in pools.items() if k != 5},
                  "after": {k: len(v) for k, v in pools.items() if k != 5}}
    else:
        pools, fstats = safe_filter(build, include_never, min_level, keep=keep_items)
    labels = [wh.SLOT_LABELS[k] for k in range(8)] + ["Weapon"]
    LIVE["pools"] = {labels[i]: [[it.name, it.tier] for it in (pools[k] or [])[:80]]
                     for i, k in enumerate(list(range(8)) + ["weapon"])}
    LIVE["phase"] = 1
    if log:
        log(f"Item lists after safe filtering: " + ", ".join(
            f"{wh.SLOT_LABELS[k] if k != 'weapon' else 'Weapon'} {fstats['before'][k]}->{fstats['after'][k]}"
            for k in fstats['after']))
    cur_own = frozenset(owned)

    def out_of_time():
        return STOP["flag"] or (time_limit is not None and time.time() - t_start > time_limit)

    def score(a, w, e):
        return build.evaluate(cur_own, a, w, wh._fit_extra(e, a, w, level), fast=True)["value"]

    def put(a, slot, it):
        a2 = list(a)
        old = a[slot]
        a2[slot] = wh.armor_with_powders(it, old.powders) if (old is not None and old.powders and slot < 4) else it
        return a2

    # ---- 1. weapons worth building around
    # (Ranked on damage alone: with minimum stats set, the starting gear often misses them whatever the
    # weapon, which would say nothing about which weapons are strong. The gear around each weapon is then
    # searched with the minimums on.)
    rb = build.copy_with(floors=None) if getattr(build, "floors", None) else build
    base_extra, _ = refine_points(rb, cur_own, armor0, weapon0, [0] * 5)
    w_scores = []
    for wpn in pools["weapon"]:
        pw, _ = wh.best_weapon_powders(rb, cur_own, armor0, wpn, base_extra, quick=True)
        wp = wh.weapon_with_powders(wpn, pw)
        if not wh._wearable(armor0, wp, level):
            # judge it on empty gear instead so heavy-requirement weapons still get a chance
            a_try = [None] * 8
        else:
            a_try = armor0
        e = _scaled_extra(base_extra, a_try, wp, level) if sum(base_extra) else refine_points(rb, cur_own, a_try, wp, [0]*5)[0]
        w_scores.append((rb.evaluate(cur_own, a_try, wp, wh._fit_extra(e, a_try, wp, level), fast=True)["value"], wpn, wp))
    w_scores.sort(key=lambda x: -x[0])
    n_w = P["weapons"]
    chosen_w, groups = [], set()
    for v, wpn, wp in w_scores:                     # best of each attack speed / main element first
        el = max(range(6), key=lambda i: wpn.damage[i][1]) if wpn.damage else 0
        g = (wpn.atk_spd, el)
        if g not in groups:
            groups.add(g)
            chosen_w.append((v, wpn, wp))
    rest = [x for x in w_scores if x not in chosen_w]
    chosen_w = sorted(chosen_w, key=lambda x: -x[0])[: max(3, n_w // 2)] + rest[: n_w]
    seen_w, weapons = set(), []
    for v, wpn, wp in chosen_w:
        if wpn.name not in seen_w:
            seen_w.add(wpn.name)
            weapons.append((wpn, wp))
    weapons = weapons[:n_w]
    if base_weapon0 is not None and base_weapon0.name not in seen_w:
        weapons.append((base_weapon0, weapon0))
        seen_w.add(base_weapon0.name)
    memory = remembered_starts(build, include_never, min_level) if pools_override is None else []
    if memory and log:
        log(f"Starting from {len(memory)} build(s) remembered from earlier scans with these settings.")
    for arm, wpn in memory:
        if wpn.name not in seen_w:
            pw, _ = wh.best_weapon_powders(build, cur_own, arm, wpn, base_extra, quick=True)
            weapons.append((wpn, wh.weapon_with_powders(wpn, pw)))
            seen_w.add(wpn.name)

    # ---- 2. local search around each weapon (in parallel on every CPU core when possible)
    LIVE["weapons"] = [{"name": w.name, "tier": w.tier, "status": "queued", "best": None, "stage": "main"}
                       for w, _wp in weapons]
    LIVE["phase"] = 2
    memory_arms = {}
    for arm, mw in memory:
        memory_arms.setdefault(mw.name, []).append(list(arm))
    jobs = [(wpn, wp, memory_arms.get(wpn.name, [])) for wpn, wp in weapons]
    deadline = (t_start + time_limit) if time_limit else None
    extra_on = bool(P.get("extra_weapons")) or bool(P.get("depth"))
    _PHASE[:] = [0.0, 0.72 if extra_on else 1.0]
    results = _run_weapon_jobs(build, cur_own, pools, jobs, armor0, base_extra, P, deadline, log)
    if not results:
        results.append((score(armor0, weapon0, base_extra), list(armor0), base_weapon0, weapon0, base_extra))

    def dedupe(rs):
        rs = sorted(rs, key=lambda x: -x[0])
        out, seen = [], set()
        for r in rs:
            key = (tuple(it.name if it else None for it in r[1]), r[2].name)
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    def agreement(rs):
        """Of the attempts built around the winning weapon (each from a different starting point), how many
        ended at the best build. Attempts around weaker weapons can't reach it, so they don't count."""
        rs = sorted(rs, key=lambda x: -x[0])
        top_v = rs[0][0] if rs else 0
        same_w = [r for r in rs if rs and r[2].name == rs[0][2].name]
        agree = sum(1 for r in same_w if (top_v > 0 and r[0] >= top_v * 0.9999) or (top_v < 0 and r[0] >= top_v * 1.0001))
        return agree, len(same_w)

    def polish(v, a, wpn, wp, e, own0):
        """Fine-tune one build: ability tree, weapon powders, close contenders per slot, exact skill points."""
        own = own0
        if ap_budget and not STOP["flag"]:
            new_own, _ = wh.best_tree(build, a, wp, ap_budget, keep=keep, extra_points=e, beam_width=30)
            if build.evaluate(new_own, a, wp, e)["value"] > build.evaluate(own, a, wp, e)["value"]:
                own = frozenset(new_own)
        pw, _ = wh.best_weapon_powders(build, own, a, wpn, e)
        wp = wh.weapon_with_powders(wpn, pw)
        if not STOP["flag"]:
            _v, a, e, wp = _refined_sweep(build, own, a, wp, pools, e, wpn=wpn)
        e, free = refine_points(build, own, a, wp, e, fast=False)
        fin = build.evaluate(own, a, wp, e)
        return (fin["value"], a, wp, wpn, own, e, free, fin)

    # ---- 3. polish the best few: tree, skill points, powders
    LIVE["phase"] = 3
    uniq = dedupe(results)
    agree, n_attempts = agreement(results)
    if P["kicks"] and uniq and not out_of_time():
        _report(0.8, "Shaking up the best build and searching again")
        kicked = _kicks(build, cur_own, pools, uniq[0], base_extra, P["kicks"], out_of_time)
        if kicked[0] > uniq[0][0]:
            uniq.insert(0, kicked)
    finals = []
    n_polish = P["polish"]
    polished = set()
    for i, (v, a, wpn, wp, e) in enumerate(uniq[:n_polish]):
        _report(0.82 + 0.15 * i / n_polish, "Fine-tuning the best builds (ability tree, skill points, powders)")
        finals.append(polish(v, a, wpn, wp, e, cur_own))
        polished.add((tuple(it.name if it else None for it in a), wpn.name))
        _live_offer(finals[-1][0], finals[-1][7]["main"], *_names(finals[-1][1], wpn))
    finals.sort(key=lambda x: -x[0])
    main_best = finals[0][0]

    # ---- 4. extra searching on top (only adds builds to choose from; everything above is kept as it was,
    # so the answer can only stay the same or get better)
    bonus = {"weapons": 0, "deeper": 0, "before": main_best, "after": main_best, "found_by": None}
    if extra_on and not out_of_time():
        _PHASE[:] = [0.72, 1.0]
        extra_results = []
        # 4a. quick passes on weapons the main search skipped; the best few then get the full search
        rest = [(wpn, wp) for _v, wpn, wp in w_scores if wpn.name not in seen_w][: P.get("extra_weapons", 0)]
        LIVE["phase"] = 4
        if rest and not out_of_time():
            bonus["weapons"] = len(rest)
            LIVE["weapons"] += [{"name": w.name, "tier": w.tier, "status": "queued", "best": None, "stage": "quick"}
                                for w, _wp in rest]
            qp = dict(P, greedy_only=True, singles_only=True, rounds=2, random_starts=0)
            _report(0.0, f"Quick look at {len(rest)} more weapons")
            quick = _run_weapon_jobs(build, cur_own, pools, [(wpn, wp, []) for wpn, wp in rest], armor0,
                                     base_extra, qp, deadline, None)
            quick = dedupe(quick)
            extra_results += quick
            promote, names = [], set()
            for r in quick:
                if r[2].name not in names:
                    names.add(r[2].name)
                    promote.append((r[2], r[3], []))
            promote = promote[: P.get("promote", 2)]
            if promote and not out_of_time():
                LIVE["weapons"] += [{"name": w.name, "tier": w.tier, "status": "queued", "best": None, "stage": "full"}
                                    for w, _wp, _m in promote]
                _report(0.35, f"Full search around the {len(promote)} most promising of them")
                extra_results += _run_weapon_jobs(build, cur_own, pools, promote, armor0, base_extra, P,
                                                  deadline, None)
        # 4b. extra depth around the best builds: three-item swaps and a few shake-ups
        depth = P.get("depth", 0)
        if depth and not out_of_time():
            _report(0.7, "Searching deeper around the best builds")
            dp = dict(P, triple=max(P["triple"], 3), short=max(P["short"], 7))
            tops, names = [], set()
            for f in finals:
                key = (tuple(it.name if it else None for it in f[1]), f[3].name)
                if key not in names:
                    names.add(key)
                    tops.append(f)
            for f in tops[:depth]:
                if out_of_time():
                    break
                fv, fa, fwp, fwpn, fown, fe = f[:6]
                res = _local_search(build, fown, pools, fwpn, fwp, [fa], fe, dp, out_of_time)
                if res and not out_of_time():
                    res = [_kicks(build, fown, pools, res[0], fe, P.get("depth_kicks", 4), out_of_time)]
                bonus["deeper"] += 1
                extra_results += [(r[0], r[1], r[2], r[3], r[4], fown) for r in res]
        # polish the best new builds not already polished
        # (the best few from the extra weapons, and the best from the deeper search: the two are scored on
        # different ability trees until polished, so each gets its own places)
        cand = []
        for group, limit in (([r for r in extra_results if len(r) == 5], P.get("extra_polish", 2)),
                             ([r for r in extra_results if len(r) > 5], max(1, P.get("depth", 1)))):
            taken = 0
            for r in sorted(group, key=lambda x: -x[0]):
                if taken >= limit:
                    break
                key = (tuple(it.name if it else None for it in r[1]), r[2].name)
                if key in polished:
                    continue
                polished.add(key)
                cand.append(r)
                taken += 1
        source = {}
        for i, r in enumerate(cand):
            if out_of_time():
                break
            _report(0.85 + 0.13 * i / max(1, len(cand)), "Fine-tuning the new contenders")
            own0 = r[5] if len(r) > 5 else cur_own
            f = polish(r[0], r[1], r[2], r[3], r[4], own0)
            source[id(f)] = "deeper search" if len(r) > 5 else "more weapons"
            finals.append(f)
            _live_offer(f[0], f[7]["main"], *_names(f[1], f[3]))
        finals.sort(key=lambda x: -x[0])
        if id(finals[0]) in source and better(finals[0][0], main_best, 1.001):
            bonus["found_by"] = source[id(finals[0])]
        bonus["after"] = finals[0][0]
        if finals[0][0] > main_best:
            agree, n_attempts = agreement(results + [r[:5] for r in extra_results])
    _PHASE[:] = [0.0, 1.0]
    LIVE["phase"] = 5
    if pools_override is None:
        remember(build, include_never, min_level, finals)
    v, a, wp, wpn, own, e, free, fin = finals[0]
    start_e, _ = refine_points(build, frozenset(owned), armor0, weapon0, [0] * 5, fast=False)
    start = build.evaluate(frozenset(owned), armor0, weapon0, start_e)
    _report(1.0, "Done")
    return {"armor": a, "weapon": wp, "base_weapon": wpn, "owned": own, "extra": e, "free": free,
            "final": fin, "start": start, "start_val": start["value"], "filter": fstats,
            "stopped": STOP["flag"], "seconds": time.time() - t_start, "effort": effort,
            "effort_name": P["name"], "agree": agree, "attempts": n_attempts, "bonus_search": bonus}


# ============================================================================== time estimate

def estimate_seconds(build, include_never=True, min_level=1, cores=None, owned=None):
    """
    Rough time for each thoroughness setting on this computer: a 1-2 second speed test with the real
    item lists, then the amount of work each setting does. Returns {effort: seconds}.
    """
    import random
    pools, _ = safe_filter(build, include_never, min_level)
    own = frozenset(owned) if owned else frozenset(build.tree.roots)
    rng = random.Random(1)
    wpool = pools["weapon"] or [wh.default_weapon(build.data, build.tree.cls, build.level)]
    t0, n = time.time(), 0
    while n < 400 and time.time() - t0 < 1.5:
        a = [rng.choice(pools[sl]) if pools[sl] and rng.random() < 0.85 else None for sl in range(8)]
        wp = rng.choice(wpool)
        wh._wearable(a, wp, build.level)
        build.evaluate(own, a, wp, [rng.randint(0, 20) for _ in range(5)], fast=True)
        n += 1
    t_eval = (time.time() - t0) / max(1, n)
    cores = cores or max(1, os.cpu_count() or 1)
    sum_pool = sum(len(pools[sl]) for sl in range(8))
    ap = wh.ability_points(build.level)
    t_tree = 2.5 * (ap / 32) ** 2 * (t_eval / 0.0004) * (2.5 if build.level >= wh.ULTIMATE_LEVEL else 1)
    out = {}
    for eff, P in EFFORT.items():
        rounds = 3
        per_start = rounds * (2 * sum_pool + 28 * (P["short"] + 3) ** 2 + 56 * P["triple"] ** 3 + 150)
        W = min(P["weapons"], len(wpool)) + 1
        S = 3 + P["random_starts"]
        local = W * S * per_start * t_eval / min(cores, W)
        ranking = len(wpool) * 140 * t_eval
        kicks = P["kicks"] * (2 * (2 * sum_pool + 28 * 81 + 150)) * t_eval
        polish = P["polish"] * (t_tree + 3 * (sum_pool + 40 * 170) * t_eval)
        out[eff] = CALIBRATION * (local + ranking + kicks + polish)
    return out


CALIBRATION = 1.0
