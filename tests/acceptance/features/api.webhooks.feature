@acceptance @api @mvp
Feature: Outbound operator webhooks
  Test harnesses capture outbound operator notifications so acceptance scenarios can
  assert emitted action-needed events and payload fields without adding product API surface.

  @todo @issue_206 @ci_opt_in @requires_webhook_capture @issue_186
  Scenario: Ready disc work webhook tells the operator to run arc-disc
    Given ordinary blank-disc work is available
    When Riverhog emits an action-needed notification for ready disc work
    Then the captured webhook payload matches "contracts/operator/action-needed-notification.schema.json"
    And the captured webhook payload matches operator notification copy "push_burn_work_ready"
    And the captured webhook payload field "event" equals "images.ready"
    And the captured webhook payload field "title" is present
    And the captured webhook payload field "body" is present
    And the captured webhook payload has exactly one action
    And the captured webhook action command equals "arc-disc"
    And the captured webhook action argv equals "arc-disc"

  @todo @issue_206 @ci_opt_in @requires_webhook_capture @issue_186
  Scenario: Image rebuild ready and reminder webhook deliveries are captured
    Given an archive with planner fixtures
    And collection "docs" has uploaded Glacier archive package
    And candidate "img_2026-04-20_01" is finalized
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    When the client posts to "/v1/recovery-sessions/rs-20260420T040001Z-rebuild-1/approve"
    And the client waits for captured webhook event "images.rebuild_ready"
    Then the captured webhook payload matches "contracts/operator/action-needed-notification.schema.json"
    And the captured webhook payload matches operator notification copy "push_recovery_ready"
    And the captured webhook payload field "session_id" equals "rs-20260420T040001Z-rebuild-1"
    And the captured webhook payload field "type" equals "image_rebuild"
    And the captured webhook payload images contain only "20260420T040001Z"
    And the captured webhook payload integer field "reminder_count" equals 0
    And the captured webhook payload field "title" is present
    And the captured webhook payload field "body" is present
    And the captured webhook payload has exactly one action
    And the captured webhook action command equals "arc-disc"
    And the captured webhook action argv equals "arc-disc"
    When the API process restarts
    And the client waits for captured webhook event "images.rebuild_ready.reminder"
    Then the captured webhook payload matches "contracts/operator/action-needed-notification.schema.json"
    And the captured webhook payload matches operator notification copy "push_recovery_ready"
    And the captured webhook payload field "session_id" equals "rs-20260420T040001Z-rebuild-1"
    And the captured webhook payload images contain only "20260420T040001Z"
    And the captured webhook payload integer field "reminder_count" equals 1
    And the captured webhook payload has exactly one action
    And the captured webhook action command equals "arc-disc"

  @todo @issue_206 @ci_opt_in @requires_webhook_capture @issue_186
  Scenario: Image rebuild ready webhook retries after a transient sink failure
    Given an archive with planner fixtures
    And collection "docs" has uploaded Glacier archive package
    And candidate "img_2026-04-20_01" is finalized
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    And the captured webhook sink fails event "images.rebuild_ready" with status 503 once
    When the client posts to "/v1/recovery-sessions/rs-20260420T040001Z-rebuild-1/approve"
    And the client waits for captured webhook attempt "images.rebuild_ready" result "failed"
    Then captured webhook event "images.rebuild_ready" has 0 successful deliveries
    And captured webhook event "images.rebuild_ready" has 1 attempts with result "failed"
    When the client waits for captured webhook event "images.rebuild_ready"
    Then the captured webhook payload matches "contracts/operator/action-needed-notification.schema.json"
    And the captured webhook payload matches operator notification copy "push_recovery_ready"
    And the captured webhook payload field "session_id" equals "rs-20260420T040001Z-rebuild-1"
    And the captured webhook payload images contain only "20260420T040001Z"
    And the captured webhook payload integer field "reminder_count" equals 0
    And the captured webhook payload has exactly one action
    And the captured webhook action command equals "arc-disc"
    And captured webhook event "images.rebuild_ready" has 1 successful deliveries
    And captured webhook event "images.rebuild_ready.reminder" has 0 successful deliveries
    And captured webhook attempt "images.rebuild_ready" result "delivered" attempt 1 happened at least 1 seconds after result "failed" attempt 1

  @todo @issue_206 @ci_opt_in @requires_webhook_capture @issue_186
  Scenario: Image rebuild ready webhook retries after a transient sink timeout
    Given an archive with planner fixtures
    And collection "docs" has uploaded Glacier archive package
    And candidate "img_2026-04-20_01" is finalized
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    And the captured webhook sink times out event "images.rebuild_ready" once
    When the client posts to "/v1/recovery-sessions/rs-20260420T040001Z-rebuild-1/approve"
    And the client waits for captured webhook attempt "images.rebuild_ready" result "timeout"
    Then captured webhook event "images.rebuild_ready" has 0 successful deliveries
    And captured webhook event "images.rebuild_ready" has 1 attempts with result "timeout"
    When the client waits up to 40 seconds for captured webhook event "images.rebuild_ready"
    Then the captured webhook payload matches "contracts/operator/action-needed-notification.schema.json"
    And the captured webhook payload matches operator notification copy "push_recovery_ready"
    And the captured webhook payload field "session_id" equals "rs-20260420T040001Z-rebuild-1"
    And the captured webhook payload images contain only "20260420T040001Z"
    And the captured webhook payload integer field "reminder_count" equals 0
    And the captured webhook payload has exactly one action
    And the captured webhook action command equals "arc-disc"
    And captured webhook event "images.rebuild_ready" has 1 successful deliveries
    And captured webhook event "images.rebuild_ready.reminder" has 0 successful deliveries
    And captured webhook attempt "images.rebuild_ready" result "delivered" attempt 1 happened at least 1 seconds after result "timeout" attempt 1

  @todo @issue_206 @ci_opt_in @requires_webhook_capture @issue_186
  Scenario: Persistent cloud backup failure webhook tells the operator to run arc
    Given collection "docs" has failed cloud backup after retries with error "s3 timeout"
    When the client waits for captured webhook event "collections.glacier_upload.failed"
    Then the captured webhook payload matches "contracts/operator/action-needed-notification.schema.json"
    And the captured webhook payload matches operator notification copy "push_cloud_backup_failed"
    And the captured webhook payload field "collection_id" equals "docs"
    And the captured webhook payload field "error" equals "s3 timeout"
    And the captured webhook payload integer field "attempts" equals 2
    And the captured webhook payload field "title" is present
    And the captured webhook payload field "body" is present
    And the captured webhook payload has exactly one action
    And the captured webhook action command equals "arc"
    And the captured webhook action argv equals "arc"

  @todo @issue_206 @ci_opt_in @requires_webhook_capture @issue_186
  Scenario: Labeling does not create a standalone notification
    Given an unlabeled verified disc is waiting for label confirmation
    When Riverhog delivers due action-needed notifications
    Then no captured webhook event asks only for labeling
    And contracts/operator/copy.py defines no labeling notification copy
    And no captured webhook action command includes a subcommand

  @todo @issue_206 @ci_opt_in @requires_webhook_capture @issue_186
  Scenario: Routine success does not create an action-needed notification
    Given a collection upload finishes successfully
    And disc work finishes successfully
    And hot storage recovery finishes successfully
    When Riverhog delivers due action-needed notifications
    Then no captured webhook event is emitted for routine success
    And contracts/operator/copy.py defines no routine-success notification copy
