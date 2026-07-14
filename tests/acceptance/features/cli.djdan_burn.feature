@acceptance @cli @mvp
Feature: djdan burn CLI
  The optical CLI clears a burn backlog only after each generated disc id is explicitly confirmed as labeled.

  Scenario: djdan burn finalizes one ready image and clears its two-disc backlog
    Given an archive with planned images
    And the operator confirms labeled disc id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the operator confirms labeled disc id "20260420T040001Z-2" at location "vault-b/shelf-01"
    When the operator runs djdan burn
    Then the command exits with code 0
    And stdout mentions "burn backlog cleared"
    And stdout mentions "20260420T040001Z-1"
    And stdout mentions "20260420T040001Z-2"
    And image "20260420T040001Z" has discs_registered 2
    And disc "20260420T040001Z-1" for image "20260420T040001Z" state is "verified"
    And disc "20260420T040001Z-2" for image "20260420T040001Z" verification_state is "verified"

  Scenario: djdan burn uses a fresh replacement id after one confirmed disc is reported lost
    Given an archive with planned images
    And disc "20260420T040001Z-1" already exists
    And disc "20260420T040001Z-2" already exists
    And the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-1" with state "lost"
    Then the response status is 200
    And the operator confirms labeled disc id "20260420T040001Z-3" at location "vault-c/shelf-01"
    When the operator runs djdan burn
    Then the command exits with code 0
    And stdout mentions "20260420T040001Z-3"
    And stderr does not mention disc id "20260420T040001Z-1"
    And image "20260420T040001Z" has discs_registered 2
    And disc "20260420T040001Z-1" for image "20260420T040001Z" state is "lost"
    And disc "20260420T040001Z-3" for image "20260420T040001Z" state is "verified"
  Scenario: djdan burn reports disc rebuild work instead of ordinary replacement backlog
    Given an archive with planned images
    And collection "docs" has uploaded archive package
    And candidate "img_2026-04-20_01" is finalized
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-2" with state "damaged"
    And archive restore "ar-20260420T040001Z-rebuild-1" restore remains pending
    When the operator runs djdan burn
    Then the command exits with code 0
    And stdout mentions "burn backlog already clear"
    And stdout mentions "burn backlog is waiting for disc rebuild restore work"
    And stdout mentions "ar-20260420T040001Z-rebuild-1"
    And stdout mentions "requested"
    And stdout does not mention "20260420T040001Z-3"
    And disc "20260420T040001Z-3" for image "20260420T040001Z" state is "needed"

  Scenario: djdan burn does not register a disc before labeled confirmation and resumes there
    Given an archive with planned images
    When the operator runs djdan burn
    Then the command exits non-zero
    And stderr mentions "label confirmation"
    And image "20260420T040001Z" has discs_registered 0
    And disc "20260420T040001Z-1" for image "20260420T040001Z" state is "needed"
    When unlabeled disc id "20260420T040001Z-1" is still available
    And the operator confirms labeled disc id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the operator confirms labeled disc id "20260420T040001Z-2" at location "vault-b/shelf-01"
    And the operator runs djdan burn
    Then the command exits with code 0
    And stderr does not mention "burning disc 20260420T040001Z-1"
    And image "20260420T040001Z" has discs_registered 2

  Scenario: djdan burn re-burns a disc after burned-media verification fails
    Given an archive with planned images
    And burned-media verification fails once for disc id "20260420T040001Z-1"
    And the operator confirms labeled disc id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the operator confirms labeled disc id "20260420T040001Z-2" at location "vault-b/shelf-01"
    When the operator runs djdan burn
    Then the command exits with code 0
    And stderr mentions "verifying burned media for 20260420T040001Z-1"
    And stderr mentions "discard or destroy this disc"
    And stderr mentions "Insert a new blank disc to retry burn disc 20260420T040001Z-1"
    And stderr mentions "burning disc 20260420T040001Z-1"
    And image "20260420T040001Z" has discs_registered 2

  Scenario: djdan burn re-burns an unfinished unlabeled disc if that disc is unavailable
    Given an archive with planned images
    When the operator runs djdan burn
    Then the command exits non-zero
    And image "20260420T040001Z" has discs_registered 0
    When unlabeled disc id "20260420T040001Z-1" is unavailable
    And the operator confirms labeled disc id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And the operator confirms labeled disc id "20260420T040001Z-2" at location "vault-b/shelf-01"
    And the operator runs djdan burn
    Then the command exits with code 0
    And stderr mentions "burning disc 20260420T040001Z-1"
    And image "20260420T040001Z" has discs_registered 2

  Scenario: djdan burn re-downloads an invalid staged ISO before finishing the backlog
    Given an archive with planned images
    And the operator confirms labeled disc id "20260420T040001Z-1" at location "vault-a/shelf-01"
    And burning disc id "20260420T040001Z-2" fails
    When the operator runs djdan burn
    Then the command exits non-zero
    And image "20260420T040001Z" has discs_registered 1
    When the staged ISO for image "20260420T040001Z" is corrupted
    And the optical burn boundary is healthy again
    And the operator confirms labeled disc id "20260420T040001Z-2" at location "vault-b/shelf-01"
    And the operator runs djdan burn
    Then the command exits with code 0
    And stderr mentions "staged ISO is invalid"
    And stderr mentions "re-downloading"
    And image "20260420T040001Z" has discs_registered 2
