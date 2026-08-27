"""
Gates file uploads behind an opt-in marker file.

Uploads are blocked by default. To allow uploads, create /dev/shm/upload
containing a date, optionally with a time, e.g. "2026-08-26" or
"2026-08-26 14:30:00". Only files created at or after that moment are
eligible for upload; everything older stays local. A date with no time
means midnight at the start of that day. The gate resets on reboot since
/dev/shm is tmpfs.
"""
import os
import stat
from datetime import datetime

UPLOAD_GATE_FILE = "/dev/shm/upload"


def get_upload_gate_timestamp() -> float | None:
  try:
    # /dev/shm is world-writable; refuse anything that isn't a plain regular
    # file so a symlink or FIFO planted there can't make us read an
    # arbitrary path or block waiting on a writer.
    if not stat.S_ISREG(os.lstat(UPLOAD_GATE_FILE).st_mode):
      return None
    with open(UPLOAD_GATE_FILE) as f:
      contents = f.read().strip()
    return datetime.fromisoformat(contents).timestamp()
  except (OSError, ValueError):
    return None


def is_upload_permitted(file_ctime: float) -> bool:
  gate_ts = get_upload_gate_timestamp()
  if gate_ts is None:
    return False
  return file_ctime >= gate_ts
