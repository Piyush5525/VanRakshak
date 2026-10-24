"""Camera-based fire/smoke detection through a user-supplied ONNX detector.

No weights are bundled. Point FIRE_MODEL_PATH at a model whose licence you have checked and
FIRE_MODEL_META at a JSON file such as:
  {"name": "...", "licence": "...", "classes": ["fire", "smoke"], "input_size": 640, "source_url": "..."}
Supported output layouts: YOLOv8-style [1, 4+nc, anchors] or end-to-end [1, N, 6] (x1,y1,x2,y2,score,class).
Detections are *candidates*: fog, cloud, dust, sun glare, industrial smoke and light can all trigger them.
"""
import hashlib
import json
import threading
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from app.core.config import Settings


class FireModelUnavailable(RuntimeError):
    pass


class FireDetector:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._session = None
        self._meta: dict[str, Any] = {}
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        path = self.settings.fire_model_path
        if not path:
            return {"configured": False, "usable": False, "reason": "FIRE_MODEL_PATH is not set; no fire/smoke model is bundled because licences and suitability must be verified first"}
        if not Path(path).is_file():
            return {"configured": True, "usable": False, "reason": "FIRE_MODEL_PATH does not point to a file"}
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return {"configured": True, "usable": False, "reason": "onnxruntime is not installed"}
        meta = self._load_meta()
        return {"configured": True, "usable": True, "model": meta.get("name", Path(path).name), "licence": meta.get("licence", "not recorded"), "classes": meta.get("classes"), "sha256": self._sha()}

    def _sha(self) -> str:
        return hashlib.sha256(Path(self.settings.fire_model_path).read_bytes()).hexdigest()

    def _load_meta(self) -> dict[str, Any]:
        p = self.settings.fire_model_meta
        if p and Path(p).is_file():
            return json.loads(Path(p).read_text(encoding="utf-8"))
        return {}

    def _ensure(self):
        with self._lock:
            if self._session is None:
                st = self.status()
                if not st["usable"]:
                    raise FireModelUnavailable(st["reason"])
                import onnxruntime as ort

                self._meta = self._load_meta()
                self._meta.setdefault("classes", ["fire", "smoke"])
                self._meta.setdefault("input_size", 640)
                self._session = ort.InferenceSession(self.settings.fire_model_path, providers=["CPUExecutionProvider"])
        return self._session

    def detect(self, image: Image.Image, conf_threshold: float = 0.4, iou: float = 0.5) -> dict[str, Any]:
        session = self._ensure()
        size = int(self._meta["input_size"])
        img = image.convert("RGB")
        w0, h0 = img.size
        scale = min(size / w0, size / h0)
        nw, nh = int(round(w0 * scale)), int(round(h0 * scale))
        canvas = Image.new("RGB", (size, size), (114, 114, 114))
        px, py = (size - nw) // 2, (size - nh) // 2
        canvas.paste(img.resize((nw, nh)), (px, py))
        x = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        out = session.run(None, {session.get_inputs()[0].name: x})[0]
        boxes = self._decode(out, conf_threshold, iou)
        dets = []
        for (x1, y1, x2, y2), score, cls in boxes:
            bx = [max((x1 - px) / scale, 0), max((y1 - py) / scale, 0), min((x2 - px) / scale, w0), min((y2 - py) / scale, h0)]
            names = self._meta["classes"]
            dets.append({"class": names[cls] if cls < len(names) else str(cls), "confidence": round(float(score), 4), "box_xyxy": [round(float(v), 1) for v in bx]})
        return {"image_size": [w0, h0], "detections": dets, "model": self._meta.get("name"), "model_sha256": self._sha(), "confidence_threshold": conf_threshold, "claim_type": "candidate_inference"}

    @staticmethod
    def _decode(out: np.ndarray, conf: float, iou: float):
        o = out[0]
        if o.ndim == 2 and o.shape[-1] == 6:  # end-to-end
            return [((r[0], r[1], r[2], r[3]), r[4], int(r[5])) for r in o if r[4] >= conf]
        if o.shape[0] < o.shape[1]:
            o = o.T  # [anchors, 4+nc]
        boxes, scores, classes = o[:, :4], o[:, 4:].max(1), o[:, 4:].argmax(1)
        keep = scores >= conf
        boxes, scores, classes = boxes[keep], scores[keep], classes[keep]
        xyxy = np.stack([boxes[:, 0] - boxes[:, 2] / 2, boxes[:, 1] - boxes[:, 3] / 2, boxes[:, 0] + boxes[:, 2] / 2, boxes[:, 1] + boxes[:, 3] / 2], 1)
        order, picked = scores.argsort()[::-1], []
        while len(order):
            i = order[0]
            picked.append(i)
            rest = order[1:]
            if not len(rest):
                break
            xx1, yy1 = np.maximum(xyxy[i, 0], xyxy[rest, 0]), np.maximum(xyxy[i, 1], xyxy[rest, 1])
            xx2, yy2 = np.minimum(xyxy[i, 2], xyxy[rest, 2]), np.minimum(xyxy[i, 3], xyxy[rest, 3])
            inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
            a = (xyxy[i, 2] - xyxy[i, 0]) * (xyxy[i, 3] - xyxy[i, 1])
            b = (xyxy[rest, 2] - xyxy[rest, 0]) * (xyxy[rest, 3] - xyxy[rest, 1])
            order = rest[inter / (a + b - inter + 1e-9) < iou]
        return [(tuple(xyxy[i]), scores[i], int(classes[i])) for i in picked]


class TemporalFilter:
    """Raise a candidate only when >= k of the last n frames from one camera contain fire/smoke."""

    def __init__(self, k: int = 3, n: int = 5):
        self.k, self.n = k, n
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=n))
        self.lock = threading.Lock()

    def update(self, camera_id: str, positive: bool) -> dict[str, Any]:
        with self.lock:
            h = self.history[camera_id]
            h.append(bool(positive))
            hits = sum(h)
            return {"hits": hits, "window": len(h), "required": f"{self.k} of {self.n}", "confirmed_by_filter": hits >= self.k and len(h) >= self.k}


def correlate(rows: list[dict[str, Any]], radius_km: float = 2.0, hours: float = 24.0) -> list[dict[str, Any]]:
    """Cluster detections close in space and time. Clusters are *possible* same-event groupings, not merges."""
    from datetime import datetime

    from app.services.routing import haversine_km

    pts = []
    for r in rows:
        c = r["geometry"]["coordinates"] if r["geometry"]["type"] == "Point" else None
        if c:
            pts.append((r, c, datetime.fromisoformat(r["observed_at"])))
    clusters: list[list[tuple]] = []
    for item in pts:
        for cl in clusters:
            if any(haversine_km(item[1], o[1]) <= radius_km and abs((item[2] - o[2]).total_seconds()) <= hours * 3600 for o in cl):
                cl.append(item)
                break
        else:
            clusters.append([item])
    out = []
    for cl in clusters:
        kinds = sorted({("camera" if r["observation_type"] == "camera_detection" else "satellite") for r, _, _ in cl})
        out.append({"member_ids": [r["id"] for r, _, _ in cl], "sources": sorted({r["source"] for r, _, _ in cl}), "evidence_kinds": kinds, "multi_source": len(kinds) > 1, "first_observed": min(t for _, _, t in cl).isoformat(), "last_observed": max(t for _, _, t in cl).isoformat(), "criteria": f"within {radius_km} km and {hours} h", "caveat": "Spatial/temporal proximity only; satellite pixels are 375 m-1 km and camera positions are approximate. Review before treating as one fire."})
    return out
