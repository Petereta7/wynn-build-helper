#!/usr/bin/env python3
"""
Wynn Build Helper app: a window you can talk to.

    python3 wynn_app.py

Opens the app in its own window (or your browser if the window component isn't installed).
You can describe your character in plain words to Claude, or fill in the build form. Every number
comes from wynn_helper.py (Wynnbuilder's formulas); Claude only reads and explains the results.

Talking to Claude needs an Anthropic API key (Settings in the app). The form works without one.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import math
import os
import re
import zlib
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wynn_helper as wh  # noqa: E402

CONFIG_PATH = os.path.join(HERE, "wynn_app_config.json")
UI_PATH = os.path.join(HERE, "app", "index.html")
API_URL = os.environ.get("WYNN_CLAUDE_URL", "https://api.anthropic.com/v1/messages")
DEFAULT_MODEL = "claude-opus-5-5"
MODELS = {"claude-opus-5-5": "Claude Opus 5.5 (smartest, the default)",
          "claude-sonnet-5-5": "Claude Sonnet 5.5 (cheaper, nearly as good)",
          "claude-haiku-5-5": "Claude Haiku 5.5 (cheapest)"}

import wynn_search as ws  # noqa: E402
import updater  # noqa: E402

RUN_LOCK = threading.Lock()          # the calculator uses some global settings, so one run at a time
STATE = {"data": None, "chat": [], "last_args": None, "last_report": None, "last_ping": time.time(),
         "browser_mode": False, "progress": None}


def _progress(frac, msg):
    STATE["progress"] = {"fraction": frac, "message": msg, "time": time.time(),
                         "started": (STATE.get("progress") or {}).get("started") if frac > 0.01 else time.time()}


ws.PROGRESS["cb"] = _progress


# ============================================================================== settings

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


KEY_FILE = os.path.join(HERE, "api_key.txt")
KEY_FILE_HELP = """# Paste your Claude API key on the line below (it starts with sk-ant-), then save this file.
# Get a key at https://console.anthropic.com/settings/keys
# Keep this file private: anyone with the key can use your API credit.
"""


def read_key_file() -> str:
    try:
        with open(KEY_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return ""


def write_key_file(key: str):
    with open(KEY_FILE, "w", encoding="utf-8") as fh:
        fh.write(KEY_FILE_HELP + (key.strip() + "\n" if key.strip() else ""))


def api_key() -> str:
    """The key from api_key.txt, else an older saved setting, else the ANTHROPIC_API_KEY variable."""
    return (read_key_file() or load_config().get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def ssl_context():
    try:
        import certifi  # optional; fixes certificate errors on some Mac Python installs
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# ============================================================================== calculator bridge

def game_data():
    if STATE["data"] is None:
        with RUN_LOCK:
            if STATE["data"] is None:
                STATE["data"] = wh.load_game_data(offline=True)
    return STATE["data"]


def _as_list_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if str(x).strip())
    return str(v)


def run_report(params: dict) -> dict:
    """Run the full report for these settings; returns {"ok", "text", "args"}."""
    args = wh.make_parser().parse_args([])
    link = (params.get("wynnbuilder_link") or params.get("link") or "").strip()
    char_note, char = "", None
    if params.get("character"):                       # one of the player's saved characters: fill in the gaps
        char, why = find_character(params["character"])
        if char is None:
            return {"ok": False, "text": why}
        params = dict(params)
        params.setdefault("class", char["class"])
        if params.get("level") in (None, ""):
            params["level"] = char.get("level")
        cur = next((b for b in char.get("builds", []) if b.get("id") == char.get("current")), None) or \
            (char.get("builds") or [None])[-1]
        if params.get("use_my_items"):              # a saved build isn't necessarily gear they have
            params.setdefault("use_saved_gear", False)
        if not params.get("gear") and params.get("use_saved_gear", True) is not False and cur:
            params["gear"] = _gear_of(cur)
        char_note = (f"Character: {char['name']} (level {params['level']} {wh.CLASS_TITLE.get(char['class'], char['class'])})"
                     + (f", starting from the gear of its current build" if params.get("gear") and cur and
                        params.get("use_saved_gear", True) is not False else "")
                     + (f", only from its {len(char.get('items') or [])} saved items" if params.get("use_my_items") else "")
                     + ".\n")
    only_txt = _as_list_text(params.get("only_items"))
    if params.get("use_my_items"):                   # "build from my items": the character's saved list
        if char is None:
            return {"ok": False, "text": "Whose items? Pass the character too (use_my_items uses a saved character's items)."}
        if not char.get("items"):
            return {"ok": False, "text": f"{char['name']} has no items saved yet. Ask the player which items they have "
                                         "(they can also add them on the character's page, under My items)."}
        only_txt = ", ".join(([only_txt] if only_txt else []) + list(char["items"]))
    cls = (params.get("class") or params.get("cls") or "").strip().lower() or None
    if cls and cls not in wh.CLASSES:
        return {"ok": False, "text": f'Unknown class "{cls}". Choose one of: {", ".join(wh.CLASSES)}.'}
    level = params.get("level")
    try:
        level = int(level) if level not in (None, "") else None
    except (TypeError, ValueError):
        return {"ok": False, "text": f'Level "{level}" is not a number.'}
    if not link and (cls is None or level is None):
        return {"ok": False, "text": "Need the class and combat level (or a Wynnbuilder link) to work out a build."}
    if level is not None and not 1 <= level <= 121:
        return {"ok": False, "text": "Level must be between 1 and 121."}
    goal = (params.get("goal") or "spells").lower()
    if goal not in wh.GOALS:
        goal = "spells"
    args.cls, args.level, args.goal = cls, level, goal
    args.goal2 = (params.get("second_goal") or params.get("goal2") or "").lower() or None
    if args.goal2 not in wh.SECOND_GOALS:
        args.goal2 = None
    args.goal2_weight = params.get("second_goal_weight", params.get("goal2_weight", 30)) or 30
    args.focus = params.get("focus") or ""
    args.from_link = link or None
    args.gear = _as_list_text(params.get("gear"))
    args.only_items = only_txt or None
    args.keep = _as_list_text(params.get("keep"))
    args.powders = params.get("powders") or ""
    args.tomes = _as_list_text(params.get("tomes"))
    args.auto_tomes = params.get("auto_tomes") is not False      # tomes picked for the build from level 55
    args.mythics = params.get("mythics") is True                  # mythic items only when the player says so
    args.aspects = _as_list_text(params.get("aspects"))
    args.enemy = params.get("enemy") or ""
    args.sliders = _as_list_text(params.get("sliders"))
    args.toggles = _as_list_text(params.get("toggles"))
    args.toggles_off = _as_list_text(params.get("toggles_off"))
    args.buffs = _as_list_text(params.get("buffs"))
    args.raid_buffs = _as_list_text(params.get("raid_buffs"))
    args.drops_only = bool(params.get("drops_only"))
    args.no_crafted = params.get("crafted") is not True       # crafted items only when the player asks for them
    if params.get("rolls") in wh.ROLL_MODES:
        args.rolls = params["rolls"]
    if params.get("poison") in ("wynnbuilder", "low"):
        args.poison = params["poison"]
    if params.get("max_powder_tier"):
        args.max_powder_tier = max(1, min(7, int(params["max_powder_tier"])))
    if params.get("plan_levels"):
        args.plan_levels = max(1, min(30, int(params["plan_levels"])))
    args.no_gear = params.get("include_gear_suggestions") is False
    args.no_plan = params.get("include_level_plan") is False
    args.no_whole_build = params.get("include_whole_build") is False
    args.deep = params.get("deep_search", True) is not False      # deep search unless asked for a quick one
    mins = params.get("minimums") or params.get("floors")
    if isinstance(mins, dict):
        args.min = ", ".join(f"{k}={v}" for k, v in mins.items() if v not in (None, ""))
    elif isinstance(mins, str):
        args.min = mins
    if params.get("item_levels"):
        args.item_levels = max(1, int(params["item_levels"]))
    if params.get("time_limit"):
        args.time_limit = float(params["time_limit"])
    args.no_official = True
    args.offline = STATE.get("data_checked", False)    # new game data is checked once, when the app opens
    buf = io.StringIO()
    STATE["progress"] = {"fraction": 0.0, "message": "Starting", "time": time.time(), "started": time.time()}
    ws.live_reset()
    with RUN_LOCK:
        try:
            with contextlib.redirect_stdout(buf):
                if args.from_link:
                    args.goal = args.goal or "spells"
                wh.run(args)
            ok = True
        except SystemExit as exc:
            buf.write(f"\n{exc}\n")
            ok = False
        except Exception as exc:
            buf.write(f"\nSomething went wrong while calculating: {exc}\n")
            traceback.print_exc()
            ok = False
    STATE["progress"] = None
    text = buf.getvalue()
    used = {k: v for k, v in params.items() if v not in (None, "", [], False)}
    summary = None
    if ok:
        STATE["last_args"], STATE["last_report"] = used, text
        try:
            summary = build_summary()
        except Exception:
            traceback.print_exc()
    if load_config().get("popup", True):
        notify_done(ok, args, text)
    if char is not None:
        text = char_note + text
        if summary:
            summary["character_id"] = char["id"]
    return {"ok": ok, "text": text, "args": used, "summary": summary, "character_id": char["id"] if char else None}


GOAL_SHORT = {"spells": "spell damage", "burst": "burst combo", "melee": "main attack DPS", "survival": "survival",
              "balanced": "damage + survival", "ability": "one ability", "healing": "healing",
              "mixed": "most damage (any style)"}


def notify_done(ok: bool, args, text: str):
    """The little white "build ready" box in the top-left corner of the screen."""
    try:
        import re as _re
        import notify
        if not ok:
            notify.show_popup("Wynn Build Helper", "The build couldn't be worked out. Details are in the app.")
            return
        who = []
        if args.level:
            who.append(f"Level {args.level}")
        if args.cls:
            who.append(wh.CLASS_TITLE.get(args.cls, args.cls))
        line = " ".join(who) or "Your build"
        if args.goal == "ability" and args.focus:
            line += f" · {args.focus}"
        elif args.goal in GOAL_SHORT:
            line += f" · {GOAL_SHORT[args.goal]}"
        m = _re.search(r"\+(\d+)% for your goal compared with (?:what you wear now|wearing nothing)", text)
        if m:
            line += f"\n+{m.group(1)}% over your current gear"
        if "No build in range meets every minimum" in text:
            line += "\nNo build met every minimum; the closest is shown"
        notify.show_popup("Your build is ready", line)
    except Exception:
        pass


STAT_NAMES = {
    "hp": "health", "hpBonus": "health bonus", "hprRaw": "health regen", "hprPct": "health regen %",
    "mr": "mana regen", "ms": "mana steal", "ls": "life steal /3s", "poison": "poison /3s",
    "sdPct": "spell damage %", "sdRaw": "spell damage", "mdPct": "main attack damage %", "mdRaw": "main attack damage",
    "damPct": "damage %", "damRaw": "damage", "rDamPct": "elemental damage %", "rSdPct": "elemental spell damage %",
    "spd": "walk speed %", "atkTier": "attack speed tiers", "critDamPct": "critical damage %", "healPct": "healing %",
    "xpb": "XP bonus %", "lb": "loot bonus %", "ref": "reflection %", "thorns": "thorns %", "expd": "exploding %",
    "spRegen": "soul point regen %", "eSteal": "stealing %", "sprint": "sprint %", "sprintReg": "sprint regen %",
    "jh": "jump height", "maxMana": "max mana", "kb": "knockback %", "rDefPct": "elemental defense %",
}
for _p, _n in zip("etwfan", ["earth", "thunder", "water", "fire", "air", "neutral"]):
    STAT_NAMES.update({_p + "Def": f"{_n} defense", _p + "DefPct": f"{_n} defense %", _p + "DamPct": f"{_n} damage %",
                       _p + "DamRaw": f"{_n} damage", _p + "SdPct": f"{_n} spell damage %", _p + "SdRaw": f"{_n} spell damage",
                       _p + "MdPct": f"{_n} main attack damage %", _p + "MdRaw": f"{_n} main attack damage"})
for _i in range(1, 5):
    STAT_NAMES.update({f"spPct{_i}": f"spell {_i} cost %", f"spRaw{_i}": f"spell {_i} cost"})


def item_summary(name: str) -> str:
    d = game_data()
    it, warn = d.find_item(name)
    if it is None:
        return warn or f'No item called "{name}".'
    lines = []
    if warn:
        lines.append(warn)
    lines.append(f"{it.name}: level {it.level} {it.tier} {it.type}" + (f", {it.class_req} only" if it.class_req else ""))
    if any(it.reqs):
        lines.append("Needs " + ", ".join(f"{wh.SKILL_NAMES[i]} {r}" for i, r in enumerate(it.reqs) if r))
    if any(it.bonus):
        lines.append("Gives " + ", ".join(f"{wh.SKILL_NAMES[i]} {b:+d}" for i, b in enumerate(it.bonus) if b))
    if it.category == "weapon":
        dmg = ", ".join(f"{wh.ELEM_NAMES[i]} {lo:g}-{hi:g}" for i, (lo, hi) in enumerate(it.damage) if hi)
        lines.append(f"Attack speed {str(it.atk_spd).replace('_', ' ').lower()}; damage {dmg}")
    if it.slots:
        lines.append(f"{it.slots} powder slots")
    stats = []
    for k, hi in sorted(it.max_stats.items()):
        lo = it.min_stats.get(k, hi)
        label = STAT_NAMES.get(k, k)
        stats.append(f"{label} {lo:g} to {hi:g}" if lo != hi else f"{label} {hi:g}")
    if stats:
        lines.append("Stats (worst to best roll): " + "; ".join(stats))
    if it.major_ids:
        for m in it.major_ids:
            desc = wh.strip_mc((d.majids.get(m) or {}).get("description", ""))
            lines.append(f"Major ID {m}: {desc} [in the calculator: {wh.MAJID_STATUS_WORDS[wh.majid_status(d, m)]}]")
    if it.set:
        lines.append(f"Part of the {it.set} set")
    notes = it.source_notes()
    if notes:
        lines.append("Note: " + "; ".join(notes))
    return "\n".join(lines)


def ability_list(cls: str) -> str:
    cls = (cls or "").lower()
    if cls not in wh.CLASSES:
        return f"Unknown class. Choose one of: {', '.join(wh.CLASSES)}."
    tree = wh.Tree(game_data(), cls)
    rows = []
    for n in tree.nodes:
        bits = [f"{n.cost} AP"]
        if n.archetype:
            bits.append(n.archetype)
        if n.ultimate:
            bits.append("ultimate, level 90+")
        rows.append(f"{n.name} ({', '.join(bits)})")
    return f"{wh.CLASS_TITLE[cls]} abilities: " + "; ".join(rows)


def autocomplete(q: str, kind: str = "") -> list:
    d = game_data()
    q = q.strip().lower()
    if len(q) < 2:
        return []
    out = []
    for it in d.items.values():
        if q in it.name.lower() and (not kind or it.type == kind or it.category == kind):
            out.append({"name": it.name, "tier": it.tier, "type": it.type, "level": it.level})
    out.sort(key=lambda x: (not x["name"].lower().startswith(q), x["level"]))
    return out[:12]


# ============================================================================== result summary and build history
# The visuals (equipment screen, before/after bars, tree map) are drawn from this, not from the report text.
# History is kept on this computer only (wynn_data/build_history.json); updates never touch it.

HISTORY_PATH = os.path.join(wh.DATA_DIR, "build_history.json")
GOAL_LABELS = {"melee": "Average DPS", "spells": "Spell damage per second", "burst": "Burst combo damage",
               "mixed": "Most damage per second (any style)",
               "survival": "Survival score", "balanced": "Damage × survival score", "healing": "Healing per second"}


def _clean_desc(text: str) -> str:
    import re as _re
    t = _re.sub(r"<\s*/?\s*br\s*/?\s*>", "\n", text or "", flags=_re.I)
    t = "\n".join(wh.strip_mc(line).strip() for line in t.split("\n"))
    t = _re.sub(r"<[^>]+>", "", t).replace("&emsp;", " ").replace("&nbsp;", " ")
    return _re.sub(r"\n{3,}", "\n\n", t).strip()


_SKILL_WORDS = {"str": "Strength", "dex": "Dexterity", "int": "Intelligence", "def": "Defense", "agi": "Agility"}
_POS_WORDS = {"above": "the ingredients above it", "under": "the ingredients below it", "left": "the one on its left",
              "right": "the one on its right", "touching": "the ones touching it", "notTouching": "the ones not touching it"}


def craft_cells(it) -> list:
    """The six grid positions of a crafted item for the crafting table: each ingredient with how strong its
    position makes it (effectiveness), what it adds there (best rolls, like the item), its requirements and
    durability, what it does to its neighbours, and where it drops. Same maths as the crafted item itself."""
    d = game_data()
    _k, _rid, _mats, ids, _atk = it.link_code
    ings = [d.ingredients.get(i) for i in ids]
    if any(g is None for g in ings):
        return []
    preps = [wh._ing_prep(g) for g in ings]
    eff = wh.craft_effectiveness(preps)
    cells = []
    for n, (g, prep) in enumerate(zip(ings, preps)):
        if g["id"] == 4000:
            cells.append(None)
            continue
        mult = float(f"{eff[n] / 100:.2f}")
        pm, item_ids, rolled, is_powder = prep
        stats = []
        for key, mn, mx in rolled:
            lo, hi = sorted([math.floor(mn * mult), math.floor(mx * mult)])
            if hi:
                stats.append([_SKILL_WORDS.get(key) or STAT_NAMES.get(key, key), hi])
        reqs = []
        for key, value in item_ids:
            v = wh._js_round(value) if is_powder else wh._js_round(value * mult + 1e-9)
            if v and key.endswith("Req"):
                reqs.append([_SKILL_WORDS.get(key[:-3], key[:-3]), v])
        cells.append({"name": g.get("name", "?"), "tier": g.get("tier", 0), "level": g.get("lvl", 0), "eff": eff[n],
                      "stats": stats, "reqs": reqs, "dura": (g.get("itemIDs") or {}).get("dura", 0) or 0,
                      "pos": [[k, v, _POS_WORDS.get(k, k)] for k, v in pm], "powder": bool(is_powder),
                      "drops": "" if is_powder else wh.ingredient_sources(d, g.get("name", ""))})
    return cells


def _roll_stats(it, rolls=None) -> dict:
    """An item's stats for these rolls ("max", "avg", "min", "base"; None = the calculator's current setting).
    Doesn't change the calculator's setting, so it's safe while a build is running."""
    if rolls is None or rolls == wh.ROLL_MODE:
        return it.stats
    if rolls == "min":
        return it.min_stats
    if rolls == "base":
        return it.base_stats
    if rolls == "avg":
        return {k: (it.min_stats[k] + it.max_stats[k]) / 2 for k in it.max_stats}
    return it.max_stats


