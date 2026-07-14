@acceptance @api
Feature: Read-only hot storage browsing
  Completed hot files become browseable only after Riverhog promotes verified
  bytes into the committed hot namespace.

  Rule: Read-only browsing exposes committed hot files only
    Scenario: The read-only browsing surface hides staging paths
      Given an archive containing collection "docs"
      When the client lists the read-only browsing root
      Then the read-only browsing surface exposes path "docs/tax/2022/invoice-123.pdf"
      And the read-only browsing surface hides path ".riverhog/"

    Scenario: The read-only browsing surface rejects writes
      When the client attempts to write "forbidden.txt" through the read-only browsing surface
      Then the read-only browsing write is rejected

    Scenario: The canonical storage bucket publishes incomplete multipart cleanup
      When the client inspects the canonical storage lifecycle configuration
      Then the storage lifecycle aborts incomplete multipart uploads after 3 days
    Scenario: The canonical harness keeps hot, staging, and collection archive objects in separate buckets
      Given an archive with planner fixtures
      And collection upload "staged-photos" has a partial file upload in progress
      And collection "docs" has uploaded archive package
      When the client gets "/v1/collections/docs"
      Then the response status is 200
      And the response collection archive object_path is under "archive/archives/"
      When the client inspects the canonical archive-storage lifecycle configuration
      Then the storage lifecycle aborts incomplete multipart uploads after 3 days
      And the hot bucket contains object "collections/docs/tax/2022/invoice-123.pdf"
      And the archive bucket does not contain object "collections/docs/tax/2022/invoice-123.pdf"
      And the hot bucket contains prefix ".riverhog/uploads/"
      And the archive bucket does not contain prefix ".riverhog/uploads/"
      And the archive bucket contains collection archive package for collection "docs"
      And the archive bucket object for collection "docs" records validated archive metadata
      And the hot bucket does not contain collection archive package for collection "docs"

    Scenario: The canonical harness enforces least-privilege bucket credentials
      Then the hot credentials cannot write object "archive/archives/forbidden/archive.tar.age" to the archive bucket
      And the archive credentials cannot write object "collections/forbidden-archive-write.txt" to the hot bucket
      And the archive credentials cannot write object ".riverhog/uploads/forbidden-archive-write" to the hot bucket
    Scenario: The canonical harness enforces least-privilege bucket reads and lists
      Given an archive with planner fixtures
      And collection upload "staged-photos" has a partial file upload in progress
      And collection "docs" has uploaded archive package
      When the client gets "/v1/collections/docs"
      Then the response status is 200
      And the hot credentials cannot read collection archive package for collection "docs" from the archive bucket
      And the hot credentials cannot list prefix "archive/archives/" in the archive bucket
      And the archive credentials cannot read object "collections/docs/tax/2022/invoice-123.pdf" from the hot bucket
      And the archive credentials cannot list prefix "collections/" in the hot bucket
      And the archive credentials cannot list prefix ".riverhog/uploads/" in the hot bucket

  Rule: Archive usage reporting exposes measured collection storage
    Scenario: Archive usage report shows totals, direct collection storage, and manifest proof state
      Given an archive with planner fixtures
      And an archive with split planner fixtures
      And collection "docs" has uploaded archive package
      And collection "photos-2024" has uploaded archive package
      When the client gets "/v1/archive"
      Then the response status is 200
      And the response contains "scope", "measured_at", "totals", "images", "collections", and "history"
      And the response Archive totals uploaded_collections is greater than 0
      And the response Archive totals measured_storage_bytes is greater than 0
      And the response Archive collection "docs" archive state is "uploaded"
      And the response Archive collection "docs" measured_storage_bytes is greater than 0
      And the response Archive collection "docs" collection manifest state is "uploaded"
      And the response Archive collection "docs" OTS proof state is "uploaded"
    Scenario: Archive usage report can focus on one collection
      Given an archive with split planner fixtures
      And collection "docs" has uploaded archive package
      When the client gets "/v1/archive?collection=docs"
      Then the response status is 200
      And the response Archive collections contain only "docs"
      And the response Archive collection "docs" archive state is "uploaded"
