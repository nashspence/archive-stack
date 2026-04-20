Place deterministic fixture builders here.

The acceptance suite assumes three fixture families:

1. staged trees
   - fixture_empty_archive
   - fixture_staged_photos_2024
   - fixture_docs_with_invoice

2. planner and image fixtures
   - fixture_planned_image_img_2026_04_20_01
   - fixture_registered_copy_br_021_a

3. fetch and optical fixtures
   - fixture_fetch_fx_1_single_file
   - fixture_fake_optical_reader_success
   - fixture_fake_optical_reader_missing_entry
   - fixture_fake_crypto_success
   - fixture_fake_crypto_bad_plaintext

Guidelines:

- Every fixture must be deterministic and self-contained.
- Every byte count used by acceptance tests must derive from real fixture files, not hand-entered constants.
- Optical fixtures should model both successful recovery and the two important failure modes:
  missing payload and bad plaintext hash.
- If release reconciliation is asynchronous internally, acceptance helpers should provide an eventual assertion such as
  wait_until_hot_matches_pins().
- CLI acceptance tests should use the same fixture families as the API acceptance tests instead of inventing parallel state.
