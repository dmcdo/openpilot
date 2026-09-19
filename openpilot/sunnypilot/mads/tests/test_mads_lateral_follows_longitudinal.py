"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from openpilot.cereal import log, custom
from opendbc.car.structs import car
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.selfdrived.events import ET
from openpilot.sunnypilot.mads.helpers import MadsSteeringModeOnBrake
from openpilot.sunnypilot.mads.tests.test_mads_steering_mode import make_car_state, make_mads

State = custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState
EventName = log.OnroadEvent.EventName
EventNameSP = custom.OnroadEventSP.EventName
ButtonEvent = car.CarState.ButtonEvent
ButtonType = car.CarState.ButtonEvent.Type


def lkas_button_press():
  cs = make_car_state()
  cs.buttonEvents = [ButtonEvent(type=ButtonType.lkas, pressed=True)]
  return cs


class TestLateralFollowsLongitudinal(OpenpilotTestCase):
  @staticmethod
  def _both_engaged(mocker, steering_mode=MadsSteeringModeOnBrake.REMAIN_ACTIVE):
    mads, sd = make_mads(mocker, steering_mode)
    mads.state_machine.state = State.enabled
    mads.enabled = True
    mads.active = True
    sd.enabled = True
    sd.enabled_prev = True
    return mads, sd

  # longitudinal disengaging takes lateral with it

  def test_longitudinal_disengage_disables_lateral(self, mocker):
    mads, sd = self._both_engaged(mocker)
    sd.enabled = False

    mads.update(make_car_state())
    assert sd.events_sp.has(EventNameSP.lkasDisable)
    assert mads.state_machine.state == State.disabled
    assert not mads.enabled
    assert not mads.active

  def test_longitudinal_disengage_overrides_pause_on_brake(self, mocker):
    mads, sd = self._both_engaged(mocker, MadsSteeringModeOnBrake.PAUSE)
    sd.enabled = False
    sd.events.add(EventName.pedalPressed)

    mads.update(make_car_state(brake_pressed=True, v_ego=10.0))
    assert not sd.events_sp.has(EventNameSP.silentLkasDisable)
    assert mads.state_machine.state == State.disabled

  def test_longitudinal_disengage_alerts_with_lateral_already_off(self, mocker):
    mads, sd = make_mads(mocker, MadsSteeringModeOnBrake.REMAIN_ACTIVE)
    sd.enabled_prev = True

    mads.update(make_car_state())
    assert sd.events_sp.has(EventNameSP.lkasDisable)
    assert mads.state_machine.state == State.disabled

  # lateral cannot come up on its own

  def test_lkas_button_blocked_while_longitudinal_off(self, mocker):
    mads, sd = make_mads(mocker, MadsSteeringModeOnBrake.REMAIN_ACTIVE)

    mads.update(lkas_button_press())
    assert not sd.events_sp.has(EventNameSP.lkasEnable)
    assert sd.events.has(EventName.wrongCarMode)
    assert mads.state_machine.state == State.disabled
    assert not mads.enabled

  def test_main_cruise_does_not_engage_lateral_while_longitudinal_off(self, mocker):
    mads, sd = make_mads(mocker, MadsSteeringModeOnBrake.REMAIN_ACTIVE)
    mads.main_enabled_toggle = True
    sd.CS_prev = make_car_state()
    sd.CS_prev.cruiseState.available = False

    mads.update(make_car_state())
    assert not sd.events_sp.has(EventNameSP.lkasEnable)
    # ACC MAIN coming up isn't a request for lateral, so it shouldn't nag
    assert not sd.events.has(EventName.wrongCarMode)
    assert mads.state_machine.state == State.disabled

  def test_paused_lateral_does_not_resume_while_longitudinal_off(self, mocker):
    mads, sd = make_mads(mocker, MadsSteeringModeOnBrake.PAUSE)
    mads.state_machine.state = State.paused
    mads.enabled = True

    mads.update(make_car_state(standstill=True))
    assert not sd.events_sp.has(EventNameSP.silentLkasEnable)
    assert mads.state_machine.state == State.paused

  def test_unified_engagement_blocked_when_longitudinal_refused(self, mocker):
    mads, sd = make_mads(mocker, MadsSteeringModeOnBrake.REMAIN_ACTIVE)
    mads.unified_engagement_mode = True
    # ACC MAIN enable event present, but selfdrived refused to engage
    sd.events.add(EventName.pcmEnable)

    mads.update(make_car_state())
    assert mads.state_machine.state == State.disabled

  # lateral may still be toggled independently while longitudinal is engaged

  def test_lkas_button_engages_lateral_while_longitudinal_on(self, mocker):
    mads, sd = make_mads(mocker, MadsSteeringModeOnBrake.REMAIN_ACTIVE)
    sd.enabled = True
    sd.enabled_prev = True

    mads.update(lkas_button_press())
    assert sd.events_sp.has(EventNameSP.lkasEnable)
    assert mads.state_machine.state == State.enabled
    assert mads.active

  def test_lkas_button_suspends_lateral_only(self, mocker):
    mads, sd = self._both_engaged(mocker)

    mads.update(lkas_button_press())
    assert sd.events_sp.has(EventNameSP.manualSteeringRequired)
    assert mads.state_machine.state == State.disabled
    assert sd.enabled

  def test_unified_engagement_brings_lateral_up_with_longitudinal(self, mocker):
    mads, sd = make_mads(mocker, MadsSteeringModeOnBrake.REMAIN_ACTIVE)
    mads.unified_engagement_mode = True
    sd.enabled = True
    sd.events.add(EventName.pcmEnable)

    mads.update(make_car_state())
    assert mads.state_machine.state == State.enabled
    assert mads.active


