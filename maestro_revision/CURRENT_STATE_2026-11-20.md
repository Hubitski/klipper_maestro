# Maestro Grand 2 IDEX — актуальное состояние (2026-11-20)

> **Главный документ для продолжения.** Заменяет `CURRENT_STATE_2026-11-14.md`
> (тот оставлен как история). `revision*.md` и архитектурный документ —
> исторические, часть утверждений в них опровергнута.

## 1. Статус на 2026-11-20

- **Klipper работает**: `state: ready`, `Printer is ready`.
- Подключены и сконфигурированы: `mcu` (ATmega1284p), `host` (linux MCU),
  `bed` (ATmega16A), `head0` (ATmega16A). `head1` в конфиге закомментирован.
- Движения X/Y/X1/Z (TMC2130) инициализируются; реальных тестов движения
  пока не было.
- `[load_cell_probe]` добавлен в конфиг, но калибровка не проходила:
  HX711 не отдавал отсчёты. Причина найдена и исправлена в прошивке
  (`eb0361b`), **но новая прошивка головы ещё не залита** — нужна
  перепрошивка head0.
- Старый сервис реле `relay-toggle.service` **остановлен и отключён**
  (`inactive`, `disabled`). GPIO6 сейчас не экспортирован. В конфиг
  добавляется наш `[host_power]`.

## 2. Железо и топология

- **Материнка:** ATmega1284p, 16 МГц, 128 КБ Flash / 16 КБ SRAM.
  Оси X, X1 (IDEX), Y, Z, TMC2130 по аппаратному SPI, sensorless
  X/X1/Y. Z-концевик `^!PC7` — **фиктивная заглушка** (ничего не
  подключено; пользователь поставил пин, чтобы Klipper не ругался).
- **Головы (2 шт.):** ATmega16A, 16 МГц, 16 КБ Flash / 1 КБ SRAM.
  Экструдер (TMC2130 по SPI), нагреватель, термистор, 2 вентилятора,
  HX711 (тензодатчик).
- **Стол:** ATmega16A, нагреватель + термистор. **Распиновка стола не
  проверена** (AI-конфиг: heater `PD5`, thermistor `PA0`).
- **Шина:** MCP2561 как PHY полудуплексного UART, 250000 бод.
  Адрес — старший полубайт байта SEQ: main `0x10`, head0 `0x20`,
  head1 `0x30`, bed `0x40`.
- **Хост:** Orange Pi Zero (Armbian, kernel 6.18.44), Klipper в
  `/home/klipper/klippy-env`. **Сеть — WiFi `wlan0` 192.168.10.24**
  (Ethernet `end0` лежит). Не отключать `xradio_wlan` — потеряется SSH.
- **Физическая шина:** `/dev/ttyS1` (`/dev/ttyS0` — консоль ядра).

### 2.1. Пины головы (проверено рабочим Arduino-скетчем)

| Функция | Пин |
|---|---|
| Extruder STEP | `PA7` |
| Extruder DIR | `PC6` |
| Extruder ENN (active low) | `PC7` |
| TMC2130 CS | `PA0` |
| TMC2130 SPI | SCK `PB7`, MOSI `PB5`, MISO `PB6` |
| Нагреватель | `PD7` |
| Термистор | `PA4` (NTC 100K 3950, подтяжка 4.7 к) |
| Вентилятор 1 | `PD5` |
| Вентилятор 2 | `PD4` |
| HX711 DOUT | `PD3` (вход, pull-up) |
| HX711 PD_SCK | `PD6` (выход) |

Платы head0 и head1 **идентичны по схеме**; отличается только placement
разъёмов. На head1 между CANH/CANL стоит 120 Ом (терминатор), на head0 —
нет; на head0 `PB3` (AIN1/OC0) посажен на землю через 0 Ом, на head1 —
нет. Суммарно на шине 60 Ом при всех платах (правильно).

### 2.2. Пины материнки (из реального `printer.cfg`)

| Ось | STEP | DIR | EN | TMC CS | DIAG |
|---|---|---|---|---|---|
| X | `PB2` | `!PB1` | `!PD5` | `PB0` | `^!PD4` |
| X1 | `PA2` | `!PA1` | `!PD5` | `PA3` | `^!PD4` |
| Y | `PC1` | `PC6` | `!PD5` | `PD6` | `^!PD4` |
| Z | `PB4` | `PA4` | `!PD5` | `PA0` | — |

