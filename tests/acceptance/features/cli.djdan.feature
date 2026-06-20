@acceptance @cli @mvp
Feature: djdan CLI
  The optical CLI fulfills a fetch from disc media and completes it through the API.
  Resume across separate client runs depends on server-side upload-session state carried by the fetch
  manifest, not client-local recovery files.

  Background:
    Given split archived target "docs/tax/2022/invoice-123.pdf" is pinned with fetch "fx-1"
    And fetch "fx-1" has a stable manifest
    And a configured optical reader can recover every required entry

  Scenario: djdan fetch completes a recoverable fetch
    When the operator runs djdan fetch "fx-1" with JSON output
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout reports fetch state "done"
    And stderr mentions copy id "20260420T040003Z-1"
    And stderr mentions copy id "20260420T040004Z-1"
    And target for fetch "fx-1" is hot

  Scenario: djdan fetch reports precise progress while streaming uploads
    When the operator runs djdan fetch "fx-1" with JSON output
    Then the command exits with code 0
    And stderr mentions "current file"
    And stderr mentions "manifest"
    And stderr mentions "%"
    And stderr mentions "/s"

  Scenario: djdan fetch fails if optical recovery fails
    Given the configured optical reader cannot recover one required entry
    When the operator runs djdan fetch "fx-1"
    Then the command exits non-zero
    And fetch "fx-1" is not "done"

  Scenario: djdan fetch resumes split recovery across repeated runs via server-side upload state
    Given the configured optical reader cannot recover copy id "20260420T040004Z-1"
    When the operator runs djdan fetch "fx-1"
    Then the command exits non-zero
    And fetch "fx-1" is not "done"
    When the configured optical reader cannot recover copy id "20260420T040003Z-1"
    And the operator runs djdan fetch "fx-1" with JSON output
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout reports fetch state "done"
    And stderr does not mention copy id "20260420T040003Z-1"
    And stderr mentions copy id "20260420T040004Z-1"
    And target for fetch "fx-1" is hot

  Scenario: djdan fetch fails if the server rejects incorrect recovered bytes
    Given the configured optical reader returns bytes the server rejects for one required entry
    When the operator runs djdan fetch "fx-1"
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
    And the operator runs djdan fetch "fx-1" with JSON output
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout reports fetch state "done"
    And target for fetch "fx-1" is hot

  Scenario: djdan image plan emits the API plan payload
    Given an archive with planned images
    And an archive with split planned images
    When the operator runs 'djdan image plan --page 1 --per-page 2 --sort candidate_id --order asc --collection docs --iso-ready --query invoice-123.pdf --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout matches the structure of GET "/v1/plan"
    And stdout mentions "img_2026-04-20_01"

  Scenario: djdan image list emits the finalized-image listing payload
    Given an archive with planned images
    And an archive with split planned images
    And candidate "img_2026-04-20_01" is finalized
    And candidate "img_2026-04-20_03" is finalized
    And copy "20260420T040001Z-1" already exists
    When the operator runs 'djdan image list --page 1 --per-page 2 --sort finalized_at --order desc --has-discs --query 040001Z --collection docs --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout matches the structure of GET "/v1/images"
    And stdout mentions "20260420T040001Z"

  Scenario: djdan disc add emits the generated-disc registration payload
    Given candidate "img_2026-04-20_01" is finalized
    When the operator runs 'djdan disc add 20260420T040001Z --at "Shelf B1" --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout mentions "20260420T040001Z-1"

  Scenario: djdan disc list emits the generated-disc listing payload
    Given candidate "img_2026-04-20_01" is finalized
    When the operator runs 'djdan disc list 20260420T040001Z --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout matches the structure of GET "/v1/images/20260420T040001Z/copies"

  Scenario: djdan disc location emits the disc update payload
    Given copy "20260420T040001Z-1" already exists
    When the operator runs 'djdan disc location 20260420T040001Z-1 --to "Shelf B2" --json'
    Then the command exits with code 0
    And stdout is valid JSON
    And stdout mentions "Shelf B2"

  Scenario: djdan image plan prints candidate ids, fill, and readiness
    Given an archive with planned images
    And an archive with split planned images
    When the operator runs 'djdan image plan --collection docs --iso-ready'
    Then the command exits with code 0
    And stdout mentions "img_2026-04-20_01"
    And stdout mentions "fill:"
    And stdout mentions "iso_ready: True"
    And stdout mentions "collections: 1 [docs]"

  Scenario: djdan image list prints finalized image records
    Given an archive with planned images
    And candidate "img_2026-04-20_01" is finalized
    When the operator runs 'djdan image list'
    Then the command exits with code 0
    And stdout mentions "images:"
    And stdout mentions "20260420T040001Z"
    And stdout mentions "protection:"

  Scenario: djdan disc add prints the generated label text and state
    Given candidate "img_2026-04-20_01" is finalized
    When the operator runs 'djdan disc add 20260420T040001Z --at "Shelf B1"'
    Then the command exits with code 0
    And stdout mentions "disc: 20260420T040001Z-1"
    And stdout mentions "label: 20260420T040001Z-1"
    And stdout mentions "state: registered"
