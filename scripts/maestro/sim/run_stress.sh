#!/bin/sh
# Run the full simulator for DURATION seconds under configurable bus stress
# and print a stability summary.
#
# Clean baseline (should have zero MCU shutdowns and zero mux timeouts):
#   DURATION=300 sh run_stress.sh
#
# Fault injection:
#   DURATION=300 ROGUE_INTERVAL=0.02 HEAD0_JITTER=0.0002 BED_DELAY=0.001 \
#       sh run_stress.sh
#
# Speed-up (experimental - the whole stack must stay consistent):
#   SIM_SPEED=10 DURATION=60 sh run_stress.sh
#
# Exit status: 0 if no MCU shutdown / lost communication / serial error,
# 1 otherwise.  Timeouts and collisions are reported but do not fail the
# run unless they lead to a shutdown.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

set -eu

SIM_DIR=$(cd "$(dirname "$0")" && pwd)
KLIPPER_DIR=${KLIPPER_DIR:-$(cd "$SIM_DIR/../../.." && pwd)}
LOG_DIR=${LOG_DIR:-/tmp/maestro_sim}
DURATION=${DURATION:-60}

# Stress defaults: clean unless the caller injects faults.
ROGUE_INTERVAL=${ROGUE_INTERVAL:-0}
HEAD0_DELAY=${HEAD0_DELAY:-0}; HEAD1_DELAY=${HEAD1_DELAY:-0}
BED_DELAY=${BED_DELAY:-0}
HEAD0_JITTER=${HEAD0_JITTER:-0}; HEAD1_JITTER=${HEAD1_JITTER:-0}
BED_JITTER=${BED_JITTER:-0}
HEAD0_DROP=${HEAD0_DROP:-0}; HEAD1_DROP=${HEAD1_DROP:-0}
BED_DROP=${BED_DROP:-0}
SIM_SPEED=${SIM_SPEED:-1}
export ROGUE_INTERVAL HEAD0_DELAY HEAD1_DELAY BED_DELAY \
       HEAD0_JITTER HEAD1_JITTER BED_JITTER HEAD0_DROP HEAD1_DROP BED_DROP \
       SIM_SPEED LOG_DIR

mkdir -p "$LOG_DIR"
rm -f "$LOG_DIR"/stress_run.out

cd "$KLIPPER_DIR"
sh "$SIM_DIR/run_sim.sh" >"$LOG_DIR/stress_run.out" 2>&1 &
RUNPID=$!

echo "run_stress: started pid $RUNPID, ${DURATION}s, speed=${SIM_SPEED}x"
sleep "$DURATION"

kill -TERM "$RUNPID" 2>/dev/null || true
wait "$RUNPID" 2>/dev/null || true

K="$LOG_DIR/klippy.log"
T="$LOG_DIR/maestro_bus.log"
M="$LOG_DIR/mux.log"
B="$LOG_DIR/sim_bus.log"

cnt() {
    # cnt <file> <grep -E pattern>; prints 0 when the file/pattern is absent
    if [ -f "$1" ]; then
        grep -cE "$2" "$1" 2>/dev/null || true
    else
        echo 0
    fi
}

shutdowns=$(cnt "$K" "MCU '[^']+' shutdown|Timer too close|Rescheduled timer in the past|Lost communication|Serial connection closed|MCU error during")
mcu_loaded=$(cnt "$K" "Loaded MCU")
timeouts=$(cnt "$T" "release: timeout")
bad=$(cnt "$T" "PARSE .*bad-")
quarantine=$(cnt "$M" "Quarantining")
qfull=$(cnt "$M" "queue full")
collisions=$(cnt "$B" "COLLISION")
rogue=$(cnt "$B" "rogue")

echo "===== stress summary (${DURATION}s, speed=${SIM_SPEED}x) ====="
echo "mcus loaded:        $mcu_loaded"
echo "shutdown/errors:    $shutdowns"
echo "mux timeouts:       $timeouts"
echo "tap parse errors:   $bad"
echo "quarantines:        $quarantine"
echo "node queue full:    $qfull"
echo "bus collisions:     $collisions"
echo "rogue injections:   $rogue"
if [ -f "$B" ]; then
    tail -1 "$B" | sed 's/^/hub: /'
fi

if [ "$shutdowns" -ne 0 ] || [ "$mcu_loaded" -lt 3 ]; then
    echo "RESULT: FAIL (MCU shutdown/error or not all MCUs loaded)"
    exit 1
fi
echo "RESULT: PASS (no MCU shutdown; inspect timeouts/collisions above)"
