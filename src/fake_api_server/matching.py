"""Score recorded endpoints the way the .NET server does.

Candidates must share the request method and path. Each candidate is then
scored on its required headers, query parameters, and optional raw body.
A constraint that is missing or different disqualifies the candidate.
The highest score wins. Equal top scores are a tie.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field


@dataclass
class Playback:
    status: int
    content: str
    content_type: str
    headers: dict[str, str]
    delay: int


@dataclass
class EndpointResponse:
    status: int
    content: str
    content_type: str
    headers: dict[str, str]
    delay: int


@dataclass
class Endpoint:
    method: str
    path: str
    body: str | None
    headers: dict[str, str]
    query: dict[str, str]
    response_mode: str
    responses: list[EndpointResponse]
    index: int = 0
    identity: tuple = field(init=False)

    def __post_init__(self) -> None:
        self.method = self.method.lower()
        self.headers = {key.lower(): value for key, value in self.headers.items()}
        self.identity = (
            self.method,
            self.path,
            self.body,
            tuple(sorted(self.headers.items())),
            tuple(sorted(self.query.items())),
        )

    def next_response(self, randbelow: Callable[[int], int]) -> Playback:
        count = len(self.responses)
        if count == 0:
            raise RuntimeError("endpoint has no responses")
        if self.response_mode == "random" and count > 1:
            self.index = randbelow(count)
        chosen = self.responses[self.index]
        if self.response_mode == "incremental":
            self.index += 1
        if self.index >= count:
            self.index = 0
        return Playback(
            status=chosen.status,
            content=chosen.content,
            content_type=chosen.content_type,
            headers=dict(chosen.headers),
            delay=chosen.delay,
        )


class MultipleMatchError(Exception):
    """More than one recorded endpoint shared the best score."""

    def __init__(self, matches: list[Endpoint], best_score: int) -> None:
        super().__init__("Multiple possible matches were found.")
        self.matches = matches
        self.best_score = best_score


def score_endpoint(
    endpoint: Endpoint,
    *,
    headers: dict[str, str],
    query: dict[str, str],
    body: str,
) -> int | None:
    """Return the match score, or None when a required constraint fails.

    Body is optional: it is scored only when the endpoint recorded one.
    Every recorded header and query parameter is required.
    """
    matches = 0
    if endpoint.body is not None:
        if endpoint.body != body:
            return None
        matches += 1

    for key, expected in endpoint.headers.items():
        if headers.get(key) != expected:
            return None
        matches += 1

    for key, expected in endpoint.query.items():
        if query.get(key) != expected:
            return None
        matches += 1

    return matches


def find_best(
    endpoints: Iterable[Endpoint],
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    query: dict[str, str],
    body: str,
) -> Endpoint | None:
    method_key = method.lower()
    scored: list[tuple[Endpoint, int]] = []
    for endpoint in endpoints:
        if endpoint.method != method_key or endpoint.path != path:
            continue
        score = score_endpoint(endpoint, headers=headers, query=query, body=body)
        if score is None:
            continue
        scored.append((endpoint, score))

    if not scored:
        return None

    best_score = max(score for _, score in scored)
    winners = [endpoint for endpoint, score in scored if score == best_score]
    if len(winners) > 1:
        raise MultipleMatchError(winners, best_score)
    return winners[0]
