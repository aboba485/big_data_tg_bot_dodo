from __future__ import annotations

import re
from dataclasses import dataclass

from app.documentation.models import EndpointDocument, EndpointParameter

SALES_CHANNEL_FILTER = "salesChannel"
SALES_CHANNEL_GROUP = "sales channel"
SALES_CHANNEL_PARAMETER_NAMES = ("salesChannel", "salesChannels")
DEFAULT_SALES_CHANNELS = ("Delivery", "Dine-in", "Takeaway")

_CANONICAL_BY_KEY = {
    "delivery": "Delivery",
    "dinein": "Dine-in",
    "takeaway": "Takeaway",
    "staffmeal": "Staff meal",
    "tracker": "Tracker",
    "tabledelivery": "TableDelivery",
}
_QUERY_ALIASES = {
    "Delivery": (
        "delivery",
        "доставка",
        "доставке",
        "доставку",
        "доставки",
        "доставкой",
    ),
    "Dine-in": (
        "dine-in",
        "dine in",
        "в зале",
    ),
    "Takeaway": (
        "takeaway",
        "take away",
        "самовывоз",
        "самовывоза",
        "самовывозу",
        "на вынос",
        "навынос",
    ),
    "Staff meal": ("staff meal", "питание персонала", "обед сотрудников"),
    "Tracker": ("tracker", "трекер"),
}
_CHANNEL_CONTEXT_RE = re.compile(r"\b(?:канал\w*|channel\w*)\b", re.IGNORECASE)
_SPLIT_RE = re.compile(
    r"(?:по\s+каналам|разб\w*\s+по\s+канал\w*|в\s+разрезе\s+канал\w*|"
    r"by\s+sales\s+channel)",
    re.IGNORECASE,
)
_ALL_TOGETHER_RE = re.compile(
    r"(?:все\s+канал\w*\s+вместе|без\s+разбив\w*\s+по\s+канал\w*|"
    r"не\s+разбив\w*\s+(?:по\s+)?канал\w*|all\s+channels?\s+together)",
    re.IGNORECASE,
)
_CLARIFICATION_MARKER = "уточнение пользователя:"


@dataclass(frozen=True)
class SalesChannelCapability:
    can_group: bool
    can_filter: bool
    values: tuple[str, ...]
    parameter: EndpointParameter | None = None


@dataclass(frozen=True)
class SalesChannelIntent:
    values: tuple[str, ...] = ()
    split: bool = False
    all_together: bool = False
    channel_context: bool = False

    @property
    def explicit(self) -> bool:
        return bool(self.values or self.split or self.all_together)


def sales_channel_key(value: str) -> str:
    normalized = value.casefold().replace("ё", "е")
    return "".join(character for character in normalized if character.isalnum())


def canonical_sales_channel(value: str, available: tuple[str, ...] = ()) -> str | None:
    key = sales_channel_key(value)
    for candidate in available:
        if sales_channel_key(candidate) == key:
            return candidate
    canonical = _CANONICAL_BY_KEY.get(key)
    if canonical is None:
        return None
    if not available:
        return canonical
    return next(
        (
            candidate
            for candidate in available
            if sales_channel_key(candidate) == sales_channel_key(canonical)
        ),
        None,
    )


def sales_channel_intent(query: str) -> SalesChannelIntent:
    normalized = query.casefold().replace("ё", "е")
    all_together = bool(_ALL_TOGETHER_RE.search(normalized))
    split = bool(_SPLIT_RE.search(normalized)) and not all_together
    marker_index = normalized.rfind(_CLARIFICATION_MARKER)
    channel_context = bool(_CHANNEL_CONTEXT_RE.search(normalized))
    if marker_index >= 0:
        channel_text = normalized[marker_index + len(_CLARIFICATION_MARKER) :]
    else:
        channel_text = normalized
    values = [
        channel
        for channel, aliases in _QUERY_ALIASES.items()
        if channel_text and any(alias in channel_text for alias in aliases)
    ]
    dine_in_context = re.search(
        r"(?:канал\w*\s+(?:ресторан\w*|зал\w*)|"
        r"(?:ресторан\w*|зал\w*)\s+канал\w*)",
        normalized,
    )
    if dine_in_context and "Dine-in" not in values:
        values.append("Dine-in")
    return SalesChannelIntent(
        values=tuple(values),
        split=split,
        all_together=all_together,
        channel_context=channel_context,
    )


def endpoint_sales_channel_capability(
    endpoint: EndpointDocument,
) -> SalesChannelCapability | None:
    fields = [
        field
        for field in endpoint.response_fields
        if field.path.replace("[]", "").split(".")[-1] in {"salesChannel", "salesChannelName"}
    ]
    parameter = next(
        (item for item in endpoint.parameters if item.name in SALES_CHANNEL_PARAMETER_NAMES),
        None,
    )
    if not fields and parameter is None:
        return None
    values: list[str] = []
    documented_values = [
        *(item for field in fields for item in field.enum),
        *(parameter.enum if parameter else []),
    ]
    for value in documented_values:
        text = str(value)
        canonical = canonical_sales_channel(text) or text
        if sales_channel_key(canonical) not in {sales_channel_key(item) for item in values}:
            values.append(canonical)
    if not values:
        values.extend(DEFAULT_SALES_CHANNELS)
    return SalesChannelCapability(
        can_group=bool(fields),
        can_filter=bool(fields or parameter),
        values=tuple(values),
        parameter=parameter,
    )


def common_sales_channel_capability(
    endpoints: list[EndpointDocument],
) -> SalesChannelCapability | None:
    capabilities = [endpoint_sales_channel_capability(endpoint) for endpoint in endpoints]
    if not capabilities or any(capability is None for capability in capabilities):
        return None
    concrete = [capability for capability in capabilities if capability is not None]
    first_values = concrete[0].values
    common_keys = {
        sales_channel_key(value)
        for value in first_values
        if all(
            any(
                sales_channel_key(candidate) == sales_channel_key(value)
                for candidate in item.values
            )
            for item in concrete[1:]
        )
    }
    values = tuple(value for value in first_values if sales_channel_key(value) in common_keys)
    return SalesChannelCapability(
        can_group=all(item.can_group for item in concrete),
        can_filter=all(item.can_filter for item in concrete),
        values=values or DEFAULT_SALES_CHANNELS,
    )


def serialize_sales_channel(
    endpoint: EndpointDocument,
    parameter: EndpointParameter,
    value: str,
) -> str:
    for candidate in parameter.enum:
        if sales_channel_key(str(candidate)) == sales_channel_key(value):
            return str(candidate)
    description = f"{endpoint.description} {parameter.description}"
    spelling_overrides = {
        "Dine-in": "DineIn",
        "Takeaway": "TakeAway",
        "Staff meal": "StaffMeal",
    }
    override = spelling_overrides.get(canonical_sales_channel(value) or value)
    return override if override and override in description else value


def sales_channel_label(value: str) -> str:
    return {
        "Delivery": "Доставка",
        "Dine-in": "Ресторан",
        "Takeaway": "Самовывоз",
        "Staff meal": "Питание персонала",
        "Tracker": "Tracker",
        "TableDelivery": "Доставка к столу",
    }.get(value, value)
