"""Real terminal teardown and native exec, using only temporary fake chats/agents."""
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

from conftest import CLAUDE_ID


def test_real_terminal_confirmation_exec_and_restoration(populated):
    result = populated.root / "native-result.json"
    ready = populated.root / "browser-ready"
    confirmed = populated.root / "confirmation-ready"
    agent = populated.root / "native-agent"
    agent.write_text(f"#!{sys.executable}\n" + "import json, os, sys, termios\n" +
                     "if '--help' in sys.argv:\n print('--resume SESSION_ID'); sys.exit(0)\n" +
                     f"with open({str(result)!r}, 'w') as stream:\n" +
                     " json.dump({'pid': os.getpid(), 'cwd': os.getcwd(), 'argv': sys.argv[1:], "
                     "'tty_flags': termios.tcgetattr(0)[3]}, stream)\n" +
                     "sys.exit(23)\n")
    agent.chmod(0o700)
    code = f'''
from pathlib import Path
from ai_bridge_cli import activity
from ai_bridge_cli.tui import app
activity.snapshot = lambda: activity.Activity()
Original = app.SessionBrowser
class AutomatedBrowser(Original):
    CSS_PATH = str(Path(app.__file__).with_name("app.tcss"))
    def on_mount(self):
        super().on_mount()
        self.ready_sent = False
        self.set_interval(0.05, self.signal_ready)
    def signal_ready(self):
        if self.loaded and not self.ready_sent:
            self.ready_sent = True
            row = [s.selector for s in self.shown].index('claude:{CLAUDE_ID}')
            self._q('#table').move_cursor(row=row)
            self._q('#table').focus()
            Path({str(ready)!r}).touch()
    def _confirm_launch(self, session, plan):
        super()._confirm_launch(session, plan)
        Path({str(confirmed)!r}).touch()
app.SessionBrowser = AutomatedBrowser
raise SystemExit(app.run())
'''
    master, slave = pty.openpty()
    flags_before = termios.tcgetattr(slave)[3]
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    driver = populated.root / "browser_driver.py"
    driver.write_text(code)
    child = subprocess.Popen([sys.executable, str(driver)], stdin=slave, stdout=slave, stderr=slave,
                             env={**os.environ, "TERM": "xterm-256color",
                                  "AI_BRIDGE_CLAUDE_BIN": str(agent)})
    output = bytearray()

    def until(predicate):
        deadline = time.monotonic() + 20
        while not predicate():
            assert time.monotonic() < deadline, output.decode(errors="replace")[-4000:]
            if select.select([master], [], [], 0.05)[0]:
                output.extend(os.read(master, 65536))
            assert child.poll() is None, output.decode(errors="replace")[-4000:]

    try:
        until(ready.exists)
        assert not result.exists()  # no launch before explicit Open
        os.write(master, b"o")
        until(confirmed.exists)
        assert not result.exists()  # preflight and dialog alone do not launch
        # Let the dialog mount and focus its confirmation button before Enter.
        until(lambda: b"closes completely" in output)
        os.write(master, b"\r")
        deadline = time.monotonic() + 20
        while child.poll() is None:
            assert time.monotonic() < deadline, output.decode(errors="replace")[-4000:]
            if select.select([master], [], [], 0.05)[0]:
                output.extend(os.read(master, 65536))
        assert child.returncode == 23
        row = json.loads(result.read_text())
        assert row["pid"] == child.pid
        assert row["cwd"] == str(populated.project)
        assert row["argv"] == ["--resume", CLAUDE_ID]
        terminal_mask = termios.ECHO | termios.ICANON | termios.ISIG
        assert row["tty_flags"] & terminal_mask == flags_before & terminal_mask
        assert termios.tcgetattr(slave)[3] == flags_before
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        os.close(master)
        os.close(slave)


def test_real_terminal_delete_uses_single_confirmation(populated):
    """Send actual terminal bytes through Textual's modal binding/focus handling."""
    from pathlib import Path

    from ai_bridge_cli.catalog import Catalog

    target = Catalog().refresh(probe_activity=False).get('claude', CLAUDE_ID)
    ready = populated.root / 'delete-browser-ready'
    choice = populated.root / 'remove-modal-ready'
    returned = populated.root / 'delete-cancelled'
    code = f'''
from pathlib import Path
from ai_bridge_cli import activity
from ai_bridge_cli.tui import app, screens
activity.snapshot = lambda: activity.Activity()
class Browser(app.SessionBrowser):
    CSS_PATH = str(Path(app.__file__).with_name('app.tcss'))
    def on_mount(self):
        super().on_mount()
        self.set_interval(0.05, self.signal)
    def signal(self):
        if not self.loaded:
            return
        if not Path({str(ready)!r}).exists():
            row = [s.selector for s in self.shown].index('claude:{CLAUDE_ID}')
            self._q('#table').move_cursor(row=row)
            self._q('#table').focus()
            Path({str(ready)!r}).touch()
        if isinstance(self.screen, screens.RemoveScreen) and self.focused:
            Path({str(choice)!r}).touch()
        if Path({str(choice)!r}).exists() and len(self.screen_stack) == 1:
            Path({str(returned)!r}).touch()
Browser(periodic_refresh=False, probe_activity=False).run()
'''
    driver = populated.root / 'delete_driver.py'
    driver.write_text(code)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
    child = subprocess.Popen([sys.executable, str(driver)], stdin=slave, stdout=slave, stderr=slave,
                             env={**os.environ, 'TERM': 'xterm-256color'})
    output = bytearray()

    def until(predicate):
        deadline = time.monotonic() + 20
        while not predicate():
            assert time.monotonic() < deadline, output.decode(errors='replace')[-4000:]
            if select.select([master], [], [], 0.05)[0]:
                output.extend(os.read(master, 65536))
            assert child.poll() is None, output.decode(errors='replace')[-4000:]

    try:
        until(ready.exists)
        os.write(master, b'd')
        until(choice.exists)
        assert Path(target.storage_path).exists() and not populated.cli_data.exists()
        os.write(master, b'c')
        until(returned.exists)
        assert Path(target.storage_path).exists() and not populated.cli_data.exists()
        choice.unlink()
        os.write(master, b'd')
        until(choice.exists)
        os.write(master, b'd')
        until(lambda: not Path(target.storage_path).exists())
        until(lambda: len(list((populated.cli_data / 'trash').glob('*/manifest.json'))) == 1)
        os.write(master, b'q')
        child.wait(timeout=20)
        assert child.returncode == 0
        batches = list((populated.cli_data / 'trash').glob('*/manifest.json'))
        assert json.loads(batches[0].read_text())['items'][0]['status'] == 'trashed'
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        os.close(master)
        os.close(slave)
