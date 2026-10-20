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
BASE_MANA_PER_SECOND = 5       # Wynnbuilder: mana/s = 5 + mr/5 + ms/3
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
    def __init__(self, raw: dict):
        self.raw = raw
        self.name = raw.get("displayName") or raw["name"]
        self.type = raw.get("type")
        self.category = raw.get("category")
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
        for k, v in raw.items():
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


def build_ingredient_table(ings: list) -> dict:
    """Ingredient id -> ingredient, including Wynnbuilder's 'No Ingredient' and the 35 powder ingredients."""
    zero_pos = {"left": 0, "right": 0, "above": 0, "under": 0, "touching": 0, "notTouching": 0}
    table = {4000: {"name": "No Ingredient", "id": 4000, "ids": {}, "posMods": dict(zero_pos),
                    "itemIDs": {"dura": 0, "strReq": 0, "dexReq": 0, "intReq": 0, "defReq": 0, "agiReq": 0}}}
    for e in range(5):
        for t in range(7):
            dura, req = POWDER_ING_INFO[t]
            item_ids = {"dura": dura, "strReq": 0, "dexReq": 0, "intReq": 0, "defReq": 0, "agiReq": 0}
            item_ids[SKILLS[e] + "Req"] = req
            pid = 7 * e + t
            table[4001 + pid] = {"name": f"{POWDER_ELEM_NAMES[e]} Powder {ROMAN_NUM[t]}", "id": 4001 + pid,
                                 "ids": {}, "posMods": dict(zero_pos), "itemIDs": item_ids,
                                 "isPowder": True, "pid": pid}
    for ing in ings:
        table[int(ing["id"])] = ing
    return table


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def make_crafted(data, recipe_id: int, mat_tiers, ing_ids, atk_spd: str = "SLOW"):
    """A crafted item with exactly the stats Wynnbuilder's crafter gives it (best rolls, like normal items)."""
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
    # Ingredient effectiveness grid (3 rows x 2 columns).
    eff = [[100, 100], [100, 100], [100, 100]]
    for n, ing in enumerate(ings):
        i, j = n // 2, n % 2
        for key, value in (ing.get("posMods") or {}).items():
            if not value:
                continue
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
    flat = [eff[0][0], eff[0][1], eff[1][0], eff[1][1], eff[2][0], eff[2][1]]
    reqs = defaultdict(int)
    min_r, max_r = defaultdict(float), defaultdict(float)
    for n, ing in enumerate(ings):
        mult = float(f"{flat[n] / 100:.2f}")        # Wynnbuilder: (eff/100).toFixed(2)
        for key, value in (ing.get("itemIDs") or {}).items():
            if key == "dura":
                continue
            reqs[key] += _js_round(value) if ing.get("isPowder") else _js_round(value * mult + 1e-9)
        for key, rolls in (ing.get("ids") or {}).items():
            if key not in ROLLED_IDS and key not in SKILLS:
                continue
            mx = rolls.get("maximum") if isinstance(rolls, dict) else None
            if not mx:
                continue
            lo, hi = sorted([math.floor(rolls.get("minimum", 0) * mult), math.floor(mx * mult)])
            min_r[key] += lo
            max_r[key] += hi
    for s in SKILLS:
        raw[s + "Req"] = reqs.get(s + "Req", 0)
        raw[s] = int(max_r.get(s, 0))
    it = Item(raw)
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
    return it


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


def _sp_sig(it):
    """A small number standing for an item's skill point signature (requirements, bonuses, crafted), worked
    out once per item. The solver reads nothing else, so items with equal signatures share answers."""
    s = it.__dict__.get("_spsig") if hasattr(it, "__dict__") else None
    if s is None:
        sig = (tuple(it.reqs), tuple(it.bonus), bool(getattr(it, "crafted", False)))
        s = _SIG_IDS.get(sig)
        if s is None:
            s = _SIG_IDS[sig] = len(_SIG_IDS)
        try:
            it._spsig = s
        except Exception:
            pass
    return s


def _sp_key(armor, weapon):
    """Cache key for the skill points a gear set needs (items in order, weapon)."""
    return (tuple(_sp_sig(a) for a in armor if a is not None), _sp_sig(weapon) if weapon is not None else None)


