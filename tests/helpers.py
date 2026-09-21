"""Shared test fixtures.

`make_config` builds a Config without going through a file, so a test can state
only the thresholds it cares about. It deliberately constructs the real frozen
dataclass rather than a stub dict: a test that passes against a stand-in for
the config proves nothing about the code that reads the real one.
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from flatsearch import config as config_mod  # noqa: E402

DEFAULTS = dict(
    min_bedrooms=2,
    max_bedrooms=0,
    min_bathrooms=2,
    budget_pcm=3000,
    furnishing="unfurnished",
    min_sqft=800,
    good_floor_from=2,
    lift_required_from_floor=3,
    aircon="preferred",
    districts_only=True,
    prime_districts=("w1", "sw1", "sw3", "nw8"),
    affluent_districts=("w2", "sw6", "n1"),
    fringe_districts=("e14", "se1"),
    move_in="",
    move_in_slack_days=7,
    stage2_cap=150,
    first_run_days=14,
    searches=(),
)


def make_config(tmpdir=None, **overrides):
    """A Config with sane defaults; pass only what the test is about."""
    values = dict(DEFAULTS)
    for key in ("prime_districts", "affluent_districts", "fringe_districts",
                "searches"):
        if key in overrides:
            overrides[key] = tuple(
                v.lower() if key != "searches" else v for v in overrides[key])
    values.update(overrides)
    root = pathlib.Path(tmpdir or tempfile.gettempdir())
    return config_mod.Config(
        path=root / "criteria.toml",
        data_dir=root / "data",
        **values)


def write_config(tmpdir, body: str) -> pathlib.Path:
    """Write a criteria.toml and return its path."""
    p = pathlib.Path(tmpdir) / "criteria.toml"
    p.write_text(body, encoding="utf-8")
    return p


MINIMAL_TOML = """
[requirements]
budget_pcm = 3000
"""
