@acceptance @api @mvp
Feature: Archive restores API
  Archive-backed restores track fetch materialization and disc rebuilds from collection-native archive packages.

  Background:
    Given an archive with planner fixtures
    And collection "docs" has uploaded archive package
  Scenario: Starting a hot fetch materialization creates durable automatic archive restore
    Given file "docs/tax/2022/invoice-123.pdf" is not hot
    And target "docs/tax/2022/invoice-123.pdf" has a draft fetch
    When the client starts fetch "fx-1" for archive
    Then the response status is 200
    When the client gets "/v1/fetches/fx-1/status"
    Then the response status is 200
    And the response archive restores contain archive restore "ar-docs-restore-1"
    When the client waits for archive restore "ar-docs-restore-1" state "completed"
    Then the response status is 200
    And the response archive restore state is "completed"
    When the API process restarts
    And the client gets "/v1/fetches/fx-1/status"
    Then the response status is 200
    And the response archive restores contain archive restore "ar-docs-restore-1"
  Scenario: Hot fetch materialization verifies manifest and proof before completing automatically
    Given file "docs/tax/2022/invoice-123.pdf" is not hot
    And target "docs/tax/2022/invoice-123.pdf" has a draft fetch
    And the client starts fetch "fx-1" for archive
    When the client waits for archive restore "ar-docs-restore-1" state "completed"
    Then the response status is 200
    And the response archive restore type is "fetch_materialization"
    And the response archive restore state is "completed"
    And the response archive restore collection "docs" archive state is "uploaded"
    And the response archive restore collection "docs" collection manifest state is "uploaded"
    And the response archive restore collection "docs" OTS proof state is "uploaded"
  Scenario: Hot fetch materialization materializes selected files into hot storage automatically
    Given file "docs/tax/2022/invoice-123.pdf" is not hot
    And target "docs/tax/2022/invoice-123.pdf" has a draft fetch
    When the client starts fetch "fx-1" for archive
    And the client waits for archive restore "ar-docs-restore-1" state "completed"
    Then the response status is 200
    And the response archive restore state is "completed"
    And the response archive restore archive_verification is "completed"
    And the response archive restore extraction is "completed"
    And the response archive restore materialization is "completed"
    And target "docs/tax/2022/invoice-123.pdf" is hot
  Scenario: Losing the last usable disc creates a disc rebuild archive restore
    Given candidate "img_2026-04-20_01" is finalized
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-2" and location "Shelf B1"
    When the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-2" with state "damaged"
    And the client gets "/v1/images/20260420T040001Z/disc-rebuild"
    Then the response status is 200
    And the response archive restore type is "disc_rebuild"
    And the response archive restore id is "ar-20260420T040001Z-rebuild-1"
    And the response archive restore state is "requested"
    And the response archive restore collections include "docs"
    And the response archive restore images contain only "20260420T040001Z"
    And the response archive restore image "20260420T040001Z" rebuild_state is "restoring_collections"
  Scenario: Active recovery inventory is filtered and paged by the API
    Given candidate "img_2026-04-20_01" is finalized
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-2" and location "Shelf B1"
    When the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-2" with state "damaged"
    And the client gets "/v1/archive-restores?terminal=active&type=disc_rebuild&page=1&per_page=1"
    Then the response status is 200
    And the response archive restore list terminal is "active"
    And the response archive restore list contains only "ar-20260420T040001Z-rebuild-1"
    And the response pagination is page 1 with per_page 1 and total 1 and pages 1
  Scenario: Disc rebuild stages a rebuilt ISO from restored collection archives
    Given candidate "img_2026-04-20_01" is finalized
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-2" and location "Shelf B1"
    When the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-2" with state "damaged"
    And the client gets "/v1/images/20260420T040001Z/disc-rebuild"
    Then the response status is 200
    And the response archive restore state is "requested"
    And the response archive restore image "20260420T040001Z" rebuild_state is "restoring_collections"
    When the client waits for archive restore "ar-20260420T040001Z-rebuild-1" state "ready"
    Then the response status is 200
    And the response archive restore type is "disc_rebuild"
    And the response archive restore image "20260420T040001Z" rebuild_state is "ready"
    When the client gets "/v1/archive-restores/ar-20260420T040001Z-rebuild-1/images/20260420T040001Z/iso"
    Then the response status is 200
    And the response body is binary ISO content
