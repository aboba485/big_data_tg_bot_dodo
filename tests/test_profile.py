from __future__ import annotations

import pytest

from app.config import PROJECT_ROOT, Settings, load_yaml
from app.documentation.loader import DocumentationLoader
from app.documentation.models import EndpointDocument, EndpointParameter, ResponseField
from app.documentation.repository import DocumentationRepository
from app.dodo.profile import ROOT_COLLECTION, ProfileResolver, derive_profile
from app.reports.metric_registry import MetricRegistry
from app.storage.sqlite import SQLiteDatabase

OVERRIDES = load_yaml(PROJECT_ROOT / "config/endpoint_overrides.yaml")


@pytest.fixture(scope="module")
def repository() -> DocumentationRepository:
    settings = Settings(_env_file=None)
    repo = DocumentationRepository(
        SQLiteDatabase(settings.sqlite_path),
        DocumentationLoader(settings.documentation_zip_path),
        allowed_operations=settings.allowed_operations,
        index_all_get=True,
    )
    repo.ensure_index()
    return repo


def _endpoint(name: str, **kwargs) -> EndpointDocument:
    defaults = dict(
        operation_id=name,
        api="",
        api_key="",
        title="",
        method="GET",
        path=f"/{name}",
        links=[],
        description="",
        scopes=[],
        deprecated=False,
        parameters=[],
        response_fields=[],
        success_response_schema=None,
        searchable_text="",
        compact_summary="",
    )
    defaults.update(kwargs)
    return EndpointDocument(**defaults)


@pytest.mark.parametrize("operation_id", sorted(OVERRIDES))
def test_derived_profile_matches_curated_overrides(repository, operation_id: str) -> None:
    """The heuristics must reproduce the hand-written config for the curated 11.

    These entries were verified against the live API, so they are the only
    ground truth available for judging the derivation rules.
    """
    endpoint = repository.get(operation_id)
    assert endpoint is not None
    profile = derive_profile(endpoint)
    expected = OVERRIDES[operation_id]

    if expected.get("date_mode") == "none":
        assert profile.date_mode == "none"
    else:
        assert profile.from_parameter == expected["from_parameter"]
        assert profile.to_parameter == expected["to_parameter"]
        assert profile.date_mode == expected["date_mode"]
    assert profile.pagination == expected["pagination"]
    if expected["pagination"]:
        assert profile.items_path == expected["items_path"]
        assert profile.end_flag_path == expected["end_flag_path"]
        assert profile.skip_parameter == expected["skip_parameter"]
        assert profile.take_parameter == expected["take_parameter"]


@pytest.mark.parametrize(
    ("operation_id", "collection"),
    [
        ("get-finances-sales-daily-units", "result"),
        ("get-finances-sales-period-units", "result"),
        ("get-delivery-statistics", "unitsStatistics"),
        ("get-staff-productivity", "productivityStatistics"),
        ("get-delivery-vouchers", "vouchers"),
        ("get-production-stop-sales-saleschannels", "stopSalesBySalesChannels"),
        ("get-production-stop-sales-statistics-ingredients", "stopSalesByIngredients"),
        ("get-production-unit-workload-by-orders", "unitWorkload"),
        ("get-production-unit-workload-by-products", "unitWorkload"),
        ("get-all-units", "units"),
        ("get-orders-client-statistics", ""),
    ],
)
def test_derived_collection_matches_metric_registry(
    repository, operation_id: str, collection: str
) -> None:
    endpoint = repository.get(operation_id)
    assert endpoint is not None
    assert derive_profile(endpoint).collection == collection


def test_metric_registry_collections_are_all_derivable(repository) -> None:
    registry = MetricRegistry()
    for metric_id, details in registry.all().items():
        endpoint = repository.get(details["operation_id"])
        assert endpoint is not None, metric_id
        assert derive_profile(endpoint).collection == details["collection"], metric_id


def test_every_indexed_endpoint_derives_without_error(repository) -> None:
    endpoints = repository.list()
    assert len(endpoints) > 90
    for endpoint in endpoints:
        profile = derive_profile(endpoint)
        assert profile.operation_id == endpoint.operation_id
        assert profile.date_mode in {"none", "date", "datetime", "datetime_hour"}
        assert not (profile.pagination and not profile.items_path)


def test_units_detected_for_most_endpoints(repository) -> None:
    with_units = [
        endpoint
        for endpoint in repository.list()
        if derive_profile(endpoint).units_parameter is not None
    ]
    assert len(with_units) > 60


def test_curated_override_wins_over_derivation(repository) -> None:
    resolver = ProfileResolver(OVERRIDES, default_max_period_days=31)
    endpoint = repository.get("get-finances-sales-daily-units")
    assert endpoint is not None
    assert derive_profile(endpoint).max_period_days == 31
    resolved = resolver.resolve(endpoint)
    assert resolved.max_period_days == 10
    assert resolved.curated is True


