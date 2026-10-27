# AGENTS.md

# fabCNC

This repository contains software used to control a physical CNC fabric-cutting machine.

Because this software controls real hardware, correctness and reliability are more important than stylistic improvements.

Agents working in this repository should treat motion-control, serial-communication, homing, coordinate-state, and machine-state bugs as potentially high severity.

---

## System Overview

The machine uses:

- Raspberry Pi 5 as the host computer
- BTT SKR Pro v1.2 controller board
- Marlin firmware on the SKR Pro
- Raspberry Pi GPIO UART connection (`/dev/ttyAMA0`, 115200 baud) to SKR Pro UART3
- TMC5160 stepper drivers
- NEMA 23 stepper motors
- 24 V motor/control system
- Rack-and-pinion motion system
- Module 1.0 rack
- 16-tooth pinion gears
- Sensorless homing where configured

The host software sends G-code commands to Marlin over the hardware UART.

IMPORTANT:

Do not assume this description completely describes the software architecture.

Before making architectural conclusions, inspect the repository and determine:

- What processes/services exist
- Where G-code originates
- Where G-code is generated or transformed
- How commands are queued
- How serial communication is implemented
- How Marlin responses are parsed
- How acknowledgements are associated with commands
- How machine state is tracked
- How homing is implemented
- How disconnect/reconnect is handled
- How jobs are started, stopped, paused, or resumed
- Whether multiple components can communicate with Marlin
- How frontend/UI commands reach the machine-control layer

Trace the actual implementation rather than relying on this document.

---

# Primary Engineering Priorities

In order of importance:

1. Physical machine safety
2. Correct machine motion
3. Reliable G-code delivery
4. Correct machine/coordinate state
5. Reliable serial communication
6. Correct failure/recovery behavior
7. Deterministic behavior
8. Maintainability
9. Performance
10. Code style

Do not recommend stylistic refactors unless they materially improve correctness, reliability, testability, or maintainability.

---

# Critical Machine Invariants

These should be treated as important system invariants.

## Command integrity

A G-code command must not be:

- silently modified
- truncated
- combined with another command
- duplicated
- skipped
- reordered

unless the protocol explicitly requires the behavior.

The command logged by the application should accurately represent the command transmitted to Marlin.

Pay particular attention to transformations that occur between:

requested operation
→ generated G-code
→ queued command
→ serialized bytes
→ Marlin

---

## Command acknowledgement

Do NOT assume that receiving:

    ok

means the physical machine completed the requested movement.

When reviewing the implementation, determine exactly what Marlin's acknowledgement means at that point in the command lifecycle.

Look for situations where application state advances before physical motion has completed.

Also inspect whether an `ok` can ever be incorrectly associated with a different command.

---

## Serial ownership

There should ideally be one clearly defined owner of the serial connection.

Search the repository for every location that can:

- open the serial device
- write to serial
- read from serial
- close the connection
- reconnect
- flush buffers
- reset the controller

Flag any situation where multiple threads, processes, services, or objects could write to or consume responses from the same connection without explicit coordination.

---

## Command queues

Carefully inspect all queues and buffers.

Look for:

- commands surviving reconnects unexpectedly
- stale commands
- duplicate sends
- retry-after-success
- commands removed before acknowledgement
- commands removed on timeout even though Marlin accepted them
- queue state disagreeing with controller state
- race conditions between sender and receiver
- job cancellation leaving queued commands
- commands from an old job entering a new job
- unbounded queues

---

# Serial Disconnect / Reconnect

Serial reliability is a known area requiring careful review.

Inspect behavior when:

- Serial disconnects
- the device disappears
- the device reappears
- reads time out
- writes time out
- Marlin resets
- Raspberry Pi reconnects
- malformed data is received
- partial lines are received
- communication stops during a long operation
- communication stops during homing

Determine what happens to:

- the currently transmitted command
- acknowledged commands
- unacknowledged commands
- queued commands
- machine position
- application position
- homing state
- job state

NEVER assume that retrying a motion command after an uncertain communication failure is safe.

A timeout does not necessarily prove that Marlin failed to receive or execute a command.

---

# Homing

Homing is safety-critical.

Review:

- homing command generation
- homing timeout behavior
- sensorless homing configuration
- long-running homing behavior
- communication during homing
- reconnect behavior during homing
- application state after homing
- Marlin position/state after homing
- premature sensorless-homing triggers

Pay special attention to any generic serial timeout that may be shorter than a legitimate homing operation.

Do not assume a long-running operation means communication has failed.

---

# Coordinate and Motion State

Trace all code affecting machine coordinates.

Pay particular attention to:

