#!/usr/bin/python3
import asyncio
from datetime import datetime
from datetime import timedelta
import json
import os
import subprocess
import sys

from filelock import SoftFileLock
from kasa import SmartPlug
import psutil


class BatteryController:
    """Manages whether we are charging the laptop battery.

    When my laptop is at home, I keep it plugged into a smart plug. This
    enables me to programatically control whether the battery is
    charging.

    To maximize battery health, we need to avoid constantly charging the
    battery, because this causes the battery to swell. In general, we
    want to charge the battery until it exceeds ``_CHARGE_THRESHOLD``
    and then discharge it until it goes below ``_DISCHARGE_THRESHOLD``.
    However, there are some exceptions.

    One recommendation I've seen is that a lithium ion battery functions
    best when kept charged in the 40% to 80% range.
    """
    # The behavior of BatteryController is stateful. Its state information is
    # stored in _STATE_FILENAME as a JSON object. It has the following state
    # fields:
    #
    # currentState: Whether the smart plug is currently on.
    # defaultState: Whether the smart plug should be turned on, outside of
    #     exceptional conditions.
    # keepStateUntil (optional): The time when defaultState may be changed.
    # manualOverrideState (optional): An override indicating whether the smart
    #     plug should be turned on. This user-supplied state overrides all
    #     other considerations. The purpose of the override is to enable us to
    #     fully charge the laptop when we anticipate being away from an outlet.
    # manualOverrideStateExpiresAt (optional): The time to discard
    #     manualOverrideState. The manual override expires after a while, in
    #     case I forget to explicitly turn it off when it is no longer needed.
    #
    # Times are represented as ISO 8601 strings returned by
    # datetime.isoformat().

    # The battery charge percentage below which we normally switch from
    # discharging to charging. This should be less than _CHARGE_THRESHOLD.
    _DISCHARGE_THRESHOLD = 50

    # The battery charge percentage above which we normally switch from
    # charging to discharging. This should be greater than
    # _DISCHARGE_THRESHOLD.
    _CHARGE_THRESHOLD = 75

    # The battery charge threshold for when we put the computer to sleep. If
    # the charge is above this percentage, then we discharge the battery as the
    # computer sleeps. Otherwise, we charge it. The assumption is that we can't
    # change the smart plug's state while the computer is sleeping.
    #
    # Normally, it's better to discharge the battery, to prevent it from
    # reaching and staying at 100% charge. However, if the charge level is very
    # low, it may be better to charge it to avoid the risk of running out of
    # battery.
    _SLEEP_CHARGE_THRESHOLD = 30

    # The amount of time after which to end a manual override, as in the
    # manualOverrideStateExpiresAt state field. This should be long enough that
    # I can enable the override at a convenient time and still have the laptop
    # be fully charged when I need to unplug it. But it shouldn't be too long,
    # to avoid the risk of excessively damaging the battery.
    _MANUAL_OVERRIDE_INTERVAL = timedelta(days=1)

    # The amount of time to fix the defaultState state field when putting the
    # computer to sleep. This deals with a conflict when putting the computer
    # to sleep. On the one hand, the normal operation of BatteryController
    # dictates that we should continually alternate between charging and
    # discharging. On the other hand, when we put the computer to sleep, we
    # need to fix the smart plug in a state appropriate to sleep mode.
    _SLEEP_INTERVAL = timedelta(minutes=2)

    # The file that stores BatteryController's state information
    _STATE_FILENAME = '/path/to/state_filename.json'

    # The static IP address of the smart plug
    _PLUG_IP_ADDRESS = '192.168.1.123'

    # The SSID of my home Wi-Fi network. If the laptop is not connected to this
    # network, then ``BatteryController`` ceases to operate. This is because
    # the laptop is unable to communicate with the smart plug, and because it's
    # not plugged into it.
    _HOME_SSID = 'HomeSSID'

    @staticmethod
    def _lock_filename():
        """Return the lock file protecting ``BatteryController``'s state.

        Whenever we may need to change the state, we must lock the file
        for the duration of the change using ``_lock()`` or
        ``_optimistic_lock()``.
        """
        return '{:s}.lock'.format(BatteryController._STATE_FILENAME)

    @staticmethod
    def _default_state():
        """Return the initial state information."""
        return {
            'currentState': True,
            'defaultState': True,
        }

    @staticmethod
    def _read_state():
        """Return the current state information, stored in ``_STATE_FILENAME``.
        """
        if not os.path.isfile(BatteryController._STATE_FILENAME):
            return BatteryController._default_state()
        else:
            with open(BatteryController._STATE_FILENAME, 'r') as file:
                return json.load(file)

    @staticmethod
    def _write_state(state):
        """Save the specified state information in ``_STATE_FILENAME``."""
        with open(BatteryController._STATE_FILENAME, 'w') as file:
            file.write(json.dumps(state, indent=4, sort_keys=True))

    @staticmethod
    def _ssid():
        """Return the SSID for the Wi-Fi network we are connected to, if any.
        """
        if os.name == 'nt' or sys.platform == 'darwin':
            if os.name == 'nt':
                command = ['Netsh', 'WLAN', 'show', 'interfaces']
            else:
                command = [
                    '/System/Library/PrivateFrameworks/Apple80211.framework/'
                    'Resources/airport',
                    '-I']

            output = subprocess.check_output(command).decode()
            for line in output.split('\n'):
                stripped_line = line.strip()
                if stripped_line.startswith('SSID'):
                    index = stripped_line.index(':')
                    return stripped_line[index + 2:]
            return None
        else:
            output = subprocess.check_output(['/sbin/iwgetid', '-r']).decode()
            ssid = output.rstrip('\n')
            if ssid:
                return ssid
            else:
                return None

    @staticmethod
    def _ping_plug():
        """Raise a ``RuntimeError`` if we fail to communicate with the plug.

        Calling ``_ping_plug()`` before calling ``SmartPlug`` methods
        may enable us to fail faster.
        """
        if os.name == 'nt':
            command = [
                'ping', '/n', '1', '/w', '600',
                BatteryController._PLUG_IP_ADDRESS]
        else:
            command = [
                'timeout', '0.6', 'ping', '-c', '1',
                BatteryController._PLUG_IP_ADDRESS]

        # Attempt to ping the plug three times
        for i in range(3):
            process = subprocess.run(
                command, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            if process.returncode == 0:
                return
        raise RuntimeError('Unable to reach the smart plug')

    @staticmethod
    def _turn_off():
        """Turn the smart plug off.

        Raises:
            Exception: If we fail to communicate with the smart plug.
        """
        BatteryController._ping_plug()
        asyncio.run(SmartPlug(BatteryController._PLUG_IP_ADDRESS).turn_off())

    @staticmethod
    def _turn_on():
        """Turn the smart plug on.

        Raises:
            Exception: If we fail to communicate with the smart plug.
        """
        BatteryController._ping_plug()
        asyncio.run(SmartPlug(BatteryController._PLUG_IP_ADDRESS).turn_on())

    @staticmethod
    def _poll(status):
        """Perform a poll or update operation.

        This updates and saves the current state, and it turns the smart
        plug on or off as indicated by the state. For example, this
        updates the ``defaultState`` field if the battery level has
        crossed ``_CHARGE_THRESHOLD`` or ``_DISCHARGE_THRESHOLD``. All
        methods that change the state or that may require us to turn the
        smart plug on or off should end by calling ``_poll``.

        Arguments:
            status (object): The current state. This may be the result
                of ``_read_state()``, or it may have some modifications
                applied to this state. The ``_poll`` method may alter
                the value of ``status``.
        """
        if BatteryController._ssid() != BatteryController._HOME_SSID:
            BatteryController._write_state(status)
            return

        now = datetime.now()
        if 'manualOverrideState' in status:
            expires_at = datetime.fromisoformat(
                status['manualOverrideStateExpiresAt'])
            if now >= expires_at:
                status.pop('manualOverrideState')
                status.pop('manualOverrideStateExpiresAt')
        if 'keepStateUntil' in status:
            keep_state_until = datetime.fromisoformat(status['keepStateUntil'])
            if now >= keep_state_until:
                status.pop('keepStateUntil')

        if 'keepStateUntil' not in status:
            battery = psutil.sensors_battery().percent
            if status['defaultState']:
                if battery >= BatteryController._CHARGE_THRESHOLD:
                    status['defaultState'] = False
            elif battery <= BatteryController._DISCHARGE_THRESHOLD:
                status['defaultState'] = True

        if 'manualOverrideState' in status:
            desired_state = status['manualOverrideState']
        else:
            desired_state = status['defaultState']

        if desired_state != status['currentState']:
            if desired_state:
                BatteryController._turn_on()
            else:
                BatteryController._turn_off()
            status['currentState'] = desired_state

        BatteryController._write_state(status)

    @staticmethod
    def _lock():
        """Return a context manager for acquiring ``_lock_filename()``.

        For example::

            with BatteryController._lock():
                status = BatteryController._read_state()
                # --- Mutate status here ---
                BatteryController._poll(status)
        """
        return SoftFileLock(BatteryController._lock_filename(), timeout=30)

    @staticmethod
    def _optimistic_lock():
        """Return a context manager for acquiring ``_lock_filename()``.

        If the file is locked, we raise an exception rather than
        waiting.

        For example::

            with BatteryController._optimistic_lock():
                status = BatteryController._read_state()
                # --- Mutate status here ---
                BatteryController._poll(status)
        """
        return SoftFileLock(BatteryController._lock_filename())

    @staticmethod
    def poll():
        """Update the state and smart plug based on the current status.

        This should be called periodically, e.g. every five minutes, in
        order to respond to changes in the battery level (and to the
        passage of time).
        """
        with BatteryController._optimistic_lock():
            status = BatteryController._read_state()
            BatteryController._poll(status)

    @staticmethod
    def print_status():
        """Output status information about ``BatteryController`` to stdout."""
        print(
            json.dumps(
                BatteryController._read_state(), indent=4, sort_keys=True))

    @staticmethod
    def enable_manual_override():
        """Override all other considerations and turn the smart plug on.

        The purpose of the override is to enable us to fully charge the
        laptop when we anticipate being away from an outlet. The
        override expires after a while, in case I forget to explicitly
        turn it off when it is no longer needed.
        """
        with BatteryController._lock():
            status = BatteryController._read_state()
            expires_at = (
                datetime.now() + BatteryController._MANUAL_OVERRIDE_INTERVAL)
            status['manualOverrideState'] = True
            status['manualOverrideStateExpiresAt'] = expires_at.isoformat()
            BatteryController._poll(status)

    @staticmethod
    def disable_manual_override():
        """Turn off manual override from an ``enable_manual_override()`` call.
        """
        with BatteryController._lock():
            status = BatteryController._read_state()
            status.pop('manualOverrideState', None)
            status.pop('manualOverrideStateExpiresAt', None)
            BatteryController._poll(status)

    @staticmethod
    def prepare_for_sleep():
        """Perform preparation shortly before putting the laptop to sleep.

        While the laptop is sleeping, we presumably can't affect the
        state of the smart plug, so ``prepare_for_sleep()`` decides
        whether to charge the battery while the laptop is asleep.
        """
        battery = psutil.sensors_battery().percent
        with BatteryController._lock():
            status = BatteryController._read_state()
            keep_state_until = (
                datetime.now() + BatteryController._SLEEP_INTERVAL)
            status['defaultState'] = (
                battery <= BatteryController._SLEEP_CHARGE_THRESHOLD)
            status['keepStateUntil'] = keep_state_until.isoformat()
            BatteryController._poll(status)

    @staticmethod
    def scan():
        """Check whether the smart plug is on.

        After checking the smart plug, we perform a poll operation, as
        in ``poll()``.

        Normally, we rely on the contents of the state file to determine
        whether the smart plug is on. However, this assumes that only
        ``BatteryController`` turns it on and off. If something else
        turns it on or off, we should call ``scan()`` to ensure that
        ``BatteryController`` picks up the change.
        """
        BatteryController._ping_plug()
        plug = SmartPlug(BatteryController._PLUG_IP_ADDRESS)
        asyncio.run(plug.update())
        with BatteryController._lock():
            status = BatteryController._read_state()
            status['currentState'] = plug.is_on
            BatteryController._poll(status)


if __name__ == '__main__':
    methods = {
        'info': BatteryController.print_status,
        'override-off': BatteryController.disable_manual_override,
        'override-on': BatteryController.enable_manual_override,
        'poll': BatteryController.poll,
        'scan': BatteryController.scan,
        'status': BatteryController.print_status,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in methods:
        raise ValueError(
            'battery_control.py accepts exactly one argument. It must be one '
            'of the following commands: {:s}'.format(
                ', '.join(sorted(list(methods.keys())))))
    methods[sys.argv[1]]()
