#include "CfmManagerXRay.hpp"

#include <rte_ether.h>

#include <algorithm>
#include <cstring>
#include <iterator>
#include <ranges>
#include <sstream>

#include "cfm_initiator.h"
#include "CfmConfiguration.hpp"
#include "CfmInitiator.hpp"
#include "CfmManager.hpp"
#include "CfmOperation.hpp"
#include "CfmSession.hpp"
#include "CfmTypes.hpp"
#include "CfmUtils.hpp"
#include "libdatapath/utils/utils_string.h"
#include "xray.h"

extern "C" {
#include "libdatapath/interfaces/interface_common.h"
}


using namespace cfm;

static constexpr size_t MAX_COLUMN_SIZE = 48;
static constexpr size_t MAX_SHORT_COLUMN = 15;
static constexpr size_t MAX_DEFECT_NAME = 26;

struct SummaryXrayRow
{
    uint8_t idx;
    char cfm_entity[MAX_SHORT_COLUMN];
    uint32_t configured;
    uint32_t ok;
    uint32_t pending;
    uint32_t error;
    uint32_t discovered;
};

struct InitiatorSummaryXrayRow
{
    uint8_t idx;
    char cfm_entity[MAX_SHORT_COLUMN];
    uint32_t active;
    uint32_t allocated;
    uint32_t freed;
};

struct InitiatorSessionXrayRow
{
    uint64_t sess_id;
    char opcode[MAX_SHORT_COLUMN];
    uint32_t oam_id;
    char state[MAX_SHORT_COLUMN];
};

struct MdXrayRow
{
    char md_id[MAX_COLUMN_SIZE];
};

struct MaXrayRow
{
    char ma_id[MAX_COLUMN_SIZE];
    char ma_name[MAX_COLUMN_SIZE];
    char md_id[MAX_COLUMN_SIZE];
    uint32_t oam_id;
    char oam_status[MAX_COLUMN_SIZE];
    uint16_t nr_defects;
    char auto_discovery[MAX_SHORT_COLUMN];
};

struct LmepXrayRow
{
    char md_name[MAX_COLUMN_SIZE];
    char parent_ma_name[MAX_COLUMN_SIZE];
    uint8_t md_level;
    uint32_t mep_id;
    uint32_t oam_id;
    char hw_id[MAX_SHORT_COLUMN];
    bool admin_state;
    char direction[MAX_SHORT_COLUMN];
    char interface[MAX_INTERFACE_NAME];
    char ccm_interval[MAX_COLUMN_SIZE];
    char mac_address[MAX_COLUMN_SIZE];
    char defects[MAX_DEFECT_NAME];
    char fng_state[MAX_DEFECT_NAME];
    bool rdi_tx;
    char oam_status[MAX_COLUMN_SIZE];
    uint16_t rmeps;
    uint16_t vsi;
};

struct MipXrayRow
{
    char md_name[MAX_COLUMN_SIZE];
    char parent_ma_name[MAX_COLUMN_SIZE];
    char mip_name[MAX_COLUMN_SIZE];
    char hw_id[MAX_SHORT_COLUMN];
    uint8_t md_level;
    bool admin_state;
    char interface[MAX_INTERFACE_NAME];
    char mac_address[MAX_COLUMN_SIZE];
    char oam_status[MAX_COLUMN_SIZE];
    uint16_t vsi;
};

struct LmepXrayCntRow
{
    uint32_t oam_id;
    uint64_t rx;
    uint64_t tx;
    uint64_t wrong_level;
    uint64_t wrong_interval;
    uint64_t wrong_remote_mep;
    uint64_t wrong_maid;
    uint64_t wrong_type;
    uint64_t passive_in;
    uint64_t passive_in_wrong_level;
    uint64_t unicast_mac_mismatch;
};

struct RmepXrayRow
{
    uint32_t mep_id;
    uint32_t local_mep_oam_id;
    char ma_id[MAX_COLUMN_SIZE];
    bool is_active;
    uint32_t timeout_ms;

    char state[MAX_SHORT_COLUMN];
    uint8_t RDI;
    char port_status[MAX_SHORT_COLUMN];
    char interface_status[MAX_COLUMN_SIZE];
    char mac_address[MAX_COLUMN_SIZE];
    bool is_missing;
    bool is_discovered;

    char hw_id[MAX_SHORT_COLUMN];
    char oam_status[MAX_COLUMN_SIZE];
};

struct InterfaceCountRow
{
    char interface_name[MAX_INTERFACE_NAME];
    uint32_t count_config;
    uint32_t count_oper;
};

static const char* RmepStateToString(RmepState state)
{
    switch (state)
    {
    case RmepState::IDLE:
        return "IDLE";
    case RmepState::START:
        return "START";
    case RmepState::FAILED:
        return "FAILED";
    case RmepState::OK:
        return "OK";
    default:
        return "INVALID";
    };
}

