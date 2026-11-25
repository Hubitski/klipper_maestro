# Maestro Grand 2 IDEX — состояние на 2026-11-25

> **Главный документ.** Заменяет `CURRENT_STATE_2026-11-22.md`.
> Новому чату читать: этот файл + `HANDOFF_2026-11-25.md`. Затем при необходимости
> `git log --oneline -15`, `scripts/maestro/host_multiplexer.py`.
> Не печатать целые `klippy.log`/`Stats`/tap: одна Stats-строка ≈1.5–2.5k токенов.
> Правила: `printer.cfg` составляет пользователь; сборка только на Pi; avrdude из GOO.

---

## 0. Где мы сейчас (кратко)

- Принтер в **shutdown**: `MCU 'bed' shutdown: Timer too close` **на конфиге**, даже с **одним** датчиком стола (PA4). Это подтверждает: причина — незакрытый `command_query_analog_in` (clock от хоста уже в прошлом), а не 4 канала ADC.
- На платах: head0 **16368**, head1 **16366** (full ADC clamp, HX711 3-sample, serial 64/64, **без boot-вентиляторов**, **без query-clamp**); bed **8848** (128/128 serial, stack 192, full clamp, **без query-clamp**); main **44252** (direct, router off).
- В репозитории: bed **8916** (query-clamp) **собирается**; head0/head1 **16416 — FAIL (32 Б over 16384)** (report-only clamp + `toolhead_fans_init` + query-clamp). Hex голов в `firmware/` — старые (16368/16366), успешной сборки новых не было.
- Mux (host-only) обновлён и протестирован: parser resync, ACK по seq, recovery guard, per-node quarantine, echo FIFO, session flush; **19 тестов OK**; затем `session_idle_time` 2→15 c и карантин «delay, not drop» (коммиты `8327cf7`, `2c73c2b`).
- Host-фиксы в Pi: `heaters.py` клампит поздний software-PWM; `hx71x.py SAMPLES_PER_BLOCK=3`; `bulk_sensor.py` `samples_per_block` + re-raise при shutdown; `mcu.py` reset 250 ms / stale config; `trigger_analog.py` 8 ms poll.
- `printer.cfg`: стол на **одном** `bed_ts1` (PA4, 10 кОм) через `temperature_combined`/`mean`; `bed_ts2/3/4` отключены; `[load_cell_probe]` **включён** (head0:PD3/PD6); `[stepper_z] endstop_pin: probe:z_virtual_endstop`, `position_endstop` закомментирован.
- Следующее: закрыть query-clamp для bed (8916), починить сборку голов (query-clamp только bed / trims), прошить bed+головы, `FIRMWARE_RESTART`, проверить bed на 1 датчике, затем тензо.

---

## 1. Железо и топология

- **Main**: ATmega1284p, 16 МГц, 128 КБ flash / 16 КБ SRAM. X, X1 (IDEX), Y, Z, TMC2130 по HW SPI, sensorless X/X1/Y. Z-концевик `^!PC7` — фиктивная заглушка; Z хомится тензо.
- **Головы head0/head1**: ATmega16A, 16 МГц, 16 КБ flash / 1 КБ SRAM. Экструдер (TMC2130), нагреватель PD7, термистор PA4, hotend fan PD4, part fan PD5, HX711 DOUT PD3 / SCLK PD6. Платы идентичны; на head1 120 Ом терминатор, на head0 PB3 на землю.
- **Bed**: ATmega16A, 16 МГц, 1 КБ SRAM. Нагреватели PD5/PD4/PD6/PD7 (4 зоны, `multi_pin`), термисторы PA4/PA5/PA6/PA7. Сейчас в конфиге один PA4 (`bed_ts1`); `bed_ts2/3/4` отключены. PA6 ранее читал −103.95 °C (пользователь подключил датчик; на 2026-11-25 он уже не −103).
- **Шина периферии**: полудуплексный UART 250000 8N1 через MCP2561 (CAN PHY) на `/dev/ttyS0` Pi. Адреса: head0 0x20, head1 0x30, bed 0x40. Main — **напрямую** на `/dev/ttyS1`, без mux и без роутера.
- **Роутер на материнке отключён** (`CONFIG_MAESTRO_ROUTER=n`); USART1 PD2/PD3 не включается, PD3 — вход со слабой подтяжкой (защита от доминанты трансивера).
- **Pi**: Orange Pi Zero, Armbian, WiFi xradio (не отключать), IP DHCP **192.168.10.23** (был .24). `/dev/ttyS0` — не консоль (`console=tty1/display`, `serial-getty@ttyS0` masked). Питание Pi общее с принтером: принтер выключен = Pi выключен.
- **Питание 24 В**: реле GPIO6; root-oneshot `maestro-gpio6.service` экспортирует GPIO6, out=1, chown klipper. `[host_power] pin: 6`, `active_low: False`, `off_when_shutdown: True`, `off_on_disconnect: False`. Реле **не** питает Pi (проверено). `M81` роняет MCU в shutdown; `M80` разрешён в shutdown.

