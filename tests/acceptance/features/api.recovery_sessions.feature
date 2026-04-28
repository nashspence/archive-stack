@acceptance @api @mvp
Feature: Recovery sessions API
  Glacier-backed recovery sessions track operator approval and restore lifecycle once a finalized image loses all protected copies.

  Background:
    Given an archive with planner fixtures
    And candidate "img_2026-04-20_01" is finalized
    And the client waits for image "20260420T040001Z" glacier state "uploaded"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"

  Scenario: Losing the last protected copy creates a durable pending-approval recovery session
    When the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    And the client gets "/v1/images/20260420T040001Z/recovery-session"
    Then the response status is 200
    And the response recovery session id is "rs-20260420T040001Z-1"
    And the response recovery session state is "pending_approval"
    And the response recovery session estimated cost is greater than 0
    And the response recovery session images contain only "20260420T040001Z"
    When the API process restarts
    And the client gets "/v1/images/20260420T040001Z/recovery-session"
    Then the response status is 200
    And the response recovery session id is "rs-20260420T040001Z-1"
    And the response recovery session state is "pending_approval"

  Scenario: Approving a recovery session progresses to ready and can be completed
    Given the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    When the client posts to "/v1/recovery-sessions/rs-20260420T040001Z-1/approve"
    Then the response status is 200
    And the response recovery session state is "restore_requested"
    When the client waits for recovery session "rs-20260420T040001Z-1" state "ready"
    Then the response status is 200
    And the response recovery session state is "ready"
    And the response recovery session latest_message contains "ready"
    When the client posts to "/v1/recovery-sessions/rs-20260420T040001Z-1/complete"
    Then the response status is 200
    And the response recovery session state is "completed"
    And the response recovery session latest_message contains "cleaned up immediately"

  Scenario: An expired recovery session requires re-initiation
    Given the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    And the client posts to "/v1/recovery-sessions/rs-20260420T040001Z-1/approve"
    When the client waits for recovery session "rs-20260420T040001Z-1" state "expired"
    Then the response status is 200
    And the response recovery session state is "expired"
    And the response recovery session latest_message contains "re-initiate recovery"
    When the client posts to "/v1/recovery-sessions/rs-20260420T040001Z-1/approve"
    Then the response status is 409
    And the error code is "invalid_state"
    And the error message contains "re-initiate recovery"
    When the client posts to "/v1/images/20260420T040001Z/recovery-session"
    Then the response status is 200
    And the response recovery session id is "rs-20260420T040001Z-2"
    And the response recovery session state is "pending_approval"
