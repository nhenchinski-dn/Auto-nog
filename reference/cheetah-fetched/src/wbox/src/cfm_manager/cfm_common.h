#ifndef _CFM_COMMON_H_
#define _CFM_COMMON_H_

#include <stdint.h>

#include "libdatapath/log_thread/log_thread.h"

enum CcmIntervalType
{
    CFM_INTERVAL_DISABLED = 0,
    CFM_INTERVAL_3_3_MS = 1,
    CFM_INTERVAL_10_MS = 2,
    CFM_INTERVAL_100_MS = 3,
    CFM_INTERVAL_1_SEC = 4,
    CFM_INTERVAL_10_SEC = 5,
    CFM_INTERVAL_1_MIN = 6,
    CFM_INTERVAL_10_MIN = 7,
};

enum FlexibleType
{
    CFM_FLEXIBLE_NONE = 1,
    CFM_FLEXIBLE_20_BYTES = 2,
    CFM_FLEXIBLE_40_BYTES = 3,
    CFM_FLEXIBLE_48_BYTES = 4,
};

enum CcmPortStatusType
{
    CFM_PORT_STATUS_NONE,
    CFM_PORT_STATUS_BLOCKED,
    CFM_PORT_STATUS_UP,
    CFM_PORT_STATUS_MAX
};

enum CcmInterfaceStatusType
{
    CFM_INTERFACE_STATUS_NONE,
    CFM_INTERFACE_STATUS_UP,
    CFM_INTERFACE_STATUS_DOWN,
    CFM_INTERFACE_STATUS_TESTING,
    CFM_INTERFACE_STATUS_UNKNOWN,
    CFM_INTERFACE_STATUS_DORMANT,
    CFM_INTERFACE_STATUS_NOTPRESENT,
    CFM_INTERFACE_STATUS_LLDOWN,
    CFM_INTERFACE_STATUS_MAX
};

enum CcmObjectType
{
    CFM_TYPE_UP_MEP,
    CFM_TYPE_DOWN_MEP,
    CFM_TYPE_REMOTE_MEP,
    CFM_TYPE_MIP,
    CFM_TYPE_OAM_GROUP,
    CFM_TYPE_MAX
};

enum cfm_oam_event_type
{
    CFM_OAM_EVENT_TYPE_PORT_DOWN,
    CFM_OAM_EVENT_TYPE_PORT_UP,
    CFM_OAM_EVENT_TYPE_INTERFACE_DOWN,
    CFM_OAM_EVENT_TYPE_INTERFACE_UP,
    CFM_OAM_EVENT_TYPE_INTERFACE_TESTING,
    CFM_OAM_EVENT_TYPE_INTERFACE_UNKNOWN,
    CFM_OAM_EVENT_TYPE_INTERFACE_DORMANT,
    CFM_OAM_EVENT_TYPE_INTERFACE_NOTPRESENT,
    CFM_OAM_EVENT_TYPE_INTERFACE_LLDOWN,
    CFM_OAM_EVENT_TYPE_CCM_TIMEOUT,
    CFM_OAM_EVENT_TYPE_CCM_TIMEIN,
    CFM_OAM_EVENT_TYPE_CCM_TIMEOUT_EARLY,
    CFM_OAM_EVENT_TYPE_REMOTE_RDI_SET,
    CFM_OAM_EVENT_TYPE_REMOTE_RDI_CLEAR,
    CFM_OAM_EVENT_TYPE_MAX
};

enum oamp_response_type
{
    CFM_OAM_RESPONSE_CREATE_GROUP,
    CFM_OAM_RESPONSE_DELETE_GROUP,
    CFM_OAM_RESPONSE_CREATE_LMEP,
    CFM_OAM_RESPONSE_DELETE_LMEP,
    CFM_OAM_RESPONSE_CREATE_RMEP,
    CFM_OAM_RESPONSE_DELETE_RMEP
};