### 1.1. Фьюзы

- head0: `lfuse 0xBF`, `hfuse 0xC7` (BOOTRST=1), lock `0xFF`.
- head1: `lfuse 0x3F`, `hfuse 0xD1` (BOOTRST исправлен с 0xD0), lock `0xFF`.
- bed: не читались; работает.
- Не трогать без необходимости.

---

## 2. Сервисы Pi и восстановление

- `klipper.service` — Klippy; `/home/klipper/klippy-env/bin/python`, конфиг `~/printer_data/config/printer.cfg`, лог `~/printer_data/logs/klippy.log`.
- `klipper-multiplexer.service` — `host_multiplexer.py`, только `--periph-port /dev/ttyS0` (main не трогает). Аргументы: `--bus-guard 0.001 --bus-timeout 0.020 --tap-log /home/klipper/printer_data/logs/maestro_bus.log`, poll 1 мс, hard watchdog 200 мс. PTY: `/tmp/klipper_mcu_{head0,head1,bed}`; `main` пропускается (нет `--main-port`).
- `klipper-mcu.service` — Linux MCU `[mcu host]`; после жёсткой аварии может быть в shutdown.
- `maestro-gpio6.service` — реле 24 В.
- Sudo/SSH: пользователь `klipper`, пароль `klipper` (и SSH, и sudo; в репозиторий не писать). На Windows: `SSH_ASKPASS=/tmp/maestro_askpass.sh SSH_ASKPASS_REQUIRE=force ssh klipper@192.168.10.23`; helper `/tmp/maestro_askpass.sh` печатает пароль.
- **Восстановление после shutdown host/MCU**:
  1. `sudo systemctl stop klipper`
  2. `sudo systemctl restart klipper-mcu`
  3. при необходимости `echo 1 > /sys/class/gpio/gpio6/value`
  4. `sudo systemctl start klipper`
  Ошибка `Can not update MCU 'host' config as it is shutdown` — именно этот случай.
- **Рестарт после правок host-кода**: `systemctl restart klipper-multiplexer`, затем `klipper-mcu`/`klipper`. Команда `RESTART` в Klippy **не** перезагружает Python-модули; нужен рестарт сервиса.

---

## 3. Прошивки: что на платах и что в репозитории

| Плата | На плате (2026-11-25) | В репозитории |
|---|---|---|
| main 1284p | 44252, direct, router off | 44252 (собирается) |
| head0 | **16368** (full clamp, HX711 3-sample, serial 64/64, **без boot-fans, без query-clamp**) | **16416 FAIL** (report-only + `toolhead_fans_init` + query-clamp; 32 Б over) |
| head1 | **16366** (то же) | **16416 FAIL** |
| bed | **8848** (128/128, stack 192, full clamp, **без query-clamp**) | **8916** (query-clamp; собирается) |

