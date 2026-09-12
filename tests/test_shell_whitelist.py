"""Shell 白名单与组合语法防逃逸回归测试。"""

from backend.tools.shell_tools import validate_command


def test_read_only_commands_are_enabled() -> None:
    for command in ("dir", "dir hello-dev", "node --check hello-dev/script.js",
                    "python -m compileall backend", "git diff --check", "where node"):
        assert validate_command(command)


def test_shell_composition_and_prefix_confusion_are_rejected() -> None:
    assert not validate_command("dir && whoami")
    assert not validate_command("dir | findstr secret")
    assert not validate_command("node --check; whoami")
    assert not validate_command("git status-malicious")
    assert not validate_command("npm run buildx")