def item_card(it, slot: str, rolls=None):
    if it is None:
        return None
    stats = []
    for k, v in _roll_stats(it, rolls).items():
        if k in wh.SKILLS or not v or k in ("atkTier",):
            continue
        stats.append([STAT_NAMES.get(k, k), round(v, 1) if isinstance(v, float) and v != int(v) else int(v)])
    card = {"slot": slot, "name": it.name, "tier": it.tier, "type": it.type, "level": it.level,
            "reqs": list(it.reqs), "bonus": list(it.bonus), "stats": stats[:22], "slots": it.slots,
            "powders": [p.label() for p in (it.powders or [])], "set": it.set,
            "major_ids": [[m, _clean_desc((game_data().majids.get(m) or {}).get("description", ""))[:240],
                           wh.majid_status(game_data(), m)] for m in (it.major_ids or [])],
            "notes": it.source_notes()}
    lc = getattr(it, "link_code", None)
    if getattr(it, "crafted", False) and lc and lc[0] == "craft":          # how to make it
        rec = game_data().recipes.get(lc[1], {})
        ings = game_data().ingredients
        card["craft"] = {"recipe": rec.get("name", ""), "profession": (rec.get("skill") or "").capitalize(),
                         "mats": list(lc[2]), "grid": ["" if i == 4000 else (ings.get(i) or {}).get("name", "?") for i in lc[3]],
                         "durability": list(getattr(it, "durability", []) or []),
                         "link": wh.CRAFTER_URL + wh.craft_code(it),
                         "level": [(rec.get("lvl") or {}).get("minimum", 0), (rec.get("lvl") or {}).get("maximum", 0)],
                         "atk_spd": str(lc[4]).lower() if card.get("type") in wh.WEAPON_TYPES else "",
                         "cells": craft_cells(it)}
    if getattr(it, "category", "") == "weapon" or slot == "Weapon":
        card["atk_spd"] = str(getattr(it, "atk_spd", "") or "").replace("_", " ").lower()
        card["damage"] = [[wh.ELEM_NAMES[i], round(lo), round(hi)] for i, (lo, hi) in enumerate(it.damage or []) if hi]
    return card