def solve_skill_points(armor: list, weapon):
    """Cached wrapper: the same gear always needs the same points, and searches ask for it many times."""
    arm = [a for a in armor if a is not None]
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
    armor = [a for a in armor if a is not None]
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
    Returns (assigned[5], total_after_all_items[5], equip_order) for the cheapest order.
    """
    armor = [a for a in armor if a is not None]
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
    def spells(self, owned, extras=(), toggles=frozenset(), sliders=None, slider_mode="none"):
        """
        Merge the owned abilities (Wynnbuilder's atree_merge, atree_scaling and collect_spells). Cached.
        extras: ((key, ability_dict), ...) from aspects and major IDs, merged after the tree.
        toggles: names of ability toggles that are on. sliders: {slider name: value} you set;
        slider_mode decides the rest: "none" = Wynnbuilder's default, "half" = half of max, "max".
        Returns (spells, stats, damage multipliers, deferred stat-input scalings, interactives).
        """
        sliders = sliders or {}
        key = (frozenset(owned), tuple(k for k, _ in extras), frozenset(toggles),
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
                        if eff.get("toggle") and eff["toggle"] not in toggles:
                            continue
                        for b in eff.get("bonuses", []):
                            apply(b, translate(b.get("value", 0)), phase)
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
                level: int = 1) -> dict:
    stats = defaultdict(float)
    stats["hp"] = level_base_hp(level)
    stats["agiDef"] = 90
    worn = [a for a in armor if a is not None] + ([weapon] if weapon else []) + list(tomes)
    for it in worn:
        for k, v in it.stats.items():
            stats[k] += v
    for k, v in set_bonuses(armor, weapon, data).items():
        if k not in SKILLS:
            stats[k] += v
    for k, v in tree_stats.items():
        stats[k] += v
    for i, s in enumerate(SKILLS):
        stats[s] = skill_totals[i]
    return stats


def spell_part_damage(stats, weapon, conversions, use_spell, ignore_speed, ignore_str, dmg_mult,
                      part_filter=None, ignored_mults=()):
    """Port of Wynnbuilder's calculateSpellDamage. Returns (normal_avg, crit_avg)."""
    wdam = weapon.damage
    conversions = list(conversions)
    for i, p in enumerate(ELEM_PREFIX):
        if part_filter is not None:
            conversions[i] += stats.get(f"{p}ConvBase:{part_filter}", 0)
        conversions[i] += stats.get(f"{p}ConvBase", 0)
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
    for i, p in enumerate(ELEM_PREFIX):
        if present[i]:
            damages[i][0] += stats.get(p + "DamAddMin", 0)
            damages[i][1] += stats.get(p + "DamAddMax", 0)
    tag = "Sd" if use_spell else "Md"
    skill_boost = [0.0] + [skill_pct(stats.get(s, 0)) * SKILL_DAMAGE_MULT[i] for i, s in enumerate(SKILLS)]
    static_boost = (stats.get(tag.lower() + "Pct", 0) + stats.get("damPct", 0)) / 100
    total_min = total_max = 0.0
    saved = []
    for i, p in enumerate(ELEM_PREFIX):
        saved.append(damages[i][:])
        total_min += damages[i][0]
        total_max += damages[i][1]
        boost = 1 + skill_boost[i] + static_boost + (stats.get(p + tag + "Pct", 0) + stats.get(p + "DamPct", 0)) / 100
        if i > 0:
            boost += (stats.get("r" + tag + "Pct", 0) + stats.get("rDamPct", 0)) / 100
        damages[i][0] *= boost
        damages[i][1] *= boost
    elem_min = total_min - saved[0][0]
    elem_max = total_max - saved[0][1]
    prop_raw = stats.get(tag.lower() + "Raw", 0) + stats.get("damRaw", 0)
    rainbow_raw = stats.get("r" + tag + "Raw", 0) + stats.get("rDamRaw", 0)
    for i, p in enumerate(ELEM_PREFIX):
        s = saved[i]
        raw = (stats.get(p + tag + "Raw", 0) + stats.get(p + "DamRaw", 0)) if present[i] else 0
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
    for k, v in dmg_mult.items():
        if ":" in k and k.split(":", 1)[1] != part_filter:
            continue                      # multiplier for one specific spell part
        if k in ignored_mults:
            continue
        if ";" in k:                      # element-specific (or 'm' = main attack only)
            tag = k.split(";", 1)[1]
            if tag == "m" and not use_spell:
                mult *= 1 + v / 100
            elif tag in ELEM_PREFIX:
                elem_mult[ELEM_PREFIX.index(tag)] *= 1 + v / 100
        else:
            mult *= 1 + v / 100
    crit_mult = 0 if ignore_str else 1 + stats.get("critDamPct", 0) / 100
    norm = crit = 0.0
    for i, (lo, hi) in enumerate(damages):
        lo, hi = lo * elem_mult[i], hi * elem_mult[i]
        lo, hi = max(lo, 0), max(hi, 0)
        norm += (lo + hi) / 2 * str_boost * mult
        crit += (lo + hi) / 2 * (str_boost + crit_mult) * mult
    return norm, crit


def spell_cost(stats, spell) -> float:
    bs = spell.get("base_spell")
    cost = spell.get("cost", 0) * (1 - skill_pct(stats.get("int", 0)) * INT_COST_MULT)
    cost += stats.get(f"spRaw{bs}", 0)
    cost *= 1 + stats.get(f"spPct{bs}", 0) / 100
    return max(1.0, cost)


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
    for e in POWDER_ELEMS:
        raw = stats.get(e + "Def", 0)
        pct = (stats.get(e + "DefPct", 0) + stats.get("rDefPct", 0)) / 100
        elem_def.append(raw + raw * pct if raw > 0 else (raw - raw * pct if raw < 0 else 0.0))
    # Wynnbuilder's display lets a negative raw defense with a big % bonus turn positive; its own rawToPct
    # (and the game) stops negative defense at 0, so the capped value is used for real hits.
    elem_eff = []
    for e in POWDER_ELEMS:
        raw = stats.get(e + "Def", 0)
        pct = (stats.get(e + "DefPct", 0) + stats.get("rDefPct", 0)) / 100
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


def evaluate_spells(spells, stats, weapon, dmg_mult) -> dict:
    """Average expected damage of each spell's headline number (crit chance from Dexterity)."""
    crit_chance = skill_pct(stats.get("dex", 0))
    heal_mult = 1.0
    for k, v in stats.items():
        if isinstance(k, str) and k.startswith("healMult."):
            heal_mult *= 1 + v / 100
    heal_mult *= 1 + stats.get("healPct", 0) / 100
    heal_base = max(5.0, stats.get("hp", 0) + stats.get("hpBonus", 0)) * heal_mult
    out = {}
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
                                         ignored_mults=tuple(part.get("ignored_mults") or ()))
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


