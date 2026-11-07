# blading-tag

A small sensor on your inline skate that records how you skate, so you can see
your push rate and how efficiently you're skating.

It uses a [Nordic nRF54L15 Tag](https://www.nordicsemi.com/Products/Development-hardware/nRF54L15-Tag):
a coin-cell board about the size of a coin, with a motion sensor
(accelerometer + gyroscope) and Bluetooth. You strap the tag to your skate and
skate with your Android phone in your pocket. The phone records the tag's data
together with GPS, and you look at the results afterwards.

> **Status: early.** The firmware streams the motion data over Bluetooth and
> updates over Bluetooth. The laptop recorder works with a real tag. The
> Android app and the analysis are built but only tested with simulated data,
> and there hasn't been a real skate session yet. See [PLAN.md](PLAN.md) for
> details and progress.

## What it will do

- **Push rate:** strokes per minute, over the whole session and per km.
- **Efficiency:** distance per push, how much of each stroke is glide vs. push,
  and how your speed relates to your push rate.
- **Fatigue:** how your pushes change from the start of a long skate to the
  end.
- **With a second tag (one per foot):** left vs. right balance.
- **Later:** technique, such as edge angle and push direction.

The tag only records when the app tells it to. When it's not in use it sleeps,
draws almost nothing, and wakes up when the skate moves. One CR2032 coin cell
should last for many sessions.

## How to use it

1. **Mount the tag** firmly on the skate: on the frame, or strapped to the
   cuff. Mount it the same way every time.
2. **Open the app** and press start. The tag wakes up when you move the skate,
   and the app connects and starts recording.
3. **Stand still for 2 seconds**, then skate. Keep the phone with you.
4. **Press stop** when you're done. The session is saved on the phone.
5. **Look at the results:** share the session from the app (or copy it over
   adb with `make -C android pull`) and run the analysis in `host/` on a
   laptop. The app doesn't show results itself yet.

## The button

The firmware uses one button on the tag (`sw0`). The board file lists a
second one, but it's disabled there and the firmware doesn't use it.

| Do this | What happens | LED |
|---|---|---|
| **Short press** while not recording | Starts recording with the default settings (200 Hz, ±16 g, ±2000 °/s), no phone needed | Green flash; red flash if it can't start (sensor fault, or still finishing the last recording) |
| **Short press** while recording | Stops recording, whether the app or the button started it | Red flash |
| **Hold 2 s**, then let go | Goes to sleep (System OFF). Bluetooth turns off and recording stops | Long red flash at 2 s, then 3 short red blinks |
| **Press** while asleep | Wakes the tag. It restarts and advertises again. This press doesn't start a recording | Short green flash at startup |

Some details:

- Sleep only starts when you **let go**. A button that's still held down
  would wake the tag right away again.
- A recording started with the button works like one started from the app.
  The tag keeps the last ~41 s in its buffer until the app connects and picks
  the session up. Without a connection it stops by itself after 5 minutes.
- **Sleep doesn't fully work yet**: the tag sometimes wakes up again by itself
  after a few seconds, and waking on motion isn't working. See
  [firmware/README.md](firmware/README.md#power-states-status).

The other LED patterns: a green blip every 2 s means recording and connected,
a yellow blip every 1 s means recording without a connection (into the
buffer), and blue blinking means the app asked the tag to identify itself.

## Repo layout

| Folder | What's in it |
|---|---|
| [`firmware/`](firmware/) | Firmware for the tag (Zephyr / nRF Connect SDK) |
| [`android/`](android/) | Android app that records the skate session |
| [`host/`](host/) | Laptop tools: Bluetooth recorder, live plot, analysis |

The parts talk to each other through the Bluetooth protocol and session file
format in [PROTOCOL.md](PROTOCOL.md).

## Building and updating the firmware

You only need podman or docker. The nRF Connect SDK toolchain runs in a
container, so nothing else needs to be installed.

```sh
make build   # build bootloader + app in the container
make ota     # update the tag over Bluetooth from the laptop
```

You can also update from your phone with the **nRF Connect Device Manager**
app, using `dfu_application.zip` from the build. A J-Link (`make flash`) is
only needed to recover a tag that won't boot.

See [`firmware/README.md`](firmware/README.md) for details.
