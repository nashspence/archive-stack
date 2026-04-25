@acceptance @cli @mvp
Feature: arc-disc CLI — copy registered via the images API
  arc-disc can recover a file whose copies were registered through POST /v1/images/{id}/copies.
  SqlAlchemyCopyService.register() reads disc paths from DISC.yml.age in image_root; this
  scenario exercises that production path end-to-end using the fixture codec.

  Background:
    Given split archived target "docs/tax/2022/invoice-123.pdf" is pinned via API copies with fetch "fx-api"
    And fetch "fx-api" has a stable manifest
    And a fake optical reader fixture can recover every required entry for fetch "fx-api"

  Scenario: arc-disc fetch recovers a file registered via the images API
    When the operator runs 'arc-disc fetch fx-api --device /dev/fake-sr0 --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout reports fetch state "done"
    And target for fetch "fx-api" is hot
