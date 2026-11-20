# Maestro Grand 2 IDEX — актуальное состояние проекта (2026-11-14)

> **УСТАРЕЛО (2026-11-20).** Актуальный документ:
> [CURRENT_STATE_2026-11-20.md](CURRENT_STATE_2026-11-20.md).

> **Это главный документ для продолжения работы.** Файлы `revision*.md`,
> `MAESTRO_GRAND2_KLIPPER_ARCHITECTURE.md` — исторические. В них есть
> утверждения, которые проверены и оказались ложными (см. §8). Если
> текущий документ противоречит им — верен текущий.

## 1. Железо (проверено)

- **Материнка:** ATmega1284p, 16 МГц, 128 КБ Flash / 16 КБ SRAM. Оси X, X1
  (IDEX), Y, Z, драйверы TMC2130 по аппаратному SPI, sensorless-хоуминг
  X/X1/Y. Принтер подключён к Orange Pi Zero через **`/dev/ttyS1`**
  (`/dev/ttyS0` — консоль ядра, не трогать).
- **Головы (2 шт.):** ATmega16A, 16 МГц, 16 КБ Flash / 1 КБ SRAM:
  экструдер (TMC2130 по SPI), нагреватель, термистор, 2 вентилятора,
  HX711 (тензодатчик для Z).
- **Стол:** ATmega16A: нагреватель + термистор. **Распиновка стола НЕ
  проверена** (взять из реальной схемы/прозвонки перед подключением
  нагревателя).
- **Шина:** MCP2561 как PHY полудуплексного UART, 250000 бод, все 4 MCU на
  одной паре. Адрес узла — старшие 4 бита байта SEQ:
  main `0x10`, head0 `0x20`, head1 `0x30`, bed `0x40`.
- **Хост:** Orange Pi Zero, Klipper в `/home/klipper/klippy-env`, конфиг
  `/home/klipper/printer_data/config/printer.cfg`.

### 1.1. Пины головы — ground truth из рабочего Arduino-скетча

| Функция | Пин | Примечание |
|---|---|---|
| Extruder STEP | `PA7` | |
| Extruder DIR | `PC6` | полярность проверить |
| Extruder ENN | `PC7` | активный низкий |
| TMC2130 CS | `PA0` | |
| TMC2130 SPI | SCK `PB7`, MOSI `PB5`, MISO `PB6` | аппаратный SPI |
| Нагреватель | `PD7` | |
| Термистор | `PA4` | ADC4, NTC 100K 3950, подтяжка 4.7 кОм |
| Вентилятор 1 | `PD5` | в скетче — аппаратный ШИМ OC1A |
| Вентилятор 2 | `PD4` | OC1B |
| HX711 DOUT | `PD3` | вход |
| HX711 PD_SCK | `PD6` | выход |

> В Klipper Timer1 занят планировщиком, поэтому вентиляторы работают через
> **программный ШИМ** (`gpiocmds.c`), это нормально.

### 1.2. Пины материнки — ground truth из реального `printer.cfg`

| Ось | STEP | DIR | EN | TMC CS | DIAG |
|---|---|---|---|---|---|
| X | `PB2` | `!PB1` | `!PD5` | `PB0` | `^!PD4` |
| X1 | `PA2` | `!PA1` | `!PD5` | `PA3` | `^!PD4` |
| Y | `PC1` | `PC6` | `!PD5` | `PD6` | `^!PD4` |
| Z | `PB4` | `PA4` | `!PD5` | `PA0` | — |

- `spi_bus: spi` (аппаратный SPI), `[duplicate_pin_override] pins: PD4`.
- Z-концевик `^!PC7` — **фиктивная заглушка**, физически ничего не
  приходит. Z домается только по тензодатчику головы.
- Прочее в реальном конфиге: `[output_pin caselight] PC4`,
  `[fan_generic enclosure_fan] PD7`, `stealthchop_threshold: 999999`.

## 2. Сборка прошивок

Сборка **только на принтере по SSH**: `klipper@192.168.10.24`
(пароль знает пользователь, в репозиторий не писать). Репозиторий:
`/home/klipper/klipper`; ветка `master`; изменения делать в Windows-клоне,
коммитить и пушить, затем на принтере `git pull` и сборка.

```bash
cd ~/klipper
git pull
./scripts/maestro/build_all_mcus.sh
```

