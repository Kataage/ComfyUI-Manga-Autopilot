"""Pydantic models that live on disk."""

from manga_autopilot.models.bible import CharacterSpeech, StoryBible
from manga_autopilot.models.character import (
    AssetRef,
    Character,
    CharacterAppearance,
    ColorPalette,
    LoraRef,
    Outfit,
)
from manga_autopilot.models.job import (
    CandidateImageMeta,
    GenerationJob,
    JobStatus,
    read_job,
    write_job,
)
from manga_autopilot.models.project import (
    OutputFormat,
    Project,
    ProjectGenerationSettings,
    ProjectSettings,
    ProjectStatus,
)
from manga_autopilot.models.scene_state import (
    CharacterSceneState,
    ObjectSceneState,
    SceneState,
    SceneStateDelta,
    StateEvent,
    StateWarning,
)

__all__ = [
    "AssetRef",
    "CandidateImageMeta",
    "Character",
    "CharacterAppearance",
    "CharacterSceneState",
    "CharacterSpeech",
    "ColorPalette",
    "GenerationJob",
    "JobStatus",
    "LoraRef",
    "Outfit",
    "ObjectSceneState",
    "OutputFormat",
    "Project",
    "ProjectGenerationSettings",
    "ProjectSettings",
    "ProjectStatus",
    "SceneState",
    "SceneStateDelta",
    "StateEvent",
    "StateWarning",
    "StoryBible",
    "read_job",
    "write_job",
]
