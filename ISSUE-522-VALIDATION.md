# Issue 522 validation

Base: a2cabb8af3c7d552ac7f484af06d4cf9dafa1c3d
Workflow source: 1731c9bb713f5912b758af2f48ac35751d2c412b

Focused collection-workflow tests: passed

## make lint

Exit status: 2

```text
E501 Line too long (170 > 100)
   --> scripts/issue_522_apply.py:473:101
    |
471 | …
472 | …
473 | …    session, collection_id, exempt_claim_id=exempt_claim_id, limit=_BLOCKER_SAMPLE_LIMIT\n    )\n""",
    |                                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
474 | …
475 | …
    |

E501 Line too long (265 > 100)
   --> scripts/issue_522_apply.py:478:101
    |
476 | …
477 | …
478 | …et("retirement_claim")\n    if not isinstance(value, dict):\n        return None\n    claim_id = value.get("claim_id")\n    return str(claim_id) if claim_id else None""",
    |       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
479 | …
    |

E501 Line too long (169 > 100)
   --> scripts/issue_522_apply.py:484:101
    |
482 | …
483 | …
484 | …  container: ContainerDep,\n    principal: CollectionDeleter,\n) -> CollectionDeletionPlanOut:\n""",
    |                                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
485 | …  container: ContainerDep,\n    principal: CollectionDeleter,\n    retirement_claim_id: str | None = None,\n) -> CollectionDeletionP…
486 | …
    |

E501 Line too long (214 > 100)
   --> scripts/issue_522_apply.py:485:101
    |
483 | …
484 | …Dep,\n    principal: CollectionDeleter,\n) -> CollectionDeletionPlanOut:\n""",
485 | …Dep,\n    principal: CollectionDeleter,\n    retirement_claim_id: str | None = None,\n) -> CollectionDeletionPlanOut:\n""",
    |           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
486 | …
487 | …
    |

E501 Line too long (188 > 100)
   --> scripts/issue_522_apply.py:490:101
    |
488 | …
489 | …
490 | …n_id,\n            principal=principal,\n            retirement_claim_id=retirement_claim_id,\n        )\n""",
    |                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
491 | …
492 | …
    |

E501 Line too long (161 > 100)
   --> scripts/issue_522_apply.py:495:101
    |
493 | …
494 | …ntext=request.event_context,\n""",
495 | …ntext=request.event_context,\n            retirement_claim_id=request.retirement_claim_id,\n""",
    |                                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
496 | …
    |

E501 Line too long (222 > 100)
   --> scripts/issue_522_apply.py:501:101
    |
499 | …
500 | …
501 | …   return self._json(\n            "POST",\n            f"/v1/collections/{str(collection_id)}/deletion-plan",\n        )\n""",
    |       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
502 | …   *,\n        retirement_claim_id: str | None = None,\n    ) -> dict[str, Any]:\n        params = (\n            {"retirement_claim…
503 | …
    |

E501 Line too long (497 > 100)
   --> scripts/issue_522_apply.py:502:101
    |
500 | …
501 | …   return self._json(\n            "POST",\n            f"/v1/collections/{str(collection_id)}/deletion-plan",\n        )\n""",
502 | …   *,\n        retirement_claim_id: str | None = None,\n    ) -> dict[str, Any]:\n        params = (\n            {"retirement_claim_id": retirement_claim_id}\n            if retirement_claim_id is not None\n            else None\n        )\n        return self._json(\n            "POST",\n            f"/v1/collections/{str(collection_id)}/deletion-plan",\n            params=params,\n        )\n""",
    |       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
503 | …
504 | …
    |

E501 Line too long (183 > 100)
   --> scripts/issue_522_apply.py:506:101
    |
504 | …
505 | …
506 | … | None = None,\n    ) -> dict[str, Any]:\n        payload: dict[str, Any] = {"challenge": challenge}\n""",
    |                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
507 | …= None,\n        event_context: Mapping[str, Any] | None = None,\n    ) -> dict[str, Any]:\n        payload: dict[str, Any] = {"chal…
508 | …
    |

E501 Line too long (343 > 100)
   --> scripts/issue_522_apply.py:507:101
    |
505 | …
506 | …  ) -> dict[str, Any]:\n        payload: dict[str, Any] = {"challenge": challenge}\n""",
507 | …ent_context: Mapping[str, Any] | None = None,\n    ) -> dict[str, Any]:\n        payload: dict[str, Any] = {"challenge": challenge}\n        if retirement_claim_id is not None:\n            payload["retirement_claim_id"] = retirement_claim_id\n""",
    |       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
508 | …
    |

E501 Line too long (1111 > 100)
   --> scripts/issue_522_apply.py:534:101
    |
532 | …
533 | …
534 | …rable payload units. Minimal protocol\nadapters create collections and retain only bounded transient custody until the\nfinalized root receipt. Jeb is a payload-free, tag-targeted transformation\ncontroller. It freezes exact immutable input roots, owns fenced claims, verifies\nderived collections from Riverhog, and separately orchestrates optional retirement.\nMunchy is a content-aware collection transform executor: one finalized collection\nset and one sealed intent produce exactly one finalized collection on success.\nTargets own any bounded encrypted or ephemeral payload workspace.\n\nTags select work but are never transform identity. Processing claims bind exact\nmanifest and content identities. Scoped transform capabilities never expose archive\npassphrases, broad S3 credentials, archive-key selection, or deletion. Every derived\ncollection carries immutable input-root, plan, execution, disposition, and\nprovenance evidence. Active or retiring claims participate in Riverhog deletion\nsafety.\n"""
    |       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
535 | …
    |

Found 62 errors.
No fixes available (1 hidden fix can be enabled with the `--unsafe-fixes` option).
make: *** [Makefile:149: ruff] Error 1
```

