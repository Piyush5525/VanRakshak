from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.geometry import validate_geojson


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegionCreate(APIModel):
    name: str = Field(min_length=2, max_length=160)
    jurisdiction: str = Field(min_length=2, max_length=160)
    geometry: dict[str, Any]

    @field_validator("geometry")
    @classmethod
    def valid_geometry(cls, value):
        return validate_geojson(value)


class ObservationCreate(APIModel):
    provider: str = "unconfigured"
    source_scene_ids: list[str] = Field(default_factory=list, max_length=100)
    acquisition_start: datetime | None = None
    acquisition_end: datetime | None = None
    processing_config: dict[str, Any] = Field(default_factory=dict)


class ChangeDetectionRequest(APIModel):
    region_id: str
    baseline_date: datetime
    comparison_date: datetime
    provider: str | None = None
    model_version: str | None = None
    threshold: float = Field(default=0.5, ge=0, le=1)
    before_raster: str | None = Field(default=None, description="File name inside RASTER_DIR; bands [NIR, SWIR, cloud mask]")
    after_raster: str | None = None
    window_days: int = Field(default=30, ge=5, le=90, description="Half-width of the compositing window around each date (planetary_computer)")
    max_cloud_cover: float = Field(default=60, ge=0, le=100)


class ForecastRequest(APIModel):
    region_id: str
    horizon_days: Literal[7, 30, 90]
    origin: datetime | None = None
    model_version: str | None = None


class SimulationRequest(APIModel):
    region_id: str
    geometry: dict[str, Any]
    horizon_days: Literal[7, 30, 90]
    mode: Literal["user_defined", "model_based"] = "user_defined"
    assumptions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("geometry")
    @classmethod
    def valid_geometry(cls, value):
        return validate_geojson(value)


class WildfireIngestRequest(APIModel):
    geometry: dict[str, Any]
    observed_at: datetime
    observation_type: Literal["active_fire", "burned_area", "suspected_fire_disturbance", "camera_detection"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    source: str = "manual"
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("geometry")
    @classmethod
    def valid_geometry(cls, value):
        return validate_geojson(value)


class AuthorizationCreate(APIModel):
    authority: str
    project_identifier: str | None = None
    project_name: str | None = None
    status: str
    geometry: dict[str, Any] | None = None
    dates: dict[str, Any] = Field(default_factory=dict)
    source_url: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("geometry")
    @classmethod
    def valid_geometry(cls, value):
        return validate_geojson(value) if value else value


class MatchAssessmentRequest(APIModel):
    change_id: str
    candidate_authorization_ids: list[str] = Field(default_factory=list, max_length=100)


class IncidentCreate(APIModel):
    title: str = Field(min_length=3, max_length=200)
    geometry: dict[str, Any]
    change_id: str | None = None
    wildfire_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("geometry")
    @classmethod
    def valid_geometry(cls, value):
        return validate_geojson(value)


class ReviewRequest(APIModel):
    decision: Literal["approve", "dismiss", "confirm", "unconfirm"]
    notes: str = Field(default="", max_length=4000)


class AssignmentRequest(APIModel):
    assignee: str = Field(min_length=2, max_length=160)


class DispatchRequest(APIModel):
    channel: Literal["test", "telegram", "voice", "email"] = "test"
    recipient: str = Field(min_length=1, max_length=240)


class FieldVerificationRequest(APIModel):
    outcome: Literal["confirmed", "unconfirmed", "pending"]
    notes: str = Field(default="", max_length=4000)
    observed_at: datetime | None = None


class BuildingEnergyRequest(APIModel):
    footprint: dict[str, Any] | None = None
    floor_area_m2: float = Field(gt=0)
    building_type: str = Field(min_length=2)
    age_years: int | None = Field(default=None, ge=0, le=300)
    occupancy_schedule: dict[str, Any] = Field(default_factory=dict)
    weather: dict[str, Any] = Field(default_factory=dict)
    meter_history: list[dict[str, Any]] = Field(default_factory=list)


class ScenarioRequest(BuildingEnergyRequest):
    assumptions: dict[str, Any] = Field(default_factory=dict)


class StationCreate(APIModel):
    name: str = Field(min_length=2, max_length=160)
    jurisdiction: str = Field(min_length=2, max_length=160)
    location: dict[str, Any]
    coverage: dict[str, Any] | None = None
    contacts: dict[str, Any] = Field(default_factory=dict, description="e.g. {'telegram_chat_id': '...', 'phone': '+91...'}")
    availability_note: str | None = None

    @field_validator("location")
    @classmethod
    def point_only(cls, value):
        if value.get("type") != "Point":
            raise ValueError("Station location must be a GeoJSON Point")
        return validate_geojson(value)

    @field_validator("coverage")
    @classmethod
    def valid_coverage(cls, value):
        return validate_geojson(value) if value else value


class PatrolRequest(APIModel):
    origin: dict[str, Any] | None = None
    incident_ids: list[str] = Field(min_length=1, max_length=20)


class RouteRequest(APIModel):
    origin: dict[str, Any] | None = Field(default=None, description="Omit to start from the nearest registered station")
    incident_id: str | None = None
    destination: dict[str, Any] | None = None
    alternatives: bool = True

    @field_validator("origin", "destination")
    @classmethod
    def valid_point(cls, value):
        if value is None:
            return value
        if value.get("type") != "Point":
            raise ValueError("origin and destination must be GeoJSON Points")
        return validate_geojson(value)
