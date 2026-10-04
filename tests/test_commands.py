"""Guest execution tests without a guest, AppleScript, or pytest-asyncio."""

import asyncio
import base64
import threading
from unittest.mock import Mock

import pytest

from mcp_utm import commands


def frame(stdout=b"", stderr=b"", exit_code="0", signal_code="-"):
    return "\n".join(("done", str(exit_code), str(signal_code),
                      base64.b64encode(stdout).decode(),
                      base64.b64encode(stderr).decode(), "END"))


@pytest.fixture(autouse=True)
def isolated_commands(monkeypatch):
    # asyncio.run creates a different loop in each test.
    monkeypatch.setattr(commands, "_jobs", {})
    monkeypatch.setattr(commands, "_start_lock", asyncio.Lock())
    monkeypatch.setattr(commands.utm, "get_vm_status", Mock(return_value="started"))
    monkeypatch.setattr(commands.utm, "_run", Mock(side_effect=["123", frame()]))


def test_awaited_success():
    result = asyncio.run(commands.run_vm_command("Ubuntu", "printf hello"))
    assert result == {
        "command_id": result["command_id"], "name": "Ubuntu", "pid": 123,
        "exited": True, "exit_code": 0, "signal_code": None,
        "stdout": "", "stderr": "", "stdout_truncated": False,
        "stderr_truncated": False, "timed_out": False,
    }
    commands.utm.get_vm_status.assert_called_once_with("Ubuntu")
    launch, fetch = [call.args[0] for call in commands.utm._run.call_args_list]
    assert 'with arguments {"-c", "printf hello"} with output capturing' in launch
    assert "guest process id 123" in fetch
    assert commands._jobs[result["command_id"]].completed_at is not None


@pytest.mark.parametrize("exit_code,signal_code,expected", [(7, "-", (7, None)), ("-", 9, (None, 9)), ("-", "-", (None, None))])
def test_exit_status(exit_code, signal_code, expected):
    commands.utm._run.side_effect = ["123", frame(stderr=b"failure", exit_code=exit_code, signal_code=signal_code)]
    result = asyncio.run(commands.run_vm_command("Ubuntu", "false"))
    assert result["exited"] is True
    assert (result["exit_code"], result["signal_code"]) == expected
    assert result["stderr"] == "failure"


@pytest.mark.parametrize("out,err", [(b" \n\t trailing \n", b"\n error \t"), ("你好 🌍\n".encode(), "é".encode()), (b"\xff\x00\xfe", b"\x80bad"), (b"", b"")])
def test_output_preserved(out, err):
    commands.utm._run.side_effect = ["123", frame(out, err)]
    result = asyncio.run(commands.run_vm_command("Ubuntu", "echo"))
    assert result["stdout"] == out.decode("utf-8", errors="replace")
    assert result["stderr"] == err.decode("utf-8", errors="replace")
    assert not result["stdout_truncated"] and not result["stderr_truncated"]


@pytest.mark.parametrize("out,err,flags", [(b"abcd", b"xy", (True, False)), (b"ab", b"xyz", (False, True)), (b"abc", b"xyz", (True, True)), (b"ab", b"xy", (False, False)), ("€".encode(), b"", (True, False))])
def test_output_cap(out, err, flags):
    commands.utm._run.side_effect = ["123", frame(out, err)]
    result = asyncio.run(commands.run_vm_command("Ubuntu", "echo", max_output_bytes=2))
    assert result["stdout"] == out[:2].decode("utf-8", errors="replace")
    assert result["stderr"] == err[:2].decode("utf-8", errors="replace")
    assert (result["stdout_truncated"], result["stderr_truncated"]) == flags


@pytest.mark.parametrize("shell,args", [("cmd.exe", ["/c"]), ("powershell.exe", ["-NoProfile", "-Command"]), ("cmd.exe", [])])
def test_windows_arguments_environment_and_stdin(shell, args):
    command = 'echo "hello" C:\\Temp'
    asyncio.run(commands.run_vm_command("Windows", command, shell=shell, shell_args=args,
                                        environment={"PATH": 'C:\\Temp\\"quoted"', "EMPTY": ""},
                                        stdin='line "one"\\two\n你好', wait=False))
    script = commands.utm._run.call_args.args[0]
    quoted_args = ', '.join('"' + arg + '"' for arg in args)
    if quoted_args:
        quoted_args += ', '
    assert f'execute vm at "{shell}" with arguments {{{quoted_args}"echo \\"hello\\" C:\\\\Temp"}}' in script
    assert 'with environment {"PATH=C:\\\\Temp\\\\\\"quoted\\"", "EMPTY="}' in script
    assert 'using input "line \\"one\\"\\\\two\n你好"' in script
    assert commands.utm._run.call_count == 1