def _is_dps_label(label) -> bool:
    s = str(label or "").lower()
    return "dps" in s or "per second" in s


# Assumptions for damage that isn't a normal cast (printed in the report).
EXTRA_DPS_UPTIME = 0.5        # summons, sigils, totems: active about half the time
EXTRA_TRIGGER_EVERY = 3.0     # one-off triggered hits (traps, shatters): about every 3 s
ULT_DPS_UPTIME = 0.25         # damage-over-time ultimates: active about a quarter of the time
ULT_HIT_EVERY = 60.0          # one-shot ultimates: about once a minute
SURVIVAL_HEAL_SECONDS = 10.0  # survival goal: effective HP plus 10 s of healing and two regen ticks


GOALS = {
    "spells": "Highest damage you can keep up with spells (mana-limited spell rotation)",
    "burst": "Biggest one-time combo (every damage spell once)",
    "melee": "Highest DPS exactly as Wynnbuilder shows it (Average DPS: main attack damage per hit x attack speed)",
    "survival": "Hardest to kill (effective HP, plus regen and self-healing)",
    "balanced": "Best mix of damage and effective HP",
    "healing": "Most healing per second (support)",
    "ability": "Highest damage of one ability you choose (--focus), e.g. puppets, Meteor or Bash",
}


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


