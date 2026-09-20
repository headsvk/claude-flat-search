"""Typed configuration, loaded and validated from criteria.toml.

This replaces a markdown file that a bespoke parser scraped `KEY=value` lines
out of. Markdown bought one real thing - the rationale for a threshold sat next
to the threshold - and cost three:

  * every value was a string, so the code carried cfg_int/cfg_list/truthy
    helpers purely to undo that;
  * a typo'd key was silently a default rather than an error;
  * search URLs were regex-scraped from the whole file, so a URL written in
    prose as an illustration became a live search. That one actually happened.

TOML keeps the comments (so the rationale still travels with the values), adds
real types, and puts the URLs in a table where they cannot be confused with
prose. `tomllib` is in the standard library from Python 3.11.

Validation is deliberately strict and happens once, at load: an unknown key is
an error rather than a silent default, because a threshold that quietly reverts
to a default filters the wrong listings for weeks without anything looking
wrong.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
import tomllib

DEFAULT_CONFIG_NAME = "criteria.toml"

FURNISHINGS = ("furnished", "unfurnished", "either")
AIRCON_MODES = ("preferred", "required", "ignored")

# section -> {key: (type, default)}. A default of None means the key is required.
SCHEMA: dict[str, dict[str, tuple]] = {
    "requirements": {
        "min_bedrooms": (int, 0),
        "max_bedrooms": (int, 0),
        "min_bathrooms": (int, 0),
        "budget_pcm": (int, None),
    },
    "preferences": {
        "furnishing": (str, "either"),
        "min_sqft": (int, 0),
        "good_floor_from": (int, 2),
        "lift_required_from_floor": (int, 3),
        "aircon": (str, "preferred"),
    },
    "districts": {
        "only": (bool, False),
        "prime": (list, []),
        "affluent": (list, []),
        "fringe": (list, []),
    },
    "dates": {
        "move_in": (str, ""),
        "move_in_slack_days": (int, 7),
    },
    "run": {
        "stage2_cap": (int, 150),
        "data_dir": (str, "data"),
    },
}


class ConfigError(Exception):
    """Raised with a message meant to be shown to a person, not a traceback."""


@dataclasses.dataclass(frozen=True)
class Config:
    path: pathlib.Path
    data_dir: pathlib.Path

    min_bedrooms: int
    max_bedrooms: int
    min_bathrooms: int
    budget_pcm: int

    furnishing: str
    min_sqft: int
    good_floor_from: int
    lift_required_from_floor: int
    aircon: str

    districts_only: bool
    prime_districts: tuple
    affluent_districts: tuple
    fringe_districts: tuple

    move_in: str
    move_in_slack_days: int

    stage2_cap: int
    searches: tuple

    # -- derived paths. Everything a run writes lives under data_dir, so the
    #    repo stays source-only and the whole search is one folder to back up.
    @property
    def state_path(self) -> pathlib.Path:
        return self.data_dir / "state.json"

    @property
    def cache_dir(self) -> pathlib.Path:
        return self.data_dir / "cache"

    @property
    def runs_dir(self) -> pathlib.Path:
        return self.data_dir / "runs"

    @property
    def daily_dir(self) -> pathlib.Path:
        return self.data_dir / "daily"

    @property
    def shortlist_path(self) -> pathlib.Path:
        return self.data_dir / "shortlist.md"

    @property
    def decisions_path(self) -> pathlib.Path:
        return self.data_dir / "decisions.md"

    def tier_of(self, district: str | None) -> int | None:
        """0 prime, 1 affluent, 2 fringe, None unlisted.

        Addresses sometimes carry the full outward code rather than the
        district: "Sloane Street, London, SW1X" not "SW1". Without the stem
        fallback, SW1X / W1K / WC2R - Belgravia, Mayfair, Covent Garden - miss
        the prime tier entirely and rank as unknown.
        """
        if not district:
            return None
        d = district.strip().lower()
        stem = re.match(r"^([a-z]{1,2}\d{1,2})[a-z]$", d)
        candidates = [d] + ([stem.group(1)] if stem else [])
        for listed, tier in ((self.prime_districts, 0),
                             (self.affluent_districts, 1),
                             (self.fringe_districts, 2)):
            for c in candidates:
                if c in listed:
                    return tier
        return None


def _districts(values, section: str, key: str) -> tuple:
    out = []
    for v in values:
        if not isinstance(v, str):
            raise ConfigError("[%s] %s must be a list of strings, got %r"
                              % (section, key, v))
        text = v.strip().lower()
        if text:
            out.append(text)
    return tuple(out)


def _searches(raw: dict) -> tuple:
    """[searches] is a table of portal -> list of URLs.

    A table, rather than URLs scraped out of the file, is the whole point: a URL
    here is unambiguously a search, and a URL written anywhere else is prose.
    """
    if not isinstance(raw, dict):
        raise ConfigError("[searches] must be a table of portal = [urls]")
    out = []
    for portal, urls in raw.items():
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list):
            raise ConfigError("[searches] %s must be a URL or a list of URLs" % portal)
        for u in urls:
            if not isinstance(u, str) or not u.startswith("http"):
                raise ConfigError("[searches] %s: %r is not a URL" % (portal, u))
            if u not in out:
                out.append(u)
    return tuple(out)


def load(path: str | pathlib.Path | None = None) -> Config:
    path = pathlib.Path(path or DEFAULT_CONFIG_NAME)
    if not path.exists():
        raise ConfigError(
            "config not found: %s\n"
            "Copy examples/criteria.example.toml to %s and edit it."
            % (path, DEFAULT_CONFIG_NAME))
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("%s is not valid TOML: %s" % (path, exc)) from None

    unknown_sections = set(raw) - set(SCHEMA) - {"searches"}
    if unknown_sections:
        raise ConfigError("unknown section(s) in %s: %s"
                          % (path, ", ".join(sorted(unknown_sections))))

    values: dict = {}
    for section, keys in SCHEMA.items():
        block = raw.get(section, {})
        if not isinstance(block, dict):
            raise ConfigError("[%s] must be a table" % section)
        unknown = set(block) - set(keys)
        if unknown:
            raise ConfigError(
                "unknown key(s) in [%s]: %s\nKnown keys: %s"
                % (section, ", ".join(sorted(unknown)), ", ".join(sorted(keys))))
        for key, (kind, default) in keys.items():
            if key not in block:
                if default is None:
                    raise ConfigError("[%s] %s is required" % (section, key))
                values["%s.%s" % (section, key)] = default
                continue
            value = block[key]
            # bool is a subclass of int; do not let true pass as a number.
            if kind is int and (isinstance(value, bool) or not isinstance(value, int)):
                raise ConfigError("[%s] %s must be a whole number, got %r"
                                  % (section, key, value))
            if kind is not int and not isinstance(value, kind):
                raise ConfigError("[%s] %s must be %s, got %r"
                                  % (section, key, kind.__name__, value))
            values["%s.%s" % (section, key)] = value

    furnishing = str(values["preferences.furnishing"]).strip().lower()
    if furnishing not in FURNISHINGS:
        raise ConfigError("[preferences] furnishing must be one of %s"
                          % ", ".join(FURNISHINGS))
    aircon = str(values["preferences.aircon"]).strip().lower()
    if aircon not in AIRCON_MODES:
        raise ConfigError("[preferences] aircon must be one of %s"
                          % ", ".join(AIRCON_MODES))

    # Every numeric key, not a subset of them. The list used to skip
    # max_bedrooms, the two floor thresholds and move_in_slack_days, so a typo'd
    # minus sign on any of those passed validation and then quietly warped the
    # ranking - which is the exact class of silent-wrong-answer this config
    # module exists to make impossible.
    for section, keys in SCHEMA.items():
        for key, (kind, _) in keys.items():
            if kind is int and values["%s.%s" % (section, key)] < 0:
                raise ConfigError("[%s] %s cannot be negative" % (section, key))
    if values["requirements.budget_pcm"] == 0:
        raise ConfigError("[requirements] budget_pcm must be greater than 0")
    if values["requirements.max_bedrooms"] and (
            values["requirements.max_bedrooms"] < values["requirements.min_bedrooms"]):
        raise ConfigError(
            "[requirements] max_bedrooms (%d) is below min_bedrooms (%d), so "
            "nothing can ever match" % (values["requirements.max_bedrooms"],
                                        values["requirements.min_bedrooms"]))

    # Validated with the SAME parser that consumes it, never a second one of
    # its own: a config that accepts a date the ranking cannot read is worse
    # than one that rejects it. An unreadable date raises nowhere downstream -
    # `base_tier` just skips the whole availability check - so a typo'd month
    # silently stops date-ranking altogether, and a flat available half a year
    # late comes back High with nothing said about it. Empty stays legal and
    # means the timing is flexible.
    from . import core
    move_in = str(values["dates.move_in"]).strip()
    if move_in:
        parsed = core.parse_date(move_in)
        if parsed is None:
            raise ConfigError(
                "[dates] move_in is not a date this can read: %r\n"
                "Use YYYY-MM-DD, e.g. 2026-12-01. Also accepted: "
                "'1 December 2026', '1 Dec 2026', '01/12/2026'.\n"
                "Leave it as \"\" if your timing is flexible." % move_in)
        move_in = parsed.isoformat()   # one spelling from here on

    data_dir = pathlib.Path(values["run.data_dir"]).expanduser()
    if not data_dir.is_absolute():
        data_dir = (path.parent / data_dir).resolve()

    return Config(
        path=path,
        data_dir=data_dir,
        min_bedrooms=values["requirements.min_bedrooms"],
        max_bedrooms=values["requirements.max_bedrooms"],
        min_bathrooms=values["requirements.min_bathrooms"],
        budget_pcm=values["requirements.budget_pcm"],
        furnishing=furnishing,
        min_sqft=values["preferences.min_sqft"],
        good_floor_from=values["preferences.good_floor_from"],
        lift_required_from_floor=values["preferences.lift_required_from_floor"],
        aircon=aircon,
        districts_only=values["districts.only"],
        prime_districts=_districts(values["districts.prime"], "districts", "prime"),
        affluent_districts=_districts(values["districts.affluent"], "districts", "affluent"),
        fringe_districts=_districts(values["districts.fringe"], "districts", "fringe"),
        move_in=move_in,
        move_in_slack_days=values["dates.move_in_slack_days"],
        stage2_cap=values["run.stage2_cap"],
        searches=_searches(raw.get("searches", {})),
    )
