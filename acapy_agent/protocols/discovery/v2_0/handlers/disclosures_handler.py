"""Handler for incoming disclose messages."""

from .....messaging.base_handler import (
    BaseHandler,
    BaseResponder,
    HandlerException,
    RequestContext,
)
from ..manager import V20DiscoveryMgr
from ..messages.disclosures import Disclosures


class DisclosuresHandler(BaseHandler):
    """Handler for incoming disclose messages."""

    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Message handler implementation."""
        self._logger.debug(f"DiscloseHandler called with context {context}")
        assert isinstance(context.message, Disclosures)

        if not context.connection_ready:
            raise HandlerException(
                "Received disclosures message from inactive connection"
            )

        profile = context.profile
        mgr = V20DiscoveryMgr(profile)
        await mgr.receive_disclose(
            context.message, connection_id=context.connection_record.connection_id
        )
