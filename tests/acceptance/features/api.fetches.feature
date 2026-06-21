@acceptance @api @mvp
Feature: Fetches API
  A fetch is a named operator work order containing one or more target selectors.

  Rule: Fetches are named and editable before they are queued
    Background:
      Given collection "docs" exists and is fully hot

    Scenario: Create a draft fetch
      When the client creates fetch "Tax audit" with target "docs/tax/2022/invoice-123.pdf"
      Then the response status is 200
      And the response contains "id", "name", "targets", "state", "files", "bytes", "entries_total", "entries_pending", "entries_partial", "entries_byte_complete", "entries_uploaded", "uploaded_bytes", "missing_bytes", "copies", and "upload_state_expires_at"
      And fetch state is "draft"
      And the response contains target "docs/tax/2022/invoice-123.pdf"

    Scenario: Edit a draft fetch target list
      Given the client creates fetch "Tax packet" with target "docs/tax/2022/invoice-123.pdf"
      When the client adds target "docs/tax/2022/receipt-456.pdf" to fetch "fx-1"
      Then the response status is 200
      And the response contains target "docs/tax/2022/receipt-456.pdf"
      When the client removes target "docs/tax/2022/invoice-123.pdf" from fetch "fx-1"
      Then the response status is 200
      And the response contains target "docs/tax/2022/receipt-456.pdf"

    Scenario: Start a draft fetch for djdan
      Given the client creates fetch "Tax recovery" with target "docs/tax/2022/invoice-123.pdf"
      When the client starts fetch "fx-1" for djdan
      Then the response status is 200
      And fetch state is "queued_djdan"

  Rule: Fetch manifests are stable and complete
    Background:
      Given fetch "fx-1" exists for target "docs/tax/2022/invoice-123.pdf"

    Scenario: Read a fetch summary
      When the client gets "/v1/fetches/fx-1"
      Then the response status is 200
      And the response contains "id", "name", "targets", "state", "files", "bytes", "entries_total", "entries_pending", "entries_partial", "entries_byte_complete", "entries_uploaded", "uploaded_bytes", "missing_bytes", "copies", and "upload_state_expires_at"

    Scenario: List fetches after restart
      When the API process restarts
      And the client gets "/v1/fetches"
      Then the response status is 200
      And "/v1/fetches" entry for target "docs/tax/2022/invoice-123.pdf" contains fetch id "fx-1"
      And "/v1/fetches" entry for target "docs/tax/2022/invoice-123.pdf" contains fetch state "queued_djdan"

    Scenario: Read the manifest twice
      When the client gets "/v1/fetches/fx-1/manifest"
      And the client gets "/v1/fetches/fx-1/manifest" again
      Then the response status is 200 both times
      And both manifests contain the same entry ids
      And both manifests contain the same logical file set

    Scenario: Read a manifest entry upload view
      When the client gets "/v1/fetches/fx-1/manifest"
      Then the response status is 200
      And fetch manifest entry "e1" contains "recovery_bytes", "upload_state", "uploaded_bytes", and "upload_state_expires_at"

  Rule: Partial upload progress survives service restarts
    Scenario: A restart mid-upload preserves the upload offset
      Given fetch "fx-1" has entry "e1" with a partial upload in progress
      When the API process restarts
      And the client posts to "/v1/fetches/fx-1/entries/e1/upload"
      Then the response status is 200
      And the returned offset matches the previously uploaded bytes

    Scenario: Expired partial upload state resets without a follow-up fetch request
      Given fetch "fx-1" has expired partial upload state for entry "e1"
      When background expiry cleanup resets fetch "fx-1" entry "e1"
      And the client gets "/v1/fetches/fx-1"
      Then the response status is 200
      And fetch state is "queued_djdan"
      When the client gets "/v1/fetches/fx-1/manifest"
      Then the response status is 200
      And fetch manifest entry "e1" contains "recovery_bytes", "upload_state", "uploaded_bytes", and "upload_state_expires_at"
      And fetch manifest entry "e1" upload state is "pending"
      And fetch manifest entry "e1" uploaded bytes is 0

  Rule: Split fetch manifests expose part-level recovery hints
    Background:
      Given split archived fetch "fx-1" exists for target "docs/tax/2022/invoice-123.pdf"

    Scenario: Read a split manifest
      When the client gets "/v1/fetches/fx-1/manifest"
      Then the response status is 200
      And fetch manifest entry "e1" lists split parts 0 and 1
      And fetch manifest entry "e1" part 0 is recoverable from copy "20260420T040003Z-1"
      And fetch manifest entry "e1" part 1 is recoverable from copy "20260420T040004Z-1"
      And fetch manifest entry "e1" part hashes and recovery-byte hashes match the published split fixture

  Rule: Fetch upload and completion are resumable and hash-verified
    Background:
      Given fetch "fx-1" exists with entry "e1"
      And entry "e1" expects sha256 "good-hash"

    Scenario: Creating or resuming an entry upload returns a resumable upload session
      When the client posts to "/v1/fetches/fx-1/entries/e1/upload"
      Then the response status is 200
      And the response contains "entry", "protocol", "upload_url", "offset", "length", "checksum_algorithm", and "expires_at"
      And the upload-session length matches fetch "fx-1" entry "e1" recovery bytes

    Scenario: Repeating upload-session creation reuses the same upload resource
      When the client posts to "/v1/fetches/fx-1/entries/e1/upload"
      And the client posts to "/v1/fetches/fx-1/entries/e1/upload" again
      Then the response status is 200 both times
      And both upload-session responses contain the same upload url

    Scenario: Fetch entry upload resources expose tus-style status and cancellation
      When the client posts to "/v1/fetches/fx-1/entries/e1/upload"
      Then the response status is 200
      And the response header "Tus-Resumable" is "1.0.0"
      And the response has header "Upload-Offset"
      And the response has header "Upload-Length"
      And the response has header "Location"
      When the client sends HEAD to "/v1/fetches/fx-1/entries/e1/upload"
      Then the response status is 204
      And the response header "Tus-Resumable" is "1.0.0"
      And the response header "Upload-Offset" is "0"
      When the client sends DELETE to "/v1/fetches/fx-1/entries/e1/upload"
      Then the response status is 204
      And the response header "Tus-Resumable" is "1.0.0"
      When the client gets "/v1/fetches/fx-1/manifest"
      Then the response status is 200
      And fetch manifest entry "e1" upload state is "pending"
      And fetch manifest entry "e1" uploaded bytes is 0

    Scenario: Fetch entry upload chunks require the tus chunk media type
      When the client posts to "/v1/fetches/fx-1/entries/e1/upload"
      Then the response status is 200
      When the client sends PATCH to "/v1/fetches/fx-1/entries/e1/upload" with upload chunk content type "application/json"
      Then the response status is 400
      And the error code is "bad_request"

    Scenario: Completing before all required entries are present fails
      When the client posts to "/v1/fetches/fx-1/complete"
      Then the response status is 409
      And the error code is "invalid_state"

    Scenario: Byte-complete entries are not uploaded until completion verifies them
      Given every required fetch entry for "fx-1" has been uploaded with the correct bytes
      When the client gets "/v1/fetches/fx-1"
      Then the response status is 200
      And the response field "entries_byte_complete" is 1
      And the response field "entries_uploaded" is 0
      When the client gets "/v1/fetches/fx-1/manifest"
      Then the response status is 200
      And fetch manifest entry "e1" upload state is "byte_complete"
      And target "docs/tax/2022/invoice-123.pdf" is not hot
      When the client posts to "/v1/fetches/fx-1/complete"
      Then the response status is 200
      And fetch state is "done"
      When the client gets "/v1/fetches/fx-1/manifest"
      Then the response status is 200
      And fetch manifest entry "e1" upload state is "uploaded"

    Scenario: Completing a fully uploaded fetch materializes the target
      Given every required fetch entry for "fx-1" has been uploaded with the correct bytes
      When the client posts to "/v1/fetches/fx-1/complete"
      Then the response status is 200
      And fetch state is "done"
      And target "docs/tax/2022/invoice-123.pdf" is hot
