"""Tests for traffic transmission related operations."""

import ipaddress
import logging
import pytest
import threading
import time
from dn_common.globals import dn_services
from tests.suites.dnos_e2e.dnos_e2e_system_dataclasses import SnapshotPath, SnapshotPathOperation
from utils.jira_utils import JiraComponent
from dnos_e2e_utils.actions.traffic_actions import (
    ArpPingFloodDNOS, 
    ArpPingFloodSpirent
)
from dnos_e2e_utils.actions.traffic_generator_actions import SetTrafficGeneratorProtocolState
from dnos_e2e_utils.actions.interface_actions import ConfigIPInterface
from dnos_e2e_utils.actions.system_actions import SaveConfig, LoadOverrideConfig, DeleteFileDnosCli, RestartContainer
from dnos_e2e_utils.validations.system_validations import ValidateContainerDown, ValidateContainerRunning
from dnos_e2e_utils.consts import OTGProtocolNames, OTGProtocolStates, SetupTypes, ComponentName, GiValidationTimeConsts
from dnos_e2e_utils.consts import BGPStates
from dnos_e2e_utils.dnos_e2e_config import get_device_names_from_topology, DNOS_PACKAGE
from dn_common.globals.dn_services import ContainerType
from ..dnos_e2e_network_dataclasses import TopologyNetworkConfig, InterfaceConfig, RouterConfig, BGPRouterInfo, BGPRouterConfig, BGPNeighborInfo
from dnos_e2e_utils.actions.bgp_actions import ConfigBGPRouter, ConfigBGPNeighbors, DisableBGPNeighbors, ConfigBGPNetwork
from dnos_e2e_utils.validations.arp_validations import ValidateARPTableEntry
from dnos_e2e_utils.validations.interface_validations import ValidateInterfaceOperationalState, ValidateInterfaceCounters
from dnos_e2e_utils.validations.traffic_generator_validations import ValidateTrafficLoss, ValidateTrafficGeneratorCounters, TrafficGeneratorValidationTime
from dnos_e2e_utils.validations.bgp_validations import ValidateBGPNeighborsState, ValidateBGPNeighborsPrefixAccepted
from dnos_e2e_utils.validations.gi_validations import ValidateCLIConnection
from dnos_e2e_utils.actions import gi_actions
from dnos_e2e_utils.actions.actions_helpers import gi_actions_helpers
from ..dnos_e2e_network_dataclasses import CountersValidation
from ..dnos_e2e_base import DnosE2EBase
from ..dnos_e2e_test_config import (
    TestConfiguration,
    TestMode,
    ClusterRequirement
)
from ..dnos_e2e_test_decorators import single_test_config

logger = logging.getLogger(__name__)

class DNOSSettings:
    ROUTER_IPV4_PORT1 = ipaddress.ip_interface("2.1.1.1/24")  # DNOS port1 IP
    ROUTER_IPV4_PORT2 = ipaddress.ip_interface("2.1.2.1/24")  # DNOS port2 IP
    ROUTER_IPV6_PORT1 = ipaddress.ip_interface("3001::1/64")  # DNOS port1 IPv6
    ROUTER_IPV6_PORT2 = ipaddress.ip_interface("3002::1/64")  # DNOS port2 IPv6
    LOCAL_AS = 100 # DNOS AS number

class SpirentSettings:
    TEST_PORT1_NAME = "port1"
    TEST_PORT2_NAME = "port2"
    
    # Spirent device IP addresses (DNOS IP + 1)
    ROUTER_IPV4_PORT1 = ipaddress.ip_interface(str(DNOSSettings.ROUTER_IPV4_PORT1.ip + 1) + "/24")  # Device1 on port1, gateway 2.1.1.1
    ROUTER_IPV4_PORT2 = ipaddress.ip_interface(str(DNOSSettings.ROUTER_IPV4_PORT2.ip + 1) + "/24")  # Device2 on port2, gateway 2.1.2.1
    ROUTER_IPV4_PORT1_STATIC_ROUTE = ipaddress.ip_interface("10.10.10.10/32")
    ROUTER_IPV4_PORT2_STATIC_ROUTE = ipaddress.ip_interface("20.20.20.20/32")
    
    # Spirent device IPv6 addresses (DNOS IPv6 + 1)
    ROUTER_IPV6_PORT1 = ipaddress.ip_interface(str(DNOSSettings.ROUTER_IPV6_PORT1.ip + 1) + "/64")  # Device1 on port1 IPv6
    ROUTER_IPV6_PORT2 = ipaddress.ip_interface(str(DNOSSettings.ROUTER_IPV6_PORT2.ip + 1) + "/64")  # Device2 on port2 IPv6
    
    REMOTE_AS = 100 # Spirent AS number
    MAC_PORT1 = "02:00:00:16:02:00" # Spirent device MAC for port1
    MAC_PORT2 = "02:00:00:16:02:01" # Spirent device MAC for port2

class InterfaceSettings:
    FEC = "rs-fec-528-514"

class BGPProtocolSettings:
    BGP_PEER_NAME_PORT1 = "bgp_peer_port1"
    BGP_PEER_NAME_PORT2 = "bgp_peer_port2"

class TrafficSettings:
    EXPECTED_PACKETS_COUNT = 200
    PACKETS_TRANSMISSION_TIMEOUT = 90
    RE = dn_services.ContainerType.ROUTING_ENGINE.service
    BGP_ESTABLISH_TIMEOUT = 30
    CONTAINER_RESTART_TIMEOUT = 60
    TRAFFIC_LOSS_VALIDATION_TIMEOUT = 30
    NO_LOSS_VALIDATION_TIMEOUT = 60
    # Fixed wait after traffic is up before collecting baseline (no gap/growth logic)
    BASELINE_SETTLE_SEC = 15
    # Allow small margin in no-loss assertion to absorb timing/counter jitter (packets)
    NO_LOSS_MAX_ALLOWED = 500
    TRAFFIC_FLOW_DURATION = 5

