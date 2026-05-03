@acceptance @cli @mvp
Feature: arc-disc recover CLI
  The optical CLI discovers and resumes image rebuild sessions for finalized images that lost all protected copies.
  @contract_gap @issue_313
  Scenario: expired recovery with local artifacts resumes without another approval
    Given statechart "arc_disc.recovery" state "expired_local_resume" is the accepted operator contract
    And recovery session "rs-20260420T040001Z-rebuild-1" has expired
    And local staged recovery artifacts are available
    When the operator runs arc-disc recover "rs-20260420T040001Z-rebuild-1"
    Then stdout includes operator copy "recovery_expired_local_resume"
    And stdout mentions "local staged recovery files"
    And stdout does not mention "Approve again"

  @contract_gap @issue_313
  Scenario: expired recovery without local artifacts returns to approval
    Given statechart "arc_disc.recovery" state "expired_needs_reapproval" is the accepted operator contract
    And recovery session "rs-20260420T040001Z-rebuild-1" has expired
    And local staged recovery artifacts are absent
    When the operator runs arc-disc recover "rs-20260420T040001Z-rebuild-1"
    Then stdout includes operator copy "recovery_expired_needs_reapproval"
    And stdout mentions "Approve again"
    And stdout mentions "Estimated cost"
    And stdout does not mention "Run arc-disc to review the next safe recovery step"

  Scenario: arc-disc recover lists one multi-image pending rebuild session
    Given an archive with planned images
    And an archive with split planned images
    And collection "docs" has uploaded Glacier archive package
    And candidate "img_2026-04-20_01" is finalized
    And candidate "img_2026-04-20_03" is finalized
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client posts to "/v1/images/20260420T040003Z/copies" with id "20260420T040003Z-1" and location "Shelf C1"
    And the client posts to "/v1/images/20260420T040003Z/copies" with id "20260420T040003Z-2" and location "Shelf D1"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    And the client patches "/v1/images/20260420T040003Z/copies/20260420T040003Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040003Z/copies/20260420T040003Z-2" with state "damaged"
    When the operator runs 'arc-disc recover'
    Then the command exits with code 0
    And stdout mentions "rs-20260420T040001Z-rebuild-1"
    And stdout mentions "image_rebuild"
    And stdout mentions "pending_approval"
    And stdout mentions "20260420T040001Z"
    And stdout mentions "20260420T040003Z"
  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc recover resumes one ready multi-image rebuild session and cleans up staged ISOs
    Given an archive with planned images
    And an archive with split planned images
    And collection "docs" has uploaded Glacier archive package
    And candidate "img_2026-04-20_01" is finalized
    And candidate "img_2026-04-20_03" is finalized
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client posts to "/v1/images/20260420T040003Z/copies" with id "20260420T040003Z-1" and location "Shelf C1"
    And the client posts to "/v1/images/20260420T040003Z/copies" with id "20260420T040003Z-2" and location "Shelf D1"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    And the client patches "/v1/images/20260420T040003Z/copies/20260420T040003Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040003Z/copies/20260420T040003Z-2" with state "damaged"
    When the operator runs arc-disc recover "rs-20260420T040001Z-rebuild-1"
    Then the command exits with code 0
    And stdout mentions "rebuild session rs-20260420T040001Z-rebuild-1 is restore_requested"
    And burned-media verification fails for copy id "20260420T040001Z-3"
    And the operator confirms labeled copy id "20260420T040001Z-4" at location "vault-b/shelf-02"
    And the operator confirms labeled copy id "20260420T040003Z-3" at location "vault-c/shelf-02"
    And the operator confirms labeled copy id "20260420T040003Z-4" at location "vault-d/shelf-02"
    When the client waits for recovery session "rs-20260420T040001Z-rebuild-1" state "ready"
    And the operator runs arc-disc recover "rs-20260420T040001Z-rebuild-1"
    Then the command exits non-zero
    When unlabeled copy id "20260420T040001Z-3" is still available
    And the optical burn boundary is healthy again
    And the operator confirms labeled copy id "20260420T040001Z-3" at location "vault-a/shelf-02"
    And the operator runs arc-disc recover "rs-20260420T040001Z-rebuild-1"
    Then the command exits with code 0
    And stdout mentions "rebuild session rs-20260420T040001Z-rebuild-1 completed"
    And stderr mentions "verifying burned media for 20260420T040001Z-3"
    And stderr does not mention "burning copy 20260420T040001Z-3"
    And the client gets "/v1/recovery-sessions/rs-20260420T040001Z-rebuild-1"
    And the response status is 200
    And the response recovery session state is "completed"
    And copy "20260420T040001Z-4" for image "20260420T040001Z" state is "verified"
    And the staged ISO for image "20260420T040001Z" is absent
    And the staged ISO for image "20260420T040003Z" is absent
