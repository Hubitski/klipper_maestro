// Run a Klipper AVR firmware image in simavr, in wall-clock time, with the
// peripherals the Maestro boards need for a host-in-the-loop simulation.
//
//   maestro_sim -m atmega16 -f 16000000 -e head0.elf
//               -u /tmp/sim_node_head0 --adc 4=455
//               --hx711 dout=3,sclk=6,counts=100000,rate=80
//
// Unlike the stock simavr runner this one paces the AVR to real time: the
// emulated cycle count is kept equal to wall clock * frequency.  Klipper
// derives every command clock from the MCU clock, so an unpaced AVR races
// ahead of the host and every timer lands in the past.
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <simavr/sim_avr.h>
#include <simavr/sim_elf.h>
#include <simavr/sim_irq.h>
#include <simavr/sim_cycle_timers.h>
#include <simavr/avr_ioport.h>
#include <simavr/avr_adc.h>
#include <simavr/avr_uart.h>
#include <simavr/parts/uart_pty.h>

static volatile sig_atomic_t stop_requested;

static void
uart_trace_in(struct avr_irq_t *irq, uint32_t value, void *param)
{
    (void)irq; (void)param;
    fprintf(stderr, "uart rx %02x\n", value & 0xff);
}

static void
uart_trace_out(struct avr_irq_t *irq, uint32_t value, void *param)
{
    (void)irq; (void)param;
    fprintf(stderr, "uart tx %02x\n", value & 0xff);
}

static void
uart_trace_flow(struct avr_irq_t *irq, uint32_t value, void *param)
{
    (void)irq; (void)value; (void)param;
    static int on;
    if (irq == NULL)
        return;
    // XON and XOFF have separate IRQs; report each edge once.
    fprintf(stderr, "uart flow %s\n", (value & 1) ? "room" : "busy");
    (void)on;
}

static void
on_signal(int sig)
{
    (void)sig;
    stop_requested = 1;
}

static uint64_t
now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static uint64_t
cycles_to_ns(uint64_t cycles, uint32_t freq)
{
    return (cycles / freq) * 1000000000ull
        + ((cycles % freq) * 1000000000ull) / freq;
}


/****************************************************************
 * ADC injection
 ****************************************************************/

struct adc_state {
    avr_t *avr;
    uint16_t mv[16];
    uint8_t set_mask;   // atmega16 only has channels 0..7
};

static void
adc_trigger_hook(struct avr_irq_t *irq, uint32_t value, void *param)
{
    struct adc_state *s = param;
    (void)irq;
    (void)value;
    for (int ch = 0; ch < 8; ch++) {
        if (s->set_mask & (1 << ch))
            avr_raise_irq(avr_io_getirq(s->avr, AVR_IOCTL_ADC_GETIRQ,
                                        ADC_IRQ_ADC0 + ch), s->mv[ch]);
    }
}


/****************************************************************
 * HX711 bit-bang stub
 ****************************************************************/

struct hx711_state {
    avr_t *avr;
    avr_irq_t *dout_irq;
    uint32_t counts;
    uint32_t period_us;
    uint8_t bits;
};

static avr_cycle_count_t
hx711_ready_timer(struct avr_t *avr, avr_cycle_count_t when, void *param)
{
    struct hx711_state *hx = param;
    (void)avr;
    (void)when;
    avr_raise_irq(hx->dout_irq, 0);   // DOUT low: conversion ready
    return 0;
}

static void
hx711_sclk_hook(struct avr_irq_t *irq, uint32_t value, void *param)
{
    struct hx711_state *hx = param;
    (void)irq;
    if (!value)
        return;
    // Rising edge: the chip shifts out the next bit (MSB first).  Klipper
    // reads DOUT after pulling SCLK low again, so setting it here is in
    // time.  Pulse 25 is the gain/channel select and starts a conversion.
    if (hx->bits < 24) {
        uint32_t bit = (hx->counts >> (23 - hx->bits)) & 1;
        avr_raise_irq(hx->dout_irq, bit);
        hx->bits++;
    } else {
        hx->bits = 0;
        avr_raise_irq(hx->dout_irq, 1);   // busy
        avr_cycle_timer_register_usec(hx->avr, hx->period_us,
                                      hx711_ready_timer, hx);
    }
}


/****************************************************************
 * Main
 ****************************************************************/