BASE_SPIRENT_CONFIG_SINGLE_PORT = {
    "layer1": [
        {
            "port_names": [SpirentSettings.TEST_PORT1_NAME],
            "name": "l1",
            "speed": "speed_100_gbps",
            "media": "fiber",
            "link_training": True,
            "rs_fec": {
                "choice": "rs528",
            },
            "promiscuous": True,
            "mtu": 1500,
        }
    ],
    "devices": [
        {
            "name": "dev1",
            "ethernets": [
                {
                    "name": "eth1",
                    "mac": SpirentSettings.MAC_PORT1,
                    "mtu": 1500,
                    "connection": {
                        "choice": "port_name",
                        "port_name": SpirentSettings.TEST_PORT1_NAME,
                    },
                    "ipv4_addresses": [
                        {
                            "name": "ipv4",
                            "gateway": str(DNOSSettings.ROUTER_IPV4_PORT1.ip),
                            "address": str(SpirentSettings.ROUTER_IPV4_PORT1.ip),
                            "prefix": int(DNOSSettings.ROUTER_IPV4_PORT1.network.prefixlen),
                        }
                    ],
                    "ipv6_addresses": [
                        {
                            "name": "ipv6",
                            "gateway": str(DNOSSettings.ROUTER_IPV6_PORT1.ip),
                            "address": str(SpirentSettings.ROUTER_IPV6_PORT1.ip),
                            "prefix": int(DNOSSettings.ROUTER_IPV6_PORT1.network.prefixlen),
                        }
                    ],
                }
            ],
            "bgp": {
                "router_id": str(SpirentSettings.ROUTER_IPV4_PORT1.ip),
                "ipv4_interfaces": [
                    {
                        "ipv4_name": "ipv4",
                        "peers": [
                            {
                                "name": BGPProtocolSettings.BGP_PEER_NAME_PORT1,
                                "peer_address": str(DNOSSettings.ROUTER_IPV4_PORT1.ip), 
                                "as_type": "ibgp",
                                "as_number": DNOSSettings.LOCAL_AS,
                            }
                        ],
                    }
                ],
            },
        }
    ],
}

PING_FLOW =  {
    "flows": [
        {
            "name": "ping_flood",
            "tx_rx": {
                "choice": "port",
                "port": {
                    "tx_name": SpirentSettings.TEST_PORT1_NAME,
                    "rx_names": [SpirentSettings.TEST_PORT1_NAME],  # Same port (loopback)
                },
            },
            "duration": {"choice": "fixed_packets", "fixed_packets": {"packets": TrafficSettings.EXPECTED_PACKETS_COUNT}},
            "rate": {"choice": "pps", "pps": 1000},
            "size": {"choice": "fixed", "fixed": 64},
            "packet": [
                {
                    "choice": "ethernet",
                    "ethernet": {
                        "src": {"choice": "value", "value": SpirentSettings.MAC_PORT1},
                        "dst": {"choice": "value", "value": "00:00:00:00:00:00"},  # Will be resolved via ARP (device emulation handles ARP)
                    },
                },
                {
                    "choice": "ipv4",
                    "ipv4": {
                        "src": {"choice": "value", "value": str(SpirentSettings.ROUTER_IPV4_PORT1.ip)},
                        "dst": {"choice": "value", "value": str(DNOSSettings.ROUTER_IPV4_PORT1.ip)},
                    },
                },
                {
                    "choice": "icmp",
                    "icmp": {
                        "type": {"choice": "value", "value": 8},  # Type 8 = ICMP Echo Request (ping request)
                        "code": {"choice": "value", "value": 0},
                    },
                },
            ],
        }
    ],
}

