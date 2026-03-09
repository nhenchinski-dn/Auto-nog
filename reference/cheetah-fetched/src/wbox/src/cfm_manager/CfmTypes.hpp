#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <variant>
#include <vector>

extern "C" {
#include "cfm_common.h"
#include "rte_ether.h"
#include "sdk_wrap_api.h"
}

namespace cfm {

static constexpr int MAID_LEN = 48;
static constexpr uint8_t NUM_DEFECT_TYPES = 5;
static constexpr std::array<uint8_t, 3> DN_OUI_PREFIX = {0x84, 0x40, 0x76};

enum class LowestAlarmPriority
{
    // Order is important
    ALL_DEF,
    MAC_REMOTE_ERROR_XCON,
    REMOTE_ERROR_XCON,
    ERROR_XCON,
    XCON,
    NO_XCON
};

enum class DefectType
{
    // Order is important
    someRDIdefect,
    someMACstatusDefect,
    someRMEPCCMDefect,
    errorCCMdefect,
    xconCCMdefect
};

enum cfm_event_source
{
    CFM_EVENT_SOURCE_OAMP,
    CFM_EVENT_SOURCE_OAMP_CONF,
    CFM_EVENT_SOURCE_INTERFACE_MANAGER,
    CFM_EVENT_CLEAR_RMEP_ID,
    CFM_EVENT_CLEAR_RMEP_MA,
    CFM_EVENT_START_SESSION,
    CFM_EVENT_STOP_SESSION,
};

enum class PolicyAction
{
    OPER_DOWN,
    IGNORE
};

enum class MepDirection
{
    DOWN,
    UP,
    BIDIRECTIONAL
};

enum class OamAction
{
    // Order is important, there is no overwrite of the lowest action.
    // E.g. if the action is DELETE, it will not be overwritten by CREATE/ UPDATE.
    NO_ACTION,
    UPDATE,
    CREATE,
    RECREATE,
    TEMP_DELETE,
    DELETE
};

enum class OamStatus
{
    INITIAL,
    WAITING_WRITE_ACK,
    WAITING_DELETE_ACK,
    ACK_WRITE_OK,
    ACK_DELETE_OK,
    INTERNAL_ERROR,
    ACK_ERROR
};

enum class RmepState
{
    IDLE,
    START,
    FAILED,
    OK
};

enum class FngState
{
    RESET,
    DEFECT,
    REPORT_DEFECT,
    DEFECT_REPORTED,
    DEFECT_CLEARING
};

inline CcmInterfaceStatusType FromEventType(cfm_oam_event_type et)
{
    switch (et)
    {
    case CFM_OAM_EVENT_TYPE_INTERFACE_DOWN:
        return CFM_INTERFACE_STATUS_DOWN;
    case CFM_OAM_EVENT_TYPE_INTERFACE_UP:
        return CFM_INTERFACE_STATUS_UP;
    case CFM_OAM_EVENT_TYPE_INTERFACE_TESTING:
        return CFM_INTERFACE_STATUS_TESTING;
    case CFM_OAM_EVENT_TYPE_INTERFACE_UNKNOWN:
        return CFM_INTERFACE_STATUS_UNKNOWN;
    case CFM_OAM_EVENT_TYPE_INTERFACE_DORMANT:
        return CFM_INTERFACE_STATUS_DORMANT;
    case CFM_OAM_EVENT_TYPE_INTERFACE_NOTPRESENT:
        return CFM_INTERFACE_STATUS_NOTPRESENT;
    case CFM_OAM_EVENT_TYPE_INTERFACE_LLDOWN:
        return CFM_INTERFACE_STATUS_LLDOWN;
    default:
        CFM_LOG(DN_LOG_ERR, "Invalid event type received: %d", et);
        return CFM_INTERFACE_STATUS_DOWN;
    };
}

struct CcmConfig
{
    bool ccm_enabled = true;
    CcmIntervalType ccm_interval;
    uint8_t loss_threshold;
    LowestAlarmPriority lowest_priority_defect = LowestAlarmPriority::ALL_DEF;
    uint16_t fng_alarm_time = 2500;
    uint16_t fng_reset_time = 10000;
    PolicyAction efd_policy = PolicyAction::IGNORE;
};

struct LocalMepData
{
    uint16_t mep_id;
    std::string mip_name;
    std::string md_id;
    std::string ma_id;
    bool is_update;
    uint32_t oam_id;
    uint32_t group_oam_id;
    std::string interface_name;
    uint8_t md_level;
    bool admin_state;
    uint16_t outer_tag;
    uint16_t outer_tpid;
    uint16_t inner_tag;
    uint16_t inner_tpid;
    uint8_t ccm_ltm_priority;
    CcmConfig ccm_config;
    MepDirection direction;
    bool update_rmeps;
    bool clear_rmeps;
    std::vector<uint16_t> rmeps;
};

struct RemoteMepData
{
    uint16_t mep_id;
    uint32_t local_oam_id;
    uint32_t local_mep_id;
    uint32_t ccm_period;
    bool is_update;
};

struct MaData
{
    uint32_t oam_id;
    std::string ma_id;
    std::string ma_name;
    std::string md_id;
    std::string md_name;
    std::string maid48;
    int flexible;
    bool auto_discovery_enabled;
    bool is_update;
};

struct LmepStats
{
    std::string md_id;
    std::string ma_id;
    uint32_t oam_id;
    uint32_t hw_id;
    uint16_t mep_id;
    cfm_endpoints_stats stats;
};

struct OamInfo
{
    OamStatus oam_status = OamStatus::INITIAL;
    OamAction current_action = OamAction::NO_ACTION;
};

struct OamInfoLmep : OamInfo
{
    uint32_t hw_id = -1;
};

struct OamInfoRmep : OamInfo
{
    uint32_t hw_id = -1;
    uint32_t local_hw_id = -1;
    uint32_t local_oam_id = -1;
};

struct MaOper
{
    OamInfo oam_info;
    MaData config;
    std::set<uint32_t> local_mep_ids;
};

struct RmepStatus
{
    RmepState state = RmepState::START;
    uint64_t failed_ok_time = 0;
    ether_addr mac_address;
    bool rdi = false;
    CcmPortStatusType port_status = CFM_PORT_STATUS_NONE;
    CcmInterfaceStatusType interface_status = CFM_INTERFACE_STATUS_NONE;
    bool is_active = false;
    bool is_missing = true;
    bool discovered = false;
};

struct RMepOper
{
    RmepStatus status;
    OamInfoRmep oam_info;
    RemoteMepData config;
};

enum class MpType
{
    LOCAL_MEP,
    LOCAL_MIP
};

class LMepOper;

using MaOperIter = std::map<uint32_t, MaOper>::iterator;
using LMepOperIter = std::map<uint32_t, LMepOper>::iterator;
using RMepOperIter = std::map<uint16_t, RMepOper>::iterator;
using CfmObjectType = std::variant<MaData, LocalMepData, RemoteMepData>;
using CfmConfigUpdate = std::vector<std::pair<OamAction, CfmObjectType>>;

struct MaConfig
{
    MaData config;
    std::map<std::pair<MpType, std::string>, std::pair<OamAction, LocalMepData>>
        endpoints;
};

struct ConfigData
{
    std::map<std::string, std::map<std::string, std::pair<OamAction, MaConfig>>>
        cfm_objects;
    std::map<std::string, uint16_t> configured_interfaces;
    std::vector<std::string> deleted_mds;
    uint32_t maximum_auto = 2048;
    uint8_t maximum_auto_syslog_threshold = 75;
};

struct OperData
{
    std::set<std::string> MDs;
    std::map<uint32_t, MaOper> MAs;
    std::map<uint32_t, LMepOper> Meps;
    std::map<std::string, uint16_t> interfaces;
    cfm_endpoints_stats stats = {};
};

struct SummaryCounters
{
    // Total number of...
    uint64_t maintenance_domains = 0;
    uint64_t maintenance_associations = 0;
    uint64_t local_meps = 0;
    uint64_t local_down_meps = 0;
    uint64_t local_up_meps = 0;
    uint64_t user_disabled_local_meps = 0;
    uint64_t internal_error_local_meps = 0;
    uint64_t remote_meps = 0;
    uint64_t remote_meps_reported_with_rdi = 0;
    uint64_t remote_meps_reported_without_rdi = 0;
    uint64_t remote_meps_lost = 0;
    uint64_t mips = 0;
    uint64_t interfaces = 0;
    uint64_t faulty_services = 0;
    uint64_t total_number_of_auto_discovered_remote_meps = 0;

    bool operator==(const SummaryCounters& rhs) const = default;
};

struct AutoDiscoveryConfig
{
    int maximum_auto_rmeps_cnt = std::numeric_limits<int>::max();
    int maximum_auto_syslog_threshold = std::numeric_limits<int>::max();
    int syslog_threshold = 0;
    int current_auto_rmeps_cnt = 0;
    bool rmep_limit_event_sent = false;
    bool rmep_threshold_event_sent = false;
};

} // namespace cfm
