@acceptance @cli @mvp
Feature: riverhog CLI
  The main CLI is focused on collections, fetch work orders, and disc-redundant hot storage.

  Rule: JSON mode emits machine-readable payloads
    Scenario: riverhog collection list emits a compact collection listing payload
      Given an archive containing collection "docs"
      When the operator runs 'riverhog collection list --page 1 --per-page 2 --sort id --order asc --query docs --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the compact collection list payload
      And stdout mentions "docs"

    Scenario: riverhog collection show emits the collection summary payload
      Given an archive containing collection "docs"
      When the operator runs 'riverhog collection show docs --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/collections/docs"
      And stdout mentions "docs"

    Scenario: riverhog find emits a paged file inventory payload
      Given an archive containing collection "docs"
      When the operator runs 'riverhog find receipt --collection docs --page 1 --per-page 2 --sort path --order asc --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/search"
      And stdout mentions "receipt-456.pdf"

    Scenario: riverhog hot fetch create emits a draft fetch payload
      Given target "docs/tax/2022/invoice-123.pdf" is valid
      When the operator runs 'riverhog hot fetch create --name "Tax recovery" "docs/tax/2022/invoice-123.pdf" --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout mentions fetch id "fx-1"
      And stdout mentions "Tax recovery"
      And stdout mentions "draft"

    Scenario: riverhog hot fetch list emits named fetches
      Given disc-covered target "docs/tax/2022/invoice-123.pdf" has queued fetch "fx-1"
      When the operator runs 'riverhog hot fetch list --page 1 --per-page 25 --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/fetches"
      And stdout mentions "per_page"
      And stdout mentions "total"
      And stdout mentions target "docs/tax/2022/invoice-123.pdf"
      And stdout mentions fetch id "fx-1"
      And stdout mentions "queued_djdan"

    Scenario: riverhog hot fetch show emits one fetch status payload
      Given fetch "fx-1" exists for target "docs/tax/2022/invoice-123.pdf"
      When the operator runs 'riverhog hot fetch show "fx-1" --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/fetches/fx-1/status"
      And stdout mentions fetch id "fx-1"
      And stdout mentions "next_action"

    Scenario: riverhog hot fetch files emits a paged selected-file payload
      Given fetch "fx-1" exists for target "docs/tax/2022/invoice-123.pdf"
      When the operator runs 'riverhog hot fetch files "fx-1" --page 1 --per-page 25 --sort bytes --order desc --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/fetches/fx-1/files"
      And stdout mentions fetch id "fx-1"
      And stdout mentions target "docs/tax/2022/invoice-123.pdf"

    Scenario: riverhog hot fetch start can queue fetch materialization
      Given file "docs/tax/2022/invoice-123.pdf" has disc coverage
      And collection "docs" has uploaded archive package
      And target "docs/tax/2022/invoice-123.pdf" has a draft fetch
      When the operator runs 'riverhog hot fetch start fx-1 --archive --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout mentions fetch id "fx-1"
      And stdout mentions "restoring_archive"
      Given archive restore "ar-docs-restore-1" restore remains pending
      When the operator runs 'riverhog hot fetch cancel fx-1 --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout mentions fetch id "fx-1"
      And stdout mentions "canceled"
      When the operator runs 'riverhog hot fetch show fx-1 --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/fetches/fx-1/status"
      And stdout mentions fetch id "fx-1"
      And stdout mentions "ar-docs-restore-1"

  Rule: Human mode remains concise and stable
    Scenario: riverhog collection upload ingests and archives a local collection source
      Given a local collection source "photos-2024" with deterministic fixture contents
      When the operator uploads collection source "photos-2024" with riverhog
      Then the command exits with code 0
      And stdout mentions "__photos-2024"
      And stdout mentions "state: archiving"
      And stdout mentions "upload: 4/4 files"

    Scenario: riverhog collection list prints collection coverage
      Given an archive with planner fixtures
      And collection "docs" has uploaded archive package
      And candidate "img_2026-04-20_01" is finalized
      And disc "20260420T040001Z-1" already exists
      When the operator runs 'riverhog collection list --query docs'
      Then the command exits with code 0
      And stdout mentions "collections"
      And stdout mentions "docs"
      And stdout mentions "partial"
      And stdout mentions "archive="
      And stdout mentions "disc="

    Scenario: riverhog collection show prints Archive and disc coverage
      Given an archive with planner fixtures
      And collection "docs" has uploaded archive package
      And candidate "img_2026-04-20_01" is finalized
      And disc "20260420T040001Z-1" already exists
      When the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-1" with location "Shelf B1", state "verified", and verification_state "verified"
      When the operator runs 'riverhog collection show docs'
      Then the command exits with code 0
      And stdout mentions "archive: uploaded"
      And stdout mentions "collection_manifest:"
      And stdout mentions "ots: uploaded"
      And stdout mentions "disc coverage: partial"
      And stdout mentions "coverage:"
      And stdout mentions "paths: tax/2022/invoice-123.pdf"
      And stdout mentions "label=20260420T040001Z-1"

    Scenario: riverhog hot fetch show lists pending files for one manifest
      Given fetch "fx-1" exists for target "docs/tax/2022/invoice-123.pdf"
      When the operator runs 'riverhog hot fetch show "fx-1"'
      Then the command exits with code 0
      And stdout mentions fetch id "fx-1"
      And stdout mentions "targets"
      And stdout mentions "files preview"
      And stdout mentions "pending"
