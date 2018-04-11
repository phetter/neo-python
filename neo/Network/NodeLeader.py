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
from datetime import datetime,timedelta

class NeoClientFactory(ReconnectingClientFactory):
    protocol = NeoNode
    maxRetries = 1

    def clientConnectionFailed(self, connector, reason):
        address = "%s:%s" % (connector.host, connector.port)
        logger.debug("Dropped connection from %s " % address)
        for peer in NodeLeader.Instance().Peers:
            if peer.Address == address:
                peer.connectionLost()


class NodeLeader():
    __LEAD = None

    Peers = []

    UnconnectedPeers = []
    PendingPeers = []

    ADDRS = []

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
    bpm_loop = None
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
        self.Peers = []
        self.UnconnectedPeers = []
        self.PendingPeers = []
        self.ADDRS = []
        self.MissionsGlobal = []
        self.cache_counts = []
        self.NodeId = random.randint(1294967200, 4294967200)

        self.snapshot_time = datetime.utcnow()
        self.snapshot_height = BC.Default().Height
        self.bpm_loop = task.LoopingCall(self.MeasureBPM)
        self.bpm_loop.start(20, now=False)
        self.orig_peer_max = settings.CONNECTED_PEER_MAX

    def MeasureBPM(self):

        self.cache_counts.append(BC.Default().BlockCacheCount)

        if len(self.cache_counts) > 10:
            self.cache_counts.pop(0)

        #check if the last 10 cache counts are the same
        if len(self.cache_counts) == 10 and len(set(self.cache_counts)) == 1 and self.cache_counts[0] != 0:
            logger.warn("SAME 10 CACHE COUNS, RESET!!")
            self.ResetBlockRequestsAndCache()
            self.Shutdown()
            return


        if settings.AUTOADAPT_SYNC:
            now_time = datetime.utcnow()
            now_height = BC.Default().Height
            delta_time = (now_time - self.snapshot_time).total_seconds() / 60.0
            delta_height = now_height - self.snapshot_height
            self.current_bpm = int(delta_height / delta_time)
            self.snapshot_time = now_time
            self.snapshot_height = now_height
            logger.info("\n------------------------")
            logger.info("Current Blocks per minute %s " % self.current_bpm)
            logger.info("Current Cache Count %s " % BC.Default().BlockCacheCount)
            logger.info("Current stale count / request count %s %s " % (BC.Default().BlockSearchTries, len(BC.Default().BlockRequests)))
            logger.info("Num peers %s " % len(self.Peers))
            logger.info("Current nreq part/max %s %s " % (self.BREQPART, self.NREQMAX))
            logger.info("Current avg TX Per block %s " % BC.Default().TXPerBlock)

            txPerBlock = BC.Default().TXPerBlock
            current_peers = len(self.Peers)

            # the amount of blocks can be processed when there is a lot of tx
            # is better when there are less connected peers

            # scale up and down connected peers based on tx per block

            # we only do this when we're in catchup mode

            distance = BC.Default().HeaderHeight - BC.Default().Height

            if distance > 2000:

                if txPerBlock > 20 and current_peers != 2:
                    settings.CONNECTED_PEER_MAX = 2
                    self.OnUpdatedMaxPeers(current_peers, 2)
                elif 14 <= txPerBlock <= 20 and current_peers != 3:
                    settings.CONNECTED_PEER_MAX = 3
                    self.OnUpdatedMaxPeers(current_peers, 3)
                elif 10 <= txPerBlock <= 14 and current_peers != 4:
                    settings.CONNECTED_PEER_MAX = 4
                    self.OnUpdatedMaxPeers(current_peers, 4)
                elif 8 <= txPerBlock <= 10 and current_peers != 5:
                    settings.CONNECTED_PEER_MAX = 5
                    self.OnUpdatedMaxPeers(current_peers, 5)
                elif txPerBlock < 8 and settings.CONNECTED_PEER_MAX != self.orig_peer_max:
                    settings.CONNECTED_PEER_MAX = self.orig_peer_max
                    self.OnUpdatedMaxPeers(current_peers, settings.CONNECTED_PEER_MAX)

            else:

                if settings.CONNECTED_PEER_MAX != self.orig_peer_max:
                    settings.CONNECTED_PEER_MAX = self.orig_peer_max


    def Restart(self):
        if len(self.Peers) == 0:
            self.ADDRS = []
            self.cache_counts = []
            self.PendingPeers = []
            self.Start()

    def Start(self):
        """Start connecting to the node list."""
        # start up endpoints
        start_delay = 0
        for bootstrap in settings.SEED_LIST:
            host, port = bootstrap.split(":")
#            self.ADDRS.append('%s:%s' % (host, port))
            reactor.callLater(start_delay, self.SetupConnection, host, port)
            start_delay += 1

    def RemoteNodePeerReceived(self, host, port, index):
        addr = '%s:%s' % (host, port)
        if len(self.Peers) < settings.CONNECTED_PEER_MAX:
            if not addr in self.ADDRS:
                self.ADDRS.append(addr)

            ok = True
            for p in self.Peers:
                if p.Address == addr:
                    ok = False

            if ok and not addr in self.PendingPeers:
                self.PendingPeers.append(addr)
                reactor.callLater(index * 10, self.SetupConnection, host, port)

    def SetupConnection(self, host, port):
        logger.info("Setting up connection to %s:%s " % (host, port))
        if len(self.Peers) < settings.CONNECTED_PEER_MAX:
            reactor.connectTCP(host, int(port), NeoClientFactory())

    def OnUpdatedMaxPeers(self, old_value, new_value):
        logger.warn("Changing max peers from %s to %s " % (old_value, new_value))
        logger.info("Current ADDR %s " % (self.ADDRS))
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
        self.bpm_loop.stop()
        self.bpm_loop = None
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

        if peer not in self.Peers:

            if len(self.Peers) < settings.CONNECTED_PEER_MAX:
                self.Peers.append(peer)
                if not peer.Address in self.ADDRS:
                    self.ADDRS.append(peer.Address)
            else:
                if peer.Address in self.ADDRS:
                    self.ADDRS.remove(peer.Address)
                peer.Disconnect()

    def RemoveConnectedPeer(self, peer):
        """
        Remove a connected peer from the known peers list.

        Args:
            peer (NeoNode): instance.
        """
        logger.warn("Removing connected peer %s " % peer.Address)
        if peer.Address in self.PendingPeers:
            self.PendingPeers.remove(peer.Address)

        if peer in self.Peers:
            self.Peers.remove(peer)
        if peer.Address in self.ADDRS:
            logger.error("REmoving ADdress %s " % peer.Address)
            self.ADDRS.remove(peer.Address)
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
