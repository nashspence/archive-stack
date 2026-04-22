@acceptance @api @mvp @xfail_contract
Feature: Pins API
  Pins define the exact set of projected-path selectors that must remain materialized in hot storage.

  Rule: Pinning is exact-selector idempotent
    Background:
      Given collection "docs" exists and is fully hot

    Scenario: Pin a whole collection that is already hot
      When the client posts to "/v1/pin" with target "docs/"
      Then the response status is 200
      And pin is true
      And hot state is "ready"
      And missing_bytes is 0
      And a fetch id is returned
      And fetch state is "done"

    Scenario: Pin a single file that is already hot
      When the client posts to "/v1/pin" with target "docs/tax/2022/invoice-123.pdf"
      Then the response status is 200
      And pin is true
      And hot state is "ready"
      And missing_bytes is 0
      And a fetch id is returned
      And fetch state is "done"

    Scenario: Repeating the same pin does not create duplicates
      Given target "docs/" is already pinned
      When the client posts to "/v1/pin" with target "docs/"
      Then the response status is 200
      And "/v1/pins" contains target "docs/" exactly once

  Rule: Releasing removes only the exact matching pin
    Background:
      Given target "docs/tax/" is pinned
      And target "docs/tax/2022/invoice-123.pdf" is pinned

    Scenario: Releasing a broader pin leaves the narrower pin intact
      When the client posts to "/v1/release" with target "docs/tax/"
      Then the response status is 200
      And "/v1/pins" does not contain target "docs/tax/"
      And "/v1/pins" still contains target "docs/tax/2022/invoice-123.pdf"
      And file "docs/tax/2022/invoice-123.pdf" remains hot

    Scenario: Releasing a narrower pin leaves the broader pin intact
      When the client posts to "/v1/release" with target "docs/tax/2022/invoice-123.pdf"
      Then the response status is 200
      And "/v1/pins" still contains target "docs/tax/"
      And "/v1/pins" does not contain target "docs/tax/2022/invoice-123.pdf"
      And file "docs/tax/2022/invoice-123.pdf" remains hot

    Scenario: Releasing a missing pin is a successful no-op
      Given target "docs/missing/" is not pinned
      When the client posts to "/v1/release" with target "docs/missing/"
      Then the response status is 200
      And pin is false

  Rule: Pin listing exposes the associated fetch manifest
    Background:
      Given archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"

    Scenario: Listing pins includes fetch id and state for each exact pin
      When the client gets "/v1/pins"
      Then the response status is 200
      And "/v1/pins" entry for target "docs/tax/2022/invoice-123.pdf" contains fetch id "fx-1"
      And "/v1/pins" entry for target "docs/tax/2022/invoice-123.pdf" contains fetch state "waiting_media"

  Rule: Releasing the last exact pin reconciles hot storage and fetch state
    Scenario: Releasing the last covering pin removes the file from hot storage
      Given collection "docs" exists and is fully hot
      And target "docs/tax/2022/invoice-123.pdf" is pinned
      When the client posts to "/v1/release" with target "docs/tax/2022/invoice-123.pdf"
      Then the response status is 200
      And file "docs/tax/2022/invoice-123.pdf" is not hot

    Scenario: Releasing the last exact pin removes the associated fetch manifest
      Given archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"
      When the client posts to "/v1/release" with target "docs/tax/2022/invoice-123.pdf"
      Then the response status is 200
      And fetch "fx-1" no longer exists

  Rule: Selectors are canonical and precise
    Scenario: A projected parent directory selector is valid for pin
      Given collection "photos/2024" exists and is fully hot
      When the client posts to "/v1/pin" with target "photos/"
      Then the response status is 200
      And pin is true
      And hot state is "ready"
      And missing_bytes is 0
      And a fetch id is returned
      And fetch state is "done"

    Scenario Outline: Invalid targets are rejected for pin
      When the client posts to "/v1/pin" with target "<target>"
      Then the response status is 400
      And the error code is "invalid_target"

      Examples:
        | target       |
        | /docs/       |
        | docs         |
        | docs//2022/  |
        | docs/./tax/  |
        | docs/../tax/ |

    Scenario Outline: Invalid targets are rejected for release
      When the client posts to "/v1/release" with target "<target>"
      Then the response status is 400
      And the error code is "invalid_target"

      Examples:
        | target       |
        | /docs/       |
        | docs         |
        | docs//2022/  |
        | docs/./tax/  |
        | docs/../tax/ |