`spi_bus: spi`, `[duplicate_pin_override] pins: PD4`,
`stealthchop_threshold: 999999`, case light `PC4`, enclosure fan `PD7`.

## 3. Прошивки и сборка

Сборка только на принтере по SSH `klipper@192.168.10.24` (пароль у
пользователя, в репозиторий не писать). Репозиторий `~/klipper`, ветка
`master`. Порядок: коммит+пуш в Windows-клоне → `git pull` на принтере →
`./scripts/maestro/build_all_mcus.sh`. Результат — `~/klipper/firmware/`
(**не** `out/`: каждый таргет делает `make clean`).

Тулчейн: avr-gcc 7.3.0 + **binutils 2.26** (баг: проверка регионов до
`--gc-sections` → ложные overflow). Обход: `-Wl,--noinhibit-exec` +
`scripts/check-avr-size.sh` (точные байты + белый список сообщений
линкера). Дополнительно `-Wl,--relax`.

### 3.1. Текущие размеры (коммит `eb0361b`)

| Прошивка | Flash | RAM |
|---|---|---|
| motherboard 1284p | 44926 Б (34.3%) | 946 Б |
| bed atmega16 | 8696 Б (53.1%) | 225 Б |
| head0 atmega16 | **16212 Б (99.0%)** | 227 Б |
| head1 atmega16 | 16212 Б (99.0%) | 227 Б |

Прошивка головы: HX711 + sensor_bulk + trigger_analog + sos_filter +
`trsync` + аппаратный SPI для TMC2130. Стек 192, куча ~605 Б.
Реальная move queue: **head0 — 30**, bed — 271, main — 1024.

**На платах сейчас прошивка `2bbc6c0` (16284 Б), без последнего фикса
HX711. Новая (16212 Б, `eb0361b`) лежит в `firmware/` и в
`C:\Users\user\Desktop\GOO (НЕ ТРОГАТЬ)\GRANDO\Firmware\KlipperFirmware`
— её надо залить в head0.**

### 3.2. Что включено в профилях

- **Голова:** stepper, ADC, software PWM, serial polled slave, HX711,
  trigger_analog, sos_filter, trsync, аппаратный SPI; `WANT_ENDSTOP=n`,
  `debugcmds/stats=n`, `AVR_STACK_SIZE=192`.
- **Стол:** ADC, GPIO, serial; без stepper/endstop; стек 256.
- **Материнка:** stepper, endstop, trsync, ADC, SPI (аппаратный+software),
  hard PWM, TMCUART, роутер `MAESTRO_ROUTER=y`, polled slave выключен.

## 4. Протокол и мультиплексор

- DEST в старшем полубайте SEQ; мультиплексор переписывает DEST и CRC16
  в обе стороны; CRC проверяется до перезаписи.
- Стартовая рассинхронизация seq — штатная (klippy resync через NAK).
- Polled slave: MCU передаёт только в ответ на команду; ACK последним;
  чужие/битые кадры молча сбрасываются.
- Мультиплексор `scripts/maestro/host_multiplexer.py`:
  `/tmp/klipper_mcu_{main,head0,head1,bed}`; арбитраж: 3 мс тишины после
  ответа, таймаут 50 мс после реальной записи, `time.monotonic()`.
- Сервис `klipper-multiplexer.service`: `User=klipper`, venv-python
  (там pyserial), порт `/dev/ttyS1`, `StartLimitIntervalSec=0`. Установлен,
  enabled, active.
- **Асинхронные сообщения** (триггер тензо) в polled-режиме ждут команды.
  Обход: `klippy/extras/trigger_analog.py` во время хоуминга каждые 8 мс
  переотправляет идемпотентный `trigger_analog_set_raw_range`, чтобы MCU
  вытолкнул очередь; укладывается в 25 мс multi-MCU trsync.

## 5. Патчи Klipper (host)

- `klippy/mcu.py`: после `reset` ждать **250 мс** (было 15 мс) — иначе
  периферия не успевала сброситься через watchdog (16 мс) + шину →
  «Failed automated reset of MCU 'bed'» (коммит `fe55447`).
