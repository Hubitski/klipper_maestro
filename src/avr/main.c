// Main starting point for AVR boards.
//
// Copyright (C) 2016,2017  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <avr/io.h> // AVR_STACK_POINTER_REG
#include <util/crc16.h> // _crc_ccitt_update
#include "autoconf.h" // CONFIG_MCU
#include "board/misc.h" // dynmem_start
#include "command.h" // DECL_CONSTANT
#include "irq.h" // irq_enable
#include "sched.h" // sched_main

// Newer avr-libc headers expose the stack pointer as SP instead of the
// older AVR_STACK_POINTER_REG alias used by Klipper.
#ifndef AVR_STACK_POINTER_REG
#define AVR_STACK_POINTER_REG SP
#endif

DECL_CONSTANT_STR("MCU", CONFIG_MCU);


/****************************************************************
 * Dynamic memory
 ****************************************************************/

// Return the start of memory available for dynamic allocations
void *
dynmem_start(void)
{
    extern char _end;
    return &_end;
}

// Return the end of memory available for dynamic allocations
void *
dynmem_end(void)
{
    // On every other supported AVR chip RAMEND+1 is already 256-byte
    // aligned, so the historical ALIGN(AVR_STACK_POINTER_REG, 256) form is
    // equivalent to RAMEND+1.  The ATmega16A has its 1KB SRAM at
    // 0x60..0x45F, so ALIGN() would round 0x45F *up* to 0x500 - 160 bytes
    // past the end of RAM - and the allocator would overwrite the stack and
    // non-existent memory.  Use the top of RAM directly.
    return (void*)(RAMEND + 1 - CONFIG_AVR_STACK_SIZE);
}


/****************************************************************
 * Misc functions
 ****************************************************************/

// Initialize the clock prescaler (if necessary)
void
prescaler_init(void)
{
#if defined(CLKPR)
    if (CONFIG_AVR_CLKPR != -1 && (uint8_t)CONFIG_AVR_CLKPR != CLKPR) {
        irqstatus_t flag = irq_save();
        CLKPR = 0x80;
        CLKPR = CONFIG_AVR_CLKPR;
        irq_restore(flag);
    }
#endif
}
DECL_INIT(prescaler_init);

// Pre-Klipper fan state on the ATmega16 toolheads.  The generic
// CONFIG_INITIAL_PINS table costs ~44 bytes and does not fit the 16KB
// flash; this is the same behavior: hotend fan (PD4) on so the hotend is
// cooled from power-up, part fan (PD5) held off instead of spinning on a
// floating gate.  The bed has WANT_STEPPER=n so its PD4/PD5 heater gates
// are not touched.
#if CONFIG_MACH_atmega16 && CONFIG_WANT_STEPPER
static void
toolhead_fans_init(void)
{
    DDRD |= (1 << PD4) | (1 << PD5);
    PORTD = (PORTD & ~(1 << PD5)) | (1 << PD4);
}
DECL_INIT(toolhead_fans_init);
#endif

// Optimized crc16_ccitt for the avr processor
uint16_t
crc16_ccitt(uint8_t *buf, uint_fast8_t len)
{
    uint16_t crc = 0xFFFF;
    while (len--)
        crc = _crc_ccitt_update(crc, *buf++);
    return crc;
}

// Main entry point for avr code.
int
main(void)
{
    irq_enable();
    sched_main();
    return 0;
}
