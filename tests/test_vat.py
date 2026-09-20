from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.reports.vat import VatOptions, apply_vat_to_aggregated_data


def _metrics(*monetary: str) -> SimpleNamespace:
    return SimpleNamespace(is_monetary=lambda metric_id: metric_id in monetary)


def test_with_vat_adds_twenty_two_percent_to_monetary_values() -> None:
    rows = [{"unitName": "Unit", "sales": 1000, "orders_count": 5}]
    result = apply_vat_to_aggregated_data(
        rows,
        ["sales", "orders_count"],
        VatOptions(mode="with_vat", rate=22),
        _metrics("sales"),
    )

    assert result == [{"unitName": "Unit", "sales": 1220.0, "orders_count": 5}]
    assert rows[0]["sales"] == 1000


def test_with_vat_adds_ten_percent() -> None:
    result = apply_vat_to_aggregated_data(
        [{"sales": 100}],
        ["sales"],
        VatOptions(mode="with_vat", rate=10),
        _metrics("sales"),
    )

    assert result[0]["sales"] == 110.0


def test_without_vat_leaves_api_values_unchanged() -> None:
    rows = [{"sales": 1000}]
    result = apply_vat_to_aggregated_data(
        rows,
        ["sales"],
        VatOptions(mode="without_vat"),
        _metrics("sales"),
    )

    assert result is rows
    assert result[0]["sales"] == 1000


def test_missing_options_leave_values_unchanged() -> None:
    rows = [{"sales": 1000}]
    result = apply_vat_to_aggregated_data(rows, ["sales"], None, _metrics("sales"))

    assert result is rows


def test_non_monetary_metrics_are_not_scaled() -> None:
    result = apply_vat_to_aggregated_data(
        [{"orders_count": 8}],
        ["orders_count"],
        VatOptions(mode="with_vat", rate=22),
        _metrics("sales"),
    )

    assert result == [{"orders_count": 8}]


def test_with_vat_requires_supported_rate() -> None:
    with pytest.raises(ValueError, match="Invalid VAT rate"):
        VatOptions(mode="with_vat", rate=None)


def test_invalid_vat_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid VAT mode"):
        VatOptions(mode="maybe")


def test_none_and_non_numeric_values_are_left_unchanged() -> None:
    result = apply_vat_to_aggregated_data(
        [{"sales": None}, {"sales": "n/a"}, {"sales": 10}],
        ["sales"],
        VatOptions(mode="with_vat", rate=22),
        _metrics("sales"),
    )

    assert result[0]["sales"] is None
    assert result[1]["sales"] == "n/a"
    assert result[2]["sales"] == 12.2
