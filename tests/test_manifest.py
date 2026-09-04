"""Tests for the integration's declared metadata.

hacs.json advertises a minimum Home Assistant version to every HACS user, and
that number decides who HACS lets install the integration at all. It is an API
claim, not a tested one: it names the oldest release that has every core API
the code imports.

The declared number is 2025.3, and the API that sets it is
AddConfigEntryEntitiesCallback in sensor.py. That was established by running
the suite against each monthly release in turn rather than by reading release
notes: 2025.2 fails at import with

    ImportError: cannot import name 'AddConfigEntryEntitiesCallback'
    from 'homeassistant.helpers.entity_platform'

while 2025.3 and 2025.4 pass in full. The previous number, 2025.8, was
OptionsFlowWithReload — that class went with the options flow, and the number
outlived the reason for it.

Declaring a floor that is too high only costs installability; declaring one
that is too low breaks setup for anyone who takes it at its word.

2025.5 through 2025.8 could not be measured the same way: each one installed,
then died on import with

    AttributeError: module 'pycares' has no attribute 'ares_query_a_result'

which is the PyPI problem described below, not a fault in the integration.
They stay unmeasured, and that is not a hole in the claim: the floor is the
*oldest* release that works, and that one was measured directly. The pin in
requirements_test.txt, which CI runs green on every push, is Home Assistant
2026.8 — it says nothing about any release in 2025.

Testing against that floor was tried and dropped. Installing a year-old Home
Assistant from PyPI today pulls newer releases of its loosely pinned indirect
dependencies, which breaks the environment rather than the integration; users
run Home Assistant in a container with everything locked, so the failures were
about PyPI, not about this code. CI therefore runs against the pin in
requirements_test.txt, with a scheduled job against the newest release.

What is left to check here is that the advertised minimum stays a floor and
never creeps above what is actually exercised.

Note what that does *not* cover. The test below compares the declared minimum
against the version the suite runs on, so it fails when the number is too
high. Nothing fails when it is too low: a commit that starts importing a core
API introduced after 2025.3 would leave hacs.json promising a release the code
can no longer run on, and CI would stay green because it tests a far newer
Home Assistant. Catching that automatically would mean running the suite
against the floor, which is the arrangement described above and abandoned for
good reason. So it is a review question: when a new import from homeassistant
appears, look up which release introduced it — read it, do not infer it from
dates — and raise both hacs.json and this docstring if it is newer than the
number above.
"""

import json
from pathlib import Path

import pytest
from awesomeversion import AwesomeVersion
from homeassistant.const import __version__ as HA_VERSION

from custom_components.brunata.api import ISSUE_TRACKER_URL

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


def test_the_issue_tracker_url_is_the_one_the_manifest_declares():
    """api.py prints this URL in two log lines asking users to report a meter.

    It is the same address as manifest.json's issue_tracker, written out a
    second time — so if the repository ever moves, one of them follows and the
    other silently sends people to a 404. Nothing else would fail.
    """
    assert ISSUE_TRACKER_URL == MANIFEST["issue_tracker"]


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
