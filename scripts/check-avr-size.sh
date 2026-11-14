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
# Usage: check-avr-size.sh <elf> <mcu> [linker-log]
#
# This file may be distributed under the terms of the GNU GPLv3 license.

set -e
export LC_ALL=C

elf=$1
mcu=$2
link_log=$3

if [ -z "$elf" ] || [ -z "$mcu" ]; then
    echo "usage: check-avr-size.sh <elf> <mcu> [linker-log]" >&2
    exit 2
fi

if [ ! -f "$elf" ]; then
    echo "check-avr-size: $elf was not created (link failed)" >&2
    exit 1
fi

# 1. The linker must not have left any unresolved symbols behind.
undef=$(avr-nm -u "$elf" || true)
if [ -n "$undef" ]; then
    echo "check-avr-size: undefined symbols in $elf:" >&2
    echo "$undef" >&2
    exit 1
fi

# 2. Only the known-bogus overflow diagnostics may be ignored.  Anything
#    else that the old linker printed (relocation truncation, missing
#    region, invalid script, ...) means the retained ELF is not usable.
if [ -n "$link_log" ] && [ -f "$link_log" ]; then
    allowed='section .* will not fit in region|section .* is not within region|region .* overflowed by [0-9]+ bytes|^collect2: error: ld returned 1 exit status$|^warning:|^[[:space:]]*$'
    bad=$(grep -vE "$allowed" "$link_log" || true)
    if [ -n "$bad" ]; then
        echo "check-avr-size: unexpected linker diagnostics:" >&2
        echo "$bad" >&2
        exit 1
    fi
fi

# 3. Human readable usage report.
report=$(avr-size --mcu="$mcu" --format=avr "$elf")
echo "$report"

# 4. Exact byte counts (the --format=avr percentages are rounded and the
#    text is locale dependent, so do not use them for the limit check).
set -- $(avr-size "$elf" | tail -n 1)
text=$1
data=$2
bss=$3
if [ -z "$text" ] || [ -z "$data" ] || [ -z "$bss" ]; then
    echo "check-avr-size: cannot parse avr-size output" >&2
    exit 2
fi

case "$mcu" in
    atmega16|atmega168)                flash=16384;  ram=1024 ;;
    atmega328|atmega328p|lgt8f328p)    flash=32768;  ram=2048 ;;
    atmega32u4)                        flash=32768;  ram=2560 ;;
    atmega644p)                        flash=65536;  ram=4096 ;;
    atmega1284p)                       flash=131072; ram=16384 ;;
    at90usb646)                        flash=65536;  ram=4096 ;;
    at90usb1286)                       flash=131072; ram=8192 ;;
    atmega1280|atmega2560)             flash=262144; ram=8192 ;;
    *)                                 flash=0;      ram=0 ;;
esac

if [ "$flash" -gt 0 ]; then
    prog=$((text + data))
    ramuse=$((data + bss))
    if [ "$prog" -gt "$flash" ]; then
        echo "check-avr-size: program $prog bytes exceeds flash $flash" >&2
        exit 1
    fi
    if [ "$ramuse" -gt "$ram" ]; then
        echo "check-avr-size: data $ramuse bytes exceeds SRAM $ram" >&2
        exit 1
    fi
    exit 0
fi

# Unknown MCU - fall back to the avr-size percentages.
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
