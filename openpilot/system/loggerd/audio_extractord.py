#!/usr/bin/env python3
import os
import threading

from openpilot.common.hardware.hw import Paths
from openpilot.system.loggerd.audio_extractor import QCAMERA_FILENAME, process_segment_audio
from openpilot.system.loggerd.uploader import listdir_by_creation

POLL_INTERVAL = 2.0


def scan(root: str) -> None:
  for logdir in listdir_by_creation(root):
    path = os.path.join(root, logdir)
    try:
      names = os.listdir(path)
    except OSError:
      continue

    # a .lock file means loggerd/encoderd is still writing this segment - qcamera.ts/rlog/qlog
    # aren't final yet, so wait for the next scan
    if any(name.endswith(".lock") for name in names):
      continue

    if QCAMERA_FILENAME not in names:
      continue

    process_segment_audio(path)


def main(exit_event: threading.Event | None = None) -> None:
  if exit_event is None:
    exit_event = threading.Event()

  while not exit_event.is_set():
    scan(Paths.log_root())
    exit_event.wait(POLL_INTERVAL)


if __name__ == "__main__":
  main()