- `avr-size out/klipper_diag.elf` после последней неудачной сборки head1: `text 16374, data 42, bss 185` → flash **16416** (16384 + 32).
- Hex голов в `~/klipper/firmware/` — от последней **успешной** сборки (16368/16366); текущая сборка голов падает, новые hex не создаются.
- Hex bed 8916 в `firmware/` есть (последняя сборка bed после query-clamp).
- Локальная копия hex: `C:\Users\user\Downloads\maestro_firmware_2026-11-22\` (обновлять scp с Pi; имена `klipper_mcu_*.hex`).
- Прошивка: `C:\Users\user\Desktop\GOO (НЕ ТРОГАТЬ)\GRANDO\AVRDUDE\avrdude.exe -C avrdude.conf -c avrisp -P COM6 -b 19200 -p m16 -U flash:w:<hex>:i -U flash:v:<hex>:i`. Для main `-p m1284p`. COM проверять (`Get-PnpDevice -Class Ports`); последний — COM6. Принтер при прошивке выключен.

### 3.1. Почему головы 16416 и как чинить

Текущие head-профили: `CONFIG_ADC_TIMER_CLAMP=y`, `CONFIG_ADC_TIMER_CLAMP_REPORT_ONLY=y`, `CONFIG_SENSOR_BULK_DATA_SIZE=12`, serial 64/64, `toolhead_fans_init()` в `src/avr/main.c`, query-clamp в `src/adccmds.c`. Сумма не влезает на 32 Б.

Варианты (по возрастанию риска):
1. **Query-clamp только для bed** через новый Kconfig (например `CONFIG_ADC_QUERY_CLAMP`, bed=y, head0/head1=n). Головы вернутся к ~16378 (fans + report-only, без query-clamp) — влезает. Bed остаётся 8916. Риск: на голове query-clock в прошлом теоретически возможен, но у головы один ADC и query идёт на старте, когда шина ещё не загружена; наблюдённый `Timer too close` у головы был software-PWM, а не query.
2. **Trims**: `trigger_analog` limited-поля/код, `console_sendf` atomic (осторожно: он же лечит IRQ-off), `basecmd`, строки, `-mcall-prologues` (риск для таймингов ISR). Не начинать с этого.
3. **Report-only вместо full clamp на головах** уже стоит; full clamp на голове был бы ещё +28 Б и точно не влезет с fans+query.
4. Не возвращать generic `CONFIG_INITIAL_PINS`: он стоит ~44 Б; `toolhead_fans_init()` (PD4 on, PD5 off) — эквивалент за ~14–20 Б.

---

## 4. `printer.cfg` (реальный, 2026-11-25)

Правило: файл пользователя; не править без явной просьбы. Бэкапы в `~/printer_data/config/printer.cfg.bak-*`:
`20261122-bed4`, `20261122-bed4test`, `20261122-extruder`, `20261122-head1`, `20261122-nohead0`, `20261122-noloadcell`, `20261122-pidtiming`, `20261122-print`, `20261122-loadcelltest`, `20261122-combinednames`, `20261125-bed1` (если создан).

- MCU: `[mcu] serial: /dev/ttyS1 baud: 250000 restart_method: command`; `[mcu host] /tmp/klipper_host_mcu`; `[mcu bed] /tmp/klipper_mcu_bed`; `[mcu head0] /tmp/klipper_mcu_head0`; `[mcu head1] /tmp/klipper_mcu_head1`; все `restart_method: command`.
- Extruder (head0): `gear_ratio: 25:1`, `rotation_distance: 32.6726`, `full_steps_per_rotation: 200`, microsteps 16, `max_extrude_only_distance: 200`, `report_time: 0.5`, `sample_count: 4`, `control: pid`, временные Kp 22.2 / Ki 1.08 / Kd 114 (калибровать).
- Extruder1 (head1): старый `rotation_distance: 7.71`, PID 4.405/0.133/36.507.
- **Bed (сейчас один датчик)**:
  ```
  [heater_bed]
  heater_pin: multi_pin:bed_heaters
  sensor_type: temperature_combined
  sensor_list: temperature_sensor bed_ts1
  combination_method: mean
  maximum_deviation: 5.0
  control: pid
  pid_Kp: 54.0
  pid_Ki: 0.77
  pid_Kd: 940.0
  max_power: 1.0
  min_temp: 0
  max_temp: 120
  ```
  `[temperature_sensor bed_ts1]` — PA4, `bed_ntc`, `pullup_resistor: 10000`, `sample_count: 1`, `report_time: 0.5`. `bed_ts2/3/4` отключены. `[multi_pin bed_heaters]` PD5/PD4/PD6/PD7.
- **Z / тензо**:
  ```
  [stepper_z]
  endstop_pin: probe:z_virtual_endstop
  #position_endstop: 0
  position_min: -2
  ...
  [load_cell_probe]
  sensor_type: hx711
  dout_pin: head0:PD3
  sclk_pin: head0:PD6
  sample_rate: 80
  gain: A-128
  z_offset: 0.0              # после PROBE_CALIBRATE
  counts_per_gram: 1000.0    # placeholder, LOAD_CELL_CALIBRATE
  reference_tare_counts: 0   # placeholder
  trigger_force: 75.0
  force_safety_limit: 2000
  speed: 2.0
  lift_speed: 5.0
  samples: 1
  ```
- `[homing_override] axes: xyz`: X/Y sensorless, затем `G1 X170 Y170 F6000` и `G28 Z` (тензо в центре).
- Прочее: `[force_move] enable_force_move: True`, TMC2130, `fan_generic part_fan_0/1`, `heater_fan hotend_fan_0/1`, `[host_power] pin: 6`, macros `T0/T1`, `LOAD_FILAMENT/UNLOAD_FILAMENT`, `[duplicate_pin_override] pins: PD4`.
- **4-датчиковый вариант** (когда понадобится): `sensor_list: temperature_sensor bed_ts1, temperature_sensor bed_ts2, temperature_sensor bed_ts3, temperature_sensor bed_ts4` + `mean`; `bed_ts1..4` — полные имена объектов (`temperature_sensor ...`), иначе `Unknown config object`. `maximum_deviation: 5.0` может быть тесным для зон; PA6 проверять. Бэкапы bed4/bed4test/combinednames.

---

## 5. Протокол и host-мультиплексор

### 5.1. Как устроено

- Klippy открывает main `/dev/ttyS1` **сам**. Периферия — только через mux: PTY `/tmp/klipper_mcu_*`, DEST rewrite + CRC16 в обе стороны, полудуплексный арбитраж.
- Slave строго polled: передаёт только после валидной команды; ACK/NAK — ровно 5 байт, ACK последним. Асинхронные отчёты копятся в TX-буфере и выталкиваются следующей командой.
- Буферы: toolhead TX/RX 64/64, bed 128/128 (нет stepper'ов, RAM свободна), main 192/96 (`serial_irq.c`, `#if CONFIG_WANT_STEPPER`).
- Tap: `/home/klipper/printer_data/logs/maestro_bus.log`; `REQ/RESP/ECHO/ACK`, `PARSE bad-len/dest/trailer/crc`, `STALL/RESYNC`, `PHY` (TIOCGICOUNT 80 Б), `quarantine`, `release: timeout`, `new session`.
- Unit-тесты: `~/klippy-env/bin/python scripts/maestro/test_multiplexer.py` — **19 OK**. Тесты гоняют реальные `_note_bus_packet`/`_release_bus`/`_dispatch_next_bus_packet`/парсер.

