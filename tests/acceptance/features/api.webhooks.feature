@acceptance @api @mvp
Feature: Outbound operator webhooks
  Test harnesses capture outbound operator notifications so acceptance scenarios can
  assert emitted events and payload fields without adding product API surface.

  @spec_harness_only
  Scenario: Recovery ready and reminder webhook deliveries are captured
    Given an archive with planner fixtures
    And candidate "img_2026-04-20_01" is finalized
    And the client waits for image "20260420T040001Z" glacier state "uploaded"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    When the client posts to "/v1/recovery-sessions/rs-20260420T040001Z-1/approve"
    And the client waits for captured webhook event "images.recovery_ready"
    Then the captured webhook payload field "session_id" equals "rs-20260420T040001Z-1"
    And the captured webhook payload images contain only "20260420T040001Z"
    And the captured webhook payload integer field "reminder_count" equals 0
    When the API process restarts
    And the client waits for captured webhook event "images.recovery_ready.reminder"
    Then the captured webhook payload field "session_id" equals "rs-20260420T040001Z-1"
    And the captured webhook payload images contain only "20260420T040001Z"
    And the captured webhook payload integer field "reminder_count" equals 1

  @spec_harness_only
  Scenario: Recovery ready webhook retries after a transient sink failure
    Given an archive with planner fixtures
    And candidate "img_2026-04-20_01" is finalized
    And the client waits for image "20260420T040001Z" glacier state "uploaded"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    And the captured webhook sink fails event "images.recovery_ready" with status 503 once
    When the client posts to "/v1/recovery-sessions/rs-20260420T040001Z-1/approve"
    And the client waits for captured webhook attempt "images.recovery_ready" result "failed"
    Then captured webhook event "images.recovery_ready" has 0 successful deliveries
    And captured webhook event "images.recovery_ready" has 1 attempts with result "failed"
    When the client waits for captured webhook event "images.recovery_ready"
    Then the captured webhook payload field "session_id" equals "rs-20260420T040001Z-1"
    And the captured webhook payload images contain only "20260420T040001Z"
    And the captured webhook payload integer field "reminder_count" equals 0
    And captured webhook event "images.recovery_ready" has 1 successful deliveries
    And captured webhook event "images.recovery_ready.reminder" has 0 successful deliveries
    And captured webhook attempt "images.recovery_ready" result "delivered" attempt 1 happened at least 1 seconds after result "failed" attempt 1

  @spec_harness_only
  Scenario: Recovery ready webhook retries after a transient sink timeout
    Given an archive with planner fixtures
    And candidate "img_2026-04-20_01" is finalized
    And the client waits for image "20260420T040001Z" glacier state "uploaded"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    And the captured webhook sink times out event "images.recovery_ready" once
    When the client posts to "/v1/recovery-sessions/rs-20260420T040001Z-1/approve"
    And the client waits for captured webhook attempt "images.recovery_ready" result "timeout"
    Then captured webhook event "images.recovery_ready" has 0 successful deliveries
    And captured webhook event "images.recovery_ready" has 1 attempts with result "timeout"
    When the client waits up to 40 seconds for captured webhook event "images.recovery_ready"
    Then the captured webhook payload field "session_id" equals "rs-20260420T040001Z-1"
    And the captured webhook payload images contain only "20260420T040001Z"
    And the captured webhook payload integer field "reminder_count" equals 0
    And captured webhook event "images.recovery_ready" has 1 successful deliveries
    And captured webhook event "images.recovery_ready.reminder" has 0 successful deliveries
    And captured webhook attempt "images.recovery_ready" result "delivered" attempt 1 happened at least 1 seconds after result "timeout" attempt 1

  @spec_harness_only
  Scenario: Persistent Glacier upload failure webhook is captured
    Given an archive with planner fixtures
    And the glacier upload fixture fails for image "20260420T040001Z" with error "s3 timeout"
    When candidate "img_2026-04-20_01" is finalized
    And the client waits for image "20260420T040001Z" glacier state "failed"
    And the client waits for captured webhook event "images.glacier_upload.failed"
    Then the captured webhook payload field "image_id" equals "20260420T040001Z"
    And the captured webhook payload field "error" equals "s3 timeout"
    And the captured webhook payload integer field "attempts" equals 2
