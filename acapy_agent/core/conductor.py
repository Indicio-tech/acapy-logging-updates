"""The Conductor.

The conductor is responsible for coordinating messages that are received
over the network, communicating with the ledger, passing messages to handlers,
instantiating concrete implementations of required modules and storing data in the
wallet.

"""

import asyncio
import hashlib
import json
import logging
from typing import Optional

from packaging import version as package_version
from qrcode import QRCode

from ..admin.base_server import BaseAdminServer
from ..admin.server import AdminResponder, AdminServer
from ..commands.upgrade import add_version_record, get_upgrade_version_list, upgrade
from ..config.default_context import ContextBuilder, DefaultContextBuilder
from ..config.injection_context import InjectionContext
from ..config.ledger import (
    get_genesis_transactions,
    ledger_config,
    load_multiple_genesis_transactions_from_config,
)
from ..config.logging import LoggingConfigurator
from ..config.provider import ClassProvider
from ..config.wallet import wallet_config
from ..connections.base_manager import BaseConnectionManager, BaseConnectionManagerError
from ..core.profile import Profile
from ..indy.verifier import IndyVerifier
from ..ledger.base import BaseLedger
from ..ledger.error import LedgerConfigError, LedgerTransactionError
from ..ledger.multiple_ledger.base_manager import (
    BaseMultipleLedgerManager,
    MultipleLedgerManagerError,
)
from ..ledger.multiple_ledger.ledger_requests_executor import IndyLedgerRequestsExecutor
from ..ledger.multiple_ledger.manager_provider import MultiIndyLedgerManagerProvider
from ..messaging.responder import BaseResponder
from ..multitenant.base import BaseMultitenantManager
from ..multitenant.manager_provider import MultitenantManagerProvider
from ..protocols.coordinate_mediation.mediation_invite_store import MediationInviteStore
from ..protocols.coordinate_mediation.v1_0.manager import MediationManager
from ..protocols.coordinate_mediation.v1_0.route_manager import RouteManager
from ..protocols.coordinate_mediation.v1_0.route_manager_provider import (
    RouteManagerProvider,
)
from ..protocols.out_of_band.v1_0.manager import OutOfBandManager
from ..protocols.out_of_band.v1_0.messages.invitation import HSProto, InvitationMessage
from ..storage.base import BaseStorage
from ..storage.error import StorageNotFoundError
from ..storage.record import StorageRecord
from ..storage.type import (
    RECORD_TYPE_ACAPY_STORAGE_TYPE,
    STORAGE_TYPE_VALUE_ANONCREDS,
    STORAGE_TYPE_VALUE_ASKAR,
)
from ..transport.inbound.manager import InboundTransportManager
from ..transport.inbound.message import InboundMessage
from ..transport.outbound.base import OutboundDeliveryError
from ..transport.outbound.manager import OutboundTransportManager, QueuedOutboundMessage
from ..transport.outbound.message import OutboundMessage
from ..transport.outbound.status import OutboundSendStatus
from ..transport.wire_format import BaseWireFormat
from ..utils.profiles import get_subwallet_profiles_from_storage
from ..utils.stats import Collector
from ..utils.task_queue import CompletedTask, TaskQueue
from ..vc.ld_proofs.document_loader import DocumentLoader
from ..version import RECORD_TYPE_ACAPY_VERSION, __version__
from ..wallet.anoncreds_upgrade import upgrade_wallet_to_anoncreds_if_requested
from ..wallet.did_info import DIDInfo
from ..wallet.singletons import IsAnonCredsSingleton
from .dispatcher import Dispatcher
from .error import ProfileError, StartupError
from .oob_processor import OobMessageProcessor
from .util import SHUTDOWN_EVENT_TOPIC, STARTUP_EVENT_TOPIC

LOGGER = logging.getLogger(__name__)
# Refer ACA-Py issue #2197
# When the from version is not found
DEFAULT_ACAPY_VERSION = "v0.7.5"


