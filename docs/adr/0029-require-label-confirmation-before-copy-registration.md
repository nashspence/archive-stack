# ADR-0029: Require Label Confirmation Before Copy Registration

## Decision

Riverhog does not register or count a burned disc copy until the operator confirms the generated label was applied.

Blank-media insertion, prepared-content verification, writing, and burned-media verification do not show or record the generated label. The label first appears at the Label Checkpoint after burned-media verification passes.

## Reason

Physical protection depends on a recoverable labeled artifact, not merely on a successful burn command.
