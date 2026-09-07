from __future__ import annotations

from app.reports.matrix import build_matrix, build_table


def test_units_by_dates_pivot_includes_empty_cell_and_column_sums() -> None:
    grid = build_matrix(
        ["day", "unitName", "sales"],
        [
            {"day": "2026-06-01", "unitName": "Москва 4-3", "sales": 235972},
            {"day": "2026-06-02", "unitName": "Москва 4-3", "sales": 215717},
            {"day": "2026-06-01", "unitName": "Смоленск-1", "sales": 100},
        ],
        {"sales": 451789},
    )

    assert grid is not None
    assert grid[0] == ["", "01.06.2026", "02.06.2026"]
    assert grid[1] == ["Москва 4-3", 235972, 215717]
    assert grid[2] == ["Смоленск-1", 100, ""]
    assert grid[3] == ["Итого", 236072, 215717]


def test_period_end_column_uses_aggregator_total() -> None:
    grid = build_matrix(
        ["unitName", "sales"],
        [{"unitName": "Москва 4-3", "sales": 100}],
        {"sales": 100},
        period_label="2026-06-01 — 2026-06-30",
    )

    assert grid is not None
    assert grid[0] == ["", "30.06.2026"]
    assert grid[1] == ["Москва 4-3", 100]
    assert grid[2] == ["Итого", 100]


def test_two_metrics_keep_separate_rows() -> None:
    grid = build_matrix(
        ["day", "unitName", "sales", "orders_count"],
        [
            {
                "day": "2026-06-01",
                "unitName": "Москва 4-3",
                "sales": 100,
                "orders_count": 2,
            }
        ],
        {"sales": 100, "orders_count": 2},
    )

    assert grid is not None
    assert grid[1] == ["Москва 4-3 / sales", 100]
    assert grid[2] == ["Москва 4-3 / orders_count", 2]
    assert grid[3] == ["Итого / sales", 100]
    assert grid[4] == ["Итого / orders_count", 2]


def test_build_table_without_unit_columns_stays_long() -> None:
    table = build_table(
        ["day", "sales"],
        [{"day": "2026-06-01", "sales": 100}],
        {"sales": 100},
    )

    assert table[0] == ["day", "sales"]
    assert table[1] == ["2026-06-01", 100]
    assert table[2][0] == "Итого"
    assert table[2][1] == 100
