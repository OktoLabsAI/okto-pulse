"""Optional graph capabilities share the existing route and lifecycle authority."""



class CommunityRoutedRankedSearch:
    def __init__(self, resolver, provider, *, operation_window):
        self._resolver = resolver
        self._provider = provider
        self._operation = operation_window


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



class CommunityRoutedObservations(CommunityRoutedRankedSearch):
    """Explicit allowlisted observation methods, never arbitrary provider dispatch."""

    def _observe(self, method, board_id, *args, **kwargs):
        with self._operation(board_id):
            snapshot = self._resolver.acquire_board_route(board_id)
            result = getattr(self._provider, method)(board_id, *args, **kwargs)
            self._resolver.revalidate_snapshot(snapshot, require_physical=True)
            return result




    def commits(self, board_id, **kwargs):
        return self._observe("commits", board_id, **kwargs)

    def as_of(self, board_id, *args, **kwargs):
        return self._observe("as_of", board_id, *args, **kwargs)

    def diff(self, board_id, *args, **kwargs):
        return self._observe("diff", board_id, *args, **kwargs)

    def analyze(self, board_id, **kwargs):
        return self._observe("analyze", board_id, **kwargs)
