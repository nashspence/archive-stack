@acceptance @api @mvp
Feature: Pins API
  Pins define the exact set of targets that must remain materialized in hot storage.

  Rule: Pinning is exact-target idempotent
    Background:
      Given collection "docs" exists and is fully hot

    Scenario: Pin a whole collection that is already hot
      When the client posts to "/v1/pin" with target "docs"
      Then the response status is 200
      And pin is true
      And hot state is "ready"
      And missing_bytes is 0
      And fetch is null

    Scenario: Pin a single file that is already hot
      When the client posts to "/v1/pin" with target "docs:/tax/2022/invoice-123.pdf"
      Then the response status is 200
      And pin is true
      And hot state is "ready"
      And missing_bytes is 0
      And fetch is null

    Scenario: Repeating the same pin does not create duplicates
      Given target "docs" is already pinned
      When the client posts to "/v1/pin" with target "docs"
      Then the response status is 200
      And "/v1/pins" contains target "docs" exactly once

  Rule: Releasing removes only the exact matching pin
    Background:
      Given target "docs:/tax/" is pinned
      And target "docs:/tax/2022/invoice-123.pdf" is pinned

    Scenario: Releasing a broader pin leaves the narrower pin intact
      When the client posts to "/v1/release" with target "docs:/tax/"
      Then the response status is 200
      And "/v1/pins" does not contain target "docs:/tax/"
      And "/v1/pins" still contains target "docs:/tax/2022/invoice-123.pdf"
      And file "docs:/tax/2022/invoice-123.pdf" remains hot

    Scenario: Releasing a narrower pin leaves the broader pin intact
      When the client posts to "/v1/release" with target "docs:/tax/2022/invoice-123.pdf"
      Then the response status is 200
      And "/v1/pins" still contains target "docs:/tax/"
      And "/v1/pins" does not contain target "docs:/tax/2022/invoice-123.pdf"
      And file "docs:/tax/2022/invoice-123.pdf" remains hot

    Scenario: Releasing a missing pin is a successful no-op
      Given target "docs:/missing/" is not pinned
      When the client posts to "/v1/release" with target "docs:/missing/"
      Then the response status is 200
      And pin is false

  Rule: Selectors are canonical and precise
    Scenario Outline: Invalid targets are rejected for pin
      When the client posts to "/v1/pin" with target "<target>"
      Then the response status is 400
      And the error code is "invalid_target"

      Examples:
        | target       |
        | docs:        |
        | docs:raw/    |
        | docs:/a/../b |
        | docs://raw/  |

    Scenario Outline: Invalid targets are rejected for release
      When the client posts to "/v1/release" with target "<target>"
      Then the response status is 400
      And the error code is "invalid_target"

      Examples:
        | target       |
        | docs:        |
        | docs:raw/    |
        | docs:/a/../b |
        | docs://raw/  |
