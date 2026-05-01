@acceptance @cli @mvp
Feature: arc-disc burn CLI
  The optical CLI clears a burn backlog only after each generated copy id is explicitly confirmed as labeled.

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc burn finalizes one ready image and clears its two-copy backlog
    Given an archive with planner fixtures
    And the burn fixture confirms labeled copy id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the burn fixture confirms labeled copy id "20260420T040001Z-2" at location "vault-b/shelf-01"
    When the operator runs 'arc-disc burn --device /dev/arc-optical0'
    Then the command exits with code 0
    And stdout mentions "burn backlog cleared"
    And stdout mentions "20260420T040001Z-1"
    And stdout mentions "20260420T040001Z-2"
    And image "20260420T040001Z" has physical_copies_registered 2
    And copy "20260420T040001Z-1" for image "20260420T040001Z" state is "verified"
    And copy "20260420T040001Z-2" for image "20260420T040001Z" verification_state is "verified"

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc burn uses a fresh replacement id after one confirmed copy is reported lost
    Given an archive with planner fixtures
    And copy "20260420T040001Z-1" already exists
    And copy "20260420T040001Z-2" already exists
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    Then the response status is 200
    And the burn fixture confirms labeled copy id "20260420T040001Z-3" at location "vault-c/shelf-01"
    When the operator runs 'arc-disc burn --device /dev/arc-optical0'
    Then the command exits with code 0
    And stdout mentions "20260420T040001Z-3"
    And stderr does not mention copy id "20260420T040001Z-1"
    And image "20260420T040001Z" has physical_copies_registered 2
    And copy "20260420T040001Z-1" for image "20260420T040001Z" state is "lost"
    And copy "20260420T040001Z-3" for image "20260420T040001Z" state is "verified"
  Scenario: arc-disc burn reports image rebuild work instead of ordinary replacement backlog
    Given an archive with planner fixtures
    And collection "docs" has uploaded Glacier archive package
    And candidate "img_2026-04-20_01" is finalized
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/copies" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-2" with state "damaged"
    When the operator runs 'arc-disc burn --device /dev/arc-optical0'
    Then the command exits with code 0
    And stdout mentions "burn backlog already clear"
    And stdout mentions "image rebuild work remains"
    And stdout mentions "rs-20260420T040001Z-rebuild-1"
    And stdout mentions "pending_approval"
    And stdout does not mention "20260420T040001Z-3"
    And copy "20260420T040001Z-3" for image "20260420T040001Z" state is "needed"

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc burn does not register a copy before labeled confirmation and resumes there
    Given an archive with planner fixtures
    When the operator runs 'arc-disc burn --device /dev/arc-optical0'
    Then the command exits non-zero
    And stderr mentions "label confirmation"
    And image "20260420T040001Z" has physical_copies_registered 0
    And copy "20260420T040001Z-1" for image "20260420T040001Z" state is "needed"
    When the burn fixture says unlabeled copy id "20260420T040001Z-1" is still available
    And the burn fixture confirms labeled copy id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the burn fixture confirms labeled copy id "20260420T040001Z-2" at location "vault-b/shelf-01"
    And the operator runs 'arc-disc burn --device /dev/arc-optical0'
    Then the command exits with code 0
    And stderr does not mention "burning copy 20260420T040001Z-1"
    And image "20260420T040001Z" has physical_copies_registered 2

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc burn resumes from burned-media verification for an available unfinished disc
    Given an archive with planner fixtures
    And the burn fixture fails while verifying burned media for copy id "20260420T040001Z-1"
    When the operator runs 'arc-disc burn --device /dev/arc-optical0'
    Then the command exits non-zero
    And stderr mentions "verifying burned media for 20260420T040001Z-1"
    And image "20260420T040001Z" has physical_copies_registered 0
    When the burn fixture says unlabeled copy id "20260420T040001Z-1" is still available
    And the burn fixture clears all burn failures
    And the burn fixture confirms labeled copy id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the burn fixture confirms labeled copy id "20260420T040001Z-2" at location "vault-b/shelf-01"
    And the operator runs 'arc-disc burn --device /dev/arc-optical0'
    Then the command exits with code 0
    And stderr does not mention "burning copy 20260420T040001Z-1"
    And image "20260420T040001Z" has physical_copies_registered 2

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc burn re-burns an unfinished unlabeled copy if that disc is unavailable
    Given an archive with planner fixtures
    When the operator runs 'arc-disc burn --device /dev/arc-optical0'
    Then the command exits non-zero
    And image "20260420T040001Z" has physical_copies_registered 0
    When the burn fixture says unlabeled copy id "20260420T040001Z-1" is unavailable
    And the burn fixture confirms labeled copy id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the burn fixture confirms labeled copy id "20260420T040001Z-2" at location "vault-b/shelf-01"
    And the operator runs 'arc-disc burn --device /dev/arc-optical0'
    Then the command exits with code 0
    And stderr mentions "burning copy 20260420T040001Z-1"
    And image "20260420T040001Z" has physical_copies_registered 2

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc burn re-downloads an invalid staged ISO before finishing the backlog
    Given an archive with planner fixtures
    And the burn fixture confirms labeled copy id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the burn fixture fails while burning copy id "20260420T040001Z-2"
    When the operator runs 'arc-disc burn --device /dev/arc-optical0'
    Then the command exits non-zero
    And image "20260420T040001Z" has physical_copies_registered 1
    When the staged ISO for image "20260420T040001Z" is corrupted
    And the burn fixture clears all burn failures
    And the burn fixture confirms labeled copy id "20260420T040001Z-2" at location "vault-b/shelf-01"
    And the operator runs 'arc-disc burn --device /dev/arc-optical0'
    Then the command exits with code 0
    And stderr mentions "staged ISO is invalid"
    And stderr mentions "re-downloading"
    And image "20260420T040001Z" has physical_copies_registered 2
