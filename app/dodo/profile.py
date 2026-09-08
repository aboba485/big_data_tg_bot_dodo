from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.documentation.models import EndpointDocument, EndpointParameter

ROOT_COLLECTION = "__root__"
DEFAULT_PAGE_SIZE = 1000
DEFAULT_MAX_UNITS = 30

CANONICAL_DATE_PAIRS = (("from", "to"), ("fromDate", "toDate"))
UNITS_LIST_PARAMETERS = ("units", "unitIds", "unitUuids")
UNITS_SINGLE_PARAMETERS = ("unit", "unitId")
SKIP_PARAMETERS = ("skip",)
TAKE_PARAMETERS = ("take",)
END_FLAG_FIELD = "isEndOfListReached"
SETTINGS_PARAMETERS = {"countryId": "dodo_country_id", "businessId": "dodo_business_id"}

HOURLY_DESCRIPTION = re.compile(r"округл\w*\s+до\s+час", re.IGNORECASE)
DATE_ONLY_DESCRIPTION = re.compile(r"без\s+времени", re.IGNORECASE)


class ExecutionProfile(BaseModel):
    """How to call one endpoint, derived from its indexed documentation.

    Curated entries in config/endpoint_overrides.yaml are layered on top of the
    derived values, because they encode server behaviour that the schema does not
    describe (undocumented period caps, for example).
    """

    operation_id: str
    units_parameter: str | None = None
    units_per_request: int = DEFAULT_MAX_UNITS
    from_parameter: str | None = None
    to_parameter: str | None = None
    date_mode: str = "none"
    max_period_days: int = 31
    pagination: bool = False
    skip_parameter: str = "skip"
    take_parameter: str | None = None
    page_size: int = DEFAULT_PAGE_SIZE
    end_flag_path: str = END_FLAG_FIELD
    items_path: str = ""
    empty_page_is_end: bool = False
    single_array_collection_fallback: bool = False
    collection: str = ""
    collection_candidates: list[str] = Field(default_factory=list)
    path_parameters: list[str] = Field(default_factory=list)
    settings_parameters: dict[str, str] = Field(default_factory=dict)
    required_unbound: list[str] = Field(default_factory=list)
    curated: bool = False

    @property
    def has_period(self) -> bool:
        return bool(self.from_parameter and self.to_parameter)

    @property
    def has_units(self) -> bool:
        return bool(self.units_parameter)

    @property
    def unsupported_path_params(self) -> list[str]:
        """Path parameters that the system cannot auto-fill."""
        supported = set(UNITS_SINGLE_PARAMETERS) | set(SETTINGS_PARAMETERS.keys())
        return [p for p in self.path_parameters if p not in supported]

    def as_override(self) -> dict[str, Any]:
        """Dict shape consumed by the existing executor and paginator."""
        return {
            "max_units": self.units_per_request if self.units_parameter else 0,
            "max_period_days": self.max_period_days,
            "date_mode": self.date_mode,
            "from_parameter": self.from_parameter or "",
            "to_parameter": self.to_parameter or "",
            "pagination": self.pagination,
            "skip_parameter": self.skip_parameter,
            "take_parameter": self.take_parameter or "take",
            "page_size": self.page_size,
            "end_flag_path": self.end_flag_path,
            "items_path": self.items_path,
            "empty_page_is_end": self.empty_page_is_end,
            "single_array_collection_fallback": self.single_array_collection_fallback,
        }


def _by_name(parameters: list[EndpointParameter]) -> dict[str, EndpointParameter]:
    return {parameter.name: parameter for parameter in parameters}


def _query_names(parameters: list[EndpointParameter]) -> set[str]:
    return {item.name for item in parameters if item.location in {"query", ""}}


def _detect_date_pair(parameters: list[EndpointParameter]) -> tuple[str | None, str | None]:
    names = _query_names(parameters)
    for start, finish in CANONICAL_DATE_PAIRS:
        if start in names and finish in names:
            return start, finish
    lookup = _by_name(parameters)
    suffixed = [
        (name, f"{name[:-4]}To")
        for name in sorted(names)
        if name.endswith("From")
        and f"{name[:-4]}To" in names
        and lookup[name].type == "string"
        and lookup[f"{name[:-4]}To"].type == "string"
    ]
    # Several endpoints expose competing ranges (hired/dismissed/lastModified).
    # Binding the report period to an arbitrary one would silently filter on the
    # wrong column, so leave it to the planner.
    return suffixed[0] if len(suffixed) == 1 else (None, None)


def _detect_date_mode(
    parameters: list[EndpointParameter], start: str | None, finish: str | None
) -> str:
    if not start or not finish:
        return "none"
    lookup = _by_name(parameters)
    descriptions = " ".join(lookup[name].description for name in (start, finish) if name in lookup)
    if HOURLY_DESCRIPTION.search(descriptions):
        return "datetime_hour"
    if DATE_ONLY_DESCRIPTION.search(descriptions):
        return "date"
    if start.endswith("Date") and finish.endswith("Date"):
        return "date"
    return "datetime"