static void
usage(const char *prog)
{
    fprintf(stderr,
        "Usage: %s -m <mcu> -f <freq> -e <firmware.elf> -u <pty-symlink>\n"
        "          [--adc ch=millivolts]... [--hx711 dout=N,sclk=N,counts=N,rate=N]\n"
        "          [--chunk-us N] [--gdb [port]] [--quiet]\n"
        "  -m  atmega16 | atmega32 | atmega1284p\n"
        "  -e  ELF (preferred, keeps symbols) or .hex\n"
        "  -u  stable path for the other end of the emulated USART0\n"
        "  --chunk-us  pacing quantum in microseconds (default 250)\n",
        prog);
}

static int
parse_u32(const char *s, uint32_t *out)
{
    char *end = NULL;
    unsigned long v = strtoul(s, &end, 0);
    if (!end || *end) return -1;
    *out = (uint32_t)v;
    return 0;
}

int
main(int argc, char **argv)
{
    const char *mcu = NULL, *fw = NULL, *pty_path = NULL;
    uint32_t freq = 16000000;
    uint32_t chunk_us = 250;
    int gdb = 0, quiet = 0, trace_uart = 0, log_level = 0;
    double speed = 1.0;
    struct adc_state adc = {0};
    struct hx711_state hx = {0};
    int have_hx = 0;

    enum { OPT_ADC = 1000, OPT_HX711, OPT_CHUNK, OPT_GDB, OPT_QUIET,
           OPT_TRACE_UART, OPT_LOG, OPT_SPEED };
    static struct option longopts[] = {
        { "adc",     required_argument, NULL, OPT_ADC },
        { "hx711",   required_argument, NULL, OPT_HX711 },
        { "chunk-us",required_argument, NULL, OPT_CHUNK },
        { "gdb",     optional_argument, NULL, OPT_GDB },
        { "quiet",   no_argument,       NULL, OPT_QUIET },
        { "trace-uart", no_argument,    NULL, OPT_TRACE_UART },
        { "log",     required_argument, NULL, OPT_LOG },
        { "speed",   required_argument, NULL, OPT_SPEED },
        { NULL, 0, NULL, 0 }
    };
    int c;
    while ((c = getopt_long(argc, argv, "m:f:e:u:h", longopts, NULL)) != -1) {
        switch (c) {
        case 'm': mcu = optarg; break;
        case 'f': if (parse_u32(optarg, &freq)) { usage(argv[0]); return 2; } break;
        case 'e': fw = optarg; break;
        case 'u': pty_path = optarg; break;
        case 'h': usage(argv[0]); return 0;
        case OPT_ADC: {
            char buf[64];
            snprintf(buf, sizeof(buf), "%s", optarg);
            char *eq = strchr(buf, '=');
            if (!eq) { usage(argv[0]); return 2; }
            *eq = 0;
            uint32_t ch, mv;
            if (parse_u32(buf, &ch) || parse_u32(eq + 1, &mv) || ch > 7) {
                usage(argv[0]);
                return 2;
            }
            adc.mv[ch] = mv;
            adc.set_mask |= 1 << ch;
            break;
        }
        case OPT_HX711: {
            char buf[128];
            snprintf(buf, sizeof(buf), "%s", optarg);
            have_hx = 1;
            hx.counts = 100000;
            hx.period_us = 12500;
            char *save = NULL;
            for (char *tok = strtok_r(buf, ",", &save); tok;
                 tok = strtok_r(NULL, ",", &save)) {
                char *eq = strchr(tok, '=');
                if (!eq) continue;
                *eq = 0;
                uint32_t v;
                if (parse_u32(eq + 1, &v)) continue;
                if (!strcmp(tok, "counts")) hx.counts = v & 0xffffff;
                else if (!strcmp(tok, "rate") && v)
                    hx.period_us = 1000000 / v;
            }
            break;
        }
        case OPT_CHUNK: if (parse_u32(optarg, &chunk_us)) { usage(argv[0]); return 2; } break;
        case OPT_GDB:
            gdb = optarg ? atoi(optarg) : 1234;
            if (!gdb) gdb = 1234;
            break;
        case OPT_QUIET: quiet = 1; break;
        case OPT_TRACE_UART: trace_uart = 1; break;
        case OPT_LOG: log_level = atoi(optarg); break;
        case OPT_SPEED: speed = atof(optarg); break;
        default: usage(argv[0]); return 2;
        }
    }
    if (!mcu || !fw || !pty_path) {
        usage(argv[0]);
        return 2;
    }
    if (chunk_us < 20) chunk_us = 20;
    if (speed < 0.1) speed = 0.1;
    if (speed > 100.0) speed = 100.0;

    elf_firmware_t f;
    memset(&f, 0, sizeof(f));
    if (elf_read_firmware(fw, &f) != 0) {
        fprintf(stderr, "maestro_sim: cannot read firmware %s\n", fw);
        return 1;
    }
    avr_t *avr = avr_make_mcu_by_name(mcu);
    if (!avr) {
        fprintf(stderr, "maestro_sim: unknown mcu '%s'\n", mcu);
        return 1;
    }
    avr_init(avr);
    avr->frequency = freq;
    if (log_level)
        avr->log = log_level;
    avr_load_firmware(avr, &f);

    // USART0 <-> PTY.  uart_pty picks its own /dev/pts/N; publish it under
    // the name the bus hub (or Klipper) expects.
    if (trace_uart) {
        avr_irq_t *uin = avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ('0'),
                                       UART_IRQ_INPUT);
        avr_irq_t *uout = avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ('0'),
                                        UART_IRQ_OUTPUT);
        avr_irq_t *xon = avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ('0'),
                                       UART_IRQ_OUT_XON);
        avr_irq_t *xoff = avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ('0'),
                                        UART_IRQ_OUT_XOFF);
        avr_irq_register_notify(uin, uart_trace_in, NULL);
        avr_irq_register_notify(uout, uart_trace_out, NULL);
        avr_irq_register_notify(xon, uart_trace_flow, NULL);
        avr_irq_register_notify(xoff, uart_trace_flow, NULL);
    }

    uart_pty_t uart;
    uart_pty_init(avr, &uart);
    uart_pty_connect(&uart, '0');
    unlink(pty_path);
    if (symlink(uart.pty.slavename, pty_path) != 0) {
        fprintf(stderr, "maestro_sim: symlink %s -> %s: %s\n", pty_path,
                uart.pty.slavename, strerror(errno));
        return 1;
    }

    // ADC: answer every conversion trigger with the configured millivolts.
    adc.avr = avr;
    if (adc.set_mask) {
        avr_irq_t *trig = avr_io_getirq(avr, AVR_IOCTL_ADC_GETIRQ,
                                        ADC_IRQ_OUT_TRIGGER);
        avr_irq_register_notify(trig, adc_trigger_hook, &adc);
    }

    // HX711 on two port D pins.
    if (have_hx) {
        hx.avr = avr;
        hx.dout_irq = avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('D'), 3);
        avr_irq_t *sclk = avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('D'), 6);
        avr_irq_register_notify(sclk, hx711_sclk_hook, &hx);
        avr_raise_irq(hx.dout_irq, 0);   // data ready from power-up
    }

    if (gdb) {
        avr->gdb_port = gdb;
        avr->state = cpu_Stopped;
        extern void avr_gdb_init(avr_t *avr);
        avr_gdb_init(avr);
    }

    // No SA_RESTART: uart_pty_stop() pokes its reader thread with SIGINT
    // and relies on select() returning EINTR to make that thread exit.
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    if (!quiet) {
        fprintf(stderr, "maestro_sim: %s %s at %u Hz, uart %s (%s), chunk %u us\n",
                mcu, fw, freq, pty_path, uart.pty.slavename, chunk_us);
        fflush(stderr);
    }

    // Real-time pacing.  Keep the AVR cycle count equal to wall time; the
    // loop only sleeps when the emulator is ahead of the clock.
    uint64_t start_ns = now_ns();
    uint64_t t0_cycles = avr->cycle;
    avr_cycle_count_t next_check = avr->cycle + (freq / 1000000ull) * chunk_us;
    while (!stop_requested && avr->state != cpu_Done
           && avr->state != cpu_Crashed) {
        avr_run(avr);
        if (avr->cycle < next_check)
            continue;
        next_check = avr->cycle + (freq / 1000000ull) * chunk_us;
        uint64_t done = avr->cycle - t0_cycles;
        uint64_t deadline = start_ns
            + (uint64_t)((double)cycles_to_ns(done, freq) / speed);
        uint64_t now = now_ns();
        if (now < deadline) {
            uint64_t remain = deadline - now;
            if (remain > 300000ull) {           // > 300 us: sleep
                struct timespec ts = {
                    .tv_sec = remain / 1000000000ull,
                    .tv_nsec = remain % 1000000000ull,
                };
                while (clock_nanosleep(CLOCK_MONOTONIC, 0, &ts, &ts) == EINTR
                       && !stop_requested)
                    ;
            } else {
                while (now_ns() < deadline && !stop_requested)
                    ;
            }
        }
    }

    if (!quiet)
        fprintf(stderr, "maestro_sim: exit state=%d cycle=%" PRIu64 "\n",
                avr->state, avr->cycle);
    unlink(pty_path);
    // uart_pty_stop() only pokes its reader thread once with SIGINT and
    // then joins; if that signal lands between select() calls the thread
    // never sees it and the join hangs.  We are powering down anyway.
    _exit(0);
}
