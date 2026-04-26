@acceptance @api @mvp
Feature: Collections API
  The API ingests collections through resumable explicit upload sessions.

  Rule: Collection uploads are explicit, resumable, and auto-finalizing
    Background:
      Given an empty archive

    Scenario: Starting a collection upload keeps the collection invisible until completion
      Given a local collection source "photos-2024" with deterministic fixture contents
      When the client creates or resumes collection upload "photos-2024"
      Then the response status is 200
      And the response contains "collection_id", "state", "files_total", "files_pending", "files_partial", "files_uploaded", "bytes_total", "uploaded_bytes", "missing_bytes", "upload_state_expires_at", "files", and "collection"
      And collection upload "photos-2024" state is "uploading"
      And collection "photos-2024" is not yet visible

    Scenario: Uploading every required file auto-finalizes the collection from the terminal upload step and survives restart
      Given a local collection source "photos-2024" with deterministic fixture contents
      When the client uploads every required file for collection "photos-2024"
      Then the response status is 200
      And collection upload "photos-2024" state is "finalized"
      And the response contains collection id "photos-2024"
      And the response contains the correct file count
      And the response contains the correct total bytes
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

    Scenario: Slash-bearing collection ids remain first-class
      Given a local collection source "photos/2024" with deterministic fixture contents
      When the client uploads every required file for collection "photos/2024"
      Then the response status is 200
      And collection upload "photos/2024" state is "finalized"
      And the response contains collection id "photos/2024"
      And the response contains the correct file count
      And the response contains the correct total bytes

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

    Scenario: Expired partial upload state is forgotten completely
      Given collection upload "photos-2024" has expired partial upload state
      When the client refreshes collection upload "photos-2024"
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

    Scenario: Read a collection summary
      When the client gets "/v1/collections/photos-2024"
      Then the response status is 200
      And the response contains "id", "files", "bytes", "hot_bytes", "archived_bytes", and "pending_bytes"
      And pending_bytes equals bytes minus archived_bytes
      And hot_bytes is between 0 and bytes
      And archived_bytes is between 0 and bytes
