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

import contextlib
import io
import json
import os
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
DEFAULT_MODEL = "claude-sonnet-5-5"
MODELS = {"claude-sonnet-5-5": "Claude Sonnet 5.5 (recommended)",
          "claude-haiku-5-5": "Claude Haiku 5.5 (cheapest)",
          "claude-opus-5-5": "Claude Opus 5.5 (smartest, costs more)"}

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
    args.focus = params.get("focus") or ""
    args.from_link = link or None
    args.gear = _as_list_text(params.get("gear"))
    args.keep = _as_list_text(params.get("keep"))
    args.powders = params.get("powders") or ""
    args.tomes = _as_list_text(params.get("tomes"))
    args.aspects = _as_list_text(params.get("aspects"))
    args.enemy = params.get("enemy") or ""
    args.sliders = _as_list_text(params.get("sliders"))
    args.toggles = _as_list_text(params.get("toggles"))
    args.toggles_off = _as_list_text(params.get("toggles_off"))
    args.buffs = _as_list_text(params.get("buffs"))
    args.raid_buffs = _as_list_text(params.get("raid_buffs"))
    args.drops_only = bool(params.get("drops_only"))
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
    if ok:
        STATE["last_args"], STATE["last_report"] = used, text
    if load_config().get("popup", True):
        notify_done(ok, args, text)
    return {"ok": ok, "text": text, "args": used}


GOAL_SHORT = {"spells": "spell damage", "burst": "burst combo", "melee": "highest DPS", "survival": "survival",
              "balanced": "damage + survival", "ability": "one ability", "healing": "healing"}


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
        m = _re.search(r"\+(\d+)% for your goal compared with what you wear now", text)
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
            lines.append(f"Major ID {m}: {desc}")
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


# ============================================================================== Claude

