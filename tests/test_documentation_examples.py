"""Keep copyable documentation examples aligned with the tool interface."""

import base64
import contextlib
import inspect
import io
import json
from pathlib import Path
import re
import runpy

from mcp_utm.commands import run_vm_command, get_vm_command_result

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = runpy.run_path(str(ROOT / "examples/windows_desktop_alert.py"))
GUIDE = (ROOT / "docs/guest-commands.md").read_text()


def test_documented_json_matches_tool_signatures():
    snippets = re.findall(r"```json\n(.*?)\n```", GUIDE, re.S)
    assert len(snippets) == 5
    for snippet in snippets:
        arguments = json.loads(snippet)
        function = get_vm_command_result if "command_id" in arguments else run_vm_command
        inspect.signature(function).bind(**arguments)


def test_powershell_encoding_and_literal_escaping():
    text = "你好 'quote' 🌍"
    assert base64.b64decode(EXAMPLE["encode_powershell"](text)).decode("utf-16le") == text
    assert EXAMPLE["ps_literal"]("It's a title") == "'It''s a title'"
    script = EXAMPLE["desktop_script"]("It's an alert: 你好", "Title's alert")
    encoded = re.search(r"-EncodedCommand ([A-Za-z0-9+/=]+)'", script).group(1)
    alert = base64.b64decode(encoded).decode("utf-16le")
    assert "'It''s an alert: 你好'" in alert
    assert "'Title''s alert'" in alert
    assert "-LogonType Interactive -RunLevel Limited" in script
    assert "Unregister-ScheduledTask" in script and "finally" in script
    assert "ALERT64" not in script


def test_documented_argument_generator_runs():
    snippet = re.search(r"uv run python - <<'PY'\n(.*?)\nPY", GUIDE, re.S).group(1)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(compile(snippet, "documented-argument-generator", "exec"), {})
    arguments = json.loads(output.getvalue())
    inspect.signature(run_vm_command).bind(**arguments)
    assert arguments["shell_args"][-1] == "-EncodedCommand"
    decoded = base64.b64decode(arguments["command"]).decode("utf-16le")
    assert "SessionId" in decoded and "OutputEncoding" in decoded


def test_documentation_relative_file_links_exist():
    paths = re.findall(r"\]\(([^)]+)\)", GUIDE)
    for path in paths:
        if not path.startswith("https:") and not path.startswith("#"):
            assert (ROOT / "docs" / path).exists()
    readme = (ROOT / "README.md").read_text()
    assert "](docs/guest-commands.md)" in readme
    assert "](examples/windows_desktop_alert.py)" in readme
