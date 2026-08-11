"""Timing budgets, scaled to the machine the tests are running on.

Several tests assert an upper bound on how long something takes. They are
regression guards, not performance specifications: their job is to catch
somebody reintroducing a per-call TCP connect (13 ms instead of 0.13 ms), or a
tool that blocks when it promised not to. What they are *not* for is asserting
that a particular machine is fast.

A shared CI runner is several times slower than a developer's laptop at exactly
the operations these bounds cover -- spawning a Python process and loading the
`cryptography` extension -- so an unscaled bound turns a slow VM into a red
build and teaches everyone to ignore the colour. Scaling keeps the guard
meaningful locally, where the numbers were measured, while leaving enough room
that CI fails only on a real regression.

Only upper bounds are scaled. A lower bound -- "a 700 ms wait must actually take
700 ms" -- is a correctness claim and is left alone.
"""

from __future__ import annotations

import os


def _factor() -> float:
    explicit = os.environ.get("CLAUDE_LINK_TEST_SLOWDOWN")
    if explicit:
        try:
            return max(1.0, float(explicit))
        except ValueError:
            pass
    # GitHub, GitLab and most others set CI=true.
    return 8.0 if os.environ.get("CI") else 1.0


SLOW_FACTOR = _factor()


def budget(seconds: float) -> float:
    """Scale an upper-bound timing budget for this machine."""
    return seconds * SLOW_FACTOR
