"""Shell 工具安全校验回归测试。"""

from backend.tools.shell_tools import _decode_process_output, validate_command


def test_process_output_decodes_utf8_without_corruption() -> None:
    assert _decode_process_output("中文输出".encode("utf-8")) == "中文输出"


def test_process_output_decodes_windows_cp936_fallback() -> None:
    raw = "文件名 PATH 列表".encode("cp936")
    assert _decode_process_output(raw) == "文件名 PATH 列表"


def test_read_only_version_command_is_allowed() -> None:
    assert validate_command("npm --version") is True


def test_allowed_command_accepts_arguments_at_a_boundary() -> None:
    assert validate_command("npm run build --prefix frontend") is True


def test_prefix_collision_is_not_allowed() -> None:
    assert validate_command("npm --versioned") is False


def test_shell_composition_is_rejected_even_after_allowed_command() -> None:
    assert validate_command("npm --version && whoami") is False
    assert validate_command("npm --version > output.txt") is False
