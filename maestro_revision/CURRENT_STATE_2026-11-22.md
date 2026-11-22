# Maestro Grand 2 IDEX — состояние и история (2026-11-22)

> **Главный документ для продолжения.** Заменяет `CURRENT_STATE_2026-11-21.md`.
> `revision*.md`, архитектурный документ и `MAESTRO_DECISIONS_AND_CHANGELOG.md` —
> исторические; часть утверждений опровергнута.
>
> **Текущий момент:** принтер в `shutdown` (последние тесты — 4 датчика bed и
> PID extruder). На платах head0/head1 прошивка **ee1650b без ADC clamp**
> (16344 Б, с HX711/trigger). Профили head0/head1 в репозитории снова с
> HX711/trigger и `ADC_TIMER_CLAMP` (report-only), но сборка **не влезает**
> (16412/16384, не хватает 28 Б). `[load_cell_probe]` в `printer.cfg` пока
> **отключён** (печать без тензо), но пользователь решил, что load cell'ы
> будут использоваться. Bed сейчас с 4 датчиками, но тест 4 датчиков падает.

---

## 1. Архитектура (решение 2026-11-21/22)

- **Материнка (ATmega1284p)** — обычный Klipper-MCU на `/dev/ttyS1`, **без
  роутера**. Прошивка `klipper_mcu_motherboard_1284p.hex`, 44252 Б
  (`CONFIG_MAESTRO_ROUTER=n`). USART1 (PD2/PD3) не включается:
  `serial1_idle_init` оставляет PD2 входом, PD3 — входом со **слабой
  внутренней подтяжкой** (без push-pull). Это защищает от плавающего TXD
  трансивера материнки (доминанта на шине).
- **Периферийная шина** (head0 0x20, head1 0x30, bed 0x40) — на `/dev/ttyS0`
  через локальный трансивер. Мультиплексор `host_multiplexer.py` создаёт
  `/tmp/klipper_mcu_{head0,head1,bed}`; main идёт напрямую.
- **Pi:** ttyS0 больше не консоль: `console=display` в
  `/boot/armbianEnv.txt`, `serial-getty@ttyS0` disabled+masked. `ttyS0`
  чистый UART 250000. `/dev/ttyS1` — main, Klipper открывает сам.
- **IP Pi:** DHCP, на 2026-11-22 был **192.168.10.23** (раньше .24).
  WiFi wlan0 (xradio), не отключать.

### 1.1 Мультиплексор / сервис

`scripts/maestro/klipper-multiplexer.service` (на Pi — в
`/etc/systemd/system/`, ставится `cp`, не из репо):

```
--periph-port /dev/ttyS0 --baud 250000
--bus-guard 0.001 --bus-timeout 0.020
--tap-log /home/klipper/printer_data/logs/maestro_bus.log
```

`host_multiplexer.py` (версия 2026-11-22):

- main — не трогает (`--main-port` не передаётся; канал main пропускается);
- периферия: DEST rewrite, CRC16, полудуплексный арбитраж;
- release шины — только после **5-байтного ACK** и пустого буфера парсера,
  не после data-блока; `bus_guard` 1 мс, `bus_timeout` 20 мс (inactivity),
  жёсткий watchdog 200 мс; ошибка записи в UART сбрасывает арбитр;
- фильтр эха своего трансивера (первый кадр, побайтно равный отправленному);
- PTY-записи буферизуются (не теряются на EAGAIN);
- `poll(1 мс)`;
- tap: ms-метки, `REQ/RESP/ECHO/ACK`, `PARSE` (bad-len/dest/trailer/crc),
  `STALL/RESYNC`, `PHY` (TIOCGICOUNT). **TIOCGICOUNT исправлен**: полный
  `serial_icounter_struct` = 20 int = 80 байт (раньше 44 Б → порча кучи
  Python раз в секунду).

Unit-тесты: `scripts/maestro/test_multiplexer.py`, **15 OK** (включая
`_note_bus_packet`: data не завершает транзакцию, ACK завершает, частичный
кадр блокирует release).

### 1.2 Tap и сниффер

- Tap: `/home/klipper/printer_data/logs/maestro_bus.log`. Писать только
  нужные поля (`grep -E "PARSE|STALL|RESYNC|PHY|timeout" | tail`), не
  печатать полный лог.
- `scripts/maestro/bus_sniffer.py` — read-only сниффер для ноута через
  FT232. FT232 TXD поднят; RXD на узле A видит только TX Pi; чтобы видеть
  шину, RXD перекинуть на MCP2561 RXD / 5 В вход левелшифтера (узел B).
  pyserial на Windows установлен (3.5).

---

## 2. Прошивки и фьюзы (на 2026-11-22)

| Плата | На плате | Размер | Комментарий |
|---|---|---|---|
| main 1284p | `ee1650b` direct | **44252 Б** | без роутера, PD3 weak pull-up, USART1 off |
| head0 | `ee1650b` | **16344 Б (99.8%)** | HX711/trigger, `console_sendf` atomic, **ADC clamp нет** |
| head1 | `ee1650b` | **16344 Б (99.8%)** | то же |
| bed | `ee1650b` | **8856 Б** | `adccmds` report-path clamp, 1 датчик стабилен |

