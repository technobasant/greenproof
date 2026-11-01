"""SHA pins for every third-party GitHub Action greenproof writes (SPEC section 6.5).

Each pin is a full 40-character commit SHA with the release it was resolved from. The
generated workflow and `action.yml` both use these, and a test keeps them in step.
Resolved 2026-11-01 with `gh api repos/OWNER/REPO/releases/latest` and, for annotated tags,
dereferenced through `git/tags/SHA` to the commit.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pin:
    repo: str
    sha: str
    tag: str

    def uses(self) -> str:
        """The text after `uses: ` in a workflow step."""
        return f"{self.repo}@{self.sha} # {self.tag}"


CHECKOUT = Pin("actions/checkout", "3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1")
SETUP_UV = Pin("astral-sh/setup-uv", "1c37ad07a6a961277cf70c0d37d6f313000f5884", "v10.3.0")
UPLOAD_ARTIFACT = Pin(
    "actions/upload-artifact", "cf430e030ddbb5b0abf93d22962f4752f3646cd9", "v7.0.2"
)
SETUP_PYTHON = Pin("actions/setup-python", "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0", "v7.1.0")
SETUP_NODE = Pin("actions/setup-node", "949feb2413d6458794dcd2491c4babbbce0c15c1", "v7.1.0")
SETUP_GO = Pin("actions/setup-go", "b7ad1dad31e06c5925ef5d2fc7ad053ef454303e", "v7.0.0")
SETUP_BUN = Pin("oven-sh/setup-bun", "0c5077e51419868618aeaa5fe8019c62421857d6", "v2.2.0")

ALL_PINS: tuple[Pin, ...] = (
    CHECKOUT,
    SETUP_UV,
    UPLOAD_ARTIFACT,
    SETUP_PYTHON,
    SETUP_NODE,
    SETUP_GO,
    SETUP_BUN,
)
