import json
import subprocess

import pytest

from router.project_analyzer import ProjectAccessError, ProjectAnalyzer, git_changed_paths


def test_salesforce_react_and_spring_boot_recognition(tmp_path):
    (tmp_path / "force-app").mkdir()
    (tmp_path / "sfdx-project.json").write_text("{}")
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "19"}}))
    (tmp_path / "pom.xml").write_text("<artifactId>spring-boot-starter</artifactId>")
    context = ProjectAnalyzer([tmp_path], 100).analyze(str(tmp_path))
    assert context.stack == ["java", "react", "salesforce", "spring-boot"]
    assert context.git_status == "not_repository"


def test_java_descriptor_alone_does_not_imply_spring_boot(tmp_path):
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }")
    assert ProjectAnalyzer([tmp_path], 100).analyze(str(tmp_path)).stack == ["java"]


def test_allowlist_blocks_traversal_and_symlink_escape(tmp_path):
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / "sfdx-project.json").write_text("{}")
    (allowed / "escape").symlink_to(outside, target_is_directory=True)
    analyzer = ProjectAnalyzer([allowed], 100)
    for requested in [outside, allowed / ".." / "outside", allowed / "escape"]:
        with pytest.raises(ProjectAccessError):
            analyzer.analyze(str(requested))
    assert analyzer.analyze(str(allowed)).stack == []


def test_disabled_scan_never_reads_requested_root():
    context = ProjectAnalyzer([], 10).analyze("/private")
    assert context.scan_status == "disabled"


def test_scan_limits_and_node_modules_are_observed(tmp_path):
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "Hidden.cls").write_text("class Hidden {}")
    for index in range(4):
        (tmp_path / f"file{index}.txt").write_text("test")
    context = ProjectAnalyzer([tmp_path], 2).analyze(str(tmp_path))
    assert context.scan_status == "partial"
    assert context.stack == []


def test_git_status_is_read_only_and_detects_high_risk_diff(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "001.sql").write_text("CREATE TABLE example (id INT);")
    context = ProjectAnalyzer([tmp_path], 100).analyze(str(tmp_path))
    assert context.files_changed == 1
    assert context.changed_paths == ["migrations/001.sql"]
    assert context.risk == "high"
    assert not (tmp_path / ".git" / "index.lock").exists()


def test_porcelain_rename_and_spaces():
    paths = git_changed_paths(b"R  new file.cls\0old file.cls\0 M second.css\0?? third.js\0")
    assert paths == ["new file.cls", "second.css", "third.js"]


@pytest.mark.parametrize("suffix", [".cls", ".trigger", ".py", ".go", ".rs", ".unknown"])
def test_source_extension_alone_does_not_raise_risk(tmp_path, suffix):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ("sample" + suffix)).write_text("test")
    assert ProjectAnalyzer([tmp_path], 100).analyze(str(tmp_path)).risk == "low"


@pytest.mark.parametrize("count,risk", [(9, "low"), (10, "medium"), (20, "high")])
def test_change_scope_uses_same_threshold_for_any_project(tmp_path, count, risk):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for index in range(count):
        (tmp_path / f"file{index}.unknown").write_text("test")
    assert ProjectAnalyzer([tmp_path], 100).analyze(str(tmp_path)).risk == risk
