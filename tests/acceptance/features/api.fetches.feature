@acceptance @api @mvp
Feature: Fetches API
  A fetch exists only to satisfy a pin for bytes that are archived but not currently hot.

  Rule: Pinning cold archived data creates a fetch
    Background:
      Given file "docs:/tax/2022/invoice-123.pdf" is archived
      And file "docs:/tax/2022/invoice-123.pdf" is not hot

    Scenario: Pin a cold archived file
      When the client posts to "/v1/pin" with target "docs:/tax/2022/invoice-123.pdf"
      Then the response status is 200
      And pin is true
      And hot state is "waiting"
      And missing_bytes is greater than 0
      And a fetch id is returned
      And fetch state is "waiting_media"

    Scenario: Repeating the same pin reuses the active fetch
      Given fetch "fx-existing" already exists for target "docs:/tax/2022/invoice-123.pdf"
      And fetch "fx-existing" is not done
      And fetch "fx-existing" is not failed
      When the client posts to "/v1/pin" with target "docs:/tax/2022/invoice-123.pdf"
      Then the response status is 200
      And the returned fetch id is "fx-existing"

  Rule: Fetch manifests are stable and complete
    Background:
      Given fetch "fx-1" exists for target "docs:/tax/2022/invoice-123.pdf"

    Scenario: Read a fetch summary
      When the client gets "/v1/fetches/fx-1"
      Then the response status is 200
      And the response contains "id", "target", "state", "files", "bytes", and "copies"

    Scenario: Read the manifest twice
      When the client gets "/v1/fetches/fx-1/manifest"
      And the client gets "/v1/fetches/fx-1/manifest" again
      Then the response status is 200 both times
      And both manifests contain the same entry ids
      And both manifests contain the same logical file set

  Rule: Fetch upload and completion are hash-verified
    Background:
      Given fetch "fx-1" exists with entry "e1"
      And entry "e1" expects sha256 "good-hash"

    Scenario: Uploading bytes with the wrong hash fails
      When the client puts incorrect plaintext bytes to "/v1/fetches/fx-1/files/e1" with header "X-Sha256: wrong-hash"
      Then the response status is 409
      And the error code is "hash_mismatch"

    Scenario: Completing before all required entries are present fails
      When the client posts to "/v1/fetches/fx-1/complete"
      Then the response status is 409
      And the error code is "invalid_state"

    Scenario: Completing a fully uploaded fetch materializes the target
      Given every required fetch entry for "fx-1" has been uploaded with the correct bytes
      When the client posts to "/v1/fetches/fx-1/complete"
      Then the response status is 200
      And fetch state is "done"
      And target "docs:/tax/2022/invoice-123.pdf" is hot
      And target "docs:/tax/2022/invoice-123.pdf" remains pinned
