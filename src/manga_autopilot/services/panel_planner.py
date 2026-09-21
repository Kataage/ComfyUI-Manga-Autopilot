"""Panel Planner (spec section 14.5)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from manga_autopilot.models.page import Dialogue, PagePlan, PanelPlan, SoundEffect
from manga_autopilot.services.json_schema_validator import validate_llm_output
from manga_autopilot.services.llm_provider import LLMProvider
from manga_autopilot.services.semantic_validation import validate_panel_sequence

log = logging.getLogger(__name__)


PANEL_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["panels"],
    "properties": {
        "panels": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["panelNumber", "purpose", "shot", "action", "emotion"],
                "properties": {
                    "panelNumber": {"type": "integer", "minimum": 1, "maximum": 24},
                    "purpose": {"type": "string"},
                    "shot": {"type": "string"},
                    "cameraAngle": {"type": "string"},
                    "characters": {"type": "array", "items": {"type": "string"}},
                    "background": {"type": "string"},
                    "action": {"type": "string"},
                    "emotion": {"type": "string"},
                    "visualPriority": {
                        "type": "string",
                        "enum": ["character", "action", "background", "emotion"],
                    },
                    "dialogue": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["speaker", "text"],
                            "properties": {
                                "characterId": {"type": "string"},
                                "speaker": {"type": "string"},
                                "text": {"type": "string"},
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "speech",
                                        "thought",
                                        "narration",
                                        "whisper",
                                    ],
                                },
                                "balloonPosition": {
                                    "type": "string",
                                    "enum": ["top", "middle", "bottom", "free"],
                                },
                            },
                        },
                    },
                    "sfx": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["text"],
                            "properties": {
                                "text": {"type": "string"},
                                "intensity": {"type": "integer"},
                                "position": {
                                    "type": "string",
                                    "enum": ["top", "middle", "bottom", "free"],
                                },
                            },
                        },
                    },
                    "layoutId": {"type": "string"},
                    "sceneDelta": {
                        "type": "object",
                        "properties": {
                            "page_number": {"type": "integer", "minimum": 1},
                            "panel_number": {"type": "integer", "minimum": 1},
                            "events": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["kind"],
                                    "properties": {
                                        "kind": {
                                            "type": "string",
                                            "enum": [
                                                "set_location",
                                                "set_time",
                                                "set_weather",
                                                "set_emotion",
                                                "set_position",
                                                "set_clothing",
                                                "acquire_object",
                                                "drop_object",
                                                "transfer_object",
                                                "set_object_state",
                                            ],
                                        },
                                        "character_id": {"type": ["string", "null"]},
                                        "target_character_id": {"type": ["string", "null"]},
                                        "value": {"type": "string"},
                                        "state": {"type": "string"},
                                        "reason": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
    },
}


PROMPT_TEMPLATE = """あなたは漫画の演出家です。
以下の PagePlan を PanelPlan リストに展開してください。

条件:
- 出力はJSONのみ
- パネル数は {panel_count}
- 各パネルに panelNumber, purpose, shot, action, emotion を含める
- characters には、そのコマに登場する人物の id を Active Characters から選んで列挙する
  （id は Active Characters に載っているものだけを使う。人物が写らないコマだけ空配列）
- 必要なら dialogue / sfx を追加する
- 1吹き出し 40 文字以内を目安

PagePlan:
{plan}

Story Bible:
{story_bible}

Prior Scene State:
{scene_state}

Active Characters:
{active_characters}

Available Layouts:
{layouts}
"""


class PanelPlanList:
    """Container of panels for a single page."""

    def __init__(self, panels: list[PanelPlan]) -> None:
        self.panels = panels

    def to_dict(self) -> list[dict[str, Any]]:
        return [p.model_dump(mode="json") for p in self.panels]


@dataclass
class PanelPlanner:
    provider: LLMProvider
    panel_count: int = 4
    max_repair_attempts: int = 1
    strict: bool = False
    character_ids: set[str] = field(default_factory=set)
    layout_id: str | None = None
    registered_layout_ids: set[str] | None = None
    story_bible: Mapping[str, Any] | str | None = None
    scene_state: Mapping[str, Any] | str | None = None
    active_characters: list[Mapping[str, Any]] = field(default_factory=list)
    layouts: list[Mapping[str, Any]] = field(default_factory=list)
    system_prompt: str = (
        "You are a manga director. Always respond with strict JSON only."
    )

    def build_prompt(self, plan: PagePlan | Mapping[str, Any] | str) -> str:
        if isinstance(plan, PagePlan):
            plan = plan.model_dump(mode="json")
        if isinstance(plan, Mapping):
            plan = json.dumps(plan, ensure_ascii=False, indent=2)
        return PROMPT_TEMPLATE.format(
            panel_count=self.panel_count,
            plan=plan,
            story_bible=self._context_json(self.story_bible),
            scene_state=self._context_json(self.scene_state),
            active_characters=self._context_json(self.active_characters),
            layouts=self._context_json(self.layouts),
        )

    @staticmethod
    def _context_json(value: Any) -> str:
        if value is None:
            return "{}"
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    async def plan(self, page_plan: PagePlan | Mapping[str, Any] | str) -> PanelPlanList:
        prompt = self.build_prompt(page_plan)
        validation_options: dict[str, Any] = {}
        if self.strict:
            def _validate_semantics(data: dict[str, Any]) -> list[str]:
                return [
                    f"{issue.path}: {issue.message}"
                    for issue in validate_panel_sequence(
                        data.get("panels", []),
                        self.panel_count,
                        self.character_ids,
                        layout_id=self.layout_id,
                        registered_layout_ids=self.registered_layout_ids,
                    )
                ]

            validation_options["semantic_validator"] = _validate_semantics
        data = await self.provider.complete_json(
            prompt,
            schema=PANEL_PLAN_SCHEMA,
            system=self.system_prompt,
            max_repair_attempts=self.max_repair_attempts,
            **validation_options,
        )
        outcome = validate_llm_output(data, jsonschema_definition=PANEL_PLAN_SCHEMA)
        if not outcome.ok:
            raise ValueError(f"PanelPlanList validation failed: {outcome.errors}")
        return PanelPlanList([self._to_model(p) for p in data["panels"]])

    @staticmethod
    def _to_model(payload: Mapping[str, Any]) -> PanelPlan:
        return PanelPlan(
            panel_number=int(payload["panelNumber"]),
            purpose=payload.get("purpose", ""),
            shot=payload.get("shot", ""),
            camera_angle=payload.get("cameraAngle", ""),
            characters=list(payload.get("characters", []) or []),
            background=payload.get("background", ""),
            action=payload.get("action", ""),
            emotion=payload.get("emotion", ""),
            visual_priority=payload.get("visualPriority", "character"),
            layout_id=payload.get("layoutId"),
            scene_delta=payload.get("sceneDelta") or {},
            dialogue=[
                Dialogue(
                    character_id=d.get("characterId"),
                    speaker=d["speaker"],
                    text=d["text"],
                    type=d.get("type", "speech"),
                    balloon_position=d.get("balloonPosition", "top"),
                )
                for d in payload.get("dialogue", []) or []
            ],
            sfx=[
                SoundEffect(
                    text=s["text"],
                    intensity=int(s.get("intensity", 0)),
                    position=s.get("position", "free"),
                )
                for s in payload.get("sfx", []) or []
            ],
        )


__all__ = [
    "PanelPlanner",
    "PanelPlanList",
    "PANEL_PLAN_SCHEMA",
    "PROMPT_TEMPLATE",
]
