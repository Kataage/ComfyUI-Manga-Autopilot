from __future__ import annotations

from manga_autopilot.models.bible import CharacterSpeech, StoryBible


def test_story_bible_round_trip_preserves_continuity_fields() -> None:
    bible = StoryBible(
        title="Rain Shrine",
        genre="fantasy",
        tone="quiet",
        theme="trust",
        world="modern Japan",
        rules=["spirits cannot cross the torii"],
        timeline=["evening: heroine reaches shrine"],
        locations={"old_shrine": "mountain shrine"},
        important_objects={"umbrella": "red paper umbrella"},
        relationships=["heroine protects cat"],
        foreshadowing=["cracked bell"],
        unresolved_events=["missing caretaker"],
    )

    restored = StoryBible.model_validate_json(bible.model_dump_json())

    assert restored == bible
    assert restored.locations["old_shrine"] == "mountain shrine"


def test_character_speech_tracks_required_voice_constraints() -> None:
    speech = CharacterSpeech(
        tone="formal",
        sentence_length="short",
        common_phrases=["そうですね"],
        forbidden_phrases=["マジで"],
    )

    assert speech.model_dump(mode="json") == {
        "tone": "formal",
        "sentence_length": "short",
        "common_phrases": ["そうですね"],
        "forbidden_phrases": ["マジで"],
    }