static const char* PortStatusToString(CcmPortStatusType status)
{
    switch (status)
    {
    case CFM_PORT_STATUS_NONE:
        return "NONE";
    case CFM_PORT_STATUS_BLOCKED:
        return "BLOCKED";
    case CFM_PORT_STATUS_UP:
        return "UP";
    default:
        return "INVALID";
    };
}

static const char* InterfaceStatusToString(CcmInterfaceStatusType status)
{
    switch (status)
    {
    case CFM_INTERFACE_STATUS_NONE:
        return "NONE";
    case CFM_INTERFACE_STATUS_UP:
        return "UP";
    case CFM_INTERFACE_STATUS_DOWN:
        return "DOWN";
    case CFM_INTERFACE_STATUS_TESTING:
        return "TESTING";
    case CFM_INTERFACE_STATUS_UNKNOWN:
        return "UNKNOWN";
    case CFM_INTERFACE_STATUS_DORMANT:
        return "DORMANT";
    case CFM_INTERFACE_STATUS_NOTPRESENT:
        return "NOT_PRESENT";
    case CFM_INTERFACE_STATUS_LLDOWN:
        return "LOWER_LAYER_DOWN";
    default:
        return "INVALID";
    };
}

static const char* DefectTypeToShortString(DefectType type)
{
    switch (type)
    {
    case DefectType::someRDIdefect:
        return "RDI";
    case DefectType::someMACstatusDefect:
        return "MAC";
    case DefectType::someRMEPCCMDefect:
        return "RMEP";
    case DefectType::errorCCMdefect:
        return "CCM";
    case DefectType::xconCCMdefect:
        return "XCON";
    default:
        return "ERROR";
    };
}

static const char* CcmIntervalToString(CcmIntervalType interval)
{
    switch (interval)
    {
    case CFM_INTERVAL_3_3_MS:
        return "3.3 ms";
    case CFM_INTERVAL_10_MS:
        return "10 ms";
    case CFM_INTERVAL_100_MS:
        return "100 ms";
    case CFM_INTERVAL_1_SEC:
        return "1 sec";
    case CFM_INTERVAL_10_SEC:
        return "10 sec";
    case CFM_INTERVAL_1_MIN:
        return "1 min";
    case CFM_INTERVAL_10_MIN:
        return "10 min";
    default:
        return "N/A";
    };
}

static const char* OamStatusToString(OamStatus status)
{
    switch (status)
    {
    case OamStatus::WAITING_WRITE_ACK:
        return "wait write ACK";
    case OamStatus::WAITING_DELETE_ACK:
        return "wait delete ACK";
    case OamStatus::ACK_WRITE_OK:
        return "OK";
    case OamStatus::ACK_DELETE_OK:
        return "delete OK";
    case OamStatus::ACK_ERROR:
        return "HW error";
    case OamStatus::INTERNAL_ERROR:
        return "internal error";
    default:
        CFM_LOG(DN_LOG_ERR, "Got invalid status %d", static_cast<int>(status));
        return "invalid status";
    };
}

static void* InitiatorSessionIterator(void* container,
                                      uint8_t* state,
                                      void* mem)
{
    auto& row = *static_cast<InitiatorSessionXrayRow*>(mem);
    auto& cfm_initiator =
        reinterpret_cast<CfmManager*>(container)->GetInstance().GetInitiator();

    CfmSession* sess = NULL;

    {
        std::scoped_lock lock(cfm_initiator.m_lock);
        auto iter = cfm_initiator.sessions.begin();
        auto end = cfm_initiator.sessions.end();
        uint32_t* sess_index = (uint32_t*)state;

        std::advance(iter, *sess_index);
        (*sess_index)++;

        if (iter == end)
        {
            return NULL;
        }

        sess = iter->second;
        sess->IncRefcount();
    }

    row.sess_id = sess->sess_id;
    row.oam_id = sess_get_oamid(sess->sess_id);
    SessionOpcode op = sess_get_opcode(sess->sess_id);
    utils_strncpy(row.opcode, sess_get_opcode_str(op), MAX_SHORT_COLUMN);

    utils_strncpy(row.state, sess_get_state_str(sess->state), MAX_SHORT_COLUMN);

    {
        std::scoped_lock lock(cfm_initiator.m_lock);
        sess->DecRefcount();
    }

    return mem;
}

