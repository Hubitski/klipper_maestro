# Полная симуляция Klipper + 3 платы на этом ПК (2026-11-25)

Цель: гонять прошивки Maestro (head0/head1/bed, опционально main) вместе с
Klipper и `host_multiplexer.py` без принтера, воспроизводить `Timer too close`,
PID, HX711/тензо и ошибки шины. Ничего не шьётся и не трогает железо.

## 1. Что проверено на этой машине

- Windows 11, Ryzen 9 7945HX (WSL видит 32 CPU), 7 ГБ RAM для WSL, 954 ГБ
  свободно. WSL2 Ubuntu 26.04 уже стоит (сейчас была Stopped, работает).
- В Ubuntu есть пакет **simavr 1.6**. Ядра: `atmega16`, `atmega32`,
  `atmega1284p` — все нужные. CLI принимает `.hex` и ELF, есть `--gdb`.
- `libsimavr-dev`: есть `uart_pty` (PTY для USART), ADC через IRQ
  (`ADC_IRQ_OUT_TRIGGER`, затем `ADC_IRQ_ADCn` в милливольтах) — то есть
  можно подавать «термистор» без правок прошивки.
- **Важно:** simavr в raw-режиме не держит реальное время (спит только когда
  сам AVR спит). Klipper считает clock по 16 МГц, поэтому нужен собственный
  раннер с пейсингом `avr->cycle / 16e6 == wall time`, иначе host-clock
  уедет в прошлое и всё будет падать на `Timer too close` само по себе.
- Python в Ubuntu 26.04 — 3.14; пины Klipper (`Jinja2 2.11.3`,
  `markupsafe 1.1.1`) на нём не соберутся. Решение: отдельный venv на
  **Python 3.9** через `uv` (как на Pi).

## 2. Почему WSL, а не нативный Windows

PTY, termios, `select`, simavr и Klipper host — POSIX. На Windows нет
`/dev/pts`, simavr `uart_pty` не портирован, com0com даёт только пары COM.
WSL2 даёт настоящие PTY, а CPU с запасом: 3–4 инстанса по 16 МГц — это
единицы процентов. Всё живёт в ext4 внутри WSL (не в `/mnt/c`).

## 3. Топология (полностью повторяет железо)

```
klippy (WSL)
 ├─ [mcu]       main  ── /tmp/sim_node_main   ── simavr atmega1284p
 ├─ [mcu head0]       ── /tmp/klipper_mcu_head0 ─┐
 ├─ [mcu head1]       ── /tmp/klipper_mcu_head1 ─┼ host_multiplexer.py
 ├─ [mcu bed]         ── /tmp/klipper_mcu_bed   ─┘        │
 │                                                /tmp/sim_bus_upstream
 │                                                         │
 │                                              sim_bus.py (hub)
 │                                              ├─ /tmp/sim_node_head0 ─ simavr atmega16
 │                                              ├─ /tmp/sim_node_head1 ─ simavr atmega16
 │                                              └─ /tmp/sim_node_bed   ─ simavr atmega16
 └─ [mcu host] ── linux MCU (x86_64) /tmp/klipper_host_mcu
```

- main — напрямую, как в реале (`/dev/ttyS1` → PTY).
- Периферия — только через настоящий `host_multiplexer.py` (тот же код, что
  на Pi), поэтому проверяются парсер, ACK, quarantine, session flush.
- `sim_bus.py` — электрическая модель общей полудуплексной линии:
  * байты от mux → **эхо обратно** в mux (трансивер) + всем трём узлам;
  * байты узла → в mux; если mux в этот момент передаёт — буфер/лог коллизии;
  * `--delay 0x40:0.03` — задержка доставки конкретному узлу (воспроизведение
    «clock уже в прошлом»), `--jitter`, `--drop`;
  * всё логируется в tap-совместимом виде.

## 4. Компоненты, которые надо написать

1. `scripts/maestro/sim/maestro_sim.c` — раннер на libsimavr:
   - загрузка ELF (символы для gdb) или hex;
   - `-m atmega16|atmega1284p -f 16000000`;
   - `uart_pty` → стабильный symlink `/tmp/sim_node_<name>`;
   - **пейсинг**: `avr_run()` + `clock_nanosleep` до `t0 + cycle/16e6`,
     гранулярность ~0.25 мс, спин на добег;
   - ADC-часть: на `ADC_IRQ_OUT_TRIGGER` отдавать `ADC_IRQ_ADCn` (мВ) из
     таблицы `--adc 4=500`;
   - HX711-часть: IRQ на PD6 (SCLK), 25 импульсов, 24 бита на PD3 (DOUT),
     `--hx711 counts=... rate=80`; значение можно менять по ходу (для
     `G28 Z`/пробинга);
   - опционально `--gdb 1234`, `--trace-pins`, VCD.
2. `scripts/maestro/sim/sim_bus.py` — hub из §3 (чистый Python, select).
3. `scripts/maestro/sim/printer-sim.cfg` — отдельный конфиг, реальный
   `printer.cfg` не трогаем: 3–4 MCU на PTY, `kinematics: none` на этапе 1,
   стол/хотэнды/вентиляторы/тензо как в реале.
4. `scripts/maestro/sim/run_sim.sh` — поднимает раннеры, hub, mux, linux
   MCU, klippy; логи в `/tmp/maestro_sim/`; `--with-main`, `--delay-bed`.
