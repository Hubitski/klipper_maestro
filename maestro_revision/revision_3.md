# Технический аудит и ревизия №3 реализации Klipper для Maestro Grand 2 IDEX

**Дата:** 25 сентября 2026 г.  
**Статус:** Критические дефекты ревизий 1–2 (протокол, эхо, арбитраж, deadlock) устранены корректно. Выявлены **5 остаточных дефектов** среднего и низкого приоритета, препятствующих надежной работе в отдельных сценариях.  
**Цель документа:** Финальный аудит после Ревизии 2 (Этап 7) — верификация ранее заявленных исправлений и обнаружение пропущенных проблем.

---

## 1. Верификация исправлений Ревизий 1 и 2

### Подтверждено как корректно исправленное:

| Дефект | Файл(ы) | Вердикт |
|:---|:---|:---|
| Коллизии на шине при `ret == -1` | `serial_irq.c:86–91` | ✅ `serial_enable_tx_irq()` строго внутри `if (ret > 0)` |
| Интерливинг байтов в роутере | `serial.c:114–174` | ✅ `tx0_owner` + декрементный `tx0_remaining` обеспечивают атомарную передачу целых кадров |
| Спонтанный NAK на шине | `command.c:356–358` | ✅ В `CONFIG_SERIAL_POLLED_SLAVE` все ошибочные пути возвращают `-1` без вызова `nak:` |
| Baud rate 250000 (pyserial) | `host_multiplexer.py:252–254` | ✅ Основной путь использует `serial.Serial(port, 250000)` |
| Подавление эха MCP2561 (матплата) | `maestro_router.c:74–77, 157–185` | ✅ `RXEN1=0` при TX, реинициализация в `USART1_TX_vect` |
| Подавление эха MCP2561 (слейвы) | `serial.c:192–211` | ✅ `RXENx=0` при TX, реинициализация в `USARTx_TX_vect` |
| Ложная демаркация по `0x7E` (роутер) | `maestro_router.c:132–145` | ✅ `STATE_PAYLOAD` считает по `frame_cnt`, не проверяет `data == 0x7E` |
| Ложная демаркация по `0x7E` (арбитр `tx0_owner`) | `serial.c:122–125, 138–140` | ✅ Владение линией по декременту `tx0_remaining`, не по `0x7E` |
| Deadlock при рассинхронизации seq | `command.c:315–321` | ✅ NAK с `next_sequence` при валидном CRC и совпадении DEST, но несовпадении seq |
| Арбитраж полудуплексной шины на хосте | `host_multiplexer.py:241–246, 384–494` | ✅ Конечный автомат `bus_busy/bus_active_dest` с FIFO и таймаутом 20 мс |
| `POLLHUP` — пересоздание PTY | `host_multiplexer.py:197–209, 312–318, 372–374` | ✅ `handle_channel_hup` → `reopen()` с re-register в poll |
| Безопасный хоуминг Z | `printer.cfg:238–258` | ✅ `[homing_override]` сдвигает T0 в центр стола перед касанием |
| SPI пины для ATmega16 | `spi.c:22–25` | ✅ Ветка `CONFIG_MACH_atmega16` наравне с `atmega644p/1284p` |
| I2C пины для ATmega16 | `i2c.c:22–24` | ✅ Ветка `CONFIG_MACH_atmega16` наравне с `atmega644p/1284p` |
| Hard PWM отключен для ATmega16 | `Kconfig:12` | ✅ `select HAVE_GPIO_HARD_PWM if !MACH_atmega16` |
| SOFTWARE_SPI в конфиге материнки | `config.mcu_motherboard_1284p:27` | ✅ `CONFIG_WANT_SOFTWARE_SPI=y` |
| Timer регистры для ATmega16 | `timer.c:15–20` | ✅ Алиасы `TIMSK1→TIMSK`, `TIFR1→TIFR` |
| ADC `DIDR0` защита | `adc.c:93–95` | ✅ `#if defined(DIDR0)` |
| UART URSEL для ATmega16 | `serial.c:91–92` | ✅ `#if defined(URSEL)` с установкой бита |

---

## 2. Выявленные остаточные дефекты

### Дефект №3.1 (СРЕДНИЙ): Проверка переполнения FIFO роутера — мертвый код

* **Файл:** `src/avr/maestro_router.c`, строки 33–42.
* **Причина:**

  ```c
  #define ROUTER_BUF_SIZE 256

  static inline int
  fifo_put(struct router_fifo *f, uint8_t b)
  {
      uint8_t h = f->head;
      if ((uint8_t)(h - f->tail) >= ROUTER_BUF_SIZE)   // << ВСЕГДА false
          return -1;
      ...
  }
  ```

  `ROUTER_BUF_SIZE` = 256. Переменные `head` и `tail` имеют тип `uint8_t`, максимальное значение которого — 255. Выражение `(uint8_t)(h - f->tail)` возвращает значение в диапазоне 0..255. Условие `>= 256` **не может быть истинным** — проверка является мертвым кодом.

