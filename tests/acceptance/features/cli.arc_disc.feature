@acceptance @cli @mvp
Feature: arc-disc CLI
  The optical CLI is the no-argument physical-media and recovery backlog clearer.
  Targeted fetch commands remain available for explicit recovery detail flows.

  Rule: No-argument physical and recovery backlog
    Scenario: arc-disc reports a missing configured optical device
      Given statechart "arc_disc.guided" state "device_missing" is the accepted operator contract
      And the configured optical device path does not exist
      When the operator runs 'arc-disc'
      Then stderr includes operator copy "device_missing"
      And stderr mentions "Check the device path"
      And stderr does not mention "xorriso"

    Scenario: arc-disc reports optical device permission problems
      Given statechart "arc_disc.guided" state "device_permission_denied" is the accepted operator contract
      And the operator cannot read or write the configured optical device
      When the operator runs 'arc-disc'
      Then stderr includes operator copy "device_permission_denied"
      And stderr mentions "Fix device permissions"
      And stderr does not mention "PermissionError"

    Scenario: arc-disc reports device loss during physical work
      Given statechart "arc_disc.burn" state "device_lost_during_work" is the accepted operator contract
      And the optical device becomes unavailable while writing media
      When the operator runs 'arc-disc'
      Then stderr includes operator copy "device_lost_during_work"
      And stderr mentions "last safe checkpoint"
      And stderr does not mention "Input/output error"

    @contract_gap @issue_208
    Scenario: arc-disc resumes unfinished local disc work before choosing new work
      Given statechart "arc_disc.guided" state "unfinished_local_disc" is the accepted operator contract
      And an unlabeled verified disc is waiting for label confirmation
      And ordinary blank-disc work is available
      When the operator runs 'arc-disc'
      Then the command exits with code 0
      And stdout includes operator copy "disc_item_unfinished_local_copy"
      And stdout mentions "Finish labeling"
      And stdout mentions "label"
      And stdout mentions "storage location"
      And stdout does not mention "candidate"

    @contract_gap @issue_208
    Scenario: arc-disc handles ready recovery before ordinary blank-disc work
      Given statechart "arc_disc.guided" state "recovery_ready" is the accepted operator contract
      And recovery data is ready for collection "docs"
      And ordinary blank-disc work is available
      When the operator runs 'arc-disc'
      Then the command exits with code 0
      And stdout includes operator copy "disc_item_recovery_ready"
      And stdout mentions "Recovery is ready"
      And stdout mentions "docs"
      And stdout mentions "replacement disc"
      And stdout does not mention "image_rebuild"

    @contract_gap @issue_208
    Scenario: arc-disc asks for recovery approval before ordinary blank-disc work
      Given statechart "arc_disc.guided" state "recovery_approval_required" is the accepted operator contract
      And recovery for collection "docs" needs approval
      And ordinary blank-disc work is available
      When the operator runs 'arc-disc'
      Then the command exits with code 0
      And stdout includes operator copy "disc_item_recovery_approval_required"
      And stdout mentions "Recovery needs approval"
      And stdout mentions "Estimated cost"
      And stdout mentions "docs"
      And stdout does not mention "pending_approval"

    @contract_gap @issue_208
    Scenario: arc-disc guides hot storage recovery that needs media
      Given statechart "arc_disc.guided" state "hot_recovery_needs_media" is the accepted operator contract
      And pinned files need recovery from disc
      When the operator runs 'arc-disc'
      Then the command exits with code 0
      And stdout includes operator copy "disc_item_hot_recovery_needs_media"
      And stdout mentions "Files need recovery from disc"
      And stdout mentions "Insert the requested disc"
      And stdout mentions target "docs/tax/2022/invoice-123.pdf"
      And stdout does not mention "fetch manifest"

    @contract_gap @issue_208
    Scenario: arc-disc clears ordinary blank-disc work only after label confirmation
      Given statechart "arc_disc.guided" state "burn_work_ready" is the accepted operator contract
      And statechart "arc_disc.burn" state "backlog_cleared" is the accepted operator contract
      And ordinary blank-disc work is available
      And the operator confirms labeled disc at storage location "vault-a/shelf-01"
      When the operator runs 'arc-disc'
      Then the command exits with code 0
      And stdout includes operator copy "burn_backlog_cleared"
      And stdout mentions "Disc work complete"
      And stdout mentions "label"
      And stdout mentions "storage location"
      And the collection is fully protected

    @contract_gap @issue_208
    Scenario: arc-disc does not count an unlabeled disc as protected
      Given statechart "arc_disc.burn" state "label_checkpoint" is the accepted operator contract
      And ordinary blank-disc work is available
      When the operator runs 'arc-disc' without label confirmation
      Then the command exits non-zero
      And stderr includes operator copy "burn_label_checkpoint"
      And stderr mentions "label"
      And the collection is not fully protected

  Rule: Targeted fetch detail remains available
  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc fetch completes a recoverable fetch
    Given split archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"
    And fetch "fx-1" has a stable manifest
    And a configured optical reader can recover every required entry
    When the operator runs arc-disc fetch "fx-1" with JSON output
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout reports fetch state "done"
    And stderr mentions copy id "20260420T040003Z-1"
    And stderr mentions copy id "20260420T040004Z-1"
    And target for fetch "fx-1" is hot

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc fetch reports precise progress while streaming uploads
    Given split archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"
    And fetch "fx-1" has a stable manifest
    And a configured optical reader can recover every required entry
    When the operator runs arc-disc fetch "fx-1" with JSON output
    Then the command exits with code 0
    And stderr mentions "current file"
    And stderr mentions "manifest"
    And stderr mentions "%"
    And stderr mentions "/s"

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc fetch fails if optical recovery fails
    Given split archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"
    And fetch "fx-1" has a stable manifest
    And a configured optical reader can recover every required entry
    And the configured optical reader cannot recover one required entry
    When the operator runs arc-disc fetch "fx-1"
    Then the command exits non-zero
    And fetch "fx-1" is not "done"

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc fetch resumes split recovery across repeated runs via server-side upload state
    Given split archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"
    And fetch "fx-1" has a stable manifest
    And a configured optical reader can recover every required entry
    And the configured optical reader cannot recover copy id "20260420T040004Z-1"
    When the operator runs arc-disc fetch "fx-1"
    Then the command exits non-zero
    And fetch "fx-1" is not "done"
    When the configured optical reader cannot recover copy id "20260420T040003Z-1"
    And the operator runs arc-disc fetch "fx-1" with JSON output
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout reports fetch state "done"
    And stderr does not mention copy id "20260420T040003Z-1"
    And stderr mentions copy id "20260420T040004Z-1"
    And target for fetch "fx-1" is hot

  @ci_opt_in @requires_optical_disc_drive @requires_human_operator @issue_186 @issue_187
  Scenario: arc-disc fetch fails if the server rejects incorrect recovered bytes
    Given split archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"
    And fetch "fx-1" has a stable manifest
    And a configured optical reader can recover every required entry
    And the configured optical reader returns bytes the server rejects for one required entry
    When the operator runs arc-disc fetch "fx-1"
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
    And the operator runs arc-disc fetch "fx-1" with JSON output
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout reports fetch state "done"
    And target for fetch "fx-1" is hot
