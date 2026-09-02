"""§ WP9 — instrument-directory source registry: fail-closed + license conformance (adversarial).

The registry must declare provenance without ever claiming coverage: every source is unavailable and
unusable until explicitly activated, an unclear/blocked license is never usable, and each entry documents
its origin, regions, venues and asset classes.
"""
from __future__ import annotations

from atp.instruments.sources import (
    InstrumentSourceLicense,
    InstrumentSourceType,
    seed_sources,
    source_by_id,
)


def test_every_declared_source_is_fail_closed():
    for s in seed_sources():
        assert s.available is False, f"{s.source_id} must default to available=False (fail-closed)"
        assert s.usable is False, f"{s.source_id} must not be usable until activated"
        assert s.status in ("MISSING", "BLOCKED")


def test_unknown_or_blocked_license_is_never_usable():
    for s in seed_sources():
        if s.license_status in (InstrumentSourceLicense.UNKNOWN.value, InstrumentSourceLicense.BLOCKED.value):
            assert s.usable is False
        if s.blocked_reason:
            assert s.status == "BLOCKED"


def test_provenance_and_metadata_documented():
    for s in seed_sources():
        assert s.provenance_url.startswith("http"), f"{s.source_id} needs a documented provenance URL"
        assert s.name and s.source_type in {t.value for t in InstrumentSourceType}
        assert s.regions, f"{s.source_id} must document its regions"
        assert s.asset_classes, f"{s.source_id} must document its asset classes"


def test_apac_directories_are_blocked_pending_license():
    for sid in ("jpx_listed_issues", "asx_listed", "hkex_listed", "sgx_listed"):
        s = source_by_id(sid)
        assert s is not None and s.status == "BLOCKED" and s.blocked_reason
        assert s.usable is False


def test_firds_and_sec_are_declared_but_missing_until_activated():
    firds = source_by_id("esma_firds")
    assert firds is not None and firds.status == "MISSING"       # license documented, no provider attached yet
    assert firds.license_status == InstrumentSourceLicense.ATTRIBUTION_REQUIRED.value
    assert firds.attribution_required is True and firds.redistribution_allowed is False
    sec = source_by_id("sec_company_tickers")
    assert sec is not None and sec.license_status == InstrumentSourceLicense.PUBLIC_DOMAIN.value
    assert sec.status == "MISSING"                                # still not usable — activation is separate


def test_source_ids_are_unique():
    ids = [s.source_id for s in seed_sources()]
    assert len(ids) == len(set(ids))


def test_ibkr_qualifier_declared_read_only_and_disabled():
    ib = source_by_id("ibkr_qualifier")
    assert ib is not None and ib.source_type == InstrumentSourceType.BROKER_QUALIFIER.value
    assert ib.available is False and ib.status == "BLOCKED"       # requires an entitled gateway; disabled here


def test_available_flag_cannot_override_a_block():
    # truthfulness: setting available=True on a BLOCKED source must NOT flip its status to AVAILABLE
    import dataclasses
    blocked = dataclasses.replace(source_by_id("jpx_listed_issues"), available=True)
    assert blocked.status == "BLOCKED" and blocked.usable is False
    unknown_lic = dataclasses.replace(source_by_id("esma_firds"), available=True,
                                      license_status=InstrumentSourceLicense.UNKNOWN.value)
    assert unknown_lic.status == "BLOCKED"                        # unknown license is blocked regardless
