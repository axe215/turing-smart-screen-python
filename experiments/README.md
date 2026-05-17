# experiments/

Exploratory scripts for the axe215 fork. Not part of the upstream main app.
Each script is standalone and probes one specific behavior of the Turing 9.2"
screen via the `LcdCommTuringUSB` protocol class.

## How to run

From the repo root, with the venv activated:

```powershell
.\.venv\Scripts\Activate.ps1
python experiments/<script>.py [args]
```

The official `main.py` must NOT be running at the same time — only one process
can hold the USB device.

## Scripts

### `phase1_play_video.py`

Uploads an MP4 to the screen's on-device storage and starts playback via one
of the three known play opcodes.

```powershell
python experiments/phase1_play_video.py rei/eva.rei/video/Finalrei.mp421103329.mp4
```

Outcomes we want to observe:
- **success** — video loops on the screen autonomously (no USB traffic needed)
- **uploaded but nothing plays** — the opcode is wrong; try `--play-cmd 110`
  or `--play-cmd 113`
- **upload fails** — protocol error during file write

See script docstring for full options.
