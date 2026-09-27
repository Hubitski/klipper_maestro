#!/usr/bin/env bash
# Build script for all 4 microcontrollers of Maestro Grand 2 IDEX
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CONFIG_DIR="${SCRIPT_DIR}/configs"
OUTPUT_DIR="${KLIPPER_DIR}/out/maestro_firmware"

mkdir -p "${OUTPUT_DIR}"
cd "${KLIPPER_DIR}"
chmod +x "${KLIPPER_DIR}/scripts/"*.sh 2>/dev/null || true

echo "=========================================================="
echo " Building Klipper for Maestro Grand 2 IDEX (4 MCUs)"
echo "=========================================================="

build_target() {
    local target_name="$1"
    local config_file="$2"
    local hex_output="$3"

    echo ""
    echo ">>> Building: ${target_name}..."
    make clean
    cp "${config_file}" "${KLIPPER_DIR}/.config"
    make olddefconfig
    make -j$(nproc 2>/dev/null || echo 2)
    mkdir -p "${OUTPUT_DIR}"
    cp "${KLIPPER_DIR}/out/klipper.elf.hex" "${hex_output}"
    echo ">>> Generated: ${hex_output}"
    avr-size "${KLIPPER_DIR}/out/klipper.elf" || true
}

# 1. Motherboard (ATmega1284p, Router enabled, Node 0x10)
build_target "Motherboard (ATmega1284p)" \
    "${CONFIG_DIR}/config.mcu_motherboard_1284p" \
    "${OUTPUT_DIR}/klipper_mcu_motherboard_1284p.hex"

# 2. Toolhead 0 (ATmega16A, Polled Slave, Node 0x20)
build_target "Toolhead 0 Left (ATmega16A)" \
    "${CONFIG_DIR}/config.mcu_toolhead0_atmega16" \
    "${OUTPUT_DIR}/klipper_mcu_head0_atmega16.hex"

# 3. Toolhead 1 (ATmega16A, Polled Slave, Node 0x30)
build_target "Toolhead 1 Right (ATmega16A)" \
    "${CONFIG_DIR}/config.mcu_toolhead1_atmega16" \
    "${OUTPUT_DIR}/klipper_mcu_head1_atmega16.hex"

# 4. Heated Bed (ATmega16A, Polled Slave, Node 0x40)
build_target "Heated Bed (ATmega16A)" \
    "${CONFIG_DIR}/config.mcu_bed_atmega16" \
    "${OUTPUT_DIR}/klipper_mcu_bed_atmega16.hex"

echo ""
echo "=========================================================="
echo " All 4 firmware binaries built successfully!"
echo " Location: ${OUTPUT_DIR}/"
ls -lh "${OUTPUT_DIR}"/*.hex
echo "=========================================================="
