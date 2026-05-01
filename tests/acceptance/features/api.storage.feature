@acceptance @api
Feature: Read-only hot storage browsing
  Completed hot files become browseable only after Riverhog promotes verified
  bytes into the committed hot namespace.

  Rule: Read-only browsing exposes committed hot files only
    Scenario: The read-only browsing surface hides staging paths
      Given an archive containing collection "docs"
      When the client lists the read-only browsing root
      Then the read-only browsing surface exposes path "docs/tax/2022/invoice-123.pdf"
      And the read-only browsing surface hides path ".arc/"

    Scenario: The read-only browsing surface rejects writes
      When the client attempts to write "forbidden.txt" through the read-only browsing surface
      Then the read-only browsing write is rejected

    Scenario: The canonical storage bucket publishes incomplete multipart cleanup
      When the client inspects the canonical storage lifecycle configuration
      Then the storage lifecycle aborts incomplete multipart uploads after 3 days
    Scenario: The canonical harness keeps hot, staging, and collection Glacier objects in separate buckets
      Given an archive with planner fixtures
      And collection upload "staged-photos" has a partial file upload in progress
      And collection "docs" has uploaded Glacier archive package
      When the client gets "/v1/collections/docs"
      Then the response status is 200
      And the response collection glacier object_path is under "glacier/collections/"
      When the client inspects the canonical archive-storage lifecycle configuration
      Then the storage lifecycle aborts incomplete multipart uploads after 3 days
      And the hot bucket contains object "collections/docs/tax/2022/invoice-123.pdf"
      And the archive bucket does not contain object "collections/docs/tax/2022/invoice-123.pdf"
      And the hot bucket contains prefix ".arc/uploads/"
      And the archive bucket does not contain prefix ".arc/uploads/"
      And the archive bucket contains collection Glacier archive package for collection "docs"
      And the archive bucket object for collection "docs" records validated archive metadata
      And the hot bucket does not contain collection Glacier archive package for collection "docs"

    Scenario: The canonical harness enforces least-privilege bucket credentials
      Then the hot credentials cannot write object "glacier/collections/forbidden/archive.tar" to the archive bucket
      And the archive credentials cannot write object "collections/forbidden-archive-write.txt" to the hot bucket
      And the archive credentials cannot write object ".arc/uploads/forbidden-archive-write" to the hot bucket
    Scenario: The canonical harness enforces least-privilege bucket reads and lists
      Given an archive with planner fixtures
      And collection upload "staged-photos" has a partial file upload in progress
      And collection "docs" has uploaded Glacier archive package
      When the client gets "/v1/collections/docs"
      Then the response status is 200
      And the hot credentials cannot read collection Glacier archive package for collection "docs" from the archive bucket
      And the hot credentials cannot list prefix "glacier/collections/" in the archive bucket
      And the archive credentials cannot read object "collections/docs/tax/2022/invoice-123.pdf" from the hot bucket
      And the archive credentials cannot list prefix "collections/" in the hot bucket
      And the archive credentials cannot list prefix ".arc/uploads/" in the hot bucket

  Rule: Glacier usage reporting distinguishes measured collection storage from estimated billing
    Scenario: Glacier usage report shows totals, direct collection cost, manifest proof state, and pricing basis
      Given an archive with planner fixtures
      And an archive with split planner fixtures
      And collection "docs" has uploaded Glacier archive package
      And collection "photos-2024" has uploaded Glacier archive package
      When the client gets "/v1/glacier"
      Then the response status is 200
      And the response contains "scope", "measured_at", "pricing_basis", "totals", "images", "collections", "billing", and "history"
      And the response Glacier totals uploaded_collections is greater than 0
      And the response Glacier totals measured_storage_bytes is greater than 0
      And the response Glacier totals estimated_monthly_cost_usd is greater than 0
      And the response Glacier collection "docs" glacier state is "uploaded"
      And the response Glacier collection "docs" measured_storage_bytes is greater than 0
      And the response Glacier collection "docs" archive manifest state is "uploaded"
      And the response Glacier collection "docs" OTS proof state is "uploaded"
    Scenario: Glacier usage report can focus on one collection
      Given an archive with split planner fixtures
      And collection "docs" has uploaded Glacier archive package
      When the client gets "/v1/glacier?collection=docs"
      Then the response status is 200
      And the response Glacier collections contain only "docs"
      And the response Glacier collection "docs" glacier state is "uploaded"
    @ci_opt_in @requires_controlled_glacier_billing @issue_186
    Scenario: Glacier usage report exposes resource-level and manifest-aware billing metadata in the spec harness
      Given an archive with split planner fixtures
      And collection "docs" has uploaded Glacier archive package
      And the spec harness exposes controlled Glacier billing metadata
      When the client gets "/v1/glacier"
      Then the response status is 200
      And the response Glacier billing surface exposes resource-level and manifest metadata
