# Maestro host-in-the-loop simulator (WSL2)

Runs the real Klipper host, the real `host_multiplexer.py` and the real AVR
firmware ELFs for head0/head1/bed under simavr, on one Linux machine.

```
klippy ── [mcu head0/head1/bed] ── host_multiplexer.py
                                      │ /tmp/sim_bus_upstream
                                      │
                                  sim_bus.py (half-duplex wire, echo,
                                  per-node delay/jitter/drop, collisions)
                                      │
                     ┌────────────────┼────────────────┐
               simavr head0     simavr head1     simavr bed
```

The host MCU is the primary `[mcu]` (Linux process build); the main
ATmega1284p is not simulated (motors/TMC are out of scope). `printer-sim.cfg`
mirrors the real heads/bed sections, including the load cell on head0.

## Setup (once, inside WSL2 Ubuntu)

```
sudo apt install simavr libsimavr-dev simavr-utils gcc-avr avr-libc \
     binutils-avr gdb-avr socat python3-venv python3-dev build-essential \
     pkg-config libelf-dev
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.9 ~/klippy-sim-env
uv pip install --python ~/klippy-sim-env/bin/python \
     -r ~/klipper/scripts/klippy-requirements.txt numpy
```

Build the host MCU (`CONFIG_MACH_LINUX=y`), then run.

## Run

```
cd ~/klipper
sh scripts/maestro/sim/run_sim.sh
```

The script starts the three simavr instances, primes the slaves' message
sequence, starts the bus hub, the multiplexer, the Linux MCU and Klippy.
Logs: `/tmp/maestro_sim/` (`klippy.log`, `mux.log`, `maestro_bus.log`,
`prime.log`, per-board `.out`). Send G-code with
`printf 'STATUS\n' > /tmp/maestro_sim/printer`.

Environment:
- `CFG=` config file (default `printer-sim.cfg`)
- `BED_ELF=` bed firmware in `~/klipper-sim/elf` (default `bed.elf`)
- `BED_DELAY=` / `BED_JITTER=` seconds of delay/jitter on bed RX
- `SIM_BUILD=`, `KLIPPY_PY=`, `LOG_DIR=`

Bed query-clock regression (needs `bed_noqc.elf`, built with
`CONFIG_ADC_QUERY_CLAMP=n`):

```
CFG=$PWD/scripts/maestro/sim/bed-only.cfg BED_ELF=bed_noqc.elf BED_DELAY=0.01 \
    sh scripts/maestro/sim/run_sim.sh
# -> MCU 'bed' error during config: Timer too close

CFG=$PWD/scripts/maestro/sim/bed-only.cfg BED_ELF=bed.elf BED_DELAY=0.01 \
    sh scripts/maestro/sim/run_sim.sh
# -> no Timer too close at config (clamped query clock)
```

## simavr quirks worked around

1. **Real-time pacing.** simavr's raw run is as-fast-as-possible; Klipper
   derives command clocks from the MCU clock, so `maestro_sim` keeps
   `avr->cycle / frequency == wall clock` with a ~250 us quantum.
2. **`sei; nop; cli` loses interrupts** in simavr (Klipper's `irq_wait()`).
   The simulation ELFs are built with one extra `nop` in `src/avr/irq.h`.
3. **TXC interrupt is not raised by enabling TXCIE** in simavr, so the
   polled-slave never re-enables RXEN after a response. The simulation
   ELFs re-enable RXEN directly in the UDRE ISR (`src/avr/serial.c`).

Patches 2 and 3 are **simulation-only**: the firmware flashed to the boards
and the hex files in `firmware/` are untouched (the Pi working tree is
restored with `git checkout` after building the sim ELFs). The ADC/HX711
stubs and the bus hub are in this directory.

4. **Sequence priming.** The firmware starts at sequence 0 while Klippy's
   first message is sequence 1; `prime_node.py` sends one valid sequence-0
   get_config before Klippy connects, otherwise the first host packet is a
   NAK and Klippy's connection-init path drops the command.

## Known limitations

- No main board: no steppers/TMC/sensorless, no Z homing or `PROBE_CALIBRATE`.
  Extruder/bed PID, ADC queries, HX711/load-cell data, heaters and fans are
  real paths.
- HX711 counts and ADC millivolts are static (no thermal model yet).
- simavr is an interpreter, not cycle-accurate; timing is wall-clock paced.
