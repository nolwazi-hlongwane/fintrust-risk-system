"""Checks on the repository itself: things a reviewer looks for before reading any code."""

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".git", ".venv", "build", "__pycache__", ".pytest_cache", ".ruff_cache", "evidence", "node_modules"}
TEXT_SUFFIXES = {".py", ".sh", ".sql", ".json", ".md", ".yml", ".yaml", ".toml", ".txt", ".csv", ".html", ".js", ".css"}
LAMBDA_MODULES = ("ingest", "score", "alert", "query", "report")


def project_files():
    """Text files that are part of the project: what git tracks, or a directory walk if git is unavailable."""
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
        candidates = [ROOT / name for name in listed if name]
    except (OSError, subprocess.CalledProcessError):
        candidates = list(ROOT.rglob("*"))
    for path in candidates:
        if path.is_file() and not SKIP_DIRS & set(path.relative_to(ROOT).parts):
            if path.suffix in TEXT_SUFFIXES or path.name in (".gitignore", ".env.example"):
                yield path


class TestNoSecrets:
    def test_no_aws_access_keys_are_committed(self):
        pattern = re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")
        found = [str(path.relative_to(ROOT)) for path in project_files() if pattern.search(path.read_text(errors="ignore"))]
        assert found == []

    def test_no_private_keys_are_committed(self):
        marker = "-----BEGIN " + "{}PRIVATE KEY-----"
        variants = [marker.format(kind) for kind in ("", "RSA ", "EC ", "OPENSSH ")]
        found = [str(path.relative_to(ROOT)) for path in project_files()
                 if any(variant in path.read_text(errors="ignore") for variant in variants)]
        assert found == []

    def test_env_files_and_build_output_are_ignored_by_git(self):
        ignored = (ROOT / ".gitignore").read_text().splitlines()
        for entry in (".env", "build/", ".venv/"):
            assert entry in ignored, f"{entry} missing from .gitignore"

    def test_only_the_throwaway_test_password_appears_in_code(self):
        """No application code holds a password; the test database's is the documented exception."""
        pattern = re.compile(r"""password\s*=\s*["'][^"'{$]+["']""", re.IGNORECASE)
        for path in (ROOT / "fintrust").glob("*.py"):
            assert not pattern.search(path.read_text()), path.name


class TestDependencies:
    @pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt"])
    def test_every_dependency_is_pinned_to_an_exact_version(self, name):
        for line in (ROOT / name).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-r "):
                continue
            assert re.fullmatch(r"[A-Za-z0-9_.\-]+(\[[a-z,]+\])?==[0-9][0-9A-Za-z.]*", line), f"not pinned: {line}"

    def test_lambda_package_has_only_the_two_runtime_dependencies(self):
        names = [line.split("==")[0] for line in (ROOT / "requirements.txt").read_text().splitlines()
                 if line.strip() and not line.startswith("#")]
        assert names == ["boto3", "psycopg2-binary"]


class TestCodePatterns:
    @pytest.mark.parametrize("module", LAMBDA_MODULES)
    def test_boto3_clients_are_created_at_module_level_not_inside_the_handler(self, module):
        """Module level = once per Lambda container, reused by every invocation."""
        source = (ROOT / "fintrust" / f"{module}.py").read_text()
        for line in source.splitlines():
            if "boto3.client(" in line or "boto3.resource(" in line:
                assert not line.startswith((" ", "\t")), f"{module}.py creates a client inside a function: {line.strip()}"

    @pytest.mark.parametrize("module", LAMBDA_MODULES)
    def test_handlers_log_through_the_json_logger_only(self, module):
        source = (ROOT / "fintrust" / f"{module}.py").read_text()
        assert "get_logger(" in source
        assert not re.search(r"^\s*print\(", source, re.M), f"{module}.py uses print() directly"

    def test_only_db_py_talks_to_psycopg2(self):
        """Every query goes through db.transaction(), which is where rollback is guaranteed."""
        users = sorted(path.name for path in (ROOT / "fintrust").glob("*.py") if re.search(r"^(import|from) psycopg2", path.read_text(), re.M))
        assert users == ["db.py"]


class TestInfrastructureFiles:
    def test_every_json_file_is_valid_json(self):
        for path in project_files():
            if path.suffix == ".json" and "infrastructure" in path.parts:
                json.loads(path.read_text())

    def test_every_shell_script_stops_on_the_first_error(self):
        for path in project_files():
            if path.suffix != ".sh" or path.name == "api_env.sh":      # api_env.sh is sourced, not run
                continue
            text = path.read_text()
            assert "set -euo pipefail" in text or "common.sh" in text, f"{path.relative_to(ROOT)} has no error handling"

    def test_every_shell_script_has_valid_syntax(self):
        for path in project_files():
            if path.suffix == ".sh":
                result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
                assert result.returncode == 0, f"{path.relative_to(ROOT)}: {result.stderr}"

    def test_ci_runs_unit_tests_on_every_push_and_integration_on_develop_only(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        assert re.search(r"^on:\n  push:\n", workflow, re.M)                      # every push, any branch
        assert "if: github.ref == 'refs/heads/develop'" in workflow                # integration scope
        assert "pytest tests/unit" in workflow and "pytest tests/integration" in workflow
