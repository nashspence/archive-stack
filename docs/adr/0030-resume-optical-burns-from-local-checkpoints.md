# ADR-0030: Resume Optical Burns from Local Checkpoints

## Decision

The optical burn workflow resumes from staged ISO, burned-media verification, or label-confirmation checkpoints when possible.
Simulated non-writing burns do not create copy checkpoints because no physical media has been written.

## Reason

Physical media work is slow, stateful, and failure-prone; retrying from the earliest safe point avoids unnecessary rework.
