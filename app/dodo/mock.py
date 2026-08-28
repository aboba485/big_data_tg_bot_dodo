from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.documentation.models import EndpointDocument
from app.dodo.request_counter import RequestCounter
from app.errors import ConfigurationError


class MockDodoApiClient:
    def __init__(self) -> None:
        self._requests = RequestCounter()

    @property
    def request_count(self) -> int:
        return self._requests.request_count

    def request_scope(self):
        return self._requests.request_scope()

    async def request_operation(
        self,
        operation: EndpointDocument,
        query_params: dict[str, str | int | bool | list[str]],
        path_values: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self._requests.count_request()
        operation_id = operation.operation_id
        units_value = query_params.get("units") or query_params.get("unitIds") or []
        units = units_value if isinstance(units_value, list) else str(units_value).split(",")
        units = units or ["000d3a240c719a8711e68aba13f7f862"]
        start = _as_date(
            query_params.get("fromDate") or query_params.get("from") or date.today().isoformat()
        )
        end = _as_date(query_params.get("toDate") or query_params.get("to") or start.isoformat())
        days = (end - start).days + 1
        if operation_id == "get-finances-sales-daily-units":
            return {
                "result": [
                    {
                        "date": (start + timedelta(days=offset)).isoformat(),
                        "unitId": unit,
                        "sales": 10000 + offset * 250,
                        "ordersCount": 100 + offset,
                        "salesBreakdown": [
                            {"salesChannel": "Delivery", "sales": 6000, "ordersCount": 60},
                            {"salesChannel": "Dine-in", "sales": 4000, "ordersCount": 40},
                        ],
                    }
                    for unit in units
                    for offset in range(days)
                ]
            }
        if operation_id == "get-delivery-statistics":
            return {
                "unitsStatistics": [
                    {
                        "unitId": unit,
                        "deliverySales": 6000 * days,
                        "deliveryOrdersCount": 60 * days,
                        "avgDeliveryOrderFulfillmentTime": 1800,
                        "avgCookingTime": 600,
                        "avgHeatedShelfTime": 120,
                        "avgOrderTripTime": 900,
                        "lateOrdersCount": 3 * days,
                        "ordersWithCourierAppCount": 54 * days,
                        "couriersShiftsDuration": 8 * 3600 * days,
                        "tripsDuration": 5 * 3600 * days,
                    }
                    for unit in units
                ]
            }
        if operation_id == "get-orders-client-statistics":
            return {
                "newClientsCount": 12 * days,
                "dineInNewClientsCount": 5 * days,
                "deliveryAndTakeawayNewClientsCount": 7 * days,
                "oldClientsCount": 80 * days,
            }
        if operation_id == "get-staff-productivity":
            return {
                "productivityStatistics": [
                    {
                        "unitId": unit,
                        "laborHours": 80 * days,
                        "sales": 10000 * days,
                        "productsPerLaborHour": 12.5,
                        "avgHeatedShelfTime": 110,
                    }
                    for unit in units
                ]
            }
        if operation_id == "get-delivery-vouchers":
            skip = int(query_params.get("skip", 0))
            vouchers = [
                {"orderId": f"order-{unit}-{index}", "unitId": unit}
                for unit in units
                for index in range(3)
            ]
            return {"vouchers": vouchers[skip:], "isEndOfListReached": True}
        if "stop-sales" in operation_id:
            key = (
                "stopSalesByIngredients"
                if "ingredients" in operation_id
                else "stopSalesBySalesChannels"
            )
            return {
                key: [
                    {
                        "id": f"stop-{unit}",
                        "unitId": unit,
                        "startedAtLocal": datetime.combine(start, datetime.min.time()).isoformat(),
                        "endedAtLocal": datetime.combine(start, datetime.min.time())
                        .replace(hour=2)
                        .isoformat(),
                        "ingredientName": "Сыр",
                        "ingredientCategoryName": "Ингредиенты",
                        "salesChannelName": "Delivery",
                        "reason": "Учебный fixture",
                    }
                    for unit in units
                ]
            }
        if "workload-by-" in operation_id:
            field = "ordersCount" if operation_id.endswith("orders") else "productsCount"
            return {
                "unitWorkload": [
                    {
                        "unitId": unit,
                        "fromLocal": start.isoformat(),
                        "toLocal": end.isoformat(),
                        field: (100 if field == "ordersCount" else 250) * days,
                    }
                    for unit in units
                ],
                "isEndOfListReached": True,
            }
        if operation_id == "get-all-units":
            return {"units": [], "isEndOfListReached": True}
        # Returning an empty payload here would render a silently blank report,
        # which is indistinguishable from an endpoint that legitimately has no data.
        raise ConfigurationError(
            f"Для операции «{operation_id}» нет mock-фикстуры. "
            "Отключите DODO_MOCK_MODE, чтобы обратиться к реальному Dodo IS."
        )


def _as_date(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])
