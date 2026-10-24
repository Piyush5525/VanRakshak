from fastapi import HTTPException


TRANSITIONS: dict[str, set[str]] = {
    "DETECTED": {"PROCESSING", "NEEDS_REVIEW", "APPROVED_FOR_INSPECTION", "DISMISSED"},
    "PROCESSING": {"NEEDS_REVIEW", "DISMISSED"},
    "NEEDS_REVIEW": {"APPROVED_FOR_INSPECTION", "DISMISSED"},
    "APPROVED_FOR_INSPECTION": {"ASSIGNED", "DISMISSED"},
    "ASSIGNED": {"DISPATCHED", "DISMISSED"},
    "DISPATCHED": {"ACKNOWLEDGED", "FIELD_VERIFICATION_PENDING"},
    "ACKNOWLEDGED": {"FIELD_VERIFICATION_PENDING", "CLOSED"},
    "FIELD_VERIFICATION_PENDING": {"CONFIRMED", "UNCONFIRMED", "CLOSED"},
    "CONFIRMED": {"CLOSED"},
    "UNCONFIRMED": {"CLOSED"},
    "DISMISSED": set(),
    "CLOSED": set(),
}


def transition(current: str, target: str) -> None:
    if target not in TRANSITIONS.get(current, set()):
        raise HTTPException(status_code=409, detail=f"Invalid incident transition {current} -> {target}")
