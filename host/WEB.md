# Local recording library

Launch from the repository root after installing the host tools:

```sh
host/.venv/bin/python -m bladetag.web --import sessions
```

Open **http://127.0.0.1:8765**. On subsequent launches, omit `--import`;
recordings and reports remain in `~/.local/share/bladetag`. Use `--library PATH`
for a different storage location or `--port 9000` for a different port.
The server listens only on this computer. No phone or tag is needed.

Upload one or more Android/recorder session ZIP exports with **Import ZIPs**.
The launch-time `--import` option accepts session folders, ZIPs, or a parent
folder of recordings. Imports are copied into the managed library, so moving
original recordings does not break reports. Identical ZIP bytes are deduplicated;
a differently packed copy of the same session can appear twice.

Walking, cycling and skating reports are queued automatically. One analysis
runs at a time in a separate process; the library stays responsive. Select
recordings and choose **Generate view** for a report, raw-signal overview,
walking/cycling/skating 3D replay, or walking/skating comparison. Each recording
also has explicit **Report**, **3D replay**, and **Raw signals** actions; an
unbuilt view has a Generate button, queued views show progress, and completed
views have an Open link. Raw overviews show
the first tag in a recording. Comparisons require
recordings of the same activity; cycling, walking 3D and raw views take one recording. Skating 3D also supports
multiple selected skating recordings.
Unknown activity recordings support the raw overview but are not automatically
classified. Analysis failures show their details and a Retry button.

Completed views are cached, including comparisons, and survive server restarts.
Repeated requests for the same selection reuse the saved result. Interrupted
jobs are marked failed on restart and can be retried. Reports use the analysis
code installed when they were generated; to rebuild everything after a code
upgrade, launch with a fresh `--library` directory and import again.

**Open** displays a cached interactive page. **Download offline** saves that
page with its data embedded: send the HTML file or open it locally without the
server or internet. These views reuse the existing report renderers and retain
their quality checks and scientific limitations. The skating 3D view embeds the pinned Three.js renderer and orbit controls
(with their MIT license), and uses system fonts, so it also works offline.

The library owns its recordings, generated HTML and job logs in one directory;
there is no need to manage individual output files. Back up that entire directory
to preserve it. Uploads are limited to 512 MiB compressed / 2 GiB uncompressed;
ZIPs are read without filesystem extraction. This is a single-user local tool,
not an authenticated network service.