* **Историческая причина:** В ревизии 2 размер буфера был увеличен со 128 до 256 байт. При 128 байтах проверка `>= 128` корректно срабатывала для `uint8_t`. После увеличения до 256 условие стало принципиально невыполнимым.

* **Следствие:** Если потребитель не успевает забирать данные и в FIFO накапливается 256 байт, `head` циклически достигает значения `tail` (оба `uint8_t` wrapped). Функция `fifo_get()` видит `head == tail` и считает буфер пустым. Весь буфер «теряется», пакет к хосту обрезается.

* **Практический риск:** Низкий при нормальной работе (оба USART работают на одной скорости 250 кбод, потребление почти мгновенное). Однако при передаче словаря идентификации Klipper (множество коротких пакетов подряд от хоста к периферии) буфер `fifo_u0_to_u1` может заполниться быстрее, чем USART1 передает данные (если одновременно шина занята ответом от другой платы).

* **Исправление:**

  ```c
  #define ROUTER_BUF_SIZE 256
  #define ROUTER_BUF_USABLE (ROUTER_BUF_SIZE - 1)  // = 255

  static inline int
  fifo_put(struct router_fifo *f, uint8_t b)
  {
      uint8_t h = f->head;
      if ((uint8_t)(h - f->tail) >= ROUTER_BUF_USABLE)
          return -1; // Full: max 255 of 256 slots used
      f->buf[h & ROUTER_BUF_MASK] = b;
      f->head = h + 1;
      return 0;
  }
  ```

  Полезная ёмкость становится 255 байт (достаточно для 3+ полных пакетов Klipper по 64 байта), а FIFO корректно различает состояния «полон» и «пуст».

---

### Дефект №3.2 (СРЕДНИЙ): `[homing_override]` падает при вызове `G28 Z` без предварительного хоуминга X и Y

* **Файл:** `scripts/maestro/printer_maestro_grand2_idex.cfg`, строки 238–258.
* **Причина:**

  ```
  {% set home_all = 'X' not in params and 'Y' not in params and 'Z' not in params %}
  {% if home_all or 'Z' in params %}
      T0
      G90
      G1 X200 Y200 F6000   ← Требует, чтобы X и Y были отхоумлены!
      G28 Z
  {% endif %}
  ```

  При вызове `G28 Z` (без X и Y) переменная `home_all` = `False`. Блоки хоуминга X и Y пропускаются. Блок Z выполняется и сразу вызывает макрос `T0`, который внутри делает `PARK_TOOLHEAD1` → `G1 X445` — попытка перемещения по неотхоумленной оси.

* **Следствие:** Klipper выдает ошибку `Must home axis first: 250.000 200.000 0.000 [0.000]` и аварийно останавливается. Пользователь или макрос, делающий `G28 Z` после ручного перемещения стола, получает ошибку.

* **Исправление:**

  ```
  {% if home_all or 'Z' in params %}
      # Ensure X and Y are homed before Z probe can move to center
      {% if "x" not in printer.toolhead.homed_axes %}
          G28 X
      {% endif %}
      {% if "y" not in printer.toolhead.homed_axes %}
          G28 Y
      {% endif %}
      T0
      G90
      G1 X200 Y200 F6000
      G28 Z
      G1 Z10 F1200
  {% endif %}
  ```

---

### Дефект №3.3 (СРЕДНИЙ): Резервный путь `termios` в `host_multiplexer.py` по-прежнему использует 230400 бод

* **Файл:** `scripts/maestro/host_multiplexer.py`, строка 271.
* **Причина:**

  ```python
  except ImportError:
      # Fallback to direct termios if pyserial is not installed
      ...
      baud_const = getattr(termios, f"B{self.baud_rate}",
                           getattr(termios, "B230400", termios.B115200))
  ```

  Это **тот самый баг**, выявленный в Ревизии 1 (Дефект №4). Ревизия 2 корректно перевела основной путь на `pyserial`, но **резервный** (`except ImportError`) остался нетронутым. Константа `B250000` отсутствует в стандартном модуле `termios` Python — фоллбек молча откатывается на **230400 бод** (рассогласование 8.5% при допуске UART ≤ 2.5%).

* **Практический риск:** Pyserial входит в зависимости Klipper и должен быть установлен всегда. Но при чистой установке вне пакета Klipper, при сборке в Docker-контейнере или при запуске мультиплексора отдельно от Klipper (для отладки) pyserial может отсутствовать.

* **Исправление:** В фоллбек-ветке использовать `termios2` / `ioctl TCSETS2` для кастомного baud rate, либо поднять `ImportError` до фатальной ошибки:

  ```python
  except ImportError:
      logging.critical(
          "pyserial is required for 250000 baud support. "
          "Install with: pip3 install pyserial"
      )
      raise SystemExit(1)
  ```

---

### Дефект №3.4 (НИЗКИЙ): Макрос `M106` не обрабатывает параметр `P` (индекс вентилятора)