BASE_SPIRENT_CONFIG_DUAL_PORT = {
    "layer1": [
        {
            "port_names": [SpirentSettings.TEST_PORT1_NAME, SpirentSettings.TEST_PORT2_NAME],
            "name": "l1",
            "speed": "speed_100_gbps",
            "media": "fiber",
            "link_training": True,
            "rs_fec": {
                "choice": "rs528",
            },
            "promiscuous": True,
            "mtu": 1500,
        }
    ],
    "devices": [
        {
            "name": "dev1",
            "ethernets": [
                {
                    "name": "eth1",
                    "mac": SpirentSettings.MAC_PORT1,
                    "mtu": 1500,
                    "connection": {
                        "choice": "port_name",
                        "port_name": SpirentSettings.TEST_PORT1_NAME,
                    },
                    "ipv4_addresses": [
                        {
                            "name": "ipv4",
                            "gateway": str(DNOSSettings.ROUTER_IPV4_PORT1.ip),
                            "address": str(SpirentSettings.ROUTER_IPV4_PORT1.ip),
                            "prefix": int(DNOSSettings.ROUTER_IPV4_PORT1.network.prefixlen),
                        }
                    ],
                    "ipv6_addresses": [
                        {
                            "name": "ipv6",
                            "gateway": str(DNOSSettings.ROUTER_IPV6_PORT1.ip),
                            "address": str(SpirentSettings.ROUTER_IPV6_PORT1.ip),
                            "prefix": int(DNOSSettings.ROUTER_IPV6_PORT1.network.prefixlen),
                        }
                    ],
                }
            ],
            "bgp": {
                "router_id": str(SpirentSettings.ROUTER_IPV4_PORT1.ip),
                "as_number": SpirentSettings.REMOTE_AS,  # Local AS for this Spirent device
                "ipv4_interfaces": [
                    {
                        "ipv4_name": "ipv4",
                        "peers": [
                            {
                                "name": BGPProtocolSettings.BGP_PEER_NAME_PORT1,
                                "peer_address": str(DNOSSettings.ROUTER_IPV4_PORT1.ip),
                                "as_type": "ibgp",
                                "as_number": DNOSSettings.LOCAL_AS,  # Peer's AS number (DNOS)
                                "capability": {
                                    "ipv4_unicast": True
                                },
                                "v4_routes": [
                                    {
                                        "name": "route_port1",
                                        "addresses":[
                                            {
                                                "address": str(SpirentSettings.ROUTER_IPV4_PORT1_STATIC_ROUTE.ip),
                                                "prefix": int(SpirentSettings.ROUTER_IPV4_PORT1_STATIC_ROUTE.network.prefixlen),
                                                "count": 1,
                                                "step": 1
                                            }
                                        ],
                                        "next_hop_mode": "manual",
                                        "next_hop_address_type": "ipv4",
                                        "next_hop_ipv4_address": str(SpirentSettings.ROUTER_IPV4_PORT1.ip),
                                        "advanced": {
                                            "origin": "igp",
                                            "include_local_preference": True,
                                            "local_preference": 123,
                                        },
                                    }
                                ]
                            }
                        ],
                    }
                ],
            },
        },
        {
            "name": "dev2",
            "ethernets": [
                {
                    "name": "eth2",
                    "mac": SpirentSettings.MAC_PORT2,
                    "mtu": 1500,
                    "connection": {
                        "choice": "port_name",
                        "port_name": SpirentSettings.TEST_PORT2_NAME,
                    },
                    "ipv4_addresses": [
                        {
                            "name": "ipv4",
                            "gateway": str(DNOSSettings.ROUTER_IPV4_PORT2.ip),
                            "address": str(SpirentSettings.ROUTER_IPV4_PORT2.ip),
                            "prefix": int(DNOSSettings.ROUTER_IPV4_PORT2.network.prefixlen),
                        }
                    ],
                    "ipv6_addresses": [
                        {
                            "name": "ipv6",
                            "gateway": str(DNOSSettings.ROUTER_IPV6_PORT2.ip),
                            "address": str(SpirentSettings.ROUTER_IPV6_PORT2.ip),
                            "prefix": int(DNOSSettings.ROUTER_IPV6_PORT2.network.prefixlen),
                        }
                    ],
                }
            ],
            "bgp": {
                "router_id": str(SpirentSettings.ROUTER_IPV4_PORT2.ip),
                "as_number": SpirentSettings.REMOTE_AS,  # Local AS for this Spirent device
                "ipv4_interfaces": [
                    {
                        "ipv4_name": "ipv4",
                        "peers": [
                            {
                                "name": BGPProtocolSettings.BGP_PEER_NAME_PORT2,
                                "peer_address": str(DNOSSettings.ROUTER_IPV4_PORT2.ip),
                                "as_type": "ibgp",
                                "as_number": DNOSSettings.LOCAL_AS,  # Peer's AS number (DNOS)
                                "capability": {
                                    "ipv4_unicast": True
                                },
                                "v4_routes": [
                                    {
                                        "name": "route_port2",
                                        "addresses":[
                                            {
                                                "address": str(SpirentSettings.ROUTER_IPV4_PORT2_STATIC_ROUTE.ip),
                                                "prefix": int(SpirentSettings.ROUTER_IPV4_PORT2_STATIC_ROUTE.network.prefixlen),
                                                "count": 1,
                                                "step": 1
                                            }
                                        ],
                                        "next_hop_mode": "manual",
                                        "next_hop_address_type": "ipv4",
                                        "next_hop_ipv4_address": str(SpirentSettings.ROUTER_IPV4_PORT2.ip),
                                        "advanced": {
                                            "origin": "igp",
                                            "include_local_preference": True,
                                            "local_preference": 123,
                                        },
                                    }
                                ]
                            }
                        ],
                    }
                ],
            },
        }
    ],
}

CONTINUOUS_TRAFFIC_FLOW =  {
    "flows": [
        {
            "name": "traffic_loss_test_flow",
            "tx_rx": {
                "choice": "port",
                "port": {
                    "tx_name": SpirentSettings.TEST_PORT1_NAME,
                    "rx_names": [SpirentSettings.TEST_PORT2_NAME],
                },
            },
            "duration": {"choice": "continuous"},
            "rate": {"choice": "pps", "pps": 1000},
            "size": {"choice": "fixed", "fixed": 128},
            "packet": [
                {
                    "choice": "ethernet",
                    "ethernet": {
                        "src": {"choice": "value", "value": SpirentSettings.MAC_PORT1},
                        "dst": {"choice": "value", "value": "00:00:00:00:00:00"},
                    },
                },
                {
                    "choice": "ipv4",
                    "ipv4": {
                        "src": {"choice": "value", "value": str(SpirentSettings.ROUTER_IPV4_PORT1_STATIC_ROUTE.ip)},
                        "dst": {"choice": "value", "value": str(SpirentSettings.ROUTER_IPV4_PORT2_STATIC_ROUTE.ip)},
                    },
                },
                {
                    "choice": "udp",
                    "udp": {
                        "src_port": {"choice": "value", "value": 5000},
                        "dst_port": {"choice": "value", "value": 5000},
                    },
                },
            ],
        },
    ],
}