- `klippy/mcu.py`: `stepper_stop_on_trigger` ищется через
  `try_lookup_command(...)` (и `try_lookup_command` принимает `cq`), и
  требуется только если к trsync привязаны шаговики. У head0 trsync без
  шаговиков (Z на материнке) — команда не нужна (коммит `f7df5b8`).
- `klippy/extras/trigger_analog.py`: опрос сенсора во время хоуминга
  (коммит `2bbc6c0`).
- `klippy/extras/host_power.py`: обязательный путь отключения, проверка
  GPIO read-back, ошибки не проглатываются, `off_on_disconnect=False`
  (коммит `7458ce9`).

## 6. Патчи прошивки

- `src/avr/main.c`: `dynmem_end()` = `RAMEND + 1 - AVR_STACK_SIZE`
  (на ATmega16 `ALIGN(0x45F,256)` уходил на 160 Б выше RAMEND) —
  `f2638db`.
- `src/avr/serial.c`, `src/avr/maestro_router.c`: не сбрасывать TXC в
  idle-пути UDRE (иначе RXEN навсегда выключен) — `f2638db`.
- `src/generic/serial_irq.c`: TX-буфер 64 (bulk-отчёты HX711).
- `src/trigger_analog.c`: под `HAVE_LIMITED_CODE_SIZE` вырезаны монитор
  сенсора и типы `gt`/`diff_peak_gt` (нужен `abs_ge`) — `42cbd14`.
- `src/sensor_hx71x.c`: **убрана проверка дополнительных тактовых битов**
  после 24 бит данных. Некоторые модули HX711 не поднимают DOUT сразу
  после 25-го импульса → каждый отсчёт помечался `DESYNC`, сенсор
  бесконечно перезапускался, хост не получал ни одного sample. Скетч
  эти биты не читает, поэтому работал — `eb0361b`.
- `src/basecmd.c`: `get_uptime`/`STATS_SUMSQ_BASE` всегда (klippy требует
  их даже при `WANT_STATS=n`) — `7458ce9`.
- `src/stepper.c`: `command_stepper_stop_on_trigger` под `WANT_ENDSTOP`;
  на голове команды нет, но хостовый фикс её и не требует.

## 7. Конфиг принтера (реальный, `~/printer_data/config/printer.cfg`)

- `[mcu]` — `/tmp/klipper_mcu_main`, `restart_method: command`.
- `[mcu host]` — `/tmp/klipper_host_mcu`.
- `[mcu bed]` — `/tmp/klipper_mcu_bed`, `restart_method: command`.
- `[mcu head0]` — `/tmp/klipper_mcu_head0`, `restart_method: command`.
- `[mcu head1]` — закомментирован.
- `[load_cell_probe]`: `sensor_type: hx711`, `dout_pin: head0:PD3`,
  `sclk_pin: head0:PD6`, `sample_rate: 80`, `gain: A-128`,
  `z_offset: 0.0` (заглушка; калибровка не пройдена).
- `counts_per_gram`/`reference_tare_counts` ещё не заданы — нужны после
  `LOAD_CELL_CALIBRATE`.
- `[stepper_z]` должен хомиться по `probe:z_virtual_endstop`.
- Экструдер: в логе `extruder: temp=0.0` — термистор, похоже, не читается
  (PA4) или не подключён.

**Правило: этот файл составляет пользователь. Не редактировать без
явной просьбы. AI-примеры конфигов в репозитории не являются реальным
конфигом.**

## 8. Что сделано 2026-11-14 → 2026-11-20

- Прошита материнка (45094 Б) и проверена: **коннект через мультиплексор
  работает**, роутер/локальный путь/USART0 живы; 1024 move.
- Прошиты head0 (16284) и bed (8696); обе подключились:
  head0 — 30 move, bed — 271 move. Периферийная multi-drop шина
  (USART1, MCP2561, polled slave, DEST rewrite) **работает**.
- Сняты дампы оригинальных прошивок: bed (flash+EEPROM), head1
  (flash+EEPROM) — лежат в папке KlipperFirmware. Оригиналы main/head0
  не сохранились (прошиты раньше).
