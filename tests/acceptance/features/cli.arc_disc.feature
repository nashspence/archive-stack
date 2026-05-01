@acceptance @cli @mvp
Feature: arc-disc CLI
  The optical CLI fulfills a fetch from disc media and completes it through the API.
  Resume across separate client runs depends on server-side upload-session state carried by the fetch
  manifest, not client-local recovery files.

  Background:
    Given split archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"
    And fetch "fx-1" has a stable manifest
    And a configured optical reader can recover every required entry

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc fetch completes a recoverable fetch
    When the operator runs 'arc-disc fetch fx-1 --device /dev/arc-optical0 --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout reports fetch state "done"
    And stderr mentions copy id "20260420T040003Z-1"
    And stderr mentions copy id "20260420T040004Z-1"
    And target for fetch "fx-1" is hot

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc fetch reports precise progress while streaming uploads
    When the operator runs 'arc-disc fetch fx-1 --device /dev/arc-optical0 --json'
    Then the command exits with code 0
    And stderr mentions "current file"
    And stderr mentions "manifest"
    And stderr mentions "%"
    And stderr mentions "/s"

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc fetch fails if optical recovery fails
    Given the optical reader fixture fails for one required entry
    When the operator runs 'arc-disc fetch fx-1 --device /dev/arc-optical0'
    Then the command exits non-zero
    And fetch "fx-1" is not "done"

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc fetch resumes split recovery across repeated runs via server-side upload state
    Given the optical reader fixture fails for copy id "20260420T040004Z-1"
    When the operator runs 'arc-disc fetch fx-1 --device /dev/arc-optical0'
    Then the command exits non-zero
    And fetch "fx-1" is not "done"
    When the optical reader fixture fails for copy id "20260420T040003Z-1"
    And the operator runs 'arc-disc fetch fx-1 --device /dev/arc-optical0 --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout reports fetch state "done"
    And stderr does not mention copy id "20260420T040003Z-1"
    And stderr mentions copy id "20260420T040004Z-1"
    And target for fetch "fx-1" is hot

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc fetch fails if the server rejects incorrect recovered bytes
    Given the optical reader fixture returns incorrect recovered bytes for one required entry
    When the operator runs 'arc-disc fetch fx-1 --device /dev/arc-optical0'
    Then the command exits non-zero
    And fetch "fx-1" is not "done"
    And stderr mentions "reset byte-complete upload"
    And stderr mentions "try another registered copy or recovered media"
    And stderr mentions "fetch remains active and incomplete"
    When the client gets "/v1/fetches/fx-1/manifest"
    Then the response status is 200
    And fetch manifest entry "e1" upload state is "pending"
    And fetch manifest entry "e1" uploaded bytes is 0
    When a configured optical reader can recover every required entry
    And the operator runs 'arc-disc fetch fx-1 --device /dev/arc-optical0 --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout reports fetch state "done"
    And target for fetch "fx-1" is hot