def test_uncurated_endpoint_still_resolves(repository) -> None:
    resolver = ProfileResolver(OVERRIDES, default_max_period_days=31)
    endpoint = repository.get("get-cancelled-sales")
    assert endpoint is not None
    profile = resolver.resolve(endpoint)
    assert profile.curated is False
    assert profile.has_period is True


def test_as_override_round_trips_executor_keys(repository) -> None:
    resolver = ProfileResolver(OVERRIDES)
    for operation_id, expected in OVERRIDES.items():
        endpoint = repository.get(operation_id)
        assert endpoint is not None
        override = resolver.resolve(endpoint).as_override()
        assert override["max_period_days"] == expected["max_period_days"]
        assert override["date_mode"] == expected["date_mode"]
        assert override["pagination"] == expected["pagination"]
        if expected["date_mode"] != "none":
            assert override["from_parameter"] == expected["from_parameter"]
            assert override["to_parameter"] == expected["to_parameter"]


def test_ambiguous_date_pairs_are_not_bound(repository) -> None:
    endpoint = repository.get("get-staff-members")
    assert endpoint is not None
    profile = derive_profile(endpoint)
    assert profile.from_parameter is None
    assert profile.date_mode == "none"


def test_single_suffix_date_pair_is_bound(repository) -> None:
    endpoint = repository.get("get-staff-shifts")
    assert endpoint is not None
    profile = derive_profile(endpoint)
    assert (profile.from_parameter, profile.to_parameter) == ("clockInFrom", "clockInTo")


def test_integer_range_is_not_treated_as_dates(repository) -> None:
    endpoint = repository.get("get-staff-members-birthdays")
    assert endpoint is not None
    assert derive_profile(endpoint).from_parameter is None


def test_take_only_endpoint_does_not_paginate(repository) -> None:
    endpoint = repository.get("get-manufacture-containers")
    assert endpoint is not None
    profile = derive_profile(endpoint)
    assert profile.pagination is False
    assert profile.take_parameter == "take"


def test_path_parameters_are_reported_as_unbound(repository) -> None:
    endpoint = repository.get("get-manufacture-orders")
    assert endpoint is not None
    profile = derive_profile(endpoint)
    assert "manufactureId" in profile.path_parameters
    assert "manufactureId" in profile.required_unbound


def test_settings_parameters_are_bound_not_asked(repository) -> None:
    endpoint = repository.get("get-all-units")
    assert endpoint is not None
    profile = derive_profile(endpoint)
    assert profile.settings_parameters == {
        "countryId": "dodo_country_id",
        "businessId": "dodo_business_id",
    }
    assert "countryId" not in profile.required_unbound
    assert "businessId" not in profile.required_unbound


def test_root_array_response_uses_sentinel_collection(repository) -> None:
    endpoint = repository.get("get-roles-list")
    assert endpoint is not None
    assert derive_profile(endpoint).collection == ROOT_COLLECTION


def test_multiple_arrays_reported_as_candidates(repository) -> None:
    endpoint = repository.get("get-ratings-standards-detalization")
    assert endpoint is not None
    profile = derive_profile(endpoint)
    assert profile.collection == ""
    assert sorted(profile.collection_candidates) == ["blocks", "criterias"]


def test_hourly_rounding_detected_from_description() -> None:
    endpoint = _endpoint(
        "example",
        parameters=[
            EndpointParameter(
                name="from",
                location="query",
                required=True,
                type="string",
                description="Начало периода в формате ISO 8601 округлённого до часов",
            ),
            EndpointParameter(name="to", location="query", required=True, type="string"),
        ],
    )
    assert derive_profile(endpoint).date_mode == "datetime_hour"


def test_date_suffix_implies_date_mode() -> None:
    endpoint = _endpoint(
        "example",
        parameters=[
            EndpointParameter(name="fromDate", location="query", required=True, type="string"),
            EndpointParameter(name="toDate", location="query", required=True, type="string"),
        ],
    )
    assert derive_profile(endpoint).date_mode == "date"


def test_pagination_requires_skip_take_and_flag() -> None:
    endpoint = _endpoint(
        "example",
        parameters=[
            EndpointParameter(name="skip", location="query", type="integer"),
            EndpointParameter(name="take", location="query", type="integer"),
        ],
        response_fields=[
            ResponseField(path="rows"),
            ResponseField(path="rows[]"),
            ResponseField(path="isEndOfListReached", type="boolean"),
        ],
    )
    profile = derive_profile(endpoint)
    assert profile.pagination is True
    assert profile.items_path == "rows"
