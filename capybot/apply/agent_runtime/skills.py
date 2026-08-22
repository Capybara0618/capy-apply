"""Discover standard Apply Skill directories and disclose their bodies on demand."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillDescriptor:
    name: str
    description: str
    path: Path


class ApplySkillLibrary:
    ROOT = Path(__file__).resolve().parent.parent / "skills"
    TOOL_PREFIX = "skill_"
    SCOPE_NAMES = {
        "opportunity": (
            "grounded-candidate-communication",
            "interview-preparation",
            "opportunity-due-diligence",
        ),
        "fit": (),
    }
    TOOL_GRANTS = {
        "grounded-candidate-communication": (
            "memory_read",
            "profile_read",
            "job_read",
            "boss_fetch_job_detail",
        ),
        "opportunity-due-diligence": (
            "memory_read",
            "job_read",
            "boss_fetch_job_detail",
            "research_company",
        ),
        "interview-preparation": (
            "memory_read",
            "job_read",
            "profile_read",
            "boss_fetch_job_detail",
            "research_company",
        ),
    }

    def discover(self, scope: str = "opportunity") -> list[dict[str, str]]:
        result = []
        for name in self.names(scope):
            path = self._path(name)
            if not path.exists():
                continue
            metadata, _ = self._parse(path)
            result.append(
                {
                    "name": name,
                    "description": metadata["description"],
                    "version": self._hash(path)[:12],
                }
            )
        return result

    def load(self, name: str, *, scope: str | None = None) -> dict[str, Any]:
        allowed = set(self.names(scope or "all"))
        if name not in allowed:
            raise ValueError(f"未知 Apply Skill: {name}")
        path = self._path(name)
        metadata, content = self._parse(path)
        return {
            "name": name,
            "description": metadata["description"],
            "content": content,
            "content_hash": self._hash(path),
            "path": str(path),
            "tool_hints": list(self.tool_grants(name)),
        }

    @classmethod
    def tool_grants(cls, name: str) -> tuple[str, ...]:
        if name not in cls.names("all"):
            raise ValueError(f"Unknown Apply Skill: {name}")
        return cls.TOOL_GRANTS.get(name, ())

    @classmethod
    def tool_name(cls, name: str) -> str:
        if name not in cls.names("all"):
            raise ValueError(f"Unknown Apply Skill: {name}")
        return f"{cls.TOOL_PREFIX}{name.replace('-', '_')}"

    @classmethod
    def names(cls, scope: str = "opportunity") -> list[str]:
        if scope == "all":
            return sorted(name for names in cls.SCOPE_NAMES.values() for name in names)
        try:
            return sorted(cls.SCOPE_NAMES[scope])
        except KeyError as exc:
            raise ValueError(f"未知 Skill scope: {scope}") from exc

    def _path(self, name: str) -> Path:
        return self.ROOT / name / "SKILL.md"

    @staticmethod
    def _parse(path: Path) -> tuple[dict[str, str], str]:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError(f"Skill 缺少 YAML frontmatter: {path}")
        try:
            end = next(
                index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
            )
        except StopIteration as exc:
            raise ValueError(f"Skill frontmatter 未闭合: {path}") from exc
        metadata: dict[str, str] = {}
        for line in lines[1:end]:
            key, separator, value = line.partition(":")
            if separator:
                metadata[key.strip()] = value.strip()
        expected_name = path.parent.name
        if metadata.get("name") != expected_name:
            raise ValueError(f"Skill name 与目录不一致: {path}")
        if not metadata.get("description"):
            raise ValueError(f"Skill 缺少 description: {path}")
        content = "\n".join(lines[end + 1 :]).strip()
        if not content:
            raise ValueError(f"Skill 正文为空: {path}")
        return metadata, content

    @staticmethod
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
