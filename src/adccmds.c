// Commands for controlling GPIO analog-to-digital input pins
//
// Copyright (C) 2016-2026  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // struct gpio_adc
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "sched.h" // DECL_TASK
#include "trigger_analog.h" // trigger_analog_update

struct analog_in {
    struct timer timer;
    uint32_t rest_time, sample_time, next_begin_time;
    uint16_t value, min_value, max_value;
    struct gpio_adc pin;
    uint8_t invalid_count, range_check_count;
    uint8_t state, sample_count;
    uint8_t bytes_per_report, data_count;
#if CONFIG_HAVE_LIMITED_CODE_SIZE
    uint8_t data[8];
#else
    uint8_t data[48];
#endif
    struct trigger_analog *ta;
};

static struct task_wake analog_wake;

// Reschedule a timer without ever falling behind the current time.  With
// several ADC channels one channel can be starved while another conversion
// is in progress; blindly adding the retry delay to a stale waketime made
// the timer fire with a waketime in the past forever (interrupt storm,
// watchdog reset, host sees "Lost communication").  On tiny MCUs with a
// single ADC channel this cannot happen, so the clamp can be compiled out
// to save flash (CONFIG_ADC_TIMER_CLAMP=n).
#if CONFIG_ADC_TIMER_CLAMP && !CONFIG_ADC_TIMER_CLAMP_REPORT_ONLY
static uint32_t
analog_clamp_waketime(uint32_t waketime, uint32_t add)
{
    uint32_t now = timer_read_time();
    if (timer_is_before(waketime, now))
        waketime = now;
    return waketime + add;
}
#define ADC_TIMER_NEXT(waketime, add) analog_clamp_waketime(waketime, add)
#elif CONFIG_ADC_TIMER_CLAMP
// Report-only clamp for the 16KB toolheads: the report path is clamped
// inline below, so the helper function is not emitted.
#define ADC_TIMER_NEXT(waketime, add) ((waketime) + (add))
#else
#define ADC_TIMER_NEXT(waketime, add) ((waketime) + (add))
#endif

static uint_fast8_t
analog_in_event(struct timer *timer)
{
    struct analog_in *a = container_of(timer, struct analog_in, timer);
    uint32_t sample_delay = gpio_adc_sample(a->pin);
    if (sample_delay) {
        a->timer.waketime = ADC_TIMER_NEXT(a->timer.waketime, sample_delay);
        return SF_RESCHEDULE;
    }
    uint16_t value = gpio_adc_read(a->pin);
    uint8_t state = a->state;
    if (state >= a->sample_count) {
        state = 0;
    } else {
        value += a->value;
    }
    a->value = value;
    a->state = state+1;
    if (a->state < a->sample_count) {
        a->timer.waketime = ADC_TIMER_NEXT(a->timer.waketime, a->sample_time);
        return SF_RESCHEDULE;
    }
    if (likely(a->value >= a->min_value && a->value <= a->max_value)) {
        a->invalid_count = 0;
    } else {
        a->invalid_count++;
        if (a->invalid_count >= a->range_check_count) {
            try_shutdown("ADC out of range");
            a->invalid_count = 0;
        }
    }
    sched_wake_task(&analog_wake);
    // A starved channel can push the next report slot into the past; the
    // ADC timer clamp must cover this report path too.  In full-clamp mode
    // reuse the helper (clamp, then add the rest delay) instead of keeping
    // a second copy of the clamp in this function - that duplicate costs
    // the last few bytes the 16KB toolhead does not have.
#if CONFIG_ADC_TIMER_CLAMP && !CONFIG_ADC_TIMER_CLAMP_REPORT_ONLY
    a->next_begin_time = ADC_TIMER_NEXT(a->next_begin_time, a->rest_time);
#else
    a->next_begin_time += a->rest_time;
#if CONFIG_ADC_TIMER_CLAMP
    {
        uint32_t _adc_now = timer_read_time();
        if (timer_is_before(a->next_begin_time, _adc_now))
            a->next_begin_time = _adc_now;
    }
#endif
#endif
    a->timer.waketime = a->next_begin_time;
    return SF_RESCHEDULE;
}

