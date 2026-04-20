from __future__ import annotations

from pathlib import Path

import yaml

from arc_core.archive_artifacts import (
    COLLECTION_HASH_MANIFEST_NAME,
    COLLECTION_HASH_PROOF_NAME,
    collection_artifact_relpaths,
    generate_collection_hash_artifacts,
)



def test_collection_artifact_relpaths() -> None:
    manifest, proof = collection_artifact_relpaths('photos-2024')
    assert manifest.endswith(COLLECTION_HASH_MANIFEST_NAME)
    assert proof.endswith(COLLECTION_HASH_PROOF_NAME)



def test_generate_collection_hash_artifacts_writes_manifest_and_proof(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'a.txt').write_text('hello')
    (source / 'nested').mkdir()
    (source / 'nested' / 'b.txt').write_text('world')

    artifact_root = tmp_path / 'artifacts'
    paths = generate_collection_hash_artifacts(
        collection_id='photos-2024',
        source_root=source,
        artifact_root=artifact_root,
    )

    assert paths.manifest_path.exists()
    assert paths.proof_path.exists()
    data = yaml.safe_load(paths.manifest_path.read_text())
    assert data['schema'] == 'collection-hash-manifest/v1'
    assert data['tree']['total_bytes'] == 10