def test_detached_poll_and_cache_returns_copies():
    async def scenario():
        started = await commands.run_vm_command("Ubuntu", "echo", wait=False)
        assert not started["exited"] and not started["timed_out"]
        assert started["exit_code"] is None and started["stdout"] == ""
        result = await commands.get_vm_command_result(started["command_id"])
        original = dict(result)
        result["stdout"] = "mutated"
        assert await commands.get_vm_command_result(started["command_id"], wait=True) == original
    asyncio.run(scenario())
    assert commands.utm._run.call_count == 2


def test_running_poll_then_wait():
    commands.utm._run.side_effect = ["123", "running", "running", frame(b"done")]
    async def scenario():
        started = await commands.run_vm_command("Ubuntu", "echo", wait=False)
        assert await commands.get_vm_command_result(started["command_id"]) == started
        assert commands._jobs[started["command_id"]].result is None
        result = await commands.get_vm_command_result(started["command_id"], wait=True)
        assert result["stdout"] == "done" and result["exited"]
    asyncio.run(scenario())
    assert commands.utm._run.call_count == 4


def test_timeout_then_later_completion_without_relaunch(monkeypatch):
    # Advance just the deadline checks, without sleeping for a real timeout.
    real_time = commands.time.monotonic
    ticks = iter([0, 0, 0, 0, 2])
    monkeypatch.setattr(commands, "time", Mock(monotonic=lambda: next(ticks, real_time())))
    commands.utm._run.side_effect = ["123", "running", frame(b"later")]
    async def scenario():
        result = await commands.run_vm_command("Ubuntu", "echo", timeout=1)
        assert result["timed_out"] and not result["exited"]
        completed = await commands.get_vm_command_result(result["command_id"])
        assert completed["stdout"] == "later" and not completed["timed_out"]
    asyncio.run(scenario())
    assert commands.utm._run.call_count == 3
    assert commands.utm.get_vm_status.call_count == 1


def test_concurrent_polls_consume_result_once():
    async def scenario():
        started = await commands.run_vm_command("Ubuntu", "echo", wait=False)
        results = await asyncio.gather(*(commands.get_vm_command_result(started["command_id"]) for _ in range(12)))
        assert all(result == results[0] for result in results)
        assert all(result is not results[0] for result in results[1:])
    asyncio.run(scenario())
    assert commands.utm._run.call_count == 2


@pytest.mark.parametrize("cancel", [True, False], ids=["cancellation", "slow-fetch-timeout"])
def test_slow_fetch_still_caches_completed_result(cancel):
    entered, release = threading.Event(), threading.Event()
    def slow_fetch(script):
        if "return id of proc" in script:
            return "123"
        entered.set()
        assert release.wait(5), "test did not release fetch"
        return frame(b"survived")
    commands.utm._run.side_effect = slow_fetch
    async def scenario():
        started = await commands.run_vm_command("Ubuntu", "echo", wait=False)
        waiter = asyncio.create_task(commands.get_vm_command_result(started["command_id"], timeout=1))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            if cancel:
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
            else:
                result = await waiter
                assert result["timed_out"] and not result["exited"]
        finally:
            release.set()
        # Poll on the same event loop. The shielded fetch owns the lock and caches.
        result = await commands.get_vm_command_result(started["command_id"])
        assert result["stdout"] == "survived" and result["exited"]
        assert commands._jobs[started["command_id"]].result == result
    asyncio.run(scenario())
    assert commands.utm._run.call_count == 2