- Профили head0/head1 в репо: `CONFIG_WANT_HX71X=y`,
  `CONFIG_WANT_TRIGGER_ANALOG=y`, `CONFIG_ADC_TIMER_CLAMP=y`,
  `CONFIG_ADC_TIMER_CLAMP_REPORT_ONLY=y`. Сборка:
  **16412 Б, FAILED (100.2%)**, не хватает 28 Б. Полный clamp — 16444.
- Варианты влезть: убрать `CONFIG_INITIAL_PINS` (20 Б, вентиляторы не
  включаются до Klipper) + inline-clamp (8 Б) — ровно 16384; либо оставить
  старую `ee1650b` без clamp и проверить новый mux timing/report_time;
  либо резать HX717/другое.
- Фьюзы: head0 lfuse 0xBF hfuse 0xC7; head1 lfuse 0x3F hfuse 0xD1
  (BOOTRST исправлен); lock 0xFF. Не трогать без необходимости.
- Прошивка: avrdude `C:\Users\user\Desktop\GOO (НЕ ТРОГАТЬ)\GRANDO\AVRDUDE`,
  `-c avrisp -b 19200`, порт проверять (`Get-PnpDevice -Class Ports`);
  последний рабочий — **COM6**; ATmega16 `-p m16`, main `-p m1284p`.
  Принтер при прошивке выключен. Hex — в
  `...\GRANDO\Firmware\KlipperFirmware`.
- Роутерный откат: `klipper_mcu_motherboard_1284p_router.hex` (45258),
  старый однопортовый mux в git `497815a` (`--port /dev/ttyS1`, все 4 PTY),
  `[mcu] serial: /tmp/klipper_mcu_main`.

---

## 3. `printer.cfg` (реальный, на 2026-11-22)

Правило: файл составляет пользователь; править только по явной просьбе.
Текущее:

- `[mcu] serial: /dev/ttyS1` (main напрямую), `[mcu head0/head1/bed]` —
  PTY мультиплексора.
- `[extruder]` (head0): `microsteps: 16`, **`gear_ratio: 25:1`**,
  **`rotation_distance: 32.6726`** (π×10.4 мм ролик), `full_steps_per_rotation:
  200`, `max_extrude_only_distance: 200`, `report_time: 0.5`,
  `sample_count: 4`, `control: pid` с временными Kp 22.2/Ki 1.08/Kd 114.
  Эквивалент ~2448 шагов/мм; калибровать по 100 мм.
- `[extruder1]` (head1): старый `rotation_distance: 7.71`, PID 4.405/0.133/36.507.
- Макросы **`LOAD_FILAMENT`/`UNLOAD_FILAMENT`** (TEMP/LENGTH/SPEED,
  `M83`, `M109`, `SAVE/RESTORE_GCODE_STATE`); `T0`/`T1` активны.
- **`[load_cell_probe]` сейчас закомментирован**, Z — фиктивный `^!PC7` +
  `position_endstop: 0`. Пользователь решил load cell'ы использовать: надо
  вернуть `[load_cell_probe]` (head0:PD3/PD6) и
  `endstop_pin: probe:z_virtual_endstop`, убрать dummy.
- **Bed 4 датчика включены:** `[temperature_sensor bed_ts2/3/4]` PA5/PA6/PA7,
  `pullup_resistor: 8700`, `sample_count: 1`, `report_time` 0.6/0.7/0.8;
  `[heater_bed]` — PA4 + `multi_pin bed_heaters` (PD5/PD4/PD6/PD7).
- Термистор стола: `bed_ntc` 22 °C/41 кОм/B3950, подтяжка в конфиге 8700.
  Пользователь измерил **10 кОм** подтяжку и **41.9 кОм при 23.9 °C** —
  старая модель не сходится; нужна калибровка/вторая точка.
- Бэкапы: `printer.cfg.bak-20261122-bed4`, `...-nohead0`, `...-head1`,
  `...-noloadcell`, `...-print`, `...-pidtiming`, `...-bed4test`,
  `...-extruder`.

---

## 4. Что проверено и работает

- Direct main: `[mcu] /dev/ttyS1`, main `ee1650b`, 44252 Б; state ready
  (когда не падает bed/head0).
- Мультиплексор ttyS0, head0/head1/bed отвечают, tap пишет кадры; 15 тестов.
- Головы/стол подключаются; термисторы голов ~25 °C; стол ~24 °C.
- Без load cell конфигурация «две головы, bed 1 датчик» проработала **~30 мин**
  без падений.
- Тензодатчик head0 ранее читался (сотни значений); HX711/trigger в
  прошивке `ee1650b` есть; `UPDATE_INTERVAL` 0.05 (отчёт ~30 Б вместо 58).
