import os

import pytest

from openpilot.common.hardware.hw import Paths
import openpilot.system.loggerd.audio_extractord as audio_extractord
from openpilot.system.loggerd.tests.loggerd_tests_common import UploaderTestCase


class TestAudioExtractord(UploaderTestCase):
  def test_scan_skips_locked_and_qcameraless_segments(self, monkeypatch):
    processed = []
    monkeypatch.setattr(audio_extractord, "process_segment_audio", lambda path: processed.append(path) or True)

    no_qcam_dir = self.seg_format.format(self.seg_num)
    self.make_file_with_data(no_qcam_dir, "rlog")

    locked_dir = self.seg_format.format(self.seg_num + 1)
    self.make_file_with_data(locked_dir, "qcamera.ts", lock=True)

    ready_dir = self.seg_format.format(self.seg_num + 2)
    self.make_file_with_data(ready_dir, "qcamera.ts")

    audio_extractord.scan(Paths.log_root())

    assert processed == [os.path.join(Paths.log_root(), ready_dir)]

  def test_scan_processes_multiple_ready_segments_in_creation_order(self, monkeypatch):
    processed = []
    monkeypatch.setattr(audio_extractord, "process_segment_audio", lambda path: processed.append(path) or True)

    dirs = [self.seg_format.format(n) for n in (self.seg_num, self.seg_num + 1, self.seg_num + 2)]
    for d in dirs:
      self.make_file_with_data(d, "qcamera.ts")

    audio_extractord.scan(Paths.log_root())

    assert processed == [os.path.join(Paths.log_root(), d) for d in dirs]


if __name__ == "__main__":
  pytest.main([__file__])
