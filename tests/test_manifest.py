"""Tests for the integration's declared metadata.

hacs.json advertises a minimum Home Assistant version to every HACS user, and
that number decides who HACS lets install the integration at all. It is an API
claim, not a tested one: it names the oldest release that has every core API
the code imports. The current floor is OptionsFlowWithReload, added in 2025.8 —
see the docstring on BrunataOptionsFlowHandler in config_flow.py, which is where
the reason lives so it cannot drift away from the code that sets it.

Testing against that floor was tried and dropped. Installing a year-old Home
Assistant from PyPI today pulls newer releases of its loosely pinned indirect
dependencies, which breaks the environment rather than the integration; users
run Home Assistant in a container with everything locked, so the failures were
about PyPI, not about this code. CI therefore runs against the pin in
requirements_test.txt, with a scheduled job against the newest release.

What is left to check here is that the advertised minimum stays a floor and
never creeps above what is actually exercised.
"""

import json
from pathlib import Path

import pytest
from awesomeversion import AwesomeVersion
from homeassistant.const import __version__ as HA_VERSION

REPO_ROOT = Path(__file__).parent.parent
MANIFEST = json.loads(
    (REPO_ROOT / "custom_components" / "brunata" / "manifest.json").read_text()
)
HACS = json.loads((REPO_ROOT / "hacs.json").read_text())


def test_declared_minimum_is_not_newer_than_the_tested_version():
    """The advertised minimum must not be newer than what the suite runs on.

    The minimum is an API claim rather than a tested one, but it should still
    be a floor: promising a release newer than the one CI exercises means
    nobody has run the code on anything we support.

    If this fails, the fix is almost always to lower hacs.json back to the
    oldest release that has every core API the code imports. Raising the pin in
    requirements_test.txt to match instead would shut out every user below the
    new number, which is a real cost — see the module docstring.
    """
    declared = AwesomeVersion(HACS["homeassistant"])
    tested = AwesomeVersion(HA_VERSION)
    assert declared <= tested, (
        f"hacs.json promises Home Assistant >= {declared}, but the suite runs "
        f"against {tested}. The advertised minimum is newer than anything "
        f"tested."
    )


def test_no_external_requirements():
    """The Brunata client is vendored in api.py. Reintroducing an external
    dependency means reintroducing a third party who can break the integration
    at runtime — if it is ever added back, pin it exactly and say so here."""
    assert MANIFEST["requirements"] == []


def test_no_third_party_loggers_declared():
    """loggers exists to route a dependency's log output through Home
    Assistant's debug toggle. With no dependency, there is nothing to route."""
    assert "loggers" not in MANIFEST


def test_manifest_and_hacs_agree_on_the_name():
    assert MANIFEST["name"] == HACS["name"]


@pytest.mark.parametrize(
    "key",
    ["domain", "name", "version", "documentation", "issue_tracker", "codeowners"],
)
def test_manifest_has_required_keys(key):
    assert MANIFEST.get(key), f"manifest.json is missing {key}"


def test_manifest_keys_are_in_the_order_hassfest_expects():
    """domain and name first, everything else alphabetical."""
    keys = list(MANIFEST)
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