@pytest.mark.parametrize("kwargs", [
    {"name": "bad\"name"}, {"command": ""}, {"command": "a\0b"}, {"command": 42},
    {"shell": ""}, {"shell": "a\0b"}, {"shell": 42},
    {"shell_args": "-c"}, {"shell_args": [1]}, {"shell_args": ["\0"]},
    {"environment": []}, {"environment": {"": "x"}}, {"environment": {"A=B": "x"}},
    {"environment": {1: "x"}}, {"environment": {"A\0": "x"}},
    {"environment": {"A": 1}}, {"environment": {"A": "\0"}},
    {"stdin": 1}, {"stdin": "\0"}, {"wait": 1},
    *({"timeout": value} for value in [0, 601, True, 1.5, "1"]),
    *({"max_output_bytes": value} for value in [0, 16777217, True, 1.5, "1"]),
])
def test_invalid_launch_input_before_subprocess(kwargs):
    with pytest.raises(ValueError):
        asyncio.run(commands.run_vm_command(**({"name": "Ubuntu", "command": "echo"} | kwargs)))
    commands.utm._run.assert_not_called()
    commands.utm.get_vm_status.assert_not_called()
    assert not commands._jobs


@pytest.mark.parametrize("kwargs", [{"command_id": "missing"}, {"command_id": None}, {"command_id": []}, {"command_id": "missing", "wait": 1}, *({"command_id": "missing", "timeout": x} for x in [0, 601, True, "1"])])
def test_invalid_poll_input(kwargs):
    with pytest.raises(ValueError):
        asyncio.run(commands.get_vm_command_result(**kwargs))
    commands.utm._run.assert_not_called()


@pytest.mark.parametrize("raw", ["", "done\n0\n-\n\n\nBAD", "done\n0\n-\n\nEND", "done\n0\n-\n\n\nEND\n", "oops\n0\n-\n\n\nEND", "done\nbad\n-\n\n\nEND", "done\n0\nbad\n\n\nEND", "done\n0\n-\n!\n\nEND", "done\n0\n-\n\n!\nEND"])
def test_malformed_result_is_not_cached(raw):
    commands.utm._run.side_effect = ["123", raw, frame()]
    async def scenario():
        started = await commands.run_vm_command("Ubuntu", "echo", wait=False)
        with pytest.raises(RuntimeError, match="Malformed UTM"):
            await commands.get_vm_command_result(started["command_id"])
        job = commands._jobs[started["command_id"]]
        assert job.result is None and job.completed_at is None
        assert (await commands.get_vm_command_result(started["command_id"]))["exited"]
    asyncio.run(scenario())


@pytest.mark.parametrize("pid", ["", "oops", "0", "-1", "1.5"])
def test_invalid_pid(pid):
    commands.utm._run.side_effect = [pid]
    with pytest.raises(RuntimeError, match="invalid guest process ID"):
        asyncio.run(commands.run_vm_command("Ubuntu", "echo"))
    assert not commands._jobs
    assert commands.utm._run.call_count == 1


@pytest.mark.parametrize("status", ["stopped", "paused", "starting"])
def test_vm_not_running(status):
    commands.utm.get_vm_status.return_value = status
    with pytest.raises(ValueError, match="VM must be running"):
        asyncio.run(commands.run_vm_command("Ubuntu", "echo"))
    commands.utm._run.assert_not_called()
    assert not commands._jobs


def test_ttl_cleanup_preserves_running_and_boundary(monkeypatch):
    monkeypatch.setattr(commands, "time", Mock(monotonic=lambda: 5000))
    for handle, completed_at in [("expired", 1399), ("boundary", 1400), ("recent", 4999), ("running", None)]:
        commands._jobs[handle] = commands._Job("Ubuntu", 1, 10, completed_at=completed_at)
    asyncio.run(commands.run_vm_command("Ubuntu", "echo", wait=False))
    assert "expired" not in commands._jobs
    assert {"boundary", "recent", "running"} <= commands._jobs.keys()
    assert len(commands._jobs) == 4


def test_registry_cap_and_expired_slot_reuse(monkeypatch):
    monkeypatch.setattr(commands, "time", Mock(monotonic=lambda: 5000))
    for index in range(commands._MAX_JOBS):
        commands._jobs[str(index)] = commands._Job("Ubuntu", index + 1, 10)
    with pytest.raises(RuntimeError, match="registry is full"):
        asyncio.run(commands.run_vm_command("Ubuntu", "echo", wait=False))
    commands.utm._run.assert_not_called()
    commands.utm.get_vm_status.assert_not_called()
    commands._jobs["0"].completed_at = 0
    result = asyncio.run(commands.run_vm_command("Ubuntu", "echo", wait=False))
    assert result["command_id"] in commands._jobs and "0" not in commands._jobs
    assert len(commands._jobs) == commands._MAX_JOBS