- G90 / absolute positioning
- G91 / relative positioning
- G20 / inches
- G21 / millimeters
- G92 / position changes
- M92 / steps-per-unit configuration
- workspace offsets
- machine offsets
- homing offsets
- scaling
- transformations
- calibration values
- persisted settings

A persistent or runtime state change may affect later commands even when those later commands themselves are correct.

Search for every place that can change these states.

---

# Motion Scaling

Unexpected deterministic scaling is considered a high-priority failure mode.

If commanded distances differ consistently from actual movement, investigate:

- M92
- steps/mm
- gear/pinion configuration
- unit conversion
- coordinate transformations
- application-level scaling
- calibration factors
- integer/float conversions
- rounding
- persisted Marlin EEPROM settings
- initialization commands
- commands sent after reconnect
- commands sent after homing

Do not assume that valid checksums or successful `ok` responses prove that the semantic G-code was correct.

---

# Marlin State

The application should not assume Marlin state unless that state is explicitly established or queried.

When relevant, investigate whether state could survive or change across:

- application restart
- reconnect
- controller reset
- machine power cycle
- job cancellation
- homing
- previous jobs

Look for hidden dependencies on previous G-code commands.

---

# Position Verification

When diagnosing motion problems, distinguish between:

1. Application-requested position
2. G-code commanded position
3. Marlin logical position
4. Expected physical position
5. Actual physical position

These are NOT automatically equivalent.

Where appropriate, use Marlin position reporting such as M114 to verify controller state.

Do not describe M114 as proof of actual physical position; it reports the controller's logical position.

---

# Logging Requirements

When modifying or reviewing logging around motion, prefer enough information to reconstruct the exact command lifecycle.

Useful information includes:

- timestamp
- job ID
- command sequence ID
- original requested operation
- generated G-code
- exact outgoing command
- exact bytes written when appropriate
- Marlin response
- acknowledgement
- timeout
- retry
- reconnect
- controller reset
- reported position

Logs should make it possible to answer:

"What exactly did the application intend to do?"

"What exactly did it send?"

"What response did Marlin return?"

"What did the application believe happened next?"

---

# Concurrency

Treat concurrency around machine control as high risk.

Inspect:

- threads
- async tasks
- subprocesses
- callbacks
- timers
- background workers
- UI-triggered actions

Look for multiple execution paths capable of changing:

- serial state
- command queues
- machine state
- job state
- position state

Prefer explicit ownership and synchronization.

---

# Error Handling

Never silently swallow exceptions related to:

- serial communication
- motion
- homing
- parsing
- job execution
- machine state

Flag broad exception handlers that convert an uncertain machine state into an apparent success state.

Physical-machine software should generally fail safely when controller state becomes uncertain.

---

# Testing Philosophy

Prioritize tests for real failure modes.

Important tests include:

- serial disconnect during command transmission
- serial disconnect after transmission but before acknowledgement
- reconnect with queued commands
- long-running homing
- timeout during homing
- duplicate acknowledgement
- delayed acknowledgement
- malformed response
- partial serial response
- controller reset
- application restart
- cancelled job
- relative/absolute mode changes
- units changes
- M92 changes
- position-state disagreement
- command ordering
- duplicate command prevention

Where hardware is unavailable, mock the Marlin serial interface and simulate these conditions.

---

# Code Review Instructions

When asked to review this repository:

1. First map the architecture.
2. Identify all executable entry points.
3. Identify all serial communication code.
4. Identify every source of G-code.
5. Identify every command queue.
6. Identify every component tracking machine state.
7. Trace a normal motion command end-to-end.
8. Trace homing end-to-end.
9. Trace disconnect/reconnect end-to-end.
10. Trace job cancellation/restart behavior.

Do not review files only in isolation.

Follow calls across modules.

Search for all callers before deciding a function is incorrect.

---

# Finding Standards

Do not flood reviews with hypothetical issues.

For each finding, attempt to disprove it before reporting it.

Classify findings as:

- CONFIRMED BUG
- POTENTIAL RISK
- NEEDS TESTING
- SAFE

For reported problems include:

- Severity
- Confidence
- File
- Function/class
- Relevant line(s)
- Exact failure mechanism
- Realistic sequence that triggers it
- Consequence
- Recommended fix
- Test that would prove the fix

Prefer five well-supported findings over fifty speculative findings.

---

# Changes

Unless explicitly instructed otherwise:

DO NOT immediately modify production code after finding an issue.

First:

1. Explain the finding.
2. Show the execution path.
3. Explain the failure mechanism.
4. Attempt to reproduce or test it.
5. Propose the smallest safe fix.

Wait for explicit instruction before implementing significant changes.

Avoid unrelated refactoring while fixing machine-control bugs.

---

# Repository Discovery

If this repository contains documentation that conflicts with assumptions in this file, investigate the actual implementation.