class Conductor:
    """Conductor class.

    Class responsible for initializing concrete implementations
    of our required interfaces and routing inbound and outbound message data.
    """

    def __init__(self, context_builder: ContextBuilder) -> None:
        """Initialize an instance of Conductor.

        Args:
            inbound_transports: Configuration for inbound transports
            outbound_transports: Configuration for outbound transports
            settings: Dictionary of various settings
            context_builder: Context builder for the conductor

        """
        self.admin_server = None
        self.context_builder = context_builder
        self.dispatcher: Optional[Dispatcher] = None
        self.inbound_transport_manager: Optional[InboundTransportManager] = None
        self.outbound_transport_manager: Optional[OutboundTransportManager] = None
        self.root_profile: Optional[Profile] = None
        self.setup_public_did: Optional[DIDInfo] = None

    force_agent_anoncreds = False

    @property
    def context(self) -> InjectionContext:
        """Accessor for the injection context."""
        assert self.root_profile, "root_profile is not set"
        return self.root_profile.context

    async def setup(self):
        """Initialize the global request context."""
        LOGGER.debug("Starting setup of the Conductor")

        context = await self.context_builder.build_context()
        LOGGER.debug("Context built successfully")

        if self.force_agent_anoncreds:
            LOGGER.debug(
                "Force agent anoncreds is enabled. "
                "Setting wallet type to 'askar-anoncreds'."
            )
            context.settings.set_value("wallet.type", "askar-anoncreds")

        # Fetch genesis transactions if necessary
        if context.settings.get("ledger.ledger_config_list"):
            LOGGER.debug(
                "Ledger config list found. Loading multiple genesis transactions"
            )
            await load_multiple_genesis_transactions_from_config(context.settings)
        if (
            context.settings.get("ledger.genesis_transactions")
            or context.settings.get("ledger.genesis_file")
            or context.settings.get("ledger.genesis_url")
        ):
            LOGGER.debug(
                "Genesis transactions/configurations found. Fetching genesis transactions"
            )
            await get_genesis_transactions(context.settings)

        # Configure the root profile
        LOGGER.debug("Configuring the root profile and setting up public DID")
        self.root_profile, self.setup_public_did = await wallet_config(context)
        context = self.root_profile.context
        LOGGER.debug("Root profile configured successfully")

        # Multiledger Setup
        ledger_config_list = context.settings.get("ledger.ledger_config_list")
        if ledger_config_list and len(ledger_config_list) > 0:
            LOGGER.debug("Setting up multiledger manager")
            context.injector.bind_provider(
                BaseMultipleLedgerManager,
                MultiIndyLedgerManagerProvider(self.root_profile),
            )
            if not context.settings.get("ledger.genesis_transactions"):
                ledger = context.injector.inject(BaseLedger)
                LOGGER.debug(
                    f"Ledger backend: {ledger.BACKEND_NAME}, Profile backend: {self.root_profile.BACKEND_NAME}"
                )
                if (
                    self.root_profile.BACKEND_NAME == "askar"
                    and ledger.BACKEND_NAME == "indy-vdr"
                ):
                    LOGGER.debug("Binding IndyCredxVerifier for 'askar' backend.")
                    context.injector.bind_provider(
                        IndyVerifier,
                        ClassProvider(
                            "acapy_agent.indy.credx.verifier.IndyCredxVerifier",
                            self.root_profile,
                        ),
                    )
                elif (
                    self.root_profile.BACKEND_NAME == "askar-anoncreds"
                    and ledger.BACKEND_NAME == "indy-vdr"
                ):
                    LOGGER.debug(
                        "Binding IndyCredxVerifier for 'askar-anoncreds' backend."
                    )
                    context.injector.bind_provider(
                        IndyVerifier,
                        ClassProvider(
                            "acapy_agent.anoncreds.credx.verifier.IndyCredxVerifier",
                            self.root_profile,
                        ),
                    )
                else:
                    LOGGER.error("Unsupported ledger backend for multiledger setup.")
                    raise MultipleLedgerManagerError(
                        "Multiledger is supported only for Indy SDK or Askar "
                        "[Indy VDR] profile"
                    )
        context.injector.bind_instance(
            IndyLedgerRequestsExecutor, IndyLedgerRequestsExecutor(self.root_profile)
        )

        # Configure the ledger
        ledger_configured = await ledger_config(
            self.root_profile, self.setup_public_did and self.setup_public_did.did
        )
        if not ledger_configured:
            LOGGER.info("No ledger configured.")
        else:
            LOGGER.info("Ledger configured successfully.")

        if not context.settings.get("transport.disabled"):
            # Register all inbound transports if enabled
            LOGGER.debug("Transport not disabled. Setting up inbound transports.")
            self.inbound_transport_manager = InboundTransportManager(
                self.root_profile, self.inbound_message_router, self.handle_not_returned
            )
            await self.inbound_transport_manager.setup()
            context.injector.bind_instance(
                InboundTransportManager, self.inbound_transport_manager
            )
            LOGGER.debug("Inbound transports registered successfully.")

            # Register all outbound transports
            LOGGER.debug("Setting up outbound transports.")
            self.outbound_transport_manager = OutboundTransportManager(
                self.root_profile, self.handle_not_delivered
            )
            await self.outbound_transport_manager.setup()
            LOGGER.debug("Outbound transports registered successfully.")

        # Initialize dispatcher
        LOGGER.debug("Initializing dispatcher.")
        self.dispatcher = Dispatcher(self.root_profile)
        await self.dispatcher.setup()
        LOGGER.debug("Dispatcher initialized successfully.")

        wire_format = context.inject_or(BaseWireFormat)
        if wire_format and hasattr(wire_format, "task_queue"):
            wire_format.task_queue = self.dispatcher.task_queue
            LOGGER.debug("Wire format task queue bound to dispatcher.")

        # Bind manager for multitenancy related tasks
        if context.settings.get("multitenant.enabled"):
            LOGGER.debug("Multitenant is enabled. Binding MultitenantManagerProvider.")
            context.injector.bind_provider(
                BaseMultitenantManager, MultitenantManagerProvider(self.root_profile)
            )

        # Bind route manager provider
        LOGGER.debug("Binding RouteManagerProvider.")
        context.injector.bind_provider(
            RouteManager, RouteManagerProvider(self.root_profile)
        )

        # Bind OobMessageProcessor to be able to receive and process unencrypted messages
        LOGGER.debug("Binding OobMessageProcessor.")
        context.injector.bind_instance(
            OobMessageProcessor,
            OobMessageProcessor(inbound_message_router=self.inbound_message_router),
        )

        # Bind default PyLD document loader
        LOGGER.debug("Binding default DocumentLoader.")
        context.injector.bind_instance(DocumentLoader, DocumentLoader(self.root_profile))

        # Admin API
        if context.settings.get("admin.enabled"):
            LOGGER.debug("Admin API is enabled. Attempting to register admin server.")
            try:
                admin_host = context.settings.get("admin.host", "0.0.0.0")
                admin_port = context.settings.get("admin.port", "80")
                self.admin_server = AdminServer(
                    admin_host,
                    admin_port,
                    context,
                    self.root_profile,
                    self.outbound_message_router,
                    self.webhook_router,
                    self.stop,
                    self.dispatcher.task_queue,
                    self.get_stats,
                )
                context.injector.bind_instance(BaseAdminServer, self.admin_server)
                LOGGER.debug(f"Admin server registered on {admin_host}:{admin_port}")
            except Exception:
                LOGGER.exception("Unable to register admin server.")
                raise

        # Fetch stats collector, if any
        collector = context.inject_or(Collector)
        if collector:
            LOGGER.debug("Stats collector found. Wrapping methods for collection.")
            # add stats to our own methods
            collector.wrap(
                self,
                (
                    # "inbound_message_router",
                    "outbound_message_router",
                    # "create_inbound_session",
                ),
            )
            # at the class level (!) should not be performed multiple times
            collector.wrap(
                BaseConnectionManager,
                (
                    # "get_connection_targets",
                    "fetch_did_document",
                    "find_inbound_connection",
                ),
            )
            LOGGER.debug("Methods wrapped with stats collector.")

    async def start(self) -> None:
        """Start the agent."""
        LOGGER.debug("Starting the Conductor agent.")
        assert self.root_profile, "root_profile is not set"
        context = self.root_profile.context
        await self.check_for_valid_wallet_type(self.root_profile)
        LOGGER.debug("Wallet type validated.")

        if not context.settings.get("transport.disabled"):
            # Start up transports if enabled
            try:
                LOGGER.debug("Transport not disabled. Starting inbound transports.")
                await self.inbound_transport_manager.start()
                LOGGER.debug("Inbound transports started successfully.")
            except Exception:
                LOGGER.exception("Unable to start inbound transports.")
                raise
            try:
                LOGGER.debug("Starting outbound transports.")
                await self.outbound_transport_manager.start()
                LOGGER.debug("Outbound transports started successfully.")
            except Exception:
                LOGGER.exception("Unable to start outbound transports.")
                raise

        # Start up Admin server
        if self.admin_server:
            LOGGER.debug("Admin server present. Starting admin server.")
            try:
                await self.admin_server.start()
                LOGGER.debug("Admin server started successfully.")
            except Exception:
                LOGGER.exception("Unable to start administration API.")
            # Make admin responder available during message parsing
            # This allows webhooks to be called when a connection is marked active,
            # for example
            responder = AdminResponder(
                self.root_profile,
                self.admin_server.outbound_message_router,
            )
            context.injector.bind_instance(BaseResponder, responder)
            LOGGER.debug("Admin responder bound to injector.")

        # Get agent label
        default_label = context.settings.get("default_label")
        LOGGER.debug(f"Agent label: {default_label}")

        if context.settings.get("transport.disabled"):
            LoggingConfigurator.print_banner(
                default_label,
                None,
                None,
                self.setup_public_did and self.setup_public_did.did,
                self.admin_server,
            )
        else:
            LoggingConfigurator.print_banner(
                default_label,
                self.inbound_transport_manager.registered_transports,
                self.outbound_transport_manager.registered_transports,
                self.setup_public_did and self.setup_public_did.did,
                self.admin_server,
            )

        LoggingConfigurator.print_notices(context.settings)

        # record ACA-Py version in Wallet, if needed
        from_version_storage = None
        from_version = None
        agent_version = f"v{__version__}"
        LOGGER.debug("Recording ACA-Py version in wallet if needed.")
        async with self.root_profile.session() as session:
            storage: BaseStorage = session.context.inject(BaseStorage)
            try:
                record = await storage.find_record(
                    type_filter=RECORD_TYPE_ACAPY_VERSION,
                    tag_query={},
                )
                from_version_storage = record.value
                LOGGER.info(
                    f"Existing acapy_version storage record found, version set to {from_version_storage}"
                )
            except StorageNotFoundError:
                LOGGER.info("Wallet version storage record not found.")

        from_version_config = self.root_profile.settings.get("upgrade.from_version")
        force_upgrade_flag = (
            self.root_profile.settings.get("upgrade.force_upgrade") or False
        )
        LOGGER.debug(
            f"Force upgrade flag: {force_upgrade_flag}, From version config: {from_version_config}"
        )

        if force_upgrade_flag and from_version_config:
            if from_version_storage:
                if package_version.parse(from_version_storage) > package_version.parse(
                    from_version_config
                ):
                    from_version = from_version_config
                else:
                    from_version = from_version_storage
            else:
                from_version = from_version_config
            LOGGER.debug(
                f"Determined from_version based on force_upgrade: {from_version}"
            )
        else:
            from_version = from_version_storage or from_version_config
            LOGGER.debug(f"Determined from_version: {from_version}")

        if not from_version:
            LOGGER.info(
                "No upgrade from version was found from wallet or via"
                f" --from-version startup argument. Defaulting to {DEFAULT_ACAPY_VERSION}."
            )
            from_version = DEFAULT_ACAPY_VERSION
            self.root_profile.settings.set_value("upgrade.from_version", from_version)
            LOGGER.debug(f"Set upgrade.from_version to default: {from_version}")

        config_available_list = get_upgrade_version_list(
            config_path=self.root_profile.settings.get("upgrade.config_path"),
            from_version=from_version,
        )
        LOGGER.debug(f"Available upgrade versions: {config_available_list}")

        if len(config_available_list) >= 1:
            LOGGER.info("Upgrade configurations available. Initiating upgrade.")
            await upgrade(profile=self.root_profile)
        elif not (from_version_storage and from_version_storage == agent_version):
            LOGGER.debug("No upgrades needed. Adding version record.")
            await add_version_record(profile=self.root_profile, version=agent_version)

        # Create a static connection for use by the test-suite
        if context.settings.get("debug.test_suite_endpoint"):
            LOGGER.debug(
                "Test suite endpoint configured. "
                "Creating static connection for test suite."
            )
            mgr = BaseConnectionManager(self.root_profile)
            their_endpoint = context.settings["debug.test_suite_endpoint"]
            _, _, test_conn = await mgr.create_static_connection(
                my_seed=hashlib.sha256(b"aries-protocol-test-subject").digest(),
                their_seed=hashlib.sha256(b"aries-protocol-test-suite").digest(),
                their_endpoint=their_endpoint,
                alias="test-suite",
            )
            LOGGER.info(
                "Created static connection for test suite\n"
                f" - My DID: {test_conn.my_did}\n"
                f" - Their DID: {test_conn.their_did}\n"
                f" - Their endpoint: {their_endpoint}\n"
            )
            del mgr
            LOGGER.debug("Static connection for test suite created and manager deleted.")

        # Clear default mediator
        if context.settings.get("mediation.clear"):
            LOGGER.debug("Mediation clear flag set. Clearing default mediator.")
            mediation_mgr = MediationManager(self.root_profile)
            await mediation_mgr.clear_default_mediator()
            LOGGER.info("Default mediator cleared.")

        # Set default mediator by id
        default_mediator_id = context.settings.get("mediation.default_id")
        if default_mediator_id:
            LOGGER.debug(f"Setting default mediator to ID: {default_mediator_id}")
            mediation_mgr = MediationManager(self.root_profile)
            try:
                await mediation_mgr.set_default_mediator_by_id(default_mediator_id)
                LOGGER.info(f"Default mediator set to {default_mediator_id}")
            except Exception:
                LOGGER.exception("Error updating default mediator.")

        # Print an invitation to the terminal
        if context.settings.get("debug.print_invitation"):
            LOGGER.debug(
                "Debug flag for printing invitation is set. Creating invitation."
            )
            try:
                mgr = OutOfBandManager(self.root_profile)
                invi_rec = await mgr.create_invitation(
                    my_label=context.settings.get("debug.invite_label"),
                    public=context.settings.get("debug.invite_public", False),
                    multi_use=context.settings.get("debug.invite_multi_use", False),
                    hs_protos=[HSProto.RFC23],
                    metadata=json.loads(
                        context.settings.get("debug.invite_metadata_json", "{}")
                    ),
                )
                base_url = context.settings.get("invite_base_url")
                invite_url = invi_rec.invitation.to_url(base_url)
                LOGGER.info(f"Invitation URL:\n{invite_url}")
                qr = QRCode(border=1)
                qr.add_data(invite_url)
                qr.print_ascii(invert=True)
                del mgr
            except Exception:
                LOGGER.exception("Error creating invitation.")

        # mediation connection establishment
        provided_invite: str = context.settings.get("mediation.invite")
        LOGGER.debug(f"Mediation invite provided: {provided_invite}")

        try:
            async with self.root_profile.session() as session:
                invite_store = MediationInviteStore(session.context.inject(BaseStorage))
                mediation_invite_record = await invite_store.get_mediation_invite_record(
                    provided_invite
                )
                LOGGER.debug("Mediation invite record retrieved successfully.")
        except Exception:
            LOGGER.exception("Error retrieving mediator invitation.")
            mediation_invite_record = None

        # Accept mediation invitation if one was specified or stored
        if mediation_invite_record is not None:
            LOGGER.debug(
                "Mediation invite record found. "
                "Attempting to accept mediation invitation."
            )
            try:
                if not mediation_invite_record.used:
                    # clear previous mediator configuration before establishing a new one
                    LOGGER.debug(
                        "Mediation invite not used. "
                        "Clearing default mediator before accepting new invite."
                    )
                    await MediationManager(self.root_profile).clear_default_mediator()

                    mgr = OutOfBandManager(self.root_profile)
                    LOGGER.debug("Receiving mediation invitation.")
                    record = await mgr.receive_invitation(
                        invitation=InvitationMessage.from_url(
                            mediation_invite_record.invite
                        ),
                        auto_accept=True,
                    )
                    async with self.root_profile.session() as session:
                        await MediationInviteStore(
                            session.context.inject(BaseStorage)
                        ).mark_default_invite_as_used()
                        LOGGER.debug("Marked mediation invite as used.")

                        await record.metadata_set(
                            session, MediationManager.SEND_REQ_AFTER_CONNECTION, True
                        )
                        await record.metadata_set(
                            session, MediationManager.SET_TO_DEFAULT_ON_GRANTED, True
                        )
                        LOGGER.debug("Set mediation metadata after connection.")

                    LOGGER.info("Attempting to connect to mediator...")
                    del mgr
                    LOGGER.debug("Mediation manager deleted after setting up mediator.")
            except Exception:
                LOGGER.exception("Error accepting mediation invitation.")

        try:
            LOGGER.debug("Checking for wallet upgrades in progress.")
            await self.check_for_wallet_upgrades_in_progress()
            LOGGER.debug("Wallet upgrades check completed.")
        except Exception:
            LOGGER.exception(
                "An exception was caught while checking for wallet upgrades in progress."
            )

        # Ensure anoncreds wallet is added to singleton (avoids unnecessary upgrade check)
        if self.root_profile.settings.get("wallet.type") == "askar-anoncreds":
            IsAnonCredsSingleton().set_wallet(self.root_profile.name)

        # notify protocols of startup status
        LOGGER.debug("Notifying protocols of startup status.")
        await self.root_profile.notify(STARTUP_EVENT_TOPIC, {})
        LOGGER.debug("Startup notification sent.")

        LOGGER.info("Listening...")

    async def stop(self, timeout=1.0):
        """Stop the agent."""
        LOGGER.info("Stopping the Conductor agent.")
        # notify protocols that we are shutting down
        if self.root_profile:
            LOGGER.debug("Notifying protocols of shutdown.")
            await self.root_profile.notify(SHUTDOWN_EVENT_TOPIC, {})
            LOGGER.debug("Shutdown notification sent.")

        shutdown = TaskQueue()
        if self.dispatcher:
            LOGGER.debug("Initiating shutdown of dispatcher.")
            shutdown.run(self.dispatcher.complete())
        if self.admin_server:
            LOGGER.debug("Initiating shutdown of admin server.")
            shutdown.run(self.admin_server.stop())
        if self.inbound_transport_manager:
            LOGGER.debug("Initiating shutdown of inbound transport manager.")
            shutdown.run(self.inbound_transport_manager.stop())
        if self.outbound_transport_manager:
            LOGGER.debug("Initiating shutdown of outbound transport manager.")
            shutdown.run(self.outbound_transport_manager.stop())

        if self.root_profile:
            # close multitenant profiles
            multitenant_mgr = self.context.inject_or(BaseMultitenantManager)
            if multitenant_mgr:
                LOGGER.debug("Closing multitenant profiles.")
                for profile in multitenant_mgr.open_profiles:
                    LOGGER.debug(f"Closing profile: {profile.name}")
                    shutdown.run(profile.close())
            LOGGER.debug("Closing root profile.")
            shutdown.run(self.root_profile.close())

        LOGGER.debug(f"Waiting for shutdown tasks to complete with timeout={timeout}.")
        await shutdown.complete(timeout)
        LOGGER.info("Conductor agent stopped successfully.")

    def inbound_message_router(
        self,
        profile: Profile,
        message: InboundMessage,
        can_respond: bool = False,
    ):
        """Route inbound messages.

        Args:
            profile: The active profile for the request
            message: The inbound message instance
            can_respond: If the session supports return routing

        """

        if message.receipt.direct_response_requested and not can_respond:
            LOGGER.warning(
                f"Direct response requested, but not supported by transport: {message.transport_type}"
            )

        # Note: at this point we could send the message to a shared queue
        # if this pod is too busy to process it

        try:
            self.dispatcher.queue_message(
                profile,
                message,
                self.outbound_message_router,
                lambda completed: self.dispatch_complete(message, completed),
            )
        except (LedgerConfigError, LedgerTransactionError) as e:
            LOGGER.error(f"Ledger error occurred in message handler: {str(e)}")
            raise

    def dispatch_complete(self, message: InboundMessage, completed: CompletedTask):
        """Handle completion of message dispatch."""
        if completed.exc_info:
            exc_class, exc, _ = completed.exc_info
            if isinstance(exc, (LedgerConfigError, LedgerTransactionError)):
                LOGGER.error(
                    f"Ledger error occurred in message handler: {str(exc)}",
                    exc_info=completed.exc_info,
                )
            elif isinstance(exc, (ProfileError, StorageNotFoundError)):
                LOGGER.error(
                    f"Storage error occurred in message handler: {exc_class.__name__}: {str(exc)}",
                    exc_info=completed.exc_info,
                )
            else:
                LOGGER.exception(
                    "Exception in message handler:", exc_info=completed.exc_info
                )

        self.inbound_transport_manager.dispatch_complete(message, completed)

    async def get_stats(self) -> dict:
        """Get the current stats tracked by the conductor."""
        stats = {
            "in_sessions": (
                len(self.inbound_transport_manager.sessions)
                if self.inbound_transport_manager
                else 0
            ),
            "out_encode": 0,
            "out_deliver": 0,
            "task_active": self.dispatcher.task_queue.current_active,
            "task_done": self.dispatcher.task_queue.total_done,
            "task_failed": self.dispatcher.task_queue.total_failed,
            "task_pending": self.dispatcher.task_queue.current_pending,
        }
        if self.outbound_transport_manager:
            for m in self.outbound_transport_manager.outbound_buffer:
                if m.state == QueuedOutboundMessage.STATE_ENCODE:
                    stats["out_encode"] += 1
                if m.state == QueuedOutboundMessage.STATE_DELIVER:
                    stats["out_deliver"] += 1
        return stats

    async def outbound_message_router(
        self,
        profile: Profile,
        outbound: OutboundMessage,
        inbound: Optional[InboundMessage] = None,
    ) -> OutboundSendStatus:
        """Route an outbound message.

        Args:
            profile: The active profile for the request
            outbound: An outbound message to be sent
            inbound: The inbound message that produced this response, if available
        """
        status: OutboundSendStatus = await self._outbound_message_router(
            profile=profile, outbound=outbound, inbound=inbound
        )
        await profile.notify(status.topic, outbound)
        return status

    async def _outbound_message_router(
        self,
        profile: Profile,
        outbound: OutboundMessage,
        inbound: Optional[InboundMessage] = None,
    ) -> OutboundSendStatus:
        """Route an outbound message.

        Args:
            profile: The active profile for the request
            outbound: An outbound message to be sent
            inbound: The inbound message that produced this response, if available
        """
        if not outbound.target and outbound.reply_to_verkey:
            if not outbound.reply_from_verkey and inbound:
                outbound.reply_from_verkey = inbound.receipt.recipient_verkey
            # return message to an inbound session
            if self.inbound_transport_manager.return_to_session(outbound):
                return OutboundSendStatus.SENT_TO_SESSION

        if not outbound.to_session_only:
            return await self.queue_outbound(profile, outbound, inbound)

    def handle_not_returned(self, profile: Profile, outbound: OutboundMessage):
        """Handle a message that failed delivery via an inbound session."""
        try:
            self.dispatcher.run_task(self.queue_outbound(profile, outbound))
        except (LedgerConfigError, LedgerTransactionError) as e:
            LOGGER.error(
                f"Ledger error occurred while handling failed delivery: {str(e)}"
            )
            raise

    async def queue_outbound(
        self,
        profile: Profile,
        outbound: OutboundMessage,
        inbound: Optional[InboundMessage] = None,
    ) -> OutboundSendStatus:
        """Queue an outbound message for transport.

        Args:
            profile: The active profile
            outbound: The outbound message to be sent
            inbound: The inbound message that produced this response, if available
        """
        has_target = outbound.target or outbound.target_list

        # populate connection target(s)
        if not has_target and outbound.connection_id:
            conn_mgr = profile.inject(BaseConnectionManager)
            try:
                assert self.dispatcher, "dispatcher is not set"
                outbound.target_list = await self.dispatcher.run_task(
                    conn_mgr.get_connection_targets(connection_id=outbound.connection_id)
                )
            except BaseConnectionManagerError:
                LOGGER.exception("Error preparing outbound message for transmission")
                return OutboundSendStatus.UNDELIVERABLE
            except (LedgerConfigError, LedgerTransactionError) as e:
                LOGGER.error(
                    f"Ledger error occurred while preparing outbound message: {str(e)}"
                )
                raise
            del conn_mgr
        # Find oob/connectionless target we can send the message to
        elif not has_target and outbound.reply_thread_id:
            message_processor = profile.inject(OobMessageProcessor)
            outbound.target = await self.dispatcher.run_task(
                message_processor.find_oob_target_for_outbound_message(profile, outbound)
            )

        return await self._queue_message(profile, outbound)

    async def _queue_message(
        self, profile: Profile, outbound: OutboundMessage
    ) -> OutboundSendStatus:
        """Save the message to an internal outbound queue."""
        try:
            await self.outbound_transport_manager.enqueue_message(profile, outbound)
            return OutboundSendStatus.QUEUED_FOR_DELIVERY
        except OutboundDeliveryError:
            LOGGER.warning("Cannot queue message for delivery, no supported transport")
            return self.handle_not_delivered(profile, outbound)

    def handle_not_delivered(
        self, profile: Profile, outbound: OutboundMessage
    ) -> OutboundSendStatus:
        """Handle a message that failed delivery via outbound transports."""
        queued_for_inbound = self.inbound_transport_manager.return_undelivered(outbound)
        return (
            OutboundSendStatus.WAITING_FOR_PICKUP
            if queued_for_inbound
            else OutboundSendStatus.UNDELIVERABLE
        )

    def webhook_router(
        self,
        topic: str,
        payload: dict,
        endpoint: str,
        max_attempts: Optional[int] = None,
        metadata: Optional[dict] = None,
    ):
        """Route a webhook through the outbound transport manager.

        Args:
            topic: The webhook topic
            payload: The webhook payload
            endpoint: The endpoint of the webhook target
            max_attempts: The maximum number of attempts
            metadata: Additional metadata associated with the payload
        """
        try:
            self.outbound_transport_manager.enqueue_webhook(
                topic, payload, endpoint, max_attempts, metadata
            )
        except OutboundDeliveryError:
            LOGGER.warning(
                "Cannot queue message webhook for delivery, no supported transport"
            )

    async def check_for_valid_wallet_type(self, profile):
        """Check wallet type and set it if not set. Raise an error if wallet type config doesn't match existing storage type."""  # noqa: E501
        async with profile.session() as session:
            storage_type_from_config = profile.settings.get("wallet.type")
            storage = session.inject(BaseStorage)
            try:
                storage_type_record = await storage.find_record(
                    type_filter=RECORD_TYPE_ACAPY_STORAGE_TYPE, tag_query={}
                )
                storage_type_from_storage = storage_type_record.value
            except StorageNotFoundError:
                storage_type_record = None

            if not storage_type_record:
                LOGGER.info("Wallet type record not found.")
                try:
                    acapy_version = await storage.find_record(
                        type_filter=RECORD_TYPE_ACAPY_VERSION, tag_query={}
                    )
                except StorageNotFoundError:
                    acapy_version = None
                # Any existing agent will have acapy_version record
                if acapy_version:
                    storage_type_from_storage = STORAGE_TYPE_VALUE_ASKAR
                    LOGGER.info(
                        f"Existing agent found. Setting wallet type to {storage_type_from_storage}."
                    )
                    await storage.add_record(
                        StorageRecord(
                            RECORD_TYPE_ACAPY_STORAGE_TYPE,
                            storage_type_from_storage,
                        )
                    )
                else:
                    storage_type_from_storage = storage_type_from_config
                    LOGGER.info(
                        f"New agent. Setting wallet type to {storage_type_from_config}."
                    )
                    await storage.add_record(
                        StorageRecord(
                            RECORD_TYPE_ACAPY_STORAGE_TYPE,
                            storage_type_from_config,
                        )
                    )

            if storage_type_from_storage != storage_type_from_config:
                if (
                    storage_type_from_config == STORAGE_TYPE_VALUE_ASKAR
                    and storage_type_from_storage == STORAGE_TYPE_VALUE_ANONCREDS
                ):
                    LOGGER.warning(
                        "The agent has been upgrade to use anoncreds wallet. Please update the wallet.type in the config file to 'askar-anoncreds'"  # noqa: E501
                    )
                    # Allow agent to create anoncreds profile with askar
                    # wallet type config by stopping conductor and reloading context
                    await self.stop()
                    self.force_agent_anoncreds = True
                    self.context.settings.set_value("wallet.type", "askar-anoncreds")
                    self.context_builder = DefaultContextBuilder(self.context.settings)
                    await self.setup()
                else:
                    raise StartupError(
                        f"Wallet type config [{storage_type_from_config}] doesn't match with the wallet type in storage [{storage_type_record.value}]"  # noqa: E501
                    )

    async def check_for_wallet_upgrades_in_progress(self):
        """Check for upgrade and upgrade if needed."""
        if self.context.settings.get_value("multitenant.enabled"):
            # Sub-wallets
            subwallet_profiles = await get_subwallet_profiles_from_storage(
                self.root_profile
            )
            await asyncio.gather(
                *[
                    upgrade_wallet_to_anoncreds_if_requested(profile, is_subwallet=True)
                    for profile in subwallet_profiles
                ]
            )

        # Stand-alone or admin wallet
        await upgrade_wallet_to_anoncreds_if_requested(self.root_profile)
