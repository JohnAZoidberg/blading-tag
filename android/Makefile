# Day-to-day build/debug workflow for the Android app, so nobody has to
# remember the adb/gradle incantations (see README.md for the SDK setup).
#
#   make build        gradle assembleDebug
#   make test         run the unit tests (protocol + session file format)
#   make flash        build + install + launch on the connected phone
#   make run          launch the app
#   make stop         force-stop the app (also ends a recording, unsaved tail)
#   make pull         copy recorded sessions from the phone into ../sessions/
#   make screenshot   grab the phone's screen into /tmp, print the path
#   make log          follow the app's crashes/ANRs/log lines
#   make crash        show the most recent crash/ANR
#   make pair CODE=123456   one-time wireless-debugging pairing
#   make connect      attach to the phone over Wi-Fi (after pairing once)
#   make devices      list attached phones
#
# adb is taken from PATH with the ~/Android/Sdk install as fallback. With
# several devices attached, pick one with SERIAL=..., e.g.
#   make screenshot SERIAL=192.168.1.185:33237

ANDROID_HOME ?= $(HOME)/Android/Sdk
ADB ?= $(shell command -v adb 2>/dev/null || echo $(ANDROID_HOME)/platform-tools/adb)

# adb target: explicit SERIAL, else whatever single device is attached.
ADB_S = $(ADB) $(if $(SERIAL),-s $(SERIAL))

APP = app.bladingtag
APK = app/build/outputs/apk/debug/app-debug.apk
# Where RecorderService writes (Context.getExternalFilesDir), readable by adb.
PHONE_SESSIONS = /sdcard/Android/data/$(APP)/files/sessions
SESSIONS ?= ../sessions

.PHONY: help build test flash install run stop pull screenshot log crash connect pair devices

help:
	@sed -n 's/^#   /  /p' Makefile

build:
	./gradlew :app:assembleDebug

test:
	./gradlew :app:testDebugUnitTest

flash: build install run

install:
	$(ADB_S) install -r $(APK)

run:
	$(ADB_S) shell am start -n $(APP)/.MainActivity

stop:
	$(ADB_S) shell am force-stop $(APP)

# Copies every session folder that isn't here yet (an unfinished one, i.e.
# still recording, is copied as far as it got, and again next time). For
# the ones already here it copies session.json, so edits made in the app
# (notes, activity, gear, body location) arrive. One way: the phone wins,
# local edits to session.json are overwritten.
pull:
	@mkdir -p $(SESSIONS)
	@for s in $$($(ADB_S) shell ls $(PHONE_SESSIONS) </dev/null | tr -d '\r'); do \
	  j="$(SESSIONS)/$$s/session.json"; \
	  if [ -e "$$j" ] && grep -qE '"end_(elapsed_ns": *[0-9]|reason")' "$$j"; then \
	    $(ADB_S) pull -q $(PHONE_SESSIONS)/$$s/session.json "$$j.phone" </dev/null || exit 1; \
	    if cmp -s "$$j" "$$j.phone"; then rm "$$j.phone"; \
	    else mv "$$j.phone" "$$j"; echo "$$s  (session.json updated)"; fi; \
	    continue; \
	  fi; \
	  echo "$$s"; $(ADB_S) pull -q $(PHONE_SESSIONS)/$$s $(SESSIONS)/ </dev/null || exit 1; \
	done

# Timestamped so repeated shots during a session don't overwrite each other.
screenshot:
	@out=/tmp/bladingtag-$$(date +%H%M%S).png; \
	$(ADB_S) exec-out screencap -p > $$out && echo $$out

# Follow the signals that matter: crashes, ANRs on this app, our own log
# lines and the BLE stack's view of connections. -T 1 starts at "now".
log:
	$(ADB_S) logcat -T 1 | grep --line-buffered -E \
	  "FATAL EXCEPTION|AndroidRuntime: Caused|Fatal signal|ANR in $(APP)|BladingTag|bladingtag"

# The most recent crash/ANR verdicts, for when the app already died.
crash:
	$(ADB_S) logcat -d -b crash | grep -B2 -A20 "FATAL EXCEPTION" | tail -40 || true
	$(ADB_S) logcat -d | grep -A3 "ANR in $(APP)" | tail -20 || true

# Wireless debugging. Pairing is one-time per machine: enable Developer
# options > Wireless debugging > "Pair device with pairing code" on the
# phone, then `make pair CODE=<the 6 digits>`. From then on `make connect`
# suffices whenever phone and machine share a network - the ports are
# discovered over mDNS, so they may change between sessions without mattering.
connect:
	@if $(ADB) devices | awk 'NR>1 && $$2=="device"' | grep -q .; then \
	  echo "already connected:"; $(ADB) devices | sed 1d; exit 0; \
	fi; \
	port=$$($(ADB) mdns services | awk '/_adb-tls-connect/ {print $$NF}' | head -1); \
	test -n "$$port" || { echo "no phone advertising wireless debugging found (same Wi-Fi? toggle Wireless debugging?)"; exit 1; }; \
	$(ADB) connect $$port

pair:
	@test -n "$(CODE)" || { echo "usage: make pair CODE=123456 (from the phone's pairing dialog)"; exit 1; }
	@port=$$($(ADB) mdns services | awk '/_adb-tls-pairing/ {print $$NF}' | head -1); \
	test -n "$$port" || { echo "no pairing service found - is the pairing dialog open on the phone?"; exit 1; }; \
	$(ADB) pair $$port $(CODE)

devices:
	$(ADB) devices -l
