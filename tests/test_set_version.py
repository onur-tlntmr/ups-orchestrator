import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = Path("packaging/rpm/ups-orchestrator.spec")


@pytest.fixture
def repo(tmp_path):
    """A throwaway git checkout holding just the script and the spec."""
    (tmp_path / "tools").mkdir()
    shutil.copy(ROOT / "tools" / "set-version.sh", tmp_path / "tools" / "set-version.sh")
    (tmp_path / SPEC).parent.mkdir(parents=True)
    spec = (ROOT / SPEC).read_text()
    # Pin a known starting point, including a bumped Release the new version must reset.
    spec = re.sub(r"(?m)^Version:(\s+).*$", r"Version:\g<1>0.1.0", spec)
    spec = re.sub(r"(?m)^Release:(\s+).*$", r"Release:\g<1>3%{?dist}", spec)
    (tmp_path / SPEC).write_text(spec)
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "tag", "v0.1.0"], check=True)
    return tmp_path


def run(repo, *args):
    return subprocess.run(
        ["bash", str(repo / "tools" / "set-version.sh"), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )


def spec_text(repo):
    return (repo / SPEC).read_text()


def test_sets_version_resets_release_and_adds_changelog_entry(repo):
    result = run(repo, "0.2.0")

    assert result.returncode == 0, result.stderr
    spec = spec_text(repo)
    assert re.search(r"(?m)^Version:\s+0\.2\.0$", spec)
    assert re.search(r"(?m)^Release:\s+1%\{\?dist\}$", spec)
    entry = spec.split("%changelog\n", 1)[1].splitlines()
    today = date.today().strftime("%a %b %d %Y")
    # Newest first, author carried over from the previous entry.
    assert entry[0] == f"* {today} Onur T. <onur@tatimur.com> - 0.2.0-1"
    assert entry[1].startswith("- TODO")
    assert 'git tag -a v0.2.0 -m "ups-orchestrator 0.2.0"' in result.stdout


def test_no_changelog_leaves_the_changelog_alone(repo):
    before = spec_text(repo).split("%changelog\n", 1)[1]

    result = run(repo, "0.2.0", "--no-changelog")

    assert result.returncode == 0, result.stderr
    assert spec_text(repo).split("%changelog\n", 1)[1] == before


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3", "1.2.3-rc1", "abc"])
def test_rejects_a_version_that_is_not_x_y_z(repo, bad):
    before = spec_text(repo)

    result = run(repo, bad)

    assert result.returncode != 0
    assert "X.Y.Z" in result.stderr
    assert spec_text(repo) == before


def test_rejects_a_version_whose_tag_already_exists(repo):
    before = spec_text(repo)

    result = run(repo, "0.1.0")

    assert result.returncode != 0
    assert "v0.1.0 already exists" in result.stderr
    assert spec_text(repo) == before


def test_show_prints_the_current_version_without_changing_anything(repo):
    before = spec_text(repo)

    result = run(repo, "--show")

    assert result.returncode == 0, result.stderr
    assert "0.1.0-3" in result.stdout
    assert spec_text(repo) == before
