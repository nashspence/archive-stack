@acceptance @cli @mvp
Feature: riverhog CLI
  The main CLI is focused on collections and pinned hot-storage sets.

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

    Scenario: riverhog collection files emits the collection files payload
      Given an archive containing collection "docs"
      When the operator runs 'riverhog collection files docs --page 2 --per-page 2 --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/collection-files/docs"
      And stdout mentions "receipt-456.pdf"

    Scenario: riverhog hot pin emits the API pin payload
      Given target "docs/tax/2022/invoice-123.pdf" is valid
      When the operator runs 'riverhog hot pin "docs/tax/2022/invoice-123.pdf" --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of POST "/v1/pin"

    Scenario: riverhog hot unpin emits the API release payload
      Given target "docs/tax/2022/invoice-123.pdf" is valid
      When the operator runs 'riverhog hot unpin "docs/tax/2022/invoice-123.pdf" --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of POST "/v1/release"

    Scenario: riverhog hot list emits fetch associations for active pins
      Given archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"
      When the operator runs 'riverhog hot list --page 1 --per-page 25 --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/pins"
      And stdout mentions "per_page"
      And stdout mentions "total"
      And stdout mentions target "docs/tax/2022/invoice-123.pdf"
      And stdout mentions fetch id "fx-1"
      And stdout mentions "waiting_media"

    Scenario: riverhog hot show emits one fetch summary payload
      Given fetch "fx-1" exists for target "docs/tax/2022/invoice-123.pdf"
      When the operator runs 'riverhog hot show "fx-1" --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/fetches/fx-1"
      And stdout mentions fetch id "fx-1"

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
      And collection "docs" has uploaded Glacier archive package
      And candidate "img_2026-04-20_01" is finalized
      And copy "20260420T040001Z-1" already exists
      When the operator runs 'riverhog collection list --query docs'
      Then the command exits with code 0
      And stdout mentions "collections"
      And stdout mentions "docs"
      And stdout mentions "under_protected"
      And stdout mentions "archive="
      And stdout mentions "disc="

    Scenario: riverhog collection show prints deep archive and physical coverage
      Given an archive with planner fixtures
      And collection "docs" has uploaded Glacier archive package
      And candidate "img_2026-04-20_01" is finalized
      And copy "20260420T040001Z-1" already exists
      When the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with location "Shelf B1", state "verified", and verification_state "verified"
      When the operator runs 'riverhog collection show docs'
      Then the command exits with code 0
      And stdout mentions "glacier: uploaded"
      And stdout mentions "collection_manifest:"
      And stdout mentions "ots: uploaded"
      And stdout mentions "disc_coverage=partial"
      And stdout mentions "coverage:"
      And stdout mentions "paths: tax/2022/invoice-123.pdf"
      And stdout mentions "label=20260420T040001Z-1"

    Scenario: riverhog hot pin prints fetch guidance when recovery is needed
      Given pinning target "docs/tax/2022/invoice-123.pdf" requires fetch "fx-1"
      When the operator runs 'riverhog hot pin "docs/tax/2022/invoice-123.pdf"'
      Then the command exits with code 0
      And stdout mentions target "docs/tax/2022/invoice-123.pdf"
      And stdout mentions fetch id "fx-1"
      And stdout mentions at least one candidate copy id

    Scenario: riverhog hot show lists pending and partial files for one pin manifest
      Given fetch "fx-1" exists for target "docs/tax/2022/invoice-123.pdf"
      When the operator runs 'riverhog hot show "fx-1"'
      Then the command exits with code 0
      And stdout mentions fetch id "fx-1"
      And stdout mentions "pending"
      And stdout mentions "partial"
      And stdout mentions "expires"
