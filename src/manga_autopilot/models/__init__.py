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
from manga_autopilot.models.generation_profile import (
    GenerationProfile,
    GenerationSettings,
    LicenseMetadata,
    ModelAssets,
    ResolutionPolicy,
    ResolvedResolution,
    SemanticPromptSegments,
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
    "GenerationProfile",
    "GenerationSettings",
    "JobStatus",
    "LicenseMetadata",
    "LoraRef",
    "ModelAssets",
    "ObjectSceneState",
    "Outfit",
    "OutputFormat",
    "Project",
    "ProjectGenerationSettings",
    "ProjectSettings",
    "ProjectStatus",
    "ResolutionPolicy",
    "ResolvedResolution",
    "SceneState",
    "SceneStateDelta",
    "SemanticPromptSegments",
    "StateEvent",
    "StateWarning",
    "StoryBible",
    "read_job",
    "write_job",
]