5. `scripts/maestro/sim/install_sim_wsl.sh` — `apt install simavr
   libsimavr-dev gcc-avr avr-libc binutils-avr gdb-avr socat python3-venv
   build-essential pkg-config`, `uv` → Python 3.9 venv → `klippy-requirements`,
   сборка linux-MCU и AVR-прошивок.

## 5. Этапы

- **P0 (полдня).** WSL-пакеты, раннер, один head0 напрямую в Klipper.
  Критерий: конфиг проходит, `get_status` отвечает, clock не уезжает
  (Klipper не ругается на `Timer too close` без искусственных задержек).
- **P1 (день).** hub + mux + 3 узла + bed с одним датчиком. Критерий:
  `state ready`, tap без `release: timeout`.
  Регресс бага стола: сборка bed **без** `CONFIG_ADC_QUERY_CLAMP` +
  `--delay 0x40:0.03` → `Timer too close` на конфиге; с флагом — проходит.
  Это проверка фиксов `7d62451`/`e6f0908` без железа.
- **P2.** main atmega1284p + steppers (без TMC — SPI-слейва нет),
  HX711-заглушка, `[load_cell_probe]`, `G28 Z`, `PROBE_CALIBRATE`.
- **P3.** Тепловая модель: ADC-значение зависит от состояния нагревателя
  (PD7/PD5), чтобы `PID_CALIBRATE` давал осмысленную кривую и проверял
  clamp в `heaters.py`.
- **P4.** Инжекция сбоев шины (delay/jitter/drop/overrun), quarantine,
  длинный soak; сравнение с реальным tap.
- **P5.** avr-gdb на `command_query_analog_in`, `sched_add_timer`,
  `analog_in_event`; скриптовый регресс (`run_sim.sh --test bed-query`).

## 6. Риски и решения

- **Python 3.14** → venv 3.9 через `uv` (не трогаем системный Python).
- **gcc-avr 14.2 против 7.3 на Pi**: для логики разницы нет; для точного
  соответствия прошитому hex можно scp-нуть `out/klipper.elf` с Pi (после
  успешной сборки) — simavr грузит ELF с символами. Иначе собираем в WSL.
- **Джиттер WSL** (сон ~50–200 мкс): Klipper clock-sync это терпит; при
  проблемах — спин-пейсинг и/или `--pace 0.1`.
- **RX в simavr доставляется мгновенно** (нет бод-тайминга на приём); TX
  тактируется по циклам. Для протокола это нормально, для 20-мс таймаутов
  mux — даже полезно (проверяем именно доставку/очереди).
- **TMC2130 по SPI не эмулируется** — в sim-конфиге секции TMC не включаем.
- **АТмега16 vs 16A**: simavr ядро `atmega16`, тот же кристалл; регистры
  совпадают (порт/DIDR0 уже учтены в порте).
- **Ресурсы**: 3–4 инстанса + Klipper — меньше 10% CPU; RAM WSL 7 ГБ хватает,
  при желании поднять `.wslconfig`.

## 7. Что нужно от пользователя

1. Разрешение поставить пакеты в WSL (~300 МБ) и `uv` (Python 3.9 venv).
2. Решить, нужен ли main-1284p сразу (P2) или начать с 3 периферийных (P0–P1).
3. Держать принтер/Pi выключенными sim не требует — всё локально.

## 8. Статус 2026-11-25 (вечер): симуляция работает

- Собран и запущен полный стек в WSL2: Klippy + `host_multiplexer.py` +
  `sim_bus.py` + 3 × simavr (ELF с Pi, gcc 7.3) + Linux host MCU.
  Klippy грузит все три MCU: head0/head1 — 62 команды, bed — 26.
- Найдены и обойдены три особенности simavr (подробно в
  `scripts/maestro/sim/README.md`): нет реального времени (свой пейсинг),
  теряются прерывания в `sei; nop; cli` (лишний nop в sim-ELF), не
  поднимается TXC при включении TXCIE (RXEN возвращается раньше в sim-ELF).
  Правки только для симуляции; прошиваемый hex и дерево Pi не меняются.
- Добавлен priming последовательности: прошивка стартует с seq 0, Klippy
  начинает с seq 1 — один валидный seq-0 `get_config` перед подключением.
- Добавлена модель общей шины: байтовые таймслоты 250 кБод, wire-AND при
  одновременной передаче, эхо в mux, per-node delay/jitter/drop, счётчик
  коллизий.
- **Воспроизведён баг стола:** `bed_noqc.elf` (`CONFIG_ADC_QUERY_CLAMP=n`) +
  `BED_DELAY=0.01` → `MCU 'bed' error during config: Timer too close`
  (как на железе). С clamped bed (`CONFIG_ADC_QUERY_CLAMP=y`) этого
  `Timer too close` нет; при искусственной задержке 10 мс позже срабатывает
  другой сторож — `Rescheduled timer in the past` (`timer.c:205`),
  то есть остаётся отдельный вопрос к `analog_in_shutdown`/таймерному списку
  (кандидат из CURRENT_STATE §7.3).
- head1 без query-clamp на конфиге тоже ловит `Rescheduled timer in the past`
  — при задержке шины его query-clock оказывается в прошлом; на железе
  голова конфигурируется, т.к. шина в этот момент свободна.

