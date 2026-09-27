#ifndef __MAESTRO_ROUTER_H
#define __MAESTRO_ROUTER_H

#include <stdint.h>

void maestro_router_rx0(uint8_t data);
int maestro_router_get_tx0(uint8_t *pdata);
int maestro_router_has_tx0(void);

#endif // __MAESTRO_ROUTER_H
