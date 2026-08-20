"""Tests for the integration's declared metadata.

hacs.json advertises a minimum Home Assistant version to every HACS user, but
CI only ever runs against the single version pytest-homeassistant-custom-
component pins. Nothing connected the two, so the declared minimum was an
untested claim: if it drifted above what the suite runs on, users on the
version we promise to support would install a build that had never been
executed against their Home Assistant.
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
    """The suite must actually run on the oldest version we claim to support.

    If this fails, either lower hacs.json to a version CI covers, or add a CI
    job pinning the matching pytest-homeassistant-custom-component release —
    see .github/workflows/pytest.yml.
    """
    declared = AwesomeVersion(HACS["homeassistant"])
    tested = AwesomeVersion(HA_VERSION)
    assert declared <= tested, (
        f"hacs.json promises Home Assistant >= {declared}, but the test suite "
        f"only runs against {tested}. The promise is untested."
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
