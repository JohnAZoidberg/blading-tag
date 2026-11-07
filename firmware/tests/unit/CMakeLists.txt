# SPDX-License-Identifier: Apache-2.0
#
# Host unit tests for the hardware-independent parts of the app, built for
# Zephyr's `unit_testing` board (a plain host binary):
#   west build -b unit_testing --no-sysbuild firmware/tests/unit -d firmware/build-unit
#   firmware/build-unit/testbinary
cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr COMPONENTS unittest REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(blading_unit)

target_sources(testbinary PRIVATE
  src/test_proto.c
  src/test_samplebuf.c
  ../../src/proto.c
  ../../src/samplebuf.c
)
target_include_directories(testbinary PRIVATE ../../src)
