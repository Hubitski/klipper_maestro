// Initialization of AVR watchdog timer.
//
// Copyright (C) 2016  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <avr/interrupt.h> // WDT_vect
#include <avr/wdt.h> // wdt_enable
#include "command.h" // shutdown
#include "irq.h" // irq_disable
#include "sched.h" // DECL_TASK

// Compatibility alias for MCUSR on older chips like ATmega16
#if !defined(MCUSR) && defined(MCUCSR)
#define MCUSR MCUCSR
#endif

static uint8_t watchdog_shutdown;

#if defined(WDTCSR) && defined(WDIE)
ISR(WDT_vect)
{
    watchdog_shutdown = 1;
    shutdown("Watchdog timer!");
}
#endif

void
watchdog_reset(void)
{
    wdt_reset();
#if defined(WDTCSR) && defined(WDIE)
    if (watchdog_shutdown) {
        WDTCSR = 1<<WDIE;
        watchdog_shutdown = 0;
    }
#endif
}
DECL_TASK(watchdog_reset);

void
watchdog_init(void)
{
    // 0.5s timeout, interrupt and system reset (if supported by chip)
    wdt_enable(WDTO_500MS);
#if defined(WDTCSR) && defined(WDIE)
    WDTCSR = 1<<WDIE;
#endif
}
DECL_INIT(watchdog_init);

// Very early reset of the watchdog
void __attribute__((naked)) __visible __section(".init3")
watchdog_early_init(void)
{
    MCUSR = 0;
    wdt_disable();
#if defined(JTD)
    // Disable JTAG interface to release Port C pins for general GPIO use.
    // The datasheet requires writing JTD twice within 4 cycles.
#if defined(MCUCSR)
    uint8_t mcu = MCUCSR | (1 << JTD);
    MCUCSR = mcu;
    MCUCSR = mcu;
#elif defined(MCUCR)
    uint8_t mcu = MCUCR | (1 << JTD);
    MCUCR = mcu;
    MCUCR = mcu;
#endif
#endif
}

// Support reset on AVR via the watchdog timer
void
command_reset(uint32_t *args)
{
    irq_disable();
    wdt_enable(WDTO_15MS);
    for (;;)
        ;
}
DECL_COMMAND_FLAGS(command_reset, HF_IN_SHUTDOWN, "reset");