### 5.2. Что уже исправлено в mux

Коммиты `8327cf7` + `2c73c2b`:
- Парсер: при bad-len/dest/trailer/CRC прыжок к следующему `0x7e`, а не удаление 1 байта; `idle_clear` (~5–6 мс) без зависимости от tap.
- Release: ACK принимается только если `seq == (request_seq+1)&0xf`; данные после ACK **продлевают** guard, а не отменяют; пустой парсер больше не требуется.
- Timeout/hard-watchdog: очистка парсера + recovery guard 3 мс; не стартовать, пока `uart_tx_queue` не пуст.
- Per-node quarantine: 3 таймаута → 1 с; очередь узла **не дропается**, `_dispatch` обходит карантинных и отдаёт их после 1 с (`delay, not drop`); per-node cap 2; все запросы через очередь.
- Echo FIFO (8 кадров, окно 100 мс) переживает границу транзакции.
- Session idle 15 с (было 2 с — Klippy в shutdown опрашивает ~5 с, и mux каждые 5 с чистил очереди, включая reset); при новой сессии `tcflush` slave + очистка очередей.
- Tap: 64 КиБ буфер, `maybe_flush`, stall/resync — только диагностика.
- HUP: PTY не закрывается/не переopen (stable symlink); `reopen()` оставлен неиспользуемым.

### 5.3. Симптомы/факты из tap

