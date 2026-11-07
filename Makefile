.PHONY: help workspace build build-incremental build-test ota-test test-hw ota ota-confirm image-state reset flash clean

# Configuration (override with `make VAR=value target`)
BOARD ?= nrf54l15tag/nrf54l15/cpuapp
TOOLCHAIN_IMAGE ?= ghcr.io/nrfconnect/sdk-nrf-toolchain:v3.4.0
# Bluetooth address of the tag, for `make ota` etc.
TAG ?= C0:BB:4A:A7:0D:57
SMPMGR ?= uvx smpmgr
UV ?= uv

FIRMWARE_DIR := firmware
BUILD_DIR := $(FIRMWARE_DIR)/build
APP_SIGNED_BIN := $(BUILD_DIR)/firmware/zephyr/zephyr.signed.bin
MERGED_HEX := $(BUILD_DIR)/merged.hex
DFU_ZIP := $(BUILD_DIR)/dfu_application.zip

CONTAINER_RUNTIME := $(shell command -v podman 2>/dev/null || command -v docker 2>/dev/null)
ifneq ($(findstring podman,$(CONTAINER_RUNTIME)),)
CONTAINER_USER := --userns=keep-id
VOLUME_OPTS := :Z
else
CONTAINER_USER := --user $(shell id -u):$(shell id -g)
VOLUME_OPTS :=
endif

# The image's entrypoint is `bash -c`, so the command is passed as one string.
CONTAINER_RUN = $(CONTAINER_RUNTIME) run --rm $(CONTAINER_USER) \
	-v $(CURDIR):/workdir/project$(VOLUME_OPTS) -w /workdir/project -e HOME=/tmp \
	$(TOOLCHAIN_IMAGE)

help:
	@echo "Firmware targets (build steps run in $(TOOLCHAIN_IMAGE)):"
	@echo "  make workspace          west init + update (once, and after west.yml changes)"
	@echo "  make build              pristine sysbuild: MCUboot + app"
	@echo "  make build-incremental  rebuild without -p"
	@echo "  make ota                upload the app over BLE in test mode and reset"
	@echo "  make image-state        show the tag's image slots"
	@echo "  make reset              reset the tag over SMP"
	@echo "  make flash              flash MCUboot + app over J-Link (recovery only)"
	@echo "  make clean              remove the build directory"
	@echo ""
	@echo "  BOARD=$(BOARD)"
	@echo "  TAG=$(TAG)"
	@echo "  runtime: $(CONTAINER_RUNTIME)"

check-runtime:
	@test -n "$(CONTAINER_RUNTIME)" || { echo "podman or docker is required"; exit 1; }

workspace: check-runtime
	$(CONTAINER_RUN) 'git config --global --add safe.directory "*" && \
		(test -d .west || west init -l $(FIRMWARE_DIR)) && \
		west update -o=--depth=1 -n'

build: check-runtime
	$(CONTAINER_RUN) 'cd $(FIRMWARE_DIR) && west build -p -b $(BOARD) --sysbuild -d build'
	@echo "App image: $(APP_SIGNED_BIN)"

build-incremental: check-runtime
	$(CONTAINER_RUN) 'cd $(FIRMWARE_DIR) && west build -b $(BOARD) --sysbuild -d build'

# Hardware tests (host/tests/hw/README.md): a test build with the J-Link
# mailbox (firmware/test.conf) in its own build directory, installed over BLE.
TEST_BUILD_DIR := $(FIRMWARE_DIR)/build-test
TEST_SIGNED_BIN := $(TEST_BUILD_DIR)/firmware/zephyr/zephyr.signed.bin
JLINK ?= 1051834948

build-test: check-runtime
	$(CONTAINER_RUN) 'cd $(FIRMWARE_DIR) && west build -p -b $(BOARD) --sysbuild -d build-test -- -Dfirmware_EXTRA_CONF_FILE=test.conf'
	@echo "Test image: $(TEST_SIGNED_BIN)"

ota-test:
	$(UV) run --script host/ota.py $(TAG) $(TEST_SIGNED_BIN)

test-hw:
	cd host && BLADETAG_HW=$(TAG) BLADETAG_JLINK=$(JLINK) .venv/bin/python -m pytest tests/hw -v

$(APP_SIGNED_BIN):
	@echo "$@ not found, run 'make build' first"; exit 1

# Upload in test mode: MCUboot swaps it in once, and the new firmware confirms
# itself after Bluetooth is up. If it doesn't, the next reset reverts it.
# host/ota.py checks every step (`smpmgr upgrade` can report success without
# the image ending up pending) and retries the flaky first BLE connect.
ota: $(APP_SIGNED_BIN)
	$(UV) run --script host/ota.py $(TAG) $(APP_SIGNED_BIN)

image-state:
	$(SMPMGR) --ble $(TAG) --timeout 10 image state-read

reset:
	$(SMPMGR) --ble $(TAG) --timeout 10 os reset

flash:
	nrfutil device program --firmware $(MERGED_HEX) --options chip_erase_mode=ERASE_ALL
	nrfutil device reset

clean:
	rm -rf $(BUILD_DIR)
