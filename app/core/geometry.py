import math
from typing import Any

from fastapi import HTTPException


def validate_geojson(geometry: dict[str, Any], max_vertices: int = 20_000) -> dict[str, Any]:
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon", "Point"}:
        raise HTTPException(status_code=422, detail="Geometry must be a GeoJSON Point, Polygon, or MultiPolygon")
    coordinates = geometry.get("coordinates")
    if coordinates is None:
        raise HTTPException(status_code=422, detail="Geometry coordinates are required")
    vertices = _count_vertices(coordinates)
    if vertices > max_vertices:
        raise HTTPException(status_code=413, detail=f"Geometry exceeds {max_vertices} vertices")
    _validate_coordinates(coordinates)
    return geometry


def _count_vertices(value: Any) -> int:
    if isinstance(value, (list, tuple)) and len(value) >= 2 and all(isinstance(x, (int, float)) for x in value[:2]):
        return 1
    return sum(_count_vertices(item) for item in value) if isinstance(value, (list, tuple)) else 0


def _validate_coordinates(value: Any) -> None:
    if isinstance(value, (list, tuple)) and len(value) >= 2 and all(isinstance(x, (int, float)) for x in value[:2]):
        lon, lat = float(value[0]), float(value[1])
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise HTTPException(status_code=422, detail="Coordinates are outside WGS84 bounds")
        return
    if not isinstance(value, (list, tuple)):
        raise HTTPException(status_code=422, detail="Invalid coordinate array")
    for item in value:
        _validate_coordinates(item)


def bbox(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    points: list[tuple[float, float]] = []

    def collect(value: Any):
        if isinstance(value, (list, tuple)) and len(value) >= 2 and all(isinstance(x, (int, float)) for x in value[:2]):
            points.append((float(value[0]), float(value[1])))
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    collect(geometry["coordinates"])
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def to_shape(geometry):
    from shapely.geometry import shape
    from shapely.validation import make_valid

    geom = shape(geometry)
    return geom if geom.is_valid else make_valid(geom)


def geodesic_area_hectares(geometry) -> float:
    """WGS84 ellipsoidal area in hectares; accepts GeoJSON dicts or shapely geometries."""
    from pyproj import Geod

    geom = to_shape(geometry) if isinstance(geometry, dict) else geometry
    if geom.is_empty or geom.geom_type in {"Point", "MultiPoint", "LineString", "MultiLineString"}:
        return 0.0
    return abs(Geod(ellps="WGS84").geometry_area_perimeter(geom)[0]) / 10_000


def approximate_area_hectares(geometry) -> float:
    return geodesic_area_hectares(geometry)
