import random
from logzero import logger
from neo.Core.Block import Block
from neo.Core.Blockchain import Blockchain as BC
from neo.Implementations.Blockchains.LevelDB.TestLevelDBBlockchain import TestLevelDBBlockchain
from neo.Core.TX.Transaction import Transaction
from neo.Core.TX.MinerTransaction import MinerTransaction
from neo.Network.NeoNode import NeoNode
from neo.Settings import settings
from twisted.internet.protocol import Factory, ReconnectingClientFactory
from twisted.internet import reactor, task
from datetime import datetime, timedelta
import random


class NeoClientFactory(ReconnectingClientFactory):
    protocol = NeoNode
    maxRetries = 2


class NodeLeader():
    __LEAD = None

    Peers = None
    UnconnectedPeers = None
    PendingPeers = None
    BadPeers = None
    ConnectToPeerLoop = None
    NodeId = None

    _MissedBlocks = []

    BREQPART = 20
    NREQMAX = 100
    BREQMAX = 10000

    KnownHashes = []
    MissionsGlobal = []
    MemPool = {}
    RelayCache = {}

    NodeCount = 0

    ServiceEnabled = False

    snapshot_height = None
    snapshot_time = None
    current_bpm = 1
    orig_peer_max = None
    cache_counts = None

    @staticmethod
    def Instance():
        """
        Get the local node instance.

        Returns:
            NodeLeader: instance.
        """
        if NodeLeader.__LEAD is None:
            NodeLeader.__LEAD = NodeLeader()
        return NodeLeader.__LEAD

    def __init__(self):
        """
        Create an instance.
        This is the equivalent to C#'s LocalNode.cs
        """
        self.Setup()
        self.ServiceEnabled = settings.SERVICE_ENABLED

    def Setup(self):
        """
        Initialize the local node.

        Returns:

        """
        self.Peers = set()
        self.UnconnectedPeers = []
        self.PendingPeers = set()
        self.BadPeers = set()
        self.MissionsGlobal = []
        self.cache_counts = []
        self.NodeId = random.randint(1294967200, 4294967200)

        self.snapshot_time = datetime.utcnow()
        self.snapshot_height = BC.Default().Height
        self.orig_peer_max = settings.CONNECTED_PEER_MAX

        self.ConnectToPeerLoop = task.LoopingCall(self.DoConnectToPeer)
        self.ConnectToPeerLoop.start(1)

    def Restart(self):
        if len(self.Peers) == 0:
            self.cache_counts = []
            self.UnconnectedPeers = []
            self.PendingPeers = set()
            self.BadPeers = set()
            self.Start()
            self.ConnectToPeerLoop.start(5)

    def Start(self):
        """Start connecting to the node list."""
        for idx, bootstrap in enumerate(settings.SEED_LIST):
            self.PendingPeers.add(bootstrap)
            host, port = bootstrap.split(':')
            self.SetupConnection(host, port)

    def RemoteNodePeerReceived(self, networkAddressWithTime):
        addr = "%s:%s" % (networkAddressWithTime.Address, networkAddressWithTime.Port)
        if addr not in self.BadPeers:
            self.UnconnectedPeers.append(networkAddressWithTime)

    def DoConnectToPeer(self):

        if len(self.UnconnectedPeers):
            random.shuffle(self.UnconnectedPeers)
            peer = self.UnconnectedPeers.pop(0)
            peer_addr = '%s:%s' % (peer.Address, peer.Port)
            self.PendingPeers.add(peer_addr)
            host, port = peer_addr.split(':')
            self.SetupConnection(host, port)

    def SetupConnection(self, host, port, timeout=5):
        if len(self.Peers) < settings.CONNECTED_PEER_MAX:
            reactor.connectTCP(host, int(port), NeoClientFactory(), timeout=timeout)

    def OnUpdatedMaxPeers(self, old_value, new_value):
        logger.warn("Changing max peers from %s to %s " % (old_value, new_value))
        if new_value < old_value:
            num_to_disconnect = old_value - new_value
            logger.warning("DISCONNECTING %s Peers, this may show unhandled error in defer " % num_to_disconnect)
            for p in self.Peers[-num_to_disconnect:]:
                p.Disconnect()
        elif new_value > old_value:
            # we'll let the peers request more themselves
            pass

    def Shutdown(self):
        """Disconnect all connected peers."""

        for p in self.Peers:
            p.Disconnect()

    def AddConnectedPeer(self, peer):
        """
        Add a new connect peer to the known peers list.

        Args:
            peer (NeoNode): instance.
        """

        if peer.Address in self.PendingPeers:
            self.PendingPeers.remove(peer.Address)

        if len(self.Peers) < settings.CONNECTED_PEER_MAX:
            self.Peers.add(peer)

        else:

            peer.Disconnect()

    def RemoveConnectedPeer(self, peer):
        """
        Remove a connected peer from the known peers list.

        Args:
            peer (NeoNode): instance.
        """
        try:
            logger.warn("Removing connected peer %s " % peer.Address)
            if peer.Address in self.PendingPeers:
                self.PendingPeers.remove(peer.Address)

            if peer in self.Peers:
                self.Peers.remove(peer)
        except Exception as e:
            logger.error("Could not remove peer %s: %s " % (peer.Address, e))