static void* InitiatorSummaryIterator(void* container,
                                      uint8_t* state,
                                      void* mem)
{
    auto& row = *static_cast<InitiatorSummaryXrayRow*>(mem);
    const auto& cfm_initiator =
        reinterpret_cast<CfmManager*>(container)->GetInstance().GetInitiator();

    row.active = 0;
    row.allocated = 0;
    row.freed = 0;

    switch (*state)
    {
    case 0:
        utils_strncpy(row.cfm_entity, "Sessions", MAX_SHORT_COLUMN);
        row.active = cfm_initiator.sessions.size();
        row.allocated = CfmInitiator::sessions_allocated;
        row.freed = CfmInitiator::sessions_freed;
        row.idx = 1;
        break;

    default: //end of xray iteration
        return nullptr;
    };

    ++(*state);

    return mem;
}

static void* SummaryIterator(void* container, uint8_t* state, void* mem)
{
    auto& row = *static_cast<SummaryXrayRow*>(mem);
    const auto& cfm_oper =
        reinterpret_cast<CfmManager*>(container)->GetInstance().GetOperation();

    row.ok = 0;
    row.error = 0;
    row.pending = 0;
    row.configured = 0;
    row.discovered = 0;

    switch (*state)
    {
    case 0:
        utils_strncpy(row.cfm_entity, "MD", MAX_SHORT_COLUMN);
        row.configured = cfm_oper.data.MDs.size();
        row.ok = row.configured;
        row.error = 0;
        row.idx = 1;
        break;

    case 1:
        utils_strncpy(row.cfm_entity, "MA", MAX_SHORT_COLUMN);
        row.configured = cfm_oper.data.MAs.size();
        row.idx = 2;

        for (const auto& [_, oper] : cfm_oper.data.MAs)
        {
            if (oper.oam_info.oam_status == OamStatus::ACK_WRITE_OK
                || oper.oam_info.oam_status == OamStatus::ACK_DELETE_OK)
            {
                ++row.ok;
            }
            else if (oper.oam_info.oam_status == OamStatus::ACK_ERROR
                     || oper.oam_info.oam_status == OamStatus::INTERNAL_ERROR)
            {
                ++row.error;
            }
            else
            {
                ++row.pending;
            }
        }

        break;

    case 2:
        utils_strncpy(row.cfm_entity, "Local MEP", MAX_SHORT_COLUMN);
        row.idx = 3;

        for (const auto& [_, oper] : cfm_oper.data.Meps)
        {
            if (oper.config.direction == MepDirection::BIDIRECTIONAL)
            {
                continue;
            }

            if (oper.oam_info.oam_status == OamStatus::ACK_WRITE_OK
                || oper.oam_info.oam_status == OamStatus::ACK_DELETE_OK)
            {
                ++row.ok;
            }
            else if (oper.oam_info.oam_status == OamStatus::ACK_ERROR
                     || oper.oam_info.oam_status == OamStatus::INTERNAL_ERROR)
            {
                ++row.error;
            }
            else
            {
                ++row.pending;
            }

            ++row.configured;
        }

        break;


    case 3:
        utils_strncpy(row.cfm_entity, "Remote MEP", MAX_SHORT_COLUMN);
        row.idx = 5;
        row.discovered = cfm_oper.auto_discovery_config.current_auto_rmeps_cnt;

        for (const auto& [_, lmep_oper] : cfm_oper.data.Meps)
        {
            for (const auto& [_, oper] : lmep_oper.rmep_db)
            {
                ++row.configured;

                if (oper.oam_info.oam_status == OamStatus::ACK_WRITE_OK
                    || oper.oam_info.oam_status == OamStatus::ACK_DELETE_OK)
                {
                    ++row.ok;
                }
                else if (oper.oam_info.oam_status == OamStatus::ACK_ERROR
                         || oper.oam_info.oam_status
                                == OamStatus::INTERNAL_ERROR)
                {
                    ++row.error;
                }
                else
                {
                    ++row.pending;
                }
            }
        }

        break;

    case 4:
        utils_strncpy(row.cfm_entity, "MIP", MAX_SHORT_COLUMN);
        row.idx = 4;

        for (const auto& [_, oper] : cfm_oper.data.Meps)
        {
            if (oper.config.direction != MepDirection::BIDIRECTIONAL)
            {
                continue;
            }

            if (oper.oam_info.oam_status == OamStatus::ACK_WRITE_OK
                || oper.oam_info.oam_status == OamStatus::ACK_DELETE_OK)
            {
                ++row.ok;
            }
            else if (oper.oam_info.oam_status == OamStatus::ACK_ERROR
                     || oper.oam_info.oam_status == OamStatus::INTERNAL_ERROR)
            {
                ++row.error;
            }
            else
            {
                ++row.pending;
            }

            ++row.configured;
        }

        break;

    default: //end of xray iteration
        return nullptr;
    };

    ++(*state);

    return mem;
}

