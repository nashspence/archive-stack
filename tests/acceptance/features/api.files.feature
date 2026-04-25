@acceptance @api
Feature: File introspection API
  Read-only endpoints expose per-file state and hot file bytes without requiring
  a full search query or internal state access.

  Rule: GET /v1/collections/{id}/files lists every file with per-file state

    Background:
      Given an archive containing collection "docs"

    Scenario: List files for a known collection
      When the client gets "/v1/collections/docs/files"
      Then the response status is 200
      And the response contains "collection_id" and "files"
      And each file entry contains "path", "bytes", "hot", and "archived"

    Scenario: Unknown collection returns not found
      When the client gets "/v1/collections/missing/files"
      Then the response status is 404
      And the error code is "not_found"

    Scenario: List files for a slash-bearing collection id
      Given an archive containing collection "photos/2024"
      When the client gets "/v1/collections/photos/2024/files"
      Then the response status is 200
      And the response contains "collection_id" and "files"

  Rule: GET /v1/files?target={target} queries files matching a selector

    Background:
      Given an archive containing collection "docs"

    Scenario: Query a hot file by exact file target
      When the client gets "/v1/files?target=docs/tax/2022/invoice-123.pdf"
      Then the response status is 200
      And the response files list has exactly 1 entry
      And every file entry has hot equal to true

    Scenario: Query a non-existent target returns an empty list
      When the client gets "/v1/files?target=docs/does-not-exist.pdf"
      Then the response status is 200
      And the response files list is empty

    Scenario: Query a directory target returns all matching files
      When the client gets "/v1/files?target=docs/"
      Then the response status is 200
      And the response files list is non-empty

    Scenario: Invalid target returns 400
      When the client gets "/v1/files?target=/invalid"
      Then the response status is 400

    Scenario: File target returns sha256 and collection fields
      When the client gets "/v1/files?target=docs/tax/2022/invoice-123.pdf"
      Then the response status is 200
      And each file entry contains "target", "collection", "path", "bytes", "sha256", "hot", and "archived"

  Rule: GET /v1/files/{target}/content serves raw bytes for hot files

    Scenario: Download a hot file returns raw bytes
      Given an archive containing collection "docs"
      When the client gets "/v1/files/docs/tax/2022/invoice-123.pdf/content"
      Then the response status is 200
      And the response content type is "application/octet-stream"

    Scenario: Download a non-hot file returns not found
      Given file "docs/tax/2022/invoice-123.pdf" is not hot
      When the client gets "/v1/files/docs/tax/2022/invoice-123.pdf/content"
      Then the response status is 404

    Scenario: Download an unknown file returns not found
      Given an archive containing collection "docs"
      When the client gets "/v1/files/docs/does-not-exist.pdf/content"
      Then the response status is 404

    Scenario: Directory target returns 400
      Given an archive containing collection "docs"
      When the client gets "/v1/files/docs//content"
      Then the response status is 400
