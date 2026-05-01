@acceptance @api @mvp
Feature: Collections API
  The API ingests collections through resumable explicit upload sessions and admits them only after collection-native Glacier archiving.

  Rule: Collection uploads are explicit, resumable, Glacier-backed, and auto-finalizing
    Background:
      Given an empty archive

    Scenario: Starting a collection upload keeps the collection invisible until completion
      Given a local collection source "photos-2024" with deterministic fixture contents
      When the client creates or resumes collection upload "photos-2024"
      Then the response status is 200
      And the response contains "collection_id", "state", "files_total", "files_pending", "files_partial", "files_uploaded", "bytes_total", "uploaded_bytes", "missing_bytes", "upload_state_expires_at", "files", and "collection"
      And collection upload "photos-2024" state is "uploading"
      And collection "photos-2024" is not yet visible

    Scenario: Collection file upload resources expose tus-style status and cancellation
      Given collection upload "photos-2024" has a partial file upload in progress
      When the client sends HEAD to "/v1/collection-uploads/photos-2024/files/albums/japan/day-01.txt/upload"
      Then the response status is 204
      And the response header "Tus-Resumable" is "1.0.0"
      And the response has header "Upload-Offset"
      And the response has header "Upload-Length"
      And the response has header "Upload-Expires"
      When the client sends DELETE to "/v1/collection-uploads/photos-2024/files/albums/japan/day-01.txt/upload"
      Then the response status is 204
      And the response header "Tus-Resumable" is "1.0.0"
      When the client creates or resumes collection upload "photos-2024" again
      Then the response status is 200
      And collection upload "photos-2024" file "albums/japan/day-01.txt" is "pending"

    Scenario: Collection file upload chunks require the tus chunk media type
      Given a local collection source "photos-2024" with deterministic fixture contents
      When the client creates or resumes collection upload "photos-2024"
      When the client posts to "/v1/collection-uploads/photos-2024/files/albums/japan/day-01.txt/upload"
      Then the response status is 200
      When the client sends PATCH to "/v1/collection-uploads/photos-2024/files/albums/japan/day-01.txt/upload" with upload chunk content type "application/json"
      Then the response status is 400
      And the error code is "bad_request"
    Scenario: Uploading every required file archives the collection before finalization and survives restart
      Given a local collection source "photos-2024" with deterministic fixture contents
      When the client uploads every required file for collection "photos-2024"
      Then the response status is 200
      And collection upload "photos-2024" state is "archiving"
      And collection "photos-2024" is not yet visible
      And collection "photos-2024" is not eligible for planning
      When the client waits for collection upload "photos-2024" state "finalized"
      Then the response status is 200
      And collection upload "photos-2024" state is "finalized"
      And the response contains collection id "photos-2024"
      And the response contains the correct file count
      And the response contains the correct total bytes
      And collection "photos-2024" glacier state is "uploaded"
      And collection "photos-2024" archive manifest state is "uploaded"
      And collection "photos-2024" OTS proof state is "uploaded"
      And collection "photos-2024" has hot_bytes equal to bytes
      And collection "photos-2024" has archived_bytes equal to 0
      And collection "photos-2024" has pending_bytes equal to bytes
      And collection "photos-2024" is eligible for planning
      When the client gets "/v1/collection-uploads/photos-2024"
      Then the response status is 404
      And the error code is "not_found"
      When the API process restarts
      And the client gets "/v1/collections/photos-2024"
      Then the response status is 200
    @ci_opt_in @requires_controlled_glacier_failure @issue_186
    Scenario: Failed Glacier archiving leaves the upload retryable and the collection invisible
      Given a local collection source "photos-2024" with deterministic fixture contents
      And collection Glacier archiving fails for "photos-2024" with error "archive bucket unavailable"
      When the client uploads every required file for collection "photos-2024"
      Then the response status is 200
      And collection upload "photos-2024" state is "failed"
      And collection upload "photos-2024" latest failure contains "archive bucket unavailable"
      And collection "photos-2024" is not yet visible
      And collection "photos-2024" is not eligible for planning
      When the client retries collection Glacier archiving for "photos-2024"
      Then collection upload "photos-2024" state is "finalized"
      And collection "photos-2024" glacier state is "uploaded"
    Scenario: Slash-bearing collection ids remain first-class
      Given a local collection source "photos/2024" with deterministic fixture contents
      When the client uploads every required file for collection "photos/2024"
      Then the response status is 200
      And collection upload "photos/2024" state is "finalized"
      And the response contains collection id "photos/2024"
      And the response contains the correct file count
      And the response contains the correct total bytes
      And collection "photos/2024" glacier state is "uploaded"

    Scenario: Uploading a descendant collection id after its ancestor exists fails
      Given collection "photos" already exists from deterministic fixture contents
      And a local collection source "photos/2024" with deterministic fixture contents
      When the client creates or resumes collection upload "photos/2024"
      Then the response status is 409
      And the error code is "conflict"

    Scenario: Uploading an ancestor collection id after its descendant exists fails
      Given collection "photos/2024" already exists from deterministic fixture contents
      And a local collection source "photos" with deterministic fixture contents
      When the client creates or resumes collection upload "photos"
      Then the response status is 409
      And the error code is "conflict"

    Scenario: A restart mid-upload preserves the collection file upload offset
      Given collection upload "photos-2024" has a partial file upload in progress
      When the API process restarts
      And the client posts to "/v1/collection-uploads/photos-2024/files/albums/japan/day-01.txt/upload"
      Then the response status is 200
      And the returned offset matches the previously uploaded bytes
      And the upload-session length matches collection "photos-2024" file "albums/japan/day-01.txt" bytes

    Scenario: Partial collection upload bytes stay out of the committed hot namespace
      Given collection upload "photos-2024" has a partial file upload in progress
      Then collection "photos-2024" does not have committed file "albums/japan/day-01.txt"

    Scenario: Expired partial upload state is forgotten completely
      Given collection upload "photos-2024" has expired partial upload state
      When background expiry cleanup removes collection upload "photos-2024"
      And the client refreshes collection upload "photos-2024"
      Then the response status is 404
      And the error code is "not_found"
      And collection "photos-2024" is not yet visible
      When the client creates or resumes collection upload "photos-2024" again
      Then the response status is 200
      And collection upload "photos-2024" state is "uploading"
      And collection upload "photos-2024" file "albums/japan/day-01.txt" is "pending"
      And collection upload "photos-2024" reports uploaded bytes 0 for every file

  Rule: Collection summaries remain stable after upload finalization
    Background:
      Given an archive containing collection "photos-2024"

    Scenario: List collection summaries with pagination
      Given an archive containing collection "docs"
      When the client gets "/v1/collections?page=1&per_page=2"
      Then the response status is 200
      And the response contains "page", "per_page", "total", "pages", and "collections"
      And the response contains 2 collection summaries
    Scenario: Read a collection summary
      When the client gets "/v1/collections/photos-2024"
      Then the response status is 200
      And the response contains "id", "files", "bytes", "hot_bytes", "archived_bytes", "pending_bytes", "glacier", "archive_manifest", "archive_format", "compression", "disc_coverage", "protection_state", "protected_bytes", and "image_coverage"
      And pending_bytes equals bytes minus archived_bytes
      And hot_bytes is between 0 and bytes
      And archived_bytes is between 0 and bytes
      And collection glacier state is "uploaded"
      And collection archive manifest state is "uploaded"
      And collection OTS proof state is "uploaded"
      And collection disc coverage state is "none"
      And collection protection_state is "cloud_only"
      And protected_bytes is 0
    Scenario: Collection summaries explain collection Glacier state and physical image coverage
      Given an archive with planner fixtures
      And copy "20260420T040001Z-1" already exists
      And collection "docs" has uploaded Glacier archive package
      When the client gets "/v1/collections/docs"
      Then the response status is 200
      And collection glacier state is "uploaded"
      And collection archive manifest state is "uploaded"
      And collection OTS proof state is "uploaded"
      And collection protection_state is "under_protected"
      And protected_bytes is 0
      And collection disc coverage state is "partial"
      And collection image coverage includes image "20260420T040001Z"
      And collection image coverage for image "20260420T040001Z" includes path "tax/2022/invoice-123.pdf"
      And collection image coverage for image "20260420T040001Z" includes copy "20260420T040001Z-1"
    Scenario: Collection physical coverage requires every split image part
      Given an archive with split planner fixtures
      And candidate "img_2026-04-20_03" is finalized
      And the client posts to "/v1/images/20260420T040003Z/copies" with id "20260420T040003Z-1" and location "vault-a/shelf-03"
      And the client patches "/v1/images/20260420T040003Z/copies/20260420T040003Z-1" with state "verified" and verification_state "verified"
      And collection "docs" keeps only path "tax/2022/invoice-123.pdf" and is archived
      And collection "docs" has uploaded Glacier archive package
      When the client gets "/v1/collections/docs"
      Then the response status is 200
      And collection protection_state is "under_protected"
      And protected_bytes is 0
      And collection disc coverage state is "partial"
      When candidate "img_2026-04-20_04" is finalized
      And the client posts to "/v1/images/20260420T040004Z/copies" with id "20260420T040004Z-1" and location "vault-a/shelf-04"
      And the client patches "/v1/images/20260420T040004Z/copies/20260420T040004Z-1" with state "verified" and verification_state "verified"
      And the client gets "/v1/collections/docs"
      Then the response status is 200
      And collection disc coverage state is "full"
    Scenario: Collection summaries can report fully protected collections
      Given an archive with planner fixtures
      And copy "20260420T040001Z-1" already exists
      And copy "20260420T040001Z-2" already exists
      And collection "docs" keeps only finalized image "20260420T040001Z" coverage and is archived
      And collection "docs" has uploaded Glacier archive package
      When the client patches "/v1/images/20260420T040001Z/copies/20260420T040001Z-1" with state "verified" and verification_state "verified"
      And the client gets "/v1/collections/docs"
      Then the response status is 200
      And collection protection_state is "fully_protected"
      And protected_bytes equals bytes
      And collection glacier state is "uploaded"
      And collection disc coverage state is "full"

    Scenario: Collection listing can filter by protection_state
      When the client gets "/v1/collections?protection_state=cloud_only"
      Then the response status is 200
      And the response collection summaries contain only "photos-2024"
