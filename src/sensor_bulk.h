#ifndef __SENSOR_BULK_H
#define __SENSOR_BULK_H

#include "autoconf.h" // CONFIG_SENSOR_BULK_DATA_SIZE
#ifndef CONFIG_SENSOR_BULK_DATA_SIZE
#define CONFIG_SENSOR_BULK_DATA_SIZE 51
#endif

struct sensor_bulk {
    uint16_t sequence, possible_overflows;
    uint8_t data_count;
    uint8_t data[CONFIG_SENSOR_BULK_DATA_SIZE];
};

void sensor_bulk_reset(struct sensor_bulk *sb);
void sensor_bulk_report(struct sensor_bulk *sb, uint8_t oid);
void sensor_bulk_status(struct sensor_bulk *sb, uint8_t oid
                        , uint32_t time1, uint32_t query_ticks, uint32_t fifo);

#endif // sensor_bulk.h