def numbers_of(ev) -> dict:
    d = ev.get("defense", {})
    return {"goal": ev.get("main", 0.0),
            "spell": ev.get("spell_dps", 0) + (ev.get("poison_spell", 0) if ev.get("spell_dps") else 0),
            "melee": ev.get("melee_dps", 0) + (ev.get("poison_melee", 0) if ev.get("melee_dps") else 0),
            "ehp": d.get("ehp_enemy") or d.get("ehp_agi", 0.0), "hp": d.get("hp", 0.0),
            "spd": ev.get("stats", {}).get("spd", 0.0), "mr": ev.get("stats", {}).get("mr", 0.0)}


def tree_static(tree) -> dict:
    nodes = []
    for n in tree.nodes:
        toggles = [e["toggle"] for e in n.raw.get("effects", []) if e.get("toggle")]
        nodes.append({"id": n.id, "name": n.name, "row": n.row, "col": n.col, "cost": n.cost, "arch": n.archetype,
                      "parents": list(n.parents), "ult": bool(n.ultimate), "desc": _clean_desc(n.raw.get("desc", ""))[:700],
                      "toggle": toggles[0] if toggles else None})
    return {"nodes": nodes, "archetypes": list(dict.fromkeys(n.archetype for n in tree.nodes if n.archetype))}


def load_history() -> list:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            h = json.load(fh)
        return h if isinstance(h, list) else []
    except Exception:
        return []


def save_history(h: list):
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(h[-60:], fh)
        os.replace(tmp, HISTORY_PATH)
    except Exception:
        pass


# ============================================================================== character library
# Your characters (name, class, level) and the builds you chose to keep for each. Kept on this computer only
# (wynn_data/library.json); updates never touch it. Builds are copied in, so clearing History doesn't remove them.

LIBRARY_PATH = os.path.join(wh.DATA_DIR, "library.json")
_LIB_LOCK = threading.Lock()


def load_library() -> dict:
    try:
        with open(LIBRARY_PATH, encoding="utf-8") as fh:
            lib = json.load(fh)
        if isinstance(lib, dict) and isinstance(lib.get("characters"), list):
            return lib
    except Exception:
        pass
    return {"characters": []}


def save_library(lib: dict):
    os.makedirs(os.path.dirname(LIBRARY_PATH), exist_ok=True)
    tmp = LIBRARY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(lib, fh)
    os.replace(tmp, LIBRARY_PATH)


def _new_id() -> str:
    return f"{int(time.time() * 1000)}{os.urandom(2).hex()}"


def _clean_name(name, fallback="") -> str:
    name = " ".join(str(name or "").split())[:40]
    return name or fallback


def library_summary(lib: dict) -> dict:
    """Everything the home page needs, without the heavy build details."""
    chars = []
    for c in lib["characters"]:
        builds = [{**{k: b.get(k) for k in ("id", "source", "saved", "date", "level", "label", "focus", "numbers", "goal", "link")},
                   "shared": bool(b.get("shared_from")), "shared_from": (b.get("shared_from") or {}).get("name") or "",
                   "items": [[x["name"], x["tier"]] if x else None for x in b.get("items", [])],
                   "gear": _gear_of(b)} for b in reversed(c.get("builds", []))]
        chars.append({"id": c["id"], "name": c["name"], "class": c["class"], "class_title": wh.CLASS_TITLE.get(c["class"], c["class"]),
                      "level": c.get("level"), "created": c.get("created"), "current": c.get("current"), "builds": builds,
                      "items": list(c.get("items") or [])})
    counts = {cls: sum(1 for c in chars if c["class"] == cls) for cls in wh.CLASSES}
    return {"classes": [{"class": cls, "title": wh.CLASS_TITLE[cls], "count": counts[cls]} for cls in wh.CLASSES],
            "characters": chars}


MAX_ITEMS = 300


def _item_base(x: str) -> str:
    """'Horseshoe x2' -> 'horseshoe' (for matching names in the list)."""
    return re.sub(r"\s+[x\u00d7]\s*\d+$|\s*\(\d+\)$|^\d+\s*[x\u00d7]\s+", "", x.strip()).lower()


def clean_items(items) -> list:
    """A character's item list: names (any spelling; 'x2' for two of a ring) or CR-/CI- codes, one per entry,
    without repeats, at most MAX_ITEMS."""
    if isinstance(items, str):
        items = [items]
    out, seen = [], set()
    for x in items or []:
        for part in re.split(r"[,;\n]", str(x)):
            part = " ".join(part.split())[:200 if part.strip()[:3].upper() in ("CR-", "CI-") else 60]
            k = _item_base(part)
            if not part or k in seen:
                continue
            seen.add(k)
            out.append(part)
    return out[:MAX_ITEMS]


def _gear_of(b) -> list:
    """A saved build's gear as the form and the bot take it (crafted pieces as their CR- codes)."""
    out = []
    for x in b.get("items", []):
        if not x:
            continue
        craft = x.get("craft") or {}
        out.append("CR-" + craft["link"].split("#", 1)[1] if craft.get("link") and "#" in craft["link"] else x["name"])
    return out


def library_action(body: dict) -> dict:
    """Create / rename / delete characters, save a build to one, remove a build, mark one as current."""
    act = body.get("action")
    with _LIB_LOCK:
        lib = load_library()
        chars = lib["characters"]
        find = lambda cid: next((c for c in chars if c["id"] == cid), None)
        if act == "create":
            cls = str(body.get("class") or "").lower()
            if cls not in wh.CLASSES:
                return {"ok": False, "error": "Pick a class."}
            c = {"id": _new_id(), "name": _clean_name(body.get("name"), f"My {wh.CLASS_TITLE[cls]}"), "class": cls,
                 "level": max(1, min(121, int(body.get("level") or 1))), "created": time.strftime("%b %d %Y"),
                 "builds": [], "current": None}
            chars.append(c)
        elif act == "update":
            c = find(body.get("id"))
            if c is None:
                return {"ok": False, "error": "That character isn't there any more."}
            if "name" in body:
                c["name"] = _clean_name(body["name"], c["name"])
            if body.get("level"):
                c["level"] = max(1, min(121, int(body["level"])))
        elif act == "delete":
            lib["characters"] = [c for c in chars if c["id"] != body.get("id")]
            c = None
        elif act == "items":                          # "My items": the items this character has
            c = find(body.get("id"))
            if c is None:
                return {"ok": False, "error": "That character isn't there any more."}
            c["items"] = clean_items(body.get("items"))
        elif act == "save":
            h = next((x for x in load_history() if x.get("id") == body.get("history_id")), None)
            if h is None:
                return {"ok": False, "error": "That build isn't in History any more."}
            c = find(body.get("character_id"))
            if c is None:
                c = {"id": _new_id(), "name": _clean_name(body.get("new_name"), f"My {h.get('class_title', '')}".strip()),
                     "class": h["class"], "level": h.get("level") or 1, "created": time.strftime("%b %d %Y"),
                     "builds": [], "current": None}
                chars.append(c)
            if c["class"] != h["class"]:
                return {"ok": False, "error": f"That build is for a {h.get('class_title')}, and {c['name']} is a "
                                              f"{wh.CLASS_TITLE.get(c['class'], c['class'])}."}
            if not any(b.get("source") == h["id"] for b in c["builds"]):
                b = dict(h, id=_new_id(), source=h["id"], saved=time.strftime("%b %d %Y %H:%M"))
                c["builds"].append(b)
                c["current"] = b["id"]
                c["level"] = max(c.get("level") or 1, h.get("level") or 1)
        elif act == "import":                         # a friend's build from a share code
            entry, err = decode_share(body.get("code"))
            if entry is None:
                return {"ok": False, "error": err}
            c = find(body.get("character_id"))
            if c is None:
                c = {"id": _new_id(), "name": _clean_name(body.get("new_name"), f"My {entry['class_title']}"),
                     "class": entry["class"], "level": entry["level"], "created": time.strftime("%b %d %Y"),
                     "builds": [], "current": None}
                chars.append(c)
            if c["class"] != entry["class"]:
                return {"ok": False, "error": f"That build is for a {entry['class_title']}, and {c['name']} is a "
                                              f"{wh.CLASS_TITLE.get(c['class'], c['class'])}."}
            src = "shared-" + hashlib.sha1(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:16]
            if not any(b.get("source") == src for b in c["builds"]):
                b = dict(entry, id=_new_id(), source=src, saved=time.strftime("%b %d %Y %H:%M"))
                c["builds"].append(b)
                if not c.get("current"):
                    c["current"] = b["id"]
        elif act in ("remove", "current"):
            c = find(body.get("character_id"))
            if c is None:
                return {"ok": False, "error": "That character isn't there any more."}
            if act == "remove":
                c["builds"] = [b for b in c["builds"] if b["id"] != body.get("build_id")]
                if c.get("current") == body.get("build_id"):
                    c["current"] = c["builds"][-1]["id"] if c["builds"] else None
            else:
                if any(b["id"] == body.get("build_id") for b in c["builds"]):
                    c["current"] = body.get("build_id")
        else:
            return {"ok": False, "error": "unknown action"}
        try:
            save_library(lib)
        except OSError as exc:
            return {"ok": False, "error": f"Couldn't save the library ({exc})."}
        out = library_summary(lib)
        out["ok"] = True
        out["character_id"] = c["id"] if c else None
        return out


def find_character(name):
    """(character, "") for a name the player used ("Sparky", "my shaman"), or (None, why not)."""
    chars = load_library()["characters"]
    q = " ".join(str(name or "").lower().split())
    if not chars:
        return None, "The player has no saved characters yet; ask for class and level instead."
    exact = [c for c in chars if c["name"].lower() == q]
    if len(exact) == 1:
        return exact[0], ""
    part = [c for c in chars if q and (q in c["name"].lower() or c["name"].lower() in q)]
    if len(part) == 1:
        return part[0], ""
    by_cls = [c for c in chars if c["class"] in q.split() or wh.CLASS_TITLE.get(c["class"], "").lower() in q]
    if len(by_cls) == 1:
        return by_cls[0], ""
    names = ", ".join(f"{c['name']} ({wh.CLASS_TITLE.get(c['class'], c['class'])} {c.get('level')})" for c in (part or by_cls or chars))
    return None, f'Which character is "{name}"? Saved characters: {names}.'


