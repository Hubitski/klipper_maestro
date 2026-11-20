// Support for bit-banging commands to HX711 and HX717 ADC chips
//
// Copyright (C) 2024 Gareth Farrington <gareth@waves.ky>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <stdbool.h>
#include <stdint.h>
#include "autoconf.h" // CONFIG_MACH_AVR
#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // gpio_out_write
#include "board/irq.h" // irq_poll
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "sched.h" // sched_add_timer
#include "sensor_bulk.h" // sensor_bulk_report
#include "trigger_analog.h" // trigger_analog_update

struct hx71x_adc {
    struct timer timer;
    uint8_t gain_channel;   // the gain+channel selection (1-4)
    uint8_t flags;
    uint32_t rest_ticks;
    uint32_t last_error;
    struct gpio_in dout; // pin used to receive data from the hx71x
    struct gpio_out sclk; // pin used to generate clock for the hx71x
    struct sensor_bulk sb;
    struct trigger_analog *ta;
};

enum {
    HX_PENDING = 1<<0, HX_OVERFLOW = 1<<1,
};

#define BYTES_PER_SAMPLE 4
#define SAMPLE_ERROR_DESYNC 1L << 31
#define SAMPLE_ERROR_READ_TOO_LONG 1L << 30

// The Maestro toolhead HX711 is never put into power-down.  The stock
// Arduino sketch keeps PD_SCK low from power-up and reads real weight,
// while Klipper's power-down/wake cycle on this board leaves the analog
// side dead: conversions still run (~80/s) but every count reads back as
// zero.  Set to 0 to restore the upstream power-down behaviour.
#define HX71X_NO_POWERDOWN 1

static struct task_wake wake_hx71x;


/****************************************************************
 * Low-level bit-banging
 ****************************************************************/

// Read 'num_bits' from the sensor, matching the stock Arduino sketch
// bit for bit: SCLK high, two nops, shift, SCLK low, two nops, read DOUT.
// Interrupts stay enabled like in the sketch; the whole read is short.
static uint32_t
hx71x_raw_read(struct gpio_in dout, struct gpio_out sclk, int num_bits)
{
    uint32_t bits_read = 0;
    while (num_bits--) {
        // Write PORTx explicitly instead of gpio_out_toggle_noirq(): on
        // classic AVRs the PINx toggle trick is not reliable, which left
        // SCLK static and the sensor returned zeros.
        gpio_out_write(sclk, 1);       // SCLK high
        asm("nop\n    nop");
        bits_read <<= 1;
        gpio_out_write(sclk, 0);       // SCLK low (bit is shifted out)
        asm("nop\n    nop");
        if (gpio_in_read(dout))
            bits_read |= 1;
    }
    return bits_read;
}

// Send 'count' gain/channel select pulses.  The stock sketch sends a
// single extra pulse and does not read DOUT during it.
static void
hx71x_gain_pulses(struct gpio_out sclk, int count)
{
    while (count--) {
        gpio_out_write(sclk, 1);
        asm("nop\n    nop");
        gpio_out_write(sclk, 0);
        asm("nop\n    nop");
    }
}


/****************************************************************
 * HX711 and HX717 Sensor Support
 ****************************************************************/

// Check if data is ready
static uint_fast8_t
hx71x_is_data_ready(struct hx71x_adc *hx71x)
{
    return !gpio_in_read(hx71x->dout);
}

// Event handler that wakes wake_hx71x() periodically
static uint_fast8_t
hx71x_event(struct timer *timer)
{
    struct hx71x_adc *hx71x = container_of(timer, struct hx71x_adc, timer);
    uint32_t rest_ticks = hx71x->rest_ticks;
    uint8_t flags = hx71x->flags;
    if (flags & HX_PENDING) {
        hx71x->sb.possible_overflows++;
        hx71x->flags = HX_PENDING | HX_OVERFLOW;
        rest_ticks *= 4;
    } else if (hx71x_is_data_ready(hx71x)) {
        // New sample pending
        hx71x->flags = HX_PENDING;
        sched_wake_task(&wake_hx71x);
        rest_ticks *= 8;
    }
    hx71x->timer.waketime += rest_ticks;
    return SF_RESCHEDULE;
}

static void
add_sample(struct hx71x_adc *hx71x, uint8_t oid, uint32_t counts,
                uint8_t force_flush) {
    // Add measurement to buffer
    hx71x->sb.data[hx71x->sb.data_count] = counts;
    hx71x->sb.data[hx71x->sb.data_count + 1] = counts >> 8;
    hx71x->sb.data[hx71x->sb.data_count + 2] = counts >> 16;
    hx71x->sb.data[hx71x->sb.data_count + 3] = counts >> 24;
    hx71x->sb.data_count += BYTES_PER_SAMPLE;

    if (hx71x->sb.data_count + BYTES_PER_SAMPLE > ARRAY_SIZE(hx71x->sb.data)
        || force_flush)
        sensor_bulk_report(&hx71x->sb, oid);
}