static void* MaIterator(void* container, uint8_t* state, void* mem)
{
    auto& row = *static_cast<MaXrayRow*>(mem);
    const auto& cfm_oper =
        reinterpret_cast<CfmManager*>(container)->GetInstance().GetOperation();

    auto iter = cfm_oper.data.MAs.begin();
    auto end = cfm_oper.data.MAs.end();

    uint32_t* ma_index = (uint32_t*)state;
    std::advance(iter, *ma_index);
    (*ma_index)++;

    if (iter == end)
    {
        return NULL;
    }

    auto& [_, oper] = *iter;

    utils_strncpy(row.ma_id,
                  TruncateStr(oper.config.ma_id, MAX_COLUMN_SIZE - 1).c_str(),
                  MAX_COLUMN_SIZE);
    utils_strncpy(row.ma_name, oper.config.ma_name.c_str(), MAX_COLUMN_SIZE);
    utils_strncpy(row.md_id,
                  TruncateStr(oper.config.md_id, MAX_COLUMN_SIZE - 1).c_str(),
                  MAX_COLUMN_SIZE);
    utils_strncpy(row.oam_status,
                  OamStatusToString(oper.oam_info.oam_status),
                  MAX_COLUMN_SIZE);
    utils_strncpy(row.auto_discovery,
                  oper.config.auto_discovery_enabled ? "enabled" : "disabled",
                  MAX_SHORT_COLUMN);
    row.oam_id = oper.config.oam_id;
    row.nr_defects = 0;

    for (auto oam_id : oper.local_mep_ids)
    {
        if (auto lmep = cfm_oper.data.Meps.find(oam_id);
            lmep != cfm_oper.data.Meps.end())
        {
            row.nr_defects +=
                std::ranges::count_if(lmep->second.Defects().begin(),
                                      lmep->second.Defects().end(),
                                      [](bool b) { return b; });
        }
    }

    return mem;
}

static void* MdIterator(void* container, uint8_t* state, void* mem)
{
    auto& row = *static_cast<MdXrayRow*>(mem);
    const auto& cfm_oper =
        reinterpret_cast<CfmManager*>(container)->GetInstance().GetOperation();

    auto iter = cfm_oper.data.MDs.begin();
    auto end = cfm_oper.data.MDs.end();

    uint32_t* md_index = (uint32_t*)state;
    std::advance(iter, *md_index);
    (*md_index)++;

    if (iter == end)
    {
        return NULL;
    }

    utils_strncpy(row.md_id,
                  TruncateStr(*iter, MAX_COLUMN_SIZE - 1).c_str(),
                  MAX_COLUMN_SIZE);

    return mem;
}

static void* LmepIterator(void* container, uint8_t* state, void* mem)
{
    auto& row = *static_cast<LmepXrayRow*>(mem);
    auto& cfm_oper =
        reinterpret_cast<CfmManager*>(container)->GetInstance().GetOperation();

    auto iter = cfm_oper.data.Meps.begin();
    auto end = cfm_oper.data.Meps.end();
    uint32_t* lmep_index = (uint32_t*)state;

    do
    {
        iter = cfm_oper.data.Meps.begin();
        std::advance(iter, *lmep_index);

        if (iter == end)
        {
            return NULL;
        }

        (*lmep_index)++;
    } while (iter->second.config.direction
             == MepDirection::BIDIRECTIONAL); // skip mip endpoints

    const auto& [mep, oper] = *iter;

    if (auto ma_iter = cfm_oper.data.MAs.find(oper.config.group_oam_id);
        ma_iter != cfm_oper.data.MAs.end())
    {
        utils_strncpy(row.parent_ma_name,
                      ma_iter->second.config.ma_name.c_str(),
                      MAX_COLUMN_SIZE);
        utils_strncpy(row.md_name,
                      ma_iter->second.config.md_name.c_str(),
                      MAX_COLUMN_SIZE);
    }
    else
    {
        utils_strncpy(row.parent_ma_name, "-", MAX_COLUMN_SIZE);
        utils_strncpy(row.md_name, "-", MAX_COLUMN_SIZE);
    }

    row.oam_id = mep;
    snprintf(row.hw_id, sizeof(row.hw_id), "0x%x", oper.oam_info.hw_id);
    row.mep_id = oper.config.mep_id;
    row.rmeps = oper.rmep_db.size();
    row.md_level = oper.config.md_level;
    row.admin_state = oper.config.admin_state;
    utils_strncpy(
        row.interface, oper.config.interface_name.c_str(), MAX_INTERFACE_NAME);
    utils_strncpy(row.direction,
                  oper.config.direction == MepDirection::DOWN ? "DOWN" : "UP",
                  5);
    utils_strncpy(row.ccm_interval,
                  CcmIntervalToString(oper.config.ccm_config.ccm_interval),
                  MAX_COLUMN_SIZE);
    utils_strncpy(row.oam_status,
                  OamStatusToString(oper.oam_info.oam_status),
                  MAX_COLUMN_SIZE);
    utils_strncpy(row.fng_state, ToString(oper.State()), MAX_DEFECT_NAME);
    row.vsi = oper.vsi;
    row.rdi_tx = oper.send_rdi;

    std::snprintf(row.mac_address,
                  sizeof(row.mac_address),
                  "%02X:%02X:%02X:%02X:%02X:%02X",
                  oper.src_mac_address[0],
                  oper.src_mac_address[1],
                  oper.src_mac_address[2],
                  oper.src_mac_address[3],
                  oper.src_mac_address[4],
                  oper.src_mac_address[5]);

    std::ostringstream ss;
    std::string separator{""};

    const auto& defects = oper.Defects();

    for (int i = 0; i < NUM_DEFECT_TYPES; ++i)
    {
        if (defects[i])
        {
            ss << separator
               << DefectTypeToShortString(static_cast<DefectType>(i));
            separator = ", ";
        }
    }

    const std::string defects_str = ss.str();

    if (defects_str.empty())
    {
        utils_strncpy(row.defects, "-", MAX_DEFECT_NAME);
    }
    else
    {
        utils_strncpy(row.defects, defects_str.c_str(), MAX_DEFECT_NAME);
    }

    return mem;
}

