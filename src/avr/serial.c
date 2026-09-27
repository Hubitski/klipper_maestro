// AVR serial port code.
//
// Copyright (C) 2016-2018  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <avr/interrupt.h> // USART_RX_vect
#include "autoconf.h" // CONFIG_SERIAL_BAUD
#include "board/misc.h" // timer_read_time, timer_from_us
#include "board/serial_irq.h" // serial_rx_byte
#include "command.h" // DECL_CONSTANT_STR
#include "sched.h" // DECL_INIT
#include "maestro_router.h"

// Reserve serial pins
#if CONFIG_SERIAL_PORT == 0
 #if CONFIG_MACH_atmega1280 || CONFIG_MACH_atmega2560
DECL_CONSTANT_STR("RESERVE_PINS_serial", "PE0,PE1");
 #else
DECL_CONSTANT_STR("RESERVE_PINS_serial", "PD0,PD1");
 #endif
#elif CONFIG_SERIAL_PORT == 1
DECL_CONSTANT_STR("RESERVE_PINS_serial", "PD2,PD3");
#elif CONFIG_SERIAL_PORT == 2
DECL_CONSTANT_STR("RESERVE_PINS_serial", "PH0,PH1");
#else
DECL_CONSTANT_STR("RESERVE_PINS_serial", "PJ0,PJ1");
#endif

// Helper macros for defining serial port aliases
#if CONFIG_MACH_atmega16
#define UCSRxA UCSRA
#define UCSRxB UCSRB
#define UCSRxC UCSRC
#define UBRRx UBRRL
#define UDRx UDR
#define UCSZx1 UCSZ1
#define UCSZx0 UCSZ0
#define U2Xx U2X
#define RXENx RXEN
#define TXENx TXEN
#define RXCIEx RXCIE
#define UDRIEx UDRIE
#define TXCx TXC
#define TXCIEx TXCIE
#else
#define AVR_SERIAL_REG1(prefix, id, suffix) prefix ## id ## suffix
#define AVR_SERIAL_REG(prefix, id, suffix) AVR_SERIAL_REG1(prefix, id, suffix)

// Serial port register aliases
#define UCSRxA AVR_SERIAL_REG(UCSR, CONFIG_SERIAL_PORT, A)
#define UCSRxB AVR_SERIAL_REG(UCSR, CONFIG_SERIAL_PORT, B)
#define UCSRxC AVR_SERIAL_REG(UCSR, CONFIG_SERIAL_PORT, C)
#define UBRRx AVR_SERIAL_REG(UBRR, CONFIG_SERIAL_PORT,)
#define UDRx AVR_SERIAL_REG(UDR, CONFIG_SERIAL_PORT,)
#define UCSZx1 AVR_SERIAL_REG(UCSZ, CONFIG_SERIAL_PORT, 1)
#define UCSZx0 AVR_SERIAL_REG(UCSZ, CONFIG_SERIAL_PORT, 0)
#define U2Xx AVR_SERIAL_REG(U2X, CONFIG_SERIAL_PORT,)
#define RXENx AVR_SERIAL_REG(RXEN, CONFIG_SERIAL_PORT,)
#define TXENx AVR_SERIAL_REG(TXEN, CONFIG_SERIAL_PORT,)
#define RXCIEx AVR_SERIAL_REG(RXCIE, CONFIG_SERIAL_PORT,)
#define UDRIEx AVR_SERIAL_REG(UDRIE, CONFIG_SERIAL_PORT,)
#define TXCx AVR_SERIAL_REG(TXC, CONFIG_SERIAL_PORT,)
#define TXCIEx AVR_SERIAL_REG(TXCIE, CONFIG_SERIAL_PORT,)
#endif

#if defined(USART_RX_vect)
// The atmega168 / atmega328 doesn't have an ID in the irq names
#define USARTx_RX_vect USART_RX_vect
#define USARTx_UDRE_vect USART_UDRE_vect
#elif defined(USART_RXC_vect)
// ATmega16 / ATmega32 use USART_RXC_vect and USART_UDRE_vect
#define USARTx_RX_vect USART_RXC_vect
#define USARTx_UDRE_vect USART_UDRE_vect
#else
#define USARTx_RX_vect AVR_SERIAL_REG(USART, CONFIG_SERIAL_PORT, _RX_vect)
#define USARTx_UDRE_vect AVR_SERIAL_REG(USART, CONFIG_SERIAL_PORT, _UDRE_vect)
#endif

#if defined(USART_TX_vect)
#define USARTx_TX_vect USART_TX_vect
#elif defined(USART_TXC_vect)
#define USARTx_TX_vect USART_TXC_vect
#elif defined(USART0_TX_vect)
#define USARTx_TX_vect AVR_SERIAL_REG(USART, CONFIG_SERIAL_PORT, _TX_vect)
#else
#define USARTx_TX_vect AVR_SERIAL_REG(USART, CONFIG_SERIAL_PORT, _TXC_vect)
#endif

