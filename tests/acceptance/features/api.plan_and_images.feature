@acceptance @api @mvp
Feature: Plan and images API
  The API reports the current best image plan, exposes image summaries, and registers physical copies.

  Background:
    Given an archive with planner fixtures
    And the planner has at least one candidate image

  Scenario: Read the current plan
    When the client gets "/v1/plan"
    Then the response status is 200
    And the response contains "ready", "target_bytes", "min_fill_bytes", "images", and "unplanned_bytes"
    And each image contains "id", "volume_id", "bytes", "fill", "files", "collections", and "iso_ready"
    And each image fill equals image bytes divided by target bytes
    And images are returned in best-first order

  Scenario: Read one image summary
    Given image "img_2026-04-20_01" exists
    When the client gets "/v1/images/img_2026-04-20_01"
    Then the response status is 200
    And the response contains image id "img_2026-04-20_01"
    And the response contains "volume_id", "bytes", "fill", "files", "collections", and "iso_ready"
    And the response field "volume_id" is null

  Scenario: First ISO download finalizes an image and stores volume_id
    Given image "img_2026-04-20_01" exists
    When the client gets "/v1/images/img_2026-04-20_01"
    Then the response status is 200
    And the response field "volume_id" is null
    When the client gets "/v1/images/img_2026-04-20_01/iso"
    Then the response status is 200
    And the response body is binary ISO content
    When the client gets "/v1/images/img_2026-04-20_01"
    Then the response status is 200
    And the response field "volume_id" matches compact UTC timestamp

  Scenario: Download an ISO for a ready image
    Given image "img_2026-04-20_01" has iso_ready true
    When the client gets "/v1/images/img_2026-04-20_01/iso"
    Then the response status is 200
    And the response body is binary ISO content

  Rule: Downloaded ISOs match the published disc contracts
    Scenario: A ready image uses the canonical disc layout and metadata contracts
      Given image "img_2026-04-20_01" has iso_ready true
      When the client downloads and inspects ISO for image "img_2026-04-20_01"
      Then the response status is 200
      And the downloaded ISO passes xorriso verification
      And the extracted ISO root matches the disc layout contract
      And the decrypted disc manifest matches the disc manifest contract
      And every referenced collection manifest matches the collection hash manifest contract
      And every referenced file sidecar matches the file sidecar contract
      And the current ISO README documents split-file recovery
      And the current ISO payload for "docs:/tax/2022/invoice-123.pdf" decrypts to the original plaintext

    Scenario: Split image parts are listed per disc and reconstruct the logical file
      Given an archive with split planner fixtures
      When the client downloads and inspects ISO for image "img_2026-04-20_03"
      Then the response status is 200
      And the downloaded ISO passes xorriso verification
      And the extracted ISO root matches the disc layout contract
      And the decrypted disc manifest matches the disc manifest contract
      And every referenced collection manifest matches the collection hash manifest contract
      And every referenced file sidecar matches the file sidecar contract
      And the current ISO lists split file "/tax/2022/invoice-123.pdf" part 1 of 2
      And the current split payload for "/tax/2022/invoice-123.pdf" is recorded
      When the client downloads and inspects ISO for image "img_2026-04-20_04"
      Then the response status is 200
      And the downloaded ISO passes xorriso verification
      And the extracted ISO root matches the disc layout contract
      And the decrypted disc manifest matches the disc manifest contract
      And every referenced collection manifest matches the collection hash manifest contract
      And every referenced file sidecar matches the file sidecar contract
      And the current ISO lists split file "/tax/2022/invoice-123.pdf" part 2 of 2
      And the current split payload for "/tax/2022/invoice-123.pdf" is recorded
      And the recorded split payloads for "docs:/tax/2022/invoice-123.pdf" reconstruct the original plaintext

  Rule: Registering a copy increases archived coverage
    Background:
      Given image "img_2026-04-20_01" covers bytes from collection "docs"

    Scenario: Register a physical copy
      When the client gets "/v1/images/img_2026-04-20_01/iso"
      Then the response status is 200
      When the client posts to "/v1/images/img_2026-04-20_01/copies" with id "BR-021-A" and location "Shelf B1"
      Then the response status is 200
      And the response contains copy id "BR-021-A"
      And the response contains image id "img_2026-04-20_01"
      And the response copy contains "volume_id", "location", and "created_at"
      And collection "docs" archived_bytes increases
      And collection "docs" pending_bytes decreases

    Scenario: Reusing a copy id for the same finalized image fails
      Given copy "BR-021-A" already exists
      When the client posts to "/v1/images/img_2026-04-20_01/copies" with id "BR-021-A" and location "Shelf B2"
      Then the response status is 409
      And the error code is "conflict"