static void* MipIterator(void* container, uint8_t* state, void* mem)
{
    auto& row = *static_cast<MipXrayRow*>(mem);
    auto& cfm_oper =
        reinterpret_cast<CfmManager*>(container)->GetInstance().GetOperation();

    auto iter = cfm_oper.data.Meps.begin();
    auto end = cfm_oper.data.Meps.end();

    uint32_t* mip_index = (uint32_t*)state;

    do
    {
        iter = cfm_oper.data.Meps.begin();
        std::advance(iter, *mip_index);

        if (iter == end)
        {
            return NULL;
        }

        (*mip_index)++;
    } while (iter->second.config.direction
             != MepDirection::BIDIRECTIONAL); // skip mep endpoints

    const auto& [mep, oper] = *iter;

    if (auto ma_iter = cfm_oper.data.MAs.find(oper.config.group_oam_id);
        ma_iter != cfm_oper.data.MAs.end())
    {
        utils_strncpy(row.parent_ma_name,
                      ma_iter->second.config.ma_name.c_str(),
                      MAX_COLUMN_SIZE);
        utils_strncpy(row.md_name,
                      ma_iter->second.config.md_name.c_str(),
                      MAX_COLUMN_SIZE);
    }
    else
    {
        utils_strncpy(row.parent_ma_name, "-", MAX_COLUMN_SIZE);
        utils_strncpy(row.md_name, "-", MAX_COLUMN_SIZE);
    }

    snprintf(row.hw_id, sizeof(row.hw_id), "0x%x", oper.oam_info.hw_id);
    utils_strncpy(row.mip_name, oper.config.mip_name.c_str(), MAX_COLUMN_SIZE);
    row.md_level = oper.config.md_level;
    row.admin_state = oper.config.admin_state;
    utils_strncpy(
        row.interface, oper.config.interface_name.c_str(), MAX_INTERFACE_NAME);
    utils_strncpy(row.oam_status,
                  OamStatusToString(oper.oam_info.oam_status),
                  MAX_COLUMN_SIZE);
    row.vsi = oper.vsi;

    std::snprintf(row.mac_address,
                  sizeof(row.mac_address),
                  "%02X:%02X:%02X:%02X:%02X:%02X",
                  oper.src_mac_address[0],
                  oper.src_mac_address[1],
                  oper.src_mac_address[2],
                  oper.src_mac_address[3],
                  oper.src_mac_address[4],
                  oper.src_mac_address[5]);

    return mem;
}

static void* LmepIteratorCnt(void* container, uint8_t* state, void* mem)
{
    auto& row = *static_cast<LmepXrayCntRow*>(mem);
    auto& cfm_oper =
        reinterpret_cast<CfmManager*>(container)->GetInstance().GetOperation();

    auto iter = cfm_oper.data.Meps.begin();
    auto end = cfm_oper.data.Meps.end();

    uint32_t* lmep_index = (uint32_t*)state;

    do
    {
        std::advance(iter, *lmep_index);
        (*lmep_index)++;

        if (iter == end)
        {
            return NULL;
        }
    }
    // skip endpoint in the process of deletion
    while ((iter->second.oam_info.current_action == OamAction::DELETE)
           || (iter->second.oam_info.current_action == OamAction::RECREATE));

    auto& [mep, oper] = *iter;

    row.oam_id = mep;
    row.rx = oper.stats->ccm_in;
    row.tx = oper.stats->ccm_out;
    row.wrong_interval = oper.stats->ccms_wrong_interval;
    row.wrong_remote_mep = oper.stats->ccms_wrong_rmep;
    row.wrong_maid = oper.stats->ccms_wrong_maid;
    row.wrong_type = oper.stats->unsupported_cfm_pdu;
    row.wrong_level = oper.stats->wrong_level;
    row.passive_in = oper.stats->passive_in;
    row.passive_in_wrong_level = oper.stats->passive_in_wrong_level;
    row.unicast_mac_mismatch = oper.stats->unicast_mac_mismatch;

    return mem;
}