@pytest.mark.dnos_e2e_sa_spirent
class TestTraffic(DnosE2EBase):
    """Test suite for traffic actions."""

    # Set requires_traffic_generator early so framework can skip before set_default_test_config runs
    default_test_config = TestConfiguration(
        requires_traffic_generator=True
    )

    @pytest.fixture(scope='class', autouse=True)
    def set_topology_network_config(self, request, setup_dnos_e2e_base):
        """
        Set up the network configuration with IP interfaces and BGP on both ports.
        """
        cls = request.cls
        cls.SA1 = BGPRouterInfo(local_as=DNOSSettings.LOCAL_AS, router_id=str(DNOSSettings.ROUTER_IPV4_PORT1.ip))
        cls.SPIRENT_DEV1 = BGPRouterInfo(local_as=SpirentSettings.REMOTE_AS, router_id=str(SpirentSettings.ROUTER_IPV4_PORT1.ip))
        cls.SPIRENT_DEV2 = BGPRouterInfo(local_as=SpirentSettings.REMOTE_AS, router_id=str(SpirentSettings.ROUTER_IPV4_PORT2.ip))

        # Get the first SA from the nodes.json file
        DEVICE_NAMES = get_device_names_from_topology() 
        cls.SA1_DEVICE_NAME = DEVICE_NAMES.get("sas", [""])[0]

        cls.dnos_port_name = cls._topology_handler.links.get_links("traffic_generator", cls._dnos_e2e_sa.setup_name)[SpirentSettings.TEST_PORT1_NAME]
        cls.dnos_port2_name = cls._topology_handler.links.get_links("traffic_generator", cls._dnos_e2e_sa.setup_name)[SpirentSettings.TEST_PORT2_NAME]

        cls.TOPOLOGY_NETWORK_CONFIG = TopologyNetworkConfig(
            routers={
                cls.SA1_DEVICE_NAME: RouterConfig(
                    router_name=cls.SA1_DEVICE_NAME,
                    interfaces=[
                        InterfaceConfig(
                            interface_name=cls.dnos_port_name,
                            ipv4_address=str(DNOSSettings.ROUTER_IPV4_PORT1),
                            ipv6_address=str(DNOSSettings.ROUTER_IPV6_PORT1),
                            fec=InterfaceSettings.FEC,
                            speed=100
                            ),
                        InterfaceConfig(
                            interface_name=cls.dnos_port2_name,
                            ipv4_address=str(DNOSSettings.ROUTER_IPV4_PORT2),
                            ipv6_address=str(DNOSSettings.ROUTER_IPV6_PORT2),
                            fec=InterfaceSettings.FEC,
                            speed=100
                            ),
                    ],
                    bgp_config=BGPRouterConfig(
                        router_info=cls.SA1,  # Use the SA1 object as router_info
                        neighbor_info=[
                            BGPNeighborInfo(
                                neighbor_ip=cls.SPIRENT_DEV1.router_id,
                                source_interface=cls.dnos_port_name,
                                remote_as=cls.SPIRENT_DEV1.local_as,
                                address_family="ipv4-unicast",
                                route_reflector_client=True,
                                nexthop_self_force=True
                            ),
                            # Port2 BGP neighbor will be added dynamically in tests that require it
                            BGPNeighborInfo(
                                neighbor_ip=cls.SPIRENT_DEV2.router_id,
                                source_interface=cls.dnos_port2_name,
                                remote_as=cls.SPIRENT_DEV2.local_as,
                                address_family="ipv4-unicast",
                                route_reflector_client=True,
                                nexthop_self_force=True
                            ),
                        ]
                    )
                )
            }
        )

    # configure test requirements since we need the traffic generator, topology handler and sa attributes
    @classmethod
    def create_default_test_config(cls) -> TestConfiguration:
        return TestConfiguration(
            test_mode=TestMode.DNOS_MODE,
            cluster_requirement=ClusterRequirement.SA_ONLY,
            requires_traffic_generator=True,
        )

    @pytest.fixture(scope='class', autouse=True)
    def set_default_test_config(self, request, set_topology_network_config, setup_dnos_e2e_base):
        """
        Set up the default test configuration for the Traffic test suite.
        Merges into the class-level default_test_config that already has requires_traffic_generator=True.
        """
        cls = request.cls
        
        # Update topology config with actual port names and source interfaces
        sa_device_config = cls.TOPOLOGY_NETWORK_CONFIG.get_router(cls.SA1_DEVICE_NAME)
        dnos_interface_config = sa_device_config.interfaces[0]
        
        dnos_interface2_config = sa_device_config.interfaces[1]

        config_interface_action = ConfigIPInterface(
            cluster_handler=cls._dnos_e2e_sa,
            interface_config=dnos_interface_config
        )
        config_interface_action.add_post_action_validations(
            [ValidateInterfaceOperationalState(
                cluster_handler=cls._dnos_e2e_sa, 
                interface=cls.dnos_port_name, 
                operational_state="up")])

        config_interface2_action = ConfigIPInterface(
            cluster_handler=cls._dnos_e2e_sa,
            interface_config=dnos_interface2_config
        )
        config_interface2_action.add_post_action_validations(
            [ValidateInterfaceOperationalState(
                cluster_handler=cls._dnos_e2e_sa, 
                interface=cls.dnos_port2_name, 
                operational_state="up")])

        dev_1_arp_validation = ValidateARPTableEntry(
                cluster_handler=cls._dnos_e2e_sa, 
                expected_entry_ip=str(SpirentSettings.ROUTER_IPV4_PORT1.ip), 
                expected_state="reachable")

        # Port2 ARP validation is only added in tests that use port2 (e.g., test_traffic_loss_on_routing_engine_restart)
        # since BASE_SPIRENT_CONFIG_SINGLE_PORT only includes dev1 (port1)

        set_traffic_generator_protocol_state_action = SetTrafficGeneratorProtocolState(
            traffic_generator=cls._topology_handler.traffic_generator,
            state=OTGProtocolStates.START,
        )

        # Configure the BGP router using router_info object
        bgp_router_config_action = ConfigBGPRouter(
            cluster_handler=cls._dnos_e2e_sa,
            router_info=sa_device_config.bgp_config.router_info
        )

        # Configure BGP neighbor using neighbor_info objects
        bgp_neighbor_config_action = ConfigBGPNeighbors(
            cluster_handler=cls._dnos_e2e_sa,
            local_as=DNOSSettings.LOCAL_AS,
            neighbor_info=sa_device_config.bgp_config.neighbor_info
        )

        # Enable BGP on the traffic generator
        enable_bgp_on_traffic_generator = SetTrafficGeneratorProtocolState(
            traffic_generator=cls._topology_handler.traffic_generator,
            state=OTGProtocolStates.UP,
            protocol_config={OTGProtocolNames.BGP: [BGPProtocolSettings.BGP_PEER_NAME_PORT1]}
        )

        # Use ConfigBGPNetwork to execute all actions with proper validation
        # Only validate port1 neighbor since port2 is not enabled in default Spirent config
        bgp_network_action = ConfigBGPNetwork(
            bgp_config_actions=[
                bgp_router_config_action,
                bgp_neighbor_config_action,
                enable_bgp_on_traffic_generator
            ],
            expected_neighbors_info={
            cls._dnos_e2e_sa: [sa_device_config.bgp_config.neighbor_info[0]]  # Only port1 neighbor
            },
            expected_state=BGPStates.ESTABLISHED
        )
        
        # Disable BGP neighbor on cleanup (both neighbors configured, but only port1 is active)
        bgp_disable_action = DisableBGPNeighbors(
            cluster_handler=cls._dnos_e2e_sa,
            local_as=DNOSSettings.LOCAL_AS,
            neighbor_info=sa_device_config.bgp_config.neighbor_info
        )
        
        # Save config at suite start and restore at suite end
        config_filename = "baseline_config"
        save_config_action = SaveConfig(
            cluster_handler=cls._dnos_e2e_sa,
            filename=config_filename
        )
        load_config_action = LoadOverrideConfig(
            cluster_handler=cls._dnos_e2e_sa,
            filename=config_filename
        )
        delete_config_file_action = DeleteFileDnosCli(
            cluster_handler=cls._dnos_e2e_sa,
            filename=config_filename,
            file_type="config"
        )
        
        additional_config = cls.create_default_test_config().merge_with(
            TestConfiguration(
                additional_suite_setup=[save_config_action, config_interface_action, config_interface2_action],
                additional_suite_cleanup=[load_config_action, delete_config_file_action],
                additional_pre_test_config=[set_traffic_generator_protocol_state_action, bgp_network_action],
                additional_post_test_cleanup=[bgp_disable_action],
                additional_pre_validations=[dev_1_arp_validation],
            )
        )
        
        cls.default_test_config = cls.default_test_config.merge_with(additional_config)


    # Note: Port counters may not increment if packets are consumed by device emulation
    # This is a Spirent limitation - packets destined to device IP are consumed and don't increment port counters
    @single_test_config(
        traffic_generator_config=BASE_SPIRENT_CONFIG_SINGLE_PORT
    )
    @pytest.mark.owner(user="lberman", component=JiraComponent.ROUTING)
    def test_ping_flood_dnos_to_spirent(self):
        """
        Test ping flooding from DNOS to Spirent.
        
        This test sends a flood of ping packets from the routing-engine container
        to the Spirent device IP address (2.1.1.2) and validates that packets are transmitted.
        """
    
        # Execute ping flood action
        logger.info("Executing ping flood from DNOS to Spirent")
        ping_flood_action = ArpPingFloodDNOS(
            cluster_handler=self.dnos_e2e_sa,
            container_name=TrafficSettings.RE,
            destination_ip=str(SpirentSettings.ROUTER_IPV4_PORT1.ip),
            transmitter_validations=[
                CountersValidation(
                    name=self.dnos_port_name,
                    counter_field="TX frames",
                    operator=">=",
                    expected_value=TrafficSettings.EXPECTED_PACKETS_COUNT,
                    timeout=TrafficSettings.PACKETS_TRANSMISSION_TIMEOUT
                )
            ]
        )
        ping_flood_action.execute()
        logger.info("Ping flood to Spirent completed successfully")

    @single_test_config(
        traffic_generator_config=BASE_SPIRENT_CONFIG_SINGLE_PORT
    )
    @pytest.mark.owner(user="lberman", component=JiraComponent.ROUTING)
    def test_arp_ping_flood_dnos_to_spirent(self):
        """
        Test ARP flooding from DNOS to Spirent.
        
        This test sets a static ARP entry and then sends a flood of ping packets
        from DNOS to Spirent, creating an ARP flood scenario.
        """
        logger.info("Executing arp-ping flood from DNOS to Spirent")
        # Execute ARP flood action (with static ARP entry)
        arp_flood_action = ArpPingFloodDNOS(
            cluster_handler=self.dnos_e2e_sa,
            container_name=TrafficSettings.RE,
            destination_ip=str(SpirentSettings.ROUTER_IPV4_PORT1.ip),
            arp_mac=SpirentSettings.MAC_PORT1,
            transmitter_validations=[
                CountersValidation(
                    name=self.dnos_port_name,
                    counter_field="TX frames",
                    operator=">=",
                    expected_value=TrafficSettings.EXPECTED_PACKETS_COUNT,
                    timeout=TrafficSettings.PACKETS_TRANSMISSION_TIMEOUT
                )
            ]
        )
        arp_flood_action.execute()

        logger.info("ARP flood to Spirent completed successfully")

    @single_test_config(
        traffic_generator_config={
            **BASE_SPIRENT_CONFIG_SINGLE_PORT,
            **PING_FLOW,
        }
    )

    @pytest.mark.owner(user="lberman", component=JiraComponent.ROUTING)
    def test_ping_flood_spirent_to_dnos(self):
        """
        Test ping flooding from Spirent to DNOS.
        
        This test sends a flood of ICMP echo requests (ping) from the Spirent traffic generator
        to the DNOS device IP address (2.1.1.1) using normal ARP resolution (no static MAC).
        Device emulation handles ARP to resolve DNOS MAC address.
        """

        logger.info("Executing ping flood from Spirent to DNOS")

        # Execute ping flood action from Spirent to DNOS
        # Uses flow "ping_flood" configured in traffic_generator_config
        # Does NOT set destination_mac - lets device emulation handle ARP resolution (like DNOS test_ping_flood)
        # Action will automatically detect same-port traffic and use port counters (Spirent limitation)
        ping_flood_action = ArpPingFloodSpirent(
            traffic_generator=self.dnos_e2e_env.traffic_generator,
            flow_name="ping_flood",
            transmitter_validations=[
                CountersValidation(
                    name=SpirentSettings.TEST_PORT1_NAME,
                    counter_field="frames_tx",
                    operator=">=",
                    expected_value=TrafficSettings.EXPECTED_PACKETS_COUNT,
                    timeout=TrafficSettings.PACKETS_TRANSMISSION_TIMEOUT
                )
            ],
            receiver_validations=[
                ValidateInterfaceCounters(
                    cluster_handler=self.dnos_e2e_sa,
                    counters_data=[
                        CountersValidation(
                            name=self.dnos_port_name,
                            counter_field="RX frames",
                            operator=">=",
                            expected_value=TrafficSettings.EXPECTED_PACKETS_COUNT,
                            timeout=TrafficSettings.PACKETS_TRANSMISSION_TIMEOUT
                        )
                    ]
                )
            ]
            # No destination_mac - device emulation will handle ARP resolution
        )
        ping_flood_action.execute()

        logger.info("Ping flood from Spirent to DNOS completed successfully")

    @single_test_config(
        traffic_generator_config={
            **BASE_SPIRENT_CONFIG_SINGLE_PORT,
            **PING_FLOW,
        }
    )
    @pytest.mark.owner(user="lberman", component=JiraComponent.ROUTING)
    def test_arp_ping_flood_spirent_to_dnos(self):
        """
        Test ping flooding from Spirent to DNOS.
        
        Device dev1 is configured with MAC/IP on port1.
        Flow ping_flood sends from dev1.eth1.ipv4.
        Device emulation handles ARP to resolve DNOS MAC.
        Packets are sent: Ethernet → IPv4 → ICMP Echo Request.
        Packets go: Spirent port1 → DNOS.
        Action validates that Spirent transmitted 200 packets (TX counters).
        """

        # Get DNOS interface MAC address for destination MAC in ping flow
        dnos_mac = self.dnos_sa_active_ncc_cli.show.show_interfaces_interface_name(self.dnos_port_name)[
            "parsed_content"
        ]["MAC Address"].split()[0]

        logger.info("Executing arp-ping flood from Spirent to DNOS")

        # Execute ping flood action from Spirent to DNOS
        # Uses flow "ping_flood" configured in traffic_generator_config
        # Action will automatically detect same-port traffic and use port counters (Spirent limitation)
        ping_flood_action = ArpPingFloodSpirent(
            traffic_generator=self.dnos_e2e_env.traffic_generator,
            flow_name="ping_flood",
            destination_mac=dnos_mac,
            transmitter_validations=[
                CountersValidation(
                    name=SpirentSettings.TEST_PORT1_NAME,
                    counter_field="frames_tx",
                    operator=">=",
                    expected_value=TrafficSettings.EXPECTED_PACKETS_COUNT,
                    timeout=TrafficSettings.PACKETS_TRANSMISSION_TIMEOUT
                )
            ],
            receiver_validations=[
                ValidateInterfaceCounters(
                    cluster_handler=self.dnos_e2e_sa,
                    counters_data=[
                        CountersValidation(
                            name=self.dnos_port_name,
                            counter_field="RX frames",
                            operator=">=",
                            expected_value=TrafficSettings.EXPECTED_PACKETS_COUNT,
                            timeout=TrafficSettings.PACKETS_TRANSMISSION_TIMEOUT
                        )
                    ]
                )
            ],
        )
        ping_flood_action.execute()

        logger.info("Ping flood from Spirent to DNOS completed successfully")

    def _setup_traffic_flow_for_l3_routing(self, port_name):
        """Setup traffic flow destination MAC to DNOS interface MAC for L3 routing."""
        logger.info(f"Getting DNOS interface MAC address for {port_name}")
        dnos_mac = self.dnos_sa_active_ncc_cli.show.show_interfaces_interface_name(port_name)[
            "parsed_content"
        ]["MAC Address"].split()[0]
        logger.info(f"DNOS {port_name} MAC: {dnos_mac}")
        
        logger.info("Updating flow destination MAC to DNOS interface MAC for L3 routing")
        with self.dnos_e2e_env.traffic_generator.otg_api.modify_configuration() as config:
            config["flows"][0]["packet"][0]["ethernet"]["dst"]["value"] = dnos_mac
        logger.info(f"Flow destination MAC updated to {dnos_mac}")

    def _setup_bgp_peers_and_validate(self):
        """Setup BGP peers and validate they are established with routes."""
        # Clear BGP AS paths for proper route advertisement
        logger.info("Clearing BGP AS paths")
        self.dnos_e2e_env.traffic_generator.otg_api.stc_api.clear_bgp_as_paths()
        
        # Validate ARP for port2 device (dev2) - this is needed since port2 is only up in this test's Spirent config
        logger.info("Validating ARP table entry for port2 device")
        ValidateARPTableEntry(
            cluster_handler=self.dnos_e2e_sa,
            expected_entry_ip=str(SpirentSettings.ROUTER_IPV4_PORT2.ip),
            expected_state="reachable"
        ).execute()
        
        # Enable BGP on both port1 and port2 peers
        logger.info("Enabling BGP on both port1 and port2 peers")
        SetTrafficGeneratorProtocolState(
            traffic_generator=self.dnos_e2e_env.traffic_generator,
            state=OTGProtocolStates.UP,
            protocol_config={OTGProtocolNames.BGP: [BGPProtocolSettings.BGP_PEER_NAME_PORT1, BGPProtocolSettings.BGP_PEER_NAME_PORT2]}
        ).execute()
        
        # Validate that both BGP neighbors are established
        logger.info("Validating both BGP neighbors are established")
        sa_router_config = self.TOPOLOGY_NETWORK_CONFIG.get_router(self.SA1_DEVICE_NAME)
        ValidateBGPNeighborsState(
            expected_neighbors_info={
                self.dnos_e2e_sa: sa_router_config.bgp_config.neighbor_info
            },
            expected_state=BGPStates.ESTABLISHED,
            timeout=TrafficSettings.BGP_ESTABLISH_TIMEOUT
        ).execute()
        
        # Validate that both BGP neighbors have accepted 1 prefix
        logger.info("Validating both BGP neighbors advertised routes")
        neighbor_ips = [neighbor.neighbor_ip for neighbor in sa_router_config.bgp_config.neighbor_info]
        ValidateBGPNeighborsPrefixAccepted(
            cluster_handler=self.dnos_e2e_sa,
            neighbor_ips=neighbor_ips,
            expected_pfx_accepted=1,
        ).execute()

        return sa_router_config, neighbor_ips

    def _start_traffic_flow(self, flow_name, stabilization_seconds=5):
        """Start traffic flow and let it stabilize."""
        logger.info(f"Starting traffic flow '{flow_name}'")
        self.dnos_e2e_env.traffic_generator.otg_api.set_flow_state(
            flow_names=[flow_name],
            state="start"
        )
        if stabilization_seconds > 0:
            logger.info(f"Allowing traffic to stabilize for {stabilization_seconds} seconds")
            time.sleep(stabilization_seconds)

    def _stop_traffic_flow(self, flow_name):
        """Stop traffic flow."""
        logger.info(f"Stopping traffic flow '{flow_name}'")
        self.dnos_e2e_env.traffic_generator.otg_api.set_flow_state(
            flow_names=[flow_name],
            state="stop"
        )

    def _wait_for_cli_and_bgp_reestablish(self, sa_router_config):
        """Wait for CLI connection and BGP neighbors to re-establish after container restart"""
        logger.info("Waiting for CLI connection")
        # Reconnect CLI - connection is already lost after container restart
        # ValidateCLIConnection calls connect_clis() which will overwrite any stale connection
        ValidateCLIConnection(
            cluster_handler=self.dnos_e2e_sa,
            timeout=GiValidationTimeConsts.VALIDATE_CLI_CONNECTION_RESTART_TIMEOUT
        ).execute()

        logger.info("Waiting for BGP neighbors to re-establish")
        ValidateBGPNeighborsState(
            expected_neighbors_info={self.dnos_e2e_sa: sa_router_config.bgp_config.neighbor_info},
            expected_state=BGPStates.ESTABLISHED,
            timeout=TrafficSettings.BGP_ESTABLISH_TIMEOUT
        ).execute()

    def _create_traffic_loss_validation(self, flow_name, expected_loss=0, operator="==", timeout=60, negative_validation=False):
        """Create traffic loss validation object."""
        return ValidateTrafficLoss(
            traffic_generator=self.dnos_e2e_env.traffic_generator,
            flow_name=flow_name,
            operator=operator,
            expected_loss=expected_loss,
            timeout=timeout,
            negative_validation=negative_validation
        )

    @pytest.mark.owner(user="lberman", component=JiraComponent.ROUTING)
    @single_test_config(
        traffic_generator_config={
            **BASE_SPIRENT_CONFIG_DUAL_PORT,
            **CONTINUOUS_TRAFFIC_FLOW,
        },
        system_snapshot_expected_changes={
            SnapshotPath.container_restart("ncc", 0, "routing_engine"): SnapshotPathOperation.INCREASE_BY(1),
        }
    )
    def test_traffic_loss_on_routing_engine_restart(self):
        """
        Test traffic loss during routing-engine container restart.

        Flow:
        1. Update flow destination MAC to DNOS interface MAC (for L3 routing)
        2. Enable BGP on both port1 and port2 peers
        3. Start the traffic flow from Spirent port1 to port2 (through DNOS)
        4. Restart routing-engine container on DNOS (with traffic loss validation)
        5. Stop the traffic flow

        Note: This test is generic and can be copied to test traffic loss for other actions
        (e.g., patch, upgrade) by changing the action in step 3.
        """
        flow_name = "traffic_loss_test_flow"

        # Get DNOS port names
        links = self.dnos_e2e_env.links.get_links("traffic_generator", self.dnos_e2e_sa.setup_name)
        DNOS_PORT1_NAME = links[SpirentSettings.TEST_PORT1_NAME]

        # Setup traffic flow for L3 routing
        self._setup_traffic_flow_for_l3_routing(DNOS_PORT1_NAME)

        # Setup BGP peers and validate
        sa_router_config, neighbor_ips = self._setup_bgp_peers_and_validate()

        # Manually control routing-engine container restart with traffic flow
        node_name = "ncc"
        node_id = 0
        container_name = "routing-engine"
        # Get node hostname for validations
        node = self.dnos_e2e_sa.get_node_by_name_and_id(node_name, node_id)
        node_hostname = node.node_name

        # Create traffic loss validation and collect baseline counters
        traffic_loss_validation = self._create_traffic_loss_validation(
            flow_name=flow_name,
            expected_loss=0,
            timeout=TrafficSettings.TRAFFIC_LOSS_VALIDATION_TIMEOUT * 2,  # 60 seconds for loss validation
            negative_validation=True  # Expect timeout (loss persists) = success
        )

        # Start traffic flow
        self._start_traffic_flow(flow_name, stabilization_seconds=0)

        # Restart container and clear post-validations (will be executed manually)
        logger.info("Initiating routing-engine container restart on NCC 0")
        RestartContainer(
            cluster_handler=self.dnos_e2e_sa,
            container_name=container_name,
            node_name=node_name,
            node_id=node_id
        ).clear_default_post_validations().execute()

        # Wait for container to be down
        ValidateContainerDown(node_hostname, container_name).execute()
        logger.info("Collecting baseline counters while RE is down")
        traffic_loss_validation.collect_data()

        # Wait for container to come back UP
        logger.info("Waiting for routing-engine container to come back up")
        ValidateContainerRunning(node_hostname, container_name, timeout=TrafficSettings.CONTAINER_RESTART_TIMEOUT).execute()        

        # Stop the flow
        self._stop_traffic_flow(flow_name)

        logger.info("Validating traffic loss while routing-engine was down")
        # Validate traffic loss after the downtime
        traffic_loss_validation.execute()

        # Validate no traffic loss after RE recovery
        logger.info("Validating no traffic loss after routing-engine recovery")
        self._wait_for_cli_and_bgp_reestablish(sa_router_config)

        # Re-validate ARP and routes advertised after RE restart to ensure forwarding plane is ready
        logger.info("Re-validating ARP table entry for port2 device after RE restart")
        ValidateARPTableEntry(
            cluster_handler=self.dnos_e2e_sa,
            expected_entry_ip=str(SpirentSettings.ROUTER_IPV4_PORT2.ip),
            expected_state="reachable"
        ).execute()

        ValidateBGPNeighborsPrefixAccepted(
            cluster_handler=self.dnos_e2e_sa,
            neighbor_ips=neighbor_ips,
            expected_pfx_accepted=1,
        ).execute()

        # Create validation instance and collect initial counters (baseline)
        no_loss_validation = self._create_traffic_loss_validation(
            flow_name=flow_name,
            expected_loss=0,
            timeout=TrafficSettings.NO_LOSS_VALIDATION_TIMEOUT
        )

        # Start traffic flow again (this resets port counters to 0)
        logger.info(f"Starting traffic flow '{flow_name}' again to verify system recovery")
        self._start_traffic_flow(flow_name, stabilization_seconds=TrafficSettings.TRAFFIC_FLOW_DURATION)

        # Stop the flow
        self._stop_traffic_flow(flow_name)

        # Validate zero traffic loss
        logger.info("Validating zero traffic loss after recovery")
        no_loss_validation.execute()


    @pytest.mark.owner(user="amaoz", component=JiraComponent.ROUTING)
    @pytest.mark.testing_task(component=JiraComponent.ROUTING, testing_task="SW-216338")
    @pytest.mark.dnos_e2e_sa_spirent
    @single_test_config(
        traffic_generator_config={
            **BASE_SPIRENT_CONFIG_DUAL_PORT,
            **CONTINUOUS_TRAFFIC_FLOW,
        },
        skip_validate_system_snapshot=True
    )
    def test_no_traffic_loss_on_re_patch_apply_and_revert(self):
        """
        Test no traffic loss during RE patch apply/revert.

        Flow:
        1. Setup - update flow destination MAC to DNOS interface MAC
        2. Enable BGP on both port1 and port2 peers
        3. Start traffic flow
        4. Apply RE patch
        5. Stop traffic - check no data loss
        6. Reset
        7. Start traffic flow
        8. Revert RE patch
        9. Stop traffic - check no data loss

        Note: This test validates that:
        - No traffic loss occurs during patch apply/revert operations
        - System remains stable after patch operations

        [Spirent Port1] ---(packets)---> [DNOS Router] ---(packets)---> [Spirent Port2]
         sends frames_tx                                                 receives frames_rx
        Zero Loss Check: (frames_tx_delta - frames_rx_delta) == 0
        """
        flow_name = "traffic_loss_test_flow"

        links = self.dnos_e2e_env.links.get_links("traffic_generator", self.dnos_e2e_sa.setup_name)
        DNOS_PORT1_NAME = links[SpirentSettings.TEST_PORT1_NAME]

        # Setup traffic flow for L3 routing
        self._setup_traffic_flow_for_l3_routing(DNOS_PORT1_NAME)

        # Setup BGP peers and validate
        sa_router_config, neighbor_ips = self._setup_bgp_peers_and_validate()

        # Get patch configuration
        affected_service = ContainerType.ROUTING_ENGINE.cli_name
        patch_version_path = self.dnos_e2e_env.patch_stack[SetupTypes.SA].get(affected_service)
        assert patch_version_path, f"Patch version for '{affected_service}' is not configured in patch_stack for {SetupTypes.SA}"

        # Extract DNOS package name and patch version
        dnos_package_name = self.dnos_e2e_sa.gi_utils.get_pkg_name_from_path(DNOS_PACKAGE, ComponentName.DNOS)
        patch_version = self.dnos_e2e_sa.gi_utils.get_version_from_path(patch_version_path)

        # Create no loss validation instance for patch apply
        no_loss_validation = self._create_traffic_loss_validation(
            flow_name=flow_name,
            expected_loss=0,
            operator=">",
            timeout=TrafficSettings.NO_LOSS_VALIDATION_TIMEOUT,
            negative_validation=True
        )

        logger.info("=== Starting Apply Patch Flow ===")

        # Start traffic flow
        logger.info(f"Starting traffic flow '{flow_name}'")
        self._start_traffic_flow(flow_name, stabilization_seconds=5)

        # Wait for traffic to flow consistently before collecting baseline
        # Wait for at least 5000 packets (5 seconds at 1000 pps) to ensure traffic is stable
        logger.info("Validating traffic is flowing consistently before collecting baseline")
        ValidateTrafficGeneratorCounters(
            traffic_generator=self.dnos_e2e_env.traffic_generator,
            counter_type="port",
            counter_data=CountersValidation(
                name=SpirentSettings.TEST_PORT2_NAME,
                counter_field="frames_rx",
                operator=">=",
                expected_value=5000,
                timeout=TrafficGeneratorValidationTime.VALIDATE_TRAFFIC_GENERATOR_COUNTERS_PACKETS_TRANSMISSION_TIMEOUT
            )
        ).execute()

        no_loss_validation.collect_data() #should be around 5000 packets

        logger.info(f"Applying RE patch: {patch_version_path}")
        service_image_map = gi_actions_helpers.build_service_to_image_map(affected_service, self.dnos_e2e_sa)
        gi_actions.ApplyDnosPatch(
            cluster_handler=self.dnos_e2e_sa,
            package_url=dnos_package_name,
            target_patch=patch_version_path,
            affected_service_image_map=service_image_map
        ).execute()

        logger.info("Stopping traffic flow after patch apply")
        self._stop_traffic_flow(flow_name)
        
        logger.info("Validating no traffic loss during patch apply")
        no_loss_validation.execute()

        # Wait for CLI and BGP to re-establish
        logger.info("Waiting for CLI and BGP to re-establish after patch apply")
        self._wait_for_cli_and_bgp_reestablish(sa_router_config)

        logger.info("=== Starting Revert Patch Flow ===")
        logger.info(f"Starting traffic flow '{flow_name}' for revert")
        self._start_traffic_flow(flow_name, stabilization_seconds=5)

        # Wait for traffic to flow consistently before collecting baseline
        # Wait for at least 5000 packets (5 seconds at 1000 pps) to ensure traffic is stable
        logger.info("Validating traffic is flowing consistently before collecting baseline for revert")
        ValidateTrafficGeneratorCounters(
            traffic_generator=self.dnos_e2e_env.traffic_generator,
            counter_type="port",
            counter_data=CountersValidation(
                name=SpirentSettings.TEST_PORT2_NAME,
                counter_field="frames_rx",
                operator=">=",
                expected_value=5000,
                timeout=TrafficGeneratorValidationTime.VALIDATE_TRAFFIC_GENERATOR_COUNTERS_PACKETS_TRANSMISSION_TIMEOUT
            )
        ).execute()

        no_loss_validation.collect_data()

        # Revert patch
        logger.info(f"Reverting RE patch: {patch_version}")
        service_image_map = gi_actions_helpers.build_service_to_image_map(affected_service, self.dnos_e2e_sa)
        gi_actions.RevertDnosPatch(
            cluster_handler=self.dnos_e2e_sa,
            dnos_package_name=dnos_package_name,
            installed_patch_version=patch_version,
            affected_service_image_map=service_image_map
        ).execute()

        # Stop traffic and check no data loss
        logger.info("Stopping traffic flow after patch revert")
        self._stop_traffic_flow(flow_name)
        logger.info("Validating no traffic loss during patch revert")
        no_loss_validation.execute()

        # Wait for CLI and BGP to re-establish
        logger.info("Waiting for CLI and BGP to re-establish after patch revert")
        self._wait_for_cli_and_bgp_reestablish(sa_router_config)

        logger.info("RE patch apply and revert completed with no traffic loss!")
