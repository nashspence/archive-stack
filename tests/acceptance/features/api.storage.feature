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

    Scenario: The canonical harness keeps hot, staging, and Glacier objects in separate buckets
      Given an archive with planner fixtures
      And collection upload "staged-photos" has a partial file upload in progress
      And candidate "img_2026-04-20_01" exists
      When the client posts to "/v1/plan/candidates/img_2026-04-20_01/finalize"
      Then the response status is 200
      When the client waits for image "20260420T040001Z" glacier state "uploaded"
      Then the response status is 200
      And the response image glacier object_path is "glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso"
      When the client inspects the canonical archive-storage lifecycle configuration
      Then the storage lifecycle aborts incomplete multipart uploads after 3 days
      And the hot bucket contains object "collections/docs/tax/2022/invoice-123.pdf"
      And the archive bucket does not contain object "collections/docs/tax/2022/invoice-123.pdf"
      And the hot bucket contains prefix ".arc/uploads/"
      And the archive bucket does not contain prefix ".arc/uploads/"
      And the archive bucket contains object "glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso"
      And the hot bucket does not contain object "glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso"

    Scenario: The canonical harness enforces least-privilege bucket credentials
      Then the hot credentials cannot write object "glacier/forbidden-hot-write.iso" to the archive bucket
      And the archive credentials cannot write object "collections/forbidden-archive-write.txt" to the hot bucket
      And the archive credentials cannot write object ".arc/uploads/forbidden-archive-write" to the hot bucket

    Scenario: The canonical harness enforces least-privilege bucket reads and lists
      Given an archive with planner fixtures
      And collection upload "staged-photos" has a partial file upload in progress
      And candidate "img_2026-04-20_01" exists
      When the client posts to "/v1/plan/candidates/img_2026-04-20_01/finalize"
      Then the response status is 200
      When the client waits for image "20260420T040001Z" glacier state "uploaded"
      Then the response status is 200
      And the hot credentials cannot read object "glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso" from the archive bucket
      And the hot credentials cannot list prefix "glacier/finalized-images/" in the archive bucket
      And the archive credentials cannot read object "collections/docs/tax/2022/invoice-123.pdf" from the hot bucket
      And the archive credentials cannot list prefix "collections/" in the hot bucket
      And the archive credentials cannot list prefix ".arc/uploads/" in the hot bucket

  Rule: Glacier usage reporting distinguishes measured storage from estimated billing
    Scenario: Glacier usage report shows totals, image state, derived collection cost, and pricing basis
      Given an archive with planner fixtures
      And an archive with split planner fixtures
      And candidate "img_2026-04-20_01" is finalized
      And candidate "img_2026-04-20_03" is finalized
      When the client waits for image "20260420T040001Z" glacier state "uploaded"
      And the client waits for image "20260420T040003Z" glacier state "uploaded"
      And the client gets "/v1/glacier"
      Then the response status is 200
      And the response contains "scope", "measured_at", "pricing_basis", "totals", "images", "collections", "billing", and "history"
      And the response Glacier totals measured_storage_bytes is greater than 0
      And the response Glacier totals estimated_monthly_cost_usd is greater than 0
      And the response Glacier image "20260420T040001Z" glacier state is "uploaded"
      And the response Glacier collection "docs" attribution_state is "derived"
      And the response Glacier collection "docs" derived_stored_bytes is greater than 0

    Scenario: Glacier usage report can focus on one finalized image
      Given candidate "img_2026-04-20_01" is finalized
      When the client waits for image "20260420T040001Z" glacier state "uploaded"
      And the client gets "/v1/glacier?image_id=20260420T040001Z"
      Then the response status is 200
      And the response Glacier images contain only "20260420T040001Z"

    Scenario: Glacier usage report can focus on one collection
      Given an archive with split planner fixtures
      And candidate "img_2026-04-20_03" is finalized
      When the client waits for image "20260420T040003Z" glacier state "uploaded"
      And the client gets "/v1/glacier?collection=docs"
      Then the response status is 200
      And the response Glacier collections contain only "docs"
      And the response Glacier collection "docs" attribution_state is "derived"