static void* RmepIterator(void* container, uint8_t* state, void* mem)
{
    static LMepOperIter lmep_iter;
    static RMepOperIter rmep_iter;

    auto& row = *static_cast<RmepXrayRow*>(mem);
    auto& cfm_oper =
        reinterpret_cast<CfmManager*>(container)->GetInstance().GetOperation();

    if (cfm_oper.data.Meps.empty())
    {
        return NULL;
    }

    if (*state == 0)
    {
        lmep_iter = cfm_oper.data.Meps.begin();
        rmep_iter = lmep_iter->second.rmep_db.begin();
        *state = 1;
    }

    while (rmep_iter == lmep_iter->second.rmep_db.end())
    {
        ++lmep_iter;

        if (lmep_iter == cfm_oper.data.Meps.end())
        {
            return NULL;
        }

        rmep_iter = lmep_iter->second.rmep_db.begin();
    }

    const auto& [mep, lmep_oper] = *lmep_iter;
    const auto& [rmep, oper] = *rmep_iter;

    row.mep_id = oper.config.mep_id;
    row.local_mep_oam_id = lmep_oper.config.oam_id;
    snprintf(row.hw_id, MAX_SHORT_COLUMN, "0x%x", oper.oam_info.hw_id);
    row.is_active = oper.status.is_active;
    row.RDI = oper.status.rdi;
    row.timeout_ms = oper.config.ccm_period;
    row.is_missing = oper.status.is_missing;
    row.is_discovered = oper.status.discovered;
    utils_strncpy(
        row.ma_id,
        TruncateStr(lmep_oper.config.ma_id, MAX_COLUMN_SIZE - 1).c_str(),
        MAX_COLUMN_SIZE);
    utils_strncpy(
        row.state, RmepStateToString(oper.status.state), MAX_SHORT_COLUMN);
    utils_strncpy(row.port_status,
                  PortStatusToString(oper.status.port_status),
                  MAX_SHORT_COLUMN);
    utils_strncpy(row.interface_status,
                  InterfaceStatusToString(oper.status.interface_status),
                  MAX_COLUMN_SIZE);
    utils_strncpy(row.oam_status,
                  OamStatusToString(oper.oam_info.oam_status),
                  MAX_COLUMN_SIZE);

    if (is_zero_ether_addr(&oper.status.mac_address))
    {
        utils_strncpy(row.mac_address, "N/A", MAX_COLUMN_SIZE);
    }
    else
    {
        ether_format_addr(
            row.mac_address, MAC_ADDRESS_LEN, &oper.status.mac_address);
    }

    ++rmep_iter;
    return mem;
}

static void* InterfaceIterator(void* container, uint8_t* state, void* mem)
{
    auto& cfm_cfg = reinterpret_cast<CfmManager*>(container)
                        ->GetInstance()
                        .GetConfiguration();
    auto& active_cfg = cfm_cfg.GetActiveConfig();
    auto& cfm_oper =
        reinterpret_cast<CfmManager*>(container)->GetInstance().GetOperation();
    auto& row = *static_cast<InterfaceCountRow*>(mem);

    if (*state < active_cfg.configured_interfaces.size())
    {
        auto iter = active_cfg.configured_interfaces.begin();
        std::advance(iter, *state);

        utils_strncpy(
            row.interface_name, iter->first.c_str(), MAX_INTERFACE_NAME);
        row.count_config = iter->second;

        if (auto oper_iter = cfm_oper.data.interfaces.find(iter->first);
            oper_iter != cfm_oper.data.interfaces.end())
        {
            row.count_oper = oper_iter->second;
        }
        else
        {
            row.count_oper = 0;
        }
    }
    else
    {
        auto iter = cfm_oper.data.interfaces.begin();
        std::advance(iter, *state - active_cfg.configured_interfaces.size());

        while (iter != cfm_oper.data.interfaces.end()
               && active_cfg.configured_interfaces.contains(iter->first))
        {
            ++iter;
        }

        if (iter == cfm_oper.data.interfaces.end())
        {
            return NULL;
        }

        utils_strncpy(
            row.interface_name, iter->first.c_str(), MAX_INTERFACE_NAME);
        row.count_config = 0;
        row.count_oper = iter->second;
    }

    *state += 1;
    return mem;
}

