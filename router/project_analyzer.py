import json
import os
import subprocess
from pathlib import Path

from router.types import ProjectContext

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "target", "build", "dist", ".next", ".run"}
RISK_MARKERS = (
    "permissionset",
    "profiles/",
    "sharingrules",
    "sharing",
    "authorization",
    "auth/",
    "migrations/",
)


class ProjectAccessError(ValueError):
    pass


def git_changed_paths(data: bytes) -> list[str]:
    """Parse porcelain v1 -z, including rename source fields and untracked files."""
    records = data.split(b"\0")
    paths = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        status = record[:2]
        paths.append(os.fsdecode(record[3:]))
        if b"R" in status or b"C" in status:
            index += 1
    return sorted(set(paths))


class ProjectAnalyzer:
    def __init__(self, roots: list[Path], max_files: int):
        self.roots = [root.expanduser().resolve() for root in roots]
        self.max_files = max_files

    def analyze(self, requested: str | None) -> ProjectContext:
        if not self.roots:
            return ProjectContext()
        if not requested:
            return ProjectContext(scan_status="unavailable")
        path = Path(requested).expanduser().resolve()
        if not any(path == root or path.is_relative_to(root) for root in self.roots):
            raise ProjectAccessError("Project path is outside SMART_ROUTER_PROJECT_ROOTS")
        if not path.is_dir():
            return ProjectContext(scan_status="unavailable")

        stack: set[str] = set()
        count = 0
        partial = False

        def walk_error(_: OSError) -> None:
            nonlocal partial
            partial = True

        for current, directories, files in os.walk(path, followlinks=False, onerror=walk_error):
            directories[:] = sorted(
                name
                for name in directories
                if name not in SKIP_DIRS and not (Path(current) / name).is_symlink()
            )
            if "force-app" in directories:
                stack.add("salesforce")
            for name in sorted(files):
                count += 1
                if count > self.max_files:
                    partial = True
                    break
                file = Path(current) / name
                if file.is_symlink():
                    continue
                if name == "sfdx-project.json" or file.suffix in {".cls", ".trigger"}:
                    stack.add("salesforce")
                if name == "package.json":
                    package = self._small_text(file)
                    try:
                        manifest = json.loads(package)
                        if isinstance(manifest, dict):
                            deps = {
                                **manifest.get("dependencies", {}),
                                **manifest.get("devDependencies", {}),
                            }
                            if "react" in deps:
                                stack.add("react")
                    except (ValueError, TypeError):
                        pass
                if name in {"pom.xml", "build.gradle", "build.gradle.kts"}:
                    stack.add("java")
                    descriptor = self._small_text(file)
                    if "spring-boot" in descriptor or "org.springframework.boot" in descriptor:
                        stack.add("spring-boot")
            if count > self.max_files:
                break
        project = ProjectContext(
            stack=sorted(stack), scan_status="partial" if partial else "complete"
        )
        self._git_status(path, project)
        if (
            project.risk == "high"
            or any(
                changed.casefold().endswith(".trigger")
                or any(marker in changed.casefold() for marker in RISK_MARKERS)
                for changed in project.changed_paths
            )
            or sum(name.casefold().endswith(".cls") for name in project.changed_paths) >= 10
        ):
            project.risk = "high"
        elif project.files_changed >= 10 or any(
            name.endswith(".cls") for name in project.changed_paths
        ):
            project.risk = "medium"
        return project

    @staticmethod
    def _small_text(file: Path) -> str:
        try:
            with file.open("rb") as handle:
                return handle.read(65536).decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _git_status(self, path: Path, project: ProjectContext) -> None:
        try:
            common = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(path)]
            root = subprocess.run(
                [*common, "rev-parse", "--show-toplevel"],
                capture_output=True,
                timeout=3,
                check=False,
            )
            if root.returncode:
                project.git_status = "not_repository"
                return
            git_root = Path(os.fsdecode(root.stdout).strip()).resolve()
            if not any(
                git_root == allowed or git_root.is_relative_to(allowed) for allowed in self.roots
            ):
                project.git_status = "unavailable"
                return
            result = subprocess.run(
                [*common, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."],
                capture_output=True,
                timeout=3,
                check=False,
            )
            if result.returncode:
                project.git_status = "unavailable"
                return
            paths = git_changed_paths(result.stdout)
            project.files_changed = len(paths)
            project.changed_paths = paths[: self.max_files]
            project.git_status = "complete"
            if len(paths) > self.max_files:
                project.scan_status = "partial"
                project.risk = "high"
        except (OSError, subprocess.TimeoutExpired):
            project.git_status = "unavailable"
