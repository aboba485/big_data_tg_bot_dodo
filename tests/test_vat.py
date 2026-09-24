from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.reports.vat import (
    VatOptions,
    apply_vat_to_aggregated_data,
    collapse_channel_rows,
    vat_rate_for_channel,
)


def _metrics(*monetary: str) -> SimpleNamespace:
    return SimpleNamespace(is_monetary=lambda metric_id: metric_id in monetary)


def test_delivery_rate_is_twelve_point_two() -> None:
    assert vat_rate_for_channel("Delivery") == 12.2


def test_dine_in_and_takeaway_use_fourteen_point_five() -> None:
    assert vat_rate_for_channel("Dine-in") == 14.5
    assert vat_rate_for_channel("Takeaway") == 14.5
    assert vat_rate_for_channel("Takeout") == 14.5


def test_with_vat_adds_delivery_rate() -> None:
    rows = [{"salesChannel": "Delivery", "sales": 1000, "orders_count": 5}]
    result = apply_vat_to_aggregated_data(
        rows,
        ["sales", "orders_count"],
        VatOptions(mode="with_vat"),
        _metrics("sales"),
    )

    assert result == [{"salesChannel": "Delivery", "sales": 1122.0, "orders_count": 5}]
    assert rows[0]["sales"] == 1000


def test_with_vat_adds_dine_in_rate() -> None:
    result = apply_vat_to_aggregated_data(
        [{"salesChannel": "Dine-in", "sales": 1000}],
        ["sales"],
        VatOptions(mode="with_vat"),
        _metrics("sales"),
    )

    assert result[0]["sales"] == 1145.0


def test_takeaway_uses_dine_in_rate() -> None:
    result = apply_vat_to_aggregated_data(
        [{"salesChannel": "Takeaway", "sales": 1000}],
        ["sales"],
        VatOptions(mode="with_vat"),
        _metrics("sales"),
    )

    assert result[0]["sales"] == 1145.0


def test_takeout_uses_dine_in_rate() -> None:
    result = apply_vat_to_aggregated_data(
        [{"salesChannel": "Takeout", "sales": 1000}],
        ["sales"],
        VatOptions(mode="with_vat"),
        _metrics("sales"),
    )

    assert result[0]["sales"] == 1145.0


def test_default_channel_is_used_when_row_has_no_channel() -> None:
    result = apply_vat_to_aggregated_data(
        [{"sales": 1000}],
        ["sales"],
        VatOptions(mode="with_vat"),
        _metrics("sales"),
        default_channel="Delivery",
    )

    assert result[0]["sales"] == 1122.0


def test_unknown_channel_is_left_unchanged() -> None:
    result = apply_vat_to_aggregated_data(
        [{"salesChannel": "Staff meal", "sales": 1000}],
        ["sales"],
        VatOptions(mode="with_vat"),
        _metrics("sales"),
    )

    assert result[0]["sales"] == 1000


def test_without_vat_leaves_api_values_unchanged() -> None:
    rows = [{"salesChannel": "Delivery", "sales": 1000}]
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
        [{"salesChannel": "Delivery", "orders_count": 8}],
        ["orders_count"],
        VatOptions(mode="with_vat"),
        _metrics("sales"),
    )

    assert result == [{"salesChannel": "Delivery", "orders_count": 8}]


def test_invalid_vat_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid VAT mode"):
        VatOptions(mode="maybe")


def test_none_and_non_numeric_values_are_left_unchanged() -> None:
    result = apply_vat_to_aggregated_data(
        [{"salesChannel": "Delivery", "sales": None}, {"salesChannel": "Delivery", "sales": "n/a"}],
        ["sales"],
        VatOptions(mode="with_vat"),
        _metrics("sales"),
    )

    assert result[0]["sales"] is None
    assert result[1]["sales"] == "n/a"


def test_collapse_sums_channel_vat_into_one_total() -> None:
    rows, columns = collapse_channel_rows(
        [
            {"unitName": "Unit", "salesChannel": "Delivery", "sales": 1122.0},
            {"unitName": "Unit", "salesChannel": "Dine-in", "sales": 1145.0},
        ],
        ["unitName", "salesChannel", "sales"],
        ["sales"],
    )

    assert columns == ["unitName", "sales"]
    assert rows == [{"unitName": "Unit", "sales": 2267.0}]
