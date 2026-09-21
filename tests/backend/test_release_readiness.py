"""Release readiness tests for v0.1.0-rc1.

Verifies that all required release artifacts exist and contain expected
content.  These tests do NOT require external services.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------- existence tests


class TestReleaseArtifactsExist:
    def test_release_notes_exist(self):
        path = _REPO_ROOT / "docs" / "release" / "v0.1.0-rc1_release_notes.md"
        assert path.exists(), f"Missing: {path}"

    def test_env_inventory_exists(self):
        path = _REPO_ROOT / "docs" / "release" / "v0.1.0-rc1_env_inventory.md"
        assert path.exists(), f"Missing: {path}"

    def test_known_limitations_exist(self):
        path = _REPO_ROOT / "docs" / "release" / "v0.1.0-rc1_known_limitations.md"
        assert path.exists(), f"Missing: {path}"

    def test_acceptance_matrix_exists(self):
        path = _REPO_ROOT / "docs" / "release" / "v1_acceptance_matrix.md"
        assert path.exists(), f"Missing: {path}"

    def test_post_release_smoke_doc_exists(self):
        path = _REPO_ROOT / "docs" / "release" / "v0.1.0-rc1_post_release_smoke.md"
        assert path.exists(), f"Missing: {path}"


# -------------------------------------------------------- version tests


class TestVersionMetadata:
    """Pin the relationships, not the number.

    Both of these used to assert the literal ``0.1.0-rc1``, so every release
    edited the test alongside the thing it was checking - which is a test that
    can only ever agree with whatever was just written. What matters is that
    the two declarations agree and that the release they name has notes.
    """

    @staticmethod
    def _pyproject_version() -> str:
        content = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"(.+?)"', content, re.MULTILINE)
        assert match, "version not found in pyproject.toml"
        return match.group(1)

    def test_pyproject_and_package_versions_agree(self):
        content = (_REPO_ROOT / "src" / "manga_autopilot" / "__init__.py").read_text(
            encoding="utf-8"
        )
        match = re.search(r'^__version__\s*=\s*"(.+?)"', content, re.MULTILINE)
        assert match, "__version__ not found in manga_autopilot/__init__.py"
        assert match.group(1) == self._pyproject_version(), (
            "pyproject.toml and manga_autopilot.__version__ disagree; the "
            "release checklist requires both to be bumped together"
        )

    def test_the_declared_version_has_release_notes(self):
        version = self._pyproject_version()
        path = _REPO_ROOT / "docs" / "release" / f"v{version}_release_notes.md"
        assert path.is_file(), (
            f"no release notes for the declared version: {path.name}"
        )

    def test_the_changelog_leads_with_the_declared_version(self):
        version = self._pyproject_version()
        content = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        headings = re.findall(r"^## (.+)$", content, re.MULTILINE)
        assert headings, "CHANGELOG.md has no version headings"
        assert headings[0].strip() == version, (
            f"CHANGELOG.md leads with {headings[0].strip()!r}, "
            f"but pyproject declares {version!r}"
        )

    def test_readme_mentions_v0_1_0_rc1(self):
        content = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        # Check for version mention in any form (v0.x, v1.0.0, 0.1.0)
        assert "v0.x" in content or "0.1.0" in content or "v1.0" in content

    def test_readme_ja_mentions_v0_1_0_rc1(self):
        content = (_REPO_ROOT / "README.ja.md").read_text(encoding="utf-8")
        assert "v0.x" in content or "0.1.0" in content or "v1.0" in content


# ------------------------------------------------------ README consistency


class TestReadmeConsistency:
    def test_readme_has_key_sections(self):
        content = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        sections = [
            "Modal Worker",
            "Artifact Store",
            "Signed",
            "remote executor",
        ]
        for section in sections:
            assert section.lower() in content.lower(), f"Missing section: {section}"

    def test_readme_ja_has_key_sections(self):
        content = (_REPO_ROOT / "README.ja.md").read_text(encoding="utf-8")
        sections = [
            "Modal",
            "アーティファクト",
            "署名",
            "リモート",
        ]
        for section in sections:
            assert section in content, f"Missing section in ja: {section}"


# -------------------------------------------------------- security scans


class TestSecurityScan:
    def test_no_model_files_committed(self):
        result = subprocess.run(
            ["find", str(_REPO_ROOT), "-name", "*.safetensors",
             "-o", "-name", "*.ckpt", "-o", "-name", "*.pt", "-o", "-name", "*.pth"],
            capture_output=True, text=True, timeout=10,
        )
        files = [f for f in result.stdout.strip().split("\n") if f]
        assert len(files) == 0, f"Model files found: {files}"

    def test_no_env_files_committed(self):
        result = subprocess.run(
            ["find", str(_REPO_ROOT), "-name", ".env",
             "-not", "-path", str(_REPO_ROOT / ".git") + "/*"],
            capture_output=True, text=True, timeout=10,
        )
        files = [f for f in result.stdout.strip().split("\n") if f]
        assert len(files) == 0, f".env files found: {files}"

    def test_no_example_secret_values(self):
        """Check that README/docs don't contain real-looking secrets."""
        for md_file in _REPO_ROOT.glob("*.md"):
            content = md_file.read_text(encoding="utf-8")
            # Real AWS key pattern: 20 uppercase chars starting with AKIA
            assert not re.search(r"AKIA[0-9A-Z]{16}", content), (
                f"Real AWS key found in {md_file.name}"
            )

    def test_git_grep_no_real_secrets(self):
        result = subprocess.run(
            ["git", "grep", "-n", "AKIA[0-9A-Z]\\{16\\}", "--", "."],
            capture_output=True, text=True, cwd=str(_REPO_ROOT), timeout=10,
        )
        assert result.returncode != 0, f"Real secrets found: {result.stdout}"


# ----------------------------------------------------- opt-in test skips


class TestOptInSkips:
    def test_real_comfy_e2e_skipped_by_default(self):
        import os
        assert os.environ.get("MANGA_AUTOPILOT_REAL_COMFYUI_E2E") != "1"

    def test_real_modal_e2e_skipped_by_default(self):
        import os
        assert os.environ.get("MANGA_AUTOPILOT_REAL_MODAL_E2E") != "1"

    def test_real_modal_comfyui_e2e_skipped_by_default(self):
        import os
        assert os.environ.get("MANGA_AUTOPILOT_REAL_MODAL_COMFYUI_E2E") != "1"

    def test_real_s3_e2e_skipped_by_default(self):
        import os
        assert os.environ.get("MANGA_AUTOPILOT_REAL_S3_E2E") != "1"
