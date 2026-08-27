"""Tests for the character planner (spec section 13)."""

from __future__ import annotations

import json

import pytest

from manga_autopilot.services.character_planner import (
    CHARACTER_LIST_SCHEMA,
    PROMPT_TEMPLATE,
    CharacterList,
    CharacterPlanner,
    CharacterSpec,
)


class _StubProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, prompt, *, schema=None, system=None) -> str:
        return self._responses.pop(0) if self._responses else "{}"

    async def complete_json(self, prompt, *, schema, system=None, max_repair_attempts=1):
        from manga_autopilot.services.llm_provider import enforce_json_schema

        text = await self.complete(prompt, schema=schema, system=system)
        try:
            return enforce_json_schema(text, schema)
        except ValueError:
            if max_repair_attempts <= 0:
                raise
            text2 = await self.complete("repair", schema=schema, system=system)
            return enforce_json_schema(text2, schema)


async def test_planner_builds_prompt() -> None:
    planner = CharacterPlanner(provider=_StubProvider([]))
    out = planner.build_prompt("An idea", {"title": "X"})
    assert "An idea" in out
    assert "title" in out


async def test_planner_parses_valid_payload() -> None:
    payload = {
        "characters": [
            {
                "id": "alice",
                "name": "Alice",
                "role": "protagonist",
                "age": "16",
                "appearance": "school uniform",
                "personality": "brave",
                "speech_style": "polite",
                "visual_traits": ["blue hair", "red eyes", "long"],
            },
            {
                "id": "bob",
                "name": "Bob",
                "role": "antagonist",
                "visual_traits": ["hooded cloak"],
            },
        ]
    }
    stub = _StubProvider([json.dumps(payload)])
    planner = CharacterPlanner(provider=stub)
    result = await planner.plan("An idea", {"title": "X"})
    assert isinstance(result, CharacterList)
    assert len(result.characters) == 2
    assert result.characters[0].id == "alice"
    assert len(result.characters[0].visual_traits) == 3


async def test_planner_rejects_missing_required() -> None:
    bad = json.dumps({"characters": [{"id": "alice", "name": "Alice"}]})  # no role
    stub = _StubProvider([bad, bad])
    planner = CharacterPlanner(provider=stub)
    with pytest.raises((ValueError, KeyError)):
        await planner.plan("An idea", {})


def test_character_list_schema_requires_characters() -> None:
    assert CHARACTER_LIST_SCHEMA["required"] == ["characters"]


def test_prompt_template_substitutes_variables() -> None:
    out = PROMPT_TEMPLATE.format(idea="A cat.", plan='{"title": "T"}')
    assert "A cat." in out
    assert '"title": "T"' in out


def test_character_spec_defaults() -> None:
    spec = CharacterSpec(id="x", name="X", role="supporting")
    assert spec.visual_traits == []
    assert spec.age == ""


# ------------------------------------------------- planner spec -> character
#
# The autopilot's define_characters hook used to hand a CharacterSpec straight
# to CharacterService.create(), which wants a Character. The AttributeError was
# swallowed into a warning, so a strict run silently ended up with no characters
# and failed much later with "character 'char_hero' is not defined".


def test_spec_to_character_reads_colours_out_of_the_traits() -> None:
    from manga_autopilot.services.character_planner import CharacterSpec, spec_to_character

    character = spec_to_character(
        CharacterSpec(
            id="char_hero",
            name="Hero",
            role="protagonist",
            visual_traits=["blue hair", "green eyes", "red scarf"],
        )
    )

    assert character.id == "char_hero"
    assert character.role == "protagonist"
    assert character.appearance.hair_color == "blue"
    assert character.appearance.eye_color == "green"


def test_spec_to_character_keeps_every_trait_verbatim() -> None:
    from manga_autopilot.services.character_planner import CharacterSpec, spec_to_character

    traits = ["blue hair", "green eyes", "red scarf", "left-handed"]
    character = spec_to_character(
        CharacterSpec(id="c", name="C", role="support", visual_traits=traits)
    )

    assert character.appearance.distinctive_features == traits
    assert character.consistency_prompt == ", ".join(traits)


def test_spec_to_character_says_unspecified_rather_than_inventing() -> None:
    from manga_autopilot.services.character_planner import (
        UNSPECIFIED,
        CharacterSpec,
        spec_to_character,
    )

    character = spec_to_character(CharacterSpec(id="c", name="C", role="support"))

    assert character.appearance.hair_color == UNSPECIFIED
    assert character.appearance.eye_color == UNSPECIFIED
    assert character.appearance.distinctive_features == []


def test_an_unknown_role_falls_back_to_support() -> None:
    from manga_autopilot.services.character_planner import CharacterSpec, spec_to_character

    assert spec_to_character(CharacterSpec(id="c", name="C", role="sidekick")).role == "support"
    assert spec_to_character(CharacterSpec(id="c", name="C", role="VILLAIN")).role == "villain"


def test_spec_to_character_produces_a_persistable_record(tmp_path) -> None:
    """The whole point: CharacterService.create() must accept the result."""
    from manga_autopilot.services.character_planner import CharacterSpec, spec_to_character
    from manga_autopilot.services.character_service import CharacterService

    service = CharacterService(project_root=tmp_path)
    character = spec_to_character(
        CharacterSpec(id="char_hero", name="Hero", role="protagonist", visual_traits=["blue hair"])
    )

    created = service.create(character)

    assert created.id == "char_hero"
    assert [c.id for c in service.list()] == ["char_hero"]
