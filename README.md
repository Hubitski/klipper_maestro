# Klipper for Maestro Grand 2 IDEX 3D Printer

[![Klipper](docs/img/klipper-logo-small.png)](https://www.klipper3d.org/)

Custom fork of **Klipper** firmware providing complete support for the unique distributed 4-microcontroller architecture of the **Maestro Grand 2 IDEX** industrial 3D printer (400×400×400 mm build volume, independent dual extruders, nozzle strain-gauge Z leveling).

---

## 1. Hardware Architecture & Background

The Maestro Grand 2 IDEX printer uses a non-standard electronics topology:

1. **Host SBC (Orange Pi Zero):**
   - Runs Linux, Klippy, Moonraker, and KlipperScreen.
   - Physically wired **only to the motherboard** via USART0 (`/dev/ttyS0` or USB-UART).
   - Has **no direct physical connection** to the toolhead or heated bed boards.

2. **Motherboard (ATmega1284p):**
   - Controls motion axes: X, X1 (dual carriage), Y, Z (TMC2130 drivers via SPI).
   - **Crucial feature:** Contains **no heater MOSFETs, no thermistor inputs, and no extruder drivers**!
   - Features two hardware USART ports:
     - `USART0`: Communication with Host (Orange Pi).
     - `USART1`: Transceiver connection to the peripheral bus.

3. **Three Peripheral Boards (ATmega16A @ 16MHz):**
   - **Toolhead 0 (Left Carriage):** Extruder stepper, hotend heater, 100k NTC thermistor, part cooling fan, heatsink fan, Z strain-gauge sensor.
   - **Toolhead 1 (Right Carriage):** Extruder stepper, hotend heater, 100k NTC thermistor, cooling fans.
   - **Heated Bed:** Bed heater power MOSFET (24V/220V), bed thermistor.

4. **Multi-Drop Differential Bus (Microchip MCP2561):**
   - All 4 nodes are connected to a shared two-wire CANH/CANL bus.
   - **None of the microcontrollers have an integrated CAN controller!**
   - The MCP2561 transceivers are used exclusively as a physical layer (PHY) for half-duplex UART differential transmission.

---

## 2. Why Upstream Klipper Could Not Work Out-of-the-Box

* **Missing ATmega16/16A Support:** Upstream Klipper has configurations for ATmega168, 328P, 1284P, and 2560, but ATmega16A is absent. Its register naming, I/O space addresses (multiplexed `UBRRH`/`UCSRC`), timer registers, and interrupt vectors differ significantly.
* **Point-to-Point Protocol Assumptions:** Klipper serial communication assumes a dedicated, point-to-point link. Placing multiple boards on a single half-duplex multi-drop line causes simultaneous transmission, packet collisions, CRC corruption, and emergency halts.
* **MCP2561 Hardware Loopback Echo:** CAN transceivers lack automatic echo cancellation: every byte transmitted on TXD is immediately reflected onto RXD.
* **JTAG Hardware Pin Locking:** ATmega16A chips ship with the JTAG fuse enabled by default, locking pins `PC2..PC5` (Port C) and preventing their use as standard GPIOs.

---

## 3. What Was Implemented & Fixed

### 1. Porting Klipper Core to ATmega16A (`src/avr/`)
* **Processor Configuration:** Added `MACH_atmega16` to `src/avr/Kconfig` with automatic selection of `HAVE_LIMITED_CODE_SIZE` (compilation with `-Os`, compact queues). Firmware uses only ~9.2 KB Flash (out of 16 KB) and ~490 bytes SRAM (out of 1 KB).
* **UART Subsystem (`src/avr/serial.c`):**
  - Mapped unindexed registers (`UCSRA`, `UCSRB`, `UCSRC`, `UBRRL`, `UBRRH`, `UDR`).
  - Implemented explicit split write for `UBRRH` (bit 7 `URSEL = 0`) and `UCSRC` (bit 7 `URSEL = 1`), solving the shared `$20` I/O address conflict.
  - Linked correct `avr-libc` interrupt vectors: `USART_RXC_vect` (Vector 11), `USART_UDRE_vect` (Vector 12), `USART_TXC_vect` (Vector 13).
* **Register Aliases & Compatibility:**
  - Timer 1: Aliased `TIMSK1 -> TIMSK` and `TIFR1 -> TIFR` (`src/avr/timer.c`).
  - ADC: Routed channels to `PORTA` (PA0..PA7) and isolated nonexistent `DIDR0` register under `#if defined(DIDR0)` (`src/avr/adc.c`).
  - Watchdog: Added `MCUSR -> MCUCSR` alias for ATmega16A (`src/avr/watchdog.c`).
  - Clock Prescaler: Guarded `CLKPR` access with `#if defined(CLKPR)`.
* **JTAG Port C Release:** Added automatic software JTAG disable sequence (`JTD` bit written twice to `MCUCSR` in `.init3`), unlocking `PC2..PC5` for general GPIO usage.

### 2. MCP2561 Hardware Echo Cancellation
* When a slave begins transmitting, the receiver is automatically disabled (`RXEN = 0`).
* Transmission completion is tracked via the Transmit Complete interrupt (`TXCIE = 1`).
* In the `USART_TXC_vect` ISR (after the final stop bit leaves the pin), the receiver is safely re-enabled (`RXEN = 1`) and the receive buffer is flushed.

### 3. Motherboard Hardware Gateway Router (`src/avr/maestro_router.c`)
* The ATmega1284p motherboard operates both hardware USARTs at 250,000 baud:
  - `USART0`: Host link (Orange Pi).
  - `USART1`: Peripheral bus link (MCP2561).
* A streaming state machine inspects packet headers:
  - Packets with `DEST == 0x10` are dispatched locally to the motherboard Klipper instance.
  - Packets with `DEST` equal to `0x20` (Head 0), `0x30` (Head 1), or `0x40` (Bed) are buffered in a 255-byte circular FIFO and transmitted monolithically to USART1.
  - Responses from the peripheral bus are atomically forwarded to USART0 toward the host.
* Includes a 5ms watchdog timer preventing motherboard transmit locks if a transit byte is dropped.

### 4. Strict Polled Multi-Drop Protocol (`src/command.c`, `src/generic/serial_irq.c`)
* Added `CONFIG_SERIAL_NODE_ID` and `CONFIG_SERIAL_POLLED_SLAVE` configuration options.
* Node Addressing:
  - `0x10`: Motherboard (Main MCU)
  - `0x20`: Left Carriage (Toolhead 0)
  - `0x30`: Right Carriage (Toolhead 1)
  - `0x40`: Heated Bed
* Slaves silently discard packets addressed to other nodes (`silent drop` without NAK) and transmit data **only** in direct response to host polls.

### 5. Host PTY Multiplexer (`scripts/maestro/host_multiplexer.py`)
* High-performance asynchronous Python daemon running on the Orange Pi.
* Creates 4 virtual pseudo-terminals (PTYs):
  - `/tmp/klipper_mcu_main`
  - `/tmp/klipper_mcu_head0`
  - `/tmp/klipper_mcu_head1`
  - `/tmp/klipper_mcu_bed`
* **Half-Duplex Bus Arbiter:** Serializes peripheral commands on a strict "request-response" basis. The bus remains busy until the slave finishes its response burst (including ADC reports and the terminating 5-byte ACK), preventing multi-packet collisions.
* Rewrites destination IDs and recalculates CCITT CRC-16 on the fly with table optimization (< 2 µs latency).
* For Klippy, each MCU appears as a standard, independent serial device.

---

## 4. Quick Start & Deployment Guide

### Step 1. Switch to this Repository on Orange Pi (via SSH)
```bash
# Stop running Klipper service
sudo systemctl stop klipper

# Switch repository origin
cd ~/klipper
git remote rename origin upstream 2>/dev/null || true
git remote add origin https://github.com/Hubitski/klipper_maestro.git
git fetch origin
git checkout master
git reset --hard origin/master

# Install pyserial dependency for custom 250000 baud support
~/klippy-env/bin/pip install pyserial
```

### Step 2. Enable and Start Multiplexer Service
```bash
sudo cp ~/klipper/scripts/maestro/klipper-multiplexer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now klipper-multiplexer.service
```
Verify that the virtual PTY devices are active:
```bash
ls -la /tmp/klipper_mcu_*
```
You should see: `klipper_mcu_main`, `klipper_mcu_head0`, `klipper_mcu_head1`, `klipper_mcu_bed`.

### Step 3. Build All 4 Firmware Binaries
On the Orange Pi (or any Linux host with `gcc-avr`, `binutils-avr`, `avr-libc` installed):
```bash
sudo apt-get update && sudo apt-get install -y gcc-avr binutils-avr avr-libc avrdude

cd ~/klipper
chmod +x scripts/maestro/build_all_mcus.sh
./scripts/maestro/build_all_mcus.sh
```
Compiled `.hex` files will be placed in `out/maestro_firmware/`:
* `klipper_mcu_motherboard_1284p.hex` (Flash to motherboard via USB/UART bootloader)
* `klipper_mcu_head0_atmega16.hex` (Flash to Toolhead 0 via ISP programmer)
* `klipper_mcu_head1_atmega16.hex` (Flash to Toolhead 1 via ISP programmer)
* `klipper_mcu_bed_atmega16.hex` (Flash to Heated Bed via ISP programmer)

*Note for ATmega16A ISP programming:* Ensure clock fuses are set for an **External 16MHz Crystal** (e.g., Low Fuse: `0xFF` or `0xEF`, High Fuse: `0xC9`).

### Step 4. Configure `printer.cfg`
In your Klipper printer configuration, declare each MCU using its respective virtual PTY path:
```ini
[mcu]
serial: /tmp/klipper_mcu_main
restart_method: command

[mcu head0]
serial: /tmp/klipper_mcu_head0
restart_method: command

[mcu head1]
serial: /tmp/klipper_mcu_head1
restart_method: command

[mcu bed]
serial: /tmp/klipper_mcu_bed
restart_method: command

# Emergency Power Cutoff & Startup Power Management for 24V PSU Relay
[host_power]
# Option A: Direct sysfs GPIO pin number on Orange Pi (e.g. 198)
# pin: 198
# Option B: Shell commands (if using gpioset, script, or wiringpi)
# on_cmd: /home/pi/scripts/psu_24v.sh on
# off_cmd: /home/pi/scripts/psu_24v.sh off
# Time to wait after turning on 24V PSU before Klipper connects to MCUs:
startup_delay: 1.5
# Cut 24V power instantly on thermal runaway / M112 emergency stop (fire prevention):
off_when_shutdown: True
```

Start Klipper:
```bash
sudo systemctl start klipper
```

---

## 5. Technical Documentation & Audit History

Comprehensive technical documentation and architectural records are located in `maestro_revision/`:
* `MAESTRO_DECISIONS_AND_CHANGELOG.md` — Complete Architecture Decision Records (ADR-001 through ADR-009) and changelog.
* `MAESTRO_GRAND2_KLIPPER_ARCHITECTURE.md` — Original hardware analysis and routing specification.
* `maestro_revision/revision_5.md` — Final hardware audit report verified against the official Microchip/Atmel ATmega16A datasheet (Doc 8154B) and cross-compiled with `avr-gcc 7.3.0`.