SYSTEM_PROMPT = """You are Wynn Build Helper, a friendly expert at Wynncraft builds, talking with a player inside a desktop app.

You have a calculator (the analyze_build tool) that uses Wynnbuilder's exact formulas to work out the best ability tree, skill points, powders, gear upgrades, tomes, aspects and a level-by-level plan. Use it for every number and recommendation. Never make up item stats, ability tree paths or damage numbers; if you're not sure, look them up with the tools.

How to work:
- To run the calculator you need the class and combat level, or a Wynnbuilder link. If either is missing, ask for it in one short question. Ask about gear too, but if they don't know or don't care, run without it.
- Baseline stats: the first time they ask for a build (or a new goal), ask whether they want any minimums kept, for example "Any baseline stats you want to keep, like at least 20,000 health or no slower walk speed?". Put it in the same message as any other question you need to ask; if you have everything else, ask it on its own and wait. If they say no or don't care, run without minimums and don't ask again for follow-ups. Pass what they say as minimums (health, ehp, walk_speed in %, mana_regen, health_regen, elemental defenses). "No negative walk speed" means walk_speed 0.
- Some abilities only give their bonus while a toggle is on. Bullwhip (Shaman) counts as on by default, since a summoner keeps enemies Whipped with Uproot or Haunting Memory; pass toggles_off if the player says they don't. Other toggles are off unless the player says they keep that buff up (then pass toggles); the report's "Toggles and sliders" section shows what each one would add, so mention a big one.
- If no build meets every minimum, the report says so and shows the closest one; tell them which minimum couldn't be met and suggest relaxing it.
- The report shows health, effective HP and walk speed for the best build next to the current gear; mention any big drop in health or walk speed so they can set a minimum.
- If they want the most damage from one specific ability ("maximum puppet damage", "biggest Meteor", "strongest Bash"), use goal ability with focus set to that ability.
- Otherwise pick the goal from what they say: melee whenever they ask for "DPS" or "highest DPS" (this is exactly Wynnbuilder's Average DPS: main attack damage per hit x attack speed, which is what players compare), spells only when they talk about spell damage or casting, burst for one big combo, survival (tanky / keeps dying), balanced (damage and survival), healing (support). If unclear, use melee for "damage/DPS". If they keep dying to a particular boss or element, pass that as the enemy hit.
- Pass item and ability names exactly as the player wrote them; the calculator corrects spelling and says so. "Mana bank" means the Mage ability Arcane Transfer.
- Crafted items are written as codes starting with CR- (and custom items CI-); pass them as gear.
- Deep search is the default and takes several minutes. Only set deep_search to false if they ask for something quick, or for small follow-up questions where a rough answer is enough.
- Think about the build as a whole, never as the best item per slot. The report's "Best whole build" section is the calculator choosing every piece together with the rest of the build on (skill points items give and need from each other, set bonuses, elements and powders, major IDs, the ability tree re-planned for that gear). Lead with that when the player wants a new build or upgrades, and use the one-slot list only for a single quick swap.
- Explain the synergy in plain words: why these pieces work together, for example "Ajax's +13 Strength covers the Strength the chestplate and leggings need, which frees points for Dexterity", "two Corrupted pieces turn on the set bonus", "every piece adds air damage, matching the air powders and Wind Prison". Check item details with lookup_item when you explain, so what you say is true. If a piece is there mainly to enable others, say so.
- Match the build to the play style they describe. A DPS or melee build is judged on Wynnbuilder's Average DPS (goal melee: damage per hit x attack speed, so attack speed matters as much as damage); a spell build on spell damage within its mana; if they want both damage and to stay alive, use balanced. If the best whole build trades away something they care about (for example less health, or a new element), point it out and offer the alternative.
- After a report, the player sees the full report in the panel next to the chat, so don't repeat it. Reply with a short plain summary: the build as a whole and why it fits together, then the 3 to 5 most useful things to do (skill points, what to click next in the tree, powders), plus anything surprising. Use short paragraphs or a few bullets, under about 220 words unless they ask for detail.
- When they change something ("I'm level 50 now", "make it tankier", "what if I use X"), run the calculator again with the change and say what's different.
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
                "focus": {"type": "string",
                          "description": "For goal 'ability': the one ability to maximise, as the player said it, "
                                         "e.g. 'puppets', 'Meteor', 'Bash', 'Totem'"},
                "gear": {"type": "array", "items": {"type": "string"},
                         "description": "Items they wear, any spelling; crafted/custom items as CR-/CI- codes"},
                "keep": {"type": "array", "items": {"type": "string"},
                         "description": "Abilities they want no matter what"},
                "wynnbuilder_link": {"type": "string", "description": "A wynnbuilder.github.io/builder link"},
                "powders": {"type": "string",
                            "description": 'Powders already slotted, e.g. "weapon=t6 t6; chestplate=w4 w4". Leave the '
                                           'weapon out to get a recommendation.'},
                "max_powder_tier": {"type": "integer", "minimum": 1, "maximum": 7},
                "tomes": {"type": "array", "items": {"type": "string"}},
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
                        "earth_defense": {"type": "number"}, "thunder_defense": {"type": "number"},
                        "water_defense": {"type": "number"}, "fire_defense": {"type": "number"},
                        "air_defense": {"type": "number"},
                    },
                },
                "sliders": {"type": "string", "description": 'Ability stack sliders, e.g. "Distortion=40"'},
                "toggles": {"type": "string",
                            "description": "Ability toggles to count as on, e.g. 'Activate Aerial Ace' (the player keeps "
                                           "that buff up). The report lists each toggle and what it would add."},
                "toggles_off": {"type": "string",
                                "description": "Toggles that are on by default to turn off, e.g. 'Activate Bullwhip' "
                                               "if the player doesn't keep enemies Whipped"},
                "buffs": {"type": "array", "items": {"type": "string", "enum": list(wh.BUFFS)}},
                "raid_buffs": {"type": "array", "items": {"type": "string"}},
                "drops_only": {"type": "boolean", "description": "Only suggest items that drop from mobs, chests or dungeons"},
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


def call_claude(messages: list, model: str) -> dict:
    key = api_key()
    body = json.dumps({"model": model, "max_tokens": 1500, "system": SYSTEM_PROMPT, "tools": TOOLS,
                       "messages": messages}).encode("utf-8")
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
    history.append({"role": "user", "content": user_text})
    reports = []
    try:
        for _ in range(8):
            resp = call_claude(history, model)
            content = resp.get("content", [])
            history.append({"role": "assistant", "content": content})
            if resp.get("stop_reason") != "tool_use":
                text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                return {"reply": text or "(no reply)", "reports": reports, "args": STATE["last_args"]}
            results = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                name, inp = block.get("name"), block.get("input") or {}
                if name == "analyze_build":
                    out = run_report(inp)
                    if out["ok"]:
                        reports.append(out["text"])
                    result = out["text"]
                elif name == "lookup_item":
                    result = item_summary(inp.get("name", ""))
                elif name == "list_abilities":
                    result = ability_list(inp.get("class", ""))
                else:
                    result = "Unknown tool."
                results.append({"type": "tool_result", "tool_use_id": block["id"], "content": result[:60000]})
            history.append({"role": "user", "content": results})
        return {"reply": "That took more steps than expected. Try asking in a simpler way.", "reports": reports}
    except ClaudeError as exc:
        del history[start:]             # forget the unanswered turn so the conversation stays valid
        return {"reply": str(exc), "error": True, "reports": reports}


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
                "popup": cfg.get("popup", True), "app_version": updater.current_version().get("version", "?"),
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
        if path == "/api/update/check":
            return self._send(200, updater.check())
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
            if "auto_update" in body:
                cfg["auto_update"] = bool(body["auto_update"])
            save_config(cfg)
            return self._send(200, {"ok": True, "has_key": bool(api_key())})
        if self.path == "/api/update/install":
            if RUN_LOCK.locked():
                return self._send(200, {"ok": False, "error": "A build is still being worked out. Install the update when it's done."})
            with RUN_LOCK:
                return self._send(200, updater.install(body.get("version")))
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
