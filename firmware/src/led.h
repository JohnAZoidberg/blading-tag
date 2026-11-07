/*
 * RGB status LED. Patterns are short blips to keep the average current low.
 */
#ifndef BLADING_LED_H_
#define BLADING_LED_H_

#include <stdint.h>
#include <zephyr/sys/util.h>

#define LED_RED    BIT(0)
#define LED_GREEN  BIT(1)
#define LED_BLUE   BIT(2)
#define LED_YELLOW (LED_RED | LED_GREEN)

enum led_mode {
	LED_MODE_OFF,
	/* Recording and connected: green blip every 2 s. */
	LED_MODE_RECORDING,
	/* Recording into the buffer, no link: yellow blip every second. */
	LED_MODE_BUFFERING,
};

void led_init(void);
void led_set_mode(enum led_mode mode);
/* Light `colors` for ms, then go back to the mode pattern. */
void led_flash(uint8_t colors, uint16_t ms);
/* Blink blue for seconds (0 stops). */
void led_identify(uint8_t seconds);

#endif /* BLADING_LED_H_ */
