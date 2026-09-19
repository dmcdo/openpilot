"""Shared helper for suites that predate the /dev/shm upload window.

Those tests assert on upload behavior, not on the window, so they point the gate at a
throwaway file with a window wide enough to cover anything they generate. The gate's own
behavior is covered by test_upload_window.py."""
import os
import tempfile

import openpilot.system.loggerd.upload_window as upload_window

# a century, so freshly written test files always land after the cutoff
WIDE_OPEN_HOURS = 24 * 365 * 100


def open_upload_window(test_case) -> str:
  """Points the upload window at a temp file that admits every drive on disk. Returns its
  path; it's cleaned up when the test's temp dir goes away."""
  fd, path = tempfile.mkstemp(prefix="upload_window_hours")
  with os.fdopen(fd, "w") as f:
    f.write(str(WIDE_OPEN_HOURS))

  upload_window.UPLOAD_WINDOW_PATH = path
  upload_window._segment_end_cache.clear()
  upload_window._route_end_cache.clear()

  # tests reuse the log root across cases, so stale per-root caches must not leak between them
  test_case._upload_window_path = path
  return path
