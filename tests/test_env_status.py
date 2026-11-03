"""scripts/env_status.py: SET or MISSING, never a value; and it says when the protected file is not protected."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import env_status as E  # noqa: E402

SECRET = "sb_secret_THIS-VALUE-MUST-NEVER-BE-PRINTED-0123456789"


def make(tmp_path, mode=0o600):
    f = tmp_path / ".env"
    f.write_text(f"STAGING_PROJECT_REF=abc\nKnovent_Servicez_Key={SECRET}\nENGINE_PIPELINE=\n# a comment\nexport Knovent_Publishable_Key='sb_publishable_x'\n")
    f.chmod(mode)
    return f


def test_names_are_reported_set_or_missing_and_no_value_is_printed(tmp_path, capsys):
    f = make(tmp_path)
    assert E.main(["--file", str(f)], env={"ENGINE_SERVICE_SECRET": "from-the-environment"}) == 0
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err and "sb_publishable_x" not in out.out and "from-the-environment" not in out.out and "abc" not in out.out
    assert "Knovent_Servicez_Key" in out.out and "SET (file)" in out.out and "ENGINE_SERVICE_SECRET" in out.out and "SET (environment)" in out.out and "MISSING" in out.out
    result, issues = E.status(f, {})
    assert result["Knovent_Servicez_Key"] == "SET (file)" and result["STAGING_PROJECT_REF"] == "SET (file)" and result["ENGINE_PIPELINE"] == "MISSING"  # an empty value is not set
    assert result["STAGING_DATABASE_URL"] == "MISSING" and issues == []


def test_a_file_other_users_can_read_is_a_problem(tmp_path, capsys):
    f = make(tmp_path, 0o644)
    assert E.main(["--file", str(f)], env={}) == 1
    assert "can be read by other users" in capsys.readouterr().out


def test_a_missing_file_is_reported_not_crashed_on(tmp_path, capsys):
    assert E.main(["--file", str(tmp_path / "nope")], env={}) == 1
    assert "does not exist" in capsys.readouterr().out


def test_a_file_inside_the_repository_is_a_problem():
    f = ROOT / "tests" / "_tmp_env_for_test"
    f.write_text("A=1\n")
    try:
        f.chmod(0o600)
        assert any("inside the repository" in p for p in E.problems(f))
    finally:
        f.unlink()


def test_the_repository_ignores_env_files():
    ignore = (ROOT / ".gitignore").read_text()
    for pattern in (".env", ".env.*", "*.env", "*.pem", "*.key", "secrets/"):
        assert pattern in ignore.splitlines(), pattern
