"""Control interface between the Runner (worker) and the UI (terminal / web).

The Runner drives the Control API:
  * test_card(...)     — shows the test card and waits (notes, start/skip/quit);
  * phase(...)         — a phase begins (quiet/action/after, dur);
  * pause(test_id...)  — pause between recordings;
  * test_done(...)     — recording finished, report saved;
  * session_start/done/error, log_line.

LocalControl — CLI behaviour (I/O); HttpControl — web (events + queue).
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any

from ..common import C, ask, confirm, err, hr, say

QUIT, START, SKIP = "quit", "start", "skip"


class Control:
    """Base (synchronous, thread-bound)."""

    def session_start(self, plan: list[dict], summary: dict) -> None:
        pass

    def session_done(self, session_dir: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass

    def log_line(self, text: str) -> None:
        pass

    def test_card(self, stage_id: str, stage_title: str, test, car: str, rep: int,
                  rep_total: int, file_preview: str) -> tuple[dict, str]:
        """Show the test card; return (note dict, action)."""
        raise NotImplementedError

    def phase(self, test_id: str, car: str, phase: str, dur_s: float) -> None:
        pass

    def pause(self, dur_s: float) -> None:
        pass

    def test_done(self, info: dict) -> None:
        pass


class LocalControl(Control):
    """CLI behaviour (as it was before the web UI)."""

    def __init__(self, auto: bool = False) -> None:
        self.auto = auto

    def session_start(self, plan: list[dict], summary: dict) -> None:
        say(C.BOLD + f"Plan: {len(plan)} recordings{C.RESET}")

    def test_card(self, stage_id, stage_title, test, car, rep, rep_total, file_preview):
        note = {"coolant": "", "air": "", "engine": "", "comment": "",
                "file": file_preview, "auto": car}
        if self.auto:
            return note, START
        say(C.CYAN + "\n  Protocol form (Appendix C):" + C.RESET)
        note["coolant"] = ask("  Coolant temp (°C)", "")
        note["air"] = ask("  Ambient temp (°C)", "")
        note["engine"] = ask("  Engine state (cold/warm)", "")
        note["comment"] = ask("  Comment", "")
        v = confirm("Preparation ready? (s=start, p=skip, q=quit)", "spq")
        note["rep"] = rep
        # map the keypress to the control actions the Runner compares against
        return note, {"s": START, "p": SKIP, "q": QUIT}.get(v, START)

    def phase(self, test_id, car, phase, dur_s) -> None:
        say(f"  {C.CYAN}▶ {phase} {int(dur_s)}″{C.RESET}")

    def pause(self, dur_s) -> None:
        say(C.DIM + f"\n⏸ pause {int(dur_s)}″…{C.RESET}")
        time.sleep(max(0.5, dur_s))

    def test_done(self, info: dict) -> None:
        say(C.GREEN + f"✓ saved {info.get('report')}{C.RESET}")

    def log_line(self, text: str) -> None:
        say(text)

    def session_done(self, session_dir: str) -> None:
        say("")
        hr()
        say(C.BOLD + f"Session finished: {session_dir}{C.RESET}")
        hr()

    def error(self, msg: str) -> None:
        err(C.RED + msg + C.RESET)