- До фикса: **8739** `release: timeout`, ~8% транзакций, `STALL want=49 have=42`, `bad-len/bad-dest/bad-trailer` на 4-report burst bed.
- После фикса: `quarantine 0x40`, head0 больше не падает от голода; в последних 1000 строках **227 REQ / 140 ECHO / 349 RESP / 100 timeout** — PHY и echo живы, узлы отвечают; реальная ошибка bed `Timer too close`.
- `new session` каждые 5 с (до 15 с) — симптом того, что все MCU были в shutdown.
- PHY-счётчики (frame/overrun/parity/brk) — нули; проблема не в проводах/трансивере.

### 5.4. Остаточные риски mux/протокола

- RXEN/UDRIE/TXCIE в `serial_enable_tx_irq()` — RMW без irq_save; теоретически может оставить RXEN выключенным (редко).
- ACK не гарантированно последний кадр: IRQ-context `command_sendf` может добавить данные после ACK; mux теперь продлевает release, но firmware-индекс транзакции не сделан.
- `console_sendf` держит IRQ выключенными на весь encode+CRC. Для 19-байтного отчёта это ~30 мкс (на 250к меньше байта), для 58-байтного было ~86 мкс (2+ байта, overrun). 3-sample уменьшил, но atomic можно сузить.
- RX-буфер toolhead 64 = RECEIVE_WINDOW 64: нулевой запас; при retransmit-бурсте возможен overrun. Не снижать ниже 64.
- 1 Мбод физически возможен (ATmega16 U2X UBRR=1, MCP2561 1 Мбит), но **не лечит** 20-мс таймауты/парсер; при 1 Мбод IRQ-off 30 мкс = 3 байта, а RX 2-байтный → overrun хуже. Сначала узкий IRQ-off.
- HUP/reopen PTY не пересоздаётся (stable symlink); stale-очереди чистятся по session-idle/flush.

---

## 6. Firmware: ATmega16A, размеры, RAM

