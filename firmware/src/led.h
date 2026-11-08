/*
 * RGB status LED. Patterns are short blips to keep the average current low.
 */
#ifndef BLADING_LED_H_
#define BLADING_LED_H_

#include <stdbool.h>
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
	/*
	 * Recording into the buffer, no link: yellow blip every second. Red
	 * instead once the buffer is full and samples are being dropped
	 * (led_set_dropping), i.e. the phone never joined or lost the tag.
	 */
	LED_MODE_BUFFERING,
};

void led_init(void);
void led_set_mode(enum led_mode mode);
/* Samples are being dropped: LED_MODE_BUFFERING blips red. Cheap to call often. */
void led_set_dropping(bool dropping);
/* Light `colors` for ms, then go back to the mode pattern. */
void led_flash(uint8_t colors, uint16_t ms);
/* Blink blue for seconds (0 stops). */
void led_identify(uint8_t seconds);

#endif /* BLADING_LED_H_ */
