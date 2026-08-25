from __future__ import annotations

from collections.abc import Mapping

# Semantic failures beyond the transport-wide authentication, authorization,
# validation, and internal-error contract. Operation IDs are the stable join
# between FastAPI routes and their generated OpenAPI operations.
RIVERHOG_OPERATION_ERROR_CODES: Mapping[str, frozenset[str]] = {
    "create_or_resume_collection_upload_session": frozenset({"conflict"}),
    "register_collection_upload_session_files": frozenset({"conflict", "not_found"}),
    "put_collection_upload_session_provenance_journal": frozenset(
        {"conflict", "length_required", "not_found"}
    ),
    "export_collection_upload_session_provenance_journal": frozenset({"not_found"}),
    "list_collection_upload_session_files": frozenset({"not_found"}),
    "complete_collection_upload_session": frozenset({"conflict", "not_found"}),
    "cancel_collection_upload_session": frozenset({"conflict", "not_found"}),
    "get_collection_upload_session": frozenset({"not_found"}),
    "list_collection_upload_session_volumes": frozenset({"not_found"}),
    "get_collection_upload_session_volume": frozenset({"not_found"}),
    "get_collection_upload_session_unit": frozenset({"not_found"}),
    "put_collection_upload_session_unit": frozenset({"conflict", "length_required", "not_found"}),
    "get_collection": frozenset({"not_found"}),
    "plan_collection_deletion": frozenset({"conflict", "invalid_state", "not_found"}),
    "delete_collection": frozenset({"conflict", "invalid_state", "not_found"}),
    "create_or_resume_archive_copy": frozenset({"conflict", "invalid_state", "not_found"}),
    "cancel_archive_copy_job": frozenset({"invalid_state", "not_found"}),
    "get_archive_copy_job": frozenset({"not_found"}),
    "plan_archive_copy_retirement": frozenset({"invalid_state", "not_found"}),
    "retire_archive_copy": frozenset(
        {"conflict", "invalid_state", "not_found", "service_unavailable"}
    ),
    "get_archive_store": frozenset({"not_found"}),
    "get_retrieval_cache_object": frozenset({"not_found"}),
    "plan_retrieval": frozenset({"invalid_state", "not_found"}),
    "create_retrieval_job": frozenset(
        {"conflict", "download_allowance_exceeded", "invalid_state", "not_found"}
    ),
    "renew_retrieval_job": frozenset({"invalid_state", "not_found"}),
    "get_retrieval_job": frozenset({"not_found"}),
    "cancel_retrieval_job": frozenset({"invalid_state", "not_found"}),
    "acknowledge_retrieval_job": frozenset({"invalid_state", "not_found"}),
    "download_retrieval_file": frozenset(
        {"download_allowance_exceeded", "invalid_state", "not_found"}
    ),
    "get_download_quota": frozenset({"download_allowance_exceeded", "not_found"}),
    "set_app_key_download_quota": frozenset({"download_allowance_exceeded", "not_found"}),
    "create_tag": frozenset({"conflict"}),
    "get_tag": frozenset({"not_found"}),
    "plan_tag_deletion": frozenset({"not_found"}),
    "delete_tag": frozenset({"conflict", "not_found"}),
    "get_collection_tags": frozenset({"not_found"}),
    "replace_collection_tags": frozenset({"conflict", "not_found"}),
    "add_collection_tag": frozenset({"conflict", "not_found"}),
    "remove_collection_tag": frozenset({"conflict", "not_found"}),
    "create_app_key": frozenset({"not_found"}),
    "rotate_app_key": frozenset({"not_found"}),
    "replace_app_key_access": frozenset({"not_found"}),
    "add_app_key_access": frozenset({"conflict", "not_found"}),
    "remove_app_key_access": frozenset({"not_found"}),
    "revoke_app_key": frozenset({"not_found"}),
    "list_collection_provenance": frozenset({"invalid_state", "not_found"}),
    "get_collection_file_provenance": frozenset({"invalid_state", "not_found"}),
    "trace_collection_file_provenance": frozenset({"invalid_state", "not_found"}),
    "export_collection_provenance_journal": frozenset({"not_found"}),
    "verify_collection_provenance": frozenset({"invalid_state", "not_found"}),
    "get_portable_collection_manifest": frozenset({"invalid_state", "not_found"}),
    "create_or_resume_processing_claim": frozenset({"conflict", "invalid_state", "not_found"}),
    "list_processing_claims": frozenset({"invalid_state"}),
    "get_processing_claim": frozenset({"invalid_state", "not_found"}),
    "renew_processing_claim": frozenset({"conflict", "invalid_state", "not_found"}),
    "restart_processing_claim": frozenset({"conflict", "invalid_state", "not_found"}),
    "abandon_processing_claim": frozenset({"conflict", "invalid_state", "not_found"}),
    "seal_processing_claim_plan": frozenset({"conflict", "invalid_state", "not_found"}),
    "create_transform_capability": frozenset({"conflict", "invalid_state", "not_found"}),
    "settle_processing_claim": frozenset({"conflict", "invalid_state", "not_found"}),
    "settle_processing_claim_outcomes": frozenset({"conflict", "invalid_state", "not_found"}),
    "begin_processing_claim_retirement": frozenset({"conflict", "invalid_state", "not_found"}),
    "release_processing_claim": frozenset({"conflict", "invalid_state", "not_found"}),
    "get_collection_derivation": frozenset({"not_found"}),
}
