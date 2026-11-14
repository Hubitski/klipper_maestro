#!/bin/sh
# Verify that a linked AVR firmware really fits in the device.
#
# Some AVR linkers (notably binutils 2.26 from the Debian/Atmel toolchain)
# run their memory-region overflow check before --gc-sections is applied.
# At that point unused .rodata and .compile_time_request sections are still
# counted against the small SRAM region, so those linkers report bogus
# errors such as:
#
#     region `data' overflowed by 223 bytes
#
# even when the final .data+.bss usage is only a couple of hundred bytes.
# The AVR build therefore links with --noinhibit-exec (which keeps the ELF
# despite those errors) and calls this script to do the real check.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

set -e

elf=$1
mcu=$2

if [ -z "$elf" ] || [ -z "$mcu" ]; then
    echo "usage: check-avr-size.sh <elf> <mcu>" >&2
    exit 2
fi

if [ ! -f "$elf" ]; then
    echo "check-avr-size: $elf was not created (link failed)" >&2
    exit 1
fi

undef=$(avr-nm -u "$elf" || true)
if [ -n "$undef" ]; then
    echo "check-avr-size: undefined symbols in $elf:" >&2
    echo "$undef" >&2
    exit 1
fi

report=$(avr-size --mcu="$mcu" --format=avr "$elf")
echo "$report"

echo "$report" | awk '
    /^Program:/ { p = $4; gsub(/[()%]/, "", p); prog = p + 0; havep = 1 }
    /^Data:/    { d = $4; gsub(/[()%]/, "", d); data = d + 0; haved = 1 }
    END {
        if (!havep || !haved) {
            print "check-avr-size: cannot parse avr-size output" > "/dev/stderr"
            exit 2
        }
        if (prog > 100.0 || data > 100.0) {
            printf "check-avr-size: firmware does not fit:" \
                   " program %.1f%%, data %.1f%%\n", prog, data > "/dev/stderr"
            exit 1
        }
    }'
