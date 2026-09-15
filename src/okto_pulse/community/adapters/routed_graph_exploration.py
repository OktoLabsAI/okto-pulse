"""Optional graph capabilities share the existing route and lifecycle authority."""

from contextlib import contextmanager


class CommunityRoutedRankedSearch:
    def __init__(self, resolver, provider, *, operation_window, mutation_window, close):
        self._resolver = resolver
        self._provider = provider
        self._operation = operation_window
        self._mutation = mutation_window
        self._close = close

    @contextmanager
    def _retire_participants(self, board_id):
        self._close(board_id)
        primary = None
        try:
            yield
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                self._close(board_id)
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.add_note(
                    f"Participant closure also failed: {type(cleanup).__name__}"
                )

    def readiness(self, board_id, node_type):
        with self._operation(board_id):
            snapshot = self._resolver.acquire_board_route(board_id)
            result = self._provider.readiness(board_id, node_type)
            self._resolver.revalidate_snapshot(snapshot, require_physical=True)
            return result

    def search(self, board_id, request):
        with self._operation(board_id):
            snapshot = self._resolver.acquire_board_route(board_id)
            result = self._provider.search(board_id, request)
            self._resolver.revalidate_snapshot(snapshot, require_physical=True)
            return result

    def prepare(self, board_id, node_type, *, reason):
        # This is schema preparation, not a background read-side index upgrade.
        with self._mutation(board_id, phase="graph_schema_migrate"):
            snapshot = self._resolver.acquire_board_route(board_id)
            with self._retire_participants(board_id):
                result = self._provider.prepare(board_id, node_type, reason=reason)
                self._resolver.revalidate_snapshot(snapshot, require_physical=True)
                return result


class CommunityRoutedObservations(CommunityRoutedRankedSearch):
    """Explicit allowlisted observation methods, never arbitrary provider dispatch."""

    def _observe(self, method, board_id, *args, **kwargs):
        with self._operation(board_id):
            snapshot = self._resolver.acquire_board_route(board_id)
            result = getattr(self._provider, method)(board_id, *args, **kwargs)
            self._resolver.revalidate_snapshot(snapshot, require_physical=True)
            return result

    def _mutate(self, method, board_id, *args, **kwargs):
        with self._mutation(board_id, phase="graph_schema_migrate"):
            snapshot = self._resolver.acquire_board_route(board_id)
            with self._retire_participants(board_id):
                result = getattr(self._provider, method)(board_id, *args, **kwargs)
                self._resolver.revalidate_snapshot(snapshot, require_physical=True)
                return result

    def activate(self, board_id, *args, **kwargs):
        return self._mutate("activate", board_id, *args, **kwargs)

    def prune(self, board_id, *args, **kwargs):
        return self._mutate("prune", board_id, *args, **kwargs)

    def commits(self, board_id, **kwargs):
        return self._observe("commits", board_id, **kwargs)

    def as_of(self, board_id, *args, **kwargs):
        return self._observe("as_of", board_id, *args, **kwargs)

    def diff(self, board_id, *args, **kwargs):
        return self._observe("diff", board_id, *args, **kwargs)

    def analyze(self, board_id, **kwargs):
        return self._observe("analyze", board_id, **kwargs)
