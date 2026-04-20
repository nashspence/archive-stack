@acceptance @cli @mvp
Feature: arc-disc CLI
  The optical CLI fulfills a fetch from disc media and completes it through the API.

  Background:
    Given fetch "fx-1" exists
    And fetch "fx-1" has a stable manifest
    And a fake optical reader fixture can recover every required encrypted entry
    And a fake crypto fixture can decrypt every required entry

  Scenario: arc-disc fetch completes a recoverable fetch
    When the operator runs 'arc-disc fetch fx-1 --device /dev/fake-sr0 --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout reports fetch state "done"
    And target for fetch "fx-1" is hot

  Scenario: arc-disc fetch fails if optical recovery fails
    Given the optical reader fixture fails for one required entry
    When the operator runs 'arc-disc fetch fx-1 --device /dev/fake-sr0'
    Then the command exits non-zero
    And fetch "fx-1" is not "done"

  Scenario: arc-disc fetch fails if decrypted bytes do not match the expected hash
    Given the crypto fixture returns incorrect plaintext for one required entry
    When the operator runs 'arc-disc fetch fx-1 --device /dev/fake-sr0'
    Then the command exits non-zero
    And the API rejects the bad upload with "hash_mismatch"
    And fetch "fx-1" is not "done"
