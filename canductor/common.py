"""Shared UI utilities: colors, prompts, countdown."""
from __future__ import annotations

import sys
import time

_USE_COLOR = sys.stdout.isatty() and sys.stderr.isatty()


class C:
    RESET = "\033[0m" if _USE_COLOR else ""
    BOLD = "\033[1m" if _USE_COLOR else ""
    DIM = "\033[2m" if _USE_COLOR else ""
    RED = "\033[31m" if _USE_COLOR else ""
    GREEN = "\033[32m" if _USE_COLOR else ""
    YELLOW = "\033[33m" if _USE_COLOR else ""
    BLUE = "\033[34m" if _USE_COLOR else ""
    CYAN = "\033[36m" if _USE_COLOR else ""


def say(line: str = "") -> None:
    print(line, flush=True)


def err(line: str = "") -> None:
    print(line, file=sys.stderr, flush=True)


def hr(ch: str = "─") -> None:
    say(C.DIM + ch * 64 + C.RESET)


def countdown(label: str, seconds: float, end: float | None = None, step: float = 1.0,
              throttle_s: float = 0.2) -> None:
    """Live one-line countdown."""
    if not sys.stderr.isatty() or seconds < throttle_s:
        if seconds > 0:
            err(f"{C.DIM}… {label} {int(seconds)}″{C.RESET}")
            time.sleep(seconds)
        return
    t0 = time.monotonic()
    if end is None:
        end = t0 + seconds
    total = end - t0
    while True:
        rest = end - time.monotonic()
        if rest <= 0:
            err(f"\r{C.RESET}")
            err(f"{C.GREEN}✓ {label} (done){C.RESET}")
            return
        bar = "▮" * int((rest / max(total, 1e-6)) * 20).rjust(20)
        err(f"\r{C.DIM}{label:20s}{C.RESET} {bar} {int(rest):3d}s")
        # pulse for the "alive" feel
        time.sleep(min(throttle_s, rest))
        continue


def ask(prompt: str, default: str = "") -> str:
    """Example: ask('Coolant temp', '45') → '45' (Enter = default)."""
    d = f" [{default}]" if default else ""
    try:
        v = input(f"{C.YELLOW}→{C.RESET}{prompt}{C.DIM}{d}{C.RESET}: ").strip()
    except EOFError:
        return default
    return v or default


def confirm(prompt: str, choices: str = "spq", default: str = "s") -> str:
    """Pick one of the choices (lowercase), Enter = default."""
    hint = "/".join(choices.upper())
    while True:
        try:
            v = input(f"{C.YELLOW}→ {prompt}{C.RESET} {C.DIM}({hint}, Enter={default.upper()}){C.RESET}: ").strip().lower()
        except EOFError:
            return default
        if not v:
            return default
        if v in choices:
            return v
        err(f"{C.RED}! options: {hint}{C.RESET}")