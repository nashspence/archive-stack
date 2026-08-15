# Issue 522 validation

Base: a2cabb8af3c7d552ac7f484af06d4cf9dafa1c3d
Workflow source: cee45f274b24a92fc3ae003b633679f145fe5fe5

Focused collection-workflow tests: passed

## make lint

Exit status: 2

```text
439 |     )
440 |     replace_once(
441 |         path,
    -         '''            if active is not None:\n                if not secrets.compare_digest(active.challenge, supplied_challenge):\n                    raise Conflict("collection deletion challenge does not match active deletion")\n                plan = cast(dict[str, object], json.loads(active.plan_json))\n''',
    -         '''            if active is not None:\n                if not secrets.compare_digest(active.challenge, supplied_challenge):\n                    raise Conflict("collection deletion challenge does not match active deletion")\n                plan = cast(dict[str, object], json.loads(active.plan_json))\n                expected_retirement = _retirement_claim_id(plan)\n                if expected_retirement != retirement_claim_id:\n                    raise Conflict("collection deletion retirement claim changed")\n''',
442 +         """            if active is not None:\n                if not secrets.compare_digest(active.challenge, supplied_challenge):\n                    raise Conflict("collection deletion challenge does not match active deletion")\n                plan = cast(dict[str, object], json.loads(active.plan_json))\n""",
443 +         """            if active is not None:\n                if not secrets.compare_digest(active.challenge, supplied_challenge):\n                    raise Conflict("collection deletion challenge does not match active deletion")\n                plan = cast(dict[str, object], json.loads(active.plan_json))\n                expected_retirement = _retirement_claim_id(plan)\n                if expected_retirement != retirement_claim_id:\n                    raise Conflict("collection deletion retirement claim changed")\n""",
444 |     )
445 |     replace_once(
446 |         path,
    -         '''                plan = _build_plan(session, collection_id=normalized_id, expires_at=expires)\n                if not secrets.compare_digest(\n''',
    -         '''                retirement = None\n                if retirement_claim_id is not None:\n                    retirement = require_retirement_exemption(\n                        session,\n                        claim_id=retirement_claim_id,\n                        collection_id=normalized_id,\n                        principal=initiator,\n                    )\n                plan = _build_plan(\n                    session,\n                    collection_id=normalized_id,\n                    expires_at=expires,\n                    exempt_claim_id=retirement_claim_id,\n                )\n                plan["retirement_claim"] = retirement\n                if not secrets.compare_digest(\n''',
447 +         """                plan = _build_plan(session, collection_id=normalized_id, expires_at=expires)\n                if not secrets.compare_digest(\n""",
448 +         """                retirement = None\n                if retirement_claim_id is not None:\n                    retirement = require_retirement_exemption(\n                        session,\n                        claim_id=retirement_claim_id,\n                        collection_id=normalized_id,\n                        principal=initiator,\n                    )\n                plan = _build_plan(\n                    session,\n                    collection_id=normalized_id,\n                    expires_at=expires,\n                    exempt_claim_id=retirement_claim_id,\n                )\n                plan["retirement_claim"] = retirement\n                if not secrets.compare_digest(\n""",
449 |     )
--------------------------------------------------------------------------------
456 |         path,
    -         '''def _build_plan(\n    session: Session,\n    *,\n    collection_id: int,\n    expires_at: datetime,\n) -> dict[str, object]:\n''',
    -         '''def _build_plan(\n    session: Session,\n    *,\n    collection_id: int,\n    expires_at: datetime,\n    exempt_claim_id: str | None = None,\n) -> dict[str, object]:\n''',
457 +         """def _build_plan(\n    session: Session,\n    *,\n    collection_id: int,\n    expires_at: datetime,\n) -> dict[str, object]:\n""",
458 +         """def _build_plan(\n    session: Session,\n    *,\n    collection_id: int,\n    expires_at: datetime,\n    exempt_claim_id: str | None = None,\n) -> dict[str, object]:\n""",
459 |     )
--------------------------------------------------------------------------------
467 |         "def _active_blockers(session: Session, collection_id: int) -> list[str]:\n",
    -         '''def _active_blockers(\n    session: Session,\n    collection_id: int,\n    *,\n    exempt_claim_id: str | None = None,\n) -> list[str]:\n''',
468 +         """def _active_blockers(\n    session: Session,\n    collection_id: int,\n    *,\n    exempt_claim_id: str | None = None,\n) -> list[str]:\n""",
469 |     )
470 |     replace_once(
471 |         path,
472 |         "    blockers: list[str] = []\n",
    -         '''    blockers: list[str] = processing_claim_blockers(\n        session, collection_id, exempt_claim_id=exempt_claim_id, limit=_BLOCKER_SAMPLE_LIMIT\n    )\n''',
473 +         """    blockers: list[str] = processing_claim_blockers(\n        session, collection_id, exempt_claim_id=exempt_claim_id, limit=_BLOCKER_SAMPLE_LIMIT\n    )\n""",
474 |     )
475 |     append_once(
476 |         path,
477 |         "def _retirement_claim_id",
    -         '''def _retirement_claim_id(plan: dict[str, object]) -> str | None:\n    value = plan.get("retirement_claim")\n    if not isinstance(value, dict):\n        return None\n    claim_id = value.get("claim_id")\n    return str(claim_id) if claim_id else None''',
478 +         """def _retirement_claim_id(plan: dict[str, object]) -> str | None:\n    value = plan.get("retirement_claim")\n    if not isinstance(value, dict):\n        return None\n    claim_id = value.get("claim_id")\n    return str(claim_id) if claim_id else None""",
479 |     )
--------------------------------------------------------------------------------
483 |         path,
    -         '''def plan_collection_deletion(\n    collection_id: int,\n    container: ContainerDep,\n    principal: CollectionDeleter,\n) -> CollectionDeletionPlanOut:\n''',
    -         '''def plan_collection_deletion(\n    collection_id: int,\n    container: ContainerDep,\n    principal: CollectionDeleter,\n    retirement_claim_id: str | None = None,\n) -> CollectionDeletionPlanOut:\n''',
484 +         """def plan_collection_deletion(\n    collection_id: int,\n    container: ContainerDep,\n    principal: CollectionDeleter,\n) -> CollectionDeletionPlanOut:\n""",
485 +         """def plan_collection_deletion(\n    collection_id: int,\n    container: ContainerDep,\n    principal: CollectionDeleter,\n    retirement_claim_id: str | None = None,\n) -> CollectionDeletionPlanOut:\n""",
486 |     )
487 |     replace_once(
488 |         path,
489 |         "        container.collection_deletions.plan(collection_id)\n",
    -         '''        container.collection_deletions.plan(\n            collection_id,\n            principal=principal,\n            retirement_claim_id=retirement_claim_id,\n        )\n''',
490 +         """        container.collection_deletions.plan(\n            collection_id,\n            principal=principal,\n            retirement_claim_id=retirement_claim_id,\n        )\n""",
491 |     )
492 |     replace_once(
493 |         path,
    -         '''            initiator=principal,\n            event_context=request.event_context,\n''',
    -         '''            initiator=principal,\n            event_context=request.event_context,\n            retirement_claim_id=request.retirement_claim_id,\n''',
494 +         """            initiator=principal,\n            event_context=request.event_context,\n""",
495 +         """            initiator=principal,\n            event_context=request.event_context,\n            retirement_claim_id=request.retirement_claim_id,\n""",
496 |     )
--------------------------------------------------------------------------------
500 |         path,
    -         '''    def plan_collection_deletion(self, collection_id: int) -> dict[str, Any]:\n        return self._json(\n            "POST",\n            f"/v1/collections/{str(collection_id)}/deletion-plan",\n        )\n''',
    -         '''    def plan_collection_deletion(\n        self,\n        collection_id: int,\n        *,\n        retirement_claim_id: str | None = None,\n    ) -> dict[str, Any]:\n        params = (\n            {"retirement_claim_id": retirement_claim_id}\n            if retirement_claim_id is not None\n            else None\n        )\n        return self._json(\n            "POST",\n            f"/v1/collections/{str(collection_id)}/deletion-plan",\n            params=params,\n        )\n''',
501 +         """    def plan_collection_deletion(self, collection_id: int) -> dict[str, Any]:\n        return self._json(\n            "POST",\n            f"/v1/collections/{str(collection_id)}/deletion-plan",\n        )\n""",
502 +         """    def plan_collection_deletion(\n        self,\n        collection_id: int,\n        *,\n        retirement_claim_id: str | None = None,\n    ) -> dict[str, Any]:\n        params = (\n            {"retirement_claim_id": retirement_claim_id}\n            if retirement_claim_id is not None\n            else None\n        )\n        return self._json(\n            "POST",\n            f"/v1/collections/{str(collection_id)}/deletion-plan",\n            params=params,\n        )\n""",
503 |     )
504 |     replace_once(
505 |         path,
    -         '''        challenge: str,\n        event_context: Mapping[str, Any] | None = None,\n    ) -> dict[str, Any]:\n        payload: dict[str, Any] = {"challenge": challenge}\n''',
    -         '''        challenge: str,\n        retirement_claim_id: str | None = None,\n        event_context: Mapping[str, Any] | None = None,\n    ) -> dict[str, Any]:\n        payload: dict[str, Any] = {"challenge": challenge}\n        if retirement_claim_id is not None:\n            payload["retirement_claim_id"] = retirement_claim_id\n''',
506 +         """        challenge: str,\n        event_context: Mapping[str, Any] | None = None,\n    ) -> dict[str, Any]:\n        payload: dict[str, Any] = {"challenge": challenge}\n""",
507 +         """        challenge: str,\n        retirement_claim_id: str | None = None,\n        event_context: Mapping[str, Any] | None = None,\n    ) -> dict[str, Any]:\n        payload: dict[str, Any] = {"challenge": challenge}\n        if retirement_claim_id is not None:\n            payload["retirement_claim_id"] = retirement_claim_id\n""",
508 |     )
--------------------------------------------------------------------------------
514 |     for signature in [
    -         '    response_model=ProcessingClaimPageOut,\n)',
    -         '    response_model=ProcessingClaimOut,\n)\ndef get_processing_claim',
    -         '    response_model=ProcessingClaimOut,\n)\ndef begin_processing_claim_retirement',
    -         '    response_model=ProcessingClaimOut,\n)\ndef release_processing_claim',
    -         '    response_model=CollectionDerivationOut,\n)\ndef get_collection_derivation',
515 +         "    response_model=ProcessingClaimPageOut,\n)",
516 +         "    response_model=ProcessingClaimOut,\n)\ndef get_processing_claim",
517 +         "    response_model=ProcessingClaimOut,\n)\ndef begin_processing_claim_retirement",
518 +         "    response_model=ProcessingClaimOut,\n)\ndef release_processing_claim",
519 +         "    response_model=CollectionDerivationOut,\n)\ndef get_collection_derivation",
520 |     ]:
--------------------------------------------------------------------------------
533 |     path = "docs/architecture.md"
    -     section = '''## Collection workflow model\n\nFinalized Riverhog collections are the only durable payload units. Minimal protocol\nadapters create collections and retain only bounded transient custody until the\nfinalized root receipt. Jeb is a payload-free, tag-targeted transformation\ncontroller. It freezes exact immutable input roots, owns fenced claims, verifies\nderived collections from Riverhog, and separately orchestrates optional retirement.\nMunchy is a content-aware collection transform executor: one finalized collection\nset and one sealed intent produce exactly one finalized collection on success.\nTargets own any bounded encrypted or ephemeral payload workspace.\n\nTags select work but are never transform identity. Processing claims bind exact\nmanifest and content identities. Scoped transform capabilities never expose archive\npassphrases, broad S3 credentials, archive-key selection, or deletion. Every derived\ncollection carries immutable input-root, plan, execution, disposition, and\nprovenance evidence. Active or retiring claims participate in Riverhog deletion\nsafety.\n'''
534 +     section = """## Collection workflow model\n\nFinalized Riverhog collections are the only durable payload units. Minimal protocol\nadapters create collections and retain only bounded transient custody until the\nfinalized root receipt. Jeb is a payload-free, tag-targeted transformation\ncontroller. It freezes exact immutable input roots, owns fenced claims, verifies\nderived collections from Riverhog, and separately orchestrates optional retirement.\nMunchy is a content-aware collection transform executor: one finalized collection\nset and one sealed intent produce exactly one finalized collection on success.\nTargets own any bounded encrypted or ephemeral payload workspace.\n\nTags select work but are never transform identity. Processing claims bind exact\nmanifest and content identities. Scoped transform capabilities never expose archive\npassphrases, broad S3 credentials, archive-key selection, or deletion. Every derived\ncollection carries immutable input-root, plan, execution, disposition, and\nprovenance evidence. Active or retiring claims participate in Riverhog deletion\nsafety.\n"""
535 |     append_once(path, "## Collection workflow model", section)
    |

unformatted: File would be reformatted
   --> scripts/riverhog_ftp_adapter.py:1:1
    |
108 |             )
    -             batches.append(
    -                 ClaimedBatch(source, str(payload["batch_id"]), batch_root, files)
    -             )
109 +             batches.append(ClaimedBatch(source, str(payload["batch_id"]), batch_root, files))
110 |         return batches
--------------------------------------------------------------------------------
141 |         for row in self._eligible(source):
    -             if selected and (len(selected) >= source.max_files or total + row[2] > source.max_bytes):
142 +             if selected and (
143 +                 len(selected) >= source.max_files or total + row[2] > source.max_bytes
144 +             ):
145 |                 break
--------------------------------------------------------------------------------
150 |         identity = "\n".join(f"{row[1]}\0{row[2]}\0{row[3]}\0{row[4]}" for row in selected)
    -         batch_id = hashlib.sha256(
    -             f"{source.id}\0{identity}".encode("utf-8")
    -         ).hexdigest()
151 +         batch_id = hashlib.sha256(f"{source.id}\0{identity}".encode("utf-8")).hexdigest()
152 |         batch_root = self._claims_root(source) / batch_id
    |

12 files would be reformatted, 504 files already formatted
make: *** [Makefile:158: format-check] Error 1
```

## make compile

Exit status: 0

```text
```

## make unit

Exit status: 2

```text

==================================== ERRORS ====================================
_____ ERROR collecting riverhog/server/tests/test_collection_workflows.py ______
import file mismatch:
imported module 'test_collection_workflows' has this __file__ attribute:
  /home/runner/work/riverhog/riverhog/packages/riverhog-protocol/tests/test_collection_workflows.py
which is not the same as the test file we want to collect:
  /home/runner/work/riverhog/riverhog/riverhog/server/tests/test_collection_workflows.py
HINT: remove __pycache__ / .pyc files and/or use a unique basename for your test file modules
=========================== short test summary info ============================
ERROR riverhog/server/tests/test_collection_workflows.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 7.84s
make: *** [Makefile:171: unit] Error 2
```

## make spec

Exit status: 0

```text
..                                                                       [100%]
2 passed in 1.91s
```

