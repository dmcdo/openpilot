import os
import time

import openpilot.system.loggerd.upload_window as upload_window
from openpilot.system.loggerd.upload_window import is_upload_allowed

HOUR = 3600


class TestUploadWindow:
  """A drive uploads iff it ended after T - N, where T is the /dev/shm file's timestamp and N
  the hours it contains."""

  def setup_method(self):
    self.now = time.time()
    self.hours = 6

  def _setup(self, tmp_path):
    self.root = tmp_path / "realdata"
    self.root.mkdir()
    self.window_path = tmp_path / "upload_window_hours"
    upload_window.UPLOAD_WINDOW_PATH = str(self.window_path)
    self._clear()

  def _clear(self):
    upload_window._segment_end_cache.clear()
    upload_window._route_end_cache.clear()

  def _drive(self, route: str, *seg_offsets_hours: float):
    """Creates one segment per offset, each offset given in hours relative to T."""
    for i, off in enumerate(seg_offsets_hours):
      d = self.root / f"{route}--{i}"
      d.mkdir(parents=True, exist_ok=True)
      f = d / "qlog.zst"
      f.write_bytes(b"x")
      os.utime(f, (self.now + off * HOUR, self.now + off * HOUR))

  def _window(self, hours, created=None):
    self.window_path.write_text(str(hours))
    created = self.now if created is None else created
    os.utime(self.window_path, (created, created))
    self._clear()

  def _allowed(self, logdir):
    self._clear()
    return is_upload_allowed(str(self.root), logdir, str(self.root / logdir / "qlog.zst"))

  def test_no_window_file_blocks_everything(self, tmp_path):
    self._setup(tmp_path)
    self._drive("r", -1)
    assert not self._allowed("r--0")

  def test_drive_ending_after_cutoff_uploads(self, tmp_path):
    self._setup(tmp_path)
    self._drive("r", -2)
    self._window(self.hours)
    assert self._allowed("r--0")

  def test_drive_ending_before_cutoff_blocked(self, tmp_path):
    self._setup(tmp_path)
    self._drive("r", -20)
    self._window(self.hours)
    assert not self._allowed("r--0")

  def test_drive_straddling_cutoff_uploads_in_full(self, tmp_path):
    # began well before T-N but ended after it: the whole drive goes, early segments included
    self._setup(tmp_path)
    self._drive("r", -30, -20, -2)
    self._window(self.hours)
    assert self._allowed("r--0")
    assert self._allowed("r--1")
    assert self._allowed("r--2")

  def test_drive_entirely_inside_window_uploads(self, tmp_path):
    self._setup(tmp_path)
    self._drive("r", -5, -4, -3)
    self._window(self.hours)
    assert all(self._allowed(f"r--{i}") for i in range(3))

  def test_drive_started_after_window_file_uploads(self, tmp_path):
    # subsumed by E > T-N, since such a drive ends after T
    self._setup(tmp_path)
    self._drive("r", 1, 2)
    self._window(self.hours)
    assert self._allowed("r--0")
    assert self._allowed("r--1")

  def test_drive_in_progress_at_T_uploads(self, tmp_path):
    self._setup(tmp_path)
    self._drive("r", -30, -2, 1)
    self._window(self.hours)
    assert all(self._allowed(f"r--{i}") for i in range(3))

  def test_hours_zero_blocks_everything_older_than_T(self, tmp_path):
    self._setup(tmp_path)
    self._drive("old", -1)
    self._drive("new", 1)
    self._window(0)
    assert not self._allowed("old--0")
    assert self._allowed("new--0")

  def test_unparseable_and_negative_fail_closed(self, tmp_path):
    self._setup(tmp_path)
    self._drive("r", -1)
    self.window_path.write_text("not-a-number")
    os.utime(self.window_path, (self.now, self.now))
    assert not self._allowed("r--0")
    self._window(-1)
    assert not self._allowed("r--0")

  def test_drives_judged_independently(self, tmp_path):
    self._setup(tmp_path)
    self._drive("recent", -2)
    self._drive("ancient", -40)
    self._window(self.hours)
    assert self._allowed("recent--0")
    assert not self._allowed("ancient--0")

  def test_non_segment_dirs_gated_on_own_mtime(self, tmp_path):
    # boot/ and crash/ belong to no drive, so they're judged on their own timestamp
    self._setup(tmp_path)
    boot = self.root / "boot"
    boot.mkdir()
    self._window(self.hours)

    old = boot / "aaaa.zst"
    old.write_bytes(b"x")
    os.utime(old, (self.now - 20 * HOUR, self.now - 20 * HOUR))
    assert not is_upload_allowed(str(self.root), "boot", str(old))

    recent = boot / "bbbb.zst"
    recent.write_bytes(b"x")
    os.utime(recent, (self.now - 2 * HOUR, self.now - 2 * HOUR))
    assert is_upload_allowed(str(self.root), "boot", str(recent))