static void RegisterInitiatorSummary(CfmManager& mgr)
{
    xray_create_type(InitiatorSummaryXrayRow, NULL);
    xray_add_slot(InitiatorSummaryXrayRow, idx, uint8_t, 0);
    xray_add_slot(InitiatorSummaryXrayRow, cfm_entity, c_string_t, 0);
    xray_add_slot(InitiatorSummaryXrayRow, active, uint32_t, 0);
    xray_add_slot(InitiatorSummaryXrayRow, allocated, uint32_t, 0);
    xray_add_slot(InitiatorSummaryXrayRow, freed, uint32_t, 0);
    xray_register(InitiatorSummaryXrayRow,
                  &mgr,
                  "/cfm/initiator_summary",
                  0,
                  InitiatorSummaryIterator);
}

static void RegisterInitiatorSession(CfmManager& mgr)
{
    xray_create_type(InitiatorSessionXrayRow, NULL);
    xray_add_slot(InitiatorSessionXrayRow, sess_id, uint64_t, 0);
    xray_add_slot(InitiatorSessionXrayRow, opcode, c_string_t, 0);
    xray_add_slot(InitiatorSessionXrayRow, oam_id, uint32_t, 0);
    xray_add_slot(InitiatorSessionXrayRow, state, c_string_t, 0);
    xray_register(InitiatorSessionXrayRow,
                  &mgr,
                  "/cfm/initiator_sessions",
                  0,
                  InitiatorSessionIterator);
}

static void RegisterSummary(CfmManager& mgr)
{
    xray_create_type(SummaryXrayRow, NULL);
    xray_add_slot(SummaryXrayRow, idx, uint8_t, 0);
    xray_add_slot(SummaryXrayRow, cfm_entity, c_string_t, 0);
    xray_add_slot(SummaryXrayRow, configured, uint32_t, 0);
    xray_add_slot(SummaryXrayRow, ok, uint32_t, 0);
    xray_add_slot(SummaryXrayRow, pending, uint32_t, 0);
    xray_add_slot(SummaryXrayRow, error, uint32_t, 0);
    xray_add_slot(SummaryXrayRow, discovered, uint32_t, 0);
    xray_register(SummaryXrayRow, &mgr, "/cfm/summary", 0, SummaryIterator);
}

static void RegisterMa(CfmManager& mgr)
{
    xray_create_type(MaXrayRow, NULL);
    xray_add_slot(MaXrayRow, ma_id, c_string_t, 0);
    xray_add_slot(MaXrayRow, ma_name, c_string_t, 0);
    xray_add_slot(MaXrayRow, md_id, c_string_t, 0);
    xray_add_slot(MaXrayRow, oam_id, uint32_t, 0);
    xray_add_slot(MaXrayRow, oam_status, c_string_t, 0);
    xray_add_slot(MaXrayRow, nr_defects, uint16_t, 0);
    xray_add_slot(MaXrayRow, auto_discovery, c_string_t, 0);
    xray_register(MaXrayRow, &mgr, "/cfm/ma", 0, MaIterator);
}

static void RegisterMd(CfmManager& mgr)
{
    xray_create_type(MdXrayRow, NULL);
    xray_add_slot(MdXrayRow, md_id, c_string_t, 0);
    xray_register(MdXrayRow, &mgr, "/cfm/md", 0, MdIterator);
}

static void RegisterLmep(CfmManager& mgr)
{
    xray_create_type(LmepXrayRow, NULL);
    xray_add_slot(LmepXrayRow, md_name, c_string_t, 0);
    xray_add_slot(LmepXrayRow, parent_ma_name, c_string_t, 0);
    xray_add_slot(LmepXrayRow, mep_id, uint32_t, 0);
    xray_add_slot(LmepXrayRow, oam_id, uint32_t, 0);
    xray_add_slot(LmepXrayRow, hw_id, c_string_t, 0);
    xray_add_slot(LmepXrayRow, md_level, uint8_t, 0);
    xray_add_slot(LmepXrayRow, admin_state, bool, 0);
    xray_add_slot(LmepXrayRow, direction, c_string_t, 0);
    xray_add_slot(LmepXrayRow, interface, c_string_t, 0);
    xray_add_slot(LmepXrayRow, vsi, uint16_t, 0);
    xray_add_slot(LmepXrayRow, ccm_interval, c_string_t, 0);
    xray_add_slot(LmepXrayRow, mac_address, c_string_t, 0);
    xray_add_slot(LmepXrayRow, defects, c_string_t, 0);
    xray_add_slot(LmepXrayRow, fng_state, c_string_t, 0);
    xray_add_slot(LmepXrayRow, rdi_tx, bool, 0);
    xray_add_slot(LmepXrayRow, oam_status, c_string_t, 0);
    xray_add_slot(LmepXrayRow, rmeps, uint16_t, 0);
    xray_register(LmepXrayRow, &mgr, "/cfm/local_meps", 0, LmepIterator);
}

