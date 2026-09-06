"""Tests for the integration's declared metadata.

hacs.json advertises a minimum Home Assistant version to every HACS user, and
that number decides who HACS lets install the integration at all. It is an API
claim, not a tested one: it names the oldest release that has every core API
the code imports.

The declared number is 2025.3. Two core APIs hold it there, and both were
established by running the suite against the releases in question rather than
by reading release notes.

The first is AddConfigEntryEntitiesCallback in sensor.py. 2025.2 fails at
import with

    ImportError: cannot import name 'AddConfigEntryEntitiesCallback'
    from 'homeassistant.helpers.entity_platform'

while 2025.3 and 2025.4 pass in full. The previous number, 2025.8, was
OptionsFlowWithReload — that class went with the options flow, and the number
outlived the reason for it.

The second is Entity.device_entry, which sensor.py reads to find a meter's
device. The same two releases were run again for it: green on 2025.3.0, red on
2025.2.0, including the two tests that touch the attribute directly.

Declaring a floor that is too high only costs installability; declaring one
that is too low breaks setup for anyone who takes it at its word.

The floor is measured by hand, on demand, and is deliberately not a standing CI
job. Measuring it works: 2025.3.0 installs from PyPI and the suite runs green on
it under Python 3.13, which is where both readings above come from. What does
not work is sweeping the whole range — 2025.5 through 2025.8 each install and
then die on

    AttributeError: module 'pycares' has no attribute 'ares_query_a_result'

which is a PyPI resolution problem rather than a fault in this integration.
Users run Home Assistant in a container with everything locked, so those
failures say nothing about the code, and a job that is red for reasons outside
the repository teaches people to ignore it. Those four releases therefore stay
unmeasured, and that is not a hole in the claim: the floor is the *oldest*
release that works, and that one was measured directly.

CI runs against the pin in requirements_test.txt instead, with a scheduled job
against the newest release. That pin is deliberately not written out here:
Dependabot bumps it weekly, and a number restated in prose goes stale at the
next bump with no test to catch it. Read requirements_test.txt for the current
one. It is years newer than anything in 2025 and says nothing about the floor.

To repeat the measurement — after a floor change, or after a new import from
homeassistant appears — a throwaway workflow that installs a pinned
homeassistant on Python 3.13 and runs pytest is the whole of it. Delete it again
afterwards rather than leaving it in the Actions list.

What is left to check here is that the advertised minimum stays a floor and
never creeps above what is actually exercised.

Note what that does *not* cover. The test below compares the declared minimum
against the version the suite runs on, so it fails when the number is too
high. Nothing fails when it is too low: a commit that starts importing a core
API introduced after 2025.3 would leave hacs.json promising a release the code
can no longer run on, and CI would stay green because it tests a far newer
Home Assistant. Catching that automatically would mean the standing job this
docstring explains the absence of. So it is a review question: when a new import
from homeassistant appears, look up which release introduced it — read it, do
not infer it from dates — then either measure it as above, or raise both
hacs.json and this docstring if it is newer than the number above.
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
