# blading-tag

A small sensor on your inline skate that records how you skate, so you can see
your push rate and how efficiently you're skating.

It uses a [Nordic nRF54L15 Tag](https://www.nordicsemi.com/Products/Development-hardware/nRF54L15-Tag):
a coin-cell board about the size of a coin, with a motion sensor
(accelerometer + gyroscope) and Bluetooth. You strap the tag to your skate and
skate with your Android phone in your pocket. The phone records the tag's data
together with GPS, and you look at the results afterwards.

> **Status: early.** The plan is done. The firmware, app and analysis are not
> built yet. See [PLAN.md](PLAN.md) for details and progress.

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

## How to use it (once built)

1. **Mount the tag** firmly on the skate: on the frame, or strapped to the
   cuff. Mount it the same way every time.
2. **Open the app** and press start. The tag wakes up when you move the skate,
   and the app connects and starts recording.
3. **Stand still for 2 seconds**, then skate. Keep the phone with you.
4. **Press stop** when you're done. The session is saved on the phone.
5. **Look at the results** in the app, or copy the file to a laptop and run
   the analysis in `host/`.

## Repo layout

| Folder | What's in it |
|---|---|
| [`firmware/`](firmware/) | Firmware for the tag (Zephyr / nRF Connect SDK) |
| [`android/`](android/) | Android app that records the skate session |
| [`host/`](host/) | Laptop tools: Bluetooth recorder, live plot, analysis |

## Building and updating the firmware (planned)

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