def score_build(results: dict, stats: dict, weapon, goal: str, ult_spells=frozenset(), special=None,
                defense=None, focus_bs=None) -> dict:
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
    income = BASE_MANA_PER_SECOND + stats.get("mr", 0) / 5 + max(0, stats.get("ms", 0)) / 3
    # Best rotation of 2-4 different spells: casts/s limited by clicks and by mana.
    best_rot, best_rot_dps = None, 0.0
    usable = [bs for bs, r in results.items() if bs in (1, 2, 3, 4)]
    for size in (2, 3, 4):
        for combo in itertools.combinations(usable, size):
            rs = [results[b] for b in combo]
            dmg = sum(r["cast"] for r in rs) / size
            if dmg <= 0:
                continue
            net_cost = sum(r["cost"] - r["mana_gained"] for r in rs) / size
            cast_rate = CLICKS_PER_SECOND / 3
            if net_cost > 0:
                cast_rate = min(cast_rate, income / net_cost)
            dps = cast_rate * dmg
            if dps > best_rot_dps:
                best_rot_dps, best_rot = dps, combo
    # Healing: best mana-limited rotation of healing spells (heal per second).
    heal_ps, heal_rot = 0.0, None
    healers = [bs for bs, r in results.items() if bs in (1, 2, 3, 4) and r.get("heal", 0) > 0]
    for size in (1, 2, 3):
        for combo in itertools.combinations(usable, size):
            if not any(b in healers for b in combo):
                continue
            rs = [results[b] for b in combo]
            heal = sum(r.get("heal", 0) for r in rs) / size
            net_cost = sum(r["cost"] - r["mana_gained"] for r in rs) / size
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
    best_damage = max(spell_goal, melee_total + special_dps)
    self_heal = SURVIVAL_HEAL_SECONDS * (heal_ps + life_steal_ps) + 2 * max(defense.get("hpr", 0), 0)
    focus_val = results.get(focus_bs, {}).get("damage", 0.0) if focus_bs is not None else 0.0
    main = {"ability": focus_val, "spells": spell_goal, "burst": burst, "melee": melee_goal,
            "survival": ehp + (ehp / max(defense.get("hp", 1), 1)) * self_heal,
            "balanced": math.sqrt(max(best_damage, 0) * max(ehp, 0)),
            "healing": heal_ps}[goal]
    # Small tie-breakers so spare points still go somewhere useful (other damage, then mana bank size).
    bank = sum(r["mana_gained"] for r in results.values())
    value = main * 1.0 + 0.002 * (best_rot_dps + burst / 5 + melee_dps) + 0.0005 * triggered_extra + 0.0001 * bank
    return {"value": value, "main": main, "spell_dps": best_rot_dps, "rotation": best_rot, "burst": burst,
            "melee_hit": melee, "melee_dps": melee_dps, "extra_dps": extra_dps, "mana_income": income,
            "special": special, "defense": defense, "heal_ps": heal_ps, "heal_rotation": heal_rot,
            "poison_spell": poison_spell, "poison_melee": poison_melee, "life_steal_ps": life_steal_ps,
            "triggered_dps": triggered_extra, "triggered_names": triggered_names, "spell_goal": spell_goal,
            "melee_goal": melee_goal, "dps_style": "spells" if spell_goal >= melee_goal else "main attack",
            "focus_value": focus_val}


# ============================================================================== toggles
# Wynnbuilder leaves every ability toggle off unless you turn it on, so an ability whose bonus sits behind a
# toggle adds nothing and the tree search never picks it. These toggles are switched on by default because
# their condition is part of a normal rotation for the build that takes the ability (the report says so, and
# --toggles-off or --no-auto-toggles turns them off). Toggles whose condition is hard to keep up (next hit
# after 4s, hits from behind, ultimates) stay off; the report shows what each would add.
AUTO_TOGGLES = {
    "Activate Bullwhip": "you keep enemies Whipped by hitting them with Uproot or Haunting Memory at least every 10s",
}


def auto_toggles_for(tree) -> list:
    names = {e.get("toggle") for n in tree.nodes for e in n.raw.get("effects", []) if e.get("toggle")}
    return [t for t in AUTO_TOGGLES if t in names]


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
                             "walk speed, mana regen, health regen, earth/thunder/water/fire/air defense")
        out[key] = float(m.group(2).replace(",", ""))
    return out


def floor_values(stats, defense) -> dict:
    return {k: fn(stats, defense) for k, (_l, _u, fn) in FLOOR_STATS.items()}


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

EVAL_STATS = {"n": 0, "tick": None}     # how many builds were scored; tick() is called every 256