- Результат: `~/klipper/firmware/*.hex` (**не** `out/`, иначе `make clean`
  удаляет чужие результаты).
- Скрипт проверяет наличие конфига, совпадение `CONFIG_SERIAL_NODE_ID`,
  реальные размеры (`avr-size` в байтах) и падает при переполнении.
- `make diag` — линковка с `--noinhibit-exec` для диагностики.

**Тулчейн:** avr-gcc 7.3.0 + **binutils 2.26**. У него баг: проверка
регионов выполняется до `--gc-sections`, поэтому он ложно пишет
`region 'data' overflowed by N bytes`. Поэтому AVR собирается с
`-Wl,--noinhibit-exec`, а `scripts/check-avr-size.sh` проверяет реальный
ELF (неразрешённые символы, точные байты, только известные ложные
сообщения линкера). Дополнительно используется `-Wl,--relax`.

### 2.1. Текущие размеры

| Прошивка | Flash | RAM |
|---|---|---|
| motherboard 1284p | 45094 Б (34.4%) | 946 Б |
| bed atmega16 | 8696 Б (53.1%) | 225 Б |
| head0 atmega16 | **16284 Б (99.4%)** | 227 Б |
| head1 atmega16 | 16284 Б (99.4%) | 227 Б |

Голова: 100 байт flash в запасе — **любая новая фича переполнит**.
RAM: куча ≈ 605 байт (стек 192, `_end = 0x143`, `dynmem_end = 0x3A0`);
оценка ~11 записей move queue. Реальное число покажет Klipper
(`get_config move_count=`).

## 3. Что сейчас в прошивках (Kconfig-профили)

Профили сборки — `scripts/maestro/configs/config.mcu_*` (это НЕ
printer.cfg; их правки нужны для сборки).

- **Голова:** stepper, ADC, software PWM (`gpiocmds`), serial polled slave,
  HX711 + sensor_bulk + trigger_analog + sos_filter, `trsync` (через
  `NEED_TRSYNC`), **аппаратный SPI** для TMC2130; `WANT_ENDSTOP=n`;
  `debugcmds/stats=n`; `AVR_STACK_SIZE=192`.
  Явно выключены все SPI-датчики по умолчанию (ADXL345, LIS2DW, BMI160,
  термопара, ADS1220, ADS131M0X, sensor_angle) — иначе +10 КБ.
- **Стол:** ADC, GPIO, serial; без stepper/endstop; стек 256.
- **Материнка:** stepper, endstop, trsync, ADC, SPI (аппаратный и
  software), hard PWM, TMCUART, роутер `CONFIG_MAESTRO_ROUTER=y`,
  `SERIAL_POLLED_SLAVE=n`, стек 256.

## 4. Протокол / мультиплексор (проверено аудитом)

- DEST в старшем полубайте SEQ; хост-мультиплексор переписывает DEST и
  пересчитывает CRC16 в обе стороны. CRC проверяется до перезаписи.
- Стартовая рассинхронизация seq — штатная: klippy начинает с `send_seq=1`,
  MCU отвечает NAK со своим ожидаемым seq, klippy делает resync.
- Polled slave: спонтанной передачи нет; ACK ставится последним; NAK только
  на свой адрес с валидным CRC; чужие/битые кадры молча сбрасываются.
- Мультиплексор (`scripts/maestro/host_multiplexer.py`):
  - создаёт `/tmp/klipper_mcu_{main,head0,head1,bed}`;
  - арбитраж шины: запрос → ожидание ответа; шина освобождается после 3 мс
    тишины (а не по ACK), таймаут ответа 50 мс после реальной записи в UART;
  - тайминги на `time.monotonic()`.
- Сервис: `scripts/maestro/klipper-multiplexer.service`, `User=klipper`,
  запуск через `~/klippy-env/bin/python` (там есть pyserial; системный
  python3 — нет), порт `/dev/ttyS1`, `StartLimitIntervalSec=0`.
- **Нюанс:** в строгом polled-режиме MCU передаёт только в ответ на команду.
  Поэтому асинхронные сообщения (триггер тензо, отчёты) ждут следующей
  команды. Для Z-пробинга это решено на хосте: `trigger_analog.py` во время
  хоуминга каждые 8 мс переотправляет идемпотентную команду raw-range,
  чтобы MCU вытолкнул очередь (таймаут multi-MCU trsync — 25 мс).

## 5. Аудит: что нашли и исправили