## make compile

Exit status: 0

```text
```

## make unit

Exit status: 2

```text
=========================== short test summary info ============================
SKIPPED [1] packages/riverhog-age/tests/test_riverhog_age_c2sp_vectors.py:25: set CCTV_AGE_TESTDATA to run the C2SP/CCTV age vector suite
FAILED companions/munchy/tests/test_munchy_server_contract.py::test_munchy_official_clients_cover_complete_positive_api_lifecycle - AssertionError: operations lack a successful real-API witness: ['create_or_resume_collection_transform', 'get_collection_transform']
FAILED tests/unit/test_app_key_api.py::test_bootstrap_and_application_keys_enforce_permissions_immediately - AttributeError: 'types.SimpleNamespace' object has no attribute 'collection_workflows'
FAILED tests/unit/test_archive_multipart_reaper.py::test_archive_maintenance_sweep_recovers_and_processes_collection_finalizations - AttributeError: 'types.SimpleNamespace' object has no attribute 'collection_workflows'
FAILED tests/unit/test_configuration_connectivity.py::test_direct_environment_settings_have_explicit_test_witnesses - AssertionError: assert 209 == 206
 +  where 209 = len({'JEB_ALLOW_INSECURE_HTTP', 'JEB_API_TOKEN', 'JEB_BASE_URL', 'JEB_BATCH_DIR', 'JEB_EVENT_CONTEXT_RETENTION', 'JEB_EVENT_REPEAT_INTERVAL', ...})
FAILED tests/unit/test_entrypoints.py::test_all_markdown_is_reachable_and_links_resolve - AssertionError: assert {PosixPath('/...ITY.md'), ...} == {PosixPath('/...ure.md'), ...}
  
  Extra items in the left set:
  PosixPath('/home/runner/work/riverhog/riverhog/docs/collection-workflows.md')
  PosixPath('/home/runner/work/riverhog/riverhog/ISSUE-522-HANDOFF.md')
  PosixPath('/home/runner/work/riverhog/riverhog/ISSUE-522-VALIDATION.md')
  
  Full diff:
    {
        PosixPath('/home/runner/work/riverhog/riverhog/AGENTS.md'),
  +     PosixPath('/home/runner/work/riverhog/riverhog/ISSUE-522-HANDOFF.md'),
  +     PosixPath('/home/runner/work/riverhog/riverhog/ISSUE-522-VALIDATION.md'),
        PosixPath('/home/runner/work/riverhog/riverhog/LICENSE.md'),
        PosixPath('/home/runner/work/riverhog/riverhog/README.md'),
        PosixPath('/home/runner/work/riverhog/riverhog/SECURITY.md'),
        PosixPath('/home/runner/work/riverhog/riverhog/THIRD_PARTY_NOTICES.md'),
        PosixPath('/home/runner/work/riverhog/riverhog/docs/architecture.md'),
  +     PosixPath('/home/runner/work/riverhog/riverhog/docs/collection-workflows.md'),
        PosixPath('/home/runner/work/riverhog/riverhog/docs/how-to/provider-qualification.md'),
        PosixPath('/home/runner/work/riverhog/riverhog/docs/operator-responsibilities.md'),
    }
FAILED tests/unit/test_entrypoints.py::test_main_context_documents_are_exact_and_directly_routed - AssertionError: assert {PosixPath('/...bilities.md')} == {PosixPath('/...bilities.md')}
  
  Extra items in the left set:
  PosixPath('/home/runner/work/riverhog/riverhog/docs/collection-workflows.md')
  
  Full diff:
    {
        PosixPath('/home/runner/work/riverhog/riverhog/docs/architecture.md'),
  +     PosixPath('/home/runner/work/riverhog/riverhog/docs/collection-workflows.md'),
        PosixPath('/home/runner/work/riverhog/riverhog/docs/operator-responsibilities.md'),
    }
FAILED tests/unit/test_entrypoints.py::test_architecture_is_scoped_to_quick_context - AssertionError: assert ['Authority m...rkflow model'] == ['Authority m...pository map']
  
  Left contains one more item: 'Collection workflow model'
  
  Full diff:
    [
        'Authority model',
        'Boundary model',
        'Repository map',
  +     'Collection workflow model',
    ]
FAILED tests/unit/test_openapi_smoke.py::test_collection_contracts_expose_the_stable_creation_timestamp - TypeError: CollectionSummary.__init__() missing 2 required positional arguments: 'content_etag' and 'manifest_sha256'
FAILED tests/unit/test_operation_lifecycle_api.py::test_riverhog_official_client_positive_disposable_lifecycle - TypeError: ServiceContainer.__init__() missing 1 required positional argument: 'collection_workflows'
FAILED tests/unit/test_operation_qualification.py::test_generated_operation_matrix_is_complete_and_fail_closed - operation_qualification.QualificationError: public operation has no official client: riverhog create_or_resume_processing_claim
FAILED tests/unit/test_operation_qualification.py::test_operation_audiences_distinguish_commands_wires_and_protocols - operation_qualification.QualificationError: public operation has no official client: riverhog create_or_resume_processing_claim
FAILED tests/unit/test_operation_qualification.py::test_exact_sha_evidence_contains_only_generated_current_rows - operation_qualification.QualificationError: public operation has no official client: riverhog create_or_resume_processing_claim
FAILED tests/unit/test_operation_qualification.py::test_timing_evidence_fails_closed_on_missing_local_operation - AssertionError: assert 'lack positive local timing witnesses' in 'public operation has no official client: riverhog create_or_resume_processing_claim'
 +  where 'public operation has no official client: riverhog create_or_resume_processing_claim' = str(QualificationError('public operation has no official client: riverhog create_or_resume_processing_claim'))
FAILED tests/unit/test_public_interface_parity.py::test_every_public_api_operation_has_an_official_client_method[riverhog-create_app-client_types0] - AssertionError: riverhog OpenAPI operations missing from its client: {'create_or_resume_processing_claim': 'POST /v1/collection-processing-claims', 'list_processing_claims': 'GET /v1/collection-processing-claims', 'get_processing_claim': 'GET /v1/collection-processing-claims/{claim_id}', 'renew_processing_claim': 'POST /v1/collection-processing-claims/{claim_id}/renew', 'create_transform_capability': 'POST /v1/collection-processing-claims/{claim_id}/capabilities', 'settle_processing_claim': 'POST /v1/collection-processing-claims/{claim_id}/settle', 'begin_processing_claim_retirement': 'POST /v1/collection-processing-claims/{claim_id}/retirement', 'release_processing_claim': 'POST /v1/collection-processing-claims/{claim_id}/release', 'get_collection_derivation': 'GET /v1/collections/{collection_id}/derivation'}
assert {'create_or_r...}/renew', ...} == {}
  
  Left contains 9 more items:
  {'begin_processing_claim_retirement': 'POST '
                                        '/v1/collection-processing-claims/{claim_id}/retirement',
   'create_or_resume_processing_claim': 'POST /v1/collection-processing-claims',
   'create_transform_capability': 'POST '
                                  '/v1/collection-processing-claims/{claim_id}/capabilities',
   'get_collection_derivation': 'GET /v1/collections/{collection_id}/derivation',
   'get_processing_claim': 'GET /v1/collection-processing-claims/{claim_id}',
   'list_processing_claims': 'GET /v1/collection-processing-claims',
   'release_processing_claim': 'POST '
                               '/v1/collection-processing-claims/{claim_id}/release',
   'renew_processing_claim': 'POST '
                             '/v1/collection-processing-claims/{claim_id}/renew',
   'settle_processing_claim': 'POST '
                              '/v1/collection-processing-claims/{claim_id}/settle'}
  
  Full diff:
  - {}
  + {
  +     'create_or_resume_processing_claim': 'POST /v1/collection-processing-claims',
  +     'list_processing_claims': 'GET /v1/collection-processing-claims',
  +     'get_processing_claim': 'GET /v1/collection-processing-claims/{claim_id}',
  +     'renew_processing_claim': 'POST /v1/collection-processing-claims/{claim_id}/renew',
  +     'create_transform_capability': 'POST /v1/collection-processing-claims/{claim_id}/capabilities',
  +     'settle_processing_claim': 'POST /v1/collection-processing-claims/{claim_id}/settle',
  +     'begin_processing_claim_retirement': 'POST /v1/collection-processing-claims/{claim_id}/retirement',
  +     'release_processing_claim': 'POST /v1/collection-processing-claims/{claim_id}/release',
  +     'get_collection_derivation': 'GET /v1/collections/{collection_id}/derivation',
  + }
FAILED tests/unit/test_public_interface_parity.py::test_every_public_api_operation_has_an_official_client_method[munchy-<lambda>-client_types1] - AssertionError: munchy OpenAPI operations missing from its client: {'create_or_resume_collection_transform': 'POST /v1/collection-transforms', 'get_collection_transform': 'GET /v1/collection-transforms/{job_id}'}
assert {'create_or_r...rms/{job_id}'} == {}
  
  Left contains 2 more items:
  {'create_or_resume_collection_transform': 'POST /v1/collection-transforms',
   'get_collection_transform': 'GET /v1/collection-transforms/{job_id}'}
  
  Full diff:
  - {}
  + {
  +     'create_or_resume_collection_transform': 'POST /v1/collection-transforms',
  +     'get_collection_transform': 'GET /v1/collection-transforms/{job_id}',
  + }
FAILED tests/unit/test_workspace_boundaries.py::test_projects_declare_their_exact_direct_runtime_dependencies - AssertionError: jeb-server: unused direct dependency riverhog-api-client
assert not ['jeb-server: unused direct dependency riverhog-api-client']
FAILED tests/unit/test_workspace_boundaries.py::test_companion_platform_clients_are_owned_by_contained_adapters - AssertionError: assert {PosixPath('c...ansforms.py')} == {PosixPath('c...riverhog.py')}
  
  Extra items in the left set:
  PosixPath('companions/munchy/server/src/munchy_core/services/collection_transforms.py')
  
  Full diff:
    {
        PosixPath('companions/munchy/server/src/munchy_core/adapters/riverhog.py'),
  +     PosixPath('companions/munchy/server/src/munchy_core/services/collection_transforms.py'),
    }
FAILED tests/unit/test_workspace_boundaries.py::test_core_dependency_graphs_are_acyclic - AssertionError: riverhog_core.catalog_base -> riverhog_core.catalog_workflow_models -> riverhog_core.catalog_base
assert ['riverhog_core.catalog_base', 'riverhog_core.catalog_workflow_models', 'riverhog_core.catalog_base'] is None
FAILED tests/unit/test_workspace_boundaries.py::test_images_copy_their_complete_internal_dependency_closure - AssertionError: companions/jeb/server/Dockerfile omits ['packages/riverhog-api-client', 'packages/riverhog-protocol']
assert not {'packages/riverhog-api-client', 'packages/riverhog-protocol'}
19 failed, 1444 passed, 1 skipped in 193.29s (0:03:13)
make: *** [Makefile:171: unit] Error 1
```

## make spec

Exit status: 0

```text
..                                                                       [100%]
2 passed in 1.62s
```

