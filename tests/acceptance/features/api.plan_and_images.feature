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
    And each image contains "id", "bytes", "fill", "files", "collections", and "iso_ready"
    And each image fill equals image bytes divided by target bytes
    And images are returned in best-first order

  Scenario: Read one image summary
    Given image "img_2026-04-20_01" exists
    When the client gets "/v1/images/img_2026-04-20_01"
    Then the response status is 200
    And the response contains image id "img_2026-04-20_01"
    And the response contains "bytes", "fill", "files", "collections", and "iso_ready"

  Scenario: Download an ISO for a ready image
    Given image "img_2026-04-20_01" has iso_ready true
    When the client gets "/v1/images/img_2026-04-20_01/iso"
    Then the response status is 200
    And the response body is binary ISO content

  Rule: Registering a copy increases archived coverage
    Background:
      Given image "img_2026-04-20_01" covers bytes from collection "docs"

    Scenario: Register a physical copy
      When the client posts to "/v1/images/img_2026-04-20_01/copies" with id "BR-021-A" and location "Shelf B1"
      Then the response status is 200
      And the response contains copy id "BR-021-A"
      And the response contains image id "img_2026-04-20_01"
      And collection "docs" archived_bytes increases
      And collection "docs" pending_bytes decreases

    Scenario: Reusing a copy id fails
      Given copy "BR-021-A" already exists
      When the client posts to "/v1/images/img_2026-04-20_01/copies" with id "BR-021-A" and location "Shelf B2"
      Then the response status is 409
      And the error code is "conflict"
