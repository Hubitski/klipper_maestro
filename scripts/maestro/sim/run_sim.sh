#!/bin/sh
# Bring up the full Maestro software stack without hardware:
#
#   Klipper (klippy) -> Linux host MCU -> host_multiplexer.py
#        -> sim_bus.py (half-duplex bus hub) -> 3 x simavr ATmega16
#
# The simulated boards run the real Klipper ELF built on the Pi (same code
# that is flashed), paced to wall-clock time by maestro_sim.
#
# Environment:
#   KLIPPER_DIR   repo root                (default: ../../.. from this file)
#   SIM_BUILD     build dir with binaries  (default: $HOME/klipper-sim)
#   KLIPPY_PY     klippy python            (default: ~/klippy-sim-env/bin/python)
#   LOG_DIR       log/PTY directory        (default: /tmp/maestro_sim)
#   BED_DELAY     seconds added to bed RX  (default 0; 0.03 reproduces the
#                 late-config "Timer too close" on a bed build without
#                 CONFIG_ADC_QUERY_CLAMP)
#   BED_JITTER    random +/- seconds on bed RX (default 0)
#
# This file may be distributed under the terms of the GNU GPLv3 license.

set -eu

SIM_DIR=$(cd "$(dirname "$0")" && pwd)
KLIPPER_DIR=${KLIPPER_DIR:-$(cd "$SIM_DIR/../../.." && pwd)}
SIM_BUILD=${SIM_BUILD:-$HOME/klipper-sim}
KLIPPY_PY=${KLIPPY_PY:-$HOME/klippy-sim-env/bin/python}
LOG_DIR=${LOG_DIR:-/tmp/maestro_sim}
BED_DELAY=${BED_DELAY:-0}
BED_JITTER=${BED_JITTER:-0}
BED_DROP=${BED_DROP:-0}
HEAD0_DELAY=${HEAD0_DELAY:-0}
HEAD0_JITTER=${HEAD0_JITTER:-0}
HEAD0_DROP=${HEAD0_DROP:-0}
HEAD1_DELAY=${HEAD1_DELAY:-0}
HEAD1_JITTER=${HEAD1_JITTER:-0}
HEAD1_DROP=${HEAD1_DROP:-0}
ROGUE_INTERVAL=${ROGUE_INTERVAL:-0}
BED_ELF=${BED_ELF:-bed.elf}
SIM_SPEED=${SIM_SPEED:-1}
HUB_BAUD=$(python3 -c "print(int(250000*$SIM_SPEED))")
BUS_GUARD=$(python3 -c "print(0.001/$SIM_SPEED)")
BUS_TIMEOUT=$(python3 -c "print(0.020/$SIM_SPEED)")

CFG=${CFG:-"$SIM_DIR/printer-sim.cfg"}
SIM="$SIM_BUILD/maestro_sim"
ELF_DIR="$SIM_BUILD/elf"

for f in "$SIM" "$KLIPPY_PY" "$ELF_DIR/head0.elf" "$ELF_DIR/head1.elf" \
         "$ELF_DIR/$BED_ELF" "$SIM_BUILD/host_mcu.elf"; do
    if [ ! -e "$f" ]; then
        echo "run_sim: missing $f" >&2
        exit 1
    fi
done