// hx71x ADC query
static void
hx71x_read_adc(struct hx71x_adc *hx71x, uint8_t oid)
{
    // Read 24 data bits, then the gain/channel select pulses (the sketch
    // sends one extra pulse and ignores it)
    uint_fast8_t gain_channel = hx71x->gain_channel;
    uint32_t counts = hx71x_raw_read(hx71x->dout, hx71x->sclk, 24);
    hx71x_gain_pulses(hx71x->sclk, gain_channel);

    // Clear pending flag (and note if an overflow occurred)
    irq_disable();
    uint8_t flags = hx71x->flags;
    hx71x->flags = 0;
    irq_enable();

    // Sign-extend the 24 bit two's complement value
    if (counts & 0x800000)
        counts |= 0xFF000000;

    // Check for errors.  Do not treat the extra gain-selection clock bits
    // as a desync indicator: some HX711 modules do not drive DOUT high
    // immediately after the last (gain-select) pulse, so those bits read
    // back as zero even though the 24 data bits are perfectly valid.  A
    // plain Arduino sketch that ignores that pulse works with such
    // modules.  A too-long read is still reported.
    if (flags & HX_OVERFLOW) {
        // Transfer took too long
        hx71x->last_error = SAMPLE_ERROR_READ_TOO_LONG;
    }

    // forever send errors until reset
    if (hx71x->last_error != 0) {
        counts = hx71x->last_error;
    }

    // probe is optional, report if enabled
    if (hx71x->last_error == 0) {
        trigger_analog_update(hx71x->ta, counts);
    }

    // Add measurement to buffer
    add_sample(hx71x, oid, counts, false);
}

// Create a hx71x sensor
void
command_config_hx71x(uint32_t *args)
{
    struct hx71x_adc *hx71x = oid_alloc(args[0]
                , command_config_hx71x, sizeof(*hx71x));
    hx71x->timer.func = hx71x_event;
    uint8_t gain_channel = args[1];
    if (gain_channel < 1 || gain_channel > 4) {
        shutdown("HX71x gain/channel out of range 1-4");
    }
    hx71x->gain_channel = gain_channel;
    hx71x->dout = gpio_in_setup(args[2], 1);
    hx71x->sclk = gpio_out_setup(args[3], 0);
#if !HX71X_NO_POWERDOWN
    gpio_out_write(hx71x->sclk, 1); // put chip in power down state
#endif
}
DECL_COMMAND(command_config_hx71x, "config_hx71x oid=%c gain_channel=%c"
             " dout_pin=%u sclk_pin=%u");

void
hx71x_attach_trigger_analog(uint32_t *args) {
    uint8_t oid = args[0];
    struct hx71x_adc *hx71x = oid_lookup(oid, command_config_hx71x);
    hx71x->ta = trigger_analog_oid_lookup(args[1]);
}
#if CONFIG_WANT_TRIGGER_ANALOG
DECL_COMMAND(hx71x_attach_trigger_analog, "hx71x_attach_trigger_analog oid=%c"
    " trigger_analog_oid=%c");
#endif

// start/stop capturing ADC data
void
command_query_hx71x(uint32_t *args)
{
    uint8_t oid = args[0];
    struct hx71x_adc *hx71x = oid_lookup(oid, command_config_hx71x);
    sched_del_timer(&hx71x->timer);
    hx71x->flags = 0;
    hx71x->last_error = 0;
    hx71x->rest_ticks = args[1];
    if (!hx71x->rest_ticks) {
        // End measurements
#if !HX71X_NO_POWERDOWN
        gpio_out_write(hx71x->sclk, 1); // put chip in power down state
#endif
        return;
    }
    // Start new measurements
#if !HX71X_NO_POWERDOWN
    gpio_out_write(hx71x->sclk, 0); // wake chip from power down
#endif
    sensor_bulk_reset(&hx71x->sb);
    irq_disable();
    hx71x->timer.waketime = timer_read_time() + hx71x->rest_ticks;
    sched_add_timer(&hx71x->timer);
    irq_enable();
}
DECL_COMMAND(command_query_hx71x, "query_hx71x oid=%c rest_ticks=%u");

void
command_query_hx71x_status(const uint32_t *args)
{
    uint8_t oid = args[0];
    struct hx71x_adc *hx71x = oid_lookup(oid, command_config_hx71x);
    irq_disable();
    const uint32_t start_t = timer_read_time();
    uint8_t is_data_ready = hx71x_is_data_ready(hx71x);
    irq_enable();
    uint8_t pending_bytes = is_data_ready ? BYTES_PER_SAMPLE : 0;
    sensor_bulk_status(&hx71x->sb, oid, start_t, 0, pending_bytes);
}
DECL_COMMAND(command_query_hx71x_status, "query_hx71x_status oid=%c");

// Background task that performs measurements
void
hx71x_capture_task(void)
{
    if (!sched_check_wake(&wake_hx71x))
        return;
    uint8_t oid;
    struct hx71x_adc *hx71x;
    foreach_oid(oid, hx71x, command_config_hx71x) {
        if (hx71x->flags)
            hx71x_read_adc(hx71x, oid);
    }
}
DECL_TASK(hx71x_capture_task);