def characters_context() -> str:
    """The player's characters, for the bot (added to its instructions every turn)."""
    chars = load_library()["characters"]
    if not chars:
        return "\n\nThe player has no saved characters yet."
    lines = []
    for c in chars:
        cur = next((b for b in c.get("builds", []) if b.get("id") == c.get("current")), None) or (c.get("builds") or [None])[-1]
        what = ""
        if cur:
            gear = ", ".join(x if not x.startswith("CR-") else "a crafted piece" for x in _gear_of(cur))
            what = f"; current build for {cur.get('label', cur.get('goal', ''))}: {gear or 'no gear'}"
        items = c.get("items") or []
        mine = ""
        if items:
            shown = [x if not x[:3].upper() in ("CR-", "CI-") else "a crafted piece" for x in items[:60]]
            mine = f"; My items ({len(items)}): " + ", ".join(shown) + (" ..." if len(items) > 60 else "")
        lines.append(f"- {c['name']}: level {c.get('level')} {wh.CLASS_TITLE.get(c['class'], c['class'])}, "
                     f"{len(c.get('builds', []))} saved build(s){what}{mine}")
    return "\n\nThe player's saved characters (their library):\n" + "\n".join(lines)


def manage_characters(inp: dict) -> str:
    """The bot's tool for the library: create a character, update one, or save the last build to one."""
    act = inp.get("action")
    if act == "create":
        r = library_action({"action": "create", "class": inp.get("class"), "name": inp.get("name"), "level": inp.get("level")})
    elif act == "update":
        c, why = find_character(inp.get("name"))
        if c is None:
            return why
        body = {"action": "update", "id": c["id"]}
        if inp.get("new_name"):
            body["name"] = inp["new_name"]
        if inp.get("level"):
            body["level"] = inp["level"]
        r = library_action(body)
    elif act in ("set_items", "add_items", "remove_items"):
        c, why = find_character(inp.get("name"))
        if c is None:
            return why
        new = clean_items(inp.get("items"))
        old = list(c.get("items") or [])
        if act == "add_items":
            new = clean_items(old + new)
        elif act == "remove_items":
            gone = {_item_base(x) for x in new}
            new = [x for x in old if _item_base(x) not in gone]
        r = library_action({"action": "items", "id": c["id"], "items": new})
        if r.get("ok"):
            STATE["library_changed"] = True
            return f"Done. {c['name']} has {len(new)} item(s) saved in My items."
    elif act == "save_last_build":
        if not STATE.get("last_history_id"):
            return "There's no build to save yet."
        c, why = find_character(inp.get("name")) if inp.get("name") else (None, "")
        if c is None and inp.get("name") and not inp.get("create_if_missing"):
            return why
        r = library_action({"action": "save", "history_id": STATE["last_history_id"],
                            "character_id": c["id"] if c else None, "new_name": inp.get("name")})
    else:
        return "Unknown action."
    if not r.get("ok"):
        return r.get("error", "That didn't work.")
    STATE["library_changed"] = True
    c = next((x for x in r["characters"] if x["id"] == r.get("character_id")), None)
    return ("Done. " + (f"{c['name']} is a level {c['level']} {c['class_title']} with {len(c['builds'])} saved build(s)."
                        if c else ""))


# ============================================================================== sharing builds
# A share code is a saved build packed into one line of text ("WBH1-..."), short enough for a Discord message.
# It holds item names (crafted pieces as their crafter codes), powders, the ability tree, skill points, the goal and
# the numbers the sender's app worked out. The receiving app rebuilds every item from its own game data, so the
# code can't carry made-up item stats; anything it can't find is shown as missing instead of guessed.

SHARE_PREFIX = "WBH1-"
_SHARE_CHARS = re.compile(r"[A-Za-z0-9._\-]+")
_NUM_KEYS = ("goal", "spell", "melee", "ehp", "hp", "spd", "mr")
_FLOOR_KEYS = ("hp", "ehp", "spd", "mr", "hpr", "ls", "ms", "net")
_LINK_OK = re.compile(r"https://wynnbuilder\.github\.io/builder/#[A-Za-z0-9_\-+/=.~]*")
SHARE_DAMAGED = "That share code is incomplete or damaged. Ask your friend to copy it again (the whole thing)."


def settings_notes(b) -> list:
    """Settings that change a build's numbers beyond the defaults (party buffs, enemy hits, ...), in plain words.
    Saved with each build so a comparison or a friend can see what the numbers assumed."""
    if b is None:
        return []
    out = []
    if b.buffs:
        out.append("Party buffs: " + ", ".join(map(str, b.buffs)))
    if b.raid_buffs:
        out.append("Raid buffs: " + ", ".join(str(getattr(x, "name", x)) for x in b.raid_buffs))
    if b.enemy:
        out.append("Enemy hits counted: " + ", ".join(
            f"{(wh.ELEM_NAMES[k] if isinstance(k, int) and k < len(wh.ELEM_NAMES) else k)} {v:g}" for k, v in b.enemy.items()))
    if b.tomes:
        out.append("Tomes: " + ", ".join(getattr(t, "name", str(t)) for t in b.tomes))
    if b.aspects:
        out.append("Aspects: " + ", ".join(f"{a.get('displayName', '?')} {t}" for a, t in b.aspects))
    if b.sliders:
        out.append("Ability stacks: " + ", ".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}"
                                                  for k, v in b.sliders.items()))
    if wh.ROLL_MODE != "max":
        out.append("Item rolls: " + {"avg": "average", "min": "worst", "base": "base values"}.get(wh.ROLL_MODE, wh.ROLL_MODE))
    return out


def _powder_text(labels) -> str:
    """["T6 Thunder", "T6 Thunder"] -> "t6 t6" (what parse_powders reads)."""
    out = []
    for p in labels or []:
        m = re.match(r"T(\d) (\w+)$", str(p))
        if m and m.group(2) in wh.POWDER_ELEM_NAMES:
            out.append(wh.POWDER_ELEMS[wh.POWDER_ELEM_NAMES.index(m.group(2))] + m.group(1))
    return " ".join(out)


def _item_ref(card):
    if not card:
        return 0
    craft = card.get("craft") or {}
    name = "CR-" + craft["link"].split("#", 1)[1] if craft.get("link") and "#" in craft["link"] else card["name"]
    pw = _powder_text(card.get("powders"))
    return [name, pw] if pw else name


def share_code(entry: dict, name: str = "") -> str:
    """A saved build (history or library entry) as a share code."""
    sp = entry.get("sp") or {}
    p = {"v": 1, "c": entry["class"], "l": entry.get("level"), "g": entry.get("goal"), "lb": entry.get("label"),
         "f": entry.get("focus"), "fl": entry.get("floors"), "t": entry.get("toggles_on"), "ts": entry.get("toggle_share"),
         "n": {k: round(float(v), 2) for k, v in (entry.get("numbers") or {}).items() if k in _NUM_KEYS},
         "sp": [sp.get(k) for k in ("assigned", "extra", "gear")] if sp.get("assigned") else None,
         "o": entry.get("owned"), "i": [_item_ref(x) for x in entry.get("items", [])], "lk": entry.get("link"),
         "s": entry.get("settings"), "r": entry.get("rolls") if entry.get("rolls") != "max" else None,
         "nm": name or None, "d": entry.get("date"), "av": updater.current_version().get("version"),
         "dv": getattr(game_data(), "version", None)}
    p = {k: v for k, v in p.items() if v not in (None, "", [], {})}
    raw = json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    # base64 with "." instead of "_": underscores turn into italics in Discord messages
    return SHARE_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=").replace("_", ".")


def _unpack(s: str):
    s = s.replace(".", "_")
    d = zlib.decompressobj()
    raw = d.decompress(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)), 200_000)
    if d.unconsumed_tail or not d.eof:
        raise ValueError("incomplete")
    return json.loads(raw.decode("utf-8"))


def read_share_code(text):
    """The packed data in a share code, wherever it is in the text (a pasted message may have words around it or
    line breaks inside it). (data, "") or (None, why not)."""
    text = str(text or "")[:20000]
    i = text.find(SHARE_PREFIX)
    if i < 0:
        return None, "That isn't a build share code (they start with WBH1-)."
    tail = text[i + len(SHARE_PREFIX):]
    m = _SHARE_CHARS.match(tail)
    token = m.group(0) if m else ""
    joined = "".join(_SHARE_CHARS.findall(re.sub(r"\s+", "", tail)[:6000]))
    tries = [token] + [joined[:n] for n in range(len(joined), len(token), -1)][:4000]
    for cand in tries:
        if not cand:
            continue
        try:
            p = _unpack(cand)
        except Exception:
            continue
        if isinstance(p, dict):
            return p, ""
    return None, SHARE_DAMAGED


def _missing_card(slot, name):
    return {"slot": slot, "name": name, "tier": "Normal", "type": "", "level": 0, "reqs": [0] * 5, "bonus": [0] * 5,
            "stats": [], "slots": 0, "powders": [], "set": None, "major_ids": [], "missing": True,
            "notes": ["Not in your game data, so its stats aren't shown. Updating the app usually fixes this."]}


def _short(v, n) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) else ""