Коммиты: `f82da27`, `b6b2f88`, `8fd12ec`, `7f0fdd4`, `4e29614`, `7458ce9`,
`b02db20` (конфиг-пример, позже откачен), `42cbd14`, `2bbc6c0`.

### Критичное

1. **`get_uptime` / `STATS_SUMSQ_BASE` были вырезаны вместе со stats.**
   klippy использует их безусловно (`clocksync.py`, `mcu.py`) →
   все три ATmega16-платы не подключились бы (`Unknown command: get_uptime`).
   Исправлено: команда и константа всегда есть, при выключенной статистике
   `stats_update()` только ведёт перенос 32-битного счётчика.
2. **Скрипт сборки сам удалял hex-файлы** (`out/maestro_firmware` +
   `make clean = rm -rf out/`). Оставался только последний. Выход перенесён
   в `firmware/`, добавлены проверки.
3. **`dynmem_end()` на ATmega16 указывал за конец SRAM.**
   `ALIGN(0x45F, 256) = 0x500` (на 160 байт выше RAMEND), куча залезала в
   стек → падение на `finalize_config`. Исправлено на
   `RAMEND + 1 - AVR_STACK_SIZE`.

### Высокое

4. **Гонка TXC** в `serial.c`/`maestro_router.c`: если UDRE-ISR опаздывал,
   сброс TXC терял единственное событие завершения → `RXEN` навсегда
   выключен, плата глохнет. Убрали сброс TXC в idle-пути.
5. **Стек 128 байт** — слишком мало; для голов 192.
6. **Не хватало flash на HX711 + trigger_analog + SPI.** Решено:
   `-Wl,--relax` (−260 Б) + под `HAVE_LIMITED_CODE_SIZE` вырезаны монитор
   датчика и типы триггера `gt`/`diff_peak_gt` (нужен только `abs_ge`) —
   ещё −286 Б. Итог 16284/16384.
7. **Мультиплексор не запускался:** `os.fcntl`/`os.F_GETFL` не существуют в
   Python. Исправлено на модуль `fcntl`; `select.error` → `OSError`;
   `sys.exit(1)` при фатальной ошибке.
8. **Юнит systemd** указывал на `/home/pi/...` и `/usr/bin/python3`; порт
   был `/dev/ttyS0` (консоль). Исправлено: `User=klipper`, venv-python,
   `/dev/ttyS1`, `StartLimitIntervalSec=0`.
9. **`host_power.py` (реле 24 В)** мог «отключить» питание, ничего не
   отключая: не требовался `off_cmd`, ошибки GPIO/команд проглатывались.
   Исправлено: обязательный путь отключения, проверка read-back, состояние
   меняется только при успехе, `off_on_disconnect` по умолчанию False.
10. **Пример конфига принтера был выдуман.** Реальные пины — в §1 и в
    `printer_data/config/printer.cfg`. AI-пример не трогать, его составляет
    пользователь.

### Сделано по host-части

- `trigger_analog.py`: опрос платы во время хоуминга (см. §4).

## 6. Что НЕ проверено / открытые риски

- **На железе ничего не запускалось.** Всё выше — статический аудит + сборка.
- Голова: 100 байт flash; куча/move queue — оценка.
- Путь Z-пробинга (HX711 → trigger → опрос → trsync материнки) не проверен.
  Если шина занята в момент триггера, сообщение уйдёт со следующей командой;
  тогда trsync-таймаут (25 мс) остановит Z и хоуминг упадёт с ошибкой —
  это безопасный отказ, но пробу придётся повторить.
- Мультиплексор не установлен; в `printer.cfg` ещё нет 4 PTY-секций.
- В реальном `printer.cfg` нет экструдеров/нагревателей/стола/`[load_cell_probe]`
  — их составляет пользователь.
- TMC2130 на головах: без `[tmc2130 extruder]` (`spi_bus: spi`, `cs_pin:
  head0:PA0`) драйвер останется на заводских настройках (ток/микрошаги не
  выставить).
- Распиновка стола не подтверждена.
- Реле 24 В не включено: нужен GPIO/команда и подтверждение, что Orange Pi
  питается не от того же 24 В.
- Роутер: нет таймаута в FSM приёма, DEST-ы захардкожены, CRC на роутере не
  проверяется (проверяет хост-мультиплексор).