- Порт: `MACH_atmega16`; регистры/векторы `USART_RXC_vect`/`USART_UDRE_vect`/`USART_TXC_vect`; `UBRRH`/`UCSRC` URSEL; Timer1 `TIMSK/TIFR`; ADC PORTA + `#if DIDR0`; GPIO toggle XOR PORTx (на ATmega16A PINx read-only); `watchdog` MCUCSR/JTD; `dynmem_end = RAMEND+1-AVR_STACK_SIZE`.
- Flash/RAM: головы 16 КБ/1 КБ, стек 192; bed 1 КБ, стек 192 (был 160; bed без stepper'ов — move queue не нужен). Heap/stack не проверяются `check-avr-size.sh` — только data+bss.
- `-Wl,--relax`, `-Wl,--noinhibit-exec` (binutils 2.26 ложные overflow), `scripts/check-avr-size.sh`.
- `trigger_analog` под `HAVE_LIMITED_CODE_SIZE` урезан (нет monitor, только `abs_ge`); `sensor_bulk` data size 12 (3 сэмпла HX711) на головах; `SOSFilter` 4 секции (можно 2 = +56 Б heap).
- `command_query_analog_in` теперь клампит clock (`7d62451`); `analog_in_shutdown` re-arm **не** клампится (кандидат, если понадобится).
- `toolhead_fans_init()`: PD4 on, PD5 off при boot (эквивалент `INITIAL_PINS="PD4,!PD5"`), только `MACH_atmega16 && WANT_STEPPER`; bed не затрагивается. **В прошитые головы 16368/16366 не входит** — нужна успешная сборка.
- HX711: bit-bang SCLK PD6/DOUT PD3; 2 NOP + overhead `gpio_out_write` ~0.4–0.6 мкс; interrupts enabled; DESYNC-проверка убрана (некоторые модули не поднимают DOUT после 25-го импульса); power-down отключён (`HX71X_NO_POWERDOWN=1`).
- `console_sendf` atomic (irq_save) — лечит TX-гонку, но даёт IRQ-off окно (см. 5.4).
- Основные коммиты: `c6c9ee3` GPIO XOR; `eb0361b` HX711 без DESYNC; `26432ce/a0af827/2a77a21/0272c55` clamp; `24529cf` 3-sample; `86a0d45` buffers/PWM host clamp; `4ce94bf` fans init; `c9f3d52` report-only; `7d62451` query clamp; `8327cf7/2c73c2b` mux.

---

## 7. Причины и симптомы (что уже поняли)

### 7.1. PID extruder → `Timer too close`
- Наблюдение: `PID_CALIBRATE HEATER=extruder TARGET=200` → `MCU 'head0' shutdown: Timer too close`, head0 retransmits.
- Причина: **software PWM** `queue_digital_out`; `heaters.py` считает `pwm_time = read_time + pwm_delay`, `pwm_delay = report_time` (0.5 с); на polled-шине сэмпл приходит позже, clock уже в прошлом → `sched.c:94`.
- Фикс: host-clamp в `heaters.py::set_pwm` (`pwm_time = max(pwm_time, estimated_print_time(now)+0.1)`).
- ADC clamp — отдельная страховка; full clamp на головах был заменён на report-only, чтобы влезли fans/размеры; bed — full clamp.

### 7.2. Load cell → «всё висит»
- 58-байтные `sensor_bulk_data` (12 сэмплов) + 17-байтный `sensor_bulk_status` не влезали в 64 Б TX; `RetryAsyncCommand` блокировал reactor Klippy на **5 с** на каждом 50-мс status-запросе; триггер, завершавший блок, терял `trsync_state`.
- `UPDATE_INTERVAL 0.05` ничего не уменьшал — размер задаёт `SENSOR_BULK_DATA_SIZE`, не интервал.
- Фикс: 3 сэмпла (12 Б / ~19 Б на проводе) + `FixedFreqReader(samples_per_block=3)` (`hx71x.py SAMPLES_PER_BLOCK=3`); +39 Б heap; статус+ACK влезают; 2 отчёта + статус + ACK = 60 Б.
- Остаток: `load_cell_probe` ~286 Б heap → move queue head0 ~4; SOS 4→2 даст +56 Б; stack 192 не снижать без проверки.

### 7.3. Bed `Timer too close` на конфиге (сейчас)
- Даже с одним датчиком. `MCU 'bed' error during config: Timer too close`.
- Причина: `command_query_analog_in` передавал host-clock в `sched_add_timer()` без clamp; при задержанной доставке конфига clock уже в прошлом. Full/report clamp покрывал только `analog_in_event`.
- Фикс: `7d62451`, query-clamp под `CONFIG_ADC_TIMER_CLAMP`; bed 8916 собирается; **на плату ещё не залит**; головы с этим фиксом не влезают.
- После прошивки bed 8916 ожидается прохождение конфига; если `Timer too close` останется — смотреть `analog_in_shutdown` re-arm и `timer.c:205`.

### 7.4. Bed 4 датчика → `Lost communication`
- 64-байтный TX не вмещал 4×~15 Б `analog_in_state` + 5 Б ACK; ACK терялся → хост объявлял bed lost. Новые 128/128 + stack 192 это лечат.
- `bed_ts3` PA6 −103.95 °C — отдельный дохлый датчик/проводка, не причина shutdown; пользователь подключил, сейчас не −103.
- `temperature_combined`: `sensor_list` — **полные имена** `temperature_sensor bed_ts1` и т.д.; `combination_method` только `min/max/mean` (`average` невалиден); `maximum_deviation: 5.0` может быть тесно для зон.

### 7.5. head0 `Lost communication`
- До mux-фикса: bed таймаутил каждые 20 мс, глобальный FIFO mux не давал слотов head0 → head0 падал. Tap: `quarantine 0x40`, `release: timeout from 0x40/0x20`.
- После mux-фикса: head0 больше не голодает; реальная ошибка — bed `Timer too close`.
- Сам head0 в shutdown мог быть из-за `Command request` (Klipper гасит все MCU при ошибке bed) — это нормальное поведение, не отдельный баг.

### 7.6. Host MCU
- `Can not update MCU 'host' config as it is shutdown` — рестарт `klipper-mcu.service` по порядку из §2.

---

## 8. Host-патчи Klipper (что и зачем)

- `klippy/mcu.py`: reset wait 250 мс; `check_restart_on_stale_config` (firmware_restart при обычном старте); `stepper_stop_on_trigger` только при наличии stepper'ов; `try_lookup_command(..., cq)`.
- `klippy/extras/heaters.py`: clamp позднего `pwm_time`.
- `klippy/extras/hx71x.py`: `UPDATE_INTERVAL=0.05` (host batch/status cadence), `SAMPLES_PER_BLOCK=3`.
- `klippy/extras/bulk_sensor.py`: `FixedFreqReader(samples_per_block=...)`; status-query error re-raise при `is_reset` или `printer.is_shutdown()`.
- `klippy/extras/adc_temperature.py`: `sample_time`/`sample_count`/`report_time` настраиваемые.
- `klippy/extras/trigger_analog.py`: poll раз в 8 мс во время homing (polled-шина), deadline 120 с.
- `klippy/extras/load_cell_probe.py`: фикс `gcmd_filter.design_filter` + `_active_design`.
- `klippy/extras/host_power.py`: safety, read-back, M80 в shutdown.
- `scripts/maestro/host_multiplexer.py`: см. §5; `test_multiplexer.py` 19 тестов.

---

## 9. Что работает / что нет

**Работает:**
- Direct main на `/dev/ttyS1`; mux на ttyS0; PTY; 3 периферийных MCU отвечают; tap; 19 тестов mux.
- Прошивки head0/head1 16368/16366 и bed 8848 залиты и верифицированы; main 44252.
- Host-фикс 5-секундного stall (3-sample), PWM clamp, mux parser/ACK/quarantine.
- Конфиг: bed один датчик, тензы включены, Z virtual; Klipper конфиг проходит до конфигурации bed.
- 4-датчиковый bed конфиг сохранён в бэкапах; PA6 подключён.

**Не работает/не проверено:**
- Bed `Timer too close` на конфиге: query-clamp не залит (bed 8916 готов); головы с query-clamp не собираются.
- Boot-вентиляторы (PD4 on при старте) не в прошитых головах; `toolhead_fans_init` в репо, сборка голов FAIL.
- Тензо: калибровка/`PROBE_CALIBRATE`/`G28 Z` не сделаны; mux-фиксы не проверены в probing.
- 4 датчика bed + PA6 + `maximum_deviation` — отложено.
- RAM head0 с тензо (~4 move slots) — не решено; SOS 4→2 не применён.
- Остаточные риски mux/firmware (RXEN RMW, ACK-last, IRQ-off, RX 64, 1 Мбод).
- Симуляция не поднята.

---

## 10. Ближайший план (по слоям)

1. **Починить сборку голов**: query-clamp только bed (`CONFIG_ADC_QUERY_CLAMP`), головы ~16378 (fans + report-only); собрать, проверить размеры. Альтернатива — trims.
2. **Прошить bed 8916** (query-clamp) и **головы** (boot fans + 3-sample; query-clamp только если влезет) — принтер выключен, COM6, `-p m16`.
3. **`FIRMWARE_RESTART`** (или stop klipper → restart klipper-mcu → start klipper); проверить `state ready` и что bed с одним датчиком проходит конфиг; `Timer too close` не повторяется.
4. **PID extruder 200 °C**; затем **PID bed 80 °C**. Если `Timer too close` — смотреть `print_time`/clock/tap; PWM clamp уже в host.
5. **Тензо**: `LOAD_CELL_CALIBRATE` → TARE → CALIBRATE GRAMS → ACCEPT → SAVE_CONFIG; `PROBE_CALIBRATE` → `Z_OFFSET_APPLY_PROBE`; `G28 Z`; soak.
6. **RAM head0**: замерить `move_count` с `[load_cell_probe]`; при необходимости `MCU_SosFilter(...,4)` → 2 или другие trims; проверить `check-avr-size.sh`/stack.
7. **4 датчика bed** — после PA6/`maximum_deviation`; 128/128 уже есть.
8. **Симуляция** — см. §11; прогнать query/PID/протокол до железа.

---

## 11. Симуляция ATmega16A на PC (план нового чата)

Цель: дебажить firmware/протокол без железа, в идеале сразу воспроизвести `command_query_analog_in` → `sched_add_timer` → `Timer too close`.

### 11.1. simavr (рекомендуется)
- Поддерживает **ATMega8/16/32**; ATmega16A — тот же core; при необходимости `-m atmega32` (периферия та же, RAM больше; прошивка использует RAMEND ATmega16).
- Linux/WSL/Pi: `sudo apt install simavr` (или из исходников). Проверить `simavr --list-cores`/`-m atmega16`.
- Запуск реального Klipper `.elf` (Klipper `make` для atmega16; для исходников добавить `-g`):
  `simavr -m atmega16 -f 16000000 -g out/klipper.elf`
  `avr-gdb out/klipper.elf -ex 'target remote :1234'`
  breakpoints: `command_query_analog_in`, `sched_add_timer`, `analog_in_event`, `console_sendf`; смотреть `args[1]`, `timer_read_time()`, `next_begin_time`.
- USART → PTY: использовать `simavr` board/`uart_pty` (примеры `boards/`); Klipper `[mcu] serial: /tmp/simavr-...`; можно поднять 3 инстанса head0/head1/bed и подключить `host_multiplexer.py` — тест протокола/mux/quarantine без плат.
- Ограничения: аналог (HX711/ADC-шум/трансивер) только кастомным peripheral; цифровые таймеры/USART/SPI/GPIO/регистры — да. Тайминг не идеально реальный.

### 11.2. simulavr
- Старше; ATmega16 поддерживает; `simulavr -d atmega16 ...` + `avr-gdb`; проще для регистров, меньше плюшек.

### 11.3. Microchip Studio (Atmel Studio) — Windows
- Симулятор ATmega16A: `Debug → Start Simulation`, I/O View, таймеры, USART, ADC, breakpoints, `.elf`. Внешних компонентов нет; быстрый регистровый дебаг.

### 11.4. Proteus VSM
- ATmega16A + внешняя обвязка/аналог (HX711, MOSFET); board-level; коммерческий.

QEMU/Renode ATmega16 не эмулируют; avrora старый. Источники: simavr README, PlatformIO simavr docs, simulavr usage.

---

## 12. Правила работы

- `printer.cfg` — пользователя; не править без явной просьбы. Бэкап перед правкой.
- Сборка **только на Pi**: commit+push → `git pull --ff-only` → `./scripts/maestro/build_all_mcus.sh`. Hex в `~/klipper/firmware/`.
- Прошивка: avrdude из `C:\Users\user\Desktop\GOO (НЕ ТРОГАТЬ)\GRANDO\AVRDUDE`, `-c avrisp -P COM6 -b 19200`, `-p m16`/`-p m1284p`; принтер выключен; COM проверять.
- Принтер выключен = Pi выключен; WiFi xradio не отключать.
- Пароли (SSH/sudo) не писать в репозиторий; askpass helper на Windows `/tmp/maestro_askpass.sh`.
- Контекст: логи/Stats/tap — только `grep`/`tail`/`head`, ≤20–50 строк; не печатать целые конфиги/диффы.
- Субагенты: для изолированных аудитов/фиксов полезны (чистый контекст), но проверять их отчёты по коду/тестам; не давать им печатать пароли/делать permission-обходы.
- Перед прошивкой/правкой конфига — явное согласие пользователя.

---

## 13. Ключевые файлы/бэкапы

- Доки: `maestro_revision/CURRENT_STATE_2026-11-25.md` (этот), `HANDOFF_2026-11-25.md`; старые 2026-11-22 — исторические.
- Mux: `scripts/maestro/host_multiplexer.py`, `test_multiplexer.py`, `klipper-multiplexer.service`, `bus_sniffer.py`.
- Профили: `scripts/maestro/configs/config.mcu_*.sh` (нет; `config.mcu_*`).
- Firmware: `src/avr/{main,serial,gpio,timer,adc,watchdog}.c`, `src/{adccmds,sensor_hx71x,sensor_bulk,trigger_analog,generic/serial_irq,command,gpiocmds}.c`, `src/Kconfig`, `src/avr/Kconfig`, `src/avr/Makefile`.
- Host: `klippy/{mcu.py,extras/{heaters,hx71x,bulk_sensor,adc_temperature,trigger_analog,load_cell_probe,host_power}.py}`.
- Hex: `C:\Users\user\Downloads\maestro_firmware_2026-11-22\` (обновлять с Pi).
- Бэкапы config — §4.
- Коммиты 2026-11-25 (последние): `7d62451` query-clamp (сборка голов FAIL), `2c73c2b` mux quarantine delay/session 15, `8327cf7` mux parser/ACK/quarantine, `c9f3d52` report-only, `4ce94bf` fans init, `86a0d45` buffers/PWM host clamp, `24529cf` 3-sample, `0272c55` full clamp helper.
