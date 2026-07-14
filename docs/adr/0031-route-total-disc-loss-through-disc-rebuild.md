# ADR-0031: Route Total Disc Loss Through Disc Rebuild

## Decision

Riverhog handles finalized images with no usable discs through disc rebuild archive restores, not the ordinary replacement-burn backlog.

## Reason

Once every usable disc is lost or damaged, the system must rebuild from collection archives before more physical discs can be trusted.
