"""Handler for incoming queries messages."""

from .....messaging.base_handler import (
    BaseHandler,
    BaseResponder,
    HandlerException,
    RequestContext,
)
from ..manager import V20DiscoveryMgr
from ..messages.queries import Queries


class QueriesHandler(BaseHandler):
    """Handler for incoming queries messages."""

    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Message handler implementation."""
        self._logger.debug(f"QueryHandler called with context {context}")
        assert isinstance(context.message, Queries)

        if not context.connection_ready:
            raise HandlerException("No connection established")

        profile = context.profile
        mgr = V20DiscoveryMgr(profile)
        reply = await mgr.receive_query(context.message)
        reply.assign_thread_from(context.message)
        reply.assign_trace_from(context.message)
        await responder.send_reply(reply)
