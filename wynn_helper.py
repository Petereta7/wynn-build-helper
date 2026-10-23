#!/usr/bin/env python3
"""
Wynn Build Helper
=================

A build helper for Wynncraft (all five classes). Given your class, level, goal and gear it works out:

  * the best legal ability tree for your level, and the order to click it in
  * how to spend your skill points so every item works and damage is as high as possible
  * gear upgrades for each slot that you can actually wear
  * a level-by-level plan of what to unlock next

Data
----
Every run pulls fresh data (cached in ./wynn_data so it also works offline):
  * Wynnbuilder's game data (ability trees with full rules and spell multipliers, all items),
    https://github.com/wynnbuilder/wynnbuilder.github.io  (GPL-3.0)
  * Wynncraft's official API, used to cross-check the ability trees and your items, so you are
    warned when the two sources disagree (for example right after a game patch).

The damage maths is a Python port of Wynnbuilder's calculations (spells, powders, crafted and custom
items, skill points, rolls, defenses, links), checked side by side against Wynnbuilder's own code;
see tests/README.md for how to re-run those checks after a game patch.

License: GPL-3.0 (this file ports logic from Wynnbuilder, which is GPL-3.0).

Usage
-----
    python3 wynn_helper.py                     # asks you questions
    python3 wynn_helper.py --class mage --level 42 --goal spells \
        --gear "Haqherphix, Ridge, Phosphene, Adventurer's Pants, Morph-Gold" \
        --keep "Arcane Transfer"
    python3 wynn_helper.py --help
"""

from __future__ import annotations

import argparse
import functools
import itertools
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "wynn_data")

WB_LIST_URL = "https://api.github.com/repos/wynnbuilder/wynnbuilder.github.io/contents/data"
WB_FILE_URL = "https://raw.githubusercontent.com/wynnbuilder/wynnbuilder.github.io/master/data/{ver}/{file}"
OFFICIAL_TREE_URL = "https://api.wynncraft.com/v3/ability/tree/{cls}"
OFFICIAL_ITEM_URL = "https://api.wynncraft.com/v3/item/search/{name}"

DATA_FILES = ["atree.json", "items.json", "majid.json", "aspects.json", "tomes.json",
              "encoding_consts.json", "recipes.json", "ingreds.json"]
CLASSES = ["archer", "warrior", "mage", "assassin", "shaman"]
CLASS_TITLE = {c: c.capitalize() for c in CLASSES}
CLASS_WEAPON = {"mage": "wand", "warrior": "spear", "archer": "bow", "assassin": "dagger", "shaman": "relik"}
SKILLS = ["str", "dex", "int", "def", "agi"]
SKILL_NAMES = ["Strength", "Dexterity", "Intelligence", "Defense", "Agility"]
ELEM_PREFIX = ["n", "e", "t", "w", "f", "a"]          # neutral, earth, thunder, water, fire, air
ELEM_NAMES = ["Neutral", "Earth", "Thunder", "Water", "Fire", "Air"]
ATTACK_SPEEDS = ["SUPER_SLOW", "VERY_SLOW", "SLOW", "NORMAL", "FAST", "VERY_FAST", "SUPER_FAST"]
ATTACK_SPEED_MULT = [0.51, 0.83, 1.5, 2.05, 2.5, 3.1, 4.3]
ARMOR_SLOTS = ["helmet", "chestplate", "leggings", "boots"]
ACCESSORY_SLOTS = ["ring", "ring", "bracelet", "necklace"]
SLOT_LABELS = ["Helmet", "Chestplate", "Leggings", "Boots", "Ring 1", "Ring 2", "Bracelet", "Necklace"]
SLOT_TYPES = ARMOR_SLOTS + ACCESSORY_SLOTS
MAX_ASSIGNED_PER_SKILL = 100
CLICKS_PER_SECOND = 9          # Wynnbuilder's default for its mana model
BASE_MANA_PER_SECOND = 5       # mana/s = 5 + mr/5, plus ms/3 only while main-attacking (mana steal works on main
                               # attack hits, not spells: wiki, Identifications)
ULTIMATE_LEVEL = 90

# Total ability points by combat level (from Wynnbuilder; matches the official wiki table).
AP_TABLE = [0, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 8, 8, 9, 9, 10, 11, 11, 12, 12, 13, 14, 14, 15, 16, 16,
            17, 17, 18, 18, 19, 19, 20, 20, 20, 21, 21, 22, 22, 23, 23, 23, 24, 24, 25, 25, 26, 26, 27, 27,
            28, 28, 29, 29, 30, 30, 31, 31, 32, 32, 33, 33, 34, 34, 34, 35, 35, 35, 36, 36, 36, 37, 37, 37,
            38, 38, 38, 38, 39, 39, 39, 39, 40, 40, 40, 40, 41, 41, 41, 41, 42, 42, 42, 42, 43, 43, 43, 43,
            44, 44, 44, 44, 45, 45, 45, 46, 46, 46, 47, 47, 47, 48, 48, 48, 49, 49, 49, 49, 50, 50]


def ability_points(level: int, rank_bonus: int = 0) -> int:
    level = max(1, min(level, len(AP_TABLE) - 1))
    return AP_TABLE[level] + rank_bonus


def skill_points_available(level: int) -> int:
    if level < 1:
        return 0
    return 200 if level >= 101 else (level - 1) * 2


def skill_pct(points: float) -> float:
    """Fraction bonus from a skill (Wynncraft curve, capped at 150 points)."""
    if points <= 0:
        return 0.0
    points = min(points, 150)
    r = 0.9908
    return (r / (1 - r) * (1 - r ** points)) / 100.0


SKILL_DAMAGE_MULT = [1, 1, 1, 0.867, 0.951]
NONSTACKING_MULTS = {"Potion", "Vulnerability", "Mask"}   # Wynnbuilder: these take the highest, not the sum

# Party/ability buffs from Wynnbuilder's boost buttons: name -> ({damage mult}, {defense mult}).
BUFFS = {
    "totem": ({"Potion": 15}, {}),                  # Shaman Totem
    "fortitude": ({"Potion": 30}, {}),              # Mage Fortitude
    "chantlunatic": ({"Potion": 20}, {}),           # Shaman Chant of the Lunatic
    "warscream": ({}, {"Potion": 20}),              # Warrior War Scream (defense)
    "emboldeningcry": ({}, {"Potion": 5}),
    "hauntingfanatic": ({"Vulnerability": 12}, {}),
    "hauntinglunatic": ({}, {"AbilityWeaken": 15}),
    "eldritchcall": ({}, {"AbilityWeaken": 20}),
    "judgement": ({"Judgement": 20}, {"Judgement": 20}),
    "chantfanatic": ({}, {"Potion": 30}),
    "weathering": ({}, {"Weathering": 20}),
    "acidrain": ({"AcidRain": 8}, {}),
}


def buff_mults(buffs, kind: int = 0) -> dict:
    """Combined damage (kind 0) or defense (kind 1) multipliers; potion-type ones don't stack."""
    out = {}
    for b in buffs:
        for k, v in BUFFS.get(b, ({}, {}))[kind].items():
            if k in NONSTACKING_MULTS:
                out[k] = max(out.get(k, 0), v)
            else:
                out[k] = out.get(k, 0) + v
    return out


# Raid buffs (Wynnbuilder's raid_buff_map).
RAID_BUFFS = {
    "Lightbearer-I": {"int": 25, "healPct": 20}, "Lightbearer-II": {"mr": 15, "sdPct": 40},
    "Lightbearer-III": {"int": 40, "healPct": 35, "majorIds": ["ARCANES"]},
    "Bioluminescent-I": {"rDamPct": 30}, "Bioluminescent-II": {"mdPct": 50, "sdPct": 50},
    "Bioluminescent-III": {"str": 10, "dex": 10, "int": 10, "def": 10, "agi": 10, "rDamPct": 50},
    "Berserk-I": {"mdPct": 75, "majorIds": ["EXPLOSIVE_IMPACT"]}, "Berserk-II": {"expd": 25, "sprintReg": 100, "mdRaw": 500},
    "Berserk-III": {"str": 30, "mdPct": 75, "mdRaw": 500},
    "Pestilent-I": {"str": 20, "poison": 3750, "ls": 250}, "Pestilent-II": {"str": 25, "poison": 5500, "ms": 12},
    "Pestilent-III": {"ls": 400, "poison": 7500, "majorIds": ["PLAGUE"]},
    "Bedrock-I": {"hpBonus": 1000, "ms": 10, "ls": 150}, "Bedrock-II": {"hpBonus": 1250, "ls": 300, "hprRaw": 700},
    "Bedrock-III": {"ms": 15, "hprRaw": 900, "majorIds": ["ALTRUISM"]},
    "Palisade": {"ls": 300, "spPct4": -50, "majorIds": ["INTANGIBLE"]},
    "Cherubim-I": {"spd": 45, "mdPct": 90}, "Cherubim-II": {"ls": 500, "rDamPct": 40},
    "Cherubim-III": {"str": 20, "dex": 20, "int": 20, "def": 20, "agi": 20},
    "Seraphim-I": {"agi": 20, "sdPct": 25, "ref": 30}, "Seraphim-II": {"mr": 20, "sdPct": 30},
    "Seraphim-III": {"dex": 30, "majorIds": ["SORCERY"]},
    "Ophanim-I": {"def": 30, "healPct": 30}, "Ophanim-II": {"agi": 30, "healPct": 25, "hprPct": 40},
    "Ophanim-III": {"hpBonus": 5000, "hprRaw": 600},
    "Throne-I": {"int": 35, "rDamPct": 25}, "Throne-II": {"mr": 25, "ms": 15},
    "Throne-III": {"int": 50, "majorIds": ["ARCANES"]},
    "Anti-I": {"expd": 50, "mainAttackRange": 50}, "Anti-II": {"str": 30, "poison": 6000},
    "Anti-III": {"def": 50, "sprint": 300, "majorIds": ["EXPLOSIVE_IMPACT"]},
    "Neophyte": {"mainAttackRange": 300, "ms": 50, "atkTier": -50},
    "Intrepid-I": {"spd": 40, "eSteal": 30, "majorIds": ["GREED"]},
    "Intrepid-II": {"hprRaw": 250, "hprPct": 20, "majorIds": ["ALTRUISM"]},
    "Intrepid-III": {"def": 25, "healPct": 25, "weakenEnemy": 5},
    "Stonewalker-I": {"str": 20, "mdRaw": 700, "thorns": 100},
    "Stonewalker-II": {"str": 30, "mdPct": 100, "expd": 50, "majorIds": ["EXPLOSIVE_IMPACT"]},
    "Stonewalker-III": {"mdRaw": 1500, "mdPct": 100},
    "Giant-I": {"str": 10, "def": 40}, "Giant-II": {"hpBonus": 3000, "hprRaw": 400},
    "Giant-III": {"hpBonus": 4000, "hprRaw": 500, "hprPct": 20},
    "Elder-I": {"agi": 30, "sdPct": 25, "ref": 100}, "Elder-II": {"int": 40, "mr": 12, "rDamPct": 30},
    "Elder-III": {"mr": 18, "sdPct": 30, "rDamPct": 30},
    "Boulderbreaker-I": {"dex": 20, "ls": 300}, "Boulderbreaker-II": {"agi": 25, "sdPct": 30, "spd": 35},
    "Boulderbreaker-III": {"str": 30, "dex": 30, "mdPct": 80},
    "Cirrus": {"agi": 30, "jh": 15, "healPct": 30},
    "Hollowed-I": {"def": 20, "hpBonus": 2000, "ref": 50}, "Hollowed-II": {"mr": 30, "hprRaw": 400, "thorns": 50},
    "Hollowed-III": {"hpBonus": 5000, "hprRaw": 500, "damPct": -30},
    "Sojourner-I": {"agi": 20, "mr": 20, "sprintReg": 80},
    "Sojourner-II": {"def": 40, "sprint": -100, "majorIds": ["FREERUNNER"]},
    "Sojourner-III": {"str": 30, "dex": 30, "hprRaw": -250},
    "Fading-I": {"hprPct": 25, "spd": 30, "majorIds": ["ALTRUISM"]}, "Fading-II": {"agi": 25, "ms": 15, "healPct": 25},
    "Fading-III": {"damRaw": 400, "damPct": 40, "str": -10, "dex": -10, "int": -10, "def": -10, "agi": -10},
    "Insidious-I": {"int": 30, "ms": 12, "sdPct": 25}, "Insidious-II": {"ls": 325, "sdPct": 40, "maxMana": 50},
    "Insidious-III": {"sdPct": 60, "spd": -40, "majorIds": ["SORCERY"]},
    "Hopeless-I": {"str": 20, "mainAttackRange": 30, "mdPct": 75},
    "Hopeless-II": {"dex": 25, "expd": 50, "majorIds": ["FISSION"]},
    "Hopeless-III": {"mdPct": 135, "spd": 60, "mr": -15},
    "Manic": {"mr": 20, "rDefPct": -100, "majorIds": ["MADNESS"]},
    "Relentless-I": {"spd": 30, "mdRaw": 600, "expd": 35}, "Relentless-II": {"weakenEnemy": 8, "spPct2": -75},
    "Ingenious-I": {"hpBonus": 3000, "spPct4": -40, "healPct": 25},
    "Ingenious-II": {"def": 30, "hprRaw": 1000, "majorIds": ["PHOENIXBORN"]},
    "Unrestrained-I": {"spRaw1": -5, "spRaw3": -5, "int": 25},
    "Unrestrained-II": {"damRaw": 250, "mdPct": 250, "poison": 7500},
    "Opulent-I": {"mr": 25, "maxMana": 40, "rDamPct": 25}, "Opulent-II": {"rDamRaw": 200, "rDamPct": 30, "majorIds": ["ARCANES"]},
    "Omniscient-I": {"ls": 650, "maxMana": 40, "dex": 20}, "Omniscient-II": {"ms": 20, "mainAttackRange": 75, "str": 20},
    "Restless": {"sdPct": -35, "maxMana": -25}, "Apathetic": {"spd": -45, "mdPct": -40},
    "Faceless": {"str": -10, "dex": -10, "def": -10, "agi": -10}, "Prideful": {"hpBonus": -3000, "rDefPct": -50},
    "Isolated": {"hprRaw": -375, "mr": -10},
}


def raid_buff_stats(names):
    """Summed stats and major IDs from the chosen raid buffs."""
    stats, majids = defaultdict(float), []
    for n in names:
        for k, v in RAID_BUFFS.get(n, {}).items():
            if k == "majorIds":
                majids.extend(v)
            else:
                stats[k] += v
    return stats, majids                  # str, dex, int, def, agi
INT_COST_MULT = 0.5 / skill_pct(150)                         # 150 Int = 50% cheaper spells


def strip_mc(text) -> str:
    text = re.sub(r"§.", "", str(text))
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


# ============================================================================== networking

def http_json(url: str, timeout: int = 20):
    headers = {"User-Agent": "WynnBuildHelper/1.0 (+https://github.com/wynnbuilder)"}
    try:
        import requests  # optional
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except ImportError:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)


# ============================================================================== game data

def _version_key(v: str):
    return tuple(int(x) for x in v.split("."))


class GameData:
    def __init__(self, version: str, atree: dict, items_json: dict, notes: list[str],
                 majids: dict | None = None, aspects: dict | None = None, tomes: dict | None = None):
        self.version = version
        self.atree = atree
        self.notes = notes
        self.sets = items_json.get("sets", {})
        self.majids = majids or {}
        self.aspects = aspects or {}
        self.tomes = [Item(t) for t in (tomes or {}).get("tomes", [])]
        self.items: dict[str, Item] = {}
        for raw in items_json["items"]:
            if not raw.get("type") or raw.get("category") not in ("armor", "accessory", "weapon"):
                continue
            it = Item(raw)
            self.items[it.name.lower()] = it

    def find_tome(self, name: str):
        import difflib
        names = {t.name.lower(): t for t in self.tomes}
        key = name.strip().lower()
        if key in names:
            return names[key]
        close = difflib.get_close_matches(key, list(names), n=1, cutoff=0.6)
        return names[close[0]] if close else None

    def find_aspect(self, cls: str, name: str):
        import difflib
        pool = {a["displayName"].lower(): a for a in self.aspects.get(CLASS_TITLE[cls], [])}
        key = name.strip().lower()
        if not key.startswith("aspect"):
            key = "aspect of " + key
        for cand in (key, key.replace("aspect of ", "aspect of the ")):
            if cand in pool:
                return pool[cand]
        close = difflib.get_close_matches(key, list(pool), n=1, cutoff=0.5)
        return pool[close[0]] if close else None

    def find_item(self, name: str):
        key = name.strip().lower()
        if key in self.items:
            return self.items[key], None
        # forgiving match: ignore punctuation, then closest name
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        target = norm(name)
        for k, it in self.items.items():
            if norm(k) == target:
                return it, None
        import difflib
        close = difflib.get_close_matches(key, list(self.items.keys()), n=3, cutoff=0.6)
        if close:
            return self.items[close[0]], f'"{name}" not found, using closest match "{self.items[close[0]].name}"'
        return None, f'"{name}" not found in the item data'


def load_game_data(offline: bool = False, data_dir: str = DATA_DIR, version: str | None = None) -> GameData:
    notes = []
    os.makedirs(data_dir, exist_ok=True)
    cached = sorted([d for d in os.listdir(data_dir)
                     if re.fullmatch(r"\d+(\.\d+)+", d)
                     and os.path.exists(os.path.join(data_dir, d, "atree.json"))
                     and os.path.exists(os.path.join(data_dir, d, "items.json"))], key=_version_key)

    if not offline:
        try:
            listing = http_json(WB_LIST_URL)
            versions = sorted([e["name"] for e in listing
                               if e.get("type") == "dir" and re.fullmatch(r"\d+(\.\d+)+", e["name"])],
                              key=_version_key)
            latest = versions[-1]
            target = os.path.join(data_dir, latest)
            missing = [f for f in DATA_FILES if not os.path.exists(os.path.join(target, f))]
            if missing:
                os.makedirs(target, exist_ok=True)
                for fname in missing:
                    try:
                        data = http_json(WB_FILE_URL.format(ver=latest, file=fname), timeout=60)
                    except Exception:
                        if fname in ("atree.json", "items.json"):
                            raise
                        continue   # optional files (major IDs, aspects, tomes)
                    with open(os.path.join(target, fname), "w", encoding="utf-8") as fh:
                        json.dump(data, fh)
                if latest not in cached:
                    cached.append(latest)
                notes.append(f"Downloaded game data {latest} from Wynnbuilder.")
            else:
                notes.append(f"Game data {latest} is up to date.")
        except Exception as exc:
            notes.append(f"Couldn't check for new game data ({type(exc).__name__}); using the saved copy.")
    if not cached:
        sys.exit("No game data available. Connect to the internet and run again.")
    if version not in cached:          # a specific saved version can be asked for (used by the self-test)
        version = cached[-1]

    def read(fname):
        path = os.path.join(data_dir, version, fname)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    data = GameData(version, read("atree.json"), read("items.json"), notes,
                    majids=read("majid.json"), aspects=read("aspects.json"), tomes=read("tomes.json"))
    data.enc = read("encoding_consts.json")
    rec = read("recipes.json") or {}
    data.recipe_types = {int(r["id"]): r["type"].lower() for r in rec.get("recipes", [])}
    data.recipes = {int(r["id"]): r for r in rec.get("recipes", [])}
    data.ingredients = build_ingredient_table(read("ingreds.json") or [])
    data.version_names = wb_version_names(data_dir, offline)
    for fname, label in (("majid.json", "major IDs"), ("aspects.json", "aspects"), ("tomes.json", "tomes"),
                         ("ingreds.json", "crafted items")):
        if read(fname) is None:
            notes.append(f"No {label} data saved yet; {label} are skipped until you run once online.")
    return data


def cross_check_official_tree(data: GameData, cls: str) -> list[str]:
    """Compare Wynnbuilder's tree for a class with the official API (names and ability point costs)."""
    try:
        official = http_json(OFFICIAL_TREE_URL.format(cls=cls))
    except Exception as exc:
        return [f"Official API not reachable ({type(exc).__name__}); tree not cross-checked."]
    off = {}
    for _page, abilities in (official.get("pages") or {}).items():
        for _aid, ab in (abilities or {}).items():
            name = strip_mc(ab.get("name", ""))
            cost = (ab.get("requirements") or {}).get("ABILITY_POINTS")
            off[name.lower()] = (name, cost)
    wb = {n["display_name"].lower(): n for n in data.atree.get(CLASS_TITLE[cls], [])}
    only_off = [off[k][0] for k in off if k not in wb]
    only_wb = [wb[k]["display_name"] for k in wb if k not in off]
    cost_diff = [f'{wb[k]["display_name"]} ({wb[k]["cost"]} vs official {off[k][1]})'
                 for k in wb if k in off and off[k][1] is not None and off[k][1] != wb[k]["cost"]]
    msgs = []
    if not off:
        return ["Official API returned no ability data; tree not cross-checked."]
    if not (only_off or only_wb or cost_diff):
        msgs.append(f"Official API check: all {len(off)} {CLASS_TITLE[cls]} abilities match (names and costs).")
    else:
        msgs.append(f"Official API check: {len(only_off) + len(only_wb) + len(cost_diff)} differences found. "
                    "Results may be off until Wynnbuilder updates.")
        if cost_diff:
            msgs.append("  cost differs: " + "; ".join(cost_diff[:6]))
        if only_off:
            msgs.append("  only in official data: " + ", ".join(only_off[:8]))
        if only_wb:
            msgs.append("  only in Wynnbuilder data: " + ", ".join(only_wb[:8]))
    return msgs


def cross_check_official_items(items: list) -> list[str]:
    """Check level and skill requirements of the given items against the official API."""
    msgs, ok = [], 0
    key_map = {"strength": 0, "dexterity": 1, "intelligence": 2, "defence": 3, "defense": 3, "agility": 4}
    for it in items:
        try:
            res = http_json(OFFICIAL_ITEM_URL.format(name=urllib.parse.quote(it.name)))
        except Exception as exc:
            return msgs + [f"Official item check skipped ({type(exc).__name__})."]
        entry = None
        if isinstance(res, dict):
            entry = res.get(it.name) or next((v for k, v in res.items() if k.lower() == it.name.lower()), None)
        if not isinstance(entry, dict):
            msgs.append(f"  {it.name}: not found in official item search")
            continue
        req = entry.get("requirements") or {}
        diffs = []
        lvl = req.get("level")
        if isinstance(lvl, int) and lvl != it.level:
            diffs.append(f"level {it.level} vs {lvl}")
        for k, idx in key_map.items():
            if isinstance(req.get(k), int) and req[k] != it.reqs[idx]:
                diffs.append(f"{SKILL_NAMES[idx]} {it.reqs[idx]} vs {req[k]}")
        if diffs:
            msgs.append(f"  {it.name}: differs from official data ({', '.join(diffs)})")
        else:
            ok += 1
    if items:
        msgs.insert(0, f"Official item check: {ok}/{len(items)} of your items match.")
    return msgs


# ============================================================================== items

NON_STAT_KEYS = {"lvl", "id", "slots", "averageDps", "fixID", "strReq", "dexReq", "intReq", "defReq", "agiReq",
                 "str", "dex", "int", "def", "agi", "atkSpd", "nDam", "eDam", "tDam", "wDam", "fDam", "aDam",
                 "remapID", "allowCraftsman", "persistent", "mainAttackRange"}

# Stats that roll when an item is identified (Wynnbuilder's rolledIDs). Everything else is fixed.
ROLLED_IDS = set("""hprPct mr sdPct mdPct ls ms xpb lb ref thorns expd spd atkTier poison hpBonus spRegen eSteal
hprRaw sdRaw mdRaw fDamPct wDamPct aDamPct tDamPct eDamPct fDefPct wDefPct aDefPct tDefPct eDefPct
spPct1 spRaw1 spPct2 spRaw2 spPct3 spRaw3 spPct4 spRaw4 rSdRaw sprint sprintReg jh lq gXp gSpd
eMdPct eMdRaw eSdPct eSdRaw eDamRaw eDamAddMin eDamAddMax tMdPct tMdRaw tSdPct tSdRaw tDamRaw tDamAddMin tDamAddMax
wMdPct wMdRaw wSdPct wSdRaw wDamRaw wDamAddMin wDamAddMax fMdPct fMdRaw fSdPct fSdRaw fDamRaw fDamAddMin fDamAddMax
aMdPct aMdRaw aSdPct aSdRaw aDamRaw aDamAddMin aDamAddMax nMdPct nMdRaw nSdPct nSdRaw nDamPct nDamRaw nDamAddMin
nDamAddMax damPct damRaw damAddMin damAddMax rMdPct rMdRaw rSdPct rDamPct rDamRaw rDamAddMin rDamAddMax critDamPct
spPct1Final spPct2Final spPct3Final spPct4Final healPct kb weakenEnemy slowEnemy rDefPct maxMana mainAttackRange""".split())
REVERSED_IDS = {"spPct1", "spRaw1", "spPct2", "spRaw2", "spPct3", "spRaw3", "spPct4", "spRaw4"}
ROLL_MODES = ["max", "avg", "min", "base"]
ROLL_MODE = "max"          # Wynnbuilder shows perfect (max) rolls; set by --rolls


def js_round(x: float) -> int:
    return math.floor(x + 0.5)


def id_round(x: float) -> int:
    r = js_round(x)
    return r if r != 0 else (1 if x > 0 else -1 if x < 0 else 0)


def roll_range(key: str, val: float, fixed: bool):
    """(min, max) roll for one ID, exactly as Wynnbuilder's expandItem."""
    if fixed or key not in ROLLED_IDS or val == 0:
        return val, val
    if (val > 0) != (key in REVERSED_IDS):
        return id_round(val * 0.3), id_round(val * 1.3)
    return id_round(val * 1.3), id_round(val * 0.7)


class Item:
    def __init__(self, raw: dict, parse_stats: bool = True):
        self.raw = raw
        self.name = raw.get("displayName") or raw["name"]
        self.type = raw.get("type")
        self.category = raw.get("category")
        self.wynn_rank = WYNN_EQUIP_RANK.get(self.type, 9)     # where it comes in Wynncraft's equip order
        self.tier = raw.get("tier") or "Normal"
        self.level = int(raw.get("lvl") or 0)
        self.class_req = (raw.get("classReq") or "").lower() or None
        self.reqs = [int(raw.get(s + "Req") or 0) for s in SKILLS]
        self.bonus = [int(raw.get(s) or 0) for s in SKILLS]
        self.set = (raw.get("sets") or [None])[0]
        self.drop = raw.get("drop")
        self.restrict = raw.get("restrict")
        self.quest = raw.get("quest")
        self.major_ids = raw.get("majorIds")
        fixed = bool(raw.get("fixID"))
        self.base_stats, self.min_stats, self.max_stats = {}, {}, {}
        # parse_stats=False: the caller sets every stat itself (crafted items), so they aren't worked out twice.
        for k, v in (raw.items() if parse_stats else ()):
            if k in NON_STAT_KEYS and k not in ("mainAttackRange",):
                continue
            if isinstance(v, dict) and v.get("static"):
                lo = hi = base = float(v.get("raw", 0))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                base = float(v)
                lo, hi = roll_range(k, base, fixed)
            else:
                continue
            self.base_stats[k], self.min_stats[k], self.max_stats[k] = base, float(lo), float(hi)
        self.atk_spd = raw.get("atkSpd")
        self.slots = int(raw.get("slots") or 0)
        self.crafted = False      # crafted: its skill point bonus only counts after everything is on
        self.custom = False
        self.link_code = None     # crafted/custom: how to write it back into a Wynnbuilder link
        self.ingred_powders = []  # crafted weapons: powders used as ingredients
        self.powders = []
        self.special = None
        self.armor_special = None
        self.damage = None
        if self.category == "weapon":
            self.damage = []
            for p in ELEM_PREFIX:
                lo, hi = (raw.get(p + "Dam") or "0-0").split("-")
                self.damage.append((float(lo), float(hi)))

    @property
    def stats(self) -> dict:
        if ROLL_MODE == "max":
            return self.max_stats
        if ROLL_MODE == "min":
            return self.min_stats
        if ROLL_MODE == "base":
            return self.base_stats
        cached = getattr(self, "_avg", None)
        if cached is None:
            cached = {k: (self.min_stats[k] + self.max_stats[k]) / 2 for k in self.max_stats}
            self._avg = cached
        return cached

    def short(self) -> str:
        bits = [f"Lv {self.level}", self.tier]
        if any(self.reqs):
            bits.append("needs " + " ".join(f"{SKILLS[i]} {r}" for i, r in enumerate(self.reqs) if r))
        if any(self.bonus):
            bits.append("gives " + " ".join(f"{SKILLS[i]} {b:+d}" for i, b in enumerate(self.bonus) if b))
        if self.major_ids:
            bits.append("major ID " + ", ".join(self.major_ids))
        notes = self.source_notes()
        if notes:
            bits.append("; ".join(notes))
        return ", ".join(bits)

    def source_notes(self) -> list:
        """Things that limit how you can get or use the item."""
        out = []
        if self.quest:
            out.append(f'needs quest "{self.quest}"')
        if self.restrict == "untradable":
            out.append("untradable (you must get it yourself)")
        elif self.restrict == "quest_item":
            out.append("quest item")
        if self.drop == "never" and not (self.crafted or self.custom):
            out.append("not a mob drop")
        return out


# ============================================================================== powders
# (min dmg, max dmg, % neutral converted, +def own element, -def previous element), tiers 1-7 per element ETWFA.
POWDER_STATS = [
    (4, 5, 17, 2, 1), (6, 7, 21, 5, 2), (7, 9, 25, 9, 3), (8, 9, 31, 14, 4), (9, 11, 38, 22, 7), (11, 12, 46, 29, 7), (12, 14, 52, 37, 12),
    (1, 8, 9, 2, 1), (1, 12, 11, 4, 1), (2, 14, 13, 8, 2), (2, 15, 17, 13, 3), (3, 17, 22, 20, 5), (4, 19, 28, 28, 6), (5, 21, 32, 36, 11),
    (3, 4, 13, 3, 1), (5, 6, 15, 6, 1), (6, 8, 17, 11, 3), (7, 8, 21, 16, 4), (8, 10, 26, 23, 6), (10, 13, 32, 32, 10), (11, 15, 38, 40, 15),
    (2, 5, 14, 3, 1), (4, 7, 16, 6, 1), (5, 9, 19, 10, 2), (6, 9, 24, 15, 3), (7, 11, 30, 22, 5), (9, 14, 37, 31, 9), (10, 16, 44, 39, 14),
    (2, 6, 11, 3, 1), (3, 9, 14, 6, 2), (4, 11, 17, 10, 3), (5, 11, 22, 16, 5), (7, 12, 28, 23, 7), (8, 15, 35, 30, 8), (9, 17, 42, 38, 13),
]
POWDER_ARMOR_HP = [5, 10, 20, 30, 45, 60, 75]
POWDER_LEVEL_REQ = [1, 5, 15, 25, 40, 55, 70]
POWDER_ELEMS = "etwfa"
POWDER_ELEM_NAMES = ["Earth", "Thunder", "Water", "Fire", "Air"]
# Weapon specials by element (index 0-4 = ETWFA): name, damage % per level, damage boost % per level, duration s.
WEAPON_SPECIALS = [
    ("Quake", [240, 280, 320, 360, 400, 440, 480], None, 0),
    ("Chain Lightning", [200, 225, 250, 275, 300, 325, 350], None, 0),
    ("Curse", None, [10, 12.5, 15, 17.5, 20, 22.5, 25], 4),
    ("Courage", [110, 125, 140, 155, 170, 185, 200], [10, 12.5, 15, 17.5, 20, 22.5, 25], 4),
    ("Wind Prison", None, [100, 125, 150, 175, 200, 225, 250], 5),
]
ARMOR_SPECIALS = ["Rage", "Kill Streak", "Concentration", "Endurance", "Dodge"]
SPECIAL_CHARGE_SECONDS = 5.0     # wiki: about 5 s of continuous main attacks fills a weapon special
SELF_CHARGE_SECONDS = 100 / 8     # Self-Sufficient major ID: specials fill by themselves at 8% a second

# Item special effects (major IDs) that Wynnbuilder's data only describes in words but that change numbers this
# helper works out. Written in Wynnbuilder's own ability format and used exactly like its modeled ones, so they only
# apply with the ability they change (dependencies) and land after it (merged after every tree ability, the item's
# weapon last). Only effects whose size the game's text states exactly; the rest stay
# uncounted and the report says so. (Self-Sufficient changes the weapon special timing; see SELF_CHARGE_SECONDS.)
HELPER_MAJIDS = {
    # Pas De Deux: "Frog Dance deals no damage" (the bigger Pool of Rejuvenation isn't counted)
    "AUBRIS_TEARS": [{"class": "Shaman", "base_abil": 5, "dependencies": [68], "properties": {}, "effects": [
        {"type": "add_spell_prop", "behavior": "overwrite", "base_spell": 2, "target_part": "Hop Damage",
         "multipliers": [0, 0, 0, 0, 0, 0]},
        {"type": "add_spell_prop", "behavior": "overwrite", "base_spell": 2, "target_part": "Total Damage",
         "hits": {"Hop Damage": 0}}]}],
    # Grand Design: "Storm Dance instead lets your Totems pull enemies": Storm Dance's +30% air on Aura is gone
    "ERGOSPHERE": [{"class": "Shaman", "base_abil": 9, "dependencies": [65], "properties": {}, "effects": [
        {"type": "add_spell_prop", "base_spell": 3, "target_part": "Single Wave", "multipliers": [0, 0, 0, 0, 0, -30]}]}],
}
HELPER_SPECIAL = {"SELFSUFFICIENT"}      # counted outside the ability format


def majid_abilities(data, mid) -> list:
    """A major ID's abilities: Wynnbuilder's, or the helper's for effects Wynnbuilder only describes."""
    mj = data.majids.get(mid) or {}
    return mj.get("abilities") or HELPER_MAJIDS.get(mid, [])


MAJID_STATUS_WORDS = {
    "counted": "counted (the way Wynnbuilder counts it)",
    "helper": "counted by this helper (Wynnbuilder doesn't count it)",
    "not counted": "not counted (movement, help for allies, only some of the time, or no exact size given)",
}


def special_effect_lines(data, items) -> list:
    """Report lines: each special effect (major ID) in this gear, what it does and whether its effect is counted."""
    out, seen = [], set()
    for it in items:
        for mid in (getattr(it, "major_ids", None) or []):
            if mid in seen:
                continue
            seen.add(mid)
            mj = data.majids.get(mid) or {}
            name = mj.get("displayName") or mid.replace("_", " ").title()
            desc = re.sub(r"\[(earth|thunder|water|fire|air|neutral)\]\s*", lambda m: m.group(1) + " ",
                          re.sub(r"<[^>]+>", " ", strip_mc(mj.get("description", "")))).strip()
            desc = " ".join(desc.split())
            out.append(f"     {name} ({it.name}): {desc} -> {MAJID_STATUS_WORDS[majid_status(data, mid)]}")
    if out:
        out.insert(0, "  Special effects (major IDs) in this build (one copy counts; they don't stack):")
    return out


def majid_status(data, mid) -> str:
    """'counted' (Wynnbuilder's numbers), 'helper' (counted by this helper; Wynnbuilder doesn't), or 'not counted'."""
    if (data.majids.get(mid) or {}).get("abilities"):
        return "counted"
    if mid in HELPER_MAJIDS or mid in HELPER_SPECIAL:
        return "helper"
    return "not counted"



class Powder:
    __slots__ = ("elem", "tier")

    def __init__(self, elem: int, tier: int):
        self.elem, self.tier = elem, tier

    @property
    def stats(self):
        return POWDER_STATS[self.elem * 7 + self.tier - 1]

    def __repr__(self):
        return f"{POWDER_ELEMS[self.elem]}{self.tier}"

    def label(self):
        return f"T{self.tier} {POWDER_ELEM_NAMES[self.elem]}"


def parse_powders(text: str) -> list:
    """'t6 t6', 'T6 thunder x2', 'e4,e4' -> [Powder, ...]"""
    out = []
    text = text.lower()
    names = {n.lower(): i for i, n in enumerate(POWDER_ELEM_NAMES)}
    for tok in re.finditer(r"(?:t(\d)\s*(earth|thunder|water|fire|air))|(?:(earth|thunder|water|fire|air)\s*t?(\d))|"
                           r"(?:\b([etwfa])\s*(\d)\b)", text):
        if tok.group(1):
            elem, tier = names[tok.group(2)], int(tok.group(1))
        elif tok.group(3):
            elem, tier = names[tok.group(3)], int(tok.group(4))
        else:
            elem, tier = POWDER_ELEMS.index(tok.group(5)), int(tok.group(6))
        count = 1
        tail = text[tok.end():tok.end() + 4]
        m = re.match(r"\s*[x×]\s*(\d)", tail)
        if m:
            count = int(m.group(1))
        out.extend(Powder(elem, max(1, min(7, tier))) for _ in range(count))
    return out


def powder_special(powders: list, slots: int):
    """(element index, level 1-7) of the special these powders unlock, or None (wiki rules)."""
    if slots < 2 or not powders:
        return None
    first = powders[0].elem
    tiers = sorted((p.tier for p in powders if p.elem == first and p.tier >= 4), reverse=True)
    if len(tiers) < 2:
        return None
    avg = sum(tiers[:2]) / 2
    level = int((avg - 4) * 2) + 1
    return first, max(1, min(7, level))


def weapon_with_powders(weapon, powders: list):
    """Copy of the weapon with powdered damage (Wynnbuilder's calc_weapon_powder)."""
    import copy
    w = copy.copy(weapon)
    powders = powders[: max(0, int(weapon.raw.get("slots") or 0))]
    w.powders = powders
    dmg = [list(x) for x in weapon.damage]
    neutral = dmg[0][:]
    order, info = [], {}
    for p in powders:
        lo, hi, conv, _, _ = p.stats
        if p.elem in info:
            info[p.elem][0] += conv / 100
            info[p.elem][1] += lo
            info[p.elem][2] += hi
        else:
            info[p.elem] = [conv / 100, lo, hi]
            order.append(p.elem)
    # Crafted weapons: powders used as ingredients add half their usual bonus (Wynnbuilder, game 2.1+).
    if weapon.crafted and not weapon.custom:
        for pid in weapon.ingred_powders:
            lo, hi, conv, _, _ = POWDER_STATS[pid]
            e = pid // 7
            add = [conv / 100 / 2, math.floor(lo / 2), math.floor(hi / 2)]
            if e in info:
                info[e] = [info[e][k] + add[k] for k in range(3)]
            else:
                info[e] = add
                order.append(e)
    for e in order:
        conv, lo, hi = info[e]
        dmin = min(neutral[0], conv * neutral[0])
        dmax = min(neutral[1], conv * neutral[1])
        neutral[0] -= dmin
        neutral[1] -= dmax
        dmg[e + 1][0] += dmin + lo
        dmg[e + 1][1] += dmax + hi
    dmg[0] = neutral
    w.damage = [tuple(x) for x in dmg]
    w.special = powder_special(powders, int(weapon.raw.get("slots") or 0))
    return w


def armor_with_powders(item, powders: list):
    """Copy of an armor piece with powder defenses and health applied."""
    import copy
    if not powders:
        return item
    a = copy.copy(item)
    powders = powders[: max(0, int(item.raw.get("slots") or 0))]
    a.powders = powders
    a.base_stats, a.min_stats, a.max_stats = dict(item.base_stats), dict(item.min_stats), dict(item.max_stats)
    a._avg = None
    for p in powders:
        _, _, _, plus, minus = p.stats
        own = POWDER_ELEMS[p.elem] + "Def"
        prev = POWDER_ELEMS[(p.elem + 4) % 5] + "Def"
        for d in (a.base_stats, a.min_stats, a.max_stats):
            d[own] = d.get(own, 0) + plus
            d[prev] = d.get(prev, 0) - minus
            d["hp"] = d.get("hp", 0) + POWDER_ARMOR_HP[p.tier - 1]
    a.armor_special = powder_special(powders, int(item.raw.get("slots") or 0))
    return a


def max_powder_tier(item_level: int, cap: int = 7) -> int:
    """Highest powder tier an item of this level accepts (crafted items: their top level)."""
    return max(t for t in range(1, 8) if POWDER_LEVEL_REQ[t - 1] <= item_level and t <= cap)


# ============================================================================== crafted and custom items
# Ports of Wynnbuilder's craft.js (crafted item stats), custom.js (custom item decoding) and the
# crafted-weapon rules in powders.js. Crafted items are read from Wynnbuilder links or from
# "CR-..." codes (Wynnbuilder's crafter); custom items from links or "CI-..." codes.

CRAFT_ATK_SPDS = ["SLOW", "NORMAL", "FAST"]
CRAFT_MAT_MULT = [0, 1, 1.25, 1.4]
# Powders used as crafting ingredients: (durability, skill requirement) by tier (Wynnbuilder load_ing.js).
POWDER_ING_INFO = [(-35, 0), (-52.5, 0), (-70, 10), (-91, 20), (-112, 28), (-133, 36), (-154, 44)]
ROMAN_NUM = ["I", "II", "III", "IV", "V", "VI", "VII"]
WEAPON_TYPES = ["wand", "spear", "bow", "dagger", "relik"]
ACCESSORY_TYPES = ["ring", "bracelet", "necklace"]
# Who can use the blank slot and powder ingredients (Wynnbuilder load_ing.js).
CRAFT_SKILLS_GEAR = ("ARMOURING", "TAILORING", "WEAPONSMITHING", "WOODWORKING", "JEWELING")
CRAFT_SKILLS_ALL = CRAFT_SKILLS_GEAR + ("COOKING", "ALCHEMISM", "SCRIBING")


def build_ingredient_table(ings: list) -> dict:
    """Ingredient id -> ingredient, including Wynnbuilder's 'No Ingredient' and the 35 powder ingredients."""
    zero_pos = {"left": 0, "right": 0, "above": 0, "under": 0, "touching": 0, "notTouching": 0}
    table = {4000: {"name": "No Ingredient", "id": 4000, "ids": {}, "posMods": dict(zero_pos), "lvl": 0, "tier": 0,
                    "skills": list(CRAFT_SKILLS_ALL),
                    "itemIDs": {"dura": 0, "strReq": 0, "dexReq": 0, "intReq": 0, "defReq": 0, "agiReq": 0}}}
    for e in range(5):
        for t in range(7):
            dura, req = POWDER_ING_INFO[t]
            item_ids = {"dura": dura, "strReq": 0, "dexReq": 0, "intReq": 0, "defReq": 0, "agiReq": 0}
            item_ids[SKILLS[e] + "Req"] = req
            pid = 7 * e + t
            table[4001 + pid] = {"name": f"{POWDER_ELEM_NAMES[e]} Powder {ROMAN_NUM[t]}", "id": 4001 + pid,
                                 "ids": {}, "posMods": dict(zero_pos), "itemIDs": item_ids, "lvl": 0, "tier": 0,
                                 "skills": list(CRAFT_SKILLS_GEAR), "isPowder": True, "pid": pid}
    for ing in ings:
        table[int(ing["id"])] = ing
    return table


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


_ING_PREPS = {}


def _ing_prep(ing):
    """The parts of an ingredient a craft reads, worked out once: position effects that aren't 0, requirement
    changes (not durability), and rolled IDs that count (in the ingredient's own order, so every sum is done in
    the same order as before)."""
    hit = _ING_PREPS.get(id(ing))
    if hit is not None and hit[0] is ing:
        return hit[1]
    pm = tuple((k, v) for k, v in (ing.get("posMods") or {}).items() if v)
    item_ids = tuple((k, v) for k, v in (ing.get("itemIDs") or {}).items() if k != "dura")
    rolled = []
    for key, rolls in (ing.get("ids") or {}).items():
        if key not in ROLLED_IDS and key not in SKILLS:
            continue
        mx = rolls.get("maximum") if isinstance(rolls, dict) else None
        if not mx:
            continue
        rolled.append((key, rolls.get("minimum", 0), mx))
    prep = (pm, item_ids, tuple(rolled), bool(ing.get("isPowder")))
    _ING_PREPS[id(ing)] = (ing, prep)
    return prep


def craft_effectiveness(preps) -> list:
    """Each grid position's effectiveness in % (3 rows x 2 columns, read left to right, top to bottom): 100,
    plus or minus what the position effects of the other ingredients give it (Wynnbuilder craft.js)."""
    eff = [[100, 100], [100, 100], [100, 100]]
    for n, prep in enumerate(preps):
        i, j = n // 2, n % 2
        for key, value in prep[0]:
            if key == "above":
                for k in range(i - 1, -1, -1):
                    eff[k][j] += value
            elif key == "under":
                for k in range(i + 1, 3):
                    eff[k][j] += value
            elif key == "left":
                if j == 1:
                    eff[i][0] += value
            elif key == "right":
                if j == 0:
                    eff[i][1] += value
            elif key == "touching":
                for k in range(3):
                    for l in range(2):
                        if (abs(k - i) == 1 and l == j) or (k == i and abs(l - j) == 1):
                            eff[k][l] += value
            elif key == "notTouching":
                for k in range(3):
                    for l in range(2):
                        if abs(k - i) > 1 or (abs(k - i) == 1 and abs(l - j) == 1):
                            eff[k][l] += value
    return [eff[0][0], eff[0][1], eff[1][0], eff[1][1], eff[2][0], eff[2][1]]


_EFF_MULT = {}


def make_crafted(data, recipe_id: int, mat_tiers, ing_ids, atk_spd: str = "SLOW"):
    """A crafted item with exactly the stats Wynnbuilder's crafter gives it (best rolls, like normal items)."""
    return _make_crafted(data, recipe_id, mat_tiers, ing_ids, atk_spd)


def _make_crafted(data, recipe_id: int, mat_tiers, ing_ids, atk_spd: str = "SLOW"):
    recipes = getattr(data, "recipes", {}) or {}
    ingtab = getattr(data, "ingredients", {}) or {}
    if recipe_id not in recipes:
        raise ValueError(f"unknown crafting recipe {recipe_id} (game data may be older than the link)")
    missing = [i for i in ing_ids if i not in ingtab]
    if missing:
        raise ValueError(f"unknown ingredient id(s) {missing} (game data may be older than the link)")
    recipe = recipes[recipe_id]
    ings = [ingtab[i] for i in ing_ids]
    rtype = recipe["type"].lower()
    rng = lambda key: ((recipe.get(key) or {}).get("minimum", 0), (recipe.get(key) or {}).get("maximum", 0))
    lvl_lo, lvl_hi = rng("lvl")
    low, high = rng("healthOrDamage")
    if rtype in ARMOR_SLOTS:
        category = "armor"
    elif rtype in WEAPON_TYPES:
        category = "weapon"
    elif rtype in ACCESSORY_TYPES:
        category = "accessory"
    else:
        raise ValueError(f"a crafted {rtype} is a consumable and can't be worn")
    slots = 0 if category == "accessory" else (1 if lvl_lo < 30 else 2 if lvl_lo < 70 else 3)
    amounts = [m["amount"] for m in recipe["materials"]]
    matmult = (CRAFT_MAT_MULT[mat_tiers[0]] * amounts[0] + CRAFT_MAT_MULT[mat_tiers[1]] * amounts[1]) / sum(amounts)
    fixed = defaultdict(float)          # stats that don't roll
    raw = {"name": f"Crafted {rtype.capitalize()} (Lv {lvl_lo}-{lvl_hi})", "type": rtype, "category": category,
           "tier": "Crafted", "lvl": lvl_lo, "slots": slots, "fixID": True}
    damage = None
    # Durability (Wynnbuilder craft.js): the recipe's, scaled by the materials, plus each ingredient's (not
    # affected by effectiveness). Below 1 the craft doesn't work in game.
    d_lo, d_hi = rng("durability")
    dura = [_js_round(d_lo * matmult), _js_round(d_hi * matmult)]
    for ing in ings:
        v = (ing.get("itemIDs") or {}).get("dura", 0) or 0
        dura = [x + v for x in dura]
    missing_dura = next((x for x in reversed(dura) if x < 1), None)
    dura = [0 if x < 1 else math.floor(x) for x in dura]
    if category == "weapon":
        ratio = {"SLOW": 2.05 / 1.5, "NORMAL": 1.0, "FAST": 2.05 / 2.5}[atk_spd]
        n_lo = math.floor(math.floor(low * matmult) * ratio)
        n_hi = math.floor(math.floor(high * matmult) * ratio)
        # Wynnbuilder computes spell and melee damage from the higher ("max roll") base.
        damage = [(math.floor(n_hi * 0.9), math.floor(n_hi * 1.1))] + [(0.0, 0.0)] * 5
        raw["atkSpd"] = atk_spd
    elif category == "armor":
        fixed["hp"] = math.floor(high * matmult)
    if category in ("armor", "accessory"):
        for ing in ings:
            if ing.get("isPowder"):
                p = Powder(ing["pid"] // 7, ing["pid"] % 7 + 1)
                _, _, _, plus, minus = p.stats
                fixed[POWDER_ELEMS[p.elem] + "Def"] += plus
                fixed[POWDER_ELEMS[(p.elem + 4) % 5] + "Def"] -= minus
    preps = [_ing_prep(ing) for ing in ings]
    flat = craft_effectiveness(preps)
    reqs = defaultdict(int)
    min_r, max_r = defaultdict(float), defaultdict(float)
    for n, ing in enumerate(ings):
        mult = _EFF_MULT.get(flat[n])
        if mult is None:                            # Wynnbuilder: (eff/100).toFixed(2)
            mult = _EFF_MULT[flat[n]] = float(f"{flat[n] / 100:.2f}")
        _pm, item_ids, rolled, is_powder = preps[n]
        for key, value in item_ids:
            reqs[key] += _js_round(value) if is_powder else _js_round(value * mult + 1e-9)
        for key, mn, mx in rolled:
            lo, hi = sorted([math.floor(mn * mult), math.floor(mx * mult)])
            min_r[key] += lo
            max_r[key] += hi
    for s in SKILLS:
        raw[s + "Req"] = reqs.get(s + "Req", 0)
        raw[s] = int(max_r.get(s, 0))
    it = Item(raw, parse_stats=False)
    it.crafted = True
    it.drop = None
    it.level_high = lvl_hi
    it.min_stats = {k: v for k, v in min_r.items() if k not in SKILLS}
    it.max_stats = {k: v for k, v in max_r.items() if k not in SKILLS}
    for k, v in fixed.items():
        it.min_stats[k] = it.min_stats.get(k, 0) + v
        it.max_stats[k] = it.max_stats.get(k, 0) + v
    it.base_stats = dict(it.max_stats)
    it._avg = None
    if damage is not None:
        it.damage = damage
        it.ingred_powders = [ing["pid"] for ing in ings if ing.get("isPowder")]
    it.ingredients = [ing["name"] for ing in ings if ing["id"] != 4000]
    it.link_code = ("craft", recipe_id, list(mat_tiers), list(ing_ids), atk_spd)
    it.durability, it.missing_durability = dura, missing_dura
    it.recipe_name = recipe.get("name", "")
    it.craft_problems = craft_problems(recipe, ings, missing_dura)
    return it


CRAFTER_URL = "https://wynnbuilder.github.io/crafter/#"


def craft_code(it) -> str:
    """The code Wynnbuilder's crafter opens ("CR-..." without the prefix)."""
    bits = Bits()
    encode_craft_bits(bits, it)
    return bits.to_b64()


def ingredient_sources(data, name: str, limit: int = 3) -> str:
    ing = next((g for g in data.ingredients.values() if g.get("name") == name or g.get("displayName") == name), None)
    mobs = [m.get("name") for m in (ing or {}).get("droppedBy") or [] if m.get("name")]
    if not mobs:
        return "no mob listed in the game data"
    mobs = list(dict.fromkeys(mobs))
    return ", ".join(mobs[:limit]) + (f" and {len(mobs) - limit} more" if len(mobs) > limit else "")


def print_crafts(data, pieces, info):
    """How to make the crafted pieces of a build: recipe, materials, the grid, where the ingredients drop."""
    import wynn_craft as wc
    crafts, seen = [], set()
    for label, it in pieces:
        if not getattr(it, "crafted", False) or not getattr(it, "link_code", None) or it.link_code[0] != "craft":
            continue
        code = craft_code(it)
        if code in seen:                                  # the same crafted ring on both hands: once
            crafts = [(l + " and " + label if c == code else l, x, c) for l, x, c in crafts]
            continue
        seen.add(code)
        crafts.append((label, it, code))
    if not crafts:
        return
    wc.remember_names(data)
    print("  Crafted pieces (designed for this build):")
    for label, it, code in crafts:
        rec = data.recipes.get(it.link_code[1], {})
        prof = (rec.get("skill") or "").capitalize()
        lo, hi = (rec.get("lvl") or {}).get("minimum", 0), (rec.get("lvl") or {}).get("maximum", 0)
        print(f"   {label}: {prof} recipe level {lo}-{hi} (to craft it yourself: {prof} level {hi}; or ask a crafter)")
        for line in wc.describe(it):
            print("     " + line)
        names = list(dict.fromkeys(n for n in it.ingredients if "Powder" not in n))
        for n in names:
            print(f"     {n}: dropped by {ingredient_sources(data, n)}")
        print(f"     Durability {it.durability[0]}-{it.durability[1]}. Open it in Wynnbuilder's crafter: {CRAFTER_URL}{code}")
    if info.get("found_by") and info.get("after", 0) > info.get("before", 0) > 0:
        print(f"  The crafted pieces add +{(info['after'] / info['before'] - 1) * 100:.1f}% over the best build "
              "without them. (\"No crafted items\" leaves them out.)")
    print("  Crafted stats use the best rolls, like every other item here (Wynnbuilder does the same).")


def craft_problems(recipe, ings, missing_dura=None) -> list:
    """Why this craft can't be made in game (Wynnbuilder's crafter warnings), or [] if it can."""
    out = []
    skill = recipe.get("skill", "")
    top = (recipe.get("lvl") or {}).get("maximum", 0)
    for ing in ings:
        if skill not in (ing.get("skills") or ()):
            out.append(f"{ing['name']} can't be used for {skill.capitalize()}")
        if (ing.get("lvl") or 0) > top:
            out.append(f"{ing['name']} is too high level for a level {top} recipe")
    if missing_dura is not None:
        out.append(f"needs {-missing_dura:g} more durability")
    return out


def decode_craft_bits(data, cur):
    """Read one crafted item (current binary format) at the cursor; the cursor ends after its padding."""
    start = cur.i
    if cur.take(1):
        raise ValueError("this crafted item uses Wynnbuilder's old format; open it in Wynnbuilder and save again")
    cur.take(7)                                           # crafted encoding version
    ing_ids = [cur.take(12) for _ in range(6)]
    recipe_id = cur.take(12)
    mats = [cur.take(3) + 1 for _ in range(2)]
    rtype = ((getattr(data, "recipes", {}) or {}).get(recipe_id) or {}).get("type", "").lower()
    atk = "SLOW"
    if rtype in WEAPON_TYPES:
        v = cur.take(4)
        atk = CRAFT_ATK_SPDS[v] if v < 3 else "SLOW"
    cur.i += 6 - ((cur.i - start) % 6)                    # padding (Wynnbuilder always skips 1-6 bits)
    return make_crafted(data, recipe_id, mats, ing_ids, atk)


def encode_craft_bits(bits, item):
    _kind, recipe_id, mats, ing_ids, atk = item.link_code
    start = len(bits.bits)
    bits.append(0, 1)
    bits.append(2, 7)
    for i in ing_ids:
        bits.append(i, 12)
    bits.append(recipe_id, 12)
    for m in mats:
        bits.append(m - 1, 3)
    if item.category == "weapon":
        bits.append(CRAFT_ATK_SPDS.index(atk), 4)
    bits.append(0, 6 - ((len(bits.bits) - start) % 6))


def crafted_from_code(data, code: str):
    """'CR-...' from Wynnbuilder's crafter (current or old format)."""
    h = code.strip()[3:]
    if not h:
        raise ValueError("empty crafted item code")
    if B64_DIGITS.index(h[0]) & 1:                        # old text format: '1' + 2 chars per ingredient ...
        body = h[1:]
        toint = lambda s: functools.reduce(lambda a, c: (a << 6) + B64_DIGITS.index(c), s, 0)
        ing_ids = [toint(body[2 * i:2 * i + 2]) for i in range(6)]
        recipe_id = toint(body[12:14])
        t = toint(body[14:15])
        mats = [3 if t % 3 == 0 else t % 3, math.floor((t - 0.5) / 3) + 1]
        a = toint(body[15:]) if body[15:] else 0
        return make_crafted(data, recipe_id, mats, ing_ids, CRAFT_ATK_SPDS[a] if a < 3 else "SLOW")
    return decode_craft_bits(data, BitCursor(h))


# Custom items (Wynnbuilder custom.js). Never reorder these lists: positions are stored in links.
CI_SAVE_ORDER = ["name", "lore", "tier", "set", "slots", "type", "material", "drop", "quest",
                 "nDam", "fDam", "wDam", "aDam", "tDam", "eDam", "atkSpd", "hp",
                 "fDef", "wDef", "aDef", "tDef", "eDef", "lvl", "classReq",
                 "strReq", "dexReq", "intReq", "defReq", "agiReq", "str", "dex", "int", "agi", "def", "id",
                 "skillpoints", "reqs", "nDam_", "fDam_", "wDam_", "aDam_", "tDam_", "eDam_",
                 "majorIds", "hprPct", "mr", "sdPct", "mdPct", "ls", "ms", "xpb", "lb",
                 "ref", "thorns", "expd", "spd", "atkTier", "poison", "hpBonus", "spRegen", "eSteal", "hprRaw",
                 "sdRaw", "mdRaw", "fDamPct", "wDamPct", "aDamPct", "tDamPct", "eDamPct",
                 "fDefPct", "wDefPct", "aDefPct", "tDefPct", "eDefPct",
                 "spPct1", "spRaw1", "spPct2", "spRaw2", "spPct3", "spRaw3", "spPct4", "spRaw4", "rSdRaw",
                 "sprint", "sprintReg", "jh", "lq", "gXp", "gSpd", "durability", "duration", "charges", "maxMana",
                 "critDamPct", "nSdRaw", "eSdRaw", "tSdRaw", "wSdRaw", "fSdRaw", "aSdRaw",
                 "rSdPct", "nSdPct", "eSdPct", "tSdPct", "wSdPct", "fSdPct", "aSdPct",
                 "rMdRaw", "nMdRaw", "eMdRaw", "tMdRaw", "wMdRaw", "fMdRaw", "aMdRaw",
                 "rMdPct", "nMdPct", "eMdPct", "tMdPct", "wMdPct", "fMdPct", "aMdPct",
                 "damRaw", "rDamRaw", "nDamRaw", "eDamRaw", "tDamRaw", "wDamRaw", "fDamRaw", "aDamRaw",
                 "damPct", "rDamPct", "nDamPct", "healPct", "mainAttackRange", "kb", "weakenEnemy", "slowEnemy",
                 "rDefPct"]
CI_STRINGS = {"name", "lore", "tier", "set", "type", "material", "drop", "quest", "majorIds", "classReq", "atkSpd",
              "displayName", "nDam", "fDam", "wDam", "aDam", "tDam", "eDam", "nDam_", "fDam_", "wDam_", "aDam_",
              "tDam_", "eDam_", "durability", "duration"}
WB_ALL_TYPES = ["Helmet", "Chestplate", "Leggings", "Boots", "Ring", "Bracelet", "Necklace", "Wand", "Spear", "Bow",
                "Dagger", "Relik", "Potion", "Scroll", "Food", "WeaponTome", "ArmorTome", "GuildTome", "LootrunTome",
                "GatherXpTome", "DungeonXpTome", "MobXpTome"]
WB_TIERS = ["Normal", "Unique", "Rare", "Legendary", "Fabled", "Mythic", "Set", "Crafted"]
WB_CLASSES = ["Warrior", "Assassin", "Mage", "Archer", "Shaman"]


def _bootstring_decode(s: str) -> str:
    """Wynnbuilder's BootstringEncoder(0, 1, 52, 104, 700, 38, '-').decode, used for custom item text."""
    base = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    b, tmin, tmax, damp, skew = len(base), 1, 52, 700, 38

    def threshold(i, bias):
        return min(max(b * (i + 1) - bias, tmin), tmax)

    def adapt(delta, length, first):
        delta = delta // damp if first else delta >> 1
        delta //= length
        k = 0
        while delta > ((b - tmin) * tmax) >> 1:
            delta //= b - tmin
            k += b
        return k + ((b - tmin + 1) * delta) // (delta + skew)

    idx = s.rfind("-")
    lit, deltas = ("", s) if idx < 0 else (s[:idx], s[idx + 1:])
    out, bias, n, i, first = list(lit), 104, 0, 0, True
    while deltas:
        pos, w, res = 0, 1, 0
        for ch in deltas:
            d = base.index(ch)
            res += d * w
            t = threshold(pos, bias)
            if d < t:
                break
            w *= b - t
            pos += 1
        deltas = deltas[pos + 1:]
        steps = res + (0 if first else 1)
        n += (i + steps) // (len(out) + 1)
        i = (i + steps) % (len(out) + 1)
        out.insert(i, chr(n))
        bias = adapt(res, len(out), first)
        first = False
    return "".join(out)


def decode_custom_bits(cur, end: int):
    """Read a custom item (current binary format) from cursor position to `end`."""
    start = cur.i
    if cur.take(1):
        raise ValueError("this custom item uses Wynnbuilder's old format; open it in Wynnbuilder and save again")
    cur.take(7)
    fixed = cur.take(1) == 0
    sext = lambda v, n: v - (1 << n) if v >= 1 << (n - 1) else v
    vals, mins, maxs = {}, {}, {}
    while cur.i + 10 <= end:
        idx = cur.take(10)
        key = CI_SAVE_ORDER[idx] if idx < len(CI_SAVE_ORDER) else None
        if key in ROLLED_IDS:
            n = cur.take(5) + 1
            lo = sext(cur.take(n), n)
            hi = lo if fixed else sext(cur.take(n), n)
            mins[key], maxs[key] = lo, hi
        elif key in CI_STRINGS:
            if key == "type":
                vals[key] = WB_ALL_TYPES[cur.take(4)]
            elif key == "tier":
                vals[key] = WB_TIERS[cur.take(4)]
            elif key == "atkSpd":
                vals[key] = ATTACK_SPEEDS[cur.take(4)]
            elif key == "classReq":
                vals[key] = WB_CLASSES[cur.take(4)]
            else:
                length = cur.take(16)
                text = "".join(B64_DIGITS[cur.take(6)] for _ in range(length))
                try:
                    vals[key] = _bootstring_decode(text)
                except Exception:
                    vals[key] = text
        else:
            n = cur.take(5) + 1
            v = sext(cur.take(n), n)
            if key:
                vals[key] = v
    code = "".join(B64_DIGITS[sum(cur.bits[k + j] << j for j in range(6) if k + j < len(cur.bits))]
                   for k in range(start, end, 6))
    rtype = str(vals.get("type", "")).lower()
    category = ("armor" if rtype in ARMOR_SLOTS else "accessory" if rtype in ACCESSORY_TYPES
                else "weapon" if rtype in WEAPON_TYPES else "other")
    raw = {"name": vals.get("name") or "Custom Item", "type": rtype, "category": category,
           "tier": vals.get("tier") or "Normal", "lvl": vals.get("lvl", 0), "slots": vals.get("slots", 0),
           "classReq": vals.get("classReq"), "fixID": True}
    if vals.get("set"):
        raw["sets"] = [vals["set"]]
    if vals.get("majorIds"):
        raw["majorIds"] = [vals["majorIds"]]
    for s in SKILLS:
        raw[s + "Req"] = vals.get(s + "Req", 0)
        raw[s] = vals.get(s, 0)
    for k, v in vals.items():
        if isinstance(v, int) and k not in raw and k not in ("id", "durability", "duration", "charges"):
            raw[k] = v
    if category == "weapon":
        raw["atkSpd"] = vals.get("atkSpd", "NORMAL")
        for p in ELEM_PREFIX:
            raw[p + "Dam"] = vals.get(p + "Dam") or "0-0"
    it = Item(raw)
    it.custom = True
    it.crafted = raw["tier"] == "Crafted"
    it.drop = None
    for k in maxs:
        it.min_stats[k], it.max_stats[k], it.base_stats[k] = float(mins[k]), float(maxs[k]), float(maxs[k])
    it._avg = None
    it.link_code = ("custom", code)
    return it


def custom_from_code(code: str):
    h = code.strip()[3:]
    if not h:
        raise ValueError("empty custom item code")
    cur = BitCursor(h)
    return decode_custom_bits(cur, len(cur.bits))


# ============================================================================== skill points

_SP_CACHE: dict = {}


_SIG_IDS: dict = {}
_SIG_EPOCH = os.urandom(8)      # this process's numbering; items copied in from another process carry theirs


def _sp_sig(it):
    """A small number standing for an item's skill point signature (requirements, bonuses, crafted), worked
    out once per item. The solver reads nothing else, so items with equal signatures share answers.
    The number is only trusted in the process that made it: items sent between the search's worker
    processes carry the sender's numbering, which means something else here, so it is worked out again."""
    s = it.__dict__.get("_spsig") if hasattr(it, "__dict__") else None
    if s is not None and s[0] is _SIG_EPOCH:
        return s[1]
    sig = (tuple(it.reqs), tuple(it.bonus), bool(getattr(it, "crafted", False)))
    n = _SIG_IDS.get(sig)
    if n is None:
        n = _SIG_IDS[sig] = len(_SIG_IDS)
    try:
        it._spsig = (_SIG_EPOCH, n)
    except Exception:
        pass
    return n


def _sp_key(armor, weapon):
    """Cache key for the skill points a gear set needs (items in order, weapon). Reads each item's remembered
    number directly when it was made in this process (the common case), else works it out."""
    ep = _SIG_EPOCH
    out = []
    for a in armor:
        if a is not None:
            s = a.__dict__.get("_spsig")
            out.append(s[1] if s is not None and s[0] is ep else _sp_sig(a))
    if weapon is None:
        return tuple(out), None
    s = weapon.__dict__.get("_spsig")
    return tuple(out), (s[1] if s is not None and s[0] is ep else _sp_sig(weapon))


# Wynncraft puts gear on in this order (boots, leggings, chestplate, helmet, ring, ring, bracelet, necklace), and so
# does Wynnbuilder (builder_graph.js: wynn_equip). With items that take skill points away, the order decides how many
# points you need, so every skill point calculation goes through the gear in this order, whatever order it's given in.
WYNN_EQUIP_RANK = {"boots": 0, "leggings": 1, "chestplate": 2, "helmet": 3, "ring": 4, "bracelet": 6, "necklace": 7}


def _wynn_sorted(arm: list) -> list:
    """The gear (no empty slots) in Wynncraft's equip order; the two rings keep their own order."""
    r = [getattr(a, "wynn_rank", 9) for a in arm]
    if all(r[i] <= r[i + 1] for i in range(len(r) - 1)):
        return arm
    return [arm[i] for i in sorted(range(len(arm)), key=r.__getitem__)]       # (sorted keeps ties in order)


def solve_skill_points(armor: list, weapon):
    """Cached wrapper: the same gear always needs the same points, and searches ask for it many times."""
    arm = _wynn_sorted([a for a in armor if a is not None])
    key = _sp_key(arm, weapon)
    hit = _SP_CACHE.get(key)
    if hit is None:
        assigned, totals, order = _solve_skill_points_uncached(arm, weapon)
        idx, used = [], set()
        for o in order:
            if o is weapon and weapon is not None and -1 not in used:
                idx.append(-1)
                used.add(-1)
                continue
            j = next((i for i, a in enumerate(arm) if a is o and i not in used), None)
            if j is None:
                j = next((i for i, a in enumerate(arm) if i not in used and a.name == getattr(o, "name", None)), 0)
            idx.append(j)
            used.add(j)
        hit = (tuple(assigned), tuple(totals), tuple(idx))
        if len(_SP_CACHE) > 300000:
            _SP_CACHE.clear()
        _SP_CACHE[key] = hit
    assigned, totals, idx = hit
    return list(assigned), list(totals), [weapon if i == -1 else arm[i] for i in idx]


def _solve_skill_points_uncached(armor: list, weapon):
    """Minimum skill points to assign (see _solve_skill_points_reference for the rules). Same answers as the
    reference, including which of several equally cheap equip orders is picked, just faster."""
    armor = _wynn_sorted([a for a in armor if a is not None])
    if any(b < 0 for a in armor for b in a.bonus) or any(a.crafted for a in armor):
        return _solve_skill_points_exact_fast(armor, weapon)
    return _solve_plain_fast(armor, weapon)


def _solve_plain_fast(armor: list, weapon):
    """The reference's depth-first search over equip orders (no negative bonuses), with the per-order
    lower bound worked out once per set of remaining items instead of at every step."""
    MAXA = MAX_ASSIGNED_PER_SKILL
    with_bonus = [a for a in armor if any(a.bonus)]
    no_bonus = [a for a in armor if not any(a.bonus)]
    n = len(with_bonus)
    NEG = -10 ** 9
    reqs = [tuple((k, it.reqs[k]) for k in range(5) if it.reqs[k] > 0) for it in with_bonus]
    bons = [tuple(it.bonus) for it in with_bonus]
    sigs = [(tuple(it.reqs), tuple(it.bonus)) for it in with_bonus]
    tail = no_bonus + ([weapon] if weapon is not None else [])
    T = [max([it.reqs[k] for it in tail if it.reqs[k] > 0] or [NEG]) for k in range(5)]
    nb_reqs = [tuple((k, it.reqs[k]) for k in range(5) if it.reqs[k] > 0) for it in no_bonus]
    w_reqs = tuple((k, weapon.reqs[k]) for k in range(5) if weapon.reqs[k] > 0) if weapon is not None else ()
    fix = [(k, it.reqs[k] + it.bonus[k]) for it in armor for k in range(5) if it.reqs[k] > 0]
    wb = list(weapon.bonus) if weapon is not None else None
    best = [None, None, None, None]     # key, assigned, final, order
    seed_total = None
    _lb, _ub = _sp_bounds(armor, weapon)
    if _ub is not None and max(_ub) <= MAXA:
        seed_total = sum(_ub)
    bound_cache = {}

    def bound_consts(used):
        c = bound_cache.get(used)
        if c is None:
            rb = [0, 0, 0, 0, 0]
            for j in range(n):
                if not used & (1 << j):
                    b = bons[j]
                    for k in range(5):
                        rb[k] += b[k]
            M = list(T)
            for j in range(n):          # every item's requirement still has to hold with everything on
                b = bons[j]
                for k, r in reqs[j]:
                    if r + b[k] > M[k]:
                        M[k] = r + b[k]
            c = [M[k] - rb[k] if M[k] > NEG else NEG for k in range(5)]
            bound_cache[used] = c
        return c

    def need(used, cur):
        c = bound_consts(used)
        t = 0
        for k in range(5):
            d = c[k] - cur[k]
            if d > 0:
                t += d
        return t

    def finish(order, assigned, cur, total):
        assigned, cur = assigned[:], cur[:]
        for rq in nb_reqs:
            for k, r in rq:
                if cur[k] < r:
                    assigned[k] += r - cur[k]
                    total += r - cur[k]
                    cur[k] = r
        for k, r in w_reqs:
            if cur[k] < r:
                assigned[k] += r - cur[k]
                total += r - cur[k]
                cur[k] = r
        for k, r in fix:
            if cur[k] < r:
                assigned[k] += r - cur[k]
                total += r - cur[k]
                cur[k] = r
        key = (0 if max(assigned) <= MAXA else 1, total)
        if best[0] is None or key < best[0]:
            best[0], best[1] = key, assigned
            best[2] = [cur[k] + wb[k] for k in range(5)] if wb is not None else cur[:]
            best[3] = list(order)

    def dfs(order, used, assigned, cur, total):
        bk = best[0]
        if bk is not None and bk[0] == 0:
            if total >= bk[1] or max(assigned) > MAXA:
                return
            if total + need(used, cur) >= bk[1]:
                return
        elif bk is None and seed_total is not None:
            if max(assigned) > MAXA or total + need(used, cur) > seed_total:
                return
        if len(order) == n:
            finish(order, assigned, cur, total)
            return
        tried = set()
        for j in range(n):
            if used & (1 << j):
                continue
            sg = sigs[j]
            if sg in tried:
                continue
            tried.add(sg)
            a2, c2, t2 = assigned[:], cur[:], total
            for k, r in reqs[j]:
                if c2[k] < r:
                    a2[k] += r - c2[k]
                    t2 += r - c2[k]
                    c2[k] = r
            b = bons[j]
            for k in range(5):
                c2[k] += b[k]
            order.append(j)
            dfs(order, used | (1 << j), a2, c2, t2)
            order.pop()

    if armor:
        dfs([], 0, [0] * 5, [0] * 5, 0)
    if best[0] is None:  # no armor at all
        assigned = [max(0, weapon.reqs[i]) for i in range(5)] if weapon else [0] * 5
        final = [assigned[i] + (weapon.bonus[i] if weapon else 0) for i in range(5)]
        return assigned, final, [weapon] if weapon else []
    return best[1], best[2], [with_bonus[j] for j in best[3]] + no_bonus + ([weapon] if weapon else [])


def _solve_skill_points_reference(armor: list, weapon):
    """
    Reference version (kept for checking the fast one).
    Minimum skill points you must assign so every item works, following Wynncraft's rules
    (as implemented by Wynnbuilder): items are equipped one at a time and each must meet its
    requirements when equipped; an item's own bonus never counts toward its own requirement;
    the weapon is equipped last and its bonus doesn't help armor requirements.
    Returns (assigned[5], total_after_all_items[5], equip_order) for the cheapest order. The gear is gone through in
    Wynncraft's equip order (boots first), as the game and Wynnbuilder do.
    """
    armor = _wynn_sorted([a for a in armor if a is not None])
    if any(b < 0 for a in armor for b in a.bonus) or any(a.crafted for a in armor):
        return _solve_skill_points_exact(armor, weapon)
    with_bonus = [a for a in armor if any(a.bonus)]
    no_bonus = [a for a in armor if not any(a.bonus)]   # these are always best equipped last
    best = None
    n = len(with_bonus)

    def finish(order_wb, assigned, cur):
        nonlocal best
        assigned, cur = assigned[:], cur[:]
        for it in no_bonus:
            for i in range(5):
                if it.reqs[i] > 0 and cur[i] < it.reqs[i]:
                    d = it.reqs[i] - cur[i]
                    assigned[i] += d
                    cur[i] += d
        if weapon is not None:
            for i in range(5):
                if weapon.reqs[i] > 0 and cur[i] < weapon.reqs[i]:
                    d = weapon.reqs[i] - cur[i]
                    assigned[i] += d
                    cur[i] += d
        # Final check: with everything on, each item must still meet its requirement
        # without counting its own bonus (an item can "pop off" after a negative bonus).
        for it in armor:
            for i in range(5):
                if it.reqs[i] > 0 and cur[i] < it.reqs[i] + it.bonus[i]:
                    d = it.reqs[i] + it.bonus[i] - cur[i]
                    assigned[i] += d
                    cur[i] += d
        under = all(a <= MAX_ASSIGNED_PER_SKILL for a in assigned)
        key = (0 if under else 1, sum(assigned))
        if best is None or key < best[0]:
            final = cur[:]
            if weapon is not None:
                final = [final[i] + weapon.bonus[i] for i in range(5)]
            best = (key, assigned, final, list(order_wb) + no_bonus + ([weapon] if weapon else []))

    # Depth-first over equip orders, in the same order itertools.permutations would visit them, skipping
    # orders that can't beat the best found so far (assigned points only ever go up) and repeated identical
    # items. Gives exactly the same result as trying every order, much faster.
    # A good order found up front lets hopeless orders be skipped early. Pruning against it is strict (>),
    # so among equally cheap orders the first in permutation order still wins, exactly as before.
    seed_total = None
    _lb, _ub = _sp_bounds(armor, weapon)
    if _ub is not None and max(_ub) <= MAX_ASSIGNED_PER_SKILL:
        seed_total = sum(_ub)
    rem_bonus_cache = {}

    def remaining_need(used, cur):
        """Points still needed at least, whatever order the remaining items go in."""
        key = used
        if key not in rem_bonus_cache:
            rem = [with_bonus[j] for j in range(n) if not used & (1 << j)]
            rem_bonus_cache[key] = (rem, [sum(max(0, it.bonus[k]) for it in rem) for k in range(5)])
        rem, rb = rem_bonus_cache[key]
        need = 0
        for k in range(5):
            m = 0
            for it in rem:
                if it.reqs[k] > 0:
                    m = max(m, it.reqs[k] - cur[k] - (rb[k] - max(0, it.bonus[k])))
            for it in no_bonus + ([weapon] if weapon is not None else []):
                if it.reqs[k] > 0:
                    m = max(m, it.reqs[k] - cur[k] - rb[k])
            need += max(0, m)
        return need

    def dfs(order_wb, used, assigned, cur):
        if best is not None and best[0][0] == 0:
            if sum(assigned) >= best[0][1] or any(a > MAX_ASSIGNED_PER_SKILL for a in assigned):
                return
            if sum(assigned) + remaining_need(used, cur) >= best[0][1]:
                return
        elif best is None and seed_total is not None:
            if any(a > MAX_ASSIGNED_PER_SKILL for a in assigned) or \
                    sum(assigned) + remaining_need(used, cur) > seed_total:
                return
        if len(order_wb) == n:
            finish(order_wb, assigned, cur)
            return
        tried = set()
        for j in range(n):
            if used & (1 << j):
                continue
            it = with_bonus[j]
            sig = (tuple(it.reqs), tuple(it.bonus))
            if sig in tried:
                continue
            tried.add(sig)
            a2, c2 = assigned[:], cur[:]
            for i in range(5):
                if it.reqs[i] > 0 and c2[i] < it.reqs[i]:
                    d = it.reqs[i] - c2[i]
                    a2[i] += d
                    c2[i] += d
            for i in range(5):
                c2[i] += it.bonus[i]
            order_wb.append(it)
            dfs(order_wb, used | (1 << j), a2, c2)
            order_wb.pop()

    if armor:
        dfs([], 0, [0] * 5, [0] * 5)
    if best is None:  # no armor at all
        assigned = [max(0, weapon.reqs[i]) for i in range(5)] if weapon else [0] * 5
        final = [assigned[i] + (weapon.bonus[i] if weapon else 0) for i in range(5)]
        return assigned, final, [weapon] if weapon else []
    return best[1], best[2], best[3]


def _solve_skill_points_exact_fast(armor: list, weapon):
    """
    Same search as _solve_skill_points_exact (same orders visited, same rejections, same winner among ties),
    written for speed: requirements are pre-listed per item, and branches that provably can't beat the
    best order found so far are skipped (points only ever get added, and ties are never skipped).
    tests/speed_check.py compares the two on thousands of gear sets.
    """
    n = len(armor)
    if not n:
        assigned = [max(0, weapon.reqs[i]) for i in range(5)] if weapon else [0] * 5
        return assigned, [assigned[i] + (weapon.bonus[i] if weapon else 0) for i in range(5)], [weapon]
    MAXA = MAX_ASSIGNED_PER_SKILL
    reqs = [tuple((k, it.reqs[k]) for k in range(5) if it.reqs[k] > 0) for it in armor]
    bon = [None if it.crafted or not any(it.bonus) else tuple(it.bonus) for it in armor]
    fixr = [tuple((k, it.reqs[k] if it.crafted else it.reqs[k] + it.bonus[k]) for k in range(5) if it.reqs[k] > 0)
            for it in armor]
    wreq = tuple((k, weapon.reqs[k]) for k in range(5) if weapon.reqs[k] > 0) if weapon is not None else ()
    pos = [[max(0, b) for b in it.bonus] if not it.crafted else [0] * 5 for it in armor]
    crafted_bonus = [0] * 5
    for c in armor:
        if c.crafted:
            for k in range(5):
                crafted_bonus[k] += c.bonus[k]
    wb = list(weapon.bonus) if weapon is not None else [0] * 5
    best = [None, None, None, None]      # key, applied, final, order (indices)

    def can_equip(sp, i):
        for k, r in reqs[i]:
            if sp[k] < r:
                return False
        return True

    def hopeless(applied, totals, total_applied, equipped, remains):
        bk = best[0]
        p = [0, 0, 0, 0, 0]
        for idx in remains:
            pb = pos[idx]
            p[0] += pb[0]; p[1] += pb[1]; p[2] += pb[2]; p[3] += pb[3]; p[4] += pb[4]
        m = [0, 0, 0, 0, 0]
        for k, r in wreq:
            d = r - totals[k] - p[k]
            if d > m[k]:
                m[k] = d
        for idx in remains:
            pb = pos[idx]
            for k, r in reqs[idx]:
                d = r - totals[k] - p[k] + pb[k]
                if d > m[k]:
                    m[k] = d
        for idx in equipped:
            for k, r in fixr[idx]:
                d = r - totals[k] - p[k]
                if d > m[k]:
                    m[k] = d
        over = False
        for k in range(5):
            if applied[k] + m[k] > MAXA:
                over = True
                break
        lb = total_applied + m[0] + m[1] + m[2] + m[3] + m[4]
        if bk[0] == 0:
            return over or lb >= bk[1]
        return over and lb >= bk[1]

    def recurse(applied, totals, total_applied, skipped_states, prior_skipped, equipped, remains):
        if best[0] is not None and len(remains) < n and hopeless(applied, totals, total_applied, equipped, remains):
            return
        if len(remains) == 1:
            last = remains[0]
            sp = list(totals)
            deltas = [0, 0, 0, 0, 0]
            for k, r in reqs[last]:
                if sp[k] < r:
                    deltas[k] += r - sp[k]
                    sp[k] = r
            b = bon[last]
            if b is not None:
                for k in range(5):
                    sp[k] += b[k]
            for k, r in wreq:
                if sp[k] < r:
                    deltas[k] += r - sp[k]
                    sp[k] = r
            for o in range(n):
                for k, r in fixr[o]:
                    if r > sp[k]:
                        deltas[k] += r - sp[k]
                        sp[k] = r
            for j, idx in enumerate(prior_skipped):
                s = skipped_states[j]
                ok = True
                for k, r in reqs[idx]:
                    if s[k] + deltas[k] < r:
                        ok = False
                        break
                if ok:
                    return
            app = [applied[k] + deltas[k] for k in range(5)]
            tot = total_applied + deltas[0] + deltas[1] + deltas[2] + deltas[3] + deltas[4]
            key = (0 if max(app) <= MAXA else 1, tot)
            if best[0] is None or key < best[0]:
                best[0], best[1] = key, app
                best[2] = [sp[k] + crafted_bonus[k] + wb[k] for k in range(5)]
                best[3] = equipped + [last]
            return
        nr = len(remains)
        for i in range(nr):
            cur = remains[i]
            sp = list(totals)
            deltas = [0, 0, 0, 0, 0]
            dsum = 0
            for k, r in reqs[cur]:
                if sp[k] < r:
                    d = r - sp[k]
                    deltas[k] = d
                    dsum += d
                    sp[k] = r
            sim_states, bad = [], False
            for j, idx in enumerate(prior_skipped):
                s0 = skipped_states[j]
                s = [s0[0] + deltas[0], s0[1] + deltas[1], s0[2] + deltas[2], s0[3] + deltas[3], s0[4] + deltas[4]] \
                    if dsum else s0
                if can_equip(s, idx):
                    bad = True
                    break
                sim_states.append(s)
            if bad:
                continue
            for h in range(i):
                if can_equip(sp, remains[h]):
                    bad = True
                    break
                sim_states.append(sp)
            if bad:
                continue
            b = bon[cur]
            mod = [sp[k] + b[k] for k in range(5)] if b is not None else sp
            recurse([applied[k] + deltas[k] for k in range(5)] if dsum else applied, mod, total_applied + dsum,
                    sim_states, prior_skipped + remains[:i], equipped + [cur], remains[i + 1:] + remains[:i])

    recurse([0] * 5, [0] * 5, 0, [], [], [], list(range(n)))
    if best[0] is None:   # every order rejected; fall back to plain worst case
        return [max(it.reqs[i] for it in armor + ([weapon] if weapon else [])) for i in range(5)], \
               [0] * 5, armor + ([weapon] if weapon else [])
    return best[1], best[2], [armor[i] for i in best[3]] + ([weapon] if weapon else [])


def _solve_skill_points_exact(armor: list, weapon):
    """
    Direct port of Wynnbuilder's calculate_skillpoints search (used when an item has a negative
    skill bonus, where equip order subtleties matter). An order is rejected if an item skipped
    earlier would already have been wearable, matching how the game switches items on.
    """
    def add5(a, b):
        return [a[i] + b[i] for i in range(5)]

    def can_equip(sp, it):
        return all(it.reqs[i] <= 0 or it.reqs[i] <= sp[i] for i in range(5))

    def apply_to_fit(sp, it):
        applied = [0] * 5
        for i in range(5):
            if it.reqs[i] > 0 and it.reqs[i] > sp[i]:
                d = it.reqs[i] - sp[i]
                applied[i] += d
                sp[i] += d
        return applied

    def fix_should_pop(sp, it):
        applied = [0] * 5
        for i in range(5):
            if it.reqs[i] <= 0:
                continue
            req = it.reqs[i] if it.crafted else it.reqs[i] + it.bonus[i]
            if req > sp[i]:
                d = req - sp[i]
                applied[i] += d
                sp[i] += d
        return applied

    best = {"key": None}
    n_arm = len(armor)
    pos_bonus = [[max(0, b) for b in it.bonus] if not it.crafted else [0] * 5 for it in armor]
    NO_REQ = -10 ** 9                  # no requirement in that skill
    fix_req = [[(it.reqs[k] if it.crafted else it.reqs[k] + it.bonus[k]) if it.reqs[k] > 0 else NO_REQ
                for k in range(5)] for it in armor]
    w_req = [weapon.reqs[k] if weapon is not None and weapon.reqs[k] > 0 else NO_REQ for k in range(5)]

    def hopeless(applied, totals, total_applied, equipped, remains):
        """True if no order below this point can beat the best found so far. Points only ever get added,
        so a lower bound on what's still needed is safe; ties are never skipped, so the result (including
        which of several equally cheap orders wins) is exactly the same as searching everything."""
        bk = best["key"]
        if bk is None:
            return False
        pos = [0] * 5
        for idx in remains:
            pb = pos_bonus[idx]
            for k in range(5):
                pos[k] += pb[k]
        need, over = 0, False
        for k in range(5):
            cap = totals[k] + pos[k]
            m = w_req[k] - cap
            for idx in remains:
                r = armor[idx].reqs[k]
                if r > 0:
                    d = r - cap + pos_bonus[idx][k]
                    if d > m:
                        m = d
            for idx in equipped:
                d = fix_req[idx][k] - cap
                if d > m:
                    m = d
            if m < 0:
                m = 0
            need += m
            if applied[k] + m > MAX_ASSIGNED_PER_SKILL:
                over = True
        lb = total_applied + need
        if bk[0] == 0:
            return over or lb >= bk[1]
        return over and lb >= bk[1]

    def recurse(applied, totals, total_applied, skipped_states, prior_skipped, equipped, remains):
        if best["key"] is not None and len(remains) < n_arm and hopeless(applied, totals, total_applied, equipped, remains):
            return
        if len(remains) == 1:
            it = armor[remains[0]]
            sp = totals[:]
            deltas = apply_to_fit(sp, it)
            if not it.crafted:
                for i in range(5):
                    sp[i] += it.bonus[i]
            if weapon is not None:
                deltas = add5(deltas, apply_to_fit(sp, weapon))
            for other in armor:
                deltas = add5(deltas, fix_should_pop(sp, other))
            for j, idx in enumerate(prior_skipped):
                if can_equip(add5(skipped_states[j], deltas), armor[idx]):
                    return
            app = add5(applied, deltas)
            tot = total_applied + sum(deltas)
            under = all(a <= MAX_ASSIGNED_PER_SKILL for a in app)
            key = (0 if under else 1, tot)
            if best["key"] is None or key < best["key"]:
                final = sp[:]
                for c in armor:              # crafted bonuses are applied once everything is on
                    if c.crafted:
                        final = add5(final, c.bonus)
                if weapon is not None:
                    final = add5(final, weapon.bonus)
                best.update(key=key, applied=app, final=final,
                            order=[armor[i] for i in equipped + [remains[0]]])
            return
        for i in range(len(remains)):
            head = remains[:i]
            skipped = prior_skipped + head
            sp = totals[:]
            it = armor[remains[i]]
            deltas = apply_to_fit(sp, it)
            sim_states, bad = [], False
            for j, idx in enumerate(prior_skipped):
                s = add5(skipped_states[j], deltas)
                if can_equip(s, armor[idx]):
                    bad = True
                    break
                sim_states.append(s)
            if bad:
                continue
            for idx in head:
                if can_equip(sp, armor[idx]):
                    bad = True
                    break
                sim_states.append(sp)
            if bad:
                continue
            mod = sp[:]
            if not it.crafted:
                for k in range(5):
                    mod[k] += it.bonus[k]
            recurse(add5(applied, deltas), mod, total_applied + sum(deltas), sim_states, skipped,
                    equipped + [remains[i]], remains[i + 1:] + head)

    if not armor:
        assigned = [max(0, weapon.reqs[i]) for i in range(5)] if weapon else [0] * 5
        return assigned, [assigned[i] + (weapon.bonus[i] if weapon else 0) for i in range(5)], [weapon]
    recurse([0] * 5, [0] * 5, 0, [], [], [], list(range(len(armor))))
    if best["key"] is None:   # every order rejected; fall back to plain worst case
        return [max(it.reqs[i] for it in armor + ([weapon] if weapon else [])) for i in range(5)], \
               [0] * 5, armor + ([weapon] if weapon else [])
    return best["applied"], best["final"], best["order"] + ([weapon] if weapon else [])


# ============================================================================== ability tree

class Node:
    __slots__ = ("id", "name", "cost", "archetype", "arch_req", "req_arch", "parents", "deps", "blockers",
                 "row", "col", "raw", "ultimate")

    def __init__(self, raw: dict):
        self.raw = raw
        self.id = raw["id"]
        self.name = raw["display_name"]
        self.cost = int(raw.get("cost", 1))
        self.archetype = raw.get("archetype") or None
        self.arch_req = int(raw.get("archetype_req") or 0)
        self.req_arch = raw.get("req_archetype") or self.archetype
        self.parents = list(raw.get("parents", []))
        self.deps = list(raw.get("dependencies", []))
        self.blockers = list(raw.get("blockers", []))
        self.row = raw["display"]["row"]
        self.col = raw["display"]["col"]
        self.ultimate = "Click Combo: F" in (raw.get("desc") or "")


def round_near(x: float) -> float:
    r = round(x)
    return r if abs(x - r) < 1e-8 else x


def _finish_scaling(eff, total, positive, translate):
    """Rounding, floor at zero and cap, as Wynnbuilder's atree_scaling."""
    if eff.get("round", not eff.get("slider", False)):
        total = math.floor(round_near(total))
    if positive and total < 0:
        total = 0
    if "max" in eff:
        cap = translate(eff["max"])
        if cap > 0 and total > cap:
            total = cap
        if cap < 0 and total < cap:
            total = cap
    return total


def default_melee(cls: str) -> dict:
    if cls == "shaman":
        return {"type": "replace_spell", "name": "Relik Melee", "base_spell": 0, "scaling": "melee",
                "use_atkspd": False, "display": "Total",
                "parts": [{"name": "Single Beam", "multipliers": [33, 0, 0, 0, 0, 0]},
                          {"name": "Total", "hits": {"Single Beam": 3}}]}
    name = {"archer": "Single Shot"}.get(cls, "Melee")
    return {"type": "replace_spell", "name": "Melee", "base_spell": 0, "scaling": "melee", "use_atkspd": False,
            "display": name, "parts": [{"name": name, "multipliers": [100, 0, 0, 0, 0, 0]}]}


def _jcopy(x):
    return json.loads(json.dumps(x))


class Tree:
    def __init__(self, data: GameData, cls: str):
        self.cls = cls
        self.nodes = [Node(r) for r in data.atree[CLASS_TITLE[cls]]]
        self.by_id = {n.id: n for n in self.nodes}
        self.by_name = {n.name.lower(): n for n in self.nodes}
        self.order = {n.id: i for i, n in enumerate(self.nodes)}
        self.roots = [n.id for n in self.nodes if not n.parents]
        self.ult_spells = frozenset(e["base_spell"] for n in self.nodes if n.ultimate
                                    for e in n.raw.get("effects", []) if e.get("type") == "replace_spell")
        self._spell_cache = {}

    def find(self, name: str):
        key = name.strip().lower()
        if key in self.by_name:
            return self.by_name[key]
        import difflib
        close = difflib.get_close_matches(key, list(self.by_name), n=1, cutoff=0.6)
        return self.by_name[close[0]] if close else None

    # ---------------------------------------------------------------- legality
    def can_activate(self, n: Node, owned: set, arch_counts: dict, level: int | None = None):
        """Returns (ok, reason). Same checks as Wynnbuilder, plus the level 90 ultimate gate."""
        if level is not None and n.ultimate and level < ULTIMATE_LEVEL:
            return False, f"needs combat level {ULTIMATE_LEVEL}"
        if not n.parents:
            return True, ""
        missing = [d for d in n.deps if d not in owned]
        if missing:
            return False, "needs " + ", ".join(self.by_id[d].name for d in missing)
        blocked = [b for b in n.blockers if b in owned]
        blocked += [o for o in owned if n.id in self.by_id[o].blockers and o not in blocked]
        if blocked:
            return False, "blocked by " + ", ".join(self.by_id[b].name for b in blocked)
        if not any(p in owned for p in n.parents):
            return False, "not connected to anything you own"
        if n.arch_req:
            have = arch_counts.get(n.req_arch, 0)
            if have < n.arch_req:
                return False, f"needs {n.arch_req} {n.req_arch} abilities (have {have})"
        return True, ""

    def validate(self, ids, level: int | None = None, rank_bonus: int = 0):
        """Find a legal click order for a set of nodes. Returns dict with ok/order/errors/ap/arch."""
        remaining = [self.by_id[i] for i in sorted(set(ids), key=lambda x: self.order[x])]
        owned, order, arch = set(), [], defaultdict(int)
        progress = True
        while remaining and progress:
            progress = False
            for n in list(remaining):
                ok, _ = self.can_activate(n, owned, arch, level)
                if ok:
                    owned.add(n.id)
                    order.append(n.id)
                    if n.archetype:
                        arch[n.archetype] += 1
                    remaining.remove(n)
                    progress = True
                    break
        errors = [f"{n.name}: {self.can_activate(n, owned, arch, level)[1]}" for n in remaining]
        ap = sum(self.by_id[i].cost for i in set(ids))
        if level is not None and ap > ability_points(level, rank_bonus):
            errors.append(f"uses {ap} ability points but level {level} has {ability_points(level, rank_bonus)}")
        return {"ok": not errors, "order": order, "errors": errors, "ap": ap, "arch": dict(arch)}

    def frontier(self, owned: frozenset, arch: dict, level: int | None):
        out = []
        for n in self.nodes:
            if n.id in owned:
                continue
            if self.can_activate(n, owned, arch, level)[0]:
                out.append(n)
        return out

    # ---------------------------------------------------------------- spells
    def toggle_bonus_kinds(self) -> dict:
        """{toggle name: "linear" (only damage multipliers, walk speed, mana regen, jump height: scale exactly with a
        share of time), "conv" (also element conversions: scale to within about 0.1%) or "other"}."""
        cached = getattr(self, "_toggle_kinds", None)
        if cached is None:
            cached = {}
            for n in self.nodes:
                for e in n.raw.get("effects", []):
                    t = e.get("toggle")
                    if not t:
                        continue
                    kind = "linear"
                    for b in e.get("bonuses", []):
                        nm = str(b.get("name", ""))
                        if b.get("type") == "stat" and not b.get("mult") and \
                                (nm.startswith("damMult.") or nm in ("spd", "mr", "jh", "mainAttackRange")):
                            continue
                        if b.get("type") == "stat" and "ConvBase" in nm:
                            kind = "conv" if kind == "linear" else kind
                            continue
                        kind = "other"
                    order = {"linear": 0, "conv": 1, "other": 2}
                    if order[kind] > order[cached.get(t, "linear")]:
                        cached[t] = kind
                    else:
                        cached.setdefault(t, kind)
            self._toggle_kinds = cached
        return cached

    def spells(self, owned, extras=(), toggles=frozenset(), sliders=None, slider_mode="none"):
        """
        Merge the owned abilities (Wynnbuilder's atree_merge, atree_scaling and collect_spells). Cached.
        extras: ((key, ability_dict), ...) from aspects and major IDs, merged after the tree.
        toggles: names of ability toggles that are on. sliders: {slider name: value} you set;
        slider_mode decides the rest: "none" = Wynnbuilder's default, "half" = half of max, "max".
        Returns (spells, stats, damage multipliers, deferred stat-input scalings, interactives).
        """
        sliders = sliders or {}
        # toggles: names that are on, or {name: share of the time on (0-1)}. A buff that's up part of the time
        # counts with its bonus scaled by that share: damage, attack speed and walk speed grow in a straight line
        # with these bonuses, so this is the average over a fight.
        tog = ({k: float(v) for k, v in toggles.items() if v} if isinstance(toggles, dict)
               else {k: 1.0 for k in toggles})
        key = (frozenset(owned), tuple(k for k, _ in extras), tuple(sorted(tog.items())),
               tuple(sorted(sliders.items())), slider_mode)
        if key in self._spell_cache:
            return self._spell_cache[key]
        merged = {999: {"id": 999, "name": "Melee", "properties": {}, "effects": [default_melee(self.cls)]},
                  998: {"id": 998, "name": "Elemental Mastery", "properties": {}, "effects": []}}

        def merge(raw, new_id, name):
            if "base_abil" in raw:
                base = merged.get(raw["base_abil"])
                if base is not None:
                    base["effects"] = base["effects"] + raw.get("effects", [])
                    for pk, pv in (raw.get("properties") or {}).items():
                        base["properties"][pk] = base["properties"].get(pk, 0) + pv
            else:
                merged[new_id] = {"id": new_id, "name": name, "properties": dict(raw.get("properties") or {}),
                                  "effects": list(raw.get("effects", []))}

        for n in sorted((self.by_id[i] for i in owned), key=lambda x: self.order[x.id]):
            merge(n.raw, n.id, n.name)
        owned_set = set(owned)
        for i, (k, abil) in enumerate(extras):
            deps = abil.get("dependencies")
            if deps and not all(d in owned_set for d in deps):
                continue
            merge(abil, abil.get("id", -1000 - i), str(k[1]))

        def translate(v):
            if isinstance(v, str):
                aid, prop = v.split(".", 1)
                ab = merged.get(int(aid))
                return (ab["properties"].get(prop, 0) if ab else 0) or 0
            return v

        # Sliders and toggles available with this tree (Wynnbuilder's atree_make_interactives).
        slider_info, toggle_names = {}, []
        pending = [eff for ab in merged.values() for eff in ab["effects"]
                   if eff["type"] == "stat_scaling" and eff.get("slider")]
        for _ in range(len(pending) + 1):
            left = []
            for eff in pending:
                name = eff.get("slider_name")
                beh = eff.get("behavior", "merge")
                if name in slider_info:
                    info = slider_info[name]
                    if beh == "overwrite":
                        if "slider_max" in eff:
                            info["max"] = eff["slider_max"]
                        if "slider_default" in eff:
                            info["default"] = eff["slider_default"]
                        info["overwritten"] = True
                    elif not info.get("overwritten"):
                        info["max"] += eff.get("slider_max", 0)
                        if "slider_max_mult" in eff:
                            info["max_mult"] = info.get("max_mult", 1) * eff["slider_max_mult"]
                        info["default"] += eff.get("slider_default", 0)
                elif beh == "merge":
                    slider_info[name] = {"max": eff.get("slider_max", 0), "default": eff.get("slider_default", 0),
                                         "step": eff.get("slider_step") or 1}
                else:
                    left.append(eff)
            if len(left) == len(pending):
                break
            pending = left
        for info in slider_info.values():
            if info.get("max_mult", 1) != 1:
                info["max"] = js_round(info["max"] * info["max_mult"])
        for ab in merged.values():
            for eff in ab["effects"]:
                if eff["type"] == "raw_stat" and eff.get("toggle") and eff["toggle"] not in toggle_names:
                    toggle_names.append(eff["toggle"])
        slider_values = {}
        for name, info in slider_info.items():
            if name in sliders:
                val = sliders[name]
            elif slider_mode == "max":
                val = info["max"]
            elif slider_mode == "none":
                val = info["default"]
            else:
                step = info["step"] or 1
                val = math.floor(info["max"] / 2 / step) * step
            slider_values[name] = max(0, min(val, info["max"])) if info["max"] else val

        stats = defaultdict(float)
        dmg_mult = {}
        deferred = []

        def add_stat(name, val):
            if name.startswith("damMult."):
                k = name[8:]
                if k.split(":")[0].split(";")[0] in NONSTACKING_MULTS and k in dmg_mult:
                    dmg_mult[k] = max(dmg_mult[k], val)
                else:
                    dmg_mult[k] = dmg_mult.get(k, 0) + val
            elif name.startswith(("defMult.", "healMult.", "manaMult.")):
                stats[name] += val
            elif "ConvBase" in name or "." not in name:
                stats[name] += val

        def apply(bonus, val, phase):
            if bonus.get("type") != phase:
                return
            if phase == "prop":
                tgt = merged.get(bonus.get("abil"))
                if tgt is not None:
                    cur = tgt["properties"].get(bonus["name"], 0) or 0
                    tgt["properties"][bonus["name"]] = cur * val if bonus.get("mult") else cur + val
            else:
                add_stat(bonus["name"], val)

        # Property changes first, then stats (Wynnbuilder's two-phase order).
        for phase in ("prop", "stat"):
            for ab in merged.values():
                for eff in ab["effects"]:
                    if eff["type"] == "raw_stat":
                        share = 1.0
                        if eff.get("toggle"):
                            share = tog.get(eff["toggle"], 0.0)
                            if share <= 0:
                                continue
                        for b in eff.get("bonuses", []):
                            v = translate(b.get("value", 0))
                            if share != 1.0 and isinstance(v, (int, float)):
                                v = 1 + (v - 1) * share if b.get("mult") else v * share
                            apply(b, v, phase)
                    elif eff["type"] == "stat_scaling" and "output" in eff:
                        if eff.get("slider"):
                            name = eff.get("slider_name")
                            if eff.get("behavior", "merge") == "modify" and name not in slider_info:
                                continue
                            val = slider_values.get(name, 0)
                            req = eff.get("requirement", 0)
                            if req > val:
                                continue
                            x = val - req
                            sc = translate((eff.get("scaling") or [0])[0])
                            if eff.get("multiplicative"):
                                total = (((100 + sc) / 100) ** int(x) - 1) * 100
                            else:
                                total = int(x) * sc
                            positive = False
                        else:
                            inputs = eff.get("inputs", [])
                            if any(i.get("type") == "stat" for i in inputs):
                                if phase == "stat":
                                    deferred.append((eff, dict((i, merged[i]["properties"]) for i in merged)))
                                continue
                            total = 0.0
                            for sc, inp in zip(eff.get("scaling", [0]), inputs):
                                src = merged.get(inp.get("abil"))
                                if src is not None:
                                    total += (src["properties"].get(inp["name"], 0) or 0) * translate(sc)
                            positive = eff.get("positive", True)
                        total += translate(eff.get("base", 0))
                        total = _finish_scaling(eff, total, positive, translate)
                        outs = eff["output"] if isinstance(eff["output"], list) else [eff["output"]]
                        for out in outs:
                            apply(out, total, phase)

        interactives = {"toggles": toggle_names,
                        "sliders": {n: (i["max"], slider_values[n]) for n, i in slider_info.items()}}

        # Collect spells.
        spells = {}
        for ab in merged.values():
            for eff in ab["effects"]:
                if eff["type"] != "replace_spell":
                    continue
                cur = spells.get(eff["base_spell"])
                if cur is None:
                    cur = _jcopy(eff)
                    spells[eff["base_spell"]] = cur
                else:
                    for k, v in eff.items():
                        cur[k] = _jcopy(v)
                for part in cur.get("parts", []):
                    if "hits" in part:
                        part["hits"] = {k: translate(v) for k, v in part["hits"].items()}
        for ab in merged.values():
            for eff in ab["effects"]:
                if eff["type"] != "add_spell_prop":
                    continue
                sp = spells.get(eff["base_spell"])
                if sp is None:
                    continue
                if "cost" in sp:
                    sp["cost"] += eff.get("cost", 0)
                if eff.get("mana_gained"):
                    sp["mana_gained"] = (sp.get("mana_gained") or 0) + translate(eff["mana_gained"])
                if "display" in eff:
                    sp["display"] = eff["display"]
                target = eff.get("target_part")
                if target is None:
                    continue
                behavior = eff.get("behavior", "merge")
                found = False
                for part in sp.get("parts", []):
                    if part["name"] != target:
                        continue
                    if "multipliers" in eff:
                        mults = part.setdefault("multipliers", [0] * 6)
                        for i, v in enumerate(eff["multipliers"]):
                            mults[i] = v if behavior == "overwrite" else mults[i] + v
                    elif "power" in eff:
                        part["power"] = eff["power"] if behavior == "overwrite" else part.get("power", 0) + eff["power"]
                    elif "hits" in eff:
                        hits = part.setdefault("hits", {})
                        for k, v in eff["hits"].items():
                            v = translate(v)
                            hits[k] = v if behavior == "overwrite" else hits.get(k, 0) + v
                    found = True
                    break
                if not found and behavior == "merge":
                    newp = {"name": target}
                    for k in ("multipliers", "power", "hits", "use_str"):
                        if k in eff:
                            newp[k] = _jcopy(eff[k])
                    if "hits" in newp:
                        newp["hits"] = {k: translate(v) for k, v in newp["hits"].items()}
                    sp.setdefault("parts", []).append(newp)
        result = (spells, dict(stats), dmg_mult, deferred, interactives)
        self._spell_cache[key] = result
        return result


# ============================================================================== damage

def set_bonuses(armor: list, weapon, data: GameData) -> dict:
    """Active set bonus for everything worn (Wynnbuilder: bonus index = pieces worn - 1)."""
    counts = defaultdict(int)
    for it in [a for a in armor if a is not None] + ([weapon] if weapon else []):
        if it.set:
            counts[it.set] += 1
    out = defaultdict(float)
    for sname, c in counts.items():
        s = data.sets.get(sname)
        if s and c - 1 < len(s.get("bonuses", [])):
            for k, v in s["bonuses"][c - 1].items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k] += v
    return out


CLASS_DEF_MULT = {"relik": 0.60, "bow": 0.70, "wand": 0.80, "dagger": 1.0, "spear": 1.0}


def level_base_hp(level: int) -> int:
    return 5 * max(1, min(level, 121)) + 5


def build_stats(armor: list, weapon, skill_totals: list, tree_stats: dict, data: GameData, tomes=(),
                level: int = 1, sb=None) -> dict:
    stats = defaultdict(float)
    stats["hp"] = level_base_hp(level)
    stats["agiDef"] = 90
    worn = [a for a in armor if a is not None] + ([weapon] if weapon else []) + list(tomes)
    for it in worn:
        for k, v in it.stats.items():
            stats[k] += v
    for k, v in (sb if sb is not None else set_bonuses(armor, weapon, data)).items():
        if k not in SKILLS:
            stats[k] += v
    for k, v in tree_stats.items():
        stats[k] += v
    for i, s in enumerate(SKILLS):
        stats[s] = skill_totals[i]
    return stats


# Stat names the damage formula reads, built once (the same names, in the same order, as building them on the fly).
_CONV_KEYS = [p + "ConvBase" for p in ELEM_PREFIX]
_ADD_MIN = [p + "DamAddMin" for p in ELEM_PREFIX]
_ADD_MAX = [p + "DamAddMax" for p in ELEM_PREFIX]
_DAM_PCT = [p + "DamPct" for p in ELEM_PREFIX]
_DAM_RAW = [p + "DamRaw" for p in ELEM_PREFIX]
_TAG_KEYS = {tag: {"pct": [p + tag + "Pct" for p in ELEM_PREFIX], "raw": [p + tag + "Raw" for p in ELEM_PREFIX],
                   "static": tag.lower() + "Pct", "rpct": "r" + tag + "Pct", "praw": tag.lower() + "Raw",
                   "rraw": "r" + tag + "Raw"} for tag in ("Sd", "Md")}
_PART_CONV_KEYS = {}


def mult_plan(dmg_mult) -> list:
    """The damage multipliers read once: (name, value, spell part or None, element tag or None), in the dict's
    order, so applying them multiplies in exactly the same order as reading the dict each time."""
    plan = []
    for k, v in dmg_mult.items():
        part = k.split(":", 1)[1] if ":" in k else None
        tag = k.split(";", 1)[1] if ";" in k else None
        plan.append((k, v, part, tag))
    return plan


def skill_boosts(stats) -> list:
    return [0.0] + [skill_pct(stats.get(s, 0)) * SKILL_DAMAGE_MULT[i] for i, s in enumerate(SKILLS)]


def spell_part_damage(stats, weapon, conversions, use_spell, ignore_speed, ignore_str, dmg_mult,
                      part_filter=None, ignored_mults=(), plan=None, skill_boost=None):
    """Port of Wynnbuilder's calculateSpellDamage. Returns (normal_avg, crit_avg).
    plan / skill_boost: mult_plan(dmg_mult) and skill_boosts(stats) worked out once per build (optional)."""
    wdam = weapon.damage
    conversions = list(conversions)
    get = stats.get
    if part_filter is not None:
        pkeys = _PART_CONV_KEYS.get(part_filter)
        if pkeys is None:
            pkeys = _PART_CONV_KEYS[part_filter] = [f"{p}ConvBase:{part_filter}" for p in ELEM_PREFIX]
        for i in range(6):
            conversions[i] += get(pkeys[i], 0)
            conversions[i] += get(_CONV_KEYS[i], 0)
    else:
        for i in range(6):
            conversions[i] += get(_CONV_KEYS[i], 0)
    present = [hi > 0 for _, hi in wdam]
    neutral_conv = conversions[0] / 100
    if neutral_conv == 0:
        present = [False] * 6
    damages, wmin, wmax = [], 0.0, 0.0
    for lo, hi in wdam:
        damages.append([lo * neutral_conv, hi * neutral_conv])
        wmin += lo
        wmax += hi
    total_convert = 0.0
    for i in range(1, 6):
        if conversions[i] > 0:
            f = conversions[i] / 100
            damages[i][0] += f * wmin
            damages[i][1] += f * wmax
            present[i] = True
            total_convert += f
    total_convert += conversions[0] / 100
    if not ignore_speed:
        spd = ATTACK_SPEED_MULT[ATTACK_SPEEDS.index(weapon.atk_spd)] if weapon.atk_spd in ATTACK_SPEEDS else 1
        for d in damages:
            d[0] *= spd
            d[1] *= spd
    for i in range(6):
        if present[i]:
            damages[i][0] += get(_ADD_MIN[i], 0)
            damages[i][1] += get(_ADD_MAX[i], 0)
    tk = _TAG_KEYS["Sd" if use_spell else "Md"]
    if skill_boost is None:
        skill_boost = skill_boosts(stats)
    static_boost = (get(tk["static"], 0) + get("damPct", 0)) / 100
    rboost = (get(tk["rpct"], 0) + get("rDamPct", 0)) / 100
    total_min = total_max = 0.0
    saved = []
    pct_keys = tk["pct"]
    for i in range(6):
        di = damages[i]
        saved.append(di[:])
        total_min += di[0]
        total_max += di[1]
        boost = 1 + skill_boost[i] + static_boost + (get(pct_keys[i], 0) + get(_DAM_PCT[i], 0)) / 100
        if i > 0:
            boost += rboost
        di[0] *= boost
        di[1] *= boost
    elem_min = total_min - saved[0][0]
    elem_max = total_max - saved[0][1]
    prop_raw = get(tk["praw"], 0) + get("damRaw", 0)
    rainbow_raw = get(tk["rraw"], 0) + get("rDamRaw", 0)
    raw_keys = tk["raw"]
    for i in range(6):
        s = saved[i]
        raw = (get(raw_keys[i], 0) + get(_DAM_RAW[i], 0)) if present[i] else 0
        lo_b = hi_b = raw
        if total_max > 0:
            lo_b += ((s[1] / total_max) if total_min == 0 else (s[0] / total_min)) * prop_raw
            hi_b += (s[1] / total_max) * prop_raw
        if i != 0 and elem_max > 0:
            lo_b += ((s[1] / elem_max) if elem_min == 0 else (s[0] / elem_min)) * rainbow_raw
            hi_b += (s[1] / elem_max) * rainbow_raw
        damages[i][0] += lo_b * total_convert
        damages[i][1] += hi_b * total_convert
    str_boost = 1 if ignore_str else 1 + skill_boost[1]
    mult = 1.0
    elem_mult = [1.0] * 6
    for k, v, part, tag in (plan if plan is not None else mult_plan(dmg_mult)):
        if part is not None and part != part_filter:
            continue                      # multiplier for one specific spell part
        if k in ignored_mults:
            continue
        if tag is not None:               # element-specific (or 'm' = main attack only)
            if tag == "m" and not use_spell:
                mult *= 1 + v / 100
            elif tag in ELEM_PREFIX:
                elem_mult[ELEM_PREFIX.index(tag)] *= 1 + v / 100
        else:
            mult *= 1 + v / 100
    crit_mult = 0 if ignore_str else 1 + get("critDamPct", 0) / 100
    norm = crit = 0.0
    for i, (lo, hi) in enumerate(damages):
        lo, hi = lo * elem_mult[i], hi * elem_mult[i]
        lo, hi = max(lo, 0), max(hi, 0)
        norm += (lo + hi) / 2 * str_boost * mult
        crit += (lo + hi) / 2 * (str_boost + crit_mult) * mult
    return norm, crit


def mana_refund_mult(stats) -> float:
    """Spell costs are divided by this in mana checks (Mage's Paradox refund: manaMult +34%, as Wynnbuilder)."""
    m = 1.0
    for k, v in stats.items():
        if k.startswith("manaMult.") and v:
            m *= 1 + v / 100
    return m


def spell_cost(stats, spell) -> float:
    bs = spell.get("base_spell")
    cost = spell.get("cost", 0) * (1 - skill_pct(stats.get("int", 0)) * INT_COST_MULT)
    cost += stats.get(f"spRaw{bs}", 0)
    cost *= 1 + stats.get(f"spPct{bs}", 0) / 100
    return max(1.0, cost) * (1 + stats.get(f"spPct{bs}Final", 0) / 100)     # Wynnbuilder's getSpellCost


def raw_to_pct(raw: float, pct: float) -> float:
    if raw < 0:
        return min(0.0, raw - raw * pct)
    if raw > 0:
        return raw + raw * pct
    return 0.0


def parse_enemy_arg(text: str) -> dict:
    """'fire 3000' / 'fire=2000, earth=1000' / '2500 neutral' -> {element index 0-5: damage per hit}."""
    out = {}
    names = {n.lower(): i for i, n in enumerate(ELEM_NAMES)}
    for part in [p for p in re.split(r"[,;]", text or "") if p.strip()]:
        num = re.search(r"\d+(?:\.\d+)?", part)
        word = re.search(r"[a-z]+", part.lower())
        if not num:
            continue
        elem = names.get(word.group(0), None) if word else 0
        if elem is None:
            elem = next((i for n, i in names.items() if word and n.startswith(word.group(0))), None)
        if elem is not None:
            out[elem] = out.get(elem, 0) + float(num.group(0))
    return out


_DEF_KEYS = [e + "Def" for e in POWDER_ELEMS]
_DEF_PCT_KEYS = [e + "DefPct" for e in POWDER_ELEMS]


def defense_stats(stats, weapon, def_mults: dict, enemy=None) -> dict:
    """Wynnbuilder's getDefenseStats: total HP, effective HP (with/without agility dodge), regen, defenses."""
    def_pct = skill_pct(stats.get("def", 0)) * 0.867
    agi_pct = skill_pct(stats.get("agi", 0)) * 0.951
    total_hp = max(5.0, stats.get("hp", 0) + stats.get("hpBonus", 0))
    mult = 2 - CLASS_DEF_MULT.get(weapon.type, 1.0)
    for v in def_mults.values():
        mult *= 1 - v / 100
    agi_red = (100 - stats.get("agiDef", 90)) / 100
    denom_agi = agi_red * agi_pct + (1 - agi_pct) * (1 - def_pct)
    ehp_agi = total_hp / denom_agi / mult
    ehp = total_hp / (1 - def_pct) / mult
    hpr = raw_to_pct(stats.get("hprRaw", 0), stats.get("hprPct", 0) / 100)
    elem_def = []
    elem_eff = []
    rdef = stats.get("rDefPct", 0)
    for i in range(5):
        raw = stats.get(_DEF_KEYS[i], 0)
        pct = (stats.get(_DEF_PCT_KEYS[i], 0) + rdef) / 100
        elem_def.append(raw + raw * pct if raw > 0 else (raw - raw * pct if raw < 0 else 0.0))
        # Wynnbuilder's display lets a negative raw defense with a big % bonus turn positive; its own rawToPct
        # (and the game) stops negative defense at 0, so the capped value is used for real hits.
        elem_eff.append(raw_to_pct(raw, pct))
    out = {"hp": total_hp, "ehp_agi": ehp_agi, "ehp": ehp, "hpr": hpr, "def_pct": def_pct * 100,
           "agi_pct": agi_pct * 100, "elem_def": elem_def, "elem_def_eff": elem_eff}
    if enemy:
        # Elemental defense is a flat reduction of that element's part of each hit, applied after the
        # percentage reductions; negative defense adds true damage (official wiki, "Elements").
        frac = denom_agi * mult
        taken = 0.0
        for e, dmg in enemy.items():
            part = dmg * frac
            if e > 0:
                d = elem_eff[e - 1]
                part = max(0.0, part - d) if d >= 0 else part - d
            taken += part
        taken = max(1.0, taken)
        hit = sum(enemy.values())
        out.update(ehp_enemy=total_hp * hit / taken, enemy_taken=taken, hits_to_die=total_hp / taken)
    return out


def evaluate_spells(spells, stats, weapon, dmg_mult, heal_items=None) -> dict:
    """Average expected damage of each spell's headline number (crit chance from Dexterity).
    heal_items: the stats' healMult.* entries in order, if already collected."""
    crit_chance = skill_pct(stats.get("dex", 0))
    heal_mult = 1.0
    if heal_items is None:
        heal_items = [(k, v) for k, v in stats.items() if isinstance(k, str) and k.startswith("healMult.")]
    for _k, v in heal_items:
        heal_mult *= 1 + v / 100
    heal_mult *= 1 + stats.get("healPct", 0) / 100
    heal_base = max(5.0, stats.get("hp", 0) + stats.get("hpBonus", 0)) * heal_mult
    out = {}
    plan, sboost = mult_plan(dmg_mult), skill_boosts(stats)
    for bs, sp in spells.items():
        parts = sp.get("parts", [])
        use_spell = sp.get("scaling", "spell") == "spell"
        ignore_speed = not sp.get("use_atkspd", True)
        values, heals = {}, {}
        for part in parts:
            if "multipliers" in part:
                n, c = spell_part_damage(stats, weapon, part["multipliers"], use_spell, ignore_speed,
                                         not part.get("use_str", True), dmg_mult,
                                         part_filter=f"{bs}.{part['name']}",
                                         ignored_mults=tuple(part.get("ignored_mults") or ()),
                                         plan=plan, skill_boost=sboost)
                values[part["name"]] = n * (1 - crit_chance) + c * crit_chance
            elif "hits" in part:
                total = heal_total = 0.0
                for k, v in part["hits"].items():
                    hits = v or 0
                    if part.get("tick_rounding") and hits:
                        hits = 1.0 / (math.floor(1.0 / hits * 20) * 0.05)
                    total += values.get(k, 0) * hits
                    heal_total += heals.get(k, 0) * hits
                values[part["name"]] = total
                if heal_total:
                    heals[part["name"]] = heal_total
            elif "power" in part:
                heals[part["name"]] = part["power"] * heal_base
                values[part["name"]] = 0.0
        disp = sp.get("display")
        if disp in values:
            dmg = values[disp]
        elif parts and disp not in ("", None):
            dmg = values.get(parts[-1]["name"], 0.0)
        else:
            dmg = 0.0
        is_dps = _is_dps_label(disp)
        # Damage of one cast. If a node relabeled the spell as a damage-per-second figure (often only true
        # while a buff or ultimate is active), fall back to the largest per-cast total among its parts.
        cast = dmg
        if is_dps and bs in (1, 2, 3, 4):
            per_cast = [values[p["name"]] for p in parts if p["name"] in values and not _is_dps_label(p["name"])]
            cast = max(per_cast) if per_cast else 0.0
        heal = heals.get(disp, 0.0) if disp in heals else (max(heals.values()) if heals else 0.0)
        out[bs] = {"name": sp.get("name", f"Spell {bs}"), "damage": dmg, "cast": cast, "heal": heal,
                   "cost": spell_cost(stats, sp) if bs in (1, 2, 3, 4) else 0.0,
                   "mana_gained": sp.get("mana_gained") or 0.0, "is_dps": is_dps}
    return out


def rotation_dps(rs, rate: float) -> float:
    """Damage per second of casting the spells `rs` in turn, `rate` casts a second in all (each spell rate/len times).
    A spell that lasts (summons, totems, storms: shown as damage per second) can't add more than its damage per
    second while it's up, however often it's recast: recasting refreshes it rather than stacking another copy.
    (Conservative: a few such spells might overlap in game; they're never counted above one copy.)"""
    each = rate / len(rs)
    tot = 0.0
    for r in rs:
        v = r["cast"] * each
        if r.get("is_dps") and r.get("damage", 0) > 0:
            v = min(v, r["damage"])
        tot += v
    return tot


def rotation_breaks(rs, rate_max: float) -> list:
    """Cast rates (up to rate_max) where a lasting spell in `rs` reaches one copy up all the time: the only places
    besides 0 and rate_max where spells + main attacks together can peak."""
    out = []
    for r in rs:
        if r.get("is_dps") and r.get("damage", 0) > 0 and r["cast"] > 0:
            b = r["damage"] * len(rs) / r["cast"]
            if 0 < b < rate_max:
                out.append(b)
    return out


def _is_dps_label(label) -> bool:
    s = str(label or "").lower()
    return "dps" in s or "per second" in s


# Assumptions for damage that isn't a normal cast (printed in the report).
EXTRA_DPS_UPTIME = 0.5        # summons, sigils, totems: active about half the time
EXTRA_TRIGGER_EVERY = 3.0     # one-off triggered hits (traps, shatters): about every 3 s
# Spells that only damage enemies that come close to you (owner, Oct 2026: "I'd have to run at the enemy"): left out of
# damage-per-second rotations, still in the burst combo. Arrow Shield's arrows hit "enemies that get too close".
CONTACT_SPELLS = frozenset({"Arrow Shield"})
ULT_DPS_UPTIME = 0.25         # damage-over-time ultimates: active about a quarter of the time
ULT_HIT_EVERY = 60.0          # one-shot ultimates: about once a minute
SURVIVAL_HEAL_SECONDS = 10.0  # survival goal: effective HP plus 10 s of healing and two regen ticks


GOALS = {
    "mixed": "Most damage per second possible, any style: main attacks only, spells, or both together (whichever "
             "is highest: the spell rotation your mana keeps up with main attacks in between; the helper's own "
             "estimate, Wynnbuilder has no such number)",
    "spells": "Highest damage you can keep up with spells (mana-limited spell rotation)",
    "burst": "Biggest one-time combo (every damage spell once)",
    "melee": "Main attack DPS exactly as Wynnbuilder shows it (Average DPS: main attack damage per hit x attack speed)",
    "survival": "Hardest to kill (effective HP, plus regen and self-healing)",
    "balanced": "Best mix of damage and effective HP",
    "healing": "Most healing per second (support)",
    "ability": "Highest damage of one ability you choose (--focus), e.g. puppets, Meteor or Bash",
}


# A second goal can be mixed in by weight ("mostly spells, 30% survival"). The score is a weighted geometric
# mean, main^(1-w) x second^w, like "balanced" (which is damage and effective HP at 50/50): scaling either part
# doesn't change which build wins, so the two never need to be in the same units.
SECOND_GOALS = ("spells", "burst", "melee", "mixed", "survival", "healing")
GOAL_WORDS = {"spells": "spell damage", "burst": "burst combo", "melee": "main attack DPS", "survival": "survival",
              "balanced": "damage and survival", "healing": "healing", "ability": "one ability",
              "mixed": "most damage (any style)"}


def style_open(goal, goal2=None) -> bool:
    """True when the goal's damage can come from spells or main attacks (so the best build may use either style's
    ability tree): most damage in any style, damage and survival, or a mix that has both kinds."""
    gs = {goal, goal2} - {None}
    return bool(gs & {"mixed", "balanced"}) or (bool(gs & {"spells", "burst"}) and "melee" in gs)


def clean_goal2(goal, goal2, weight):
    """(second goal, weight 0-0.9) or (None, 0) when there's no usable second goal."""
    goal2 = (goal2 or "").strip().lower() or None
    try:
        w = float(weight or 0)
    except (TypeError, ValueError):
        w = 0.0
    if w > 1:
        w /= 100.0                         # given as a percentage
    w = max(0.0, min(0.9, w))
    if goal2 not in SECOND_GOALS or goal2 == goal or w <= 0:
        return None, 0.0
    return goal2, w


def mix_goals(m1: float, m2: float, w: float) -> float:
    return max(m1, 1e-9) ** (1 - w) * max(m2, 1e-9) ** w


def resolve_focus(tree, text: str):
    """Find the ability a player means ('puppet damage', 'meteor', 'bash'): (spell slot, name, unlocking node id)."""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", str(x).lower())
    filler = {"highest", "high", "damaging", "damage", "dmg", "dps", "max", "maximum", "maximize", "strongest",
              "strong", "biggest", "big", "most", "best", "my", "the", "for", "a", "an", "of", "with", "build",
              "hardest", "hitting", "hit", "hits", "powerful", "i", "want", "level", "lvl", "per", "second", "and"}
    words = [w for w in re.findall(r"[a-z0-9']+", str(text).lower()) if w not in filler and not w.isdigit()]
    if not words:
        return None
    queries = []
    for q in [norm(" ".join(words))] + [norm(w) for w in words]:
        for qq in (q, q[:-1] if q.endswith("s") and len(q) > 3 else None):
            if qq and qq not in queries:
                queries.append(qq)
    best = None
    for n in tree.nodes:
        for e in n.raw.get("effects", []):
            if e.get("type") != "replace_spell" or e.get("base_spell") is None:
                continue
            names = [(e.get("name"), 3), (e.get("display"), 2)] + [(pt.get("name"), 1) for pt in e.get("parts", [])] + \
                    [(n.name, 2)]
            for nm, weight in names:
                if not nm:
                    continue
                k = norm(nm)
                for qq in queries:
                    score = (10 if k == qq else 6 if k.startswith(qq) else 4 if qq in k else 0) + weight
                    if score > weight and (best is None or score > best[0]):
                        best = (score, int(e["base_spell"]), e.get("name") or n.name, n.id)
    return best[1:] if best else None



# Wynnbuilder's in-game-tested poison multipliers per (adjusted) attack speed (display.js).
POISON_ATKSPD_MULT = [2.6239, 2.1198, 1.3125, 1.0083, 0.75, 0.5333, 0.3333]
POISON_MODE = "wynnbuilder"   # --poison low: listed value / 3 per second, no stacking (the wiki's plain reading)


def poison_dps(stats: dict, weapon) -> tuple:
    """(poison damage/s while hitting with spells, while hitting with main attacks), as Wynnbuilder shows."""
    poison = stats.get("poison", 0)
    if poison <= 0:
        return 0.0, 0.0
    tier = ATTACK_SPEEDS.index(weapon.atk_spd) if weapon.atk_spd in ATTACK_SPEEDS else 3
    tier = max(0, min(6, tier + int(stats.get("atkTier", 0))))
    if POISON_MODE == "low":
        return poison / 3, poison / 3
    spell = math.floor(poison * 2)
    melee = math.floor(poison * ATTACK_SPEED_MULT[tier] * POISON_ATKSPD_MULT[tier] * 3)
    return float(spell), float(melee)


def mana_lines(m) -> list:
    """Plain words for the mana check of a spell rotation."""
    if not m:
        return []
    head = (f"Mana: at full speed ({m['full_rate']:.0f} casts/s) the rotation uses {m['used']:.1f}/s; you get "
            f"{m['regen']:.1f}/s back from regen" + (f" and {m['from_spells']:.1f}/s from spells" if m["from_spells"] > 0 else "")
            + f". Mana bar {m['bar']:.0f}.")
    if m.get("steal", 0) > 0:
        head += (f" (Mana steal {m['steal']:.0f}/3s isn't counted here: it only works on main attack hits, "
                 "so it refills mana only while you main-attack.)")
    if m["seconds"] is None:
        return [head, "  You never run out: full casting speed can be kept up forever."]
    return [head, f"  From a full bar you run dry after about {m['seconds']:.0f}s at full speed "
                  f"({fmt_num(m['full_dps'])} damage/s). After that you can keep up about {m['sustained_rate']:.1f} casts/s, "
                  "which is the damage per second counted above."]


def score_build(results: dict, stats: dict, weapon, goal: str, ult_spells=frozenset(), special=None,
                defense=None, focus_bs=None, goal2=None, goal2_weight=0.0) -> dict:
    melee = results.get(0, {}).get("damage", 0.0)
    tier = ATTACK_SPEEDS.index(weapon.atk_spd) if weapon.atk_spd in ATTACK_SPEEDS else 3
    tier = max(0, min(6, tier + int(stats.get("atkTier", 0))))
    melee_dps = melee * ATTACK_SPEED_MULT[tier]
    poison_spell, poison_melee = poison_dps(stats, weapon)
    # Life steal: listed per 3 seconds; you get it while you keep hitting.
    life_steal_ps = max(0.0, stats.get("ls", 0)) / 3
    dmg_spells = [(bs, r) for bs, r in results.items() if bs in (1, 2, 3, 4) and r["cast"] > 0]
    burst = sum(r["cast"] for _, r in dmg_spells)
    # Summons, sigils and ultimates count as sustained damage. One-off hits that need something to happen
    # first (procs like Shield Strike or Explosive Impact) are situational: they count in a burst combo but
    # not in damage per second, like Wynnbuilder's DPS figure.
    sustained_extra = triggered_extra = special_dps = 0.0
    triggered_names = []
    for bs, r in results.items():
        if bs < 5 or r["damage"] <= 0:
            continue
        if bs in ult_spells:
            sustained_extra += r["damage"] * ULT_DPS_UPTIME if r["is_dps"] else r["damage"] / ULT_HIT_EVERY
            burst += r["damage"] * 5 if r["is_dps"] else r["damage"]
        elif r["is_dps"]:
            sustained_extra += r["damage"] * EXTRA_DPS_UPTIME
            burst += r["damage"] * 3
        else:
            triggered_extra += r["damage"] / EXTRA_TRIGGER_EVERY
            triggered_names.append(r["name"])
            burst += r["damage"]
    if special and special["damage"] > 0:
        special_dps = special["damage"] / special["cycle"]
        burst += special["damage"]
    extra_dps = sustained_extra + special_dps
    # Mana income while casting: base regen + mana regen. Mana steal only works on main attack hits (the full
    # amount every 3 s while attacking non-stop; a negative one drains), so it counts only in the share of time
    # spent main-attacking (the mixed goal below), never in a spells-only rotation.
    income = BASE_MANA_PER_SECOND + stats.get("mr", 0) / 5
    ms_ps = stats.get("ms", 0) / 3
    # Mana refunds like Mage's Paradox ("refunding 25% of your spent mana"): Wynnbuilder's mana calculator divides
    # spell costs by these (manaMult, +34% for Paradox) when checking a rotation; the same here.
    mana_mult = mana_refund_mult(stats)
    # Best rotation of 2-4 different spells: casts/s limited by clicks and by mana.
    best_rot, best_rot_dps = None, 0.0
    usable = [bs for bs, r in results.items() if bs in (1, 2, 3, 4)]
    # Spells whose damage only hits enemies that come up to you (Arrow Shield: "enemies that get too close") aren't
    # damage you can keep up from range: spell rotations count them as 0 (they can still be cast); burst counts them.
    rot_res = {b: (dict(r, cast=0.0) if r.get("name") in CONTACT_SPELLS else r) for b, r in results.items()}
    for size in (2, 3, 4):
        for combo in itertools.combinations(usable, size):
            rs = [rot_res[b] for b in combo]
            if sum(r["cast"] for r in rs) <= 0:
                continue
            net_cost = sum(r["cost"] / mana_mult - r["mana_gained"] for r in rs) / size
            cast_rate = CLICKS_PER_SECOND / 3
            if net_cost > 0:
                cast_rate = min(cast_rate, income / net_cost)
            dps = rotation_dps(rs, cast_rate)
            if dps > best_rot_dps:
                best_rot_dps, best_rot = dps, combo
    # Mana check for that rotation (3 casts/s at 9 clicks/s, mana income 5 + mr/5 (mana steal needs main attacks),
    # mana bar 100 + max mana bonus + Intelligence %): how long full speed lasts.
    mana = None
    if best_rot:
        rs = [rot_res[b] for b in best_rot]
        full_rate = CLICKS_PER_SECOND / 3
        used = full_rate * sum(r["cost"] / mana_mult for r in rs) / len(rs)
        back = full_rate * sum(r["mana_gained"] for r in rs) / len(rs)
        net = income + back - used
        bar = 100 + stats.get("maxMana", 0) + math.floor(skill_pct(stats.get("int", 0)) * 100)
        full_dps = rotation_dps(rs, full_rate)
        mana = {"used": used, "regen": income, "from_spells": back, "net": net, "bar": max(1, bar),
                "steal": stats.get("ms", 0),
                "full_rate": full_rate, "full_dps": full_dps,
                "seconds": None if net >= -1e-9 else max(1, bar) / -net,
                "sustained_rate": full_rate if net >= -1e-9 else full_rate * max(0.0, income) / max(1e-9, used - back)}
    # Healing: best mana-limited rotation of healing spells (heal per second).
    heal_ps, heal_rot = 0.0, None
    healers = [bs for bs, r in results.items() if bs in (1, 2, 3, 4) and r.get("heal", 0) > 0]
    for size in (1, 2, 3):
        for combo in itertools.combinations(usable, size):
            if not any(b in healers for b in combo):
                continue
            rs = [results[b] for b in combo]
            heal = sum(r.get("heal", 0) for r in rs) / size
            net_cost = sum(r["cost"] / mana_mult - r["mana_gained"] for r in rs) / size
            rate = CLICKS_PER_SECOND / 3
            if net_cost > 0:
                rate = min(rate, income / net_cost)
            if size == 1:
                rate = min(rate, 1.0)     # repeating one spell back-to-back is penalised in-game
            if rate * heal > heal_ps:
                heal_ps, heal_rot = rate * heal, combo
    defense = defense or {}
    # With an enemy hit entered (--enemy), effective HP is measured against that hit (elemental defenses count).
    ehp = defense.get("ehp_enemy") or defense.get("ehp_agi", 0.0)
    spell_total = best_rot_dps + (poison_spell if best_rot_dps > 0 else 0.0)
    melee_total = melee_dps + (poison_melee if melee_dps > 0 else 0.0)
    spell_goal = spell_total + sustained_extra + special_dps
    melee_goal = melee_dps          # Wynnbuilder's "Average DPS": per-hit average x attack speed, nothing added
    # Spells and main attacks together (the helper's own estimate; Wynnbuilder has none). A cast is 3 clicks at
    # Wynnbuilder's 9 clicks a second, so casting r spells a second takes r/3 of the time; main attacks fill the
    # rest at the weapon's attack speed. Damage is straight-line in r, so the best is either no spells or the
    # fastest rate your clicks and mana allow, for whichever rotation is best. Poison counts once (the larger).
    mixed_goal, mixed_info = melee_dps + poison_melee + special_dps, {"rotation": None, "rate": 0.0, "melee_share": 1.0}
    for size in (2, 3, 4):
        for combo in itertools.combinations(usable, size):
            rs = [rot_res[b] for b in combo]
            if sum(r["cast"] for r in rs) <= 0:
                continue
            net_cost = sum(r["cost"] / mana_mult - r["mana_gained"] for r in rs) / size
            rmax = CLICKS_PER_SECOND / 3
            # mana: r casts/s cost r x net; income + mana steal x the main-attack share (1 - 3r/clicks)
            denom = net_cost + 3 * ms_ps / CLICKS_PER_SECOND
            if denom > 0:
                rmax = min(rmax, max(0.0, (income + ms_ps) / denom))
            elif rmax * denom > income + ms_ps:     # (spells that give mana back can outpace a draining steal)
                rmax = 0.0
            # spell damage only grows straight with the cast rate until a lasting spell is up all the time, so the
            # best mix is at the fastest rate or at one of those points
            for rate in [rmax] + rotation_breaks(rs, rmax):
                share = max(0.0, 1 - rate * 3 / CLICKS_PER_SECOND)
                sp_part = rotation_dps(rs, rate)
                tot = (sp_part + melee_dps * share + max(poison_spell, poison_melee if share > 0 else 0.0)
                       + sustained_extra + special_dps)
                if tot > mixed_goal:
                    mixed_goal, mixed_info = tot, {"rotation": combo, "rate": rate, "melee_share": share,
                                                   "spell_part": sp_part, "melee_part": melee_dps * share}
    best_damage = max(spell_goal, melee_total + special_dps)
    # Life steal only heals on main attack hits (wiki), like mana steal, so it counts for the share of time the build
    # spends main-attacking: all of it for main attacks, none for spells, the main-attack share between casts for most
    # damage in any style; for the other goals by whichever style deals the build's damage (owner agreed, Oct 2026).
    if goal == "melee":
        ls_share = 1.0
    elif goal in ("spells", "burst", "healing"):
        ls_share = 0.0
    elif goal == "mixed":
        ls_share = mixed_info["melee_share"] if mixed_info.get("rotation") else 1.0
    elif goal == "ability":
        ls_share = 1.0 if focus_bs == 0 else 0.0
    else:
        ls_share = 1.0 if melee_total + special_dps >= spell_goal else 0.0
    self_heal = SURVIVAL_HEAL_SECONDS * (heal_ps + life_steal_ps * ls_share) + 2 * max(defense.get("hpr", 0), 0)
    focus_val = results.get(focus_bs, {}).get("damage", 0.0) if focus_bs is not None else 0.0
    goals = {"ability": focus_val, "spells": spell_goal, "burst": burst, "melee": melee_goal, "mixed": mixed_goal,
             "survival": ehp + (ehp / max(defense.get("hp", 1), 1)) * self_heal,
             "balanced": math.sqrt(max(best_damage, 0) * max(ehp, 0)),
             "healing": heal_ps}
    main = goals[goal]
    parts = None
    if goal2 and goal2_weight > 0:              # main goal mixed with a second one by weight
        parts = {goal: main, goal2: goals[goal2]}
        main = mix_goals(main, goals[goal2], goal2_weight)
    # Small tie-breakers so spare points still go somewhere useful (other damage, then mana bank size).
    bank = sum(r["mana_gained"] for r in results.values())
    value = main * 1.0 + 0.002 * (best_rot_dps + burst / 5 + melee_dps) + 0.0005 * triggered_extra + 0.0001 * bank
    return {"value": value, "main": main, "spell_dps": best_rot_dps, "rotation": best_rot, "burst": burst,
            "melee_hit": melee, "melee_dps": melee_dps, "extra_dps": extra_dps, "mana_income": income,
            "special": special, "defense": defense, "heal_ps": heal_ps, "heal_rotation": heal_rot,
            "poison_spell": poison_spell, "poison_melee": poison_melee, "life_steal_ps": life_steal_ps, "ls_share": ls_share,
            "triggered_dps": triggered_extra, "triggered_names": triggered_names, "spell_goal": spell_goal,
            "melee_goal": melee_goal, "dps_style": "spells" if spell_goal >= melee_goal else "main attack",
            "focus_value": focus_val, "goal_parts": parts, "goal_values": goals, "mana": mana, "mixed": mixed_info}


# ============================================================================== toggles
# Wynnbuilder leaves every ability toggle off unless you turn it on, so an ability whose bonus sits behind a
# toggle adds nothing and the tree search never picks it. These toggles are switched on by default because
# their condition is part of a normal rotation for the build that takes the ability (the report says so, and
# --toggles-off or --no-auto-toggles turns them off). Toggles whose condition is hard to keep up (next hit
# after 4s, hits from behind, ultimates) stay off; the report shows what each would add.
MASKS = ("Mask of the Lunatic", "Mask of the Fanatic", "Mask of the Heretic")
# Toggles counted by default. Only buffs whose condition is part of normal play and whose timing is known:
#   "on": a standing state you keep up (Bullwhip: hit with Uproot or Haunting Memory at least every 10s)
#   "uptime": counted for the share of time it can be up, from its duration and cooldown in the game data
#   "ult": an ultimate's buff, counted for the same share of time as ultimate damage (ULT_DPS_UPTIME)
# Buffs that depend on a resource whose timing the data doesn't give (Holy Power, Tricks, Unstable, Blood
# Pool, Corrupted, "next hit" effects) stay off; the report shows what each would add.
TOGGLE_RULES = {
    "Activate Bullwhip": ("on", "you keep enemies Whipped by hitting them with Uproot or Haunting Memory at least every 10s"),
    "Coursing Restraints": ("uptime", "Shocking Bomb's shock lasts {dur:g}s and can be reapplied every {cd:g}s"),
    "Activate Angelic Ascension": ("ult", "ultimate, active about {pct:.0f}% of the time"),
    "Activate Gravitational Collapse": ("ult", "ultimate, active about {pct:.0f}% of the time"),
    "Awakened": ("ult", "ultimate, active about {pct:.0f}% of the time"),
}
AUTO_TOGGLES = {k: v[1] for k, v in TOGGLE_RULES.items()}      # (kept for older callers)


def _blend(runs):
    """Weighted average of results that have the same shape (numbers averaged; text and items from the heaviest)."""
    runs = [(w, r) for w, r in runs if w > 0]
    tot = sum(w for w, _ in runs) or 1.0
    heavy = max(runs, key=lambda x: x[0])[1]

    def mix(vals):
        first = vals[0][1]
        if isinstance(first, bool) or first is None:
            return heavy_of(vals)
        if isinstance(first, (int, float)) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for _, v in vals):
            return sum(w * v for w, v in vals) / tot
        if isinstance(first, dict) and all(isinstance(v, dict) for _, v in vals):
            out = {}
            keys = list(first) + [k for _, v in vals for k in v if k not in first]
            for k in dict.fromkeys(keys):
                sub = [(w, v[k]) for w, v in vals if k in v]
                if len(sub) == len(vals):
                    out[k] = mix(sub)
                elif all(isinstance(x, (int, float)) and not isinstance(x, bool) for _, x in sub):
                    out[k] = mix([(w, v.get(k, 0.0)) for w, v in vals])    # a stat missing in a run is 0 there
                else:
                    out[k] = heavy_of(sub)
            if isinstance(first, defaultdict):
                dd = defaultdict(first.default_factory)
                dd.update(out)
                return dd
            return out
        if isinstance(first, (list, tuple)) and all(isinstance(v, (list, tuple)) and len(v) == len(first) for _, v in vals) \
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for _, v in vals for x in v):
            res = [sum(w * v[i] for w, v in vals) / tot for i in range(len(first))]
            return type(first)(res) if isinstance(first, tuple) else res
        return heavy_of(vals)

    def heavy_of(vals):
        return max(vals, key=lambda x: x[0])[1]
    return mix(runs)


def _node_id(tree, name):
    ids = tree.__dict__.setdefault("_name_ids", {})
    if name not in ids:
        n = next((x for x in tree.nodes if x.name == name), None)
        ids[name] = n.id if n is not None else None
    return ids[name]


def _owns(tree, owned, name) -> bool:
    i = _node_id(tree, name)
    return i is not None and i in owned


# A default buff that can't be kept up with some trees: (condition, why it's off when the condition fails)
TOGGLE_NEEDS = {
    "Activate Bullwhip": (lambda tree, owned: not _owns(tree, owned, "Mystic Masks") or _owns(tree, owned, "Haunting Memory"),
                          "with Mystic Masks, Uproot switches masks instead of hitting, so Bullwhip needs Haunting Memory"),
}


def tree_archetypes(tree) -> list:
    """This class's archetypes (e.g. Archer: Boltslinger, Sharpshooter, Trapper)."""
    return sorted({n.archetype for n in tree.nodes if getattr(n, "archetype", None)})


def archetype_counts(tree, owned) -> dict:
    """{archetype: abilities of it in the tree} (one per ability, like the game's archetype requirements)."""
    of = tree.__dict__.get("_arch_of")
    if of is None:
        of = tree.__dict__["_arch_of"] = {n.id: n.archetype for n in tree.nodes if getattr(n, "archetype", None)}
    cnt = {}
    for i in owned:
        a = of.get(i)
        if a:
            cnt[a] = cnt.get(a, 0) + 1
    return cnt


def archetype_core(tree) -> dict:
    """{ability id: archetype} for abilities that make an archetype's play style: anything that changes a spell or how
    you move or fight, adds a spell, brings a state or stacks to manage (toggle, slider), or needs points in that
    archetype first. What's left are plain stat boosts (element masteries and the like) that a build of another style
    can borrow (owner, Oct 2026: "if a Battle Monk node improves Paladin, that's fine ... as long as the general play
    style isn't affected")."""
    core = tree.__dict__.get("_arch_core")
    if core is None:
        core = {}
        for n in tree.nodes:
            if not getattr(n, "archetype", None):
                continue
            effs = n.raw.get("effects", [])
            plain = n.arch_req == 0 and effs and all(
                e.get("type") in ("raw_stat", "stat_scaling") and not e.get("toggle") and not e.get("slider")
                for e in effs)
            if not plain:
                core[n.id] = n.archetype
        tree.__dict__["_arch_core"] = core
    return core


def avoided_core(tree, owned, avoid) -> list:
    """Play-style abilities in the tree from archetypes you said to leave out."""
    core = archetype_core(tree)
    return [i for i in owned if core.get(i) in avoid]


def archetype_lead(tree, owned, name) -> tuple:
    """(abilities of archetype `name` in the tree, most abilities of any other archetype)."""
    cnt = archetype_counts(tree, owned)
    return cnt.get(name, 0), max((v for k, v in cnt.items() if k != name), default=0)


def archetype_line(tree, owned, main=None, avoid=()) -> str:
    """'Archetypes in this tree: Trapper 9, Boltslinger 4, Sharpshooter 0 (main: Trapper)'."""
    cnt = {a: 0 for a in tree_archetypes(tree)}
    cnt.update(archetype_counts(tree, owned))
    txt = "Archetypes in this tree: " + ", ".join(f"{a} {n}" for a, n in sorted(cnt.items(), key=lambda x: -x[1]))
    if main:
        mine = cnt.get(main, 0)
        ok = mine > max((v for k, v in cnt.items() if k != main), default=0)
        txt += f" (your main archetype {main}: " + ("the most, as asked)" if ok else "couldn't be made the most here)")
    for a in avoid:
        bad = avoided_core(tree, owned, (a,))
        borrowed = [tree.by_id[i].name for i in owned if getattr(tree.by_id[i], "archetype", None) == a and i not in bad]
        if bad:
            txt += f"; {len(bad)} {a} play-style abilities couldn't be avoided at this level"
        else:
            txt += f"; no {a} play style, as asked"
        if borrowed:
            txt += f" (borrowed only plain boosts: {', '.join(borrowed)})"
    return txt


def match_archetype(tree, text):
    """The archetype a player means ("sharp", "trapper", "battle monk"), or None; raises ValueError if unclear."""
    t = (text or "").strip().lower().replace("-", " ")
    if not t or t in ("any", "none", "no"):
        return None
    names = tree_archetypes(tree)
    hit = [a for a in names if a.lower() == t] or [a for a in names if a.lower().startswith(t)] or \
          [a for a in names if t in a.lower()]
    if len(hit) != 1:
        raise ValueError(f"Unknown archetype {text!r}; {CLASS_TITLE[tree.cls]} archetypes: {', '.join(names)}.")
    return hit[0]


def auto_toggles_for(tree) -> dict:
    """{toggle name: (share of the time on, why)} counted by default for this class's tree."""
    out = {}
    for n in tree.nodes:
        props = n.raw.get("properties") or {}
        for e in n.raw.get("effects", []):
            t = e.get("toggle")
            if not t or t not in TOGGLE_RULES or t in out:
                continue
            mode, why = TOGGLE_RULES[t]
            if mode == "on":
                out[t] = (1.0, why)
            elif mode == "uptime":
                dur, cd = float(props.get("duration") or 0), float(props.get("cooldown") or 0)
                if dur > 0 and cd > 0:
                    out[t] = (min(1.0, dur / cd), why.format(dur=dur, cd=cd))
            elif mode == "ult":
                out[t] = (ULT_DPS_UPTIME, why.format(pct=ULT_DPS_UPTIME * 100))
    return out


def inter_names(tree, owned) -> set:
    """Toggle names the abilities in this tree have."""
    names = set()
    for i in owned:
        n = tree.by_id.get(i)
        if n is not None:
            names |= {e["toggle"] for e in n.raw.get("effects", []) if e.get("toggle")}
    return names


def parse_toggle_arg(tree, text: str):
    """"Contamination=70%, Activate Luster Purge" -> {resolved name: share} (1.0 when no share is given)."""
    out = {}
    for part in [p.strip() for p in (text or "").split(",") if p.strip()]:
        m = re.match(r"^(.*?)\s*(?:=|:|\s)\s*(\d+(?:\.\d+)?)\s*%\s*$", part)
        name, share = (m.group(1), float(m.group(2)) / 100) if m else (part, 1.0)
        out[resolve_toggles(tree, [name])[0]] = max(0.0, min(1.0, share))
    return out


def resolve_toggles(tree, names) -> list:
    """Match what you typed ("Bullwhip", "activate bullwhip") to the tree's toggle names."""
    pairs = [(e["toggle"], n.name) for n in tree.nodes for e in n.raw.get("effects", []) if e.get("toggle")]
    norm = lambda x: re.sub(r"[^a-z0-9]", "", str(x).lower())
    out = []
    for raw in names:
        q = norm(raw)
        hit = next((t for t, _n in pairs if norm(t) == q), None) or \
            next((t for t, n in pairs if norm(n) == q or norm(t) == "activate" + q), None) or \
            next((t for t, n in pairs if q and (q in norm(t) or q in norm(n))), None)
        name = hit or raw
        if name not in out:
            out.append(name)
    return out


# ============================================================================== minimum stats ("floors")
# A floor is a minimum the build must meet, e.g. at least 20,000 health or no negative walk speed. The search
# maximizes the goal only among builds that meet every floor.

FLOOR_STATS = {
    # key: (label, unit, how to read it from (stats, defense))
    "hp": ("Health", "", lambda st, df: df.get("hp", 0.0)),
    "ehp": ("Effective HP", "", lambda st, df: df.get("ehp_enemy") or df.get("ehp_agi", 0.0)),
    "spd": ("Walk speed", "%", lambda st, df: st.get("spd", 0.0)),
    "mr": ("Mana regen", "/5s", lambda st, df: st.get("mr", 0.0)),
    "hpr": ("Health regen", "", lambda st, df: df.get("hpr", 0.0)),
    "ls": ("Life steal", "/3s", lambda st, df: st.get("ls", 0.0)),
    "ms": ("Mana steal", "/3s", lambda st, df: st.get("ms", 0.0)),
    # Never losing health: health gained per second from health regen (ticks every 4 s, wiki) plus life steal (per
    # 3 s while you keep main-attacking, wiki). Either can be negative as long as the total isn't. Life steal only
    # counts for the share of time the build main-attacks (floor_values; none for spell-only builds).
    "net": ("Health gained per second (regen + life steal)", "/s",
            lambda st, df: df.get("hpr", 0.0) / 4 + st.get("ls", 0.0) / 3),
    "eDef": ("Earth defense", "", lambda st, df: df.get("elem_def_eff", [0] * 5)[0]),
    "tDef": ("Thunder defense", "", lambda st, df: df.get("elem_def_eff", [0] * 5)[1]),
    "wDef": ("Water defense", "", lambda st, df: df.get("elem_def_eff", [0] * 5)[2]),
    "fDef": ("Fire defense", "", lambda st, df: df.get("elem_def_eff", [0] * 5)[3]),
    "aDef": ("Air defense", "", lambda st, df: df.get("elem_def_eff", [0] * 5)[4]),
}
FLOOR_ALIASES = {
    "hp": "hp", "health": "hp", "life": "hp", "ehp": "ehp", "effectivehp": "ehp", "effective": "ehp",
    "spd": "spd", "walkspeed": "spd", "speed": "spd", "walk": "spd", "ws": "spd", "movespeed": "spd",
    "mr": "mr", "manaregen": "mr", "mana": "mr", "hpr": "hpr", "healthregen": "hpr", "regen": "hpr", "hpregen": "hpr",
    "ls": "ls", "lifesteal": "ls", "lifestealing": "ls", "ms": "ms", "manasteal": "ms",
    "net": "net", "nethealth": "net", "nethp": "net", "netregen": "net", "healthpersecond": "net", "sustain": "net",
    "neverlosehealth": "net", "nohealthloss": "net", "healthgain": "net",
    "earth": "eDef", "earthdef": "eDef", "earthdefense": "eDef", "edef": "eDef",
    "thunder": "tDef", "thunderdef": "tDef", "thunderdefense": "tDef", "tdef": "tDef",
    "water": "wDef", "waterdef": "wDef", "waterdefense": "wDef", "wdef": "wDef",
    "fire": "fDef", "firedef": "fDef", "firedefense": "fDef", "fdef": "fDef",
    "air": "aDef", "airdef": "aDef", "airdefense": "aDef", "adef": "aDef",
}
FLOOR_PENALTY = 1e9


def clean_floors(floors) -> dict:
    """{key: minimum} with known keys only (accepts names like "health" or "walk speed")."""
    out = {}
    for k, v in dict(floors or {}).items():
        key = FLOOR_ALIASES.get(re.sub(r"[^a-z]", "", str(k).lower()))
        if key is None or v is None or v == "":
            continue
        try:
            out[key] = float(str(v).replace(",", "").replace("%", ""))
        except ValueError:
            continue
    return out


def parse_floors(text: str) -> dict:
    """'hp=20000, walk speed=0, fire def=100' (or 'hp 20000; speed 0') -> {"hp": 20000, "spd": 0, "fDef": 100}"""
    out = {}
    for part in re.split(r"[;,]\s*(?=[A-Za-z])", text or ""):
        m = re.match(r"\s*([A-Za-z][A-Za-z ._-]*?)\s*(?:=|:|>=|\s)\s*(-?[\d,.]+)\s*%?\s*$", part)
        if not m:
            raise ValueError(f"can't read minimum {part.strip()!r}; use e.g. \"hp=20000, walk speed=0\"")
        key = FLOOR_ALIASES.get(re.sub(r"[^a-z]", "", m.group(1).lower()))
        if key is None:
            raise ValueError(f"unknown stat {m.group(1).strip()!r} for a minimum; use one of: health, ehp, "
                             "walk speed, mana regen, health regen, life steal, mana steal, net health, earth/thunder/water/fire/air defense")
        out[key] = float(m.group(2).replace(",", ""))
    return out


def floor_values(stats, defense, ls_share: float = 1.0) -> dict:
    """Each minimum's value. ls_share: share of time spent main-attacking (life steal works only then)."""
    out = {k: fn(stats, defense) for k, (_l, _u, fn) in FLOOR_STATS.items()}
    out["net"] = defense.get("hpr", 0.0) / 4 + stats.get("ls", 0.0) / 3 * ls_share
    return out


def floor_shortfall(floors: dict, vals: dict) -> float:
    """How far below the minimums a build is (0 when it meets them all), each as a fraction of its scale."""
    short = 0.0
    for k, need in floors.items():
        have = vals.get(k, 0.0)
        if have < need - 1e-9:
            short += (need - have) / max(abs(need), 100.0 if k in ("hp", "ehp") else 10.0)
    return short


def floor_misses(floors: dict, vals: dict) -> list:
    """['Health 14,200 (needs 20,000)', ...] for each minimum not met."""
    out = []
    for k, need in floors.items():
        have = vals.get(k, 0.0)
        if have < need - 1e-9:
            label, unit, _fn = FLOOR_STATS[k]
            f = (lambda x: fmt_num(x) if abs(x) >= 1000 else f"{x:.0f}")
            out.append(f"{label} {f(have)}{unit} (needs {f(need)}{unit})")
    return out


def fmt_speed(x: float) -> str:
    return f"{x:+.0f}%"


def describe_floors(floors: dict) -> str:
    return ", ".join(f"{FLOOR_STATS[k][0]} at least {fmt_num(v) if abs(v) >= 1000 else f'{v:g}'}{FLOOR_STATS[k][1]}"
                     for k, v in floors.items() if k in FLOOR_STATS)


# ============================================================================== build evaluation

EVAL_STATS = {"n": 0, "tick": None}
LAST_RESULT: dict = {}      # the last report's builds as objects (the app turns them into its visuals)     # how many builds were scored; tick() is called every 256


class Build:
    """Everything needed to score one combination of tree + gear + skill points."""

    def __init__(self, data: GameData, tree: Tree, level: int, goal: str, tomes=(), aspects=(),
                 toggles=frozenset(), sliders=None, slider_mode="none", buffs=(), raid_buffs=(), enemy=None,
                 focus=None, floors=None, toggle_share=None, auto_masks=False, toggles_off=(), auto_toggles=(),
                 goal2=None, goal2_weight=0.0, archetype=None, avoid_archetypes=()):
        self.data, self.tree, self.level, self.goal = data, tree, level, goal
        # the archetype you want to play mainly: the tree must have more of its abilities than of any other archetype
        # (abilities from other paths are fine when they help); None = any
        self.archetype = archetype or None
        # archetypes you don't want to play: none of their abilities that change how you play (only plain boosts that
        # help your build, like an element mastery; see archetype_core)
        self.avoid_archetypes = tuple(sorted(set(avoid_archetypes or ())))
        self.goal2, self.goal2_weight = clean_goal2(goal, goal2, goal2_weight)   # optional second goal and its weight
        self.toggle_share = {k: float(v) for k, v in (toggle_share or {}).items() if 0 < float(v) < 1}
        self.auto_masks = bool(auto_masks)  # wear whichever Mystic Mask helps the goal most
        self.floors = clean_floors(floors)  # minimum stats every build must meet, e.g. {"hp": 20000, "spd": 0}
        self.focus = focus                 # (spell slot, name, node id) for the "ability" goal
        self.enemy = dict(enemy or {})     # element index -> damage per enemy hit (--enemy)
        self.tomes = list(tomes)          # Item objects
        self.aspects = list(aspects)      # (aspect dict, tier 1-4)
        self.toggles = frozenset(toggles)
        self.toggles_off = frozenset(toggles_off)   # toggles you turned off (--toggles-off)
        self.auto_toggles = frozenset(auto_toggles)  # the ones on because of the defaults (not because you said so)
        self.sliders = dict(sliders or {})
        self.slider_mode = slider_mode
        self.buffs = tuple(buffs)          # party/potion buffs, names from BUFFS
        self.raid_buffs = tuple(raid_buffs)

    def copy_with(self, **kw):
        fields = dict(level=self.level, goal=self.goal, tomes=self.tomes, aspects=self.aspects,
                      toggles=self.toggles, sliders=self.sliders, slider_mode=self.slider_mode,
                      buffs=self.buffs, raid_buffs=self.raid_buffs, enemy=self.enemy, focus=self.focus,
                      floors=self.floors, toggle_share=self.toggle_share, auto_masks=self.auto_masks,
                      toggles_off=self.toggles_off, auto_toggles=self.auto_toggles, goal2=self.goal2,
                      goal2_weight=self.goal2_weight, archetype=self.archetype,
                      avoid_archetypes=self.avoid_archetypes)
        fields.update(kw)
        return Build(self.data, self.tree, **fields)

    def required_points(self, armor, weapon):
        return solve_skill_points(armor, weapon)

    def extra_abilities(self, armor, weapon, extra_majids=()):
        """Aspect tiers and item major IDs as extra ability nodes (Wynnbuilder order: aspects, then major IDs)."""
        out = []
        for asp, tier in self.aspects:
            tiers = asp.get("tiers", [])
            if not tiers:
                continue
            t = tiers[max(0, min(tier, len(tiers)) - 1)]
            for j, ab in enumerate(t.get("abilities") or []):
                out.append((("asp", asp["displayName"], tier, j), ab))
        cls_title = CLASS_TITLE[self.tree.cls]
        seen = set()
        sources = [a.major_ids or [] for a in armor if a is not None] + [weapon.major_ids or [] if weapon else []]
        sources.append(list(extra_majids))
        for mids in sources:
            for mid in mids:
                if mid in seen or mid not in self.data.majids:
                    continue
                seen.add(mid)
                for j, ab in enumerate(majid_abilities(self.data, mid)):
                    if ab.get("class") in (cls_title, "Any"):
                        out.append((("mid", mid, j), ab))
        return tuple(out)

    @staticmethod
    def _item_code(it):
        if it is None:
            return None
        return (it.name, repr(it.link_code) if it.link_code else None, tuple(repr(p) for p in (it.powders or ())))

    def evaluate(self, owned, armor, weapon, extra_points=None, fast=False):
        """Score a build, remembering results: the same build (gear, powders, tree, skill points) is only
        worked out once per Build. fast=True (searches): only the goal's number is kept."""
        key = (frozenset(owned), tuple(self._item_code(a) for a in armor), self._item_code(weapon),
               tuple(extra_points) if extra_points else None, fast, ROLL_MODE, POISON_MODE)
        cache = self.__dict__.setdefault("_eval_cache", {})
        hit = cache.get(key)
        if hit is not None:
            self.__dict__["_eval_hits"] = self.__dict__.get("_eval_hits", 0) + 1
            return dict(hit)
        res = self._evaluate(owned, armor, weapon, extra_points, fast)
        EVAL_STATS["n"] += 1                       # builds actually scored (for the live view in the app)
        if EVAL_STATS["tick"] is not None and not EVAL_STATS["n"] & 255:
            EVAL_STATS["tick"]()
        if len(cache) > (300000 if fast else 20000):
            cache.clear()
        cache[key] = {"value": res["value"], "main": res["main"]} if fast else res
        return dict(res) if not fast else res

    def toggle_map(self, owned=None) -> dict:
        """{toggle name: share of the time on}: the ones you (or the defaults) turned on, and partial ones.
        With `owned`, default buffs this tree can't keep up are left out (see TOGGLE_NEEDS)."""
        m = {t: 1.0 for t in self.toggles}
        m.update(self.toggle_share)
        if owned is not None:
            for t, (ok, _why) in TOGGLE_NEEDS.items():
                if t in m and t in self.auto_toggles and not ok(self.tree, owned):
                    del m[t]
        return m

    def mask_options(self, owned) -> list:
        """The Mystic Masks to try, if this tree has them and you didn't pick one yourself."""
        if not self.auto_masks:
            return []
        if not _owns(self.tree, owned, "Mystic Masks"):
            return []
        tm = self.toggle_map(owned)
        if any(m in tm for m in MASKS):
            return []
        return list(MASKS)

    def _evaluate(self, owned, armor, weapon, extra_points=None, fast=False):
        masks = self.mask_options(owned)
        if not masks:
            return self._evaluate_shares(owned, armor, weapon, extra_points, fast, self.toggle_map(owned))
        best = None
        transmute_id = _node_id(self.tree, "Transmute")
        for m in masks:          # you always wear one mask: use the one that helps your goal most
            tm = dict(self.toggle_map(owned))
            tm[m] = 1.0
            if m == "Mask of the Lunatic" and transmute_id is not None and transmute_id in owned \
                    and "Lunatic Transmute" not in self.toggles_off:
                tm["Lunatic Transmute"] = 1.0
            r = self._evaluate_shares(owned, armor, weapon, extra_points, fast, tm)
            if best is None or r["value"] > best["value"]:
                best, best_mask = r, m
        best = dict(best)
        best["mask"] = best_mask
        return best

    def _evaluate_shares(self, owned, armor, weapon, extra_points, fast, tog):
        """Buffs that are up part of the time. Ones whose bonuses only add damage multipliers, walk speed or mana
        regen are exact when scaled by their share (damage grows in a straight line with them). For any other
        buff (attack counts, element conversions, defense, spell costs) the build is worked out with the buff on
        and off and the two are averaged by the share of time: the exact average over a fight."""
        kinds = self.tree.toggle_bonus_kinds()
        # While searching (fast), buffs that only change damage multipliers and element conversions are scaled
        # (within about 0.1%); the numbers you're shown (fast=False) are always worked out exactly.
        ok = ("linear", "conv") if fast else ("linear",)
        split = [k for k, f in tog.items() if 0 < f < 1 and kinds.get(k, "linear") not in ok]
        if not split:
            return self._evaluate_with(owned, armor, weapon, extra_points, fast, tog)
        runs = []
        for combo in itertools.product((0, 1), repeat=len(split)):
            t2, w = dict(tog), 1.0
            for k, on in zip(split, combo):
                f = tog[k]
                w *= f if on else (1 - f)
                if on:
                    t2[k] = 1.0
                else:
                    t2.pop(k, None)
            if w > 0:
                runs.append((w, self._evaluate_with(owned, armor, weapon, extra_points, fast, t2)))
        return _blend(runs)

    def _evaluate_with(self, owned, armor, weapon, extra_points, fast, tog):
        assigned, totals, order = solve_skill_points(armor, weapon)
        extra = extra_points or [0] * 5
        sb = set_bonuses(armor, weapon, self.data)
        tome_sp = [sum(t.bonus[i] for t in self.tomes) for i in range(5)]
        rb_stats, rb_majids = raid_buff_stats(self.raid_buffs)
        # Set, tome and raid-buff skill points add to totals but don't help meet requirements (Wynnbuilder).
        totals = [totals[i] + extra[i] + int(sb.get(SKILLS[i], 0)) + tome_sp[i] + int(rb_stats.get(SKILLS[i], 0))
                  for i in range(5)]
        spells, tstats, dmult, deferred, _inter = self.tree.spells(
            owned, self.extra_abilities(armor, weapon, rb_majids), tog, self.sliders, self.slider_mode)
        stats = build_stats(armor, weapon, totals, tstats, self.data, self.tomes, level=self.level, sb=sb)
        for k, v in rb_stats.items():
            if k not in SKILLS:
                stats[k] += v
        dmult = dict(dmult)
        for k, v in buff_mults(self.buffs).items():
            if k in NONSTACKING_MULTS and k in dmult:
                dmult[k] = max(dmult[k], v)
            else:
                dmult[k] = dmult.get(k, 0) + v
        # Scalings that read your gear stats (e.g. attack speed from main attack damage).
        for eff, props in deferred:
            total = 0.0
            for sc, inp in zip(eff.get("scaling", [0]), eff.get("inputs", [])):
                scv = sc if not isinstance(sc, str) else (props.get(int(sc.split(".")[0]), {}).get(sc.split(".", 1)[1], 0) or 0)
                if inp.get("type") == "stat":
                    total += stats.get(inp["name"], 0) * scv
                else:
                    total += (props.get(inp.get("abil"), {}).get(inp["name"], 0) or 0) * scv
            total = _finish_scaling(eff, total + (eff.get("base", 0) if not isinstance(eff.get("base", 0), str) else 0),
                                    eff.get("positive", True), lambda v: v if not isinstance(v, str) else 0)
            for out in (eff["output"] if isinstance(eff["output"], list) else [eff["output"]]):
                if out.get("type") != "stat":
                    continue
                name = out["name"]
                if name.startswith("damMult."):
                    dmult[name[8:]] = dmult.get(name[8:], 0) + total
                elif "." not in name:
                    stats[name] += total
        special = None
        if getattr(weapon, "special", None):
            elem, lvl = weapon.special
            name, dmg_list, boost_list, dur = WEAPON_SPECIALS[elem]
            cycle = SPECIAL_CHARGE_SECONDS + 1 if self.goal in ("melee", "dps") else 3 * SPECIAL_CHARGE_SECONDS
            if any("SELFSUFFICIENT" in (x.major_ids or ()) for x in list(armor) + [weapon] if x is not None):
                cycle = SELF_CHARGE_SECONDS        # fills by itself, and can't be charged by hitting any more
            uptime = 1.0 if self.goal == "burst" else min(1.0, dur / cycle)
            dmg = 0.0
            if dmg_list:
                conv = [0] * 6
                conv[elem + 1] = dmg_list[lvl - 1]
                n, c = spell_part_damage(stats, weapon, conv, False, True, False, dmult)
                cc = skill_pct(stats.get("dex", 0))
                dmg = n * (1 - cc) + c * cc
            if boost_list and self.goal != "melee":
                dmult["PowderSpecial"] = boost_list[lvl - 1] * uptime
            special = {"name": name, "level": lvl, "damage": dmg, "cycle": cycle,
                       "boost": boost_list[lvl - 1] if boost_list else 0, "uptime": uptime}
        if fast and self.goal == "melee" and self.goal2 in (None, "melee"):
            spells = {k: v for k, v in spells.items() if k == 0}
        elif fast and self.goal == "ability" and self.focus and not self.goal2:
            spells = {k: v for k, v in spells.items() if k == self.focus[0]}
        heal_items, def_items = [], []
        for k, v in stats.items():                    # one pass for both kinds of multiplier (same order)
            if isinstance(k, str) and k[:5] in ("healM", "defMu"):
                if k.startswith("healMult."):
                    heal_items.append((k, v))
                elif k.startswith("defMult."):
                    def_items.append((k, v))
        res = evaluate_spells(spells, stats, weapon, dmult, heal_items)
        def_mults = {}
        for k, v in def_items:
            def_mults[k[8:]] = def_mults.get(k[8:], 0) + v
        for k, v in buff_mults(self.buffs, 1).items():
            def_mults[k] = max(def_mults.get(k, 0), v) if k in NONSTACKING_MULTS else def_mults.get(k, 0) + v
        defense = defense_stats(stats, weapon, def_mults, self.enemy)
        sc = score_build(res, stats, weapon, self.goal, self.tree.ult_spells, special, defense,
                         focus_bs=self.focus[0] if self.focus else None, goal2=self.goal2, goal2_weight=self.goal2_weight)
        if self.floors or self.archetype or self.avoid_archetypes:
            short = 0.0
            if self.floors:
                vals = floor_values(stats, defense, sc["ls_share"])
                short = floor_shortfall(self.floors, vals)
                sc["floor_values"] = vals
            if self.archetype:
                # your main archetype must have more abilities in the tree than any other (counted like the game's
                # archetype requirements: one per ability); each one short counts like a missed minimum
                mine, other = archetype_lead(self.tree, owned, self.archetype)
                sc["archetype_counts"] = (mine, other)
                if mine <= other:
                    short += (other - mine + 1) / 5.0
            if self.avoid_archetypes:
                # archetypes you don't want: each of their play-style abilities counts like a missed minimum
                n_bad = len(avoided_core(self.tree, owned, self.avoid_archetypes))
                if n_bad:
                    short += n_bad / 5.0
            sc["floors_met"] = short <= 0
            if short > 0:
                # Below a minimum: worse than any build that meets them all, and the closer it gets, the higher
                # it scores, so the search can climb toward builds that qualify.
                sc["value"] = -FLOOR_PENALTY * short - 1.0
        sc.update({"results": res, "stats": stats, "assigned": assigned, "totals": totals, "order": order})
        return sc

    def allocate_free_points(self, owned, armor, weapon, step: int = 2):
        """Greedy: spend leftover skill points where they raise the goal score most."""
        assigned, _, _ = solve_skill_points(armor, weapon)
        free = skill_points_available(self.level) - sum(assigned)
        extra = [0] * 5
        if free < 0:
            return extra, free
        base = self.evaluate(owned, armor, weapon, extra)["value"]
        while free > 0:
            best_i, best_v = None, base
            chunk = min(step, free)
            for i in range(5):
                if assigned[i] + extra[i] + chunk > MAX_ASSIGNED_PER_SKILL:
                    continue
                trial = extra[:]
                trial[i] += chunk
                v = self.evaluate(owned, armor, weapon, trial)["value"]
                if v > best_v + 1e-9:
                    best_i, best_v = i, v
            if best_i is None:
                break
            extra[best_i] += chunk
            free -= chunk
            base = best_v
        return extra, free


# ============================================================================== optimizer

def optimize_tree(build: Build, armor, weapon, ap_budget: int, keep=(), start=(), extra_points=None,
                  beam_width: int = 60):
    """
    Beam search for the highest-scoring legal tree within the ability point budget.
    keep: node ids that must be in the result. start: node ids already owned (plan mode).
    """
    tree = build.tree
    keep = set(keep)
    start_owned = frozenset(start) if start else frozenset()
    if not start_owned:
        start_owned = frozenset(r for r in tree.roots)
    arch0 = defaultdict(int)
    for i in start_owned:
        a = tree.by_id[i].archetype
        if a:
            arch0[a] += 1
    cache = {}

    def value(owned):
        if owned not in cache:
            cache[owned] = build.evaluate(owned, armor, weapon, extra_points)["value"]
        return cache[owned]

    def ap(owned):
        return sum(tree.by_id[i].cost for i in owned)

    level = build.level
    # Guidance toward must-have nodes: their ancestors, required nodes and archetype minimums.
    anc = {}
    for k in keep:
        stack, seen_a = [k], {k}
        while stack:
            cur = tree.by_id[stack.pop()]
            for p in cur.parents + cur.deps:
                if p not in seen_a:
                    seen_a.add(p)
                    stack.append(p)
        anc[k] = seen_a

    def progress(owned, arch):
        total = 0.0
        for k in keep:
            if k in owned:
                total += 1000
                continue
            kn = tree.by_id[k]
            if kn.arch_req:
                total += min(arch.get(kn.req_arch, 0), kn.arch_req)
            total += 0.1 * len(owned & anc[k])
        return total

    beam = [(start_owned, dict(arch0))]
    best = (value(start_owned) if keep <= start_owned else float("-inf"), start_owned)
    seen = {start_owned}
    while beam:
        candidates = []
        for owned, arch in beam:
            spent = ap(owned)
            for n in tree.frontier(owned, arch, level):
                if spent + n.cost > ap_budget:
                    continue
                new = owned | {n.id}
                if new in seen:
                    continue
                seen.add(new)
                narch = dict(arch)
                if n.archetype:
                    narch[n.archetype] = narch.get(n.archetype, 0) + 1
                candidates.append((new, narch))
        if not candidates:
            break
        scored = []
        for owned, arch in candidates:
            v = value(owned)
            if keep <= owned and v > best[0]:
                best = (v, owned)
            # Rank: progress toward must-have nodes first, then value, then cheaper.
            scored.append(((progress(owned, arch), v, -ap(owned)), owned, arch))
        scored.sort(key=lambda x: x[0], reverse=True)
        # Keep diversity: best states overall plus the best state per archetype mix.
        chosen, by_mix = [], {}
        for s in scored:
            mix = tuple(sorted(s[2].items()))
            if mix not in by_mix:
                by_mix[mix] = s
        # ... and the states furthest along each archetype: abilities that need an archetype count (like Orphion's
        # Pulse, 5 Light Bender) often only pay off after several abilities that change nothing on their own, which a
        # beam ranked by score alone drops on the way (a mage survival tree once lost it: 1,352 instead of 2,163).
        far = []
        archs = {a for _sc, _o, ar in scored for a in ar}
        for a in sorted(archs):
            far += sorted(scored, key=lambda x: (x[2].get(a, 0), x[0]), reverse=True)[: max(1, beam_width // 12)]
        pool = list(by_mix.values())[: beam_width // 3] + far + scored
        uniq = []
        ids = set()
        for s in pool:
            if s[1] not in ids:
                ids.add(s[1])
                uniq.append(s)
            if len(uniq) >= beam_width:
                break
        beam = [(s[1], s[2]) for s in uniq]
    return best[1], best[0]


def improve_tree(build: Build, armor, weapon, ap_budget: int, owned, keep=(), extra_points=None,
                 rounds: int = 4, refill_width: int = 15):
    """
    Local search after the beam search: drop one or two abilities that can be removed legally
    (keeping must-haves), refill the freed points with a small search, keep any improvement.
    """
    tree = build.tree
    keep = set(keep)
    level = build.level

    def val(o):
        return build.evaluate(o, armor, weapon, extra_points)["value"]

    best, best_v = frozenset(owned), val(owned)
    for _ in range(rounds):
        improved = False
        removable = [n for n in best if n not in keep and n not in tree.roots
                     and tree.validate(best - {n}, level)["ok"]]
        candidates = [frozenset([n]) for n in removable]
        candidates += [frozenset(p) for p in itertools.combinations(removable, 2)
                       if tree.validate(best - set(p), level)["ok"]]
        for drop in candidates:
            start = best - drop
            new, v = optimize_tree(build, armor, weapon, ap_budget, keep=keep, start=start,
                                   extra_points=extra_points, beam_width=refill_width)
            if v > best_v * 1.0005 and tree.validate(new, level)["ok"]:
                best, best_v, improved = frozenset(new), v, True
                break
        if not improved:
            break
    return best, best_v


def best_tree(build: Build, armor, weapon, ap_budget: int, keep=(), extra_points=None, beam_width: int = 60):
    """Run the open search plus one guided search per reachable ultimate; keep the best result."""
    tree = build.tree
    runs = [tuple(keep)]
    if build.level >= ULTIMATE_LEVEL:
        for n in tree.nodes:
            if n.ultimate and n.cost <= ap_budget:
                runs.append(tuple(keep) + (n.id,))
    best = (None, float("-inf"))
    for k in runs:
        owned, v = optimize_tree(build, armor, weapon, ap_budget, keep=k, extra_points=extra_points,
                                 beam_width=beam_width)
        if v > best[1] and set(keep) <= set(owned):
            best = (owned, v)
    if best[0] is not None:
        best = improve_tree(build, armor, weapon, ap_budget, best[0], keep=keep, extra_points=extra_points)
    return best


def best_weapon_powders(build: Build, owned, armor, base_weapon, extra_points, max_tier: int = 7, quick=False):
    """
    Try every element sequence for the weapon's powder slots (highest allowed tier). Order matters:
    it changes how neutral damage converts and which special you unlock.
    quick=True only tries all-one-element fills (used when scanning many weapons).
    Returns (powders, value).
    """
    slots = base_weapon.slots
    if slots <= 0:
        return [], build.evaluate(owned, armor, base_weapon, extra_points)["value"]
    tier = max_powder_tier(getattr(base_weapon, "level_high", base_weapon.level), max_tier)
    if quick:
        seqs = [(e,) * slots for e in range(5)]
    else:
        seqs, seen = [], set()
        for seq in itertools.product(range(5), repeat=slots):
            first_order = tuple(dict.fromkeys(seq))
            key = (first_order, tuple(sorted(seq)))
            if key not in seen:
                seen.add(key)
                seqs.append(seq)
    best = ([], build.evaluate(owned, armor, weapon_with_powders(base_weapon, []), extra_points)["value"])
    for seq in seqs:
        pw = [Powder(e, tier) for e in seq]
        v = build.evaluate(owned, armor, weapon_with_powders(base_weapon, pw), extra_points)["value"]
        if v > best[1]:
            best = (pw, v)
    return best


def best_armor_powders(build: Build, owned, armor_base, armor_cur, weapon, extra, max_tier: int = 7):
    """
    Best powders for each armor piece (one element per piece, highest tier it accepts), slot by slot.
    Returns ({slot: (powders, gain, element_matters)}, new armor list). element_matters is False when every
    element scores the same (armor powders all add the same health; the element only matters against an
    enemy element, see --enemy).
    """
    cur = list(armor_cur)
    picks = {}
    for s in range(4):
        base = armor_base[s]
        if base is None or base.slots <= 0:
            continue
        tier = max_powder_tier(getattr(base, "level_high", base.level), max_tier)
        now = build.evaluate(owned, cur, weapon, extra)["value"]
        scores = []
        for e in range(5):
            trial = list(cur)
            trial[s] = armor_with_powders(base, [Powder(e, tier)] * base.slots)
            scores.append((build.evaluate(owned, trial, weapon, extra)["value"], e, trial))
        best_v, best_e, best_trial = max(scores, key=lambda x: x[0])
        if best_v > now * 1.001:
            matters = max(x[0] for x in scores) - min(x[0] for x in scores) > 1e-6 * max(best_v, 1)
            cur = best_trial
            picks[s] = ([Powder(best_e, tier)] * base.slots, best_v / now - 1 if now > 0 else 0, matters)
    return picks, cur


TOME_SLOTS = [("weaponTome", 2), ("armorTome", 4), ("guildTome", 1)]
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}


def auto_tomes(build: Build, owned, armor, weapon, extra) -> list:
    """Tomes to put in the free weapon and armor tome slots for this build: the best for the goal, one at a time,
    only ones that add at least 0.1%. [(slot type, tome, gain)]."""
    if build.level < AUTO_TOMES_LEVEL or not build.data.tomes:
        return []
    picks, _ = recommend_tomes(build, owned, armor, weapon, extra, types=AUTO_TOME_TYPES)
    picks = [p for p in picks if p[2] >= 0.001]
    # Armor tome slots the goal doesn't use still get the tome with the most health (free survivability; the goal's
    # number doesn't change). gain None marks them.
    free = dict(TOME_SLOTS)["armorTome"] - sum(1 for k, _t, _g in picks if k == "armorTome")
    hp = sorted((t for t in build.data.tomes if t.type == "armorTome" and t.level <= build.level
                 and t.raw.get("remapID") is None and t.max_stats.get("hpBonus", 0) > 0 and mythic_ok(t)
                 and all(t.name != p[1].name for p in picks)),
                key=lambda t: (-t.max_stats.get("hpBonus", 0), -t.max_stats.get("hprRaw", 0), t.name))
    picks += [("armorTome", t, None) for t in hp[:max(0, free)]]
    return picks


def aspect_slots(level: int) -> int:
    """Aspect slots unlocked at a combat level (wiki: 1 + slots at 20, 40, 60, 80)."""
    return 1 + sum(level >= t for t in (20, 40, 60, 80))


AUTO_TOMES_LEVEL = 55       # builds from this level get tomes picked for them (weapon and armor tomes need 60)
AUTO_TOME_TYPES = ("weaponTome", "armorTome")     # guild tomes need a guild, so they're only used when you list them


def recommend_tomes(build: Build, owned, armor, weapon, extra, types=None):
    """Greedy best tomes per slot type for the goal. Returns [(slot type, tome, gain fraction)]."""
    base = build.evaluate(owned, armor, weapon, extra)["value"]
    chosen = list(build.tomes)
    picks = []
    for ttype, count in TOME_SLOTS:
        if types is not None and ttype not in types:
            continue
        have = sum(1 for t in chosen if t.type == ttype)
        for _ in range(max(0, count - have)):
            best, best_v = None, None
            for t in build.data.tomes:
                if t.type != ttype or t.level > build.level or any(c.name == t.name for c in chosen):
                    continue
                if t.raw.get("remapID") is not None or not mythic_ok(t):   # old name kept for old links / mythic
                    continue
                v = build.copy_with(tomes=chosen + [t]).evaluate(owned, armor, weapon, extra)["value"]
                if best_v is None or v > best_v:
                    best, best_v = t, v
            if best is None:
                break
            prev = build.copy_with(tomes=chosen).evaluate(owned, armor, weapon, extra)["value"]
            chosen.append(best)
            picks.append((ttype, best, best_v / prev - 1 if prev > 0 else 0))
    total = build.copy_with(tomes=chosen).evaluate(owned, armor, weapon, extra)["value"]
    return picks, (total / base - 1 if base > 0 else 0)


def recommend_aspects(build: Build, owned, armor, weapon, extra, tier: int = 1):
    """Greedy best aspects for your free aspect slots, at the given tier."""
    slots = aspect_slots(build.level) - len(build.aspects)
    pool = build.data.aspects.get(CLASS_TITLE[build.tree.cls], [])
    chosen = list(build.aspects)
    base = build.evaluate(owned, armor, weapon, extra)["value"]
    picks = []
    for _ in range(max(0, slots)):
        cur = build.copy_with(aspects=chosen).evaluate(owned, armor, weapon, extra)["value"]
        best, best_v = None, cur
        for asp in pool:
            if any(a["displayName"] == asp["displayName"] for a, _ in chosen):
                continue
            t = min(tier, len(asp.get("tiers", [])) or 1)
            v = build.copy_with(aspects=chosen + [(asp, t)]).evaluate(owned, armor, weapon, extra)["value"]
            if v > best_v * 1.001:
                best, best_v = (asp, t), v
        if best is None:
            break
        chosen.append(best)
        picks.append((best[0], best[1], best_v / cur - 1 if cur > 0 else 0))
    total = build.copy_with(aspects=chosen).evaluate(owned, armor, weapon, extra)["value"]
    return picks, (total / base - 1 if base > 0 else 0)


def parse_aspect_arg(data: GameData, cls: str, text: str):
    out, warnings = [], []
    for part in [p.strip() for p in (text or "").split(",") if p.strip()]:
        m = re.match(r"(.*?)(?:\s+(?:tier\s*)?(\d|iv|iii|ii|i))?$", part, re.I)
        name, tier_s = m.group(1), (m.group(2) or "1").lower()
        tier = int(tier_s) if tier_s.isdigit() else ROMAN.get(tier_s, 1)
        asp = data.find_aspect(cls, name)
        if asp is None:
            warnings.append(f'Aspect "{part}" not found for {CLASS_TITLE[cls]}.')
            continue
        out.append((asp, max(1, min(tier, len(asp.get("tiers", [])) or 1))))
    return out, warnings


def parse_powder_arg(text: str) -> dict:
    """'weapon=t6 t6; helmet=e4 e4' -> {'weapon': [...], 'helmet': [...]}"""
    out = {}
    for chunk in (text or "").split(";"):
        if "=" not in chunk:
            continue
        slot, spec = chunk.split("=", 1)
        slot = slot.strip().lower()
        slot = {"chest": "chestplate", "legs": "leggings", "pants": "leggings", "helm": "helmet",
                "boot": "boots"}.get(slot, slot)
        if slot in ("weapon", "helmet", "chestplate", "leggings", "boots"):
            out[slot] = parse_powders(spec)
    return out


_OWN_COUNT = re.compile(r"^(?:(\d+)\s*[x\u00d7]\s+)?(.*?)(?:\s+[x\u00d7]\s*(\d+)|\s*\((\d+)\))?$", re.I)


def parse_owned_items(data: GameData, text, cls: str):
    """
    The player's own items for "build from my items": "Ajax, Horseshoe x2, CR-..." (commas, semicolons or new
    lines; "x2", "2x" or "(2)" for two of a ring). Returns a dict:
      counts {item_key: how many (1 or 2)}, extra [crafted/custom Items], items [Items], notes [plain words].
    Names are matched like the gear box (closest match is said out loud); unknown names are left out, never guessed.
    """
    if isinstance(text, (list, tuple)):
        text = ", ".join(str(x) for x in text)
    counts, extra, items, notes, unknown = {}, [], [], [], []
    for raw in [p.strip() for p in re.split(r"[,;\n]", str(text or ""))[:400] if p.strip()]:
        m = _OWN_COUNT.match(raw[:200])
        name = (m.group(2) if m else raw).strip()
        n = int((m and (m.group(1) or m.group(3) or m.group(4))) or 1)
        if not name:
            continue
        if name[:3].upper() in ("CR-", "CI-"):
            try:
                it = crafted_from_code(data, name) if name[:3].upper() == "CR-" else custom_from_code(name)
            except Exception as exc:
                notes.append(f"Couldn't read {name[:12]}... ({exc}); left out.")
                continue
        else:
            it, warn = data.find_item(name)
            if it is None:
                unknown.append(name)
                continue
            if warn:
                notes.append(warn + ".")
        if it.category == "weapon" and it.type != CLASS_WEAPON[cls]:
            notes.append(f"{it.name} is a {it.type}; a {CLASS_TITLE[cls]} uses a {CLASS_WEAPON[cls]}, so it's left out.")
            continue
        k = item_key(it)
        if k not in counts:
            items.append(it)
            if it.crafted or it.custom:
                extra.append(it)
        counts[k] = min(2, counts.get(k, 0) + max(1, n))
    if unknown:
        notes.append("Not found in the item data, so left out: " + ", ".join(unknown[:12])
                     + (f" and {len(unknown) - 12} more" if len(unknown) > 12 else "") + ".")
    return {"counts": counts, "extra": extra, "items": items, "notes": notes}


def only_spec(owned_items: dict, cls: str, level: int) -> dict:
    """What the search needs from the player's items: allowed names, rings they have only one of, crafted pieces,
    and whether any weapon may be used (when they listed none they can use)."""
    counts = owned_items["counts"]
    kind = CLASS_WEAPON[cls]
    has_weapon = any(it.type == kind and it.level <= level for it in owned_items["items"])
    return {"names": frozenset(counts), "extra": list(owned_items["extra"]), "any_weapon": not has_weapon,
            "one": frozenset(item_key(it) for it in owned_items["items"] if it.type == "ring" and counts[item_key(it)] < 2)}


def default_weapon(data: GameData, cls: str, level: int, build=None, armor=None, allowed=None):
    """
    Best class weapon you could use at this level (used when you don't name one).
    With a build, the top candidates are scored for your goal; otherwise highest damage wins.
    `allowed`: only these items (the player's own items, by item_key).
    """
    kind = CLASS_WEAPON[cls]
    avail = skill_points_available(level)
    options = [it for it in data.items.values() if it.type == kind and it.level <= level
               and it.drop != "never" and sum(it.reqs) <= avail and mythic_ok(it)]
    if allowed is not None:
        options = [it for it in data.items.values() if it.type == kind and it.level <= level
                   and item_key(it) in allowed]
    if not options:
        options = [it for it in data.items.values() if it.type == kind and it.level <= max(level, 1) and mythic_ok(it)] or \
                  sorted((it for it in data.items.values() if it.type == kind), key=lambda it: it.level)[:1]
    options.sort(key=lambda it: (it.raw.get("averageDps") or 0, it.level), reverse=True)
    if build is None:
        return options[0]
    armor = armor or [None] * 8
    roots = frozenset(build.tree.roots)
    pool = options[:40]
    if build.goal in ("survival", "balanced", "healing") or build.goal2 in ("survival", "healing"):
        pool += sorted(options, key=lambda it: it.max_stats.get("hpBonus", 0), reverse=True)[:20]
    best, best_v = options[0], None
    for it in pool:
        assigned, _, _ = solve_skill_points(armor, it)
        if sum(assigned) > avail or max(assigned) > MAX_ASSIGNED_PER_SKILL:
            continue
        v = build.evaluate(roots, armor, it, None)["value"]
        if best_v is None or v > best_v:
            best, best_v = it, v
    return best


def click_order(tree: Tree, owned, level):
    return tree.validate(owned, level)["order"]


ALLOW_MYTHIC = False      # mythic items only when the player says so (--mythics / "mythics": true); set by run()


def mythic_ok(it) -> bool:
    """Mythic items (and mythic tomes) are left out of every search and suggestion unless the player allows them."""
    return ALLOW_MYTHIC or getattr(it, "tier", None) != "Mythic"


def _gear_pool(build: Build, kind: str, level_window: int, include_never: bool):
    cls, level = build.tree.cls, build.level
    return [it for it in build.data.items.values() if it.type == kind
            and level - level_window <= it.level <= level and (include_never or it.drop != "never")
            and it.class_req in (None, cls) and mythic_ok(it)]


def _swap(armor, weapon, slot, it, build, owned, extra):
    """Gear with `it` in `slot`. Armor powders move to the new piece; a new weapon gets quick best powders."""
    a2, w2 = list(armor), weapon
    if slot == "weapon":
        pw, _ = best_weapon_powders(build, owned, armor, it, extra, quick=True)
        w2 = weapon_with_powders(it, pw)
    else:
        old = armor[slot]
        a2[slot] = armor_with_powders(it, old.powders) if old is not None and old.powders else it
    return a2, w2


def _sp_bounds(armor, weapon):
    """(lower bound, one achievable assignment) of skill points to assign, without the full order search."""
    arm = [a for a in armor if a is not None]
    if any(b < 0 for a in arm for b in a.bonus) or any(getattr(a, "crafted", False) for a in arm):
        return None, None
    items = arm + ([weapon] if weapon is not None else [])
    lb = [0] * 5
    for k in range(5):
        for it in items:
            if it.reqs[k] <= 0:
                continue
            others = sum(max(0, a.bonus[k]) for a in arm if a is not it) if it is not weapon else \
                sum(max(0, a.bonus[k]) for a in arm)
            lb[k] = max(lb[k], it.reqs[k] - others)
    # one simple order: items giving the most skill points first, weapon last
    order = sorted(arm, key=lambda a: -sum(max(0, b) for b in a.bonus))
    assigned, cur = [0] * 5, [0] * 5
    for it in order + ([weapon] if weapon is not None else []):
        for k in range(5):
            if it.reqs[k] > 0 and cur[k] < it.reqs[k]:
                d = it.reqs[k] - cur[k]
                assigned[k] += d
                cur[k] += d
        if it is not weapon:
            for k in range(5):
                cur[k] += it.bonus[k]
    return lb, assigned


_WEAR_CACHE: dict = {}
ONLY_ONE: frozenset = frozenset()   # "build from my items": rings the player has only one of (so can't wear two)


def item_key(it) -> str:
    """What identifies an item in the player's list of items: its name, or for a crafted/custom piece its recipe
    (crafted pieces share names)."""
    code = getattr(it, "link_code", None)
    return repr(code) if code else it.name


def double_ring_blocked(armor) -> bool:
    """True if this gear wears the same ring twice and the player only has one."""
    if not ONLY_ONE or len(armor) < 6 or armor[4] is None or armor[5] is None:
        return False
    k = item_key(armor[4])
    return k == item_key(armor[5]) and k in ONLY_ONE


def _wearable(armor, weapon, level) -> bool:
    """Can this gear be worn at this level (enough skill points, none over the per-skill cap)? Remembered."""
    if ONLY_ONE and double_ring_blocked(armor):
        return False
    key = (_sp_key(armor, weapon), level)
    hit = _WEAR_CACHE.get(key)
    if hit is None:
        hit = _wearable_uncached(armor, weapon, level)
        if len(_WEAR_CACHE) > 500000:
            _WEAR_CACHE.clear()
        _WEAR_CACHE[key] = hit
    return hit


def _wearable_uncached(armor, weapon, level) -> bool:
    avail = skill_points_available(level)
    lb, ub = _sp_bounds(armor, weapon)
    if lb is not None:
        if sum(max(0, x) for x in lb) > avail or max(lb) > MAX_ASSIGNED_PER_SKILL:
            return False
        if sum(ub) <= avail and max(ub) <= MAX_ASSIGNED_PER_SKILL:
            return True
    else:
        # Items with negative bonuses or crafted items: a quick lower bound still rules out most gear that
        # can't be worn (each requirement minus every bonus the other items could possibly give).
        arm = [a for a in armor if a is not None]
        pos = [[max(0, b) for b in a.bonus] if not getattr(a, "crafted", False) else [0] * 5 for a in arm]
        tot = [sum(p[k] for p in pos) for k in range(5)]
        need = 0
        for k in range(5):
            m = weapon.reqs[k] - tot[k] if weapon is not None and weapon.reqs[k] > 0 else 0
            for a, p in zip(arm, pos):
                if a.reqs[k] > 0:
                    m = max(m, a.reqs[k] - tot[k] + p[k])
            m = max(0, m)
            if m > MAX_ASSIGNED_PER_SKILL:
                return False
            need += m
        if need > avail:
            return False
    assigned, _, _ = solve_skill_points(armor, weapon)
    return sum(assigned) <= avail and max(assigned) <= MAX_ASSIGNED_PER_SKILL


def suggest_gear(build: Build, owned, armor, weapon, extra_fn, per_slot: int = 3, level_window: int = 12,
                 include_never: bool = True):
    """For each slot, items you can wear that raise the goal score the most."""
    level = build.level
    base_extra, _ = extra_fn(owned, armor, weapon)
    base_val = build.evaluate(owned, armor, weapon, base_extra)["value"]
    suggestions = {}
    for slot in list(range(8)) + ["weapon"]:
        kind = CLASS_WEAPON[build.tree.cls] if slot == "weapon" else SLOT_TYPES[slot]
        current = weapon if slot == "weapon" else armor[slot]
        ranked = []
        for it in _gear_pool(build, kind, level_window, include_never):
            if current is not None and it.name == current.name:
                continue
            a2, w2 = _swap(armor, weapon, slot, it, build, owned, base_extra)
            if not _wearable(a2, w2, level):
                continue
            ranked.append((build.evaluate(owned, a2, w2, base_extra)["value"], it))
        ranked.sort(key=lambda x: x[0], reverse=True)
        top = []
        for _v, it in ranked[: per_slot * 3]:
            a2, w2 = list(armor), weapon
            if slot == "weapon":
                pw, _ = best_weapon_powders(build, owned, armor, it, base_extra)
                w2 = weapon_with_powders(it, pw)
            else:
                a2, w2 = _swap(armor, weapon, slot, it, build, owned, base_extra)
            ex, free = extra_fn(owned, a2, w2)
            if free < 0:
                continue
            v2 = build.evaluate(owned, a2, w2, ex)["value"]
            top.append((v2, w2 if slot == "weapon" else a2[slot]))
        top.sort(key=lambda x: x[0], reverse=True)
        suggestions[slot] = [(v / base_val - 1 if base_val > 0 else 0, it) for v, it in top[:per_slot]
                             if v > base_val * 1.005]
    return suggestions, base_val


def mixed_lines(ev, goal) -> list:
    """Report lines for the most damage in any style (shown for the mixed goal, or when it beats both other styles)."""
    mi = ev.get("mixed") or {}
    gv = ev.get("goal_values") or {}
    mx, sp, ml = gv.get("mixed", 0), gv.get("spells", 0), gv.get("melee", 0)
    if goal != "mixed" and not (mx > 1.02 * max(sp, ml) and min(sp, ml) > 0):
        return []
    res = ev.get("results") or {}
    out = [f"  Most damage, any style: {fmt_num(mx)}/s (the helper's own estimate; Wynnbuilder has no such number)."]
    if mi.get("rotation"):
        names = " + ".join(res[b]["name"] for b in mi["rotation"] if b in res)
        if mi["melee_share"] <= 1e-9:
            out.append(f"     Best style: spells. Cast {names} at full speed ({mi['rate']:.1f} a second, "
                       f"{fmt_num(mi.get('spell_part', 0))}/s); there's no time left for main attacks.")
        else:
            out.append(f"     Best style: spells and main attacks together. Cast {names} about {mi['rate']:.1f} times a "
                       f"second ({fmt_num(mi.get('spell_part', 0))}/s), main attack the rest of the time "
                       f"({mi['melee_share'] * 100:.0f}%: {fmt_num(mi.get('melee_part', 0))}/s).")
        out.append("     A cast takes 3 clicks at 9 clicks a second, like Wynnbuilder's mana model; summons, poison (once) "
                   "and the weapon special are added like for spells.")
    else:
        out.append("     Best style: main attacks only (casting would cost more main-attack damage than the spells add).")
    if goal != "mixed":
        out.append(f"     That's more than spells alone ({fmt_num(sp)}/s) or main attacks alone ({fmt_num(ml)}/s): "
                   "try the \"most damage (any style)\" goal.")
    return out


def how_to_play(build: "Build", owned, armor, weapon, fin: dict) -> list:
    """A short "how to play this build" from what the calculator counted: which attacks to use and in what order, how
    fast the mana lets you cast, which buffs the numbers assume and how to keep them up, the weapon special, and
    what life/mana steal do for this style. Only facts the helper used; nothing guessed."""
    goal = build.goal
    res = fin.get("results") or {}
    gv = fin.get("goal_values") or {}
    mi = fin.get("mixed") or {}
    out = []
    name = lambda b: res[b]["name"] if b in res else f"spell {b}"
    # 1. the attacks
    rot, rate, share = None, 0.0, 1.0
    if goal == "mixed":
        if mi.get("rotation"):
            rot, rate, share = mi["rotation"], mi["rate"], mi["melee_share"]
    elif goal in ("spells", "burst", "healing"):
        rot = fin.get("rotation") or (fin.get("heal_rotation") if goal == "healing" else None)
        share = 0.0
    elif goal in ("balanced", "survival", "ability") and gv.get("spells", 0) > gv.get("melee", 0) and fin.get("rotation"):
        rot, share = fin.get("rotation"), 0.0
    if goal == "burst":
        spells = [b for b in sorted(res) if b in (1, 2, 3, 4) and res[b].get("damage", 0) > 0]
        out.append("Burst: open with every damage spell once (" + ", ".join(name(b) for b in spells) + "), then keep "
                   "main-attacking while your mana comes back.")
    elif rot:
        names = ", ".join(name(b) for b in rot)
        m = fin.get("mana") or {}
        if goal != "mixed":
            rate = m.get("sustained_rate") or 0.0
        if share > 1e-9:
            out.append(f"Mix spells and main attacks: cast {names} in turn (about {rate:.1f} casts a second) and "
                       f"main-attack in between, about {share * 100:.0f}% of the time.")
        else:
            out.append(f"Spell build: cast {names} in turn" + (f", about {rate:.1f} a second" if rate else "") +
                       ". Alternate them: casting the same spell back-to-back costs more mana in Wynncraft.")
        if m:
            if m.get("seconds") is None:
                out.append("Mana: you can keep that up forever at full speed (3 casts a second).")
            elif goal != "mixed":
                out.append(f"Mana: at full speed (3 casts a second) a full bar lasts about {m['seconds']:.0f}s; after that "
                           f"slow down to about {m['sustained_rate']:.1f} casts a second (the damage shown assumes that pace).")
        lasting = [name(b) for b in rot if b in res and res[b].get("is_dps")]
        if lasting:
            out.append(f"{', '.join(lasting)} keep{'s' if len(lasting) == 1 else ''} dealing damage while up: recast just "
                       "before it runs out. Recasting refreshes it; it doesn't add a second copy.")
    else:
        spd = (getattr(weapon, "atk_spd", "") or "").replace("_", " ").lower()
        out.append("Main attacks: keep hitting" + (f" ({spd} attack speed)" if spd else "") + ". Spells aren't "
                   "part of the damage shown; use them for movement or utility.")
    near = [r["name"] for b, r in sorted(res.items()) if b in (1, 2, 3, 4) and r.get("name") in CONTACT_SPELLS
            and r.get("damage", 0) > 0]
    if near and goal != "burst":
        out.append(f"{', '.join(near)} only hits enemies that come close to you, so its damage isn't counted in your "
                   "damage per second; use it for defense, or as extra damage when enemies are on you.")
    # 2. buffs the numbers assume
    tm = build.toggle_map(owned)
    have = inter_names(build.tree, owned)
    rules = auto_toggles_for(build.tree)
    for t, sh in tm.items():
        if t not in have or t in MASKS:
            continue
        why = rules.get(t, (None, ""))[1]
        if t in build.auto_toggles and why:
            out.append(f"{t}: counted {'all the time' if sh >= 0.999 else f'{sh * 100:.0f}% of the time'} - {why}.")
        else:
            out.append(f"{t}: counted {'all the time' if sh >= 0.999 else f'{sh * 100:.0f}% of the time'} (you turned it on)"
                       " - keep it up that much in game, or the numbers will be lower.")
    if fin.get("mask"):
        out.append(f"Wear {fin['mask']} (cast Uproot to switch masks); the numbers assume that mask.")
    # 3. weapon special
    sp = fin.get("special")
    if sp and (sp.get("damage", 0) > 0 or sp.get("boost", 0) > 0):
        if any("SELFSUFFICIENT" in (x.major_ids or ()) for x in list(armor) + [weapon] if x is not None):
            how = "it fills by itself (Self-Sufficient: 8% a second); use it whenever it's full"
        else:
            how = f"main-attack about {SPECIAL_CHARGE_SECONDS:.0f}s to charge it, then use it"
        if goal == "melee":
            how += " (extra damage on top: the main attack DPS shown leaves it out, like Wynnbuilder)"
        elif sp.get("cycle"):
            how += f" (counted about every {sp['cycle']:.0f}s)"
        out.append(f"Weapon powder special ({sp['name']}): {how}.")
    # 4. life / mana steal for this style
    st = fin.get("stats") or {}
    if st.get("ls", 0) or st.get("ms", 0):
        ls_share = fin.get("ls_share", 1.0)
        if ls_share <= 0:
            out.append("Life steal and mana steal only work on main attack hits, so they do nothing while you only cast "
                       "(the numbers don't count them).")
        elif ls_share < 1:
            out.append("Life steal and mana steal only work on main attack hits: they help in the time you main-attack "
                       "between casts (counted that way).")
    return out


def wynnbuilder_view(build: "Build", owned, armor, weapon, extra, fin: dict) -> list:
    """What Wynnbuilder shows when the build's link is opened (it starts with every ability toggle off), worked out
    with the same maths, and each reason the app's numbers differ. Lets the bot answer "why is it different on
    Wynnbuilder" without seeing Wynnbuilder."""
    data, tree = build.data, build.tree
    # goal melee: no powder special boost blended in (Wynnbuilder leaves the special off); every spell is still worked out
    wbb = build.copy_with(goal="melee", goal2=None, toggles=[], toggle_share={}, auto_masks=False, auto_toggles=(),
                          floors=None)
    ev = wbb.evaluate(owned, armor, weapon, extra)
    df, res = ev.get("defense") or {}, ev.get("results") or {}
    out = ["Opening the link, Wynnbuilder starts with every ability toggle off. With them off it shows (same maths): "
           f"Average DPS (main attack) {fmt_num(ev['melee_goal'])}, health {fmt_num(df.get('hp', 0))}, effective HP "
           f"{fmt_num(df.get('ehp_agi', 0))} ({fmt_num(df.get('ehp', 0))} without agility), health regen "
           f"{fmt_num(df.get('hpr', 0))}."]
    sp = [f"{r['name']} {fmt_num(r['damage'])}{'/s' if r.get('is_dps') else ''}" for b, r in sorted(res.items())
          if b != 0 and r.get("damage", 0) > 0]
    if sp:
        out.append("Spells there (average per cast; '/s' = damage per second while it lasts): " + ", ".join(sp) + ".")
    fres = fin.get("results") or {}
    diff = []
    tm = build.toggle_map(owned)
    have = inter_names(tree, owned)
    full = [t for t, v in tm.items() if t in have and v >= 0.999 and t not in MASKS]
    part = [f"{t} {v * 100:.0f}%" for t, v in tm.items() if t in have and v < 0.999]
    if fin.get("mask"):
        full.append(fin["mask"])
    if full:
        diff.append("The app counts these buffs as on: " + ", ".join(full) + ". Tick them in Wynnbuilder to see the same numbers.")
    if part:
        diff.append("The app counts these for part of the time: " + ", ".join(part) + ". Wynnbuilder can only show them fully "
                    "on or off, so its numbers will be higher (on) or lower (off).")
    if full or part:
        sp2 = [f"{r['name']} {fmt_num(r['damage'])}{'/s' if r.get('is_dps') else ''}" for b, r in sorted(fres.items())
               if b != 0 and r.get("damage", 0) > 0]
        out.append(f"With the app's buffs: Average DPS {fmt_num(fin.get('melee_goal', 0))}" +
                   (", spells " + ", ".join(sp2) if sp2 else "") + ".")
    spc = fin.get("special")
    if spc and (spc.get("damage", 0) > 0 or spc.get("boost", 0) > 0):
        diff.append(f"The weapon's powder special ({spc['name']}) isn't in Wynnbuilder's numbers until you turn it on there"
                    + ("; the app's main attack DPS leaves it out too." if build.goal == "melee"
                       else "; the app counts it for the share of time it's charged and up."))
    if build.buffs or build.raid_buffs:
        diff.append("Party / raid buffs you entered (" + ", ".join(list(build.buffs) + list(build.raid_buffs)) +
                    ") are counted: tick the same boosts in Wynnbuilder.")
    if build.enemy:
        diff.append("Effective HP against the enemy hit you entered is the helper's own; Wynnbuilder's effective HP has "
                    "no enemy hit.")
    helper_ids = sorted({m for x in list(armor) + [weapon] if x is not None for m in (x.major_ids or ())
                         if majid_status(data, m) == "helper"})
    if helper_ids:
        diff.append("Special effects the helper counts but Wynnbuilder doesn't: " + ", ".join(
            (data.majids.get(m) or {}).get("displayName") or m.title() for m in helper_ids) + ".")
    if ROLL_MODE != "max":
        diff.append(f"Item rolls: the app uses {ROLL_MODE} rolls; Wynnbuilder shows perfect (max) rolls.")
    diff.append("Wynnbuilder has no spell-rotation, \"most damage (any style)\", survival or balanced score: those are "
                "the helper's own. In rotations a lasting spell (summons, totems, storms) counts at most one copy, "
                "and mana steal / life steal only for time spent main-attacking (in Wynnbuilder's mana calculator, "
                "leave \"mana steal\" unticked for a spell-only build).")
    diff.append("Items, powders, tomes, skill points and the ability tree are all in the link, so those match.")
    return out + ["Why the app's numbers can differ:"] + ["  " + d for d in diff]


def print_worth_getting(build: Build, wb: dict, only: dict, extra_fn, include_never: bool, per_slot: int = 3):
    """'Build from my items': single items the player doesn't have that would raise the best build from their
    items the most (each on its own, with the rest of that build kept)."""
    print_header(f"Worth getting next (items level {max(1, build.level - 12)}–{build.level} you don't have)")
    armor, weapon, owned = list(wb["armor"]), wb["weapon"], frozenset(wb["owned"])
    sugg, _ = suggest_gear(build, owned, armor, weapon, extra_fn, per_slot=per_slot * 3, include_never=include_never)
    names, one = only["names"], only["one"]
    shown = set()
    any_s = False
    for slot, lst in sugg.items():
        label = "Weapon" if slot == "weapon" else SLOT_LABELS[slot]
        cur = weapon if slot == "weapon" else armor[slot]
        keep = []
        for gain, it in lst:
            k = item_key(it)
            second = it.type == "ring" and k in one     # a second copy of a ring you have one of
            if (k in names and not second) or (it.type == "ring" and (k, "ring") in shown):
                continue
            keep.append((gain, it, second))
            if it.type == "ring":
                shown.add((k, "ring"))
            if len(keep) >= per_slot:
                break
        if not keep:
            continue
        any_s = True
        print(f"  {label} (now: {cur.name if cur is not None else '(empty)'})")
        for gain, it, second in keep:
            print(f"     +{gain * 100:5.1f}%  {it.name:<24} {it.short()}" + ("  (a second one)" if second else ""))
    if not any_s:
        print("  No single item in range beats the build from your items one piece at a time for this goal.")
    else:
        print("  Each gain is that one item swapped into the build from your items (skill points re-spent, same tree).")
        print("  With a new item, run the build again: the rest of the gear may change around it.")


def suggest_sets(build: Build, owned, armor, weapon, extra_fn, level_window: int = 12, include_never: bool = True,
                 top_n: int = 3):
    """
    Sets only pay off with 2+ pieces, which one-slot-at-a-time suggestions can't find. For every set with
    wearable pieces in range, try each combination of 2+ pieces on top of your gear.
    Returns [(gain, set name, {slot: item}, base value)].
    """
    level, cls = build.level, build.tree.cls
    base_extra, _ = extra_fn(owned, armor, weapon)
    base_val = build.evaluate(owned, armor, weapon, base_extra)["value"]
    by_name = {it.name: it for it in build.data.items.values()}
    screened = []
    for sname, sdef in build.data.sets.items():
        if any(isinstance(b, dict) and b.get("illegal") for b in sdef.get("bonuses", [])[1:]):
            continue                                   # the game forbids wearing 2+ pieces of these
        pieces = []
        for nm in sdef.get("items", []):
            it = by_name.get(nm)
            if it is None or not (level - level_window <= it.level <= level) or it.class_req not in (None, cls):
                continue
            if not include_never and it.drop == "never":
                continue
            if it.category == "weapon" and it.type != CLASS_WEAPON[cls]:
                continue
            pieces.append(it)
        if len(pieces) < 2:
            continue
        # Possible slots for each piece (rings can go in either ring slot).
        options = []
        for it in pieces[:10]:
            if it.category == "weapon":
                options.append((it, ["weapon"]))
            elif it.type == "ring":
                options.append((it, [4, 5]))
            elif it.type in SLOT_TYPES:
                options.append((it, [SLOT_TYPES.index(it.type)]))
        seen = set()
        for r in range(2, len(options) + 1):
            for combo in itertools.combinations(options, r):
                placed, ok = {}, True
                for it, slots in combo:
                    free = [sl for sl in slots if sl not in placed]
                    if not free:
                        ok = False
                        break
                    placed[free[0]] = it
                if not ok:
                    continue
                key = frozenset((sl, it.name) for sl, it in placed.items())
                if key in seen:
                    continue
                seen.add(key)
                a2, w2 = list(armor), weapon
                for sl, it in placed.items():
                    a2, w2 = _swap(a2, w2, sl, it, build, owned, base_extra)
                if not _wearable(a2, w2, level):
                    continue
                v = build.evaluate(owned, a2, w2, base_extra)["value"]
                screened.append((v, sname, placed, a2, w2))
    screened.sort(key=lambda x: x[0], reverse=True)
    out, used = [], set()
    for _v, sname, placed, a2, w2 in screened:
        if sname in used:
            continue
        ex, free = extra_fn(owned, a2, w2)
        if free < 0:
            continue
        v2 = build.evaluate(owned, a2, w2, ex)["value"]
        if v2 > base_val * 1.005:
            out.append((v2 / base_val - 1 if base_val > 0 else 0, sname, placed, a2, w2))
            used.add(sname)
        if len(out) >= top_n:
            break
    out.sort(key=lambda x: x[0], reverse=True)
    return out, base_val


def _fit_extra(extra, armor, weapon, level):
    """Trim free skill points so required + free never exceed what the level allows."""
    assigned, _, _ = solve_skill_points(armor, weapon)
    room = skill_points_available(level) - sum(assigned)
    ex = list(extra)
    while sum(ex) > max(0, room):
        i = max(range(5), key=lambda k: ex[k])
        ex[i] -= 1
    for i in range(5):
        ex[i] = max(0, min(ex[i], MAX_ASSIGNED_PER_SKILL - assigned[i]))
    return ex


def optimize_whole_build(build: Build, owned, armor, weapon, base_weapon, extra_fn, keep=(), ap_budget: int = 0,
                         level_window: int = 15, include_never: bool = True, pool_size: int = 16, rounds: int = 3,
                         beam: int = 30):
    """
    Choose all gear together for the goal, instead of one slot at a time.

    1. For each slot, shortlist items that score well with your current build on, plus items that score
       well on their own (so a better direction isn't missed just because it doesn't fit today's gear).
    2. Swap one slot at a time with everything else on, keeping a swap only if the whole build improves
       (skill points they need or give each other, set bonuses, elements, major IDs all count).
    3. Try changing two slots at once, which finds pairs that only work together.
    4. Re-spend free skill points and re-plan the ability tree for the new gear, then repeat.
    Returns a dict with the new armor, weapon, tree, free points and scores.
    """
    level = build.level
    slots = list(range(8)) + ["weapon"]
    cur_a, cur_w, cur_base, cur_own = list(armor), weapon, base_weapon, frozenset(owned)
    ex, free = extra_fn(cur_own, cur_a, cur_w)
    if free < 0:
        ex = [0] * 5
    start = build.evaluate(cur_own, cur_a, cur_w, ex)
    start_val = start["value"]

    def val(a, w, own, e):
        return build.evaluate(own, a, w, _fit_extra(e, a, w, level))["value"]

    def apply(a, w, base, slot, it):
        if slot == "weapon":
            pw, _ = best_weapon_powders(build, cur_own, a, it, ex, quick=True)
            return a, weapon_with_powders(it, pw), it
        a2 = list(a)
        old = a[slot]
        a2[slot] = armor_with_powders(it, old.powders) if old is not None and old.powders else it
        return a2, w, base

    # 1. shortlists
    pools = {}
    blank = [None] * 8
    for slot in slots:
        kind = CLASS_WEAPON[build.tree.cls] if slot == "weapon" else SLOT_TYPES[slot]
        if slot == 5 and 4 in pools:
            pools[5] = pools[4]
            continue
        scored = []
        for it in _gear_pool(build, kind, level_window, include_never):
            a2, w2, _b = apply(cur_a, cur_w, cur_base, slot, it)
            v_ctx = val(a2, w2, cur_own, ex) if _wearable(a2, w2, level) else -1e18
            a3, w3, _b = apply(blank, cur_w, cur_base, slot, it)
            v_alone = build.evaluate(cur_own, a3, w3, [0] * 5)["value"] if _wearable(a3, w3, level) else -1e18
            scored.append((v_ctx, v_alone, it))
        by_ctx = [x[2] for x in sorted(scored, key=lambda x: -x[0])[:pool_size] if x[0] > -1e18]
        by_alone = [x[2] for x in sorted(scored, key=lambda x: -x[1])[:pool_size // 2] if x[1] > -1e18]
        seen, pool = set(), []
        for it in by_ctx + by_alone:
            if it.name not in seen:
                seen.add(it.name)
                pool.append(it)
        pools[slot] = pool
    # Set pieces in range join the shortlists so 2+ piece set bonuses can be found.
    by_name = {it.name: it for it in build.data.items.values()}
    for sname, sdef in build.data.sets.items():
        if any(isinstance(b, dict) and b.get("illegal") for b in sdef.get("bonuses", [])[1:]):
            continue
        for nm in sdef.get("items", []):
            it = by_name.get(nm)
            if it is None or not (level - level_window <= it.level <= level) or it.class_req not in (None, build.tree.cls):
                continue
            if not include_never and it.drop == "never":
                continue
            for slot in slots:
                kind = CLASS_WEAPON[build.tree.cls] if slot == "weapon" else SLOT_TYPES[slot]
                if it.type == kind and all(p.name != it.name for p in pools[slot]):
                    pools[slot].append(it)

    cur_val = val(cur_a, cur_w, cur_own, ex)
    for _round in range(rounds):
        changed = False
        # 2. one slot at a time, everything else on
        for slot in slots:
            best = None
            current = cur_w if slot == "weapon" else cur_a[slot]
            for it in pools[slot]:
                if current is not None and it.name == current.name:
                    continue
                a2, w2, b2 = apply(cur_a, cur_w, cur_base, slot, it)
                if not _wearable(a2, w2, level):
                    continue
                v = val(a2, w2, cur_own, ex)
                if v > cur_val * 1.0005 and (best is None or v > best[0]):
                    best = (v, a2, w2, b2)
            if best:
                cur_val, cur_a, cur_w, cur_base = best
                changed = True
        # 3. two slots at once (top few per slot, judged with the current build on)
        short = {}
        for slot in slots:
            ranked = []
            for it in pools[slot]:
                a2, w2, b2 = apply(cur_a, cur_w, cur_base, slot, it)
                ranked.append((val(a2, w2, cur_own, ex) if _wearable(a2, w2, level) else -1e18, it))
            short[slot] = [it for v, it in sorted(ranked, key=lambda x: -x[0])[:5]]
        best = None
        for s1, s2 in itertools.combinations(slots, 2):
            for i1 in short[s1]:
                a1, w1, b1 = apply(cur_a, cur_w, cur_base, s1, i1)
                for i2 in short[s2]:
                    a2, w2, b2 = apply(a1, w1, b1, s2, i2)
                    if not _wearable(a2, w2, level):
                        continue
                    v = val(a2, w2, cur_own, ex)
                    if v > cur_val * 1.001 and (best is None or v > best[0]):
                        best = (v, a2, w2, b2)
        if best:
            cur_val, cur_a, cur_w, cur_base = best
            changed = True
        # 4. re-spend skill points and re-plan the tree for this gear
        if changed or _round == 0:
            ex, free = extra_fn(cur_own, cur_a, cur_w)
            if ap_budget:
                new_own, _ = best_tree(build, cur_a, cur_w, ap_budget, keep=keep, extra_points=ex, beam_width=beam)
                if build.evaluate(new_own, cur_a, cur_w, ex)["value"] > build.evaluate(cur_own, cur_a, cur_w, ex)["value"]:
                    cur_own = frozenset(new_own)
                    ex, free = extra_fn(cur_own, cur_a, cur_w)
            cur_val = val(cur_a, cur_w, cur_own, ex)
        if not changed:
            break
    # Final weapon powders with the full search, then final skill points.
    pw, _ = best_weapon_powders(build, cur_own, cur_a, cur_base, ex)
    cur_w = weapon_with_powders(cur_base, pw)
    ex, free = extra_fn(cur_own, cur_a, cur_w)
    final = build.evaluate(cur_own, cur_a, cur_w, ex)
    return {"armor": cur_a, "weapon": cur_w, "base_weapon": cur_base, "owned": cur_own, "extra": ex,
            "free": free, "final": final, "start": start, "start_val": start_val}


def combine_upgrades(build: Build, owned, armor, weapon, sugg: dict, set_combos: list, extra_fn):
    """Best wearable mix: start from your gear (or a set combo) and add single-slot upgrades greedily."""
    starts = [(list(armor), weapon, None)] + [(a2, w2, sname) for _g, sname, _p, a2, w2 in set_combos]
    best = None
    for a0, w0, sname in starts:
        a2, w2 = list(a0), w0
        ex, free = extra_fn(owned, a2, w2)
        if free < 0:
            continue
        cur_val = build.evaluate(owned, a2, w2, ex)["value"]
        locked = {s for s in range(8) if a2[s] is not armor[s]} | ({"weapon"} if w2 is not weapon else set())
        order_slots = sorted(sugg, key=lambda s: -(sugg[s][0][0] if sugg[s] else 0))
        for slot in order_slots:
            if slot in locked:
                continue
            for _gain, it in sugg[slot]:
                ta, tw = list(a2), w2
                if slot == "weapon":
                    tw = it
                else:
                    ta[slot] = it          # two of the same ring is allowed in Wynncraft
                ex, free = extra_fn(owned, ta, tw)
                if free < 0:
                    continue
                v = build.evaluate(owned, ta, tw, ex)["value"]
                if v > cur_val * 1.005:
                    a2, w2, cur_val = ta, tw, v
                    break
        if best is None or cur_val > best[0]:
            best = (cur_val, a2, w2, sname)
    return best


def level_plan(build: Build, owned, armor, weapon, keep_ids, levels_ahead: int, rank_ap: int, beam: int,
               include_never: bool = True, min_gain: float = 0.03):
    """
    One line per upcoming level: abilities to click (when you get ability points), where your 2 new skill
    points go, and items that unlock at exactly that level and beat what you wear now.
    Gear is not swapped as you go, so each level compares against your current gear.
    """
    tree, level = build.tree, build.level
    cur, cur_ap = owned, ability_points(level, rank_ap)
    prev_extra, _ = build.allocate_free_points(cur, armor, weapon)
    lines = []
    last = min(len(AP_TABLE) - 1, level + levels_ahead)
    for lvl in range(level + 1, last + 1):
        b2 = build.copy_with(level=lvl)
        parts = []
        ap_l = ability_points(lvl, rank_ap)
        if ap_l > cur_ap:
            ex_tmp, _ = b2.allocate_free_points(cur, armor, weapon)
            nxt, _ = optimize_tree(b2, armor, weapon, ap_l, keep=keep_ids, start=cur, extra_points=ex_tmp,
                                   beam_width=max(20, beam // 2))
            added = [tree.by_id[i] for i in click_order(tree, nxt, lvl) if i not in cur]
            spare = ap_l - sum(tree.by_id[i].cost for i in nxt)
            txt = ", ".join(f"{n.name} ({n.cost} AP)" for n in added) if added else "keep the new point for a bigger one later"
            if added and spare:
                txt += f", {spare} AP saved"
            parts.append("abilities: " + txt)
            cur, cur_ap = nxt, ap_l
        ex2, _ = b2.allocate_free_points(cur, armor, weapon)
        diff = [ex2[i] - prev_extra[i] for i in range(5)]
        if any(d < 0 for d in diff):
            parts.append("skill points: re-spread to " + " ".join(f"{SKILLS[i].upper()} +{ex2[i]}" for i in range(5) if ex2[i]))
        elif any(diff):
            parts.append("skill points: " + ", ".join(f"+{d} {SKILL_NAMES[i]}" for i, d in enumerate(diff) if d))
        prev_extra = ex2
        # Items that unlock at exactly this level.
        now = b2.evaluate(cur, armor, weapon, ex2)["value"]
        finds = []
        for slot in list(range(8)) + ["weapon"]:
            if slot in (5,):
                continue                                   # ring 2 uses the same items as ring 1
            kind = CLASS_WEAPON[tree.cls] if slot == "weapon" else SLOT_TYPES[slot]
            best = None
            for it in build.data.items.values():
                if it.type != kind or it.level != lvl or it.class_req not in (None, tree.cls):
                    continue
                if (not include_never and it.drop == "never") or not mythic_ok(it):
                    continue
                a2, w2 = _swap(armor, weapon, slot, it, b2, cur, ex2)
                if not _wearable(a2, w2, lvl):
                    continue
                v = b2.evaluate(cur, a2, w2, ex2)["value"]
                if now > 0 and v > now * (1 + min_gain) and (best is None or v > best[0]):
                    best = (v, it)
            if best:
                label = "weapon" if slot == "weapon" else SLOT_TYPES[slot]
                finds.append((best[0] / now - 1, f"{best[1].name} ({label}, +{(best[0] / now - 1) * 100:.0f}%)"))
        if finds:
            finds.sort(reverse=True)
            parts.append("new gear: " + ", ".join(t for _g, t in finds[:3]))
        lines.append(f"  Level {lvl:>3}: " + ("; ".join(parts) if parts else "nothing new"))
    return lines


# ============================================================================== Wynnbuilder links
# Port of Wynnbuilder's build_encode_decode.js (binary format, version 12+). See its ENCODING.md.

WB_BUILDER_URL = "https://wynnbuilder.github.io/builder/#"
WB_LOAD_ITEM_URL = "https://raw.githubusercontent.com/wynnbuilder/wynnbuilder.github.io/master/js/load_item.js"
B64_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz+-"
# Wynnbuilder's data version list (position = version number in links). Refreshed from GitHub when online.
WB_VERSION_NAMES = ["2.0.1.1", "2.0.1.2", "2.0.2.1", "2.0.2.3", "2.0.3.1", "2.0.4.1", "2.0.4.3", "2.0.4.4",
                    "2.1.0.0", "2.1.0.1", "2.1.1.0", "2.1.1.1", "2.1.1.2", "2.1.1.3", "2.1.1.4", "2.1.1.5",
                    "2.1.1.6", "2.1.1.7", "2.1.2.0", "2.1.3.0", "2.1.3.4", "2.1.4.0", "2.1.5.0", "2.1.6.0",
                    "2.2.0.0", "2.2.0.7", "2.2.0.12", "2.2.0.14", "2.2.0.19", "2.2.0.21", "2.2.0.31", "2.2.1.0",
                    "2.2.2.0", "2.2.3.0", "2.2.4.0", "2.2.5.0"]
TOME_SLOT_TYPES = ["weaponTome", "weaponTome", "armorTome", "armorTome", "armorTome", "armorTome", "guildTome",
                   "lootrunTome", "gatherXpTome", "gatherXpTome", "dungeonXpTome", "dungeonXpTome",
                   "mobXpTome", "mobXpTome"]
POWDERABLE_INDEX = {0: 0, 1: 1, 2: 2, 3: 3, 8: 4}   # helmet..boots, weapon


def wb_version_names(data_dir: str = DATA_DIR, offline: bool = False) -> list:
    path = os.path.join(data_dir, "versions.json")
    if not offline:
        try:
            req = urllib.request.Request(WB_LOAD_ITEM_URL, headers={"User-Agent": "WynnBuildHelper/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                src = resp.read().decode("utf-8")
            m = re.search(r"const wynn_version_names = \[(.*?)\];", src, re.S)
            names = re.findall(r"'([\d.]+)'", m.group(1)) if m else []
            if names:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(names, fh)
                return names
        except Exception:
            pass
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return WB_VERSION_NAMES


class Bits:
    """Wynnbuilder's BitVector: values stored least-significant bit first, 6 bits per Base64 digit."""

    def __init__(self):
        self.bits = []

    def append(self, value: int, length: int):
        value &= (1 << length) - 1 if length < 32 else 0xFFFFFFFF
        for i in range(length):
            self.bits.append((value >> i) & 1)

    def flag(self, enc: dict, field: str, name: str):
        self.append(enc[field][name], enc[field]["BITLEN"])

    def to_b64(self) -> str:
        out = []
        for i in range(0, len(self.bits), 6):
            chunk = self.bits[i:i + 6]
            out.append(B64_DIGITS[sum(b << k for k, b in enumerate(chunk))])
        return "".join(out)


class BitCursor:
    def __init__(self, b64: str):
        self.bits = []
        for ch in b64:
            v = B64_DIGITS.index(ch)
            self.bits.extend((v >> k) & 1 for k in range(6))
        self.i = 0

    def take(self, length: int) -> int:
        v = 0
        for k in range(length):
            v |= self.bits[self.i + k] << k
        self.i += length
        return v


def _tree_children(tree: Tree):
    children = {n.id: [] for n in tree.nodes}
    for n in tree.nodes:
        for p in n.parents:
            if p in children:
                children[p].append(n.id)
    root = next(n.id for n in tree.nodes if not n.parents)
    return root, children


def _encode_powders(bits: Bits, enc: dict, powders: list):
    if not powders:
        bits.flag(enc, "EQUIPMENT_POWDERS_FLAG", "NO_POWDERS")
        return
    tiers = enc["POWDER_TIERS"]
    ids = [p.elem * 7 + p.tier - 1 for p in powders]
    # Group by element in order of first appearance (Wynnbuilder's collectPowders).
    chunks, order = [], {}
    for pid in ids:
        e = pid // 7
        if e not in order:
            order[e] = len(chunks)
            chunks.append([])
        chunks[order[e]].append(pid)
    bits.flag(enc, "EQUIPMENT_POWDERS_FLAG", "HAS_POWDERS")

    def enc_idx(pid):
        return (pid // 7) * tiers + pid % 7

    prev = -1
    for chunk in chunks:
        i = 0
        while i < len(chunk):
            pid = chunk[i]
            if prev >= 0:
                bits.flag(enc, "POWDER_REPEAT_OP", "NO_REPEAT")
                if pid % 7 == prev % 7:
                    bits.flag(enc, "POWDER_REPEAT_TIER_OP", "REPEAT_TIER")
                    wrap = ((pid - prev) // 7) % len(enc["POWDER_ELEMENTS"]) - 1
                    bits.append(wrap, enc["POWDER_WRAPPER_BITLEN"])
                else:
                    bits.flag(enc, "POWDER_REPEAT_TIER_OP", "CHANGE_POWDER")
                    bits.flag(enc, "POWDER_CHANGE_OP", "NEW_POWDER")
                    bits.append(enc_idx(pid), enc["POWDER_ID_BITLEN"])
            else:
                bits.append(enc_idx(pid), enc["POWDER_ID_BITLEN"])
            i += 1
            while i < len(chunk) and chunk[i] == pid:
                bits.flag(enc, "POWDER_REPEAT_OP", "REPEAT")
                i += 1
            prev = pid
    bits.flag(enc, "POWDER_REPEAT_OP", "NO_REPEAT")
    bits.flag(enc, "POWDER_REPEAT_TIER_OP", "CHANGE_POWDER")
    bits.flag(enc, "POWDER_CHANGE_OP", "NEW_ITEM")


def encode_build_link(data: GameData, tree: Tree, enc: dict, version_id: int, level: int, armor: list, weapon,
                      tomes: list, aspects: list, owned, auto_totals: list, final_totals: list) -> str:
    bits = Bits()
    bits.append(0xC, 6)
    bits.append(version_id, 10)
    equipment = list(armor) + [weapon]
    for idx, it in enumerate(equipment):
        code = getattr(it, "link_code", None) if it is not None else None
        if code and code[0] == "craft":
            bits.flag(enc, "EQUIPMENT_KIND", "CRAFTED")
            encode_craft_bits(bits, it)
        elif code and code[0] == "custom":
            bits.flag(enc, "EQUIPMENT_KIND", "CUSTOM")
            bits.append(len(code[1]), 12)
            for ch in code[1]:
                bits.append(B64_DIGITS.index(ch), 6)
        else:
            bits.flag(enc, "EQUIPMENT_KIND", "NORMAL")
            bits.append(0 if it is None else int(it.raw["id"]) + 1, enc["ITEM_ID_BITLEN"])
        if idx in POWDERABLE_INDEX:
            _encode_powders(bits, enc, list(getattr(it, "powders", []) or []) if it is not None else [])
    # Tomes
    slots = [None] * enc["TOME_NUM"]
    for t in tomes:
        for i, ttype in enumerate(TOME_SLOT_TYPES[: enc["TOME_NUM"]]):
            if ttype == t.type and slots[i] is None:
                slots[i] = t
                break
    if all(s is None for s in slots):
        bits.flag(enc, "TOMES_FLAG", "NO_TOMES")
    else:
        bits.flag(enc, "TOMES_FLAG", "HAS_TOMES")
        for s in slots:
            if s is None:
                bits.flag(enc, "TOME_SLOT_FLAG", "UNUSED")
            else:
                bits.flag(enc, "TOME_SLOT_FLAG", "USED")
                bits.append(int(s.raw["id"]), enc["TOME_ID_BITLEN"])
    # Skill points (totals, only where they differ from Wynnbuilder's automatic values)
    deltas = [f - a for f, a in zip(final_totals, auto_totals)]
    if all(d == 0 for d in deltas):
        bits.flag(enc, "SP_FLAG", "AUTOMATIC")
    else:
        bits.flag(enc, "SP_FLAG", "ASSIGNED")
        for f, d in zip(final_totals, deltas):
            if d == 0:
                bits.flag(enc, "SP_ELEMENT_FLAG", "ELEMENT_UNASSIGNED")
            else:
                bits.flag(enc, "SP_ELEMENT_FLAG", "ELEMENT_ASSIGNED")
                bits.append(int(f) & ((1 << enc["MAX_SP_BITLEN"]) - 1), enc["MAX_SP_BITLEN"])
    # Level
    if level == enc["MAX_LEVEL"]:
        bits.flag(enc, "LEVEL_FLAG", "MAX")
    else:
        bits.flag(enc, "LEVEL_FLAG", "OTHER")
        bits.append(level, enc["LEVEL_BITLEN"])
    # Aspects
    asp = list(aspects)[: enc["NUM_ASPECTS"]]
    if not asp:
        bits.flag(enc, "ASPECTS_FLAG", "NO_ASPECTS")
    else:
        bits.flag(enc, "ASPECTS_FLAG", "HAS_ASPECTS")
        for i in range(enc["NUM_ASPECTS"]):
            if i < len(asp):
                a, tier = asp[i]
                bits.flag(enc, "ASPECT_SLOT_FLAG", "USED")
                bits.append(int(a["id"]), enc["ASPECT_ID_BITLEN"])
                bits.append(tier - 1, enc["ASPECT_TIER_BITLEN"])
            else:
                bits.flag(enc, "ASPECT_SLOT_FLAG", "UNUSED")
    # Ability tree: depth-first walk from the root, one bit per child visited.
    root, children = _tree_children(tree)
    owned = set(owned)
    visited = set()

    def walk(head):
        for c in children[head]:
            if c in visited:
                continue
            visited.add(c)
            if c in owned:
                bits.append(1, 1)
                walk(c)
            else:
                bits.append(0, 1)
    walk(root)
    return WB_BUILDER_URL + bits.to_b64()


def decode_build_link(data: GameData, enc: dict, link: str) -> dict:
    """Read a Wynnbuilder link: items, powders, tomes, skill points, level, aspects and tree."""
    code = link.split("#", 1)[1] if "#" in link else link.strip()
    cur = BitCursor(code)
    flag = cur.take(6)
    if flag <= 11:
        raise ValueError("This is an old-format Wynnbuilder link; open it on Wynnbuilder and copy it again.")
    version_id = cur.take(10)
    by_id = {int(it.raw["id"]): it for it in data.items.values()}
    equipment, powders, crafted = [], {}, []
    for i in range(enc["EQUIPMENT_NUM"]):
        kind = cur.take(enc["EQUIPMENT_KIND"]["BITLEN"])
        if kind == enc["EQUIPMENT_KIND"]["CRAFTED"]:
            try:
                equipment.append(decode_craft_bits(data, cur))
            except ValueError as exc:
                if "old format" in str(exc):
                    raise
                crafted.append((i, str(exc)))
                equipment.append(None)
        elif kind == enc["EQUIPMENT_KIND"]["CUSTOM"]:
            length = cur.take(12) * 6
            end = cur.i + length
            try:
                equipment.append(decode_custom_bits(cur, end))
            except (ValueError, IndexError) as exc:
                crafted.append((i, str(exc)))
                equipment.append(None)
            cur.i = end
        else:
            iid = cur.take(enc["ITEM_ID_BITLEN"])
            equipment.append(by_id.get(iid - 1) if iid else None)
        if i in POWDERABLE_INDEX:
            if cur.take(enc["EQUIPMENT_POWDERS_FLAG"]["BITLEN"]) == enc["EQUIPMENT_POWDERS_FLAG"]["HAS_POWDERS"]:
                tiers = enc["POWDER_TIERS"]

                def dec_idx(v):
                    return v + (v // tiers) * (7 - tiers)
                pw = [dec_idx(cur.take(enc["POWDER_ID_BITLEN"]))]
                while True:
                    if cur.take(1) == enc["POWDER_REPEAT_OP"]["REPEAT"]:
                        pw.append(pw[-1])
                        continue
                    if cur.take(1) == enc["POWDER_REPEAT_TIER_OP"]["REPEAT_TIER"]:
                        wrap = cur.take(enc["POWDER_WRAPPER_BITLEN"])
                        prev = pw[-1]
                        e = (prev // 7 + wrap + 1) % len(enc["POWDER_ELEMENTS"])
                        pw.append(e * 7 + prev % 7)
                        continue
                    if cur.take(1) == enc["POWDER_CHANGE_OP"]["NEW_POWDER"]:
                        pw.append(dec_idx(cur.take(enc["POWDER_ID_BITLEN"])))
                        continue
                    break
                powders[i] = [Powder(p // 7, p % 7 + 1) for p in pw]
    tomes = []
    tome_by_id = {int(t.raw["id"]): t for t in data.tomes}
    if cur.take(1) == enc["TOMES_FLAG"]["HAS_TOMES"]:
        for _ in range(enc["TOME_NUM"]):
            if cur.take(1) == enc["TOME_SLOT_FLAG"]["USED"]:
                t = tome_by_id.get(cur.take(enc["TOME_ID_BITLEN"]))
                if t is not None:
                    tomes.append(t)
    sp = None
    if cur.take(1) == enc["SP_FLAG"]["ASSIGNED"]:
        sp = []
        for _ in range(enc["SP_TYPES"]):
            if cur.take(1) == enc["SP_ELEMENT_FLAG"]["ELEMENT_ASSIGNED"]:
                v = cur.take(enc["MAX_SP_BITLEN"])
                if v >= 1 << (enc["MAX_SP_BITLEN"] - 1):
                    v -= 1 << enc["MAX_SP_BITLEN"]
                sp.append(v)
            else:
                sp.append(None)
    level = enc["MAX_LEVEL"] if cur.take(1) == enc["LEVEL_FLAG"]["MAX"] else cur.take(enc["LEVEL_BITLEN"])
    weapon = equipment[8]
    cls = next((c for c, w in CLASS_WEAPON.items() if weapon is not None and w == weapon.type), None)
    aspects = []
    if cur.take(1) == enc["ASPECTS_FLAG"]["HAS_ASPECTS"]:
        pool = {int(a["id"]): a for a in data.aspects.get(CLASS_TITLE.get(cls, ""), [])}
        for _ in range(enc["NUM_ASPECTS"]):
            if cur.take(1) == enc["ASPECT_SLOT_FLAG"]["USED"]:
                aid = cur.take(enc["ASPECT_ID_BITLEN"])
                tier = cur.take(enc["ASPECT_TIER_BITLEN"]) + 1
                if aid in pool:
                    aspects.append((pool[aid], tier))
    owned = []
    if cls:
        tree = Tree(data, cls)
        root, children = _tree_children(tree)
        owned.append(root)
        visited = set()

        def walk(head):
            for c in children[head]:
                if c in visited:
                    continue
                visited.add(c)
                if cur.i < len(cur.bits) and cur.take(1):
                    owned.append(c)
                    walk(c)
        walk(root)
    return {"version_id": version_id, "armor": equipment[:8], "weapon": weapon, "powders": powders,
            "tomes": tomes, "sp": sp, "level": level, "aspects": aspects, "class": cls, "tree": owned,
            "crafted_slots": crafted}


# ============================================================================== reporting

def fmt_num(x: float) -> str:
    if x >= 10000:
        return f"{x:,.0f}"
    return f"{x:,.1f}" if x < 100 else f"{x:,.0f}"


def prune_spare(build, owned, armor, weapon, extra, level):
    """Leave spare ability points unspent (the owner's choice): take out every ability that doesn't change the goal's
    number (and then anything that only led to it), as long as the tree stays legal. The goal's number stays exactly
    the same and minimums stay met. Returns (tree, ideas): ideas are abilities taken out that would add a lot to
    something else (e.g. an ultimate's combo damage), as [(name, [other abilities it needs back], AP, text)]."""
    tree = build.tree
    owned = frozenset(owned)
    ref = build.evaluate(owned, armor, weapon, extra)
    m0 = ref.get("main", 0.0)
    if m0 <= 0 or not ref.get("floors_met", True):
        return owned, []
    removed = []
    changed = True
    while changed:
        changed = False
        for nid in reversed(tree.validate(owned, level)["order"]):
            if nid in tree.roots:
                continue
            rest = owned - {nid}
            if not tree.validate(rest, level)["ok"]:
                continue
            r = build.evaluate(rest, armor, weapon, extra)
            if r.get("floors_met", True) and r.get("main", 0.0) >= m0 * (1 - 1e-12):
                owned, changed = rest, True
                removed.append(nid)
    if not removed:
        return owned, []
    # what the taken-out abilities would still add to something else, with whatever they need to be reached
    base = build.evaluate(owned, armor, weapon, extra)
    ideas, gone = [], set(removed)
    for nid in removed:
        need, stack = set(), [nid]
        while stack:
            cur = tree.by_id[stack.pop()]
            for p in cur.parents + cur.deps:
                if p in gone and p not in need and p != nid:
                    need.add(p)
                    stack.append(p)
        add = owned | {nid} | need
        if not tree.validate(add, level)["ok"]:
            continue
        r = build.evaluate(add, armor, weapon, extra)
        best = None
        for k, word in (("burst", "combo damage"), ("spell_dps", "spell damage"), ("melee_dps", "main attack damage"),
                        ("heal_ps", "healing")):
            a, b = r.get(k, 0) or 0, base.get(k, 0) or 0
            if b > 0 and a / b - 1 >= 0.05 and (best is None or a / b - 1 > best[0]):
                best = (a / b - 1, word)
        da, db = (r.get("defense") or {}).get("ehp_agi", 0) or 0, (base.get("defense") or {}).get("ehp_agi", 0) or 0
        if db > 0 and da / db - 1 >= 0.05 and (best is None or da / db - 1 > best[0]):
            best = (da / db - 1, "effective HP")
        if best:
            g = best[0] * 100
            ideas.append((tree.by_id[nid].name, [tree.by_id[i].name for i in sorted(need, key=removed.index)],
                          sum(tree.by_id[i].cost for i in {nid} | need),
                          f"+{g:.1f}% {best[1]}" if g < 10 else f"+{g:.0f}% {best[1]}"))
    ideas.sort(key=lambda x: -float(x[3].split("%")[0].lstrip("+")))
    kept = []                      # one idea per branch: skip ones on the way to (or reached through) a better one
    for idea in ideas:
        branch = {idea[0]} | set(idea[1])
        if any(idea[0] in k[4] or k[0] in branch for k in kept):
            continue
        kept.append(idea + (branch,))
    return owned, [k[:4] for k in kept[:4]]


def unspent_lines(tree, owned, level, ideas) -> list:
    left = ability_points(level) - tree.validate(owned, level)["ap"]
    if left <= 0:
        return []
    lines = [f"  {left} ability point{'s' if left != 1 else ''} left unspent: nothing else in reach raises your goal, "
             "so spend them on whatever you like (or keep them for later)."]
    for name, need, ap, text in ideas:
        way = (f" (with {', '.join(need)})" if len(need) <= 3 else f" (with {len(need)} abilities on the way)") if need else ""
        lines.append(f"    Idea: {name}{way}, {ap} AP: {text}, but nothing for your goal.")
    return lines


def tree_roles(build, owned, armor, weapon, extra, level) -> dict:
    """Why each ability is in a tree, worked out with the calculator: {node id: {"role", "gain", "text"}}.
    role: "start" (the first ability), "goal" (taking it out lowers the goal's number by gain %), "path" (needed to
    reach abilities that count: taking it out, and everything that then can't stay, lowers the goal's number),
    "spare" (taking it out, with anything that only it leads to, doesn't change the goal's number; text says what
    else it adds, if anything)."""
    tree = build.tree
    owned = frozenset(owned)
    full = build.evaluate(owned, armor, weapon, extra)
    key = "main" if full.get("main", 0) > 0 else "value"
    base = full.get(key, 0.0)
    by_name = {n.name: n.id for n in tree.nodes}

    def prune(rest):
        """Take out what can't stay once an ability is gone (no way to it, archetype count too low, ...)."""
        rest, gone, arch = set(rest), set(), False
        while True:
            v = tree.validate(rest, level)
            bad = [e.partition(": ") for e in v["errors"] if ": " in e]
            bad = [(by_name.get(nm), why) for nm, _, why in bad if by_name.get(nm) in rest]
            if not bad:
                return frozenset(rest), gone, arch
            for nid, why in bad:
                arch |= "abilities (have" in why
                rest.discard(nid)
                gone.add(nid)

    def gain_of(rest):
        r = build.evaluate(rest, armor, weapon, extra)
        was = r.get(key, 0.0)
        return ((base / was - 1) * 100 if was > 0 else (100.0 if base > 0 else 0.0)), r

    def pct(g):
        return f"+{g:.1f}%" if g < 10 else f"+{g:.0f}%"

    order = tree.validate(owned, level)["order"]
    out, later = {}, []
    for nid in order:                                  # first: the ones that can be taken out on their own
        if nid in tree.roots:
            out[nid] = {"role": "start", "gain": None, "text": "the ability every tree starts with"}
            continue
        rest, gone, _arch = prune(owned - {nid})
        if gone:
            later.append((nid, rest, gone, _arch))
            continue
        gain, r = gain_of(rest)
        if gain > 1e-7:
            out[nid] = {"role": "goal", "gain": gain, "text": f"{pct(gain)} for your goal" if gain >= 0.05 else
                        "a tiny bit (under 0.1%) for your goal"}
            continue
        side = []
        for k, word in (("spell_dps", "spell damage"), ("melee_dps", "main attack damage"), ("burst", "combo damage"),
                        ("heal_ps", "healing")):
            a, b = full.get(k, 0) or 0, r.get(k, 0) or 0
            if b > 0 and a / b - 1 >= 0.002:
                side.append(((a / b - 1) * 100, word))
        da, db = (full.get("defense") or {}).get("ehp_agi", 0) or 0, (r.get("defense") or {}).get("ehp_agi", 0) or 0
        if db > 0 and da / db - 1 >= 0.002:
            side.append(((da / db - 1) * 100, "effective HP"))
        side.sort(reverse=True)
        if side and side[0][0] >= 5:                   # big for something else: worth keeping, not a free point
            out[nid] = {"role": "extra", "gain": 0.0,
                        "text": f"doesn't change your goal's number, but adds {pct(side[0][0])[1:]} {side[0][1]}: worth keeping"}
            continue
        text = (f"doesn't change your goal's number; adds {pct(side[0][0])[1:]} {side[0][1]}" if side else
                "doesn't change anything the calculator counts (a free point: take something you like instead)")
        out[nid] = {"role": "spare", "gain": 0.0, "text": text}
    for nid, rest, gone, arch in later:              # then: the ones others need
        gain, _r = gain_of(rest)
        goal_after = sorted((out[g]["gain"], tree.by_id[g].name) for g in gone if out.get(g, {}).get("role") == "goal")
        names = [nm for _g, nm in reversed(goal_after)] or [tree.by_id[g].name for g in sorted(gone, key=order.index)]
        if gain <= 1e-7:
            out[nid] = {"role": "spare", "gain": 0.0,
                        "text": "doesn't change your goal's number; it only leads to " + ", ".join(names[:3]) +
                                (", which don't either" if len(names) > 1 else ", which doesn't either")}
            continue
        n = tree.by_id[nid]
        if arch and n.archetype:
            text = f"counts toward the {n.archetype} abilities that {', '.join(names[:3])} need" + ("s" if len(names[:3]) == 1 else "")
        else:
            text = "on the way to " + ", ".join(names[:3])
        out[nid] = {"role": "path", "gain": gain, "text": f"{text} (together {pct(gain)} for your goal)"}
    return out


def roles_lines(tree, roles, top=6) -> list:
    """Short report lines: the abilities that matter most, how many are paths, and the spare ones."""
    goal = sorted(((r["gain"], tree.by_id[i].name) for i, r in roles.items() if r["role"] == "goal"), reverse=True)
    path = [tree.by_id[i].name for i, r in roles.items() if r["role"] == "path"]
    spare = [(tree.by_id[i], r) for i, r in roles.items() if r["role"] == "spare"]
    extra = [(tree.by_id[i], r) for i, r in roles.items() if r["role"] == "extra"]
    lines = []
    if goal:
        lines.append("  Abilities that matter most for your goal: " + ", ".join(
            f"{nm} (+{g:.1f}%)" if g < 10 else f"{nm} (+{g:.0f}%)" for g, nm in goal[:top]) + ".")
    if path:
        lines.append(f"  {len(path)} abilities are there to reach those (a way through the tree, or the archetype count "
                     "one of them needs).")
    if extra:
        lines.append("  Not for your goal, but worth keeping: " + "; ".join(
            f"{n.name} ({r['text'].split('adds ', 1)[1].split(':')[0]})" for n, r in extra[:4]) + ".")
    if spare:
        pts = sum(n.cost for n, _ in spare)
        lines.append(f"  {len(spare)} abilities ({pts} AP) don't change your goal's number; they're spare points, picked "
                     "for small extras:")
        for n, r in spare[:8]:
            extra = r["text"].split("; ", 1)[1] if "; " in r["text"] else "no effect the calculator counts"
            lines.append(f"    {n.name}: {extra}")
        lines.append("  Swap spare points for anything you like (mobility, defense) without losing anything for your goal.")
    return lines


def describe_tree(tree: Tree, owned, level) -> list[str]:
    res = tree.validate(owned, level)
    lines, running = [], 0
    for i, nid in enumerate(res["order"], 1):
        n = tree.by_id[nid]
        running += n.cost
        arch = f"  [{n.archetype}]" if n.archetype else ""
        lines.append(f"  {i:2}. {n.name:<28} {n.cost} AP  (total {running}){arch}")
    for e in res["errors"]:
        lines.append(f"  !! {e}")
    return lines


def print_header(text: str):
    print()
    print(text)
    print("-" * len(text))


def run(args):
    global ROLL_MODE, CLICKS_PER_SECOND, POISON_MODE
    ROLL_MODE = getattr(args, "rolls", "max") or "max"
    POISON_MODE = getattr(args, "poison", "wynnbuilder") or "wynnbuilder"
    CLICKS_PER_SECOND = getattr(args, "cps", CLICKS_PER_SECOND) or CLICKS_PER_SECOND
    t0 = time.time()
    LAST_RESULT.clear()
    global ALLOW_MYTHIC
    ALLOW_MYTHIC = bool(getattr(args, "mythics", False))
    data = load_game_data(offline=args.offline)
    link = None
    if getattr(args, "from_link", None):
        if not data.enc:
            sys.exit("Link support needs Wynnbuilder's encoding data; run once online first.")
        try:
            link = decode_build_link(data, data.enc, args.from_link)
        except Exception as exc:
            sys.exit(f"Couldn't read that Wynnbuilder link: {exc}")
        if link["class"] is None:
            sys.exit("That link has no weapon, so the class is unknown. Add --class.")
        if args.cls and args.cls != link["class"]:
            print(f"  The link is a {CLASS_TITLE[link['class']]} build; using that class.")
        args.cls = link["class"]
        args.level = args.level or link["level"]
    cls = args.cls
    level = args.level
    goal = args.goal
    print(f"Wynn Build Helper  ·  game data {data.version}  ·  {CLASS_TITLE[cls]}, level {level}, goal: {goal}")
    for n in data.notes:
        print("  " + n)

    tree = Tree(data, cls)
    if not args.offline and not args.no_official:
        for m in cross_check_official_tree(data, cls):
            print("  " + m)

    # ---- gear
    armor = [None] * 8
    weapon = None
    unplaced = []
    link_tree = None
    if link:
        armor = list(link["armor"])
        weapon = link["weapon"]
        for i, why in link["crafted_slots"]:
            print(f"  {SLOT_LABELS[i] if i < 8 else 'Weapon'}: couldn't read this crafted/custom item ({why}); "
                  "treated as empty.")
        spec = []
        for i, pw in link["powders"].items():
            slot = ARMOR_SLOTS[i] if i < 4 else "weapon"
            spec.append(slot + "=" + " ".join(f"{POWDER_ELEMS[p.elem]}{p.tier}" for p in pw))
        if spec and not args.powders:
            args.powders = "; ".join(spec)
        if link["tomes"] and not args.tomes:
            args.tomes = ", ".join(t.name for t in link["tomes"])
        if link["aspects"] and not args.aspects:
            args.aspects = ", ".join(f"{a['displayName']} {t}" for a, t in link["aspects"])
        link_tree = frozenset(link["tree"])
        args.gear = ""
    for name in [g for g in (args.gear or "").split(",") if g.strip()]:
        code = name.strip()
        if code[:3].upper() in ("CR-", "CI-"):
            try:
                it = crafted_from_code(data, code) if code[:3].upper() == "CR-" else custom_from_code(code)
            except Exception as exc:
                print(f"  Couldn't read {code[:12]}...: {exc}")
                continue
            warn = None
        else:
            it, warn = data.find_item(name)
        if warn:
            print("  " + warn)
        if it is None:
            continue
        if it.category == "weapon":
            if it.type != CLASS_WEAPON[cls]:
                print(f"  {it.name} is a {it.type}; a {CLASS_TITLE[cls]} uses a {CLASS_WEAPON[cls]}. Ignored.")
                continue
            weapon = it
            continue
        placed = False
        for s, t in enumerate(SLOT_TYPES):
            if t == it.type and armor[s] is None:
                armor[s] = it
                placed = True
                break
        if not placed:
            unplaced.append(it.name)
    if unplaced:
        print("  No free slot for: " + ", ".join(unplaced))
    # ---- "build from my items": only the items the player says they have (plus what they wear)
    only = None
    if getattr(args, "only_items", None):
        mine = parse_owned_items(data, args.only_items, cls)
        worn_now = [a for a in armor if a is not None] + ([weapon] if weapon is not None else [])
        for it in worn_now:                      # what you wear is yours too (two of a ring if you wear two)
            k = item_key(it)
            n = sum(1 for a in worn_now if item_key(a) == k)
            if k not in mine["counts"]:
                mine["items"].append(it)
                if it.crafted or it.custom:
                    mine["extra"].append(it)
            mine["counts"][k] = max(mine["counts"].get(k, 0), min(2, n))
        only = only_spec(mine, cls, level)
        usable = [it for it in mine["items"] if it.level <= level]
        print(f"  Building only from your items: {len(mine['items'])} item(s) listed"
              + (f", {len(usable)} usable at level {level}" if len(usable) != len(mine["items"]) else "") + ".")
        for n in mine["notes"]:
            print("  " + n)
        missing = [lab for s_i, lab in enumerate(SLOT_LABELS[:8])
                   if not any(it.type == SLOT_TYPES[s_i] and it.level <= level for it in mine["items"])]
        missing = list(dict.fromkeys("Rings" if m.startswith("Ring") else m for m in missing))
        if missing:
            print("  Nothing in your list for: " + ", ".join(missing) + " (left empty).")
        if only["any_weapon"]:
            print(f"  No {CLASS_WEAPON[cls]} you can use at level {level} in your list, so the weapon can be any {CLASS_WEAPON[cls]}.")
        if only["one"]:
            print("  Rings you have one of are never worn twice (add \"x2\" to a ring you have two of).")
        if not getattr(args, "tomes", None) and level >= AUTO_TOMES_LEVEL:
            print("  Tomes: only ones you list in the Tomes box (none listed, so none are used).")
        if not ALLOW_MYTHIC and any(it.tier == "Mythic" for it in mine["items"]):
            print("  Mythic items in your list are used (they're yours).")
        LAST_RESULT["only_items"] = {"count": len(mine["items"]), "names": [it.name for it in mine["items"]][:400]}
    assumed_weapon = False
    if weapon is None:
        allowed = None if (only is None or only["any_weapon"]) else only["names"]
        weapon = default_weapon(data, cls, level, Build(data, tree, level, goal), armor, allowed=allowed)
        assumed_weapon = True
    no_gear = assumed_weapon and all(a is None for a in armor)
    worn = [a for a in armor if a] + [weapon]

    # Powders you already have slotted (armor ones change defense/health; weapon ones change damage).
    powder_spec = parse_powder_arg(args.powders)
    armor_base = list(armor)                  # without powders, for powder recommendations
    for s, slot_name in enumerate(ARMOR_SLOTS):
        if slot_name in powder_spec and armor[s] is not None:
            armor[s] = armor_with_powders(armor[s], powder_spec[slot_name])
    base_weapon = weapon
    user_weapon_powders = powder_spec.get("weapon")
    weapon = weapon_with_powders(base_weapon, user_weapon_powders or [])
    if not args.offline and not args.no_official and args.gear:
        for m in cross_check_official_items([w for w in worn if w is not None]):
            print("  " + m)

    owned_tomes = []
    for tname in [t for t in (args.tomes or "").split(",") if t.strip()]:
        tm = data.find_tome(tname)
        if tm is None:
            print(f'  Tome "{tname.strip()}" not found. Ignored.')
        elif tm.level > level:
            print(f"  {tm.name} needs level {tm.level}. Ignored.")
        else:
            owned_tomes.append(tm)
    owned_aspects, warns = parse_aspect_arg(data, cls, args.aspects)
    for w in warns:
        print("  " + w)
    if len(owned_aspects) > aspect_slots(level):
        print(f"  Only {aspect_slots(level)} aspect slots at level {level}; using the first {aspect_slots(level)}.")
        owned_aspects = owned_aspects[:aspect_slots(level)]
    user_toggles = parse_toggle_arg(tree, args.toggles or "")            # {name: share}, what you said
    toggles_off = set(resolve_toggles(tree, [t.strip() for t in (getattr(args, "toggles_off", "") or "").split(",")
                                             if t.strip()]))
    no_auto = getattr(args, "no_auto_toggles", False)
    share = dict(user_toggles)
    auto_on = {}                                                          # name -> (share, why) assumed by default
    if not no_auto:
        for tname, (frac, why) in auto_toggles_for(tree).items():
            if tname not in share and tname not in toggles_off:
                share[tname] = frac
                auto_on[tname] = (frac, why)
    share = {k: v for k, v in share.items() if k not in toggles_off and v > 0}
    toggles = [k for k, v in share.items() if v >= 1]
    toggle_share = {k: v for k, v in share.items() if v < 1}
    sliders = {}
    for part in [x for x in (args.sliders or "").split(",") if "=" in x]:
        k, v = part.split("=", 1)
        try:
            sliders[k.strip()] = float(v)
        except ValueError:
            print(f'  Slider value "{part.strip()}" is not a number. Ignored.')
    buffs = [b.strip().lower().replace(" ", "") for b in (args.buffs or "").split(",") if b.strip()]
    for b in [b for b in buffs if b not in BUFFS]:
        print(f'  Buff "{b}" not recognised. Options: {", ".join(BUFFS)}')
    buffs = [b for b in buffs if b in BUFFS]
    raid_buffs = []
    rb_lookup = {k.lower(): k for k in RAID_BUFFS}
    for b in [b.strip() for b in (args.raid_buffs or "").split(",") if b.strip()]:
        key = rb_lookup.get(b.lower().replace(" ", "-"))
        if key:
            raid_buffs.append(key)
        else:
            print(f'  Raid buff "{b}" not recognised (e.g. Lightbearer-II, Seraphim-I).')
    focus = None
    if goal == "ability":
        ftxt = (getattr(args, "focus", "") or "").strip()
        if re.sub(r"[^a-z]", "", ftxt.lower()) in ("melee", "mainattack", "main", "attack", "dps", ""):
            if not ftxt:
                print("  No ability given for the ability goal (use --focus, e.g. \"Puppet\"); using main attack DPS instead.")
            goal = "melee"
            args.goal = goal
        else:
            focus = resolve_focus(tree, ftxt)
            if focus is None:
                names = sorted({e.get("name") for n in tree.nodes for e in n.raw.get("effects", [])
                                if e.get("type") == "replace_spell" and e.get("name")})
                sys.exit(f'Couldn\'t find an ability matching "{ftxt}" for {CLASS_TITLE[cls]}. Try one of: ' + ", ".join(names))
            if focus[0] == 0:                       # the main attack: that's the highest-DPS goal
                print("  That's your main attack, so this uses the main attack DPS goal (Wynnbuilder's Average DPS).")
                focus, goal = None, "melee"
                args.goal = goal
            else:
                print(f"  Focusing on: {focus[1]} (unlocked by {tree.by_id[focus[2]].name}).")
    enemy = parse_enemy_arg(getattr(args, "enemy", "") or "")
    if getattr(args, "enemy", "") and not enemy:
        print('  Couldn\'t read --enemy; use e.g. "fire 3000" or "fire=2000, earth=1000".')
    floors = {}
    if getattr(args, "min", ""):
        try:
            floors.update(parse_floors(args.min))
        except ValueError as exc:
            sys.exit(f"--min: {exc}")
    for attr, key in (("min_hp", "hp"), ("min_ehp", "ehp"), ("min_walk_speed", "spd"), ("min_mana_regen", "mr")):
        if getattr(args, attr, None) is not None:
            floors[key] = float(getattr(args, attr))
    if floors:
        print(f"  Minimums every suggested build must keep: {describe_floors(floors)}.")
    try:
        main_arch = match_archetype(tree, getattr(args, "archetype", "") or "")
        avoid_arch = [a for a in (match_archetype(tree, t) for t in
                                  re.split(r"[,;/]| and ", getattr(args, "avoid_archetype", "") or "") if t.strip()) if a]
    except ValueError as exc:
        sys.exit(str(exc))
    if main_arch and main_arch in avoid_arch:
        sys.exit(f"{main_arch} can't be both your main archetype and one to avoid.")
    if avoid_arch:
        print(f"  Archetypes left out: {', '.join(avoid_arch)} (none of their play-style abilities; plain boosts like an "
              "element mastery can still be borrowed if they help).")
    if main_arch:
        print(f"  Main archetype: {main_arch}. The ability tree keeps more {main_arch} abilities than any other "
              "archetype's (others only where they help).")
    build = Build(data, tree, level, goal, tomes=owned_tomes, aspects=owned_aspects, toggles=toggles,
                  sliders=sliders, slider_mode=args.slider_mode, buffs=buffs, raid_buffs=raid_buffs, enemy=enemy,
                  focus=focus, floors=floors, toggle_share=toggle_share,
                  auto_masks=not no_auto and not (set(MASKS) & set(toggles_off)), toggles_off=toggles_off,
                  auto_toggles=set(auto_on), goal2=getattr(args, "goal2", None),
                  goal2_weight=getattr(args, "goal2_weight", 0) or 0, archetype=main_arch,
                  avoid_archetypes=avoid_arch)
    if getattr(args, "goal2", None) and not build.goal2:
        print(f"  (A second goal of \"{args.goal2}\" can't be mixed with {goal}; using {goal} alone.)")
    if build.goal2:
        print(f"  Goal mix: {round((1 - build.goal2_weight) * 100)}% {GOAL_WORDS.get(goal, goal)} + "
              f"{round(build.goal2_weight * 100)}% {GOAL_WORDS.get(build.goal2, build.goal2)}. The score is a weighted "
              "mix of the two (like \"balanced\"), so a build only wins by being good at both in that proportion.")
    floor_build = build                   # the whole-build search always keeps the minimums
    try:                                   # item names for the app's live view while the tree is planned
        import wynn_search as _ws
        if _ws.LIVE:
            _ws.live_pools_hint(build, not getattr(args, "drops_only", False))
    except Exception:
        pass
    include_never = not getattr(args, "drops_only", False)
    budget = ability_points(level, args.rank_ap)

    keep_ids = [focus[2]] if focus else []
    for k in [k for k in (args.keep or "").split(",") if k.strip()]:
        n = tree.find(k)
        if n is None:
            print(f'  Ability "{k.strip()}" not found in the {CLASS_TITLE[cls]} tree. Ignored.')
        else:
            keep_ids.append(n.id)
            if n.name.lower() != k.strip().lower():
                print(f'  Using "{n.name}" for "{k.strip()}".')

    # ---- skill points needed for the gear
    assigned, totals, order = solve_skill_points(armor, weapon)
    avail = skill_points_available(level)
    print_header("Your gear")
    too_high = [it for it in armor + [weapon] if it is not None and it.level > level]
    for s, it in enumerate(armor):
        if it:
            print(f"  {SLOT_LABELS[s]:<11} {it.name:<24} {it.short()}")
            if getattr(it, "ingredients", None):
                print(f"  {'':<11} made with {', '.join(it.ingredients)}")
    print(f"  {'Weapon':<11} {weapon.name:<24} {weapon.short()}" + ("   (assumed: no weapon given)" if assumed_weapon else ""))
    if getattr(weapon, "ingredients", None):
        print(f"  {'':<11} made with {', '.join(weapon.ingredients)}")
    if too_high:
        print(f"  !! Level {level} can't use: " + ", ".join(f"{it.name} (needs level {it.level})" for it in too_high)
              + ". The report still counts them, as Wynnbuilder does.")

    print_header("Skill points")
    print(f"  You have {avail} skill points at level {level}.")
    if sum(assigned) > avail:
        print(f"  !! This gear needs {sum(assigned)} assigned points; you only have {avail}. Some items won't work.")
    elif max(assigned) > MAX_ASSIGNED_PER_SKILL:
        print(f"  !! This gear needs more than {MAX_ASSIGNED_PER_SKILL} points in one skill, which isn't allowed.")

    def extra_fn(owned, a, w):
        return build.allocate_free_points(owned, a, w)

    # ---- tree
    owned_seed = frozenset(tree.roots)
    extra0, _ = extra_fn(owned_seed, armor, weapon)
    if level >= ULTIMATE_LEVEL:
        print("  Searching ability trees, including each ultimate (can take up to a minute)...")
    chosen, _ = optimize_tree(build, armor, weapon, budget, keep=keep_ids, extra_points=extra0, beam_width=args.beam)
    extra, free = extra_fn(chosen, armor, weapon)
    # Best powders for this tree; used for the rest of the plan unless you told us yours.
    rec_powders, _ = best_weapon_powders(build, chosen, armor, base_weapon, extra, args.max_powder_tier)
    if user_weapon_powders is None and base_weapon.slots:
        weapon = weapon_with_powders(base_weapon, rec_powders)
        extra, free = extra_fn(chosen, armor, weapon)
    chosen2, _ = best_tree(build, armor, weapon, budget, keep=keep_ids, extra_points=extra, beam_width=args.beam)
    if build.evaluate(chosen2, armor, weapon, extra)["value"] >= build.evaluate(chosen, armor, weapon, extra)["value"]:
        chosen = chosen2
    extra, free = extra_fn(chosen, armor, weapon)
    final = build.evaluate(chosen, armor, weapon, extra)
    if floor_build.floors and not final.get("floors_met", True):
        # Your gear can't reach the minimums with any tree: plan the tree, powders and upgrades for it on the
        # goal alone (otherwise they'd only chase the minimums), and let the whole-build search keep them.
        print("  !! Your current gear can't reach your minimums ("
              + "; ".join(floor_misses(floor_build.floors, final.get("floor_values", {})))
              + "). The tree, powders and upgrades for it below aim at your goal alone; the best whole build "
              "further down picks gear that keeps every minimum.")
        build = build.copy_with(floors=None)
        chosen, _ = optimize_tree(build, armor, weapon, budget, keep=keep_ids, extra_points=extra0, beam_width=args.beam)
        extra, free = extra_fn(chosen, armor, weapon)
        rec_powders, _ = best_weapon_powders(build, chosen, armor, base_weapon, extra, args.max_powder_tier)
        if user_weapon_powders is None and base_weapon.slots:
            weapon = weapon_with_powders(base_weapon, rec_powders)
            extra, free = extra_fn(chosen, armor, weapon)
        chosen2, _ = best_tree(build, armor, weapon, budget, keep=keep_ids, extra_points=extra, beam_width=args.beam)
        if build.evaluate(chosen2, armor, weapon, extra)["value"] >= build.evaluate(chosen, armor, weapon, extra)["value"]:
            chosen = chosen2
        extra, free = extra_fn(chosen, armor, weapon)
        final = build.evaluate(chosen, armor, weapon, extra)
        final = dict(final, floor_values=floor_values(final["stats"], final["defense"], final.get("ls_share", 1.0)),
                     floors_met=False)

    search_start = chosen            # the whole-build search starts from this tree, exactly as before
    try:                             # spare points are left unspent (abilities that don't change the goal's number)
        pruned, tree_ideas = prune_spare(build, chosen, armor, weapon, extra, level)
    except Exception:
        pruned, tree_ideas = chosen, []
    if pruned != frozenset(chosen):
        chosen = pruned
        final = build.evaluate(chosen, armor, weapon, extra)
    LAST_RESULT["current_ideas"] = tree_ideas
    LAST_RESULT.update(cls=cls, level=level, goal=goal, focus=focus, tree=tree, build=floor_build, no_gear=no_gear,
                       current={"armor": list(armor), "weapon": weapon, "owned": frozenset(chosen), "extra": list(extra),
                                "eval": final})
    req_line = ", ".join(f"{SKILL_NAMES[i]} {assigned[i]}" for i in range(5) if assigned[i])
    print(f"  Needed to wear everything: {sum(assigned)} points ({req_line or 'none'})")
    ex_line = ", ".join(f"{SKILL_NAMES[i]} +{extra[i]}" for i in range(5) if extra[i])
    if avail - sum(assigned) >= 0:
        print(f"  Best use of the other {avail - sum(assigned)}: {ex_line or 'nothing helps your goal'}"
              + (f" ({free} left over)" if free > 0 else ""))
    total_assign = [assigned[i] + extra[i] for i in range(5)]
    print("  Put in:   " + "  ".join(f"{SKILLS[i].upper()} {total_assign[i]}" for i in range(5)))
    print("  Totals:   " + "  ".join(f"{SKILLS[i].upper()} {final['totals'][i]}" for i in range(5))
          + "   (with item bonuses)")
    eq = " → ".join(it.name for it in order if it)
    print(f"  Equip in this order: {eq}")

    # ---- powders
    print_header("Powders")
    if base_weapon.slots:
        tier_cap = max_powder_tier(getattr(base_weapon, "level_high", base_weapon.level), args.max_powder_tier)
        rec2, rec_val = best_weapon_powders(build, chosen, armor, base_weapon, extra, args.max_powder_tier)
        def plabel(pw):
            return ", ".join(p.label() for p in pw) if pw else "none"
        if user_weapon_powders is None:
            print(f"  Weapon ({base_weapon.slots} slots, up to T{tier_cap} at item level {base_weapon.level}): "
                  f"use {plabel(rec2)}  (in this order)")
            print("  The rest of this report assumes these powders. Use --powders \"weapon=...\" to enter yours.")
        else:
            print(f"  Weapon: you have {plabel(weapon.powders)}")
            cur_val = final["value"]
            if rec_val > cur_val * 1.01:
                print(f"  Better: {plabel(rec2)} (in this order) for +{(rec_val / cur_val - 1) * 100:.0f}% on your goal")
            else:
                print("  That's already the best choice for your goal.")
        sp = final.get("special")
        if sp:
            what = []
            if sp["damage"]:
                what.append(f"{fmt_num(sp['damage'])} damage")
            if sp["boost"]:
                what.append(f"+{sp['boost']:g}% damage for {WEAPON_SPECIALS[[w[0] for w in WEAPON_SPECIALS].index(sp['name'])][3]}s")
            print(f"  Special unlocked: {sp['name']} level {sp['level']} ({', '.join(what)}), "
                  f"counted about every {sp['cycle']:.0f}s")
        else:
            print("  No weapon special (needs 2+ slots and two Tier 4+ powders of the first powder's element).")
    else:
        print(f"  {base_weapon.name} has no powder slots.")
    ap_picks, _ap_armor = best_armor_powders(build, chosen, armor_base, armor, weapon, extra, args.max_powder_tier)
    if ap_picks:
        for s_i, (pw, gain, matters) in ap_picks.items():
            have = armor[s_i].powders if armor[s_i] is not None else []
            elem = POWDER_ELEM_NAMES[pw[0].elem] if matters else "any element"
            what = f"{len(pw)}x T{pw[0].tier} {elem}"
            now_txt = f" (now: {', '.join(p.label() for p in have)})" if have else ""
            print(f"  {SLOT_LABELS[s_i]}: {what}  +{gain * 100:.1f}% for your goal{now_txt}")
        if not all(m for _pw, _g, m in ap_picks.values()):
            print("  Every element adds the same health; pick the element you get hit by most "
                  "(or enter it with --enemy so it's chosen for you).")
    elif any(a is not None and a.slots for a in armor_base[:4]):
        if goal in ("dps", "spells", "burst", "melee", "healing"):
            print("  Armor powders don't change damage or healing for this goal (they add health and defense).")
        else:
            print("  Your armor powders are already the best choice.")
    armor_sp = [(SLOT_LABELS[s], a) for s, a in enumerate(armor) if a is not None and a.armor_special]
    for label, a in armor_sp:
        e, lvl = a.armor_special
        print(f"  {label}: {ARMOR_SPECIALS[e]} level {lvl} (situational elemental damage bonus; not counted)")

    print_header(f"Ability tree for level {level} ({budget} ability points)")
    for line in describe_tree(tree, chosen, level):
        print(line)
    for line in unspent_lines(tree, chosen, level, tree_ideas):
        print(line)
    print("  " + archetype_line(tree, chosen, build.archetype, build.avoid_archetypes))
    if link_tree:
        yours = build.evaluate(link_tree, armor, weapon, extra)
        v = tree.validate(link_tree, level)
        gain = final["value"] / yours["value"] - 1 if yours["value"] > 0 else 0
        status = "legal" if v["ok"] else "not legal (" + "; ".join(v["errors"][:2]) + ")"
        print(f"  Your tree from the link: {len(link_tree)} abilities, {v['ap']} AP, {status}.")
        if gain > 0.005:
            added = [tree.by_id[i].name for i in chosen if i not in link_tree]
            removed = [tree.by_id[i].name for i in link_tree if i not in chosen]
            print(f"  This tree scores {gain * 100:.0f}% higher for your goal. Add: {', '.join(added) or '-'}; "
                  f"remove: {', '.join(removed) or '-'}")
        else:
            print("  Your tree is already as good as the recommendation for this goal.")

    _sp, _ts, _dm, _df, inter = tree.spells(chosen, build.extra_abilities(armor, weapon, raid_buff_stats(build.raid_buffs)[1]),
                                            build.toggles, build.sliders, build.slider_mode)
    if inter["toggles"] or inter["sliders"]:
        print_header("Toggles and sliders in this tree")
        for name, (mx, val) in inter["sliders"].items():
            src = "you set" if name in build.sliders else {"half": "half of max", "none": "default", "max": "max"}[build.slider_mode]
            print(f"  Slider  {name:<32} {val:g} of {mx:g}  ({src})")
        base_v = final["value"]
        tm = build.toggle_map(chosen)
        mask = final.get("mask")
        for name in inter["toggles"]:
            f = tm.get(name, 0.0)
            need = TOGGLE_NEEDS.get(name)
            if name in auto_on and need and not need[0](tree, chosen):
                print(f"  Toggle  {name:<32} off  ({need[1]})")
                continue
            if name in MASKS and mask:
                status = "ON" if name == mask else "off"
                note = "  (the mask you'd wear: best for your goal)" if name == mask else ""
            elif name == "Lunatic Transmute" and mask == "Mask of the Lunatic" and name not in build.toggles_off:
                status, note = "ON", "  (comes with Mask of the Lunatic)"
            elif f >= 1:
                status = "ON"
                note = (f"  (assumed: {auto_on[name][1]}; --toggles-off \"{name}\" if you don't)" if name in auto_on
                        else "  (you turned it on)")
            elif f > 0:
                status = f"{f * 100:.0f}% of the time"
                note = (f"  (assumed: {auto_on[name][1]})" if name in auto_on else "  (you set this)")
            else:
                status, note = "off", ""
                if base_v > 0:
                    try:     # what keeping it up would add, so you can decide
                        on_v = build.copy_with(toggles=build.toggles | {name}).evaluate(chosen, armor, weapon, extra)["value"]
                        if on_v > base_v * 1.001:
                            g = (on_v / base_v - 1) * 100
                            gs = f"{g:.0f}" if g >= 10 else f"{g:.1f}"
                            note = (f"  (if you keep it up all the time: +{gs}% for your goal; "
                                    "half the time adds about half that)")
                    except Exception:
                        pass
            print(f"  Toggle  {name:<32} {status}{note}")
        print('  Turn a toggle on with --toggles "Name" (or "Name=60%" if it\'s up part of the time), off with')
        print('  --toggles-off "Name". In the chat, just say e.g. "I keep Contamination up about 70% of the time".')
        print("  Only buffs whose timing is known are counted by default; the rest are off so numbers aren't inflated.")
        print("  Toggles you turn on also count when the tree and whole build are chosen.")
        print("  Stacks can change the best tree a lot, so it's worth entering what you usually reach.")

    print_header("What it does")
    for bs in sorted(final["results"]):
        r = final["results"][bs]
        if r["damage"] <= 0 and bs != 0:
            continue
        label = "Main attack" if bs == 0 else r["name"]
        if bs in tree.ult_spells:
            label += " (ultimate)"
        cost = f"   {r['cost']:.0f} mana" if bs in (1, 2, 3, 4) else ""
        gain = f"   +{r['mana_gained']:.0f} mana/hit" if r["mana_gained"] else ""
        if bs in (1, 2, 3, 4) and r["is_dps"]:
            print(f"  {label:<26} {fmt_num(r['cast']):>8} avg damage{cost}{gain}"
                  f"   (up to {fmt_num(r['damage'])}/s while its buff or ultimate is active)")
        else:
            unit = "damage/s while active" if r["is_dps"] else "avg damage"
            near = ("   (only hits enemies that come close: not in damage per second, only in the combo)"
                    if r.get("name") in CONTACT_SPELLS else "")
            print(f"  {label:<26} {fmt_num(r['damage']):>8} {unit}{cost}{gain}{near}")
    rot = final["rotation"]
    if rot:
        names = " + ".join(final["results"][b]["name"] for b in rot)
        print(f"  Best spell rotation: {names}  ≈ {fmt_num(final['spell_dps'])} damage/s "
              f"(mana income {final['mana_income']:.1f}/s)")
        for line in mana_lines(final.get("mana")):
            print("  " + line)
    print(f"  Main attack: {fmt_num(final['melee_dps'])} DPS (Wynnbuilder's Average DPS: damage per hit x attack speed)"
          f"   One full combo: {fmt_num(final['burst'])}")
    if goal == "melee" and final.get("special"):
        print("  The weapon special's damage boost isn't included in that DPS (Wynnbuilder leaves it off until you turn it on).")
    if goal == "ability" and focus:
        r = final["results"].get(focus[0])
        unit = "damage/s while active" if (r and r["is_dps"]) else "average damage per cast"
        print(f"  Focused ability: {focus[1]}: {fmt_num(final['focus_value'])} {unit}")
    if goal == "balanced":
        print(f"  Your damage per second comes mainly from {final['dps_style']}s: spells "
              f"{fmt_num(final['spell_goal'])}/s, main attacks {fmt_num(final['melee_goal'])}/s "
              "(including summons, poison and weapon special).")
    if final["triggered_dps"] > 0:
        print(f"  Situational hits ({', '.join(final['triggered_names'][:4])}) would add about "
              f"{fmt_num(final['triggered_dps'])}/s if they triggered every {EXTRA_TRIGGER_EVERY:.0f}s; they depend on "
              "the fight, so they only count in the burst combo, not in damage per second.")
    if final["poison_spell"] or final["poison_melee"]:
        print(f"  Poison: +{fmt_num(final['poison_spell'])}/s while hitting with spells, "
              f"+{fmt_num(final['poison_melee'])}/s with main attacks (included above in your goal score)")
    print("  (Mana income: base regen + mana regen; mana steal only counts for time spent main-attacking.)")
    for line in mixed_lines(final, goal):
        print(line)
    dfn = final["defense"]
    print_header("Survivability")
    print(f"  Health {fmt_num(dfn['hp'])}   Effective HP {fmt_num(dfn['ehp_agi'])} "
          f"(without Agility dodge: {fmt_num(dfn['ehp'])})   HP regen {dfn['hpr']:.0f}")
    print(f"  Defense {dfn['def_pct']:.1f}% less damage   Agility {dfn['agi_pct']:.1f}% dodge chance"
          f"   Walk speed {fmt_speed(final['stats'].get('spd', 0))}")
    if floor_build.floors:
        miss = floor_misses(floor_build.floors, final.get("floor_values", {}))
        if miss:
            print("  !! With this gear you miss your minimums: " + "; ".join(miss)
                  + ". The best whole build below picks gear that meets them.")
        else:
            print(f"  Meets your minimums: {describe_floors(floor_build.floors)}.")
    print("  Elemental defense: " + "  ".join(f"{POWDER_ELEM_NAMES[i]} {dfn['elem_def_eff'][i]:.0f}" for i in range(5)))
    if any(abs(a - b) > 0.5 for a, b in zip(dfn["elem_def"], dfn["elem_def_eff"])):
        print("  (Wynnbuilder shows " + ", ".join(f"{POWDER_ELEM_NAMES[i]} {dfn['elem_def'][i]:.0f}" for i in range(5)
              if abs(dfn['elem_def'][i] - dfn['elem_def_eff'][i]) > 0.5)
              + ": it lets negative raw defense turn positive with a big % bonus; here it stops at 0.)")
    if final["life_steal_ps"] > 0:
        sh = final.get("ls_share", 1.0)
        print(f"  Life steal: {fmt_num(final['life_steal_ps'] * 3)} per 3s while you keep main-attacking "
              f"(≈ {fmt_num(final['life_steal_ps'])} HP/s)"
              + ("" if sh >= 1 else " - it only works on main attack hits, so it isn't counted for this spell build"
                 if sh <= 0 else f" - counted for the {sh * 100:.0f}% of the time spent main-attacking between casts"))
    if build.enemy:
        hit = " + ".join(f"{fmt_num(v)} {ELEM_NAMES[e].lower()}" for e, v in build.enemy.items())
        print(f"  Against a hit of {hit}: you take {fmt_num(dfn['enemy_taken'])} per hit, "
              f"{dfn['hits_to_die']:.1f} hits to die (effective HP {fmt_num(dfn['ehp_enemy'])})")
    if goal == "healing" and final["heal_ps"] <= 0:
        print(f"  {CLASS_TITLE[cls]} has no healing spells in this tree; the healing goal can't score anything.")
    if final["heal_ps"] > 0:
        names = " + ".join(final["results"][b]["name"] for b in final["heal_rotation"])
        print(f"  Healing: {fmt_num(final['heal_ps'])} HP/s with {names}")
    if goal not in ("melee", "dps") and final["melee_dps"] > 1.5 * max(final["spell_dps"], 1):
        print(f"  Tip: your main attack does {final['melee_dps'] / max(final['spell_dps'], 1):.1f}x the damage per second "
              "of your spells with this gear. Try --goal melee to build around it.")
    elif goal not in ("spells", "dps") and final["spell_dps"] > 1.5 * max(final["melee_dps"], 1):
        print("  Tip: your spells out-damage your main attack with this gear. Try --goal spells.")

    # ---- level plan
    if not args.no_plan:
        print_header("Level-by-level plan")
        if level >= len(AP_TABLE) - 1:
            print("  You're at the level cap.")
        else:
            for line in level_plan(build, chosen, armor, weapon, keep_ids, args.plan_levels, args.rank_ap, args.beam,
                                   include_never):
                print(line)
            print("  New gear is compared with what you wear now (only items that unlock at that level, +3% or more).")

    # ---- gear suggestions
    if not args.no_gear and only is None:
        print_header(f"Gear upgrades you can wear (items level {max(1, level - 12)}–{level})")
        sugg, base_val = suggest_gear(build, chosen, armor, weapon, extra_fn, per_slot=args.per_slot,
                                      include_never=include_never)
        any_s = False
        for slot, lst in sugg.items():
            label = "Weapon" if slot == "weapon" else SLOT_LABELS[slot]
            current = weapon.name if slot == "weapon" else (armor[slot].name if armor[slot] else "(empty)")
            if not lst:
                continue
            any_s = True
            print(f"  {label} (now: {current})")
            for gain, it in lst:
                print(f"     +{gain * 100:5.1f}%  {it.name:<24} {it.short()}")
        sets, _ = suggest_sets(build, chosen, armor, weapon, extra_fn, include_never=include_never)
        if sets:
            print("  Set combos (set bonuses need 2+ pieces worn together):")
            for gain, sname, placed, _a2, _w2 in sets:
                pieces = ", ".join(f"{it.name} ({'weapon' if sl == 'weapon' else SLOT_LABELS[sl]})"
                                   for sl, it in sorted(placed.items(), key=lambda x: str(x[0])))
                print(f"     +{gain * 100:5.1f}%  {sname} set, {len(placed)} pieces: {pieces}")
        if not any_s and not sets:
            print("  Nothing in range beats your current gear one piece at a time for this goal.")
        print("  Gains assume you re-spend skill points and keep the tree above. Weapons get their own best powders;")
        print("  armor powders move to the new piece. Items marked \"not a mob drop\" can be raid, quest, merchant or")
        print("  event rewards, or no longer obtainable; use --drops-only to leave them out.")

    # ---- whole build: all gear chosen together
    if not args.no_gear and not getattr(args, "no_whole_build", False):
        print_header("Best whole build (all gear chosen together)")
        import wynn_search as ws
        rng = getattr(args, "item_levels", None)
        min_lvl = max(1, level - rng) if rng else 1
        mode = "Deep search" if getattr(args, "deep", False) else "Searching"
        print(f"  {mode}: every item from level {min_lvl} to {level}" + ("" if ALLOW_MYTHIC else " except mythics (say you can use mythics to include them)")
              + ", built around each top weapon"
              + (f" (stops after {args.time_limit:.0f}s)" if getattr(args, "time_limit", None) else "") + "...")
        if ws.PROGRESS.get("cb") is None:
            ws.PROGRESS["cb"] = lambda f, m: None
        # Tomes are part of the build from level 55 (unless you listed yours, turned it off, or build from your
        # items): picked for the finished build, after the search, like armor powders. (Picking them before the
        # search once led it to a worse build, so the search path stays the same and builds only gain.)
        tome_base, tome_picks = floor_build, []
        auto_on = (not build.tomes and getattr(args, "auto_tomes", True) and level >= AUTO_TOMES_LEVEL
                   and only is None)        # building from your items: only tomes you list (--tomes)
        # Other ability trees: the search picks gear for one tree at a time, and the tree above is planned around
        # your starting gear, which can lock it into one style (a main-attack tree never seriously tries spell
        # gear: assassin L50 balanced found 10,165 that way, 15,740 from a spell tree). When the goal's damage can
        # come from either style, the search also runs from a tree planned for spells and one planned for main
        # attacks (where those differ from the tree above), and their builds are added to the ones to choose from.
        seeds, finals_in, style_runs, orig_cb, stopped = [], [], [], ws.PROGRESS.get("cb"), False
        if style_open(floor_build.goal, getattr(floor_build, "goal2", None)):
            for g in ("spells", "melee"):
                t_g, _ = best_tree(build.copy_with(goal=g, goal2=None), armor, weapon, budget, keep=keep_ids,
                                   extra_points=extra, beam_width=args.beam)
                if frozenset(t_g) != frozenset(search_start) and all(frozenset(t_g) != frozenset(t) for _g, t in style_runs):
                    style_runs.append((g, t_g))
        n_runs = (2 if goal == "mixed" else 0) + len(style_runs) + 1
        tl = getattr(args, "time_limit", None)

        def keep_final(wg):
            finals_in.append((wg["armor"], wg.get("base_weapon") or wg["weapon"], wg["weapon"], frozenset(wg["owned"]),
                              list(wg["extra"])))
        # Most damage in any style: first the best spell-only and main-attack-only builds, each searched as deeply as
        # the main search (owner: nothing sacrificed), and the main search starts from those too, so it can only end
        # at or above them (in any-style damage).
        if goal == "mixed":
            by_name = {it.name: it for it in data.items.values()}
            for k, g in enumerate(("spells", "melee")):
                ws.PROGRESS["cb"] = (lambda lo, g: lambda f, m: orig_cb(lo + f / n_runs, f"First, the best {GOAL_WORDS[g]} build: {m}"))(k / n_runs, g)
                wg = ws.search_best_build(floor_build.copy_with(goal=g, goal2=None), search_start, armor, weapon, base_weapon,
                                          keep=keep_ids, ap_budget=budget, include_never=include_never, min_level=min_lvl,
                                          deep=getattr(args, "deep", False), time_limit=(tl / 3 if tl else None), log=None,
                                          crafted=False, only=only)
                arm = [by_name.get(a.name) if (a is not None and not a.crafted and not a.custom) else None for a in wg["armor"]]
                bw = wg.get("base_weapon") or wg["weapon"]
                if not (bw.crafted or bw.custom):
                    seeds.append((arm, by_name.get(bw.name, bw)))
                keep_final(wg)
                if wg.get("stopped"):
                    stopped = True
                    break
            print("  Most damage in any style: the search also starts from the best spell-only and main-attack-only builds.")
        done_runs = 2 if goal == "mixed" else 0
        for g, t_g in style_runs:
            if stopped:
                break
            ws.PROGRESS["cb"] = (lambda lo, g: lambda f, m: orig_cb(lo + f / n_runs, f"Searching with a tree planned for {GOAL_WORDS[g]}: {m}"))(done_runs / n_runs, g)
            wg = ws.search_best_build(floor_build, t_g, armor, weapon, base_weapon, keep=keep_ids, ap_budget=budget,
                                      include_never=include_never, min_level=min_lvl, deep=getattr(args, "deep", False),
                                      time_limit=(tl / n_runs if tl else None), log=None, crafted=False, only=only)
            keep_final(wg)
            done_runs += 1
            if wg.get("stopped"):
                stopped = True
        if style_runs:
            print(f"  The search also ran from {len(style_runs)} other ability tree(s), planned for "
                  + " and ".join(GOAL_WORDS[g] for g, _t in style_runs) + ".")
        ws.PROGRESS["cb"] = (lambda lo: lambda f, m: orig_cb(lo + f / n_runs, m))(done_runs / n_runs)
        try:
            wb = ws.search_best_build(floor_build, search_start, armor, weapon, base_weapon, keep=keep_ids, ap_budget=budget,
                                      include_never=include_never, min_level=min_lvl,
                                      deep=getattr(args, "deep", False),
                                      time_limit=(1 if stopped else getattr(args, "time_limit", None)),
                                      log=lambda m: print("  " + m), crafted=not getattr(args, "no_crafted", True),
                                      only=only, extra_starts=seeds, extra_finals=finals_in)
        finally:
            ws.PROGRESS["cb"] = orig_cb
        if auto_on:
            picks = auto_tomes(tome_base, frozenset(wb["owned"]), wb["armor"], wb["weapon"], wb["extra"])
            if picks:
                tb2 = tome_base.copy_with(tomes=[t for _k, t, _g in picks])
                own_b = frozenset(wb["owned"])
                fin2 = tb2.evaluate(own_b, wb["armor"], wb["weapon"], wb["extra"])
                ex3, _ = ws.refine_points(tb2, own_b, wb["armor"], wb["weapon"], list(wb["extra"]), fast=False)
                ex3, _free3 = ws._legal_points(tb2, wb["armor"], wb["weapon"], ex3)
                fin3 = tb2.evaluate(own_b, wb["armor"], wb["weapon"], ex3)
                if fin3["value"] > fin2["value"] and fin3.get("floors_met", True) >= fin2.get("floors_met", True):
                    fin2, wb["extra"], wb["free"] = fin3, ex3, _free3      # skill points re-spent with the tomes on
                if fin2["value"] >= wb["final"]["value"] and fin2.get("floors_met", True) >= wb["final"].get("floors_met", True):
                    st_e, _ = ws.refine_points(tb2, frozenset(search_start), armor, weapon, [0] * 5, fast=False)
                    wb["start"] = tb2.evaluate(frozenset(search_start), armor, weapon, st_e)
                    wb["final"], floor_build, tome_picks = fin2, tb2, picks
                    LAST_RESULT["build"] = floor_build
        LAST_RESULT["auto_tomes"] = [(k, t.name, g) for k, t, g in tome_picks]
        if wb.get("stopped"):
            print("  (Stopped early: this is the best build found so far.)")
        bs = wb.get("bonus_search") or {}
        if bs.get("found_by"):
            print(f"  How sure: the main search's attempts ended lower; the extra {bs['found_by']} on top found this "
                  "better build. Running again (with Deep search if this was a quick one) may find better still.")
        elif wb.get("attempts"):
            k, n = wb["agree"], wb["attempts"]
            sure = ("very likely the best" if k >= 3 and k >= n / 2 else
                    "probably the best" if k >= 2 else
                    "only one attempt got here; Deep search may find better")
            print(f"  How sure: {k} of {n} search attempts around this weapon, each from a different starting point, "
                  f"ended at this build ({sure}).")
        fin, st = wb["final"], wb["start"]
        LAST_RESULT["best"] = {"armor": list(wb["armor"]), "weapon": wb["weapon"], "owned": frozenset(wb["owned"]),
                               "extra": list(wb["extra"]), "eval": fin}
        LAST_RESULT["start"] = st
        LAST_RESULT["craft_info"] = wb.get("crafted") or {}
        gain = fin["value"] / st["value"] - 1 if st["value"] > 0 else 0
        if floor_build.floors:
            if not fin.get("floors_met", True):
                print("  !! No build in range meets every minimum you set. This is the closest one; it still misses: "
                      + "; ".join(floor_misses(floor_build.floors, fin.get("floor_values", {}))) + ".")
                gain = 0
            elif not st.get("floors_met", True):
                print(f"  Your current gear misses your minimums ({'; '.join(floor_misses(floor_build.floors, st.get('floor_values', {})))}); "
                      "this build meets them all.")
            else:
                print(f"  Meets your minimums: {describe_floors(floor_build.floors)}.")
        floor_case = bool(floor_build.floors) and (not fin.get("floors_met", True) or not st.get("floors_met", True))
        if not floor_case and gain < 0.005:
            print("  Your current gear is already the best combination in range for this goal.")
        else:
            if floor_case:
                print("  Each piece was picked with the others on, so skill points they give each other, set bonuses,")
                print("  elements, major IDs and the re-planned ability tree all count.")
            else:
                print(f"  +{gain * 100:.0f}% for your goal compared with "
                      + ("wearing nothing (you didn't enter any gear)" if no_gear else "what you wear now")
                      + ". Each piece was picked with the")
                print("  others on, so skill points they give each other, set bonuses, elements, major IDs and the")
                print("  re-planned ability tree all count.")
            for s_i in range(8):
                it = wb["armor"][s_i]
                old_it = armor[s_i]
                mark = "keep" if (it is not None and old_it is not None and it.name == old_it.name) else "new"
                if it is None:
                    continue
                apw = (" with " + ", ".join(p.label() for p in it.powders)) if getattr(it, "powders", None) else ""
                print(f"  {SLOT_LABELS[s_i]:<11} {it.name:<24} ({mark}) {it.short()}{apw}")
            w = wb["weapon"]
            mark = "keep" if w.name == weapon.name else "new"
            pw = (" with " + ", ".join(p.label() for p in w.powders)) if w.powders else ""
            print(f"  {'Weapon':<11} {w.name:<24} ({mark}) {w.short()}{pw}")
            ass, _, _ = solve_skill_points(wb["armor"], w)
            print("  Skill points: " + "  ".join(f"{SKILLS[i].upper()} {ass[i] + wb['extra'][i]}" for i in range(5)))
            added = [tree.by_id[i].name for i in wb["owned"] if i not in chosen]
            removed = [tree.by_id[i].name for i in chosen if i not in wb["owned"]]
            if added or removed:
                print(f"  Tree changes for this gear: add {', '.join(added) or '-'}; remove {', '.join(removed) or '-'}")
            print("  " + archetype_line(tree, wb["owned"], build.archetype, build.avoid_archetypes))
            try:                      # why each ability is there (the panel's tree map shows the same)
                roles_b = tree_roles(floor_build, wb["owned"], wb["armor"], w, wb["extra"], level)
                LAST_RESULT["best_roles"] = roles_b
                for line in roles_lines(tree, roles_b):
                    print(line)
                LAST_RESULT["best_ideas"] = wb.get("tree_ideas") or []
                for line in unspent_lines(tree, wb["owned"], level, wb.get("tree_ideas") or []):
                    print(line)
            except Exception:
                pass
            names_b = inter_names(tree, wb["owned"])
            tm_b = floor_build.toggle_map(wb["owned"])
            counted = [f"{k} ({'on' if v >= 1 else f'{v * 100:.0f}% of the time'})" for k, v in tm_b.items() if k in names_b]
            if fin.get("mask"):
                counted.append(f"{fin['mask']} (the mask you'd wear)")
            if counted:
                print("  Ability buffs counted in this build: " + ", ".join(counted) + ". (See \"Toggles and sliders\" for why.)")
            for line in special_effect_lines(data, list(wb["armor"]) + [w]):
                print(line)
            if tome_picks:
                print("  Tomes in this build (counted in every number here, before and after): "
                      + ", ".join(t.name for _k, t, _g in tome_picks) + ".")
            def nums(r):
                return (r["spell_dps"] + (r["poison_spell"] if r["spell_dps"] else 0),
                        r["melee_dps"] + (r["poison_melee"] if r["melee_dps"] else 0), r["defense"].get("ehp_agi", 0))
            a0, a1 = nums(st), nums(fin)
            if goal == "ability" and focus:
                print(f"  {focus[1]}: {fmt_num(st.get('focus_value', 0))} -> {fmt_num(fin.get('focus_value', 0))}")
            print(f"  Spell damage/s {fmt_num(a0[0])} -> {fmt_num(a1[0])}   Main attack/s {fmt_num(a0[1])} -> {fmt_num(a1[1])}"
                  f"   Effective HP {fmt_num(a0[2])} -> {fmt_num(a1[2])}")
            print(f"  Health {fmt_num(st['defense'].get('hp', 0))} -> {fmt_num(fin['defense'].get('hp', 0))}"
                  f"   Walk speed {fmt_speed(st['stats'].get('spd', 0))} -> {fmt_speed(fin['stats'].get('spd', 0))}"
                  f"   Mana regen {st['stats'].get('mr', 0):.0f} -> {fin['stats'].get('mr', 0):.0f}")
            if fin.get("mana") and goal in ("spells", "burst", "balanced") or fin.get("mana") and build.goal2 == "spells":
                for line in mana_lines(fin.get("mana")):
                    print("  Best build's " + line[0].lower() + line[1:] if line.startswith("Mana") else "  " + line)
            for line in mixed_lines(fin, goal):
                print(line.replace("Most damage, any style", "Best build's most damage, any style", 1))
            if fin.get("goal_parts"):
                sp_, fp_ = st.get("goal_parts") or {}, fin["goal_parts"]
                print("  Goal mix: " + "   ".join(f"{GOAL_WORDS.get(k, k).capitalize()} {fmt_num(sp_.get(k, 0))} -> "
                                                    f"{fmt_num(v)}" for k, v in fp_.items())
                      + f"   (score {fmt_num(st['main'])} -> {fmt_num(fin['main'])})")
            bs = wb.get("bonus_search") or {}
            if bs.get("found_by") and bs.get("after", 0) > bs.get("before", 0) > 0:
                print(f"  The extra search ({bs['found_by']}) found this build: "
                      f"+{(bs['after'] / bs['before'] - 1) * 100:.1f}% over the main search's best.")
            if data.enc and data.version in data.version_names:
                try:
                    url = encode_build_link(data, tree, data.enc, data.version_names.index(data.version), level,
                                            wb["armor"], w, floor_build.tomes, build.aspects, wb["owned"],
                                            [fin["totals"][i] - wb["extra"][i] for i in range(5)], fin["totals"])
                    print("  Whole build in Wynnbuilder: " + url)
                    LAST_RESULT["best_link"] = url
                    tick = sorted(set(floor_build.toggle_map(wb["owned"])) - set(floor_build.toggle_share)
                                  | ({fin["mask"]} if fin.get("mask") else set()))
                    if fin.get("mask") == "Mask of the Lunatic" and "Lunatic Transmute" in inter_names(tree, wb["owned"]) \
                            and "Lunatic Transmute" not in floor_build.toggles_off:
                        tick.append("Lunatic Transmute")
                    part = {k: v for k, v in floor_build.toggle_share.items() if k in inter_names(tree, wb["owned"])}
                    tick = [t for t in tick if t in inter_names(tree, wb["owned"])]
                    if tick or part:
                        print("  Wynnbuilder starts with every toggle off. " +
                              (f"To see the same numbers there, tick: {', '.join(tick)}. " if tick else "") +
                              (f"It can't show buffs that are up part of the time ({', '.join(f'{k} {v * 100:.0f}%' for k, v in part.items())}), "
                               "so its numbers will be a little lower." if part else ""))
                except Exception:
                    pass
            if any("not a mob drop" in (x.short() if x else "") for x in wb["armor"] + [w]):
                print("  \"Not a mob drop\" items come from raids, quests, merchants or events, or are no longer obtainable;")
                print("  use --drops-only (or the drops option in the app) to build only from mob drops.")
            print_crafts(data, [(SLOT_LABELS[i], x) for i, x in enumerate(wb["armor"]) if x is not None]
                         + [("Weapon", w)], wb.get("crafted") or {})
        if only is not None:
            print_worth_getting(floor_build, wb, only, extra_fn, include_never, args.per_slot)

    # ---- how to play: from the best whole build when there is one, else the current gear
    try:
        hb = LAST_RESULT.get("best")
        if hb:
            hp_lines = how_to_play(floor_build, frozenset(hb["owned"]), hb["armor"], hb["weapon"], hb["eval"])
        else:
            hp_lines = how_to_play(floor_build, chosen, armor, weapon, final)
    except Exception:
        hp_lines = []
    LAST_RESULT["how_to_play"] = hp_lines
    if hp_lines:
        print_header("How to play this build")
        for line in hp_lines:
            print("  - " + line)
    # ---- what Wynnbuilder shows for the link, and why the app can differ
    try:
        if hb and LAST_RESULT.get("best_link"):
            wv = wynnbuilder_view(floor_build, frozenset(hb["owned"]), hb["armor"], hb["weapon"], hb["extra"], hb["eval"])
        else:
            wv = []
    except Exception:
        wv = []
    LAST_RESULT["wynnbuilder_view"] = wv
    if wv:
        print_header("In Wynnbuilder (the best build's link above)")
        for line in wv:
            print("  " + line)
    if link_tree:                      # the player's own link: what Wynnbuilder shows for it (their tree)
        try:
            # the link's own skill points where it set them (Wynnbuilder fills the rest with what the gear needs)
            need = solve_skill_points(armor, weapon)[0]
            sp_l = list(link.get("sp") or [None] * 5)
            ex_l = [max(0, sp_l[i] - need[i]) if sp_l[i] is not None else 0 for i in range(5)]
            ev_l = build.evaluate(link_tree, armor, weapon, ex_l)
            wl = wynnbuilder_view(build, link_tree, armor, weapon, ex_l, ev_l)
        except Exception:
            wl = []
        LAST_RESULT["wynnbuilder_view_link"] = wl
        if wl:
            print_header("Your link in Wynnbuilder (your gear and your tree)")
            for line in wl:
                print("  " + line)

    # ---- tomes and aspects
    if data.tomes:
        print_header("Tomes")
        if build.tomes:
            print("  Using: " + ", ".join(t.name for t in build.tomes))
        auto_t = LAST_RESULT.get("auto_tomes") or []
        if level < min(t.level for t in data.tomes):
            print(f"  Tomes start at level {min(t.level for t in data.tomes)}.")
        elif auto_t:
            print("  Picked for the best whole build above (and counted in its numbers):")
            for ttype, name, gain in auto_t:
                kind = {"weaponTome": "Weapon tome", "armorTome": "Armor tome"}[ttype]
                print(f"  {kind:<12} {name:<44} " + (f"+{gain * 100:4.1f}%" if gain is not None
                                                      else "adds health (your goal's number doesn't change)"))
            if not any(k == "weaponTome" for k, _n, _g in auto_t):
                print("  No weapon tome adds to this goal, so those slots are left free.")
            print("  Guild tomes need a guild, so they're left out; list yours (or say which tomes you have) to use them.")
        else:
            picks, total = recommend_tomes(build, chosen, armor, weapon, extra)
            skipped = [p for p in picks if p[2] < 0.001]
            picks = [p for p in picks if p[2] >= 0.001]
            if skipped:
                kinds = sorted({ {"weaponTome": "weapon", "armorTome": "armor", "guildTome": "guild"}[p[0]] for p in skipped})
                print(f"  {', '.join(kinds).capitalize()} tomes don't add damage for this goal (they add defense or health).")
            if picks:
                for ttype, tm, gain in picks:
                    kind = {"weaponTome": "Weapon tome", "armorTome": "Armor tome", "guildTome": "Guild tome"}[ttype]
                    print(f"  {kind:<12} {tm.name:<44} +{gain * 100:4.1f}%")
                print(f"  All together: +{total * 100:.0f}% for your goal")
            else:
                print("  None of the tomes you can use add to your goal.")
    if data.aspects:
        print_header(f"Aspects ({aspect_slots(level)} slots at level {level})")
        for asp, tier in build.aspects:
            print(f"  Using: {asp['displayName']} (tier {tier})")
        if level < 54 and not build.aspects:
            print("  Aspects come from raids, which start at level 54.")
        else:
            picks, total = recommend_aspects(build, chosen, armor, weapon, extra, args.aspect_tier)
            for asp, tier, gain in picks:
                desc = strip_mc(asp["tiers"][tier - 1].get("description", ""))
                print(f"  {asp['displayName']:<40} tier {tier}  +{gain * 100:4.1f}%   {desc[:70]}")
            if picks:
                print(f"  All together: +{total * 100:.0f}% for your goal (at tier {args.aspect_tier}; "
                      "use --aspect-tier to compare)")
            elif aspect_slots(level) > len(build.aspects):
                print("  No aspect adds to your goal with this tree.")
            pool = data.aspects.get(CLASS_TITLE[cls], [])
            modelled = sum(1 for a in pool if any(t.get("abilities") for t in a.get("tiers", [])))
            print(f"  Note: {modelled} of {len(pool)} {CLASS_TITLE[cls]} aspects have effects in the data. The rest "
                  "(and area, range or cooldown changes) can't be scored, so judge those by their description.")

    if data.enc and data.version in data.version_names:
        try:
            auto = [final["totals"][i] - extra[i] for i in range(5)]
            url = encode_build_link(data, tree, data.enc, data.version_names.index(data.version), level, armor,
                                    weapon, build.tomes, build.aspects, chosen, auto, final["totals"])
            print_header("Open in Wynnbuilder")
            print("  " + url)
            print("  (Your gear, powders, tomes, aspects, skill points and this tree. Assumed weapon or powders are "
                  "included as shown above.)")
        except Exception as exc:
            print(f"  Couldn't make a Wynnbuilder link ({exc}).")

    print()
    print(f"Done in {time.time() - t0:.1f}s.")
    print_assumptions(args)


def print_assumptions(args):
    roll_text = {"max": "perfect (max) item rolls, like Wynnbuilder", "avg": "average item rolls",
                 "min": "worst item rolls", "base": "base item values"}[ROLL_MODE]
    lines = [
        "How it's counted: Wynnbuilder's damage formulas, average hits including crits from Dexterity, "
        f"{roll_text}.",
        f"Assumed timing: summons/sigils active {EXTRA_DPS_UPTIME:.0%} of the time, ultimates {ULT_DPS_UPTIME:.0%} "
        f"uptime (or once every {ULT_HIT_EVERY:.0f}s), {CLICKS_PER_SECOND} clicks/s. Main attack DPS = damage per hit x "
        "attacks per second, like Wynnbuilder. Situational proc hits count only in the burst combo.",
        f"Weapon specials fire about every {SPECIAL_CHARGE_SECONDS + 1:.0f}s on melee builds and "
        f"{3 * SPECIAL_CHARGE_SECONDS:.0f}s otherwise; their damage boosts assume a single-target fight (bosses).",
        ("Poison uses Wynnbuilder's in-game-tested figures (--poison low for a cautious estimate); "
         if POISON_MODE == "wynnbuilder" else "Poison counted cautiously (listed value / 3 per second); ")
        + "life steal counts as healing while you keep hitting. "
        "Effective HP is for any element unless you enter an enemy hit with --enemy.",
    ]
    print()
    for line in lines:
        print(line)


def ask(prompt, default=None, choices=None):
    while True:
        suffix = f" [{default}]" if default is not None else ""
        val = input(f"{prompt}{suffix}: ").strip()
        if not val and default is not None:
            val = str(default)
        if choices and val.lower() not in choices:
            print("  Choose one of: " + ", ".join(choices))
            continue
        if val:
            return val


def ask_more_options(args):
    """Optional questions for everything else the command line can set."""
    more = input("Set more options (powders, tomes, aspects, buffs, enemy, rolls)? [y/N]: ").strip().lower()
    if more not in ("y", "yes"):
        return
    print("Press Enter to skip any question.")
    v = input('  Powders you have, e.g. "weapon=t6 t6; chestplate=w4 w4" (leave weapon out to get a recommendation): ').strip()
    if v:
        args.powders = v
    v = input("  Highest powder tier you can get [7]: ").strip()
    if v.isdigit():
        args.max_powder_tier = max(1, min(7, int(v)))
    v = input("  Tomes you have, comma-separated: ").strip()
    if v:
        args.tomes = v
    v = input('  Aspects you have with tier, e.g. "Comet 2, Ray of Frost 1": ').strip()
    if v:
        args.aspects = v
    v = input('  Ability sliders (your usual stacks), e.g. "Distortion=40": ').strip()
    if v:
        args.sliders = v
    v = input("  Party buffs (" + ", ".join(BUFFS) + "): ").strip()
    if v:
        args.buffs = v
    v = input('  Raid buffs, e.g. "Lightbearer-II": ').strip()
    if v:
        args.raid_buffs = v
    v = input('  An enemy hit to survive, e.g. "fire 3000": ').strip()
    if v:
        args.enemy = v
    v = input("  Item rolls: max (like Wynnbuilder), avg, min or base [max]: ").strip().lower()
    if v in ROLL_MODES:
        args.rolls = v
    v = input("  Only suggest items that drop from mobs/chests/dungeons? [y/N]: ").strip().lower()
    if v in ("y", "yes"):
        args.drops_only = True


def main(argv=None):
    p = make_parser()
    args = p.parse_args(argv)
    _main_with_args(args)


def make_parser():
    p = argparse.ArgumentParser(description="Wynncraft build helper: ability tree, skill points, gear and a level plan.")
    p.add_argument("--class", dest="cls", choices=CLASSES)
    p.add_argument("--level", type=int)
    p.add_argument("--goal", choices=list(GOALS), default=None)
    p.add_argument("--goal2", choices=list(SECOND_GOALS), default=None,
                   help="a second goal to mix in, e.g. survival (with --goal2-weight)")
    p.add_argument("--goal2-weight", type=float, default=30,
                   help="how much the second goal counts, in percent (default 30)")
    p.add_argument("--gear", help='comma-separated item names, e.g. "Haqherphix, Ridge, Phosphene"')
    p.add_argument("--only-items", dest="only_items", default=None,
                   help='build only from these items (the ones you have), e.g. "Ajax, Horseshoe x2, Alarm"; '
                        'what you wear (--gear) counts too')
    p.add_argument("--keep", help='abilities you want no matter what, e.g. "Arcane Transfer"')
    p.add_argument("--rank-ap", type=int, default=0, help="extra ability points from rank (VIP+ 2, HERO 4)")
    p.add_argument("--powders", default="",
                   help='powders you have, e.g. "weapon=t6 t6; chestplate=w4 w4". Weapon left out = recommend best')
    p.add_argument("--max-powder-tier", type=int, default=7, help="highest powder tier you can get (default 7)")
    p.add_argument("--tomes", default="", help='tomes you have, comma-separated (by name)')
    p.add_argument("--archetype", default="",
                   help='the archetype you want to play mainly, e.g. "Sharpshooter": the tree keeps more of its abilities '
                        'than of any other archetype (other abilities only where they help)')
    p.add_argument("--avoid-archetype", default="",
                   help='archetypes you don\'t want to play, e.g. "Battle Monk" (comma-separated): none of their abilities')
    p.add_argument("--mythics", action="store_true", default=False,
                   help="allow mythic items (and mythic tomes) in the search and suggestions (off by default)")
    p.add_argument("--no-auto-tomes", dest="auto_tomes", action="store_false", default=True,
                   help="don't pick tomes for the build (from level 55 the best weapon and armor tomes are added)")
    p.add_argument("--aspects", default="", help='aspects you have with tier, e.g. "Comet 2, Ray of Frost 1"')
    p.add_argument("--aspect-tier", type=int, default=1, help="tier to assume for recommended aspects (default 1)")
    p.add_argument("--toggles", default="", help='ability toggles to turn on, e.g. "Activate Gravitational Collapse"')
    p.add_argument("--toggles-off", default="", help='toggles that are on by default to turn off, e.g. "Activate Bullwhip"')
    p.add_argument("--no-auto-toggles", action="store_true",
                   help="don't assume any toggle is on (Wynnbuilder's default); see AUTO_TOGGLES for the ones that are")
    p.add_argument("--sliders", default="", help='stack sliders, e.g. "Distortion=40, Corrupted=60"')
    p.add_argument("--slider-mode", choices=["none", "half", "max"], default="none",
                   help="sliders you don't set: none = Wynnbuilder's default (usually 0), half = half of max, max")
    p.add_argument("--buffs", default="", help="party buffs: " + ", ".join(BUFFS))
    p.add_argument("--cps", type=float, default=9, help="clicks per second you manage (spell casting speed, default 9)")
    p.add_argument("--from-link", default=None, help="load your build from a Wynnbuilder link (class, level, gear, "
                                                     "powders, tomes, aspects, tree)")
    p.add_argument("--raid-buffs", default="", help='raid buffs, e.g. "Lightbearer-II, Seraphim-I"')
    p.add_argument("--rolls", choices=ROLL_MODES, default="max",
                   help="item stat rolls: max (perfect, like Wynnbuilder), avg (typical drop), min, base")
    p.add_argument("--focus", default="", help='for --goal ability: the ability to maximise, e.g. "Puppet", "Meteor", "Bash"')
    p.add_argument("--enemy", default="", help='an enemy hit to measure survival against, e.g. "fire 3000" or '
                                               '"fire=2000, earth=1000" (elemental defenses then count)')
    p.add_argument("--min", default="", help='minimum stats the build must keep, e.g. "hp=20000, walk speed=0, '
                                             'fire def=100" (health, ehp, walk speed, mana regen, health regen, '
                                             'earth/thunder/water/fire/air defense)')
    p.add_argument("--min-hp", type=float, default=None, help="minimum health")
    p.add_argument("--min-ehp", type=float, default=None, help="minimum effective HP (against --enemy if given)")
    p.add_argument("--min-walk-speed", type=float, default=None, help="minimum walk speed bonus in %%, e.g. 0")
    p.add_argument("--min-mana-regen", type=float, default=None, help="minimum mana regen")
    p.add_argument("--poison", choices=["wynnbuilder", "low"], default="wynnbuilder",
                   help="poison damage: wynnbuilder (its in-game-tested figures, default) or low (value/3 per second)")
    p.add_argument("--drops-only", action="store_true",
                   help="only suggest items that drop from mobs, chests or dungeons")
    p.add_argument("--no-whole-build", action="store_true",
                   help="skip choosing all gear together (faster)")
    p.add_argument("--deep", action="store_true", default=True,
                   help="deep search for the best whole build (the default)")
    p.add_argument("--crafted", dest="no_crafted", action="store_false",
                   help="also design crafted items for your build (off by default)")
    p.add_argument("--no-crafted", dest="no_crafted", action="store_true", default=True,
                   help="leave crafted items out of the whole-build search (the default)")
    p.add_argument("--quick", dest="deep", action="store_false",
                   help="faster, normal search instead of deep search (about a minute)")
    p.add_argument("--time-limit", type=float, default=None,
                   help="stop the whole-build search after this many seconds and keep the best so far")
    p.add_argument("--item-levels", type=int, default=None,
                   help="only use items within this many levels below yours (default: every item up to your level)")
    p.add_argument("--plan-levels", type=int, default=12, help="how many levels ahead to plan")
    p.add_argument("--per-slot", type=int, default=3, help="gear suggestions per slot")
    p.add_argument("--beam", type=int, default=60, help="search width (higher = slower, more thorough)")
    p.add_argument("--offline", action="store_true", help="use saved data only")
    p.add_argument("--no-official", action="store_true", help="skip the official API cross-check")
    p.add_argument("--no-plan", action="store_true")
    p.add_argument("--no-gear", action="store_true")
    return p


def _main_with_args(args):

    if args.from_link:
        args.goal = args.goal or "spells"
        args.keep = args.keep or ""
        run(args)
        return
    if args.cls is None or args.level is None:
        print("Wynn Build Helper — press Enter to accept the [default].")
        args.cls = args.cls or ask("Class (" + "/".join(CLASSES) + ")", "mage", CLASSES).lower()
        args.level = args.level or int(ask("Combat level", 42))
        if args.goal is None:
            print("Goals: " + "; ".join(f"{k} = {v}" for k, v in GOALS.items()))
            args.goal = ask("Goal", "spells", list(GOALS)).lower()
            if args.goal == "ability" and not getattr(args, "focus", ""):
                args.focus = ask("Which ability (e.g. Puppet, Meteor, Bash)")
        link_in = input("Wynnbuilder link of your build (Enter to skip): ").strip()
        if link_in:
            args.from_link = link_in
            args.goal = args.goal or "spells"
            args.keep = args.keep or ""
            run(args)
            return
        if args.gear is None:
            args.gear = input("Your gear, comma-separated; crafted items as CR-... codes (Enter to skip): ").strip()
        if args.keep is None:
            args.keep = input("Abilities you must keep, comma-separated (Enter to skip): ").strip()
        if not getattr(args, "min", ""):
            v = input('Any minimum stats the build must keep? e.g. "hp=20000, walk speed=0" (Enter for none): ').strip()
            if v:
                args.min = v
        ask_more_options(args)
    args.goal = args.goal or "spells"
    if not 1 <= args.level <= 121:
        sys.exit("Level must be between 1 and 121.")
    run(args)


if __name__ == "__main__":
    main()
