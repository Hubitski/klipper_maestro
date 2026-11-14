#!/usr/bin/env bash
# Build script for all 4 microcontrollers of Maestro Grand 2 IDEX
#
# NOTE: the output directory must NOT be inside out/.  Every target starts
# with "make clean" (which is rm -rf out/), so storing the generated hex
# files there would delete the results of the previous targets.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CONFIG_DIR="${SCRIPT_DIR}/configs"
OUTPUT_DIR="${KLIPPER_DIR}/firmware"
BUILD_FAILURES=0

mkdir -p "${OUTPUT_DIR}"
cd "${KLIPPER_DIR}" || exit 1
chmod +x "${KLIPPER_DIR}/scripts/"*.sh 2>/dev/null || true

echo "=========================================================="
echo " Building Klipper for Maestro Grand 2 IDEX (4 MCUs)"
echo "=========================================================="

build_target() {
    local target_name="$1"
    local config_file="$2"
    local hex_output="$3"
    local want_id resolved_id

    echo ""
    echo ">>> Building: ${target_name}..."

    if [ ! -r "${config_file}" ]; then
        echo ">>> FAILED: ${target_name} - config not found: ${config_file}"
        return 1
    fi

    make clean || { echo ">>> FAILED: ${target_name} - make clean"; return 1; }
    cp "${config_file}" "${KLIPPER_DIR}/.config" \
        || { echo ">>> FAILED: ${target_name} - cannot install config"; return 1; }
    make olddefconfig \
        || { echo ">>> FAILED: ${target_name} - olddefconfig"; return 1; }

    # Make sure the resolved configuration really is the requested node.
    want_id=$(sed -n 's/^CONFIG_SERIAL_NODE_ID=//p' "${config_file}" | head -n1)
    resolved_id=$(sed -n 's/^CONFIG_SERIAL_NODE_ID=//p' "${KLIPPER_DIR}/.config" \
                  | head -n1)
    if [ -n "${want_id}" ] && [ -n "${resolved_id}" ] \
       && [ $((want_id)) -ne $((resolved_id)) ]; then
        echo ">>> FAILED: ${target_name} - node id ${want_id} resolved as ${resolved_id}"
        return 1
    fi

    if ! make -j"$(nproc 2>/dev/null || echo 2)"; then
        echo ""
        echo ">>> FAILED: ${target_name} - memory overflow or build error."
        echo ">>> Building diagnostic ELF to show real memory requirements..."
        make diag || true
        echo ">>> Note: out/klipper_diag.elf is NOT flashable, it is only"
        echo ">>>       a size report for the overflowing build."
        return 1
    fi

    if [ ! -s "${KLIPPER_DIR}/out/klipper.elf.hex" ]; then
        echo ">>> FAILED: ${target_name} - hex file was not produced"
        return 1
    fi
    cp "${KLIPPER_DIR}/out/klipper.elf.hex" "${hex_output}" \
        || { echo ">>> FAILED: ${target_name} - cannot copy hex"; return 1; }

    echo ">>> Generated: ${hex_output}"
    avr-size "${KLIPPER_DIR}/out/klipper.elf"
    return 0
}

# 1. Motherboard (ATmega1284p, Router enabled, Node 0x10)
build_target "Motherboard (ATmega1284p)" \
    "${CONFIG_DIR}/config.mcu_motherboard_1284p" \
    "${OUTPUT_DIR}/klipper_mcu_motherboard_1284p.hex" \
    || BUILD_FAILURES=$((BUILD_FAILURES + 1))

# 2. Heated Bed (ATmega16A, Polled Slave, Node 0x40) - lightest build
build_target "Heated Bed (ATmega16A)" \
    "${CONFIG_DIR}/config.mcu_bed_atmega16" \
    "${OUTPUT_DIR}/klipper_mcu_bed_atmega16.hex" \
    || BUILD_FAILURES=$((BUILD_FAILURES + 1))

# 3. Toolhead 0 (ATmega16A, Polled Slave, Node 0x20)
build_target "Toolhead 0 Left (ATmega16A)" \
    "${CONFIG_DIR}/config.mcu_toolhead0_atmega16" \
    "${OUTPUT_DIR}/klipper_mcu_head0_atmega16.hex" \
    || BUILD_FAILURES=$((BUILD_FAILURES + 1))

# 4. Toolhead 1 (ATmega16A, Polled Slave, Node 0x30)
build_target "Toolhead 1 Right (ATmega16A)" \
    "${CONFIG_DIR}/config.mcu_toolhead1_atmega16" \
    "${OUTPUT_DIR}/klipper_mcu_head1_atmega16.hex" \
    || BUILD_FAILURES=$((BUILD_FAILURES + 1))

echo ""
echo "=========================================================="
if [ "${BUILD_FAILURES}" -eq 0 ]; then
    echo " All 4 firmware binaries built successfully!"
    echo " Location: ${OUTPUT_DIR}/"
    ls -lh "${OUTPUT_DIR}"/*.hex
else
    echo " RESULT: ${BUILD_FAILURES} of 4 builds FAILED (see size reports above)."
    echo " Firmware directory: ${OUTPUT_DIR}/"
fi
echo "=========================================================="
exit "${BUILD_FAILURES}"
