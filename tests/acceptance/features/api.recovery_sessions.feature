@acceptance @api @mvp
Feature: Recovery sessions API
  Glacier-backed recovery sessions track cloud-fetch recovery and image rebuilds from collection-native archive packages.

  Background:
    Given an archive with planner fixtures
    And collection "docs" has uploaded Glacier archive package
  Scenario: Starting a hot cloud-fetch creates durable automatic restore sessions
    Given file "docs/tax/2022/invoice-123.pdf" is archived
    And target "docs/tax/2022/invoice-123.pdf" has a draft fetch
    When the client starts fetch "fx-1" for cloud
    Then the response status is 200
    When the client gets "/v1/fetches/fx-1/cloud-fetch"
    Then the response status is 200
    And the response cloud-fetch sessions contain recovery session "rs-docs-restore-1"
    When the client waits for recovery session "rs-docs-restore-1" state "completed"
    Then the response status is 200
    And the response recovery session state is "completed"
    When the API process restarts
    And the client gets "/v1/fetches/fx-1/cloud-fetch"
    Then the response status is 200
    And the response cloud-fetch sessions contain recovery session "rs-docs-restore-1"
  Scenario: Hot cloud-fetch verifies manifest and proof before completing automatically
    Given file "docs/tax/2022/invoice-123.pdf" is archived
    And target "docs/tax/2022/invoice-123.pdf" has a draft fetch
    And the client starts fetch "fx-1" for cloud
    When the client waits for recovery session "rs-docs-restore-1" state "completed"
    Then the response status is 200
    And the response recovery session type is "collection_restore"
    And the response recovery session state is "completed"
    And the response recovery session collection "docs" glacier state is "uploaded"
    And the response recovery session collection "docs" collection manifest state is "uploaded"
    And the response recovery session collection "docs" OTS proof state is "uploaded"
  Scenario: Hot cloud-fetch materializes selected files into hot storage automatically
    Given file "docs/tax/2022/invoice-123.pdf" is archived
    And target "docs/tax/2022/invoice-123.pdf" has a draft fetch
    When the client starts fetch "fx-1" for cloud
    And the client waits for recovery session "rs-docs-restore-1" state "completed"
    Then the response status is 200
    And the response recovery session state is "completed"
    And the response recovery session archive_verification is "completed"
    And the response recovery session extraction is "completed"
    And the response recovery session materialization is "completed"
    And target "docs/tax/2022/invoice-123.pdf" is hot
  Scenario: Losing the last protected copy creates an image rebuild session
    Given candidate "img_2026-04-20_01" is finalized
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"
    When the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    And the client gets "/v1/images/20260420T040001Z/rebuild-session"
    Then the response status is 200
    And the response recovery session type is "image_rebuild"
    And the response recovery session id is "rs-20260420T040001Z-rebuild-1"
    And the response recovery session state is "restore_requested"
    And the response recovery session collections include "docs"
    And the response recovery session images contain only "20260420T040001Z"
    And the response recovery session image "20260420T040001Z" rebuild_state is "restoring_collections"
  Scenario: Image rebuild stages a rebuilt ISO from restored collection archives
    Given candidate "img_2026-04-20_01" is finalized
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"
    When the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    And the client gets "/v1/images/20260420T040001Z/rebuild-session"
    Then the response status is 200
    And the response recovery session state is "restore_requested"
    And the response recovery session image "20260420T040001Z" rebuild_state is "restoring_collections"
    When the client waits for recovery session "rs-20260420T040001Z-rebuild-1" state "ready"
    Then the response status is 200
    And the response recovery session type is "image_rebuild"
    And the response recovery session image "20260420T040001Z" rebuild_state is "ready"
    When the client gets "/v1/recovery-sessions/rs-20260420T040001Z-rebuild-1/images/20260420T040001Z/iso"
    Then the response status is 200
    And the response body is binary ISO content
