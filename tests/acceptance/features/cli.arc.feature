@acceptance @cli @mvp
Feature: arc CLI
  The main CLI is a thin stable wrapper over the API.

  Rule: JSON mode mirrors API payloads
    @xfail_contract
    Scenario: arc pin emits the API pin payload
      Given target "docs/tax/2022/invoice-123.pdf" is valid
      When the operator runs 'arc pin "docs/tax/2022/invoice-123.pdf" --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of POST "/v1/pin"

    @xfail_contract
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

    @xfail_contract
    Scenario: arc plan emits the API plan payload
      Given an archive with planner fixtures
      And an archive with split planner fixtures
      When the operator runs 'arc plan --page 1 --per-page 2 --sort candidate_id --order asc --collection docs --iso-ready --query invoice-123.pdf --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/plan"
      And stdout mentions "img_2026-04-20_01"

    @xfail_contract
    Scenario: arc images emits the finalized-image listing payload
      Given an archive with planner fixtures
      And an archive with split planner fixtures
      And candidate "img_2026-04-20_01" is finalized
      And candidate "img_2026-04-20_03" is finalized
      And copy "BR-021-A" already exists
      When the operator runs 'arc images --page 1 --per-page 2 --sort finalized_at --order desc --has-copies --query 040001Z --collection docs --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/images"
      And stdout mentions "20260420T040001Z"

    @xfail_contract
    Scenario: arc pins emits fetch associations for active pins
      Given archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"
      When the operator runs 'arc pins --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/pins"
      And stdout mentions target "docs/tax/2022/invoice-123.pdf"
      And stdout mentions fetch id "fx-1"
      And stdout mentions "waiting_media"

  Rule: Non-JSON mode remains concise and stable
    @xfail_contract
    Scenario: arc plan prints candidate ids, fill, and readiness
      Given an archive with planner fixtures
      And an archive with split planner fixtures
      When the operator runs 'arc plan --collection docs --iso-ready'
      Then the command exits with code 0
      And stdout mentions "img_2026-04-20_01"
      And stdout mentions "fill:"
      And stdout mentions "iso_ready: True"
      And stdout mentions "collections: 1 [docs]"

    @xfail_contract
    Scenario: arc images prints finalized ids, filenames, and copy counts
      Given an archive with planner fixtures
      And candidate "img_2026-04-20_01" is finalized
      And copy "BR-021-A" already exists
      When the operator runs 'arc images --has-copies'
      Then the command exits with code 0
      And stdout mentions "20260420T040001Z"
      And stdout mentions "20260420T040001Z.iso"
      And stdout mentions "copies: 1"
      And stdout mentions "collections: 1 [docs]"

    @xfail_contract
    Scenario: arc pin prints fetch guidance when recovery is needed
      Given pinning target "docs/tax/2022/invoice-123.pdf" requires fetch "fx-1"
      When the operator runs 'arc pin "docs/tax/2022/invoice-123.pdf"'
      Then the command exits with code 0
      And stdout mentions target "docs/tax/2022/invoice-123.pdf"
      And stdout mentions fetch id "fx-1"
      And stdout mentions at least one candidate copy id

    @xfail_contract
    Scenario: arc fetch lists pending and partial files for one pin manifest
      Given fetch "fx-1" exists for target "docs/tax/2022/invoice-123.pdf"
      When the operator runs 'arc fetch "fx-1"'
      Then the command exits with code 0
      And stdout mentions fetch id "fx-1"
      And stdout mentions "pending"
      And stdout mentions "partial"
      And stdout mentions "expires"
