@acceptance @cli @mvp
Feature: djdan disc rebuild CLI
  The optical CLI lists disc rebuild archive restores and the burn workflow consumes ready rebuild work automatically.
  Scenario: djdan disc rebuild lists one multi-image pending disc rebuild archive restore
    Given an archive with planned images
    And an archive with split planned images
    And collection "docs" has uploaded archive package
    And candidate "img_2026-04-20_01" is finalized
    And candidate "img_2026-04-20_03" is finalized
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client posts to "/v1/images/20260420T040003Z/discs" with id "20260420T040003Z-1" and location "Shelf C1"
    And the client posts to "/v1/images/20260420T040003Z/discs" with id "20260420T040003Z-2" and location "Shelf D1"
    And the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-2" with state "damaged"
    And the client patches "/v1/images/20260420T040003Z/discs/20260420T040003Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040003Z/discs/20260420T040003Z-2" with state "damaged"
    And archive restore "ar-20260420T040001Z-rebuild-1" restore remains pending
    When the operator runs 'djdan disc rebuild list'
    Then the command exits with code 0
    And stdout mentions "ar-20260420T040001Z-rebuild-1"
    And stdout mentions "disc rebuild"
    And stdout mentions "requested"
    And stdout mentions "20260420T040001Z"
    And stdout mentions "20260420T040003Z"
    When the operator runs 'djdan disc rebuild pause ar-20260420T040001Z-rebuild-1 --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout mentions "paused"
    When the operator runs 'djdan disc rebuild resume ar-20260420T040001Z-rebuild-1 --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout mentions "requested"
  Scenario: djdan burn resumes one ready multi-disc rebuild archive restore and cleans up staged ISOs
    Given an archive with planned images
    And an archive with split planned images
    And collection "docs" has uploaded archive package
    And candidate "img_2026-04-20_01" is finalized
    And candidate "img_2026-04-20_03" is finalized
    And candidate "img_2026-04-20_04" is finalized
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-1" and location "Shelf A1"
    And the client posts to "/v1/images/20260420T040001Z/discs" with id "20260420T040001Z-2" and location "Shelf B1"
    And the client posts to "/v1/images/20260420T040003Z/discs" with id "20260420T040003Z-1" and location "Shelf C1"
    And the client posts to "/v1/images/20260420T040003Z/discs" with id "20260420T040003Z-2" and location "Shelf D1"
    And the client posts to "/v1/images/20260420T040004Z/discs" with id "20260420T040004Z-1" and location "Shelf E1"
    And the client posts to "/v1/images/20260420T040004Z/discs" with id "20260420T040004Z-2" and location "Shelf F1"
    And the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-2" with state "damaged"
    And the client patches "/v1/images/20260420T040003Z/discs/20260420T040003Z-1" with state "lost"
    And the client patches "/v1/images/20260420T040003Z/discs/20260420T040003Z-2" with state "damaged"
    And burned-media verification fails once for disc id "20260420T040001Z-3"
    And the operator confirms labeled disc id "20260420T040001Z-4" at location "vault-b/shelf-02"
    And the operator confirms labeled disc id "20260420T040003Z-3" at location "vault-c/shelf-02"
    And the operator confirms labeled disc id "20260420T040003Z-4" at location "vault-d/shelf-02"
    When the client waits for archive restore "ar-20260420T040001Z-rebuild-1" state "ready"
    And the operator runs djdan burn
    Then the command exits non-zero
    And stderr mentions "discard or destroy this disc"
    And stderr mentions "Insert a new blank disc to retry burn disc 20260420T040001Z-3"
    When unlabeled disc id "20260420T040001Z-3" is still available
    And the optical burn boundary is healthy again
    And the operator confirms labeled disc id "20260420T040001Z-3" at location "vault-a/shelf-02"
    And the operator runs djdan burn
    Then the command exits with code 0
    And stdout mentions "burn backlog cleared"
    And stderr does not mention "verifying burned media for 20260420T040001Z-3"
    And stderr does not mention "burning disc 20260420T040001Z-3"
    And the client gets "/v1/archive-restores/ar-20260420T040001Z-rebuild-1"
    And the response status is 200
    And the response archive restore state is "completed"
    And disc "20260420T040001Z-4" for image "20260420T040001Z" state is "verified"
    And the staged ISO for image "20260420T040001Z" is absent
    And the staged ISO for image "20260420T040003Z" is absent
