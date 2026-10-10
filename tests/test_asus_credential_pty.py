"""Real controlling PTY tests; only fixed, nonsecret fixture input is used."""

import json
import os
import select
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

IMPORTER = Path(__file__).resolve().parents[1] / "deploy/asus/import_credentials.py"


@pytest.mark.parametrize("finish", ["input", "eof", "interrupt"])
def test_real_tty_noecho_restored_and_fds_closed(finish):
    fixture = "FIXTURE_ONLY_DO_NOT_ECHO_2026"
    code = f"""
import fcntl,termios,os,json,importlib.util,sys
fcntl.ioctl(0,termios.TIOCSCTTY,0)
spec=importlib.util.spec_from_file_location("pty_importer",{str(IMPORTER)!r})
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
before=termios.tcgetattr(0)
fds=len(os.listdir("/proc/self/fd"))
try:
 value=m.hidden_token()
 result={{"result":"returned","fixture_matched":value=={fixture!r}}}
except EOFError:
 result={{"result":"eof"}}
except KeyboardInterrupt:
 result={{"result":"interrupt"}}
result.update(echo_restored=termios.tcgetattr(0)==before,fd_delta=len(os.listdir("/proc/self/fd"))-fds)
print(json.dumps(result),flush=True)
"""
    master, slave = os.openpty()
    process = None
    output = b""
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", code],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        deadline = time.monotonic() + 8
        while b"Service Token (hidden): " not in output:
            assert time.monotonic() < deadline, "no hidden prompt on the real PTY"
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                output += os.read(master, 8192)
            assert process.poll() is None, "TTY reader exited before its prompt"
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        if finish == "input":
            os.write(master, fixture.encode() + b"\n")
        elif finish == "eof":
            os.write(master, b"\x04")
        else:
            os.write(master, b"\x03")
        while b'"fd_delta"' not in output:
            assert time.monotonic() < deadline, "TTY reader did not finish"
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                output += os.read(master, 8192)
        assert process.wait(timeout=2) == 0
        assert fixture.encode() not in output
        assert termios.tcgetattr(slave)[3] & termios.ECHO
        text = output.decode().replace("\r", "")
        report = json.JSONDecoder().raw_decode(text[text.index("{") :])[0]
        assert report["echo_restored"] and report["fd_delta"] == 0
        assert (
            report["result"]
            == {"input": "returned", "eof": "eof", "interrupt": "interrupt"}[finish]
        )
        if finish == "input":
            assert report["fixture_matched"]
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master)
        os.close(slave)