static void RegisterMip(CfmManager& mgr)
{
    xray_create_type(MipXrayRow, NULL);
    xray_add_slot(MipXrayRow, md_name, c_string_t, 0);
    xray_add_slot(MipXrayRow, parent_ma_name, c_string_t, 0);
    xray_add_slot(MipXrayRow, mip_name, c_string_t, 0);
    xray_add_slot(MipXrayRow, md_level, uint8_t, 0);
    xray_add_slot(MipXrayRow, hw_id, c_string_t, 0);
    xray_add_slot(MipXrayRow, admin_state, bool, 0);
    xray_add_slot(MipXrayRow, interface, c_string_t, 0);
    xray_add_slot(MipXrayRow, mac_address, c_string_t, 0);
    xray_add_slot(MipXrayRow, vsi, uint16_t, 0);
    xray_add_slot(MipXrayRow, oam_status, c_string_t, 0);
    xray_register(MipXrayRow, &mgr, "/cfm/mips", 0, MipIterator);
}

static void RegisterLmepCnt(CfmManager& mgr)
{
    xray_create_type(LmepXrayCntRow, NULL);
    xray_add_slot(LmepXrayCntRow, oam_id, uint32_t, 0);
    xray_add_slot(LmepXrayCntRow, rx, uint64_t, 0);
    xray_add_slot(LmepXrayCntRow, tx, uint64_t, 0);
    xray_add_slot(LmepXrayCntRow, wrong_level, uint64_t, 0);
    xray_add_slot(LmepXrayCntRow, wrong_interval, uint64_t, 0);
    xray_add_slot(LmepXrayCntRow, wrong_remote_mep, uint64_t, 0);
    xray_add_slot(LmepXrayCntRow, wrong_maid, uint64_t, 0);
    xray_add_slot(LmepXrayCntRow, wrong_type, uint64_t, 0);
    xray_add_slot(LmepXrayCntRow, passive_in, uint64_t, 0);
    xray_add_slot(LmepXrayCntRow, passive_in_wrong_level, uint64_t, 0);
    xray_add_slot(LmepXrayCntRow, unicast_mac_mismatch, uint64_t, 0);
    xray_register(
        LmepXrayCntRow, &mgr, "/cfm/local_meps_cnt", 0, LmepIteratorCnt);
}

static void RegisterRmep(CfmManager& mgr)
{
    xray_create_type(RmepXrayRow, NULL);
    xray_add_slot(RmepXrayRow, hw_id, c_string_t, 0);
    xray_add_slot(RmepXrayRow, mep_id, uint32_t, 0);
    xray_add_slot(RmepXrayRow, local_mep_oam_id, uint32_t, 0);
    xray_add_slot(RmepXrayRow, ma_id, c_string_t, 0);
    xray_add_slot(RmepXrayRow, is_active, bool, 0);
    xray_add_slot(RmepXrayRow, timeout_ms, uint32_t, 0);
    xray_add_slot(RmepXrayRow, state, c_string_t, 0);
    xray_add_slot(RmepXrayRow, RDI, uint8_t, 0);
    xray_add_slot(RmepXrayRow, port_status, c_string_t, 0);
    xray_add_slot(RmepXrayRow, interface_status, c_string_t, 0);
    xray_add_slot(RmepXrayRow, mac_address, c_string_t, 0);
    xray_add_slot(RmepXrayRow, is_missing, bool, 0);
    xray_add_slot(RmepXrayRow, is_discovered, bool, 0);
    xray_add_slot(RmepXrayRow, oam_status, c_string_t, 0);
    xray_register(RmepXrayRow, &mgr, "/cfm/remote_meps", 0, RmepIterator);
}

static void RegisterInterfaces(CfmManager& mgr)
{
    xray_create_type(InterfaceCountRow, NULL);
    xray_add_slot(InterfaceCountRow, interface_name, c_string_t, 0);
    xray_add_slot(InterfaceCountRow, count_config, uint32_t, 0);
    xray_add_slot(InterfaceCountRow, count_oper, uint32_t, 0);
    xray_register(
        InterfaceCountRow, &mgr, "/cfm/interfaces", 0, InterfaceIterator);
}

void cfm::CfmManagerXrayInit(CfmManager& mgr)
{
    CFM_LOG(DN_LOG_INFO, "Registering CFM xray");
    RegisterSummary(mgr);
    RegisterMd(mgr);
    RegisterMa(mgr);
    RegisterLmep(mgr);
    RegisterLmepCnt(mgr);
    RegisterRmep(mgr);
    RegisterMip(mgr);
    RegisterInterfaces(mgr);

    RegisterInitiatorSummary(mgr);
    RegisterInitiatorSession(mgr);
}
