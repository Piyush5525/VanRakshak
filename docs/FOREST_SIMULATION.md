# Incident simulation in the Forest Explorer

Open any incident in the **AR Forest Explorer** to see a **simulated** 3D forest inside its boundary and play a deforestation, a fire-spread, or a combined scenario.

> **Everything shown is simulated.** It is a browser-generated illustration based on the selected incident geometry. It is not real mapping, not satellite evidence, not a forecast and not a legal determination. Scenes are labelled `SIMULATED DEFORESTATION — NOT AN OBSERVATION`, `SIMULATED FIRE SPREAD — NOT A FORECAST`, or `SIMULATED FIRE, THEN CLEARING — NOT AN OBSERVATION OR A FORECAST`.

## How to open it

| From | Button | Link |
|---|---|---|
| Live map, a reported incident | **Open in Forest Explorer** (3D simulation) | `/app#/ar-explorer/<incident_id>` |
| Live map, a fire incident | 3D simulation · Open in Forest Explorer | `/app#/ar-explorer/fire:<id>` |
| Live map, a tree-loss spot | **Open in Forest Explorer** | `/app#/ar-explorer/forest:<id>` |
| Console, an investigation | **Open in Forest Explorer** | `#/ar-explorer/<incident_id>` |

**With no parameter** the Forest Explorer opens on a chooser: cards for reported incidents, fire incidents and tree-loss spots (with filters and search), and the monitored regions. With a **region id** it is **Region analysis mode** (real elevation and ESA WorldCover tree cover). With an **incident** it is **Incident simulation mode**; the mode is shown at the top. An unknown id returns to the chooser.

The scene takes keyboard focus: arrow keys turn the view, plus and minus zoom, space plays or pauses; a hidden status line announces play, pause and finish to screen readers.

## What is built

* **Boundary:** a Polygon or MultiPolygon incident uses its exact geometry (holes respected). A Point incident gets a synthetic, irregular boundary generated from its id, labelled "Synthetic boundary generated from point incident."
* **Local scene:** longitude and latitude are converted to metres around the incident's centre. The boundary is drawn as an outline and a faint curtain, and the floor is cut to the boundary shape.
* **Trees:** simulated positions on a jittered grid, only inside the boundary, about 260 per hectare (never more than 9,000). The seed comes from the incident id, so the same incident always produces the same forest. Trees are instanced meshes (trunk plus two cones). States: healthy, burning, burned, cleared.

## Scenarios

* **Deforestation:** clearing spreads outwards from the middle of the boundary as a deterministic, irregular wavefront over a grid of cells. Trees shrink, tilt and disappear, the ground turns brown, and dust rises. Progress label: `Simulated clearing: 42%`.
* **Fire spread:** one or two deterministic ignition points. A wavefront over the same grid, biased by a chosen wind direction and strength, takes trees from healthy to burning to burned. A simulated fire perimeter is drawn, with smoke and embers. The fire stops after reaching part of the forest so the end shows both burned and unburned ground.
* **Combined:** fire first (leaving burned ground), then clearing of the forest that is left, starting at the edge of the burned area. The clearing step can be switched off.

Controls: Play, Pause, Reset, Restart, speed 0.5×, 1×, 2×, a time slider, and view toggles. Click the ground to inspect a spot.

## Code

`frontend/js/sim/geo.js` (boundary, projection, point-in-polygon, tree generation) and `frontend/js/sim/engine.js` (the wavefront and tree states) are plain functions with no browser or Three.js dependency. `frontend/js/pages/ar-incident.js` draws the scene. `frontend/js/pages/ar-explorer.js` chooses between the two modes. Tests: `tests/e2e/test_forest_sim.py`.