def decode_share(text):
    """A share code back into a saved build, every item rebuilt from this app's game data. (build, "") or (None, why)."""
    p, err = read_share_code(text)
    if p is None:
        return None, err
    if p.get("v") != 1:
        return None, "This share code was made by a newer version of the app. Update the app, then open it again."
    cls = p.get("c")
    if cls not in wh.CLASSES:
        return None, SHARE_DAMAGED
    d = game_data()
    try:
        level = max(1, min(121, int(p.get("l") or 1)))
    except (TypeError, ValueError):
        level = 1
    tree = wh.Tree(d, cls)
    ids = {n.id for n in tree.nodes}
    owned = sorted({x for x in (p.get("o") or []) if isinstance(x, int) and x in ids})
    try:
        order = tree.validate(frozenset(owned), level)["order"]
    except Exception:
        order = owned
    refs = p.get("i")
    if not isinstance(refs, list) or len(refs) != 9:
        return None, SHARE_DAMAGED
    rolls = p.get("r") if p.get("r") in wh.ROLL_MODES else "max"
    items, missing = [], []
    for i, ref in enumerate(refs):
        slot = wh.SLOT_LABELS[i] if i < 8 else "Weapon"
        if not ref:
            items.append(None)
            continue
        if isinstance(ref, list) and ref:
            name, pw = _short(ref[0], 400), _short(ref[1] if len(ref) > 1 else "", 80)
        else:
            name, pw = _short(ref, 400), ""
        crafted = name.upper().startswith("CR-")
        it = None
        try:
            it = wh.crafted_from_code(d, name) if crafted else d.items.get(name.lower())
        except Exception:
            it = None
        want = wh.SLOT_TYPES[i] if i < 8 else wh.CLASS_WEAPON[cls]
        if it is None or it.type != want:
            shown = f"Crafted {want}" if crafted else name[:60]
            missing.append(shown)
            items.append(_missing_card(slot, shown))
            continue
        powders = wh.parse_powders(pw)
        if i == 8:
            it = wh.weapon_with_powders(it, powders)
        elif i < 4 and powders:
            it = wh.armor_with_powders(it, powders)
        items.append(item_card(it, slot, rolls))

    def num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
    numbers = {k: num(v) for k, v in (p.get("n") or {}).items() if k in _NUM_KEYS and num(v) is not None}
    numbers = {k: numbers.get(k, 0.0) for k in _NUM_KEYS}
    sp = p.get("sp")
    ok_sp = (isinstance(sp, list) and len(sp) == 3 and all(isinstance(r, list) and len(r) == 5 and
             all(num(x) is not None and -1000 <= x <= 1000 for x in r) for r in sp))
    sp = dict(zip(("assigned", "extra", "gear"), sp)) if ok_sp else {"assigned": [0] * 5, "extra": [0] * 5, "gear": [0] * 5}
    floors = {k: num(v) for k, v in (p.get("fl") or {}).items() if k in _FLOOR_KEYS and num(v) is not None} \
        if isinstance(p.get("fl"), dict) else {}
    toggles = [_short(t, 60) for t in (p.get("t") or [])[:30] if isinstance(t, str)] if isinstance(p.get("t"), list) else []
    share = {_short(k, 60): float(v) for k, v in (p.get("ts") or {}).items()
             if num(v) is not None and 0 < v < 1} if isinstance(p.get("ts"), dict) else {}
    settings = [_short(s, 200) for s in (p.get("s") or [])[:12] if isinstance(s, str)] if isinstance(p.get("s"), list) else []
    link = _short(p.get("lk"), 3000)
    link = link if _LINK_OK.fullmatch(link) else ""
    goal = p.get("g") if p.get("g") in wh.GOALS else None
    data_v = _short(p.get("dv"), 30)
    notes = []
    if data_v and data_v != str(getattr(d, "version", "")):
        notes.append(f"Your friend's app has different game data ({data_v}; yours is {d.version}). Item stats shown "
                     "are from yours; the numbers are from theirs.")
    entry = {"class": cls, "class_title": wh.CLASS_TITLE[cls], "level": level, "goal": goal,
             "label": _short(p.get("lb"), 100) or GOAL_LABELS.get(goal, "Score"), "focus": _short(p.get("f"), 100) or None,
             "floors": floors, "toggles_on": toggles, "toggle_share": share, "numbers": numbers, "items": items, "sp": sp,
             "owned": owned, "order": order, "link": link, "settings": settings, "rolls": rolls,
             "date": _short(p.get("d"), 20),
             "shared_from": {"name": _clean_name(p.get("nm"), ""), "app": _short(p.get("av"), 20), "data": data_v,
                             "missing": missing, "notes": notes}}
    return entry, ""


def find_saved_build(body: dict):
    """(build, character name) for {"character_id", "build_id"} (library) or {"history_id"}; (None, "") if gone."""
    if body.get("history_id"):
        h = next((x for x in load_history() if x.get("id") == body["history_id"]), None)
        return h, ""
    c = next((x for x in load_library()["characters"] if x["id"] == body.get("character_id")), None)
    b = next((x for x in (c or {}).get("builds", []) if x["id"] == body.get("build_id")), None)
    return b, (c["name"] if b is not None else "")


def build_summary():
    """The last report as data for the visuals, plus its before/after baseline; saves it to history."""
    R = wh.LAST_RESULT
    if not R.get("current"):
        return None
    tree, level, b = R["tree"], R["level"], R["build"]
    focus = R.get("focus")
    label = focus[1] if R["goal"] == "ability" and focus else GOAL_LABELS.get(R["goal"], "Score")
    b0 = R.get("build")
    if b0 is not None and getattr(b0, "goal2", None):          # a mixed goal: "70% Spell damage + 30% Survival"
        label = (f"{round((1 - b0.goal2_weight) * 100)}% {label} + {round(b0.goal2_weight * 100)}% "
                 f"{GOAL_LABELS.get(b0.goal2, b0.goal2)}")

    def pack(x):
        ev = x["eval"]
        ass, _, _ = wh.solve_skill_points(x["armor"], x["weapon"])
        extra = list(x["extra"])
        totals = list(ev.get("totals") or [0] * 5)
        return {"items": [item_card(a, wh.SLOT_LABELS[i]) for i, a in enumerate(x["armor"])] + [item_card(x["weapon"], "Weapon")],
                "numbers": numbers_of(ev), "owned": sorted(x["owned"]),
                "order": tree.validate(x["owned"], level)["order"],
                "sp": {"assigned": list(ass), "extra": extra, "gear": [totals[i] - ass[i] - extra[i] for i in range(5)]},
                "mana": ({k: v for k, v in ev["mana"].items()} if ev.get("mana") else None),
                "goal_parts": ev.get("goal_parts"), "roles": roles_of(x),
                "unspent": {"ap": max(0, wh.ability_points(level) - tree.validate(x["owned"], level)["ap"]),
                            "ideas": [list(i) for i in (R.get("best_ideas") if x is R.get("best") else R.get("current_ideas")) or []]}}

    def roles_of(x):                    # why each ability is in the tree (shown on the tree map)
        try:
            return {str(k): v for k, v in wh.tree_roles(b, x["owned"], x["armor"], x["weapon"], list(x["extra"]), level).items()}
        except Exception:
            return {}

    best_ev = (R.get("best") or R["current"])["eval"]
    mask = best_ev.get("mask")
    on = set(b.toggle_map((R.get("best") or R["current"])["owned"])) - set(b.toggle_share) | ({mask} if mask else set())
    if mask == "Mask of the Lunatic" and "Lunatic Transmute" not in b.toggles_off:
        on.add("Lunatic Transmute")
    out = {"class": R["cls"], "class_title": wh.CLASS_TITLE[R["cls"]], "level": level, "goal": R["goal"], "label": label,
           "focus": focus[1] if focus else None, "floors": dict(b.floors), "toggles_on": sorted(on),
           "toggle_share": dict(b.toggle_share), "mask": mask,
           "no_gear": R["no_gear"], "current": pack(R["current"]), "best": pack(R["best"]) if R.get("best") else None,
           "best_link": R.get("best_link"), "tree": tree_static(tree),
           "settings": settings_notes(b) + ([f"Only your items ({R['only_items']['count']} listed)"] if R.get("only_items") else [])
                       + (["Mythic items allowed"] if wh.ALLOW_MYTHIC else []),
           "rolls": wh.ROLL_MODE}
    hist = load_history()
    if out["best"]:
        zero = {k: 0.0 for k in out["best"]["numbers"]}
        if not R["no_gear"]:
            st = R.get("start") or R["current"]["eval"]
            out["baseline"] = {"kind": "gear", "label": "Your gear", "numbers": numbers_of(st),
                               "items": [x["name"] if x else None for x in out["current"]["items"]]}
        else:
            prev = next((h for h in reversed(hist) if h.get("class") == R["cls"] and h.get("goal") == R["goal"]
                         and h.get("focus") == out["focus"] and h.get("label") == label), None)
            if prev:
                out["baseline"] = {"kind": "history", "numbers": prev["numbers"],
                                   "label": f"Your last {out['class_title']} build (level {prev['level']}, {prev['date']})",
                                   "items": [x["name"] if x else None for x in prev.get("items", [])]}
            else:
                out["baseline"] = {"kind": "zero", "label": "Starting from zero (no gear entered, no earlier build)",
                                   "numbers": zero, "items": None}
        entry = {"id": str(int(time.time() * 1000)), "date": time.strftime("%b %d %H:%M"), "class": R["cls"],
                 "class_title": out["class_title"], "level": level, "goal": R["goal"], "label": label,
                 "focus": out["focus"], "floors": out["floors"], "toggles_on": out["toggles_on"],
                 "toggle_share": out["toggle_share"],
                 "numbers": out["best"]["numbers"], "items": out["best"]["items"], "sp": out["best"]["sp"],
                 "owned": out["best"]["owned"], "order": out["best"]["order"], "link": out["best_link"],
                 "settings": out["settings"], "rolls": out["rolls"], "roles": out["best"].get("roles") or {},
                 "unspent": out["best"].get("unspent")}
        hist.append(entry)
        save_history(hist)
        out["history_id"] = entry["id"]
        STATE["last_history_id"] = entry["id"]
    return out


# ============================================================================== Claude

