# host

Python tools that run on a laptop.

**Not written yet.** Phases 4 and 6 in [../PLAN.md](../PLAN.md).

Planned:

- **Recorder:** connects to the tag over BLE (bleak), logs to a file, and
  plots live. Used to check the firmware before the Android app exists.
- **Analysis:** reads session files (from the recorder or the Android app) and
  computes push detection, push rate, push/glide split, distance per push and
  fatigue drift, with plots.