enum cfm_counters_action
{
    CFM_COUNTERS_ACTION_START,
    CFM_COUNTERS_ACTION_STOP
};

struct cfm_endpoints_stats
{
    /*
        This struct is used by counter manager thread to update
        an endpoints counters in oper Redis. see CfmRedisClient.c
    */
    // the hw id
    uint32_t hw_id;

    // keys used to write the entries to the Redis
    char md_id[256];
    char ma_id[256];
    char mep_id[256];
    flag_t is_mip;

    // statistics
    uint64_t ccm_in;
    uint64_t ccm_out;
    uint64_t wrong_level;
    uint64_t ccms_wrong_interval;
    uint64_t ccms_wrong_rmep;
    uint64_t ccms_wrong_maid;
    uint64_t unsupported_cfm_pdu;
    uint64_t passive_in;
    uint64_t passive_in_wrong_level;
    uint64_t lbm_out;
    uint64_t lbr_in;
    uint64_t ltm_in;
    uint64_t ltm_out;
    uint64_t ltr_in;
    uint64_t ltr_out;
    uint64_t unicast_mac_mismatch;
    uint64_t wrong_level_snapshot;
    uint64_t ccms_wrong_interval_snapshot;
    uint64_t ccms_wrong_rmep_snapshot;
    uint64_t ccms_wrong_maid_snapshot;
    uint64_t unsupported_cfm_pdu_snapshot;
    uint64_t passive_in_snapshot;
    uint64_t passive_in_wrong_level_snapshot;
    uint64_t lbm_out_snapshot;
    uint64_t lbr_in_snapshot;
    uint64_t ltm_in_snapshot;
    uint64_t ltm_out_snapshot;
    uint64_t ltr_in_snapshot;
    uint64_t ltr_out_snapshot;
    uint64_t unicast_mac_mismatch_snapshot;
};

#define CFM_RATE_LIMITED_LOG_INTERVAL (10000) // every ten seconds

#define CFM_LOG(LOG_LEVEL, __msg__, ...) \
    CHEETAH_CUSTOM_LOG(LOG_LEVEL, CFM_LOGGER_ID, "CFM: " __msg__, ##__VA_ARGS__)

#define CFM_RATE_LIMITED_LOG(LOG_LEVEL, __msg__, ...)              \
    CHEETAH_RATE_LIMITED_CUSTOM_LOG(LOG_LEVEL,                     \
                                    CFM_LOGGER_ID,                 \
                                    CFM_RATE_LIMITED_LOG_INTERVAL, \
                                    "CFM: " __msg__,               \
                                    ##__VA_ARGS__)

#define GET_MEP_STRING(mep_config) \
    (mep_config.direction == MepDirection::BIDIRECTIONAL ? "MIP" : "LMEP")

#define INVALID_HW_ID (0xFFFFFFFF)

extern int g_cfm_pdu_debug_flag;
/* if you need to log the packet buffer at SDK level, please use LOG_CFM_PACKET
 * otherwise please use FMT_LOG_CFM_PACKET from CfmPackets.hpp */
#define LOG_CFM_PACKET(buffer)                                             \
    if (g_cfm_pdu_debug_flag)                                              \
    {                                                                      \
        unsigned int i;                                                    \
        unsigned int len = 0;                                              \
        unsigned int bytes_num = 110;                                      \
        char l_buffer[240] = {0};                                          \
        for (i = 0; i < bytes_num; i++)                                    \
        {                                                                  \
            len += snprintf(l_buffer + len, 240 - len, "%02x", buffer[i]); \
        }                                                                  \
        CFM_LOG(DN_LOG_INFO, "DEBUG: %s", l_buffer);                       \
    }

#define LOG_CFM_EXTRA_INFO(...)                       \
    if (g_cfm_pdu_debug_flag)                         \
    {                                                 \
        CFM_LOG(DN_LOG_DEBUG, "DEBUG: " __VA_ARGS__); \
    }


#endif // _CFM_COMMON_H_
