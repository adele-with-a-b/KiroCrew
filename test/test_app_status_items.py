"""Tests for app-contributed status items (``contributes.statusItems``).

A status item is drawn in the top bar on every page, so these tests weight the
install-time refusals: a malformed item must fail the install with a reason,
not install clean and never appear.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.manifest_status_items import (
    MAX_STATUS_ITEM_LABEL,
    MAX_STATUS_ITEMS_PER_APP,
)

BASE = {
    "name": "my-app",
    "version": "1.0.0",
    "displayName": "My App",
    "description": "d",
}

GOOD = {"id": "disk", "label": "Disk free", "icon": "Database", "statusPath": "status/disk"}


def _mf(items, **contributes) -> AppManifest:
    return AppManifest.from_dict({**BASE, "contributes": {"statusItems": items, **contributes}})


def _status_errors(m: AppManifest) -> list[str]:
    return [e for e in m.validate() if "status" in e.lower()]


class TestParsing:
    def test_parses_an_item(self):
        m = _mf([GOOD])
        (item,) = m.contributes.statusItems
        assert (item.id, item.label, item.icon, item.statusPath) == (
            "disk",
            "Disk free",
            "Database",
            "status/disk",
        )

    def test_absent_is_empty_and_omitted_from_output(self):
        m = AppManifest.from_dict(BASE)
        assert m.contributes.statusItems == []
        assert "statusItems" not in m.to_dict().get("contributes", {})

    def test_round_trip_preserves_items_and_normalizes_the_leading_slash(self):
        m = _mf([{**GOOD, "statusPath": "/status/disk"}])
        again = AppManifest.from_dict(m.to_dict())
        assert again.contributes.statusItems[0].statusPath == "status/disk"
        assert again.contributes.statusItems[0].to_dict() == GOOD

    def test_icon_is_optional_and_omitted_when_unset(self):
        item = {k: v for k, v in GOOD.items() if k != "icon"}
        m = _mf([item])
        assert _status_errors(m) == []
        assert "icon" not in m.contributes.statusItems[0].to_dict()

    def test_a_non_dict_entry_is_kept_so_validate_refuses_it(self):
        m = _mf(["disk"])
        assert len(m.contributes.statusItems) == 1
        assert any("missing required field: id" in e for e in m.validate())

    def test_a_non_list_value_is_refused_not_silently_empty(self):
        m = _mf({"id": "disk"})
        assert m.contributes.statusItems == []
        assert any("statusItems must be an array" in e for e in m.validate())


class TestValidation:
    def test_a_well_formed_pair_validates_clean(self):
        m = _mf([GOOD, {**GOOD, "id": "sync", "label": "Last sync", "icon": "Shield"}])
        assert _status_errors(m) == []

    @pytest.mark.parametrize("field", ["id", "label", "statusPath"])
    def test_required_fields(self, field):
        m = _mf([{**GOOD, field: ""}])
        assert any(f"missing required field: {field}" in e for e in m.validate())

    def test_a_blank_label_is_missing(self):
        m = _mf([{**GOOD, "label": "   "}])
        assert any("missing required field: label" in e for e in m.validate())

    @pytest.mark.parametrize("bad", ["Disk", "disk_1", "-disk", "disk-", "di sk"])
    def test_id_must_be_kebab_case(self, bad):
        assert any("kebab-case" in e for e in _mf([{**GOOD, "id": bad}]).validate())

    def test_duplicate_ids_are_refused(self):
        m = _mf([GOOD, {**GOOD, "statusPath": "status/other"}])
        assert any("duplicated" in e for e in m.validate())

    def test_per_app_cap(self):
        items = [{**GOOD, "id": f"i{n}"} for n in range(MAX_STATUS_ITEMS_PER_APP + 1)]
        assert any("at most" in e for e in _mf(items).validate())
        assert not any("at most" in e for e in _mf(items[:-1]).validate())

    @pytest.mark.parametrize("bad", [None, 5, ["Disk"]])
    def test_a_non_string_label_is_missing_not_stringified(self, bad):
        m = _mf([{**GOOD, "label": bad}])
        assert m.contributes.statusItems[0].label == ""
        assert any("missing required field: label" in e for e in m.validate())

    @pytest.mark.parametrize("bad", ["Disk\u202efree", "Disk\u0007", "Disk\u2028free"])
    def test_a_label_with_hidden_characters_is_refused(self, bad):
        assert any(
            "control, format or separator" in e for e in _mf([{**GOOD, "label": bad}]).validate()
        )

    def test_a_label_may_carry_the_joiners(self):
        assert _status_errors(_mf([{**GOOD, "label": "ডিস্ক\u200c"}])) == []

    def test_label_length_cap(self):
        m = _mf([{**GOOD, "label": "x" * (MAX_STATUS_ITEM_LABEL + 1)}])
        assert any("label is longer" in e for e in m.validate())

    def test_icon_name_length_cap(self):
        assert any(
            "icon name is too long" in e for e in _mf([{**GOOD, "icon": "X" * 41}]).validate()
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "https://evil.example/x",
            "//evilhost/x",
            "../other-app/status",
            "status?x=1",
            "Status/Disk",
            "status/disk\n",
            "x" * 65,
        ],
    )
    def test_refuses_anything_that_is_not_a_relative_backend_route(self, bad):
        assert any(
            "statusPath must be a relative backend route" in e
            for e in _mf([{**GOOD, "statusPath": bad}]).validate()
        )

    def test_a_bad_item_does_not_mask_a_session_control_error(self):
        m = _mf([{**GOOD, "id": ""}], sessionControls=[{"id": "x"}])
        errors = m.validate()
        assert any("status item" in e for e in errors)
        assert any("entryPoint" in e for e in errors)


class TestSigning:
    def test_a_status_item_only_manifest_signs_its_route(self):
        """Repointing a signed app's ``statusPath`` must change the signed bytes."""
        a = _mf([GOOD]).signing_payload()
        b = _mf([{**GOOD, "statusPath": "status/other"}]).signing_payload()
        assert a != b

    def test_no_status_items_keeps_the_payload_identical(self):
        assert AppManifest.from_dict(BASE).signing_payload() == _mf([]).signing_payload()
