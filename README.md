# Makeblock XY Plotter GUI

Standalone Python/Tk GUI for loading an SVG, previewing its toolpath, and sending
Makeblock XY Plotter laser G-code over USB serial.

## Run

The GUI is intended for Linux with Python 3 and Tkinter. Its Python modules
use the standard library; no pip packages are required. On Debian/Ubuntu,
Tkinter is provided by the `python3-tk` package.

```bash
git clone https://github.com/Mathew-Hart/xy_plotter_gui.git
cd xy_plotter_gui
python3 xy_plotter_gui.py
```

`launch_xy_plotter_gui.sh` and `makeblock-xy-plotter.desktop` retain the
original Linux installation path, `{insert path here}`. Adjust
those paths if installing the desktop shortcut elsewhere.

The app defaults to the CH340 controller found at:

```text
/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00-port0
```

## Protocol Notes

The controller is running a patched Makeblock mDraw XY firmware. The exact
flashed build is saved at:

```text
firmware/xybot_limit_safe.hex
```

The source copy is saved at `firmware/xybot_limit_safe.ino`. This build keeps
the stock mDraw laser protocol, but aborts normal plotting motion and turns the
laser off if a debounced endstop that was open at the start of a move closes
during that move. It also adds real `G90`/`G91` coordinate-mode handling,
`G92 X0 Y0` position reset support, and immediate `M5` motor setup reloads.

This machine is configured with `A1 B0`, so unmirrored SVG/G-code coordinates
now match the physical left/right orientation after homing.

The firmware accepts:

- `G21` for millimeters
- `G90` for absolute coordinates
- `G91` for relative jogs
- `G92 X0 Y0` to set the current position as zero
- `G0` / `G1 X... Y... F...` for moves
- `M10` to read XY setup
- `M11` to read endstop state
- `M5 A1 B0 H310 W380 S80` to restore the current motor/setup EEPROM values
- `M4 0` for laser off
- `M4 255` for laser on at full power

The GUI defaults to binary laser mode:

```text
M4 0
G1 X... Y... F...
M4 255
G1 X... Y... F...
M4 0
```

Legacy `Z servo` and `M1 servo` modes are still present for non-laser firmware,
but they are not the default.

## Safety

Dry-run is enabled by default. Non-dry-run laser-on commands require the
`Arm laser` checkbox. The GUI sends `M4 0` before command sequences and after
cancels/errors when connected in laser mode.

The GUI refuses to plot or save G-code if the transformed SVG coordinates fall
outside the configured machine area, and out-of-bounds preview paths are drawn
red.

Generated plot jobs end with `M4 0`, `G28`, `M4 0`, so the laser is off while
the tool returns to the home switches after a completed cut.

Closed contours also get a default 3 mm overcut. After the path returns to its
start point, the laser continues along the beginning of the contour before
turning off, making the closure gap negative.
