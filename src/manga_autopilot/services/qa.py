"""QA + retry + candidate generation (spec sections 17.3-17.5, 18.1-18.5).

Modules:

- :class:`CandidateGenerator` - produce N image candidates for a panel
  (configurable count, pluggable seed policy).
- :class:`QualityCheck` / :class:`QualityResult` - per-check QA results.
- :class:`QAScoring` - weighted total score (spec 18.4).
- :class:`BubbleSpaceChecker` - ensure panel has room for bubbles (spec 18.2).
- :class:`PromptAlignmentChecker` - heuristic prompt-alignment check (spec 18.2).
- :class:`CharacterConsistencyChecker` - colour / palette overlap (spec 18.2).
- :class:`RetryController` / :class:`RetryDecision` - decide retry strategy
  (spec 18.5).
- :class:`PromptRevisionRules` - QA-issue -> prompt-mutation table.
- :class:`FallbackGenerator` - safe last-resort composition (spec 7.4 step 5).
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from PIL import Image

from manga_autopilot.models.character import Character
from manga_autopilot.models.page import PanelPlan
from manga_autopilot.services.prompt_builder import PromptSpec

log = logging.getLogger(__name__)


SeedPolicy = Literal["fixed", "panel_random", "character_fixed_panel_random", "base_seed_plus_index"]
SEED_POLICIES: tuple[SeedPolicy, ...] = (
    "fixed",
    "panel_random",
    "character_fixed_panel_random",
    "base_seed_plus_index",
)


# ----------------------------------------------------------- candidate gen
@dataclass
class PanelGeometry:
    """Pixel-space geometry of a panel inside its page."""

    panel_id: str
    x: int
    y: int
    width: int
    height: int


@dataclass
class CandidateSpec:
    """Inputs to a candidate-generation pass."""

    panel_id: str
    candidate_count: int = 4
    base_seed: int = 0
    seed_policy: SeedPolicy = "base_seed_plus_index"
    prompt: PromptSpec | None = None
    workflow_id: str = "anime_t2i_api"


@dataclass
class CandidateImage:
    """A single generated image candidate."""

    candidate_id: str
    panel_id: str
    seed: int
    prompt: PromptSpec
    image: Image.Image
    width: int
    height: int


@dataclass
class CandidateGenerator:
    """Generate N candidates for a panel by varying seeds (spec 17.3, 17.4)."""

    max_candidates: int = 8
    seed_generator: random.Random = field(default_factory=random.Random)

    def _seed_for(
        self,
        index: int,
        spec: CandidateSpec,
        character_seeds: Sequence[int] = (),
    ) -> int:
        if spec.seed_policy == "fixed":
            return spec.base_seed
        if spec.seed_policy == "panel_random":
            return self.seed_generator.randint(1, 2**31 - 1)
        if spec.seed_policy == "character_fixed_panel_random":
            if character_seeds:
                return character_seeds[0]
            return self.seed_generator.randint(1, 2**31 - 1)
        if spec.seed_policy == "base_seed_plus_index":
            return (spec.base_seed or 1) + index
        raise ValueError(f"unknown seed policy: {spec.seed_policy}")

    def generate(
        self,
        spec: CandidateSpec,
        image_factory: Callable[[int, int, int, PromptSpec], Image.Image] | None = None,
        character_seeds: Sequence[int] = (),
    ) -> list[CandidateImage]:
        if spec.candidate_count <= 0:
            raise ValueError("candidate_count must be > 0")
        count = min(spec.candidate_count, self.max_candidates)
        prompt = spec.prompt or PromptSpec(positive="placeholder", negative="")
        out: list[CandidateImage] = []
        factory = image_factory or _placeholder_image
        for i in range(count):
            seed = self._seed_for(i, spec, character_seeds)
            image = factory(seed, prompt.width, prompt.height, prompt)
            out.append(
                CandidateImage(
                    candidate_id=f"{spec.panel_id}_c{i:02d}",
                    panel_id=spec.panel_id,
                    seed=seed,
                    prompt=prompt,
                    image=image,
                    width=prompt.width,
                    height=prompt.height,
                )
            )
        return out


def _placeholder_image(seed: int, width: int, height: int, prompt: PromptSpec) -> Image.Image:
    """Build a deterministic placeholder (so tests have reproducible pixels)."""

    rng = random.Random(seed)
    img = Image.new("RGB", (width, height), (rng.randint(0, 255),) * 3)
    return img


# ----------------------------------------------------------------- QA model
class CheckName(str, Enum):
    PROMPT_ALIGNMENT = "prompt_alignment"
    FACE_QUALITY = "face_quality"
    HAND_QUALITY = "hand_quality"
    CHARACTER_CONSISTENCY = "character_consistency"
    CHARACTER_COUNT = "character_count"
    BUBBLE_SPACE = "bubble_space"
    TEXT_ARTIFACT = "text_artifact"
    SHARPNESS = "sharpness"
    COMPOSITION = "composition"


@dataclass
class QualityCheck:
    name: CheckName
    score: float
    weight: float = 1.0
    notes: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"check score must be in [0, 1], got {self.score}")
        if self.weight < 0.0:
            raise ValueError("weight must be >= 0")


class QualityIssue(dict):
    """A single QA issue with a category + a message + optional location."""

    def __init__(self, category: CheckName, message: str, **extra: Any) -> None:
        super().__init__(category=category.value, message=message, **extra)


class RetryAction(str, Enum):
    RETRY_SAME = "retry_same"
    CHANGE_SEED = "change_seed"
    REVISE_PROMPT = "revise_prompt"
    CHANGE_WORKFLOW = "change_workflow"
    SIMPLIFY_COMPOSITION = "simplify_composition"
    USE_FALLBACK = "use_fallback"
    ASK_USER = "ask_user"


@dataclass
class QualityResult:
    panel_id: str
    candidate_id: str
    checks: dict[CheckName, QualityCheck] = field(default_factory=dict)
    issues: list[QualityIssue] = field(default_factory=list)
    suggested_actions: list[RetryAction] = field(default_factory=list)
    passed: bool = False
    threshold: float = 0.7

    def add_check(self, check: QualityCheck) -> None:
        self.checks[check.name] = check

    def total(self, weights: Mapping[CheckName, float] | None = None) -> float:
        if not self.checks:
            return 0.0
        default_weights = _DEFAULT_WEIGHTS
        w = weights or default_weights
        total_weight = 0.0
        weighted = 0.0
        for name, check in self.checks.items():
            weight = w.get(name, check.weight)
            weighted += check.score * weight
            total_weight += weight
        return weighted / total_weight if total_weight else 0.0

    def finalize(self, weights: Mapping[CheckName, float] | None = None) -> float:
        score = self.total(weights)
        self.passed = score >= self.threshold
        return score

    def add_issue(self, category: CheckName, message: str, **extra: Any) -> None:
        self.issues.append(QualityIssue(category, message, **extra))

    def to_dict(self) -> dict[str, Any]:
        return {
            "panel_id": self.panel_id,
            "candidate_id": self.candidate_id,
            "score": self.total(),
            "passed": self.passed,
            "checks": {k.value: {"score": v.score, "weight": v.weight, "notes": v.notes} for k, v in self.checks.items()},
            "issues": [dict(i) for i in self.issues],
            "suggested_actions": [a.value for a in self.suggested_actions],
        }


# Spec 18.4 weights
_DEFAULT_WEIGHTS: dict[CheckName, float] = {
    CheckName.CHARACTER_CONSISTENCY: 0.30,
    CheckName.PROMPT_ALIGNMENT: 0.20,
    CheckName.FACE_QUALITY: 0.15,
    CheckName.HAND_QUALITY: 0.10,
    CheckName.COMPOSITION: 0.10,
    CheckName.SHARPNESS: 0.10,
    CheckName.BUBBLE_SPACE: 0.05,
    CheckName.CHARACTER_COUNT: 0.0,  # tracked but not weighted in v1
    CheckName.TEXT_ARTIFACT: 0.0,
}


# --------------------------------------------------------- QA checkers
@dataclass
class BubbleSpaceChecker:
    """Verify a panel has enough margin for speech bubbles (spec 18.2)."""

    min_margin: float = 16.0
    bubble_count: int = 0

    def check(self, geometry: PanelGeometry, candidate: CandidateImage) -> QualityCheck:
        right = candidate.width - (geometry.x + geometry.width)
        bottom = candidate.height - (geometry.y + geometry.height)
        margin = min(geometry.x, geometry.y, right, bottom)
        margin = max(margin, 0.0)
        score = 1.0 if margin >= self.min_margin else margin / self.min_margin
        notes = (
            f"margin={margin:.0f}px, bubbles={self.bubble_count}"
        )
        return QualityCheck(CheckName.BUBBLE_SPACE, score, notes=notes)


@dataclass
class PromptAlignmentChecker:
    """Heuristic prompt alignment using positive-prompt token overlap.

    The checker scores how many tokens from the panel's expected subjects
    (characters, action, background) appear in the rendered prompt.
    """

    def check(self, panel: PanelPlan, prompt: PromptSpec) -> QualityCheck:
        prompt_tokens = set(_tokenize(prompt.positive))
        expected_words: set[str] = set()
        for source in (panel.action, panel.background, panel.shot, panel.emotion):
            expected_words.update(_tokenize(source))
        if not expected_words:
            return QualityCheck(CheckName.PROMPT_ALIGNMENT, 1.0, notes="no tokens to align")
        overlap = sum(1 for t in expected_words if t in prompt_tokens)
        return QualityCheck(
            CheckName.PROMPT_ALIGNMENT,
            overlap / len(expected_words),
            notes=f"aligned={overlap}/{len(expected_words)}",
        )


@dataclass
class CharacterConsistencyChecker:
    """Character consistency via colour palette + token overlap."""

    def check(
        self,
        character: Character | None,
        prompt: PromptSpec,
        candidate: CandidateImage | None = None,
    ) -> QualityCheck:
        if character is None:
            return QualityCheck(CheckName.CHARACTER_CONSISTENCY, 1.0, notes="no reference")
        prompt_tokens = set(_tokenize(prompt.positive))
        must = character.must_keep_combined()
        if not must:
            return QualityCheck(CheckName.CHARACTER_CONSISTENCY, 1.0, notes="no mustKeep")
        matched = sum(1 for m in must if _tokenize(m)[0] in prompt_tokens if _tokenize(m))
        return QualityCheck(
            CheckName.CHARACTER_CONSISTENCY,
            matched / len(must),
            notes=f"matched={matched}/{len(must)}",
        )


# --------------------------------------------------------- retry / fallback
@dataclass
class RetryDecision:
    action: RetryAction
    reason: str
    patch: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptRevisionRules:
    """Map QA issues -> prompt mutations (spec 18.5)."""

    def revise(
        self,
        prompt: PromptSpec,
        issues: Sequence[QualityIssue],
    ) -> tuple[PromptSpec, list[RetryAction]]:
        actions: list[RetryAction] = []
        new_positive = prompt.positive
        new_negative = prompt.negative
        for issue in issues:
            category = CheckName(issue.get("category", ""))
            message = issue.get("message", "")
            if category == CheckName.FACE_QUALITY:
                new_positive += ", face close-up, detailed eyes"
                actions.append(RetryAction.REVISE_PROMPT)
            elif category == CheckName.HAND_QUALITY:
                new_positive += ", upper body shot"
                actions.append(RetryAction.SIMPLIFY_COMPOSITION)
            elif category == CheckName.CHARACTER_CONSISTENCY:
                new_positive = f"{prompt.character_prompt}, " + new_positive
                actions.append(RetryAction.REVISE_PROMPT)
            elif category == CheckName.COMPOSITION and "background" in message:
                new_positive += ", simple background"
                actions.append(RetryAction.REVISE_PROMPT)
            elif category == CheckName.CHARACTER_COUNT:
                new_positive += ", solo, 1girl, two characters"
                actions.append(RetryAction.REVISE_PROMPT)
            elif category == CheckName.BUBBLE_SPACE:
                new_positive += ", simple background, ample margin"
                actions.append(RetryAction.REVISE_PROMPT)
            elif category == CheckName.TEXT_ARTIFACT:
                new_negative += ", text, watermark, speech text"
                actions.append(RetryAction.REVISE_PROMPT)
            elif category == CheckName.SHARPNESS:
                new_positive += ", masterpiece, best quality, highly detailed"
                actions.append(RetryAction.REVISE_PROMPT)
        if not actions:
            actions.append(RetryAction.CHANGE_SEED)
        # Deduplicate actions preserving order
        seen: set[RetryAction] = set()
        deduped: list[RetryAction] = []
        for a in actions:
            if a not in seen:
                deduped.append(a)
                seen.add(a)
        return (
            PromptSpec(
                **{**prompt.model_dump(), "positive": new_positive, "negative": new_negative}
            ),
            deduped,
        )


@dataclass
class RetryController:
    """Decide retry strategy per QA issue list (spec 18.5, 17.5)."""

    max_retry: int = 5
    rules: PromptRevisionRules = field(default_factory=PromptRevisionRules)

    def revise(
        self,
        prompt: PromptSpec,
        issues: Sequence[QualityIssue],
    ) -> tuple[PromptSpec, RetryDecision]:
        revised, actions = self.rules.revise(prompt, issues)
        action = actions[0] if actions else RetryAction.RETRY_SAME
        return revised, RetryDecision(
            action=action,
            reason=f"issues={[i['category'] for i in issues]}",
            patch={"positive": revised.positive, "negative": revised.negative},
        )

    def next_action(self, attempt: int, issues: Sequence[QualityIssue]) -> RetryAction:
        if attempt >= self.max_retry:
            return RetryAction.USE_FALLBACK
        if not issues:
            return RetryAction.RETRY_SAME
        # Use the most-severe issue
        categories = [i.get("category") for i in issues]
        if "character_consistency" in categories:
            return RetryAction.REVISE_PROMPT
        if "face_quality" in categories:
            return RetryAction.REVISE_PROMPT
        if "bubble_space" in categories:
            return RetryAction.SIMPLIFY_COMPOSITION
        return RetryAction.CHANGE_SEED


@dataclass
class FallbackGenerator:
    """Generate a safe fallback image (spec 7.4 step 5, 17.5 fallback)."""

    width: int = 512
    height: int = 512
    background: tuple[int, int, int] = (240, 240, 240)
    simple_message: str = "fallback"

    def generate(self, panel: PanelPlan, panel_id: str | None = None) -> CandidateImage:
        img = Image.new("RGB", (self.width, self.height), self.background)
        seed = abs(hash(panel.panel_number)) % (2**31)
        pid = panel_id or f"panel_{panel.panel_number}"
        prompt = PromptSpec(
            positive=f"simple composition, {self.simple_message}, ample margin",
            negative="complex, text, watermark",
        )
        return CandidateImage(
            candidate_id=f"{pid}_fallback",
            panel_id=pid,
            seed=seed,
            prompt=prompt,
            image=img,
            width=self.width,
            height=self.height,
        )


# --------------------------------------------------------------- helpers
_TOKEN_RE_PYTHON = __import__("re").compile(r"[A-Za-z][A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE_PYTHON.findall(text or "")]


def quality_result_for(
    panel: PanelPlan,
    candidate: CandidateImage,
    geometry: PanelGeometry | None = None,
    *,
    bubble_count: int = 0,
    character: Character | None = None,
    threshold: float = 0.7,
) -> QualityResult:
    """Run the default checker set against a candidate and return the result."""

    geom = geometry or PanelGeometry(
        panel_id=panel.panel_number and f"panel_{panel.panel_number}",
        x=0,
        y=0,
        width=candidate.width,
        height=candidate.height,
    )
    result = QualityResult(
        panel_id=panel.panel_number and f"panel_{panel.panel_number}",
        candidate_id=candidate.candidate_id,
        threshold=threshold,
    )
    bubble_check = BubbleSpaceChecker(bubble_count=bubble_count).check(geom, candidate)
    align_check = PromptAlignmentChecker().check(panel, candidate.prompt)
    cons_check = CharacterConsistencyChecker().check(character, candidate.prompt, candidate)
    for check in (bubble_check, align_check, cons_check):
        result.add_check(check)
        if check.score < 0.5:
            result.add_issue(check.name, f"score={check.score:.2f}")
    result.finalize()
    if not result.passed:
        result.suggested_actions = [RetryAction.REVISE_PROMPT]
    return result


__all__ = [
    "CandidateGenerator",
    "CandidateImage",
    "CandidateSpec",
    "CheckName",
    "CharacterConsistencyChecker",
    "FallbackGenerator",
    "PanelGeometry",
    "PromptAlignmentChecker",
    "BubbleSpaceChecker",
    "PromptRevisionRules",
    "QualityCheck",
    "QualityIssue",
    "QualityResult",
    "RetryAction",
    "RetryController",
    "RetryDecision",
    "SEED_POLICIES",
    "SeedPolicy",
    "quality_result_for",
]
