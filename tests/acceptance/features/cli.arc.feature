@acceptance @cli @mvp @xfail_contract
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
      When the operator runs 'arc plan --json'
      Then the command exits with code 0
      And stdout is valid JSON
      And stdout matches the structure of GET "/v1/plan"

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