SYSTEM_PROMPT = """You are Wynn Build Helper, a friendly expert at Wynncraft builds, talking with a player inside a desktop app.

You have a calculator (the analyze_build tool) that uses Wynnbuilder's exact formulas to work out the best ability tree, skill points, powders, gear upgrades, tomes, aspects and a level-by-level plan. Use it for every number and recommendation. Never make up item stats, ability tree paths or damage numbers; if you're not sure, look them up with the tools.

How to work:
- To run the calculator you need the class and combat level, or a Wynnbuilder link. If either is missing, ask for it in one short question. Ask about gear too, but if they don't know or don't care, run without it.
- Questions before a build (class, level, gear, minimums) must be short and easy to scan: one line each, a few words, no explanations or examples beyond a couple of words. If you need several, put them as a short list, e.g.
  "Quick check before I start:
  - Any minimums? (e.g. 20k health, no slower walk speed)
  - Gear you're wearing? (or skip)"
  Don't ask about crafted items; they're only used when the player brings them up.
  Save longer explanations for when you explain a decision or a result.
- Baseline stats: the first time they ask for a build (or a new goal), ask whether they want any minimums kept, as one short line like the example above. Put it in the same message as any other question you need to ask; if you have everything else, ask it on its own and wait. If they say no or don't care, run without minimums and don't ask again for follow-ups. Pass what they say as minimums (health, ehp, walk_speed in %, mana_regen, health_regen, life_steal, mana_steal, elemental defenses). "No negative walk speed" means walk_speed 0. "No negative health", "never lose health", "don't lose more HP than I gain" means net_health 0: health regen plus life steal together, so either can be negative as long as the total isn't (this is usually what players mean; use health_regen 0 / life_steal 0 only when they clearly mean each stat on its own).
- Numbers only come from the calculator. Never estimate a build's numbers yourself ("should add about +44%"), and never hand-edit a build the calculator chose and present it as a result. If a build breaks something the player asked for, pass it as a minimum and run again; if the calculator can't enforce it, say so plainly and ask before changing anything. To check a changed build, run analyze_build with that gear.
- Some abilities only give their bonus while a toggle is on. By default the calculator only counts buffs whose timing is known: Bullwhip (on), the best Mystic Mask (you always wear one), Coursing Restraints for the share of time its duration and cooldown allow, and ultimate buffs for the same share as ultimate damage. Everything else (Holy Power, Tricks, Unstable, Blood Pool, Corrupted, "next hit" effects) is off so numbers aren't inflated. If the player says they keep a buff up, pass it in toggles, with a share if it's only part of the time, e.g. "Contamination=70%, Activate Luster Purge". Pass toggles_off for buffs they say they don't keep up. The report's "Toggles and sliders" section shows what each off toggle would add; mention a big one and ask if they keep it up.
- Characters: the player's saved characters come with each message, in a note from the app (the player didn't type it). When they mean one ("my shaman", "Sparky", "my main"), pass character with its name instead of asking for class and level: its class, level and current gear are filled in (pass gear only to change it, use_saved_gear false to start from nothing). If they say a character leveled up or was renamed, update it with manage_characters. After a build the app itself asks whether to save it to a character's library; save it with manage_characters only if they ask you to. Don't create characters unless they ask.
- Building from their own items: when the player says which items they have and wants a build out of them ("I have these items, make a build", "only use what I own"), pass only_items with those names (add x2 to a ring they have two of; a ring listed once is never worn twice). What they wear (gear) counts as theirs too. When they mean a character's saved items ("use my items"), pass character and use_my_items true. If they want their list kept for next time, save it with manage_characters (set_items, add_items or remove_items). The report's "Worth getting next" part lists single items they don't have that would raise that build the most; mention the best one or two. A slot with nothing in their list stays empty, and with no usable weapon listed the weapon can be any.
- Sharing and comparing: every saved build has a Share button that makes a share code (starts with WBH1-) a friend pastes into their app (Home page, or in this chat, which opens it straight away), and a Compare button that puts two saved builds side by side (numbers, gear, skill points, abilities). Point the player to those when they ask; you can't make or read share codes yourself.
- If they want a mix ("mostly damage but I want to survive too", "70/30 spells and tankiness"), pass the main goal plus second_goal and second_goal_weight (the share for the second goal, default 30). This is a weighted mix, softer than a minimum: use minimums when they name a number ("at least 20k health"), the mix when they describe a preference. balanced is the same idea at 50/50 for damage and effective HP.
- The report's "Abilities that matter most" lines say which abilities in the best build's tree raise the goal and by how much, which are only there to reach those (a path or an archetype count), and which are spare points that don't change the goal's number. When the player asks why an ability is there or says the tree looks odd, explain with that, and say spare points can go anywhere they like. Points that wouldn't raise the goal are left unspent on purpose (the report says how many and lists ideas, e.g. an ultimate that adds combo damage); say so if they ask why the tree doesn't use every point.
- If no build meets every minimum, the report says so and shows the closest one; tell them which minimum couldn't be met and suggest relaxing it.
- The report shows health, effective HP and walk speed for the best build next to the current gear; mention any big drop in health or walk speed so they can set a minimum.
- If they want the most damage from one specific ability ("maximum puppet damage", "biggest Meteor", "strongest Bash"), use goal ability with focus set to that ability.
- Damage goals. "Max DPS", "highest DPS", "as much DPS/damage as possible", "most damage" and plain "DPS" or "damage" mean goal mixed: the most damage per second possible in any style (main attacks only, spells, or both together, whichever is highest; it never scores a build below its main-attack or spell-only damage). It's the helper's own estimate (Wynnbuilder has no such number), so say so; the report says which style wins and the rotation. It runs several full searches (spell-only, main-attack-only, searches from ability trees planned for spells and for main attacks, then any style starting from all of them), so it takes about three to five times as long as other goals; mention that in one short line when you start it. Use melee only when they ask specifically for main attacks / a melee or M1 build / Wynnbuilder's Average DPS (main attack damage per hit x attack speed, the number Wynnbuilder shows), and spells only when they want a spell-only build. Mana steal and life steal only work on main attack hits, never on spells: never suggest them to keep a spell-only rotation going (the calculator doesn't count mana steal for spell casting).
- Other goals from what they say: burst for one big combo, survival (tanky / keeps dying), balanced (damage and survival), healing (support). If they keep dying to a particular boss or element, pass that as the enemy hit.
- Pass item and ability names exactly as the player wrote them; the calculator corrects spelling and says so. "Mana bank" means the Mage ability Arcane Transfer.
- Crafted items are written as codes starting with CR- (and custom items CI-); pass them as gear.
- Crafted items are off unless the player asks for them. Set crafted to true only when they say they want crafted items in the build (e.g. "with crafted items", "I can craft", "include crafteds"); otherwise leave it out. With crafted on, the search designs crafted items for the build (recipe, materials and the 6-ingredient grid, following the game's crafting rules) and uses them where they beat normal items, and the report's "Crafted pieces" part says how to make each one and where its ingredients drop. When the best build has crafted pieces, say which and what they add; someone has to craft them (the player, with the profession level the report gives, or a crafter). If they ask whether crafted items would help, you can offer to run it with crafted on.
- Deep search is the default and takes several minutes. Only set deep_search to false if they ask for something quick, or for small follow-up questions where a rough answer is enough.
- Think about the build as a whole, never as the best item per slot. The report's "Best whole build" section is the calculator choosing every piece together with the rest of the build on (skill points items give and need from each other, set bonuses, elements and powders, major IDs, the ability tree re-planned for that gear). Lead with that when the player wants a new build or upgrades, and use the one-slot list only for a single quick swap.
- Explain the synergy in plain words: why these pieces work together, for example "Ajax's +13 Strength covers the Strength the chestplate and leggings need, which frees points for Dexterity", "two Corrupted pieces turn on the set bonus", "every piece adds air damage, matching the air powders and Wind Prison". Check item details with lookup_item when you explain, so what you say is true. If a piece is there mainly to enable others, say so.
- Match the build to the play style they describe. A melee / M1 build is judged on Wynnbuilder's Average DPS (goal melee: damage per hit x attack speed, so attack speed matters as much as damage); a spell build on spell damage within its mana; plain "DPS" means the most damage in any style (goal mixed, see above); if they want both damage and to stay alive, use balanced. If the best whole build trades away something they care about (for example less health, or a new element), point it out and offer the alternative.
- After a report, the player sees the full report in the panel next to the chat, so don't repeat it. Reply with a short plain summary: the build as a whole and why it fits together, then the 3 to 5 most useful things to do (skill points, what to click next in the tree, powders), plus anything surprising. Use short paragraphs or a few bullets, under about 220 words unless they ask for detail.
- When they change something ("I'm level 50 now", "make it tankier", "what if I use X"), run the calculator again with the change and say what's different.
- Mythic items are never used unless the player says you can use mythics; then set mythics true (for that build and follow-ups in the same chat). Mythics they wear or list as their items count as theirs. Don't ask about mythics; if a build would clearly need one, you can mention they can allow them.
- Tomes: from level 55 the best weapon and armor tomes are picked for the build and counted in its numbers (tomes themselves need level 60; guild tomes are left out because they need a guild). The report says which. If the player lists the tomes they have, pass them as tomes and only those are used; if they say they don't use tomes, set auto_tomes false.
- Item special effects (major IDs, common on Fabled and Mythic items): the report's "Special effects" lines say what each one in the build does and whether it's counted. Most are counted with Wynnbuilder's numbers; a few the helper counts itself (for example Pas De Deux making Frog Dance deal no damage, Self-Sufficient's weapon special timing); movement, ally, situational ones and ones without an exact size aren't counted. Say so when the player asks about a Fabled or Mythic item, and don't claim an uncounted effect adds damage.
- Mention a limitation only when it matters for their question (for example: items marked "not a mob drop" may be raid, quest, merchant or event rewards; poison numbers follow Wynnbuilder).
- Plain words, no jargon the player didn't use. Never mention tools, JSON or code."""