void
serial_init(void)
{
    UCSRxA = CONFIG_SERIAL_BAUD_U2X ? (1<<U2Xx) : 0;
    uint32_t cm = CONFIG_SERIAL_BAUD_U2X ? 8 : 16;
#if CONFIG_MACH_atmega16
    uint16_t bval = DIV_ROUND_CLOSEST(CONFIG_CLOCK_FREQ, cm * CONFIG_SERIAL_BAUD) - 1UL;
    UBRRH = (bval >> 8) & 0x0F; // bit 7 (URSEL) is 0 to select UBRRH
    UBRRL = bval & 0xFF;
    UCSRxC = (1<<URSEL) | (1<<UCSZx1) | (1<<UCSZx0);
#else
    UBRRx = DIV_ROUND_CLOSEST(CONFIG_CLOCK_FREQ, cm * CONFIG_SERIAL_BAUD) - 1UL;
#if defined(URSEL)
    UCSRxC = (1<<URSEL) | (1<<UCSZx1) | (1<<UCSZx0);
#else
    UCSRxC = (1<<UCSZx1) | (1<<UCSZx0);
#endif
#endif
#if CONFIG_SERIAL_POLLED_SLAVE
    // In polled slave mode, do not enable UDRIE until a response packet is ready
    UCSRxB = (1<<RXENx) | (1<<TXENx) | (1<<RXCIEx);
#else
    UCSRxB = (1<<RXENx) | (1<<TXENx) | (1<<RXCIEx) | (1<<UDRIEx);
#endif
}
DECL_INIT(serial_init);

// Rx interrupt - data available to be read.
ISR(USARTx_RX_vect)
{
#if CONFIG_MAESTRO_ROUTER
    maestro_router_rx0(UDRx);
#else
    serial_rx_byte(UDRx);
#endif
}

// Tx interrupt - data can be written to serial.
ISR(USARTx_UDRE_vect)
{
#if CONFIG_MAESTRO_ROUTER
    static uint8_t tx0_owner; // 0=none, 1=local, 2=router
    static uint8_t tx0_remaining; // number of bytes remaining in current frame
    static uint32_t tx0_last_transit_time; // timestamp of last transit byte sent
    uint8_t data;

    if (tx0_owner == 2) {
        // Router currently owns USART0 TX until peripheral frame is completely sent
        if (maestro_router_get_tx0(&data) == 0) {
            UDRx = data;
            tx0_last_transit_time = timer_read_time();
            if (tx0_remaining > 0)
                tx0_remaining--;
            if (tx0_remaining == 0)
                tx0_owner = 0;
            return;
        }
        // Check if peripheral transit transmission stalled (> 5ms without data)
        // This prevents permanent deadlock if a peripheral frame was truncated or corrupted
        uint32_t now = timer_read_time();
        if (now - tx0_last_transit_time > timer_from_us(5000)) {
            tx0_owner = 0;
            tx0_remaining = 0;
        } else {
            // FIFO temporarily empty mid-frame, wait for next byte from USART1
            UCSRxB &= ~(1 << UDRIEx);
            return;
        }
    }

    if (tx0_owner == 1) {
        // Local Klipper currently owns USART0 TX until its frame finishes
        int ret = serial_get_tx_byte(&data);
        if (ret == 0) {
            UDRx = data;
            if (tx0_remaining > 0)
                tx0_remaining--;
            if (tx0_remaining == 0)
                tx0_owner = 0;
            return;
        }
        tx0_owner = 0;
    }

    // Transmitter is idle: choose next frame (transit router has priority)
    if (maestro_router_has_tx0()) {
        if (maestro_router_get_tx0(&data) == 0) {
            if (data >= MESSAGE_MIN && data <= MESSAGE_MAX) {
                tx0_owner = 2;
                tx0_remaining = data - 1;
                tx0_last_transit_time = timer_read_time();
            } else {
                tx0_owner = 0;
            }
            UDRx = data;
            return;
        }
    }

    int ret = serial_get_tx_byte(&data);
    if (ret == 0) {
        if (data >= MESSAGE_MIN && data <= MESSAGE_MAX) {
            tx0_owner = 1;
            tx0_remaining = data - 1;
        } else {
            tx0_owner = 0;
        }
        UDRx = data;
        return;
    }

    // Nothing to transmit from either source
    UCSRxB &= ~(1 << UDRIEx);
#else
    uint8_t data;
    int ret = serial_get_tx_byte(&data);
    if (ret) {
        UCSRxB &= ~(1<<UDRIEx);
#if CONFIG_SERIAL_POLLED_SLAVE
        // All bytes written to UDR. Enable TX Complete interrupt to re-enable
        // receiver (RXEN) as soon as the final stop bit leaves the pin.
        UCSRxA |= (1 << TXCx);
        UCSRxB |= (1 << TXCIEx);
#endif
    } else {
        UDRx = data;
    }
#endif
}

#if CONFIG_SERIAL_POLLED_SLAVE
ISR(USARTx_TX_vect)
{
    UCSRxB &= ~(1 << TXCIEx);
    // Frame transmission finished; re-enable receiver on MCP2561 bus
    UCSRxB |= (1 << RXENx);
    // Flush any residual byte from receiver FIFO
    uint8_t dummy = UDRx;
    (void)dummy;
}
#endif

// Enable tx interrupts
void
serial_enable_tx_irq(void)
{
#if CONFIG_SERIAL_POLLED_SLAVE
    // Suppress MCP2561 loopback echo: disable receiver while slave transmits
    UCSRxB &= ~(1 << RXENx);
#endif
    UCSRxB |= 1<<UDRIEx;
}
