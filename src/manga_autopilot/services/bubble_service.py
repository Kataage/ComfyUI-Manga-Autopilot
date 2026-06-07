"""Speech-bubble service: persistence + placement orchestration."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from manga_autopilot.models.bubble import SpeechBubble
from manga_autopilot.models.panel import PanelLayout
from manga_autopilot.services.bubble_layout import BubblePlacement, place_bubbles

log = logging.getLogger(__name__)


class BubbleNotFoundError(Exception):
    pass


@dataclass
class BubbleService:
    project_root: Path

    @property
    def bubbles_path(self) -> Path:
        from manga_autopilot.models.bubble import bubble_storage_filename

        return self.project_root / bubble_storage_filename()

    def _load(self) -> list[SpeechBubble]:
        if not self.bubbles_path.exists():
            return []
        try:
            raw = json.loads(self.bubbles_path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"could not parse {self.bubbles_path}: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError("bubbles.json must be a JSON array")
        return [SpeechBubble.model_validate(item) for item in raw]

    def _save(self, bubbles: list[SpeechBubble]) -> None:
        self.project_root.mkdir(parents=True, exist_ok=True)
        payload = [b.model_dump(mode="json") for b in bubbles]
        self.bubbles_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # --------------------------------------------------------------- CRUD
    def list_bubbles(self, panel_id: str | None = None) -> list[SpeechBubble]:
        bubbles = self._load()
        if panel_id is None:
            return bubbles
        return [b for b in bubbles if b.panel_id == panel_id]

    def upsert(self, bubble: SpeechBubble) -> SpeechBubble:
        bubbles = self._load()
        for i, existing in enumerate(bubbles):
            if existing.id == bubble.id:
                bubbles[i] = bubble
                self._save(bubbles)
                return bubble
        bubbles.append(bubble)
        self._save(bubbles)
        return bubble

    def delete(self, bubble_id: str) -> None:
        bubbles = self._load()
        new = [b for b in bubbles if b.id != bubble_id]
        if len(new) == len(bubbles):
            raise BubbleNotFoundError(bubble_id)
        self._save(new)

    def delete_for_panel(self, panel_id: str) -> int:
        bubbles = self._load()
        kept = [b for b in bubbles if b.panel_id != panel_id]
        removed = len(bubbles) - len(kept)
        if removed:
            self._save(kept)
        return removed

    # -------------------------------------------------------------- layout
    def layout_panel(
        self,
        panel: PanelLayout,
        bubbles: Iterable[SpeechBubble] | None = None,
    ) -> list[BubblePlacement]:
        candidates = (
            list(bubbles) if bubbles is not None else self.list_bubbles(panel.panel_id)
        )
        return place_bubbles(candidates, panel)


__all__ = ["BubbleNotFoundError", "BubbleService"]
