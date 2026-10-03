"""IMU/GNSS fusion for sessions recorded by the Yard Survey Android app.

Works on a session folder as the app records it (`raw/gnss.bin`,
`raw/imu.bin`, `raw/cube.ulg`, `events.jsonl`), not on the export, because
the fusion needs the raw streams and their arrival times.

- `ulog`: PX4's log format, read into numpy record arrays.
- `clock`: the Cube's clock onto the phone's (TIMESYNC round trips) and the
  phone's onto GNSS time (GGA arrivals).
- `rawsession`: GNSS epochs, phone IMU and Cube IMU, all on UTC seconds.
- `checks`: what the fusion depends on, measured before any filter is built:
  clock agreement, rig geometry, sensor noise and biases, vibration.

Nothing here prints a position: reports carry counts, rates, times, angles
and offsets between devices.
"""