#        self.UnconnectedPeers.add(peer.Address)

        if len(self.Peers) == 0:
            reactor.callLater(10, self.Restart)

    def ResetBlockRequestsAndCache(self):
        """Reset the block request counter and its cache."""
        logger.debug("Resseting Block requests")
        self.MissionsGlobal = []
        BC.Default().BlockSearchTries = 0
        for p in self.Peers:
            p.myblockrequests = set()
        BC.Default().ResetBlockRequests()
        BC.Default()._block_cache = {}

    def InventoryReceived(self, inventory):
        """
        Process a received inventory.

        Args:
            inventory (neo.Network.Inventory): expect a Block type.

        Returns:
            bool: True if processed and verified. False otherwise.
        """
        if inventory.Hash.ToBytes() in self._MissedBlocks:
            self._MissedBlocks.remove(inventory.Hash.ToBytes())

        if inventory is MinerTransaction:
            return False

        if type(inventory) is Block:
            if BC.Default() is None:
                return False

            if BC.Default().ContainsBlock(inventory.Index):
                return False

            if not BC.Default().AddBlock(inventory):
                return False

        else:
            if not inventory.Verify(self.MemPool.values()):
                return False

    def RelayDirectly(self, inventory):
        """
        Relay the inventory to the remote client.

        Args:
            inventory (neo.Network.Inventory):

        Returns:
            bool: True if relayed successfully. False otherwise.
        """
        relayed = False

        self.RelayCache[inventory.Hash.ToBytes()] = inventory

        for peer in self.Peers:
            relayed |= peer.Relay(inventory)

        if len(self.Peers) == 0:
            if type(BC.Default()) is TestLevelDBBlockchain:
                # mock a true result for tests
                return True

            logger.info("no connected peers")

        return relayed

    def Relay(self, inventory):
        """
        Relay the inventory to the remote client.

        Args:
            inventory (neo.Network.Inventory):

        Returns:
            bool: True if relayed successfully. False otherwise.
        """
        if type(inventory) is MinerTransaction:
            return False

        if inventory.Hash.ToBytes() in self.KnownHashes:
            return False

        self.KnownHashes.append(inventory.Hash.ToBytes())

        if type(inventory) is Block:
            pass

        elif type(inventory) is Transaction or issubclass(type(inventory), Transaction):
            if not self.AddTransaction(inventory):
                return False
        else:
            # consensus
            pass

        relayed = self.RelayDirectly(inventory)
        # self.
        return relayed

    def GetTransaction(self, hash):
        if hash in self.MemPool.keys():
            return self.MemPool[hash]
        return None

    def AddTransaction(self, tx):
        """
        Add a transaction to the memory pool.

        Args:
            tx (neo.Core.TX.Transaction): instance.

        Returns:
            bool: True if successfully added. False otherwise.
        """
        if BC.Default() is None:
            return False

        if tx.Hash.ToBytes() in self.MemPool.keys():
            return False

        if BC.Default().ContainsTransaction(tx.Hash):
            return False

        if not tx.Verify(self.MemPool.values()):
            logger.error("Veryfiying tx result... failed")
            return False

        self.MemPool[tx.Hash.ToBytes()] = tx

        return True
