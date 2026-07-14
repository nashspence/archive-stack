@acceptance @api @mvp
Feature: Collections API
  The API ingests collections through resumable explicit upload sessions and admits them only after the collection archive upload completes.

  Rule: Collection uploads are explicit, resumable, archive-backed, and auto-finalizing
    Background:
      Given an empty archive

    Scenario: Starting a collection upload keeps the collection invisible until completion
      Given a local collection source "photos-2024" with deterministic fixture contents
      When the client creates or resumes collection upload "photos-2024"
      Then the response status is 200
      And the response contains "collection_id", "state", "files_total", "files_pending", "files_partial", "files_uploaded", "bytes_total", "uploaded_bytes", "missing_bytes", "upload_state_expires_at", "files", and "collection"
      And collection upload "photos-2024" state is "uploading"
      And collection "photos-2024" is not yet visible

    Scenario: Collection file upload leases expose direct tus upload state
      Given collection upload "photos-2024" has a partial file upload in progress
      When the client posts to "/v1/collection-uploads/photos-2024/files/albums/japan/day-01.txt/upload"
      Then the response status is 200
      And the response contains "path", "protocol", "upload_url", "offset", "length", "checksum_algorithm", and "expires_at"
      And the response header "Tus-Resumable" is "1.0.0"
      And the response has header "Upload-Offset"
      And the response has header "Upload-Length"
      And the response has header "Upload-Expires"
      And the returned offset matches the previously uploaded bytes

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
      And collection "photos-2024" archive state is "uploaded"
      And collection "photos-2024" manifest state is "uploaded"
      And collection "photos-2024" OTS proof state is "uploaded"
      And collection "photos-2024" has hot_bytes equal to bytes
      And collection "photos-2024" has no disc coverage
      And collection "photos-2024" is eligible for planning
      When the client gets "/v1/collection-uploads/photos-2024"
      Then the response status is 404
      And the error code is "not_found"
      When the API process restarts
      And the client gets "/v1/collections/photos-2024"
      Then the response status is 200
    Scenario: Retrying archive upload leaves the upload retryable and the collection invisible
      Given a local collection source "photos-2024" with deterministic fixture contents
      And collection archive upload fails for "photos-2024" with error "archive bucket unavailable"
      When the client uploads every required file for collection "photos-2024"
      Then the response status is 200
      And collection upload "photos-2024" state is "archiving"
      And collection upload "photos-2024" latest failure contains "archive bucket unavailable"
      And the client waits for collection "photos-2024" archive state "retrying"
      And collection "photos-2024" is not yet visible
      And collection "photos-2024" is not eligible for planning
      When the client retries collection archive upload for "photos-2024"
      Then collection upload "photos-2024" state is "finalized"
      And collection "photos-2024" archive state is "uploaded"
    Scenario: Slash-bearing upload slugs normalize to server-minted collection ids
      Given a local collection source "photos/2024" with deterministic fixture contents
      When the client uploads every required file for collection "photos/2024"
      Then the response status is 200
      And collection upload "photos/2024" state is "finalized"
      And the response contains collection id "photos/2024"
      And the response contains the correct file count
      And the response contains the correct total bytes
      And collection "photos/2024" archive state is "uploaded"

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
      And the response contains "id", "files", "bytes", "hot_bytes", "archive", "collection_manifest", "archive_format", "compression", "disc_coverage", "disc_redundancy", and "image_coverage"
      And hot_bytes is between 0 and bytes
      And disc coverage bytes is between 0 and bytes
      And collection archive state is "uploaded"
      And collection manifest state is "uploaded"
      And collection OTS proof state is "uploaded"
      And collection disc coverage state is "none"
      And collection disc redundancy state is "none"
      And disc redundancy bytes is 0
    Scenario: Collection summaries explain archive state and disc coverage
      Given an archive with planner fixtures
      And disc "20260420T040001Z-1" already exists
      And collection "docs" has uploaded archive package
      When the client gets "/v1/collections/docs"
      Then the response status is 200
      And collection archive state is "uploaded"
      And collection manifest state is "uploaded"
      And collection OTS proof state is "uploaded"
      And collection disc redundancy state is "none"
      And disc redundancy bytes is 0
      And collection disc coverage state is "partial"
      And collection image coverage includes image "20260420T040001Z"
      And collection image coverage for image "20260420T040001Z" includes path "tax/2022/invoice-123.pdf"
      And collection image coverage for image "20260420T040001Z" includes disc "20260420T040001Z-1"
    Scenario: Collection disc coverage requires every split image part
      Given an archive with split planner fixtures
      And candidate "img_2026-04-20_03" is finalized
      And the client posts to "/v1/images/20260420T040003Z/discs" with id "20260420T040003Z-1" and location "vault-a/shelf-03"
      And the client patches "/v1/images/20260420T040003Z/discs/20260420T040003Z-1" with state "verified" and verification_state "verified"
      And collection "docs" keeps only disc-covered path "tax/2022/invoice-123.pdf"
      And collection "docs" has uploaded archive package
      When the client gets "/v1/collections/docs"
      Then the response status is 200
      And collection disc redundancy state is "none"
      And disc redundancy bytes is 0
      And collection disc coverage state is "partial"
      When candidate "img_2026-04-20_04" is finalized
      And the client posts to "/v1/images/20260420T040004Z/discs" with id "20260420T040004Z-1" and location "vault-a/shelf-04"
      And the client patches "/v1/images/20260420T040004Z/discs/20260420T040004Z-1" with state "verified" and verification_state "verified"
      And the client gets "/v1/collections/docs"
      Then the response status is 200
      And collection disc coverage state is "full"
    Scenario: Collection summaries can report full disc redundancy
      Given an archive with planner fixtures
      And disc "20260420T040001Z-1" already exists
      And disc "20260420T040001Z-2" already exists
      And collection "docs" keeps only finalized image "20260420T040001Z" disc coverage
      And collection "docs" has uploaded archive package
      When the client patches "/v1/images/20260420T040001Z/discs/20260420T040001Z-1" with state "verified" and verification_state "verified"
      And the client gets "/v1/collections/docs"
      Then the response status is 200
      And collection disc redundancy state is "full"
      And disc redundancy bytes equals bytes
      And collection archive state is "uploaded"
      And collection disc coverage state is "full"

    Scenario: Collection listing can filter by disc redundancy
      When the client gets "/v1/collections?disc_redundancy=none"
      Then the response status is 200
      And the response collection summaries contain only "photos-2024"