* **Файл:** `scripts/maestro/printer_maestro_grand2_idex.cfg`, строки 271–279.
* **Причина:**

  ```
  [gcode_macro M106]
  gcode:
      {% set speed = params.S|default(255)|float / 255.0 %}
      {% if printer.toolhead.extruder == "extruder1" %}
          SET_FAN_SPEED FAN=part_fan_1 SPEED={speed}
      {% else %}
          SET_FAN_SPEED FAN=part_fan_0 SPEED={speed}
      {% endif %}
  ```

  Макрос игнорирует параметр `P` (номер вентилятора). Некоторые слайсеры (PrusaSlicer, Simplify3D) в режиме двух экструдеров генерируют `M106 P0 S255` и `M106 P1 S128` для раздельного управления обдувом конкретного хотенда, не привязываясь к текущему активному экструдеру.

* **Следствие:** В двухматериальных печатях слайсер может включить обдув только на активном экструдере, независимо от параметра `P`. Обдув неактивного хотенда игнорируется.

* **Исправление:**

  ```
  [gcode_macro M106]
  gcode:
      {% set speed = params.S|default(255)|float / 255.0 %}
      {% if params.P is defined %}
          {% if params.P|int == 1 %}
              SET_FAN_SPEED FAN=part_fan_1 SPEED={speed}
          {% else %}
              SET_FAN_SPEED FAN=part_fan_0 SPEED={speed}
          {% endif %}
      {% else %}
          {% if printer.toolhead.extruder == "extruder1" %}
              SET_FAN_SPEED FAN=part_fan_1 SPEED={speed}
          {% else %}
              SET_FAN_SPEED FAN=part_fan_0 SPEED={speed}
          {% endif %}
      {% endif %}
  ```

---

### Дефект №3.5 (НИЗКИЙ): Макросы `T0`/`T1` не идемпотентны — лишние движения при повторном вызове

* **Файл:** `scripts/maestro/printer_maestro_grand2_idex.cfg`, строки 291–320.
* **Причина:**

  Макрос `T0`:
  ```
  SET_DUAL_CARRIAGE CARRIAGE=1
  PARK_TOOLHEAD1               ← Перемещает каретку 1 на X=445
  SET_DUAL_CARRIAGE CARRIAGE=0
  ACTIVATE_EXTRUDER EXTRUDER=extruder
  ```

  При вызове `T0`, когда T0 и так активен, макрос активирует каретку 1, двигает её в парковку (X=445), затем переключается обратно на каретку 0. Это ~1 секунда впустую + риск столкновения, если каретка 1 уже была в парковке и не была отозвана.

* **Исправление:**

  ```
  [gcode_macro T0]
  gcode:
      {% if printer.toolhead.extruder != "extruder" %}
          SET_DUAL_CARRIAGE CARRIAGE=1
          PARK_TOOLHEAD1
          SET_DUAL_CARRIAGE CARRIAGE=0
          ACTIVATE_EXTRUDER EXTRUDER=extruder
          SET_GCODE_OFFSET X=0 Y=0 Z=0
      {% endif %}
  ```

  Аналогично для `T1`: проверка `printer.toolhead.extruder != "extruder1"`.

---

## 3. Итоговая оценка

| Приоритет | Количество | Область | Блокирует установку? |
|:---|:---|:---|:---|
| КРИТИЧЕСКИЙ | 0 | — | — |
| СРЕДНИЙ | 3 | Прошивка (FIFO), Конфиг (homing), Хост (baud fallback) | Частично — `G28 Z` вызовет ошибку |
| НИЗКИЙ | 2 | Конфиг (M106, T0/T1) | Нет |

**Общий вердикт:** Ядро системы (протокол, маршрутизация, арбитраж, подавление эха, портирование ATmega16A) реализовано корректно и полностью соответствует архитектурному документу. Дефекты ревизий 1 и 2 подтверждены как устранённые.

Оставшиеся 5 дефектов не являются show-stoppers при стандартном использовании (`G28` без параметров, один активный экструдер). Однако дефекты №3.1 (FIFO), №3.2 (homing_override) и №3.3 (baud fallback) рекомендуются к исправлению до установки на реальный принтер — они проявятся при нестандартных сценариях (перезапуск связи, ручной `G28 Z`, запуск без pyserial).

---

## 4. Пошаговый план исправления (Action Plan)

### Этап 1: Прошивка MCU
1. **`src/avr/maestro_router.c`**: Исправить проверку переполнения FIFO: `>= ROUTER_BUF_SIZE` → `>= (ROUTER_BUF_SIZE - 1)`.

### Этап 2: Хостовый мультиплексор
2. **`scripts/maestro/host_multiplexer.py`**: Заменить fallback `termios` на фатальную ошибку `SystemExit(1)` с инструкцией установки pyserial.

### Этап 3: Конфигурация принтера
3. **`printer_maestro_grand2_idex.cfg`**: 
   - В `[homing_override]` добавить проверку `printer.toolhead.homed_axes` перед перемещением по X/Y в блоке Z.
   - В `[gcode_macro M106]` добавить обработку параметра `P`.
   - В `[gcode_macro T0]` и `[gcode_macro T1]` добавить проверку активного экструдера для идемпотентности.

### Этап 4: Тестирование
4. Дополнить `test_multiplexer.py` тестом на поведение при заполнении FIFO до 255 элементов (граница переполнения).