Update your understanding based on:

- README files
- configuration
- tests
- deployment scripts
- service definitions
- source code

Do not invent missing architecture.

When uncertain, state what is unknown and identify the code or runtime evidence needed to determine it.

---

# Verified Repository Architecture

The following describes the implementation verified in October 2026. It is a
navigation aid, not a substitute for inspecting the current code after future
changes.

## Runtime ownership

- `cnc_ui/main.py` is the deployed NiceGUI entry point.
- `setup.sh` installs one `fabcnc.service` process for machine control.
- `cnc_ui/cnc/controller.py` creates the single process-global `CNCController`.
- Only `CNCController` may open, read, write, flush, close, or reconnect the
  Marlin serial device.
- Serial operations are transactions protected by `operation_lock`; synchronous
  exchanges explicitly pause the background reader and wait for it to yield
  response ownership.
- Do not add direct `serial.Serial` use or bypass `_exchange_locked` /
  `_write_serial_payload` from UI, toolpath, or file-processing code.

## Production job path

The normal cutting path is:

DXF upload
→ `DXFProcessor`
→ browser canvas transformations
→ an immutable per-client shape/settings snapshot
→ a per-request `ToolpathGenerator`
→ per-client G-code snapshot and SHA-256 digest
→ `CNCController.start_job`
→ normalized ASCII commands
→ acknowledged numbered/checksummed `M28` upload
→ `M29`
→ `M20` verification
→ `M23 FABJOB.GCO`
→ `M24`
→ controller-local SD playback
→ `M27` progress and `M114` logical-position polling.

An upload acknowledgement proves acceptance into the SD-write transaction. An
`M24` acknowledgement proves acceptance of the start command. Neither proves
physical motion completed.

Short utility sequences and disconnect recovery still use the numbered host
streamer. These paths must fail closed on missing or ambiguous acknowledgements;
never synthesize acknowledgement progress or continue after a timeout.

## Machine-state safety rules

- Manual G-code, XY zeroing, jogging, utility moves, and new jobs are rejected
  unless the controller is in a known safe state for that operation.
- A multi-command operation must reserve the machine before starting its worker
  thread so two UI callbacks cannot both observe Idle.
- Relative-mode sequences are atomic. `G91`, motion, `G90`, completion barrier,
  and state restoration must never interleave with another operation.
- Operations that must return with the machine stationary require an
  acknowledged `M400`; serial silence is not success.
- Homing uses operation-specific long timeouts and must not set `homed=True`
  until its acknowledgements and completion barrier succeed.
- Missing or ambiguous delivery/acknowledgement enters `serial_uncertain` state.
  Motion remains blocked until explicit stopped-machine recovery and re-homing.
- If an SD print might still be active, preserve its recovery marker. Do not
  clear it merely because pause, resume, stop, or status acknowledgement was
  lost.
- Manual console commands must remain blocked during upload, playback, pause,
  homing, reconnect, and uncertain state. This includes apparently harmless
  modal/query additions unless their response ownership is explicitly safe.

## Browser and job identity

NiceGUI serves multiple browser clients in one process. Do not store the
reviewed G-code only in a mutable process-global variable. The job started by a
client must come from that client's immutable generated snapshot. Retain the
G-code digest and controller job ID in lifecycle logs so preview, upload,
responses, and completion can be correlated.

## Recovery expectations

- UART loss during SD playback may leave Marlin cutting autonomously.
- Never retry a motion command merely because its acknowledgement was lost.
- Reconnect reconciles a possible SD print with `M27` rather than replaying it.
- An uncertain non-SD stream is stopped and requires operator verification.
- Controller reset or communication recovery invalidates homing and requires a
  new full home before ordinary motion or a new job.

## Required validation

Run the safety regression suite after any controller, UI-control, queue,
homing, or toolpath-lifecycle change:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile cnc_ui/cnc/controller.py cnc_ui/main.py tests/test_controller_safety.py
git diff --check
```

Tests that touch controller logic must use a fake Marlin serial interface and
must not require connected hardware. Add regression coverage for every fixed
failure sequence, especially stale acknowledgements, reader handoff,
disconnect-after-write, resend bursts, active-job command rejection, and
multi-client job identity.

Passing mocked tests does not replace supervised hardware validation. Before
deployment, verify the flashed Marlin configuration, sensorless homing,
steps-per-unit, SD pause/resume/stop semantics, UART reconnect behavior,
controller reset behavior, and physical E-stop operation on the actual SKR Pro
machine.

---

# Commit and Push Workflow

Whenever committing and pushing repository changes, increment the application
revision number in `cnc_ui/logging_setup.py` (`APP_VERSION`) and include that
revision bump in the same commit.