def _detect_units(parameters: list[EndpointParameter]) -> tuple[str | None, int]:
    names = _query_names(parameters)
    for name in UNITS_LIST_PARAMETERS:
        if name in names:
            return name, DEFAULT_MAX_UNITS
    for name in UNITS_SINGLE_PARAMETERS:
        if name in names:
            return name, 1
    return None, DEFAULT_MAX_UNITS


def _top_level_arrays(endpoint: EndpointDocument) -> list[str]:
    result = []
    for field in endpoint.response_fields:
        path = field.path
        if path.endswith("[]") and path.count("[]") == 1 and "." not in path:
            name = path[:-2]
            if name and name not in result:
                result.append(name)
    return result


def _detect_collection(endpoint: EndpointDocument) -> tuple[str, list[str]]:
    schema = endpoint.success_response_schema or {}
    if schema.get("type") == "array":
        return ROOT_COLLECTION, []
    candidates = _top_level_arrays(endpoint)
    if len(candidates) == 1:
        return candidates[0], []
    if len(candidates) > 1:
        return "", candidates
    return "", []


def _detect_pagination(
    endpoint: EndpointDocument, items_path: str
) -> tuple[bool, str, str | None, str]:
    names = _query_names(endpoint.parameters)
    skip = next((name for name in SKIP_PARAMETERS if name in names), None)
    take = next((name for name in TAKE_PARAMETERS if name in names), None)
    has_end_flag = any(field.path == END_FLAG_FIELD for field in endpoint.response_fields)
    # The paginator advances by skip and stops on the end flag. Endpoints that
    # expose only take, or omit the flag, would either loop or raise on the final
    # page, so they are fetched as a single large page instead.
    enabled = bool(skip and take and has_end_flag and items_path)
    return enabled, skip or "skip", take, END_FLAG_FIELD


def derive_profile(
    endpoint: EndpointDocument,
    *,
    default_max_period_days: int = 31,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> ExecutionProfile:
    parameters = endpoint.parameters
    start, finish = _detect_date_pair(parameters)
    date_mode = _detect_date_mode(parameters, start, finish)
    units_parameter, units_per_request = _detect_units(parameters)
    collection, collection_candidates = _detect_collection(endpoint)
    items_path = collection if collection and collection != ROOT_COLLECTION else ""
    pagination, skip_parameter, take_parameter, end_flag_path = _detect_pagination(
        endpoint, items_path
    )
    path_parameters = [item.name for item in parameters if item.location == "path"]
    settings_parameters = {
        item.name: SETTINGS_PARAMETERS[item.name]
        for item in parameters
        if item.name in SETTINGS_PARAMETERS
    }
    bound = {
        units_parameter,
        start,
        finish,
        skip_parameter,
        take_parameter,
        *settings_parameters,
    }
    required_unbound = [
        item.name for item in parameters if item.required and item.name not in bound
    ]
    return ExecutionProfile(
        operation_id=endpoint.operation_id,
        units_parameter=units_parameter,
        units_per_request=units_per_request,
        from_parameter=start,
        to_parameter=finish,
        date_mode=date_mode,
        max_period_days=default_max_period_days,
        pagination=pagination,
        skip_parameter=skip_parameter,
        take_parameter=take_parameter,
        page_size=page_size,
        end_flag_path=end_flag_path,
        items_path=items_path,
        collection=collection,
        collection_candidates=collection_candidates,
        path_parameters=path_parameters,
        settings_parameters=settings_parameters,
        required_unbound=required_unbound,
    )


class ProfileResolver:
    def __init__(
        self,
        overrides: dict[str, dict[str, Any]],
        *,
        default_max_period_days: int = 31,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.overrides = overrides
        self.default_max_period_days = default_max_period_days
        self.page_size = page_size

    def resolve(self, endpoint: EndpointDocument) -> ExecutionProfile:
        profile = derive_profile(
            endpoint,
            default_max_period_days=self.default_max_period_days,
            page_size=self.page_size,
        )
        override = self.overrides.get(endpoint.operation_id)
        return _apply_override(profile, override) if override else profile


def _apply_override(profile: ExecutionProfile, override: dict[str, Any]) -> ExecutionProfile:
    values = profile.model_dump()
    values["curated"] = True
    if "max_units" in override:
        maximum = int(override["max_units"])
        values["units_per_request"] = maximum if maximum > 0 else profile.units_per_request
        if maximum <= 0:
            values["units_parameter"] = None
    for key, target in (
        ("max_period_days", "max_period_days"),
        ("date_mode", "date_mode"),
        ("from_parameter", "from_parameter"),
        ("to_parameter", "to_parameter"),
        ("pagination", "pagination"),
        ("skip_parameter", "skip_parameter"),
        ("take_parameter", "take_parameter"),
        ("page_size", "page_size"),
        ("end_flag_path", "end_flag_path"),
        ("items_path", "items_path"),
        ("empty_page_is_end", "empty_page_is_end"),
        ("single_array_collection_fallback", "single_array_collection_fallback"),
    ):
        if key in override:
            values[target] = override[key]
    if override.get("date_mode") == "none":
        values["from_parameter"] = None
        values["to_parameter"] = None
    if override.get("items_path"):
        values["collection"] = override["items_path"]
        values["collection_candidates"] = []
    return ExecutionProfile.model_validate(values)
