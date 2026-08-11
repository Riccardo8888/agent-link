"""Marks `tests` as a regular package.

Without this file `tests` is a namespace package, and a namespace portion never
wins: the import machinery keeps scanning the rest of sys.path and a regular
`tests` package found anywhere later takes it. Any third-party wheel that ships
its own top-level `tests/` -- ultralytics does -- then owns the name, and
`from tests.timing import budget` resolves into that stranger's package and
raises ModuleNotFoundError. CI never sees it because CI installs one dependency
into a clean interpreter.
"""