mkdir -p "$LOG_DIR"
rm -f "$LOG_DIR"/*.log "$LOG_DIR"/*.out "$LOG_DIR"/printer "$LOG_DIR"/klippy.sock
rm -f /tmp/sim_node_head0 /tmp/sim_node_head1 /tmp/sim_node_bed \
      /tmp/sim_bus_upstream /tmp/klipper_host_mcu

PIDS=""

cleanup() {
    trap - INT TERM EXIT
    [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
    sleep 0.2
    [ -n "$PIDS" ] && kill -9 $PIDS 2>/dev/null || true
    rm -f /tmp/sim_node_head0 /tmp/sim_node_head1 /tmp/sim_node_bed \
          /tmp/sim_bus_upstream /tmp/klipper_host_mcu
    rm -f "$LOG_DIR/printer"
    echo "run_sim: stopped (logs in $LOG_DIR)"
}
trap cleanup INT TERM EXIT

# --- simulated boards ------------------------------------------------------
# ADC values at 25 C: head EPCOS 100K / 4.7k pull-up = 224 mV,
# bed NTC 40k / 10k pull-up = 1000 mV.
"$SIM" -m atmega16 -f 16000000 -e "$ELF_DIR/head0.elf" \
    -u /tmp/sim_node_head0 --adc 4=224 --speed "$SIM_SPEED" \
    --hx711 counts=1000,rate=80 >"$LOG_DIR/head0.out" 2>&1 &
PIDS="$PIDS $!"

"$SIM" -m atmega16 -f 16000000 -e "$ELF_DIR/head1.elf" \
    -u /tmp/sim_node_head1 --adc 4=224 --speed "$SIM_SPEED" \
    >"$LOG_DIR/head1.out" 2>&1 &
PIDS="$PIDS $!"

"$SIM" -m atmega16 -f 16000000 -e "$ELF_DIR/$BED_ELF" \
    -u /tmp/sim_node_bed --adc 4=1000 --speed "$SIM_SPEED" \
    >"$LOG_DIR/bed.out" 2>&1 &
PIDS="$PIDS $!"

# Wait for the three PTYs to be published.
i=0
while [ $i -lt 50 ]; do
    [ -e /tmp/sim_node_head0 ] && [ -e /tmp/sim_node_head1 ] \
        && [ -e /tmp/sim_node_bed ] && break
    i=$((i + 1))
    sleep 0.1
done
if [ ! -e /tmp/sim_node_bed ]; then
    echo "run_sim: boards did not start, see $LOG_DIR/*.out" >&2
    exit 1
fi

# --- sequence alignment ----------------------------------------------------
# The firmware starts at sequence 0, Klippy's first message is sequence 1.
# One valid sequence-0 get_config per node advances the firmware and drains
# the queued "starting" report before the bus is connected.
python3 "$SIM_DIR/prime_node.py" head0=/tmp/sim_node_head0 \
    head1=/tmp/sim_node_head1 bed=/tmp/sim_node_bed \
    >"$LOG_DIR/prime.log" 2>&1 || true

# --- bus hub ---------------------------------------------------------------
python3 "$SIM_DIR/sim_bus.py" --upstream /tmp/sim_bus_upstream \
    --baud "$HUB_BAUD" \
    --delay "head0=$HEAD0_DELAY" --delay "head1=$HEAD1_DELAY" \
    --delay "bed=$BED_DELAY" \
    --jitter "head0=$HEAD0_JITTER" --jitter "head1=$HEAD1_JITTER" \
    --jitter "bed=$BED_JITTER" \
    --drop "head0=$HEAD0_DROP" --drop "head1=$HEAD1_DROP" \
    --drop "bed=$BED_DROP" \
    --rogue-interval "$ROGUE_INTERVAL" \
    >"$LOG_DIR/sim_bus.log" 2>&1 &
PIDS="$PIDS $!"

i=0
while [ $i -lt 50 ] && [ ! -e /tmp/sim_bus_upstream ]; do
    i=$((i + 1)); sleep 0.1
done

# --- multiplexer (the same code that runs on the Pi) -----------------------
(cd "$KLIPPER_DIR" && python3 scripts/maestro/host_multiplexer.py \
    --periph-port /tmp/sim_bus_upstream --baud "$HUB_BAUD" \
    --bus-guard "$BUS_GUARD" --bus-timeout "$BUS_TIMEOUT" \
    --tap-log "$LOG_DIR/maestro_bus.log") >"$LOG_DIR/mux.log" 2>&1 &
PIDS="$PIDS $!"

# --- Linux host MCU --------------------------------------------------------
"$SIM_BUILD/host_mcu.elf" -I /tmp/klipper_host_mcu \
    >"$LOG_DIR/host_mcu.log" 2>&1 &
PIDS="$PIDS $!"

# --- Klippy ----------------------------------------------------------------
rm -f "$LOG_DIR/printer"
mkfifo "$LOG_DIR/printer"
# Hold the write side open so Klippy's read side does not see EOF.
tail -f /dev/null >"$LOG_DIR/printer" &
FIFO_HOLD=$!
PIDS="$PIDS $FIFO_HOLD"

( cd "$KLIPPER_DIR" && exec "$KLIPPY_PY" klippy/klippy.py "$CFG" \
    -l "$LOG_DIR/klippy.log" -a "$LOG_DIR/klippy.sock" \
    -I "$LOG_DIR/printer" ) &
KLIPPY_PID=$!
PIDS="$PIDS $KLIPPY_PID"

echo "run_sim: klippy pid $KLIPPY_PID, logs $LOG_DIR"
echo "run_sim: send G-code with: printf 'STATUS\\n' > $LOG_DIR/printer"
wait "$KLIPPY_PID"
