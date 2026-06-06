"""Generic media-ingest primitives for Riverhog-adjacent workflows."""

__all__ = [
    "MUNCHY_PROFILE_TARGET",
    "MUNCHY_WEBHOOK_EMOJI",
    "EncodeProfile",
    "MunchyRunnerClient",
    "RunnerUploadRequest",
]


def __getattr__(name: str) -> object:
    if name == "MUNCHY_WEBHOOK_EMOJI":
        from munchy.notifications import MUNCHY_WEBHOOK_EMOJI

        return MUNCHY_WEBHOOK_EMOJI
    if name in {"MUNCHY_PROFILE_TARGET", "EncodeProfile"}:
        from munchy.profiles import MUNCHY_PROFILE_TARGET, EncodeProfile

        return {"MUNCHY_PROFILE_TARGET": MUNCHY_PROFILE_TARGET, "EncodeProfile": EncodeProfile}[
            name
        ]
    if name in {"MunchyRunnerClient", "RunnerUploadRequest"}:
        from munchy.runner_client import MunchyRunnerClient, RunnerUploadRequest

        return {
            "MunchyRunnerClient": MunchyRunnerClient,
            "RunnerUploadRequest": RunnerUploadRequest,
        }[name]
    raise AttributeError(name)
