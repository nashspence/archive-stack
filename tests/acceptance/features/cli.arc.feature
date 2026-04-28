@acceptance @cli @mvp
Feature: arc CLI
  The main CLI is a thin stable wrapper over the API.

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

  Rule: Non-JSON mode remains concise and stable
    Scenario: arc upload ingests a local collection source
      Given a local collection source "photos-2024" with deterministic fixture contents
      When the operator uploads collection source "photos-2024" with arc
      Then the command exits with code 0
      And stdout mentions "collection: photos-2024"
      And stdout mentions "state: finalized"
      And collection "photos-2024" has hot_bytes equal to bytes

    Scenario: arc plan prints candidate ids, fill, and readiness
      Given an archive with planner fixtures
      And an archive with split planner fixtures
      When the operator runs 'arc plan --collection docs --iso-ready'
      Then the command exits with code 0
      And stdout mentions "img_2026-04-20_01"
      And stdout mentions "fill:"
      And stdout mentions "iso_ready: True"
      And stdout mentions "collections: 1 [docs]"

    Scenario: arc images prints finalized ids and protection progress
      Given an archive with planner fixtures
      And candidate "img_2026-04-20_01" is finalized
      And copy "20260420T040001Z-1" already exists
      When the operator runs 'arc images --has-copies'
      Then the command exits with code 0
      And stdout mentions "20260420T040001Z"
      And stdout mentions "20260420T040001Z.iso"
      And stdout mentions "protection: partially_protected copies=1/2 glacier="
      And stdout mentions "collections: 1 [docs]"

    Scenario: arc copy add prints the generated label text and state
      Given candidate "img_2026-04-20_01" is finalized
      When the operator runs 'arc copy add 20260420T040001Z --at "Shelf B1"'
      Then the command exits with code 0
      And stdout mentions "copy: 20260420T040001Z-1"
      And stdout mentions "label: 20260420T040001Z-1"
      And stdout mentions "state: registered"

    Scenario: arc pin prints fetch guidance when recovery is needed
      Given pinning target "docs/tax/2022/invoice-123.pdf" requires fetch "fx-1"
      When the operator runs 'arc pin "docs/tax/2022/invoice-123.pdf"'
      Then the command exits with code 0
      And stdout mentions target "docs/tax/2022/invoice-123.pdf"
      And stdout mentions fetch id "fx-1"
      And stdout mentions at least one candidate copy id

    Scenario: arc fetch lists pending and partial files for one pin manifest
      Given fetch "fx-1" exists for target "docs/tax/2022/invoice-123.pdf"
      When the operator runs 'arc fetch "fx-1"'
      Then the command exits with code 0
      And stdout mentions fetch id "fx-1"
      And stdout mentions "pending"
      And stdout mentions "partial"
      And stdout mentions "expires"