- Частичные записи в PTY не буферизуются (потеря кадра → retry).
- В старых `revision*.md` есть ложные утверждения: «9.2 КБ Flash / 490 Б
  SRAM», «0 errors, 0 warnings», «стек 128 применяется» (символ не имел
  prompt — значение игнорировалось до 4e29614), «ttyS0».

## 7. План (что делаем сейчас)

1. **Прошить материнку** `klipper_mcu_motherboard_1284p.hex` (не atmega16!).
2. Запустить Klipper как есть (`[mcu] serial: /dev/ttyS1`) — проверить
   коннект. Это тест главного MCU и локального пути роутера.
3. Установить мультиплексор:
   ```bash
   sudo cp scripts/maestro/klipper-multiplexer.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now klipper-multiplexer
   ls -la /tmp/klipper_mcu_*
   ```
   Переключить `[mcu]` на `/tmp/klipper_mcu_main` (Klipper не должен
   открывать ttyS1 напрямую) — это тест мультиплексора.
4. Прошить head0, добавить `[mcu head0]` (`/tmp/klipper_mcu_head0`) — первый
   тест периферийной шины.
5. Затем head1 и bed; затем экструдеры/нагреватели/вентиляторы/TMC в
   пользовательском конфиге.
6. Z-пробинг: `[load_cell_probe]` c `sensor_type: hx711`,
   `dout_pin: head0:PD3`, `sclk_pin: head0:PD6`, `sample_rate: 80`,
   `gain: A-128`; `LOAD_CELL_CALIBRATE`; затем `G28 Z`.
   Требуется numpy в `~/klippy-env` (системный numpy 1.26.4 симлинкнут в
   venv; если venv пересоздадут — повторить).
7. Реле/`[host_power]` — после подтверждения GPIO и питания Pi.

### Диагностика

- Лог: `~/printer_data/logs/klippy.log` (ищем `mcu 'mcu'`, `Unable to
  connect`, `Communication timeout during homing`, `Trigger analog error`,
  `HX711`, `Move queue overflow`).
- Размеры: `./scripts/maestro/build_all_mcus.sh` (печатает `Program/Data`).
- Если прошивка не влезает: `make diag` (ELF сохраняется).

## 8. Карта изменённых файлов

- `src/avr/main.c` — `dynmem_end()` через `RAMEND+1`.
- `src/avr/serial.c` — polled slave: без сброса TXC, RXEN/TXC.
- `src/avr/maestro_router.c` — без сброса TXC1.
- `src/avr/Kconfig` — `NEED_TRSYNC`, prompt у `AVR_STACK_SIZE`.
- `src/avr/Makefile` — `--noinhibit-exec`, `-Wl,--relax`, лог линкера.
- `src/trigger_analog.c` — trim под `HAVE_LIMITED_CODE_SIZE`.
- `src/Makefile` — `trsync.c` по `NEED_TRSYNC`, `endstop.c` отдельно.
- `src/Kconfig` — `WANT_DEBUGCMDS`, `WANT_STATS`, `WANT_STEPPER`,
  `WANT_ENDSTOP`, `NEED_TRSYNC`.
- `src/basecmd.c` — `get_uptime`/`STATS_SUMSQ_BASE` всегда.
- `src/generic/serial_irq.c` — TX-буфер 64 (bulk-отчёты HX711).
- `src/stepper.c` — `command_stepper_stop_on_trigger` под `WANT_ENDSTOP`.
- `Makefile` — diag-таргет, `EXTRA`-логика линковки.
- `scripts/check-avr-size.sh`, `scripts/maestro/build_all_mcus.sh`,
  `scripts/maestro/host_multiplexer.py`, `klipper-multiplexer.service`,
  `klippy/extras/host_power.py`, `klippy/extras/trigger_analog.py`.
- `scripts/maestro/configs/config.mcu_*` — профили сборки (не printer.cfg).
- `.gitignore` — `firmware/`.

## 9. Правила работы

- **Не трогать** `/home/klipper/printer_data/config/printer.cfg` и
  AI-примеры конфигов — их составляет пользователь.
- Не трогать `scripts/maestro/configs/` без необходимости: это профили
  сборки, их изменения влияют на прошивку.
- Сначала коммит+пуш, потом `git pull` и сборка на принтере.
- Не доверять старым докам и комментариям — проверять по даташиту,
  реальному `printer.cfg`, рабочему скетчу и живому логу.
- Ничего не прошивать без явной команды пользователя.
