# Host SBC Power Control with Emergency Shutdown Safety
#
# Part of Klipper port for Maestro Grand 2 IDEX
#
# This module allows Klipper to control an Orange Pi / Host SBC GPIO pin
# (or execute custom commands) to manage 24V power supply relays.
#
# Key Features:
# 1. Powers ON the 24V PSU immediately at Klipper startup (before MCUs connect).
# 2. Waits a configurable startup_delay (e.g. 1.5s) for PSU voltage to stabilize
#    and MCUs to boot, resolving the chicken-and-egg connection deadlock.
# 3. IMMEDIATELY turns OFF 24V power when Klipper enters SHUTDOWN state
#    (thermal runaway, heater fault, shorted bed MOSFET, or emergency stop M112).
# 4. Supports FIRMWARE_RESTART: re-arms and powers up the printer safely.
# 5. Supports G-code commands M80 (power on) and M81 (power off).

import os
import time
import logging
import subprocess

class HostPower:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()

        # Configuration options
        self.pin = config.getint('pin', None)
        self.on_cmd = config.get('on_cmd', None)
        self.off_cmd = config.get('off_cmd', None)

        if self.pin is None and self.on_cmd is None:
            raise config.error(
                "[%s] requires either 'pin' (sysfs GPIO number) or 'on_cmd' (shell command)"
                % (self.name,)
            )
        # Fire safety: there must be a way to actually cut the power.  Without
        # this check a config with only on_cmd would silently "turn off"
        # nothing on shutdown while reporting success.
        if self.pin is None and not self.off_cmd:
            raise config.error(
                "[%s] requires 'off_cmd' (or 'pin') so power can be cut on shutdown"
                % (self.name,)
            )

        self.active_low = config.getboolean('active_low', False)
        self.startup_delay = config.getfloat('startup_delay', 1.5, minval=0.0)
        self.off_when_shutdown = config.getboolean('off_when_shutdown', True)
        # Default False: a normal klippy exit/restart must not power-cycle the
        # printer (and possibly the host itself).  Shutdowns still cut power.
        self.off_on_disconnect = config.getboolean('off_on_disconnect', False)

        self.is_powered = False

        # Turn power ON immediately at initialization (before klippy:mcu_identify!)
        logging.info("host_power: Powering ON 24V printer power supply...")
        self._set_power(True)

        if self.startup_delay > 0.:
            logging.info(
                "host_power: Waiting %.1fs for 24V PSU stabilization and MCU boot...",
                self.startup_delay
            )
            time.sleep(self.startup_delay)

        # Register event handlers for shutdown and disconnect
        self.printer.register_event_handler("klippy:shutdown", self._handle_shutdown)
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)

        # Register G-Code commands
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("M80", self.cmd_M80, desc="Turn on 24V power supply")
        gcode.register_command("M81", self.cmd_M81, desc="Turn off 24V power supply")
        gcode.register_command(
            "HOST_POWER_ON", self.cmd_M80, desc="Turn on 24V power supply"
        )
        gcode.register_command(
            "HOST_POWER_OFF", self.cmd_M81, desc="Turn off 24V power supply"
        )

    def _set_gpio_sysfs(self, state):
        if self.pin is None:
            return True
        pin_dir = "/sys/class/gpio/gpio%d" % (self.pin,)
        if not os.path.exists(pin_dir):
            try:
                with open("/sys/class/gpio/export", "w") as f:
                    f.write("%d\n" % (self.pin,))
                time.sleep(0.05)
            except Exception as e:
                logging.error("host_power: Failed to export gpio%d: %s",
                              self.pin, e)
                return False
            if not os.path.exists(pin_dir):
                logging.error("host_power: gpio%d did not appear after export",
                              self.pin)
                return False

        try:
            with open(os.path.join(pin_dir, "direction"), "w") as f:
                f.write("out\n")
            # Determine output logic level
            val = 0 if (state == self.active_low) else 1
            value_path = os.path.join(pin_dir, "value")
            with open(value_path, "w") as f:
                f.write("%d\n" % (val,))
            # Verify that the write really took effect - a wrong pin number
            # must not look like a successful power cut.
            with open(value_path, "r") as f:
                readback = int(f.read().strip())
            if readback != val:
                logging.error("host_power: gpio%d read back %d, expected %d",
                              self.pin, readback, val)
                return False
        except Exception as e:
            logging.error("host_power: Failed to write to gpio%d: %s",
                          self.pin, e)
            return False
        return True

    def _run_cmd(self, cmd):
        if not cmd:
            return True
        try:
            res = subprocess.run(
                cmd, shell=True, timeout=5,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if res.returncode != 0:
                logging.error(
                    "host_power: Command '%s' exited with code %d: %s",
                    cmd, res.returncode, res.stderr.decode().strip()
                )
                return False
        except Exception as e:
            logging.error("host_power: Execution of '%s' failed: %s", cmd, e)
            return False
        return True

    def _set_power(self, state):
        ok = True
        if self.pin is not None:
            ok = self._set_gpio_sysfs(state) and ok
        if state:
            if self.on_cmd:
                ok = self._run_cmd(self.on_cmd) and ok
        else:
            if self.off_cmd:
                ok = self._run_cmd(self.off_cmd) and ok
        if ok:
            self.is_powered = state
            logging.info("host_power: 24V PSU state -> %s",
                          "ON" if state else "OFF")
        else:
            logging.error("host_power: FAILED to switch 24V PSU %s",
                          "ON" if state else "OFF")
        return ok

    def _handle_shutdown(self):
        if self.off_when_shutdown:
            logging.warning(
                "host_power: EMERGENCY SHUTDOWN DETECTED! Cutting 24V PSU power to prevent fire hazard!"
            )
            self._set_power(False)

    def _handle_disconnect(self):
        if self.off_on_disconnect:
            logging.info("host_power: Klipper disconnect/exit. Cutting 24V PSU power.")
            self._set_power(False)

    def cmd_M80(self, gcmd):
        if not self.is_powered:
            gcmd.respond_info("Turning on 24V Power Supply...")
            self._set_power(True)
        else:
            gcmd.respond_info("24V Power Supply is already ON")

    def cmd_M81(self, gcmd):
        gcmd.respond_info("Turning off 24V Power Supply...")
        self._set_power(False)

    def get_status(self, eventtime):
        return {
            'power': self.is_powered
        }

def load_config(config):
    return HostPower(config)
