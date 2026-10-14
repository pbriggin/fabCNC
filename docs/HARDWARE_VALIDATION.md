# Supervised Hardware Validation

Run this checklist after controller, firmware, wiring, or motion-setting changes.
Software tests do not establish physical safety.

## Preconditions

- Two people present: one operator and one observer at the physical E-stop.
- Cutter removed or Z constrained above the material for initial motion tests.
- Clear table, known-good 24 V supply, and current Marlin configuration archived.
- USB/DTR reset behavior and controller SD card available for observation.
- Record application, controller, and toolpath logs for the entire session.

## Procedure

1. Verify the physical E-stop removes hazardous motion and Marlin reports its
   halted state. Confirm software cannot clear it without operator recovery.
2. Cold-start the Pi and SKR Pro. Confirm the UI starts disconnected or idle,
   never homed, and rejects jog/job commands until a full successful home.
3. Home each axis individually, then all axes. Verify direction, sensorless
   trigger repeatability, configured sensitivity, 180-second timeout behavior,
   logical `M114` coordinates, and that `M400` is acknowledged before Idle.
4. Command measured 10, 100, and 500 mm X/Y moves and measured Z/A moves.
   Compare requested G-code, Marlin logical position, and physical travel.
   Verify units, `M92`, pinion/rack configuration, and EEPROM values.
5. Run a non-cutting square and curved/bulged-polyline fixture. Verify shape
   dimensions, A-axis tangent direction, corner lifts, safe Z, and return home.
6. During separate sacrificial jobs, disconnect USB: during SD upload, after
   `M24` write/before acknowledgement, during playback, during pause, and during
   `M27` polling. Confirm the UI never reports stopped solely because USB was
   lost and preserves the SD recovery marker.
7. Reconnect after each fault. Confirm only an explicit `M27` active report
   resumes monitoring and only explicit `Not SD printing` clears the marker.
8. Test Pause, Resume, and Stop. Verify Marlin's actual `M25`, `M24`, `M524`,
   and `M410` semantics and that lost acknowledgements require physical
   verification rather than optimistic state changes.
9. Reset/power-cycle Marlin during idle, homing, upload, and playback. Confirm
   homing is invalidated and ordinary motion remains blocked until recovery.
10. Open two authenticated browsers. Load and transform different files,
    generate both previews, and confirm each starts exactly its own SHA-256
    identified G-code snapshot with its own settings and filename.

For every failure, preserve logs and record commanded position, `M114` logical
position, measured physical position, firmware response, and operator action.
Do not deploy cutting operation until every applicable item passes.
