"""Generic media-ingest primitives for Riverhog-adjacent workflows."""

__all__ = [
    "MUNCHY_PROFILE_TARGET",
    "EncodeProfile",
    "MunchyRunnerClient",
    "SubmissionUploadRequest",
]


def __getattr__(name: str) -> object:
    if name in {"MUNCHY_PROFILE_TARGET", "EncodeProfile"}:
        from munchy.profiles import MUNCHY_PROFILE_TARGET, EncodeProfile

        return {"MUNCHY_PROFILE_TARGET": MUNCHY_PROFILE_TARGET, "EncodeProfile": EncodeProfile}[
            name
        ]
    if name in {"MunchyRunnerClient", "SubmissionUploadRequest"}:
        from munchy.runner_client import MunchyRunnerClient, SubmissionUploadRequest

        return {
            "MunchyRunnerClient": MunchyRunnerClient,
            "SubmissionUploadRequest": SubmissionUploadRequest,
        }[name]
    raise AttributeError(name)
