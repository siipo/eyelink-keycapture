# EyeLink Key Capture

Records EyeLink data while the participant reads images in another program, such as a DICOM viewer. When the participant presses the response key, the program does three things:

- writes a `KEYPRESS` message to the EDF;
- saves a native-resolution screenshot of the tracked screen;
- adds a Data Viewer `!V IMGLOAD` message, so gaze is shown over exactly what was on screen at the press.

Each case is one trial in Data Viewer. One session (EDF file) can hold any number of cases.

## Download

Get `EyeLinkKeyCapture.zip` from the [Releases](https://github.com/siipo/eyelink-keycapture/releases) page. It contains the ready-built program, so you don't need Python. `QUICKSTART.txt` inside the zip is a one-page guide for lab staff.

## Setup on the Display PC

1. Copy the whole `EyeLinkKeyCapture` folder to the Display PC. Nothing else needs to be installed: the EyeLink DLLs are included. Use a short path such as `C:\EyeLinkKeyCapture`. In a deeply nested folder, Windows fails to load the DLLs with "The filename or extension is too long".
2. Connect the Display PC to the Host PC by Ethernet, as for WebLink. Set `host_ip` in `config.ini` if it isn't `100.1.1.1`.
3. Make sure Windows runs the 4200×2800 screen at its native resolution. The program finds that screen automatically (`monitor = auto`).
4. Antivirus or IT tools may flag the program, because it listens to the keyboard globally. If they do, whitelist the folder.

## Running a session

1. Start the EyeLink Host software, then run `EyeLinkKeyCapture.exe`.
2. Type the participant/session ID. Use letters, digits and `_`. The Host PC keeps only the first 8 characters, but the saved EDF keeps the full ID.
3. **Calibrate:** the calibration screen opens on the 4200×2800 screen. Press Enter for the camera image, C to calibrate, V to validate, then Esc to finish. You can also drive this from the Host PC. To change how far the targets spread across the screen, set `calibration_area_horizontal` and `calibration_area_vertical` in `config.ini` (percent of screen width and height; EyeLink defaults 88 and 83).
4. Bring the DICOM viewer to the front.
5. Use these keys while the viewer has focus:

| Key | Default | Action |
|---|---|---|
| Case key | `2` | Starts the next case (a new trial). Also saves a screenshot of the start of the case. |
| Response key | `1` | Participant marks a finding: EDF message plus screenshot. |
| Recalibrate | `F9` | Ends the current case and opens calibration. Afterwards, press the case key to continue. |
| Quit | `F10` twice | Ends the case, stops recording, transfers the EDF and exits. |

All keys can be changed in `config.ini`. The number keys on the numpad also work. The viewer receives the key press too, so choose keys the viewer ignores.

## Output

Each session gets its own folder, `data\<ID>_<date>_<time>\`:

```
<ID>_<date>_<time>.edf    EDF recording
images\case01_start.png   screen when case 1 started
images\case01_kp001.png   screen at keypress 1 (in case 1)
keypresses.csv            every case start and keypress, with image file names
session.log               console log
```

Keep the EDF and the `images` folder together. Data Viewer finds the images by path relative to the EDF.

## EDF messages

| Message | Meaning |
|---|---|
| `TRIALID n` | start of case n |
| `CASE_START n` | case n recording started |
| `KEYPRESS k case n` | response key press number k (counted across the whole session) |
| `!V IMGLOAD FILL images/...png` | background image for Data Viewer from this point on |
| `!V TRIAL_VAR case n`, `!V TRIAL_VAR keypresses k` | trial variables |
| `CASE_END n`, `TRIAL_RESULT 0` | end of case n |

Messages carry the key-press time: if the program sends a message a few milliseconds after the key went down, the message has an offset that Data Viewer subtracts.

## Analysis in Data Viewer

- Open the EDF. Each case is a trial. The background switches to each keypress screenshot at the moment of that press.
- To get the gaze that belongs to a press, take the **last fixation that ends within 500 ms before the `KEYPRESS` message**. Participants often look away as they press. Data Viewer can do this with an interest period that ends at `KEYPRESS` and starts 500 ms earlier, plus a fixation report.
- Draw interest areas on the finding in each keypress screenshot. Then check whether that fixation falls inside the interest area.

## Testing without a tracker

Set `dummy_mode = true` in `config.ini`. The keys, screenshots and CSV work as normal, but no gaze data is recorded and the program writes no EDF.

## Building the .exe

**In the browser (no local Python needed):** GitHub Actions builds the program on GitHub's Windows machines.
- Edit files directly on github.com: open a file and click the pencil icon.
- For a test build, go to **Actions › Build › Run workflow**. When the run finishes, the program can be downloaded from the run page under *Artifacts*.
- To publish a new version, go to **Releases › Draft a new release**, create a new tag such as `v1.1` and publish it. The workflow builds `EyeLinkKeyCapture.zip` and attaches it to the release.

**Locally:** on a Windows x64 PC with Python 3.12 installed, run `build.bat`. It creates `dist\EyeLinkKeyCapture\`.
