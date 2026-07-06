"""Centralized logging for the release-query pipeline.

Usage::

    from src.logger import Log

    Log.info("Fetching releases...")   # auto-captures caller file + function
    Log.error("Download failed: ...")
"""

import inspect
import os
import sys


class Log:
    """Structured logger that auto-captures the calling file and function.

    Output format (written to stderr)::

        [release-query] [INFO] extract.fetch_gerrit_releases — Gerrit: 12 commits
        [release-query] [ERROR] artifactory._download_one_rpm — Download failed: 404
    """

    _TAG = "release-query"

    @classmethod
    def info(cls, msg):
        caller_file, caller_func = cls._caller_context()
        print(
            f"[{cls._TAG}] [INFO] {caller_file}.{caller_func} — {msg}",
            file=sys.stderr, flush=True,
        )

    @classmethod
    def error(cls, msg):
        caller_file, caller_func = cls._caller_context()
        print(
            f"[{cls._TAG}] [ERROR] {caller_file}.{caller_func} — {msg}",
            file=sys.stderr, flush=True,
        )

    @staticmethod
    def _caller_context():
        """Walk up the stack to find the real caller (skip logger internals).

        Returns (module_name, function_name).  module_name is the bare
        filename without extension (e.g. ``extract``, ``endor``).
        """
        frame = inspect.currentframe()
        try:
            # frame 0 = _caller_context, 1 = info/error, 2 = actual caller
            caller = frame.f_back.f_back
            filepath = caller.f_code.co_filename
            module = os.path.splitext(os.path.basename(filepath))[0]
            func = caller.f_code.co_name
            return module, func
        finally:
            del frame
