"""Export one JSON Schema per topic to ``docs/events/``.

Usage (from ``app/``): ``uv run python -m shared.events.export ../docs/events``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from shared.events import PAYLOADS, Envelope


def export(target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for topic, payload in sorted(PAYLOADS.items()):
        schema = Envelope[payload].model_json_schema()  # type: ignore[valid-type]
        schema["title"] = f"{topic} event"
        schema["$id"] = f"https://cardvault.example/events/{topic}.schema.json"
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        path = target / f"{topic}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "../docs/events")
    for p in export(out):
        print(p)