TOOLS = [
    {
        "name": "analyze_build",
        "description": ("Work out the best build with Wynnbuilder's formulas. Returns the full report: gear, skill points "
                        "to assign, powders, best ability tree in click order, damage, survivability, a level-by-level "
                        "plan, gear upgrades (single items and set combos), tomes, aspects and a Wynnbuilder link. "
                        "Needs class + level, or a Wynnbuilder link. Takes 5 to 60 seconds."),
        "input_schema": {
            "type": "object",
            "properties": {
                "class": {"type": "string", "enum": wh.CLASSES},
                "level": {"type": "integer", "minimum": 1, "maximum": 121, "description": "Combat level"},
                "goal": {"type": "string", "enum": list(wh.GOALS),
                         "description": "; ".join(f"{k}: {v}" for k, v in wh.GOALS.items())},
                "character": {"type": "string",
                              "description": "One of the player's saved characters, by name (or 'my shaman' if they "
                                             "have one shaman). Fills in its class, level and current gear; anything "
                                             "you pass yourself overrides that."},
                "use_saved_gear": {"type": "boolean",
                                   "description": "With character: start from its current build's gear (default true). "
                                                  "False to build from nothing."},
                "second_goal": {"type": "string", "enum": list(wh.SECOND_GOALS),
                                "description": "Optional second goal to mix in by weight, e.g. survival when they want "
                                               "mostly damage but also to stay alive. Not the same as goal."},
                "second_goal_weight": {"type": "number",
                                       "description": "How much the second goal counts, in percent (10-90, default 30)."},
                "focus": {"type": "string",
                          "description": "For goal 'ability': the one ability to maximise, as the player said it, "
                                         "e.g. 'puppets', 'Meteor', 'Bash', 'Totem'"},
                "gear": {"type": "array", "items": {"type": "string"},
                         "description": "Items they wear, any spelling; crafted/custom items as CR-/CI- codes"},
                "only_items": {"type": "array", "items": {"type": "string"},
                               "description": "Build only from these items (the ones the player has), any spelling; "
                                              "'Horseshoe x2' for two of a ring. What they wear counts too."},
                "use_my_items": {"type": "boolean",
                                 "description": "With character: build only from that character's saved items (My items)."},
                "keep": {"type": "array", "items": {"type": "string"},
                         "description": "Abilities they want no matter what"},
                "wynnbuilder_link": {"type": "string", "description": "A wynnbuilder.github.io/builder link"},
                "powders": {"type": "string",
                            "description": 'Powders already slotted, e.g. "weapon=t6 t6; chestplate=w4 w4". Leave the '
                                           'weapon out to get a recommendation.'},
                "max_powder_tier": {"type": "integer", "minimum": 1, "maximum": 7},
                "tomes": {"type": "array", "items": {"type": "string"},
                          "description": "Tomes the player has. Leave out to let the calculator pick the best weapon and "
                                         "armor tomes for the build (from level 55; tomes need level 60)."},
                "mythics": {"type": "boolean",
                            "description": "Allow mythic items (and mythic tomes). Default false: mythics are left out "
                                           "unless the player says you can use mythics."},
                "auto_tomes": {"type": "boolean",
                               "description": "Default true: pick tomes for the build. False if the player says they "
                                              "don't use tomes."},
                "aspects": {"type": "string", "description": 'Aspects with tier, e.g. "Comet 2, Ray of Frost 1"'},
                "enemy": {"type": "string", "description": 'An enemy hit to survive, e.g. "fire 3000" or "fire=2000, earth=1000"'},
                "minimums": {
                    "type": "object",
                    "description": ("Minimum stats every suggested build must keep (the player's baselines). The search "
                                    "maximizes the goal only among builds that meet all of them. Leave out what they "
                                    "didn't ask for."),
                    "properties": {
                        "health": {"type": "number", "description": "Total health, e.g. 20000"},
                        "ehp": {"type": "number", "description": "Effective HP (against the enemy hit if one is given)"},
                        "walk_speed": {"type": "number",
                                       "description": "Walk speed bonus in %, e.g. 0 for 'no slower than normal', 20 for +20%"},
                        "mana_regen": {"type": "number"},
                        "health_regen": {"type": "number"},
                        "life_steal": {"type": "number", "description": "Life steal per 3 s, e.g. 0 for 'no negative life steal'"},
                        "mana_steal": {"type": "number", "description": "Mana steal per 3 s"},
                        "net_health": {"type": "number",
                                       "description": "Health gained per second from health regen (every 4 s) plus life "
                                                      "steal (per 3 s while main-attacking). 0 = never lose health."},
                        "earth_defense": {"type": "number"}, "thunder_defense": {"type": "number"},
                        "water_defense": {"type": "number"}, "fire_defense": {"type": "number"},
                        "air_defense": {"type": "number"},
                    },
                },
                "sliders": {"type": "string", "description": 'Ability stack sliders, e.g. "Distortion=40"'},
                "toggles": {"type": "string",
                            "description": "Ability toggles the player keeps up, comma-separated, with an optional share of "
                                           "the time, e.g. 'Activate Aerial Ace, Contamination=70%'. The report lists each "
                                           "toggle and what it would add."},
                "toggles_off": {"type": "string",
                                "description": "Toggles that are on by default to turn off, e.g. 'Activate Bullwhip' "
                                               "if the player doesn't keep enemies Whipped"},
                "buffs": {"type": "array", "items": {"type": "string", "enum": list(wh.BUFFS)}},
                "raid_buffs": {"type": "array", "items": {"type": "string"}},
                "drops_only": {"type": "boolean", "description": "Only suggest items that drop from mobs, chests or dungeons"},
                "crafted": {"type": "boolean", "description": "Design crafted items for the build too (default false). True only "
                            "when the player asks for crafted items."},
                "rolls": {"type": "string", "enum": wh.ROLL_MODES,
                          "description": "Item rolls: max (default, like Wynnbuilder), avg, min, base"},
                "poison": {"type": "string", "enum": ["wynnbuilder", "low"]},
                "plan_levels": {"type": "integer", "minimum": 1, "maximum": 30},
                "include_gear_suggestions": {"type": "boolean", "description": "Default true. False makes it faster"},
                "include_level_plan": {"type": "boolean", "description": "Default true. False makes it faster"},
                "deep_search": {"type": "boolean",
                                "description": "Default true: deep search for the best whole build (several minutes). "
                                               "Set false only when they want a quick answer (about a minute)."},
                "item_levels": {"type": "integer", "minimum": 1,
                                "description": "Only use items within this many levels below theirs (default: all items up to their level)"},
                "include_whole_build": {"type": "boolean",
                                        "description": "Default true: also choose all gear together as one build "
                                                       "(needs gear suggestions on). False makes it faster"},
            },
        },
    },
    {
        "name": "manage_characters",
        "description": "The player's saved characters (listed in your instructions). create: a new character "
                       "(name, class, level). update: change a character's level or name (name = current name). "
                       "save_last_build: save the build from the last report to a character's library (name; "
                       "create_if_missing true to make a new character with that name). set_items / add_items / "
                       "remove_items: the character's My items list (name, items; 'x2' for two of a ring). "
                       "Only when the player asks.",
        "input_schema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "update", "save_last_build", "set_items", "add_items",
                                                  "remove_items"]},
            "name": {"type": "string"}, "class": {"type": "string", "enum": wh.CLASSES},
            "items": {"type": "array", "items": {"type": "string"}},
            "level": {"type": "integer"}, "new_name": {"type": "string"}, "create_if_missing": {"type": "boolean"}},
            "required": ["action"]},
    },
    {
        "name": "lookup_item",
        "description": "Stats, requirements, powder slots, major IDs and set of one item (any spelling).",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "list_abilities",
        "description": "Every ability in a class's tree with its cost and archetype.",
        "input_schema": {"type": "object", "properties": {"class": {"type": "string", "enum": wh.CLASSES}},
                         "required": ["class"]},
    },
]


class ClaudeError(Exception):
    pass


def clean_history(history: list, turn_start: int = None, keep_thinking: bool = True) -> list:
    """The conversation as Claude's API accepts it, whatever happened before.

    - Only the parts the API takes back; no empty messages; every tool request answered (a request without an
      answer, e.g. from a reply that was cut off or a tool that failed, is left out, as are answers without one);
      the same speaker never twice in a row.
    - Claude's thinking: each thought is tied to exactly what came before it. Thoughts from earlier turns are left
      out (the API allows that); thoughts from the turn in progress are kept unchanged (it needs those while it
      uses tools), and nothing before them changes during the turn.
    - Build reports from earlier turns are shortened, except the most recent one, so a long chat doesn't outgrow
      what Claude can read. Only turns before the one in progress are touched, so it stays the same all turn.
    turn_start: index in history of the player's message that started the turn in progress (default: the last
    player message that isn't tool answers)."""
    if turn_start is None:
        turn_start = 0
        for i, m in enumerate(history):
            c = m.get("content")
            if m.get("role") == "user" and (isinstance(c, str) or not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c or [])):
                turn_start = i
    msgs = []
    for idx, m in enumerate(history):
        content = m.get("content")
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}] if content.strip() else []
        else:
            blocks = []
            for b in content or []:
                t = b.get("type")
                if t in ("thinking", "redacted_thinking"):
                    if keep_thinking and idx >= turn_start and m.get("role") == "assistant":
                        blocks.append(b)                  # exactly as Claude sent it
                elif t == "text" and (b.get("text") or "").strip():
                    blocks.append({"type": "text", "text": b["text"]})
                elif t == "tool_use" and b.get("id") and b.get("name"):
                    blocks.append({"type": "tool_use", "id": b["id"], "name": b["name"],
                                   "input": b.get("input") if isinstance(b.get("input"), dict) else {}})
                elif t == "tool_result" and b.get("tool_use_id"):
                    blocks.append({"type": "tool_result", "tool_use_id": b["tool_use_id"],
                                   "content": str(b.get("content") or "(no result)")})
        msgs.append({"role": m.get("role"), "content": blocks, "old": idx < turn_start})
    # every tool request needs its answer in the very next message, and every answer its request
    for i, m in enumerate(msgs):
        if m["role"] != "assistant":
            continue
        nxt = msgs[i + 1] if i + 1 < len(msgs) and msgs[i + 1]["role"] == "user" else None
        answered = {b["tool_use_id"] for b in (nxt["content"] if nxt else []) if b["type"] == "tool_result"}
        m["content"] = [b for b in m["content"] if b["type"] != "tool_use" or b["id"] in answered]
        if not any(b["type"] in ("text", "tool_use") for b in m["content"]):
            m["content"] = []                              # thoughts alone aren't a message
    for i, m in enumerate(msgs):
        if m["role"] != "user":
            continue
        prev = msgs[i - 1] if i > 0 and msgs[i - 1]["role"] == "assistant" else None
        asked = {b["id"] for b in (prev["content"] if prev else []) if b["type"] == "tool_use"}
        m["content"] = [b for b in m["content"] if b["type"] != "tool_result" or b["tool_use_id"] in asked]
    # shorten build reports from earlier turns, all but the most recent of them
    names = {b["id"]: b["name"] for m in msgs if m["role"] == "assistant" for b in m["content"] if b["type"] == "tool_use"}
    old_reports = [b for m in msgs if m["role"] == "user" and m["old"] for b in m["content"]
                   if b["type"] == "tool_result" and names.get(b["tool_use_id"]) == "analyze_build"]
    for b in old_reports[:-1]:
        if len(b["content"]) > 6000:
            b["content"] = b["content"][:6000] + "\n... (older report shortened; run it again for the full details)"
    # no empty messages, no speaker twice in a row, starting with the player
    out = []
    for m in msgs:
        if not m["content"] or m["role"] not in ("user", "assistant"):
            continue
        m = {"role": m["role"], "content": m["content"]}
        if out and out[-1]["role"] == m["role"]:
            strip = lambda c: [b for b in c if b["type"] not in ("thinking", "redacted_thinking")]
            if m["role"] == "assistant":
                out[-1] = {"role": "assistant", "content": strip(out[-1]["content"]) + strip(m["content"])}
            else:
                out[-1] = {"role": "user", "content": out[-1]["content"] + m["content"]}
            continue
        out.append(m)
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


class ThinkingMismatch(Exception):
    pass


def call_claude(messages: list, model: str, turn_start: int = None, keep_thinking: bool = True) -> dict:
    key = api_key()
    # The instructions never change during a chat (the player's characters go with each message instead), because
    # Claude's thinking is tied to them.
    body = json.dumps({"model": model, "max_tokens": 4096, "system": SYSTEM_PROMPT, "tools": TOOLS,
                       "messages": clean_history(messages, turn_start, keep_thinking)}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=ssl_context()) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.load(exc).get("error", {}).get("message", "")
        except Exception:
            pass
        low = detail.lower()
        if exc.code == 400 and "thinking" in low and ("signature" in low or "bound" in low):
            raise ThinkingMismatch(detail)
        if "credit balance" in low:
            raise ClaudeError("Your Claude API credit has run out. Add credit at console.anthropic.com (Plans & Billing), "
                              "then send your message again.")
        if "prompt is too long" in low or "too many tokens" in low:
            raise ClaudeError("This chat got too long for Claude to read. Press New chat (top of the chat) to start fresh; "
                              "your builds stay in History and your library.")
        msg = {401: "Claude didn't accept the API key. Check it in Settings.",
               403: "This API key isn't allowed to use that model. Try another model in Settings.",
               404: "That model isn't available on your account. Pick another in Settings.",
               429: "Too many requests or no API credit left. Check your usage at console.anthropic.com.",
               529: "Claude is busy right now. Try again in a minute."}.get(exc.code, f"Claude returned an error ({exc.code}).")
        raise ClaudeError(msg + (f" ({detail})" if detail and exc.code not in (401,) else ""))
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "CERTIFICATE" in reason.upper():
            raise ClaudeError("Your Python can't check secure connections. On a Mac, open Applications > Python and run "
                              "'Install Certificates.command', then restart the app.")
        raise ClaudeError(f"Couldn't reach Claude ({reason}). Check your internet connection.")


