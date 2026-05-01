@acceptance @cli @mvp
Feature: arc CLI
  The main CLI is the no-argument operator home and a stable wrapper over the API in JSON flows.

  Rule: JSON mode mirrors API payloads
    Scenario: arc pin emits the API pin payload
      Given target "docs/tax/2022/invoice-123.pdf" is valid
      When the operator runs 'arc pin "docs/tax/2022/invoice-123.pdf" --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of POST "/v1/pin"

    Scenario: arc release emits the API release payload
      Given target "docs/tax/2022/invoice-123.pdf" is valid
      When the operator runs 'arc release "docs/tax/2022/invoice-123.pdf" --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of POST "/v1/release"

    Scenario: arc find emits the API search payload
      When the operator runs 'arc find "invoice" --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/search"

    Scenario: arc plan emits the API plan payload
      Given an archive with planner fixtures
      And an archive with split planner fixtures
      When the operator runs 'arc plan --page 1 --per-page 2 --sort candidate_id --order asc --collection docs --iso-ready --query invoice-123.pdf --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/plan"
      And stdout mentions "img_2026-04-20_01"

    Scenario: arc images emits the finalized-image listing payload
      Given an archive with planner fixtures
      And an archive with split planner fixtures
      And candidate "img_2026-04-20_01" is finalized
      And candidate "img_2026-04-20_03" is finalized
      And copy "20260420T040001Z-1" already exists
      When the operator runs 'arc images --page 1 --per-page 2 --sort finalized_at --order desc --has-copies --query 040001Z --collection docs --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/images"
      And stdout mentions "20260420T040001Z"

    Scenario: arc glacier emits the collection-native Glacier usage payload
      Given an archive with planner fixtures
      And collection "docs" has uploaded Glacier archive package
      When the operator runs 'arc glacier --collection docs --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/glacier"
      And stdout mentions "docs"

    Scenario: arc pins emits fetch associations for active pins
      Given archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"
      When the operator runs 'arc pins --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/pins"
      And stdout mentions target "docs/tax/2022/invoice-123.pdf"
      And stdout mentions fetch id "fx-1"
      And stdout mentions "waiting_media"

    Scenario: arc show --files emits the collection files payload
      Given an archive containing collection "docs"
      When the operator runs 'arc show docs --files --page 2 --per-page 2 --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/collection-files/docs"
      And stdout mentions "receipt-456.pdf"

    Scenario: arc status emits the files query payload
      Given an archive containing collection "docs"
      When the operator runs 'arc status "docs/" --page 2 --per-page 2 --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/files"
      And stdout mentions "receipt-456.pdf"

    Scenario: arc copy add emits the generated-copy registration payload
      Given candidate "img_2026-04-20_01" is finalized
      When the operator runs 'arc copy add 20260420T040001Z --at "Shelf B1" --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout mentions "20260420T040001Z-1"

    Scenario: arc copy list emits the generated-copy listing payload
      Given candidate "img_2026-04-20_01" is finalized
      When the operator runs 'arc copy list 20260420T040001Z --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/images/20260420T040001Z/copies"

    Scenario: arc copy move emits the copy update payload
      Given copy "20260420T040001Z-1" already exists
      When the operator runs 'arc copy move 20260420T040001Z 20260420T040001Z-1 --to "Shelf B2" --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout mentions "Shelf B2"

    Scenario: arc copy mark emits the copy state-transition payload
      Given copy "20260420T040001Z-1" already exists
      When the operator runs 'arc copy mark 20260420T040001Z 20260420T040001Z-1 --state verified --verification-state verified --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout mentions "verified"

  Rule: No-argument operator home
    @todo @issue_206
    Scenario: arc opens the operator home when no attention is needed
      Given the archive has no non-physical attention items
      When the operator runs 'arc'
      Then the command exits with code 0
      And stdout matches operator copy "arc_home_no_attention"
      And stdout mentions "No attention needed"
      And stdout mentions "upload a collection"
      And stdout mentions "search hot storage"
      And stdout mentions "pin files"
      And stdout mentions "get files"
      And stdout mentions "release pins"
      And stdout does not mention "Usage:"

    @todo @issue_206
    Scenario: arc prioritizes cloud backup failures before at-will workflows
      Given collection "docs" has failed cloud backup after retries
      When the operator runs 'arc'
      Then the command exits with code 0
      And stdout includes operator copy "arc_item_cloud_backup_failed"
      And stdout mentions "Cloud backup needs attention"
      And stdout mentions "docs"
      And stdout mentions "retry cloud backup"
      And stdout does not mention "Glacier"
      And stdout does not mention "archive_manifest"

    @todo @issue_206
    Scenario: arc prioritizes setup and notification health before ordinary at-will workflows
      Given setup needs attention
      And notification delivery needs attention
      When the operator runs 'arc'
      Then the command exits with code 0
      And stdout includes operator copy "arc_item_setup_needs_attention"
      And stdout includes operator copy "arc_item_notification_health_failed"
      And stdout mentions "Setup needs attention"
      And stdout mentions "Notifications need attention"
      And stdout does not mention "webhook"

  Rule: Normal human copy uses operator terms
    @todo @issue_206
    Scenario: arc upload ingests and archives a local collection source
      Given a local collection source "photos-2024" with deterministic fixture contents
      When the operator uploads collection source "photos-2024" with arc
      Then the command exits with code 0
      And stdout includes operator copy "upload_finalized"
      And stdout mentions "Collection photos-2024"
      And stdout mentions "Cloud backup is safe"
      And stdout does not mention "finalized"
      And stdout does not mention "Glacier"
      And collection "photos-2024" has hot_bytes equal to bytes

    @todo @issue_206
    Scenario: arc plan describes disc work without candidate terminology
      Given an archive with planned disc work
      When the operator runs 'arc plan --collection docs --iso-ready'
      Then the command exits with code 0
      And stdout includes operator copy "plan_disc_work_ready"
      And stdout mentions "Disc work is ready"
      And stdout mentions "docs"
      And stdout mentions "blank disc"
      And stdout does not mention "candidate"
      And stdout does not mention "iso_ready"

    @todo @issue_206
    Scenario: arc images points physical media work to arc-disc
      Given an archive with planned disc work
      And a disc copy already exists for collection "docs"
      When the operator runs 'arc images --has-copies'
      Then the command exits with code 0
      And stdout includes operator copy "images_physical_work_summary"
      And stdout mentions "Disc work needs attention"
      And stdout mentions "Run arc-disc"
      And stdout mentions "fully protected"
      And stdout does not mention "ready_to_finalize"
      And stdout does not mention "waiting_for_future_iso"
      And stdout does not mention "noncompliant_collections"

    @todo @issue_206
    Scenario: arc show describes collection safety without storage internals
      Given collection "docs" is safe in cloud backup
      And collection "docs" has partial disc coverage
      When the operator runs 'arc show docs'
      Then the command exits with code 0
      And stdout includes operator copy "collection_summary"
      And stdout mentions "cloud backup is safe"
      And stdout mentions "Disc coverage is partial"
      And stdout mentions "Labels"
      And stdout mentions "Storage location"
      And stdout does not mention "glacier_path"
      And stdout does not mention "archive_manifest"
      And stdout does not mention "protection_state"

    @todo @issue_206
    Scenario: arc show does not overstate split physical coverage from one disc
      Given collection "docs" has one split file protected by one disc
      When the operator runs 'arc show docs'
      Then the command exits with code 0
      And stdout includes operator copy "collection_summary"
      And stdout mentions "Disc coverage is partial"
      And stdout does not mention "fully protected"

    @todo @issue_206
    Scenario: arc glacier prints cloud backup cost and health
      Given collection "docs" is safe in cloud backup
      When the operator runs 'arc glacier --collection docs'
      Then the command exits with code 0
      And stdout includes operator copy "cloud_backup_report"
      And stdout mentions "Cloud backup"
      And stdout mentions "Estimated monthly cost"
      And stdout does not mention "glacier="
      And stdout does not mention "ots="
      And stdout does not mention "pricing_basis"

    @todo @issue_206 @ci_opt_in @requires_controlled_glacier_billing @issue_186
    Scenario: arc glacier prints resource-level and manifest-aware billing metadata in the spec harness
      Given an archive with split planner fixtures
      And collection "docs" has uploaded Glacier archive package
      And the spec harness exposes controlled Glacier billing metadata
      When the operator runs 'arc glacier'
      Then the command exits with code 0
      And stdout exposes Glacier billing resource-level and manifest metadata

    @todo @issue_206
    Scenario: arc copy add prints the generated label text and state
      Given candidate "img_2026-04-20_01" is finalized
      When the operator runs 'arc copy add 20260420T040001Z --at "Shelf B1"'
      Then the command exits with code 0
      And stdout includes operator copy "copy_registered"
      And stdout mentions "Disc label"
      And stdout mentions "20260420T040001Z-1"
      And stdout mentions "storage location Shelf B1"
      And stdout does not mention "copy slot"
      And stdout does not mention "registered"

    @todo @issue_206
    Scenario: arc pin prints fetch guidance when recovery is needed
      Given pinning target "docs/tax/2022/invoice-123.pdf" requires fetch "fx-1"
      When the operator runs 'arc pin "docs/tax/2022/invoice-123.pdf"'
      Then the command exits with code 0
      And stdout includes operator copy "pin_waiting_for_disc"
      And stdout mentions target "docs/tax/2022/invoice-123.pdf"
      And stdout mentions "Files need recovery from disc"
      And stdout mentions "Run arc-disc"
      And stdout does not mention "fetch manifest"
      And stdout does not mention "candidate"

    @todo @issue_206
    Scenario: arc fetch lists pending and partial files for one pin manifest
      Given fetch "fx-1" exists for target "docs/tax/2022/invoice-123.pdf"
      When the operator runs 'arc fetch "fx-1"'
      Then the command exits with code 0
      And stdout includes operator copy "fetch_detail_pending"
      And stdout mentions "Files need recovery from disc"
      And stdout mentions "Pending files"
      And stdout mentions "Partly restored files"
      And stdout mentions "Run arc-disc"
      And stdout does not mention "manifest"
