"""Car ordinal → name lookup.

Forza broadcasts only a numeric ``CarOrdinal``; names come from two places,
in priority order:

1. The user's own registry (``cars`` table, set once via "✎ name car") —
   always wins, and is the only way to record build context.
2. The community-seeded ``app/data/car_ordinals.json`` shipped with the app
   (versioned, verified-in-game entries only — see its ``_meta``).

Unmatched ordinals resolve to ``None`` and are presented as
"Unknown car — ordinal N". Names are never fabricated or borrowed from
other Forza titles.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

_cache: Optional[Dict[str, Any]] = None


def _data_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "app" / "data" / "car_ordinals.json"
    return Path(__file__).parent / "data" / "car_ordinals.json"


def _load() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_data_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a broken seed must never crash the app
            log.warning("car_ordinals.json missing or invalid; lookups disabled")
            _cache = {"_meta": {}, "ordinals": {}}
    return _cache


def lookup(ordinal: Optional[int]) -> Optional[str]:
    """Community-seed name for an ordinal, or None if unknown."""
    if ordinal is None:
        return None
    entry = _load().get("ordinals", {}).get(str(int(ordinal)))
    if not entry:
        return None
    if isinstance(entry, str):
        return entry
    display = str(entry.get("display_name") or "").strip()
    if display:
        return display
    parts = [str(entry.get("year") or "").strip(),
             str(entry.get("manufacturer") or "").strip(),
             str(entry.get("model") or "").strip(),
             str(entry.get("trim") or "").strip()]
    name = " ".join(p for p in parts if p)
    return name or None


def seed_meta() -> Dict[str, Any]:
    return dict(_load().get("_meta", {}))
