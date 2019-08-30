"""
Module for implementing a bitcoin/lightning regtest network.
"""
import time
import logging
import importlib
import pickle
import os
import tempfile
from collections import defaultdict
import importlib.util


from lnregtest.lib.common import (
    logger_config, WAIT_AFTER_MINING_THREE, WAIT_AFTER_ALL_LND_STARTED,
    WAIT_AFTER_FILLING_WALLETS, WAIT_BEFORE_CLEANUP
)

from lnregtest.lib.network_components import RegTestLND, RegTestBitcoind
from lnregtest.lib.utils import format_dict

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class RegtestNetwork(object):
    """
    Wires all the network components together and controls the logic.
    """
    def __init__(self, binary_folder=None, network_definition_location='star_ring',
                 nodedata_folder='', node_limit='C', from_scratch=True):
        """
        :param binary_folder:str:
            absolute path to where node/cli binaries reside, if not given,
            binaries will be taken from $PATH
        :param network_definition_location: str:
            specifies python module in network_templates which defines the
            network
            alternatively, an absolute path to a python
        :param nodedata_folder: str:
            absolute path to nodedata folder, where node runtime data is saved
            if no value is given, a temporary folder is created
        :param node_limit: char:
            even if more nodes are specified in the network definition,
            the number of nodes used can be limited by specifying an upper
            limit indicated by a character, e.g. 'C' would create nodes A, B, C
        :param from_scratch: bool:
            specifies if the network should be generated without restoring from
            a previous state
        """
        # determine what network_definition is
        if os.path.isabs(network_definition_location):
            # if absolute path, import from absolute path
            spec = importlib.util.spec_from_file_location(
                "network_definition",
                network_definition_location)

            network_definition_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(network_definition_module)
            # extract the file name from the path and use as suffix for
            # runtime data folder
            file_without_extension = os.path.splitext(
                network_definition_location)[0]
            network_definition_suffix = os.path.basename(
                file_without_extension)
        else:
            # if network_definition is not an absolute path
            network_definition_suffix = network_definition_location
            network_definition_module = importlib.import_module(
                ".network_definitions." + network_definition_location,
                package='lnregtest')

        # define where runtime data will be saved to
        if not nodedata_folder:
            if not from_scratch:
                raise ValueError(
                    'If no nodedata_folder is given, '
                    'restarting is not possible.')
            # create temporary directory
            self.temp_dir = tempfile.TemporaryDirectory(
                prefix='lnregtest-', suffix='-' + network_definition_suffix)
            self.nodedata_folder = self.temp_dir.name
        else:
            self.nodedata_folder = os.path.join(
                nodedata_folder, network_definition_suffix)
        logger.info('Runtime data resides in: %s', self.nodedata_folder)

        # initialize bitcoind
        self.from_scratch = from_scratch
        self.node_limit = node_limit
        self.bitcoind = RegTestBitcoind(self.nodedata_folder, binary_folder)

        self.node_defintion = self.get_reduced_network_definition(
            network_definition_module)

        # define empty node and channel mappings
        self.channel_mapping = defaultdict(dict)
        self.channel_mapping_inverse = {}
        self.node_mapping = {}
        self.node_mapping_inverse = {}

        # initialize lnd nodes
        self.running = False
        self.lnd_nodes = {
            node_name: RegTestLND(
                name=node_name,
                node_properties=node_properties,
                nodedata_folder=self.nodedata_folder,
                binary_folder=binary_folder
            ) for node_name, node_properties in self.node_defintion.items()
        }
        self.master_node = self.lnd_nodes['A']

        # define paths for node and channel mappings
        self.node_mapping_path = os.path.join(
            self.nodedata_folder, 'node_mapping.pickle')
        self.channel_mapping_path = os.path.join(
            self.nodedata_folder, 'channel_mapping.pickle')

        # read in channel and node mappings
        if not from_scratch:
            self.read_mappings()

    def run_nocleanup(self):
        """
        Runs the daemons without cleaning up in the end. Use in conjunction
        with self.cleanup() (try-finally).
        """
        logger.info("Running regtest network (from scratch: %s).",
                    self.from_scratch)
        # start bitcoind
        self.bitcoind.start(self.from_scratch)

        if self.from_scratch:
            # fill the bitcoind wallet
            self.bitcoind.fill_addresses(10)
            self.bitcoind.mine_blocks(100)

        self.lnds_start()

        # delay to get all started up
        time.sleep(WAIT_AFTER_ALL_LND_STARTED)
        self.determine_node_mapping()

        # channel-related things
        if self.from_scratch:
            self.lnds_fill_wallets()
            time.sleep(WAIT_AFTER_FILLING_WALLETS)
            self.lnds_connect_open_channels()
            # finalize last channel
            self.bitcoind.mine_blocks(3)
            time.sleep(3)
        self.determine_channel_mapping()

        if self.from_scratch:
            self.save_mappings()
        else:
            self.read_mappings()

        # set fees on a per node basis
        # self.lnds_set_fees()

        self.lnds_print_info()

        # in the future it could be necessary to trigger updates
        # such that all the graph data is propagated through the network
        # self.master_node_connect_other_nodes()
        self.master_node_disconnect_connect()
        self.master_node_print_networkinfo()
        self.master_node_graph_view()

        self.running = True

        logger.info("\nLocal lightning network is running. Have fun!")
        self.print_lncli_commands()

    def run_once(self):
        """
        Run the network and terminate after.
        """
        try:
            self.run_nocleanup()
        finally:
            self.cleanup()

    def run_continuously(self):
        """
        Run the network continuously.
        """
        try:
            self.run_nocleanup()
            while True:
                time.sleep(2)
        except KeyboardInterrupt:
            logger.info("Will shut down lnregtest.")
        finally:
            self.cleanup()

    def run_from_background(self):
        self.read_mappings()

    def cleanup(self):
        time.sleep(WAIT_BEFORE_CLEANUP)
        self.stop_components()
        try:
            self.temp_dir.cleanup()
        except AttributeError:
            logger.debug(
                "Will keep network data, as nodedata_folder is given.")

    def stop_components(self):
        self.lnds_stop()
        self.bitcoind.stop()

    def get_reduced_network_definition(self, network_definition):
        """
        Checks, if network definition was correct and extracts relevant
        information.

        The network size is reduced depending on self.node_limit.
        :param network_definition: dict
        :return: dict
        """
        # TODO: include check for network definition
        # TODO: check if channel numbers are running numbers and not duplicate
        network = {}
        for node_name, node_instance in network_definition.nodes.items():
            node = {}
            if node_name <= self.node_limit:
                node['grpc_port'] = node_instance['grpc_port']
                node['rest_port'] = node_instance['rest_port']
                node['port'] = node_instance['port']
                node['base_fee_msat'] = node_instance['base_fee_msat']
                node['fee_rate'] = node_instance['fee_rate']
                channels = {}
                for channel, channel_properties in \
                        node_instance['channels'].items():
                    if channel_properties['to'] <= self.node_limit:
                        channels[channel] = channel_properties
                node['channels'] = channels
                network[node_name] = node
        return network

    def print_lncli_commands(self):
        """
        Prints commands for controlling the lightning nodes directly from
        the shell.
        """
        logger.info('lncli commands:')
        for node_name, node_instance in self.lnd_nodes.items():
            node_instance.print_lncli_command()

    def lnds_start(self):
        """
        Starts all LN nodes.
        """
        for node_name, node_instance in self.lnd_nodes.items():
            node_instance.start(from_scratch=self.from_scratch)

    def lnds_stop(self):
        """
        Stops all LN nodes.
        """
        for node_name, node_instance in self.lnd_nodes.items():
            try:
                node_instance.stop()
            except Exception as e:
                print(e)

    def lnds_set_pubkeys(self):
        """
        Tells the LN nodes to set their node pub keys.

        This can only be done after the RPC is up.
        """
        for node_name, node_instance in self.lnd_nodes.items():
            node_instance.set_node_pubkey()

    def lnds_get_addresses(self):
        """
        Generates addresses in LN nodes' wallets.

        :return: list of str
            List of addresses.
        """
        addresses = []
        for node_name, node_instance in self.lnd_nodes.items():
            info = node_instance.getaddress()
            logger.info("%s: %s", node_name, info)
            addresses.append(info['address'])
        return addresses

    def lnds_connect_open_channels(self):
        """
        Connects LN nodes and opens channels between them.
        """
        for node_name, node_instance in self.lnd_nodes.items():
            for channel, channel_data in \
                    self.node_defintion[node_name]['channels'].items():

                # connect nodes
                node_to_connect = channel_data['to']
                if node_to_connect > self.node_limit:
                    continue
                node_pubkey = self.lnd_nodes[node_to_connect].pubkey
                node_port = self.lnd_nodes[node_to_connect].lndport
                node_host = 'localhost:{}'.format(node_port)
                node_instance.connect(node_pubkey, node_host)

                # open channel
                capacity = int(channel_data['capacity'])
                total_relative = (channel_data['ratio_local'] +
                                  channel_data['ratio_remote'])
                local_relative = \
                    float(channel_data['ratio_local']) / total_relative
                remote_relative = \
                    float(channel_data['ratio_remote']) / \
                    total_relative
                remote_sat = int(capacity * remote_relative)
                info = node_instance.openchannel(
                    node_pubkey, capacity, remote_sat)

                # save funding txid, to later on get a channel mapping
                self.channel_mapping[channel]['funding_txid'] = \
                    info['funding_txid']

            # finalize channel creation
            self.bitcoind.mine_blocks(3)
            time.sleep(WAIT_AFTER_MINING_THREE)

    def lnds_print_info(self):
        """
        Prints out essential information about the state of a node.
        """
        for node_name, node_instance in self.lnd_nodes.items():
            info = node_instance.getinfo()
            logger.info(
                "%s: synced: %s, active: %s, inactive: %s, pending: %s",
                node_name,
                info['synced_to_chain'],
                info['num_active_channels'],
                info['num_inactive_channels'],
                info['num_pending_channels']
            )

    def lnds_fill_wallets(self):
        """
        Funds LN nodes' wallets.
        """
        addresses = self.lnds_get_addresses()
        self.bitcoind.sendtoaddresses(addresses, amount=1)
        self.bitcoind.mine_blocks(6)

    def lnds_set_fees(self):
        """
        Updates fees for each node.
        """
        for node_name, node_instance in self.lnd_nodes.items():
            node_definition = self.node_defintion[node_name]
            logger.info("%s: Update node policy.", node_name)
            node_instance.updatechanpolicy(
                node_definition['base_fee_msat'],
                node_definition['fee_rate'],
            )
        # TODO: set fees on a per channel basis

    def determine_node_mapping(self):
        """
        Updates the node mappings.

        Sets the mappings (dicts)
            self.node_mapping: node name (e.g. 'A') -> node pub key
            self.node_mapping_inverse: node pub key -> node name (e.g. 'A')
        """
        self.lnds_set_pubkeys()
        for node_name, node_instance in self.lnd_nodes.items():
            self.node_mapping[node_name] = node_instance.pubkey
        self.node_mapping_inverse = {
            p: n for n, p in self.node_mapping.items()}

    def determine_channel_mapping(self):
        """
        Updates the channel mappings.

        Sets the mappings (dicts)
            self.channel_mapping: channel number (e.g. 3) ->
                channel_id, funding_txid, channel_point
            self.channel_mapping_inverse: channel_id ->
                channel number (e.g. 3)
        """
        # create a temporary mapping from funding tx to the channel number
        map_ftx_to_cn = {
            f['funding_txid']: i for i, f in self.channel_mapping.items()}

        for node_name, node_instance in self.lnd_nodes.items():
            channel_info = node_instance.listchannels()
            for channel in (channel_info['channels']):
                # map channel id to funding transaction
                funding_txid, channel_point = \
                    channel['channel_point'].split(':')
                channel_number = map_ftx_to_cn[funding_txid]
                self.channel_mapping[channel_number]['channel_id'] = \
                    int(channel['chan_id'])
                self.channel_mapping[channel_number]['channel_point'] = \
                    int(channel_point)

        # also set the inverse mapping
        self.channel_mapping_inverse = {
            p['channel_id']: n for n, p in self.channel_mapping.items()}

    def assemble_graph(self):
        """
        Gives a representation of the state of the LN network.

        The graph cannot be fetched from describegraph, as each node only has
        a local view of the network, therefore we need to ask each node
        about its open channels and gather the information.

        :return: dict
        """
        graph = {}

        for node_name, node_instance in self.lnd_nodes.items():
            edges = {}
            channel_info = node_instance.listchannels()
            for channel in (channel_info['channels']):
                edge = {
                    'remote_name':
                        self.node_mapping_inverse[channel['remote_pubkey']],
                    'capacity': int(channel['capacity']),
                    'local_balance': int(channel['local_balance']),
                    'remote_balance': int(channel['remote_balance']),
                    'num_updates': int(channel['num_updates']),
                    'initiator': bool(channel['initiator']),
                }
                # TODO: extend with more properties
                edges[self.channel_mapping_inverse[
                    int(channel['chan_id'])]] = edge
            graph[node_name] = edges

        # TODO: extend with feereport properties
        return graph

    def save_mappings(self):
        """
        Pickles the node and channel mappings.
        """
        with open(self.node_mapping_path, 'wb') as f:
            pickle.dump(self.node_mapping, f, protocol=pickle.HIGHEST_PROTOCOL)
        with open(self.channel_mapping_path, 'wb') as f:
            pickle.dump(
                self.channel_mapping, f, protocol=pickle.HIGHEST_PROTOCOL)

    def read_mappings(self):
        """
        Loads the node and channel mappings.
        """
        with open(self.node_mapping_path, 'rb') as f:
            self.node_mapping = pickle.load(f)
            self.node_mapping_inverse = {
                p: n for n, p in self.node_mapping.items()}
        with open(self.channel_mapping_path, 'rb') as f:
            self.channel_mapping = pickle.load(f)
            self.channel_mapping_inverse = {
                p['channel_id']: n for n, p in self.channel_mapping.items()}

    def master_node_disconnect_connect(self):
        """
        Disconnects and connects all peers from master LN node (node A).
        """
        peers = [channel_values['to'] for channel, channel_values in
                 self.node_defintion['A']['channels'].items()]
        print(peers)
        print(self.node_mapping)
        for p in peers:
            self.master_node.disconnect(self.node_mapping[p])
            node_pubkey = self.lnd_nodes[p].pubkey
            node_port = self.lnd_nodes[p].lndport
            node_host = 'localhost:{}'.format(node_port)
            self.master_node.connect(node_pubkey, node_host)

    def master_node_connect_other_nodes(self):
        """
        Connects the master node with all other peers in the network.
        """
        peers = self.node_defintion.keys()
        for p in peers:
            node_pubkey = self.lnd_nodes[p].pubkey
            node_port = self.lnd_nodes[p].lndport
            node_host = 'localhost:{}'.format(node_port)
            self.master_node.connect(node_pubkey, node_host)

    def master_node_graph_view(self):
        """
        Prints the graph view of the master node.
        """
        graph = self.master_node.describegraph()
        # extract relevant information from graph and map pub keys and
        # channel ids to human readable identifiers
        channel_list = []
        for channel in graph['edges']:
            node_name_1 = self.node_mapping_inverse[channel['node1_pub']]
            node_name_2 = self.node_mapping_inverse[channel['node2_pub']]
            channel_number = self.channel_mapping_inverse[
                int(channel['channel_id'])]
            if node_name_1 < node_name_2:
                channel_list.append((node_name_1, node_name_2, channel_number))
            else:
                channel_list.append((node_name_2, node_name_1, channel_number))
        channel_list.sort(key=lambda x: (x[0], x[2]))

        logger.info("Graph view of master node:")
        for channel in channel_list:
            logger.info("{} -> {} (channel #{})".format(
                channel[0], channel[1], channel[2]))

    def master_node_stop_and_start(self):
        self.master_node.stop()
        self.master_node.start(from_scratch=False)

    def master_node_print_networkinfo(self):
        logger.info("Master node info:")
        logger.info(format_dict(self.master_node.getnetworkinfo()))


if __name__ == '__main__':
    import logging.config
    logging.config.dictConfig(logger_config)
    logger.level = logging.INFO

    testnet = RegtestNetwork(
        network_definition_location='star_ring', node_limit='I', from_scratch=True)

    try:
        testnet.run_nocleanup()
        logger.info(format_dict(testnet.channel_mapping))
        logger.info(format_dict(testnet.node_mapping))
        logger.info(format_dict(testnet.assemble_graph()))
    finally:
        testnet.stop_components()
