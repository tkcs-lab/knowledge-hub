# Deviations: usage-log

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-schema-and-settings

- None.

## p2-event-logging

- worker: usage.py reads the cursor key file inside the try block, so an unreadable key counts as a dropped event and never raises; the plan code did not say where the read goes.

## p3-usage-tool

- pending

## p4-retention

- pending

## p5-docs-and-retirement

- pending

## Final review

- pending

## Finishing

- pending