class TestEngagementChimes(OpenpilotTestCase):
  """The chime follows from the alert types selfdrived collects, so assert on those."""

  @staticmethod
  def _make(mocker, steering_mode=MadsSteeringModeOnBrake.REMAIN_ACTIVE):
    mads, sd = make_mads(mocker, steering_mode)
    sd.state_machine.current_alert_types = [ET.PERMANENT]
    return mads, sd

  def test_engage_chime_on_lateral_reengage(self, mocker):
    mads, sd = self._make(mocker)
    sd.enabled = True
    sd.enabled_prev = True

    mads.update(lkas_button_press())
    assert sd.events_sp.has(EventNameSP.lkasEnable)
    assert ET.ENABLE in sd.state_machine.current_alert_types

  def test_disengage_chime_on_lateral_suspend(self, mocker):
    mads, sd = self._make(mocker)
    mads.state_machine.state = State.enabled
    mads.enabled = True
    mads.active = True
    sd.enabled = True
    sd.enabled_prev = True

    mads.update(lkas_button_press())
    assert sd.events_sp.has(EventNameSP.manualSteeringRequired)
    assert ET.USER_DISABLE in sd.state_machine.current_alert_types

  def test_disengage_chime_on_longitudinal_disengage(self, mocker):
    mads, sd = self._make(mocker)
    mads.state_machine.state = State.enabled
    mads.enabled = True
    mads.active = True
    sd.enabled_prev = True

    mads.update(make_car_state())
    assert sd.events_sp.has(EventNameSP.lkasDisable)
    assert ET.USER_DISABLE in sd.state_machine.current_alert_types

  def test_no_duplicate_disable_event_in_disengage_mode(self, mocker):
    mads, sd = self._make(mocker, MadsSteeringModeOnBrake.DISENGAGE)
    mads.state_machine.state = State.enabled
    mads.enabled = True
    mads.active = True
    sd.enabled_prev = True
    sd.events.add(EventName.pedalPressed)

    mads.update(make_car_state(brake_pressed=True, v_ego=10.0))
    assert sd.events_sp.names.count(EventNameSP.lkasDisable) == 1