void
command_config_analog_in(uint32_t *args)
{
    struct gpio_adc pin = gpio_adc_setup(args[1]);
    struct analog_in *a = oid_alloc(
        args[0], command_config_analog_in, sizeof(*a));
    a->timer.func = analog_in_event;
    a->pin = pin;
    a->state = 1;
}
DECL_COMMAND(command_config_analog_in, "config_analog_in oid=%c pin=%u");

void
command_query_analog_in(uint32_t *args)
{
    struct analog_in *a = oid_lookup(args[0], command_config_analog_in);
    sched_del_timer(&a->timer);
    gpio_adc_cancel_sample(a->pin);
    a->next_begin_time = args[1];
#if CONFIG_ADC_QUERY_CLAMP
    // The query clock comes from the host.  On the polled multi-drop bus
    // the config block can be delivered late enough that this clock is
    // already past, and the sched_add_timer() below then shuts the MCU
    // down with "Timer too close" during config (seen on the bed).  Clamp
    // it exactly like the report path.  Only the bed enables this: the
    // 16KB toolheads need the flash for the fans/load cell and their one
    // ADC channel is queried before the bus gets busy.
    {
        uint32_t now = timer_read_time();
        if (timer_is_before(a->next_begin_time, now))
            a->next_begin_time = now;
    }
#endif
    a->timer.waketime = a->next_begin_time;
    a->sample_time = args[2];
    a->sample_count = args[3];
    a->state = a->sample_count + 1;
    a->rest_time = args[4];
    a->bytes_per_report = args[5];
    a->data_count = 0;
    a->min_value = args[6];
    a->max_value = args[7];
    a->range_check_count = args[8];
    if (! a->sample_count)
        return;
    if (a->bytes_per_report > ARRAY_SIZE(a->data))
        shutdown("Invalid analog_in bytes_per_report");
    sched_add_timer(&a->timer);
}
DECL_COMMAND(command_query_analog_in,
             "query_analog_in oid=%c clock=%u sample_ticks=%u sample_count=%c"
             " rest_ticks=%u bytes_per_report=%c"
             " min_value=%hu max_value=%hu range_check_count=%c");

void
command_analog_in_attach_trigger_analog(uint32_t *args) {
    struct analog_in *a = oid_lookup(args[0], command_config_analog_in);
    a->ta = trigger_analog_oid_lookup(args[1]);
}
#if CONFIG_WANT_TRIGGER_ANALOG
DECL_COMMAND(command_analog_in_attach_trigger_analog,
    "analog_in_attach_trigger_analog oid=%c trigger_analog_oid=%c");
#endif

#define BYTES_PER_SAMPLE 2

void
analog_in_task(void)
{
    if (!sched_check_wake(&analog_wake))
        return;
    uint8_t oid;
    struct analog_in *a;
    foreach_oid(oid, a, command_config_analog_in) {
        if (a->state != a->sample_count)
            continue;
        irq_disable();
        if (a->state != a->sample_count) {
            irq_enable();
            continue;
        }
        uint16_t value = a->value;
        uint32_t next_begin_time = a->next_begin_time;
        a->state++;
        irq_enable();
        trigger_analog_update(a->ta, value);
        uint8_t *d = &a->data[a->data_count];
        d[0] = value;
        d[1] = value >> 8;
        a->data_count += BYTES_PER_SAMPLE;
        if (a->data_count + BYTES_PER_SAMPLE > a->bytes_per_report) {
            sendf("analog_in_state oid=%c next_clock=%u values=%*s"
                  , oid, next_begin_time, a->data_count, a->data);
            a->data_count = 0;
        }
    }
}
DECL_TASK(analog_in_task);

void
analog_in_shutdown(void)
{
    uint8_t i;
    struct analog_in *a;
    foreach_oid(i, a, command_config_analog_in) {
        gpio_adc_cancel_sample(a->pin);
        a->ta = NULL;
        if (a->sample_count) {
            a->state = a->sample_count + 1;
            a->next_begin_time += a->rest_time;
            a->timer.waketime = a->next_begin_time;
            sched_add_timer(&a->timer);
        }
    }
}
DECL_SHUTDOWN(analog_in_shutdown);
