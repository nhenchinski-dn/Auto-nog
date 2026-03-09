import logging
import pytest
import waiting
from itertools import cycle
from time import sleep
from typing import Dict
from enum import Enum

from cheetah_api import interfaces_pb2 as interfaces_pb
from wb_api.bridge_domain_pb2 import BridgeDomainConfig, BridgeDomainInterfaceConfig
import wb_api.packet_injector_pb2 as pkt_injector
import wb_api.cfm_pb2 as cfm_pb
import wb_api.cfm_initiator_pb2 as cfm_initiator_pb
import wb_api.cfm_events_injector_pb2 as cfm_events
import generated_dn_api as gen_dn_api
from qpb.bd_fpm_in_pb2 import BdLocalMacAdd
from dn_common import consts
from dn_common.fib import consts as fib_consts

from scapy.all import Dot1Q, Dot1AD, Ether, Raw
from utils.scapy.cfm import *

from wbox.conftest import IS_JERICHO_2_B1, IS_NCP3, remote_test, devvm_test, get_device_version, IS_NCPL_SA, j3_skip, q3d_skip
from wbox.wbox_test import WBoxTestCase, skip_func_validate_resources
from .wbox_test_utils import auto_assign_member_id_to_interface, create_lag_interface
from .cfm_test_utils import CfmTestUtils
from utils.jira_utils import JiraComponent
from utils.network import mac_2_bytes
from corm import COrmObj, DBClientAPI
from copy import deepcopy
import threading
from addict import Dict
from qpb.fpm_pb2 import AcEsiStatus
from wb_api.evpn_pb2 import EvpnAcConfig, EvpnAcEtreeStatus, EvpnEthTagConfig, EvpnEviConfig
from parameterized import parameterized

logger = logging.getLogger(__name__)

MIN_SYNCE_SUPPORTED_DEVICE_VERSION_NCP3 = 2

### GLOBALS ###
CCM_PKT_PERIOD_DISABLED = 0
CCM_PKT_PERIOD_3MS      = 1
CCM_PKT_PERIOD_10MS     = 2
CCM_PKT_PERIOD_100MS    = 3
CCM_PKT_PERIOD_1S       = 4
CCM_PKT_PERIOD_10S      = 5
CCM_PKT_PERIOD_1M       = 6
CCM_PKT_PERIOD_10M      = 7

CFM_START_MEP_OAM_ID = 0
CFM_START_MIP_OAM_ID = 2000

ccm_config = cfm_pb.CfmContinuityCheckConfig()
ccm_config.ccm_enabled = cfm_pb.AdminState.ENABLED
ccm_config.ccm_interval = cfm_pb.CcmIntervalType.INTERVAL_1_SEC
ccm_config.loss_threshold = 3
ccm_config.fng_alarm_time = 2500
ccm_config.fng_reset_time = 4000

defect_clear_interval_multiplier = 3.5
wait_timeout_s = 25

ma1_name = "ab"
ma2_name = "ac"
ma_icc_name = "ma_icc"
md_name = "test_md"
maid1 = bytes(CCM.create_maid(ma_name=ma1_name, ma_name_format=MA_NAME_FORMAT_E.MA_UINT16))
maid2 = bytes(CCM.create_maid(ma_name="ac", ma_name_format=MA_NAME_FORMAT_E.MA_UINT16))
maid_icc = bytes(CCM.create_maid(ma_name="ma_icc_maid", ma_name_format=MA_NAME_FORMAT_E.MA_ICC))

md_level_down_mep = 2
md_level_up_mep = 5
md_level_mip = 3
down_mep_oam_id = CFM_START_MEP_OAM_ID + 0
down_mep_mep_id = 123
down_mep_remote_mep_id = 17
up_mep_oam_id = CFM_START_MEP_OAM_ID + 1
up_mep_mep_id = 456
up_mep_remote_mep_id = 155
md_id = "md"
ma_id1 = "ma1"
ma_id2 = "ma2"
ma_icc = "ma_icc"
group_id1 = 0
group_id2 = 1
group_id_icc = 2

md_id_icc = "md_icc"
md_name_icc = "test_md_icc"

mip_oam_id = CFM_START_MIP_OAM_ID
mip_name = "mip"

nr_ma = 20
nr_lmep = 1
nr_rmep = 200
bd_target_mac = "66:55:44:33:22:11"

BD_ID = 1
vsi_2 = 1001

max_auto_limit = 6
max_auto_threshold = 50

LMEP_PATH = "/drivenets-top/services/ethernet-oam/connectivity-fault-management/maintenance-domains"
LMEP_PATH += "/maintenance-domain/maintenance-associations/maintenance-association/local-meps"

LMEP_OPER_PATH = LMEP_PATH + "/local-mep/oper-items"

class EventDefectType(Enum):
    xconCCMdefect = 0
    errorCCMdefect = 1
    someRMEPCCMDefect = 2
    someMACstatusDefect = 3
    someRDIdefect = 4

CFM_TPID = 0x8902

def _create_bd_config_pb(
    bd_id,
    vsi,
    name,
    admin_state=True,
    mac_learning=True,
    mac_table_limit=64000,
    mac_table_aging_time=320,
    irb_interface=None
):
    return BridgeDomainConfig(
        bd_id=bd_id,
        vsi_id=vsi,
        name=name,
        admin_state=admin_state,
        mac_learning=mac_learning,
        mac_table_limit=mac_table_limit,
        mac_table_aging_time=mac_table_aging_time,
        irb_interface=irb_interface
    )

DN_OUI = '84:40:76'
MY_CFM_MAC_BASE = f"{DN_OUI}:00"
LOOPBACK_MIN_FRAME_SIZE = 9
LOOPBACK_MIN_FRAME_SIZE_DATA_TLV = 12

### TESTS ###

@j3_skip()
@q3d_skip()
class TestCfmManagerBase(CfmTestUtils, WBoxTestCase):

    def _send_add_bridge_domain_pb(self, bd_id, vsi, name, **kwargs):
        """
        Utiliy - Configure a bridge-domain service via protobuf
        """
        self.handler.wb_api.bridge_domain.bd_add(
            _create_bd_config_pb(bd_id, vsi, name, **kwargs)
        )

    def _send_del_bridge_domain_pb(self, bd_config):
        """
        Utiliy - Deletes a bridge-domain service via protobuf
        """
        self.handler.wb_api.bridge_domain.bd_del(bd_config)

    def _send_add_bridge_domain_interface_pb(self, bd_id, iface_name=None, ifindex=None):
        """
        Utiliy - Attach a L2-interface (AC) to a bridge-domain service via protobuf
        """
        if ifindex is None:
            if_data = self.handler.api.interface.get_interface(name=iface_name)
            ifindex = if_data.interface.get_interface.data.management_id

        self.handler.wb_api.bridge_domain.bd_interface_add(
            BridgeDomainInterfaceConfig(bd_id=bd_id, ifindex=ifindex)
        )

    def _send_remove_bridge_domain_interface_pb(self, bd_id, iface_name=None, ifindex=None):
        """
        Utiliy - Detach a L2-interface (AC) from a bridge-domain service via protobuf
        """
        if ifindex is None:
            if_data = self.handler.api.interface.get_interface(name=iface_name)
            ifindex = if_data.interface.get_interface.data.management_id

        self.handler.wb_api.bridge_domain.bd_interface_del(
            BridgeDomainInterfaceConfig(bd_id=bd_id, ifindex=ifindex)
        )

    @classmethod
    def set_iface_port_speed(cls, iface_name: str, port_speed: int, with_commit: bool = True):
        resp = cls.handler.api.interface.get_interface(name=iface_name)
        iface_data = resp.interface.get_interface.data
        cls.handler.api.interface.set_interface_port_speed(interface_name=iface_name, port_speed=port_speed)
        cls.handler.api.interface.update_interface(name=iface_name, updates=iface_data)
        if with_commit:
            cls.handler.full_commit()

    def _ccm_interval_to_ms(self, interval: cfm_pb.CcmIntervalType) -> float:
        if interval == cfm_pb.CcmIntervalType.INTERVAL_3_3_MS:
            return 3.3
        elif interval == cfm_pb.CcmIntervalType.INTERVAL_10_MS:
            return 10
        elif interval == cfm_pb.CcmIntervalType.INTERVAL_100_MS:
            return 100
        elif interval == cfm_pb.CcmIntervalType.INTERVAL_1_SEC:
            return 1000
        elif interval == cfm_pb.CcmIntervalType.INTERVAL_10_SEC:
            return 10 * 1000
        elif interval == cfm_pb.CcmIntervalType.INTERVAL_1_MIN:
            return 60 * 1000
        elif interval == cfm_pb.CcmIntervalType.INTERVAL_10_MIN:
            return 10 * 60 * 1000
        else:
            return 0

    def send_ccm_packets_thread(self, event, timeout=1):
        while not event.is_set():
            ccm_pkt = (
                Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
                Dot1Q(vlan=10, prio=5, type=0x8902) /
                CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
                CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
            )
            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt, number_of_packets=1)
            sleep(timeout)

    def send_ccm_multiple_packets_thread(self, event, nr_ma, timeout=1, interval=CCM_PKT_PERIOD_1S):
        while not event.is_set():
            for i in range(nr_ma):
                maid = bytes(CCM.create_maid(ma_name=f"ab{i}", ma_name_format=3))
                ccm_pkt = (
                    Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src=f'00:01:02:03:{(i >> 8) & 0xFF:02x}:{i & 0xFF:02x}') /
                    Dot1Q(vlan=50 + i, prio=5, type=0x8902) /
                    CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
                    CCM(mep_id=10 + i, ccm_interval=interval, maid=maid)
                )
                self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt, number_of_packets=1, log=False)

            # Only sleep for small scale tests - large scale already takes long enough to send
            if nr_ma <= 20:
                sleep(timeout)

    def send_ccm_with_rdi_packets_thread(self, event, timeout=1):
        while not event.is_set():
            ccm_pkt = (
                Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
                Dot1Q(vlan=10, prio=5, type=0x8902) /
                CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
                CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1, rdi=1)
            )
            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt, number_of_packets=1)

    def _assert_trap_counters(self, trap_name, expected_val) -> bool:
        traps_xray = self.handler.get_xray_stats('/traps')

        for trap in traps_xray:
            if trap['trap_name'] == trap_name:
                return int(trap['hw_accepted_counter']) == expected_val

        return False

    def _assert_oam_summary_xray(self, expected: Dict[str, int]) -> bool:
        oam_summary = self.handler.get_xray_stats("/cfm/summary")

        for elem in oam_summary:
            if elem["cfm_entity"] not in expected:
                continue

            exp_count = expected[elem["cfm_entity"]]
            configured = int(elem['configured'])
            ok = int(elem['ok'])
            err = int(elem['error'])

            if exp_count != configured or exp_count != ok or err != 0:
                logger.warning(f"For {elem['cfm_entity']}: expected {exp_count} != actual 'configured' {configured}, "
                    f"'ok' {ok}, 'error' {err}")

                return False

        return True

    def _assert_oam_initiator_summary_xray(self, expected: Dict[str, int]) -> bool:
        oam_summary = self.handler.get_xray_stats("/cfm/initiator_summary")
        xray_line = oam_summary[0]

        for key, exp in expected.items():
            actual = int(xray_line[key])
            if (actual != exp):
                logger.warning(f"'{key}' expected: {exp} != actual: {actual}")
                return False

        return True

    def _get_oam_initiator_summary_xray(self, field) -> int:
        oam_summary = self.handler.get_xray_stats("/cfm/initiator_summary")
        return int(oam_summary[0][field])

    def _assert_oam_summary_error_xray(self, expected: Dict[str, int]) -> bool:
        oam_summary = self.handler.get_xray_stats("/cfm/summary")

        for elem in oam_summary:
            entity = elem["cfm_entity"]
            if entity not in expected:
                continue

            exp_count = expected[entity]
            configured = int(elem['configured'])
            ok = int(elem['ok'])
            err = int(elem['error'])

            if (exp_count != configured or exp_count != err):
                logger.warning(f"For {entity}: expected {exp_count} != actual 'configured' {configured}, "
                    f"'ok' {ok}, 'error' {err}")
                return False

        return True

    def _check_xray_rmep(self, ma_id: str, rmep_id: int, xray_expected: Dict) -> bool:
        rmep_data = self.handler.get_xray_stats("/cfm/remote_meps")
        for rmep in rmep_data:
            if rmep['ma_id'] == ma_id and rmep['mep_id'] == str(rmep_id):
                for key, exp in xray_expected.items():
                    if exp != rmep[key]:
                        logger.warning(f"Field '{key}' expected: '{exp}' != actual '{rmep[key]}'")
                        return False
                return True

        return False

    def read_xray_lmep_cnt(self, oam_id, counter):
        lmep_stats_xray = self.handler.get_xray_stats('/cfm/local_meps_cnt')
        for ep_counters in lmep_stats_xray:
            if int(ep_counters.get('oam_id')) == oam_id:
                return int(ep_counters.get(counter))

    def _assert_lmep_stats_counters(self, oam_id, counter, expected_val) -> bool:
        lmep_stats_xray = self.handler.get_xray_stats('/cfm/local_meps_cnt')

        for ep_counters in lmep_stats_xray:
            if int(ep_counters.get('oam_id')) == oam_id:
                return int(ep_counters.get(counter)) == expected_val

        return False

    def _assert_operdb_lmep_empty(self) -> bool:
        with DBClientAPI() as corm_api:
            cfm_oper = corm_api.get_by_path(LMEP_OPER_PATH, ['*', '*', '*'], include_lists=True, is_recursive=True)

            logger.warning(f"Nr. lmeps left in oper: {len(cfm_oper)}")

            if (len(cfm_oper) != 0):
                return False

            cfm_oper = corm_api.get_by_path(LMEP_PATH + "/mip/oper-items",
                                            ['*', '*', '*'], include_lists=True, is_recursive=True)

            logger.warning(f"Nr. mips left in oper: {len(cfm_oper)}")

            if (len(cfm_oper) != 0):
                return False

        return True

    def _assert_operdb_rmep_empty(self) -> bool:
        with DBClientAPI() as corm_api:
            cfm_oper = corm_api.get_by_path(LMEP_OPER_PATH + "/mep-db",
                ['*', '*', '*', '*'], include_lists=True, is_recursive=True)
            logger.warning(f"Nr. rmeps left in oper: {len(cfm_oper)}")

            return len(cfm_oper) == 0

    def _assert_operdb_count(self, nr_lmep, nr_mip, nr_rmep) -> bool:
        with DBClientAPI() as corm_api:
            cfm_oper = corm_api.get_by_path(LMEP_OPER_PATH, ['*', '*', '*'], include_lists=True, is_recursive=True)

            if (len(cfm_oper) != nr_lmep):
                logger.warning(f"Operdb lmeps count: {len(cfm_oper)} expected: {nr_lmep}")
                return False

            mip_oper = corm_api.get_by_path(LMEP_PATH + "/mip/oper-items",
                                            ['*', '*', '*'], include_lists=True, is_recursive=True)

            if (len(mip_oper) != nr_mip):
                logger.warning(f"Operdb mips count: {len(mip_oper)} expected: {nr_mip}")
                return False

            rmep_oper = corm_api.get_by_path(LMEP_OPER_PATH + "/mep-db",
                                            ['*', '*', '*', '*'], include_lists=True, is_recursive=True)

            if (len(rmep_oper) != nr_rmep):
                logger.warning(f"Operdb rmeps count: {len(rmep_oper)} expected: {nr_rmep}")
                return False

        return True


    def _assert_operdb_rmep(self, rmep_config: Dict, rmep_oper_expected: Dict) -> bool:
        readobj = COrmObj(LMEP_OPER_PATH + "/mep-db",
                          [rmep_config['md_id'], rmep_config['ma_id'], rmep_config['mep_id'], rmep_config['rmep_id']])
        readobj.db_get(timeout=10000)

        for field in rmep_oper_expected:
            if rmep_oper_expected[field] != getattr(readobj, field):
                logger.warning(f"{rmep_config}: Field '{field}' expected value: '{rmep_oper_expected[field]}' "
                    f"got: '{getattr(readobj, field)}'")
                return False

        return True

    def _get_mac_address(self, lmep_config: Dict) -> str:
        readobj = COrmObj(LMEP_OPER_PATH, [lmep_config['md_id'], lmep_config['ma_id'], lmep_config['mep_id']])
        readobj.db_get(timeout=10000)

        return getattr(readobj, "mac_address")

    def _assert_get_mac_address(self, lmep_config: Dict) -> bool:
        readobj = COrmObj(LMEP_OPER_PATH, [lmep_config['md_id'], lmep_config['ma_id'], lmep_config['mep_id']])
        readobj.db_get(timeout=10000)

        return (getattr(readobj, "mac_address") != None)

    def _assert_operdb_lmep_cnt(self, lmep_config: Dict, current_lmep: Dict, current_summary_lmep: Dict,
                                lmep_oper_expected: Dict, lmep_oper_summary_expected: Dict, mp_type="local-mep") -> bool:
        readobj = COrmObj(f"{LMEP_PATH}/{mp_type}/oper-items/pdu-statistics",
                          [lmep_config['md_id'], lmep_config['ma_id'], lmep_config['mep_id']])
        readobj.db_get(timeout=10000)

        readobj_summary = COrmObj("/drivenets-top/services/ethernet-oam/connectivity-fault-management/global/global-statistics/pdu-statistics")
        readobj_summary.db_get(timeout=10000)

        for field in lmep_oper_expected:
            v_get = getattr(readobj, field)
            v_oper = v_get if v_get else 0
            if lmep_oper_expected[field] != None:
                if v_oper:
                    logger.warning(f"for {field} expected difference {lmep_oper_expected[field]} got: {v_oper - current_lmep[field]}")
                    if v_oper - current_lmep[field] < lmep_oper_expected[field]:
                        return False
                else:
                    logger.warning(f"for {field} no value yet in LMEP statistics")
                    return False

        for field in lmep_oper_summary_expected:
            v_get = getattr(readobj_summary, field)
            v_oper = v_get if v_get else 0
            if lmep_oper_summary_expected[field] != None:
                if v_oper:
                    logger.warning(f"for summary {field} expected difference {lmep_oper_summary_expected[field]} got {v_oper - current_summary_lmep[field]}")
                    if v_oper - current_summary_lmep[field] < lmep_oper_summary_expected[field]:
                        return False
                else:
                    logger.warning(f"for {field} no value yet in summary statistics")
                    return False

        return True

    def _get_current_counters(self, lmep_config: Dict, mp_type="local-mep"):
        lmep_fields = {'ccm_in', 'ccm_out', 'ccms_wrong_interval', 'wrong_level', 'ccms_wrong_maid',
                       'ccms_wrong_rmep', 'lbm_out', 'lbr_in', 'unsupported_cfm_pdu',
                       'ltm_in', 'ltm_out', 'ltr_in', 'ltr_out', 'unicast_mac_mismatch', 'passive_in', 'passive_in_wrong_level'}
        mip_fields = {'wrong_level', 'ltm_in', 'ltm_out', 'ltr_in', 'ltr_out',
                        'unsupported_cfm_pdu', 'unicast_mac_mismatch'}
        lmep_summary_fields = {'ccm_in', 'ccm_out', 'ccms_wrong_interval', 'wrong_level', 'ccms_wrong_maid',
                               'ccms_wrong_rmep', 'lbm_out', 'lbr_in', 'unsupported_cfm_pdu',
                               'ltm_in', 'ltm_out', 'ltr_in', 'ltr_out', 'unicast_mac_mismatch', 'passive_in', 'passive_in_wrong_level'}

        readobj = COrmObj(f"{LMEP_PATH}/{mp_type}/oper-items/pdu-statistics",
                          [lmep_config['md_id'], lmep_config['ma_id'], lmep_config['mep_id']])
        readobj.db_get(timeout=10000)

        readobj_summary = COrmObj("/drivenets-top/services/ethernet-oam/connectivity-fault-management/global/global-statistics/pdu-statistics")
        readobj_summary.db_get(timeout=10000)

        items = {}
        mp_fields = lmep_fields if mp_type == "local-mep" else mip_fields

        for field in mp_fields:
            value = getattr(readobj, field)
            items[field] = value if value else 0

        items_summary = {}
        for field in lmep_summary_fields:
            value = getattr(readobj_summary, field)
            items_summary[field] = value if value else 0

        return (items, items_summary)

    def _diff_orm_counters(self, cnt_before, mp_diff, stats_diff, mep_id, md_id, ma_id, counters_list):
        cnt_bf_mep, cnt_bf_summary = cnt_before
        cnt_af_mep, cnt_af_summary = self._get_current_counters(lmep_config={
                "md_id" : md_id,
                "ma_id" : ma_id,
                "mep_id" : str(mep_id)})

        for counter in counters_list:
            if cnt_af_mep[counter] - cnt_bf_mep[counter] != mp_diff:
                logger.error(f"per_mep::{counter} expected diff {mp_diff} but "
                    f"got {cnt_af_mep[counter] - cnt_bf_mep[counter]}; mep_id: {mep_id}")
                return False

            if cnt_af_summary[counter] - cnt_bf_summary[counter] != stats_diff:
                logger.error(f"summary::{counter} expected diff {stats_diff} but "
                    f"got {cnt_af_summary[counter] - cnt_bf_summary[counter]}; mep_id: {mep_id}")
                return False

        return True

    def _assert_traffic(self, traffic_expected: bool):
        def pkt_filter_vlan_10(x):
            return (x.getlayer(Ether).dst == f'01:80:c2:00:00:3{md_level_down_mep}' and x.haslayer(Dot1Q) and x.getlayer(Dot1Q).vlan == 10)

        if not self._is_real_traffic_test():
            return

        ccm_pkt_down= (
            Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=10, prio=5, type=0x8902) /
            CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
            CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
        )
        ccm_pkt_count = int(self.cfm_read_counters()['cfm_down_mep_ccm'])
        delta = 1 if traffic_expected else 0

        # Start traffic capture (stats + tcpdump)
        capture = self.cfm_start_traffic_capture("assert_traffic")

        try:
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_vlan_10)
            rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
            self.assertEqual(len(rx_packets), delta)

            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down, number_of_packets=1)
            sleep(1)
            assert int(self.cfm_read_counters()['cfm_down_mep_ccm']) == ccm_pkt_count + delta
        except Exception:
            self.cfm_diagnose_no_traffic(self.WB_IF_1_NAME, ccm_pkt_down, "assert_traffic")
            raise
        finally:
            self.cfm_stop_traffic_capture(capture)

    def _assert_good_packets(self, expected: int):
        ccm_good_pkt_count = int(self.cfm_read_counters()['cfm_ccm_good_packet'])

        logger.warning(f"got {ccm_good_pkt_count} expected {expected}")

        # looks that there is BCM issue and sometimes there were trapped more than expected number of packets
        return ccm_good_pkt_count >= expected and ccm_good_pkt_count <= expected + 1

    def _gen_my_cfm_mac(self, parent_internal_index):
        byte4 = (parent_internal_index >> 8) % 0xFF
        byte5 = (parent_internal_index & 0xFF) % 0xFF

        return f"{MY_CFM_MAC_BASE}:{byte4:02x}:{byte5:02x}"

    def _install_downmep(self, oam_id=down_mep_oam_id, if_id=0):
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[if_id],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

    def _send_recreate_downmep(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

    def _install_upmep(self, oam_id=up_mep_oam_id, if_id=1, level=md_level_up_mep):
        self.handler.wb_api.cfm.create_md(md_id=md_id)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id2, maid48=bytes(maid2), md_id=md_id,
                                          ma_name=ma2_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=group_id2)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=oam_id,
            mep_id=up_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id2,
            group_oam_id=group_id2,
            interface_name=iface_names[if_id],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][if_id],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][if_id],
            ccm_ltm_priority=5,
            md_level=level,
            remote_mep_ids=[up_mep_remote_mep_id],
            ccm_config=ccm_config)

        self.handler.full_commit()

    def _install_mip(self, md_id=md_id, md_name=md_name, if_id=0, level=md_level_mip):
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_icc, maid48=bytes(maid_icc), md_id=md_id, oam_id=group_id_icc,
                                          ma_name=ma_icc_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_NONE,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_mip(
            oam_id=mip_oam_id,
            name=mip_name,
            md_id=md_id,
            ma_id=ma_icc,
            group_oam_id=group_id_icc,
            interface_name=iface_names[if_id],
            admin_state=cfm_pb.AdminState.ENABLED,
            md_level=level,
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

    def _prepare_basic_oam_setup(self):
        self._install_downmep()
        self._install_upmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 2,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

    def _uninstall_mip(self, delete_md=True, delete_ma=True, md_id=md_id, mip_name=mip_name):
        self.handler.wb_api.cfm.delete_mip(md_id=md_id, ma_id=ma_icc, mip_name=mip_name)
        if delete_ma:
            self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_icc)

        if delete_md:
            self.handler.wb_api.cfm.delete_md(md_id=md_id)

        self.handler.full_commit()

    def _uninstall_upmep(self, delete_md=True):
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id2, mep_id=up_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id2)

        if delete_md:
            self.handler.wb_api.cfm.delete_md(md_id=md_id)

        self.handler.full_commit()

    def _uninstall_downmep(self, delete_md=True):
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)

        if delete_md:
            self.handler.wb_api.cfm.delete_md(md_id=md_id)

        self.handler.full_commit()

    def _cleanup_basic_oam_setup(self):
        self._uninstall_downmep(delete_md=False)
        self._uninstall_upmep(delete_md=True)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

    def _install_mep_autodiscovery(self, md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id, oam_id=down_mep_oam_id):
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id, maid48=bytes(maid1), md_id=md_id, oam_id=oam_id,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE, auto_discovery_enabled=cfm_pb.AdminState.ENABLED)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=oam_id,
            mep_id=mep_id,
            md_id=md_id,
            ma_id=ma_id,
            group_oam_id=oam_id,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.set_auto_discovery_threshold(maximum_auto=max_auto_limit, maximum_auto_syslog_threshold=max_auto_threshold)
        self.handler.full_commit()

    def _uninstall_mep_autodiscovery(self, md_id, ma_id, mep_id):
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id, mep_id=mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    def send_trap_wrong_packet(self, hw_id, rmep_id, trap_code, maid=maid1, vlan=10):
        ccm_pkt = (
            Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=vlan, prio=5, type=CFM_TPID) /
            CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
            CCM(mep_id=rmep_id,
                ccm_interval=cfm_pb.CcmIntervalType.INTERVAL_1_SEC, maid=maid)
        )

        iface_vsi = self.if_internal_idx_2_vsi(1000)
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt,
            wbox_trap_codes=[trap_code], cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=hw_id, vsi=iface_vsi))

        return ccm_pkt

    def _validate_ccm_in_counters(self, md_id, ma_id, mep_id, pkt_count, pkt, interface):
        (counters, counters_summary) = self._get_current_counters(lmep_config={
        "md_id" : md_id,
        "ma_id" : ma_id,
        "mep_id" : str(mep_id)})

        if (pkt_count > 0):
            self.handler.data_communicator.tx(interface=interface, packet=pkt, number_of_packets=pkt_count)

        lmep_oper_expected = {}
        for key in counters.keys():
            lmep_oper_expected[key] = None

        lmep_oper_expected['ccm_in'] = pkt_count

        lmep_oper_summary_expected = {}
        for key in counters_summary.keys():
            lmep_oper_summary_expected[key] = None

        lmep_oper_summary_expected['ccm_in'] = pkt_count

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id,
                "mep_id" : str(mep_id)
                },
            current_lmep=counters,
            current_summary_lmep=counters_summary,
            lmep_oper_expected=lmep_oper_expected,
            lmep_oper_summary_expected=lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=25)

    def _validate_cfm_mac_in_oper(self, md_id, ma_id, mep_id) -> str:
        waiting.wait(lambda: self._assert_get_mac_address(lmep_config={
            "md_id" : md_id,
            "ma_id" : ma_id,
            "mep_id" : str(mep_id)
            }), sleep_seconds=0.5, timeout_seconds=10)

        dst_cfm_mac = self._get_mac_address(lmep_config={
            "md_id" : md_id,
            "ma_id" : ma_id,
            "mep_id" : str(mep_id)})

        return dst_cfm_mac

    def _validate_slr_count(self, md_id, ma_id, mep_id, level, pkt_count, interface):
            def slr_filter(x):
                return (x.getlayer(Ether).dst == '00:01:02:03:04:05') and (x.haslayer(SLR))

            SourceMEP_ID = 888
            TestID = 999
            TxFcf = 5432

            dst_cfm_mac = self._validate_cfm_mac_in_oper(md_id, ma_id, mep_id)

            pkt = (Ether(dst=dst_cfm_mac, src='00:01:02:03:04:05') /
                Dot1Q(vlan=10, prio=5) /
                CFM(md_level=level) /
                SLM(SourceMEP_ID=SourceMEP_ID, TestID=TestID, TxFcf=TxFcf))

            self.handler.data_communicator.start_sniffer(interface=interface, pkt_filter=slr_filter)
            self.handler.data_communicator.tx(interface=interface, packet=pkt, number_of_packets=pkt_count)
            rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=pkt_count)

            self.assertEqual(len(rx_packets), pkt_count)

            rx_packet = rx_packets[pkt_count - 1]
            self.assertEqual(rx_packet[CFM].md_level, level)
            self.assertEqual(rx_packet[SLR].SourceMEP_ID, SourceMEP_ID) # copied from SLM
            self.assertEqual(rx_packet[SLR].ResponderMEP_ID, mep_id) # populated by BCM
            self.assertEqual(rx_packet[SLR].TestID, TestID) # copied from SLM
            self.assertEqual(rx_packet[SLR].TxFcf, TxFcf) # copied from SLM
            self.assertEqual(rx_packet[SLR].TxFcb, rx_packets[0][SLR].TxFcb + pkt_count - 1) # check BCM populates

    def _assert_xray_mp_field(self, table, mp_id_field, mp_id_val, field, expected_value):
        xray_data = self.handler.get_xray_stats(table)
        for entry in xray_data:
            if str(entry[mp_id_field]) == str(mp_id_val):
                return entry[field].lower() == expected_value.lower()

        return False

    def _create_lbr_test_packet(self, level, dst, src='00:01:02:03:04:05', transaction_id=100):
        return (
            Ether(dst=dst, src=src) /
            Dot1Q(vlan=10, prio=5, type=0x8902) /
            CFM(md_level=level, opcode=LBR.opcode) /
            LBR(transaction_id=transaction_id))

    def _create_ltm_test_packet(self, level, dst, src='00:01:02:03:04:05'):
        return (
            (Ether(dst=f'01:80:c2:00:00:3{level + 8:x}', src=src) /
               Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
               CFM(md_level=level, opcode=LTM.opcode) /
               LTM(use_fdb_only=1, transaction_id=99, ttl=3,
                    original_mac="aa:aa:bb:bb:cc:cc", target_mac=dst,
                    tlv_list=[
                        LtmEgressIdentifierTlv(initiator_mac="aa:22:33:44:55:66", initiator_id=66)])))

    def _test_linktrace_mip_ltm_fwd_and_ltr(self, vlan_fwd):
        iface_names = WBoxTestCase.global_params["interfaces"]
        target_mac_existing = "66:55:44:33:22:11"
        original_mac = "aa:aa:bb:bb:cc:cc"
        initiator_mac = "aa:22:33:44:55:66"
        initiator_id = 66
        transaction_id = 99
        ttl = 5

        # Disable MAC table HW <-> SW sync
        self.handler.execute_command('mact set traverse run 0')

        self.handler.fpm_api.send_bridge_domain_mac_add_msg(
            bd_id=0,
            vsi_id=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
            action_type=BdLocalMacAdd.ActionType.NEW,
            ifindex=self.handler.api.interface.get_interface(iface_names[1]).interface.get_interface.data.management_id,
            mac_bytes=mac_2_bytes(target_mac_existing))

        self._install_mip()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0,
            "MIP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        ltm = (Ether(dst=f'01:80:c2:00:00:3{md_level_mip + 8:x}', src="00:01:02:03:04:05") /
                Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
                CFM(md_level=md_level_mip, opcode=LTM.opcode) /
                LTM(use_fdb_only=1, transaction_id=transaction_id, ttl=ttl,
                    original_mac=original_mac, target_mac=target_mac_existing,
                    tlv_list=[LtmEgressIdentifierTlv(initiator_mac=initiator_mac, initiator_id=initiator_id)]))

        def pkt_filter_ltm_and_ltr(x):
            return (x.haslayer(LTR) or (x.haslayer(LTM) and x.getlayer(Ether).src != "00:01:02:03:04:05"))

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_ltm_and_ltr)
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm, number_of_packets=1)
        rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=2)
        self.assertEqual(len(rx_packets), 2)
        for i in range(2):
            if (rx_packets[i]).haslayer(LTM):
                rx_packet_ltm = rx_packets[i]
                self.assertEqual(rx_packet_ltm[LTM].ttl, ttl - 1)
                self.assertEqual(rx_packet_ltm[LTM].transaction_id, transaction_id)
                self.assertEqual(rx_packet_ltm[LTM].original_mac, original_mac)
                self.assertEqual(rx_packet_ltm[LTM].target_mac, target_mac_existing)
                self.assertEqual(rx_packet_ltm[Dot1Q].vlan, vlan_fwd)
            elif (rx_packets[i]).haslayer(LTR):
                rx_packet_ltr = rx_packets[i]
                self.assertEqual(rx_packet_ltr[LTR].ttl, ttl - 1)
                self.assertEqual(rx_packet_ltr[Ether].dst, original_mac)
                self.assertEqual(rx_packet_ltr[Ether].src, WBoxTestCase.global_params["my_cfm_mac"][0])
            else:
                assert False, "Unexpected packet received"

        # Enable MAC table HW <-> SW sync
        self.handler.execute_command('mact set traverse run 1')

        self._uninstall_mip()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "MIP": 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

    def check_disable_l2_service_and_delete_downmep(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # wait for defect state machine to trigger REDIS write
        sleep(5)

        vlan_data = interfaces_pb.Interface()
        vlan_data.name = iface_names[0]
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.l2_service = False

        self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_names[0])
        self.handler.api.interface.update_interface(name=iface_names[0], updates=vlan_data)

        self._uninstall_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        vlan_data = interfaces_pb.Interface()
        vlan_data.name = iface_names[0]
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.l2_service = True

        self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_names[0])
        self.handler.api.interface.update_interface(name=iface_names[0], updates=vlan_data)
        self.handler.full_commit()

    def check_disable_l2_service_and_delete_upmep(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self._install_upmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # wait for defect state machine to trigger REDIS write
        sleep(5)

        vlan_data = interfaces_pb.Interface()
        vlan_data.name = iface_names[1]
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.l2_service = False

        self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_names[1])
        self.handler.api.interface.update_interface(name=iface_names[1], updates=vlan_data)

        self._uninstall_upmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        vlan_data = interfaces_pb.Interface()
        vlan_data.name = iface_names[1]
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.l2_service = True

        self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_names[1])
        self.handler.api.interface.update_interface(name=iface_names[1], updates=vlan_data)
        self.handler.full_commit()

    @classmethod
    def if_internal_idx_2_vsi(cls, internal_index):
        # based on fib_definitions.h
        INTERFACE_VSI_ID_MIN = 0x1004
        return internal_index + INTERFACE_VSI_ID_MIN

    @classmethod
    def get_rmep_hw_id(cls, lmep_oam_id, mep_id):
        return lmep_oam_id * 8192 + mep_id

    def send_good_packet(self, level, rmep_mep_id, lmep_oam_id, maid, vlan=10,
                         interval=CCM_PKT_PERIOD_1S, interface=WBoxTestCase.WB_IF_1_NAME):
        ccm_pkt = (
            Ether(dst=f'01:80:c2:00:00:3{level}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=vlan, prio=5, type=0x8902) /
            CFM(md_level=level, opcode=CCM.opcode) /
            CCM(mep_id=rmep_mep_id, ccm_interval=interval, maid=maid)
        )

        if self._is_real_traffic_test():
            self.handler.data_communicator.tx(interface=interface, packet=ccm_pkt, number_of_packets=1)
        else:
            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt,
                wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_GOOD_PACKET],
                cfm_oam_info=pkt_injector.CfmData(rmep_hw_id=self.get_rmep_hw_id(lmep_oam_id, rmep_mep_id)))

def opcode2str(test_type: cfm_initiator_pb.SessionOpcode) -> str | None:
    return {
        cfm_initiator_pb.SessionOpcode.DMM: 'two-way-delay-measurement',
        cfm_initiator_pb.SessionOpcode.SLM: 'two-way-synthetic-loss-measurement',
        cfm_initiator_pb.SessionOpcode.LTM: 'linktrace',
        cfm_initiator_pb.SessionOpcode.LBM: 'loopback',
    }.get(test_type)

ON_DEMAND_BASE = "/drivenets-top/services/performance-monitoring/cfm-tests/on-demand-tests"
def on_demand_test_info(test_type: cfm_initiator_pb.SessionOpcode) -> str:
    return f"{ON_DEMAND_BASE}/{opcode2str(test_type)}/test-result/test-info"

def on_demand_test_result(test_type: cfm_initiator_pb.SessionOpcode) -> str:
    return f"{ON_DEMAND_BASE}/{opcode2str(test_type)}/test-result/test-results"

@pytest.mark.first
@pytest.mark.wbox_j2_beta
@pytest.mark.owner(user="vpostovaru", component=JiraComponent.WHITEBOX)
@pytest.mark.wbox_cfm
class TestCfmManager(TestCfmManagerBase):

    @pytest.fixture(scope='class', autouse=True)
    def setup_interfaces(self, request, events_queue):
        cls = request.cls
        cls.management_id_allocator = cycle(range(1000, 60000))
        cls.events_queue = events_queue

        cls.set_iface_oper_state(
            iface_name=WBoxTestCase.WB_IF_1_NAME, is_oper_up=True)
        cls.set_iface_oper_state(
            iface_name=WBoxTestCase.WB_IF_2_NAME, is_oper_up=True)

        WBoxTestCase.global_params["interfaces"] = []
        WBoxTestCase.global_params["my_cfm_mac"] = []
        WBoxTestCase.global_params["outer_tag"] = []
        WBoxTestCase.global_params["outer_tpid"] = []
        WBoxTestCase.global_params["expected_transaction_id"] = 0

        if_vlans = []

        for i in range(2 * nr_ma):
            if_vlans.append({"parent": WBoxTestCase.WB_IF_1_NAME, "vlan_tag": (i + 10), "outer_tpid": 0x8100,
                             "l2_service": True, "pcp_preserve": True})

        with self.vlans_manager_with_config(if_vlans) as iface_handle:
            self._send_add_bridge_domain_pb(bd_id=BD_ID, vsi=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
                                            name="bridge0", admin_state=True)

            # add first 2 interfaces to BD
            for i in range(2):
                self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=iface_handle[i].management_id)

            self.handler.full_commit()

            for i in range(2 * nr_ma):
                parent_internal_index = self.handler.api.interface.get_interface(
                    iface_handle[i].name.split('.')[0]).interface.get_interface.data.internal_index
                WBoxTestCase.global_params["interfaces"].append(iface_handle[i].name)
                WBoxTestCase.global_params["my_cfm_mac"].append(self._gen_my_cfm_mac(parent_internal_index))
                WBoxTestCase.global_params["outer_tag"].append(iface_handle[i].sub.vlan_tag)
                WBoxTestCase.global_params["outer_tpid"].append(iface_handle[i].sub.outer_tpid)

            yield

            # remove first 2 interfaces from BD
            for i in range(2):
                # it may be that interface has a different management id at teardown as some tests
                # are delete the interface and recreate it with a different management_id
                self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_handle[i].name)

            self._send_del_bridge_domain_pb(_create_bd_config_pb(bd_id=BD_ID, vsi=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
                                                                 name="bridge0"))

            self.handler.full_commit()

    @pytest.fixture(scope='function', autouse=True)
    def check_test_clear(self):
        yield
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0,
            "MIP" : 0
        }), sleep_seconds=0.5, timeout_seconds=25)

        waiting.wait(lambda: self._assert_operdb_lmep_empty(), sleep_seconds=0.5, timeout_seconds=25)
        waiting.wait(lambda: self._assert_operdb_rmep_empty(), sleep_seconds=0.5, timeout_seconds=25)

    @remote_test()
    def test_update_group(self):
        pass

    @remote_test()
    def test_update_group_with_traffic(self):
        def pkt_filter_icc_ccm(x):
            return (x.haslayer(CCM) and x.getlayer(CCM).maid[0] == 1 and x.getlayer(CCM).maid[1] == 32
                    and x.getlayer(CCM).maid[2] == 13)

        def pkt_filter_uint16_ccm(x):
            return (x.haslayer(CCM) and x.getlayer(CCM).maid[0] == 1 and x.getlayer(CCM).maid[1] == 3
                    and x.getlayer(CCM).maid[2] == 2)

        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_uint16_ccm)
        rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
        self.assertEqual(len(rx_packets), 1)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid_icc), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_icc_ccm)
        rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
        self.assertEqual(len(rx_packets), 1)

        self._uninstall_downmep()

    @remote_test()
    def test_update_group_same_flexible(self):
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_20_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid_icc), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_20_BYTES,
                                          req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_40_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid_icc), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_40_BYTES,
                                          req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid_icc), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_NONE,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid_icc), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_NONE,
                                          req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_oam_summary_error_xray({
            "MA" : 1,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    @remote_test()
    def test_update_group_wrong_flexible(self):
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_40_BYTES,
                                          req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_oam_summary_error_xray({
            "MA" : 1,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()


    @remote_test()
    def test_update_group_icc_local_mep(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid_icc), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_NONE,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # MEP fails if bank >= 5
        # waiting.wait(lambda: self._assert_oam_summary_error_xray({
        #     "Local MEP" : 1,
        # }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # MEP OK if bank < 5 (currently in bank1)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Local MEP" : 1,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()


    """ Test adding, updating and deleting OAM objects. Only count of objects is verified.
        1. Add objects, verify xray
        2. Update LMEP and RMEP configs
        3. Delete objects, verify all correctly deleted and ACKed
    """
    def test_oam_update_interval(self):
        self._prepare_basic_oam_setup()

        iface_names = WBoxTestCase.global_params["interfaces"]

        ccm_config_update = deepcopy(ccm_config)
        ccm_config_update.ccm_interval = cfm_pb.CcmIntervalType.INTERVAL_100_MS

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config_update,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 2,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._cleanup_basic_oam_setup()

    def test_update_non_existing_components(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        with self.assertRaises(gen_dn_api.InvalidResponseCodeException):
            self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)

        with self.assertRaises(gen_dn_api.InvalidResponseCodeException):
            self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                        ma_name=ma1_name, md_name=md_name,
                                        flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                        req_type=cfm_pb.CreateRequestType.CREATE)

        with self.assertRaises(gen_dn_api.InvalidResponseCodeException):
            # send an update to an unexisting endpoint mep id
            self.handler.wb_api.cfm.create_lmep(
                oam_id=down_mep_oam_id,
                mep_id=down_mep_mep_id,
                md_id=md_id,
                ma_id=ma_id1,
                group_oam_id=group_id1,
                interface_name=iface_names[0],
                direction=cfm_pb.MepDirection.DOWN,
                admin_state=cfm_pb.AdminState.ENABLED,
                outer_tag=WBoxTestCase.global_params["outer_tag"][0],
                outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
                ccm_ltm_priority=5,
                md_level=md_level_down_mep,
                ccm_config=ccm_config,
                req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            req_type=cfm_pb.CreateRequestType.CREATE)

        # send an update with rmep
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)

        self.handler.full_commit()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_tx_enable_disable(self):
        def pkt_filter_vlan_10(x):
            return (x.getlayer(Ether).dst == f'01:80:c2:00:00:3{md_level_down_mep}' and x.haslayer(Dot1Q) and x.getlayer(Dot1Q).vlan == 10)

        self._prepare_basic_oam_setup()

        iface_names = WBoxTestCase.global_params["interfaces"]

        ccm_config_tx_disable = deepcopy(ccm_config)
        ccm_config_tx_disable.ccm_enabled = cfm_pb.AdminState.DISABLED

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config_tx_disable,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()
        sleep(2)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 2,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        if self._is_real_traffic_test():
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_vlan_10)
            rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
            self.assertEqual(len(rx_packets), 0)

        # switch back to ccm_config_tx_disable.ccm_enabled set to **default** cfm_pb.AdminState.ENABLED
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config, # switch
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()
        sleep(2)

        if self._is_real_traffic_test():
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_vlan_10)
            rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)

        self._cleanup_basic_oam_setup()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_create_mep_tx_disabled(self):
        def pkt_filter_vlan_10(x):
            return (x.getlayer(Ether).dst == f'01:80:c2:00:00:3{md_level_down_mep}' and x.haslayer(Dot1Q)
                    and x.getlayer(Dot1Q).vlan == 10)

        self._prepare_basic_oam_setup()

        # delete the downMEP and its RMEP
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)

        self.handler.full_commit()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        iface_names = WBoxTestCase.global_params["interfaces"]

        ccm_config_tx_disable = deepcopy(ccm_config)
        ccm_config_tx_disable.ccm_enabled = cfm_pb.AdminState.DISABLED

        # recreate DownMEP with TX disabled
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config_tx_disable,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 2,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        if self._is_real_traffic_test():
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_vlan_10)
            rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
            self.assertEqual(len(rx_packets), 0)

        self._cleanup_basic_oam_setup()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_admin_state_enable_disable(self):

        self._install_downmep()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self._assert_traffic(True)

        # wait for the timeout detection interval to make sure we get RMEP timeout
        sleep(3)

        waiting.wait(lambda: self._check_xray_rmep(
            ma_id1, down_mep_remote_mep_id,
            xray_expected={
                'oam_status': 'OK',
                'state': 'FAILED',    # meaning we got timeout
                'is_active': 'true'
            }), sleep_seconds=0.5, timeout_seconds=1)

        # switch LMEP admin_state to disabled
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.DISABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()
        sleep(1)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._assert_traffic(False)

        # wait for the timeout detection interval to make sure we don't get RMEP timeout
        sleep(3)

        waiting.wait(lambda: self._check_xray_rmep(
            ma_id1, down_mep_remote_mep_id,
            xray_expected={
                'oam_status': 'OK',   # meaning RMEP is correctly configured in OAMP
                'state': 'START',     # meaning we didn't get timeout nor good packets
                'is_active': 'false'  # meaning RMEP timeout functionality is disabled
            }), sleep_seconds=0.5, timeout_seconds=1)

        # switch admin_state back to enabled
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()
        sleep(1)

        self._assert_traffic(True)

        # wait for the timeout detection interval to make sure we get a RMEP timeout
        sleep(3)
        waiting.wait(lambda: self._check_xray_rmep(
            ma_id1, down_mep_remote_mep_id,
            xray_expected={
                'oam_status': 'OK',
                'state': 'FAILED',    # meaning we got timeout
                'is_active': 'true'
            }), sleep_seconds=0.5, timeout_seconds=1)

        self._uninstall_downmep()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_create_lmep_disabled(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        # create LMEP with admin_state disabled
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.DISABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # wait a little bit to make sure that no CCMs are sent
        sleep(2)

        self._assert_traffic(False)

        # wait for the timeout detection interval to make sure we don't get a RMEP timeout
        sleep(3)
        waiting.wait(lambda: self._check_xray_rmep(
            ma_id1, down_mep_remote_mep_id,
            xray_expected={
                'oam_status': 'OK',                # meaning RMEP is correctly configured in OAMP
                'state': 'START',                  # meaning we didn't get timeout nor good packets
                'is_active': 'false'               # meaning RMEP timeout functionality is disabled
            }), sleep_seconds=0.5, timeout_seconds=1)

        # switch admin_state back to enabled
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()
        sleep(1)

        self._assert_traffic(True)

        # wait for the timeout detection interval to make sure we get a RMEP timeout
        sleep(3)
        waiting.wait(lambda: self._check_xray_rmep(
            ma_id1, down_mep_remote_mep_id,
            xray_expected={
                'oam_status': 'OK',
                'state': 'FAILED',    # meaning we got timeout
                'is_active': 'true'
            }), sleep_seconds=0.5, timeout_seconds=1)

        self._uninstall_downmep()

    def test_oam_level_update(self):
        self._prepare_basic_oam_setup()

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep + 1,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()
        sleep(2)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD": 1,
            "MA": 2,
            "Local MEP": 2,
            "Remote MEP": 2
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        xr_data = self.handler.get_xray_stats("/cfm/local_meps")
        assert len(xr_data) == 2
        assert xr_data[0]['md_level'] == str(md_level_down_mep + 1)
        assert xr_data[0]['md_name'] == md_name
        assert xr_data[0]['parent_ma_name'] == ma1_name

        self._cleanup_basic_oam_setup()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_oam_update_pcp(self):
        def pkt_filter_pcp(x):
            return (x.haslayer(Dot1Q) and x.getlayer(Dot1Q).prio == 6)

        self._install_downmep()

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=6,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        if self._is_real_traffic_test():
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_pcp)
            rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep()

    def test_oam_update_interface_vlan(self):
        def pkt_filter_vlan_4020(x):
            return (x.haslayer(Dot1Q) and x.getlayer(Dot1Q).vlan == 4020)

        def pkt_filter_vlan_10(x):
            return (x.haslayer(Dot1Q) and x.getlayer(Dot1Q).vlan == 10)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        if self._is_real_traffic_test():
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_vlan_10)
            rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)

        vlan_data = interfaces_pb.Interface()
        vlan_data.name = iface_names[0]
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.sub.vlan_tag = 4020

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=4020,
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=6,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.api.interface.update_interface(name=iface_names[0], updates=vlan_data)
        self.handler.full_commit()

        # wait for commit to apply in HW
        sleep(5)

        if self._is_real_traffic_test():
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_vlan_4020)
            rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        vlan_data.sub.vlan_tag = WBoxTestCase.global_params["outer_tag"][0]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=6,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.api.interface.update_interface(name=iface_names[0], updates=vlan_data)
        self.handler.full_commit()

        # wait for commit to apply in HW
        sleep(5)

        if self._is_real_traffic_test():
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_vlan_10)
            rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)

        self._uninstall_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_update_vlan_tx_disabled(self):
        def pkt_filter_vlan_4020(x):
            return (x.haslayer(Dot1Q) and x.getlayer(Dot1Q).vlan == 4020)

        def pkt_filter_vlan_10(x):
            return (x.haslayer(Dot1Q) and x.getlayer(Dot1Q).vlan == 10)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self._install_downmep()

        # delete the downMEP and its RMEP
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        ccm_config_tx_disable = deepcopy(ccm_config)
        ccm_config_tx_disable.ccm_enabled = cfm_pb.AdminState.DISABLED
        #this small ccm_interval is used to maximaze the chance of getting a spurious packet
        ccm_config_tx_disable.ccm_interval = cfm_pb.CcmIntervalType.INTERVAL_3_3_MS

        # recreate DownMEP with TX disabled
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config_tx_disable,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # double check that MEP doesn't send CCMs
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_vlan_10)
        rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
        self.assertEqual(len(rx_packets), 0)

        # update the VLAN of the interface to 4020
        vlan_data = interfaces_pb.Interface()
        vlan_data.name = iface_names[0]
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.sub.vlan_tag = 4020

        # update the VLAN of MEP to match interface VLAN (while keeping TX disabled)
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=4020,
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=6,
            md_level=md_level_down_mep,
            ccm_config=ccm_config_tx_disable,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.api.interface.update_interface(name=iface_names[0], updates=vlan_data)

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_vlan_4020)
        self.handler.full_commit()

        # wait for commit to apply in HW
        sleep(3)

        rx_packets = self.handler.data_communicator.rx(timeout=1, number_of_packets=1)
        self.assertEqual(len(rx_packets), 0)

        self._uninstall_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

        vlan_data.sub.vlan_tag = WBoxTestCase.global_params["outer_tag"][0]
        self.handler.api.interface.update_interface(name=iface_names[0], updates=vlan_data)
        self.handler.full_commit()

    def test_oam_delete_and_update_interface_vlan(self):
        self._install_downmep()

        iface_names = WBoxTestCase.global_params["interfaces"]
        vlan_data = interfaces_pb.Interface()
        vlan_data.name = iface_names[0]
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.sub.vlan_tag = 4020

        self.handler.api.interface.update_interface(name=iface_names[0], updates=vlan_data)
        self._uninstall_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        vlan_data.sub.vlan_tag = WBoxTestCase.global_params["outer_tag"][0]
        self.handler.api.interface.update_interface(name=iface_names[0], updates=vlan_data)
        self.handler.full_commit()

    def test_disable_l2_service_and_delete_downmep_phy(self):
        self.check_disable_l2_service_and_delete_downmep()

    def test_disable_l2_service_and_delete_upmep_phy(self):
        self.check_disable_l2_service_and_delete_upmep()

    def test_disable_l2_service_and_delete_mip(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self._install_mip()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "MIP" : 1,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        vlan_data = interfaces_pb.Interface()
        vlan_data.name = iface_names[0]
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.l2_service = False

        self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_names[0])
        self.handler.api.interface.update_interface(name=iface_names[0], updates=vlan_data)

        self._uninstall_mip()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "MIP": 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        vlan_data = interfaces_pb.Interface()
        vlan_data.name = iface_names[0]
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.l2_service = True

        self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_names[0])
        self.handler.api.interface.update_interface(name=iface_names[0], updates=vlan_data)
        self.handler.full_commit()

    def test_delete_interface_and_downmep(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Capture original interface details before deletion to restore them later
        original_iface_resp = self.handler.api.interface.get_interface(name=iface_names[0])
        original_mgmt_id = original_iface_resp.interface.get_interface.data.management_id

        self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_names[0])
        self.handler.api.interface.delete_interface(name=iface_names[0])

        self._uninstall_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Recreate the interface with the same management_id to maintain resource consistency
        # This ensures the setup fixture's teardown can properly clean it up
        original_vlan_tag = WBoxTestCase.global_params["outer_tag"][0]
        original_outer_tpid = WBoxTestCase.global_params["outer_tpid"][0]
        
        # Create interface manually with original management_id
        vlan_data = interfaces_pb.Interface()
        vlan_data.name = f"{WBoxTestCase.WB_IF_1_NAME}.{original_vlan_tag}"
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.sub.vlan_tag = original_vlan_tag
        vlan_data.sub.outer_tpid = original_outer_tpid
        vlan_data.management_id = original_mgmt_id  # Use the original management_id
        vlan_data.internal_index = vlan_data.l3_internal_index = vlan_data.management_id
        vlan_data.admin_status = interfaces_pb.ON
        vlan_data.l2_service = True
        vlan_data.pcp_preserve = True
        
        self.handler.api.interface.create_interface(
            type=interfaces_pb.SUB_INTERFACE, 
            name=vlan_data.name, 
            data=vlan_data
        )
        self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=vlan_data.management_id)

        self.handler.full_commit()

    @devvm_test()
    def test_delete_interface_and_recreate_downmep(self):
        """ Delete interface and recreate + update LMEP in same commit
        """
        # Create interface
        iface_names = WBoxTestCase.global_params["interfaces"]
        interface = {"parent": WBoxTestCase.WB_IF_1_NAME, "vlan_tag": 1001, "outer_tpid": 0x8100,
                     "l2_service": True, 'pcp_preserve': True}
        interfaces_addict=[Dict(interface)]
        vlan_1001 = self._create_vlans(interfaces_addict)[0]
        self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=vlan_1001.management_id)

        # Create CFM setup with newly created interface
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=vlan_1001.name,
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=1001,
            outer_tpid=0x8100,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Remove interface and recreate LMEP with new interface
        # Send LMEP update in same commit
        self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=vlan_1001.name)
        self.handler.api.interface.delete_interface(name=vlan_1001.name)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        sleep(3)

        ifaces_xray = self.handler.get_xray_stats("/cfm/interfaces")
        assert len(ifaces_xray) == 1
        assert ifaces_xray[0] == {'interface_name': 'ge100-0/0/5.10', 'count_config': '1', 'count_oper': '1'}

        self._uninstall_downmep()

    def test_delete_interface_and_upmep(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self._install_upmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Capture original interface details before deletion to restore them later
        original_iface_resp = self.handler.api.interface.get_interface(name=iface_names[1])
        original_mgmt_id = original_iface_resp.interface.get_interface.data.management_id

        self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_names[1])
        self.handler.api.interface.delete_interface(name=iface_names[1])

        self._uninstall_upmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Recreate the interface with the same management_id to maintain resource consistency
        # This ensures the setup fixture's teardown can properly clean it up
        original_vlan_tag = WBoxTestCase.global_params["outer_tag"][1]  # Second interface
        original_outer_tpid = WBoxTestCase.global_params["outer_tpid"][1]
        
        # Create interface manually with original management_id
        vlan_data = interfaces_pb.Interface()
        vlan_data.name = f"{WBoxTestCase.WB_IF_1_NAME}.{original_vlan_tag}"
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.sub.vlan_tag = original_vlan_tag
        vlan_data.sub.outer_tpid = original_outer_tpid
        vlan_data.management_id = original_mgmt_id  # Use the original management_id
        vlan_data.internal_index = vlan_data.l3_internal_index = vlan_data.management_id
        vlan_data.admin_status = interfaces_pb.ON
        vlan_data.l2_service = True
        vlan_data.pcp_preserve = True
        
        self.handler.api.interface.create_interface(
            type=interfaces_pb.SUB_INTERFACE, 
            name=vlan_data.name, 
            data=vlan_data
        )
        self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=vlan_data.management_id)

        self.handler.full_commit()

    def test_oam_create_interface_and_endpoints_in_the_same_commit(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        # Capture original interface details before deletion to restore them later
        original_iface_resp = self.handler.api.interface.get_interface(name=iface_names[0])
        original_mgmt_id = original_iface_resp.interface.get_interface.data.management_id

        # remove the interface
        self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_names[0])
        self.handler.api.interface.delete_interface(name=iface_names[0])
        self.handler.full_commit()

        # Recreate the interface with the same management_id to maintain resource consistency
        original_vlan_tag = WBoxTestCase.global_params["outer_tag"][0]
        original_outer_tpid = WBoxTestCase.global_params["outer_tpid"][0]
        
        # Create interface manually with original management_id
        vlan_data = interfaces_pb.Interface()
        vlan_data.name = f"{WBoxTestCase.WB_IF_1_NAME}.{original_vlan_tag}"
        vlan_data.lag.name = WBoxTestCase.WB_IF_1_NAME
        vlan_data.sub.vlan_tag = original_vlan_tag
        vlan_data.sub.outer_tpid = original_outer_tpid
        vlan_data.management_id = original_mgmt_id  # Use the original management_id
        vlan_data.internal_index = vlan_data.l3_internal_index = vlan_data.management_id
        vlan_data.admin_status = interfaces_pb.ON
        vlan_data.l2_service = True
        vlan_data.pcp_preserve = True
        
        self.handler.api.interface.create_interface(
            type=interfaces_pb.SUB_INTERFACE, 
            name=vlan_data.name, 
            data=vlan_data
        )
        self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=vlan_data.management_id)

        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep()

    def test_add_remove_same_commit(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id2, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id2,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id2, mep_id=down_mep_mep_id)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id + 1,
            md_id=md_id,
            ma_id=ma_id2,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.full_commit()
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id2, mep_id=down_mep_mep_id + 1)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id2)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    def test_move_lmep_to_different_ma(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id2, maid48=bytes(maid2), md_id=md_id, oam_id=group_id2,
                                          ma_name=ma2_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        if not self._is_real_traffic_test():
            event_if_down = cfm_events.CfmEvent(
                event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_INTERFACE_DOWN,
                rmep_hw_id=self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id)
            )

            event_if_up = cfm_events.CfmEvent(
                event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_INTERFACE_UP,
                rmep_hw_id=self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id)
            )

            # Inject event to trigger MACstatus defect
            self.handler.wb_api.cfm_events_injector.inject_event(event_if_down)

            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
                "fng_state" : 'fng-defect-reported',
                "highest_priority_defect" : 'def-mac-status',
                "defects" : ['def-mac-status']
            }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            # Inject event to clear MACstatus defect
            self.handler.wb_api.cfm_events_injector.inject_event(event_if_up)

            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
                "fng_state" : 'fng-reset'
            }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id + 1,
            md_id=md_id,
            ma_id=ma_id2,
            group_oam_id=group_id2,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 1,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        xr_data = self.handler.get_xray_stats("/cfm/local_meps")
        assert len(xr_data) == 1
        assert xr_data[0]['oam_status'] == 'OK'

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id2, str(down_mep_mep_id + 1)], {
            "fng_state" : 'fng-reset',
            "highest_priority_defect" : 'none',
            "defects" : None
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # check that counters increments after endpoint move
        if self._is_real_traffic_test():
            (counters, counters_summary) = self._get_current_counters(lmep_config={
                "md_id" : md_id,
                "ma_id" : ma_id2,
                "mep_id" : str(down_mep_mep_id + 1)})

            lmep_oper_expected = {}
            for key in counters.keys():
                lmep_oper_expected[key] = None

            lmep_oper_expected['ccm_out'] = 2

            lmep_oper_summary_expected = {}
            for key in counters_summary.keys():
                lmep_oper_summary_expected[key] = None

            lmep_oper_summary_expected['ccm_out'] = 2

            waiting.wait(lambda: self._assert_operdb_lmep_cnt(
                lmep_config= {
                    "md_id" : md_id,
                    "ma_id" : ma_id2,
                    "mep_id" : str(down_mep_mep_id + 1)
                    },
                current_lmep=counters,
                current_summary_lmep=counters_summary,
                lmep_oper_expected=lmep_oper_expected,
                lmep_oper_summary_expected=lmep_oper_summary_expected
            ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id2, mep_id=down_mep_mep_id + 1)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id2)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    @devvm_test()
    def test_move_ma_to_new_md(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=25)

        md_id2 = "md2"
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.create_md(md_id=md_id2, req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id2, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id2,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=25)

        xr_data = self.handler.get_xray_stats("/cfm/local_meps")
        assert len(xr_data) == 1
        assert xr_data[0]['oam_status'] == 'OK'

        # cleanup
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id2, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id2, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.wb_api.cfm.delete_md(md_id=md_id2)
        self.handler.full_commit()

    @devvm_test()
    def test_replace_ma_same_oam_id(self):
        """ Verify that an MA can be correctly replaced.
            I.e.: same oam_id, but different keys
        """
        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=25)

        # Remove old MA and create new MA with same oam_id
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id2, maid48=bytes(maid2), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma2_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id2,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()
        sleep(2)

        # Verify that the new MA's config is correctly applied
        ma_xray = self.handler.get_xray_stats("/cfm/ma")[0]
        assert ma_xray['ma_id'] == ma_id2
        assert ma_xray['ma_name'] == ma2_name

        # Cleanup
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id2, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id2)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_recreate_group_local_and_remote_update_same_commit(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        # install downmep
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # recreate ma
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                    ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                    req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                    ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                    req_type=cfm_pb.CreateRequestType.UPDATE)

        # update l/rmep
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # cleanup
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

    def test_update_rmeps(self):
        iface_names = WBoxTestCase.global_params["interfaces"]
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep - 1,
            ccm_config=ccm_config,
            remote_mep_ids=[7000, 7001, 7002],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=5)

        ccm_config_update = deepcopy(ccm_config)
        ccm_config_update.ccm_interval = 4
        ccm_config_update.loss_threshold = 6

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep - 1,
            ccm_config=ccm_config_update,
            remote_mep_ids=[7000, 7001],
            update_rmeps=True,
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=5)

        rmep_data = self.handler.get_xray_stats("/cfm/remote_meps")
        rmep_timeout = self._ccm_interval_to_ms(ccm_config_update.ccm_interval) * ccm_config_update.loss_threshold
        for rmep in rmep_data:
            assert rmep['oam_status'] == 'OK'
            assert rmep['timeout_ms'] == str(rmep_timeout)

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    def test_complex_commit(self):
        self._prepare_basic_oam_setup()

        lmep_3_id = down_mep_oam_id + 8
        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep - 1,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=up_mep_oam_id,
            mep_id=up_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id2,
            group_oam_id=group_id2,
            interface_name=iface_names[1],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][1],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][1],
            ccm_ltm_priority=5,
            md_level=md_level_up_mep - 1,
            ccm_config=ccm_config,
            remote_mep_ids=[up_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=up_mep_oam_id,
            mep_id=up_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id2,
            group_oam_id=group_id2,
            interface_name=iface_names[1],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][1],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][1],
            ccm_ltm_priority=5,
            md_level=md_level_up_mep - 1,
            ccm_config=ccm_config,
            req_type=cfm_pb.CreateRequestType.UPDATE)

        maid3 = bytes(CCM.create_maid(ma_name="xy", ma_name_format=3))

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid3), md_id=md_id, oam_id=group_id1,
                                          ma_name="xy", md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=lmep_3_id,
            mep_id=down_mep_mep_id + 1,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep - 1,
            ccm_config=ccm_config,
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=up_mep_oam_id,
            mep_id=up_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id2,
            group_oam_id=group_id2,
            interface_name=iface_names[1],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][1],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][1],
            ccm_ltm_priority=5,
            md_level=md_level_up_mep - 1,
            ccm_config=ccm_config,
            remote_mep_ids=[up_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 3,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id + 1)
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id2, mep_id=up_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id2)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_update_period_on_tx_disabled(self):
        def validate_wrong_interval(expected_out_pkts, expected_in_pkts, expected_wrong_interval_pkts):
            ccm_pkt_down_wrong_interval = (
                Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
                Dot1Q(vlan=10, prio=5, type=0x8902) /
                CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
                CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
            )

            (counters, counters_summary) = self._get_current_counters(lmep_config={
            "md_id" : md_id,
            "ma_id" : ma_id1,
            "mep_id" : str(down_mep_mep_id)})

            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME,
                                              packet=ccm_pkt_down_wrong_interval,
                                              number_of_packets=1)

            lmep_oper_expected = {}
            for key in counters.keys():
                lmep_oper_expected[key] = None

            if (expected_out_pkts):
                lmep_oper_expected['ccm_out'] = expected_out_pkts
            if (expected_in_pkts):
                lmep_oper_expected['ccm_in'] = expected_in_pkts
            if (expected_wrong_interval_pkts):
                lmep_oper_expected['ccms_wrong_interval'] = expected_wrong_interval_pkts

            lmep_oper_summary_expected = {}
            for key in counters_summary.keys():
                lmep_oper_summary_expected[key] = None

            if (expected_out_pkts):
                lmep_oper_summary_expected['ccm_out'] = expected_out_pkts
            if (expected_in_pkts):
                lmep_oper_summary_expected['ccm_in'] = expected_in_pkts
            if (expected_wrong_interval_pkts):
                lmep_oper_summary_expected['ccms_wrong_interval'] = expected_wrong_interval_pkts

            waiting.wait(lambda: self._assert_operdb_lmep_cnt(
                lmep_config= {
                    "md_id" : md_id,
                    "ma_id" : ma_id1,
                    "mep_id" : str(down_mep_mep_id)
                    },
                current_lmep=counters,
                current_summary_lmep=counters_summary,
                lmep_oper_expected=lmep_oper_expected,
                lmep_oper_summary_expected=lmep_oper_summary_expected
            ), sleep_seconds=1, timeout_seconds=wait_timeout_s)


        iface_names = WBoxTestCase.global_params["interfaces"]

        # install downmep
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        # create downmep with default configuration
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        validate_wrong_interval(1, 1, 0)

        # disable downmep tx
        ccm_config_update = deepcopy(ccm_config)
        ccm_config_update.ccm_enabled = cfm_pb.AdminState.DISABLED

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config_update,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # wait for tx to stop
        sleep(2)

        validate_wrong_interval(0, 1, 0)

        # update downmep interval to 10s
        ccm_config_update.ccm_interval = cfm_pb.CcmIntervalType.INTERVAL_10_SEC

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config_update,
            remote_mep_ids=[down_mep_remote_mep_id],
            update_rmeps=True,
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._check_xray_rmep(
            ma_id1, down_mep_remote_mep_id,
            xray_expected={
                'timeout_ms': '30000'
            }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # wait for tx period update
        sleep(1)

        validate_wrong_interval(0, 1, 1)

        # cleanup
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_update_period_and_recreate(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        # install downmep
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        # create downmep with default configuration
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # recreate downmep
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # update downmep interval to 10s
        ccm_config_update = deepcopy(ccm_config)
        ccm_config_update.ccm_interval = cfm_pb.CcmIntervalType.INTERVAL_10_SEC

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config_update,
            remote_mep_ids=[down_mep_remote_mep_id],
            update_rmeps=True,
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._check_xray_rmep(
            ma_id1, down_mep_remote_mep_id,
            xray_expected={
                'timeout_ms': '30000'
            }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # recreate downmep
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # cleanup
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    def test_wrong(self):
        pass

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_wrong_level(self):
        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        (counters, counters_summary) = self._get_current_counters(lmep_config={
            "md_id" : md_id,
            "ma_id" : ma_id1,
            "mep_id" : str(down_mep_mep_id)})

        md_level_lower = 1
        ccm_pkt_down_wrong_level_lower = (
            Ether(dst=f'01:80:c2:00:00:3{md_level_lower}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=10, prio=5, type=0x8902) /
            CFM(md_level=md_level_lower, opcode=CCM.opcode) /
            CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
        )
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down_wrong_level_lower, number_of_packets=1)

        lmep_oper_expected = {}
        for key in counters.keys():
            lmep_oper_expected[key] = None

        lmep_oper_expected['wrong_level'] = 1
        lmep_oper_expected['ccm_out'] = 1

        lmep_oper_summary_expected = {}
        for key in counters_summary.keys():
            lmep_oper_summary_expected[key] = None

        lmep_oper_summary_expected['ccm_out'] = 1
        lmep_oper_summary_expected['wrong_level'] = 1

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)
                },
            current_lmep=counters,
            current_summary_lmep=counters_summary,
            lmep_oper_expected=lmep_oper_expected,
            lmep_oper_summary_expected=lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_wrong_interval(self):
        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        (counters, counters_summary) = self._get_current_counters(lmep_config={
            "md_id" : md_id,
            "ma_id" : ma_id1,
            "mep_id" : str(down_mep_mep_id)})

        ccm_pkt_down_wrong_interval = (
            Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=10, prio=5, type=0x8902) /
            CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
            CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_10S, maid=maid1)
        )
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down_wrong_interval, number_of_packets=1)

        lmep_oper_expected = {}
        for key in counters.keys():
            lmep_oper_expected[key] = None

        lmep_oper_expected['ccm_out'] = 1
        lmep_oper_expected['ccm_in'] = 1
        lmep_oper_expected['ccms_wrong_interval'] = 1

        lmep_oper_summary_expected = {}
        for key in counters_summary.keys():
            lmep_oper_summary_expected[key] = None

        lmep_oper_summary_expected['ccm_out'] = 1
        lmep_oper_summary_expected['ccm_in'] = 1
        lmep_oper_summary_expected['ccms_wrong_interval'] = 1

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)
                },
            current_lmep=counters,
            current_summary_lmep=counters_summary,
            lmep_oper_expected=lmep_oper_expected,
            lmep_oper_summary_expected=lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_wrong_rmep(self):
        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        (counters, counters_summary) = self._get_current_counters(lmep_config={
            "md_id" : md_id,
            "ma_id" : ma_id1,
            "mep_id" : str(down_mep_mep_id)})

        ccm_pkt_down_wrong_rmep = (
            Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=10, prio=5, type=0x8902) /
            CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
            CCM(mep_id=down_mep_remote_mep_id + 1, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
        )
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down_wrong_rmep, number_of_packets=1)

        lmep_oper_expected = {}
        for key in counters.keys():
            lmep_oper_expected[key] = None

        lmep_oper_expected['ccm_out'] = 1
        lmep_oper_expected['ccm_in'] = 1
        lmep_oper_expected['ccms_wrong_rmep'] = 1

        lmep_oper_summary_expected = {}
        for key in counters_summary.keys():
            lmep_oper_summary_expected[key] = None

        lmep_oper_summary_expected['ccm_out'] = 1
        lmep_oper_summary_expected['ccm_in'] = 1
        lmep_oper_summary_expected['ccms_wrong_rmep'] = 1

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)
                },
            current_lmep=counters,
            current_summary_lmep=counters_summary,
            lmep_oper_expected=lmep_oper_expected,
            lmep_oper_summary_expected=lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_wrong_maid(self):
        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        ccm_pkt_down_wrong_maid = (
            Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=10, prio=5, type=0x8902) /
            CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
            CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid2)
        )

        (counters, counters_summary) = self._get_current_counters(lmep_config={
            "md_id" : md_id,
            "ma_id" : ma_id1,
            "mep_id" : str(down_mep_mep_id)})

        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down_wrong_maid, number_of_packets=1)

        lmep_oper_expected = {}
        for key in counters.keys():
            lmep_oper_expected[key] = None

        lmep_oper_expected['ccm_out'] = 1
        lmep_oper_expected['ccm_in'] = 1
        lmep_oper_expected['ccms_wrong_maid'] = 1

        lmep_oper_summary_expected = {}
        for key in counters_summary.keys():
            lmep_oper_summary_expected[key] = None

        lmep_oper_summary_expected['ccm_out'] = 1
        lmep_oper_summary_expected['ccm_in'] = 1
        lmep_oper_summary_expected['ccms_wrong_maid'] = 1

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)
                },
            current_lmep=counters,
            current_summary_lmep=counters_summary,
            lmep_oper_expected=lmep_oper_expected,
            lmep_oper_summary_expected=lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

    @pytest.mark.extended_tests
    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_good_packet(self):
        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        ccm_good_pkt_count = int(self.cfm_read_counters()['cfm_ccm_good_packet'])

        (counters, counters_summary) = self._get_current_counters(lmep_config={
            "md_id" : md_id,
            "ma_id" : ma_id1,
            "mep_id" : str(down_mep_mep_id)})

        ccm_pkt_down= (
            Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=10, prio=5, type=0x8902) /
            CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
            CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
        )

        # punt good packet period is set to 19s by default (this is maximum value supported in OAMP)
        # so if 40 packets are sent at 1s interval, good packet punting expectation is:
        # 1st packet at first packet send, 2nd packet at 19s and 3rd packet at 38s
        packet_to_send_cnt = 40
        expected_punted_packets = 3

        for _ in range(packet_to_send_cnt):
            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down, number_of_packets=1)
            sleep(1)

        lmep_oper_expected = {}
        for key in counters.keys():
            lmep_oper_expected[key] = None

        lmep_oper_expected['ccm_in'] = packet_to_send_cnt

        lmep_oper_summary_expected = {}
        for key in counters_summary.keys():
            lmep_oper_summary_expected[key] = None

        lmep_oper_summary_expected['ccm_in'] = packet_to_send_cnt

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)
                },
            current_lmep=counters,
            current_summary_lmep=counters_summary,
            lmep_oper_expected=lmep_oper_expected,
            lmep_oper_summary_expected=lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_operdb_rmep(
            rmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id),
                "rmep_id" : str(down_mep_remote_mep_id)
                },
            rmep_oper_expected= {
                "mac_address" : "00:01:02:03:04:05"
                },
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_good_packets(ccm_good_pkt_count + expected_punted_packets),
                                     sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep()

    @pytest.mark.skipif(not IS_NCP3, reason="port speed change is supported on ncp3 devices")
    def test_port_speed_change(self):
        logger.info("Running port speed change tests...")

    @pytest.mark.skipif(not IS_NCP3, reason="port speed change is supported on ncp3 devices")
    def test_port_speed_change_on_mep(self):

        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_400GB,
                                  with_commit=False)

        self._uninstall_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # configure port speed back to 100
        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_100GB)
        self.set_iface_oper_state(iface_name=self.WB_IF_1_NAME, is_oper_up=True, is_link_up=True)
        sleep(5)

    @pytest.mark.skipif(not IS_NCP3, reason="port speed change is supported on ncp3 devices")
    def test_port_speed_change_on_mip(self):

        self._install_mip()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "MIP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_400GB,
                                  with_commit=False)

        self._uninstall_mip()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "MIP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # configure port speed back to 100
        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_100GB)
        self.set_iface_oper_state(iface_name=self.WB_IF_1_NAME, is_oper_up=True, is_link_up=True)
        sleep(5)

    @pytest.mark.skipif(not IS_NCP3, reason="port speed change is supported on ncp3 devices")
    def test_port_speed_change_with_traffic(self):
        def _check_traffic():
            # self.handler.execute_command("rx tracing enable")
            (counters, counters_summary) = self._get_current_counters(lmep_config={
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)})

            ccm_pkt_down = (
                Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
                Dot1Q(vlan=10, prio=5, type=0x8902) /
                CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
                CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
            )

            pkts = 5
            for _ in range(pkts):
                self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down, number_of_packets=1)
                sleep(1)

            lmep_oper_expected = {}
            for key in counters.keys():
                lmep_oper_expected[key] = None

            lmep_oper_expected['ccm_out'] = pkts
            lmep_oper_expected['ccm_in'] = pkts

            lmep_oper_summary_expected = {}
            for key in counters_summary.keys():
                lmep_oper_summary_expected[key] = None

            lmep_oper_summary_expected['ccm_out'] = pkts
            lmep_oper_summary_expected['ccm_in'] = pkts

            waiting.wait(lambda: self._assert_operdb_lmep_cnt(
                lmep_config= {
                    "md_id" : md_id,
                    "ma_id" : ma_id1,
                    "mep_id" : str(down_mep_mep_id)
                    },
                current_lmep=counters,
                current_summary_lmep=counters_summary,
                lmep_oper_expected=lmep_oper_expected,
                lmep_oper_summary_expected=lmep_oper_summary_expected
            ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

            waiting.wait(lambda: self._assert_operdb_rmep(
                rmep_config= {
                    "md_id" : md_id,
                    "ma_id" : ma_id1,
                    "mep_id" : str(down_mep_mep_id),
                    "rmep_id" : str(down_mep_remote_mep_id)
                    },
                rmep_oper_expected= {
                    "mac_address" : "00:01:02:03:04:05"
                    },
            ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

            # self.handler.execute_command("rx tracing disable")

        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        _check_traffic()

        self._send_recreate_downmep()

        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_400GB)
        self.set_iface_oper_state(iface_name=self.WB_IF_1_NAME, is_oper_up=True, is_link_up=True)
        sleep(5)

        self._send_recreate_downmep()

        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_100GB)
        self.set_iface_oper_state(iface_name=self.WB_IF_1_NAME, is_oper_up=True, is_link_up=True)
        sleep(5)

        _check_traffic()

        self._uninstall_downmep()

    @remote_test()
    def test_oam_update_direction(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        self._install_downmep()

        event = threading.Event()
        packet_thread = threading.Thread(target=self.send_ccm_packets_thread, args=(event, 0.00001))
        packet_thread.start()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                {"fng_state" : 'fng-defect-reported'}),
                                     sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                {"fng_state" : 'fng-defect-clearing'}),
                                     sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        event.set()
        packet_thread.join()

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                {"fng_state" : 'fng-defect-reported'}),
                                     sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep()

    @pytest.mark.extended_tests
    @remote_test()
    def test_update_maid_with_traffic_rdi_set(self):
        def pkt_filter_no_rdi(x):
            return (x.getlayer(Ether).dst == f'01:80:c2:00:00:3{md_level_down_mep}' and x.haslayer(Dot1Q) and x.getlayer(Dot1Q).vlan == 10 and x.haslayer(Raw) and x.getlayer(CCM).rdi == 0x0)

        def pkt_filter_has_rdi(x):
            return (x.getlayer(Ether).dst == f'01:80:c2:00:00:3{md_level_down_mep}' and x.haslayer(Dot1Q) and x.getlayer(Dot1Q).vlan == 10 and x.haslayer(Raw) and x.getlayer(CCM).rdi == 0x1)

        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        logger.info("Sending CCM packets with RDI")
        # send rdi bit from remote
        event = threading.Event()
        packet_thread = threading.Thread(target=self.send_ccm_with_rdi_packets_thread, args=(event, 1))
        packet_thread.start()

        sleep(5)

        try:
            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                    {"fng_state" : 'fng-defect-reported',
                                                                     "defects" : ['def-rdi-ccm']}),
                                         sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            # there should be no RDI into the tx packet
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_no_rdi)
            rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)

        except Exception as e:
            raise e
        finally:
            event.set()
            packet_thread.join()

        logger.info("Sending CCM packets without RDI")
        # send rdi bit from remote
        event = threading.Event()
        packet_thread = threading.Thread(target=self.send_ccm_packets_thread, args=(event, 1))
        packet_thread.start()

        sleep(5)

        try:
            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                    {"fng_state" : 'fng-reset'}),
                                         sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            # there should be no RDI into the tx packet
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_no_rdi)
            rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)
        except Exception as e:
            raise e
        finally:
            event.set()
            packet_thread.join()

        logger.info("Sending CCM packets with RDI")
        # send rdi bit from remote
        event = threading.Event()
        packet_thread = threading.Thread(target=self.send_ccm_with_rdi_packets_thread, args=(event, 1))
        packet_thread.start()

        sleep(5)

        try:
            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                    {"fng_state" : 'fng-defect-reported',
                                                                     "defects" : ['def-rdi-ccm']}),
                                         sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            # there should be no RDI into the tx packet
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_no_rdi)
            rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)
        except Exception as e:
            event.set()
            packet_thread.join()
            raise e

        logger.info("Switch MAID")
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid2), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Send wrong CCM packet to trigger xCon error, which should be the highest priority defect
        try:
            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                    {"fng_state" : 'fng-defect-reported',
                                                                     "defects" : ['def-remote-ccm', 'def-xcon-ccm']}),
                                         sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            # there should be RDI into the tx packet
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_has_rdi)
            rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)
        except Exception as e:
            raise e
        finally:
            event.set()
            packet_thread.join()

        logger.info("Sending CCM packets without RDI")

        event = threading.Event()
        packet_thread = threading.Thread(target=self.send_ccm_packets_thread, args=(event, 1))
        packet_thread.start()

        sleep(5)

        logger.info("Switch back MAID")

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        try:
            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                    {"fng_state" : 'fng-reset'}),
                                         sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            # there should be no RDI into the tx packet
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_no_rdi)
            rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)
        except Exception as e:
            raise e
        finally:
            event.set()
            packet_thread.join()

        logger.info("Sending CCM packets with RDI")
        # send rdi bit from remote
        event = threading.Event()
        packet_thread = threading.Thread(target=self.send_ccm_with_rdi_packets_thread, args=(event, 1))
        packet_thread.start()

        try:
            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                    {"fng_state" : 'fng-defect-reported',
                                                                     "defects" : ['def-rdi-ccm']}),
                                         sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            # there should be no RDI into the tx packet
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_no_rdi)
            rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)
        except Exception as e:
            raise e
        finally:
            event.set()
            packet_thread.join()

        logger.info("Sending CCM packets without RDI")

        event = threading.Event()
        packet_thread = threading.Thread(target=self.send_ccm_packets_thread, args=(event, 1))
        packet_thread.start()

        try:
            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                    {"fng_state" : 'fng-reset'}),
                                         sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            # there should be no RDI into the tx packet
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_no_rdi)
            rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)
        except Exception as e:
            raise e
        finally:
            event.set()
            packet_thread.join()

        self._uninstall_downmep()

    @remote_test()
    def test_update_maid_with_traffic_no_rdi(self):
        def pkt_filter_no_rdi(x):
            return (x.getlayer(Ether).dst == f'01:80:c2:00:00:3{md_level_down_mep}' and x.haslayer(Dot1Q)
                    and x.getlayer(Dot1Q).vlan == 10 and x.haslayer(Raw) and x.getlayer(CCM).rdi == 0x0)

        def pkt_filter_has_rdi(x):
            return (x.getlayer(Ether).dst == f'01:80:c2:00:00:3{md_level_down_mep}' and x.haslayer(Dot1Q)
                    and x.getlayer(Dot1Q).vlan == 10 and x.haslayer(Raw) and x.getlayer(CCM).rdi == 0x1)
        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        logger.info("Sending CCM packets")
        event = threading.Event()
        packet_thread = threading.Thread(target=self.send_ccm_packets_thread, args=(event, 1))
        packet_thread.start()

        sleep(5)

        try:
            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                    {"fng_state" : 'fng-reset'}),
                                         sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            # there should be no RDI into the tx packet
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_no_rdi)
            rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)
        except Exception as e:
            event.set()
            packet_thread.join()
            raise e

        logger.info("Switch MAID")
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid2), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Send wrong CCM packet to trigger xCon error, which should be the highest priority defect
        try:
            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                    {"fng_state" : 'fng-defect-reported',
                                                                     "defects" : ['def-remote-ccm', 'def-xcon-ccm']}),
                                         sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            waiting.wait(lambda: self._assert_operdb_rmep(
                                            rmep_config= {
                                                "md_id" : md_id,
                                                "ma_id" : ma_id1,
                                                "mep_id" : str(down_mep_mep_id),
                                                "rmep_id" : str(down_mep_remote_mep_id)
                                                },
                                            rmep_oper_expected= {
                                                "rmep_state" : 'rmep-failed'
                                                },
                                        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

            # there should be RDI into the tx packet
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_has_rdi)
            rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)
        except Exception as e:
            event.set()
            packet_thread.join()
            raise e

        logger.info("Switch back MAID")

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        try:
            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                    {"fng_state" : 'fng-reset'}),
                                         sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            # there should be no RDI into the tx packet
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_no_rdi)
            rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)
        except Exception as e:
            raise e
        finally:
            event.set()
            packet_thread.join()

        self._uninstall_downmep()

    @remote_test()
    def test_update_maid_with_no_traffic(self):
        def pkt_filter_has_rdi(x):
            return (x.getlayer(Ether).dst == f'01:80:c2:00:00:3{md_level_down_mep}' and x.haslayer(Dot1Q)
                    and x.getlayer(Dot1Q).vlan == 10 and x.haslayer(Raw) and x.getlayer(CCM).rdi == 0x1)

        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                {"fng_state" : 'fng-defect-reported',
                                                                    "defects" : ['def-remote-ccm']}),
                                        sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_operdb_rmep(
                                        rmep_config= {
                                            "md_id" : md_id,
                                            "ma_id" : ma_id1,
                                            "mep_id" : str(down_mep_mep_id),
                                            "rmep_id" : str(down_mep_remote_mep_id)
                                            },
                                        rmep_oper_expected= {
                                            "rmep_state" : 'rmep-failed'
                                            },
                                    ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        # there should be RDI into the tx packet
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_has_rdi)
        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
        self.assertEqual(len(rx_packets), 1)

        logger.info("Switch MAID")
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid2), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                {"fng_state" : 'fng-defect-reported',
                                                                    "defects" : ['def-remote-ccm']}),
                                        sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_operdb_rmep(
                                        rmep_config= {
                                            "md_id" : md_id,
                                            "ma_id" : ma_id1,
                                            "mep_id" : str(down_mep_mep_id),
                                            "rmep_id" : str(down_mep_remote_mep_id)
                                            },
                                        rmep_oper_expected= {
                                            "rmep_state" : 'rmep-failed'
                                            },
                                    ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        # there should be RDI into the tx packet
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_has_rdi)
        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
        self.assertEqual(len(rx_packets), 1)

        logger.info("Switch back MAID")

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)],
                                                                {"fng_state" : 'fng-defect-reported',
                                                                    "defects" : ['def-remote-ccm']}),
                                        sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_operdb_rmep(
                                        rmep_config= {
                                            "md_id" : md_id,
                                            "ma_id" : ma_id1,
                                            "mep_id" : str(down_mep_mep_id),
                                            "rmep_id" : str(down_mep_remote_mep_id)
                                            },
                                        rmep_oper_expected= {
                                            "rmep_state" : 'rmep-failed'
                                            },
                                    ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        # there should be RDI into the tx packet
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_has_rdi)
        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
        self.assertEqual(len(rx_packets), 1)

        self._uninstall_downmep()


    def _validate_initiator(self, sess_type):
        first_id = CFM_START_MEP_OAM_ID
        packet_count = 5
        sessions_allocated = self._get_oam_initiator_summary_xray("allocated")
        session_id = 0
        dst_mac = "00:11:22:33:44:55"

        self._install_downmep(oam_id = first_id)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        # test missing MEP
        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = sess_type,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id + 3),
            dest = cfm_initiator_pb.SessionDest(dmac=dst_mac),
            interval_ms = 1 * 1000,
            pkt_count = packet_count,
            frame_size = 234,
            pcp = 7,
        )
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_ERR_MISSING_MEP)
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
        }))

        # test disabled MEP
        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=first_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.DISABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()
        sleep(2)
        # TODO: this is not working
        # waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
        #     "admin_state" : 'disabled',
        # }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = sess_type,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id),
            dest = cfm_initiator_pb.SessionDest(dmac=dst_mac),
            interval_ms = 1 * 1000,
            pkt_count = packet_count,
            frame_size = 234,
            pcp = 7,
        )
        self.assertEqual(res.cfm_initiator.start_resp.status,
                         cfm_initiator_pb.SessionStartStatus.START_ERR_DISABLED_MEP)
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
        }))

        self.handler.wb_api.cfm.create_lmep(
            oam_id=first_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)
        self.handler.full_commit()
        sleep(2)

        # test ok mep
        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = sess_type,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id),
            dest = cfm_initiator_pb.SessionDest(dmac=dst_mac),
            interval_ms = 1 * 1000,
            pkt_count = packet_count,
            frame_size = 234,
            pcp = 7,
        )

        # skip if session unsupported
        if res.cfm_initiator.start_resp.status == cfm_initiator_pb.SessionStartStatus.START_ERR_UNSUPPORTED:
            self._uninstall_downmep(delete_md = True)
            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 0,
                "MA" : 0,
                "Local MEP" : 0,
                "Remote MEP" : 0
            }), sleep_seconds=0.5, timeout_seconds=5)
            return
        # expect OK message
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        session_id = res.cfm_initiator.start_resp.id
        logger.info(f"Session_id {res.cfm_initiator.start_resp.id} status {res.cfm_initiator.start_resp.status}")
        sessions_allocated += 1
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 1,
            "allocated" : sessions_allocated,
        }))
        self._validate_test_info(sess_type, packet_count, dst_mac)

        # Start session on same mep and same type; expect error
        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = sess_type,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id),
            dest = cfm_initiator_pb.SessionDest(dmac="55:44:33:22:11:00"),
            interval_ms = 10 * 1000,
            pkt_count = packet_count + 10,
            frame_size = 456,
            pcp = 6,
        )

        # expect error message
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_ERR_EXISTS)
        logger.info(f"Session_id {res.cfm_initiator.start_resp.id} status {res.cfm_initiator.start_resp.status}")
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 1,
            "allocated" : sessions_allocated,
        }))

        # Send stop session in middle of active session
        for count in range(3):
            sleep(1)
            result = self._on_demand_measurement_stats(md_id, ma_id1, str(down_mep_mep_id), sess_type)
            logger.info(f"=== iter {count}, stats: {result.to_json()}")
            self.assertTrue(result.measurement_validity in ('valid', 'incomplete'))

        # send stop
        res = self.handler.wb_api.cfm_initiator.stop_req(id=session_id)
        self.assertEqual(res.cfm_initiator.stop_resp.status, cfm_initiator_pb.SessionStopStatus.STOP_OK)
        self.assertTrue(self._assert_oam_initiator_summary_xray({"active" : 0}))

        result = self._on_demand_measurement_stats(md_id, ma_id1, str(down_mep_mep_id), sess_type)
        waiting.wait(lambda: 'invalid' == self._on_demand_measurement_stats( #stopped above
            md_id, ma_id1, str(down_mep_mep_id), sess_type).measurement_validity,
                     sleep_seconds=0.5, timeout_seconds=2)

        # wait for session to be freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=3)

        self._uninstall_downmep(delete_md = True)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)


    def _validate_test_info(self, op: cfm_initiator_pb.SessionOpcode,
                            pkt_cnt: int, dst_mac: str) -> None:
        ti_path = on_demand_test_info(op)
        lmep_id = str(down_mep_mep_id)

        with DBClientAPI() as corm_api:
            waiting.wait(lambda: len(corm_api.get_by_path(ti_path, [md_id, ma_id1, lmep_id])) > 0,
                         sleep_seconds=0.5, timeout_seconds=2)

            sess_orm = corm_api.get_by_path(ti_path, [md_id, ma_id1, lmep_id])
            ti = sess_orm[0].to_json()
            src_mac = self._get_mac_address({'md_id': md_id, 'ma_id': ma_id1, 'mep_id': lmep_id})

            self.assertGreater(len(ti.pop('start-time')), 0)
            ti.pop('end-time')

            exp = {'count': pkt_cnt,
                   'interval': 1,
                   'pcp': 7,
                   'source-interface': f'{self.WB_IF_1_NAME}.10', # vlan sub-if
                   'source-ma-name': ma_id1,
                   'source-md-name': md_id,
                   'source-mep-id': down_mep_mep_id,
                   'source-mac-address': src_mac,
                   'target-type': 'mac-address',
                   'target-mac-address': dst_mac,
                   'timeout': 2}

            # linktrace has `max-hops` field but not `count` and `interval`
            if cfm_initiator_pb.SessionOpcode.LTM == op:
                exp['max-hops'] = 64
                for k in ('count', 'interval'): del exp[k]
            elif cfm_initiator_pb.SessionOpcode.LBM == op:
                exp['size'] = 234 # frame_size

            self.assertDictEqual(exp, ti)

    def _on_demand_measurement_stats(self, md_id: str, ma_id: str, mep_id: str,
                                     op: cfm_initiator_pb.SessionOpcode,
                                     include_lists: bool = False) -> COrmObj | None:
        with DBClientAPI() as corm_api:
            result = corm_api.get_by_path(on_demand_test_result(op), [md_id, ma_id, mep_id],
                                            is_recursive=True, include_lists=include_lists)
            return result[0] if len(result) > 0 else None

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    def test_initiator(self):
        pass

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    def test_initiator_slm(self):
        self._validate_initiator(cfm_initiator_pb.SessionOpcode.SLM)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    def test_initiator_dmm(self):
        self._validate_initiator(cfm_initiator_pb.SessionOpcode.DMM)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    def test_initiator_ltm(self):
        self._validate_initiator(cfm_initiator_pb.SessionOpcode.LTM)
        WBoxTestCase.global_params["expected_transaction_id"] += 1

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    def test_initiator_lbm(self):
        self._validate_initiator(cfm_initiator_pb.SessionOpcode.LBM)

    def _validate_initiator_start_stop_start(self, sess_type):
        first_id = CFM_START_MEP_OAM_ID
        packet_count = 5
        sessions_allocated = self._get_oam_initiator_summary_xray("allocated")
        session_id = 0

        self._install_downmep(oam_id = first_id)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        # validate start-stop-start sequence in burst

        # start session
        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = sess_type,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id),
            dest = cfm_initiator_pb.SessionDest(dmac="00:11:22:33:44:55"),
            interval_ms = 1 * 1000,
            pkt_count = packet_count,
            frame_size = 234,
            pcp = 7,
        )

        # skip if session unsupported
        if res.cfm_initiator.start_resp.status == cfm_initiator_pb.SessionStartStatus.START_ERR_UNSUPPORTED:
            self._uninstall_downmep(delete_md = True)
            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 0,
                "MA" : 0,
                "Local MEP" : 0,
                "Remote MEP" : 0
            }), sleep_seconds=0.5, timeout_seconds=5)
            return

        # validate session ok
        start_resp = res.cfm_initiator.start_resp
        self.assertEqual(start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        session_id = start_resp.id
        sessions_allocated += 1
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 1,
            "allocated" : sessions_allocated,
        }))

        # stop session
        res = self.handler.wb_api.cfm_initiator.stop_req(id=session_id)
        self.assertEqual(res.cfm_initiator.stop_resp.status, cfm_initiator_pb.SessionStopStatus.STOP_OK)
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
        }))

        # start session again
        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = sess_type,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id),
            dest = cfm_initiator_pb.SessionDest(dmac="00:11:22:33:44:55"),
            interval_ms = 1 * 1000,
            pkt_count = packet_count,
            frame_size = 234,
            pcp = 7,
        )
        # validate session ok
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        sessions_allocated += 1
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 1,
            "allocated" : sessions_allocated,
        }))

        # wait for session to timeout
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=10)

        self._uninstall_downmep(delete_md = True)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    def test_initiator_start_stop_start(self):
        self._validate_initiator_start_stop_start(cfm_initiator_pb.SessionOpcode.SLM)
        self._validate_initiator_start_stop_start(cfm_initiator_pb.SessionOpcode.DMM)
        self._validate_initiator_start_stop_start(cfm_initiator_pb.SessionOpcode.LTM)
        self._validate_initiator_start_stop_start(cfm_initiator_pb.SessionOpcode.LBM)
        WBoxTestCase.global_params["expected_transaction_id"] += 2

    def _validate_initiator_mep_deletion(self, sess_type):
        first_id = CFM_START_MEP_OAM_ID
        packet_count = 5
        sessions_allocated = self._get_oam_initiator_summary_xray("allocated")

        self._install_downmep(oam_id = first_id)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        # test mep deletion
        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = sess_type,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id),
            dest = cfm_initiator_pb.SessionDest(dmac="00:11:22:33:44:55"),
            interval_ms = 1 * 1000,
            pkt_count = packet_count,
            frame_size = 234,
            pcp = 7,
        )

        # skip if session unsupported
        if res.cfm_initiator.start_resp.status == cfm_initiator_pb.SessionStartStatus.START_ERR_UNSUPPORTED:
            self._uninstall_downmep(delete_md = True)
            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 0,
                "MA" : 0,
                "Local MEP" : 0,
                "Remote MEP" : 0
            }), sleep_seconds=0.5, timeout_seconds=5)
            return

        # expect OK message
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        sessions_allocated += 1
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 1,
            "allocated" : sessions_allocated,
        }))

        # Delete MEP in middle of active session
        for _ in range(3):
            sleep(1)
            stats = self._on_demand_measurement_stats(md_id, ma_id1, '*', sess_type)
            if stats is not None:
                logger.info(f"Stats: {stats.to_json()}")

        self._uninstall_downmep(delete_md = True)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=5)


    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    def test_initiator_mep_deletion(self):
        self._validate_initiator_mep_deletion(cfm_initiator_pb.SessionOpcode.SLM)
        self._validate_initiator_mep_deletion(cfm_initiator_pb.SessionOpcode.DMM)
        self._validate_initiator_mep_deletion(cfm_initiator_pb.SessionOpcode.LTM)
        self._validate_initiator_mep_deletion(cfm_initiator_pb.SessionOpcode.LBM)
        WBoxTestCase.global_params["expected_transaction_id"] += 1

    def _validate_initiator_port_speed_change(self, sess_type):
        packet_count = 5
        sessions_allocated = self._get_oam_initiator_summary_xray("allocated")

        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        sleep(5)

        # start session
        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = sess_type,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_oam_id),
            dest = cfm_initiator_pb.SessionDest(dmac="00:11:22:33:44:55"),
            interval_ms = 1 * 1000,
            pkt_count = packet_count,
            frame_size = 234,
            pcp = 7,
        )

        # skip if session unsupported
        if res.cfm_initiator.start_resp.status == cfm_initiator_pb.SessionStartStatus.START_ERR_UNSUPPORTED:
            self._uninstall_downmep()
            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 0,
                "MA" : 0,
                "Local MEP" : 0,
                "Remote MEP" : 0
            }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)
            return

        # validate session ok
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        sessions_allocated += 1
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 1,
            "allocated" : sessions_allocated,
        }))

        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_400GB)
        self.set_iface_oper_state(iface_name=self.WB_IF_1_NAME, is_oper_up=True, is_link_up=True)

        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=2)

        sleep(5)

        self._send_recreate_downmep()

        # configure port speed back to 100
        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_100GB)
        self.set_iface_oper_state(iface_name=self.WB_IF_1_NAME, is_oper_up=True, is_link_up=True)
        sleep(5)

        self._send_recreate_downmep()

        self._uninstall_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

    @pytest.mark.extended_tests
    @pytest.mark.skipif(not IS_NCP3, reason="port speed change is supported ONLY on ncp3 devices")
    def test_initiator_port_speed_change(self):
        self._validate_initiator_port_speed_change(cfm_initiator_pb.SessionOpcode.SLM)
        self._validate_initiator_port_speed_change(cfm_initiator_pb.SessionOpcode.DMM)
        self._validate_initiator_port_speed_change(cfm_initiator_pb.SessionOpcode.LTM)
        self._validate_initiator_port_speed_change(cfm_initiator_pb.SessionOpcode.LBM)
        WBoxTestCase.global_params["expected_transaction_id"] += 1

    #TODO: move these helper functions up up and away :)
    def _validate_slm_tx(self, rx_packets, src_mac, dst_mac, vlan, pcp, level, mep_id):
        for i in range(0, len(rx_packets)):
            packet = rx_packets[i]

            self.assertEqual(str(packet[Ether].dst), dst_mac)
            self.assertEqual(str(packet[Ether].src), src_mac)
            self.assertEqual(packet[Dot1Q].vlan, vlan)
            self.assertEqual(packet[Dot1Q].prio, pcp)

            self.assertEqual(packet[CFM].md_level, level)
            self.assertEqual(packet[SLM].tlv_offset, 16)
            self.assertEqual(packet[SLM].SourceMEP_ID, mep_id)
            self.assertEqual(packet[SLM].Res_ResponderMEP_ID, 0)
            self.assertNotEqual(packet[SLM].TestID, 0)
            self.assertAlmostEqual(packet[SLM].TxFcf, i, delta=1)
            self.assertEqual(packet[SLM].Res_TxFcb, 0)

    def _validate_ltm_tx(self, rx_packets, src_mac, dst_mac, original_mac, target_mac, vlan, pcp, level, transaction_id):
        for i in range(0, len(rx_packets)):
            packet = rx_packets[i]

            self.assertEqual(str(packet[Ether].dst), dst_mac)
            self.assertEqual(str(packet[Ether].src), src_mac)
            self.assertEqual(packet[Dot1Q].vlan, vlan)
            self.assertEqual(packet[Dot1Q].prio, pcp)

            self.assertEqual(packet[CFM].md_level, level)
            self.assertEqual(packet[LTM].transaction_id, transaction_id) # first transaction id is 1, it goes incrementatlly
            self.assertEqual(packet[LTM].original_mac, original_mac)
            self.assertEqual(packet[LTM].target_mac, target_mac)

    def _validate_lbm_tx(self, rx_packets, src_mac, dst_mac, vlan, pcp, level, transaction_id):
        for i in range(0, len(rx_packets)):
            packet = rx_packets[i]

            self.assertEqual(str(packet[Ether].dst), dst_mac)
            self.assertEqual(str(packet[Ether].src), src_mac)
            self.assertEqual(packet[Dot1Q].vlan, vlan)
            self.assertEqual(packet[Dot1Q].prio, pcp)

            self.assertEqual(packet[CFM].md_level, level)
            self.assertEqual(packet[LBM].transaction_id, transaction_id) # first transaction id is 1, it goes incrementally

    def _send_initiator_start_request(self, opcode, md_id, ma_id, mep_oam_id, dmac, pcp, packet_count=1, interval_ms=1*1000, frame_size=44):
        sessions_allocated = self._get_oam_initiator_summary_xray("allocated")

        res = self.handler.wb_api.cfm_initiator.start_req(sess_type=opcode,
                                                          source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id, mep_id=mep_oam_id),
                                                          dest=cfm_initiator_pb.SessionDest(dmac=dmac),
                                                          pcp=pcp, pkt_count=packet_count, interval_ms=interval_ms, frame_size=frame_size)

        # expect OK message
        logger.info(f"{res}")
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        logger.info(f"Session_id {res.cfm_initiator.start_resp.id} status {res.cfm_initiator.start_resp.status}")
        sessions_allocated += 1
        self.assertTrue(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 1,
            "allocated" : sessions_allocated,
        }))

        if opcode == cfm_initiator_pb.SessionOpcode.LTM:
            WBoxTestCase.global_params["expected_transaction_id"] += 1

        return (res, sessions_allocated)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_initiator_SLM_tx(self):
        down_dst_mac = "00:11:22:33:44:55"
        down_pcp = 7

        up_dst_mac = "66:55:44:33:22:11"
        up_pcp = 4
        def pkt_filter_slm_down(x):
            return x.getlayer(Ether).dst == '00:11:22:33:44:55' and x.haslayer(Dot1Q) and x.haslayer(SLM)
        def pkt_filter_slm_up(x):
            return x.getlayer(Ether).dst == '66:55:44:33:22:11' and x.haslayer(Dot1Q) and x.haslayer(SLM)
        first_id = CFM_START_MEP_OAM_ID
        last_id = first_id + 999
        packet_count = 3 # 3 packets will be sent until timeout
        sessions_allocated = self._get_oam_initiator_summary_xray("allocated")

        self._install_downmep(oam_id = first_id)
        self._install_upmep(oam_id = last_id)

        # self.handler.execute_command("rx tracing enable")

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 2,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=5)

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_slm_down)

        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = cfm_initiator_pb.SessionOpcode.SLM,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id),
            dest = cfm_initiator_pb.SessionDest(dmac=down_dst_mac),
            interval_ms = 1 * 1000,
            pkt_count = packet_count,
            frame_size = 234,
            pcp = down_pcp,
        )

        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=packet_count)
        # might miss first packet. packet_count or packet_count - 1 is ok
        self.assertAlmostEqual(len(rx_packets), packet_count, delta=1)

        my_cfm_mac = self._validate_cfm_mac_in_oper(md_id, ma_id1, down_mep_mep_id)

        # expect OK message
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        print("Session_id " + str(res.cfm_initiator.start_resp.id) + " status " + str(res.cfm_initiator.start_resp.status))
        sessions_allocated += 1

        self._validate_slm_tx(rx_packets, src_mac = my_cfm_mac, dst_mac = down_dst_mac,
                            vlan = WBoxTestCase.global_params["outer_tag"][0], pcp = down_pcp,
                            level = md_level_down_mep, mep_id = down_mep_mep_id)

        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=7)

        # Validate upmep TX
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_slm_up)

        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = cfm_initiator_pb.SessionOpcode.SLM,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id2, mep_id=last_id),
            dest = cfm_initiator_pb.SessionDest(dmac=up_dst_mac),
            interval_ms = 1 * 1000,
            pkt_count = packet_count,
            frame_size = 234,
            pcp = up_pcp,
        )

        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=packet_count)
        # might miss first packet. packet_count or packet_count - 1 is ok
        self.assertAlmostEqual(len(rx_packets), packet_count, delta=1)

        my_cfm_mac = self._validate_cfm_mac_in_oper(md_id, ma_id2, up_mep_mep_id)

        # expect OK message
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        print("Session_id " + str(res.cfm_initiator.start_resp.id) + " status " \
            + str(res.cfm_initiator.start_resp.status))
        sessions_allocated += 1

        self._validate_slm_tx(rx_packets, src_mac = my_cfm_mac, dst_mac = up_dst_mac,
                            vlan = WBoxTestCase.global_params["outer_tag"][0], pcp = up_pcp,
                            level = md_level_up_mep, mep_id = up_mep_mep_id)


        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=7)

        self._uninstall_downmep(delete_md = False)
        self._uninstall_upmep(delete_md = True)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

    @remote_test()
    def test_initiator_LTM_tx(self):
        multicast_down_dst_mac = "01:80:c2:00:00:3a" # use multicast da class 2
        multicast_up_dst_mac = "01:80:c2:00:00:3d"
        down_dst_mac = "00:11:22:33:44:55"
        down_pcp = 7

        up_dst_mac = "66:55:44:33:22:11"
        up_pcp = 4

        def pkt_filter_ltm_down(x):
            return x.getlayer(Ether).dst == multicast_down_dst_mac and x.haslayer(Dot1Q) and x.haslayer(LTM)
        def pkt_filter_ltm_up(x):
            return x.getlayer(Ether).dst == multicast_up_dst_mac and x.haslayer(Dot1Q) and x.haslayer(LTM)

        first_id = CFM_START_MEP_OAM_ID
        last_id = first_id + 999
        packet_count = 1 # 1 packets will be sent and will be expected a couple of LTR's

        sessions_allocated = self._get_oam_initiator_summary_xray("allocated")

        self._install_downmep(oam_id = first_id)
        self._install_upmep(oam_id = last_id)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 2,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=5)

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_ltm_down)

        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = cfm_initiator_pb.SessionOpcode.LTM,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id),
            dest = cfm_initiator_pb.SessionDest(dmac=down_dst_mac),
            pcp = down_pcp,
        )

        WBoxTestCase.global_params["expected_transaction_id"] += 1

        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=packet_count)
        self.assertEqual(len(rx_packets), packet_count)

        my_cfm_mac = self._validate_cfm_mac_in_oper(md_id, ma_id1, down_mep_mep_id)

        # expect OK message
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        print("Session_id " + str(res.cfm_initiator.start_resp.id) + " status " \
            + str(res.cfm_initiator.start_resp.status))
        sessions_allocated += 1

        self._validate_ltm_tx(rx_packets, src_mac = my_cfm_mac, dst_mac = multicast_down_dst_mac,
                              original_mac=my_cfm_mac, target_mac=down_dst_mac,
                              vlan = WBoxTestCase.global_params["outer_tag"][0], pcp = down_pcp,
                              level = md_level_down_mep,
                              transaction_id=WBoxTestCase.global_params["expected_transaction_id"])

        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=7)

        # Validate upmep TX
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_ltm_up)

        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = cfm_initiator_pb.SessionOpcode.LTM,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id2, mep_id=last_id),
            dest = cfm_initiator_pb.SessionDest(dmac=up_dst_mac),
            pcp = up_pcp,
        )

        WBoxTestCase.global_params["expected_transaction_id"] += 1

        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=packet_count)
        self.assertEqual(len(rx_packets), packet_count)

        my_cfm_mac = self._validate_cfm_mac_in_oper(md_id, ma_id2, up_mep_mep_id)

        # expect OK message
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        print("Session_id " + str(res.cfm_initiator.start_resp.id) + " status " \
            + str(res.cfm_initiator.start_resp.status))
        sessions_allocated += 1

        self._validate_ltm_tx(rx_packets, src_mac = my_cfm_mac, dst_mac = multicast_up_dst_mac,
                              original_mac=my_cfm_mac, target_mac=up_dst_mac,
                              vlan = WBoxTestCase.global_params["outer_tag"][0], pcp = up_pcp,
                              level = md_level_up_mep,
                              transaction_id=WBoxTestCase.global_params["expected_transaction_id"])

        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=7)

        self._uninstall_downmep(delete_md = False)
        self._uninstall_upmep(delete_md = True)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

    @parameterized.expand([
        (down_mep_oam_id, ma_id1, md_level_down_mep, 0, "00:11:22:33:44:55", 7,),
        (up_mep_oam_id, ma_id2, md_level_up_mep, 1, "66:55:44:33:22:11", 4,)
    ])
    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_initiator_LBM_tx(self, mep_oam_id, ma_id, md_level, my_cfm_mac_id, destination_mac, pcp):
        self.handler.execute_command("cfm pdu set debug log enable")
        my_cfm_mac = WBoxTestCase.global_params["my_cfm_mac"][my_cfm_mac_id]
        packet_count = 0

        def pkt_filter_lbm_tx(x):
            return x.getlayer(Ether).dst == destination_mac and x.haslayer(LBM)

        self._prepare_basic_oam_setup()

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_lbm_tx)

        _, sessions_allocated = self._send_initiator_start_request(opcode=cfm_initiator_pb.SessionOpcode.LBM,
                                                                     md_id=md_id, ma_id=ma_id, mep_oam_id=mep_oam_id,
                                                                     dmac=destination_mac, pcp=pcp)
        packet_count = 1 # 1 packet was sent, 1 LBM is expected

        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=packet_count)
        self.assertEqual(len(rx_packets), packet_count)

        self._validate_lbm_tx(rx_packets, src_mac=my_cfm_mac, dst_mac=destination_mac,
                              vlan=WBoxTestCase.global_params["outer_tag"][0], pcp=pcp,
                              level=md_level,
                              transaction_id=packet_count)

        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=7)

        self._cleanup_basic_oam_setup()
        self.handler.execute_command("cfm pdu set debug log disable")

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_initiator_SLM_rx(self):
        first_id = CFM_START_MEP_OAM_ID
        last_id = first_id + 999
        packet_count = 5
        sessions_allocated = self._get_oam_initiator_summary_xray("allocated")

        self._install_downmep(oam_id = first_id)
        self._install_upmep(oam_id = last_id)

        # self.handler.execute_command("rx tracing enable")

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 2,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=5)

        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = cfm_initiator_pb.SessionOpcode.SLM,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id),
            dest = cfm_initiator_pb.SessionDest(dmac="00:11:22:33:44:55"),
            interval_ms = 1 * 1000,
            pkt_count = packet_count,
            frame_size = 234,
            pcp = 7,
        )

        # expect OK message
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        print("Session_id " + str(res.cfm_initiator.start_resp.id) + " status " \
            + str(res.cfm_initiator.start_resp.status))
        sessions_allocated += 1
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 1,
            "allocated" : sessions_allocated,
        }))

        down_cfm_mac = self._validate_cfm_mac_in_oper(md_id, ma_id1, down_mep_mep_id)

        remote_tx = 90
        slr_count = 4
        slr_sent = 0

        session_stats_ok = False
        session_stats_done = False

        for _ in range(10):
            if (slr_sent < slr_count):
                pkt = (Ether(dst=down_cfm_mac, src='00:01:02:03:04:05') /
                        Dot1Q(vlan=10, prio=5) /
                        CFM(md_level=md_level_down_mep) /
                        SLR(SourceMEP_ID=down_mep_mep_id, TestID=0, TxFcb=remote_tx))
                self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=pkt, number_of_packets=1)
                slr_sent += 1
                remote_tx += 1

            sleep(1)
            stats = self._on_demand_measurement_stats(md_id, ma_id1, str(down_mep_mep_id),
                                                      cfm_initiator_pb.SessionOpcode.SLM)
            self.assertIsNotNone(stats)

            logger.info(f"SLM Stats: {stats.to_json()}")
            session_stats_ok |= stats.measurement_validity == 'incomplete'
            session_stats_done |= stats.measurement_validity == 'valid'

            if session_stats_done:
                self.assertEqual(stats.synthetic_loss_pdus_sent, packet_count)
                self.assertEqual(stats.synthetic_loss_pdus_received, slr_count)
                self.assertEqual(stats.slm_received_by_remote_mep, slr_count)
                self.assertEqual(stats.missing_slr, packet_count - slr_count)

                self.assertEqual(stats.frame_loss.far_end.frame_loss_far_end_count, packet_count - slr_count)
                self.assertEqual(stats.frame_loss.far_end.frame_loss_far_end_percentage,
                                 (packet_count - slr_count) * 100 / float(packet_count))

                self.assertEqual(stats.frame_loss.near_end.frame_loss_near_end_count, 0)
                self.assertEqual(stats.frame_loss.near_end.frame_loss_near_end_percentage, float(0))

                break

        self.assertTrue(session_stats_ok)
        self.assertTrue(session_stats_done)

        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=7)

        # validate UPMep rx
        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = cfm_initiator_pb.SessionOpcode.SLM,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id2, mep_id=last_id),
            dest = cfm_initiator_pb.SessionDest(dmac="00:11:22:33:44:55"),
            interval_ms = 1 * 1000,
            pkt_count = packet_count,
            frame_size = 234,
            pcp = 7,
        )

        # expect OK message
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        print("Session_id " + str(res.cfm_initiator.start_resp.id) + " status " \
            + str(res.cfm_initiator.start_resp.status))
        sessions_allocated += 1
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 1,
            "allocated" : sessions_allocated,
        }))

        up_cfm_mac = self._validate_cfm_mac_in_oper(md_id, ma_id2, up_mep_mep_id)

        remote_tx = 90
        slr_count = 4
        slr_sent = 0

        session_stats_ok = False
        session_stats_done = False

        for _ in range(10):
            if (slr_sent < slr_count):
                pkt = (Ether(dst=up_cfm_mac, src='00:01:02:03:04:05') /
                        Dot1Q(vlan=10, prio=5) /
                        CFM(md_level=md_level_up_mep) /
                        SLR(SourceMEP_ID=up_mep_mep_id, TestID=0, TxFcb=remote_tx))
                self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=pkt, number_of_packets=1)
                slr_sent += 1
                remote_tx -= 1 # send backwards

            sleep(1)
            stats = self._on_demand_measurement_stats(md_id, ma_id2, str(up_mep_mep_id),
                                                      cfm_initiator_pb.SessionOpcode.SLM)
            self.assertIsNotNone(stats)

            logger.info(f"SLM Stats: {stats.to_json()}")
            session_stats_ok |= stats.measurement_validity == 'incomplete'
            session_stats_done |= stats.measurement_validity == 'valid'

            if session_stats_done:
                self.assertEqual(stats.synthetic_loss_pdus_sent, packet_count)
                self.assertEqual(stats.synthetic_loss_pdus_received, slr_count)
                self.assertEqual(stats.slm_received_by_remote_mep, slr_count)
                self.assertEqual(stats.missing_slr, packet_count - slr_count)

                self.assertEqual(stats.frame_loss.far_end.frame_loss_far_end_count, packet_count - slr_count)
                self.assertEqual(stats.frame_loss.far_end.frame_loss_far_end_percentage,
                                 (packet_count - slr_count) * 100 / float(packet_count))

                self.assertEqual(stats.frame_loss.near_end.frame_loss_near_end_count, 0)
                self.assertEqual(stats.frame_loss.near_end.frame_loss_near_end_percentage, float(0))

                break

        # validate session has gone through all states
        self.assertTrue(session_stats_ok)
        self.assertTrue(session_stats_done)

        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=7)

        self._uninstall_downmep(delete_md = False)
        self._uninstall_upmep(delete_md = True)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_initiator_LTM_rx(self):
        down_dst_mac = "00:11:22:33:44:55"
        up_dst_mac = "66:55:44:33:22:11"

        first_id = CFM_START_MEP_OAM_ID
        last_id = first_id + 999
        sessions_allocated = self._get_oam_initiator_summary_xray("allocated")

        self._install_downmep(oam_id = first_id)
        self._install_upmep(oam_id = last_id)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 2,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=5)

        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = cfm_initiator_pb.SessionOpcode.LTM,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id),
            dest = cfm_initiator_pb.SessionDest(dmac=down_dst_mac),
            pcp = 7,
        )

        WBoxTestCase.global_params["expected_transaction_id"] += 1

        # expect OK message
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        print("Session_id " + str(res.cfm_initiator.start_resp.id) + " status " \
            + str(res.cfm_initiator.start_resp.status))
        sessions_allocated += 1
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 1,
            "allocated" : sessions_allocated,
        }))

        down_cfm_mac = self._validate_cfm_mac_in_oper(md_id, ma_id1, down_mep_mep_id)

        # send 10 packets and then wait till session is done
        # ltm session sends 1 packet and waits 5s to get ltrs from all hops
        ltm_session_timeout = 5
        ltr_sent = 0
        ltr_pkt_to_send = 10
        packet_interval = 0.1
        iteration = ltm_session_timeout / packet_interval

        session_stats_ok = False
        session_stats_done = False

        for _ in range(int(iteration)):
            if (ltr_sent < ltr_pkt_to_send):
                pkt = (Ether(dst=down_cfm_mac, src=f'00:01:02:03:04:{ltr_sent + 3:0>2x}') /
                        Dot1Q(vlan=10, prio=5) /
                        CFM(md_level=md_level_down_mep) /
                        LTR(use_fdb_only=1, transaction_id=100, ttl=3 + ltr_pkt_to_send - ltr_sent,
                            tlv_list=[LtrEgressIdentifierTlv(last_egress_mac="aa:22:33:44:55:66",
                                                             last_egress_id=66,
                                                             next_egress_mac="bb:22:33:44:55:66",
                                                             next_egress_id=77),
                                      LtrReplyIngressTlv(ingress_mac="cc:22:33:44:55:66",
                                                         ingress_action=1)]))

                self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=pkt, number_of_packets=1)
                ltr_sent += 1

            sleep(packet_interval)
            stats = self._on_demand_measurement_stats(md_id, ma_id1, str(down_mep_mep_id),
                                                      cfm_initiator_pb.SessionOpcode.LTM,
                                                      include_lists=True)
            self.assertIsNotNone(stats)
            logger.info(f"LTM Stats: {stats.to_json()}")

            session_stats_ok |= stats.measurement_validity == 'incomplete'
            session_stats_done |= stats.measurement_validity == 'valid'

            if session_stats_done:
                self.assertEqual(stats.ltr_received, ltr_sent)
                self.assertEqual(len(stats.hop_info), ltr_sent)
                break

        # validate session has gone through all states
        self.assertTrue(session_stats_ok)
        self.assertTrue(session_stats_done)

        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=7)

        # validate UPMep rx
        res = self.handler.wb_api.cfm_initiator.start_req(
            sess_type = cfm_initiator_pb.SessionOpcode.LTM,
            source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id2, mep_id=last_id),
            dest = cfm_initiator_pb.SessionDest(dmac=up_dst_mac),
            pcp = 7,
        )

        WBoxTestCase.global_params["expected_transaction_id"] += 1

        # expect OK message
        self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)
        print("Session_id " + str(res.cfm_initiator.start_resp.id) + " status " \
            + str(res.cfm_initiator.start_resp.status))
        sessions_allocated += 1
        self.assertTrue(self._assert_oam_initiator_summary_xray({
            "active" : 1,
            "allocated" : sessions_allocated,
        }))

        up_cfm_mac = self._validate_cfm_mac_in_oper(md_id, ma_id2, up_mep_mep_id)

        # send 10 packets and then wait till session is done
        # ltm session sends 1 packet and waits 5s to get ltrs from all hops
        ltr_sent = 0

        session_stats_ok = False
        session_stats_done = False

        for _ in range(int(iteration)):
            if (ltr_sent < ltr_pkt_to_send):
                pkt = (Ether(dst=up_cfm_mac, src=f'00:01:02:03:04:{ltr_sent + 3:0>2x}') /
                        Dot1Q(vlan=10, prio=5) /
                        CFM(md_level=md_level_up_mep) /
                        LTR(use_fdb_only=1, transaction_id=100, ttl=3 + ltr_pkt_to_send - ltr_sent,
                            tlv_list=[LtrEgressIdentifierTlv(last_egress_mac="aa:22:33:44:55:66",
                                                             last_egress_id=66,
                                                             next_egress_mac="bb:22:33:44:55:66",
                                                             next_egress_id=77),
                                      LtrReplyIngressTlv(ingress_mac="cc:22:33:44:55:66", ingress_action=1)]))

                self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=pkt, number_of_packets=1)
                ltr_sent += 1

            sleep(packet_interval)
            stats = self._on_demand_measurement_stats(md_id, ma_id2, str(up_mep_mep_id),
                                                      cfm_initiator_pb.SessionOpcode.LTM,
                                                      include_lists=True)
            self.assertIsNotNone(stats)
            logger.info(f"LTM Stats: {stats.to_json()}")

            session_stats_ok |= stats.measurement_validity == 'incomplete'
            session_stats_done |= stats.measurement_validity == 'valid'

            if session_stats_done:
                self.assertEqual(stats.ltr_received, ltr_sent)
                self.assertEqual(len(stats.hop_info), ltr_sent)
                break

        # validate session has gone through all states
        self.assertTrue(session_stats_ok)
        self.assertTrue(session_stats_done)

        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=7)

        self._uninstall_downmep(delete_md = False)
        self._uninstall_upmep(delete_md = True)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

    def _wait_for_stats_done_lbm(self, md_id, ma_id, mep_id,
                                 exp_tx, exp_rx, exp_rx_bad_order,
                                 exp_timeout=0, exp_malformed=0):
        def _validate_lbm_stats(lbm_stats):
            exp_lost = exp_tx - exp_rx
            ok = True
            if lbm_stats.lbm_transmitted != exp_tx:
                logger.error(f"lbm_transmitted expected {exp_tx} != actual {lbm_stats.lbm_transmitted}")
                ok = False
            if lbm_stats.lbr_received != exp_rx:
                logger.error(f"lbr_received expected {exp_rx} != actual {lbm_stats.lbr_received}")
                ok = False
            if lbm_stats.lost_lbr != exp_lost:
                logger.error(f"lost_lbr expected {exp_lost} != actual {lbm_stats.lost_lbr}")
                ok = False
            if lbm_stats.out_of_order_lbr != exp_rx_bad_order:
                logger.error(f"out_of_order_lbr expected {exp_rx_bad_order} != actual {lbm_stats.out_of_order_lbr}")
                ok = False
            if lbm_stats.invalid_lbr != exp_malformed:
                logger.error(f"invalid_lbr expected {exp_malformed} != actual {lbm_stats.invalid_lbr}")
                ok = False
            if lbm_stats.lbr_timeout != exp_timeout:
                logger.error(f"lbr_timeout expected {exp_timeout} != actual {lbm_stats.lbr_timeout}")
                return False
            return ok

        stats = self._on_demand_measurement_stats(md_id, ma_id, str(mep_id),
                                                  cfm_initiator_pb.SessionOpcode.LBM)
        if stats is None:
            return False

        logger.info(stats.to_json())

        if stats.measurement_validity != 'valid':
            return False

        return _validate_lbm_stats(stats)

    @parameterized.expand([
        (down_mep_oam_id, ma_id1, down_mep_mep_id, "00:11:22:33:44:55", 7,),
        (up_mep_oam_id, ma_id2, up_mep_mep_id, "66:55:44:33:22:11", 4,)
    ])
    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    @pytest.mark.skipif(IS_NCPL_SA, reason="SW-182206")
    def test_initiator_LBM_rx(self, mep_oam_id, ma_id, mep_id, destination_mac, pcp):
        ### test setup ###
        self.handler.execute_command("cfm pdu set debug log enable")
        packet_count = 5
        sessions_allocated = 0

        self._prepare_basic_oam_setup()

        ## counters can be read after lmep creation
        ## check counters ALWAYS for all installed endpoints
        down_cnt_before = self._get_current_counters(lmep_config={
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)})

        up_cnt_before = self._get_current_counters(lmep_config={
                "md_id" : md_id,
                "ma_id" : ma_id2,
                "mep_id" : str(up_mep_mep_id)})

        ### end of test setup ###

        self.handler.cfm_reflector.start_sniffer(interface=self.WB_IF_1_NAME, sniffer_idx=1)
        self.handler.cfm_reflector.start_reflect_packets(interface=self.WB_IF_1_NAME, timeout=1)

        ### Validate DownMEP/UpMEP LBM - LBR rx ###
        res, sessions_allocated = self._send_initiator_start_request(opcode=cfm_initiator_pb.SessionOpcode.LBM,
                                                                     md_id=md_id, ma_id=ma_id, mep_oam_id=mep_oam_id,
                                                                     dmac=destination_mac, pcp=pcp,
                                                                     packet_count=packet_count, interval_ms=1*1000)

        self.assertEqual(res.cfm_initiator.start_resp.status,
                         cfm_initiator_pb.SessionStartStatus.START_OK)

        waiting.wait(lambda: self._wait_for_stats_done_lbm(md_id, ma_id, mep_id, packet_count, packet_count, 0),
                     sleep_seconds=1, timeout_seconds=15)

        self.handler.cfm_reflector.stop_reflect_packets()

        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=7)

        ### check also redis counters
        if mep_id == down_mep_mep_id:
            exp_down = packet_count
            exp_up = 0
        else:
            exp_down = 0
            exp_up = packet_count

        waiting.wait(lambda: self._diff_orm_counters(
                down_cnt_before, exp_down, packet_count, down_mep_mep_id, md_id, ma_id1, ['lbm_out', 'lbr_in']),
                sleep_seconds=1, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._diff_orm_counters(
                up_cnt_before, exp_up, packet_count, up_mep_mep_id, md_id, ma_id2, ['lbm_out', 'lbr_in']),
                sleep_seconds=1, timeout_seconds=wait_timeout_s)

        ### end of test: cleanup ###
        self._cleanup_basic_oam_setup()
        self.handler.execute_command("cfm pdu set debug log disable")

    def test_initiator_LBM(self):
        pass

    @parameterized.expand([
        (False, # skip 1 packet in reflection
         False, # alter the transaction id for the reflected packet
         False, # alter the msdu for the reflected packet
         False), # delay the reflected packet
        (True, # skip 1 packet in reflection
         False, # alter the transaction id for the reflected packet
         False,
         False), # delay the reflected packet
        (False, # skip 1 packet in reflection
         True, # alter the transaction id for the reflected packet
         False,
         False), # delay the reflected packet

        # Disabled because these two tests are flaky and failing in jenkins
        # (False, # skip 1 packet in reflection
        #  False, # alter the transaction id for the reflected packet
        #  True,
        #  False), # delay the reflected packet
        # (False, # skip 1 packet in reflection
        #  False, # alter the transaction id for the reflected packet
        #  False,
        #  True), # delay the reflected packet
    ])
    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_initiator_LBM_tx_stats_downmep(self, skip_packet, alter_transaction_id, alter_msdu, delay_pkt):
        packet_count = 5
        interval_seconds = 1
        marked_pkt_no = 2 # which packet to skip/alter transaction id

        self.handler.execute_command("cfm pdu set debug log enable")
        destination_mac = "00:11:22:33:44:55"
        pcp = 7
        sessions_allocated = 0
        interval_ms = interval_seconds * 1000
        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        exp_rx = packet_count
        exp_malformed_rx = 0
        exp_timeout_rx = 0

        self.handler.cfm_reflector.start_sniffer(interface=self.WB_IF_1_NAME, sniffer_idx=1)

        if skip_packet:
            self.handler.cfm_reflector.skip_packet(marked_pkt_no)
            exp_rx = packet_count - 1
            exp_timeout_rx = 1

        if alter_transaction_id:
            self.handler.cfm_reflector.force_transaction_id(marked_pkt_no)
            exp_rx = packet_count - 1
            exp_timeout_rx = 1

        if alter_msdu:
            self.handler.cfm_reflector.alter_msdu(marked_pkt_no)
            exp_malformed_rx = 1

        if delay_pkt:
            self.handler.cfm_reflector.delay_packet(n=marked_pkt_no, delay=2.5)
            exp_timeout_rx = 1

        self.handler.cfm_reflector.start_reflect_packets(interface=self.WB_IF_1_NAME, timeout=1)

        ### Validate DownMEP/UpMEP LBM - LBR rx ###
        res, sessions_allocated = self._send_initiator_start_request(opcode=cfm_initiator_pb.SessionOpcode.LBM,
                                                                     md_id=md_id, ma_id=ma_id1,
                                                                     mep_oam_id=down_mep_oam_id,
                                                                     dmac=destination_mac, pcp=pcp,
                                                                     packet_count=packet_count, interval_ms=interval_ms)
        self.assertEqual(res.cfm_initiator.start_resp.status,
                         cfm_initiator_pb.SessionStartStatus.START_OK)
        waiting.wait(lambda: self._wait_for_stats_done_lbm(md_id, ma_id1, down_mep_mep_id,
                                                           packet_count, exp_rx, 0,
                                                           exp_timeout_rx, exp_malformed_rx),
                     sleep_seconds=1, timeout_seconds=15)

        self.handler.cfm_reflector.stop_reflect_packets()

        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
            "active" : 0,
            "allocated" : sessions_allocated,
            "freed" : sessions_allocated
        }), sleep_seconds=0.5, timeout_seconds=7)

        self._uninstall_downmep()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)
        self.handler.execute_command("cfm pdu set debug log disable")

    @parameterized.expand([
        (44, # frame size is a valid value > default packet length
         1), # interval between tx packets
         (LOOPBACK_MIN_FRAME_SIZE, # frame size = default packet length; pdu size will default to LoopbackHeader size and no DataTlv
         1), # interval between tx packets
        (LOOPBACK_MIN_FRAME_SIZE - 4, # frame size < default packet length; pdu size will default to LoopbackHeader size and no DataTlv
         1), # interval between tx packets
         (LOOPBACK_MIN_FRAME_SIZE + 2, # frame size < default packet length; pdu size will default to LoopbackHeader size and no DataTlv
         2), # interval between tx packets
         (LOOPBACK_MIN_FRAME_SIZE_DATA_TLV, # frame size is the default packet length; only data_tlv_payload will be missing
         1), # interval between tx packets
    ])
    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_initiator_LBM_tx_session_params(self, frame_size, interval_seconds):
        self.handler.execute_command("cfm pdu set debug log enable")
        destination_mac = "00:11:22:33:44:55"
        pcp = 7
        sessions_allocated = 0
        interval_ms = interval_seconds * 1000
        packet_count = 5
        exp_tx_rx = 5

        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        ### test
        self.handler.cfm_reflector.start_sniffer(interface=self.WB_IF_1_NAME, sniffer_idx=1)

        self.handler.cfm_reflector.start_reflect_packets(interface=self.WB_IF_1_NAME, timeout=1)

        res, sessions_allocated = self._send_initiator_start_request(opcode=cfm_initiator_pb.SessionOpcode.LBM,
                                                                     md_id=md_id, ma_id=ma_id1,
                                                                     mep_oam_id=down_mep_oam_id,
                                                                     dmac=destination_mac, pcp=pcp,
                                                                     packet_count=packet_count,
                                                                     interval_ms=interval_ms, frame_size=frame_size)
        self.assertEqual(res.cfm_initiator.start_resp.status,
                         cfm_initiator_pb.SessionStartStatus.START_OK)

        waiting.wait(lambda: self._wait_for_stats_done_lbm(md_id, ma_id1, down_mep_mep_id,
                                                           exp_tx_rx, exp_tx_rx, 0),
            sleep_seconds=1, timeout_seconds=15)

        ####
        self.handler.cfm_reflector.stop_reflect_packets()
        # wait for session to become inactive and freed
        waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
                "active" : 0,
                "allocated" : sessions_allocated,
                "freed" : sessions_allocated,
        }), sleep_seconds=1, timeout_seconds=15)

        self._uninstall_downmep()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)
        self.handler.execute_command("cfm pdu set debug log enable")

    @pytest.mark.extended_tests
    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_monolith_slm_oper(self):
        first_id = CFM_START_MEP_OAM_ID
        last_id = first_id + 999

        self._install_downmep(oam_id = first_id)
        self._install_upmep(oam_id = last_id)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 2,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=5)

        self._assert_traffic(True)

        ccm_pkt_down= (
            Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=10, prio=5, type=0x8902) /
            CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
            CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
        )

        packet_to_send_cnt = 5
        self._validate_ccm_in_counters(md_id, ma_id1, down_mep_mep_id, packet_to_send_cnt, ccm_pkt_down,
                                       self.WB_IF_1_NAME)

        # let counters to increment
        seconds = 20
        while(seconds):
            print(f"Sleeping for {seconds} seconds")
            sleep(10)
            seconds = seconds - 10

        packet_to_send_cnt = 10
        self._validate_slr_count(md_id, ma_id1, down_mep_mep_id, md_level_down_mep, packet_to_send_cnt,
                                 self.WB_IF_1_NAME)
        # validate ccm_in has not changed
        self._validate_ccm_in_counters(md_id, ma_id1, down_mep_mep_id, 0, None, self.WB_IF_1_NAME)

        sleep(10)

        # again
        packet_to_send_cnt = 13
        self._validate_slr_count(md_id, ma_id1, down_mep_mep_id, md_level_down_mep, packet_to_send_cnt,
                                 self.WB_IF_1_NAME)
        # validate ccm_in has not changed
        self._validate_ccm_in_counters(md_id, ma_id1, down_mep_mep_id, 0, None, self.WB_IF_1_NAME)

        ccm_pkt_up = (
            Ether(dst=f'01:80:c2:00:00:3{md_level_up_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=10, prio=5, type=0x8902) /
            CFM(md_level=md_level_up_mep, opcode=CCM.opcode) /
            CCM(mep_id=up_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
        )

        packet_to_send_cnt = 5
        self._validate_ccm_in_counters(md_id, ma_id2, up_mep_mep_id, packet_to_send_cnt, ccm_pkt_up, self.WB_IF_1_NAME)

        # let counters to increment
        seconds = 20
        while(seconds):
            print(f"Sleeping for {seconds} seconds")
            sleep(10)
            seconds = seconds - 10

        packet_to_send_cnt = 30
        self._validate_slr_count(md_id, ma_id2, up_mep_mep_id, md_level_up_mep, packet_to_send_cnt, self.WB_IF_1_NAME)
        # validate ccm_in has not changed
        self._validate_ccm_in_counters(md_id, ma_id2, up_mep_mep_id, 0, None, self.WB_IF_1_NAME)

        sleep(10)

        # again
        packet_to_send_cnt = 13
        self._validate_slr_count(md_id, ma_id2, up_mep_mep_id, md_level_up_mep, packet_to_send_cnt, self.WB_IF_1_NAME)
        # validate ccm_in has not changed
        self._validate_ccm_in_counters(md_id, ma_id2, up_mep_mep_id, 0, None, self.WB_IF_1_NAME)

        self._uninstall_downmep(delete_md = False)
        self._uninstall_upmep(delete_md = True)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)



    @pytest.mark.extended_tests
    @pytest.mark.skipif(IS_NCP3 and (get_device_version() is None
                                     or get_device_version() < MIN_SYNCE_SUPPORTED_DEVICE_VERSION_NCP3),
                        reason="Missing SyncE timesync between units")
    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_initiator_dmm(self):
        first_id = CFM_START_MEP_OAM_ID
        pkt_count = 10

        self._install_downmep(oam_id = first_id)

        # self.handler.execute_command("rx tracing enable")

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        dst_cfm_mac = self._validate_cfm_mac_in_oper(md_id, ma_id1, down_mep_mep_id)

        def check_dmm_session():
            def pkt_filter_dmm(x):
                return x.getlayer(Ether).dst == '00:01:02:03:04:05' and x.haslayer(Dot1Q) and x.haslayer(DMM)

            def session_started() -> bool:
                stats = self._on_demand_measurement_stats(md_id, ma_id1, str(down_mep_mep_id),
                                                          cfm_initiator_pb.SessionOpcode.DMM)
                return stats is not None and stats.measurement_validity == 'incomplete'

            sessions_allocated = self._get_oam_initiator_summary_xray("allocated")

            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_dmm)
            res = self.handler.wb_api.cfm_initiator.start_req(
                sess_type = cfm_initiator_pb.SessionOpcode.DMM,
                source = cfm_initiator_pb.SessionMep(md_id=md_id, ma_id=ma_id1, mep_id=first_id),
                dest = cfm_initiator_pb.SessionDest(dmac="00:01:02:03:04:05"),
                interval_ms = 1 * 1000,
                pkt_count = pkt_count,
                frame_size = 234,
                pcp = 4,
            )
            self.assertEqual(res.cfm_initiator.start_resp.status, cfm_initiator_pb.SessionStartStatus.START_OK)

            waiting.wait(lambda : session_started(), sleep_seconds=0.5, timeout_seconds=3)

            stats = self._on_demand_measurement_stats(md_id, ma_id1, str(down_mep_mep_id),
                                                      cfm_initiator_pb.SessionOpcode.DMM)

            rx_count = 0
            dmr_count = 0

            while stats.measurement_validity == 'incomplete':
                rx_packets = self.handler.data_communicator.rx(timeout=1, number_of_packets=rx_count + 1)
                stats = self._on_demand_measurement_stats(md_id, ma_id1, str(down_mep_mep_id),
                                                          cfm_initiator_pb.SessionOpcode.DMM)
                if (stats is None or stats.dmr_received is None):
                    continue

                dmr_count = max(dmr_count, stats.dmr_received)

                print(stats.to_json())

                if len(rx_packets) > rx_count:
                    tx_timestampf = rx_packets[-1][DMM].tx_timestampf
                    rx_timestampf = tx_timestampf + 10
                    tx_timestampb = rx_timestampf + 10
                    dmr_pkt = (Ether(dst=dst_cfm_mac, src='00:01:02:03:04:05') /
                            Dot1Q(vlan=10, prio=5) /
                            CFM(md_level=md_level_down_mep) /
                            DMR(tx_timestampf=tx_timestampf, rx_timestampf=rx_timestampf, tx_timestampb=tx_timestampb))
                    self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=dmr_pkt, number_of_packets=1)
                    rx_count = len(rx_packets)

            # wait for session to be freed
            waiting.wait(lambda: self._assert_oam_initiator_summary_xray({
                "active": 0,
                "allocated": sessions_allocated + 1,
                "freed": sessions_allocated + 1
            }), sleep_seconds=0.5, timeout_seconds=3)

            stats = self._on_demand_measurement_stats(md_id, ma_id1, str(down_mep_mep_id),
                                                      cfm_initiator_pb.SessionOpcode.DMM)
            self.assertEqual(stats.measurement_validity, 'valid')
            self.assertEqual(stats.dmm_transmitted, pkt_count)
            self.assertAlmostEqual(stats.dmr_received, pkt_count, delta=1)
            self.assertAlmostEqual(stats.success_rate_percent, 100.0, delta=10.0)

        # run ETH-DM check multiple times for memory-reuse validation
        for _ in range(3):
            check_dmm_session()

        self._uninstall_downmep(delete_md = True)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

    def test_add_remove_rmeps(self):
        self._install_downmep()
        iface_names = WBoxTestCase.global_params["interfaces"]

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id + 1, down_mep_remote_mep_id + 2, down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id + 2],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep()

    @devvm_test()
    def test_many_objects_simple(self):
        """ PLEASE don't modify this test. If you need to add new logic, create a new test.
        """
        nr_md = 100
        nr_ma_in_md = 10
        nr_lmep_in_ma = 1
        nr_rmep_in_lmep = 5
        oam_id = CFM_START_MEP_OAM_ID
        scale_wait_timeout_s = 120

        # Create all objects
        for md_idx in range(nr_md):
            self.handler.wb_api.cfm.create_md(md_id=str(md_idx))

            for ma_idx in range(nr_ma_in_md):
                maid_tmp = bytes(CCM.create_maid(ma_name=f"ab{ma_idx}", ma_name_format=3))
                self.handler.wb_api.cfm.create_ma(
                    ma_id=str(ma_idx), maid48=bytes(maid_tmp), md_id=str(md_idx),
                    ma_name=f"ab{ma_idx}", md_name=str(md_idx),
                    flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=oam_id)

                for lmep_idx in range(nr_lmep_in_ma):
                    rmeps = [lmep_idx + k for k in range(nr_rmep_in_lmep)]

                    self.handler.wb_api.cfm.create_lmep(
                        oam_id=oam_id,
                        mep_id=oam_id,
                        md_id=str(md_idx),
                        ma_id=str(ma_idx),
                        group_oam_id=ma_idx,
                        interface_name=self.WB_IF_1_NAME,
                        direction=cfm_pb.MepDirection.DOWN,
                        admin_state=cfm_pb.AdminState.ENABLED,
                        outer_tag=10,
                        outer_tpid=0x8100,
                        ccm_ltm_priority=5,
                        md_level=md_level_down_mep,
                        remote_mep_ids=rmeps)

                    self.handler.wb_api.cfm.create_mip(
                        oam_id=mip_oam_id + oam_id,
                        name=str(oam_id),
                        md_id=str(md_idx),
                        ma_id=str(ma_idx),
                        group_oam_id=ma_idx,
                        interface_name=self.WB_IF_1_NAME,
                        admin_state=cfm_pb.AdminState.ENABLED,
                        md_level=md_level_mip,
                        req_type=cfm_pb.CreateRequestType.CREATE)

                    oam_id = oam_id + 1

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : nr_md,
            "MA" : nr_md * nr_ma_in_md,
            "Local MEP" : nr_md * nr_ma_in_md * nr_lmep_in_ma,
            "MIP" : nr_md * nr_ma_in_md * nr_lmep_in_ma,
            "Remote MEP" : nr_md * nr_ma_in_md * nr_lmep_in_ma * nr_rmep_in_lmep,
        }), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        # Wait until all data is in operdb
        waiting.wait(lambda: self._assert_operdb_count(
            nr_md * nr_ma_in_md * nr_lmep_in_ma, nr_md * nr_ma_in_md * nr_lmep_in_ma,
            nr_md * nr_ma_in_md * nr_lmep_in_ma * nr_rmep_in_lmep),
            sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

        oam_id = CFM_START_MEP_OAM_ID

        # Delete all objects
        for md_idx in range(nr_md):
            self.handler.wb_api.cfm.delete_md(md_id=str(md_idx))
            for ma_idx in range(nr_ma_in_md):
                self.handler.wb_api.cfm.delete_ma(ma_id=str(ma_idx), md_id=str(md_idx))
                for lmep_idx in range(nr_lmep_in_ma):
                    self.handler.wb_api.cfm.delete_lmep(mep_id=oam_id, ma_id=str(ma_idx), md_id=str(md_idx))
                    self.handler.wb_api.cfm.delete_mip(mip_name=str(oam_id), ma_id=str(ma_idx), md_id=str(md_idx))
                    oam_id = oam_id + 1

        self.handler.full_commit()

    @devvm_test()
    def test_lmep_mip_same_name(self):
        """ Verify if LMEP and MIP work with the same name
        """
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name,
                                          flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_mip(
            oam_id=mip_oam_id + down_mep_oam_id,
            name=str(down_mep_mep_id),
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            admin_state=cfm_pb.AdminState.ENABLED,
            md_level=md_level_mip,
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_operdb_count(1, 1, 1), sleep_seconds=1,
                     timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_mip(md_id=md_id, ma_id=ma_id1, mip_name=str(down_mep_mep_id))
        self._uninstall_downmep(delete_md = True)

    @remote_test()
    @pytest.mark.extended_tests
    # this test checks on a big scale how defect state machine works while there are recreate endpoints
    # while there is continuously traffic running, each remote endpoints will pass different defect states since it reach clear defect state
    def test_many_objects_scale_with_traffic_for_1_remote_oper_state_checks(self):
        scale_nr_ma = 200
        scale_nr_rmep = 1 # 1 remote mep per local mep for easily traffic generation
        oam_id = CFM_START_MEP_OAM_ID + 0
        scale_wait_timeout_s = 120

        # Use 10-second interval for scale tests - sending 200 packets takes ~8-9s
        ccm_config_update = deepcopy(ccm_config)
        ccm_config_update.ccm_interval = cfm_pb.CcmIntervalType.INTERVAL_10_SEC
        ccm_config_update.loss_threshold = 1

        if_vlans = []
        for i in range(scale_nr_ma):
            if_vlans.append({"parent": WBoxTestCase.WB_IF_1_NAME, "vlan_tag": (i + 2 * nr_ma + 10),
                             "outer_tpid": 0x8100, "l2_service": True, 'pcp_preserve': True})

        with self.vlans_manager_with_config(if_vlans) as vlans:
            for if_vlan in vlans:
                self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=if_vlan.management_id)

            self.handler.full_commit()

            # create scale configuration
            self.handler.wb_api.cfm.create_md(md_id="1")
            self.handler.wb_api.cfm.create_md(md_id="2")

            for i in range(scale_nr_ma):
                maid_tmp = bytes(CCM.create_maid(ma_name=f"ab{i}", ma_name_format=3))
                self.handler.wb_api.cfm.create_ma(
                    ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                    ma_name=f"ab{i}", md_name="1",
                    flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i)

                for j in range(nr_lmep):
                    rmeps = [i * nr_lmep + j + 10 + k for k in range(scale_nr_rmep)]

                    self.handler.wb_api.cfm.create_lmep(
                        oam_id=oam_id,
                        mep_id=i * nr_lmep + j,
                        md_id="1",
                        ma_id=str(i),
                        group_oam_id=i,
                        interface_name=vlans[i].name,
                        direction=cfm_pb.MepDirection.DOWN,
                        admin_state=cfm_pb.AdminState.ENABLED,
                        outer_tag=vlans[i].sub.vlan_tag,
                        outer_tpid=vlans[i].sub.outer_tpid,
                        ccm_ltm_priority=5,
                        md_level=md_level_down_mep,
                        remote_mep_ids=rmeps,
                        ccm_config=ccm_config_update)

                oam_id = oam_id + 1

            self.handler.full_commit()

            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 2,
                "MA" : scale_nr_ma,
                "Local MEP" : scale_nr_ma * nr_lmep,
                "Remote MEP" : scale_nr_ma * nr_lmep * scale_nr_rmep,
            }), sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

            # Start traffic capture (stats + tcpdump)
            capture = self.cfm_start_traffic_capture("scale_test")

            # Start the CCM traffic
            logger.info("Sending CCM packets")
            event = threading.Event()
            packet_thread = threading.Thread(target=self.send_ccm_multiple_packets_thread,
                                             args=(event, scale_nr_ma, 1, CCM_PKT_PERIOD_10S))
            packet_thread.start()

            try:
                # wait till all remote endpoints were updated to ok state
                for i in range(scale_nr_ma):
                    for j in range(nr_lmep):
                        waiting.wait(lambda: self.assert_operdb(["1", str(i), str(i * nr_lmep + j)],
                                                                                {"fng_state" : 'fng-reset'}),
                                                    sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

                        for k in range(scale_nr_rmep):
                            waiting.wait(lambda: self._assert_operdb_rmep(
                                rmep_config= { "md_id": "1",
                                              "ma_id": str(i),
                                              "mep_id":  str(i * nr_lmep + j),
                                              "rmep_id": str(i * scale_nr_rmep + k + 10),},
                                rmep_oper_expected= {"rmep_state": 'rmep-ok'}),
                                         sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

                # update the maid for all mas by sending ma recreate to trigger remote endpoints update to state failed
                for i in range(scale_nr_ma):
                    maid_tmp = bytes(CCM.create_maid(ma_name=f"abc{i}", ma_name_format=3))
                    self.handler.wb_api.cfm.create_ma(
                        ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                        ma_name=f"ab{i}", md_name="1",
                        flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i,
                        req_type=cfm_pb.CreateRequestType.RECREATE)

                self.handler.full_commit()

                waiting.wait(lambda: self._assert_oam_summary_xray({
                    "MD" : 2,
                    "MA" : scale_nr_ma,
                    "Local MEP" : scale_nr_ma * nr_lmep,
                    "Remote MEP" : scale_nr_ma * nr_lmep * scale_nr_rmep,
                }), sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

                # wait till all remote endpoints were updated to failed state because maid update
                for i in range(scale_nr_ma):
                    for j in range(nr_lmep):
                        waiting.wait(lambda: self.assert_operdb(["1", str(i), str(i * nr_lmep + j)],
                                                                                {"fng_state" : 'fng-defect-reported',
                                                                                "defects" : ['def-remote-ccm', 'def-xcon-ccm']}),
                                                    sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

                        for k in range(scale_nr_rmep):
                            waiting.wait(lambda: self._assert_operdb_rmep(
                                                            rmep_config= {
                                                                "md_id" : "1",
                                                                "ma_id" : str(i),
                                                                "mep_id" :  str(i * nr_lmep + j),
                                                                "rmep_id" : str(i * scale_nr_rmep + k + 10)
                                                                },
                                                            rmep_oper_expected= {
                                                                "rmep_state" : 'rmep-failed'
                                                                },
                                                        ), sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

                # update the maid for all mas by sending ma recreate to trigger remote endpoints update to state ok
                for i in range(scale_nr_ma):
                    maid_tmp = bytes(CCM.create_maid(ma_name=f"ab{i}", ma_name_format=3))
                    self.handler.wb_api.cfm.create_ma(
                        ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                        ma_name=f"ab{i}", md_name="1",
                        flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i,
                        req_type=cfm_pb.CreateRequestType.RECREATE)

                self.handler.full_commit()

                waiting.wait(lambda: self._assert_oam_summary_xray({
                    "MD" : 2,
                    "MA" : scale_nr_ma,
                    "Local MEP" : scale_nr_ma * nr_lmep,
                    "Remote MEP" : scale_nr_ma * nr_lmep * scale_nr_rmep,
                }), sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

                # wait till all remote endpoints were updated to ok state
                for i in range(scale_nr_ma):
                    for j in range(nr_lmep):
                        waiting.wait(lambda: self.assert_operdb(["1", str(i), str(i * nr_lmep + j)],
                                                                                {"fng_state" : 'fng-reset'}),
                                                    sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

                        for k in range(scale_nr_rmep):
                            waiting.wait(lambda: self._assert_operdb_rmep(
                                                            rmep_config= {
                                                                "md_id" : "1",
                                                                "ma_id" : str(i),
                                                                "mep_id" :  str(i * nr_lmep + j),
                                                                "rmep_id" : str(i * scale_nr_rmep + k + 10)
                                                                },
                                                            rmep_oper_expected= {
                                                                "rmep_state" : 'rmep-ok'
                                                                },
                                                        ), sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)
            except Exception:
                self.cfm_dump_failure_diagnostics()
                raise
            finally:
                event.set()
                packet_thread.join()
                self.cfm_stop_traffic_capture(capture)

            oam_id = CFM_START_MEP_OAM_ID + 0

            for i in range(scale_nr_ma):
                for j in range(nr_lmep):
                    self.handler.wb_api.cfm.delete_lmep(md_id="1", ma_id=str(i), mep_id=i * nr_lmep + j)
                    oam_id = oam_id + 1

                self.handler.wb_api.cfm.delete_ma(ma_id=str(i), md_id="1")

            self.handler.wb_api.cfm.delete_md(md_id="1")
            self.handler.wb_api.cfm.delete_md(md_id="2")
            self.handler.full_commit()

            for if_vlan in vlans:
                self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=if_vlan.management_id)

            self.handler.full_commit()

    @pytest.mark.extended_tests
    @remote_test()
    def test_many_objects_scale(self):
        scale_nr_ma = 200
        scale_nr_rmep = 1
        oam_id = CFM_START_MEP_OAM_ID + 0
        scale_wait_timeout_s = 120

        # Use 10-second interval for scale tests - sending 200 packets takes ~8-9s
        ccm_config_update = deepcopy(ccm_config)
        ccm_config_update.ccm_interval = cfm_pb.CcmIntervalType.INTERVAL_10_SEC
        ccm_config_update.loss_threshold = 1

        if_vlans = []
        for i in range(scale_nr_ma):
            if_vlans.append({"parent": WBoxTestCase.WB_IF_1_NAME, "vlan_tag": (i + 2 * nr_ma + 10),
                             "outer_tpid": 0x8100, "l2_service": True, 'pcp_preserve': True})

        with self.vlans_manager_with_config(if_vlans) as vlans:
            for if_vlan in vlans:
                self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=if_vlan.management_id)

            self.handler.full_commit()

            self.handler.wb_api.cfm.create_md(md_id="1")
            self.handler.wb_api.cfm.create_md(md_id="2")

            for i in range(scale_nr_ma):
                maid_tmp = bytes(CCM.create_maid(ma_name=f"ab{i}", ma_name_format=3))
                self.handler.wb_api.cfm.create_ma(
                    ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                    ma_name=f"ab{i}", md_name="1",
                    flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i)

                for j in range(nr_lmep):
                    rmeps = [i * nr_lmep + j + 10 + k for k in range(scale_nr_rmep)]

                    self.handler.wb_api.cfm.create_lmep(
                        oam_id=oam_id,
                        mep_id=i * nr_lmep + j,
                        md_id="1",
                        ma_id=str(i),
                        group_oam_id=i,
                        interface_name=vlans[i].name,
                        direction=cfm_pb.MepDirection.DOWN,
                        admin_state=cfm_pb.AdminState.ENABLED,
                        outer_tag=vlans[i].sub.vlan_tag,
                        outer_tpid=vlans[i].sub.outer_tpid,
                        ccm_ltm_priority=5,
                        md_level=md_level_down_mep,
                        remote_mep_ids=rmeps,
                        ccm_config=ccm_config_update)

                oam_id = oam_id + 1

            self.handler.full_commit()

            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 2,
                "MA" : scale_nr_ma,
                "Local MEP" : scale_nr_ma * nr_lmep,
                "Remote MEP" : scale_nr_ma * nr_lmep * scale_nr_rmep,
            }), sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

            # Start traffic capture (stats + tcpdump)
            capture = self.cfm_start_traffic_capture("scale_rmep_test")

            logger.info("Sending CCM packets")
            event = threading.Event()
            packet_thread = threading.Thread(target=self.send_ccm_multiple_packets_thread,
                                             args=(event, scale_nr_ma, 1, CCM_PKT_PERIOD_10S))
            packet_thread.start()

            try:
                self.cfm_check_all_rmep_states(
                    scale_nr_ma=scale_nr_ma,
                    scale_nr_rmep=scale_nr_rmep,
                    nr_lmep=nr_lmep,
                    expected_state='rmep-ok',
                    timeout_s=scale_wait_timeout_s
                )

                iteration_count = 0
                for _ in range(2):
                    iteration_count += 1
                    logger.info(f"=== Iteration {iteration_count}/2: Testing MAID change ===")

                    # Change MAID to trigger rmep-failed state
                    for i in range(scale_nr_ma):
                        maid_tmp = bytes(CCM.create_maid(ma_name=f"abc{i}", ma_name_format=3))
                        self.handler.wb_api.cfm.create_ma(
                            ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                            ma_name=f"ab{i}", md_name="1",
                            flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i,
                            req_type=cfm_pb.CreateRequestType.RECREATE)

                    self.handler.full_commit()

                    waiting.wait(lambda: self._assert_oam_summary_xray({
                        "MD" : 2,
                        "MA" : scale_nr_ma,
                        "Local MEP" : scale_nr_ma * nr_lmep,
                        "Remote MEP" : scale_nr_ma * nr_lmep * scale_nr_rmep,
                    }), sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

                    self.cfm_check_all_rmep_states(
                        scale_nr_ma=scale_nr_ma,
                        scale_nr_rmep=scale_nr_rmep,
                        nr_lmep=nr_lmep,
                        expected_state='rmep-failed',
                        timeout_s=scale_wait_timeout_s
                    )

                    # Restore original MAID to trigger rmep-ok state
                    for i in range(scale_nr_ma):
                        maid_tmp = bytes(CCM.create_maid(ma_name=f"ab{i}", ma_name_format=3))
                        self.handler.wb_api.cfm.create_ma(
                            ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                            ma_name=f"ab{i}", md_name="1",
                            flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i,
                            req_type=cfm_pb.CreateRequestType.RECREATE)

                    self.handler.full_commit()

                    waiting.wait(lambda: self._assert_oam_summary_xray({
                        "MD" : 2,
                        "MA" : scale_nr_ma,
                        "Local MEP" : scale_nr_ma * nr_lmep,
                        "Remote MEP" : scale_nr_ma * nr_lmep * scale_nr_rmep,
                    }), sleep_seconds=1, timeout_seconds=scale_wait_timeout_s)

                    self.cfm_check_all_rmep_states(
                        scale_nr_ma=scale_nr_ma,
                        scale_nr_rmep=scale_nr_rmep,
                        nr_lmep=nr_lmep,
                        expected_state='rmep-ok',
                        timeout_s=scale_wait_timeout_s
                    )
            finally:
                event.set()
                packet_thread.join()
                self.cfm_stop_traffic_capture(capture)

            oam_id = CFM_START_MEP_OAM_ID + 0

            for i in range(scale_nr_ma):
                for j in range(nr_lmep):
                    self.handler.wb_api.cfm.delete_lmep(md_id="1", ma_id=str(i), mep_id=i * nr_lmep + j)
                    oam_id = oam_id + 1

                self.handler.wb_api.cfm.delete_ma(ma_id=str(i), md_id="1")

            self.handler.wb_api.cfm.delete_md(md_id="1")
            self.handler.wb_api.cfm.delete_md(md_id="2")
            self.handler.full_commit()

            for if_vlan in vlans:
                self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=if_vlan.management_id)

            self.handler.full_commit()


    @pytest.mark.long
    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_many_objects_monolith_scale_bank_placement(self):
        scale_nr_ma = 1000
        scale_nr_rmep = 4
        oam_id = CFM_START_MEP_OAM_ID + 0

        if_vlans = []
        for i in range(scale_nr_ma):
            if_vlans.append({"parent": WBoxTestCase.WB_IF_1_NAME, "vlan_tag": (i + 2 * nr_ma + 10), "outer_tpid": 0x8100, "l2_service": True, 'pcp_preserve': True})

        with self.vlans_manager_with_config(if_vlans) as vlans:
            self.handler.wb_api.cfm.create_md(md_id="1")
            self.handler.wb_api.cfm.create_md(md_id="2")

            for i in range(scale_nr_ma):
                maid_tmp = bytes(CCM.create_maid(ma_name=f"ab{i}", ma_name_format=3))
                self.handler.wb_api.cfm.create_ma(
                    ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                    ma_name=f"ab{i}", md_name="1",
                    flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i)

                for j in range(nr_lmep):
                    rmeps = [k for k in range(scale_nr_rmep)]

                    self.handler.wb_api.cfm.create_lmep(
                        oam_id=oam_id,
                        mep_id=i * nr_lmep + j,
                        md_id="1",
                        ma_id=str(i),
                        group_oam_id=i,
                        interface_name=vlans[i].name,
                        direction=cfm_pb.MepDirection.DOWN,
                        admin_state=cfm_pb.AdminState.ENABLED,
                        ccm_ltm_priority=5,
                        md_level=md_level_down_mep,
                        remote_mep_ids=rmeps,
                        ccm_config=ccm_config)

                oam_id = oam_id + 1

            self.handler.full_commit()

            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 2,
                "MA" : scale_nr_ma,
                "Local MEP" : scale_nr_ma * nr_lmep,
                "Remote MEP" : scale_nr_ma * nr_lmep * scale_nr_rmep,
            }), sleep_seconds=0.5, timeout_seconds=25)

            try:
                self.bcm("dbal table dump table=OAMP_MEP_DB", return_output=True)
            except Exception as e:
                print(e)

            with open('/var/log/dn/last_diag_command', 'r') as last_diag_command_file:
                mep_db_lines = [line for line in last_diag_command_file]

            MEP_DB_BANK_SIZE = 8192
            bank1_first_mep_id = int(MEP_DB_BANK_SIZE / 2) # second half of bank1
            bank5_first_loss_id = (4 * MEP_DB_BANK_SIZE)
            result_type_eth_oam_down_mep_found = False
            result_type_lm_db_found = False

            first_mep_id = bank1_first_mep_id # second half of bank1
            first_mep_counter = 4 #first entry from first half of bank1
            first_mep_found = False

            last_mep_id = bank1_first_mep_id + 4 * (scale_nr_ma - 1)
            last_mep_counter = 4 + last_mep_id - first_mep_id # last entry from first half of bank1
            last_mep_found = False

            first_slm_id = bank5_first_loss_id # start of bank5
            first_slm_found = False

            last_slm_id = bank5_first_loss_id + 4 * (scale_nr_ma - 1)
            last_slm_found = False

            print("Check in OAMP_MEP_DB:")
            print(f"Total entries: {scale_nr_ma}")
            print(f"First MEP id: {first_mep_id}")
            print(f"First MEP counter: {first_mep_counter}")
            print(f"Last MEP id: {last_mep_id}")
            print(f"Last MEP counter: {last_mep_counter}")
            print(f"First SLM id: {first_slm_id}")
            print(f"Last SLM id: {last_slm_id}")

            for i in range(len(mep_db_lines)):
                # validate 1k MEP entries
                if ("Result type  ETH_OAM_DOWN_MEP") in mep_db_lines[i] and "ETH_OAM_DOWN_MEP_EGRESS_INJECTION" not in mep_db_lines[i]:
                    result_type_eth_oam_down_mep_found = True
                    self.assertTrue(f"Total Entries: {scale_nr_ma}" in mep_db_lines[i+1])

                # validate first mep id is first in bank5
                if (f"| 0   | {first_mep_id}   |") in mep_db_lines[i]:
                    first_mep_found = True
                    # validate counter_base is first in second half of bank5
                    self.assertTrue(f"| {first_mep_counter} " in mep_db_lines[i])

                # validate last mep id is first + 1k * 4
                if (f"| {scale_nr_ma - 1} | {last_mep_id}   |") in mep_db_lines[i]:
                    last_mep_found = True
                    # validate counter_base is first + 1k * 4 in second half of bank5
                    self.assertTrue(f"| {last_mep_counter} " in mep_db_lines[i])

                # validate 1k SLM entries
                if ("Result type  LM_DB") in mep_db_lines[i]:
                    result_type_lm_db_found = True
                    self.assertTrue(f"Total Entries: {scale_nr_ma}" in mep_db_lines[i+1])

                # validate first SLM id is first in bank6
                if (f"| 0   | {first_slm_id}  |") in mep_db_lines[i]:
                    first_slm_found = True

                # validate last SLM id is first + 1k * 4
                if (f"| {scale_nr_ma - 1} | {last_slm_id}  |") in mep_db_lines[i]:
                    last_slm_found = True

            try:
                self.assertTrue(result_type_eth_oam_down_mep_found)
                self.assertTrue(first_mep_found)
                self.assertTrue(last_mep_found)
                self.assertTrue(result_type_lm_db_found)
                self.assertTrue(first_slm_found)
                self.assertTrue(last_slm_found)

                print("MEP_DB OK")
            except Exception as e:
                raise e
            finally:

                oam_id = CFM_START_MEP_OAM_ID + 0

                for i in range(scale_nr_ma):
                    for j in range(nr_lmep):
                        self.handler.wb_api.cfm.delete_lmep(md_id="1", ma_id=str(i), mep_id=i * nr_lmep + j)
                        oam_id = oam_id + 1

                    self.handler.wb_api.cfm.delete_ma(md_id="1", ma_id=str(i))

                self.handler.wb_api.cfm.delete_md(md_id="1")
                self.handler.wb_api.cfm.delete_md(md_id="2")
                self.handler.full_commit()

                waiting.wait(lambda: self._assert_oam_summary_xray({
                    "MD" : 0,
                    "MA" : 0,
                    "Local MEP" : 0,
                    "Remote MEP" : 0,
                }), sleep_seconds=0.5, timeout_seconds=25)


    @pytest.mark.long
    @pytest.mark.extended_tests
    def test_many_objects_scale_add_remove_counting(self):
        scale_nr_ma = 1000
        scale_nr_rmep = 4
        oam_id = CFM_START_MEP_OAM_ID + 0
        mip_oam_id = CFM_START_MIP_OAM_ID + 0

        if_vlans = []
        for i in range(scale_nr_ma):
            if_vlans.append({"parent": WBoxTestCase.WB_IF_1_NAME, "vlan_tag": (i + 2 * nr_ma + 10), "outer_tpid": 0x8100, "l2_service": True, 'pcp_preserve': True})

        with self.vlans_manager_with_config(if_vlans) as vlans:
            self.handler.wb_api.cfm.create_md(md_id="1")
            self.handler.wb_api.cfm.create_md(md_id="2")

            for i in range(scale_nr_ma):
                maid_tmp = bytes(CCM.create_maid(ma_name=f"ab{i}", ma_name_format=3))
                self.handler.wb_api.cfm.create_ma(
                    ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                    ma_name=f"ab{i}", md_name="1",
                    flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i)

                for j in range(nr_lmep):
                    rmeps = [k for k in range(scale_nr_rmep)]
                    self.handler.wb_api.cfm.create_lmep(
                        oam_id=oam_id,
                        mep_id=i * nr_lmep + j,
                        md_id="1",
                        ma_id=str(i),
                        group_oam_id=i,
                        interface_name=vlans[i].name,
                        direction=cfm_pb.MepDirection.DOWN,
                        admin_state=cfm_pb.AdminState.ENABLED,
                        ccm_ltm_priority=5,
                        md_level=md_level_down_mep,
                        remote_mep_ids=rmeps,
                        ccm_config=ccm_config)

                    self.handler.wb_api.cfm.create_mip(
                        oam_id=mip_oam_id,
                        name=mip_name,
                        md_id="1",
                        ma_id=str(i),
                        group_oam_id=i,
                        interface_name=vlans[i].name,
                        admin_state=cfm_pb.AdminState.ENABLED,
                        md_level=md_level_mip,
                        req_type=cfm_pb.CreateRequestType.CREATE)

                oam_id = oam_id + 1
                mip_oam_id = mip_oam_id + 1

            self.handler.full_commit()

            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 2,
                "MA" : scale_nr_ma,
                "Local MEP" : scale_nr_ma * nr_lmep,
                "MIP" : scale_nr_ma * nr_lmep,
                "Remote MEP" : scale_nr_ma * nr_lmep * scale_nr_rmep,
            }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            sleep(90) # let redis write queue to get full

            oam_id = CFM_START_MEP_OAM_ID + 0
            mip_oam_id = CFM_START_MIP_OAM_ID + 0

            for i in range(scale_nr_ma):
                for j in range(nr_lmep):
                    self.handler.wb_api.cfm.delete_lmep(md_id="1", ma_id=str(i), mep_id=i * nr_lmep + j)
                    self.handler.wb_api.cfm.delete_mip(md_id="1", ma_id=str(i), mip_name=mip_name)
                    oam_id = oam_id + 1
                    mip_oam_id = mip_oam_id + 1

                self.handler.wb_api.cfm.delete_ma(md_id="1", ma_id=str(i))

            self.handler.wb_api.cfm.delete_md(md_id="1")
            self.handler.wb_api.cfm.delete_md(md_id="2")
            self.handler.full_commit()

    @pytest.mark.skipif(not IS_NCP3, reason="port speed change is supported on ncp3 devices")
    def test_many_objects_port_speed_change(self):
        oam_id = CFM_START_MEP_OAM_ID + 0

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_md(md_id="1")
        self.handler.wb_api.cfm.create_md(md_id="2")

        for i in range(nr_ma):
            maid_tmp = bytes(CCM.create_maid(ma_name=f"ab{i}", ma_name_format=3))
            self.handler.wb_api.cfm.create_ma(
                ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                ma_name=f"ab{i}", md_name="1",
                flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i)

            for j in range(nr_lmep):
                rmeps = [k for k in range(nr_rmep)]

                self.handler.wb_api.cfm.create_lmep(
                    oam_id=oam_id,
                    mep_id=i * nr_lmep + j,
                    md_id="1",
                    ma_id=str(i),
                    group_oam_id=i,
                    interface_name=iface_names[2 * i],
                    direction=cfm_pb.MepDirection.DOWN,
                    admin_state=cfm_pb.AdminState.ENABLED,
                    outer_tag=WBoxTestCase.global_params["outer_tag"][i],
                    outer_tpid=WBoxTestCase.global_params["outer_tpid"][i],
                    ccm_ltm_priority=5,
                    md_level=md_level_down_mep,
                    remote_mep_ids=rmeps,
                    ccm_config=ccm_config)

            oam_id = oam_id + 1

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 2,
            "MA" : nr_ma,
            "Local MEP" : nr_ma * nr_lmep,
            "Remote MEP" : nr_ma * nr_lmep * nr_rmep,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        oam_id = CFM_START_MEP_OAM_ID + 0

        for i in range(nr_ma):
            for j in range(nr_lmep):
                rmeps = [k for k in range(nr_rmep)]

                self.handler.wb_api.cfm.create_lmep(
                    oam_id=oam_id,
                    mep_id=i * nr_lmep + j,
                    md_id="1",
                    ma_id=str(i),
                    group_oam_id=i,
                    interface_name=iface_names[i],
                    direction=cfm_pb.MepDirection.DOWN,
                    admin_state=cfm_pb.AdminState.ENABLED,
                    outer_tag=WBoxTestCase.global_params["outer_tag"][2 * i],
                    outer_tpid=WBoxTestCase.global_params["outer_tpid"][2 * i],
                    ccm_ltm_priority=5,
                    md_level=md_level_down_mep,
                    ccm_config=ccm_config,
                    remote_mep_ids=rmeps,
                    req_type=cfm_pb.CreateRequestType.RECREATE)

                oam_id = oam_id + 1

        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_400GB)
        self.set_iface_oper_state(iface_name=self.WB_IF_1_NAME, is_oper_up=True, is_link_up=True)
        sleep(5)

        oam_id = CFM_START_MEP_OAM_ID + 0

        for i in range(nr_ma):
            for j in range(nr_lmep):
                rmeps = [k for k in range(nr_rmep)]

                self.handler.wb_api.cfm.create_lmep(
                    oam_id=oam_id,
                    mep_id=i * nr_lmep + j,
                    md_id="1",
                    ma_id=str(i),
                    group_oam_id=i,
                    interface_name=iface_names[i],
                    direction=cfm_pb.MepDirection.DOWN,
                    admin_state=cfm_pb.AdminState.ENABLED,
                    outer_tag=WBoxTestCase.global_params["outer_tag"][2 * i + 1],
                    outer_tpid=WBoxTestCase.global_params["outer_tpid"][2 * i + 1],
                    ccm_ltm_priority=5,
                    md_level=md_level_down_mep,
                    ccm_config=ccm_config,
                    remote_mep_ids=rmeps,
                    req_type=cfm_pb.CreateRequestType.RECREATE)

                oam_id = oam_id + 1

        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_100GB)
        self.set_iface_oper_state(iface_name=self.WB_IF_1_NAME, is_oper_up=True, is_link_up=True)
        sleep(5)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 2,
            "MA" : nr_ma,
            "Local MEP" : nr_ma * nr_lmep,
            "Remote MEP" : nr_ma * nr_lmep * nr_rmep,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        oam_id = CFM_START_MEP_OAM_ID + 0
        for i in range(nr_ma):
            for j in range(nr_lmep):
                self.handler.wb_api.cfm.delete_lmep(md_id="1", ma_id=str(i), mep_id=i * nr_lmep + j)
                oam_id = oam_id + 1

            self.handler.wb_api.cfm.delete_ma(md_id="1", ma_id=str(i))

        self.handler.wb_api.cfm.delete_md(md_id="1")
        self.handler.wb_api.cfm.delete_md(md_id="2")
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0,
        }), sleep_seconds=0.5, timeout_seconds=25)

    @pytest.mark.extended_tests
    @pytest.mark.skipif(not IS_NCP3, reason="port speed change is supported on ncp3 devices")
    def test_many_objects_port_speed_change_add_recreate_remove(self):
        oam_id = CFM_START_MEP_OAM_ID + 0

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_md(md_id="1")
        self.handler.wb_api.cfm.create_md(md_id="2")

        for i in range(nr_ma):
            maid_tmp = bytes(CCM.create_maid(ma_name=f"ab{i}", ma_name_format=3))
            self.handler.wb_api.cfm.create_ma(
                ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                ma_name=f"ab{i}", md_name="1",
                flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i)

            for j in range(nr_lmep):
                rmeps = [k for k in range(nr_rmep)]

                self.handler.wb_api.cfm.create_lmep(
                    oam_id=oam_id,
                    mep_id=i * nr_lmep + j,
                    md_id="1",
                    ma_id=str(i),
                    group_oam_id=i,
                    interface_name=iface_names[2 * i],
                    direction=cfm_pb.MepDirection.DOWN,
                    admin_state=cfm_pb.AdminState.ENABLED,
                    outer_tag=WBoxTestCase.global_params["outer_tag"][i],
                    outer_tpid=WBoxTestCase.global_params["outer_tpid"][i],
                    ccm_ltm_priority=5,
                    md_level=md_level_down_mep,
                    remote_mep_ids=rmeps,
                    ccm_config=ccm_config)

            # commit create lmep + rmep
            self.handler.full_commit()

            for j in range(nr_lmep):
                rmeps = [k for k in range(nr_rmep)]

                self.handler.wb_api.cfm.create_lmep(
                    oam_id=oam_id,
                    mep_id=i * nr_lmep + j,
                    md_id="1",
                    ma_id=str(i),
                    group_oam_id=i,
                    interface_name=iface_names[2 * i + 1],
                    direction=cfm_pb.MepDirection.DOWN,
                    admin_state=cfm_pb.AdminState.ENABLED,
                    ccm_ltm_priority=5,
                    md_level=md_level_down_mep,
                    ccm_config=ccm_config,
                    remote_mep_ids=rmeps,
                    req_type=cfm_pb.RECREATE)

            oam_id = oam_id + 1

            logger.info("Before commit recreate all endpoints")

            self.handler.full_commit()

            logger.info("After commit recreate all endpoints")

            oam_id_l = CFM_START_MEP_OAM_ID + 0

            for k in range(i):
                for j in range(nr_lmep):
                    rmeps = [k for k in range(nr_rmep)]

                    self.handler.wb_api.cfm.create_lmep(
                        oam_id=oam_id_l,
                        mep_id=k * nr_lmep + j,
                        md_id="1",
                        ma_id=str(k),
                        group_oam_id=k,
                        interface_name=iface_names[2 * k + 1],
                        direction=cfm_pb.MepDirection.DOWN,
                        admin_state=cfm_pb.AdminState.ENABLED,
                        ccm_ltm_priority=5,
                        md_level=md_level_down_mep,
                        ccm_config=ccm_config,
                        remote_mep_ids=rmeps,
                        req_type=cfm_pb.CreateRequestType.RECREATE)

                    oam_id_l = oam_id_l + 1

            self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_400GB, with_commit=False)

            logger.info("Before commit port speed change")
            self.handler.full_commit()
            logger.info("After commit port speed change")

            oam_id_l = CFM_START_MEP_OAM_ID + 0

            for k in range(i):
                for j in range(nr_lmep):
                    rmeps = [k for k in range(nr_rmep)]

                    self.handler.wb_api.cfm.create_lmep(
                        oam_id=oam_id_l,
                        mep_id=k * nr_lmep + j,
                        md_id="1",
                        ma_id=str(k),
                        group_oam_id=k,
                        interface_name=iface_names[2 * k + 1],
                        direction=cfm_pb.MepDirection.DOWN,
                        admin_state=cfm_pb.AdminState.ENABLED,
                        ccm_ltm_priority=5,
                        md_level=md_level_down_mep,
                        ccm_config=ccm_config,
                        remote_mep_ids=rmeps,
                        req_type=cfm_pb.CreateRequestType.RECREATE)

                    oam_id_l = oam_id_l + 1

            self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_100GB)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 2,
            "MA" : nr_ma,
            "Local MEP" : nr_ma * nr_lmep,
            "Remote MEP" : nr_ma * nr_lmep * nr_rmep,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        oam_id = CFM_START_MEP_OAM_ID + 0

        for i in range(nr_ma):
            for j in range(nr_lmep):
                self.handler.wb_api.cfm.delete_lmep(md_id="1", ma_id=str(i), mep_id=i * nr_lmep + j)
                oam_id = oam_id + 1

            self.handler.wb_api.cfm.delete_ma(md_id="1", ma_id=str(i))

        self.handler.wb_api.cfm.delete_md(md_id="1")
        self.handler.wb_api.cfm.delete_md(md_id="2")
        self.handler.full_commit()

        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_400GB, with_commit=False)

        logger.info("Before commit port speed change")
        self.handler.full_commit()
        logger.info("After commit port speed change")

        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_100GB)
        self.set_iface_oper_state(iface_name=self.WB_IF_1_NAME, is_oper_up=True, is_link_up=True)
        sleep(5)

    @pytest.mark.extended_tests
    @pytest.mark.skipif(not IS_NCP3, reason="port speed change is supported on ncp3 devices")
    def test_many_objects_port_speed_change_add_remove(self):
        oam_id = CFM_START_MEP_OAM_ID + 0

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_md(md_id="1")
        self.handler.wb_api.cfm.create_md(md_id="2")

        for i in range(nr_ma):
            maid_tmp = bytes(CCM.create_maid(ma_name=f"ab{i}", ma_name_format=3))
            self.handler.wb_api.cfm.create_ma(
                ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                ma_name=f"ab{i}", md_name="1",
                flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i)

            for j in range(nr_lmep):
                rmeps = [k for k in range(nr_rmep)]

                self.handler.wb_api.cfm.create_lmep(
                    oam_id=oam_id,
                    mep_id=i * nr_lmep + j,
                    md_id="1",
                    ma_id=str(i),
                    group_oam_id=i,
                    interface_name=iface_names[i],
                    direction=cfm_pb.MepDirection.DOWN,
                    admin_state=cfm_pb.AdminState.ENABLED,
                    outer_tag=WBoxTestCase.global_params["outer_tag"][i],
                    outer_tpid=WBoxTestCase.global_params["outer_tpid"][i],
                    ccm_ltm_priority=5,
                    md_level=md_level_down_mep,
                    remote_mep_ids=rmeps,
                    ccm_config=ccm_config)

            oam_id = oam_id + 1

            logger.info("Before commit create all endpoints")

            self.handler.full_commit()

            logger.info("After commit create all endpoints")

            oam_id_l = CFM_START_MEP_OAM_ID + 0

            for k in range(i):
                for j in range(nr_lmep):
                    rmeps = [k for k in range(nr_rmep)]

                    self.handler.wb_api.cfm.create_lmep(
                        oam_id=oam_id_l,
                        mep_id=k * nr_lmep + j,
                        md_id="1",
                        ma_id=str(k),
                        group_oam_id=k,
                        interface_name=iface_names[k],
                        direction=cfm_pb.MepDirection.DOWN,
                        admin_state=cfm_pb.AdminState.ENABLED,
                        outer_tag=WBoxTestCase.global_params["outer_tag"][i],
                        outer_tpid=WBoxTestCase.global_params["outer_tpid"][i],
                        ccm_ltm_priority=5,
                        md_level=md_level_down_mep,
                        ccm_config=ccm_config,
                        remote_mep_ids=rmeps,
                        req_type=cfm_pb.CreateRequestType.RECREATE)

                    oam_id_l = oam_id_l + 1

            self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_400GB, with_commit=False)

            logger.info("Before commit port speed change")
            self.handler.full_commit()
            logger.info("After commit port speed change")

            oam_id_l = CFM_START_MEP_OAM_ID + 0

            for k in range(i):
                for j in range(nr_lmep):
                    rmeps = [k for k in range(nr_rmep)]

                    self.handler.wb_api.cfm.create_lmep(
                        oam_id=oam_id_l,
                        mep_id=k * nr_lmep + j,
                        md_id="1",
                        ma_id=str(k),
                        group_oam_id=k,
                        interface_name=iface_names[k],
                        direction=cfm_pb.MepDirection.DOWN,
                        admin_state=cfm_pb.AdminState.ENABLED,
                        outer_tag=WBoxTestCase.global_params["outer_tag"][i],
                        outer_tpid=WBoxTestCase.global_params["outer_tpid"][i],
                        ccm_ltm_priority=5,
                        md_level=md_level_down_mep,
                        ccm_config=ccm_config,
                        remote_mep_ids=rmeps,
                        req_type=cfm_pb.CreateRequestType.RECREATE)

                    oam_id_l = oam_id_l + 1

            self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_100GB)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 2,
            "MA" : nr_ma,
            "Local MEP" : nr_ma * nr_lmep,
            "Remote MEP" : nr_ma * nr_lmep * nr_rmep,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        oam_id = CFM_START_MEP_OAM_ID + 0

        for i in range(nr_ma):
            for j in range(nr_lmep):
                self.handler.wb_api.cfm.delete_lmep(md_id="1", ma_id=str(i), mep_id=i * nr_lmep + j)
                oam_id = oam_id + 1

            self.handler.wb_api.cfm.delete_ma(md_id="1", ma_id=str(i))

        self.handler.wb_api.cfm.delete_md(md_id="1")
        self.handler.wb_api.cfm.delete_md(md_id="2")
        self.handler.full_commit()

        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_400GB, with_commit=False)

        logger.info("Before commit port speed change")
        self.handler.full_commit()
        logger.info("After commit port speed change")

        self.set_iface_port_speed(iface_name=self.WB_IF_1_NAME, port_speed=interfaces_pb.port_speed_100GB)
        self.set_iface_oper_state(iface_name=self.WB_IF_1_NAME, is_oper_up=True, is_link_up=True)
        sleep(5)

    def assert_defect_detected_event(self, md_id: str, ma_id: str, md_name: str, ma_name: str, md_level: int, defect: EventDefectType):
        events = self.events_queue.get_all_events(['cfm_defect_condition_detected'])
        assert(len(events))
        logger.info(events[0])
        cfm_event = events[0].cfm_defect_condition_detected
        assert(md_id == cfm_event.maintenance_domain_name)
        assert(ma_id == cfm_event.maintenance_association_name)
        assert(md_name == cfm_event.md_name)
        assert(ma_name == cfm_event.short_ma_name)
        assert(md_level == cfm_event.md_level)
        assert(defect.value == cfm_event.highest_defect)

    def assert_defect_cleared_event(self, md_id: str, ma_id: str, md_name: str, ma_name: str, md_level: int):
        events = self.events_queue.get_all_events(['cfm_defect_condition_cleared'])
        assert(len(events))
        logger.info(events[0])
        cfm_event = events[0].cfm_defect_condition_cleared
        assert(md_id == cfm_event.maintenance_domain_name)
        assert(ma_id == cfm_event.maintenance_association_name)
        assert(md_name == cfm_event.md_name)
        assert(ma_name == cfm_event.short_ma_name)
        assert(md_level == cfm_event.md_level)

    def assert_cfm_maximum_auto_discovered_rmeps_limit_threshold_exceeded(self, threshold, limit):
        events = self.events_queue.get_all_events(['cfm_maximum_auto_discovered_rmeps_limit_threshold_exceeded'])
        assert(len(events))
        logger.info(events[0])
        cfm_event = events[0].cfm_maximum_auto_discovered_rmeps_limit_threshold_exceeded
        assert(threshold == cfm_event.threshold)
        assert(limit == cfm_event.limit)

    def assert_cfm_maximum_auto_discovered_rmeps_limit_threshold_cleared(self, threshold, limit):
        events = self.events_queue.get_all_events(['cfm_maximum_auto_discovered_rmeps_limit_threshold_cleared'])
        assert(len(events))
        logger.info(events[0])
        cfm_event = events[0].cfm_maximum_auto_discovered_rmeps_limit_threshold_cleared
        assert(threshold == cfm_event.threshold)
        assert(limit == cfm_event.limit)

    def assert_cfm_maximum_auto_discovered_rmeps_limit_reached(self, limit):
        events = self.events_queue.get_all_events(['cfm_maximum_auto_discovered_rmeps_limit_reached'])
        assert(len(events))
        logger.info(events[0])
        cfm_event = events[0].cfm_maximum_auto_discovered_rmeps_limit_reached
        assert(limit == cfm_event.limit)

    def assert_cfm_maximum_auto_discovered_rmeps_limit_cleared(self, limit):
        events = self.events_queue.get_all_events(['cfm_maximum_auto_discovered_rmeps_limit_cleared'])
        assert(len(events))
        logger.info(events[0])
        cfm_event = events[0].cfm_maximum_auto_discovered_rmeps_limit_cleared
        assert(limit == cfm_event.limit)

    def assert_operdb(self, keys: list[str], expected: dict) -> bool:
        readobj = COrmObj(LMEP_OPER_PATH, keys)
        readobj.db_get(timeout=10000)

        for field, value in expected.items():
            # use a set to avoid different order of elements in the list
            read_value = readobj.get_field(field)

            if value is None and read_value is not None:
                logger.error(f"{keys}: Assert '{field}' failed! expected: {value} actual: {read_value}. Retrying...")
                return False
            elif (read_value is not None) and (set(value) != set(read_value)):
                logger.error(f"{keys}: Assert '{field}' failed! expected: {value} actual: {read_value}. Retrying...")
                return False

        return True

    def assert_operdb_n_entries(self, keys_list: list[list[str]], expected: dict) -> bool:
        for keys in keys_list:
            if not self.assert_operdb(keys, expected):
                return False

        return True

    @devvm_test()
    def test_defect_rdi_down_mep(self):
        self._prepare_basic_oam_setup()
        self.events_queue.clear_events()

        event = cfm_events.CfmEvent(
            event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_REMOTE_RDI_SET,
            rmep_hw_id=self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id)
        )

        self.handler.wb_api.cfm_events_injector.inject_event(event)
        sleep(ccm_config.fng_alarm_time / 900)

        self.assert_defect_detected_event(md_id, ma_id1, md_name, ma1_name, md_level_down_mep, EventDefectType.someRDIdefect)
        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-defect-reported',
            "highest_priority_defect" : 'def-rdi-ccm',
            "defects" : ['def-rdi-ccm']
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        event.event_type = cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_REMOTE_RDI_CLEAR
        self.handler.wb_api.cfm_events_injector.inject_event(event)
        sleep(ccm_config.fng_reset_time / 900)

        self.assert_defect_cleared_event(md_id, ma_id1, md_name, ma1_name, md_level_down_mep)
        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-reset',
            "highest_priority_defect" : 'none',
            "defects" : None
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._cleanup_basic_oam_setup()

    @devvm_test()
    def test_defect_rdi_up_mep(self):
        self._prepare_basic_oam_setup()
        self.events_queue.clear_events()

        event = cfm_events.CfmEvent(
            event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_REMOTE_RDI_SET,
            rmep_hw_id=self.get_rmep_hw_id(up_mep_oam_id, up_mep_remote_mep_id)
        )

        self.handler.wb_api.cfm_events_injector.inject_event(event)
        sleep(ccm_config.fng_alarm_time / 900)

        self.assert_defect_detected_event(md_id, ma_id2, md_name, ma2_name, md_level_up_mep, EventDefectType.someRDIdefect)

        event.event_type = cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_REMOTE_RDI_CLEAR
        self.handler.wb_api.cfm_events_injector.inject_event(event)
        sleep(ccm_config.fng_reset_time / 900)

        self.assert_defect_cleared_event(md_id, ma_id2, md_name, ma2_name, md_level_up_mep)
        self._cleanup_basic_oam_setup()

    @devvm_test()
    def test_defect_ccm(self):
        def _send_and_check():
            ccm_pkt = (
                Ether(dst='01:80:c2:00:00:32', src='00:01:02:03:04:05') /
                Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
                CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
                CCM(mep_id=down_mep_remote_mep_id,
                    ccm_interval=cfm_pb.CcmIntervalType.INTERVAL_1_SEC, maid=maid1)
            )
            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt,
                wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_CCM_INTERVAL_ERROR],
                cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=down_mep_oam_id))

            events = self.events_queue.get_all_events(['cfm_defect_condition_detected'])

            return len(events) > 0

        self._prepare_basic_oam_setup()
        self.events_queue.clear_events()

        interval_ms = self._ccm_interval_to_ms(ccm_config.ccm_interval)
        waiting.wait(_send_and_check, sleep_seconds=interval_ms / 1100, timeout_seconds=interval_ms / 1000 * 10)
        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-defect-reported',
            "highest_priority_defect" : 'def-error-ccm',
            "defects" : ['def-error-ccm']
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        sleep((interval_ms * defect_clear_interval_multiplier / 900) + (ccm_config.fng_reset_time / 900))
        self.assert_defect_cleared_event(md_id, ma_id1, md_name, ma1_name, md_level_down_mep)
        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-reset',
            "highest_priority_defect" : 'none',
            "defects" : None
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._cleanup_basic_oam_setup()

    @devvm_test()
    def test_multiple_defects(self):
        def send_packet():
            ccm_pkt = (
                Ether(dst='01:80:c2:00:00:32', src='00:01:02:03:04:05') /
                Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
                CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
                CCM(mep_id=down_mep_remote_mep_id,
                    ccm_interval=cfm_pb.CcmIntervalType.INTERVAL_1_SEC, maid=maid2)
            )
            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt,
                wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_MAID_ERROR],
                cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=down_mep_oam_id))

        self._prepare_basic_oam_setup()
        self.events_queue.clear_events()

        self.send_good_packet(md_level_down_mep, down_mep_remote_mep_id, down_mep_oam_id, maid1, 10)
        self.send_good_packet(md_level_up_mep, up_mep_remote_mep_id, up_mep_oam_id, maid2, 11)

        waiting.wait(lambda: self._assert_operdb_rmep(
            rmep_config = {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id),
                "rmep_id" : str(down_mep_remote_mep_id)
            },
            rmep_oper_expected = {
                "rmep_id" : down_mep_remote_mep_id,
                "port_status_tlv": "no-port-state-tlv",
                "interface_status_tlv": "no-interface-status-tlv"
            },
        ), sleep_seconds=1, timeout_seconds=25)

        event = cfm_events.CfmEvent(
            event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_INTERFACE_DOWN,
            rmep_hw_id=self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id)
        )

        # Inject event to trigger MACstatus defect
        self.handler.wb_api.cfm_events_injector.inject_event(event)
        sleep(ccm_config.fng_alarm_time / 900)

        lmep_xray = self.handler.get_xray_stats("/cfm/local_meps")[0]
        assert(lmep_xray["rdi_tx"] == "true")

        # Check if defect detected system event was sent
        self.assert_defect_detected_event(md_id, ma_id1, md_name, ma1_name, md_level_down_mep, EventDefectType.someMACstatusDefect)

        # Check rmep operdata
        waiting.wait(lambda: self._assert_operdb_rmep(
            rmep_config = {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id),
                "rmep_id" : str(down_mep_remote_mep_id)
            },
            rmep_oper_expected = {
                "rmep_id" : down_mep_remote_mep_id,
                "port_status_tlv": "no-port-state-tlv",
                "interface_status_tlv": "down"
            },
        ), sleep_seconds=1, timeout_seconds=25)

        # Send wrong CCM packet to trigger xCon error, which should be the highest priority defect
        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-defect-reported',
            "highest_priority_defect" : 'def-xcon-ccm',
            "defects" : ['def-mac-status', 'def-xcon-ccm']
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s, on_poll=send_packet)

        lmep_xray = self.handler.get_xray_stats("/cfm/local_meps")[0]
        assert(lmep_xray["rdi_tx"] == "true")

        # Check if system event was sent for the new highest defect
        self.assert_defect_detected_event(md_id, ma_id1, md_name, ma1_name, md_level_down_mep, EventDefectType.xconCCMdefect)

        # Wait until xCon defect is cleared
        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-defect-reported',
            "highest_priority_defect" : 'def-mac-status',
            "defects" : ['def-mac-status']
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        sleep(ccm_config.fng_alarm_time / 900)
        lmep_xray = self.handler.get_xray_stats("/cfm/local_meps")[0]
        assert(lmep_xray["rdi_tx"] == "true")

        # After clearing a defect, a new system event should be sent for the remaining highest defect above threshold
        self.assert_defect_detected_event(md_id, ma_id1, md_name, ma1_name, md_level_down_mep, EventDefectType.someMACstatusDefect)

        # Clear last defect
        event.event_type = cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_INTERFACE_UP
        self.handler.wb_api.cfm_events_injector.inject_event(event)
        event.event_type = cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_PORT_UP
        self.handler.wb_api.cfm_events_injector.inject_event(event)

        sleep(ccm_config.fng_reset_time / 900)

        lmep_xray = self.handler.get_xray_stats("/cfm/local_meps")[0]
        assert(lmep_xray["rdi_tx"] == "false")

        # Check if system event was sent for clearing the last defect
        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-reset',
            "highest_priority_defect" : 'none',
            "defects" : None
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)
        self.assert_defect_cleared_event(md_id, ma_id1, md_name, ma1_name, md_level_down_mep)

        waiting.wait(lambda: self._assert_operdb_rmep(
            rmep_config = {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id),
                "rmep_id" : str(down_mep_remote_mep_id)
            },
            rmep_oper_expected = {
                "rmep_id" : down_mep_remote_mep_id,
                "port_status_tlv": "up",
                "interface_status_tlv": "up"
            },
        ), sleep_seconds=1, timeout_seconds=25)

        self._cleanup_basic_oam_setup()

    @devvm_test()
    def test_defect_reported_after_threshold_change(self):
        ccm_config_dp = deepcopy(ccm_config)
        ccm_config_dp.lowest_priority_defect = cfm_pb.LowestAlarmPriorityType.XCON

        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        iface_names = WBoxTestCase.global_params["interfaces"]

        # Create LMEP with lowest alam priority set to XCON
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config_dp,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()
        sleep(1)

        event = cfm_events.CfmEvent(
            event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_INTERFACE_DOWN,
            rmep_hw_id=self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id)
        )

        # Inject event to trigger MACstatus defect
        self.handler.wb_api.cfm_events_injector.inject_event(event)
        sleep(ccm_config.fng_alarm_time / 900)

        # Defect should be present but not reported
        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-reset',
            "highest_priority_defect" : 'def-mac-status',
            "defects" : ['def-mac-status']
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Change lowest priority defect to include MACstatus defect
        ccm_config_dp.lowest_priority_defect = cfm_pb.LowestAlarmPriorityType.MAC_REMOTE_ERROR_XCON
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config_dp,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        # Defect should be reported after threshold change
        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-defect-reported',
            "highest_priority_defect" : 'def-mac-status',
            "defects" : ['def-mac-status']
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.assert_defect_detected_event(md_id, ma_id1, md_name, ma1_name, md_level_down_mep, EventDefectType.someMACstatusDefect)

        ccm_config_dp.lowest_priority_defect = cfm_pb.LowestAlarmPriorityType.XCON
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config_dp,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-reset',
            "highest_priority_defect" : 'def-mac-status',
            "defects" : ['def-mac-status']
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Cleanup
        self._uninstall_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

    @devvm_test()
    def test_defect_mac_status_multiple_rmeps(self):
        iface_names = WBoxTestCase.global_params["interfaces"]
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id, down_mep_remote_mep_id + 1, down_mep_remote_mep_id + 2],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        event = cfm_events.CfmEvent(
            event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_PORT_DOWN,
            rmep_hw_id=self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id)
        )
        self.handler.wb_api.cfm_events_injector.inject_event(event)
        sleep(3)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-reset',
            "highest_priority_defect" : 'none',
            "defects" : None
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        event.event_type = cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_INTERFACE_DOWN
        self.handler.wb_api.cfm_events_injector.inject_event(event)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-defect-reported',
            "highest_priority_defect" : 'def-mac-status',
            "defects" : ['def-mac-status']
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        event.event_type = cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_INTERFACE_UP
        self.handler.wb_api.cfm_events_injector.inject_event(event)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-reset',
            "highest_priority_defect" : 'none',
            "defects" : None
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        event.event_type = cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_PORT_DOWN
        event.rmep_hw_id = self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id + 1)
        self.handler.wb_api.cfm_events_injector.inject_event(event)
        event.rmep_hw_id = self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id + 2)
        self.handler.wb_api.cfm_events_injector.inject_event(event)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-defect-reported',
            "highest_priority_defect" : 'def-mac-status',
            "defects" : ['def-mac-status']
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        event.event_type = cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_PORT_UP
        self.handler.wb_api.cfm_events_injector.inject_event(event)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-reset',
            "highest_priority_defect" : 'none',
            "defects" : None
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)

        self.handler.full_commit()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_clear_rmep_defect_on_recreate(self):
        def recreate_lmep(level):
            self.handler.wb_api.cfm.create_lmep(
                oam_id=down_mep_oam_id,
                mep_id=down_mep_mep_id,
                md_id=md_id,
                ma_id=ma_id1,
                group_oam_id=group_id1,
                interface_name=WBoxTestCase.global_params["interfaces"][0],
                direction=cfm_pb.MepDirection.DOWN,
                admin_state=cfm_pb.AdminState.ENABLED,
                outer_tag=WBoxTestCase.global_params["outer_tag"][0],
                outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
                ccm_ltm_priority=5,
                md_level=level,
                ccm_config=ccm_config,
                remote_mep_ids=[down_mep_remote_mep_id],
                req_type=cfm_pb.CreateRequestType.RECREATE)
            self.handler.full_commit()

        self._install_downmep()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        event = threading.Event()
        packet_thread = threading.Thread(target=self.send_ccm_packets_thread, args=(event, ))

        try:
            packet_thread.start()
            sleep(5)

            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
                "fng_state" : 'fng-reset',
                "highest_priority_defect" : 'none',
                "defects" : None
            }), sleep_seconds=0.5, timeout_seconds=10)

            recreate_lmep(md_level_down_mep + 1)

            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
                "fng_state" : 'fng-defect-reported',
                "highest_priority_defect" : 'def-xcon-ccm',
                "defects" : ['def-remote-ccm', 'def-xcon-ccm']
            }), sleep_seconds=0.5, timeout_seconds=10)

            recreate_lmep(md_level_down_mep)

            waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
                "fng_state" : 'fng-reset',
                "highest_priority_defect" : 'none',
                "defects" : None
            }), sleep_seconds=0.5, timeout_seconds=20)
        except Exception as e:
            raise e
        finally:
            event.set()
            packet_thread.join()

        self._uninstall_downmep()

    @devvm_test()
    def test_maximum_mds_add_and_remove_md_same_commit(self):
        """ Verify maximum MD exceeded fails the commit.
            Verify that if MAs are added and removed in the same commit,
            they are counted correctly. I.e. Removed subtracted before validation
        """
        nr_md = 2000

        for md_idx in range(nr_md):
            self.handler.wb_api.cfm.create_md(md_id=str(md_idx))

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : nr_md,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0,
        }), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        # Add an MD to exceed maximum count
        self.handler.wb_api.cfm.create_md(md_id=str(nr_md))

        with self.assertRaises(gen_dn_api.InvalidResponseCodeException):
            self.handler.full_commit()

        self.handler.rollback()

        # Add and remove MD in the same commit
        self.handler.wb_api.cfm.delete_md(md_id='0')
        self.handler.wb_api.cfm.create_md(md_id=str(nr_md))
        self.handler.full_commit()
        sleep(2)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : nr_md,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0,
        }), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        # Delete all objects
        for md_idx in range(1, nr_md + 1):
            self.handler.wb_api.cfm.delete_md(md_id=str(md_idx))
        self.handler.full_commit()

    @devvm_test()
    def test_maximum_mas_add_and_remove_ma_same_commit(self):
        """ Verify maximum MA exceeded fails the commit.
            Verify that if MAs are added and removed in the same commit,
            they are counted correctly. I.e. Removed subtracted before validation
        """
        nr_ma_in_md = 2000

        self.handler.wb_api.cfm.create_md(md_id='1')

        for ma_idx in range(nr_ma_in_md):
            maid_tmp = bytes(CCM.create_maid(ma_name=str(ma_idx), ma_name_format=3))
            self.handler.wb_api.cfm.create_ma(
                ma_id=str(ma_idx), maid48=bytes(maid_tmp), md_id='1',
                ma_name=f"ab{ma_idx}", md_name='1',
                flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=ma_idx)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : nr_ma_in_md,
            "Local MEP" : 0,
            "Remote MEP" : 0,
        }), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        # Add an MA to exceed maximum count
        maid_tmp = bytes(CCM.create_maid(ma_name=str(nr_ma_in_md), ma_name_format=3))
        self.handler.wb_api.cfm.create_ma(
            ma_id=str(nr_ma_in_md), maid48=bytes(maid_tmp), md_id='1',
            ma_name=f"ab{nr_ma_in_md}", md_name='1',
            flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=nr_ma_in_md)

        with self.assertRaises(gen_dn_api.InvalidResponseCodeException):
            self.handler.full_commit()

        self.handler.rollback()

        # Add and remove MA in the same commit
        self.handler.wb_api.cfm.delete_ma(ma_id='0', md_id='1')
        self.handler.wb_api.cfm.create_ma(
            ma_id=str(nr_ma_in_md), maid48=bytes(maid_tmp), md_id='1',
            ma_name=f"ab{nr_ma_in_md}", md_name='1',
            flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=nr_ma_in_md)
        self.handler.full_commit()

        sleep(2)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : nr_ma_in_md,
            "Local MEP" : 0,
            "Remote MEP" : 0,
        }), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        # Delete all objects
        for ma_idx in range(1, nr_ma_in_md + 1):
            self.handler.wb_api.cfm.delete_ma(ma_id=str(ma_idx), md_id='1')
        self.handler.wb_api.cfm.delete_md(md_id='1')
        self.handler.full_commit()

    @devvm_test()
    def test_maximum_lmeps_add_and_remove_lmep_same_commit(self):
        """ Verify that if LMEPs are added and removed in the same commit,
            they are counted correctly. I.e. Removed subtracted before validation
            Fix for: https://drivenets.atlassian.net/browse/SW-178042
        """
        nr_ma_in_md = 1000
        nr_lmep_in_ma = 1
        oam_id = CFM_START_MEP_OAM_ID

        # Create all objects
        self.handler.wb_api.cfm.create_md(md_id='1')

        for ma_idx in range(nr_ma_in_md):
            maid_tmp = bytes(CCM.create_maid(ma_name=f"{ma_idx}", ma_name_format=3))
            self.handler.wb_api.cfm.create_ma(
                ma_id=str(ma_idx), maid48=bytes(maid_tmp), md_id='1',
                ma_name=f"ab{ma_idx}", md_name='1',
                flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=oam_id)

            for _ in range(nr_lmep_in_ma):
                self.handler.wb_api.cfm.create_lmep(
                    oam_id=oam_id,
                    mep_id=oam_id,
                    md_id='1',
                    ma_id=str(ma_idx),
                    group_oam_id=ma_idx,
                    interface_name=self.WB_IF_1_NAME,
                    direction=cfm_pb.MepDirection.DOWN,
                    admin_state=cfm_pb.AdminState.ENABLED,
                    outer_tag=10,
                    outer_tpid=0x8100,
                    ccm_ltm_priority=5,
                    md_level=md_level_down_mep)

                oam_id = oam_id + 1

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : nr_ma_in_md,
            "Local MEP" : nr_ma_in_md * nr_lmep_in_ma,
            "Remote MEP" : 0,
        }), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        oam_id = CFM_START_MEP_OAM_ID

        # Add an LMEP to exceed maximum count
        self.handler.wb_api.cfm.create_lmep(
            oam_id=oam_id,
            mep_id=1001,
            md_id='1',
            ma_id='0',
            group_oam_id=0,
            interface_name=self.WB_IF_1_NAME,
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=10,
            outer_tpid=0x8100,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep)

        with self.assertRaises(gen_dn_api.InvalidResponseCodeException):
            self.handler.full_commit()

        self.handler.rollback()

        # Remove and add LMEP in the same commit
        self.handler.wb_api.cfm.delete_lmep(md_id='1', ma_id='0', mep_id=0)
        self.handler.wb_api.cfm.create_lmep(
            oam_id=oam_id,
            mep_id=1001,
            md_id='1',
            ma_id='0',
            group_oam_id=0,
            interface_name=self.WB_IF_1_NAME,
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=10,
            outer_tpid=0x8100,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep)

        # Verify that the number of LMEP is not counted as 1001
        self.handler.full_commit()
        sleep(1)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : nr_ma_in_md,
            "Local MEP" : nr_ma_in_md * nr_lmep_in_ma,
            "Remote MEP" : 0,
        }), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        # Delete all objects
        self.handler.wb_api.cfm.delete_md(md_id='1')
        self.handler.wb_api.cfm.delete_ma(ma_id='0', md_id='1')
        self.handler.wb_api.cfm.delete_lmep(mep_id=1001, ma_id='0', md_id='1')

        for ma_idx in range(1, nr_ma_in_md):
            oam_id = oam_id + 1
            self.handler.wb_api.cfm.delete_ma(ma_id=str(ma_idx), md_id='1')
            self.handler.wb_api.cfm.delete_lmep(mep_id=oam_id, ma_id=str(ma_idx), md_id='1')

        self.handler.full_commit()

    def test_maximum_rmeps_exceeded(self):
        # create downmep with empty remote list
        # these will add a downmep with no remote meps
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # build the remote mep list
        max_rmep = 8191
        iface_names = WBoxTestCase.global_params["interfaces"]
        rmeps = [k for k in range(max_rmep + 1)]

        # send a local mep update with the max number of remote meps
        # this will return a failure proto message causing the commit to fail
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=rmeps,
            req_type=cfm_pb.CreateRequestType.UPDATE)

        with self.assertRaises(gen_dn_api.InvalidResponseCodeException):
            self.handler.full_commit()

        self.handler.rollback()

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

    def test_delete_non_existent_ma(self):
        self._prepare_basic_oam_setup()

        with self.assertRaises(gen_dn_api.InvalidResponseCodeException):
            self.handler.wb_api.cfm.delete_ma(md_id="non_existent_md", ma_id="non_existent_ma")
            self.handler.full_commit()

        self.handler.rollback()

        self._cleanup_basic_oam_setup()

    def _prepare_check_counters(self, counter_name, is_upmep):
        if is_upmep:
            mep_name = up_mep_mep_id
            ma_id = ma_id2
            mep_name = up_mep_mep_id
        else:
            mep_name = down_mep_mep_id
            ma_id = ma_id1
            mep_name = down_mep_mep_id

        (counters, counters_summary) = self._get_current_counters(lmep_config={
            "md_id" : md_id,
            "ma_id" : ma_id,
            "mep_id" : str(mep_name)})

        lmep_oper_expected = {}
        for key, _ in counters.items():
            lmep_oper_expected[key] = None

        lmep_oper_expected[counter_name] = 1
        lmep_oper_expected['ccm_out'] = 1

        lmep_oper_summary_expected = {}
        for key, _ in counters_summary.items():
            lmep_oper_summary_expected[key] = None

        lmep_oper_summary_expected['ccm_out'] = 1
        lmep_oper_summary_expected[counter_name] = 1

        return (lmep_oper_expected, lmep_oper_summary_expected, counters, counters_summary)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_wrong_pdu_both_directions(self):
        self._prepare_basic_oam_setup() # install and verify DownMep + UpMep

        invalid_opcode = 25
        ccm_pkt_down_wrong_pdu = (
            Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=10, prio=5, type=0x8902) /
            CFM(md_level=md_level_down_mep, opcode=invalid_opcode) /
            CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
        )

        ccm_pkt_up_wrong_pdu = (
            Ether(dst=f'01:80:c2:00:00:3{md_level_up_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=10, prio=5, type=0x8902) /
            CFM(md_level=md_level_up_mep, opcode=invalid_opcode) /
            CCM(mep_id=up_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid2)
        )

        down_lmep_oper_expected, down_lmep_oper_summary_expected, down_counters, down_counters_summary = self._prepare_check_counters(counter_name='unsupported_cfm_pdu', is_upmep=False)
        up_lmep_oper_expected, up_lmep_oper_summary_expected, up_counters, up_counters_summary         = self._prepare_check_counters(counter_name='unsupported_cfm_pdu', is_upmep=True)

        # downmep
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down_wrong_pdu, number_of_packets=1)

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)
                },
            current_lmep=down_counters,
            current_summary_lmep=down_counters_summary,
            lmep_oper_expected=down_lmep_oper_expected,
            lmep_oper_summary_expected=down_lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        # upmep
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_up_wrong_pdu, number_of_packets=1)

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id2,
                "mep_id" : str(up_mep_mep_id)
                },
            current_lmep=up_counters,
            current_summary_lmep=up_counters_summary,
            lmep_oper_expected=up_lmep_oper_expected,
            lmep_oper_summary_expected=up_lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self._cleanup_basic_oam_setup() # delete and verify DownMep + UpMep

    @devvm_test()
    def test_no_oam_processing_commit(self):
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.full_commit()

        sleep(1)

        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    @devvm_test()
    def test_no_defects_after_admin_disabled(self):
        self._prepare_basic_oam_setup()

        event = cfm_events.CfmEvent(
            event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_REMOTE_RDI_SET,
            rmep_hw_id=self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id)
        )

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm_events_injector.inject_event(event)
        sleep(ccm_config.fng_alarm_time / 900)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-defect-reported',
            "highest_priority_defect" : 'def-rdi-ccm',
            "defects" : ['def-rdi-ccm']
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # switch LMEP admin_state to disabled
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.DISABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()
        sleep(2)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-reset',
            "highest_priority_defect" : 'none',
            "defects" : None
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._cleanup_basic_oam_setup()

    @devvm_test()
    def test_defect_cleared_after_rmep_delete(self):
        """ Verify that defect is cleared after RMEP that caused the defect is deleted
        """
        def has_defect():
            xr_data = self.handler.get_xray_stats("/cfm/local_meps")
            assert len(xr_data) == 1
            return xr_data[0]['fng_state'] != 'FNG_RESET'

        self._install_downmep()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        iface_names = WBoxTestCase.global_params["interfaces"]
        event_to = cfm_events.CfmEvent(
            event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_CCM_TIMEOUT,
            rmep_hw_id=self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id)
        )
        self.handler.wb_api.cfm_events_injector.inject_event(event_to)

        waiting.wait(lambda: has_defect(), sleep_seconds=0.5, timeout_seconds=wait_timeout_s,
                                     on_poll=lambda: logger.info("Waiting for defect to be detected"))

        # Delete RMEP
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

        waiting.wait(lambda: not has_defect(), sleep_seconds=0.5, timeout_seconds=wait_timeout_s,
                                     on_poll=lambda: logger.info("Waiting for defect to be cleared"))

        self._uninstall_downmep()

    def test_linktrace(self):
        pass

    @devvm_test()
    def test_linktrace_mep_reply(self):
        """ Test LTR reply with all fields correct
        """
        def pkt_filter(x):
            return x.haslayer(CFM)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self._prepare_basic_oam_setup()
        original_mac = "aa:aa:bb:bb:cc:cc"

        self.handler.fpm_api.send_bridge_domain_mac_add_msg(
            bd_id=0,
            vsi_id=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
            action_type=BdLocalMacAdd.ActionType.NEW,
            ifindex=self.handler.api.interface.get_interface(iface_names[0]).interface.get_interface.data.management_id,
            mac_bytes=mac_2_bytes(bd_target_mac))

        sleep(1)

        ltm = (Ether() /
               Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
               CFM(md_level=md_level_down_mep, opcode=LTM.opcode) /
               LTM(use_fdb_only=1, transaction_id=99, ttl=20,
                    original_mac=original_mac, target_mac=WBoxTestCase.global_params["my_cfm_mac"][0],
                    tlv_list=[
                        SenderIDTlv(chassis_id="80:55:77:22:33:15"),
                        LtmEgressIdentifierTlv(initiator_mac="aa:22:33:44:55:66", initiator_id=66)]))

        iface_vsi = self.if_internal_idx_2_vsi(self.handler.api.interface.get_interface(iface_names[0]).interface.get_interface.data.internal_index)
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm,
            wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_LTM],
            cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=down_mep_oam_id, vsi=iface_vsi))

        rx_packets = self.handler.data_communicator.rx(number_of_packets=1)

        assert len(rx_packets) == 1
        ltr = rx_packets[0]
        assert ltr[Ether].src == WBoxTestCase.global_params["my_cfm_mac"][0]
        assert ltr[Ether].dst == original_mac
        assert ltr.haslayer(Dot1Q)
        assert ltr[Dot1Q].vlan == 10
        assert ltr.haslayer(CFM)
        assert ltr[CFM].opcode == LTR.opcode
        assert ltr[CFM].md_level == md_level_down_mep
        assert ltr.haslayer(LTR)
        assert ltr[LTR].use_fdb_only == 1
        assert ltr[LTR].terminal_mep == 1
        assert ltr[LTR].fwd_yes == 0
        assert ltr[LTR].tlv_offset == 6
        assert ltr[LTR].transaction_id == 99
        assert ltr[LTR].ttl == 19
        assert ltr[LTR].relay_action == 1
        assert len(ltr[LTR].tlv_list) == 3

        egress_id_tlv = ltr[LTR].tlv_list[0]
        assert egress_id_tlv.type == 8
        assert egress_id_tlv.length == 16
        assert egress_id_tlv.last_egress_id == 66
        assert egress_id_tlv.last_egress_mac == "aa:22:33:44:55:66"

        reply_ingress_tlv = ltr[LTR].tlv_list[1]
        assert reply_ingress_tlv.type == 5
        assert reply_ingress_tlv.length == 7
        assert reply_ingress_tlv.ingress_mac == WBoxTestCase.global_params["my_cfm_mac"][0]

        self._cleanup_basic_oam_setup()

    @devvm_test()
    def test_linktrace_mep_reply_no_tlv(self):
        """ Test LTR reply has no TLVs when LTM has no TLVs
        """
        def pkt_filter(x):
            return x.haslayer(CFM)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self._prepare_basic_oam_setup()

        self.handler.fpm_api.send_bridge_domain_mac_add_msg(
            bd_id=0,
            vsi_id=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
            action_type=BdLocalMacAdd.ActionType.NEW,
            ifindex=self.handler.api.interface.get_interface(iface_names[0]).interface.get_interface.data.management_id,
            mac_bytes=mac_2_bytes(bd_target_mac))

        sleep(1)

        ltm = (Ether() /
               Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
               CFM(md_level=md_level_down_mep, opcode=LTM.opcode) /
               LTM(use_fdb_only=1, transaction_id=99, ttl=20,
                    original_mac="aa:aa:bb:bb:cc:cc", target_mac=WBoxTestCase.global_params["my_cfm_mac"][0],
                    tlv_list=[SenderIDTlv(chassis_id="80:55:77:22:33:15")]))

        iface_vsi = self.if_internal_idx_2_vsi(self.handler.api.interface.get_interface(iface_names[0])
                                               .interface.get_interface.data.internal_index)
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm,
            wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_LTM],
            cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=down_mep_oam_id, vsi=iface_vsi))

        rx_packets = self.handler.data_communicator.rx(number_of_packets=1)

        assert len(rx_packets) == 1
        ltr = rx_packets[0]
        assert ltr.haslayer(CFM)
        assert ltr[CFM].opcode == LTR.opcode
        assert ltr.haslayer(LTR)
        assert ltr[LTR].use_fdb_only == 1
        assert ltr[LTR].terminal_mep == 0
        assert ltr[LTR].fwd_yes == 0
        assert ltr[LTR].tlv_offset == 6
        assert ltr[LTR].transaction_id == 99
        assert ltr[LTR].ttl == 19
        assert ltr[LTR].relay_action == 1
        assert len(ltr[LTR].tlv_list) == 1

        self._cleanup_basic_oam_setup()

    @devvm_test()
    def test_linktrace_mep_reply_ttl_zero(self):
        """ Test LTM packet is dropped when TTL = 0
        """
        def pkt_filter(x):
            return x.haslayer(CFM)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self._prepare_basic_oam_setup()

        self.handler.fpm_api.send_bridge_domain_mac_add_msg(
            bd_id=0,
            vsi_id=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
            action_type=BdLocalMacAdd.ActionType.NEW,
            ifindex=self.handler.api.interface.get_interface(iface_names[0]).interface.get_interface.data.management_id,
            mac_bytes=mac_2_bytes(bd_target_mac))

        sleep(1)

        ltm = (Ether() /
               Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
               CFM(md_level=md_level_down_mep, opcode=LTM.opcode) /
               LTM(use_fdb_only=1, transaction_id=99, ttl=0,
                    original_mac="aa:aa:bb:bb:cc:cc", target_mac=WBoxTestCase.global_params["my_cfm_mac"][0],
                    tlv_list=[LtmEgressIdentifierTlv(initiator_mac="aa:22:33:44:55:66", initiator_id=66)]))

        iface_vsi = self.if_internal_idx_2_vsi(self.handler.api.interface.get_interface(iface_names[0])
                                               .interface.get_interface.data.internal_index)
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm,
            wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_LTM],
            cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=down_mep_oam_id, vsi=iface_vsi))

        rx_packets = self.handler.data_communicator.rx(number_of_packets=1)
        assert len(rx_packets) == 0

        self._cleanup_basic_oam_setup()

    @devvm_test()
    def test_linktrace_mep_reply_mac_mismatch(self):
        """ Test LTM packet is dropped on terminal MEP when my_cfm_mac doesn't match target MAC
        """
        def pkt_filter(x):
            return x.haslayer(CFM)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self._prepare_basic_oam_setup()

        self.handler.fpm_api.send_bridge_domain_mac_add_msg(
            bd_id=0,
            vsi_id=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
            action_type=BdLocalMacAdd.ActionType.NEW,
            ifindex=self.handler.api.interface.get_interface(iface_names[0]).interface.get_interface.data.management_id,
            mac_bytes=mac_2_bytes(bd_target_mac))

        sleep(1)

        ltm = (Ether() /
               Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
               CFM(md_level=md_level_down_mep, opcode=LTM.opcode) /
               LTM(use_fdb_only=1, transaction_id=99, ttl=0,
                    original_mac="aa:aa:bb:bb:cc:cc", target_mac=WBoxTestCase.global_params["my_cfm_mac"][0],
                    tlv_list=[LtmEgressIdentifierTlv(initiator_mac="aa:22:33:44:55:66", initiator_id=66)]))

        iface_vsi = self.if_internal_idx_2_vsi(self.handler.api.interface.get_interface(iface_names[0])
                                               .interface.get_interface.data.internal_index)
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm,
            wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_LTM],
            cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=down_mep_oam_id, vsi=iface_vsi))

        rx_packets = self.handler.data_communicator.rx(number_of_packets=1)
        assert len(rx_packets) == 0

        self._cleanup_basic_oam_setup()

    @devvm_test()
    def test_linktrace_vlan_tags(self):
        def pkt_filter(x):
            return x.haslayer(CFM)

        def diff_counters(cnt_before, diff):
            cnt_bf_mep, cnt_bf_summary = cnt_before
            cnt_af_mep, cnt_af_summary = self._get_current_counters(lmep_config={
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)})

            if cnt_af_mep['ltr_out'] - cnt_bf_mep['ltr_out'] != diff:
                logger.error("'ltr_out' expected diff {diff} but got {cnt_af_mep['ltr_out'] - cnt_bf_mep['ltr_out']}")
                return False

            if cnt_af_summary['ltr_out'] - cnt_bf_summary['ltr_out'] != diff:
                logger.error("'ltr_out' expected diff {diff} but got {cnt_af_summary['ltr_out'] - cnt_bf_summary['ltr_out']}")
                return False

            if cnt_af_mep['ltm_in'] - cnt_bf_mep['ltm_in'] != diff:
                logger.error("'ltm_in' expected diff {diff} but got {cnt_af_mep['ltm_in'] - cnt_bf_mep['ltm_in']}")
                return False

            if cnt_af_summary['ltm_in'] - cnt_bf_summary['ltm_in'] != diff:
                logger.error("'ltm_in' expected diff {diff} but got {cnt_af_summary['ltm_in'] - cnt_bf_summary['ltm_in']}")
                return False

            return True

        self._prepare_basic_oam_setup()
        cnt_before = self._get_current_counters(lmep_config={
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)})

        ltm_untagged = (Ether() /
                        CFM(md_level=2, opcode=LTM.opcode) /
                        LTM(use_fdb_only=1, transaction_id=99, ttl=3,
                            original_mac="aa:aa:bb:bb:cc:cc", target_mac=WBoxTestCase.global_params["my_cfm_mac"][0],
                            tlv_list=[LtmEgressIdentifierTlv(initiator_mac="aa:22:33:44:55:66", initiator_id=66)]))

        ltm_1tagged = (Ether() /
                       Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
                       CFM(md_level=2, opcode=LTM.opcode) /
                       LTM(use_fdb_only=1, transaction_id=99, ttl=3,
                           original_mac="aa:aa:bb:bb:cc:cc", target_mac=WBoxTestCase.global_params["my_cfm_mac"][0],
                           tlv_list=[LtmEgressIdentifierTlv(initiator_mac="aa:22:33:44:55:66", initiator_id=66)]))

        ltm_2tagged = (Ether() /
                       Dot1AD(vlan=20, prio=5) /
                       Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
                       CFM(md_level=2, opcode=LTM.opcode) /
                       LTM(use_fdb_only=1, transaction_id=99, ttl=3,
                           original_mac="aa:aa:bb:bb:cc:cc", target_mac=WBoxTestCase.global_params["my_cfm_mac"][0],
                           tlv_list=[LtmEgressIdentifierTlv(initiator_mac="aa:22:33:44:55:66", initiator_id=66)]))

        iface_names = WBoxTestCase.global_params["interfaces"]

        iface_vsi = self.if_internal_idx_2_vsi(self.handler.api.interface.get_interface(iface_names[0])
                                               .interface.get_interface.data.internal_index)
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm_untagged,
            wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_LTM],
            cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=down_mep_oam_id, vsi=iface_vsi))
        rx_packets = self.handler.data_communicator.rx(number_of_packets=1)
        assert rx_packets[0].haslayer(Dot1Q) == False

        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm_1tagged,
            wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_LTM],
            cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=down_mep_oam_id, vsi=iface_vsi))
        rx_packets = self.handler.data_communicator.rx(number_of_packets=1)
        assert rx_packets[0].haslayer(Dot1Q)

        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm_2tagged,
            wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_LTM],
            cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=down_mep_oam_id, vsi=iface_vsi))
        rx_packets = self.handler.data_communicator.rx(number_of_packets=1)
        assert rx_packets[0].haslayer(Dot1Q)
        assert rx_packets[0].haslayer(Dot1AD)

        waiting.wait(lambda: diff_counters(cnt_before, 3), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._cleanup_basic_oam_setup()

    @devvm_test()
    def test_linktrace_mip_devvm(self):
        def pkt_filter(x):
            return x.haslayer(CFM)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self._install_mip()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0,
            "MIP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        original_mac = "aa:aa:bb:bb:cc:cc"
        initiator_mac = "aa:22:33:44:55:66"
        initiator_id = 66
        ttl = 2

        self.handler.fpm_api.send_bridge_domain_mac_add_msg(
            bd_id=0,
            vsi_id=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
            action_type=BdLocalMacAdd.ActionType.NEW,
            ifindex=self.handler.api.interface.get_interface(iface_names[0]).interface.get_interface.data.management_id,
            mac_bytes=mac_2_bytes(bd_target_mac))

        sleep(1)

        ltm = (Ether(dst=f'01:80:c2:00:00:3{md_level_mip + 8:x}', src="00:01:02:03:04:05") /
                Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
                CFM(md_level=md_level_mip, opcode=LTM.opcode) /
                LTM(use_fdb_only=1, transaction_id=99, ttl=ttl,
                    original_mac=original_mac, target_mac=bd_target_mac,
                    tlv_list=[LtmEgressIdentifierTlv(initiator_mac=initiator_mac, initiator_id=initiator_id)]))

        if_data = self.handler.api.interface.get_interface(name=iface_names[0])
        iface_vsi = self.if_internal_idx_2_vsi(if_data.interface.get_interface.data.internal_index)

        for direction in range(2):
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)
            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm,
                wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_LTM],
                cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=0, vsi=iface_vsi, direction=direction))

            rx_packets = self.handler.data_communicator.rx(number_of_packets=2)

            assert len(rx_packets) == 2
            for pkt in rx_packets:
                if pkt.haslayer(LTM):
                    ltm_received = pkt
                    assert pkt[Ether].src == WBoxTestCase.global_params["my_cfm_mac"][0]
                    assert pkt[CFM].md_level == md_level_mip
                    assert pkt[CFM].opcode == LTM.opcode
                    assert pkt[LTM].use_fdb_only == 1
                    assert pkt[LTM].transaction_id == 99
                    assert pkt[LTM].ttl == ttl-1
                    assert pkt[LTM].original_mac == original_mac
                    assert pkt[LTM].target_mac == bd_target_mac
                    assert pkt[LTM].tlv_list[0].initiator_id == 0
                    assert pkt[LTM].tlv_list[0].initiator_mac == WBoxTestCase.global_params["my_cfm_mac"][0]
                elif pkt.haslayer(LTR):
                    assert pkt[CFM].md_level == md_level_mip
                    assert pkt[CFM].opcode == LTR.opcode
                    assert pkt[LTR].use_fdb_only == 1
                    assert pkt[LTR].terminal_mep == 0
                    assert pkt[LTR].fwd_yes == 1
                    assert pkt[LTR].transaction_id == 99
                    assert pkt[LTR].ttl == ttl-1
                    assert pkt[LTR].relay_action == 2
                    for tlv in pkt[LTR].tlv_list:
                        if tlv.type == 8:
                            assert tlv.last_egress_id == initiator_id
                            assert tlv.last_egress_mac == initiator_mac
                            assert tlv.next_egress_id == 0
                            assert tlv.next_egress_mac == WBoxTestCase.global_params["my_cfm_mac"][0]
                        elif tlv.type == 5:
                            assert tlv.ingress_action == 1
                            assert tlv.ingress_mac == WBoxTestCase.global_params["my_cfm_mac"][0]
                        elif tlv.type == 6:
                            assert tlv.egress_action == 1
                            assert tlv.egress_mac == WBoxTestCase.global_params["my_cfm_mac"][0]
                        else:
                            assert False, "Unexpected TLV"
                else:
                    assert False, "Unexpected packet"

            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm_received,
                wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_LTM],
                cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=0, vsi=iface_vsi, direction=direction))

            rx_packets = self.handler.data_communicator.rx(number_of_packets=1)
            assert len(rx_packets) == 1
            assert rx_packets[0].haslayer(LTR)
            assert rx_packets[0][LTR].fwd_yes == 0

        self._uninstall_mip()

    def test_add_remove_mip(self):
        self._install_mip()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0,
            "MIP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        sleep(2)

        self._uninstall_mip()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0,
            "MIP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)


    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_linktrace_mip_ltm_fwd_and_ltr(self):
        self._test_linktrace_mip_ltm_fwd_and_ltr(11)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_oam_punting_ltm_operdb(self):
        def diff_counters(cnt_before, mp_diff, stats_diff, mep_id, md_id, ma_id):
            cnt_bf_mep, cnt_bf_summary = cnt_before
            cnt_af_mep, cnt_af_summary = self._get_current_counters(lmep_config={
                "md_id" : md_id,
                "ma_id" : ma_id,
                "mep_id" : str(mep_id)})

            if cnt_af_mep['ltm_in'] - cnt_bf_mep['ltm_in'] != mp_diff:
                logger.error(f"'ltm_in' expected diff {mp_diff} but got {cnt_af_mep['ltm_in'] - cnt_bf_mep['ltm_in']}; mep_id: {mep_id}")
                return False

            if cnt_af_summary['ltm_in'] - cnt_bf_summary['ltm_in'] != stats_diff:
                logger.error(f"'ltm_in' expected diff {stats_diff} but got {cnt_af_summary['ltm_in'] - cnt_bf_summary['ltm_in']}; mep_id: {mep_id}")
                return False

            if cnt_af_mep['ltr_out'] - cnt_bf_mep['ltr_out'] != mp_diff:
                logger.error(f"'ltr_out' expected diff {mp_diff} but got {cnt_af_mep['ltr_out'] - cnt_bf_mep['ltr_out']}; mep_id: {mep_id}")
                return False

            if cnt_af_summary['ltr_out'] - cnt_bf_summary['ltr_out'] != stats_diff:
                logger.error(f"'ltr_out' expected diff {stats_diff} but got {cnt_af_summary['ltr_out'] - cnt_bf_summary['ltr_out']}; mep_id: {mep_id}")
                return False

            return True

        self._prepare_basic_oam_setup() # install and verify DownMep + UpMep

        down_cnt_before = self._get_current_counters(lmep_config={
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)})

        up_cnt_before = self._get_current_counters(lmep_config={
                "md_id" : md_id,
                "ma_id" : ma_id2,
                "mep_id" : str(up_mep_mep_id)})

        ltm_pkt_down = self._create_ltm_test_packet(md_level_down_mep, dst=WBoxTestCase.global_params["my_cfm_mac"][0])
        ltm_pkt_up = self._create_ltm_test_packet(md_level_up_mep, dst=WBoxTestCase.global_params["my_cfm_mac"][1])

        # downmep
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm_pkt_down, number_of_packets=1)

        # upmep
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ltm_pkt_up, number_of_packets=1)

        waiting.wait(lambda: diff_counters(down_cnt_before, 1, 2, down_mep_mep_id, md_id, ma_id1), sleep_seconds=1, timeout_seconds=wait_timeout_s)
        waiting.wait(lambda: diff_counters(up_cnt_before, 1, 2, up_mep_mep_id, md_id, ma_id2), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self._cleanup_basic_oam_setup() # delete and verify DownMep + UpMep


    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_oam_trap_mac_mismatch_unicast(self):
        def _prepare_check_mac_counters(is_upmep):
            if is_upmep:
                mep_name = up_mep_mep_id
                ma_id = ma_id2
                mep_name = up_mep_mep_id
            else:
                mep_name = down_mep_mep_id
                ma_id = ma_id1
                mep_name = down_mep_mep_id

            (counters, counters_summary) = self._get_current_counters(lmep_config={
                "md_id" : md_id,
                "ma_id" : ma_id,
                "mep_id" : str(mep_name)})

            lmep_oper_expected = {}
            for key, _ in counters.items():
                lmep_oper_expected[key] = None

            lmep_oper_expected['unicast_mac_mismatch'] = 1

            lmep_oper_summary_expected = {}
            for key, _ in counters_summary.items():
                lmep_oper_summary_expected[key] = None

            lmep_oper_summary_expected['unicast_mac_mismatch'] = 1

            return (lmep_oper_expected, lmep_oper_summary_expected, counters, counters_summary)

        WRONG_MAC = '00:00:00:02:03:b2' #make sure it's unicast....
        self._prepare_basic_oam_setup() # install and verify DownMep + UpMep

        down_lmep_oper_expected, down_lmep_oper_summary_expected, down_counters, down_counters_summary = _prepare_check_mac_counters(is_upmep=False)
        up_lmep_oper_expected, up_lmep_oper_summary_expected, up_counters, up_counters_summary         = _prepare_check_mac_counters(is_upmep=True)

        #### LBR
        lbr_pkt_up = self._create_lbr_test_packet(md_level_up_mep, dst=WRONG_MAC)
        lbr_pkt_down = self._create_lbr_test_packet(md_level_down_mep, dst=WRONG_MAC)
        good_lbr_pkt_down = self._create_lbr_test_packet(md_level_down_mep, dst=WBoxTestCase.global_params["my_cfm_mac"][0])

        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=good_lbr_pkt_down, number_of_packets=1)
        # downmep
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=lbr_pkt_down, number_of_packets=1)

        # upmep
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=lbr_pkt_up, number_of_packets=1)

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)
                },
            current_lmep=down_counters,
            current_summary_lmep=down_counters_summary,
            lmep_oper_expected=down_lmep_oper_expected,
            lmep_oper_summary_expected=down_lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id2,
                "mep_id" : str(up_mep_mep_id)
                },
            current_lmep=up_counters,
            current_summary_lmep=up_counters_summary,
            lmep_oper_expected=up_lmep_oper_expected,
            lmep_oper_summary_expected=up_lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self._cleanup_basic_oam_setup() # delete and verify DownMep + UpMep

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_oam_trap_mip_match_wrong_pdu(self):
        # Only LBM and LTM are handled in MIP
        mip_config = {"md_id": md_id, "ma_id": ma_icc, "mep_id": str(mip_name)}

        lbr_pkt = self._create_lbr_test_packet(md_level_mip, dst=WBoxTestCase.global_params["my_cfm_mac"][0])

        self._install_mip()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "MIP": 1,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        (counters, counters_summary) = self._get_current_counters(lmep_config=mip_config, mp_type="mip")

        lmep_oper_expected = {}
        for key, _ in counters.items():
            lmep_oper_expected[key] = None

        lmep_oper_expected['unsupported_cfm_pdu'] = 1

        lmep_oper_summary_expected = {}
        for key, _ in counters_summary.items():
            lmep_oper_summary_expected[key] = None

        lmep_oper_summary_expected['unsupported_cfm_pdu'] = 1

        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=lbr_pkt, number_of_packets=1)

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config=mip_config,
            current_lmep=counters,
            current_summary_lmep=counters_summary,
            lmep_oper_expected=lmep_oper_expected,
            lmep_oper_summary_expected=lmep_oper_summary_expected,
            mp_type="mip"
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self._uninstall_mip()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "MIP": 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)


    # @devvm_test()
    # def test_defect_n_meps_on_same_vsi_wrong_level_n_defects(self):

    #     downmep_level_0 = 0
    #     downmep_level_2 = 2
    #     downmep_level_5 = 5

    #     def send_packet(md_level=1):
    #         ccm_pkt = (
    #             Ether(dst=f'01:80:c2:00:00:3{md_level}', src='00:01:02:03:04:05') /
    #             Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
    #             CFM(md_level=md_level, opcode=CCM.opcode) /
    #             CCM(mep_id=down_mep_remote_mep_id,
    #                 ccm_interval=cfm_pb.CcmIntervalType.INTERVAL_1_SEC, maid=maid2)
    #         )
    #         self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt,
    #             wbox_trap_codes=[pkt_injector.TRAP_CODE_CFM_LEVEL_ERROR],
    #             cfm_oam_info=pkt_injector.CfmData(lmep_hw_id=down_mep_oam_id, vsi=1000))

    #     self._install_downmep() # uses md_level=2=downmep_level_2

    #     ## 2nd MEP
    #     iface_names = WBoxTestCase.global_params["interfaces"]

    #     self.handler.wb_api.cfm.create_lmep(
    #         oam_id=down_mep_oam_id + 1,
    #         mep_id=down_mep_mep_id + 1,
    #         md_id=md_id,
    #         ma_id=ma_id1,
    #         group_oam_id=group_id1,
    #         interface_name=iface_names[0],
    #         direction=cfm_pb.MepDirection.DOWN,
    #         admin_state=cfm_pb.AdminState.ENABLED,
    #         outer_tag=WBoxTestCase.global_params["outer_tag"][0],
    #         outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
    #         ccm_ltm_priority=5,
    #         md_level=downmep_level_0,
    #         ccm_config=ccm_config,
    #         req_type=cfm_pb.CreateRequestType.CREATE)

    #     self.handler.wb_api.cfm.create_rmep(
    #         mep_id=down_mep_remote_mep_id + 1,
    #         local_oam_id=down_mep_oam_id + 1,
    #         local_mep_id=down_mep_mep_id + 1,
    #         req_type=cfm_pb.CreateRequestType.CREATE)

    #     self.handler.wb_api.cfm.create_lmep(
    #         oam_id=down_mep_oam_id + 2,
    #         mep_id=down_mep_mep_id + 2,
    #         md_id=md_id,
    #         ma_id=ma_id1,
    #         group_oam_id=group_id1,
    #         interface_name=iface_names[0],
    #         direction=cfm_pb.MepDirection.DOWN,
    #         admin_state=cfm_pb.AdminState.ENABLED,
    #         outer_tag=WBoxTestCase.global_params["outer_tag"][0],
    #         outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
    #         ccm_ltm_priority=5,
    #         md_level=downmep_level_5,
    #         ccm_config=ccm_config,
    #         req_type=cfm_pb.CreateRequestType.CREATE)

    #     self.handler.wb_api.cfm.create_rmep(
    #         mep_id=down_mep_remote_mep_id + 2,
    #         local_oam_id=down_mep_oam_id + 2,
    #         local_mep_id=down_mep_mep_id + 2,
    #         req_type=cfm_pb.CreateRequestType.CREATE)

    #     self.handler.full_commit()

    #     waiting.wait(lambda: self._assert_oam_summary_xray({
    #         "MD" : 1,
    #         "MA" : 1,
    #         "Local MEP" : 3,
    #         "MIP": 0,
    #         "Remote MEP" : 3
    #     }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

    #     # Send wrong CCM packet to trigger xCon error, which should be the highest priority defect
    #     ### we send a pkt with md_level = 1; expect defects for md_level=2 and md_level=5
    #     send_packet()
    #     waiting.wait(lambda: self.assert_operdb_n_entries([
    #                                                                 [md_id, ma_id1, str(down_mep_mep_id)],
    #                                                                 [md_id, ma_id1, str(down_mep_mep_id + 2)]
    #                                                             ], {
    #         "highest_priority_defect" : 'def-xcon-ccm',
    #         "defects" : ['def-xcon-ccm']
    #     }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

    #     self.handler.wb_api.cfm.delete_rmep(mep_id=down_mep_remote_mep_id + 1, local_oam_id=down_mep_oam_id + 1)
    #     self.handler.wb_api.cfm.delete_lmep(oam_id=down_mep_oam_id + 1)
    #     self.handler.wb_api.cfm.delete_rmep(mep_id=down_mep_remote_mep_id + 2, local_oam_id=down_mep_oam_id + 2)
    #     self.handler.wb_api.cfm.delete_lmep(oam_id=down_mep_oam_id + 2)
    #     self._uninstall_downmep()
    #     waiting.wait(lambda: self._assert_oam_summary_xray({
    #         "MD" : 0,
    #         "MA" : 0,
    #         "Local MEP" : 0,
    #         "MIP": 0,
    #         "Remote MEP" : 0
    #     }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @pytest.mark.wbox_j2_beta
    @remote_test()
    def test_passive_path_down_directions(self):
        self._install_downmep() # install and verify DownMep

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        ccm_pkt_down_passive_path = (
            Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=11, prio=5, type=0x8902) /
            CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
            CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
        )

        down_lmep_oper_expected, down_lmep_oper_summary_expected, down_counters, down_counters_summary = self._prepare_check_counters(counter_name="passive_in", is_upmep=False)

        # downmep
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down_passive_path, number_of_packets=1)

        waiting.wait(lambda: self._assert_operdb_lmep(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)
                },
            current_lmep=down_counters,
            current_summary_lmep=down_counters_summary,
            lmep_oper_expected=down_lmep_oper_expected,
            lmep_oper_summary_expected=down_lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)
        self._uninstall_downmep() # delete and verify DownMep

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_passive_path_down_directions_wrong_level(self):
        self._install_downmep() # install and verify DownMep

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD": 1,
            "MA": 1,
            "Local MEP": 1,
            "MIP": 0,
            "Remote MEP": 1,
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        ccm_pkt_down_passive_path = (
            Ether(dst='01:80:c2:00:00:31', src='00:01:02:03:04:05') /
            Dot1Q(vlan=11, prio=5, type=0x8902) /
            CFM(md_level=1, opcode=CCM.opcode) /
            CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
        )

        down_lmep_oper_expected, down_lmep_oper_summary_expected, down_counters, down_counters_summary = self._prepare_check_counters(counter_name="passive_in_wrong_level", is_upmep=False)
        # downmep
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down_passive_path, number_of_packets=1)

        waiting.wait(lambda: self._assert_operdb_lmep(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)
                },
            current_lmep=down_counters,
            current_summary_lmep=down_counters_summary,
            lmep_oper_expected=down_lmep_oper_expected,
            lmep_oper_summary_expected=down_lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep() # delete and verify DownMep

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_passive_path_down_directions(self):
        self._install_downmep() # install and verify DownMep

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        ccm_pkt_down_passive_path = (
            Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
            Dot1Q(vlan=11, prio=5, type=0x8902) /
            CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
            CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
        )

        down_lmep_oper_expected, down_lmep_oper_summary_expected, down_counters, down_counters_summary = self._prepare_check_counters(counter_name="passive_in", is_upmep=False)

        # downmep
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down_passive_path, number_of_packets=1)

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)
                },
            current_lmep=down_counters,
            current_summary_lmep=down_counters_summary,
            lmep_oper_expected=down_lmep_oper_expected,
            lmep_oper_summary_expected=down_lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)
        self._uninstall_downmep() # delete and verify DownMep

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @pytest.mark.wbox_j2_beta
    @remote_test()
    def test_passive_path_down_directions_wrong_level(self):
        self._install_downmep() # install and verify DownMep

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        ccm_pkt_down_passive_path = (
            Ether(dst='01:80:c2:00:00:31', src='00:01:02:03:04:05') /
            Dot1Q(vlan=11, prio=5, type=0x8902) /
            CFM(md_level=1, opcode=CCM.opcode) /
            CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
        )

        down_lmep_oper_expected, down_lmep_oper_summary_expected, down_counters, down_counters_summary = self._prepare_check_counters(counter_name="passive_in_wrong_level", is_upmep=False)
        # downmep
        self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt_down_passive_path, number_of_packets=1)

        waiting.wait(lambda: self._assert_operdb_lmep_cnt(
            lmep_config= {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id)
                },
            current_lmep=down_counters,
            current_summary_lmep=down_counters_summary,
            lmep_oper_expected=down_lmep_oper_expected,
            lmep_oper_summary_expected=down_lmep_oper_summary_expected
        ), sleep_seconds=1, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep() # delete and verify DownMep

    def test_autodiscovery(self):
        logger.info("Running auto-discovery related tests...")

    @devvm_test()
    def test_autodiscovery_simple(self):
        """ Send CCM packet to trigger autodiscovery and install new RMEP
        """
        self._install_mep_autodiscovery(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._uninstall_mep_autodiscovery(md_id, ma_id1, down_mep_mep_id)

    @devvm_test()
    def test_autodiscovery_two_lmeps_different_config(self):
        """ Test if auto-discovery works correctly with differently configured
            LMEPs: one has auto-discovery off, the other on
            Expected to see one new RMEP added and one defect raised.
        """
        def wait_xray_defect():
            lmep_xray = self.handler.get_xray_stats("/cfm/local_meps")
            for lmep in lmep_xray:
                if lmep['mep_id'] == str(down_mep_mep_id) and lmep['defects'] == 'CCM':
                    return True
                else:
                    logger.info(f"Expected: mep_id={down_mep_mep_id}, defects=CCM, Actual: mep_id={lmep['mep_id']}, defects={lmep['defects']}")
            return False

        oam_id2 = 1
        md_id2 = 'md2'
        self._install_downmep()
        self._install_mep_autodiscovery(md_id=md_id2, ma_id=ma_id2, mep_id=down_mep_mep_id + 1, oam_id=oam_id2)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 2,
            "MA" : 2,
            "Local MEP" : 2,
            "MIP": 0,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: wait_xray_defect(), sleep_seconds=1, timeout_seconds=wait_timeout_s,
                     on_poll=lambda: self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR))

        self.send_trap_wrong_packet(oam_id2, down_mep_remote_mep_id + 1, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 2,
            "MA" : 2,
            "Local MEP" : 2,
            "MIP": 0,
            "Remote MEP" : 2
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._uninstall_downmep()
        self._uninstall_mep_autodiscovery(md_id2, ma_id2, down_mep_mep_id + 1)

    @devvm_test()
    def test_autodiscovery_defect_on_new_rmep(self):
        """ Test if CCM defects is triggered on new RMEP
            and is correctly written in operdb
        """
        self._install_mep_autodiscovery(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id, oam_id=down_mep_oam_id)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Install RMEP
        ccm_pkt = self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        event = cfm_events.CfmEvent(
            event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_INTERFACE_DOWN,
            rmep_hw_id=self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id)
        )
        self.handler.wb_api.cfm_events_injector.inject_event(event)

        waiting.wait(lambda: self._assert_operdb_rmep(
            rmep_config = {
                "md_id" : md_id,
                "ma_id" : ma_id1,
                "mep_id" : str(down_mep_mep_id),
                "rmep_id" : str(down_mep_remote_mep_id)
            },
            rmep_oper_expected = {
                "rmep_id" : down_mep_remote_mep_id,
                "mac_address": ccm_pkt.getlayer(Ether).src,
                "rmep_state": "rmep-ok",
                "port_status_tlv": "no-port-state-tlv",
                "interface_status_tlv": "down",
            },
        ), sleep_seconds=1, timeout_seconds=25,)

        self._uninstall_mep_autodiscovery(md_id, ma_id1, down_mep_mep_id)

    @devvm_test()
    def test_autodiscovery_update_interval(self):
        """ Send CCM packet to trigger autodiscovery and install new RMEP.
            Send recreate/update request for the parent LMEP and check
            if all RMEPs are updated with the new interval.
        """
        self._install_mep_autodiscovery(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 1, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 2, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        new_ccm_config = deepcopy(ccm_config)
        iface_names = WBoxTestCase.global_params["interfaces"]

        for action in [cfm_pb.CreateRequestType.RECREATE, cfm_pb.CreateRequestType.UPDATE]:
            new_ccm_config.loss_threshold += 1
            logger.info(f"Switching loss threshold to {new_ccm_config.loss_threshold}")

            self.handler.wb_api.cfm.create_lmep(
                oam_id=down_mep_oam_id,
                mep_id=down_mep_mep_id,
                md_id=md_id,
                ma_id=ma_id1,
                group_oam_id=down_mep_oam_id,
                interface_name=iface_names[0],
                direction=cfm_pb.MepDirection.DOWN,
                admin_state=cfm_pb.AdminState.ENABLED,
                outer_tag=WBoxTestCase.global_params["outer_tag"][0],
                outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
                ccm_ltm_priority=5,
                md_level=md_level_down_mep,
                ccm_config=new_ccm_config,
                remote_mep_ids=[],
                update_rmeps=True,
                req_type=action)

            self.handler.full_commit()
            sleep(3)

            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 1,
                "MA" : 1,
                "Local MEP" : 1,
                "MIP": 0,
                "Remote MEP" : 3
            }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            rmep_data = self.handler.get_xray_stats("/cfm/remote_meps")
            rmep_timeout = self._ccm_interval_to_ms(new_ccm_config.ccm_interval) * new_ccm_config.loss_threshold
            for rmep in rmep_data:
                assert rmep['oam_status'] == 'OK'
                assert rmep['timeout_ms'] == str(rmep_timeout)

        # Lower maximum_auto below current RMEP count to trigger a clearing
        # Check if RMEPs are cleared correctly [bug SW-173677]
        self.handler.wb_api.cfm.set_auto_discovery_threshold(maximum_auto=2, maximum_auto_syslog_threshold=max_auto_threshold)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._uninstall_mep_autodiscovery(md_id, ma_id1, down_mep_mep_id)

    @devvm_test()
    def test_autodiscovery_clear_commands(self):
        """ Test that clearing of discovered RMEPs by ID or by MA works correctly
        """
        def count_operdb_rmeps(expected_count):
            with DBClientAPI() as corm_api:
                obj = corm_api.get_by_path("/drivenets-top/services/ethernet-oam/connectivity-fault-management/maintenance-domains/maintenance-domain/maintenance-associations/maintenance-association/local-meps/local-mep/oper-items/mep-db",
                                           ['*', '*', '*', '*'], include_lists=True, is_recursive=True)
            return len(obj) == expected_count

        self._install_mep_autodiscovery(md_id='md1', ma_id=ma_id1, mep_id=down_mep_mep_id, oam_id=down_mep_oam_id)
        self._install_mep_autodiscovery(md_id='md2', ma_id=ma_id2, mep_id=down_mep_mep_id + 1, oam_id=down_mep_oam_id + 1)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 2,
            "MA" : 2,
            "Local MEP" : 2,
            "MIP": 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Install 2 RMEPs in each MA
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 1, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id + 1, down_mep_remote_mep_id + 2, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id + 1, down_mep_remote_mep_id + 3, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 2,
            "MA" : 2,
            "Local MEP" : 2,
            "MIP": 0,
            "Remote MEP" : 4
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Verify if the auto-discovered RMEP counter is correct
        oam_summary = self.handler.get_xray_stats("/cfm/summary")
        assert oam_summary[3]['discovered'] == '4'
        waiting.wait(lambda: count_operdb_rmeps(4), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.clear_discovered_rmeps(md_id='md1', ma_id=ma_id1, mep_id=down_mep_remote_mep_id)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 2,
            "MA" : 2,
            "Local MEP" : 2,
            "MIP": 0,
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)
        waiting.wait(lambda: count_operdb_rmeps(3), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.clear_discovered_rmeps(md_id='md2', ma_id=ma_id2)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 2,
            "MA" : 2,
            "Local MEP" : 2,
            "MIP": 0,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)
        waiting.wait(lambda: count_operdb_rmeps(1), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._uninstall_mep_autodiscovery('md1', ma_id1, down_mep_mep_id)
        self._uninstall_mep_autodiscovery('md2', ma_id2, down_mep_mep_id + 1)

    @devvm_test()
    def test_autodiscovery_system_events(self):
        """ Verify all 4 system events are sent correctly:
            - max discovered RMEP limit reached
            - max discovered RMEP limit cleared
            - RMEP % threshold exceeded
            - RMEP % threshold cleared
        """
        self.events_queue.clear_events()
        self._install_mep_autodiscovery(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id, oam_id=down_mep_oam_id)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        sleep(1)

        # Install discovered RMEPs
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 1, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 2, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 3, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 4
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Verify threshold exceeded system event sent
        self.assert_cfm_maximum_auto_discovered_rmeps_limit_threshold_exceeded(max_auto_threshold, max_auto_limit)

        # Install one more above threshold, but below limit. Expect no new system event
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 4, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        events = self.events_queue.get_all_events(['cfm_maximum_auto_discovered_rmeps_limit_threshold_exceeded'])
        assert(len(events) == 0)

        # Install one more to reach the limit. Expect system event
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 5, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 6
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.assert_cfm_maximum_auto_discovered_rmeps_limit_reached(6)

        # Try to install one more, shall not be allowed
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 6, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)

        sleep(3)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 6
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Remove RMEPs, verify both clear events were sent
        self.handler.wb_api.cfm.clear_discovered_rmeps(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_remote_mep_id)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 5
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.assert_cfm_maximum_auto_discovered_rmeps_limit_cleared(max_auto_limit)

        self.handler.wb_api.cfm.clear_discovered_rmeps(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_remote_mep_id + 1)
        self.handler.wb_api.cfm.clear_discovered_rmeps(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_remote_mep_id + 2)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.assert_cfm_maximum_auto_discovered_rmeps_limit_threshold_cleared(max_auto_threshold, max_auto_limit)

        self._uninstall_mep_autodiscovery(md_id, ma_id1, down_mep_mep_id)

    @devvm_test()
    def test_autodiscovery_max_auto_config_change(self):
        """ Verify that changing the maximum auto RMEPs to a lower value than
            the current maximum will trigger the clearing of all discovered RMEPs
        """

        self._install_mep_autodiscovery(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id, oam_id=down_mep_oam_id)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        sleep(1)

        # Install discovered RMEPs
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 1, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 2, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.set_auto_discovery_threshold(maximum_auto=2, maximum_auto_syslog_threshold=max_auto_threshold)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._uninstall_mep_autodiscovery(md_id, ma_id1, down_mep_mep_id)

    @devvm_test()
    def test_autodiscovery_same_mep_id(self):
        """ Verify if a discovered RMEP has the same mep ID as the LMEP,
            the RMEP is not installed and a defect is raised
        """

        self._install_mep_autodiscovery(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id, oam_id=down_mep_oam_id)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        sleep(1)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        sleep(1)

        lmep_xray = self.handler.get_xray_stats("/cfm/local_meps")[0]
        assert lmep_xray['defects'] == 'CCM'

        self._uninstall_mep_autodiscovery(md_id, ma_id1, down_mep_mep_id)

    @devvm_test()
    def test_autodiscovery_clear_rmeps_on_autodiscovery_disabled(self):
        """ Verify that all RMEPs are removed from the MA when auto-discovery is disabled
        """
        for action in [cfm_pb.CreateRequestType.RECREATE, cfm_pb.CreateRequestType.UPDATE]:
            self._install_mep_autodiscovery(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id, oam_id=down_mep_oam_id)
            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 1,
                "MA" : 1,
                "Local MEP" : 1,
                "MIP": 0,
                "Remote MEP" : 0
            }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
            self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 1, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
            self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 2, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)

            waiting.wait(lambda: self._assert_oam_summary_xray({
                "Remote MEP" : 3
            }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=down_mep_oam_id,
                                            ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                            req_type=action, auto_discovery_enabled=cfm_pb.AdminState.DISABLED)
            self.handler.full_commit()

            waiting.wait(lambda: self._assert_oam_summary_xray({
                "Remote MEP" : 0
            }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

            self._uninstall_mep_autodiscovery(md_id, ma_id1, down_mep_mep_id)

    @devvm_test()
    def test_autodiscovery_disable_configure_same_commit(self):
        """ Verify that disabling auto-discovery while there are active
            RMEPs installed and adding statically configured RMEPs in the
            same commit works as expected
        """

        self._install_mep_autodiscovery(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id, oam_id=down_mep_oam_id)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        sleep(1)

        # Install discovered RMEPs
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 1, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 2, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        xray_summary = self.handler.get_xray_stats("/cfm/summary")
        assert(xray_summary[3]['discovered'] == '3')

        # Disable auto-discovery and configure static RMEP in the same commit
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=down_mep_oam_id,
                                        ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                        req_type=cfm_pb.CreateRequestType.UPDATE, auto_discovery_enabled=cfm_pb.AdminState.DISABLED)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=down_mep_oam_id,
            interface_name=WBoxTestCase.global_params["interfaces"][0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        xray_summary = self.handler.get_xray_stats("/cfm/summary")
        assert(xray_summary[3]['discovered'] == '0')

        # Re-enable auto-discovery and discover RMEPs
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=down_mep_oam_id,
                                        ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                        req_type=cfm_pb.CreateRequestType.UPDATE, auto_discovery_enabled=cfm_pb.AdminState.ENABLED)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=down_mep_oam_id,
            interface_name=WBoxTestCase.global_params["interfaces"][0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()
        sleep(1)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Install discovered RMEPs
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 1, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 2, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        xray_summary = self.handler.get_xray_stats("/cfm/summary")
        assert(xray_summary[3]['discovered'] == '3')

        self._uninstall_mep_autodiscovery(md_id, ma_id1, down_mep_mep_id)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_autodiscovery_real_ccm_flow(self):
        """ Simulate a real scenario, where a remote MEP is sending
            CCM packets to the local MEP. The remote MEP should be
            installed automatically.
        """
        def send_ccm_and_verify():
            ccm_pkt = (
                Ether(dst=f'01:80:c2:00:00:3{md_level_down_mep}', src='00:01:02:03:04:05') /
                Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
                CFM(md_level=md_level_down_mep, opcode=CCM.opcode) /
                CCM(mep_id=down_mep_remote_mep_id,
                    ccm_interval=cfm_pb.CcmIntervalType.INTERVAL_1_SEC, maid=maid1)
            )
            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=ccm_pkt, number_of_packets=1)

            if not self._assert_oam_summary_xray({
                "MD" : 1,
                "MA" : 1,
                "Local MEP" : 1,
                "MIP": 0,
                "Remote MEP" : 1
            }):
                return False

            if not self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
                "fng_state" : 'fng-reset',
                "highest_priority_defect" : 'none',
                "defects" : None
            }):
                return False

            return True

        self._install_mep_autodiscovery(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id, oam_id=down_mep_oam_id)
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "MIP": 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(send_ccm_and_verify, sleep_seconds=1, timeout_seconds=wait_timeout_s)

        # Disable autodiscovery and verify if LMEP is still operational (bug SW-173098)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=down_mep_oam_id,
                                ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                req_type=cfm_pb.CreateRequestType.UPDATE, auto_discovery_enabled=cfm_pb.AdminState.DISABLED)
        self.handler.full_commit()
        sleep(2)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._uninstall_mep_autodiscovery(md_id, ma_id1, down_mep_mep_id)

    @devvm_test()
    def test_missing_rmeps_devvm(self):
        """ Verify that missing meps (configured but never heard of)
            are correctly counted and written to operdb
        """

        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id, down_mep_remote_mep_id + 1, down_mep_remote_mep_id + 2],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Send TIMEOUT event to simulate real scenario
        event_to = cfm_events.CfmEvent(
            event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_CCM_TIMEOUT,
            rmep_hw_id=self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id)
        )
        self.handler.wb_api.cfm_events_injector.inject_event(event_to)
        sleep(2)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "missing-rmeps" : [down_mep_remote_mep_id, down_mep_remote_mep_id + 1, down_mep_remote_mep_id + 2]
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        event_ti = cfm_events.CfmEvent(
            event_type=cfm_events.CfmEventType.CFM_OAM_EVENT_TYPE_CCM_TIMEIN,
            rmep_hw_id=self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id)
        )
        self.handler.wb_api.cfm_events_injector.inject_event(event_ti)
        event_ti.rmep_hw_id = self.get_rmep_hw_id(down_mep_oam_id, down_mep_remote_mep_id + 2)
        self.handler.wb_api.cfm_events_injector.inject_event(event_ti)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "missing-rmeps" : [down_mep_remote_mep_id + 1]
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    @remote_test()
    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    def test_missing_rmeps_wbox(self):
        """ Verify that missing meps (configured but never heard of)
            are correctly counted and written to operdb
        """

        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                          ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        iface_names = WBoxTestCase.global_params["interfaces"]

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id, down_mep_remote_mep_id + 1, down_mep_remote_mep_id + 2],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "fng_state" : 'fng-defect-reported'
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        waiting.wait(lambda: self.assert_operdb([md_id, ma_id1, str(down_mep_mep_id)], {
            "missing-rmeps" : [down_mep_remote_mep_id, down_mep_remote_mep_id + 1, down_mep_remote_mep_id + 2]
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

    @remote_test()
    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @pytest.mark.wbox_j2_beta
    def test_correct_action_ordering_complex_commit(self):
        """ Verify that the order of HW config update actions
            are in correct order in a complex commit
        """
        iface_names = WBoxTestCase.global_params["interfaces"]

        # Create simple auto-discovery setup
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                            ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                            auto_discovery_enabled=cfm_pb.AdminState.ENABLED, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[0],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][0],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][0],
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Discover 3 new RMEPs
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 1, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)
        self.send_trap_wrong_packet(down_mep_oam_id, down_mep_remote_mep_id + 2, pkt_injector.TRAP_CODE_CFM_RMEP_ERROR)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "Remote MEP" : 3
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        # Commit with multiple changes:
        # - Disable auto-discovery (which clears RMEPs)
        # - Create new MA with different ccm interval
        # - Create new LMEP in new MA
        # - Recreate LMEP with different direction and level

        new_ccm_config = deepcopy(ccm_config)
        new_ccm_config.ccm_interval = cfm_pb.CcmIntervalType.INTERVAL_10_MS

        self.handler.wb_api.cfm.create_ma(ma_id=ma_id2, maid48=bytes(maid2), md_id=md_id, oam_id=group_id2,
                                            ma_name=ma2_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                            auto_discovery_enabled=cfm_pb.AdminState.DISABLED, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_id1, maid48=bytes(maid1), md_id=md_id, oam_id=group_id1,
                                            ma_name=ma1_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES,
                                            auto_discovery_enabled=cfm_pb.AdminState.DISABLED, req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=up_mep_oam_id,
            mep_id=up_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id2,
            group_oam_id=group_id2,
            interface_name=iface_names[2],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][2],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][2],
            ccm_ltm_priority=5,
            md_level=md_level_up_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[up_mep_remote_mep_id, up_mep_remote_mep_id + 1],
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1, 
            interface_name=iface_names[1],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][1],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][1],
            ccm_ltm_priority=5,
            md_level=md_level_up_mep,
            ccm_config=new_ccm_config,
            remote_mep_ids=[up_mep_remote_mep_id, up_mep_remote_mep_id + 1],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1, 
            interface_name=iface_names[1],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][1],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][1],
            ccm_ltm_priority=5,
            md_level=md_level_up_mep,
            ccm_config=new_ccm_config,
            remote_mep_ids=[up_mep_remote_mep_id, up_mep_remote_mep_id + 1],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1, 
            interface_name=iface_names[1],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][1],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][1],
            ccm_ltm_priority=5,
            md_level=md_level_up_mep,
            ccm_config=new_ccm_config,
            remote_mep_ids=[up_mep_remote_mep_id, up_mep_remote_mep_id + 1],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1, 
            interface_name=iface_names[1],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            outer_tag=WBoxTestCase.global_params["outer_tag"][1],
            outer_tpid=WBoxTestCase.global_params["outer_tpid"][1],
            ccm_ltm_priority=5,
            md_level=md_level_up_mep,
            ccm_config=new_ccm_config,
            remote_mep_ids=[up_mep_remote_mep_id, up_mep_remote_mep_id + 1],
            req_type=cfm_pb.CreateRequestType.UPDATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 2,
            "Local MEP" : 2,
            "Remote MEP" : 4
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        xray = self.handler.get_xray_stats("/cfm/local_meps")
        for lmep in xray:
            if lmep['mep_id'] == str(down_mep_mep_id):
                assert lmep['md_level'] == '5'
                assert lmep['interface'] == iface_names[1]
                assert lmep['ccm_interval'] == '10 ms'

        # Cleanup
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)
        self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id2, mep_id=up_mep_mep_id)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id1)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_id2)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()


@pytest.mark.first
@pytest.mark.wbox_j2_beta
@pytest.mark.owner(user="vpostovaru", component=JiraComponent.WHITEBOX)
@pytest.mark.wbox_cfm
class TestCfmManagerEvpn(TestCfmManagerBase):

    def create_ac(
        self,
        parent,
        vid,
        admin_status=interfaces_pb.ON,
        l2_service=True,
        mpls_enabled=True,
    ):
        vlan_data = interfaces_pb.Interface()
        vlan_data.name = f"{parent}.{vid}"
        vlan_data.lag.name = parent
        vlan_data.sub.vlan_tag = vid
        vlan_data.management_id = next(self.management_id_allocator)
        vlan_data.internal_index = vlan_data.l3_internal_index = vlan_data.management_id
        vlan_data.admin_status = admin_status
        vlan_data.l2_service = l2_service
        vlan_data.mpls_enabled = mpls_enabled
        self.handler.api.interface.create_interface(
            type=interfaces_pb.SUB_INTERFACE,
            name=vlan_data.name,
            data=vlan_data,
        )

        self.handler.api.interface.update_interface(name=vlan_data.name, updates=vlan_data)

        return Dict(
            {
                "iface_data": vlan_data,
                "name": vlan_data.name,
                "management_id": vlan_data.management_id,
                "vlan": vid,
                "parent": parent,
            }
        )

    def delete_ac(self, ac):
        self.handler.api.interface.delete_interface(name=ac.name)

    def evpn_add_evi(self, evi_id, evpn_name):
        self.handler.wb_api.evpn.evpn_add_evi(EvpnEviConfig(evi_id=evi_id, evpn_name=evpn_name))

    def evpn_del_evi(self, evi_id):
        self.handler.wb_api.evpn.evpn_del_evi(EvpnEviConfig(evi_id=evi_id))

    def evpn_add_ethernet_tag(self, evi_id, eth_tag_id, vsi_id, admin_state=True, mac_table_limit=64000, mac_table_aging_time=320, mac_learning=True, irb_ifindex=None):
        self.handler.wb_api.evpn.evpn_add_eth_tag(EvpnEthTagConfig(evi_id=evi_id, eth_tag_id=eth_tag_id, vsi_id=vsi_id, admin_state=admin_state,
                                                  mac_table_limit=mac_table_limit, mac_table_aging_time=mac_table_aging_time, mac_learning=mac_learning, irb_ifindex=irb_ifindex))

    def evpn_del_ethernet_tag(self, evi_id, eth_tag_id):
        self.handler.wb_api.evpn.evpn_del_eth_tag(EvpnEthTagConfig(evi_id=evi_id, eth_tag_id=eth_tag_id))

    def evpn_add_ac(self, evi_id, eth_tag_id, ifindex, is_sticky=False, etree_status=EvpnAcEtreeStatus.ROOT):
        self.handler.wb_api.evpn.evpn_add_ac(EvpnAcConfig(
            evi_id=evi_id, eth_tag_id=eth_tag_id, ifindex=ifindex, is_sticky=is_sticky, etree_status=etree_status))

    def evpn_del_ac(self, evi_id, eth_tag_id, ifindex):
        self.handler.wb_api.evpn.evpn_del_ac(EvpnAcConfig(
            evi_id=evi_id, eth_tag_id=eth_tag_id, ifindex=ifindex))

    def create_nexthop_neighbor(self, interface, nh_oid, ip, label, mac="88:88:88:88:88:88"):
        self.handler.fpm_api.add_neighbor(
            if_id=interface.management_id,
            ip=ip,
            mac_bytes=mac_2_bytes(mac)
        )
        self.handler.fpm_api.add_oid(
            oid=nh_oid,
            nhs=[
                {
                    "if_id": interface.management_id,
                    "ip": ip,
                    "mpls_labels": [label],
                    "vrf_id": 0,
                }
            ],
        )

        return Dict(
            {
                "interface": interface,
                "ip": ip,
                "nh_oid": nh_oid,
                "label": label,
                "mac": mac,
            }
        )

    def del_nexthop_neighbor(self, nh):
        self.handler.fpm_api.del_neighbor(if_id=nh.interface.management_id, ip=nh.ip)
        self.handler.fpm_api.del_oid(oid=nh.nh_oid)

    @pytest.fixture(scope='class', autouse=True)
    def setup_interfaces(self, request):
        cls = request.cls
        cls.evi_id_allocator = cycle(range(1, 2001))
        cls.vsi_id_allocator = cycle(
            range(fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN, fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MAX + 1))
        cls.management_id_allocator = cycle(range(1000, 60000))

        AC1_VLAN = 100
        NH1_AC_VLAN = 200
        ETH_TAG_ID = 0
        VSI_ID = fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN
        EVI_ID = next(self.evi_id_allocator)
        EVPN_NAME = f"evpn{EVI_ID}"
        NH1_OID = 301
        NH1_OID_LABEL = 3001
        PE1_IP = "10.10.10.10"
        cls.REMOTE_MAC_1 = "20:00:00:00:00:00"
        NEIGHBOR_MAC_ADDR = "00:00:00:00:00:02"
        UC_LABEL = 1000
        MC_LABEL = 2000
        NH1_OID_EVPN_LABEL = 3111
        PE1_IML_LABEL = 2001
        ESI1_LABEL = 100

        # Create ACs
        ac1 = self.create_ac(parent=self.WB_IF_1_NAME, vid=AC1_VLAN)
        nh1_ac = self.create_ac(parent=self.WB_IF_2_NAME, vid=NH1_AC_VLAN)
        self.handler.full_commit()

        cls.ac1 = ac1
        parent_internal_index = self.handler.api.interface.get_interface(self.WB_IF_1_NAME).interface.get_interface.data.internal_index
        cls.my_cfm_mac = self._gen_my_cfm_mac(parent_internal_index)

        # Configurations
        self.evpn_add_evi(evi_id=EVI_ID, evpn_name=EVPN_NAME)
        self.evpn_add_ethernet_tag(evi_id=EVI_ID, eth_tag_id=ETH_TAG_ID, vsi_id=VSI_ID, mac_table_aging_time=0)
        self.evpn_add_ac(evi_id=EVI_ID, eth_tag_id=ETH_TAG_ID, ifindex=ac1.management_id)
        self.handler.full_commit()

        nh1 = self.create_nexthop_neighbor(interface=nh1_ac, nh_oid=NH1_OID, ip=PE1_IP,
                                     label=NH1_OID_LABEL, mac=NEIGHBOR_MAC_ADDR)

        # Configure EVPN
        self.handler.fpm_api.evpn_add_evi(evi_id=EVI_ID, evi_name=EVPN_NAME)
        self.handler.fpm_api.evpn_add_eth_tag(evi_id=EVI_ID, eth_tag=ETH_TAG_ID, control_word=False, incoming_fat_label=False, outgoing_fat_label=False)

        self.handler.fpm_api.evpn_install_bum_range(MC_LABEL, 8000)
        self.handler.fpm_api.evpn_install_evpn_labels(
            evi_id=EVI_ID, eth_tag=ETH_TAG_ID, label_uc=UC_LABEL, label_bum=MC_LABEL)

        labeled_nexthop = Dict()
        labeled_nexthop["label"] = NH1_OID_EVPN_LABEL
        labeled_nexthop["oid"] = NH1_OID
        self.handler.fpm_api.evpn_add_remote_mac_route(
            evi_id=EVI_ID,
            eth_tag=ETH_TAG_ID,
            mac_bytes=mac_2_bytes(cls.REMOTE_MAC_1),
            labeled_nexthops=[labeled_nexthop],
        )

        # Set bum route
        self.handler.fpm_api.evpn_install_bum_route(
            evi_id=EVI_ID,
            eth_tag=ETH_TAG_ID,
            originator_ip=PE1_IP,
            remote_label=PE1_IML_LABEL,
            nexthop_oid=NH1_OID,
        )

        self.handler.fpm_api.evpn_ac_add(
            evi_id=EVI_ID,
            eth_tag=ETH_TAG_ID,
            ifindex=ac1.management_id,
            esi_label=ESI1_LABEL,
            esi_originators=[
                (PE1_IP, PE1_IML_LABEL),
            ],
            block_mode=AcEsiStatus.BLOCK_NONE,
        )

        sleep(3)

        yield

        self.evpn_del_evi(evi_id=EVI_ID)
        self.del_nexthop_neighbor(nh1)
        self.evpn_del_ethernet_tag(evi_id=EVI_ID, eth_tag_id=ETH_TAG_ID)
        self.delete_ac(nh1_ac)
        self.delete_ac(ac1)
        self.handler.full_commit()

    @remote_test()
    def test_linktrace_evpn(self):
        pass

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_linktrace_evpn_mip_fwd_ltm(self):
        """ Test cfm_tx_ltm FEC
        """
        def pkt_filter(pkt):
            return (
                pkt.haslayer(LTM)
            )

        setup = self.__class__

        # Configure a single MIP
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_icc, maid48=bytes(maid_icc), md_id=md_id, oam_id=group_id_icc,
                                          ma_name=ma_icc_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_NONE,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_mip(
            oam_id=mip_oam_id,
            name=mip_name,
            md_id=md_id,
            ma_id=ma_icc,
            group_oam_id=group_id_icc,
            interface_name=setup.ac1.name,
            admin_state=cfm_pb.AdminState.ENABLED,
            md_level=md_level_mip,
            req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0,
            "MIP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        md_level = 3
        ttl = 30
        src = "00:01:02:03:04:05"
        original_mac="aa:aa:bb:bb:cc:cc"
        initiator_mac="aa:22:33:44:55:66"
        initiator_id=66

        # Send LTM to non-terminal MIP, expect LTM forwarded to next hop
        ltm = (Ether(dst=f'01:80:c2:00:00:3{md_level + 8:x}', src=src) /
                    Dot1Q(vlan=setup.ac1.vlan, type=CFM_TPID) /
                    CFM(md_level=md_level, opcode=LTM.opcode) /
                    LTM(use_fdb_only=1, transaction_id=99, ttl=ttl,
                            original_mac=original_mac, target_mac=setup.REMOTE_MAC_1,
                            tlv_list=[LtmEgressIdentifierTlv(initiator_mac=initiator_mac, initiator_id=initiator_id)]))

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_2_NAME, pkt_filter=pkt_filter)
        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm)
        rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)

        assert len(rx_packets) == 1
        pkt = rx_packets[0]
        assert pkt.getlayer(0).name == "Ethernet"
        assert pkt.getlayer(1).name == "802.1Q"
        assert pkt.getlayer(2).name == "MPLS"
        assert pkt.getlayer(3).name == "MPLS"
        assert pkt.getlayer(4).name == "Ethernet"
        assert pkt.getlayer(5).name == "802.1Q"
        assert pkt.getlayer(6).name == "CFM"
        assert pkt.getlayer(7).name == "LTM"

        assert pkt.getlayer(4).src == setup.my_cfm_mac
        assert pkt[CFM].md_level == md_level
        assert pkt[LTM].use_fdb_only == 1
        assert pkt[LTM].transaction_id == 99
        assert pkt[LTM].ttl == ttl-1
        assert pkt[LTM].original_mac == original_mac
        assert pkt[LTM].target_mac == setup.REMOTE_MAC_1
        assert pkt[LTM].tlv_list[0].initiator_id == 0
        assert pkt[LTM].tlv_list[0].initiator_mac == setup.my_cfm_mac

        # Cleanup
        self.handler.wb_api.cfm.delete_mip(md_id=md_id, ma_id=ma_icc, mip_name=mip_name)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_icc)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0,
            "MIP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)

        # wait till the LTR send at random time is released
        sleep(2)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_linktrace_evpn_mip_send_ltr(self):
        """ Test cfm_tx
        """
        setup = self.__class__

        def pkt_filter(pkt):
            return (
                pkt.haslayer(LTR)
            )

        # Configure a single MIP
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_icc, maid48=bytes(maid_icc), md_id=md_id, oam_id=group_id_icc,
                                          ma_name=ma_icc_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_NONE,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_mip(
            oam_id=mip_oam_id,
            name=mip_name,
            md_id=md_id,
            ma_id=ma_icc,
            group_oam_id=group_id_icc,
            interface_name=setup.ac1.name,
            admin_state=cfm_pb.AdminState.ENABLED,
            md_level=md_level_mip,
            req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0,
            "MIP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        md_level = 3
        ttl = 30
        src = "00:01:02:03:04:05"
        original_mac="aa:aa:bb:bb:cc:cc"
        initiator_mac="aa:22:33:44:55:66"
        initiator_id=66

        # Send LTM to non-terminal MIP, expect LTR
        ltm = (Ether(dst=f'01:80:c2:00:00:3{md_level + 8:x}', src=src) /
                    Dot1Q(vlan=setup.ac1.vlan, type=CFM_TPID) /
                    CFM(md_level=md_level, opcode=LTM.opcode) /
                    LTM(use_fdb_only=1, transaction_id=99, ttl=ttl,
                            original_mac=original_mac, target_mac=setup.REMOTE_MAC_1,
                            tlv_list=[LtmEgressIdentifierTlv(initiator_mac=initiator_mac, initiator_id=initiator_id)]))

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)
        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm)
        rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)

        assert len(rx_packets) == 1
        pkt = rx_packets[0]
        assert pkt.haslayer(LTR)
        assert pkt[CFM].md_level == md_level
        assert pkt[LTR].use_fdb_only == 1
        assert pkt[LTR].terminal_mep == 0
        assert pkt[LTR].fwd_yes == 1
        assert pkt[LTR].transaction_id == 99
        assert pkt[LTR].ttl == ttl-1
        assert pkt[LTR].relay_action == 2
        for tlv in pkt[LTR].tlv_list:
            if tlv.type == 8:
                assert tlv.last_egress_id == initiator_id
                assert tlv.last_egress_mac == initiator_mac
                assert tlv.next_egress_id == 0
                assert tlv.next_egress_mac == setup.my_cfm_mac
            elif tlv.type == 5:
                assert tlv.ingress_action == 1
                assert tlv.ingress_mac == setup.my_cfm_mac

        # Cleanup
        self.handler.wb_api.cfm.delete_mip(md_id=md_id, ma_id=ma_icc, mip_name=mip_name)
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_icc)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0,
            "MIP" : 0
        }), sleep_seconds=0.5, timeout_seconds=5)


@pytest.mark.first
@pytest.mark.wbox_j2_beta
@pytest.mark.owner(user="vpostovaru", component=JiraComponent.WHITEBOX)
@pytest.mark.wbox_cfm
class TestCfmManagerLag(TestCfmManagerBase):

    def _create_lag(self, lag_name, lag_id, lag_mac):
        if_data = interfaces_pb.Interface(name=lag_name)

        if_data.management_id = next(self.management_id_allocator)
        if_data.internal_index = if_data.l3_internal_index = if_data.management_id
        if_data.lag.lag_index = lag_id
        if_data.mac_address = lag_mac
        if_data.admin_status = interfaces_pb.ON
        create_lag_interface(self, if_data, set_interface_mode=True)
        self.handler.full_commit()

        return if_data

    def _delete_lag(self, lag_name):
        self.handler.api.interface.delete_interface(name=lag_name)
        self.handler.full_commit()

    @pytest.fixture(scope='class', autouse=True)
    def setup_interfaces(self, request, events_queue):
        cls = request.cls
        cls.management_id_allocator = cycle(range(1000, 60000))
        cls.events_queue = events_queue

        cls.set_iface_oper_state(
            iface_name=WBoxTestCase.WB_IF_1_NAME, is_oper_up=True)
        cls.set_iface_oper_state(
            iface_name=WBoxTestCase.WB_IF_2_NAME, is_oper_up=True)

        WBoxTestCase.global_params["interfaces"] = []
        WBoxTestCase.global_params["my_cfm_mac"] = []
        WBoxTestCase.global_params["outer_tag"] = []
        WBoxTestCase.global_params["outer_tpid"] = []

        if_vlans = []

        lag_name = "bundle-1"
        lag_data = self._create_lag(lag_name, 0, WBoxTestCase.WB_IF_1_MAC)
        auto_assign_member_id_to_interface(self.handler, WBoxTestCase.WB_IF_1_NAME)
        self.handler.api.interface.add_phy_to_lag(lag_name=lag_name, phy_name=WBoxTestCase.WB_IF_1_NAME)
        self.handler.full_commit()
        self.set_iface_oper_state(iface_name=lag_name, is_oper_up=True)

        if_vlans = [{"parent": lag_name, "vlan_tag": 10, "outer_tpid": 0x8100, "l2_service": True, 'pcp_preserve': True},
                    {"parent": lag_name, "vlan_tag": 20, "outer_tpid": 0x8100, "l2_service": True, 'pcp_preserve': True}]

        with self.vlans_manager_with_config(if_vlans) as iface_handle:
            self._send_add_bridge_domain_pb(bd_id=BD_ID, vsi=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN, name="bridge0", admin_state=True)
            for i in range(2):
                self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=iface_handle[i].management_id)

            self.handler.full_commit()
            sleep(1)

            WBoxTestCase.global_params["interfaces"].append(iface_handle[0].name)
            WBoxTestCase.global_params["interfaces"].append(iface_handle[1].name)

            WBoxTestCase.global_params["my_cfm_mac"].append(self._gen_my_cfm_mac(lag_data.internal_index))
            WBoxTestCase.global_params["my_cfm_mac"].append(self._gen_my_cfm_mac(lag_data.internal_index))

            WBoxTestCase.global_params["outer_tag"].append(iface_handle[0].sub.vlan_tag)
            WBoxTestCase.global_params["outer_tag"].append(iface_handle[1].sub.vlan_tag)

            WBoxTestCase.global_params["outer_tpid"].append(iface_handle[0].sub.outer_tpid)
            WBoxTestCase.global_params["outer_tpid"].append(iface_handle[1].sub.outer_tpid)

            yield

            for i in range(2):
                # it may be that interface has a different management id at teardown
                self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_handle[i].name)

            self._send_del_bridge_domain_pb(_create_bd_config_pb(bd_id=BD_ID, vsi=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN, name="bridge0"))

            self.handler.full_commit()

        self.handler.api.interface.remove_phy_from_lag(lag_name=lag_name, phy_name=WBoxTestCase.WB_IF_1_NAME)
        self.handler.full_commit()
        self._delete_lag(lag_name)

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_empty_lag_and_endpoint_delete(self):
        lag_name = "bundle-1"
        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._assert_traffic(True)

        self.handler.api.interface.remove_phy_from_lag(lag_name=lag_name, phy_name=WBoxTestCase.WB_IF_1_NAME)
        self._uninstall_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self.handler.api.interface.add_phy_to_lag(lag_name=lag_name, phy_name=WBoxTestCase.WB_IF_1_NAME)
        self._install_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._assert_traffic(True)

        self._uninstall_downmep()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        self._assert_traffic(False)

    def test_disable_l2_service_and_delete_downmep_lag(self):
        self.check_disable_l2_service_and_delete_downmep()

    def test_disable_l2_service_and_delete_upmep_lag(self):
        self.check_disable_l2_service_and_delete_upmep()


    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    @remote_test()
    def test_linktrace_lag_mip_ltm_fwd_and_ltr(self):
        self._test_linktrace_mip_ltm_fwd_and_ltr(20)

    @pytest.mark.extended_tests
    @remote_test()
    def test_scale_lmep_change_interface_with_defect(self):
        lag_name = "bundle-1"
        scale_nr_ma = 1000

        if_vlans1 = []
        if_vlans2 = []
        for i in range(scale_nr_ma):
            if_vlans1.append({"parent": lag_name, "vlan_tag": (i + 1000), "outer_tpid": 0x8100, "l2_service": True, 'pcp_preserve': True})
            if_vlans2.append({"parent": lag_name, "vlan_tag": (i + 2000), "outer_tpid": 0x8100, "l2_service": True, 'pcp_preserve': True})

        vlans1 = self._create_vlans([Dict(d) for d in if_vlans1])
        vlans2 = self._create_vlans([Dict(d) for d in if_vlans2])

        self.handler.wb_api.cfm.create_md(md_id="1")

        for i in range(scale_nr_ma):
            maid_tmp = bytes(CCM.create_maid(ma_name=f"ab{i}", ma_name_format=3))
            self.handler.wb_api.cfm.create_ma(
                ma_id=str(i), maid48=bytes(maid_tmp), md_id="1",
                ma_name=f"ab{i}", md_name="1",
                flexible=cfm_pb.FlexibleType.FLEXIBLE_48_BYTES, oam_id=i)

            self.handler.wb_api.cfm.create_lmep(
                oam_id=i,
                mep_id=i,
                md_id="1",
                ma_id=str(i),
                group_oam_id=i,
                interface_name=vlans1[i].name,
                direction=cfm_pb.MepDirection.DOWN,
                admin_state=cfm_pb.AdminState.ENABLED,
                outer_tag=if_vlans1[i]["vlan_tag"],
                outer_tpid=if_vlans1[i]["outer_tpid"],
                ccm_ltm_priority=5,
                md_level=md_level_down_mep,
                ccm_config=ccm_config,
                remote_mep_ids=[i+1],
                req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : scale_nr_ma,
            "Local MEP" : scale_nr_ma,
            "Remote MEP" : scale_nr_ma
        }), sleep_seconds=0.5, timeout_seconds=10)

        self._delete_vlans([Dict(d) for d in if_vlans1])
        for i in range(scale_nr_ma):
            self.handler.wb_api.cfm.create_lmep(
                oam_id=i,
                mep_id=i,
                md_id="1",
                ma_id=str(i),
                group_oam_id=i,
                interface_name=vlans2[i].name,
                direction=cfm_pb.MepDirection.DOWN,
                admin_state=cfm_pb.AdminState.ENABLED,
                outer_tag=if_vlans2[i]["vlan_tag"],
                outer_tpid=if_vlans2[i]["outer_tpid"],
                ccm_ltm_priority=5,
                md_level=md_level_down_mep,
                ccm_config=ccm_config,
                req_type=cfm_pb.CreateRequestType.RECREATE)

        # expectation is that previously installed RMEPs to raise a timeout **exactly** during processing of this commit
        # (more exactly at early post commit phase, when old interface is no longer in active config )
        # by default timeout is generated after 3 seconds from RMEP installation in RMEP_DB
        # testing showed that this interval of 3 seconds  is enough in order to have timeouts firing exactly at early post commit phase
        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : scale_nr_ma,
            "Local MEP" : scale_nr_ma,
            "Remote MEP" : scale_nr_ma
        }), sleep_seconds=0.5, timeout_seconds=10)

        for i in range(scale_nr_ma):
            self.handler.wb_api.cfm.delete_lmep(md_id="1", ma_id=str(i), mep_id=i)
            self.handler.wb_api.cfm.delete_ma(md_id="1", ma_id=str(i))
        self.handler.wb_api.cfm.delete_md(md_id="1")
        self._delete_vlans([Dict(d) for d in if_vlans2])

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 0,
            "MA" : 0,
            "Local MEP" : 0,
            "Remote MEP" : 0
        }), sleep_seconds=0.5, timeout_seconds=10)


@pytest.mark.first
@pytest.mark.wbox_j2_beta
@pytest.mark.owner(user="vpostovaru", component=JiraComponent.WHITEBOX)
@pytest.mark.wbox_cfm
class TestCfmManagerSubOLagAndSubOPhy(TestCfmManagerBase):

    ### let's create an experimental setup with a teardown
    installed_meps = []
    installed_rmeps = []
    installed_mips = []
    installed_mds = []
    installed_mas = []
    installed_mps = []

    def _install_downmep_and_check(self, new_lmep=1, if_id=0):
        self._install_downmep(if_id=if_id)
        if md_id not in type(self).installed_mds:
            type(self).installed_mds.append(md_id)
        type(self).installed_mas.append((md_id, ma_id1))
        type(self).installed_rmeps.append(down_mep_remote_mep_id)
        expected_meps = (len(type(self).installed_meps) + new_lmep)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : len(type(self).installed_mds),
            "MA" : len(type(self).installed_mas),
            "Local MEP" : expected_meps,
            "Remote MEP" : expected_meps
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        type(self).installed_meps.append(down_mep_oam_id)

    def _install_upmep_and_check(self, new_lmep=1, if_id=1, level=md_level_up_mep):
        self._install_upmep(if_id=if_id, level=level)
        if md_id not in type(self).installed_mds:
            self.installed_mds.append(md_id)
        type(self).installed_mas.append((md_id, ma_id2))
        type(self).installed_rmeps.append(up_mep_remote_mep_id)
        expected_meps = (len(self.installed_meps) + new_lmep)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : len(type(self).installed_mds),
            "MA" : len(type(self).installed_mas),
            "Local MEP" : expected_meps,
            "Remote MEP" : expected_meps
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        type(self).installed_meps.append(up_mep_oam_id)

    def _install_mip_and_check(self, md_id_mip=md_id, md_name=md_name, new_lmip=1, if_id=0, level=md_level_mip):
        self._install_mip(md_id=md_id_mip, md_name=md_name, if_id=if_id, level=level)
        if md_id_mip not in type(self).installed_mds:
            type(self).installed_mds.append(md_id_mip)
        type(self).installed_mas.append((md_id_mip, ma_icc))
        expected_mips = (len(type(self).installed_mips) + new_lmip)

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : len(type(self).installed_mds),
            "MA" : len(type(self).installed_mas),
            "Local MEP" : len(type(self).installed_meps),
            "MIP": expected_mips,
            "Remote MEP" : len(type(self).installed_meps)
        }), sleep_seconds=0.5, timeout_seconds=wait_timeout_s)

        type(self).installed_mips.append((md_id_mip, ma_icc, mip_name))

    def tearDown(self):
        num_mips = len(type(self).installed_mips)
        num_meps = len(type(self).installed_meps)
        total_mps = num_mips + num_meps
        if total_mps == 0:
            logger.info("Nothing to cleanup here")
            return

        if (total_mps > 3) or (num_mips > 1) or (num_meps > 2):
            logger.info(f"You are on your own with {total_mps} MPs ({num_meps} MEPs, {num_mips} MIPs); I can only use _uninstall_mip/downmep/upmep")
            return

        for mip_params in type(self).installed_mips:
            self.handler.wb_api.cfm.delete_mip(*mip_params)

        for oam_id in type(self).installed_meps:
            # rmep_id? hardcode for now I guess
            if oam_id == up_mep_oam_id:
                self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id2, mep_id=up_mep_mep_id)
            else:
                self.handler.wb_api.cfm.delete_lmep(md_id=md_id, ma_id=ma_id1, mep_id=down_mep_mep_id)

        for ma_id_ in type(self).installed_mas:
            self.handler.wb_api.cfm.delete_ma(*ma_id_)

        for md_id_ in type(self).installed_mds:
            self.handler.wb_api.cfm.delete_md(md_id=md_id_)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
                    "MD" : 0,
                    "MA" : 0,
                    "Local MEP" : 0,
                    "Remote MEP" : 0,
                    "MIP" : 0
                }), sleep_seconds=0.5, timeout_seconds=5)

        type(self).installed_meps = []
        type(self).installed_mips = []
        type(self).installed_mas = []
        type(self).installed_mds = []

    def _create_lag(self, lag_name, lag_id, lag_mac):
        if_data = interfaces_pb.Interface(name=lag_name)

        if_data.management_id = next(self.management_id_allocator)
        if_data.internal_index = if_data.l3_internal_index = if_data.management_id
        if_data.lag.lag_index = lag_id
        if_data.mac_address = lag_mac
        if_data.admin_status = interfaces_pb.ON
        create_lag_interface(self, if_data, set_interface_mode=True)
        self.handler.full_commit()

        return if_data

    def _delete_lag(self, lag_name):
        self.handler.api.interface.delete_interface(name=lag_name)
        self.handler.full_commit()

    @pytest.fixture(scope='class', autouse=True)
    def setup_interfaces(self, request, events_queue):
        cls = request.cls
        cls.management_id_allocator = cycle(range(1000, 60000))
        cls.events_queue = events_queue

        cls.set_iface_oper_state(
            iface_name=WBoxTestCase.WB_IF_1_NAME, is_oper_up=True)
        cls.set_iface_oper_state(
            iface_name=WBoxTestCase.WB_IF_2_NAME, is_oper_up=True)

        WBoxTestCase.global_params["interfaces"] = []
        WBoxTestCase.global_params["my_cfm_mac"] = []
        WBoxTestCase.global_params["outer_tag"] = []
        WBoxTestCase.global_params["outer_tpid"] = []
        if_vlans = []
        ### create lag for BD
        lag_name = "bundle-1"
        self._create_lag(lag_name, 0, WBoxTestCase.WB_IF_1_MAC)
        auto_assign_member_id_to_interface(self.handler, WBoxTestCase.WB_IF_1_NAME)
        self.handler.api.interface.add_phy_to_lag(lag_name=lag_name, phy_name=WBoxTestCase.WB_IF_1_NAME)
        self.handler.full_commit()
        self.set_iface_oper_state(iface_name=lag_name, is_oper_up=True)

        if_vlans = [{"parent": lag_name, "vlan_tag": 10, "outer_tpid": 0x8100, "l2_service": True, 'pcp_preserve': True},
                    {"parent": lag_name, "vlan_tag": 20, "outer_tpid": 0x8100, "l2_service": True, 'pcp_preserve': True},
                    {"parent": WBoxTestCase.WB_IF_2_NAME, "vlan_tag": 40, "outer_tpid": 0x8100, "l2_service": True, 'pcp_preserve': True}]

        with self.vlans_manager_with_config(if_vlans) as iface_handle:
            self._send_add_bridge_domain_pb(bd_id=BD_ID, vsi=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN, name="bridge0", admin_state=True, mac_learning=True, mac_table_limit=64000, mac_table_aging_time=320)
            for i in range(len(if_vlans)):
                self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=iface_handle[i].management_id)

            self.handler.full_commit()
            sleep(1)

            for i in range(len(if_vlans)):
                parent_internal_index = self.handler.api.interface.get_interface(iface_handle[i].name.split('.')[0]).interface.get_interface.data.internal_index
                WBoxTestCase.global_params["interfaces"].append(iface_handle[i].name)
                WBoxTestCase.global_params["my_cfm_mac"].append(self._gen_my_cfm_mac(parent_internal_index))
                WBoxTestCase.global_params["outer_tag"].append(iface_handle[i].sub.vlan_tag)
                WBoxTestCase.global_params["outer_tpid"].append(iface_handle[i].sub.outer_tpid)

            yield

            for i in range(len(if_vlans)):
                # it may be that interface has a different management id at teardown
                self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_handle[i].name)

            self._send_del_bridge_domain_pb(_create_bd_config_pb(bd_id=BD_ID, vsi=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN, name="bridge0"))

            self.handler.full_commit()

        self.handler.api.interface.remove_phy_from_lag(lag_name=lag_name, phy_name=WBoxTestCase.WB_IF_1_NAME)
        self.handler.full_commit()
        self._delete_lag(lag_name)

    def test_recreate_if_changed(self):
        pass

    def test_recreate_if_changed_mip(self):
        iface_names = WBoxTestCase.global_params["interfaces"]
        my_cfm_macs = WBoxTestCase.global_params["my_cfm_mac"]

        self._install_mip_and_check()

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/mips",
                                        mp_id_field="mip_name",  # this is because we don't have the oam_id field in xray for mips
                                        mp_id_val=mip_name,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[0]), sleep_seconds=0.5, timeout_seconds=5)
        ## recreate mip
        self.handler.wb_api.cfm.create_mip(
            oam_id=mip_oam_id,
            name=mip_name,
            md_id=md_id,
            ma_id=ma_icc,
            group_oam_id=group_id_icc,
            interface_name=iface_names[2],
            admin_state=cfm_pb.AdminState.ENABLED,
            md_level=md_level_mip,
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/mips",
                                        mp_id_field="mip_name",  # this is because we don't have the oam_id field in xray for mips
                                        mp_id_val=mip_name,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[2]), sleep_seconds=0.5, timeout_seconds=5)

    def test_recreate_if_changed_mep(self):
        iface_names = WBoxTestCase.global_params["interfaces"]
        my_cfm_macs = WBoxTestCase.global_params["my_cfm_mac"]

        self._install_downmep_and_check()
        self._install_upmep_and_check()

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/local_meps",
                                        mp_id_field="oam_id",
                                        mp_id_val=down_mep_oam_id,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[0]), sleep_seconds=0.5, timeout_seconds=5)

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/local_meps",
                                        mp_id_field="oam_id",
                                        mp_id_val=up_mep_oam_id,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[1]), sleep_seconds=0.5, timeout_seconds=5)

        ## recreate downmep
        self.handler.wb_api.cfm.create_lmep(
            oam_id=down_mep_oam_id,
            mep_id=down_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id1,
            group_oam_id=group_id1,
            interface_name=iface_names[2],
            direction=cfm_pb.MepDirection.DOWN,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[down_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/local_meps",
                                        mp_id_field="oam_id",
                                        mp_id_val=down_mep_oam_id,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[2]), sleep_seconds=0.5, timeout_seconds=5)

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/local_meps",
                                        mp_id_field="oam_id",
                                        mp_id_val=up_mep_oam_id,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[1]), sleep_seconds=0.5, timeout_seconds=5)

        ## recreate upmep
        self.handler.wb_api.cfm.create_lmep(
            oam_id=up_mep_oam_id,
            mep_id=up_mep_mep_id,
            md_id=md_id,
            ma_id=ma_id2,
            group_oam_id=group_id2,
            interface_name=iface_names[2],
            direction=cfm_pb.MepDirection.UP,
            admin_state=cfm_pb.AdminState.ENABLED,
            ccm_ltm_priority=5,
            md_level=md_level_down_mep,
            ccm_config=ccm_config,
            remote_mep_ids=[up_mep_remote_mep_id],
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/local_meps",
                                        mp_id_field="oam_id",
                                        mp_id_val=down_mep_oam_id,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[2]), sleep_seconds=0.5, timeout_seconds=5)

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/local_meps",
                                        mp_id_field="oam_id",
                                        mp_id_val=up_mep_oam_id,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[2]), sleep_seconds=0.5, timeout_seconds=5)

    def test_recreate_if_changed_mip_coex_mep(self):
        iface_names = WBoxTestCase.global_params["interfaces"]
        my_cfm_macs = WBoxTestCase.global_params["my_cfm_mac"]

        self._install_mip_and_check()
        self._install_downmep_and_check()

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/mips",
                                        mp_id_field="mip_name",  # this is because we don't have the oam_id field in xray for mips
                                        mp_id_val=mip_name,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[0]), sleep_seconds=0.5, timeout_seconds=5)

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/local_meps",
                                        mp_id_field="oam_id",
                                        mp_id_val=down_mep_oam_id,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[0]), sleep_seconds=0.5, timeout_seconds=5)

        ## recreate mip
        self.handler.wb_api.cfm.create_mip(
            oam_id=mip_oam_id,
            name=mip_name,
            md_id=md_id,
            ma_id=ma_icc,
            group_oam_id=group_id_icc,
            interface_name=iface_names[2],
            admin_state=cfm_pb.AdminState.ENABLED,
            md_level=md_level_mip,
            req_type=cfm_pb.CreateRequestType.RECREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/mips",
                                        mp_id_field="mip_name",  # this is because we don't have the oam_id field in xray for mips
                                        mp_id_val=mip_name,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[2]), sleep_seconds=0.5, timeout_seconds=5)

        waiting.wait(lambda: self._assert_xray_mp_field(
                                        table="/cfm/local_meps",
                                        mp_id_field="oam_id",
                                        mp_id_val=down_mep_oam_id,
                                        field="mac_address",
                                        expected_value=my_cfm_macs[0]), sleep_seconds=0.5, timeout_seconds=5)

    @remote_test()
    def test_mip_mep_order_of_creation(self):
        def pkt_filter(pkt):
            return (
                pkt.haslayer(LTR)
            )

        my_cfm_macs = WBoxTestCase.global_params["my_cfm_mac"]
        self._install_mip_and_check(md_id_mip=md_id_icc, md_name=md_name_icc)

        ltm_pkt_mip = self._create_ltm_test_packet(md_level_mip, dst=my_cfm_macs[0])
        ltm_pkt_down = self._create_ltm_test_packet(md_level_down_mep, dst=my_cfm_macs[0])
        # Send LTM to non-terminal MIP, expect LTM forwarded to next hop

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)

        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm_pkt_mip)
        rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
        assert len(rx_packets) == 1
        assert rx_packets[0][CFM].md_level == md_level_mip

        # configure a lower level Down MEP
        self._install_downmep_and_check()

        ### make sure MIP still receives LTM correctly
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter) # restarting the sniffer flushes the queue
        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm_pkt_mip)
        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
        assert len(rx_packets) == 1
        assert rx_packets[0][CFM].md_level == md_level_mip

        ### make sure MEP also receives LTM correctly
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)
        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm_pkt_down)
        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
        assert len(rx_packets) == 1
        assert rx_packets[0][CFM].md_level == md_level_down_mep

    @parameterized.expand([(False,), (True,)])
    @remote_test()
    def test_mip_mep_on_the_same_level(self, has_mip_higher_level):
        iface_names = WBoxTestCase.global_params["interfaces"]
        second_mip_name = "second_mip"

        def pkt_filter(pkt):
            return (
                pkt.haslayer(LTR)
            )

        my_cfm_macs = WBoxTestCase.global_params["my_cfm_mac"]

        ltm_pkt_mip = self._create_ltm_test_packet(md_level_mip, dst=my_cfm_macs[0])
        ltm_pkt_down = self._create_ltm_test_packet(md_level_down_mep, dst=my_cfm_macs[0])
        ltm_pkt_up_mep = self._create_ltm_test_packet(md_level_mip, dst=my_cfm_macs[2])

        self._install_downmep_and_check()                          # create downmep on level 2
        self._install_upmep_and_check(if_id=2, level=md_level_mip) # create upmep on level 3
        self._install_mip_and_check(if_id=2, level=7)              # create the first mip on level 7 on the same interface as upmep

        if has_mip_higher_level:
            self.handler.wb_api.cfm.create_mip(
                oam_id=mip_oam_id + 1,
                name=second_mip_name,
                md_id=md_id,
                ma_id=ma_icc,
                group_oam_id=group_id_icc,
                interface_name=iface_names[0],
                admin_state=cfm_pb.AdminState.ENABLED,
                md_level=7,
                req_type=cfm_pb.CreateRequestType.CREATE)          # create the second mip on level 7 on the same interface as downmep
            self.handler.full_commit()

            # wait 30s to with traffic to increment level errors
            sleep(30)

            self.handler.wb_api.cfm.delete_mip(md_id=md_id, ma_id=ma_icc, mip_name=second_mip_name) # delete the second mip on level 7
            self.handler.full_commit()

            self.handler.wb_api.cfm.create_mip(
                oam_id=mip_oam_id + 1,
                name=second_mip_name,
                md_id=md_id,
                ma_id=ma_icc,
                group_oam_id=group_id_icc,
                interface_name=iface_names[0],
                admin_state=cfm_pb.AdminState.ENABLED,
                md_level=md_level_mip,
                req_type=cfm_pb.CreateRequestType.CREATE) # create the second mip on level 7 on the same interface as downmep
            self.handler.full_commit()

            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 1,
                "MA" : 3,
                "Local MEP" : 2,
                "Remote MEP" : 2,
                "MIP" : 2
            }), sleep_seconds=0.5, timeout_seconds=5)

        else:
            self.handler.wb_api.cfm.create_mip(
                oam_id=mip_oam_id + 1,
                name=second_mip_name,
                md_id=md_id,
                ma_id=ma_icc,
                group_oam_id=group_id_icc,
                interface_name=iface_names[0],
                admin_state=cfm_pb.AdminState.ENABLED,
                md_level=md_level_mip,
                req_type=cfm_pb.CreateRequestType.CREATE) # create the second mip on level 3 on the same interface as downmep
            self.handler.full_commit()

            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 1,
                "MA" : 3,
                "Local MEP" : 2,
                "Remote MEP" : 2,
                "MIP" : 2
            }), sleep_seconds=0.5, timeout_seconds=5)

        # Disable MAC table HW <-> SW sync
        self.handler.execute_command('mact set traverse run 0')

        # Learn target MAC address
        self.handler.fpm_api.send_bridge_domain_mac_add_msg(
            bd_id=0,
            vsi_id=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
            action_type=BdLocalMacAdd.ActionType.NEW,
            ifindex=self.handler.api.interface.get_interface(iface_names[2]).interface.get_interface.data.management_id,
            mac_bytes=mac_2_bytes(my_cfm_macs[2]))

        ### make sure MIP still receives LTM correctly
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter) # restarting the sniffer flushes the queue
        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm_pkt_mip)
        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
        assert len(rx_packets) == 1
        assert rx_packets[0][CFM].md_level == md_level_mip

        ### make sure MEP also receives LTM correctly
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)
        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm_pkt_down)
        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=1)
        assert len(rx_packets) == 1
        assert rx_packets[0][CFM].md_level == md_level_down_mep

        # sending ltm to the upmep
        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter) # restarting the sniffer flushes the queue        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter) # restarting the sniffer flushes the queue
        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm_pkt_up_mep)

        rx_packets = self.handler.data_communicator.rx(timeout=5, number_of_packets=3)
        assert len(rx_packets) == 3
        assert rx_packets[0][CFM].md_level == md_level_mip
        assert rx_packets[1][CFM].md_level == md_level_mip
        assert rx_packets[2][CFM].md_level == md_level_mip

        # unlearn mac
        self.handler.fpm_api.send_bridge_domain_mac_remove_msg(
            bd_id=0,
            vsi_id=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
            mac_bytes=mac_2_bytes(my_cfm_macs[2]))

        # Enable MAC table HW <-> SW sync
        self.handler.execute_command('mact set traverse run 1')

        self.handler.wb_api.cfm.delete_mip(md_id=md_id, ma_id=ma_icc, mip_name=second_mip_name) # delete the second mip
        self.handler.full_commit()


    @remote_test()
    def test_interface_disable(self):
        lag_name = "bundle-1"

        def pkt_filter(pkt):
            return (
                pkt.haslayer(LTR)
            )

        my_cfm_macs = WBoxTestCase.global_params["my_cfm_mac"]
        self._install_mip_and_check(md_id_mip=md_id_icc, md_name=md_name_icc)

        ltm_pkt_mip = self._create_ltm_test_packet(md_level_mip, dst=my_cfm_macs[0])
        # Send LTM to non-terminal MIP, expect LTM forwarded to next hop

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)

        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm_pkt_mip)
        rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
        assert len(rx_packets) == 1
        assert rx_packets[0][CFM].md_level == md_level_mip

        self.set_iface_oper_state(iface_name=lag_name, is_oper_up=False, is_link_up=True)

        sleep(5)

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)

        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm_pkt_mip)
        rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
        assert len(rx_packets) == 0

        self.set_iface_oper_state(iface_name=lag_name, is_oper_up=True, is_link_up=True)

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)

        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm_pkt_mip)
        rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
        assert len(rx_packets) == 1
        assert rx_packets[0][CFM].md_level == md_level_mip

        # wait till the LTR send at random time is released
        sleep(2)

    def send_ccm_packets_thread(self, event, interface, src='00:01:02:03:04:05', vlan=10, timeout=1):
        while not event.is_set():
            ccm_pkt = (
                Ether(dst=f'01:80:c2:00:00:3{md_level_mip}', src=src) /
                Dot1Q(vlan=vlan, prio=5, type=0x8902) /
                CFM(md_level=md_level_mip, opcode=CCM.opcode) /
                CCM(mep_id=down_mep_remote_mep_id, ccm_interval=CCM_PKT_PERIOD_1S, maid=maid1)
            )
            self.handler.data_communicator.tx(interface=interface, packet=ccm_pkt, number_of_packets=1)
            sleep(timeout)

    @remote_test()
    def test_fwd_ltm_over_two_mips(self):
        iface_names = WBoxTestCase.global_params["interfaces"]

        def pkt_filter(pkt):
            return (
                pkt.haslayer(LTR)
            )

        """ Test that LTM is forwarded over two MIPs """
        # Create CFM config with 2 MIPs on two separate PHYS interfaces
        self.handler.wb_api.cfm.create_md(md_id=md_id, req_type=cfm_pb.CreateRequestType.CREATE)
        self.handler.wb_api.cfm.create_ma(ma_id=ma_icc, maid48=bytes(maid_icc), md_id=md_id, oam_id=group_id_icc,
                                          ma_name=ma_icc_name, md_name=md_name, flexible=cfm_pb.FlexibleType.FLEXIBLE_NONE,
                                          req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_mip(
            oam_id=mip_oam_id,
            name=mip_name,
            md_id=md_id,
            ma_id=ma_icc,
            group_oam_id=group_id_icc,
            interface_name=iface_names[0],
            admin_state=cfm_pb.AdminState.ENABLED,
            md_level=md_level_mip,
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.wb_api.cfm.create_mip(
            oam_id=mip_oam_id+1,
            name=mip_name+'2',
            md_id=md_id,
            ma_id=ma_icc,
            group_oam_id=group_id_icc,
            interface_name=iface_names[2],
            admin_state=cfm_pb.AdminState.ENABLED,
            md_level=md_level_mip,
            req_type=cfm_pb.CreateRequestType.CREATE)

        self.handler.full_commit()

        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 0,
            "Remote MEP" : 0,
            "MIP" : 2
        }), sleep_seconds=0.5, timeout_seconds=5)

        target_mac_existing = "66:55:44:33:22:11"
        original_mac = "aa:aa:bb:bb:cc:cc"
        initiator_mac = "aa:22:33:44:55:66"
        initiator_id = 66
        transaction_id = 99
        ttl = 5

        # Disable MAC table HW <-> SW sync
        self.handler.execute_command('mact set traverse run 0')

        # Learn target MAC address
        self.handler.fpm_api.send_bridge_domain_mac_add_msg(
            bd_id=0,
            vsi_id=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
            action_type=BdLocalMacAdd.ActionType.NEW,
            ifindex=self.handler.api.interface.get_interface(iface_names[2]).interface.get_interface.data.management_id,
            mac_bytes=mac_2_bytes(target_mac_existing))

        ltm = (Ether(dst=f'01:80:c2:00:00:3{md_level_mip + 8:x}', src="00:01:02:03:04:05") /
                Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
                CFM(md_level=md_level_mip, opcode=LTM.opcode) /
                LTM(use_fdb_only=1, transaction_id=transaction_id, ttl=ttl,
                    original_mac=original_mac, target_mac=target_mac_existing,
                    tlv_list=[LtmEgressIdentifierTlv(initiator_mac=initiator_mac, initiator_id=initiator_id)]))

        self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)
        self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm)
        rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=2)
        self.assertEqual(len(rx_packets), 2)

        # Cleanup
        self.handler.wb_api.cfm.delete_mip(md_id=md_id, ma_id=ma_icc, mip_name=mip_name)
        self.handler.wb_api.cfm.delete_mip(md_id=md_id, ma_id=ma_icc, mip_name=mip_name+'2')
        self.handler.wb_api.cfm.delete_ma(md_id=md_id, ma_id=ma_icc)
        self.handler.wb_api.cfm.delete_md(md_id=md_id)
        self.handler.full_commit()

        # unlearn mac
        self.handler.fpm_api.send_bridge_domain_mac_remove_msg(
            bd_id=0,
            vsi_id=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN,
            mac_bytes=mac_2_bytes(target_mac_existing))

        # Enable MAC table HW <-> SW sync
        self.handler.execute_command('mact set traverse run 1')

        # wait till the LTR send at random time is released
        sleep(2)

    @remote_test()
    @skip_func_validate_resources
    def test_interface_xconnect(self):
        def pkt_filter(pkt):
            return (
                pkt.haslayer(LTR)
            )

        interfaces = WBoxTestCase.global_params["interfaces"]

        target_mac_existing = "66:55:44:33:22:11"
        original_mac = "aa:aa:bb:bb:cc:cc"
        initiator_mac = "aa:22:33:44:55:66"
        initiator_id = 66
        transaction_id = 99
        ttl = 5

        self._install_mip_and_check(md_id_mip=md_id_icc, md_name=md_name_icc, if_id=2)

        # Send LTM to non-terminal MIP, expect LTM forwarded to next hop

        ltm_pkt_mip = (Ether(dst=f'01:80:c2:00:00:3{md_level_mip + 8:x}', src="00:01:02:03:04:05") /
                       Dot1Q(vlan=10, prio=5, type=CFM_TPID) /
                       CFM(md_level=md_level_mip, opcode=LTM.opcode) /
                       LTM(use_fdb_only=1, transaction_id=transaction_id, ttl=ttl,
                           original_mac=original_mac, target_mac=target_mac_existing,
                           tlv_list=[LtmEgressIdentifierTlv(initiator_mac=initiator_mac, initiator_id=initiator_id)]))


        # 2 means bd -> xcon -> bd -> xcon -> bd
        for i in range(2):
            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)

            self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm_pkt_mip)
            rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
            assert len(rx_packets) == 1
            assert rx_packets[0][CFM].md_level == md_level_mip


            # remove the bridge
            for i in range(len(interfaces)):
                # it may be that interface has a different management id at teardown
                self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=interfaces[i])

            self._send_del_bridge_domain_pb(_create_bd_config_pb(bd_id=BD_ID, vsi=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN, name="bridge0"))

            # create cross connect
            self.api.set_interface_cross_connect(first_interface_name=interfaces[0],
                                            second_interface_name=interfaces[2],
                                            enable=True,
                                            xc_name="xcc1")

            self.handler.full_commit()

            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter)

            self.handler.data_communicator.tx(self.WB_IF_1_NAME, ltm_pkt_mip)
            rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
            assert len(rx_packets) == 1
            assert rx_packets[0][CFM].md_level == md_level_mip

            # remove cross connect
            self.api.set_interface_cross_connect(first_interface_name=interfaces[0],
                                            second_interface_name=interfaces[2],
                                            enable=False,
                                            xc_name="xcc1")


            # restore the bridge back
            self._send_add_bridge_domain_pb(bd_id=BD_ID, vsi=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN, name="bridge0", admin_state=True)

            for i in range(len(interfaces)):
                # it may be that interface has a different management id at teardown
                self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=self.handler.api.interface.get_interface(interfaces[i]).interface.get_interface.data.management_id)

            self.handler.full_commit()

            self.handler.wb_api.cfm.delete_mip(md_id=md_id_icc, ma_id=ma_icc, mip_name=mip_name)
            self.handler.full_commit()

            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 1,
                "MA" : 1,
                "Local MEP" : 0,
                "Remote MEP" : 0,
                "MIP" : 0
            }), sleep_seconds=0.5, timeout_seconds=5)

            self.handler.wb_api.cfm.create_mip(
                oam_id=mip_oam_id,
                name=mip_name,
                md_id=md_id_icc,
                ma_id=ma_icc,
                group_oam_id=group_id_icc,
                interface_name=interfaces[2],
                admin_state=cfm_pb.AdminState.ENABLED,
                md_level=md_level_mip,
                req_type=cfm_pb.CreateRequestType.CREATE)

            self.handler.full_commit()

            waiting.wait(lambda: self._assert_oam_summary_xray({
                "MD" : 1,
                "MA" : 1,
                "Local MEP" : 0,
                "Remote MEP" : 0,
                "MIP" : 1
            }), sleep_seconds=0.5, timeout_seconds=5)

        # wait till the LTR send at random time is released
        sleep(2)


@pytest.mark.first
@pytest.mark.wbox_j2_beta
@pytest.mark.owner(user="vpostovaru", component=JiraComponent.WHITEBOX)
@pytest.mark.wbox_cfm
class TestCfmManagerVlanList(TestCfmManagerBase):

    @pytest.fixture(scope='class', autouse=True)
    def setup_interfaces(self, request, events_queue):
        cls = request.cls
        cls.management_id_allocator = cycle(range(1000, 60000))
        cls.events_queue = events_queue

        cls.set_iface_oper_state(
            iface_name=WBoxTestCase.WB_IF_1_NAME, is_oper_up=True)
        cls.set_iface_oper_state(
            iface_name=WBoxTestCase.WB_IF_2_NAME, is_oper_up=True)

        parent_internal_index = self.handler.api.interface.get_interface(WBoxTestCase.WB_IF_1_NAME).interface.get_interface.data.internal_index

        if_vlans = [
            Dict({
                "parent": WBoxTestCase.WB_IF_1_NAME,
                "name": WBoxTestCase.WB_IF_1_NAME + ".downMep",
                "vlan_tags": [10, 11],
                "outer_tpid": 0x8100,
                "l2_service": True,
                'pcp_preserve': True
            }),
            Dict({
                "parent": WBoxTestCase.WB_IF_1_NAME,
                "name": WBoxTestCase.WB_IF_1_NAME + ".upMep",
                "vlan_tags": [20, 21],
                "outer_tpid": 0x8100,
                "l2_service": True,
                'pcp_preserve': True
            })
        ]

        with self.vlans_manager_with_config(if_vlans) as iface_handle:
            self._send_add_bridge_domain_pb(bd_id=BD_ID, vsi=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN, name="bridge0", admin_state=True)
            # add first 2 interfaces to BD
            for i in range(2):
                self._send_add_bridge_domain_interface_pb(bd_id=BD_ID, ifindex=iface_handle[i].management_id)

            self.handler.full_commit()

            WBoxTestCase.global_params["interfaces"] = []
            WBoxTestCase.global_params["my_cfm_mac"] = []
            WBoxTestCase.global_params["outer_tag"] = []
            WBoxTestCase.global_params["outer_tpid"] = []

            WBoxTestCase.global_params["interfaces"].append(iface_handle[0].name)
            WBoxTestCase.global_params["interfaces"].append(iface_handle[1].name)

            WBoxTestCase.global_params["my_cfm_mac"].append(self._gen_my_cfm_mac(parent_internal_index))
            WBoxTestCase.global_params["my_cfm_mac"].append(self._gen_my_cfm_mac(parent_internal_index))

            WBoxTestCase.global_params["outer_tag"].append(iface_handle[0].sub.vlan_tags[0])
            WBoxTestCase.global_params["outer_tag"].append(iface_handle[1].sub.vlan_tags[0])

            WBoxTestCase.global_params["outer_tpid"].append(iface_handle[0].sub.outer_tpid)
            WBoxTestCase.global_params["outer_tpid"].append(iface_handle[1].sub.outer_tpid)

            yield

            # remove first 2 interfaces from BD
            for i in range(2):
                # it may be that interface has a different management id at teardown as some tests
                # are delete the interface and recreate it with a different management_id
                self._send_remove_bridge_domain_interface_pb(bd_id=BD_ID, iface_name=iface_handle[i].name)

            self._send_del_bridge_domain_pb(_create_bd_config_pb(bd_id=BD_ID, vsi=fib_consts.L2_SERVICE_NO_IRB_VSI_ID_MIN, name="bridge0"))

            self.handler.full_commit()

    @pytest.mark.skipif(not IS_JERICHO_2_B1, reason="CFM does not work on less than b1 devices")
    def test_dmr_down_with_vlan_list(self):
        def pkt_filter_vlan(x):
            return x.getlayer(Ether).dst == '00:01:02:03:04:05' and x.haslayer(Dot1Q) and x.haslayer(DMR)

        self._install_downmep()
        waiting.wait(lambda: self._assert_oam_summary_xray({
            "MD" : 1,
            "MA" : 1,
            "Local MEP" : 1,
            "Remote MEP" : 1
        }), sleep_seconds=0.5, timeout_seconds=5)

        tx_ts_down = 1234

        for vlan_id in [10, 11]:
            dmm_pkt_down = (
                Ether(dst=WBoxTestCase.global_params["my_cfm_mac"][0], src='00:01:02:03:04:05') /
                Dot1Q(vlan=vlan_id, prio=5, type=0x8902) /
                CFM(md_level=md_level_down_mep, opcode=DMM.opcode) /
                DMM(tx_timestampf=tx_ts_down)
            )

            self.handler.data_communicator.start_sniffer(interface=self.WB_IF_1_NAME, pkt_filter=pkt_filter_vlan)
            self.handler.data_communicator.tx(interface=self.WB_IF_1_NAME, packet=dmm_pkt_down, number_of_packets=1)
            rx_packets = self.handler.data_communicator.rx(timeout=2, number_of_packets=1)
            self.assertEqual(len(rx_packets), 1)
            self.assertEqual(rx_packets[0][Dot1Q].vlan, vlan_id)
            self.assertEqual(rx_packets[0][CFM].md_level, md_level_down_mep)

        self._uninstall_downmep()
