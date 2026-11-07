# SPDX-License-Identifier: Apache-2.0
cmake_minimum_required(VERSION 3.20.0)

# App devicetree additions (ADC, ADXL367 INT1). The board overlay in boards/
# (flash layout) is still picked up automatically.
list(APPEND EXTRA_DTC_OVERLAY_FILE ${CMAKE_CURRENT_LIST_DIR}/blading.overlay)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(blading_tag)

target_sources(app PRIVATE
  src/main.c
  src/app.c
  src/ble.c
  src/proto.c
  src/samplebuf.c
  src/imu.c
  src/motion.c
  src/battery.c
  src/led.c
)
target_sources_ifdef(CONFIG_BLADING_TEST_HOOKS app PRIVATE src/testhook.c)