- Экструдер head0: геометрия 25:1/32.6726 занесена, макросы подачи созданы.
- `load_cell_probe.py`: исправлено применение нового фильтра
  (`gcmd_filter.design_filter`, `_active_design = gcmd_filter`).
- `trigger_analog.py`: опрос 120 с вместо 10 с (долгий tap/пробинг).

## 5. Открытые проблемы (приоритет)

1. **PID extruder → `Timer too close` (head0).**
   `PID_CALIBRATE HEATER=extruder TARGET=200` падает почти сразу:
   `MCU 'head0' shutdown: Timer too close`. В логе `print_time` сильно
   отстаёт от MCU clock; у head0 большие `bytes_retransmit`. Прошивка на
   плате `ee1650b` **без ADC clamp**; профиль с clamp не влезает (28 Б).
   `report_time: 0.5`, `BUS_TIMEOUT=0.020`, guard 0.001, poll 1 мс уже
   применены — не помогло. Дальше: либо влезть с clamp (убрать
   `CONFIG_INITIAL_PINS` + inline), либо проверить старую прошивку без
   clamp на новом timing, либо увеличить запас `pwm_delay`/разобрать
   print_time.
2. **Load cell.** Пользователь: использовать. `[load_cell_probe]` в
   конфиге отключён; профили head0/head1 с HX711/trigger восстановлены.
   Head0/head1 с clamp не влезают; нужен выбор (см. п.1). После прошивки:
   вернуть `[load_cell_probe]` на head0, Z virtual, `LOAD_CELL_CALIBRATE`
   → TARE → CALIBRATE GRAMS → ACCEPT → SAVE_CONFIG; затем PROBE_CALIBRATE.
3. **Bed 4 датчика.** Тест провален: `Lost communication with MCU 'bed'`,
   `bed_ts3` (PA6) = **-103.95 °C**, `bed_ts2/4` ~24 °C. Report-clamp
   (bed 8856) не спас. Похоже на PA6-датчик/4-канальный ADC. Для рабочего
   принтера вернуть bed на 1 датчик (PA4); конфиг 4 датчиков сохранён
   (`printer.cfg.bak-20261122-bed4test` — до включения 4-х).
4. **Стабильность шины/head0.** До этого head0 терялся после 58-байтных
   HX711-отчётов; `console_sendf` atomic + `UPDATE_INTERVAL=0.05` +
   ACK-release/timing; проверить soak. При отказе смотреть tap:
   `PARSE/STALL` = кадры/буфер, `0000`+тишина = TTL/трансивер/STBY.
5. **Bed 4/ADC clamp.** Полный clamp на bed есть; 4 датчика всё равно
   падают; report-path clamp добавлен; причина не закрыта. Возможные
   кандидаты: PA6-датчик, RX/TX 64 Б, stack 160, ADC timer.
6. **Термистор стола.** Модель 22/41k/B3950 и pullup 8700 vs измеренные
   41.9k@23.9 и 10k — recalibrate.
7. **head0/head1 flash** 16344/16384; любая новая фича требует места.

---

## 6. Ключевые коммиты (2026-11-22)

- `ee1650b` — bus arbiter/tap fix, polled-slave `console_sendf` atomic,
  PD3 weak pull-up, `adccmds` report clamp, `load_cell_probe` filter,
  `trigger_analog` 120 s.
- `be20f12` — `hx71x UPDATE_INTERVAL` 0.10→0.05.
- `902f2c1` — `BUS_TIMEOUT` 20 мс, `BUS_GUARD` 1 мс, mux poll 1 мс.
- `2708c5d` — `bus_sniffer.py`.
- `903f8c3` — tap.
- `18792e3`/`040ccab`/`df13731`/`6b53cd3` — direct bus, no router, PD3
  high-Z/pull-up history.
- `497815a` — состояние до direct (старый mux/router).
- Текущая ветка/HEAD — см. `git log`; профили head0/head1 с clamp
  (сборка не влезает) в `4c22c01`/`6168aad`/`2a77a21`/`4963f6c`.

---

## 7. Правила работы

- `printer.cfg` составляет пользователь; не править без явной просьбы.
- Сборка только на Pi по SSH (`klipper@192.168.10.23`; пароль у
  пользователя): commit+push → `git pull --ff-only` →
  `./scripts/maestro/build_all_mcus.sh`. Hex в `~/klipper/firmware/`.
- Прошивка: avrdude из `...\GRANDO\AVRDUDE`, `-c avrisp -b 19200`,
  `-p m16`/`-p m1284p`; порт проверять (сейчас COM6). Принтер выключен.
- Принтер выключен = Pi выключен (общее питание). WiFi не отключать.
- Логи: **не печатать полные `Stats`** (одна строка ~1.5–2k токенов);
  `grep -o "head0:[^h]*"`, `grep -E "Timer too close|Lost communication"`,
  `tail -n 5`. Tap — только `grep -E "PARSE|STALL|RESYNC|PHY|timeout"`.
- Load cell'ы: пользователь их будет использовать; не отключать
  `HX711/trigger` в прошивке без явной просьбы.