def chat_turn(user_text: str) -> dict:
    """One user message: let Claude talk and run the calculator as often as it needs."""
    if not api_key():
        return {"reply": "To chat, add your Claude API key in Settings (top right). You can use the form in the "
                         "meantime: it gives the same results.", "needs_key": True}
    model = load_config().get("model") or DEFAULT_MODEL
    history = STATE["chat"]
    start = len(history)
    # what the app knows right now (the player's characters) goes with the message, so it stays exactly as sent
    history.append({"role": "user", "content": [{"type": "text", "text": user_text},
                                                 {"type": "text", "text": "(Note from the app, not typed by the player:"
                                                  + characters_context() + ")"}]})
    reports, summaries = [], []
    char_id = None
    STATE["library_changed"] = False
    try:
        keep = True
        for _ in range(8):
            try:
                resp = call_claude(history, model, start, keep)
            except ThinkingMismatch:          # shouldn't happen any more; if it does, once more without thoughts
                keep = False
                resp = call_claude(history, model, start, keep)
            content = resp.get("content", [])
            history.append({"role": "assistant", "content": content})
            if resp.get("stop_reason") != "tool_use":
                text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                return {"reply": text or "(no reply)", "reports": reports, "summaries": summaries, "args": STATE["last_args"],
                        "character_id": char_id, "library_changed": STATE.get("library_changed", False)}
            results = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                name, inp = block.get("name"), block.get("input") or {}
                try:
                    if name == "analyze_build":
                        out = run_report(inp)
                        if out.get("character_id"):
                            char_id = out["character_id"]
                        if out["ok"]:
                            reports.append(out["text"])
                            summaries.append(out.get("summary"))
                        result = out["text"]
                    elif name == "manage_characters":
                        result = manage_characters(inp)
                    elif name == "lookup_item":
                        result = item_summary(inp.get("name", ""))
                    elif name == "list_abilities":
                        result = ability_list(inp.get("class", ""))
                    else:
                        result = "Unknown tool."
                except Exception as exc:         # always answer, so the conversation stays valid
                    traceback.print_exc()
                    result = f"That didn't work ({type(exc).__name__}: {exc})."
                results.append({"type": "tool_result", "tool_use_id": block["id"], "content": (str(result) or "(no result)")[:60000]})
            history.append({"role": "user", "content": results})
        return {"reply": "That took more steps than expected. Try asking in a simpler way.", "reports": reports}
    except (ClaudeError, ThinkingMismatch) as exc:
        del history[start:]             # forget the unanswered turn so the conversation stays valid
        msg = str(exc) if isinstance(exc, ClaudeError) else "Claude couldn't continue this chat. Press New chat to start fresh."
        return {"reply": msg, "error": True, "reports": reports}
    except Exception as exc:            # anything else: same, and say what happened
        traceback.print_exc()
        del history[start:]
        return {"reply": f"Something went wrong talking to Claude ({type(exc).__name__}). Try again; if it keeps "
                         "happening, press New chat.", "error": True, "reports": reports}


# ============================================================================== web server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        STATE["last_ping"] = time.time()
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            with open(UI_PATH, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if path == "/api/meta":
            d = game_data()
            cfg = load_config()
            return self._send(200, {
                "classes": wh.CLASSES, "goals": wh.GOALS, "buffs": list(wh.BUFFS), "version": d.version,
                "has_key": bool(api_key()), "model": cfg.get("model") or DEFAULT_MODEL, "models": MODELS,
                "popup": cfg.get("popup", True), "sound": cfg.get("sound", True), "app_version": updater.current_version().get("version", "?"),
                "auto_update": cfg.get("auto_update", True), "updates_configured": updater.configured(),
                "key_hint": ("…" + api_key()[-4:]) if api_key() else ""})
        if path == "/api/items":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            return self._send(200, autocomplete((q.get("q") or [""])[0], (q.get("kind") or [""])[0]))
        if path == "/api/abilities":
            from urllib.parse import parse_qs, urlparse
            cls = ((parse_qs(urlparse(self.path).query).get("class") or [""])[0]).lower()
            names = []
            if cls in wh.CLASSES:
                tree = wh.Tree(game_data(), cls)
                names = sorted({e.get("name") for n in tree.nodes for e in n.raw.get("effects", [])
                                if e.get("type") == "replace_spell" and e.get("name")})
            return self._send(200, names)
        if path == "/api/history":
            return self._send(200, [{**{k: h.get(k) for k in ("id", "date", "class_title", "level", "label", "focus", "numbers", "floors")},
                                     "items": [[x["name"], x["tier"]] if x else None for x in h.get("items", [])]}
                                    for h in reversed(load_history())])
        if path == "/api/library":
            return self._send(200, library_summary(load_library()))
        if path.startswith("/api/library/build"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            cid, bid = (q.get("c") or [""])[0], (q.get("b") or [""])[0]
            c = next((x for x in load_library()["characters"] if x["id"] == cid), None)
            b = next((x for x in (c or {}).get("builds", []) if x["id"] == bid), None)
            if b is None:
                return self._send(404, {"error": "not found"})
            try:
                b = dict(b, tree=tree_static(wh.Tree(game_data(), b["class"])))
            except Exception:
                pass
            return self._send(200, dict(b, character={"id": c["id"], "name": c["name"]}))
        if path.startswith("/api/history/"):
            hid = path.rsplit("/", 1)[-1]
            h = next((x for x in load_history() if x.get("id") == hid), None)
            if h is None:
                return self._send(404, {"error": "not found"})
            try:
                h = dict(h, tree=tree_static(wh.Tree(game_data(), h["class"])))
            except Exception:
                pass
            return self._send(200, h)
        if path == "/api/live":
            full = "full=1" in self.path
            return self._send(200, ws.live_snapshot(full) if STATE.get("progress") else {"id": None})
        if path == "/api/update/check":
            return self._send(200, updater.check())        # a newer version is always offered, even after switching back
        if path == "/api/versions":
            return self._send(200, updater.list_versions())
        if path == "/api/ping":
            return self._send(200, {"ok": True})
        if path == "/api/progress":
            p = STATE.get("progress")
            if p:
                p = dict(p)
                el = time.time() - (p.get("started") or time.time())
                f = p.get("fraction") or 0
                p["elapsed"] = el
                p["eta"] = (el / f - el) if f > 0.08 else None
            return self._send(200, {"progress": p})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        STATE["last_ping"] = time.time()
        body = self._body()
        if self.path == "/api/analyze":
            return self._send(200, run_report(body))
        if self.path == "/api/chat":
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(200, {"reply": ""})
            return self._send(200, chat_turn(text))
        if self.path == "/api/stop":
            ws.STOP["flag"] = True
            return self._send(200, {"ok": True})
        if self.path == "/api/chat/reset":
            STATE["chat"] = []
            return self._send(200, {"ok": True})
        if self.path == "/api/settings":
            cfg = load_config()
            if "api_key" in body:
                write_key_file(body.get("api_key") or "")      # the key lives in api_key.txt
                cfg.pop("api_key", None)
            if body.get("model") in MODELS:
                cfg["model"] = body["model"]
            if "popup" in body:
                cfg["popup"] = bool(body["popup"])
            if "sound" in body:
                cfg["sound"] = bool(body["sound"])
            if "auto_update" in body:
                cfg["auto_update"] = bool(body["auto_update"])
            save_config(cfg)
            return self._send(200, {"ok": True, "has_key": bool(api_key())})
        if self.path == "/api/library":
            return self._send(200, library_action(body))
        if self.path == "/api/share":                  # a saved build -> share code
            b, name = find_saved_build(body)
            if b is None:
                return self._send(200, {"ok": False, "error": "That build isn't saved any more."})
            try:
                return self._send(200, {"ok": True, "code": share_code(b, name)})
            except Exception as exc:
                return self._send(200, {"ok": False, "error": f"Couldn't make a share code ({type(exc).__name__})."})
        if self.path == "/api/share/open":             # share code -> build to look at (not saved yet)
            entry, err = decode_share(body.get("code"))
            if entry is None:
                return self._send(200, {"ok": False, "error": err})
            try:
                entry["tree"] = tree_static(wh.Tree(game_data(), entry["class"]))
            except Exception:
                pass
            return self._send(200, {"ok": True, "build": entry})
        if self.path == "/api/history/delete":
            save_history([h for h in load_history() if h.get("id") != body.get("id")])
            return self._send(200, {"ok": True})
        if self.path == "/api/history/clear":
            save_history([])
            return self._send(200, {"ok": True})
        if self.path == "/api/update/install":
            if RUN_LOCK.locked():
                return self._send(200, {"ok": False, "error": "A build is still being worked out. Install the update when it's done."})
            with RUN_LOCK:
                return self._send(200, updater.install(body.get("version")))
        if self.path == "/api/versions/switch":
            target = str(body.get("version") or "").lstrip("v")
            if not target:
                return self._send(200, {"ok": False, "error": "Pick a version first."})
            if RUN_LOCK.locked():
                return self._send(200, {"ok": False, "error": "A build is still being worked out. Switch when it's done."})
            with RUN_LOCK:
                return self._send(200, updater.install(version=target))
        if self.path == "/api/restart":
            self._send(200, {"ok": True})
            def _restart():
                time.sleep(0.5)
                try:
                    updater.restart_app()
                finally:
                    os._exit(0)
            threading.Thread(target=_restart, daemon=True).start()
            return
        if self.path == "/api/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        return self._send(404, {"error": "not found"})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    if not os.path.exists(KEY_FILE):          # first start: a file to paste the API key into
        try:
            write_key_file("")
        except OSError:
            pass
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    # Get game data ready (downloads new data when online) while the window opens.
    def warm():
        try:
            with RUN_LOCK:
                STATE["data"] = wh.load_game_data(offline=False)
        except BaseException:
            pass
        STATE["data_checked"] = True
    threading.Thread(target=warm, daemon=True).start()
    try:
        import webview  # pywebview: a real app window
    except Exception:
        webview = None
    if webview is not None and "--browser" not in sys.argv:
        webview.create_window("Wynn Build Helper", url, width=1320, height=860, min_size=(900, 600),
                              background_color="#171a2c")
        webview.start()
        os._exit(0)
    STATE["browser_mode"] = True
    print(f"Wynn Build Helper is running at {url}")
    print("It opened in your browser. Close the browser tab (or press Ctrl+C here) to stop it.")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(5)
            if time.time() - STATE["last_ping"] > 120:      # the page pings every 20 s while it's open
                break
    except KeyboardInterrupt:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