- Найдены и исправлены: тайминг reset (250 мс), отсутствие
  `stepper_stop_on_trigger` (хостовый фикс), HX711 DESYNC (прошивка).
- Настроен systemd shutdown watchdog (`/etc/systemd/system.conf.d/watchdog.conf`:
  `RebootWatchdogSec=10s`, `ShutdownWatchdogSec=10s`) — против зависания
  reboot/shutdown (sunxi + xradio). Проверить реальным `reboot`.
- Диагностика SD: `NCard` 14.6 GiB (12/2022), ошибок MMC/EXT4 в логах
  нет, ФС clean, быстрый read-тест OK. Ёмкость в старых логах была
  14.8 GiB — карта стареет; рекомендуется замена. `journald`
  `Storage=volatile` (прошлые загрузки не сохраняются).
- Найден сервис реле: `relay-toggle.service`, GPIO6 (sysfs, active high,
  включал реле при старте и делал unexport). **Остановлен и отключён.**

## 9. Открытые проблемы / следующие шаги

1. **Залить новую прошивку head0 (16212 Б, `eb0361b`)** — фикс HX711.
   После этого: `LOAD_CELL_CALIBRATE`, `PROBE_CALIBRATE`,
   `Z_OFFSET_APPLY_PROBE`, `G28 Z`.
2. **Добавить `[host_power] pin: 6`** в конфиг (реле). Пока сервис
   отключён, не перезагружать принтер до добавления — иначе платы без
   питания. Пример:
   ```ini
   [host_power]
   pin: 6
   active_low: False
   startup_delay: 1.5
   off_when_shutdown: True
   off_on_disconnect: False
   ```
   Проверить `M80`/`M81`; убедиться, что реле не питает Orange Pi.
3. **Правая голова (head1) не связывается.** Отличия: 120 Ом терминатор
   (правильный), `PB3` 0 Ом на землю на head0 и отсутствует на head1.
   Гипотеза: этот net управляет `STBY` (пин 8) MCP2561; STBY должен быть
   **0 В** для нормального режима. Проверить напряжение на пине 8 и
   прозвонку PB3→STBY; попробовать временно посадить PB3 на землю.
   Сейчас в правой голове прошивка head0 (`0x20`) для теста — вернуть
   `head1` (`0x30`) и добавить `[mcu head1]`.
4. **Экструдер `temp=0.0`** — проверить термистор на PA4/подключение.
5. **Версии MCU `2bbc6c0` vs хост `eb0361b`** — предупреждение; после
   перепрошивки всех плат текущим коммитом уйдёт.
6. **Проверить реальный `reboot`** с новым watchdog; при зависании —
   посмотреть, перезагрузился ли через ~10 с.
7. Не забыть: сейчас на шине могут быть две платы с `0x20` (head0 и
   правая, прошитая как head0) — для финальной схемы оставить одну.

## 10. Правила работы

- Не трогать `~/printer_data/config/printer.cfg` (кроме явных просьб,
  как сейчас с реле).
- Не трогать AI-примеры конфигов в репозитории.
- Сначала коммит+пуш, потом `git pull` и сборка на принтере.
- Прошивка: `C:\Users\user\Desktop\GOO (НЕ ТРОГАТЬ)\GRANDO\AVRDUDE\avrdude.exe`
  (`-c avrisp -P COM6 -b 19200`), hex в
  `...\GRANDO\Firmware\KlipperFirmware`. Для ATmega16 `-p m16`, для
  материнки `-p m1284p`; **не перепутать**.
- Не доверять старым докам: проверять по даташиту, реальному конфигу,
  рабочему скетчу и живому логу.
- SSH-пароль и sudo-пароль не писать в репозиторий.

## 11. Коммиты (ключевые)

`f82da27` diag-таргет · `b6b2f88` обход ложного overflow · `8fd12ec`
trsync/trigger_analog + host mux fcntl · `7f0fdd4` сервис/порт · `4e29614`
стек/TX-буфер · `7458ce9` get_uptime/hex/host safety · `42cbd14` фит
HX711+trigger+SPI · `2bbc6c0` host-опрос триггера + откат примера конфига ·
`fe55447` пауза reset 250 мс · `f7df5b8` trsync без шаговиков ·
`eb0361b` HX711 без проверки лишних битов.
