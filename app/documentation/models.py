from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RawOperation(BaseModel):
    model_config = ConfigDict(extra="allow")

    api: str = ""
    api_key: str = ""
    id: str
    title: str = ""
    method: str
    path: str
    links: list[str] = Field(default_factory=list)
    all_links: list[str] = Field(default_factory=list)
    description: str = ""
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    responses: Any = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=list)
    deprecated: bool = False


class DocumentationBundle(BaseModel):
    model_config = ConfigDict(extra="allow")

    format_version: int
    operation_count: int
    operations: list[RawOperation]


class EndpointParameter(BaseModel):
    name: str
    location: str = ""
    required: bool = False
    type: str = ""
    description: str = ""
    format: str = ""
    enum: list[Any] = Field(default_factory=list)


class ResponseField(BaseModel):
    path: str
    type: str = ""
    description: str = ""
    enum: list[Any] = Field(default_factory=list)
    format: str = ""
    nullable: bool = False


class EndpointDocument(BaseModel):
    operation_id: str
    api: str
    api_key: str
    title: str
    method: str
    path: str
    links: list[str]
    description: str
    scopes: list[str]
    deprecated: bool
    parameters: list[EndpointParameter]
    response_fields: list[ResponseField]
    success_response_schema: dict[str, Any] | None
    searchable_text: str
    compact_summary: str


class EndpointCandidate(BaseModel):
    operation_id: str
    score: float
    compact_summary: str
    response_fields: list[str] = Field(default_factory=list)
    parameters: list[EndpointParameter] = Field(default_factory=list)