class Build:
    """Everything needed to score one combination of tree + gear + skill points."""

    def __init__(self, data: GameData, tree: Tree, level: int, goal: str, tomes=(), aspects=(),
                 toggles=frozenset(), sliders=None, slider_mode="none", buffs=(), raid_buffs=(), enemy=None,
                 focus=None, floors=None):
        self.data, self.tree, self.level, self.goal = data, tree, level, goal
        self.floors = clean_floors(floors)  # minimum stats every build must meet, e.g. {"hp": 20000, "spd": 0}
        self.focus = focus                 # (spell slot, name, node id) for the "ability" goal
        self.enemy = dict(enemy or {})     # element index -> damage per enemy hit (--enemy)
        self.tomes = list(tomes)          # Item objects
        self.aspects = list(aspects)      # (aspect dict, tier 1-4)
        self.toggles = frozenset(toggles)
        self.sliders = dict(sliders or {})
        self.slider_mode = slider_mode
        self.buffs = tuple(buffs)          # party/potion buffs, names from BUFFS
        self.raid_buffs = tuple(raid_buffs)

    def copy_with(self, **kw):
        fields = dict(level=self.level, goal=self.goal, tomes=self.tomes, aspects=self.aspects,
                      toggles=self.toggles, sliders=self.sliders, slider_mode=self.slider_mode,
                      buffs=self.buffs, raid_buffs=self.raid_buffs, enemy=self.enemy, focus=self.focus,
                      floors=self.floors)
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
                for j, ab in enumerate(self.data.majids[mid].get("abilities", [])):
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

    def _evaluate(self, owned, armor, weapon, extra_points=None, fast=False):
        assigned, totals, order = solve_skill_points(armor, weapon)
        extra = extra_points or [0] * 5
        sb = set_bonuses(armor, weapon, self.data)
        tome_sp = [sum(t.bonus[i] for t in self.tomes) for i in range(5)]
        rb_stats, rb_majids = raid_buff_stats(self.raid_buffs)
        # Set, tome and raid-buff skill points add to totals but don't help meet requirements (Wynnbuilder).
        totals = [totals[i] + extra[i] + int(sb.get(SKILLS[i], 0)) + tome_sp[i] + int(rb_stats.get(SKILLS[i], 0))
                  for i in range(5)]
        spells, tstats, dmult, deferred, _inter = self.tree.spells(
            owned, self.extra_abilities(armor, weapon, rb_majids), self.toggles, self.sliders, self.slider_mode)
        stats = build_stats(armor, weapon, totals, tstats, self.data, self.tomes, level=self.level)
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
        if fast and self.goal == "melee":
            spells = {k: v for k, v in spells.items() if k == 0}
        elif fast and self.goal == "ability" and self.focus:
            spells = {k: v for k, v in spells.items() if k == self.focus[0]}
        res = evaluate_spells(spells, stats, weapon, dmult)
        def_mults = {}
        for k, v in stats.items():
            if isinstance(k, str) and k.startswith("defMult."):
                def_mults[k[8:]] = def_mults.get(k[8:], 0) + v
        for k, v in buff_mults(self.buffs, 1).items():
            def_mults[k] = max(def_mults.get(k, 0), v) if k in NONSTACKING_MULTS else def_mults.get(k, 0) + v
        defense = defense_stats(stats, weapon, def_mults, self.enemy)
        sc = score_build(res, stats, weapon, self.goal, self.tree.ult_spells, special, defense,
                         focus_bs=self.focus[0] if self.focus else None)
        if self.floors:
            vals = floor_values(stats, defense)
            short = floor_shortfall(self.floors, vals)
            sc["floor_values"], sc["floors_met"] = vals, short <= 0
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
        pool = list(by_mix.values())[: beam_width // 3] + scored
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


def aspect_slots(level: int) -> int:
    """Aspect slots unlocked at a combat level (wiki: 1 + slots at 20, 40, 60, 80)."""
    return 1 + sum(level >= t for t in (20, 40, 60, 80))


def recommend_tomes(build: Build, owned, armor, weapon, extra):
    """Greedy best tomes per slot type for the goal. Returns [(slot type, tome, gain fraction)]."""
    base = build.evaluate(owned, armor, weapon, extra)["value"]
    chosen = list(build.tomes)
    picks = []
    for ttype, count in TOME_SLOTS:
        have = sum(1 for t in chosen if t.type == ttype)
        for _ in range(max(0, count - have)):
            best, best_v = None, None
            for t in build.data.tomes:
                if t.type != ttype or t.level > build.level or any(c.name == t.name for c in chosen):
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


def default_weapon(data: GameData, cls: str, level: int, build=None, armor=None):
    """
    Best class weapon you could use at this level (used when you don't name one).
    With a build, the top candidates are scored for your goal; otherwise highest damage wins.
    """
    kind = CLASS_WEAPON[cls]
    avail = skill_points_available(level)
    options = [it for it in data.items.values() if it.type == kind and it.level <= level
               and it.drop != "never" and sum(it.reqs) <= avail]
    if not options:
        options = [it for it in data.items.values() if it.type == kind and it.level <= max(level, 1)] or \
                  sorted((it for it in data.items.values() if it.type == kind), key=lambda it: it.level)[:1]
    options.sort(key=lambda it: (it.raw.get("averageDps") or 0, it.level), reverse=True)
    if build is None:
        return options[0]
    armor = armor or [None] * 8
    roots = frozenset(build.tree.roots)
    pool = options[:40]
    if build.goal in ("survival", "balanced", "healing"):
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


def _gear_pool(build: Build, kind: str, level_window: int, include_never: bool):
    cls, level = build.tree.cls, build.level
    return [it for it in build.data.items.values() if it.type == kind
            and level - level_window <= it.level <= level and (include_never or it.drop != "never")
            and it.class_req in (None, cls)]


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


def _wearable(armor, weapon, level) -> bool:
    """Can this gear be worn at this level (enough skill points, none over the per-skill cap)? Remembered."""
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
                if not include_never and it.drop == "never":
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
    assumed_weapon = False
    if weapon is None:
        weapon = default_weapon(data, cls, level, Build(data, tree, level, goal), armor)
        assumed_weapon = True
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
    toggles = resolve_toggles(tree, [t.strip() for t in (args.toggles or "").split(",") if t.strip()])
    toggles_off = set(resolve_toggles(tree, [t.strip() for t in (getattr(args, "toggles_off", "") or "").split(",")
                                             if t.strip()]))
    auto_on = []
    if not getattr(args, "no_auto_toggles", False):
        for tname in auto_toggles_for(tree):
            if tname not in toggles and tname not in toggles_off:
                toggles.append(tname)
                auto_on.append(tname)
    toggles = [t for t in toggles if t not in toggles_off]
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
                print("  No ability given for the ability goal (use --focus, e.g. \"Puppet\"); using highest DPS instead.")
            goal = "melee"
            args.goal = goal
        else:
            focus = resolve_focus(tree, ftxt)
            if focus is None:
                names = sorted({e.get("name") for n in tree.nodes for e in n.raw.get("effects", [])
                                if e.get("type") == "replace_spell" and e.get("name")})
                sys.exit(f'Couldn\'t find an ability matching "{ftxt}" for {CLASS_TITLE[cls]}. Try one of: ' + ", ".join(names))
            if focus[0] == 0:                       # the main attack: that's the highest-DPS goal
                print("  That's your main attack, so this uses the highest DPS goal (Wynnbuilder's Average DPS).")
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
    build = Build(data, tree, level, goal, tomes=owned_tomes, aspects=owned_aspects, toggles=toggles,
                  sliders=sliders, slider_mode=args.slider_mode, buffs=buffs, raid_buffs=raid_buffs, enemy=enemy,
                  focus=focus, floors=floors)
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
        final = dict(final, floor_values=floor_values(final["stats"], final["defense"]), floors_met=False)

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
        for name in inter["toggles"]:
            note = ""
            if name in auto_on:
                note = f"  (assumed on: {AUTO_TOGGLES[name]}; --toggles-off \"{name}\" if you don't)"
            elif name not in build.toggles and base_v > 0:
                try:     # what turning it on would add, so you can decide
                    on_v = build.copy_with(toggles=build.toggles | {name}).evaluate(chosen, armor, weapon, extra)["value"]
                    if on_v > base_v * 1.001:
                        note = f"  (on: +{(on_v / base_v - 1) * 100:.0f}% for your goal, if you can keep it up)"
                except Exception:
                    pass
            print(f"  Toggle  {name:<32} {'ON' if name in build.toggles else 'off'}{note}")
        print('  Set your typical stacks with --sliders "Name=value" and turn on toggles with --toggles "Name".')
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
            print(f"  {label:<26} {fmt_num(r['damage']):>8} {unit}{cost}{gain}")
    rot = final["rotation"]
    if rot:
        names = " + ".join(final["results"][b]["name"] for b in rot)
        print(f"  Best spell rotation: {names}  ≈ {fmt_num(final['spell_dps'])} damage/s "
              f"(mana income {final['mana_income']:.1f}/s)")
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
    print("  (Mana income counts mana steal from main attacks, as Wynnbuilder does.)")
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
        print(f"  Life steal: {fmt_num(final['life_steal_ps'] * 3)} per 3s while you keep hitting "
              f"(≈ {fmt_num(final['life_steal_ps'])} HP/s)")
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
    if not args.no_gear:
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
        print(f"  {mode}: every item from level {min_lvl} to {level}, built around each top weapon"
              + (f" (stops after {args.time_limit:.0f}s)" if getattr(args, "time_limit", None) else "") + "...")
        if ws.PROGRESS.get("cb") is None:
            ws.PROGRESS["cb"] = lambda f, m: None
        wb = ws.search_best_build(floor_build, chosen, armor, weapon, base_weapon, keep=keep_ids, ap_budget=budget,
                                  include_never=include_never, min_level=min_lvl,
                                  deep=getattr(args, "deep", False), time_limit=getattr(args, "time_limit", None),
                                  log=lambda m: print("  " + m))
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
                print(f"  +{gain * 100:.0f}% for your goal compared with what you wear now. Each piece was picked with the")
                print("  others on, so skill points they give each other, set bonuses, elements, major IDs and the")
                print("  re-planned ability tree all count.")
            for s_i in range(8):
                it = wb["armor"][s_i]
                old_it = armor[s_i]
                mark = "keep" if (it is not None and old_it is not None and it.name == old_it.name) else "new"
                if it is None:
                    continue
                print(f"  {SLOT_LABELS[s_i]:<11} {it.name:<24} ({mark}) {it.short()}")
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
            bs = wb.get("bonus_search") or {}
            if bs.get("found_by") and bs.get("after", 0) > bs.get("before", 0) > 0:
                print(f"  The extra search ({bs['found_by']}) found this build: "
                      f"+{(bs['after'] / bs['before'] - 1) * 100:.1f}% over the main search's best.")
            if data.enc and data.version in data.version_names:
                try:
                    url = encode_build_link(data, tree, data.enc, data.version_names.index(data.version), level,
                                            wb["armor"], w, build.tomes, build.aspects, wb["owned"],
                                            [fin["totals"][i] - wb["extra"][i] for i in range(5)], fin["totals"])
                    print("  Whole build in Wynnbuilder: " + url)
                except Exception:
                    pass
            if any("not a mob drop" in (x.short() if x else "") for x in wb["armor"] + [w]):
                print("  \"Not a mob drop\" items come from raids, quests, merchants or events, or are no longer obtainable;")
                print("  use --drops-only (or the drops option in the app) to build only from mob drops.")

    # ---- tomes and aspects
    if data.tomes:
        print_header("Tomes")
        if build.tomes:
            print("  Using: " + ", ".join(t.name for t in build.tomes))
        if level < min(t.level for t in data.tomes):
            print(f"  Tomes start at level {min(t.level for t in data.tomes)}.")
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
    p.add_argument("--gear", help='comma-separated item names, e.g. "Haqherphix, Ridge, Phosphene"')
    p.add_argument("--keep", help='abilities you want no matter what, e.g. "Arcane Transfer"')
    p.add_argument("--rank-ap", type=int, default=0, help="extra ability points from rank (VIP+ 2, HERO 4)")
    p.add_argument("--powders", default="",
                   help='powders you have, e.g. "weapon=t6 t6; chestplate=w4 w4". Weapon left out = recommend best')
    p.add_argument("--max-powder-tier", type=int, default=7, help="highest powder tier you can get (default 7)")
    p.add_argument("--tomes", default="", help='tomes you have, comma-separated (by name)')
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
