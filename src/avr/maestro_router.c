// Maestro Grand 2 Hardware Router for ATmega1284p
// Routes packets between Host (USART0) and Peripheral Bus (USART1 -> MCP2561)
//
// Part of Klipper port for Maestro Grand 2 IDEX

#include <avr/io.h>
#include <avr/interrupt.h>
#include "autoconf.h"
#include "board/serial_irq.h"
#include "command.h"
#include "sched.h"
#include "maestro_router.h"

#if CONFIG_MAESTRO_ROUTER

DECL_CONSTANT_STR("RESERVE_PINS_maestro_router", "PD2,PD3");

#define ROUTER_BUF_SIZE 256
#define ROUTER_BUF_MASK (ROUTER_BUF_SIZE - 1)
#define ROUTER_BUF_USABLE (ROUTER_BUF_SIZE - 1)

struct router_fifo {
    uint8_t buf[ROUTER_BUF_SIZE];
    volatile uint8_t head;
    volatile uint8_t tail;
};

// USART0 (Host) -> USART1 (Peripherals MCP2561)
static struct router_fifo fifo_u0_to_u1;

// USART1 (Peripherals MCP2561) -> USART0 (Host)
static struct router_fifo fifo_u1_to_u0;

static inline int
fifo_put(struct router_fifo *f, uint8_t b)
{
    uint8_t h = f->head;
    if ((uint8_t)(h - f->tail) >= ROUTER_BUF_USABLE)
        return -1; // FIFO full, drop byte
    f->buf[h & ROUTER_BUF_MASK] = b;
    f->head = h + 1;
    return 0;
}

static inline int
fifo_get(struct router_fifo *f, uint8_t *pb)
{
    uint8_t t = f->tail;
    if (t == f->head)
        return -1;
    *pb = f->buf[t & ROUTER_BUF_MASK];
    f->tail = t + 1;
    return 0;
}

// Check if there is data from peripherals waiting to be sent to Host
int
maestro_router_has_tx0(void)
{
    return fifo_u1_to_u0.head != fifo_u1_to_u0.tail;
}

// Called from ISR(USART0_UDRE_vect) to check if there is data from peripherals to send to Host
int
maestro_router_get_tx0(uint8_t *pdata)
{
    return fifo_get(&fifo_u1_to_u0, pdata);
}

// Put byte to USART1 TX FIFO without triggering transmission until frame is complete
static void
router_tx1_put_byte(uint8_t data)
{
    fifo_put(&fifo_u0_to_u1, data);
}

// Start transmission of fully received frame on USART1 to MCP2561 bus
static void
router_tx1_start(void)
{
    // Suppress MCP2561 loopback echo by disabling USART1 receiver during TX
    UCSR1B &= ~(1 << RXEN1);
    UCSR1B |= (1 << UDRIE1);
}

// State machine for USART0 RX: Demultiplexes Host packets
enum {
    STATE_IDLE_LEN = 0,
    STATE_WAIT_SEQ = 1,
    STATE_PAYLOAD  = 2,
};

static uint8_t rx_state;
static uint8_t frame_len;
static uint8_t frame_cnt;
static uint8_t target_local;

// Called from ISR(USART0_RX_vect) on each byte received from Host
void
maestro_router_rx0(uint8_t data)
{
    switch (rx_state) {
    case STATE_IDLE_LEN:
        if (data == MESSAGE_SYNC) {
            // Inter-frame sync, pass to local Klipper to keep tasks awake
            serial_rx_byte(data);
            return;
        }
        if (data >= MESSAGE_MIN && data <= MESSAGE_MAX) {
            frame_len = data;
            frame_cnt = 1;
            rx_state = STATE_WAIT_SEQ;
        }
        return;

    case STATE_WAIT_SEQ: {
        uint8_t dest = data & ~MESSAGE_SEQ_MASK;
        frame_cnt = 2;
        if (dest == 0x10) {
            // Local Main MCU (ATmega1284p)
            target_local = 1;
            serial_rx_byte(frame_len);
            serial_rx_byte(data);
            rx_state = STATE_PAYLOAD;
        } else if (dest == 0x20 || dest == 0x30 || dest == 0x40) {
            // Peripheral board (0x20 Head0, 0x30 Head1, 0x40 Bed)
            target_local = 0;
            router_tx1_put_byte(frame_len);
            router_tx1_put_byte(data);
            rx_state = STATE_PAYLOAD;
        } else {
            // Invalid destination ID - drop false frame and resync
            rx_state = STATE_IDLE_LEN;
        }
        return;
    }

    case STATE_PAYLOAD:
        // Do NOT abort on MESSAGE_SYNC (0x7E) inside payload/CRC!
        // 0x7E is a valid data byte in Klipper protocol VLQ and CRC16.
        if (target_local) {
            serial_rx_byte(data);
        } else {
            router_tx1_put_byte(data);
        }
        frame_cnt++;
        if (frame_cnt >= frame_len) {
            // Start transmitting full frame to peripheral bus only when completely buffered
            if (!target_local)
                router_tx1_start();
            rx_state = STATE_IDLE_LEN;
        }
        return;
    }
}

// USART1 RX interrupt: bytes received from peripheral MCP2561 bus
ISR(USART1_RX_vect)
{
    uint8_t data = UDR1;
    fifo_put(&fifo_u1_to_u0, data);
    // Enable USART0 TX interrupt to forward byte to Host
    serial_enable_tx_irq();
}

// USART1 UDRE interrupt: transmit buffer to peripheral MCP2561 bus empty
ISR(USART1_UDRE_vect)
{
    uint8_t data;
    int ret = fifo_get(&fifo_u0_to_u1, &data);
    if (ret) {
        // Buffer empty: disable UDRE and enable TX Complete (TXC) interrupt
        // to re-enable RXEN1 as soon as the final stop bit leaves the pin.
        UCSR1B &= ~(1 << UDRIE1);
        UCSR1A |= (1 << TXC1); // Clear flag by writing 1
        UCSR1B |= (1 << TXCIE1);
    } else {
        UDR1 = data;
    }
}

// USART1 TX Complete interrupt: frame finished transmitting on MCP2561 bus
#if defined(USART1_TX_vect)
ISR(USART1_TX_vect)
#else
ISR(USART1_TXC_vect)
#endif
{
    UCSR1B &= ~(1 << TXCIE1);
    uint8_t dummy = UDR1;
    (void)dummy;
    // Re-enable USART1 receiver now that line is free
    UCSR1B |= (1 << RXEN1);
}

// Initialize USART1 for MCP2561 communication at 250000 baud
void
maestro_router_init(void)
{
    UCSR1A = (1 << U2X1);
    uint32_t cm = 8;
    UBRR1 = DIV_ROUND_CLOSEST(CONFIG_CLOCK_FREQ, cm * CONFIG_SERIAL_BAUD) - 1UL;
    UCSR1C = (1 << UCSZ11) | (1 << UCSZ10);
    UCSR1B = (1 << RXEN1) | (1 << TXEN1) | (1 << RXCIE1);
}
DECL_INIT(maestro_router_init);

#endif // CONFIG_MAESTRO_ROUTER
