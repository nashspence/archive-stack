@acceptance @cli @mvp
Feature: arc-disc burn CLI
  The optical CLI clears a burn backlog only after each generated copy id is explicitly confirmed as labeled.

  Scenario: arc-disc burn finalizes one ready image and clears its two-copy backlog
    Given an archive with planner fixtures
    And the burn fixture confirms labeled copy id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the burn fixture confirms labeled copy id "20260420T040001Z-2" at location "vault-b/shelf-01"
    When the operator runs 'arc-disc burn --device /dev/fake-sr0'
    Then the command exits with code 0
    And stdout mentions "burn backlog cleared"
    And stdout mentions "20260420T040001Z-1"
    And stdout mentions "20260420T040001Z-2"
    And image "20260420T040001Z" has physical_copies_registered 2
    And copy "20260420T040001Z-1" for image "20260420T040001Z" state is "verified"
    And copy "20260420T040001Z-2" for image "20260420T040001Z" verification_state is "verified"

  Scenario: arc-disc burn does not register a copy before labeled confirmation and resumes there
    Given an archive with planner fixtures
    When the operator runs 'arc-disc burn --device /dev/fake-sr0'
    Then the command exits non-zero
    And stderr mentions "label confirmation"
    And image "20260420T040001Z" has physical_copies_registered 0
    And copy "20260420T040001Z-1" for image "20260420T040001Z" state is "needed"
    When the burn fixture says unlabeled copy id "20260420T040001Z-1" is still available
    And the burn fixture confirms labeled copy id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the burn fixture confirms labeled copy id "20260420T040001Z-2" at location "vault-b/shelf-01"
    And the operator runs 'arc-disc burn --device /dev/fake-sr0'
    Then the command exits with code 0
    And stderr mentions "resuming label confirmation for 20260420T040001Z-1"
    And stderr does not mention "burning copy 20260420T040001Z-1"
    And image "20260420T040001Z" has physical_copies_registered 2

  Scenario: arc-disc burn resumes from burned-media verification for an available unfinished disc
    Given an archive with planner fixtures
    And the burn fixture fails while verifying burned media for copy id "20260420T040001Z-1"
    When the operator runs 'arc-disc burn --device /dev/fake-sr0'
    Then the command exits non-zero
    And stderr mentions "verifying burned media for 20260420T040001Z-1"
    And image "20260420T040001Z" has physical_copies_registered 0
    When the burn fixture says unlabeled copy id "20260420T040001Z-1" is still available
    And the burn fixture clears all burn failures
    And the burn fixture confirms labeled copy id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the burn fixture confirms labeled copy id "20260420T040001Z-2" at location "vault-b/shelf-01"
    And the operator runs 'arc-disc burn --device /dev/fake-sr0'
    Then the command exits with code 0
    And stderr mentions "verifying burned media for 20260420T040001Z-1"
    And stderr does not mention "burning copy 20260420T040001Z-1"
    And image "20260420T040001Z" has physical_copies_registered 2

  Scenario: arc-disc burn re-burns an unfinished unlabeled copy if that disc is unavailable
    Given an archive with planner fixtures
    When the operator runs 'arc-disc burn --device /dev/fake-sr0'
    Then the command exits non-zero
    And image "20260420T040001Z" has physical_copies_registered 0
    When the burn fixture says unlabeled copy id "20260420T040001Z-1" is unavailable
    And the burn fixture confirms labeled copy id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the burn fixture confirms labeled copy id "20260420T040001Z-2" at location "vault-b/shelf-01"
    And the operator runs 'arc-disc burn --device /dev/fake-sr0'
    Then the command exits with code 0
    And stderr mentions "unlabeled disc for 20260420T040001Z-1 is unavailable; restarting burn"
    And stderr mentions "burning copy 20260420T040001Z-1"
    And image "20260420T040001Z" has physical_copies_registered 2

  Scenario: arc-disc burn re-downloads an invalid staged ISO before finishing the backlog
    Given an archive with planner fixtures
    And the burn fixture confirms labeled copy id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the burn fixture fails while burning copy id "20260420T040001Z-2"
    When the operator runs 'arc-disc burn --device /dev/fake-sr0'
    Then the command exits non-zero
    And image "20260420T040001Z" has physical_copies_registered 1
    When the staged ISO for image "20260420T040001Z" is corrupted
    And the burn fixture clears all burn failures
    And the burn fixture confirms labeled copy id "20260420T040001Z-2" at location "vault-b/shelf-01"
    And the operator runs 'arc-disc burn --device /dev/fake-sr0'
    Then the command exits with code 0
    And stderr mentions "staged ISO is invalid"
    And stderr mentions "re-downloading"
    And image "20260420T040001Z" has physical_copies_registered 2
